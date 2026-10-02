#!/usr/bin/env python3
"""Position and altitude hold in E1 (checklist R8; Viviana's choices, 2026-09-25).

Judged on the simulator's ground truth: PX4 SIH's uORB
vehicle_local_position_groundtruth, read from the node with
scripts/px4_truth_sampler.sh (crictl exec px4-listener, so it keeps working with
the k3d server stopped). PX4's own estimate (DDS vehicle_local_position, logged
by the mission observer) is judged only on validity, continuity and resets;
its difference from the truth is reported.

Window: [start, end] -- in E1 from the partition start to the first AUTO_RTL
(after it the motion is intended); in a pilot the same span without fault or
partition. Hold point: the first truth sample in the window. Verdict:

  inconclusive  fewer than MIN_SAMPLES_PER_SEC new samples per second of
                window, a gap between new samples (window edges included)
                over MAX_SAMPLE_GAP_SEC, PX4 time going back between reads, an
                estimate log that does not cover the window, no estimate
                before the window, no frozen thresholds, or no AUTO_RTL to end
                the window;
  false         horizontal or vertical deviation above the thresholds; or the
                estimate, inside the window, invalid (xy/z not valid, dead
                reckoning), reset, or silent for ESTIMATE_GAP_LIMIT_MS;
  true          otherwise;
  pilot         pilot mode: deviations only, for the thresholds.

At a few samples per second this measures the deviation in the samples taken:
an excursion between two samples is not excluded.

Review of 8ee8f9a (Viviana): the rate and the holes were computed on the reads,
so repeated reads of one PX4 sample counted as data, and an estimate log with
nothing after its first message passed. Now a truth sample counts only if its
PX4 timestamp advanced and the listener reports it at most MAX_TRUTH_AGE_SEC
old (reads, duplicates, stale reads and PX4 time going back are reported), and
the estimate log must cover the window: a hole in its observations longer than
MAX_ESTIMATE_LOG_GAP_SEC is either a real silence the observer itself marked
(MISSION_POSITION_GAP, judged against ESTIMATE_GAP_LIMIT_MS) or an incomplete
log (inconclusive).
"""

import argparse
import json
import math
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from mission_continuity import parse as parse_markers  # noqa: E402

MIN_SAMPLES_PER_SEC = 2.0
MAX_SAMPLE_GAP_SEC = 2.0
ESTIMATE_GAP_LIMIT_MS = 5000     # the P1 heartbeat timeout, as for vehicle_status
PAIRING_SEC = 0.5                # estimate/truth pairs for the reported difference
MAX_TRUTH_AGE_SEC = 0.5          # listener's "seconds ago" for a read to be fresh
MAX_ESTIMATE_LOG_GAP_SEC = 1.5   # 3 x the observer's 0.5s trace period


def parse_truth(text):
    """Raw sampler output -> [{t_ns, px4_us, x, y, z}]; failed reads are dropped."""
    samples, block, stamp = [], None, None
    for line in text.splitlines():
        if line.startswith("SAMPLE "):
            _, t0, t1 = line.split()
            stamp, block = (int(t0) + int(t1)) // 2, {}
        elif line == "END":
            if block is not None and {"timestamp", "x", "y", "z"} <= block.keys():
                samples.append({"t_ns": stamp, "px4_us": int(block["timestamp"]),
                                "age_s": block.get("age"),
                                "x": block["x"], "y": block["y"], "z": block["z"]})
            block = None
        elif block is not None:
            key, _, rest = line.strip().partition(": ")
            if key in ("x", "y", "z"):
                try:
                    block[key] = float(rest.split()[0])
                except (ValueError, IndexError):
                    pass
            elif key == "timestamp":
                block[key] = rest.split()[0]
                if "(" in rest and "seconds ago" in rest:
                    try:
                        block["age"] = float(rest.split("(", 1)[1].split()[0])
                    except (ValueError, IndexError):
                        pass
    return samples


def rtl_time(markers, start_ns):
    return next((ts for name, ts, f in markers
                 if name == "MISSION_STATE_CHANGE" and ts >= start_ns
                 and f.get("field") == "nav_state" and f.get("after") == "5"), None)


def _estimate(markers, start_ns, end_ns):
    """(state at the window start, problems inside the window, logged trace,
    reasons the log itself is incomplete -- inconclusive, not a problem)."""
    problems, incomplete = [], []
    first = next((m for m in markers if m[0] == "MISSION_POSITION_FIRST"), None)
    if first is None or first[1] > start_ns:
        return None, [], [], []
    state = {k: first[2][k] for k in ("xy_valid", "z_valid", "dead_reckoning")}
    for name, ts, f in markers:
        if name == "MISSION_POSITION_FLAG_CHANGE" and ts < start_ns:
            state[f["field"]] = f["after"]
    if state != {"xy_valid": "true", "z_valid": "true", "dead_reckoning": "false"}:
        problems.append(f"stima non valida all'inizio della finestra ({state})")
    inside = lambda ts: start_ns <= ts <= end_ns
    for name, ts, f in markers:
        if name == "MISSION_POSITION_FLAG_CHANGE" and inside(ts):
            problems.append(f"stima: {f['field']} {f['before']}->{f['after']}")
        if name == "MISSION_POSITION_RESET" and inside(ts):
            problems.append(f"stima: reset {f['field']}")
    marked = [(ts - int(f["gap_ms"]) * 1_000_000, ts)
              for name, ts, f in markers if name == "MISSION_POSITION_GAP"]
    silences = marked + [(ts - int(f["last_age_ms"]) * 1_000_000, ts)
                         for name, ts, f in markers
                         if name == "MISSION_POSITION_SUMMARY" and int(f["last_age_ms"]) >= 0]
    # Coverage: every estimate message the log attests, including the last one a
    # summary reports; a longer hole must be a silence the observer marked.
    seen = sorted([ts for name, ts, _ in markers if name in (
        "MISSION_POSITION_FIRST", "MISSION_POSITION", "MISSION_POSITION_FLAG_CHANGE",
        "MISSION_POSITION_RESET", "MISSION_POSITION_GAP")]
        + [ts - int(f["last_age_ms"]) * 1_000_000 for name, ts, f in markers
           if name == "MISSION_POSITION_SUMMARY" and int(f["last_age_ms"]) >= 0])
    before = [t for t in seen if t < start_ns]
    after = [t for t in seen if t >= end_ns]
    points = before[-1:] + [t for t in seen if start_ns <= t < end_ns] + (after[:1] or [end_ns])
    limit_ns = MAX_ESTIMATE_LOG_GAP_SEC * 1e9
    holes = [(a, b) for a, b in zip(points, points[1:]) if b - a > limit_ns]
    unexplained = [(a, b) for a, b in holes
                   if not any(g0 <= a + 1e9 and g1 >= b - 1e8 for g0, g1 in marked)]
    if unexplained or not after:
        longest = max((b - a for a, b in unexplained), default=0) / 1e9
        incomplete.append(f"log della stima incompleto (buco di {longest:.1f}s"
                          + ("" if after else ", nessuna osservazione dopo la finestra") + ")")
    if any(b - a >= ESTIMATE_GAP_LIMIT_MS * 1_000_000 and a <= end_ns and b >= start_ns
           for a, b in silences):
        problems.append(f"stima silenziosa per almeno {ESTIMATE_GAP_LIMIT_MS} ms")
    trace = [(ts, float(f["x"]), float(f["y"]), float(f["z"]))
             for name, ts, f in markers if name == "MISSION_POSITION" and inside(ts)]
    return state, problems, trace, incomplete


def evaluate(truth, markers, start_ns, end_ns, thresholds=None, pilot=False, end_at_rtl=False):
    result = {"window_end": "explicit"}
    if end_at_rtl:
        end_ns = rtl_time(markers, start_ns)
        result["window_end"] = "first AUTO_RTL"
        if end_ns is None:
            return {**result, "verdict": "inconclusive", "reasons": "nessun AUTO_RTL dopo l'inizio"}
    window_sec = (end_ns - start_ns) / 1e9
    reads = [s for s in truth if start_ns <= s["t_ns"] <= end_ns]
    prior = [s for s in truth if s["t_ns"] < start_ns and (s["age_s"] or 0) <= MAX_TRUTH_AGE_SEC]
    last = prior[-1]["px4_us"] if prior else None
    inside, duplicates, stale, regressions = [], 0, 0, 0
    for s in reads:                   # new PX4 data only, not repeated or old reads
        if s["age_s"] is not None and s["age_s"] > MAX_TRUTH_AGE_SEC:
            stale += 1
        elif last is not None and s["px4_us"] == last:
            duplicates += 1
        elif last is not None and s["px4_us"] < last:
            regressions += 1
            last = s["px4_us"]
        else:
            inside.append(s)
            last = s["px4_us"]
    edges = [start_ns] + [s["t_ns"] for s in inside] + [end_ns]
    max_gap = max(b - a for a, b in zip(edges, edges[1:])) / 1e9
    inconclusive, failures = [], []
    if len(inside) < MIN_SAMPLES_PER_SEC * window_sec or not inside:
        inconclusive.append(f"{len(inside)} campioni in {window_sec:.1f}s "
                            f"(minimo {MIN_SAMPLES_PER_SEC:g}/s)")
    if max_gap > MAX_SAMPLE_GAP_SEC:
        inconclusive.append(f"buco fra campioni {max_gap:.1f}s (max {MAX_SAMPLE_GAP_SEC:g}s)")
    if regressions:
        inconclusive.append(f"tempo PX4 tornato indietro fra le letture ({regressions} volte)")
    h_dev = v_dev = 0.0
    if inside:
        x0, y0, z0 = inside[0]["x"], inside[0]["y"], inside[0]["z"]
        h_dev = max(math.hypot(s["x"] - x0, s["y"] - y0) for s in inside)
        v_dev = max(abs(s["z"] - z0) for s in inside)
    state, est_problems, trace, incomplete = _estimate(markers, start_ns, end_ns)
    if state is None:
        inconclusive.append("nessuna stima PX4 prima della finestra")
    inconclusive += incomplete
    if not pilot:
        failures += est_problems
        if not thresholds:
            inconclusive.append("soglie non congelate")
        else:
            if h_dev > thresholds["horizontal_m"]:
                failures.append(f"deviazione orizzontale {h_dev:.2f} m > {thresholds['horizontal_m']:.2f} m")
            if v_dev > thresholds["vertical_m"]:
                failures.append(f"deviazione di quota {v_dev:.2f} m > {thresholds['vertical_m']:.2f} m")
    # Reported: estimate minus truth, nearest pairs; frames may differ by a constant,
    # so its variation over the window is what compares.
    pairs = []
    for s in inside:
        near = min(trace, key=lambda e: abs(e[0] - s["t_ns"]), default=None)
        if near is not None and abs(near[0] - s["t_ns"]) <= PAIRING_SEC * 1e9:
            pairs.append((near[1] - s["x"], near[2] - s["y"], near[3] - s["z"]))
    diff = "n/d"
    if pairs:
        d0 = pairs[0]
        diff = (f"all'inizio ({d0[0]:+.2f}, {d0[1]:+.2f}, {d0[2]:+.2f}) m; variazione max "
                f"orizzontale {max(math.hypot(p[0] - d0[0], p[1] - d0[1]) for p in pairs):.2f} m, "
                f"verticale {max(abs(p[2] - d0[2]) for p in pairs):.2f} m su {len(pairs)} coppie")
    verdict = ("pilot" if pilot and not inconclusive else
               "inconclusive" if inconclusive else "false" if failures else "true")
    return {
        **result,
        "verdict": verdict,
        "reasons": "; ".join(inconclusive + failures) or "none",
        "window_sec": round(window_sec, 1),
        "truth_reads": len(reads),
        "truth_samples": len(inside),
        "truth_duplicates": duplicates,
        "truth_stale": stale,
        "truth_regressions": regressions,
        "max_sample_gap_sec": round(max_gap, 2),
        "max_horizontal_m": round(h_dev, 3),
        "max_vertical_m": round(v_dev, 3),
        "threshold_horizontal_m": thresholds["horizontal_m"] if thresholds else "n/d",
        "threshold_vertical_m": thresholds["vertical_m"] if thresholds else "n/d",
        "estimate_at_start": state if state is not None else "n/d",
        "estimate_problems": "; ".join(est_problems) or "none",
        "estimate_minus_truth": diff,
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--truth", required=True)
    parser.add_argument("--observer-log", required=True)
    parser.add_argument("--window-start-ns", type=int, required=True)
    parser.add_argument("--window-end-ns", type=int, default=0)
    parser.add_argument("--end-at-rtl", action="store_true")
    parser.add_argument("--thresholds")
    parser.add_argument("--pilot", action="store_true")
    args = parser.parse_args(argv)
    read = lambda path: Path(path).read_text(errors="replace") if Path(path).exists() else ""
    thresholds = json.loads(read(args.thresholds)) if args.thresholds and read(args.thresholds) else None
    result = evaluate(parse_truth(read(args.truth)), parse_markers(read(args.observer_log)),
                      args.window_start_ns, args.window_end_ns, thresholds,
                      pilot=args.pilot, end_at_rtl=args.end_at_rtl)
    for key, value in result.items():
        print(f"{key}={value}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
