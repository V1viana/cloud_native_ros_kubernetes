#!/usr/bin/env python3
"""S2 local ground truth (R11, docs/R11_S2_PARTITION.md, "Ground truth locale").

The samples the harness recorded on drone01's node (samples.jsonl), judged with
the same window rule as variant A's detector and B's bridge (the V6 contract:
px4_event_detector_plugin/src/rule_state_machines.cpp, LatencySloStateMachine):
a window opens with its first sample and closes at the first evaluation at
least W after its start that holds a sample, evaluations every 0.2 s; p95 by
nearest rank (with 8-9 samples a window, the largest: one sample above 250 ms
makes the window violating); samples with an invalid latency or cpu dropped; an episode starts at the third consecutive window above 250 ms
and returns at the third consecutive window below 150 ms, a window in between
resetting both counts. Times are the recorder's receive times on the host's
monotonic clock (shared by every container of the host).

The evaluation grid starts at the first sample and steps 0.2 s: the local
return is the end of the window that completes the third one below 150 ms --
the instant from which the runner counts the 2 s / 30 s before restoring the
link. The detectors of A and B receive the same samples at their own times and
evaluate on their own grids: small offsets, declared, not removed.

GroundTruth.feed(sample) and .advance(now) serve the runner while it reads the
file; replay(path) the judge. A sample earlier than one before it in the file is
counted in out_of_order and left out, never placed silently: the judge treats a
non-empty count as a validity problem. Tested offline: operator/tests/test_s2_ground_truth.py.
"""

import argparse
import json
import math
import sys

WINDOW_SEC, EVAL_SEC = 2.0, 0.2
VIOLATION_MS, RECOVERY_MS, CONSECUTIVE = 250.0, 150.0, 3


def p95(values):
    values = sorted(values)
    return values[max(1, math.ceil(0.95 * len(values))) - 1]


class GroundTruth:
    def __init__(self, window_sec=WINDOW_SEC, eval_sec=EVAL_SEC, violation_ms=VIOLATION_MS,
                 recovery_ms=RECOVERY_MS, consecutive=CONSECUTIVE):
        self.w, self.step = window_sec, eval_sec
        self.violation, self.recovery, self.n = violation_ms, recovery_ms, consecutive
        self._pending = []                   # samples not yet evaluated, by receive time
        self._start = None                   # the open window's start
        self._samples = []
        self._next_eval = None
        self.violation_windows = self.recovery_windows = 0
        self.incident = False
        self.windows, self.episodes = [], []
        self.last_sample = None
        self.out_of_order = []               # samples earlier than one before them in the file
        self._fed = 0

    def feed(self, sample):
        """One recorder line (dict with recv_mono and latency_ms), in file order."""
        t, latency = sample["recv_mono"], sample["latency_ms"]
        cpu = sample.get("cpu_percent", 0.0)
        # as A: a sample with an invalid latency or cpu is dropped
        index, self._fed = self._fed, self._fed + 1
        if not all(isinstance(v, (int, float)) and math.isfinite(v) and v >= 0 for v in (latency, cpu)):
            return
        if self.last_sample is not None and t < self.last_sample:
            # The rule reads samples in receive order; one out of it is counted (a
            # validity problem for the judge), never silently placed (review of c6b6b7e).
            self.out_of_order.append({"index": index, "recv_mono": t, "after": self.last_sample})
            return
        if self._next_eval is None:
            self._next_eval = t + self.step
        self._pending.append((t, float(latency)))
        self.last_sample = t

    def advance(self, now):
        """Every evaluation up to `now` (the samples fed so far must be all of them
        with a receive time up to `now`)."""
        while self._next_eval is not None and self._next_eval <= now:
            tick = self._next_eval
            while self._pending and self._pending[0][0] <= tick:
                t, latency = self._pending.pop(0)
                if self._start is None:
                    self._start = t
                self._samples.append(latency)
            self._tick(tick)
            self._next_eval = round(tick + self.step, 9)

    def _tick(self, now):
        if self._start is None or now - self._start < self.w or not self._samples:
            return
        value = p95(self._samples)
        window = {"start": self._start, "end": now, "samples": len(self._samples), "p95": value}
        self.windows.append(window)
        self._samples, self._start = [], now
        if value > self.violation:
            self.violation_windows += 1
            self.recovery_windows = 0
            if not self.incident and self.violation_windows >= self.n:
                self.incident = True
                self.episodes.append({"entered": now, "first_violating_window_start": self.windows[-self.n]["start"],
                                      "returned": None})
        elif value < self.recovery:
            self.violation_windows = 0
            if self.incident:
                self.recovery_windows += 1
                if self.recovery_windows >= self.n:
                    self.incident, self.recovery_windows = False, 0
                    self.episodes[-1]["returned"] = now
        else:
            self.violation_windows = self.recovery_windows = 0

    def summary(self):
        return {"windows": len(self.windows), "episodes": self.episodes, "incident_open": self.incident,
                "last_sample": self.last_sample, "out_of_order": len(self.out_of_order)}


def replay(path, until=None):
    """The whole file, evaluated up to its last sample (or `until`)."""
    truth = GroundTruth()
    with open(path) as handle:
        for line in handle:
            if line.strip():
                record = json.loads(line)
                if record.get("event", "sample") == "sample":
                    truth.feed(record)
    if truth.last_sample is not None:
        truth.advance(until if until is not None else truth.last_sample)
    return truth


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("samples")
    parser.add_argument("--until", type=float)
    args = parser.parse_args(argv)
    print(json.dumps(replay(args.samples, args.until).summary(), indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
