#!/usr/bin/env python3
"""S4 edge pilot report (R13, docs/R13_S4_EDGE_PILOT_PROTOCOL.md): the facts of one
run, no comparative verdict. Offline, on the evidence of scripts/run_s4_bench.sh:

  validity     preparation stable, nothing foreign on the edge, the guard alive
               before the stop (and whether it fired), the fault's start observed
               within 2 s of T0, read coverage (API <= 3 s, health <= 10 s);
  preparation  the migration's times, recorded apart from the fault;
  fault        T0, the first sample with the container stopped and the kubelet not
               connected, the node's NotReady (a distinct, later measure);
  service      drone03's EDGE health: last positive before T0, first answer after
               T0 not positive, first positive after the start; the ONBOARD's own
               answers apart (its resumption is never the edge's return);
  declared     A: the edge Deployment and the dispatcher's lines; B: the edge
               ROSModule and the policy -- every change of value over time;
  evictions    Pod UIDs and deletions seen, eviction events;
  collateral   drone01/02/04: non-positive health, Pod UID/restart changes, actions.
Output s4-pilot-report<suffix>.json, never overwritten. Tested offline:
operator/tests/test_s4_pilot_report.py.
"""

import argparse
import json
import os
import re
import sys

LAG_LIMIT_SEC = 2.0
API_GAP_SEC, HEALTH_GAP_SEC = 3.0, 10.0
OTHERS = ("drone01", "drone02", "drone04")


def read_jsonl(path):
    out = []
    if not os.path.exists(path):
        return out
    with open(path, errors="replace") as h:
        for line in h:
            line = line.strip()
            if line:
                try:
                    out.append(json.loads(line))
                except ValueError:
                    out.append({"_unparsed": line[:200]})
    return out


def read_json(path):
    try:
        with open(path) as h:
            return json.load(h)
    except (OSError, ValueError):
        return None


def load(d):
    return {"marks": read_jsonl(os.path.join(d, "phases.jsonl")),
            "samples": read_jsonl(os.path.join(d, "edge-samples.jsonl")),
            "health": [r for r in read_jsonl(os.path.join(d, "prober-health.jsonl")) if r.get("event") == "health"],
            "inventory": read_jsonl(os.path.join(d, "observers/inventory.jsonl")),
            "nodes": read_jsonl(os.path.join(d, "observers/nodes.jsonl")),
            "events": read_json(os.path.join(d, "events-all.json")) or {},
            "logs": {name[5:-6]: read_jsonl(os.path.join(d, "observers", name))
                     for name in (os.listdir(os.path.join(d, "observers")) if os.path.isdir(os.path.join(d, "observers"))
                                  else []) if name.startswith("logs-") and name.endswith(".jsonl")}}


def first_mark(marks, name):
    return next((m for m in marks if m.get("mark") == name), None)


def max_gap(times, lo, hi):
    points = sorted(t for t in times if lo <= t <= hi)
    if not points:
        return None
    edges = [lo] + points + [hi]
    return round(max(b - a for a, b in zip(edges, edges[1:])), 3)


def changes(series):
    """[(t, value)] -> the points where the value changes."""
    out, last = [], object()
    for t, value in series:
        if value != last:
            out.append({"utc": t, "value": value})
            last = value
    return out


def report(d, variant, edge_node):
    data = load(d)
    marks = data["marks"]
    out = {"variant": variant, "result_dir": os.path.basename(os.path.normpath(d))}
    end = next((m for m in reversed(marks) if m.get("mark") == "driver_end"), None)
    out["driver_status"] = (end or {}).get("status")
    for name in ("not_started", "aborted", "driver_error", "guard_fired"):
        m = first_mark(marks, name)
        if m:
            out.setdefault("stops", []).append({name: m.get("reason") or m.get("error") or m.get("record")})
    prep = {n: first_mark(marks, n) for n in ("prep_start", "prep_delay_set", "prep_edge_serving",
                                               "prep_delay_reset", "prep_action", "prep_stable")}
    if prep["prep_start"]:
        base = prep["prep_start"]["mono"]
        out["preparation"] = {n[5:]: None if m is None else round(m["mono"] - base, 3) for n, m in prep.items()}
        out["preparation"]["action_state"] = (prep["prep_action"] or {}).get("state")
        out["preparation"]["stable"] = bool((prep["prep_stable"] or {}).get("ok"))
    t0 = first_mark(marks, "t0")
    guard = first_mark(marks, "guard_started")
    pods = first_mark(marks, "edge_pods")
    validity = {"preparation_stable": bool((prep.get("prep_stable") or {}).get("ok")),
                "edge_foreign_pods": None if pods is None else pods.get("foreign"),
                "guard_alive_before_stop": None if guard is None else guard.get("alive"),
                "guard_fired": first_mark(marks, "guard_fired") is not None}
    if t0 is None:
        out["validity"] = validity
        return out
    t0u, t0m = t0["utc"], t0["mono"]
    start = first_mark(marks, "start_command_start")
    horizon = first_mark(marks, "horizon_end")
    lo, hi = t0u, (horizon or marks[-1])["utc"]
    # the fault: outside the cluster
    down = next((s for s in data["samples"] if s["m0"] >= t0m and (s.get("inspect") or {}).get("running") is False
                 and (s.get("kubelet") or {}).get("result") != "connected"), None)
    lag = None if down is None else round(down["m1"] - t0m, 3)
    validity["fault_start_lag_sec"] = lag
    validity["fault_start_within_2s"] = lag is not None and lag <= LAG_LIMIT_SEC
    node_reads = [r for r in data["nodes"] if "error" not in r and "m1" in r]
    inv_reads = [r for r in data["inventory"] if "error" not in r and "m1" in r]
    validity["coverage"] = {
        "nodes_max_gap_sec": max_gap([r["w1"] for r in node_reads], lo, hi),
        "inventory_max_gap_sec": max_gap([r["w1"] for r in inv_reads], lo, hi),
        "health_max_gap_sec": {t: max_gap([r["outcome_utc"] for r in data["health"] if r.get("target") == t], lo, hi)
                               for t in sorted({r.get("target") for r in data["health"]})}}
    cov = validity["coverage"]
    validity["coverage"]["ok"] = all(v is not None and v <= API_GAP_SEC
                                     for v in (cov["nodes_max_gap_sec"], cov["inventory_max_gap_sec"])) and \
        all(v is not None and v <= HEALTH_GAP_SEC for v in cov["health_max_gap_sec"].values())
    out["validity"] = validity
    not_ready = next((r for r in node_reads if r["w0"] >= t0u and any(
        n.get("name") == edge_node and n.get("ready") != "True" for n in r.get("nodes") or [])), None)
    back_ready = None if start is None else next(
        (r for r in node_reads if r["w0"] >= start["utc"] and any(
            n.get("name") == edge_node and n.get("ready") == "True" for n in r.get("nodes") or [])), None)
    out["fault"] = {"t0_utc": t0u, "stop_command_end": (first_mark(marks, "stop_command_end") or {}).get("utc"),
                    "first_down_sample": None if down is None else {"w0": down["w0"], "w1": down["w1"],
                                                                     "inspect": down.get("inspect"),
                                                                     "kubelet": down.get("kubelet")},
                    "not_ready_after_t0_sec": None if not_ready is None else round(not_ready["w1"] - t0u, 3),
                    "start_command_utc": None if start is None else start["utc"],
                    "node_back_after_start_sec": (first_mark(marks, "node_back") or {}).get("after_start_sec"),
                    "node_ready_after_start_sec": None if back_ready is None or start is None
                    else round(back_ready["w1"] - start["utc"], 3)}
    # drone03's service, edge and onboard apart
    health = sorted(data["health"], key=lambda r: r.get("sent_utc", 0))

    def series(target):
        return [r for r in health if r.get("target") == target]
    edge, onboard = series("drone03-edge"), series("drone03-onboard")
    before = [r for r in edge if r["sent_utc"] < t0u and r.get("result") == "positive"]
    lost = next((r for r in edge if r["sent_utc"] >= t0u and r.get("result") != "positive"), None)
    returned = None if start is None else next(
        (r for r in edge if r["sent_utc"] >= start["utc"] and r.get("result") == "positive"), None)
    onboard_back = next((r for r in onboard if r["sent_utc"] >= t0u and r.get("result") == "positive"), None)

    def brief(r, base):
        return None if r is None else {"after_sec": round(r["sent_utc"] - base, 3), "result": r.get("result"),
                                       "reason": r.get("reason"), "answer": r.get("answer")}
    out["service"] = {
        "edge_last_positive_before_t0": brief(before[-1] if before else None, t0u),
        "edge_first_not_positive_after_t0": brief(lost, t0u),
        "edge_first_positive_after_start": None if start is None else brief(returned, start["utc"]),
        "edge_returned_within_horizon": returned is not None,
        "onboard_first_positive_after_t0": brief(onboard_back, t0u),
        "edge_results_after_t0": _counts(r for r in edge if r["sent_utc"] >= t0u),
        "onboard_results_after_t0": _counts(r for r in onboard if r["sent_utc"] >= t0u)}
    # declared state over time
    out["declared"] = declared(variant, inv_reads, data["logs"], t0u)
    out["evictions"] = evictions(inv_reads, data["events"], t0u)
    out["collateral"] = collateral(variant, health, inv_reads, data["logs"], t0u)
    return out


def _counts(records):
    counts = {}
    for r in records:
        counts[r.get("result")] = counts.get(r.get("result"), 0) + 1
    return counts


def declared(variant, reads, logs, t0u):
    out = {}
    if variant == "a":
        out["edge_deployment_ready"] = changes(
            (r["w1"], next((d.get("ready") for d in r.get("deployments") or []
                            if d.get("name") == "drone03-companion-analytics"), "absent"))
            for r in reads if r["w1"] >= t0u - 5)
        lines = [l for l in logs.get("operational-event-dispatcher-s4", []) if (l.get("w") or 0) >= t0u]
        out["dispatcher_lines_after_t0"] = [l.get("line", "")[:240] for l in lines
                                            if "drone03" in l.get("line", "")][:40]
    else:
        def module(r):
            m = next((m for m in r.get("rosmodules") or [] if m.get("name", "").startswith("companion-analytics-drone03")
                      and m.get("placement") == "edge"), None)
            return "absent" if m is None else f"{m.get('observedLifecycleState')}"
        out["edge_rosmodule_state"] = changes((r["w1"], module(r)) for r in reads if r["w1"] >= t0u - 5)
        out["policy_state"] = changes(
            (r["w1"], next((p.get("state") for p in r.get("policies") or []
                            if p.get("name") == "analytics-latency-slo-s4-drone03"), "absent"))
            for r in reads if r["w1"] >= t0u - 5)
        lines = [l for l in logs.get("fleet-operator", []) if (l.get("w") or 0) >= t0u]
        out["operator_lines_after_t0"] = [l.get("line", "")[:240] for l in lines
                                          if "drone03" in l.get("line", "")][:40]
    return out


def evictions(reads, events, t0u):
    uids, terminating = {}, set()
    for r in reads:
        for p in r.get("pods") or []:
            uids.setdefault(p["name"], set()).add(p.get("uid"))
            if p.get("terminating") and r["w1"] >= t0u:
                terminating.add(p["name"])
    evicting, before = [], 0
    for e in events.get("items") or []:
        if e.get("reason") not in ("TaintManagerEviction", "Killing", "NodeNotReady", "Evicted"):
            continue
        when = event_time(e)
        # second resolution: an event in T0's own second is kept; before it, not an effect of the fault
        if when is not None and when < int(t0u):
            before += 1
            continue
        evicting.append({"object": (e.get("involvedObject") or {}).get("name"), "reason": e.get("reason"),
                         "time": when, "message": (e.get("message") or "")[:160]})
    evicting.sort(key=lambda e: float("inf") if e["time"] is None else e["time"])
    return {"pods_terminating_after_t0": sorted(terminating), "events": evicting[:40],
            "events_before_t0": before,
            "names_with_several_uids": sorted(n for n, u in uids.items() if len(u) > 1)}


def event_time(event):
    """The event's latest time (lastTimestamp, or eventTime for the newer events API),
    UTC seconds; None if absent or unreadable."""
    from datetime import datetime
    for key in ("lastTimestamp", "eventTime", "firstTimestamp"):
        value = event.get(key)
        if value:
            try:
                return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
            except ValueError:
                return None
    return None


def collateral(variant, health, reads, logs, t0u):
    out = {}
    for robot in OTHERS:
        after = [r for r in health if r.get("target") == f"{robot}-onboard" and r["sent_utc"] >= t0u]
        restarts = {}
        for r in reads:
            for p in r.get("pods") or []:
                if robot in p.get("name", ""):
                    restarts.setdefault(p["name"], set()).add((p.get("uid"), json.dumps(p.get("restarts"),
                                                                                         sort_keys=True)))
        out[robot] = {"health_after_t0": _counts(after),
                      "pods_changed": sorted(n for n, v in restarts.items() if len(v) > 1)}
    if variant == "a":
        text = [l.get("line", "") for l in logs.get("operational-event-dispatcher-s4", []) if (l.get("w") or 0) >= t0u]
        out["actions_other_robots"] = [t[:200] for t in text if re.search(r"accepted for drone0[124]-", t)]
    else:
        out["policy_states_other_robots"] = {
            name: changes((r["w1"], next((p.get("state") for p in r.get("policies") or [] if p.get("name") == name),
                                         "absent")) for r in reads if r["w1"] >= t0u)
            for name in ("telemetry-heartbeat-s4-drone02", "analytics-latency-slo-s4-drone04")}
    return out


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("result_dir")
    parser.add_argument("--variant", required=True, choices=("a", "b"))
    parser.add_argument("--edge-node", default="k3d-cloud-native-s4-agent-4")
    parser.add_argument("--suffix", default="")
    args = parser.parse_args(argv)
    path = os.path.join(args.result_dir, f"s4-pilot-report{args.suffix}.json")
    if os.path.exists(path):
        print(f"{path} exists: a new reading goes to a new file (--suffix=...)", file=sys.stderr)
        return 4
    result = report(args.result_dir, args.variant, args.edge_node)
    with open(path, "w") as h:
        json.dump(result, h, indent=1, default=str)
    print(path)
    return 0


if __name__ == "__main__":
    sys.exit(main())
