import pytest

from common.pdf_validation import analyze_capture, analyze_pdf
from test_multi_meter_pdf import report_page
from test_pdf_printouts import analyze_pages


REPORT = 'Отчёт о суточных параметрах\nЗаводской номер: 0098765\nс 01.09.2026 по 03.09.2026'


@pytest.mark.parametrize('count', [1, 4, 37, 81])
def test_arbitrary_columns_are_all_checked(count):
    table = [['Дата', *[f'Величина {i}' for i in range(count)], 'НС'],
             [None, *['м³/ч' if i % 2 else 'кг/м²' for i in range(count)], None],
             *[[f'0{day}.09.2026', *['−0,250' if i % 2 else '0' for i in range(count)], None]
               for day in range(1, 4)]]
    result = analyze_capture(REPORT, [table])
    assert result['state'] == 'validated'
    assert result['checked_rows'] == 3 and result['checked_measurements'] == 3 * count
    assert result['series'][0]['required_columns'] == list(range(1, count + 1))
    assert result['series'][0]['optional_columns'] == [count + 1]


@pytest.mark.parametrize('change, code, expected', [
    ('missing_day', 'missing_daily_dates', 'rejected'),
    ('duplicate_day', 'duplicate_daily_dates', 'review_required'),
    ('empty_last_column', 'empty_required_measurement', 'rejected'),
    ('merged_values', 'multiple_values_in_measurement_cell', 'review_required'),
    ('short_row', 'unexpected_column_count', 'review_required'),
    ('split_values', 'multiline_columns_not_aligned', 'review_required'),
    ('wrong_merge', 'dated_row_after_summary', 'review_required'),
    ('unknown_date_column', 'date_column_header_unrecognized', 'review_required'),
])
def test_bad_data_and_damaged_layout_block(change, code, expected):
    table = [['Дата', 'Новый объём', 'Новый расход'], ['01.09.2026', '0', '-3'],
             ['02.09.2026', '1', '-4'], ['03.09.2026', '2', '-5']]
    if change == 'missing_day':
        table.pop(2)
    elif change == 'duplicate_day':
        table.append(table[-1][:])
    elif change == 'empty_last_column':
        table[3][-1] = None
    elif change == 'merged_values':
        table[3][-1] = '2 -5'
    elif change == 'short_row':
        table[3].pop()
    elif change == 'split_values':
        table[3][-1] = '12\n23'
    elif change == 'wrong_merge':
        table.insert(3, ['Итого', '3', '-12'])
    else:
        table[0][0] = 'Несопоставимый столбец'
    result = analyze_capture(REPORT, [table])
    assert result['state'] == expected
    issue = next(issue for issue in result['issues'] if issue['code'] == code)
    assert issue['table'] == 0
    if change == 'empty_last_column':
        assert (issue['row'], issue['day'], issue['column']) == (4, '2026-09-03', 2)


def test_counter_caption_does_not_require_same_measurement_names():
    text = (REPORT + '\n01.09.2026 -3\n02.09.2026 0\n03.09.2026 3\n'
            'Показания (нарастающим итогом)\n31.08.2026 0\n03.09.2026 3')
    daily = [['Дата', 'С суточной сетки'], ['01.09.2026', '-3'], ['02.09.2026', '0'], ['03.09.2026', '3']]
    cumulative = [['Дата', 'Параметр Y'], ['31.08.2026', '0'], ['03.09.2026', '3']]
    result = analyze_capture(text, [daily, cumulative])
    assert result['state'] == 'validated'
    assert [(s['kind'], s['checked_measurements']) for s in result['series']] == [('daily', 3), ('cumulative', 2)]


def test_incomplete_second_daily_table_cannot_become_counter():
    daily = [['Дата', 'Величина A', 'Величина B'], ['01.09.2026', '1', '2'],
             ['02.09.2026', '0', '2'], ['03.09.2026', '1', '2']]
    second = [['Дата', 'Величина A'], ['01.09.2026', '2'], ['03.09.2026', '5']]
    result = analyze_capture(REPORT, [daily, second])
    assert result['state'] == 'rejected'
    assert [s['kind'] for s in result['series']] == ['daily', 'daily']
    assert any(i['code'] == 'missing_daily_dates' and i['dates'] == ['2026-09-02'] for i in result['issues'])


def test_printed_counter_with_opening_on_period_start():
    daily = [['Дата', 'Параметр A', 'Параметр B'], ['01.09.2026', '1', '2'],
             ['02.09.2026', '0', '2'], ['03.09.2026', '1', '2'], ['Средние', '0', '2'], ['Итого', '2', '6']]
    counters = [['Дата', 'Параметр A'], ['01.09.2026', '2'], ['03.09.2026', '4'], ['Итого', '2']]
    result = analyze_capture(REPORT, [daily, counters])
    assert result['state'] == 'validated' and result['checked_measurements'] == 8
    assert [s['kind'] for s in result['series']] == ['daily', 'cumulative']


def test_unextracted_additional_table_is_not_a_pass(monkeypatch, tmp_path):
    from types import SimpleNamespace

    page = report_page('0098765')
    text = page.extract_text() + '\n01.09.2026 0 -3\n02.09.2026 0 1\n31.08.2026 --- ---\n02.09.2026 8'
    result = analyze_pages(monkeypatch, tmp_path, [SimpleNamespace(extract_text=lambda: text,
                                                                  extract_tables=page.extract_tables)])
    assert result['state'] == 'review_required' and result['checked_rows'] == 2
    assert result['issues'][0]['dates'] == {'31.08.2026': 1, '02.09.2026': 1}


def test_unreadable_pdf_goes_to_review(tmp_path):
    path = tmp_path / 'broken.pdf'
    path.write_bytes(b'%PDF-1.7\n')
    result = analyze_pdf(path)
    assert result['state'] == 'review_required'
    assert result['reason'] == 'pdf_extraction_failed' and result['issues'][0]['page'] == 1
