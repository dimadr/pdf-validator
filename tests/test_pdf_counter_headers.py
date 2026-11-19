import pytest

from common.pdf_validation import analyze_capture


COUNTER_HEADERS = [
    'Дата', 'V1 М³', 'V2 М³', 'M1 т', 'M2 т', 'Mтс т', 'Mгв т',
    'W1 ГКал', 'W2 ГКал', 'Wтс ГКал', 'Wгв ГКал', 'Tнар ч',
]


@pytest.mark.parametrize('case, state', [
    ('valid', 'validated'),
    ('empty_measurement', 'rejected'),
    ('unknown_column', 'validated'),
])
def test_tsrv_cumulative_quantities(case, state):
    text = 'ТСРВ-026М №1303637\n(01.09.2026 0:00:00 - 02.09.2026 0:00:00)'
    headers = list(COUNTER_HEADERS)
    counters = [headers, ['31.08.2026', *['0'] * 11], ['02.09.2026', *['1'] * 11]]
    if case == 'empty_measurement':
        counters[2][5] = None
    elif case == 'unknown_column':
        headers[5] = 'X9 т'
    daily = [[*headers, 't1'], ['01.09.2026', *['0'] * 12], ['02.09.2026', *['0'] * 12]]

    result = analyze_capture(text, [daily, counters])
    assert result['state'] == state
    assert result['calculator_number'] == '1303637'
    codes = {issue['code'] for issue in result['issues']}
    series = result['series'][1]
    assert series['kind'] == 'cumulative' and series['checked_rows'] == 2
    assert series['required_columns'] == list(range(1, len(headers)))
    assert series['checked_measurements'] == 22
    assert ('empty_required_measurement' in codes) == (case == 'empty_measurement')
