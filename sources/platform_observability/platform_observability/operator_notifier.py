"""Operator notification sink with durable forwarding to the audit writer."""

import json
import os
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

from .http_client import post_json


def utc_now():
    return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace(
        "+00:00", "Z"
    )


class OperatorNotifier:
    def __init__(self, audit_url, sender=post_json, clock=utc_now):
        self._audit_url = audit_url
        self._sender = sender
        self._clock = clock

    def notify(self, notification):
        required = ("correlation_id", "robot_id", "event_type", "outcome")
        missing = [field for field in required if not notification.get(field)]
        if missing:
            raise ValueError(f"missing notification fields: {', '.join(missing)}")
        durable_record = dict(notification)
        durable_record.update(
            {
                "record_type": "operator_notification",
                "timestamp_utc": notification.get("timestamp_utc") or self._clock(),
            }
        )
        print(
            json.dumps(
                {"component": "operator_notifier", **durable_record},
                sort_keys=True,
            ),
            flush=True,
        )
        if self._audit_url:
            self._sender(self._audit_url, durable_record)
        return durable_record


class NotifierHttpHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        if urlparse(self.path).path == "/healthz":
            self._json_response(200, {"status": "ok"})
        else:
            self._json_response(404, {"error": "not found"})

    def do_POST(self):
        if urlparse(self.path).path != "/notifications":
            self._json_response(404, {"error": "not found"})
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            notification = json.loads(self.rfile.read(length).decode("utf-8"))
            self.server.notifier.notify(notification)
        except (ValueError, json.JSONDecodeError, RuntimeError) as exc:
            self._json_response(400, {"error": str(exc)})
            return
        self._json_response(202, {"accepted": True})

    def log_message(self, pattern, *args):
        return

    def _json_response(self, status, body):
        payload = json.dumps(body).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)


def main():
    audit_url = os.environ.get(
        "AUDIT_URL", "http://p2-audit-writer:8080/records"
    )
    host = os.environ.get("LISTEN_ADDRESS", "0.0.0.0")
    port = int(os.environ.get("PORT", "8081"))
    server = ThreadingHTTPServer((host, port), NotifierHttpHandler)
    server.notifier = OperatorNotifier(audit_url)
    print(
        json.dumps({"component": "operator_notifier", "event": "ready"}),
        flush=True,
    )
    server.serve_forever()


if __name__ == "__main__":
    main()
