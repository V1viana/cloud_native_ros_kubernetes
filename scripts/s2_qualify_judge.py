#!/usr/bin/env python3
"""S2 bench qualification judge (R11, "Qualificazione live del banco"), offline,
on the evidence of scripts/run_s2.sh with S2_CASE=qualification.

Every check is pre-registered and kept apart: `ok`, `fail` (observed against
the expectation) or `unknown` (the measurement does not hold). The bench is
QUALIFIED only if every check is ok; NOT_QUALIFIED otherwise, with the failing
and unknown checks named; INTERRUPTED if the real guard fired or the driver did
not end. Actions after the restore are recorded, never counted as transients.
The counters prove traffic intercepted (positive in at least one read), never
the cut's hold, which Q3_hold reads apart every second (rules, probes, gaps
unknown); every decrease of a counter is recorded, not interpreted.
The qualified topology (variant, peers, k3s version and arguments, the bench's
files, the images) is what a cell must match. Output s2-qualify<suffix>.json,
never overwritten. Exit: 0 QUALIFIED, 1 NOT_QUALIFIED, 3 INTERRUPTED.
Tested offline: operator/tests/test_s2_qualify_judge.py.
"""

import argparse
import importlib.util
import json
import math
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))


def _load(name):
    spec = importlib.util.spec_from_file_location(name, os.path.join(HERE, f"{name}.py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


j = _load("s2_judge")
q = _load("s2_qualify")

PROTOCOL_ID = "s2-partition-v1-qualification"
GUARD_LIMIT_SEC = 120.0
MIN_FREE_BYTES = 2 * 1024 ** 3
CYCLE_GUARD_SEC, CYCLE_EARLY_SEC, CYCLE_LATE_SEC = 10.0, 1.0, 3.0
LEASE_STALL_AFTER_SEC = 12.0      # lease renewals every 10 s: none from 12 s after the first DROP
NOT_READY_BY_SEC = 60.0           # grace period (default 40 s in Kubernetes 1.31) + 20 s
REQUIRED_COUNTERS = ("api out", "discovery out", "vxlan out", "vxlan in")


def check(checks, name, status, detail=None):
    checks.append({"check": name, "status": status, "detail": detail})


def step(checks):
    statuses = {c["status"] for c in checks}
    return {"status": "fail" if "fail" in statuses else ("unknown" if "unknown" in statuses else "ok"),
            "checks": checks}


def ok_if(condition):
    return "ok" if condition else "fail"


def q1_facts(data, marks):
    checks = []
    facts = (marks.first("facts") or {}).get("facts")
    if facts is None:
        check(checks, "facts recorded", "unknown", "no facts mark")
        return step(checks), {}
    threshold = q.eviction_threshold(facts.get("tolerations_on_node"))
    if threshold is None or threshold["min_sec"] is None:
        check(checks, "eviction threshold > 120 s", "unknown", "tolerations not read")
    else:
        lowest = threshold["min_sec"]
        check(checks, "eviction threshold > 120 s", ok_if(lowest == "forever" or lowest > GUARD_LIMIT_SEC),
              {"min_sec": lowest, "lowest_pods": sorted(p for p, v in threshold["per_pod"].items() if v == lowest)[:5]})
    free = facts.get("free_bytes")
    check(checks, "free space >= 2 GiB", "unknown" if free is None else ok_if(free >= MIN_FREE_BYTES), free)
    names = sorted(p["name"] for p in facts.get("peers") or [])
    check(checks, "peers are the E0 topology", ok_if(names == sorted(q.EXPECTED_PEERS)), names)
    for key in ("server_version", "k3s_server_cmdline"):
        check(checks, f"{key} recorded", "ok" if facts.get(key) else "unknown", facts.get(key))
    # decision after the second eight-cell round: nothing but drone01's workloads where
    # the partition cuts, at the start and at the end; the system services' pins recorded
    start, end = data.get("placement_start"), data.get("placement_end")
    if start is None or end is None:
        check(checks, "only drone01's workloads on the isolated node (start and end)", "unknown",
              "placement not recorded")
    else:
        foreign = [f"{when}: {p['namespace']}/{p['name']}" for when, placed in (("start", start), ("end", end))
                   for p in placed.get("foreign") or []]
        check(checks, "only drone01's workloads on the isolated node (start and end)", ok_if(not foreign),
              {"foreign": foreign, "system_start": start.get("system"), "system_end": end.get("system"),
               "system_pins": facts.get("system_pins")})
    return step(checks), facts


def q2_guard_cycle(data, marks):
    checks = []
    started, killed = marks.first("cycle_guard_started"), marks.first("cycle_driver_killed")
    fired, clean, back = marks.first("cycle_guard_fired"), marks.first("cycle_clean"), marks.first("cycle_connected")
    applied, stable = marks.first("cycle_applied"), marks.first("stabilized")
    check(checks, "cycle guard running before the cut", ok_if(bool(started and started.get("alive"))))
    check(checks, "cut applied by a separate process", ok_if(bool(applied and applied.get("ok"))),
          (applied or {}).get("detail"))
    check(checks, "that process killed before the guard fired",
          ok_if(bool(killed and fired and killed["mono"] < fired["mono"])))
    fired_utc = data.get("cycle_fired_utc")
    if started is None or fired_utc is None:
        check(checks, "guard fired 10 s after its start", "fail" if started else "unknown", fired_utc)
    else:
        after = fired_utc - started["utc"]
        check(checks, "guard fired 10 s after its start",
              ok_if(CYCLE_GUARD_SEC - CYCLE_EARLY_SEC <= after <= CYCLE_GUARD_SEC + CYCLE_LATE_SEC), round(after, 3))
    check(checks, "every rule removed by the guard alone",
          ok_if(bool(clean and clean.get("ok")) and marks.first("cycle_emergency_remove") is None))
    check(checks, "connection back after the guard", ok_if(bool(back and back.get("ok"))), (back or {}).get("detail"))
    check(checks, "stabilized before the real cut", ok_if(bool(stable and stable.get("ok"))))
    return step(checks)


LEASE_SCOPE = ("the lease renewal channel of drone01's node (kubelet -> API): interrupted during the cut; "
               "with the probed paths cut and the DROP ahead of any ACCEPT of established traffic -- "
               "not proof that every TCP connection open before the cut was interrupted")


def agent0(read):
    return next((n for n in read.get("nodes") or [] if n.get("name") == "k3d-cloud-native-p2-agent-0"), None)


def lease_channel(data, reference_start, first_drop, restore_start, restore_end, horizon):
    """Decision on point 4: at least two distinct renewTime values before the cut,
    none new from +12 s to the restore, a new one after it. A window the node reads
    do not cover (gap > 3 s) is unknown; a renewal during the cut is a fail."""
    reads = j.ok_reads(data["nodes"])

    def renewals(lo, hi):
        values = []
        for r in reads:
            node = agent0(r)
            if lo <= r["m0"] <= hi and node is not None and node.get("lease_renew"):
                values.append(node["lease_renew"])
        return values

    def covered(lo, hi):
        return j.read_coverage(data["nodes"], lo, hi)["ok"]
    out = {}
    before = sorted(set(renewals(reference_start, first_drop)))
    out["before"] = ("ok" if len(before) >= 2 else "unknown") if covered(reference_start, first_drop) else "unknown", \
        {"renewals": before}
    during_lo = first_drop + LEASE_STALL_AFTER_SEC
    during = sorted(set(renewals(during_lo, restore_start)))
    if len(during) > 1:
        status = "fail"                         # an increment while the node is cut off
    elif not covered(during_lo, restore_start) or not during:
        status = "unknown"
    else:
        status = "ok"
    out["during"] = status, {"renewals": during}
    last_during = j.parse_iso(during[-1]) if during else None
    after = sorted(set(renewals(restore_end, horizon)))
    newer = [v for v in after if last_during is not None and (j.parse_iso(v) or 0) > last_during]
    if newer:
        status = "ok"
    elif not covered(restore_end, horizon) or last_during is None:
        status = "unknown"
    else:
        status = "fail"
    out["after"] = status, {"renewals": after[-3:], "first_new": newer[0] if newer else None}
    return out


def not_ready(data, first_drop, restore_start):
    """When agent-0 was first seen not Ready in the cut, and the actual condition
    (Unknown for a node that stopped reporting, False for one reporting NotReady)."""
    for r in j.ok_reads(data["nodes"]):
        node = agent0(r)
        if node is not None and first_drop <= r["m0"] <= restore_start and node.get("ready") != "True":
            return {"at_sec": round(r["m0"] - first_drop, 3), "ready": node.get("ready"), "reason": node.get("reason")}
    return None


def q3_cut(data, marks, tl):
    checks = []
    guard, verified = marks.first("guard_started"), marks.first("cut_verified")
    drop, restore = marks.first("drop_apply_start"), marks.first("restore_start")
    check(checks, "real guard running before the first DROP",
          ok_if(bool(guard and drop and guard.get("alive") and guard["mono"] < drop["mono"])))
    check(checks, "cut verified (jumps, chain, rules, probes time out)", ok_if(verified is not None),
          (marks.first("cut_not_verified") or {}).get("reasons"))
    counters = ((restore or {}).get("status") or {}).get("accounting") or {}
    totals = {}
    for key, value in counters.items():
        service, direction, _peer = key.split(" ")
        totals[f"{service} {direction}"] = totals.get(f"{service} {direction}", 0) + value["pkts"]
    # traffic intercepted: positive in at least one read of the cut -- proof that
    # such traffic was intercepted, not that the cut held (decision after the fifth
    # qualification: the counters may be reset; the hold is Q3_hold)
    window = j.cut_window(marks)
    reads = j.counter_reads(data, marks, window) if window else {"driver": [], "samples": []}
    seen = j.intercepted(reads, REQUIRED_COUNTERS)
    for name in REQUIRED_COUNTERS:
        check(checks, f"traffic intercepted: {name} positive in at least one read of the cut", seen[name]["status"],
              seen[name])
    reference, written = marks.first("reference_start"), marks.first("partition_restored_written")
    names = ("lease renewal channel: >= 2 renewals before the cut",
             "lease renewal channel: no renewal from +12 s in the cut",
             "lease renewal channel: a new renewal after the restore")
    if drop is None or restore is None or reference is None or written is None:
        for name in names:
            check(checks, name, "unknown")
        check(checks, "node not Ready by 60 s", "unknown")
    else:
        channel = lease_channel(data, reference["mono"], drop["mono"], restore["mono"], written["restore_end_mono"],
                                (tl.get("horizon") or {}).get("mono", math.inf))
        for name, key in zip(names, ("before", "during", "after")):
            check(checks, name, channel[key][0], channel[key][1])
        seen = not_ready(data, drop["mono"], restore["mono"])
        check(checks, "node not Ready by 60 s",
              ok_if(seen is not None and seen["at_sec"] <= NOT_READY_BY_SEC), seen)
        inventory = j.read_coverage(data["inventory"], drop["mono"], restore["mono"])
        check(checks, "API from the host throughout the cut", ok_if(inventory["ok"]), inventory)
    return step(checks), totals, reads


def q3_hold(data, marks):
    """The cut held as declared, read apart from the counters: every rules sample
    as declared, no probe across the cut (a connection invalidates), both measured
    throughout at the declared resolution -- a gap is unknown, never ok."""
    hold = j.cut_hold(data, marks)
    checks = []
    check(checks, "cut held: every rules sample with the jumps first in INPUT/OUTPUT/FORWARD, the chain and "
                  "every DROP", hold["status"]["rules"], hold.get("rules") or hold.get("reason"))
    check(checks, "cut held: no probe across the cut (TCP from drone01's harness, ICMP from its node)",
          hold["status"]["probes"], {"breaches": hold.get("breaches"), "probes": hold.get("probes")}
          if "probes" in hold else hold.get("reason"))
    check(checks, f"cut held: measured throughout, rules and every probed path at most {j.HOLD_GAP_SEC:.0f} s apart",
          hold["status"]["coverage"], hold.get("gaps") if "gaps" in hold else hold.get("reason"))
    # the monitor's own load (decision after the fifth qualification): the recorder's
    # continuity under the monitor alone, the bench's 2 s budget; the intervals
    # against the reference are recorded
    intervals = j.recorder_intervals(data, marks)
    hold["recorder_intervals"] = intervals
    alone = intervals.get("monitor_alone")
    load, load_end = marks.first("monitor_load_start"), marks.first("monitor_load_end")
    if alone is None or not alone["samples"]:
        status = "unknown"
    else:
        samples = [s["recv_mono"] for s in data["samples"] or [] if s.get("event") == "sample" and j._mine(s)]
        worst = j.gaps(samples, load["mono"], load_end["mono"])
        status = ok_if(worst <= j.SAMPLE_GAP_SEC)
        alone = {**alone, "max_gap_sec_with_edges": None if math.isinf(worst) else round(worst, 3)}
    check(checks, f"monitor's load: recorder continuity under the monitor alone (gap <= {j.SAMPLE_GAP_SEC:.0f} s)",
          status, {"monitor_alone": alone, "reference": intervals.get("reference")})
    return step(checks), hold


def eviction_status(data, baseline, drop, reads, horizon, tl):
    """Decision 1 after the first round. Only drone01's Pods (by name and UID, from
    the last inventory before the first DROP) and only the window from the first
    DROP to the horizon: a taint manager's deletion request, an Evicted Pod, a
    deletion (deletionTimestamp) or a disappearance fail the check; a cancellation
    is a separate fact, no eviction; a message that cannot be classified is unknown."""
    if baseline is None or drop is None:
        return "unknown", "no inventory before the first DROP"
    pods = {p["name"]: p["uid"] for p in baseline.get("pods") or [] if p.get("node") == "k3d-cloud-native-p2-agent-0"}
    lo = drop["utc"] - 1.0                           # event times have whole seconds
    hi = ((tl.get("horizon") or {}).get("utc") or math.inf) + 1.0
    facts = {"requests": [], "cancellations": [], "evicted": [], "unclassified": [], "deleted_or_missing": []}
    for e in (data["k8s_events"] or {}).get("items") or []:
        obj = e.get("involvedObject") or {}
        if obj.get("kind") != "Pod" or obj.get("name") not in pods or obj.get("uid") not in (None, pods[obj["name"]]):
            continue
        stamp = j.parse_iso(e.get("lastTimestamp") or e.get("eventTime") or e.get("firstTimestamp"))
        if stamp is None or not lo <= stamp <= hi:
            continue
        message, reason = e.get("message") or "", e.get("reason")
        entry = {"pod": obj["name"], "reason": reason, "message": message[:160], "time": stamp, "count": e.get("count")}
        if reason == "Evicted":
            facts["evicted"].append(entry)
        elif reason == "TaintManagerEviction" and message.startswith("Marking for deletion"):
            facts["requests"].append(entry)
        elif reason == "TaintManagerEviction" and message.startswith("Cancelling deletion"):
            facts["cancellations"].append(entry)
        elif reason == "TaintManagerEviction":
            facts["unclassified"].append(entry)
    for r in reads:
        if drop["mono"] <= r["m0"] <= horizon:
            now = {p["name"]: p for p in r.get("pods") or []}
            for name, uid in pods.items():
                pod = now.get(name)
                if pod is None or pod.get("uid") != uid or pod.get("terminating"):
                    facts["deleted_or_missing"].append({"pod": name, "at": r["w0"],
                                                        "state": "missing" if pod is None else
                                                        ("terminating" if pod.get("terminating") else "replaced")})
    first_seen = {}
    for item in facts["deleted_or_missing"]:
        first_seen.setdefault(item["pod"], item)
    facts["deleted_or_missing"] = list(first_seen.values())
    if facts["requests"] or facts["evicted"] or facts["deleted_or_missing"]:
        return "fail", facts
    return ("unknown" if facts["unclassified"] else "ok"), facts


def q4_local(data, marks, tl, gt, harness):
    checks = []
    verified, remove = marks.first("cut_verified"), marks.first("remove_start")
    samples = [s for s in data["samples"] or [] if s.get("event") == "sample" and j._mine(s)]
    if verified is None or remove is None:
        check(checks, "local metrics through the cut", "unknown")
    else:
        worst = j.gaps([s["recv_mono"] for s in samples], verified["mono"], remove["mono"])
        check(checks, "local metrics through the cut (gap <= 2 s)", ok_if(worst <= j.SAMPLE_GAP_SEC),
              None if math.isinf(worst) else round(worst, 3))
    check(checks, "parameter services through the cut (pulse confirmed)",
          ok_if(bool((harness.get("pulse") or {}).get("confirmed"))))
    episode = gt.get("episode")
    inside = bool(episode and episode.get("returned") and verified and remove
                  and episode["first_violating_window_start"]["mono"] > verified["mono"]
                  and episode["returned"]["mono"] < remove["mono"])
    check(checks, "local entry and return inside the cut", ok_if(inside))
    reads = j.ok_reads(data["inventory"])
    drop = marks.first("drop_apply_start")
    horizon = (tl.get("horizon") or {}).get("mono", math.inf)

    def drone01(read):
        return {p["name"]: (p["uid"], tuple(sorted((p.get("restarts") or {}).items())))
                for p in read.get("pods") or [] if p.get("node") == "k3d-cloud-native-p2-agent-0"}
    baseline = next((r for r in reversed(reads) if drop and r["m1"] <= drop["mono"]), None)
    if baseline is None:
        check(checks, "drone01 Pods: same UIDs and restarts", "unknown")
    else:
        changed = next((r for r in reads if drop and drop["mono"] <= r["m0"] <= horizon
                        and drone01(r) != drone01(baseline)), None)
        check(checks, "drone01 Pods: same UIDs and restarts", ok_if(changed is None),
              None if changed is None else changed["w0"])
    check(checks, "no eviction of drone01's Pods", *eviction_status(data, baseline, drop, reads, horizon, tl))
    return step(checks)


def health_back(data, written, tl):
    """Decision 2 after the first round, from the prober's history: the first
    positive answer of drone01's onboard, with the expected robot and instance, to
    a call started after the end of the removal and within the way-back budget.
    A negative, inactive or other-identity answer does not satisfy it; later
    answers stay visible (a late migration), and a first positive does not
    certify continuous availability. No positive with the target observed
    throughout the budget is a fail; observations missing (gap > 10 s) unknown."""
    if written is None:
        return "unknown", "no restore"
    end = written["restore_end_mono"]
    budget = end + q.QPARAMS["back_max_sec"]
    records = [h for h in data["health"] or [] if h.get("event") == "health" and h.get("target") == "drone01-onboard"
               and h.get("sent_mono", -math.inf) > end]
    within = [h for h in records if h["sent_mono"] <= budget]
    first = next((h for h in within if h.get("result") == "positive"
                  and ((h.get("answer") or {}).get("robot_id"), (h.get("answer") or {}).get("instance_id"))
                  == (j.ROBOT, "onboard")), None)
    horizon = (tl.get("horizon") or {}).get("mono", math.inf)
    later = {}
    for h in records:
        if first is not None and h["sent_mono"] > first["sent_mono"] and h["sent_mono"] <= horizon:
            later[h["result"]] = later.get(h["result"], 0) + 1
    detail = {"first_positive": None if first is None else {
                  k: first.get(k) for k in ("sent_mono", "sent_utc", "outcome_mono", "outcome_utc", "result", "answer")}
              | {"after_restore_sec": round(first["sent_mono"] - end, 3)},
              "budget_sec": q.QPARAMS["back_max_sec"], "later_results": later,
              "note": "a first positive answer does not certify continuous availability"}
    if first is not None:
        return "ok", detail
    # observed throughout the budget: an answer (not a collector error) within 10 s of
    # the restore, of each other and of the budget's end
    observed = sorted(h["outcome_mono"] for h in within if h.get("result") != "error")
    edges = [end] + observed + [budget]
    worst = max(b - a for a, b in zip(edges, edges[1:]))
    detail["max_gap_sec"] = round(worst, 3)
    return ("fail" if observed and worst <= j.HEALTH_GAP_SEC else "unknown"), detail


OBSERVER_STOP_SEC = 5.0


def observers_stopped(data):
    """Decision 3 after the first round: every log follower ends within 5 s of the
    runner's stop, and the runner had to kill none of the observers."""
    run = data["run"]
    stop = run.get("observers_stop_utc")
    killed = run.get("observers_killed")
    if stop is None or killed is None:
        return "unknown", "stop time or killed observers not recorded"
    ends = {}
    for name, records in data["logs"].items():
        after = [r["w"] for r in records or [] if r.get("kind") == "follow_end" and r.get("w", -1) >= stop]
        ends[name] = None if not after else round(max(after) - stop, 3)
    late = {name: v for name, v in ends.items() if v is None or v > OBSERVER_STOP_SEC}
    detail = {"follow_end_after_stop_sec": ends, "killed": killed}
    return ("fail" if late or killed else "ok"), detail


def q5_restore(data, marks, tl, out):
    checks = []
    statuses = marks.by.get("restore_status") or []
    check(checks, "every rule removed, partition-restored written",
          ok_if(bool(statuses and statuses[-1].get("clean")) and data["partition_restored"] is not None))
    probes = marks.by.get("restore_probe") or []
    check(checks, "drone01 reaches the cluster again", ok_if(bool(probes and probes[-1].get("connected"))))
    for name, label in (("back_exec", "the cluster reaches drone01 (exec)"), ("back_node", "node Ready, lease renewing")):
        mark = marks.first(name)
        check(checks, label, "unknown" if mark is None else ok_if(mark.get("ok")), (mark or {}).get("detail"))
    check(checks, "drone01 health: first positive answer to a call after the restore",
          *health_back(data, marks.first("partition_restored_written"), tl))
    check(checks, "observers stopped within 5 s, none killed by the runner", *observers_stopped(data))
    start, horizon = tl.get("phase_start"), tl.get("horizon")
    if start and horizon:
        for name, records in (("inventory", data["inventory"]), ("nodes", data["nodes"]), ("audit", data["audit"]),
                              ("local copy", data["copy_reads"])):
            cov = j.read_coverage(records, start["mono"], horizon["mono"])
            check(checks, f"{name} covered (gap <= 3 s)", ok_if(cov["ok"]), cov["max_gap_sec"])
    isolation = out.get("isolation") or {}
    check(checks, "drone02/03 available throughout", isolation.get("availability", {}).get("status", "unknown"),
          isolation.get("availability", {}).get("violations") or isolation.get("availability", {}).get("unknown"))
    freshness = isolation.get("status_freshness", {}).get("status", "unknown")
    if freshness != "not_applicable":
        check(checks, "drone02/03 status fresh (B)", freshness, isolation.get("status_freshness", {}).get("violations"))
    px4 = out.get("px4_continuity") or {}
    check(checks, "PX4 continuity of the three drones", px4.get("status", "unknown"), px4.get("failures") or px4.get("unknown"))
    clock_problems = []
    j.clock_section(marks, clock_problems)
    check(checks, "clocks aligned before and after", ok_if(not clock_problems), clock_problems)
    peers_before, peers_after = marks.first("drop_apply_start"), marks.first("peers_end")
    check(checks, "peers unchanged", "unknown" if not (peers_before and peers_after)
          else ok_if(peers_before.get("peers") == peers_after.get("peers")))
    return step(checks)


def judge(result_dir):
    data = j.load(result_dir)
    data["cycle_fired_utc"] = None
    cycle_file = os.path.join(result_dir, q.GUARD_CYCLE_DIR, "guard-fired")
    if os.path.exists(cycle_file):
        with open(cycle_file) as h:
            text = h.read().strip()
        try:
            data["cycle_fired_utc"] = float(text)
        except ValueError:
            pass
    run = data["run"]
    variant = run.get("variant")
    marks = j.Marks(data["phases"])
    tl = j.timeline(run, marks)
    problems = []
    harness = j.harness_section({**run, "case": "short"}, data["events"], marks, problems)
    data["_harness"] = harness
    gt = j.ground_truth_section({**data, "run": {**run, "case": "short"}}, marks, tl, None, [])
    out = {"protocol_id": PROTOCOL_ID, "variant": variant, "run_id": run.get("run_id"),
           "result_dir": os.path.basename(os.path.normpath(result_dir))}
    out["isolation"] = j.isolation_section(data, variant, tl, j.log_lines(data["logs"].get(j.A_DISPATCHER)), [])
    out["px4_continuity"] = j.px4_section(data, marks, tl)
    provenance_problems = []
    out["provenance"] = j.provenance_section(data, provenance_problems, cell=False)
    steps = {}
    steps["Q1_facts"], facts = q1_facts(data, marks)
    if provenance_problems:
        steps["Q1_facts"]["checks"].append({"check": "inputs and images as declared", "status": "fail",
                                            "detail": provenance_problems})
        steps["Q1_facts"]["status"] = "fail"
    steps["Q2_guard_cycle"] = q2_guard_cycle(data, marks)
    steps["Q3_cut"], counters, reads = q3_cut(data, marks, tl)
    steps["Q3_hold"], out["cut_hold"] = q3_hold(data, marks)
    out["counter_decreases"] = {name: j.counter_decreases(stream) for name, stream in reads.items()}
    out["lease_channel_scope"] = LEASE_SCOPE
    out["not_identified"] = {"udp-sport-11811 in": (
        "packets from the server's port 11811: the discovery server does not send from it, so this raw "
        "count does not measure discovery traffic towards the node, and zero is not its absence; the "
        "server's address alone would count other traffic too (decision 5 after the first round)")}
    # the counters per peer and direction at each read (decision on point 1): cumulative,
    # they say what was dropped, not how long each flow was blocked
    out["counters"] = {m.get("at"): ((m.get("status") or {}).get("accounting") or {})
                       for m in marks.by.get("cut_counters") or []}
    out["counters"]["before_restore"] = (((marks.first("restore_start") or {}).get("status") or {})
                                         .get("accounting") or {})
    steps["Q4_local"] = q4_local(data, marks, tl, gt, harness)
    steps["Q5_restore"] = q5_restore(data, marks, tl, out)
    out["steps"] = steps
    out["counters_before_restore"] = counters
    restore = marks.first("remove_end")
    after = []
    if variant == "a":
        for line in j.log_lines(data["logs"].get(j.A_DISPATCHER)):
            if "DeploymentRequest accepted for " in line["text"] and restore and (line["m"] or 0) >= restore["mono"]:
                after.append(line["text"])
    else:
        for r in j.ok_reads(data["inventory"]):
            policy = j.policy_of(r) or {}
            if restore and r["m0"] >= restore["mono"] and policy.get("correlationId"):
                after.append(policy["correlationId"])
        after = sorted(set(after))
    out["actions_after_restore"] = {"recorded": after, "note": "kept, not used for the missed-transient rate"}
    # memory of drone01's analytics Pod (B's bridge near its limit): peaks, informative;
    # an OOM shows as a restart in Q4
    memory = {}
    observers = os.path.join(result_dir, "observers")
    for name in sorted(os.listdir(observers)) if os.path.isdir(observers) else []:
        if name.startswith("memory-") and name.endswith(".jsonl"):
            values = [r.get("workingSetBytes") for r in j.read_jsonl(os.path.join(observers, name)) or []
                      if isinstance(r.get("workingSetBytes"), int)]
            memory[name[7:-6]] = {"peak_bytes": max(values) if values else None, "reads": len(values)}
    out["memory"] = memory
    out["qualified_topology"] = {**q.topology(facts, variant),
                                 "bench": ((run.get("provenance") or {}).get("inputs_start") or {}).get("bench"),
                                 "images": (run.get("provenance") or {}).get("images")}
    interrupted = []
    if data["guard_fired"] is not None:
        interrupted.append("the real guard fired")
    end = marks.last("driver_end")
    if end is None or end.get("status") == "interrupted":
        interrupted.append("the driver did not end normally")
    failing = [f"{name}: {c['check']}" for name, s in steps.items() for c in s["checks"] if c["status"] == "fail"]
    unknown = [f"{name}: {c['check']}" for name, s in steps.items() for c in s["checks"] if c["status"] == "unknown"]
    if end is not None and end.get("status") == "aborted":
        failing.append(f"aborted: {(marks.last('aborted') or {}).get('reason')}")
    out["verdict"] = "INTERRUPTED" if interrupted else ("QUALIFIED" if not failing and not unknown else "NOT_QUALIFIED")
    out["reasons"] = {"interrupted": interrupted, "fail": failing, "unknown": unknown}
    return out


EXIT = {"QUALIFIED": 0, "NOT_QUALIFIED": 1, "INTERRUPTED": 3}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("result_dir")
    parser.add_argument("--suffix", default="")
    args = parser.parse_args(argv)
    path = os.path.join(args.result_dir, f"s2-qualify{args.suffix}.json")
    if os.path.exists(path):
        print(f"{path} exists: a re-judgement goes to a new file (--suffix=...)", file=sys.stderr)
        return 4
    result = judge(args.result_dir)
    with open(path, "w") as h:
        json.dump(result, h, indent=1, default=str)
    print(f"{result['verdict']}: {path}")
    for kind, reasons in result["reasons"].items():
        for reason in reasons:
            print(f"  {kind}: {reason}")
    return EXIT[result["verdict"]]


if __name__ == "__main__":
    sys.exit(main())
