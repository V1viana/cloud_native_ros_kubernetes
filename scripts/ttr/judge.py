#!/usr/bin/env python3
"""Time-to-rebuild judge (docs/TTR_PILOT_PREREGISTRATION.md).

  judge.py final RESULT_DIR VARIANT       -> RESULT_DIR/verdict.json
  judge.py wait  RESULT_DIR VARIANT BUDGET_SEC
      polls the samples until the full fleet's stable series is confirmed and the
      procedure has ended (RESULT_DIR/procedure.rc exists), or BUDGET_SEC after t0.

Inputs in RESULT_DIR: t0 (wall clock, just before the first procedure command),
procedure.rc and procedure.end, k8s-samples.jsonl, ros-samples.jsonl, events.json (final).

Rules (fixed before the pilot):
- A Kubernetes sample is ready when every Deployment of the variant's list (spec.py) has
  spec.replicas >= 1 ready, updated and observed at its generation. A ROS sample is ready
  when every expected lifecycle node is found once and answers "active". Error samples are
  gaps, never "not ready".
- The fleet is ready at t when the latest successful sample of BOTH samplers at or before t
  is ready. t1 is the first sample time from which the fleet stays ready for STABLE_SEC; it
  is confirmed only after those 15 s and the 15 s are not added to the duration.
- t1 must be resolved: for each sampler, the last successful sample before t1 is at most
  MAX_GAP_SEC before it, and inside [t1, t1 + 15 s] no two successful samples of a sampler
  are more than MAX_GAP_SEC apart. Otherwise INCONCLUSIVE: an observer gap is never turned
  into a later t1.
- CENSORED (a valid run, the fleet not ready within the budget) only when that is the sole
  reason and both observers have successful samples without gaps up to t0 + budget;
  otherwise INCONCLUSIVE (not valid). Only MEASURED runs give a time.
- INCONCLUSIVE also when: the procedure failed; no confirmed t1 within the budget; an image
  was pulled (Event "Pulling") between t0 and t1; the cadence did not hold (more than 5% of
  the attempt intervals of a sampler above 1.5 s between t0 and t1 + 15 s).
- The common subset (per drone Agent, PX4, analytics; analytics active) is judged on the
  same samples by the same rules, as a secondary measure.
"""

import calendar
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import spec  # noqa: E402

STABLE_SEC = 15.0
MAX_GAP_SEC = 2.0
CADENCE_LIMIT_SEC = 1.5
CADENCE_MAX_SHARE_ABOVE = 0.05


def k8s_ready(sample, variant, subset="full"):
    deployments = sample["deployments"] or {}
    for name in spec.deployments(variant, subset):
        d = deployments.get(name)
        if not d:
            return False
        want = d["replicas"]
        if want < 1 or d["ready"] < want or d["updated"] < want:
            return False
        if d["generation"] is not None and (d["observed"] or 0) < d["generation"]:
            return False
    return True


def ros_ready(sample, variant, subset="full"):
    nodes = sample["nodes"] or {}
    return all((nodes.get(f"{robot}/{key}") or {}).get("state") == "active"
               for robot, key in spec.lifecycle_keys(variant, subset))


def ok_samples(samples):
    return [s for s in samples if not s.get("error")]


def find_t1(k8s, ros, variant, t0, subset="full"):
    """(t1, problem). t1 None with problem None means: not (yet) confirmed."""
    series = {"k8s": [(s["t"], k8s_ready(s, variant, subset)) for s in ok_samples(k8s)],
              "ros": [(s["t"], ros_ready(s, variant, subset)) for s in ok_samples(ros)]}
    events = sorted((t, src, ready) for src, points in series.items() for t, ready in points if t >= t0)
    state = {}
    for src, points in series.items():  # state at t0, from the last sample before it
        before = [ready for t, ready in points if t < t0]
        state[src] = before[-1] if before else None
    if state["k8s"] and state["ros"]:
        return None, "fleet already ready before t0: not an empty cluster"
    candidate = None
    for t, src, ready in events:
        state[src] = ready
        if state["k8s"] and state["ros"]:
            if candidate is None:
                candidate = t
        else:
            candidate = None
        if candidate is not None and t >= candidate + STABLE_SEC:
            for name, points in series.items():
                times = [pt for pt, _ in points]
                before = [pt for pt in times if pt < candidate]
                inside = [pt for pt in times if candidate <= pt <= candidate + STABLE_SEC]
                if not before or candidate - before[-1] > MAX_GAP_SEC:
                    return None, f"{name}: gap before t1 above {MAX_GAP_SEC}s, t1 not resolved"
                chain = [before[-1]] + inside
                if any(b - a > MAX_GAP_SEC for a, b in zip(chain, chain[1:])):
                    return None, f"{name}: gap above {MAX_GAP_SEC}s inside the stable window"
                if not inside or inside[-1] < candidate + STABLE_SEC - MAX_GAP_SEC:
                    return None, f"{name}: stable window not covered to its end"
            return candidate, None
    return None, None


def cadence(samples, t_from, t_to):
    inside = [s for s in samples if t_from <= s["t_start"] <= t_to]
    starts = [s["t_start"] for s in inside]
    intervals = sorted(b - a for a, b in zip(starts, starts[1:]))
    ok_times = [s["t"] for s in ok_samples(inside)]
    gaps = [b - a for a, b in zip(ok_times, ok_times[1:])]
    above = sum(1 for i in intervals if i > CADENCE_LIMIT_SEC)
    return {"attempts": len(inside), "errors": len(inside) - len(ok_times),
            "interval_median_s": round(intervals[len(intervals) // 2], 3) if intervals else None,
            "interval_p95_s": round(intervals[int(0.95 * (len(intervals) - 1))], 3) if intervals else None,
            "interval_max_s": round(intervals[-1], 3) if intervals else None,
            "intervals_above_limit": above,
            "max_success_gap_s": round(max(gaps), 3) if gaps else None,
            "held": bool(intervals) and above <= CADENCE_MAX_SHARE_ABOVE * len(intervals)}


def first_ready(k8s, ros, variant, t0):
    """Descriptive: first time each listed component was seen ready (seconds after t0)."""
    out = {}
    for s in ok_samples(k8s):
        for name in spec.deployments(variant):
            d = (s["deployments"] or {}).get(name)
            if name not in out and d and d["replicas"] >= 1 and d["ready"] >= d["replicas"]:
                out[name] = round(s["t"] - t0, 3)
    for s in ok_samples(ros):
        for robot, key in spec.lifecycle_keys(variant):
            entry = (s["nodes"] or {}).get(f"{robot}/{key}") or {}
            if f"ros:{robot}/{key}" not in out and entry.get("state") == "active":
                out[f"ros:{robot}/{key}"] = round(s["t"] - t0, 3)
    return out


def pulls(events, t_from, t_to):
    out = []
    for e in events:
        if e.get("reason") != "Pulling":
            continue
        stamp = e.get("lastTimestamp") or e.get("eventTime") or e.get("firstTimestamp")
        if not stamp:
            out.append(e.get("message"))
            continue
        t = calendar.timegm(time.strptime(stamp[:19], "%Y-%m-%dT%H:%M:%S"))
        if t_from - 1 <= t <= t_to + 1:
            out.append(e.get("message"))
    return out


def coverage_to_limit(samples, t0, limit):
    """(ok, detail): successful samples from t0 to the limit with no gap above MAX_GAP_SEC --
    what it takes to say that no stable series went unseen."""
    times = sorted(s["t"] for s in ok_samples(samples) if t0 <= s["t"])
    inside = [t for t in times if t <= limit]
    if not inside or inside[0] - t0 > MAX_GAP_SEC:
        return False, "no successful sample right after t0"
    if not any(t >= limit for t in times):
        return False, f"successful samples stop at {inside[-1] - t0:.0f}s, before the {limit - t0:.0f}s limit"
    chain = inside + [min(t for t in times if t >= limit)]
    worst = max((b - a for a, b in zip(chain, chain[1:])), default=0.0)
    if worst > MAX_GAP_SEC:
        return False, f"gap of {worst:.1f}s between successful samples"
    return True, None


def judge(k8s, ros, variant, t0, procedure_rc, procedure_end, events, budget_sec):
    reasons = []
    result = {"variant": variant, "t0": t0, "stable_sec": STABLE_SEC, "max_gap_sec": MAX_GAP_SEC}
    t1, problem = find_t1(k8s, ros, variant, t0)
    t1c, problem_c = find_t1(k8s, ros, variant, t0, subset="common")
    if procedure_rc != 0:
        reasons.append(f"procedure failed (rc={procedure_rc})")
    if problem:
        reasons.append(problem)
    elif t1 is None:
        reasons.append(f"no confirmed stable series within {budget_sec}s")
    if t1 is not None and t1 - t0 > budget_sec:
        reasons.append("t1 beyond the budget")
    end = (t1 if t1 is not None else t0 + budget_sec) + STABLE_SEC
    result["cadence"] = {"k8s": cadence(k8s, t0, end), "ros": cadence(ros, t0, end)}
    for name, c in result["cadence"].items():
        if not c["held"]:
            reasons.append(f"{name}: cadence not held ({c['intervals_above_limit']}/{max(c['attempts'] - 1, 0)} "
                           f"intervals above {CADENCE_LIMIT_SEC}s)")
    pulled = pulls(events, t0, t1 if t1 is not None else end) if events is not None else None
    if pulled is None:
        reasons.append("events not collected: image pulls not verifiable")
    elif pulled:
        reasons.append(f"{len(pulled)} image pull(s) between t0 and t1")
    # CENSORED (draft TTR_R14_BLOCK_PREREGISTRATION.md 4; review of Viviana, 1 October): the fleet
    # was NOT seen ready within the budget, and this is a valid observation only if it is the
    # sole reason AND both observers have successful samples, without gaps, up to the limit.
    # Observers that stop early leave the run INCONCLUSIVE (not valid), never censored.
    not_ready = f"no confirmed stable series within {budget_sec}s"
    censored = False
    if reasons == [not_ready]:
        coverage = {name: coverage_to_limit(samples, t0, t0 + budget_sec) for name, samples in (("k8s", k8s), ("ros", ros))}
        result["coverage_to_limit"] = {name: {"ok": ok, "detail": detail} for name, (ok, detail) in coverage.items()}
        censored = all(ok for ok, _ in coverage.values())
        if not censored:
            reasons += [f"{name}: observer not covering up to the limit ({detail})"
                        for name, (ok, detail) in coverage.items() if not ok]
    result.update({
        "verdict": "MEASURED" if not reasons else "CENSORED" if censored else "INCONCLUSIVE", "reasons": reasons,
        "valid": not reasons or censored,
        "t1": t1, "time_to_rebuild_s": round(t1 - t0, 3) if t1 is not None else None,
        "procedure_rc": procedure_rc,
        "procedure_end_s": round(procedure_end - t0, 3) if procedure_end is not None else None,
        "secondary_common_subset": {"t1": t1c, "time_s": round(t1c - t0, 3) if t1c is not None else None,
                                    "problem": problem_c},
        "image_pulls": pulled,
        "first_ready_s": first_ready(k8s, ros, variant, t0),
        "samples_before_t0": {"k8s": sum(1 for s in k8s if s["t"] < t0), "ros": sum(1 for s in ros if s["t"] < t0)},
        "deployments": spec.deployments(variant), "lifecycle": [f"{r}/{k}" for r, k in spec.lifecycle_keys(variant)],
    })
    return result


def load_jsonl(path):
    out = []
    if os.path.exists(path):
        for line in open(path):
            try:
                out.append(json.loads(line))
            except ValueError:
                pass  # a line cut by a stop, or kubectl exec noise
    return [s for s in out if isinstance(s, dict) and "t" in s and "t_start" in s]


def read_opt(path, cast):
    return cast(open(path).read().strip()) if os.path.exists(path) else None


def main(argv):
    mode, res, variant = argv[0], argv[1], argv[2]
    t0 = read_opt(os.path.join(res, "t0"), float)
    if mode == "wait":
        budget = float(argv[3])
        while True:
            k8s, ros = load_jsonl(os.path.join(res, "k8s-samples.jsonl")), load_jsonl(os.path.join(res, "ros-samples.jsonl"))
            t1, problem = find_t1(k8s, ros, variant, t0)
            done = os.path.exists(os.path.join(res, "procedure.rc"))
            if done and (t1 is not None or problem):
                print(json.dumps({"t1": t1, "problem": problem}))
                return 0
            if time.time() > t0 + budget + STABLE_SEC:
                print(json.dumps({"t1": t1, "problem": problem or "budget exceeded", "procedure_done": done}))
                return 1
            time.sleep(2)
    events_path = os.path.join(res, "events.json")
    events = json.load(open(events_path)).get("items", []) if os.path.exists(events_path) else None
    result = judge(load_jsonl(os.path.join(res, "k8s-samples.jsonl")), load_jsonl(os.path.join(res, "ros-samples.jsonl")),
                   variant, t0, read_opt(os.path.join(res, "procedure.rc"), int),
                   read_opt(os.path.join(res, "procedure.end"), float), events, float(argv[3]) if len(argv) > 3 else 1800.0)
    json.dump(result, open(os.path.join(res, "verdict.json"), "w"), indent=1)
    print(json.dumps({k: result[k] for k in ("verdict", "reasons", "time_to_rebuild_s", "procedure_end_s")}))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
