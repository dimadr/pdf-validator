from datetime import timedelta
from uuid import UUID

import httpx
import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from backend.pipeline_views import delivery_page
from common import brevo_delivery as integration
from common.delivery import deliver_recipient, plan_intent
from common.ingest import BlobStore, ingest_uid
from common.models import BrevoDeliveryEvent, BrevoPollCheckpoint, ProcessingRevision, RecipientDelivery, SMTPAttempt, Setting, utcnow
from test_api_rc import api, api_databases, seed as api_seed


class Ack:
    def send(self, work):
        return {'code': 250, 'response': f'2.0.0 OK: queued as <provider-{work["id"].hex}@relay.example.com>'}


def send(factory, record, addresses, provider='brevo'):
    with factory.begin() as db:
        setting = db.scalar(select(Setting).where(Setting.key == 'smtp_provider'))
        if setting is None:
            setting = Setting(key='smtp_provider')
            db.add(setting)
        setting.value = provider
        revision = db.get(ProcessingRevision, UUID(str(record['revision'])))
        intent = plan_intent(db, revision, [(address, 'client') for address in addresses], f'test-{revision.id}')
        db.flush()
        rows = list(db.scalars(select(RecipientDelivery).where(RecipientDelivery.intent_id == intent.id)))
        ids = {row.recipient: row.id for row in rows}
    for delivery_id in ids.values():
        assert deliver_recipient(factory, delivery_id, Ack())['state'] == 'accepted'
    return ids


def provider_id(delivery_id):
    return f'<provider-{delivery_id.hex}@relay.example.com>'


def event(delivery_id, address, kind, at, **extra):
    return {'messageId': provider_id(delivery_id), 'email': address, 'event': kind,
            'date': at.isoformat(), **extra}


@pytest.fixture
def mock_api():
    clients = []
    def make(handler):
        client = integration.BrevoEventsClient('synthetic-api-key-only-for-tests', transport=httpx.MockTransport(handler))
        clients.append(client)
        return client
    yield make
    for client in clients:
        client.close()


def test_exact_provider_identity_per_recipient_and_mdn_independent(session_factory, seed, tmp_path, monkeypatch, mock_api):
    first, second, fallback = seed(), seed(), seed()
    alice, bob = 'alice@example.com', 'bob@example.com'
    ids = send(session_factory, first, [alice, bob])
    other = send(session_factory, second, [alice])[alice]
    send(session_factory, fallback, [alice], provider='timeweb')
    now = utcnow()
    pages = {
        provider_id(ids[alice]): [event(ids[alice], alice, 'delivered', now),
                                  event(ids[alice], alice, 'deferred', now+timedelta(seconds=1)),
                                  event(ids[alice], bob, 'blocked', now)],
        provider_id(ids[bob]): [event(ids[bob], bob, 'opened', now)],
        provider_id(other): [event(other, alice, 'hard_bounce', now, reason='Mailbox unavailable')],
    }
    requests = []
    def handler(request):
        params = request.url.params
        requests.append((params['messageId'], params['email']))
        assert request.method == 'GET' and str(request.url).startswith(integration.EVENTS_URL)
        return httpx.Response(200, json={'events': pages[params['messageId']]})
    client = mock_api(handler)
    result = integration.poll_brevo(session_factory, client, now)
    assert (result['requests'], result['matched'], result['unmatched']) == (3, 4, 1)
    assert set(requests) == {(provider_id(ids[alice]), alice), (provider_id(ids[bob]), bob), (provider_id(other), alice)}
    integration.poll_brevo(session_factory, client, now+timedelta(minutes=31))

    with session_factory() as db:
        row = db.get(RecipientDelivery, ids[alice])
        assert row.message_id != provider_id(row.id)
        mid = row.message_id
    raw = (f'From: {alice}\r\nMIME-Version: 1.0\r\n'
           'Content-Type: multipart/report; report-type=disposition-notification; boundary="mdn"\r\n\r\n'
           '--mdn\r\nContent-Type: message/disposition-notification\r\n\r\n'
           f'Final-Recipient: rfc822; {alice}\r\nOriginal-Message-ID: {mid}\r\n'
           'Disposition: manual-action/MDN-sent-manually; displayed\r\n\r\n--mdn--\r\n').encode()
    monkeypatch.setenv('UPLOAD_DIR', str(tmp_path))
    ingest_uid(session_factory, BlobStore(tmp_path), 'account', 'INBOX', 1, 9, raw)
    with session_factory() as db:
        assert db.scalar(select(func.count()).select_from(BrevoDeliveryEvent)) == 4
        assert db.scalar(select(func.count()).select_from(SMTPAttempt)) == 4
        rows = {row['recipient']: row for row in delivery_page(db, first['attachment'])['items']}
        assert rows[alice]['delivery']['status'] == 'delivered' and rows[alice]['read_confirmed_at']
        assert rows[bob]['delivery']['status'] == 'unknown' and rows[bob]['delivery']['opened_at']
        assert rows[bob]['read_confirmed_at'] is None
        bounce = delivery_page(db, second['attachment'])['items'][0]['delivery']
        assert bounce['status'] == 'bounced' and bounce['reason'] == 'Mailbox unavailable'
        assert delivery_page(db, fallback['attachment'])['items'][0]['delivery']['status'] == 'unknown'


def test_ambiguous_ids_never_promote_delivery(session_factory, seed, mock_api):
    address = 'same@example.com'
    ids = [send(session_factory, seed(), [address])[address] for _ in range(2)]
    with session_factory.begin() as db:
        for delivery_id in ids:
            db.get(RecipientDelivery, delivery_id).message_id = '<collision@example.com>'
            attempt = db.scalar(select(SMTPAttempt).where(SMTPAttempt.delivery_id == delivery_id))
            attempt.response = '250 queued as <collision@example.com>'
    client = mock_api(lambda _: pytest.fail('Ambiguous identifier must not reach API'))
    assert integration.poll_brevo(session_factory, client)['requests'] == 0
    with session_factory() as db:
        assert set(db.scalars(select(BrevoPollCheckpoint.last_error))) == {'ambiguous_identifier'}
        assert not db.scalar(select(BrevoDeliveryEvent.event_key))


def test_checkpoint_resumes_pagination_across_invocations(session_factory, seed, monkeypatch, mock_api):
    monkeypatch.setattr(integration, 'PAGE_SIZE', 2)
    address = 'page@example.com'
    record = seed()
    delivery_id = send(session_factory, record, [address])[address]
    now = utcnow()
    pages = {0: [event(delivery_id, address, 'requests', now), event(delivery_id, address, 'deferred', now)],
             2: [event(delivery_id, address, 'delivered', now)]}
    offsets = []
    dates = []
    def handler(request):
        offset = int(request.url.params['offset'])
        offsets.append(offset)
        dates.append(request.url.params['endDate'])
        return httpx.Response(200, json={'events': pages[offset]})
    integration.poll_brevo(session_factory, mock_api(handler), now)
    integration.poll_brevo(session_factory, mock_api(handler), now+timedelta(minutes=11))
    assert offsets == [0, 2] and dates[0] == dates[1]
    with session_factory() as db:
        assert db.scalar(select(BrevoPollCheckpoint.page_offset)) == 0
        assert delivery_page(db, record['attachment'])['items'][0]['delivery']['status'] == 'delivered'


def test_poll_budget_services_remaining_sends_on_next_tick(session_factory, seed, monkeypatch, mock_api):
    monkeypatch.setattr(integration, 'MAX_REQUESTS', 2)
    send(session_factory, seed(), ['a@example.com', 'b@example.com', 'c@example.com'])
    now = utcnow()
    recipients = []
    def handler(request):
        recipients.append(request.url.params['email'])
        return httpx.Response(200, json={'events': []})
    assert integration.poll_brevo(session_factory, mock_api(handler), now)['requests'] == 2
    assert integration.poll_brevo(session_factory, mock_api(handler), now+timedelta(minutes=10))['requests'] == 1
    assert set(recipients) == {'a@example.com', 'b@example.com', 'c@example.com'}


@pytest.mark.parametrize('failure', [429, 503, 'timeout'])
def test_api_failure_persists_backoff_and_never_retries_smtp(session_factory, seed, mock_api, failure):
    address = 'failure@example.com'
    send(session_factory, seed(), [address])
    now = utcnow()
    calls = []
    def handler(request):
        calls.append(request)
        if failure == 'timeout':
            raise httpx.ReadTimeout('synthetic', request=request)
        return httpx.Response(failure, headers={'Retry-After': '1200'})
    result = integration.poll_brevo(session_factory, mock_api(handler), now)
    assert result['state'] == 'backoff' and len(calls) == 1
    assert integration.poll_brevo(session_factory, mock_api(handler), now+timedelta(minutes=1))['state'] == 'backoff'
    send(session_factory, seed(), [address])
    with session_factory() as db:
        assert db.scalar(select(func.count()).select_from(SMTPAttempt)) == 2
        assert db.scalar(select(BrevoPollCheckpoint.page_offset)) == 0
        assert not db.scalar(select(BrevoDeliveryEvent.event_key))


def test_admin_event_history_is_scoped(api, tmp_path, mock_api):
    record = api_seed(api.engine, tmp_path, validated=True)
    factory = sessionmaker(bind=api.engine)
    address = 'admin-view@example.com'
    delivery_id = send(factory, record, [address])[address]
    now = utcnow()
    client = mock_api(lambda _: httpx.Response(200, json={'events': [event(delivery_id, address, 'delivered', now)]}))
    integration.poll_brevo(factory, client, now)
    path = f"/api/v1/attachments/{record['attachment']}/delivery-events?delivery_id={delivery_id}"
    response = api.request('GET', path)
    assert response.status_code == 200 and response.json()['items'][0]['recipient'] == address
    api.user.role = 'operator'
    assert api.request('GET', path).status_code == 403
