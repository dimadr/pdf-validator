"""Correlate signed Mail.ru receipts that omit Original-Message-ID."""
import re
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime, getaddresses, parsedate_to_datetime
from html import unescape

from sqlalchemy import or_, select

from common.models import DeliveryIntent, RecipientDelivery

MONTHS = dict(zip('января февраля марта апреля мая июня июля августа сентября октября ноября декабря'.split(),
                  range(1, 13)))
SUBJECT = re.compile(r'^Прочитано письмо «([^»\r\n]{1,255})»[ \t]*\r?$', re.MULTILINE)
SEND_TIME = re.compile(r'^Напоминаем, что вы[ \u00a0]+отправляли это письмо [^,\r\n]+,\s*'
                       r'(\d{1,2}) ([а-я]+) (\d{4}), (\d{1,2}):(\d{2}) UTC\.', re.MULTILINE)


def _single_address(message, header):
    fields = message.get_all(header, [])
    addresses = getaddresses([str(value) for value in fields])
    return addresses[0][1] if len(fields) == len(addresses) == 1 else None


def _signed_receipt(message, raw, domain):
    import dkim
    from dkim.util import InvalidTagValueList, parse_tag_value
    from dns.exception import DNSException

    if any(len(message.get_all(name, [])) != 1 for name in ('From', 'To', 'Date', 'Subject', 'Content-Type')):
        return False
    for index, signature in enumerate(message.get_all('DKIM-Signature', [])):
        try:
            tags = parse_tag_value(str(signature).encode('ascii'))
            signed = {name.strip().lower() for name in tags.get(b'h', b'').split(b':')}
            # Require a signature covering all routing metadata and the complete body.
            if tags.get(b'd', b'').lower() != domain.encode('ascii') or b'l' in tags:
                continue
            if not {b'from', b'to', b'date', b'subject', b'content-type'}.issubset(signed):
                continue
            if dkim.DKIM(raw, timeout=3).verify(idx=index, dnsfunc=dkim.get_txt):
                return True
        except (InvalidTagValueList, dkim.DKIMException, DNSException, OSError, UnicodeError):
            continue
    return False


def _matches_subject(quoted, subject):
    if quoted.endswith('...'):
        prefix = quoted[:-3]
        return len(prefix) == 20 and len(subject) > len(prefix) and subject.startswith(prefix)
    return quoted == subject


def match_mailru_delivery(db, message, raw, account, recipient):
    """Return one accepted client delivery ID or leave the receipt unmatched."""
    if len(raw) > 256 * 1024 or _single_address(message, 'From') != recipient:
        return None
    if _single_address(message, 'To') != account or recipient.count('@') != 1:
        return None
    domain = recipient.rsplit('@', 1)[1].lower()
    if not message.get_all('DKIM-Signature'):
        return None
    parts = message.get_payload()
    if not isinstance(parts, list) or not parts:
        return None
    try:
        plain = next(part for part in parts[0].walk() if part.get_content_type() == 'text/plain')
        body = unescape(plain.get_payload(decode=True).decode('utf-8'))
        sent = SEND_TIME.search(body)
        quoted = SUBJECT.search(body)
        if not sent or not quoted:
            return None
        day, month, year, hour, minute = sent.groups()
        original_minute = datetime(int(year), MONTHS[month], int(day), int(hour), int(minute), tzinfo=timezone.utc)
        receipt_date = parsedate_to_datetime(message.get('Date', ''))
        if receipt_date.tzinfo is None or receipt_date < original_minute:
            return None
        latest_acceptance = receipt_date + timedelta(minutes=2)
    except (AttributeError, KeyError, OverflowError, StopIteration, ValueError):
        return None

    # The SMTP Date is frozen in the recipient row, including on retries.
    dates = [format_datetime(original_minute + timedelta(seconds=second)) for second in range(60)]
    candidates = db.execute(select(RecipientDelivery.id, RecipientDelivery.state, RecipientDelivery.role,
                                   DeliveryIntent.payload['subject'].astext)
        .join(DeliveryIntent, RecipientDelivery.intent_id == DeliveryIntent.id)
        .where(RecipientDelivery.state.in_(('accepted', 'uncertain', 'in_flight')),
               RecipientDelivery.recipient == recipient, RecipientDelivery.mime_date.in_(dates),
               or_(RecipientDelivery.accepted_at.is_(None), RecipientDelivery.accepted_at <= latest_acceptance)))
    matches = [(delivery_id, state, role) for delivery_id, state, role, subject in candidates
               if subject and _matches_subject(quoted[1], subject)]
    if len(matches) != 1:
        return None
    delivery_id, state, role = matches[0]
    if state == 'accepted' and role == 'client' and _signed_receipt(message, raw, domain):
        return delivery_id
    return None
