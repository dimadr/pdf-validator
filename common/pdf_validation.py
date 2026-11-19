"""Versioned, side-effect-free PDF format adapters for Project1.

No task, broker, database or network imports. Object resolution belongs to the
caller and must use the complete identity returned here.
"""
from __future__ import annotations

import calendar
from collections import Counter
from datetime import date, datetime, timedelta
from pathlib import Path
import re
import unicodedata

ADAPTER_VERSION = 'project1-formats/2.1'
DATE_TOKEN = r'\d{2}[./]\d{2}[./](?:\d{4}|\d{2})(?!\d)'
TIME_TOKEN = r'\d{1,2}:\d{2}(?::\d{2})?'
SERIAL = r'([0-9][0-9A-Za-zА-Яа-я]*(?:-[0-9A-Za-zА-Яа-я]+)*)(?![0-9A-Za-zА-Яа-я_-])'
EMPTY = {'', '-', '–', '—', '---', 'нд', 'н/д'}
SERVICE_HEADERS = {'нс', 'ns', 'коднс', 'отказы', 'примечание', 'нештатныеситуации'}
DATE_PREFIX = re.compile(r'^\d{1,2}[./]\d{1,2}[./]')
TOTAL_HEADERS = ('итого', 'средние', 'среднее', 'сумма')
CUMULATIVE_CAPTION = re.compile(r'нарастающ\w+\s+итог\w*|накопительн\w+\s+показан\w*', re.I)
REPORT_TITLE = re.compile(r'^\s*(?:отч[её]т(?![ \t]+(?:сформирован|принят))|ведомость|распечатка)\b', re.I | re.M)
NUMBER_TOKEN = r'[+−-]?\d+(?:[.,]\d+)?'
GROUPED_NUMBER = re.compile(r'[+−-]?\d{1,3}(?: \d{3})+(?:[.,]\d+)?')


def _issue(code, *, severity='rejected', **details):
    return {'code': code, 'severity': severity, **details}


def _label(value):
    return re.sub(r'[^a-zа-яё0-9]', '', _heading_key(value))


def _heading_key(value):
    return ' '.join(unicodedata.normalize('NFKC', _text(value)).casefold().split())


def _text(value):
    return str(value if value is not None else '').replace('\u00a0', ' ').replace('\u202f', ' ').strip()


def _empty(value):
    value = _text(value)
    return value.lower() in EMPTY or bool(re.fullmatch(r'_+', value))


def _parse_date(value, context=None):
    value = _text(value)
    if re.fullmatch(r'\d{4}-\d{2}-\d{2}', value):
        return date.fromisoformat(value)
    if not re.fullmatch(DATE_TOKEN, value):
        raise ValueError('unsupported_date')
    day, month, year = map(int, re.split(r'[./]', value))
    if len(re.split(r'[./]', value)[-1]) == 2:
        # The application's current records are contemporary. Caller context
        # determines the century; without it the documented report scope is 2000+.
        anchor = (context or {}).get('received_at')
        anchor_year = getattr(anchor, 'year', None)
        if anchor_year is None and anchor:
            try:
                anchor_year = datetime.fromisoformat(str(anchor).replace('Z', '+00:00')).year
            except ValueError:
                anchor_year = None
        year += ((anchor_year or 2000) // 100) * 100
    return date(year, month, day)


def _stamp(value, context=None):
    value = _text(value).replace('\n', ' ')
    match = re.fullmatch(rf'({DATE_TOKEN})(?:\s+(\d{{1,2}}(?::\d{{2}}(?::\d{{2}})?)?))?', value)
    if not match:
        return None
    try:
        day = _parse_date(match.group(1), context)
    except ValueError:
        return None
    clock = match.group(2)
    if clock:
        parts = [int(v) for v in clock.split(':')]
        if parts[0] > 24 or any(v > 59 for v in parts[1:]) or (parts[0] == 24 and any(parts[1:])):
            return None
    return day, clock


def _identity(text, *, document_order=False):
    labels = [
        r'Заводской\s*номер\s*(?:[:=]\s*)?',
        r'Номер\s*прибора\s*(?:[:=]\s*)?',
        r'Теплосчетчик\s+МКТС\s*[:=]?\s*№\s*',
        r'ТСРВ[-\s][^\n№]*№\s*',
        r'Тепловычислитель\s+СПТ94[^\n]*?идентификатор\s+ИД\s*=\s*',
    ]
    positions = {}
    for label in labels:
        for match in re.finditer(label + SERIAL, text, re.I):
            number = match.group(1)
            if 4 <= len(number) <= 50:
                start = match.start(1)
                positions[number] = min(start, positions.get(number, start))
    candidates = sorted(positions, key=positions.get) if document_order else sorted(positions)
    state = 'unique' if len(candidates) == 1 else 'ambiguous' if candidates else 'missing'
    return state, candidates


def _families(text):
    patterns = [r'ВКТ-[4789]\b', r'ТВ[78](?:-\d+(?:\.\d+)?)?', r'СПТ94\d[\d.()]*',
                r'МКТС', r'ВТД-В', r'ВКГ-3Т', r'ТСРВ-\d+(?:[А-ЯA-Z]|\(\d+\))?']
    return sorted({m.group(0).upper() for pattern in patterns for m in re.finditer(pattern, text, re.I)})


def _billing_period(context):
    subject = str((context or {}).get('message_subject') or '')
    days = re.match(r'^(\d{2})-(\d{2})\s', subject)
    dispatch = re.search(r'\((\d{2}\.\d{2}\.\d{4})\)\s*$', subject)
    if not days:
        return None
    start_day, end_day = map(int, days.groups())
    if not (1 <= start_day <= 31 and 1 <= end_day <= 31):
        return None
    if dispatch:
        dispatch_day = _parse_date(dispatch.group(1), context)
    else:
        received = (context or {}).get('received_at')
        try:
            dispatch_day = (received if isinstance(received, datetime) else datetime.fromisoformat(str(received).replace('Z', '+00:00'))).date()
        except (ValueError, TypeError):
            return None
    end = dispatch_day - timedelta(days=1)
    if not dispatch:
        end = end.replace(day=min(end_day, calendar.monthrange(end.year, end.month)[1]))
        if end >= dispatch_day:
            previous = end.replace(day=1) - timedelta(days=1)
            end = previous.replace(day=min(end_day, calendar.monthrange(previous.year, previous.month)[1]))
        # A missing dispatch date is usable only within a short provider reporting
        # window; a delayed arbitrary manual resend needs explicit period context.
        if (dispatch_day - end).days > 7:
            return None
    elif end.day != min(end_day, calendar.monthrange(end.year, end.month)[1]):
        return None
    if start_day == 1 and end_day == 31:
        start = end.replace(day=1)
    else:
        month_anchor = end.replace(day=1)
        if start_day > end.day:
            month_anchor -= timedelta(days=1)
        if start_day > calendar.monthrange(month_anchor.year, month_anchor.month)[1]:
            return None
        start = month_anchor.replace(day=start_day)
    return start, end


def _period(text, context):
    patterns = [
        rf'\(\s*({DATE_TOKEN})\s+{TIME_TOKEN}\s*[-–—]\s*({DATE_TOKEN})\s+{TIME_TOKEN}\s*\)',
        rf'(?:\bс\b|\bc\b)\s*({DATE_TOKEN})(?:\s+{TIME_TOKEN})?\s*(?:по|до)\s*({DATE_TOKEN})',
        rf'за\s*({DATE_TOKEN})\s*г?\.?\s*[-–—]\s*({DATE_TOKEN})',
    ]
    found = set()
    try:
        for pattern in patterns:
            for start, end in re.findall(pattern, text, re.I):
                found.add((_parse_date(start, context), _parse_date(end, context)))
    except ValueError:
        return None, None, [_issue('invalid_declared_period')]
    if any(start > end or (end - start).days > 366 for start, end in found):
        return None, None, [_issue('invalid_declared_period')]
    if len(found) > 1:
        return None, None, [_issue('multiple_declared_periods', severity='review_required')]
    explicit = (context or {}).get('expected_period')
    if explicit:
        try:
            pair = tuple(_parse_date(v, context) for v in explicit)
            if len(pair) != 2 or pair[0] > pair[1]:
                raise ValueError('invalid_period')
        except (ValueError, TypeError):
            return None, None, [_issue('invalid_context_period', severity='review_required')]
        if found and pair not in found:
            return None, None, [_issue('context_period_conflicts_with_pdf', severity='review_required')]
        return pair, 'explicit_context', []
    if found:
        return next(iter(found)), 'pdf_declared_range', []
    billing = _billing_period(context)
    if billing:
        formed = set()
        for value in re.findall(rf'Отч[её]т сформирован:\s*({DATE_TOKEN})', text, re.I):
            try:
                formed.add(_parse_date(value, context))
            except ValueError:
                return None, None, [_issue('invalid_formed_date')]
        if formed and formed != {billing[1]}:
            return None, None, [_issue('billing_period_conflicts_with_formed_date', severity='review_required')]
        return billing, 'provider_billing_window_and_formed_date' if formed else 'provider_billing_window', []
    return None, None, [_issue('expected_period_missing', severity='review_required')]


def _headers(table):
    width = max((len(row) for row in table if row), default=0)
    columns = [[] for _ in range(width)]
    for row in table:
        if row and DATE_PREFIX.match(_text(row[0])):
            break
        if row and _label(row[0]).startswith(TOTAL_HEADERS):
            break
        for index, cell in enumerate(row or []):
            if cell:
                columns[index].append(_text(cell))
    return [' '.join(parts) for parts in columns]


def _table_structure(table):
    headers = _headers(table)
    if not headers or _label(headers[0]) not in ('дата', 'датаивремя', 'датавремя', 'суткичас'):
        return None, headers, 'date_column_header_unrecognized'
    if any(not header for header in headers[1:]):
        return None, headers, 'measurement_column_header_missing'
    if not any(_label(header) not in SERVICE_HEADERS for header in headers[1:]):
        return None, headers, 'measurement_columns_missing'
    layout = 'spt_positional' if _label(headers[0]) == 'суткичас' else 'scalar_report'
    return layout, headers, None


def _date_cells(table):
    return [match.group() for row in table if row for line in _text(row[0]).splitlines()
            if (match := re.match(DATE_TOKEN, line.strip()))]


def _closing_rows(table):
    return [_label(row[0]) for row in table if row and _label(row[0]).startswith(TOTAL_HEADERS)]


def _paired_counters(table, headers, previous, days, period):
    previous_headers = [_heading_key(h) for h in _headers(previous)]
    current_headers = [_heading_key(h) for h in headers]
    if not days:
        return False
    counter_footer = (_closing_rows(previous) in (['средние', 'итого'], ['среднее', 'итого'])
                      and _closing_rows(table) == ['итого'])
    if days[0] == period[0] - timedelta(days=1) and (current_headers != previous_headers or counter_footer):
        return True
    # Some forms print the opening counter on the first day itself. The
    # daily grid ends with averages and totals. Its separate counter grid
    # prints a subset of those columns and a single total, without averages.
    return (days == list(period) and set(current_headers[1:]) < set(previous_headers[1:])
            and counter_footer)


def _table_adapters(tables, text, period, context):
    """Determine table roles from the printed sequence and dates, not quantities."""
    lines = text.splitlines()
    cursor = 0
    hourly = bool(re.search(r'часов(?:ая|ой)\s+ведомост|почасов', text, re.I))
    preceding_table = None
    adapters = []
    for table in tables:
        layout, headers, problem = _table_structure(table)
        dates = _date_cells(table)
        between_tables = ''
        for index, token in enumerate(dates):
            position = next((i for i in range(cursor, len(lines))
                             if re.match(rf'^\s*{re.escape(token)}(?:\s|$)', lines[i])), None)
            if position is None:
                break
            if index == 0:
                between_tables = '\n'.join(lines[cursor:position])
            cursor = position + 1
        if problem:
            adapters.append((None, headers, problem))
            if dates:
                preceding_table = table
            continue
        counter = CUMULATIVE_CAPTION.search(between_tables) or (
            preceding_table is not None and period and not REPORT_TITLE.search(between_tables)
            and _paired_counters(table, headers, preceding_table, _calendar_days(dates, context), period)
        )
        if counter:
            kind = 'cumulative'
        else:
            kind = 'hourly' if hourly else 'daily'
            preceding_table = table
        adapters.append(((layout, kind, headers), headers, None))
    return adapters


def _normalized_rows(table, layout, headers, context):
    width = len(headers)
    optional = {i for i, header in enumerate(headers) if _label(header) in SERVICE_HEADERS}
    rows, issues = [], []
    started = closed = False
    for row_index, row in enumerate(table):
        if not row:
            continue
        first = _text(row[0])
        if _label(first).startswith(TOTAL_HEADERS):
            closed = True
            continue
        if not DATE_PREFIX.match(first):
            if started and not closed and any(not _empty(cell) for cell in row):
                issues.append(_issue('measurement_row_without_date', severity='review_required', row=row_index + 1))
            continue
        started = True
        if closed:
            issues.append(_issue('dated_row_after_summary', severity='review_required', row=row_index + 1))
        if len(row) != width:
            issues.append(_issue('unexpected_column_count', severity='review_required', row=row_index + 1,
                                 found=len(row), expected=width))
            continue
        if layout == 'spt_positional' and len(first.splitlines()) >= 1 and all(_empty(c) for c in row[1:]):
            for line in first.splitlines():
                tokens = line.split()
                if len(tokens) != width + 1:
                    issues.append(_issue('spt_column_count', severity='review_required', row=row_index + 1))
                    continue
                stamp = _stamp(tokens[0] + ' ' + tokens[1], context)
                if stamp is None:
                    issues.append(_issue('invalid_measurement_date', row=row_index + 1))
                    continue
                rows.append((stamp, [tokens[0] + ' ' + tokens[1], *tokens[2:]], row_index + 1))
            continue
        lines = first.splitlines()
        if len(lines) == 2 and _stamp(' '.join(lines), context):
            lines = [' '.join(lines)]
        stamps = [_stamp(line, context) for line in lines]
        if not all(stamps):
            issues.append(_issue('unsupported_date_or_layout', severity='review_required', row=row_index + 1))
            continue
        count = len(lines)
        columns = []
        for col, cell in enumerate(row):
            if col == 0:
                values = lines
            else:
                values = _text(cell).splitlines()
                if not values:
                    values = [''] * count
                elif col in optional and len(values) != count:
                    values = [''] * count
            if len(values) != count:
                issues.append(_issue('multiline_columns_not_aligned', severity='review_required', row=row_index + 1, column=col))
                break
            columns.append(values)
        else:
            rows.extend((stamp, [column[index] for column in columns], row_index + 1)
                        for index, stamp in enumerate(stamps))
    return rows, optional, issues


def _calendar_days(tokens, context):
    days = []
    for token in tokens:
        try:
            days.append(_parse_date(token, context))
        except ValueError:
            continue
    return days


def _check_series(table, index, adapter, period, period_source, number, context):
    layout, kind, headers = adapter
    rows, optional, issues = _normalized_rows(table, layout, headers, context)
    required = [i for i in range(1, len(headers)) if i not in optional]
    if not rows:
        issues.append(_issue('zero_checked_rows', severity='review_required'))
    for stamp, cells, physical_row in rows:
        for col in required:
            if _empty(cells[col]):
                issues.append(_issue('empty_required_measurement', day=stamp[0].isoformat(), row=physical_row,
                                     column=col, header=headers[col]))
            elif (re.fullmatch(rf'{NUMBER_TOKEN}(?:\s+{NUMBER_TOKEN})+', cells[col].strip())
                  and not GROUPED_NUMBER.fullmatch(cells[col].strip())):
                issues.append(_issue('multiple_values_in_measurement_cell', severity='review_required',
                                     day=stamp[0].isoformat(), row=physical_row, column=col, header=headers[col]))
    detected_dates = _date_cells(table)
    days = _calendar_days(detected_dates, context)
    unique = sorted(set(days))
    if period and days and kind == 'daily':
        expected = [period[0] + timedelta(days=i) for i in range((period[1] - period[0]).days + 1)]
        missing = sorted(set(expected) - set(days))
        outside = sorted(set(days) - set(expected))
        if missing:
            severity = 'review_required' if any(issue['code'] == 'measurement_row_without_date' for issue in issues) else 'rejected'
            issues.append(_issue('missing_daily_dates', severity=severity, dates=[value.isoformat() for value in missing]))
        if outside:
            issues.append(_issue('dates_outside_declared_period', dates=[value.isoformat() for value in outside]))
        if len(days) != len(unique):
            issues.append(_issue('duplicate_daily_dates', severity='review_required',
                                 dates=[day.isoformat() for day, count in Counter(days).items() if count > 1]))
    elif period and days and kind == 'cumulative':
        if len(days) != 2:
            issues.append(_issue('cumulative_boundary_rows_required', severity='review_required'))
        elif len(unique) != 2 or unique[0] not in {period[0] - timedelta(days=1), period[0]} or unique[-1] != period[1]:
            issues.append(_issue('cumulative_period_mismatch', dates=[value.isoformat() for value in days]))
    elif kind == 'hourly':
        # No production hourly format is present in the current corpus. Treat a
        # detected hourly form as an explicit model, never as 24 daily duplicates.
        issues.append(_issue('hourly_period_contract_required', severity='review_required'))
    return {
        'series_id': f'table-{index}', 'meter': number, 'circuit_key': f'table-{index}',
        'circuit_source': 'separate_printed_table', 'kind': kind, 'layout': layout,
        'period': {'start': period[0].isoformat(), 'end': period[1].isoformat()} if period else None,
        'period_source': period_source, 'required_columns': required,
        'optional_columns': sorted(optional), 'headers': headers,
        'detected_rows': len(detected_dates), 'checked_rows': len(rows),
        'checked_measurements': len(rows) * len(required), 'issues': issues,
    }


def analyze_capture(text, tables, context=None, *, require_identity=True):
    """Analyze captured text/tables with no DB, network, file or task effects."""
    context = context or {}
    text = str(text or '').replace('\u00a0', ' ').replace('\u202f', ' ')
    identity_state, candidates = _identity(text)
    number = candidates[0] if identity_state == 'unique' else None
    families = _families(text)
    issues = []
    if require_identity and identity_state != 'unique':
        issues.append(_issue(f'identity_{identity_state}', severity='review_required'))
    period, period_source, period_issues = _period(text, context)
    issues.extend(period_issues)
    series = []
    tables = tables or []
    unparsed = []
    for index, (adapter, headers, problem) in enumerate(_table_adapters(tables, text, period, context)):
        if adapter is None:
            issues.append(_issue(problem, severity='review_required', table=index))
            unparsed.append({'table': index, 'headers': headers, 'detected_rows': len(_date_cells(tables[index]))})
            continue
        result = _check_series(tables[index], index, adapter, period, period_source, number, context)
        series.append(result)
        issues.extend({**issue, 'table': index} for issue in result['issues'])
    if not tables:
        issues.append(_issue('measurement_tables_not_extracted', severity='review_required'))
    if not any(row['kind'] in {'daily', 'hourly'} for row in series):
        issues.append(_issue('daily_series_missing', severity='review_required'))
    state = _validation_state(issues)
    return {
        'state': state, 'calculator_number': number, 'identity_state': identity_state,
        'serial_candidates': candidates, 'reason': ';'.join(sorted({issue['code'] for issue in issues})) or 'validated',
        'issues': issues, 'adapter_version': ADAPTER_VERSION, 'instrument_models': families,
        'series': series, 'unparsed_tables': unparsed, 'detected_tables': len(tables),
        'checked_rows': sum(row['checked_rows'] for row in series),
        'checked_measurements': sum(row['checked_measurements'] for row in series),
    }


def _validation_state(issues):
    if any(issue['severity'] == 'rejected' for issue in issues):
        return 'rejected'
    if any(issue['severity'] == 'review_required' for issue in issues):
        return 'review_required'
    return 'rejected' if issues else 'validated'


def _continuation_rows(prior_headers, text, table, first_data):
    """Use matching extracted headers, or independently visible column names."""
    if first_data:
        return first_data if [_heading_key(h) for h in _headers(table)] == [_heading_key(h) for h in prior_headers] else None
    header = ' '.join(_heading_key(h) for h in prior_headers)
    visible = {_heading_key(line) for line in text.splitlines()}
    if header in visible and all(len(row) == len(prior_headers) for row in table if row):
        return 0
    return None


def _continues_daily_period(old_text, old_table, adapter, new_row, context):
    layout, kind, headers = adapter
    period, _, _ = _period(old_text, context)
    if kind != 'daily' or not period:
        return False
    new_stamp = _stamp(_text(new_row[0]).splitlines()[0], context)
    old_rows, _, _ = _normalized_rows(old_table, layout, headers, context)
    return bool(new_stamp and old_rows and
                period[0] <= max(stamp[0] for stamp, _, _ in old_rows) < new_stamp[0] <= period[1])


def _printouts(pages, context):
    """Keep report boundaries and join only verifiable continuation tables."""
    groups, unverified = [], {}
    period_context = {'received_at': (context or {}).get('received_at')}
    for index, (text, tables) in enumerate(pages, 1):
        sources = [[(index, i, row) for row in range(1, len(table) + 1)] for i, table in enumerate(tables)]
        if groups and tables and groups[-1][2]:
            start, old_text, old_tables, page_numbers, old_sources = groups[-1]
            old_table, table = old_tables[-1], tables[0]
            old_serials, new_serials = set(_identity(old_text)[1]), set(_identity(text)[1])
            old_models, new_models = _families(old_text), _families(text)
            _, period_source, problems = _period(text, period_context)
            declared_period = period_source or any(issue['code'] != 'expected_period_missing' for issue in problems)
            marked = re.search(r'^\s*(?:продолжение\b|(?:страница|стр\.)\s*(?:[2-9]|\d{2,})\s+из\s+\d+\b)', text, re.I | re.M)
            printout_title = REPORT_TITLE.search(text)
            first_data = next((i for i, row in enumerate(table) if row and re.match(DATE_TOKEN, _text(row[0]))), None)
            closed = bool(_closing_rows(old_table))
            metadata_match = not (new_serials - old_serials or set(new_models) - set(old_models))
            adapter = None
            if metadata_match and not closed:
                old_period, _, _ = _period(old_text, context)
                adapter = _table_adapters(old_tables, old_text, old_period, context)[-1][0]
            if adapter and first_data is not None and (marked or not (declared_period or printout_title) and
                    _continues_daily_period(old_text, old_table, adapter, table[first_data], context)):
                row_start = _continuation_rows(adapter[2], text, table, first_data)
                if row_start is not None:
                    joined = [*old_tables[:-1], [*old_table, *table[row_start:]], *tables[1:]]
                    joined_sources = [*old_sources[:-1], [*old_sources[-1], *sources[0][row_start:]], *sources[1:]]
                    groups[-1] = (start, old_text + '\n' + text, joined, [*page_numbers, index], joined_sources)
                    continue
                if not marked or first_data == 0:
                    unverified[start] = index
            if marked and metadata_match and first_data != 0:
                groups[-1] = (start, old_text + '\n' + text, [*old_tables, *tables], [*page_numbers, index],
                              [*old_sources, *sources])
                continue
        groups.append((index, text, tables, [index], sources))
    return groups, unverified


def _printed_rows_missing_from_tables(text, tables):
    # A visible dated measurement row outside the extracted grids is evidence
    # of incomplete extraction, even when every extracted series passed.
    printed = Counter()
    for line in text.splitlines():
        match = re.match(rf'^\s*({DATE_TOKEN})\s+(.+)', line)
        if match:
            values = re.sub(rf'^{TIME_TOKEN}(?:\s+|$)', '', match.group(2).strip())
            if values and not re.match(rf'^[-–—]\s+{DATE_TOKEN}', values) and (
                    re.search(r'\d', values) or all(_empty(token) for token in values.split())):
                printed[match.group(1)] += 1
    extracted = Counter(day for table in tables for day in _date_cells(table))
    return dict(printed - extracted)


def _analyze_printouts(pages, identity_state, candidates, context):
    """Validate each report over its full period independently of its meter number."""
    issues, series, models, periods = [], [], set(), set()
    printouts, unparsed = [], []
    if identity_state == 'missing':
        issues.append(_issue('identity_missing', severity='review_required'))
    groups, unverified = _printouts(pages, context)
    for printout, (index, text, tables, page_numbers, sources) in enumerate(groups, 1):
        result = analyze_capture(text, tables, context, require_identity=False)
        meter = result['calculator_number']
        printouts.append({'printout': printout, 'pages': page_numbers, 'detected_tables': len(tables),
                          'checked_rows': result['checked_rows']})
        for page in page_numbers:
            missing = _printed_rows_missing_from_tables(*pages[page - 1])
            titles = REPORT_TITLE.findall(pages[page - 1][0])
            if len(titles) > 1:
                issues.append(_issue('printout_boundaries_unverified', severity='review_required',
                                     page=page, printout=printout, found=len(titles)))
            if missing:
                issues.append(_issue('printed_rows_not_extracted', severity='review_required',
                                     page=page, printout=printout, dates=missing))
        if index in unverified:
            issues.append(_issue('continuation_unverified', severity='review_required',
                                 page=unverified[index], printout=printout))
            last_table = len(tables) - 1
            # Keep the missing dates visible while the boundary is in review.
            for issue in result['issues']:
                if (issue['code'], issue.get('table')) == ('missing_daily_dates', last_table):
                    issue['severity'] = 'review_required'
            for report in result['series']:
                if report['series_id'] == f'table-{last_table}':
                    for issue in report['issues']:
                        if issue['code'] == 'missing_daily_dates':
                            issue['severity'] = 'review_required'
        models.update(result['instrument_models'])
        def location(issue, table=None):
            row = issue.get('row')
            origin = sources[table] if table is not None else []
            page, physical_table, physical_row = origin[row - 1] if row else (origin[0] if origin else (index, table, None))
            position = {'page': page, 'printout': printout, 'meter': meter}
            if table is not None:
                position['physical_table'] = physical_table + 1
            if row:
                position['row'] = physical_row
            return {**issue, **position}

        issues.extend(location(issue, issue.get('table')) for issue in result['issues'])
        unparsed.extend(location(table, table['table']) for table in result['unparsed_tables'])
        for report in result['series']:
            table = int(report['series_id'].removeprefix('table-'))
            if report['period']:
                periods.add((report['period']['start'], report['period']['end']))
            series.append({
                **report, **location({}, table), 'pages': sorted({p for p, _, _ in sources[table]}),
                'issues': [location(issue, table) for issue in report['issues']],
                'series_id': f"page-{index}-{report['series_id']}",
                'circuit_key': f"page-{index}-{report['circuit_key']}",
            })

    if len(periods) > 1:
        issues.append(_issue('multiple_declared_periods', severity='review_required'))

    return {
        'state': _validation_state(issues), 'calculator_number': candidates[0] if candidates else None,
        'identity_state': 'multiple' if identity_state == 'ambiguous' else identity_state,
        'serial_candidates': candidates, 'reason': ';'.join(sorted({issue['code'] for issue in issues})) or 'validated',
        'issues': issues, 'adapter_version': ADAPTER_VERSION, 'instrument_models': sorted(models),
        'page_count': len(pages), 'printouts': printouts, 'detected_tables': sum(len(tables) for _, tables in pages),
        'series': series, 'unparsed_tables': unparsed, 'checked_rows': sum(report['checked_rows'] for report in series),
        'checked_measurements': sum(report['checked_measurements'] for report in series),
    }


def analyze_pdf(path, context=None):
    """Read an existing PDF; parser failures are technical, never business rejects."""
    import pdfplumber

    pages = []
    try:
        if not Path(path).is_file():
            raise FileNotFoundError('missing_pdf')
        with pdfplumber.open(path) as pdf:
            for page in pdf.pages:
                text = _text(page.extract_text())
                pages.append((text, page.extract_tables() or []))
        text = ''.join(f'\n--- Page {index} ---\n{page}' for index, (page, _) in enumerate(pages, 1))
        identity_state, candidates = _identity(text, document_order=True)
    except Exception as error:
        code = 'pdf_missing' if isinstance(error, FileNotFoundError) else 'pdf_extraction_failed'
        state = 'technical_error' if code == 'pdf_missing' else 'review_required'
        return {
            'state': state, 'calculator_number': None, 'identity_state': 'missing',
            'serial_candidates': [], 'reason': code,
            'issues': [_issue(code, severity=state, page=len(pages) + 1, error_type=type(error).__name__)],
            'adapter_version': ADAPTER_VERSION, 'page_count': len(pages), 'printouts': [], 'detected_tables': 0,
            'series': [], 'unparsed_tables': [], 'checked_rows': 0, 'checked_measurements': 0,
        }
    if not pages:
        return {**_analyze_printouts([], 'missing', [], context), 'reason': 'pdf_no_pages',
                'issues': [_issue('pdf_no_pages', severity='review_required')]}
    return _analyze_printouts(pages, identity_state, candidates, context)
