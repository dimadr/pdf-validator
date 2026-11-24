"""Read-only Brevo polling scoped to the SMTP ledger. No send or retry effects."""
import hashlib
import json
import os
import re
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from uuid import UUID

from sqlalchemy import func, or_, select
from sqlalchemy.dialects.postgresql import insert

from common.models import BrevoDeliveryEvent, BrevoPollCheckpoint, RecipientDelivery, Setting, SMTPAttempt, utcnow

EVENTS_URL = 'https://api.brevo.com/v3/smtp/statistics/events'
PAGE_SIZE = 1000
MAX_REQUESTS = 6
LOOKBACK = timedelta(days=30)
POLL_LOCK = 703271189042
BACKOFF_KEY = 'brevo_events_backoff'
SCAN_KEY = 'brevo_events_scan_v2'
FINAL_EVENTS = {'delivered': 'delivered', 'hardbounce': 'bounced', 'softbounce': 'bounced',
                'hardbounces': 'bounced', 'softbounces': 'bounced',
                'blocked': 'blocked', 'invalid': 'failed', 'error': 'failed', 'failed': 'failed'}
OPEN_EVENTS = {'opened', 'uniqueopened', 'proxyopen', 'proxyopened'}
MESSAGE_ID = re.compile(r'[^\s<>@]+@[^\s<>@]+')
ACK_ID = re.compile(r'\bqueued as\s+(<[^<>\s]+>)', re.IGNORECASE)


def canonical_id(value):
    if not isinstance(value, str):
        return None
    value = value.strip()
    if value.startswith('<') and value.endswith('>'):
        value = value[1:-1]
    if len(value) > 253 or not MESSAGE_ID.fullmatch(value):
        return None
    local, domain = value.rsplit('@', 1)
    return f'{local}@{domain.lower()}'


def recipient_key(value):
    address = canonical_id(value) if isinstance(value, str) and '<' not in value else None
    # Brevo can report different casing from the original SMTP envelope.
    return address.lower() if address and address.isascii() else address


def load_api_key():
    name = os.getenv('BREVO_API_KEY_FILE')
    if not name:
        return None
    path = Path(name)
    smtp_path = os.getenv('BREVO_SMTP_KEY_FILE')
    if smtp_path and path.resolve() == Path(smtp_path).resolve():
        return None
    try:
        key = path.read_text(encoding='utf-8').strip()
    except (OSError, UnicodeError):
        return None
    return key if key and not any(char.isspace() for char in key) else None


class BrevoUnavailable(Exception):
    def __init__(self, code, retry_after=0):
        super().__init__(code)
        self.code = code
        self.retry_after = retry_after


def _retry_after(value, now):
    try:
        if value.isdigit():
            return min(int(value), 86400)
        return max(0, min(int((parsedate_to_datetime(value) - now).total_seconds()), 86400))
    except (TypeError, ValueError, OverflowError):
        return 0


class BrevoEventsClient:
    def __init__(self, key, transport=None):
        import httpx

        self.httpx = httpx
        self.key = key
        self.http = httpx.Client(headers={'api-key': key, 'Accept': 'application/json'},
                                 timeout=httpx.Timeout(5, connect=3), transport=transport,
                                 follow_redirects=False, trust_env=False)

    def close(self):
        self.http.close()

    def page(self, start, end, offset):
        try:
            response = self.http.get(EVENTS_URL, params={
                'startDate': start.isoformat(), 'endDate': end.isoformat(),
                'sort': 'desc', 'offset': offset, 'limit': PAGE_SIZE,
            })
        except self.httpx.TimeoutException:
            raise BrevoUnavailable('timeout') from None
        except self.httpx.HTTPError:
            raise BrevoUnavailable('transport_error') from None
        if response.status_code != 200:
            code = response.status_code
            delay = _retry_after(response.headers.get('Retry-After', ''), utcnow()) if code == 429 or code >= 500 else 0
            if code in (401, 403):
                delay = 3600
            raise BrevoUnavailable(f'http_{code}', delay)
        try:
            if len(response.content) > 2_000_000 or self.key in response.text:
                raise ValueError
            rows = response.json().get('events', [])
            if not isinstance(rows, list) or len(rows) > PAGE_SIZE:
                raise ValueError
            return rows
        except (AttributeError, ValueError):
            raise BrevoUnavailable('invalid_response') from None


@dataclass(frozen=True)
class Target:
    attempt_id: UUID
    recipient: str
    identifiers: tuple
    started_at: datetime


def _targets(db, cutoff):
    base = select(SMTPAttempt, RecipientDelivery).join(RecipientDelivery, SMTPAttempt.delivery_id == RecipientDelivery.id)
    base = base.where(SMTPAttempt.provider == 'brevo', SMTPAttempt.state == 'accepted',
                      RecipientDelivery.smtp_provider == 'brevo', RecipientDelivery.state == 'accepted')
    due = db.execute(base.outerjoin(BrevoPollCheckpoint, SMTPAttempt.id == BrevoPollCheckpoint.smtp_attempt_id)
        .where(SMTPAttempt.started_at >= cutoff - LOOKBACK,
               func.coalesce(RecipientDelivery.accepted_at, SMTPAttempt.finished_at, SMTPAttempt.started_at) <= cutoff,
               or_(BrevoPollCheckpoint.next_poll_at.is_(None), BrevoPollCheckpoint.next_poll_at <= cutoff))).all()
    if not due:
        return [], {}

    identities = {}
    targets = {}
    recipients = {d.recipient for _, d in due}
    ascii_recipients = {recipient.lower() for recipient in recipients if recipient.isascii()}
    # Include historical collisions even when the other attempt is not due.
    same_recipient = or_(RecipientDelivery.recipient.in_(recipients),
                         func.lower(RecipientDelivery.recipient.collate('C')).in_(ascii_recipients))
    for attempt, delivery in db.execute(base.where(same_recipient)):
        ack = ACK_ID.search(attempt.response or '')
        identifiers = tuple(dict.fromkeys(filter(None, (canonical_id(ack[1]) if ack else None,
                                                       canonical_id(delivery.message_id)))))
        recipient = recipient_key(delivery.recipient)
        target = Target(attempt.id, recipient or '', identifiers if recipient else (), attempt.started_at)
        targets[attempt.id] = target
        for identifier in target.identifiers:
            identities.setdefault((recipient, identifier), set()).add(attempt.id)
    return [targets[a.id] for a, _ in due if a.id in targets], identities


def _event_kind(value):
    return re.sub(r'[^a-z]', '', value.lower()) if isinstance(value, str) else ''


def _event_date(value):
    try:
        when = datetime.fromisoformat(value.replace('Z', '+00:00'))
        return when.astimezone(timezone.utc) if when.tzinfo else None
    except (AttributeError, TypeError, ValueError, OverflowError):
        return None


def _store_event(db, raw, target, identities, now):
    if not isinstance(raw, dict):
        return None
    mid = canonical_id(raw.get('messageId'))
    recipient = recipient_key(raw.get('email'))
    if recipient != target.recipient or identities.get((recipient, mid)) != {target.attempt_id}:
        return None
    kind = _event_kind(raw.get('event'))[:64]
    when = _event_date(raw.get('date'))
    if not kind or not when or not target.started_at - timedelta(minutes=2) <= when <= now + timedelta(minutes=5):
        processing = 'invalid_event'
    elif kind in FINAL_EVENTS or kind == 'deferred':
        processing = 'delivery'
    elif kind in OPEN_EVENTS:
        processing = 'open_signal'
    else:
        processing = 'observed'
    key = hashlib.sha256(json.dumps([str(target.attempt_id), raw], sort_keys=True,
                                    separators=(',', ':')).encode()).hexdigest()
    reason = raw.get('reason')
    db.execute(insert(BrevoDeliveryEvent).values(event_key=key, smtp_attempt_id=target.attempt_id,
        provider_message_id=mid, event_type=kind or 'unknown', occurred_at=when,
        reason=reason if isinstance(reason, str) else None, processing_state=processing,
        payload=raw).on_conflict_do_nothing())
    return processing


def _backoff(db):
    row = db.scalar(select(Setting).where(Setting.key == BACKOFF_KEY))
    if row:
        try:
            value = json.loads(row.value)
            return _event_date(value['until']), max(0, int(value['failures']))
        except (AttributeError, TypeError, ValueError, KeyError):
            pass
    return None, 0


def _save_setting(db, key, value):
    serialized = json.dumps(value, default=lambda dt: dt.isoformat())
    db.execute(insert(Setting).values(key=key, value=serialized)
               .on_conflict_do_update(index_elements=[Setting.key], set_={'value': serialized}))


def _scan(db, now):
    raw = db.scalar(select(Setting.value).where(Setting.key == SCAN_KEY))
    if raw:
        try:
            state = json.loads(raw)
            cutoff = _event_date(state['cutoff'])
            offset = int(state['offset'])
            if cutoff and now - timedelta(hours=6) <= cutoff <= now and offset > 0:
                return cutoff, date.fromisoformat(state['start']), offset
        except (TypeError, ValueError, KeyError):
            pass
    return now, None, 0


def _checkpoints(db, updates, now):
    rows = [dict(smtp_attempt_id=attempt_id, last_error=error, page_offset=0, window_end=None,
                 next_poll_at=now + (timedelta(hours=6) if error else timedelta(minutes=10)),
                 last_polled_at=None if error else now) for attempt_id, error in updates]
    for index in range(0, len(rows), 500):
        stmt = insert(BrevoPollCheckpoint).values(rows[index:index + 500])
        db.execute(stmt.on_conflict_do_update(index_elements=[BrevoPollCheckpoint.smtp_attempt_id], set_={
            'next_poll_at': stmt.excluded.next_poll_at, 'last_error': stmt.excluded.last_error,
            'last_polled_at': func.coalesce(stmt.excluded.last_polled_at, BrevoPollCheckpoint.last_polled_at),
            'page_offset': 0, 'window_end': None,
        }))


def poll_brevo(factory, client, now=None):
    now = now or utcnow()
    deadline = time.monotonic() + 8
    report = {'state': 'ok', 'requests': 0, 'matched': 0, 'unmatched': 0}
    # Separate transaction keeps the lease short and pages durable even if a later request fails.
    with factory.begin() as guard:
        if not guard.scalar(select(func.pg_try_advisory_xact_lock(POLL_LOCK))):
            return {'state': 'busy'}
        until, failures = _backoff(guard)
        if until and until > now:
            return {'state': 'backoff', 'until': until.isoformat()}
        cutoff, start, offset = _scan(guard, now)
        targets, identities = _targets(guard, cutoff)
        unique = {target.attempt_id: target for target in targets
                  if any(identities[(target.recipient, mid)] == {target.attempt_id} for mid in target.identifiers)}
        with factory.begin() as db:
            _checkpoints(db, ((target.attempt_id, 'ambiguous_identifier' if target.identifiers else 'missing_identifier')
                              for target in targets if target.attempt_id not in unique), now)
            if not unique:
                row = db.scalar(select(Setting).where(Setting.key == SCAN_KEY))
                if row:
                    db.delete(row)
                return report
        # Overlap the preceding calendar day for provider date filters. The
        # exact timestamp and the (recipient, message ID) match stay mandatory.
        start = start or (min(target.started_at for target in unique.values()).date() - timedelta(days=1))
        end = cutoff.date()
        for _ in range(MAX_REQUESTS):
            if time.monotonic() >= deadline:
                break
            report['requests'] += 1
            try:
                events = client.page(start, end, offset)
            except BrevoUnavailable as error:
                delay = max(error.retry_after, min(3600, 300 * 2 ** min(failures, 4)))
                with factory.begin() as db:
                    _save_setting(db, BACKOFF_KEY, {'until': now + timedelta(seconds=delay), 'failures': failures+1})
                report.update(state='backoff', reason=error.code)
                return report
            full = len(events) == PAGE_SIZE
            with factory.begin() as db:
                for event in events:
                    key = (recipient_key(event.get('email')), canonical_id(event.get('messageId'))) if isinstance(event, dict) else None
                    owners = identities.get(key, ())
                    target = unique.get(next(iter(owners))) if len(owners) == 1 else None
                    if target and _store_event(db, event, target, identities, now) is not None:
                        report['matched'] += 1
                    else:
                        report['unmatched'] += 1
                if failures:
                    row = db.scalar(select(Setting).where(Setting.key == BACKOFF_KEY))
                    if row:
                        db.delete(row)
                    failures = 0
                if full:
                    offset += PAGE_SIZE
                    _save_setting(db, SCAN_KEY, {'cutoff': cutoff, 'start': start, 'offset': offset})
                else:
                    row = db.scalar(select(Setting).where(Setting.key == SCAN_KEY))
                    if row:
                        db.delete(row)
                    _checkpoints(db, ((attempt_id, None) for attempt_id in unique), now)
                    return report
        report['state'] = 'partial'
    return report
