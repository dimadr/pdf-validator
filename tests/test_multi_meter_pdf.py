"""PDF page checks, exact object binding and the actual delivery guard."""
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from common.delivery import deliver_recipient
from common.models import DeliveryIntent, Object, ProcessingRevision, RecipientDelivery
from common.pipeline import validate_revision
from common.service import request_resend


def report_page(serial, days=(1, 2), quantity='0', period=(1, 2)):
    draft = f'Прибор: ВКТ-7 Заводской номер: {serial}\n({period[0]:02}.09.2026 0:00:00 - {period[1]:02}.09.2026 0:00:00)'
    table = [['Дата', 't1', 'Q'], *[[f'{day:02}.09.2026', '10', quantity] for day in days]]
    return SimpleNamespace(extract_text=lambda: draft, extract_tables=lambda: [table])


def set_pdf(monkeypatch, pages):
    from contextlib import nullcontext

    monkeypatch.setattr('pdfplumber.open', lambda path: nullcontext(SimpleNamespace(pages=pages)))


@pytest.mark.parametrize(
    'case,state,reason',
    [
        ('single', 'validated', 'validated'),
        ('shared', 'validated', 'validated'),
        ('different', 'review_required', 'meters_belong_to_different_objects'),
        ('unknown', 'review_required', 'exact_object_not_found'),
        ('duplicate', 'review_required', 'ambiguous_object_binding'),
        ('missing_day', 'rejected', 'missing_daily_dates'),
        ('empty', 'rejected', 'empty_required_measurement'),
        ('different_periods', 'review_required', 'multiple_declared_periods'),
        ('unlabelled', 'review_required', 'identity_ambiguous'),
    ],
)
def test_pdf_meter_routing(session_factory, seed, monkeypatch, case, state, reason):
    record = seed(state='received')
    first = report_page('0012345')
    second = report_page('0098765', days=(1,) if case == 'missing_day' else (1, 2),
                         quantity=None if case == 'empty' else '0')
    if case == 'different_periods':
        second = report_page('0098765', days=(2, 3), period=(2, 3))
    pages = [first] if case == 'single' else [first, second]
    if case == 'unlabelled':
        pages.append(report_page(''))
    set_pdf(monkeypatch, pages)
    other = seed(state='received') if case in ('different', 'duplicate') else None
    with session_factory.begin() as db:
        obj = db.get(Object, record['object'])
        obj.calculator_number = {
            'different': '0012345',
            'unknown': '0012345, 0098765A',
        }.get(case, ' 0098765, 0012345 ')
        if other:
            db.get(Object, other['object']).calculator_number = (
                '0098765' if case == 'different' else '0098765, 555555'
            )

    assert validate_revision(session_factory, record['revision'])['state'] == state
    with session_factory() as db:
        revision = db.get(ProcessingRevision, record['revision'])
        assert revision.reason == reason
        deliveries = list(db.scalars(select(RecipientDelivery)))
        intents = list(db.scalars(select(DeliveryIntent)))
        assert len(intents) == 1
        if state == 'validated':
            assert revision.object_id == record['object']
            expected = ['0012345'] if case == 'single' else ['0012345', '0098765']
            assert revision.validation_result['object_meter_serials'] == expected
            assert {series['meter'] for series in revision.validation_result['series']} == set(expected)
            assert sorted((row.role, row.recipient) for row in deliveries) == [
                ('client', 'one@example.invalid'), ('client', 'two@example.invalid'),
            ]
        else:
            assert deliveries and all(row.role == 'admin' for row in deliveries)
            if state == 'rejected':
                assert any(issue.get('meter') == '0098765' and issue['code'] == reason
                           for issue in revision.validation_result['issues'])


@pytest.mark.parametrize('changed', [False, True])
def test_pdf_binding_rechecked_before_smtp(session_factory, seed, monkeypatch, changed):
    record = seed(state='received', recipients='client@example.invalid')
    set_pdf(monkeypatch, [report_page('0012345'), report_page('0098765')])
    with session_factory.begin() as db:
        db.get(Object, record['object']).calculator_number = '0012345, 0098765'
    assert validate_revision(session_factory, record['revision'])['role'] == 'client'
    with session_factory.begin() as db:
        delivery = db.scalar(select(RecipientDelivery.id))
        request_resend(db, record['attachment'], 'multi-meter-retry', record['revision'])

    def change_binding(_):
        if changed:
            with session_factory.begin() as db:
                db.get(Object, record['object']).calculator_number = '0012345'

    sent = []
    transport = SimpleNamespace(send=lambda work: sent.append((work['recipient'], work['content'])))
    result = deliver_recipient(session_factory, delivery, transport, before_smtp=change_binding)
    if changed:
        assert result == {'state': 'cancelled', 'reason': 'object_binding_changed'}
        assert sent == []
    else:
        assert result['state'] == 'accepted'
        assert sent == [('client@example.invalid', record['path'].read_bytes())]
