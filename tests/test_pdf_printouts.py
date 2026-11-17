from types import SimpleNamespace

import pytest

from common.pdf_validation import analyze_pdf
from test_multi_meter_pdf import report_page


def analyze_pages(monkeypatch, tmp_path, pages):
    from contextlib import nullcontext

    path = tmp_path / 'report.pdf'
    path.write_bytes(b'%PDF-synthetic')
    monkeypatch.setattr('pdfplumber.open', lambda _: nullcontext(SimpleNamespace(pages=pages)))
    return analyze_pdf(path)


@pytest.mark.parametrize('serials', [('76524',), ('76524', '76524', '76524'), ('76524', '69976', '69976')])
@pytest.mark.parametrize('empty_last', [False, True])
def test_checks_every_printout(monkeypatch, tmp_path, serials, empty_last):
    pages = [report_page(serial) for serial in serials]
    if empty_last:
        pages[-1] = report_page(serials[-1], quantity=None)
    result = analyze_pages(monkeypatch, tmp_path, pages)
    assert result['calculator_number'] == '76524'
    assert result['checked_rows'] == 2 * len(pages)
    assert len(result['series']) == len(pages)
    assert result['state'] == ('rejected' if empty_last else 'validated')
    if empty_last:
        assert any(issue['code'] == 'empty_required_measurement' for issue in result['issues'])


@pytest.mark.parametrize('serials', [('76524', '76524', '76524'), ('76524', '69976', '69976')])
def test_unextracted_measurements_on_last_page_block(monkeypatch, tmp_path, serials):
    pages = [report_page(serial) for serial in serials]
    original = pages[-1].extract_text()
    pages[-1] = SimpleNamespace(
        extract_text=lambda: original + '\n01.09.2026 10 ---\n02.09.2026 10 0',
        extract_tables=lambda: [],
    )
    result = analyze_pages(monkeypatch, tmp_path, pages)
    assert result['state'] == 'review_required'
    assert any(issue.get('page') == 3 for issue in result['issues'])


@pytest.mark.parametrize('repeated_header', [False, True])
@pytest.mark.parametrize('empty_last', [False, True])
def test_one_printout_continues_on_next_page(monkeypatch, tmp_path, repeated_header, empty_last):
    first = report_page('76524', days=(1, 2), period=(1, 4))
    continuation = report_page('76524', days=(3, 4), period=(1, 4), quantity=None if empty_last else '0')
    tables = continuation.extract_tables()
    if not repeated_header:
        tables[0].pop(0)
    continuation = SimpleNamespace(
        extract_text=lambda: 'Продолжение ведомости' if repeated_header else 'Дата t1 Q\n03.09.2026 10 0\n04.09.2026 10 0',
        extract_tables=lambda: tables,
    )
    result = analyze_pages(monkeypatch, tmp_path, [first, continuation])
    assert result['state'] == ('rejected' if empty_last else 'validated')
    assert result['checked_rows'] == 4
    assert len(result['series']) == 1
    if empty_last:
        assert any(issue['code'] == 'empty_required_measurement' for issue in result['issues'])


def test_separate_incomplete_printouts_cannot_fill_each_other(monkeypatch, tmp_path):
    pages = [report_page('76524', days=(1,)), report_page('76524', days=(2,))]
    result = analyze_pages(monkeypatch, tmp_path, pages)
    assert result['state'] == 'rejected'
    assert len(result['series']) == 2
    assert sum(issue['code'] == 'missing_daily_dates' for issue in result['issues']) == 2


def test_closed_printout_cannot_be_completed_from_next_page(monkeypatch, tmp_path):
    page = report_page('76524', days=(1,))
    closed_table = [*page.extract_tables()[0], ['Итого', '10', '0']]
    closed = SimpleNamespace(extract_text=page.extract_text, extract_tables=lambda: [closed_table])
    tail = SimpleNamespace(
        extract_text=lambda: '02.09.2026 10 0',
        extract_tables=lambda: [[['02.09.2026', '10', '0']]],
    )
    result = analyze_pages(monkeypatch, tmp_path, [closed, tail])
    assert result['state'] == 'rejected'
    assert any(issue['code'] == 'missing_daily_dates' for issue in result['issues'])


def repeated_meter_tail(quantity='0'):
    data = report_page('76524', days=(3, 4), period=(1, 4), quantity=quantity)
    return SimpleNamespace(
        extract_text=lambda: 'Прибор: ВКТ-7 Заводской номер: 76524\nДата t1 Q',
        extract_tables=data.extract_tables,
    )


@pytest.mark.parametrize('quantity', ['0', None])
def test_repeated_meter_without_continuation_marker(monkeypatch, tmp_path, quantity):
    first = report_page('76524', days=(1, 2), period=(1, 4))
    result = analyze_pages(monkeypatch, tmp_path, [first, repeated_meter_tail(quantity)])
    assert len(result['series']) == 1
    assert result['checked_rows'] == 4
    assert result['state'] == ('validated' if quantity is not None else 'rejected')
    if quantity is None:
        assert any(issue['code'] == 'empty_required_measurement' for issue in result['issues'])


@pytest.mark.parametrize('heading', ['Дата t2 Q2', ''])
def test_same_width_headerless_incompatible_or_unproven(monkeypatch, tmp_path, heading):
    first = report_page('76524', days=(1, 2), period=(1, 4))
    tail = SimpleNamespace(
        extract_text=lambda: heading + '\n03.09.2026 10 0\n04.09.2026 10 0',
        extract_tables=lambda: [[['03.09.2026', '10', '0'], ['04.09.2026', '10', '0']]],
    )
    result = analyze_pages(monkeypatch, tmp_path, [first, tail])
    assert result['state'] == 'review_required'
    assert result['series'][0]['checked_rows'] == 2
    assert any(issue['code'] == 'continuation_unverified' for issue in result['issues'])


def test_independent_same_meter_printouts_with_own_period(monkeypatch, tmp_path):
    pages = [report_page('76524'), report_page('76524')]
    result = analyze_pages(monkeypatch, tmp_path, pages)
    assert result['state'] == 'validated'
    assert [series['pages'] for series in result['series']] == [[1], [2]]
    assert result['checked_rows'] == 4


def test_new_printout_title_starts_a_new_report_with_same_meter(monkeypatch, tmp_path):
    first = report_page('76524', days=(1, 2), period=(1, 4))
    tail = repeated_meter_tail()
    next_report = SimpleNamespace(
        extract_text=lambda: 'Отчёт о параметрах\n' + tail.extract_text(),
        extract_tables=tail.extract_tables,
    )
    result = analyze_pages(monkeypatch, tmp_path, [first, next_report])
    assert result['state'] != 'validated'
    assert [report['pages'] for report in result['series']] == [[1], [2]]
