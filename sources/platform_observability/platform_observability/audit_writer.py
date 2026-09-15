"""Append-only JSONL audit service backed by persistent storage."""

import json
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse


class AuditValidationError(ValueError):
    """An audit record does not satisfy the minimum envelope."""


class JsonlAuditStore:
    REQUIRED_FIELDS = {"correlation_id", "record_type", "timestamp_utc"}

    def __init__(self, path):
        self.path = Path(path)
        self._lock = threading.Lock()

    def append(self, record):
        if not isinstance(record, dict):
            raise AuditValidationError("audit record must be a JSON object")
        missing = sorted(
            field for field in self.REQUIRED_FIELDS if not record.get(field)
        )
        if missing:
            raise AuditValidationError(
                f"missing required fields: {', '.join(missing)}"
            )
        line = json.dumps(record, sort_keys=True, separators=(",", ":"))
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._lock:
            with self.path.open("a", encoding="utf-8") as stream:
                stream.write(line + "\n")
                stream.flush()
                os.fsync(stream.fileno())
        return record

    def records(self, correlation_id=""):
        if not self.path.exists():
            return []
        records = []
        with self._lock:
            with self.path.open(encoding="utf-8") as stream:
                for line in stream:
                    if not line.strip():
                        continue
                    record = json.loads(line)
                    if not correlation_id or (
                        record.get("correlation_id") == correlation_id
                    ):
                        records.append(record)
        return records


class AuditHttpHandler(BaseHTTPRequestHandler):
    MAX_BODY_BYTES = 1024 * 1024

    def do_GET(self):
        parsed = urlparse(self.path)
        if parsed.path == "/healthz":
            self._json_response(200, {"status": "ok"})
            return
        if parsed.path == "/records":
            correlation_id = parse_qs(parsed.query).get(
                "correlation_id", [""]
            )[0]
            self._json_response(
                200,
                {"records": self.server.store.records(correlation_id)},
            )
            return
        self._json_response(404, {"error": "not found"})

    def do_POST(self):
        if urlparse(self.path).path != "/records":
            self._json_response(404, {"error": "not found"})
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if length <= 0 or length > self.MAX_BODY_BYTES:
                raise AuditValidationError("invalid request body length")
            record = json.loads(self.rfile.read(length).decode("utf-8"))
            self.server.store.append(record)
        except (AuditValidationError, json.JSONDecodeError) as exc:
            self._json_response(400, {"error": str(exc)})
            return
        self._json_response(201, {"accepted": True})

    def log_message(self, pattern, *args):
        print(
            json.dumps({"component": "audit_writer", "message": pattern % args}),
            flush=True,
        )

    def _json_response(self, status, body):
        payload = json.dumps(body).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)


def main():
    path = os.environ.get("AUDIT_PATH", "/data/audit.jsonl")
    host = os.environ.get("LISTEN_ADDRESS", "0.0.0.0")
    port = int(os.environ.get("PORT", "8080"))
    server = ThreadingHTTPServer((host, port), AuditHttpHandler)
    server.store = JsonlAuditStore(path)
    print(
        json.dumps(
            {"component": "audit_writer", "event": "ready", "path": path}
        ),
        flush=True,
    )
    server.serve_forever()


if __name__ == "__main__":
    main()
