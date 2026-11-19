"""Compare V1, previous V2 and the candidate on a private PDF capture."""
import argparse
import ast
from collections import Counter
from contextlib import nullcontext
import csv
from datetime import datetime, timedelta
import gzip
from importlib.util import module_from_spec, spec_from_file_location
import json
import logging
from pathlib import Path
import re
from types import SimpleNamespace
from typing import Dict, List, Optional
from unittest.mock import patch

from common import pdf_validation


def legacy_validator(path):
    source = ast.parse(path.read_text(encoding='utf-8'))
    validator = next(node for node in source.body if isinstance(node, ast.ClassDef) and node.name == 'PDFValidator')
    scope = dict(Dict=Dict, List=List, Optional=Optional, re=re, datetime=datetime, timedelta=timedelta,
                 logger=logging.getLogger('legacy_pdf'))
    exec(compile(ast.Module(body=[validator], type_ignores=[]), str(path), 'exec'), scope)
    return scope['PDFValidator']


def baseline_module(path):
    spec = spec_from_file_location('baseline_pdf_validation', path)
    module = module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def compare_legacy(validator, pages):
    text = '\n'.join(text or '' for text, _ in pages)
    tables = [table for _, tables in pages for table in tables]
    dates = validator.validate_dates(text)
    measurements = validator.validate_tables(tables)
    number = validator.extract_calculator_number(text)
    rows = [row for table in tables for row in table if row and re.match(r'^\d{2}\.\d{2}\.\d{4}', str(row[0]).strip())]
    accepted = dates['dates_ok'] and measurements['tables_ok']
    return {'state': 'validated' if accepted else 'rejected', 'calculator_number': number,
            'detected_tables': len(tables), 'checked_rows': len(rows),
            'checked_measurements': sum(max(0, len(row) - 2) for row in rows),
            'date_gaps': measurements['date_gaps'], 'errors': measurements['errors'],
            'dates_ok': dates['dates_ok'], 'printouts': None, 'table_roles': None}


class ObjectSnapshot:
    """Read-only mirror of the two versions' binding and dispatcher decisions."""

    def __init__(self, rows):
        self.rows = [dict(object_id=row['object_id'], serials=row['serials'],
                          active=row['active'] == 't', has_email=row['has_email'] == 't') for row in rows]
        self.full_numbers = {}
        for row in self.rows:
            for serial in {value.strip() for value in row['serials'].split(',')}:
                if serial:
                    self.full_numbers.setdefault(serial, []).append(row)

    def route(self, result, *, legacy=False):
        serial = result['calculator_number']
        state = result['state']
        obj = None
        if legacy:
            owners = [row for row in self.full_numbers.get(serial, []) if row['serials'] == serial]
            inactive = next((row for row in owners if not row['active']), None)
            if inactive:
                return dict(state='discarded_inactive', role=None, object_id=inactive['object_id'])
            obj = next(iter(owners), None)
            if obj is None and (digits := re.match(r'^\d{3,}', serial or '')):
                matches = [row for row in self.rows if row['active'] and row['has_email']
                           and row['serials'].startswith(digits.group())]
                obj = matches[0] if len(matches) == 1 else None
            role = 'client' if state == 'validated' and obj and obj['has_email'] else 'admin'
        else:
            owners = self.full_numbers.get(serial, [])
            obj = owners[0] if len(owners) == 1 else None
            if obj and not obj['active']:
                return dict(state='discarded_inactive', role=None, object_id=obj['object_id'])
            if state == 'validated' and not (obj and obj['has_email']):
                state = 'review_required'
            role = ('client' if state == 'validated' else 'admin') if state != 'technical_error' else None
        return dict(state=state, role=role, object_id=obj['object_id'] if obj else None)


def compare_capture(record, legacy, baseline, objects):
    capture = record['pages']
    metadata = record['metadata']
    cohort = metadata['corpus']
    mail = metadata['mail']
    context = cohort['context'] if cohort else {}
    if not cohort and mail:
        source = min(mail, key=lambda item: item['received_at'])
        context = {'message_subject': source['subject'], 'received_at': source['received_at']}
    pages = [SimpleNamespace(extract_text=lambda text=text: text, extract_tables=lambda tables=tables: tables)
             for text, tables in capture]

    def open_capture(_):
        if record['error']:
            raise ValueError(record['error'])
        return nullcontext(SimpleNamespace(pages=pages))

    with patch('pdfplumber.open', open_capture):
        previous = baseline.analyze_pdf(record['path'], context)
        candidate = pdf_validation.analyze_pdf(record['path'], context)
    old = compare_legacy(legacy, capture)
    if record['error']:
        candidate['issues'][0]['error_type'] = record['error']
    if objects:
        old['routing'] = objects.route(old, legacy=True)
        for result in (previous, candidate):
            result['routing'] = objects.route(result)
    return {'sha256': record['sha'], 'cohort_193': bool(cohort), 'page_count': len(pages),
            'uids': sorted({msg['imap_uid'] for msg in mail if msg['imap_uid']}),
            'v1': old, 'previous_v2': previous, 'candidate_v2': candidate}


def sanitized_record(index, record):
    versions = {}
    for version in ('v1', 'previous_v2', 'candidate_v2'):
        result = record[version]
        coverage = [{
            'page': series.get('page', 1), 'pages': series.get('pages', [1]),
            'table': series.get('physical_table'), 'printout': series.get('printout'), 'kind': series['kind'],
            'detected_rows': series.get('detected_rows'), 'checked_rows': series['checked_rows'],
            'required_columns': series['required_columns'], 'optional_columns': series['optional_columns'],
            'checked_measurements': series.get('checked_measurements'),
        } for series in result.get('series', [])]
        issues = [{key: issue[key] for key in ('code', 'severity', 'page', 'printout', 'physical_table',
                                               'row', 'column', 'expected', 'found') if key in issue}
                  for issue in result.get('issues', [])]
        routing = result.get('routing', {})
        versions[version] = dict(state=result['state'], reason=result.get('reason'),
                                 printouts=result.get('printouts'), detected_tables=result.get('detected_tables'),
                                 checked_rows=result['checked_rows'], checked_measurements=result.get('checked_measurements'),
                                 route_state=routing.get('state'), recipient_role=routing.get('role'),
                                 object_found=bool(routing.get('object_id')), tables=coverage, issues=issues)
    return dict(case=index, cohort_193=record['cohort_193'], page_count=record['page_count'], **versions)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('capture', type=Path, help='JSONL.gz from the PDF archive extractor')
    parser.add_argument('legacy', type=Path, help='worker/pdf_validator.py from old')
    parser.add_argument('baseline', type=Path, help='common/pdf_validation.py from deployed V2')
    parser.add_argument('output', type=Path, help='Private JSONL output')
    parser.add_argument('--objects', type=Path, help='Read-only CSV snapshot of object bindings')
    parser.add_argument('--sanitized', type=Path, help='Optional anonymous JSONL.gz with coverage and decisions')
    parser.add_argument('--partial', action='store_true', help='Read completed records from an ongoing capture')
    args = parser.parse_args()
    legacy, baseline = legacy_validator(args.legacy), baseline_module(args.baseline)
    objects = None
    if args.objects:
        with args.objects.open(encoding='utf-8') as stream:
            objects = ObjectSnapshot(csv.DictReader(stream))
    totals = Counter()
    with (gzip.open(args.capture, 'rt', encoding='utf-8') as stream,
          args.output.open('w', encoding='utf-8') as output,
          gzip.open(args.sanitized, 'wt', encoding='utf-8') if args.sanitized else nullcontext() as sanitized):
        try:
            for index, line in enumerate(stream, 1):
                record = compare_capture(json.loads(line), legacy, baseline, objects)
                output.write(json.dumps(record, ensure_ascii=False) + '\n')
                if sanitized:
                    sanitized.write(json.dumps(sanitized_record(index, record), ensure_ascii=False) + '\n')
                candidate = record['candidate_v2']
                previous = record['previous_v2']
                previous_state, state = previous['state'], candidate['state']
                totals[f'v2: {previous_state} -> {state}'] += 1
                totals[f'v1: {record["v1"]["state"]} -> {state}'] += 1
                if record['cohort_193']:
                    totals[f'193: {previous_state} -> {state}'] += 1
                if (previous['state'], previous['checked_rows']) != (state, candidate['checked_rows']):
                    totals[f'change reason: {candidate["reason"]}'] += 1
        except (EOFError, json.JSONDecodeError):
            if not args.partial:
                raise
    print(json.dumps(totals, indent=2, ensure_ascii=False, sort_keys=True))


if __name__ == '__main__':
    main()
