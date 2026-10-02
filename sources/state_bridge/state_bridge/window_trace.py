"""Measurement-only trace of metric-window closures and sends (R11, D6 bench).

Active only when STATE_BRIDGE_WINDOW_TRACE names a file: the D6 check
(scripts/run_window_transport_check.sh) sets it on the Fleet Operator, which
passes it to the bridge sidecar; unset everywhere else, where every call is a
no-op and the Deployments are unchanged. One JSON line per event, with the
process's wall clock (UTC epoch seconds) and monotonic clock:
  closed      seq, start, end, samples, p95 of each window, when it closes;
  send_start  the sequence numbers in the snapshot, when its request starts;
  send_end    the same, ok and the error, when it ends;
each line also carries how long the previous write took (prev_write_us): the
cost of the collection itself. With STATE_BRIDGE_TICK_TRACE too (the tick
diagnosis, docs/R11_S2_PARTITION.md), bridge.py wraps its two timer callbacks
and writes one `tick` per invocation: raw times at entry and exit, rcl's
until-next/since-last and period, thread and native id, the callback's result.
The expected time and the lateness are derived later, by the analysis.
With STATE_BRIDGE_DETAIL_TRACE too (option 3 of the D6 diagnosis), every
record also carries the wait for the trace's own lock (lock_wait_us, apart
from prev_write_us), and DetailRecorder keeps compact records of the other
activity -- ROS subscription callbacks with thread CPU time, service calls,
HTTP requests, the spec watch -- in a bounded queue that one thread writes
out once a second as a single "detail" line: no write per callback, and the
flush's own cost goes in the next line.
The trace never feeds the framework, never changes scheduling and is never
used to retry; it is read from outside the Pod.
The same file on both revisions of the D6 check (before and after D6). No ROS
import.
"""

import collections
import json
import os
import threading
import time

ENV = "STATE_BRIDGE_WINDOW_TRACE"
ENV_TICKS = "STATE_BRIDGE_TICK_TRACE"
ENV_DETAIL = "STATE_BRIDGE_DETAIL_TRACE"


class WindowTrace:
    def __init__(self, path=None, ticks=None, detail=None):
        path = path if path is not None else os.environ.get(ENV, "")
        self._lock = threading.Lock()
        self._file = open(path, "a", buffering=1) if path else None
        self._last_write_us = None
        ticks = ticks if ticks is not None else bool(os.environ.get(ENV_TICKS, ""))
        self.ticks = self._file is not None and ticks
        detail = detail if detail is not None else bool(os.environ.get(ENV_DETAIL, ""))
        self.detail_enabled = self._file is not None and detail

    @property
    def enabled(self):
        return self._file is not None

    def _write(self, event, **fields):
        if self._file is None:
            return
        requested = time.monotonic() if self.detail_enabled else None
        with self._lock:
            started = time.monotonic()
            if requested is not None:
                fields["lock_wait_us"] = round((started - requested) * 1e6)
            record = {"event": event, "utc": time.time(), "mono": started,
                      "thread": threading.current_thread().name, **fields,
                      "prev_write_us": self._last_write_us}
            self._file.write(json.dumps(record) + "\n")
            self._file.flush()
            self._last_write_us = round((time.monotonic() - started) * 1e6)

    def closed(self, seq, window):
        self._write("closed", seq=seq, start=window["start"], end=window["end"],
                    samples=window["samples"], p95=window["p95"])

    def send_started(self, seqs):
        self._write("send_start", seqs=list(seqs))

    def send_finished(self, seqs, ok, error=None):
        self._write("send_end", seqs=list(seqs), ok=ok, error=error)

    def tick(self, **fields):
        if self.ticks:
            self._write("tick", **fields)

    def detail(self, **fields):
        self._write("detail", **fields)


class NullDetail:
    """The detail recorder when the detail trace is off: nothing is measured."""
    enabled = False

    def timed(self, kind, fn):
        return fn

    def item(self, kind, start, end, **fields):
        pass

    def start(self):
        pass

    def stop(self, timeout=None):
        pass


NULL_DETAIL = NullDetail()


class DetailRecorder:
    """Compact records of the bridge's other activity (option 3 of the D6 diagnosis),
    on the monotonic clock of the tick records, with the thread's native id. Kept in
    a bounded deque (append is thread-safe) and written by one thread once a second
    as one "detail" line; what did not fit is counted, never waited for."""
    enabled = True

    def __init__(self, trace, period_sec=1.0, maxlen=50000):
        self._trace = trace
        self._period_sec = period_sec
        self._items = collections.deque(maxlen=maxlen)
        self._appended = 0
        self._taken = 0
        self._last_flush_us = None
        self._stop = threading.Event()
        self._thread = None

    def item(self, kind, start, end, **fields):
        self._items.append({"k": kind, "t0": start, "t1": end, "tid": threading.get_native_id(),
                            "thread": threading.current_thread().name, **fields})
        self._appended += 1

    def timed(self, kind, fn):
        """fn, with its wall and thread CPU time recorded at every call."""
        def run(*args, **kwargs):
            start, cpu = time.monotonic(), time.thread_time()
            try:
                return fn(*args, **kwargs)
            finally:
                self.item(kind, start, time.monotonic(), cpu_us=round((time.thread_time() - cpu) * 1e6))
        return run

    def flush(self):
        started = time.monotonic()
        items = []
        while True:
            try:
                items.append(self._items.popleft())
            except IndexError:
                break
        self._taken += len(items)
        self._trace.detail(items=items, dropped=max(0, self._appended - self._taken - len(self._items)),
                           prev_flush_us=self._last_flush_us)
        self._last_flush_us = round((time.monotonic() - started) * 1e6)

    def start(self):
        self._thread = threading.Thread(target=self._run, name="detail-flush", daemon=True)
        self._thread.start()

    def stop(self, timeout=None):
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout)
        self.flush()

    def _run(self):
        while not self._stop.wait(self._period_sec):
            self.flush()


def snapshot_seqs(status_patch):
    """The window sequence numbers a metricWindows status patch carries."""
    return [w["seq"] for entry in (status_patch.get("metricWindows") or {}).values()
            for w in (entry or {}).get("windows") or []]
