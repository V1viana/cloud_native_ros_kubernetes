"""Opt-in trace of every OperationalEvent the dispatcher receives (R11 / S2).

The S2 contract (docs/R11_S2_PARTITION.md, "Ricezione, riconoscimento e
azione") needs variant A's central reception: the dispatcher's logs record only
the admitted incidents, not every arrival -- STATE_RECOVERED and the events the
admission discards are invisible. With OPERATIONAL_EVENT_TRACE=1 the existing
callback writes, before the admission, one "received" line per event (ids,
robot, component, type, state, severity, the source stamp and the receive time
on both clocks), then one "admission" line with its outcome. Off by default:
nothing is written and the callback is the one before. Lines go to stdout with
a fixed prefix, collected with the Pod's logs; no new DDS subscriber, and
deduplication, QoS, timeouts, retries and actions are untouched. A failed write
is counted and reported once, never raised into the callback. No ROS import.
"""

import json
import os
import sys
import time

ENV = "OPERATIONAL_EVENT_TRACE"
PREFIX = "S2_EVENT_TRACE "


def _stamp(event):
    stamp = getattr(getattr(event, "header", None), "stamp", None)
    return None if stamp is None else stamp.sec + stamp.nanosec * 1e-9


def event_fields(event):
    return {"event_id": event.event_id, "correlation_id": event.correlation_id, "source": event.source,
            "robot_id": event.robot_id, "component": event.component, "event_type": event.event_type,
            "state": int(event.state), "severity": int(event.severity), "source_stamp": _stamp(event)}


class NullTrace:
    enabled = False

    def received(self, event):
        pass

    def admission(self, event, claimed):
        pass


class EventTrace:
    enabled = True

    def __init__(self, logger=None, stream=None, mono=time.monotonic, utc=time.time):
        self._logger, self._stream = logger, stream or sys.stdout
        self._mono, self._utc = mono, utc
        self.errors = 0

    @classmethod
    def from_env(cls, logger=None):
        return cls(logger) if os.environ.get(ENV, "") == "1" else NullTrace()

    def _write(self, kind, **fields):
        try:
            record = {"trace": kind, "recv_mono": self._mono(), "recv_utc": self._utc(), "pid": os.getpid(),
                      **fields}
            self._stream.write(PREFIX + json.dumps(record) + "\n")
            self._stream.flush()
        except Exception as exc:  # noqa: BLE001 -- coverage lost, control goes on
            self.errors += 1
            if self.errors == 1 and self._logger is not None:
                self._logger.error(f"operational event trace failed (coverage lost from here): {exc}")

    def received(self, event):
        try:
            fields = event_fields(event)
        except Exception as exc:  # noqa: BLE001
            fields = {"unreadable": str(exc)}
        self._write("received", **fields)

    def admission(self, event, claimed):
        self._write("admission", correlation_id=getattr(event, "correlation_id", None),
                    event_id=getattr(event, "event_id", None), claimed=bool(claimed))


def parse_line(line):
    """The record of one trace line of the Pod's log, or None for any other line."""
    at = line.find(PREFIX)
    if at < 0:
        return None
    try:
        return json.loads(line[at + len(PREFIX):])
    except ValueError:
        return None
