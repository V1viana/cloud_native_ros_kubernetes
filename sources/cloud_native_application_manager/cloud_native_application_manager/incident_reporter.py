"""Non-blocking audit and operator notification reporting."""

import json
import queue
import sqlite3
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from .control_loop import Phase


def utc_now():
    return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace(
        "+00:00", "Z"
    )


def post_json(url, payload, timeout_sec=1.0):
    request = Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urlopen(request, timeout=timeout_sec) as response:
            response.read()
    except HTTPError as exc:
        detail = exc.read().decode("utf-8")
        raise RuntimeError(f"HTTP {exc.code}: {detail}") from exc
    except URLError as exc:
        raise RuntimeError(f"endpoint unavailable: {exc.reason}") from exc


class IncidentReporter:
    """Create one ordered reporting stream with optional durable delivery."""

    def __init__(
        self,
        audit_url="",
        notifier_url="",
        sender=post_json,
        clock=utc_now,
        on_error=lambda message: None,
        asynchronous=True,
        queue_size=256,
        delivery_attempts=20,
        retry_delay_sec=0.5,
        sleeper=time.sleep,
        spool_path="",
    ):
        self._audit_url = audit_url
        self._notifier_url = notifier_url
        self._sender = sender
        self._clock = clock
        self._on_error = on_error
        self._asynchronous = asynchronous
        self._delivery_attempts = int(delivery_attempts)
        self._retry_delay_sec = float(retry_delay_sec)
        self._sleeper = sleeper
        self._spool = SqliteOutbox(spool_path, clock=clock) if spool_path else None
        self._wake = threading.Event()
        self._stop = threading.Event()
        if self._delivery_attempts < 1 or self._retry_delay_sec < 0.0:
            raise ValueError("invalid observability retry configuration")
        self._queue = queue.Queue(maxsize=queue_size)
        if asynchronous:
            self._worker = threading.Thread(target=self._run, daemon=True)
            self._worker.start()
            if self._spool and self._spool.pending_count():
                self._wake.set()
        elif self._spool:
            self.flush()

    def begin(self, request, source_timestamp=""):
        return IncidentAuditSession(self, request, source_timestamp)

    def audit(self, record):
        self._submit(self._audit_url, record)

    def notify(self, notification):
        self._submit(self._notifier_url, notification)

    def _submit(self, url, payload):
        if not url:
            return
        if self._spool:
            self._spool.enqueue(url, payload)
            if self._asynchronous:
                self._wake.set()
            else:
                self.flush()
            return
        if not self._asynchronous:
            self._deliver(url, payload)
            return
        try:
            self._queue.put_nowait((url, payload))
        except queue.Full:
            self._on_error("observability queue is full; record dropped")

    def _run(self):
        if self._spool:
            self._run_persistent()
            return
        while True:
            url, payload = self._queue.get()
            try:
                self._deliver(url, payload)
            finally:
                self._queue.task_done()

    def _run_persistent(self):
        while not self._stop.is_set():
            delivered = self.flush()
            if delivered == 0 and self._spool.pending_count():
                self._sleeper(self._retry_delay_sec)
                continue
            self._wake.wait(timeout=1.0)
            self._wake.clear()

    def flush(self):
        """Deliver durable records in FIFO order until empty or blocked."""
        if not self._spool:
            return 0
        delivered = 0
        while True:
            item = self._spool.peek()
            if item is None:
                return delivered
            record_id, url, payload = item
            if not self._deliver(url, payload):
                self._spool.mark_failed(record_id, "delivery attempts exhausted")
                return delivered
            self._spool.acknowledge(record_id)
            delivered += 1

    def pending_count(self):
        if self._spool:
            return self._spool.pending_count()
        return self._queue.qsize()

    def close(self):
        self._stop.set()
        self._wake.set()

    def _deliver(self, url, payload):
        for attempt in range(1, self._delivery_attempts + 1):
            try:
                self._sender(url, payload)
                return True
            except Exception as exc:
                if attempt == self._delivery_attempts:
                    self._on_error(
                        "observability delivery failed after "
                        f"{attempt} attempts: {exc}"
                    )
                    return False
                self._sleeper(self._retry_delay_sec)


class SqliteOutbox:
    """Small FIFO outbox whose rows survive Application Manager restarts."""

    def __init__(self, path, clock=utc_now):
        self._path = Path(path)
        self._clock = clock
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS delivery_outbox (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    url TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    attempts INTEGER NOT NULL DEFAULT 0,
                    last_error TEXT NOT NULL DEFAULT ''
                )
                """
            )

    def enqueue(self, url, payload):
        encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        with self._connect() as connection:
            connection.execute(
                "INSERT INTO delivery_outbox "
                "(url, payload_json, created_at) VALUES (?, ?, ?)",
                (url, encoded, self._clock()),
            )

    def peek(self):
        with self._connect() as connection:
            row = connection.execute(
                "SELECT id, url, payload_json FROM delivery_outbox "
                "ORDER BY id LIMIT 1"
            ).fetchone()
        if row is None:
            return None
        return row[0], row[1], json.loads(row[2])

    def acknowledge(self, record_id):
        with self._connect() as connection:
            connection.execute(
                "DELETE FROM delivery_outbox WHERE id = ?", (record_id,)
            )

    def mark_failed(self, record_id, error):
        with self._connect() as connection:
            connection.execute(
                "UPDATE delivery_outbox "
                "SET attempts = attempts + 1, last_error = ? WHERE id = ?",
                (str(error), record_id),
            )

    def pending_count(self):
        with self._connect() as connection:
            row = connection.execute(
                "SELECT COUNT(*) FROM delivery_outbox"
            ).fetchone()
        return row[0]

    def _connect(self):
        return sqlite3.connect(self._path, timeout=5.0)


class IncidentAuditSession:
    """Collect the minimum audit fields for one correlated action."""

    def __init__(self, reporter, request, source_timestamp):
        self._reporter = reporter
        self._request = request
        self._source_timestamp = source_timestamp
        self._received = reporter._clock()
        self._decision = self._received
        self._action_started = ""
        self._verification_started = ""
        self._actions = []
        self._reporter.audit(self._base("incident_started"))

    def feedback(self, update):
        timestamp = self._reporter._clock()
        if update.phase == Phase.ACTING and not self._action_started:
            self._action_started = timestamp
        if update.phase == Phase.VERIFYING and not self._verification_started:
            self._verification_started = timestamp
        if update.phase in (Phase.ACTING, Phase.ROLLING_BACK):
            self._actions.append(update.message)
        record = self._base("incident_feedback")
        record.update(
            {
                "timestamp_utc": timestamp,
                "phase": update.phase.name,
                "progress": update.progress,
                "message": update.message,
                "observations": update.observations,
            }
        )
        self._reporter.audit(record)

    def complete(self, result):
        completed = self._reporter._clock()
        failure_reason = "" if result.success else result.metrics.get("error", "")
        record = self._base("incident_completed")
        record.update(
            {
                "timestamp_utc": completed,
                "manager_received_timestamp": self._received,
                "decision_timestamp": self._decision,
                "action_started_timestamp": self._action_started,
                "verification_started_timestamp": self._verification_started,
                "completed_timestamp": completed,
                "requested_outcome": self._request.requested_outcome,
                "actions_executed": list(self._actions),
                "success": result.success,
                "outcome": result.outcome,
                "final_phase": result.final_phase,
                "rollback_performed": result.rollback_performed,
                "failure_reason": failure_reason,
                "artifacts": result.metrics,
            }
        )
        self._reporter.audit(record)
        self._reporter.notify(
            {
                "timestamp_utc": completed,
                "correlation_id": self._request.event.correlation_id,
                "event_id": self._request.event.event_id,
                "robot_id": self._request.event.robot_id,
                "event_type": self._request.event.event_type,
                "success": result.success,
                "outcome": result.outcome,
                "final_phase": result.final_phase,
                "rollback_performed": result.rollback_performed,
            }
        )
        return record

    def _base(self, record_type):
        event = self._request.event
        return {
            "record_type": record_type,
            "timestamp_utc": self._reporter._clock(),
            "correlation_id": event.correlation_id,
            "event_id": event.event_id,
            "robot_id": event.robot_id,
            "event_type": event.event_type,
            "policy_id": self._request.policy_id,
            "source_timestamp": self._source_timestamp,
        }
