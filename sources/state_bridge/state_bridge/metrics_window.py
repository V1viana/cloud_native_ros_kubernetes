"""Rolling p95 over a time window of raw metric samples.

AdaptationPolicy.spec.trigger names a metric like `latency_p95_ms`
(proposal S3), not a raw instantaneous reading -- computing it has to live
somewhere, and the State Bridge is the only component that sees every raw
MetricSample as it is published (subscribe_metric's callback fires on each
message, independent of the bridge's own poll/patch tick), so it computes
the aggregate before ever writing to status.metrics.
"""

import math
import threading
import time


class MetricsWindow:
    def __init__(self, window_sec, monotonic=time.monotonic):
        if window_sec <= 0:
            raise ValueError("window_sec must be positive")
        self._window_sec = window_sec
        self._monotonic = monotonic
        self._samples = []  # list of (timestamp, value), oldest first

    def add(self, value):
        self._samples.append((self._monotonic(), value))
        self._prune()

    def p95(self):
        self._prune()
        if not self._samples:
            return None
        values = sorted(value for _, value in self._samples)
        # Nearest-rank method: simple, no interpolation, matches what a
        # small onboard sample count (a few Hz over a ~10s window) needs
        # without pulling in a stats dependency for one percentile.
        index = max(0, math.ceil(0.95 * len(values)) - 1)
        return values[index]

    def _prune(self):
        cutoff = self._monotonic() - self._window_sec
        while self._samples and self._samples[0][0] < cutoff:
            self._samples.pop(0)


class TumblingWindow:
    """Tumbling p95 windows, variant A's semantics (V6, docs/CRD_CONTRACT_AUDIT.md).

    Transcribes the imperative baseline's LatencySloStateMachine
    (px4_event_detector_plugin/src/rule_state_machines.cpp, processSample/tick):
    a window opens with the first valid sample and restarts when the previous one
    closes; it closes at the first evaluation at least window_sec after its start
    that holds a sample, and a window with no sample never closes; the statistic
    is the nearest-rank p95 of every sample it holds, whatever instance published
    it (D3b: the union of the Active replicas' samples, each weighing the same).
    Locked: samples arrive on one ROS callback, evaluation runs on another.
    """

    def __init__(self, window_sec):
        if window_sec <= 0:
            raise ValueError("window_sec must be positive")
        self.window_sec = window_sec
        self._start = None
        self._samples = []
        self._lock = threading.Lock()

    def add(self, latency_ms, cpu_percent, now):
        """now: wall-clock seconds. Invalid samples are dropped, as in A."""
        if not all(isinstance(v, (int, float)) and math.isfinite(v) and v >= 0
                   for v in (latency_ms, cpu_percent)):
            return
        with self._lock:
            if self._start is None:
                self._start = now
            self._samples.append(float(latency_ms))

    def open_window(self):
        """(start, samples) of the window being filled; (None, 0) before the first sample."""
        with self._lock:
            return self._start, len(self._samples)

    def evaluate(self, now):
        """The window just closed as {start, end, samples, p95}, or None."""
        with self._lock:
            if (self._start is None or now - self._start < self.window_sec
                    or not self._samples):
                return None
            values = sorted(self._samples)
            rank = math.ceil(0.95 * len(values))
            window = {"start": self._start, "end": now, "samples": len(values),
                      "p95": values[max(1, rank) - 1]}
            self._samples = []
            self._start = now
            return window
