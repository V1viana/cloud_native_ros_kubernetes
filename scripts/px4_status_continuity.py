#!/usr/bin/env python3
"""E2 mission continuity from a continuous uORB sampler (R9, decision D5-b).

Viviana (2026-09-26): E1's observer reads PX4 over DDS through the Agent, and
E2's fault stops the Agent, so E2 samples PX4's own vehicle_status from inside
the PX4 container (px4-listener, run_e2.sh), the same in A and B. Each read is
"READ <wall ns>" followed by the listener's output. This script:
  - separates new samples (a PX4 timestamp not seen before) from repeated
    reads of the same message and from failed reads;
  - folds the new samples through E1's own ContinuityTracker
    (sources/mission_observer/mission_observer/continuity.py) into E1's
    markers, with a summary at every read, and judges them with E1's own
    evaluate() (scripts/mission_continuity.py): armed and in Hold at window
    start, no nav/arming/failsafe change, no gap of new samples reaching
    GAP_LIMIT_MS, no PX4 clock going back -- inside the window, the fault
    included;
  - adds the checks of the sampling itself, pre-registered: reads cover the
    window with no gap above READ_GAP_LIMIT_MS, and the source is compatible
    with the gap limit (median interval between new samples in the window at
    most SOURCE_PERIOD_LIMIT_MS). Either failing makes the continuity
    verdict inconclusive, never false.
Continuous sampling, not an event stream: a transition that starts and ends
between two samples can be missed. Verdict JSON on stdout.
Tested offline: operator/tests/test_px4_status_continuity.py.
"""

import argparse
import json
from pathlib import Path
import re
import statistics
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "sources/mission_observer"))
from mission_continuity import GAP_LIMIT_MS, evaluate  # noqa: E402
from mission_observer.continuity import FIELDS, ContinuityTracker  # noqa: E402

READ_GAP_LIMIT_MS = 2000       # the sampler reads about every 0.3-0.5s
SOURCE_PERIOD_LIMIT_MS = 1000  # >= 1 Hz of new samples: the 5s limit spans >= 5 of them
START_STATE = {"arming_state": "2", "nav_state": "4", "failsafe": "False"}

READ = re.compile(r"^READ (\d+)\s*$")
FIELD = re.compile(r"^\s*([a-zA-Z0-9_]+):\s*(\S+)")


def parse_reads(text):
    """[(read_ns, {timestamp, nav_state, arming_state, failsafe} or None)]."""
    reads, current, fields = [], None, {}

    def close():
        if current is not None:
            wanted = {"timestamp", *FIELDS}
            reads.append((current, {k: fields[k] for k in wanted} if wanted <= fields.keys() else None))
    for line in text.splitlines():
        match = READ.match(line)
        if match:
            close()
            current, fields = int(match.group(1)), {}
            continue
        match = FIELD.match(line)
        if match and current is not None and match.group(1) not in fields:
            fields[match.group(1)] = match.group(2)
    close()
    return reads


def markers_from_reads(reads):
    tracker = ContinuityTracker()
    markers, seen, repeated, failed, new_times = [], None, 0, 0, []
    for read_ns, sample in reads:
        if sample is None:
            failed += 1
        elif sample["timestamp"] == seen:
            repeated += 1
        else:
            seen = sample["timestamp"]
            new_times.append(read_ns)
            state = {field: sample[field] for field in FIELDS}
            for name, fields in tracker.on_status(read_ns, int(sample["timestamp"]), state):
                markers.append((name, read_ns, {k: str(v) for k, v in fields.items()}))
        name, fields = tracker.summary(read_ns)
        markers.append((name, read_ns, {k: str(v) for k, v in fields.items()}))
    return markers, {"reads": len(reads), "new_samples": len(new_times), "repeated_reads": repeated,
                     "failed_reads": failed, "new_sample_times": new_times}


def judge(text, window_start_ns, window_end_ns):
    reads = parse_reads(text)
    markers, stats = markers_from_reads(reads)
    result = evaluate(markers, window_start_ns, window_end_ns, "0", start_state=START_STATE)
    inconclusive = []
    ok_reads = [ns for ns, sample in reads if sample is not None]
    around = [ns for ns in ok_reads if window_start_ns <= ns <= window_end_ns]
    before = [ns for ns in ok_reads if ns < window_start_ns]
    after = [ns for ns in ok_reads if ns > window_end_ns]
    edges = ([before[-1]] if before else []) + around + ([after[0]] if after else [])
    read_gaps = [(b - a) // 1_000_000 for a, b in zip(edges, edges[1:])]
    max_read_gap = max(read_gaps, default=None)
    if not before or not after or max_read_gap is None or max_read_gap > READ_GAP_LIMIT_MS:
        inconclusive.append(f"reads do not cover the window (max gap {max_read_gap} ms, "
                            f"limit {READ_GAP_LIMIT_MS} ms)")
    inside = [ns for ns in stats["new_sample_times"] if window_start_ns <= ns <= window_end_ns]
    periods = [(b - a) // 1_000_000 for a, b in zip(inside, inside[1:])]
    source_period = statistics.median(periods) if periods else None
    if source_period is None or source_period > SOURCE_PERIOD_LIMIT_MS:
        inconclusive.append(f"source period {source_period} ms above {SOURCE_PERIOD_LIMIT_MS} ms: "
                            f"not compatible with the {GAP_LIMIT_MS} ms gap limit")
    verdict = result["verdict"]
    if inconclusive and verdict != "inconclusive":
        verdict = "inconclusive"
    reasons = "; ".join(inconclusive + ([result["reasons"]] if result["reasons"] != "none" else []))
    stats.pop("new_sample_times")
    return {**result, "verdict": verdict, "reasons": reasons or "none", **stats,
            "max_read_gap_ms": max_read_gap, "read_gap_limit_ms": READ_GAP_LIMIT_MS,
            "median_source_period_ms": source_period, "source_period_limit_ms": SOURCE_PERIOD_LIMIT_MS,
            "window_sec": round((window_end_ns - window_start_ns) / 1e9, 1)}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("samples")
    parser.add_argument("--window-start-ns", type=int, required=True)
    parser.add_argument("--window-end-ns", type=int, required=True)
    args = parser.parse_args(argv)
    try:
        text = Path(args.samples).read_text(errors="replace")
    except OSError:
        text = ""
    print(json.dumps(judge(text, args.window_start_ns, args.window_end_ns), indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
