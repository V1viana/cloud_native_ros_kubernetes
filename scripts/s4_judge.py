#!/usr/bin/env python3
"""S4 cell judge (R13, docs/R13_S4_CONTRACT_DRAFT.md), offline, on the evidence of
scripts/run_s4_bench.sh with S4_MODE=cell. A VALID trial and a FUNCTIONAL success
are kept apart (Viviana, after the edge pilot): a control plane that does not recover
with valid observations is a valid FAIL, not an INCONCLUSIVE.

Validity (each ok / fail; any fail -> INCONCLUSIVE):
  preparation   drone03 on the edge, stable (edge Active, onboard Inactive, no
                pending action), recorded apart;
  edge_clean    nothing but drone03's edge instance on the edge node before T0;
  premise       drone01 armed in Hold at T0 (PX4 reads before T0);
  guard         edge cells: seen alive before the stop (fired -> INTERRUPTED);
  effects       each injected fault's EFFECT observed within 2 s of T0 (battery:
                the low value received; telemetry: micro_ros_agent in state T, and
                drone02's vehicle_status then interrupted; edge: container stopped
                and kubelet unreachable), and within 2 s of each other;
  coverage      health answers <= 3 s apart per target, API reads <= 3 s.
Functional properties (ok / fail / unknown), each reported apart:
  battery    drone01's first nav_state change after T0 is AUTO_RTL, PX4 status
             continuous (mission_continuity, E1's semantics);
  telemetry  drone02's vehicle_status received again after the stop, within the
             horizon; the observed silence reported (a fast replacement of the Agent
             is recorded, not discarded as a bench fault);
  edge       drone03's edge health positive again after the start, within the
             horizon (the service);
  collateral every robot without a fault in the cell (drone04 always): positive
             health <= 3 s apart, Pods unchanged, no action, PX4 continuous.
Declared state of the edge, reported APART and never in the verdict (Viviana): the
seconds the declared state showed the edge available while its service was absent
(A: the Deployment Ready; B: the ROSModule Active) and, in B, the seconds the edge
instance's lastObservedTime was older than DECLARED_LAG_SEC (a stale state); 10 s is
a descriptive threshold, pre-registered.
Coverage counts every recorded probe attempt (timeouts and negative answers too).
Verdict: INTERRUPTED > NOT_STARTED > INCONCLUSIVE (validity, or a functional property
unknown) > FAIL (a functional property failed) > PASS.
Output s4-judge<suffix>.json, never overwritten. Exit 0 PASS, 1 FAIL, 2 INCONCLUSIVE,
3 INTERRUPTED, 5 NOT_STARTED. Tested offline: operator/tests/test_s4_judge.py.
"""

import argparse
import json
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import mission_continuity  # noqa: E402
import px4_status_continuity  # noqa: E402
from s4_pilot_report import read_jsonl  # noqa: E402

EFFECT_LIMIT_SEC = 2.0
HEALTH_GAP_SEC = 3.0
API_GAP_SEC = 3.0
TELEMETRY_GAP_SEC = 3.0         # descriptive: drone02's status silent at least this long (the rule's timeout)
DECLARED_LAG_SEC = 10.0         # descriptive threshold of the declared-state measures
NAV_HOLD, NAV_RTL, ARMED = "4", "5", "2"
ROBOT_OF = {"battery": "drone01", "telemetry": "drone02", "edge": "drone03"}
CELLS = {"l1-battery": ("battery",), "l1-telemetry": ("telemetry",), "l1-edge": ("edge",),
         "l3": ("battery", "telemetry", "edge")}
EXIT = {"PASS": 0, "FAIL": 1, "INCONCLUSIVE": 2, "INTERRUPTED": 3, "NOT_STARTED": 5}


def load(d):
    obs = os.path.join(d, "observers")
    return {"marks": read_jsonl(os.path.join(d, "phases.jsonl")),
            "samples": read_jsonl(os.path.join(d, "edge-samples.jsonl")),
            "health": [r for r in read_jsonl(os.path.join(d, "prober-health.jsonl")) if r.get("event") == "health"],
            "faults": read_jsonl(os.path.join(d, "faults.jsonl")),
            "inventory": [r for r in read_jsonl(os.path.join(obs, "inventory.jsonl")) if "error" not in r and "w1" in r],
            "nodes": [r for r in read_jsonl(os.path.join(obs, "nodes.jsonl")) if "error" not in r and "w1" in r],
            "logs": {n[5:-6]: read_jsonl(os.path.join(obs, n)) for n in (os.listdir(obs) if os.path.isdir(obs) else [])
                     if n.startswith("logs-") and n.endswith(".jsonl")},
            "px4": {r: _text(os.path.join(obs, f"px4-{r}.txt")) for r in ("drone01", "drone02", "drone03", "drone04")},
            "harness_log": _text(os.path.join(d, "logs", "s4-battery-fault-harness-drone01.log"))}


def _text(path):
    try:
        with open(path, errors="replace") as h:
            return h.read()
    except OSError:
        return None


def mark(marks, name):
    return next((m for m in marks if m.get("mark") == name), None)


def check(status, detail=None):
    return {"status": status, "detail": detail}


def max_gap(times, lo, hi):
    points = sorted(t for t in times if lo <= t <= hi)
    edges = [lo] + points + [hi]
    return round(max(b - a for a, b in zip(edges, edges[1:])), 3)


# ---- PX4 ------------------------------------------------------------------

def px4_state_before(text, t_ns):
    reads = [(ns, s) for ns, s in px4_status_continuity.parse_reads(text or "") if s is not None and ns < t_ns]
    return None if not reads else {k: reads[-1][1].get(k) for k in ("arming_state", "nav_state", "failsafe")}


def px4_continuity(text, lo_ns, hi_ns, expect_nav=None, not_before_ns=0):
    if text is None:
        return check("unknown", "no uORB reads")
    reads = px4_status_continuity.parse_reads(text)
    markers, stats = px4_status_continuity.markers_from_reads(reads)
    start = px4_state_before(text, lo_ns)
    if start is None:
        return check("unknown", "no PX4 read before the window")
    result = mission_continuity.evaluate(markers, lo_ns, hi_ns, "0", start_state=start, expect_nav=expect_nav,
                                         not_before_ns=not_before_ns)
    ok_ns = [ns for ns, s in reads if s is not None]
    inside = [ns for ns in ok_ns if lo_ns <= ns <= hi_ns]
    edges = [max([ns for ns in ok_ns if ns < lo_ns] or [lo_ns])] + inside + \
        [min([ns for ns in ok_ns if ns > hi_ns] or [hi_ns])]
    read_gap = max(((b - a) // 1_000_000 for a, b in zip(edges, edges[1:])), default=None)
    status = {"true": "ok", "false": "fail"}.get(result["verdict"], "unknown")
    if read_gap is None or read_gap > px4_status_continuity.READ_GAP_LIMIT_MS:
        # a gap of the reads is a gap of the measure; an observed change stays a fail
        if status == "ok" or (status == "fail" and all(f.strip().startswith("status gap")
                                                       for f in result["reasons"].split(";"))):
            status = "unknown"
    return check(status, {"reasons": result["reasons"], "transitions": result["transitions"], "start": start,
                          "max_read_gap_ms": read_gap})


# ---- the cell ---------------------------------------------------------------

def judge(d, variant, cell):
    data = load(d)
    marks = data["marks"]
    faults = CELLS[cell]
    out = {"variant": variant, "cell": cell, "faults": list(faults), "result_dir": os.path.basename(os.path.normpath(d))}
    end = next((m for m in reversed(marks) if m.get("mark") == "driver_end"), None)
    driver = (end or {}).get("status")
    out["driver_status"] = driver
    if driver == "not_started":
        out["verdict"] = "NOT_STARTED"
        out["reasons"] = [(mark(marks, "not_started") or {}).get("reason")]
        return out
    t0 = mark(marks, "t0")
    horizon = mark(marks, "horizon_end")
    if driver != "completed" or t0 is None or horizon is None or mark(marks, "guard_fired"):
        out["verdict"] = "INTERRUPTED"
        out["reasons"] = [m.get("reason") or m.get("error") or m.get("mark") for m in marks
                          if m.get("mark") in ("aborted", "driver_error", "guard_fired", "cleanup_failed")] \
            or [f"driver {driver}"]
        return out
    t0u, hu = t0["utc"], horizon["utc"]
    start = mark(marks, "start_command_start")
    validity, functional = {}, {}
    prep = mark(marks, "prep_stable")
    validity["preparation"] = check("ok" if prep and prep.get("ok") else "fail", prep and prep.get("detail"))
    pods = mark(marks, "edge_pods")
    validity["edge_clean"] = check("ok" if pods is not None and not pods.get("foreign") else "fail",
                                   pods and pods.get("foreign"))
    state = px4_state_before(data["px4"].get("drone01"), int(t0u * 1e9))
    validity["premise"] = check("ok" if state and state.get("arming_state") == ARMED
                                and state.get("nav_state") == NAV_HOLD else "fail", state)
    if "edge" in faults:
        guard = mark(marks, "guard_started")
        validity["guard"] = check("ok" if guard and guard.get("alive") else "fail")
    effects = (mark(marks, "effects") or {}).get("effects") or {}
    lags, observed = {}, []
    for fault in faults:
        e = effects.get(fault)
        lags[fault] = None if not e else e.get("lag_sec")
        if e:
            observed.append(e["observed_utc"])
    # the telemetry path after the stop: drone02's status stream, its silence described
    telemetry_gap = None
    if "telemetry" in faults:
        times = sorted(r["recv_utc"] for r in data["faults"]
                       if r.get("event") == "vehicle_status" and "drone02" in r.get("topic", ""))
        # split at the instant the process was seen stopped (state T), T0 without it
        split = (effects.get("telemetry") or {}).get("observed_utc") or t0u
        before = [t for t in times if t <= split]
        # the functional window ends at horizon_end: receipts after it (the observers run
        # 10 s longer only to close the continuity check) never count
        after = [t for t in times if split < t <= hu]
        telemetry_gap = {"last_before_utc": before[-1] if before else None,
                         "first_after_utc": after[0] if after else None,
                         "silence_sec": None if not before else round((after[0] if after else hu) - before[-1], 3)}
        telemetry_gap["silence_at_least_sec"] = TELEMETRY_GAP_SEC
        telemetry_gap["silence_reached"] = (telemetry_gap["silence_sec"] or 0) >= TELEMETRY_GAP_SEC
    spread = round(max(observed) - min(observed), 3) if len(observed) == len(faults) and observed else None
    effect_ok = all(v is not None and v <= EFFECT_LIMIT_SEC for v in lags.values()) and \
        (len(faults) == 1 or (spread is not None and spread <= EFFECT_LIMIT_SEC))
    if telemetry_gap is not None:          # the stream was flowing before the stop
        effect_ok = effect_ok and telemetry_gap["last_before_utc"] is not None
    validity["effects"] = check("ok" if effect_ok else "fail", {"lag_sec": lags, "spread_sec": spread,
                                                               "telemetry_path": telemetry_gap})
    gaps = {t: max_gap([r["outcome_utc"] for r in data["health"] if r.get("target") == t], t0u - 5, hu)
            for t in sorted({r.get("target") for r in data["health"]})}
    api = {"nodes": max_gap([r["w1"] for r in data["nodes"]], t0u - 5, hu),
           "inventory": max_gap([r["w1"] for r in data["inventory"]], t0u - 5, hu)}
    coverage_ok = bool(gaps) and all(v <= HEALTH_GAP_SEC for v in gaps.values()) and \
        all(v <= API_GAP_SEC for v in api.values())
    validity["coverage"] = check("ok" if coverage_ok else "fail", {"health": gaps, "api": api})

    lo_ns, hi_ns = int(t0u * 1e9), int(hu * 1e9)
    if "battery" in faults:
        functional["battery"] = px4_continuity(data["px4"].get("drone01"), lo_ns, hi_ns, expect_nav=NAV_RTL,
                                               not_before_ns=lo_ns)
        functional["battery"]["harness_markers"] = sorted(set(re.findall(r"E1_[A-Z_]+", data["harness_log"] or "")))
    if "telemetry" in faults:
        back = (telemetry_gap or {}).get("first_after_utc")
        functional["telemetry"] = check("ok" if back is not None else "fail",
                                        {"returned_after_t0_sec": None if back is None else round(back - t0u, 3),
                                         **(telemetry_gap or {})})
    if "edge" in faults:
        functional["edge_service"], out["declared"] = edge_properties(data, variant, t0u, start, hu)
    functional["collateral"] = collateral(data, variant, faults, t0u, hu)
    out["validity"], out["functional"] = validity, functional
    reasons = [f"validity {k}" for k, v in validity.items() if v["status"] != "ok"]
    fails = [f"functional {k}" for k, v in functional.items() if v["status"] == "fail"]
    unknown = [f"functional {k} unknown" for k, v in functional.items() if v["status"] == "unknown"]
    if reasons or unknown:
        out["verdict"] = "INCONCLUSIVE"
        out["reasons"] = reasons + unknown + fails
    elif fails:
        out["verdict"] = "FAIL"
        out["reasons"] = fails
    else:
        out["verdict"] = "PASS"
        out["reasons"] = []
    return out


def edge_properties(data, variant, t0u, start, hu):
    edge = sorted((r for r in data["health"] if r.get("target") == "drone03-edge"), key=lambda r: r["sent_utc"])
    returned = None if start is None else next(
        (r for r in edge if start["utc"] <= r["sent_utc"] <= hu and r.get("result") == "positive"), None)
    service = check("ok" if returned else "fail",
                    {"first_positive_after_start_sec": None if returned is None
                     else round(returned["sent_utc"] - start["utc"], 3)})
    # declared available while the service is absent, second by second over [T0, horizon]
    declared_at = []
    for r in data["inventory"]:
        if not t0u <= r["w1"] <= hu:
            continue
        if variant == "a":
            dep = next((x for x in r.get("deployments") or [] if x.get("name") == "drone03-companion-analytics"), None)
            available = bool(dep and (dep.get("ready") or 0) >= 1)
        else:
            mod = next((x for x in r.get("rosmodules") or [] if x.get("placement") == "edge"
                        and x.get("name", "").startswith("companion-analytics-drone03")), None)
            available = bool(mod and mod.get("observedLifecycleState") == "Active")
        declared_at.append((r["w1"], available))
    service_at = [(r["sent_utc"], r.get("result") == "positive") for r in edge if t0u <= r["sent_utc"] <= hu]
    wrong = 0.0
    for (t, available), (t_next, _) in zip(declared_at, declared_at[1:] + [(hu, None)]):
        before = [ok for ts, ok in service_at if ts <= t]
        if available and before and not before[-1]:
            wrong += t_next - t
    declared = {"declared_available_while_service_absent_sec": round(wrong, 1),
                "descriptive_threshold_sec": DECLARED_LAG_SEC, "above_threshold": wrong > DECLARED_LAG_SEC,
                "declared_reads": len(declared_at), "in_verdict": False}
    if variant == "b":
        declared.update(stale_state(data, t0u, hu))
    return service, declared


def _utc(text):
    from datetime import datetime
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp()
    except (AttributeError, ValueError):
        return None


def stale_state(data, t0u, hu):
    """B: the edge instance's lastObservedTime against each read's own time; the
    seconds it was older than DECLARED_LAG_SEC, and the oldest."""
    ages = []
    for r in data["inventory"]:
        if not t0u <= r["w1"] <= hu:
            continue
        mod = next((x for x in r.get("rosmodules") or [] if x.get("placement") == "edge"
                    and x.get("name", "").startswith("companion-analytics-drone03")), None)
        seen = [_utc((v or {}).get("lastObservedTime")) for v in ((mod or {}).get("lifecycleInstances") or {}).values()]
        seen = [t for t in seen if t is not None]
        ages.append((r["w1"], None if not seen else r["w1"] - max(seen)))
    stale = 0.0
    for (t, age), (t_next, _) in zip(ages, ages[1:] + [(hu, None)]):
        if age is not None and age > DECLARED_LAG_SEC:
            stale += t_next - t
    known = [a for _, a in ages if a is not None]
    return {"stale_state_sec": round(stale, 1), "max_last_observed_age_sec": round(max(known), 1) if known else None,
            "stale_reads_without_time": sum(1 for _, a in ages if a is None)}


def collateral(data, variant, faults, t0u, hu):
    affected = {ROBOT_OF[f] for f in faults}
    others = [r for r in ("drone01", "drone02", "drone03", "drone04") if r not in affected]
    detail, bad, unknown = {}, [], []
    for robot in others:
        target = "drone03-edge" if robot == "drone03" else f"{robot}-onboard"
        positives = [r["sent_utc"] for r in data["health"] if r.get("target") == target and r.get("result") == "positive"]
        gap = max_gap(positives, t0u, hu)
        pods = {}
        for r in data["inventory"]:
            if t0u - 5 <= r["w1"] <= hu:
                for p in r.get("pods") or []:
                    if robot in p.get("name", ""):
                        pods.setdefault(p["name"], set()).add((p.get("uid"), json.dumps(p.get("restarts"),
                                                                                         sort_keys=True)))
        changed = sorted(n for n, v in pods.items() if len(v) > 1)
        if variant == "a":
            actions = [l.get("line", "")[:160] for l in data["logs"].get("operational-event-dispatcher-s4", [])
                       if t0u <= (l.get("w") or 0) <= hu and f"accepted for {robot}-" in l.get("line", "")]
        else:
            actions = sorted({p.get("state") for r in data["inventory"] if t0u <= r["w1"] <= hu
                              for p in r.get("policies") or [] if p.get("name", "").endswith(robot)
                              and p.get("state") not in ("Nominal", "Recovered", None)})
        px4 = px4_continuity(data["px4"].get(robot), int(t0u * 1e9), int(hu * 1e9))
        detail[robot] = {"health_max_gap_sec": gap, "pods_changed": changed, "actions": actions,
                         "px4": px4["status"], "px4_detail": px4["detail"]}
        if gap > HEALTH_GAP_SEC or changed or actions or px4["status"] == "fail":
            bad.append(robot)
        elif px4["status"] == "unknown":
            unknown.append(robot)
    return check("fail" if bad else ("unknown" if unknown else "ok"), {"robots": others, **detail})


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("result_dir")
    parser.add_argument("--variant", required=True, choices=("a", "b"))
    parser.add_argument("--cell", required=True, choices=sorted(CELLS))
    parser.add_argument("--suffix", default="")
    args = parser.parse_args(argv)
    path = os.path.join(args.result_dir, f"s4-judge{args.suffix}.json")
    if os.path.exists(path):
        print(f"{path} exists: a re-judgement goes to a new file (--suffix=...)", file=sys.stderr)
        return 4
    result = judge(args.result_dir, args.variant, args.cell)
    with open(path, "w") as h:
        json.dump(result, h, indent=1, default=str)
    print(f"{result['verdict']}: {path}")
    for reason in result.get("reasons") or []:
        print(f"  {reason}")
    return EXIT[result["verdict"]]


if __name__ == "__main__":
    sys.exit(main())
