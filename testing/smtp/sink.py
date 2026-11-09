"""Synthetic-only SMTP sink. It stores mail and never relays it."""
from __future__ import annotations
import hashlib
import json
import os
from pathlib import Path
import re
import signal
import threading
import time
from datetime import datetime, timezone
from email.utils import parseaddr
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit
from uuid import uuid4


def is_synthetic_address(address: str) -> bool:
    parsed = parseaddr(address)[1]
    if not parsed or "@" not in parsed or parsed != address:
        return False
    domain = parsed.rsplit("@", 1)[1].lower().rstrip(".")
    return domain.endswith((".invalid", ".test")) or domain in {
        "example.com", "example.org", "example.net", "localhost"
    }


class DiskStore:
    def __init__(self, directory: Path):
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)
        self.lock = threading.Lock()

    def record(self, sender: str, recipients: list[str], content: bytes, connection_id: str | None = None) -> dict:
        if not is_synthetic_address(sender) or not recipients or not all(
            is_synthetic_address(address) for address in recipients
        ):
            raise ValueError("SMTP sink accepts synthetic addresses only")
        ident = uuid4().hex
        metadata = {
            "id": ident,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "mail_from": sender,
            "recipients": list(recipients),
            "bytes": len(content),
            "sha256": hashlib.sha256(content).hexdigest(),
        }
        if connection_id is not None:
            metadata["connection_id"] = connection_id
        with self.lock:
            raw_temp = self.directory / (ident + ".eml.part")
            json_temp = self.directory / (ident + ".json.part")
            with raw_temp.open("xb") as stream:
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(raw_temp, self.directory / (ident + ".eml"))
            with json_temp.open("x", encoding="utf-8") as stream:
                json.dump(metadata, stream)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(json_temp, self.directory / (ident + ".json"))
        return metadata

    def list_messages(self) -> list[dict]:
        with self.lock:
            messages = [json.loads(path.read_text(encoding="utf-8"))
                        for path in self.directory.glob("*.json")]
        return sorted(messages, key=lambda message: message["created_at"])

    def get(self, ident: str, raw: bool = False):
        if not re.fullmatch(r"[0-9a-f]{32}", ident):
            raise FileNotFoundError("Invalid message id")
        path = self.directory / (ident + (".eml" if raw else ".json"))
        return path.read_bytes() if raw else json.loads(path.read_text(encoding="utf-8"))


class SinkHandler:
    def __init__(self, store: DiskStore):
        self.store = store

    async def handle_MAIL(self, server, session, envelope, address, mail_options):
        if not is_synthetic_address(address):
            return "550 Synthetic sender addresses only"
        if not hasattr(session, "_sink_connection_id"):
            session._sink_connection_id = uuid4().hex
        envelope.mail_from = address
        envelope.mail_options.extend(mail_options)
        return "250 OK"

    async def handle_RCPT(self, server, session, envelope, address, rcpt_options):
        if not is_synthetic_address(address):
            return "550 Synthetic recipient addresses only"
        envelope.rcpt_tos.append(address)
        envelope.rcpt_options.extend(rcpt_options)
        return "250 OK"

    async def handle_DATA(self, server, session, envelope):
        content = envelope.original_content
        if not isinstance(content, bytes):
            content = content.encode("utf-8")
        record = self.store.record(envelope.mail_from, envelope.rcpt_tos, content, session._sink_connection_id)
        return "250 Stored " + record["id"]


def make_http_handler(store: DiskStore):
    class HttpHandler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            return

        def do_GET(self):
            path = urlsplit(self.path).path
            try:
                if path == "/health":
                    payload = {"status": "ok", "relay": False}
                elif path == "/messages":
                    payload = store.list_messages()
                else:
                    match = re.fullmatch(r"/messages/([0-9a-f]{32})(/raw)?", path)
                    if not match:
                        self.send_error(404)
                        return
                    payload = store.get(match.group(1), raw=bool(match.group(2)))
                raw = isinstance(payload, bytes)
                body = payload if raw else json.dumps(payload).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "message/rfc822" if raw else "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            except FileNotFoundError:
                self.send_error(404)
    return HttpHandler


def main():
    from aiosmtpd.controller import Controller
    store = DiskStore(Path(os.environ.get("SMTP_SINK_DATA", "/data")))
    controller = Controller(
        SinkHandler(store), hostname="0.0.0.0", port=int(os.environ.get("SMTP_SINK_PORT", "1025")),
        enable_SMTPUTF8=True, data_size_limit=int(os.environ.get("SMTP_SINK_MAX_BYTES", "73400320")),
    )
    http = ThreadingHTTPServer(
        ("0.0.0.0", int(os.environ.get("SMTP_SINK_HTTP_PORT", "8025"))), make_http_handler(store)
    )
    stopped = threading.Event()
    for number in (signal.SIGTERM, signal.SIGINT):
        signal.signal(number, lambda *_args: stopped.set())
    controller.start()
    http_thread = threading.Thread(target=http.serve_forever, daemon=True)
    http_thread.start()
    try:
        stopped.wait()
    finally:
        http.shutdown()
        http.server_close()
        controller.stop()


if __name__ == "__main__":
    main()
