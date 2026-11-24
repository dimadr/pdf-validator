from datetime import timedelta
from uuid import UUID

import httpx
import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from backend.pipeline_views import delivery_page, pipeline_view
from common import brevo_delivery as integration
from common.delivery import deliver_recipient, plan_intent
from common.ingest import BlobStore, ingest_uid
from common.models import Attachment, BrevoDeliveryEvent, BrevoPollCheckpoint, ProcessingRevision, RecipientDelivery, SMTPAttempt, Setting, utcnow
from test_api_rc import api, api_databases, seed as api_seed


def test_brevo_is_polled_every_five_minutes_without_slowing_imap(monkeypatch):
    monkeypatch.setenv('DATABASE_URL', 'sqlite://')
    monkeypatch.setenv('REDIS_URL', 'memory://')
    from worker.celery_app import celery_app

    schedule = celery_app.conf.beat_schedule
    assert schedule['brevo-delivery-events'] == {'task': 'pipeline.poll_brevo_events', 'schedule': 300.0}
    assert schedule['fetch-emails-every-2-minutes']['schedule'] == 120.0


class Ack:
    def send(self, work):
        return {'code': 250, 'response': f'2.0.0 OK: queued as <provider-{work["id"].hex}@relay.example.com>'}


def send(factory, record, addresses, provider='brevo', kind='automatic'):
    with factory.begin() as db:
        setting = db.scalar(select(Setting).where(Setting.key == 'smtp_provider'))
        if setting is None:
            setting = Setting(key='smtp_provider')
            db.add(setting)
        setting.value = provider
        revision = db.get(ProcessingRevision, UUID(str(record['revision'])))
        intent = plan_intent(db, revision, [(address, 'client') for address in addresses], f'test-{revision.id}-{kind}', kind=kind)
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
        requests.append(params['startDate'])
        assert request.method == 'GET' and str(request.url).startswith(integration.EVENTS_URL)
        assert 'messageId' not in params and 'email' not in params
        return httpx.Response(200, json={'events': [item for page in pages.values() for item in page]})
    client = mock_api(handler)
    result = integration.poll_brevo(session_factory, client, now)
    assert (result['requests'], result['matched'], result['unmatched']) == (1, 4, 1)
    assert requests == [(now.date() - timedelta(days=1)).isoformat()]
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
        attachment = db.get(Attachment, first['attachment'])
        assert pipeline_view(db, attachment)['client_delivery']['confirmation'] == {
            'confirmed': 1, 'total': 2, 'brevo_delivered': 1, 'read_confirmed': 1,
        }
        bounce = next(row['delivery'] for row in delivery_page(db, second['attachment'])['items']
                      if row['role'] == 'client')
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
    monkeypatch.setattr(integration, 'MAX_REQUESTS', 1)
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
    assert integration.poll_brevo(session_factory, mock_api(handler), now)['state'] == 'partial'
    assert integration.poll_brevo(session_factory, mock_api(handler), now+timedelta(minutes=5))['state'] == 'ok'
    assert offsets == [0, 2] and dates[0] == dates[1]
    with session_factory() as db:
        assert db.scalar(select(BrevoPollCheckpoint.page_offset)) == 0
        assert delivery_page(db, record['attachment'])['items'][0]['delivery']['status'] == 'delivered'


def test_batch_catches_up_mass_sends_and_leaves_no_event_unknown(session_factory, seed, mock_api):
    record = seed()
    addresses = [f'bulk-{i}@example.com' for i in range(36)]
    deliveries = send(session_factory, record, addresses)
    now = utcnow()
    events = [event(deliveries[address], address, 'delivered', now) for address in addresses[:-2]]
    events.append(event(deliveries[addresses[-2]], addresses[-2], 'deferred', now))
    result = integration.poll_brevo(session_factory, mock_api(lambda _: httpx.Response(200, json={'events': events})), now)
    assert (result['requests'], result['matched']) == (1, 35)
    with session_factory() as db:
        statuses = {row['recipient']: row['delivery']['status']
                    for row in delivery_page(db, record['attachment'])['items']}
        assert list(statuses.values()).count('delivered') == 34
        assert statuses[addresses[-2]] == 'deferred' and statuses[addresses[-1]] == 'unknown'
        assert db.scalar(select(func.count()).select_from(SMTPAttempt)) == len(addresses)


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
        assert db.scalar(select(BrevoPollCheckpoint.page_offset)) is None
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


def test_document_list_counts_delivery_and_mdn_independently_for_current_clients(api, tmp_path, mock_api):
    factory = sessionmaker(bind=api.engine)
    both, delivered, ack, opened, read, partial = [api_seed(api.engine, tmp_path, validated=True) for _ in range(6)]
    alice, bob = 'alice@example.com', 'bob@example.com'
    both_ids = send(factory, both, [alice])
    delivered_ids = send(factory, delivered, [alice])
    ack_ids = send(factory, ack, [alice])
    opened_ids = send(factory, opened, [alice])
    partial_ids = send(factory, partial, [alice, bob])
    send(factory, read, [alice], provider='timeweb')
    now = utcnow()
    with factory.begin() as db:
        for item in (both, read):
            attachment = db.get(Attachment, UUID(item['attachment']))
            attachment.read_receipt_received = True
            attachment.read_receipt_at = now

    pages = {provider_id(both_ids[alice]): [event(both_ids[alice], alice, 'delivered', now)],
             provider_id(delivered_ids[alice]): [event(delivered_ids[alice], alice, 'delivered', now)],
             provider_id(ack_ids[alice]): [],
             provider_id(opened_ids[alice]): [event(opened_ids[alice], alice, 'opened', now)],
             provider_id(partial_ids[alice]): [event(partial_ids[alice], alice, 'delivered', now)],
             provider_id(partial_ids[bob]): [event(partial_ids[bob], bob, 'opened', now)]}
    client = mock_api(lambda _: httpx.Response(200, json={'events': [item for page in pages.values() for item in page]}))
    integration.poll_brevo(factory, client, now)

    def documents():
        response = api.request('GET', '/api/v1/attachments')
        assert response.status_code == 200, response.text
        return {row['id']: row for row in response.json()}

    rows = documents()
    for item, total, confirmed, brevo, mdn in (
        (both, 1, 1, 1, 1), (delivered, 1, 1, 1, 0), (ack, 1, 0, 0, 0),
        (opened, 1, 0, 0, 0), (read, 1, 1, 0, 1), (partial, 2, 1, 1, 0),
    ):
        assert rows[item['attachment']]['client_delivery']['confirmation'] == {
            'confirmed': confirmed, 'total': total, 'brevo_delivered': brevo, 'read_confirmed': mdn,
        }

    send(factory, delivered, [alice], kind='manual_resend')
    send(factory, read, [alice], provider='timeweb', kind='manual_resend')
    rows = documents()
    for item in (delivered, read):
        assert rows[item['attachment']]['client_delivery']['confirmation'] == {
            'confirmed': 0, 'total': 1, 'brevo_delivered': 0, 'read_confirmed': 0,
        }


def test_case_variant_catches_up_existing_delivery_without_resending(api, tmp_path, mock_api):
    factory = sessionmaker(bind=api.engine)
    record = api_seed(api.engine, tmp_path, validated=True)
    address = 'Meter.Office@example.com'
    other = 'other@example.com'
    ids = send(factory, record, [address, other])
    now = utcnow()
    historical = now - timedelta(days=1)
    with factory.begin() as db:
        delivery = db.get(RecipientDelivery, ids[address])
        delivery.accepted_at = historical
        attempt = db.scalar(select(SMTPAttempt).where(SMTPAttempt.delivery_id == ids[address]))
        attempt.started_at = attempt.finished_at = historical
        db.add(BrevoPollCheckpoint(smtp_attempt_id=attempt.id, next_poll_at=now))

    events = [
        event(ids[address], address.lower(), 'delivered', historical + timedelta(seconds=1)),
        event(ids[other], 'someone@example.com', 'delivered', now),
        event(ids[other], other, 'softBounces', now, reason='450 synthetic temporary rejection'),
    ]
    client = mock_api(lambda _: httpx.Response(200, json={'events': events}))
    assert integration.poll_brevo(factory, client, now)['matched'] == 2

    path = f"/api/v1/attachments/{record['attachment']}"
    detail = api.request('GET', path)
    assert detail.status_code == 200
    assert detail.json()['client_delivery']['confirmation'] == {
        'total': 2, 'confirmed': 1, 'brevo_delivered': 1, 'read_confirmed': 0,
    }
    history = api.request('GET', path + '/deliveries')
    assert history.status_code == 200
    statuses = {row['recipient']: row['delivery']['status'] for row in history.json()['items']}
    assert statuses == {address: 'delivered', other: 'deferred'}
    integration.poll_brevo(factory, client, now + timedelta(minutes=11))
    with factory() as db:
        assert db.scalar(select(func.count()).select_from(SMTPAttempt)) == 2
        assert db.scalar(select(func.count()).select_from(BrevoDeliveryEvent)) == 2


def test_case_variant_collision_with_not_due_attempt_is_ambiguous(session_factory, seed, mock_api):
    first, second = 'Meter.Office@example.com', 'meter.office@example.com'
    first_id = send(session_factory, seed(), [first])[first]
    second_id = send(session_factory, seed(), [second])[second]
    now = utcnow()
    with session_factory.begin() as db:
        for delivery_id in (first_id, second_id):
            delivery = db.get(RecipientDelivery, delivery_id)
            delivery.message_id = '<same-id@example.com>'
            attempt = db.scalar(select(SMTPAttempt).where(SMTPAttempt.delivery_id == delivery_id))
            attempt.response = '250 queued as <same-id@example.com>'
            if delivery_id == second_id:
                db.add(BrevoPollCheckpoint(smtp_attempt_id=attempt.id, next_poll_at=now + timedelta(hours=1)))
    client = mock_api(lambda _: pytest.fail('Ambiguous identifier must not reach API'))
    assert integration.poll_brevo(session_factory, client, now)['requests'] == 0
    with session_factory() as db:
        assert list(db.scalars(select(BrevoPollCheckpoint.last_error).where(
            BrevoPollCheckpoint.last_error.is_not(None)))) == ['ambiguous_identifier']
        assert db.scalar(select(func.count()).select_from(BrevoDeliveryEvent)) == 0


@pytest.mark.parametrize('followup,status,alert_phases', [
    ('delivered', 'delivered', ['temporary']),
    ('hardBounces', 'bounced', ['final', 'temporary']),
])
def test_dispatcher_alert_transitions_are_per_recipient_and_idempotent(
        api, tmp_path, mock_api, monkeypatch, followup, status, alert_phases):
    from common.delivery import DISPATCHER_ALERT_TASK
    from common.models import DeliveryIntent, OutboxEvent

    factory = sessionmaker(bind=api.engine)
    dispatcher = 'dispatcher@example.invalid'
    monkeypatch.setenv('NOTIFY_EMAIL', dispatcher)
    record = api_seed(api.engine, tmp_path, validated=True)
    first, other = 'target@example.invalid', 'other@example.invalid'
    ids = send(factory, record, [first, other])
    now = utcnow()
    monkeypatch.setenv('BREVO_DELIVERY_ALERTS_SINCE', (now - timedelta(minutes=1)).isoformat())
    reason = '450 4.1.8 Sender address rejected: Domain not found'
    soft = event(ids[first], first, 'softBounces', now, reason=reason)
    rows = [soft, dict(soft), {**soft, 'tag': 'duplicate metadata'}]
    client = mock_api(lambda _: httpx.Response(200, json={'events': rows}))
    initial = integration.poll_brevo(factory, client, now)
    assert initial['dispatcher_alerts'] == 1

    def alerts(db):
        return db.execute(select(DeliveryIntent, RecipientDelivery)
            .join(RecipientDelivery).where(DeliveryIntent.request_key.like('brevo-delivery-alert:%'))).all()

    with factory() as db:
        [(intent, alert)] = alerts(db)
        assert intent.kind == 'admin_notification' and alert.role == 'admin' and alert.recipient == dispatcher
        expected = {'smtp_code': 450, 'smtp_enhanced_code': '4.1.8', 'reason': reason,
                    'recipient': first, 'calculator_number': db.get(ProcessingRevision, UUID(record['revision'])).calculator_number,
                    'at': now.isoformat(), 'provider_message_id': integration.canonical_id(provider_id(ids[first])),
                    'event': 'softbounces'}
        event_row = db.scalar(select(BrevoDeliveryEvent).where(BrevoDeliveryEvent.event_type == 'softbounces'))
        assert intent.payload['delivery_problem'] == event_row.payload['delivery_problem']
        assert all(intent.payload['delivery_problem'][key] == value for key, value in expected.items())
        assert intent.payload['delivery_problem']['object']
        assert db.scalar(select(OutboxEvent.topic).where(
            OutboxEvent.payload['delivery_id'].astext == str(alert.id))) == DISPATCHER_ALERT_TASK
        alert_id = alert.id

    class DispatcherAck(Ack):
        def send(self, work):
            assert work['recipient'] == dispatcher and work['content'] is None
            assert reason in work['payload']['body']
            return super().send(work)

    assert deliver_recipient(factory, alert_id, DispatcherAck())['state'] == 'skipped'
    assert deliver_recipient(factory, ids[other], DispatcherAck(), dispatcher_alert=True)['state'] == 'skipped'
    assert deliver_recipient(factory, alert_id, DispatcherAck(), dispatcher_alert=True)['state'] == 'accepted'

    next_poll = now + timedelta(minutes=11)
    rows.extend([
        event(ids[first], first, followup, now + timedelta(minutes=1), reason='550 5.1.1 Mailbox unavailable')
        if status == 'bounced' else event(ids[first], first, followup, now + timedelta(minutes=1)),
        event(alert_id, dispatcher, 'softBounces', now + timedelta(minutes=2), reason=reason),
    ])
    integration.poll_brevo(factory, client, next_poll)
    integration.poll_brevo(factory, client, next_poll + timedelta(minutes=11))

    history = api.request('GET', f"/api/v1/attachments/{record['attachment']}/deliveries")
    assert history.status_code == 200
    indexed = {row['recipient']: row for row in history.json()['items'] if row['role'] == 'client'}
    assert indexed[first]['delivery']['status'] == status
    assert indexed[first]['delivery']['recovered'] == (status == 'delivered')
    assert indexed[first]['delivery']['previous_problem'] == (reason if status == 'delivered' else None)
    assert indexed[other]['delivery']['status'] == 'unknown'

    with factory() as db:
        assert sorted(intent.request_key.rpartition(':')[2] for intent, _ in alerts(db)) == alert_phases
        attempts = list(db.scalars(select(SMTPAttempt).join(RecipientDelivery).where(RecipientDelivery.role == 'client')))
        assert len(attempts) == 2 and all(attempt.attempt == 1 for attempt in attempts)


def test_initial_start_boundary_avoids_historical_alerts(session_factory, seed, mock_api, monkeypatch):
    from common.models import DeliveryIntent

    address = 'target@example.invalid'
    monkeypatch.delenv('BREVO_DELIVERY_ALERTS_SINCE', raising=False)
    delivery_id = send(session_factory, seed(), [address])[address]
    occurred = utcnow()
    row = event(delivery_id, address, 'softBounces', occurred, reason='451 4.4.0 Temporary failure')
    client = mock_api(lambda _: httpx.Response(200, json={'events': [row]}))
    now = occurred + timedelta(minutes=2)
    assert integration.poll_brevo(session_factory, client, now)['matched'] == 1
    integration.poll_brevo(session_factory, client, now + timedelta(minutes=11))
    with session_factory() as db:
        assert not db.scalar(select(DeliveryIntent.id).where(DeliveryIntent.kind == 'admin_notification'))
        assert db.scalar(select(BrevoDeliveryEvent.event_type)) == 'softbounces'


def test_existing_failure_can_be_alerted_with_scoped_start_without_editing_event(
        session_factory, seed, mock_api, monkeypatch):
    from common.models import DeliveryIntent

    address = 'target@example.invalid'
    delivery_id = send(session_factory, seed(), [address])[address]
    now = utcnow()
    reason = '450 4.1.8 Sender address rejected: Domain not found'
    with session_factory.begin() as db:
        attempt_id = db.scalar(select(SMTPAttempt.id).where(SMTPAttempt.delivery_id == delivery_id))
        db.add(BrevoDeliveryEvent(event_key='c' * 64, smtp_attempt_id=attempt_id,
                                provider_message_id=integration.canonical_id(provider_id(delivery_id)),
                                event_type='softbounces', occurred_at=now,
                                reason=reason, processing_state='delivery', payload={}))
    monkeypatch.setenv('BREVO_DELIVERY_ALERTS_SINCE', now.isoformat())
    client = mock_api(lambda _: httpx.Response(200, json={'events': []}))
    result = integration.poll_brevo(session_factory, client, now + timedelta(minutes=10))
    assert result['dispatcher_alerts'] == 1
    with session_factory() as db:
        assert db.get(BrevoDeliveryEvent, 'c' * 64).payload == {}
        intent = db.scalar(select(DeliveryIntent).where(DeliveryIntent.kind == 'admin_notification'))
        assert intent.payload['delivery_problem']['smtp_code'] == 450
        assert intent.payload['delivery_problem']['recipient'] == address
