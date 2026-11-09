"""Read-only Brevo polling scoped to the SMTP ledger. No send or retry effects."""
import hashlib
import json
import os
import re
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from uuid import UUID

from sqlalchemy import func, or_, select
from sqlalchemy.dialects.postgresql import insert

from common.models import BrevoDeliveryEvent, BrevoPollCheckpoint, RecipientDelivery, Setting, SMTPAttempt, utcnow

EVENTS_URL = 'https://api.brevo.com/v3/smtp/statistics/events'
PAGE_SIZE = 100
MAX_REQUESTS = 24
LOOKBACK = timedelta(days=30)
POLL_LOCK = 703271189042
BACKOFF_KEY = 'brevo_events_backoff'
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
    return canonical_id(value) if isinstance(value, str) and '<' not in value else None


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
                                 timeout=httpx.Timeout(3, connect=2), transport=transport,
                                 follow_redirects=False, trust_env=False)

    def close(self):
        self.http.close()

    def page(self, identifier, recipient, start, end, offset):
        try:
            response = self.http.get(EVENTS_URL, params={
                'messageId': f'<{identifier}>', 'email': recipient,
                'startDate': start.isoformat(), 'endDate': end.isoformat(),
                'sort': 'asc', 'offset': offset, 'limit': PAGE_SIZE,
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
            if len(response.content) > 1_000_000 or self.key in response.text:
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


def _targets(db, now):
    base = select(SMTPAttempt, RecipientDelivery).join(RecipientDelivery, SMTPAttempt.delivery_id == RecipientDelivery.id)
    base = base.where(SMTPAttempt.provider == 'brevo', SMTPAttempt.state == 'accepted',
                      RecipientDelivery.smtp_provider == 'brevo', RecipientDelivery.state == 'accepted')
    due = db.execute(base.outerjoin(BrevoPollCheckpoint, SMTPAttempt.id == BrevoPollCheckpoint.smtp_attempt_id)
        .where(SMTPAttempt.started_at >= now - LOOKBACK,
               or_(BrevoPollCheckpoint.next_poll_at.is_(None), BrevoPollCheckpoint.next_poll_at <= now))
        .order_by(func.coalesce(BrevoPollCheckpoint.next_poll_at, SMTPAttempt.started_at), SMTPAttempt.id)
        .limit(MAX_REQUESTS)).all()
    if not due:
        return [], {}

    identities = {}
    targets = {}
    for attempt, delivery in db.execute(base.where(RecipientDelivery.recipient.in_({d.recipient for _, d in due}))):
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
        targets, identities = _targets(guard, now)
        for target in targets:
            if time.monotonic() >= deadline:
                break
            with factory.begin() as db:
                cursor = db.get(BrevoPollCheckpoint, target.attempt_id)
                if cursor is None:
                    cursor = BrevoPollCheckpoint(smtp_attempt_id=target.attempt_id, next_poll_at=now, page_offset=0)
                    db.add(cursor)
                known = [mid for mid in target.identifiers if identities[(target.recipient, mid)] == {target.attempt_id}]
                if not known:
                    cursor.next_poll_at = now + timedelta(hours=6)
                    cursor.last_error = 'ambiguous_identifier' if target.identifiers else 'missing_identifier'
                    continue
                end = cursor.window_end or now
                report['requests'] += 1
                try:
                    events = client.page(known[0], target.recipient, target.started_at.date(), end.date(), cursor.page_offset)
                except BrevoUnavailable as error:
                    delay = max(error.retry_after, min(3600, 600 * 2 ** min(failures, 3)))
                    values = json.dumps({'until': (now + timedelta(seconds=delay)).isoformat(), 'failures': failures+1})
                    db.execute(insert(Setting).values(key=BACKOFF_KEY, value=values)
                               .on_conflict_do_update(index_elements=[Setting.key], set_={'value': values}))
                    cursor.last_error = error.code
                    report.update(state='backoff', reason=error.code)
                    return report
                unmatched = 0
                for event in events:
                    if _store_event(db, event, target, identities, now) is None:
                        unmatched += 1
                report['unmatched'] += unmatched
                report['matched'] += len(events) - unmatched
                full = len(events) == PAGE_SIZE
                cursor.last_error = 'unmatched_event' if unmatched else None
                cursor.last_polled_at = now
                cursor.page_offset = cursor.page_offset + PAGE_SIZE if full else 0
                cursor.window_end = end if full else None
                cursor.next_poll_at = now + timedelta(minutes=10 if full else 30)
                if failures:
                    row = db.scalar(select(Setting).where(Setting.key == BACKOFF_KEY))
                    if row:
                        db.delete(row)
                    failures = 0
    return report
