"""S2 harness core (R11, docs/R11_S2_PARTITION.md, "Protocollo scelto"): no ROS import.

One test component on drone01's node, in one process and one DDS participant:
  recorder  every MetricSample of the analytics, with the receive time on the
            host's monotonic clock (shared by every container of the host) and
            UTC, to samples.jsonl -- the local ground truth, never forwarded;
  injector  the transient: processing_delay_ms of the analytics node from the
            nominal value to `high` for `duration_s`, then back, through the
            node's own parameter services; every request, answer and read-back
            recorded to events.jsonl, the return to nominal retried until it is
            confirmed or the restore deadline passes, and attempted on shutdown.
One participant on purpose: with the Fast DDS discovery server, discovery
traffic goes through the server, which the partition cuts off; the reliable
metrics subscription keeps traffic flowing both ways between this participant
and the analytics' while the link is down (to be verified live, not assumed).

Commands come as files in the control directory (a hostPath on the node),
written by the runner from the host through `docker exec` -- a channel that
does not depend on the cluster's network:
  arm.json    {"id", "target_node", "parameter", "nominal"}: resolve the node
              and read the parameter; armed only if it equals the nominal;
  pulse.json  {"id", "start_utc", "high", "duration_s", "low"}: the pulse,
              at an absolute UTC time chosen by the runner.
Each command runs once per id and only if its "run" is the harness's run_id; the
runner writes them by an atomic rename in the same directory. Files already
there when the harness starts are recorded and never executed: a restarted
harness does not replay a run's commands as if they were new. Tested offline: operator/tests/test_s2_harness.py.
"""

import json
import math
import os
import threading
import time

RESTORE_RETRY_SEC = 0.5
RESTORE_DEADLINE_SEC = 60.0


class CommandError(ValueError):
    pass


class JsonlLog:
    """Append-only JSON lines, each written and flushed whole, with both clocks."""

    def __init__(self, path, mono=time.monotonic, utc=time.time):
        self._file = open(path, "a", buffering=1)
        self._lock = threading.Lock()
        self._mono, self._utc = mono, utc

    def write(self, event, fields_at=None, **fields):
        """Both clocks are read under the lock, so the file's order is their order;
        fields_at(mono, utc), if given, builds more fields from those same readings."""
        with self._lock:
            mono, utc = self._mono(), self._utc()
            record = {"event": event, "mono": mono, "utc": utc, **fields,
                      **(fields_at(mono, utc) if fields_at else {})}
            self._file.write(json.dumps(record) + "\n")
            self._file.flush()
        return record

    def close(self):
        self._file.close()


def sample_record(msg, recv_mono, recv_utc):
    """One MetricSample, as the recorder writes it (no field is transformed)."""
    stamp = getattr(getattr(msg, "header", None), "stamp", None)
    return {"recv_mono": recv_mono, "recv_utc": recv_utc,
            "stamp": None if stamp is None else stamp.sec + stamp.nanosec * 1e-9,
            "robot_id": msg.robot_id, "component": msg.component, "latency_ms": msg.latency_ms,
            "queue_depth": msg.queue_depth, "cpu_percent": msg.cpu_percent}


def write_sample(log, msg):
    """The recorder's write: receive times read under the log's lock (review of
    c6b6b7e: read before it, two concurrent callbacks could leave the file out of
    receive order, and the ground truth reads it in file order)."""
    return log.write("sample", fields_at=lambda mono, utc: sample_record(msg, mono, utc))


def _number(command, key, positive=False):
    value = command.get(key)
    if not isinstance(value, (int, float)) or isinstance(value, bool) or not math.isfinite(value):
        raise CommandError(f"{key} must be a finite number, got {value!r}")
    if positive and value <= 0:
        raise CommandError(f"{key} must be positive, got {value!r}")
    return float(value)


def parse_arm(command):
    if not command.get("id") or not str(command.get("target_node", "")).startswith("/"):
        raise CommandError("arm needs an id and an absolute target_node")
    return {"id": str(command["id"]), "target_node": command["target_node"],
            "parameter": command.get("parameter", "processing_delay_ms"),
            "nominal": _number(command, "nominal")}


def parse_pulse(command):
    if not command.get("id"):
        raise CommandError("pulse needs an id")
    return {"id": str(command["id"]), "start_utc": _number(command, "start_utc"),
            "high": _number(command, "high"), "duration_s": _number(command, "duration_s", positive=True),
            "low": _number(command, "low")}


class Injector:
    """The transient on one parameter of one node, through a port:
      port.bind(target_node, parameter)
      port.get() -> float                  (raises on failure)
      port.set(value) -> (ok, reason)      (raises on failure or timeout)
    Blocking by design: it runs on its own thread, never in a ROS callback."""

    def __init__(self, port, log, *, utc=time.time, sleep=time.sleep,
                 restore_retry_sec=RESTORE_RETRY_SEC, restore_deadline_sec=RESTORE_DEADLINE_SEC):
        self._port, self._log = port, log
        self._utc, self._sleep = utc, sleep
        self._retry, self._deadline = restore_retry_sec, restore_deadline_sec
        self.armed = None
        self._active = None                   # the pulse whose value may still be high
        self._restore_deadline = None         # one for the whole pulse (review of c6b6b7e)
        self._stop = threading.Event()
        self.done = set()

    def _sleep_until(self, deadline):
        """At most 10 ms towards `deadline`; never a negative sleep (the clock moves
        between two reads -- review of c6b6b7e)."""
        remaining = deadline - self._utc()
        if remaining > 0:
            self._sleep(min(0.01, remaining))

    def arm(self, command):
        arm = parse_arm(command)
        try:
            self._port.bind(arm["target_node"], arm["parameter"])
            value = self._port.get()
        except Exception as exc:  # noqa: BLE001 -- recorded, the run decides
            self._log.write("arm_failed", id=arm["id"], target_node=arm["target_node"], error=str(exc))
            return False
        ok = abs(value - arm["nominal"]) < 1e-9
        self._log.write("armed" if ok else "arm_failed", id=arm["id"], target_node=arm["target_node"],
                        parameter=arm["parameter"], value=value, nominal=arm["nominal"])
        self.armed = arm if ok else None
        return ok

    def _set(self, pulse, phase, value):
        self._log.write(f"{phase}_request", id=pulse["id"], value=value)
        try:
            ok, reason = self._port.set(value)
        except Exception as exc:  # noqa: BLE001
            self._log.write(f"{phase}_answer", id=pulse["id"], ok=False, reason=str(exc))
            return False
        self._log.write(f"{phase}_answer", id=pulse["id"], ok=ok, reason=reason)
        if not ok:
            return False
        try:
            read = self._port.get()
        except Exception as exc:  # noqa: BLE001
            self._log.write(f"{phase}_readback", id=pulse["id"], ok=False, error=str(exc))
            return False
        confirmed = abs(read - value) < 1e-9
        self._log.write(f"{phase}_readback", id=pulse["id"], ok=confirmed, value=read)
        return confirmed

    def _restore(self, pulse, reason):
        """Back to `low` until confirmed, retried, bounded by one deadline fixed at the
        pulse's first restore: a later call (shutdown) never opens a second one."""
        if self._restore_deadline is None:
            self._restore_deadline = self._utc() + self._deadline
        deadline = self._restore_deadline
        if self._utc() >= deadline:
            self._log.write("restore_gave_up", id=pulse["id"], attempts=0, reason=reason,
                            detail="restore deadline already passed")
            return False
        attempt = 0
        while True:
            attempt += 1
            if self._set(pulse, "restore", pulse["low"]):
                self._active = None
                self._log.write("restored", id=pulse["id"], attempts=attempt, reason=reason)
                return True
            if self._utc() >= deadline:
                self._log.write("restore_gave_up", id=pulse["id"], attempts=attempt, reason=reason)
                return False
            self._sleep(max(0.0, min(self._retry, deadline - self._utc())))

    def pulse(self, command):
        pulse = parse_pulse(command)
        if self.armed is None:
            self._log.write("pulse_refused", id=pulse["id"], reason="not armed")
            return False
        if pulse["id"] in self.done:
            return False
        self.done.add(pulse["id"])
        self._log.write("pulse_scheduled", **pulse)
        while self._utc() < pulse["start_utc"]:
            if self._stop.is_set():
                self._log.write("pulse_cancelled", id=pulse["id"])
                return False
            self._sleep_until(pulse["start_utc"])
        self._active, self._restore_deadline = pulse, None
        high_ok = False
        try:
            high_ok = self._set(pulse, "high", pulse["high"])
            end = pulse["start_utc"] + pulse["duration_s"]
            while self._utc() < end and not self._stop.is_set():
                self._sleep_until(end)
        finally:
            restored = self._restore(pulse, "stopped" if self._stop.is_set() else "end of pulse")
        self._log.write("pulse_done", id=pulse["id"], high_confirmed=high_ok, restored=restored)
        return high_ok and restored

    def stop(self):
        """Shutdown: a running pulse ends now and is restored by its own thread;
        call restore_if_active() afterwards for a pulse left high elsewhere."""
        self._stop.set()

    def restore_if_active(self):
        if self._active is not None:
            return self._restore(self._active, "shutdown")
        return True


def dispatch(injector, name, command, log):
    """One command; an invalid one or an unexpected error is recorded and the
    worker goes on (review of c6b6b7e: an error left the worker, and the node)."""
    try:
        if name == "arm.json":
            injector.arm(command)
        else:
            injector.pulse(command)
    except CommandError as exc:
        log.write("command_invalid", file=name, id=command.get("id"), error=str(exc))
    except Exception as exc:  # noqa: BLE001 -- recorded; the run's evidence shows it
        log.write("command_failed", file=name, id=command.get("id"), error=f"{type(exc).__name__}: {exc}")


class CommandPoller:
    """Reads the control directory; hands each new command of this run (by id) over once."""

    NAMES = ("arm.json", "pulse.json")

    def __init__(self, directory, log, run_id):
        if not run_id:
            raise ValueError("run_id is required")
        self._dir, self._log, self._run = directory, log, str(run_id)
        self._seen = set()
        self._unreadable = set()

    def _read(self, name):
        path = os.path.join(self._dir, name)
        try:
            with open(path) as handle:
                return json.load(handle)
        except FileNotFoundError:
            return None
        except (OSError, ValueError) as exc:
            if name not in self._unreadable:        # once per file, not every poll
                self._unreadable.add(name)
                self._log.write("command_unreadable", file=name, error=str(exc))
            return None

    def prime(self):
        """At start: whatever is already there is a residue, never executed."""
        for name in self.NAMES:
            command = self._read(name)
            if command is not None:
                self._seen.add((name, str(command.get("run")), str(command.get("id"))))
                self._log.write("command_present_at_start", file=name, run=command.get("run"),
                                id=command.get("id"))

    def poll(self):
        out = []
        for name in self.NAMES:
            command = self._read(name)
            if command is None:
                continue
            self._unreadable.discard(name)
            key = (name, str(command.get("run")), str(command.get("id")))
            if key in self._seen:
                continue
            self._seen.add(key)
            if str(command.get("run")) != self._run:
                self._log.write("command_other_run", file=name, run=command.get("run"), id=command.get("id"),
                                expected_run=self._run)
                continue
            self._log.write("command_seen", file=name, run=self._run, id=command.get("id"))
            out.append((name, command))
        return out
