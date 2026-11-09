"""Real isolated-service probes; refuses arbitrary hosts and never uses production data."""
from __future__ import annotations
import argparse
import json
import os
import time
from datetime import datetime, timezone
from email import message_from_bytes
from email.utils import format_datetime
from urllib.parse import urlsplit
from urllib.request import urlopen
from uuid import uuid4

PREFIX = "project1:acceptance:restart"


def require_scope():
    if os.environ.get("PROJECT1_TEST_SCOPE") != "isolated-compose":
        raise RuntimeError("Only the isolated compose scope is allowed")
    db_url = os.environ["TEST_DATABASE_URL"]
    redis_url = os.environ["TEST_REDIS_URL"]
    if urlsplit(db_url).hostname != "postgres" or not urlsplit(db_url).path.startswith("/project1_test"):
        raise RuntimeError("The isolated postgres service/database is required")
    if urlsplit(redis_url).hostname != "redis":
        raise RuntimeError("The isolated redis service is required")
    if os.environ.get("SMTP_SERVER") != "smtp" or os.environ.get("SMTP_TEST_MODE") != "true":
        raise RuntimeError("The synthetic SMTP sink is required")
    return db_url, redis_url


def baseline():
    from sqlalchemy import create_engine, text
    from redis import Redis
    db_url, redis_url = require_scope()
    engine = create_engine(db_url)
    with engine.connect() as db:
        version = db.execute(text("SHOW server_version_num")).scalar_one()
        current = db.execute(text("SELECT current_database()")).scalar_one()
        assert str(version).startswith("15") and current.startswith("project1_test")
    engine.dispose()
    redis = Redis.from_url(redis_url, decode_responses=True)
    assert redis.info("server")["redis_version"].startswith("7.")
    assert redis.config_get("appendonly")["appendonly"] == "yes"
    with urlopen(os.environ["SMTP_SINK_URL"] + "/health", timeout=5) as response:
        health = json.load(response)
    assert health == {"status": "ok", "relay": False}
    print(json.dumps({"postgres15": True, "redis7_aof": True, "smtp_sink_no_relay": True}))


def smtp_wire():
    from common.smtp_transport import SMTPTransport
    require_scope()
    base = os.environ["SMTP_SINK_URL"]
    ids = []
    content = b"%PDF-1.4\nsynthetic acceptance attachment\n%%EOF\n"
    for number in range(2):
        ident = "<" + uuid4().hex + "@example.invalid>"
        ids.append(ident)
        SMTPTransport().send({
            "recipient": "recipient" + str(number) + "@example.invalid",
            "date": format_datetime(datetime.now(timezone.utc)),
            "message_id": ident,
            "content": content,
            "payload": {"subject": "Synthetic acceptance", "filename": "synthetic.pdf"},
        })
    with urlopen(base + "/messages", timeout=5) as response:
        records = json.load(response)
    selected = []
    for record in records:
        with urlopen(base + "/messages/" + record["id"] + "/raw", timeout=5) as response:
            raw = message_from_bytes(response.read())
        if raw["Message-ID"] not in ids:
            continue
        assert raw["Date"] and len(record["recipients"]) == 1
        attachments = [part for part in raw.walk() if part.get_content_type() == "application/pdf"]
        assert len(attachments) == 1 and attachments[0].get_payload(decode=True) == content
        selected.append(record)
    assert len(selected) == 2 and len({record["connection_id"] for record in selected}) == 2
    print(json.dumps({"smtp_wire_accepted": 2, "separate_connections": True, "mime_date_and_pdf_bytes": True}))


def redis_prepare():
    from redis import Redis
    from kombu import Connection, Exchange, Producer, Queue
    db_url, redis_url = require_scope()
    redis = Redis.from_url(redis_url, decode_responses=True)
    ident = uuid4().hex
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from common.models import OutboxEvent
    from uuid import UUID
    engine = create_engine(db_url)
    factory = sessionmaker(bind=engine)
    with factory() as db:
        with db.begin():
            db.add(OutboxEvent(id=UUID(ident), topic="acceptance.restart_probe", payload={"id":ident}, available_at=datetime.now(timezone.utc)))
    engine.dispose()
    queue_name = "project1_acceptance_restart_" + ident
    redis.set(PREFIX + ":expected", json.dumps({"id": ident, "queue": queue_name}), ex=3600)
    with Connection(redis_url, transport_options={"visibility_timeout": 2}) as connection:
        with connection.channel() as channel:
            queue = Queue(queue_name, Exchange(queue_name, type="direct", durable=True), routing_key=queue_name, durable=True)
            queue(channel).declare()
            Producer(channel).publish({"id": ident}, exchange=queue.exchange, routing_key=queue_name,
                                      serializer="json", delivery_mode=2, declare=[queue])
    print(json.dumps({"redis_durable_probe_published": True}))


def redis_reserve_and_crash():
    from redis import Redis
    from kombu import Connection, Exchange, Queue
    _, redis_url = require_scope()
    redis = Redis.from_url(redis_url, decode_responses=True)
    expected = json.loads(redis.get(PREFIX + ":expected"))
    # Deliberately no context-manager cleanup: simulate loss after reserve, before ACK.
    connection = Connection(redis_url, transport_options={"visibility_timeout": 2})
    channel = connection.channel()
    name = expected["queue"]
    queue = Queue(name, Exchange(name, type="direct", durable=True), routing_key=name, durable=True)(channel)
    message = queue.get(no_ack=False)
    assert message is not None and message.payload == {"id": expected["id"]}
    assert redis.hlen("unacked") == 1
    redis.set(PREFIX + ":reserved", "yes", ex=3600)
    print(json.dumps({"redis_message_reserved_unacked": True, "intentional_private_process_loss": True}), flush=True)
    os._exit(0)


def redis_restore_and_ack():
    from redis import Redis
    from kombu import Connection, Exchange, Queue
    _, redis_url = require_scope()
    redis = Redis.from_url(redis_url, decode_responses=True)
    assert redis.get(PREFIX + ":reserved") == "yes"
    expected = json.loads(redis.get(PREFIX + ":expected"))
    with Connection(redis_url, transport_options={"visibility_timeout": 2}) as connection:
        with connection.channel() as channel:
            name = expected["queue"]
            queue = Queue(name, Exchange(name, type="direct", durable=True), routing_key=name, durable=True)(channel)
            message = None
            for _ in range(20):
                channel.qos.restore_visible(num=100, interval=1)
                message = queue.get(no_ack=False)
                if message is not None:
                    break
                time.sleep(0.5)
            assert message is not None and message.payload == {"id": expected["id"]}
            message.ack()
            assert redis.hlen("unacked") == 0
            assert queue.get(no_ack=True) is None
    from sqlalchemy import create_engine, select
    from sqlalchemy.orm import sessionmaker
    from common.models import OutboxEvent
    from common.delivery import dispatch_outbox
    from kombu import Producer
    from uuid import UUID
    engine = create_engine(os.environ["TEST_DATABASE_URL"])
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    with factory() as db:
        event = db.get(OutboxEvent, UUID(expected["id"]))
        assert event is not None and event.published_at is None
    outbox_name = expected["queue"] + "_outbox"
    with Connection(redis_url) as connection:
        with connection.channel() as channel:
            outbox_queue = Queue(outbox_name, Exchange(outbox_name,type="direct",durable=True),routing_key=outbox_name,durable=True)(channel)
            outbox_queue.declare()
            def publish(topic, payload):
                assert topic == "acceptance.restart_probe"
                Producer(channel).publish(payload,exchange=outbox_queue.exchange,routing_key=outbox_name,serializer="json",delivery_mode=2,declare=[outbox_queue])
            assert dispatch_outbox(factory,publish) == 1
            message = outbox_queue.get(no_ack=False)
            assert message is not None and message.payload == {"id":expected["id"]}
            message.ack()
    with factory() as db:
        assert db.get(OutboxEvent,UUID(expected["id"])).published_at is not None
    engine.dispose()
    print(json.dumps({"redis_restart_redelivery_verified": True, "ack_removed_unacked": True,"postgres_outbox_survived_redis_restart_and_dispatched":True}))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("phase", choices=["baseline", "smtp-wire", "redis-before-restart", "redis-reserve-and-crash", "redis-after-restart"])
    phase = parser.parse_args().phase
    {"baseline": baseline, "smtp-wire": smtp_wire, "redis-before-restart": redis_prepare,
     "redis-reserve-and-crash": redis_reserve_and_crash, "redis-after-restart": redis_restore_and_ack}[phase]()
