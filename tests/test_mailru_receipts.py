from datetime import datetime, timezone
from email.message import EmailMessage
from email.policy import SMTP as SMTP_POLICY
from email.utils import format_datetime
from base64 import b64encode

import dkim
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from sqlalchemy import select

from common.ingest import BlobStore
from common.mailbox_archive import NOTIFICATIONS, archive_mailbox, folder_name
from common.models import Attachment, DeliveryIntent, OutboxEvent, RecipientDelivery
from test_delivery_core import SMTP, deliver_recipient, planned
from test_mailbox_archive import Source

ACCOUNT = 'reports@example.com'
RECIPIENT = 'synthetic-recipient@bk.ru'
SUBJECT = 'Отчет ТЕСТ MDN 09.11.2026'
SENT_AT = datetime(2026, 11, 9, 7, 57, 32, tzinfo=timezone.utc)


@pytest.fixture(scope='module')
def signing_key():
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


def signed_receipt(key, monkeypatch, original_id=None):
    receipt = EmailMessage()
    receipt['From'] = RECIPIENT
    receipt['To'] = ACCOUNT
    receipt['Date'] = 'Mon, 09 Nov 2026 10:58:17 +0300'
    receipt['Subject'] = 'Подтверждение прочтения'
    receipt.set_type('multipart/report')
    receipt.set_param('report-type', 'disposition-notification')
    body = EmailMessage()
    body.set_content('Здравствуйте!\n\nПрочитано письмо «Отчет ТЕСТ MDN 09.11...»\n\n'
                     'Напоминаем, что вы\u00a0отправляли это письмо Понедельник,  9 ноября 2026, 7:57 UTC.\n')
    receipt.attach(body)
    part = EmailMessage()
    part.set_type('message/disposition-notification')
    report = EmailMessage()
    report['Final-Recipient'] = 'rfc822;' + RECIPIENT
    report['Disposition'] = 'manual-action/MDN-sent-manually; displayed'
    if original_id:
        report['Original-Message-ID'] = original_id
    part.set_payload([report])
    receipt.attach(part)
    private = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.TraditionalOpenSSL,
                                serialization.NoEncryption())
    public = key.public_key().public_bytes(serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo)
    monkeypatch.setattr(dkim, 'get_txt', lambda name, timeout: b'v=DKIM1; p=' + b64encode(public))
    raw = receipt.as_bytes(policy=SMTP_POLICY)
    signature = dkim.sign(raw, b'mail4', b'bk.ru', private,
                          include_headers=[b'from', b'to', b'date', b'subject', b'content-type'])
    return signature + raw


def accepted_send(factory, seed):
    record = seed()
    delivery_id = planned(factory, record, [RECIPIENT])[0]
    deliver_recipient(factory, delivery_id, SMTP())
    with factory.begin() as db:
        row = db.get(RecipientDelivery, delivery_id)
        row.mime_date = format_datetime(SENT_AT)
        row.accepted_at = SENT_AT
        db.get(DeliveryIntent, row.intent_id).payload = {'subject': SUBJECT}
    return record, delivery_id


@pytest.mark.parametrize('case', ['unique', 'ambiguous', 'uncertain_collision', 'bad_signature', 'wrong_id'])
def test_mailru_sent_receipt_is_archived_only_for_verified_unique_send(
        session_factory, seed, tmp_path, monkeypatch, signing_key, case):
    record, delivery_id = accepted_send(session_factory, seed)
    if case in ('ambiguous', 'uncertain_collision'):
        _, conflicting_id = accepted_send(session_factory, seed)
        if case == 'uncertain_collision':
            with session_factory.begin() as db:
                row = db.get(RecipientDelivery, conflicting_id)
                row.state = 'uncertain'
                row.accepted_at = None
    raw = signed_receipt(signing_key, monkeypatch, '<wrong@example.com>' if case == 'wrong_id' else None)
    if case == 'bad_signature':
        monkeypatch.setattr(dkim, 'get_txt', lambda name, timeout: b'v=DKIM1; p=')
    source = Source(b'')
    source.boxes = {'INBOX': {}, 'Sent': {196: raw, 197: b'Subject: ordinary outgoing email\r\n\r\n'}}
    request = source.connection.uid

    def timeweb_search(command, *args):
        if command == 'SEARCH' and args[1:3] == ('HEADER', 'Content-Type'):
            return 'OK', [b'']  # Timeweb misses even existing Content-Type headers.
        return request(command, *args)

    monkeypatch.setattr(source.connection, 'uid', timeweb_search)
    store = BlobStore(tmp_path / 'store')
    with session_factory() as db:
        existing_outbox = set(db.scalars(select(OutboxEvent.id)))
    result = archive_mailbox(session_factory, source, store, ACCOUNT)
    assert result['moved_or_confirmed'] == int(case == 'unique')
    assert 197 in source.boxes['Sent']
    assert (196 in source.boxes['Sent']) == (case != 'unique')
    if case == 'unique':
        assert list(source.boxes[folder_name(NOTIFICATIONS)].values()) == [raw]
        assert archive_mailbox(session_factory, source, store, ACCOUNT)['moved_or_confirmed'] == 0
    with session_factory() as db:
        assert bool(db.get(Attachment, record['attachment']).read_receipt_received) == (case == 'unique')
        assert db.get(RecipientDelivery, delivery_id).attempts == 1
        assert set(db.scalars(select(OutboxEvent.id))) == existing_outbox
