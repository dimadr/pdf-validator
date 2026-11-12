import hashlib
import io
import os
from collections import Counter
from datetime import date
from email import message_from_bytes, policy
from email.message import EmailMessage
from pathlib import Path
from uuid import UUID

import pytest
import xlwt
from sqlalchemy import select

from common.delivery import deliver_recipient
from common.ingest import BlobStore, ingest_uid, mime_manifest
from common.models import Attachment, DeliveryIntent, EmailSource, Object, ProcessingRevision, RecipientDelivery
from common.pipeline import validate_revision
from common.service import request_reprocess
from common.smtp_transport import SMTPTransport
from common.xls_validation import analyze_xls


SERIAL = '00999991'
REVIEW_EMAIL = 'xls-review@example.invalid'
SOURCE_EMAIL = 'eldis@example.invalid'


@pytest.fixture(autouse=True)
def configure_review_email(monkeypatch):
    monkeypatch.setenv('XLS_REVIEW_EMAIL', REVIEW_EMAIL)
HEADERS = ['Дата', 't1', 'Vp1', 'VрDС1', 'Vс1', 'P1', 'Pб', 'Ro', 'N2', 'CO2', 'BHP', 'ВОС', 'NS']
UNITS = ['', '°C', 'м³', 'm3', 'м3', 'МПа', 'кг/см2', 'кг/м3', '%', '%', 'ч', 'ч', '']
DAILY = [12, 10000, 10000, 50000, .05, 1.01, .69, .38, .37, 24, 0, '*']


def workbook(*, changes=None, header=HEADERS, serial=SERIAL, day_count=1, excel_dates=False):
    book = xlwt.Workbook()
    sheet = book.add_sheet('Page 1')
    sheet.write(0, 0, f'Отчет о суточных параметрах газоснабжения\n(01.09.2026 0:00:00 - {day_count:02}.09.2026 0:00:00)')
    sheet.write(1, 0, 'Объект: Тестовый газовый объект')
    sheet.write(2, 0, f'Прибор: ВКГ-3Т Заводской номер: {serial}')
    sheet.write(3, 0, 'Точка измерения: УУ2; Ресурс: Газ')
    sheet.write(4, 0, 'Схема измерений: Труба Т1')
    for col, (name, unit) in enumerate(zip(header, UNITS)):
        sheet.write(6, col, name)
        sheet.write(7, col, unit)
    for index in range(day_count):
        row = 8 + index
        day = index + 1
        if excel_dates:
            sheet.write(row, 0, date(2026, 9, day), xlwt.easyxf(num_format_str='DD.MM.YYYY'))
        else:
            sheet.write(row, 0, f'{day:02}.09.2026')
        values = DAILY.copy()
        for col, value in (changes or {}).items():
            values[col - 1] = value
        for col, value in enumerate(values, 1):
            if value is not None:
                sheet.write(row, col, value)
    for row, label in [(8 + day_count, 'Средние:'), (9 + day_count, 'Итого:')]:
        sheet.write(row, 0, label)
        for col in range(1, 12):
            sheet.write(row, col, 999999 if col in (2, 3, 10) else '---')
    stream = io.BytesIO()
    book.save(stream)
    return stream.getvalue()


def check(tmp_path, data):
    path = tmp_path / 'report.xls'
    path.write_bytes(data)
    return analyze_xls(str(path))


@pytest.mark.parametrize(('col', 'value', 'code'), [
    (2, -1, 'negative_volume'), (3, '−1,25', 'negative_volume'), (4, -1, 'negative_volume'),
    (2, '10\u00a0000,01', 'volume_above_10000'), (3, 10000.01, 'volume_above_10000'),
    (10, 24.01, 'hours_above_24'), (1, '—', 'missing_measurement'),
    (2, '---', 'missing_measurement'), (8, None, 'missing_measurement'),
    (5, '1 2', 'unreadable_measurement'),
])
def test_daily_rules(tmp_path, col, value, code):
    result = check(tmp_path, workbook(changes={col: value}))
    assert [issue['code'] for issue in result['issues']] == [code]
    assert result['issues'][0]['date'] == '2026-09-01'
    assert result['state'] == ('review_required' if code == 'unreadable_measurement' else 'rejected')


def test_limits_zero_date_cells_and_totals(tmp_path):
    result = check(tmp_path, workbook(changes={1: -15, 5: 0, 7: 0, 12: None}, excel_dates=True))
    assert result['state'] == 'validated' and not result['issues']
    assert result['calculator_number'] == SERIAL and result['checked_rows'] == 1


@pytest.mark.parametrize(('data', 'reason'), [
    (lambda: b'not a workbook', 'xls_unreadable'),
    (lambda: workbook(header=[*HEADERS[:5], 'Unknown', *HEADERS[6:]]), 'xls_structure_unknown'),
    (lambda: workbook(serial=''), 'xls_identity_unknown'),
])
def test_fail_closed(tmp_path, data, reason):
    result = check(tmp_path, data())
    assert result['state'] == 'review_required'
    assert reason in {issue['code'] for issue in result['issues']}


@pytest.mark.parametrize(('name', 'state', 'days', 'issues'), [
    ('Правильный.xls', 'validated', 30, {}),
    ('С ошибкой.xls', 'rejected', 31, {'negative_volume': 3, 'volume_above_10000': 3, 'hours_above_24': 3}),
])
def test_owner_samples(name, state, days, issues):
    folder = os.getenv('ELDIS_XLS_SAMPLE_DIR')
    if not folder:
        pytest.skip('ELDIS_XLS_SAMPLE_DIR required for the two owner-provided files')
    result = analyze_xls(str(Path(folder) / name))
    assert result['state'] == state and result['checked_rows'] == days
    assert result['calculator_number'] and result['identity_state'] == 'unique'
    assert Counter(issue['code'] for issue in result['issues']) == issues


def _message(*attachments):
    mail = EmailMessage()
    mail['From'] = SOURCE_EMAIL
    mail['To'] = 'processor@example.invalid'
    mail.set_content('Synthetic report')
    for name, content, subtype in attachments:
        mail.add_attachment(content, maintype='application', subtype=subtype, filename=name)
    return mail.as_bytes()


def test_workbook_with_pdf_label_still_routes_as_xls():
    raw = _message(('mislabeled.pdf', workbook(), 'pdf'))
    _, parts, _ = mime_manifest(raw)
    assert [part['format'] for part in parts] == ['xls']


def _setup(factory, seed, *, active=True, serial=SERIAL):
    record = seed(active=active, recipients='client@example.invalid')
    with factory.begin() as db:
        db.get(EmailSource, record['source']).email = SOURCE_EMAIL
        db.get(Object, record['object']).calculator_number = serial


def _ingest(factory, tmp_path, data, uid=1):
    result = ingest_uid(factory, BlobStore(tmp_path / 'mail'), 'account', 'INBOX', 1, uid, data)
    assert result['state'] == 'complete'
    with factory() as db:
        attachments = db.scalars(select(Attachment).where(Attachment.message_id == UUID(result['message_id'])))
        return {row.filename: (row.id, row.current_revision_id) for row in attachments}


class _CapturedSMTP:
    def __init__(self, monkeypatch):
        self.messages = []
        sink = self

        class Connection:
            def __init__(self, *args, **kwargs):
                pass

            def sendmail(self, sender, recipients, content):
                sink.messages.append((recipients, message_from_bytes(content, policy=policy.default)))
                self.submission_reply = (250, b'local synthetic acknowledgement')
                return {}

            def quit(self):
                pass

        monkeypatch.setattr('common.smtp_transport.RecordedSMTP', Connection)
        monkeypatch.setattr('common.smtp_transport.credentials', lambda provider: {
            'host': 'localhost', 'port': 25, 'sender': 'sender@example.invalid',
            'reply_to': 'sender@example.invalid', 'user': None, 'password': '', 'test': True,
        })


def test_mixed_mail_routes_originals_and_never_calls_ai_for_xls(session_factory, seed, tmp_path, monkeypatch):
    _setup(session_factory, seed)
    good = workbook()
    bad = workbook(changes={1: '---', 2: -3, 3: 10001, 4: -2, 5: '—', 10: 25}, day_count=2)
    raw = _message(('valid.xls', good, 'pdf'), ('invalid.xls', bad, 'octet-stream'),
                   ('old.pdf', b'%PDF-synthetic', 'pdf'))
    items = _ingest(session_factory, tmp_path, raw)
    assert set(items) == {'valid.xls', 'invalid.xls', 'old.pdf'}
    ai_calls = []
    monkeypatch.setattr('common.ai_fallback.run_fallback', lambda *args: ai_calls.append(args[1]) or args[2])
    for name, state in [('valid.xls', 'validated'), ('invalid.xls', 'rejected'), ('old.pdf', 'validated')]:
        revision = items[name][1]
        result = validate_revision(session_factory, revision, analyzer=lambda *a, **kw: {
            'state': 'validated', 'calculator_number': SERIAL, 'identity_state': 'unique',
        })
        assert result['state'] == state
        assert validate_revision(session_factory, revision)['state'] == 'skipped'
    assert ai_calls == [items['old.pdf'][1]]
    with session_factory() as db:
        intents = {intent.revision_id: intent for intent in db.scalars(select(DeliveryIntent))}
        assert set(intents) == {revision for _, revision in items.values()}
        pending = {r.intent_id: r for r in db.scalars(select(RecipientDelivery))}
        assert len(pending) == 3
        for name, address in [('valid.xls', 'client@example.invalid'), ('invalid.xls', REVIEW_EMAIL),
                              ('old.pdf', 'client@example.invalid')]:
            recipient = pending[intents[items[name][1]].id]
            assert recipient.recipient == address
            assert recipient.role == ('admin' if name == 'invalid.xls' else 'client')
        bad_revision = db.get(ProcessingRevision, items['invalid.xls'][1])
        assert len(bad_revision.validation_result['issues']) == 12
        for date_text in ('01.09.2026', '02.09.2026'):
            assert intents[bad_revision.id].payload['body'].count(date_text) == 6
        delivery_ids = [r.id for r in pending.values()]
    capture = _CapturedSMTP(monkeypatch)
    for delivery_id in delivery_ids:
        assert deliver_recipient(session_factory, delivery_id, SMTPTransport())['state'] == 'accepted'
    sent = {next(msg.iter_attachments()).get_content_type() + ':' + recipients[0]: msg
            for recipients, msg in capture.messages}
    for address, original in [('client@example.invalid', good), (REVIEW_EMAIL, bad)]:
        part = next(sent['application/vnd.ms-excel:' + address].iter_attachments())
        assert part.get_payload(decode=True) == original
        assert part.get_filename() == ('valid.xls' if address.startswith('client') else 'invalid.xls')
    assert next(sent['application/pdf:client@example.invalid'].iter_attachments()).get_payload(decode=True) == b'%PDF-synthetic'
    assert _ingest(session_factory, tmp_path, raw, uid=2) == {}
    with session_factory.begin() as db:
        retry = request_reprocess(db, items['invalid.xls'][0], 'regression-reprocess')
        retry_id = retry.id
    assert validate_revision(session_factory, retry_id)['state'] == 'rejected'
    with session_factory() as db:
        assert len(list(db.scalars(select(RecipientDelivery)))) == 3
        assert db.get(Attachment, items['invalid.xls'][0]).file_sha256 == hashlib.sha256(bad).hexdigest()


@pytest.mark.parametrize(('content', 'active', 'registered_serial', 'state'), [
    (lambda: b'unreadable .xls', True, SERIAL, 'review_required'),
    (workbook, True, '999991', 'review_required'),
    (workbook, False, SERIAL, 'review_required'),
    (lambda: workbook(changes={2: -1}), False, SERIAL, 'rejected'),
])
def test_xls_manual_review_has_one_admin_and_keeps_original(
    session_factory, seed, tmp_path, monkeypatch, content, active, registered_serial, state,
):
    _setup(session_factory, seed, active=active, serial=registered_serial)
    data = content()
    items = _ingest(session_factory, tmp_path, _message(('review.xls', data, 'vnd.ms-excel')))
    revision_id = items['review.xls'][1]
    assert validate_revision(session_factory, revision_id)['state'] == state
    with session_factory() as db:
        delivery, = db.scalars(select(RecipientDelivery)).all()
        assert (delivery.role, delivery.recipient) == ('admin', REVIEW_EMAIL)
        delivery_id = delivery.id
    capture = _CapturedSMTP(monkeypatch)
    assert deliver_recipient(session_factory, delivery_id, SMTPTransport())['state'] == 'accepted'
    assert next(capture.messages[0][1].iter_attachments()).get_payload(decode=True) == data
