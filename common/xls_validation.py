"""Deterministic reader for the Eldis daily gas report in Excel 97–2003 format."""
import re
from struct import error as StructError
from datetime import datetime, timedelta
from decimal import Decimal
from unicodedata import normalize

import xlrd


_TITLE = "отчет о суточных параметрах газоснабжения"
_DATE = r"\d{2}\.\d{2}\.\d{4}"
_PERIOD = re.compile(rf"\(({_DATE})\s+0{{1,2}}:00:00\s*[-–—]\s*({_DATE})\s+0{{1,2}}:00:00\)")
_SERIAL = re.compile(r"заводской\s+номер\s*:\s*([A-Za-zА-Яа-я0-9]+(?:-[A-Za-zА-Яа-я0-9]+)*)\s*$", re.I)
_LOOKALIKES = str.maketrans("рсвнотдб", "pcbhotdb")
_ALIASES = {"vpds1": "vpdc1", "vs1": "vc1", "vnr": "bhp", "vos": "boc"}
_MEASUREMENTS = {
    "t1": ("t1", {"°c", "°с"}),
    "vp1": ("Vр1", {"м3", "m3"}),
    "vpdc1": ("VрДС1", {"м3", "m3"}),
    "vc1": ("Vс1", {"м3", "m3"}),
    "p1": ("P1", {"мпа", "mpa"}),
    "pb": ("Pб", {"кг/см2", "kg/cm2"}),
    "ro": ("Ro", {"кг/м3", "kg/m3"}),
    "n2": ("N2", {"%"}),
    "co2": ("CO2", {"%"}),
    "bhp": ("ВНР", {"ч", "h"}),
    "boc": ("ВОС", {"ч", "h"}),
}
_LIMITS = {"vp1": (0, 10000), "vpdc1": (0, 10000), "vc1": (0, None), "bhp": (None, 24)}
_DASHES = frozenset("-–—−‒―")
_NUMBER = re.compile(r"[+-]?(?:[0-9]+|[0-9]{1,3}(?: [0-9]{3})+)(?:[.,][0-9]+)?")


class ReportStructureError(ValueError):
    pass


def _text(value):
    return normalize("NFKC", str(value)).strip()


def _heading(value):
    value = "".join(_text(value).casefold().split())
    if value == "дата":
        return "date"
    value = value.translate(_LOOKALIKES)
    return _ALIASES.get(value, value)


def _date(cell, datemode):
    try:
        if cell.ctype == xlrd.XL_CELL_DATE:
            stamp = xlrd.xldate_as_datetime(cell.value, datemode)
            return stamp.date() if stamp == datetime.combine(stamp.date(), datetime.min.time()) else None
        if cell.ctype == xlrd.XL_CELL_TEXT and re.fullmatch(_DATE, _text(cell.value)):
            return datetime.strptime(_text(cell.value), "%d.%m.%Y").date()
    except (ValueError, OverflowError):
        pass
    return None


def _measurement(cell):
    value = _text(cell.value)
    if cell.ctype in (xlrd.XL_CELL_EMPTY, xlrd.XL_CELL_BLANK) or (
        cell.ctype == xlrd.XL_CELL_TEXT and (not value or set(value) <= _DASHES)
    ):
        return None, "missing_measurement", "Отсутствуют суточные показания"
    if cell.ctype == xlrd.XL_CELL_NUMBER:
        number = Decimal(value)
    elif cell.ctype == xlrd.XL_CELL_TEXT and _NUMBER.fullmatch(value.replace("−", "-")):
        number = Decimal(value.replace("−", "-").replace(" ", "").replace(",", "."))
    else:
        return None, "unreadable_measurement", "Значение нельзя достоверно прочитать как число"
    if not number.is_finite():
        return None, "unreadable_measurement", "Значение не является конечным числом"
    return number, None, None


def _table(sheet):
    headers = [row for row in range(sheet.nrows) if any(_heading(value) == "date" for value in sheet.row_values(row))]
    if len(headers) != 1 or headers[0] + 2 >= sheet.nrows:
        raise ReportStructureError("Ожидалась одна таблица с заголовком «Дата» и строкой единиц измерения")
    header = headers[0]
    columns = {}
    for col, value in enumerate(sheet.row_values(header)):
        key = _heading(value)
        if not key:
            continue
        if key in columns or key not in {"date", "ns", *_MEASUREMENTS}:
            raise ReportStructureError("Неизвестный или повторяющийся заголовок измерения")
        columns[key] = col
    if set(columns) != {"date", "ns", *_MEASUREMENTS}:
        raise ReportStructureError("Не найдены все колонки суточного газового отчёта")
    for key, (label, units) in _MEASUREMENTS.items():
        unit = "".join(_text(sheet.cell_value(header + 1, columns[key])).casefold().split())
        if unit not in units:
            raise ReportStructureError(f"Неизвестная единица измерения для {label}")
    return header, columns


def _issue(code, rule, *, row=None, day=None, parameter=None, value=None):
    return {"code": code, "rule": rule, "row": row, "date": day.isoformat() if day else None,
            "parameter": parameter, "value": value}


def _result(issues, *, serial=None, object_name=None, checked_rows=0):
    unreadable = {"xls_unreadable", "xls_structure_unknown", "xls_dates_invalid", "xls_identity_unknown",
                  "unreadable_measurement"}
    if any(issue["code"] in unreadable for issue in issues):
        state, reason = "review_required", "xls_requires_review"
    elif issues:
        state, reason = "rejected", "xls_measurement_violations"
    else:
        state, reason = "validated", None
    return {"state": state, "reason": reason, "identity_state": "unique" if serial else "missing",
            "calculator_number": serial, "report_object": object_name, "checked_rows": checked_rows,
            "adapter_version": "eldis-gas-xls-v1", "issues": issues}


def _read_report(book):
    if book.nsheets != 1:
        raise ReportStructureError("Ожидался один лист суточного отчёта Eldis")
    sheet = book.sheet_by_index(0)
    header, columns = _table(sheet)
    preamble = [_text(value) for row in range(header) for value in sheet.row_values(row) if value != ""]
    titles = [value for value in preamble if value.casefold().replace("ё", "е").startswith(_TITLE)]
    if len(titles) != 1 or not any(re.search(r"\bресурс\s*:\s*газ\b", value, re.I) for value in preamble):
        raise ReportStructureError("Не распознан суточный газовый отчёт Eldis")
    period = _PERIOD.search(titles[0])
    if not period:
        raise ReportStructureError("Не распознан период отчёта")
    try:
        start, end = (datetime.strptime(value, "%d.%m.%Y").date() for value in period.groups())
    except ValueError as error:
        raise ReportStructureError("Невозможная дата в периоде отчёта") from error
    serials = {match[1] for value in preamble if (match := _SERIAL.search(value))}
    serial = next(iter(serials)) if len(serials) == 1 else None
    objects = [value.split(":", 1)[1].strip() for value in preamble if value.casefold().startswith("объект:")]
    object_name = objects[0] if len(objects) == 1 else None
    issues, dates = [], []
    if not serial:
        issues.append(_issue("xls_identity_unknown", "Не определён однозначный заводской номер прибора"))
    summary_seen = total_seen = False
    for row in range(header + 2, sheet.nrows):
        date_cell = sheet.cell(row, columns["date"])
        label = _text(date_cell.value).casefold().rstrip(":")
        if label in ("средние", "итого"):
            summary_seen = True
            total_seen |= label == "итого"
            continue
        has_measurements = any(sheet.cell_value(row, columns[key]) != "" for key in _MEASUREMENTS)
        if not label and not has_measurements:
            continue
        day = _date(date_cell, book.datemode)
        if summary_seen and not day and not has_measurements:
            continue
        if not day or summary_seen:
            issues.append(_issue("xls_dates_invalid", "Некорректная строка в суточной таблице",
                                 row=row + 1, parameter="Дата", value=_text(date_cell.value)))
            continue
        dates.append(day)
        for key, (parameter, _) in _MEASUREMENTS.items():
            cell = sheet.cell(row, columns[key])
            number, code, rule = _measurement(cell)
            if number is not None and key in _LIMITS:
                minimum, maximum = _LIMITS[key]
                if minimum is not None and number < minimum:
                    code, rule = "negative_volume", "Объём меньше 0 м³"
                elif maximum is not None and number > maximum:
                    code = "hours_above_24" if key == "bhp" else "volume_above_10000"
                    rule = "ВНР больше 24 часов за сутки" if key == "bhp" else "Объём больше 10 000 м³ за сутки"
            if code:
                value = _text(cell.value)
                if cell.ctype == xlrd.XL_CELL_ERROR:
                    value = xlrd.error_text_from_code.get(cell.value, value)
                issues.append(_issue(code, rule, row=row + 1, day=day, parameter=parameter, value=value))
    if (not dates or len(dates) != (end - start).days + 1
            or any((day - start).days != index for index, day in enumerate(dates))):
        issues.append(_issue("xls_dates_invalid", "Суточные даты не покрывают период отчёта без пропусков и дублей"))
    if not total_seen:
        issues.append(_issue("xls_structure_unknown", "Не найдена строка «Итого»"))
    return _result(issues, serial=serial, object_name=object_name, checked_rows=len(dates))


def analyze_xls(path):
    try:
        with xlrd.open_workbook(path, on_demand=True) as book:
            return _read_report(book)
    except ReportStructureError as error:
        return _result([_issue("xls_structure_unknown", str(error))])
    except (xlrd.XLRDError, OSError, ValueError, IndexError, AssertionError, StructError):
        return _result([_issue("xls_unreadable", "Файл XLS не удалось прочитать")])
