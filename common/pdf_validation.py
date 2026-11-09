"""Versioned, side-effect-free PDF format adapters for Project1.

No task, broker, database or network imports. Object resolution belongs to the
caller and must use the complete identity returned here.
"""
from __future__ import annotations

import calendar
from datetime import date, datetime, timedelta
from pathlib import Path
import re

ADAPTER_VERSION = 'project1-formats/2.0'
DATE_TOKEN = r'\d{2}[./]\d{2}[./](?:\d{4}|\d{2})(?!\d)'
TIME_TOKEN = r'\d{1,2}:\d{2}(?::\d{2})?'
SERIAL = r'([0-9][0-9A-Za-zА-Яа-я]*(?:-[0-9A-Za-zА-Яа-я]+)*)(?![0-9A-Za-zА-Яа-я_-])'
EMPTY = {'', '-', '–', '—', '---', 'нд', 'н/д'}
COUNTER_COLUMNS = {
    'v1', 'v2', 'v3', 'v4', 'm', 'м', 'm1', 'm2', 'm3', 'm4', 'м1', 'м2', 'м3', 'м4',
    'mу', 'му', 'my', 'мy', 'mг', 'мг', 'mгв', 'мгв', 'mtv', 'мтв', 'мтс', 'dм', 'dm',
    'q', 'q1', 'q2', 'q3', 'q4', 'qo', 'qg', 'qг', 'qtv', 'qтв', 'q12', 'qnt', 'qnthip', 'qntp',
    'w', 'w6', 'wтс', 'wгв', 'wгвс', 'wбезгвс', 'twork', 'tns', 'tи', 'ти',
    'внр', 'вос', 'траб', 'tраб', 'тнар', 'тот', 'тпит', 'треж', 'тнс',
    'времяраб', 'времянс', 'времяработы',
}


def _issue(code, *, severity='rejected', **details):
    return {'code': code, 'severity': severity, **details}


def _label(value):
    return re.sub(r'[^a-zа-яё0-9]', '', str(value or '').lower())


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


def _identity(text):
    labels = [
        r'Заводской\s*номер\s*(?:[:=]\s*)?',
        r'Номер\s*прибора\s*(?:[:=]\s*)?',
        r'Теплосчетчик\s+МКТС\s*[:=]?\s*№\s*',
        r'ТСРВ[-\s][^\n№]*№\s*',
        r'Тепловычислитель\s+СПТ94[^\n]*?идентификатор\s+ИД\s*=\s*',
    ]
    candidates = set()
    for label in labels:
        for match in re.finditer(label + SERIAL, text, re.I):
            if 4 <= len(match.group(1)) <= 50:
                candidates.add(match.group(1))
    candidates = sorted(candidates)
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
        if row and re.match(r'^\d{2}[./]\d{2}[./]', _text(row[0])):
            break
        if row and _label(row[0]).startswith(('итого', 'средние', 'сумма')):
            break
        for index, cell in enumerate(row or []):
            if cell:
                columns[index].append(_text(cell))
    return [' '.join(parts) for parts in columns]


def _counter_header(value):
    label = _label(value)
    if label in COUNTER_COLUMNS:
        return True
    return any(label.endswith(unit) and label[:-len(unit)] in COUNTER_COLUMNS
               for unit in ('гкал', 'кгссм2', 'час', 'мпа', 'ати', 'мкуб', 'м3', 'ч', 'т'))


def _table_adapter(table, families, text):
    headers = _headers(table)
    normalized = [_label(value) for value in headers]
    if not families or not headers:
        return None
    first = normalized[0]
    if first.startswith('суткичас') and any(model.startswith('СПТ94') for model in families):
        if not any(value.startswith(('q', 'тепло')) for value in normalized):
            return None
        return 'spt_positional', 'daily', headers
    if first not in ('дата', 'датаивремя', 'датавремя'):
        return None
    temperature = any(re.search(r'(?:^|\s)t[1-4](?:\b|\s)|температур', value, re.I) for value in headers)
    # Scalar Eldis/TSRV and VTD daily signatures carry temperature columns.
    # The MKTS grouped header has explicit temperature/mass headings.
    if temperature or any(value.startswith(('t1', 't2', 't3', 't4')) for value in normalized):
        # A temperature-only table is not a complete consumption report. Gas
        # computer forms require their volume quantity; heat forms require Q/W.
        gas_form = 'ВКГ-3Т' in families
        has_primary = any(value.startswith(('vp', 'vс')) for value in normalized) if gas_form else any(
            value.startswith(('q', 'w', 'тэ', 'тепло', 'теплэн')) for value in normalized
        )
        if not has_primary:
            return None
        kind = 'hourly' if re.search(r'часов(?:ая|ой)\s+ведомост|почасов', text, re.I) else 'daily'
        return 'mkts_columns' if 'МКТС' in families else 'scalar_report', kind, headers
    # Integral/counter signatures contain known physical quantities/time only.
    if len(normalized) >= 3 and all(_counter_header(value) for value in headers[1:]):
        return 'scalar_counters', 'cumulative', headers
    return None


def _normalized_rows(table, layout, headers, context):
    width = len(headers)
    optional = {i for i, header in enumerate(headers) if _label(header) in
                {'нс', 'ns', 'коднс', 'отказы', 'примечание', 'нештатныеситуации'}}
    rows, issues = [], []
    for row_index, row in enumerate(table):
        if not row:
            continue
        first = _text(row[0])
        if not re.match(r'^\d{2}[./]\d{2}[./]', first):
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
                rows.append((stamp, [tokens[0] + ' ' + tokens[1], *tokens[2:]]))
            continue
        lines = first.splitlines()
        if len(lines) == 2 and _stamp(' '.join(lines), context):
            lines = [' '.join(lines)]
        stamps = [_stamp(line, context) for line in lines]
        if not all(stamps):
            issues.append(_issue('unsupported_date_or_layout', severity='review_required', row=row_index + 1))
            continue
        count = len(lines)
        padded = list(row) + [None] * max(0, width - len(row))
        if len(padded) != width:
            issues.append(_issue('unexpected_column_count', severity='review_required', row=row_index + 1))
            continue
        columns = []
        for col, cell in enumerate(padded):
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
            rows.extend((stamp, [column[index] for column in columns]) for index, stamp in enumerate(stamps))
    return rows, optional, issues


def _check_series(table, index, adapter, period, period_source, number, context):
    layout, kind, headers = adapter
    rows, optional, issues = _normalized_rows(table, layout, headers, context)
    required = [i for i in range(1, len(headers)) if i not in optional]
    if not rows:
        issues.append(_issue('zero_checked_rows', severity='review_required'))
    for stamp, cells in rows:
        for col in required:
            if _empty(cells[col]):
                issues.append(_issue('empty_required_measurement', day=stamp[0].isoformat(), column=col, header=headers[col]))
    days = [stamp[0] for stamp, _ in rows]
    unique = sorted(set(days))
    if period and rows and kind == 'daily':
        expected = [period[0] + timedelta(days=i) for i in range((period[1] - period[0]).days + 1)]
        missing = sorted(set(expected) - set(days))
        outside = sorted(set(days) - set(expected))
        if missing:
            issues.append(_issue('missing_daily_dates', dates=[value.isoformat() for value in missing]))
        if outside:
            issues.append(_issue('dates_outside_declared_period', dates=[value.isoformat() for value in outside]))
        if len(days) != len(unique):
            issues.append(_issue('duplicate_daily_dates', severity='review_required'))
    elif period and rows and kind == 'cumulative':
        if len(rows) != 2:
            issues.append(_issue('cumulative_boundary_rows_required', severity='review_required'))
        elif unique[0] not in {period[0] - timedelta(days=1), period[0]} or unique[-1] != period[1]:
            issues.append(_issue('cumulative_period_mismatch'))
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
        'checked_rows': len(rows), 'issues': issues,
    }


def analyze_capture(text, tables, context=None):
    """Analyze captured text/tables with no DB, network, file or task effects."""
    context = context or {}
    text = str(text or '').replace('\u00a0', ' ').replace('\u202f', ' ')
    identity_state, candidates = _identity(text)
    number = candidates[0] if identity_state == 'unique' else None
    families = _families(text)
    issues = []
    if identity_state != 'unique':
        issues.append(_issue(f'identity_{identity_state}', severity='review_required'))
    if not families:
        issues.append(_issue('unknown_instrument_format', severity='review_required'))
    period, period_source, period_issues = _period(text, context)
    issues.extend(period_issues)
    series = []
    for index, table in enumerate(tables or []):
        adapter = _table_adapter(table, families, text)
        if adapter is None:
            issues.append(_issue('unknown_table_signature', severity='review_required', table=index))
            continue
        result = _check_series(table, index, adapter, period, period_source, number, context)
        series.append(result)
        issues.extend({**issue, 'table': index} for issue in result['issues'])
    if not series or not any(row['kind'] in {'daily', 'hourly'} for row in series):
        issues.append(_issue('daily_series_missing', severity='review_required'))
    if any(issue['severity'] == 'rejected' for issue in issues):
        state = 'rejected'
    elif any(issue['severity'] == 'review_required' for issue in issues):
        state = 'review_required'
    elif issues:
        state = 'rejected'
    else:
        state = 'validated'
    return {
        'state': state, 'calculator_number': number, 'identity_state': identity_state,
        'serial_candidates': candidates, 'reason': ';'.join(sorted({issue['code'] for issue in issues})) or 'validated',
        'issues': issues, 'adapter_version': ADAPTER_VERSION, 'instrument_models': families,
        'series': series, 'checked_rows': sum(row['checked_rows'] for row in series),
    }


def analyze_pdf(path, context=None):
    """Read an existing PDF; parser failures are technical, never business rejects."""
    import pdfplumber

    try:
        if not Path(path).is_file():
            raise FileNotFoundError('missing_pdf')
        texts, tables = [], []
        with pdfplumber.open(path) as pdf:
            for index, page in enumerate(pdf.pages):
                texts.append(f'\n--- Page {index + 1} ---\n{page.extract_text() or ""}')
                tables.extend(page.extract_tables() or [])
        return analyze_capture(''.join(texts), tables, context)
    except Exception as error:
        code = 'pdf_missing' if isinstance(error, FileNotFoundError) else 'pdf_extraction_failed'
        return {
            'state': 'technical_error', 'calculator_number': None, 'identity_state': 'missing',
            'serial_candidates': [], 'reason': code,
            'issues': [_issue(code, severity='technical_error', error_type=type(error).__name__)],
            'adapter_version': ADAPTER_VERSION, 'series': [], 'checked_rows': 0,
        }
