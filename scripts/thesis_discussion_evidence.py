#!/usr/bin/env python3
"""Evidence behind the discussion of chapter 6 (decision of Viviana, 5 October 2026): the records that support the
mechanisms named in the discussion, extracted from what the campaign and the S1 block already stored, with no new
run. Every row has its source (file and field), its window and denominator, the resolution of its instants, and
says whether a value is a direct reading (judge, log line, counter, health answer) or reported by the Kubernetes
Events. Missing data are rows with a status and a reason, never zero.

  discussion_s1_repair.csv           S1 block: Deployment recreated, Pod ready, service available (s1-judge.json)
  discussion_s2_windows.csv          S2 short and long: windows of the episode seen in the ROSModule status (B)
  discussion_s3_requests_runs.csv    S3: API requests per resource, verb, subresource over the fixed window
  discussion_s3_requests.csv         the same per fleet size: medians per variant and median paired difference
  discussion_s4_edge.csv             S4 with the edge fault: health answers of the edge instance after node_back
  discussion_s4_agent.csv            S4 with the telemetry fault: control record, rollout and new Pod of the agent
  discussion_agent_deployment.csv    every valid execution with kubernetes-resources.yaml: the agent's Deployment

Usage: python3 scripts/thesis_discussion_evidence.py --campaign CAMPAIGN_DIR --s1-block S1_BLOCK_DIR --out DATA_DIR
"""
import argparse
import collections
import csv
import json
import os
import re
import statistics
import sys

import yaml

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import r14_schedule as rs  # noqa: E402
import thesis_pod_events_tables as pe  # noqa: E402

rel, load_json, read_marks, first_mark, interval = pe.rel, pe.load_json, pe.read_marks, pe.first_mark, pe.interval

S2_CASES = ("s2-short", "s2-long")
S4_EDGE = ("s4-l3", "s4-l1-edge")
S4_TELEMETRY = ("s4-l3", "s4-l1-telemetry")
S3 = {"s3-n3": 3, "s3-n10": 10}
VIOLATING_MS = 250.0          # the threshold scripts/s2_judge.py uses for a violating window (p95Ms > 250)
AGENT = "drone02-microxrce-agent"
EDGE_TARGET = "drone03-edge"
B_RESTART = re.compile(r"^(\S+) .*RestartComponent: triggered rollout of Deployment " + AGENT + r"\b")
A_EVENT = re.compile(r"drone02-TelemetryHeartbeatLost-(\d{16})-\d+")


def fmt(x, nd=1):
    return "" if x is None else f"{x:.{nd}f}"


def valid_runs(campaign, configs):
    """(entry, run record) of the valid executions of the calendar, in calendar order."""
    for e in rs.build():
        if e["config"] not in configs:
            continue
        run_dir = os.path.join(campaign, "runs", f"{e['seq']:03d}-{e['config']}-{e['variant']}")
        rec = load_json(os.path.join(run_dir, "run.json"))
        if rec is not None and rec.get("valid") is True:     # denominators: valid executions only
            yield e, rec, run_dir


def base(e, fields):
    row = {k: "" for k in fields}
    row.update(config=e["config"], variant=e["variant"], pair=e["pair"], seq=e["seq"])
    return row


# --- S1: the repair as read by the judge -------------------------------------------------------------------------

S1_FIELDS = ["block", "config", "variant", "pair", "seq", "status", "reason", "deployment_recreated_s",
             "pod_ready_s", "service_available_s", "max_read_gap_s", "kind", "resolution", "source"]


def s1_rows(s1_block):
    rows = []
    for e in rs.build_s1_block():
        run_dir = os.path.join(s1_block, "runs", f"{e['seq']:03d}-{e['config']}-{e['variant']}")
        rec = load_json(os.path.join(run_dir, "run.json"))
        if rec is None or rec.get("valid") is not True:
            continue
        row = {**base(e, S1_FIELDS), "block": "s1-block", "kind": "direct reading (judge, inventory reads)"}
        path = os.path.join(rec.get("result_dir") or "", "s1-judge.json")
        j = load_json(path)
        if j is None:
            rows.append({**row, "status": "not recorded", "reason": "s1-judge.json missing", "source": rel(path)})
            continue
        recovered = j.get("recovery") == "recovered"
        rows.append({**row, "status": "recovered" if recovered else "not recovered within the window",
                     "reason": "" if recovered else str(j.get("recovery")),
                     "deployment_recreated_s": fmt(j.get("object_repaired_s")), "pod_ready_s": fmt(j.get("pod_ready_s")),
                     "service_available_s": fmt(j.get("service_available_s")),
                     "max_read_gap_s": fmt(j.get("max_inventory_gap_s")),
                     "resolution": "0.1 s as recorded; an instant is the first inventory read that shows the state",
                     "source": rel(path) + "#/object_repaired_s,/pod_ready_s,/service_available_s,/recovery,"
                                           "/max_inventory_gap_s"})
    return rows


# --- S2: windows of the episode seen in the status --------------------------------------------------------------

S2_FIELDS = ["config", "variant", "pair", "seq", "status", "reason", "episode_windows_seen", "violating_seen",
             "seen_after_restore", "local_violating_windows", "sequences_never_seen", "max_read_gap_s", "recognized",
             "kind", "limits", "source"]
S2_LIMITS = ("distinct windows (instance, sequence) seen at least once across all inventory reads, cumulative, not "
             "windows present at the same time in the status; the reads are about one second apart, so a sequence "
             "absent from the reads does not prove that the API server never held it")


def s2_rows(campaign):
    rows = []
    for e, rec, _ in valid_runs(campaign, S2_CASES):
        row = base(e, S2_FIELDS)
        path = os.path.join(rec.get("result_dir") or "", "s2-judge.json")
        j = load_json(path)
        if e["variant"] == "a":
            rows.append({**row, "status": "not applicable",
                         "reason": "variant A publishes no metric windows in a status: its latency rule is evaluated "
                                   "by the Event Detector on the robot's node",
                         "recognized": "" if j is None else str(j["recognized"]["value"]).lower(),
                         "source": rel(path) + "#/recognized/value"})
            continue
        if j is None:
            rows.append({**row, "status": "not recorded", "reason": "s2-judge.json missing", "source": rel(path)})
            continue
        r = j["received"]
        restore = ((j["timeline"].get("restore") or {}).get("command_end") or {}).get("mono")
        ep = r.get("episode_windows") or []
        rows.append({**row, "status": "recorded", "kind": "direct reading (inventory reads of the ROSModule status)",
                     "episode_windows_seen": len(ep),
                     "violating_seen": sum(1 for w in ep if (w.get("p95Ms") or 0) > VIOLATING_MS),
                     "seen_after_restore": ("" if restore is None else
                                            sum(1 for w in ep if w["first_seen"]["first_with"]["mono"] >= restore)),
                     "local_violating_windows": r.get("local_violating_windows"),
                     "sequences_never_seen": sum(len(v) for v in (r.get("seq_not_observed") or {}).values()),
                     "max_read_gap_s": fmt((r.get("coverage") or {}).get("max_gap_sec"), 3),
                     "recognized": str(j["recognized"]["value"]).lower(), "limits": S2_LIMITS,
                     "source": rel(path) + "#/received/episode_windows,/received/seq_not_observed,"
                                           "/received/local_violating_windows,/received/coverage,"
                                           "/timeline/restore/command_end,/recognized/value"})
    return rows


# --- S3: API requests per resource over the fixed window ---------------------------------------------------------

S3_RUN_FIELDS = ["fleet_size", "variant", "pair", "seq", "status", "reason", "resource", "verb", "subresource",
                 "requests", "window_s", "rate_per_s", "source"]
S3_FIELDS = ["fleet_size", "resource", "verb", "subresource", "runs_a", "runs_b", "median_rate_a", "median_rate_b",
             "pairs", "median_paired_difference_b_minus_a", "kind", "window", "source"]
S3_WINDOW = ("fixed window of the load levels: from the runner's snapshot before the injection "
             "(metrics-before-incident.json) to the sampler's end read (edges.json api_end)")


def s3_run(rec, run_dir):
    """{(resource, verb, subresource): requests}, window_s, sources; or a reason."""
    before_path = os.path.join(rec.get("result_dir") or "", "metrics-before-incident.json")
    edges_path = os.path.join(run_dir, "edges.json")
    before, edges = load_json(before_path), load_json(edges_path)
    if before is None or edges is None:
        return None, None, "metrics-before-incident.json or edges.json missing", rel(before_path)
    start = (before.get("counters") or {}).get("apiserver_request_total")
    end = (edges.get("api_end") or {}).get("L0")
    t0, t1 = before.get("timestamp_utc"), ((edges.get("api_end") or {}).get("scrape") or {}).get("start")
    if not isinstance(start, dict) or not isinstance(end, dict) or t0 is None or t1 is None:
        return None, None, "a counter read or its instant is missing", rel(before_path)
    groups = collections.Counter()
    for key in set(start) | set(end):
        delta = end.get(key, 0.0) - start.get(key, 0.0)
        if delta < 0:
            return None, None, "a counter series went down in the window (reset)", rel(edges_path)
        lab = json.loads(key)
        groups[(lab.get("resource", ""), lab.get("verb", ""), lab.get("subresource", ""))] += delta
    src = (rel(before_path) + "#/counters/apiserver_request_total,/timestamp_utc; "
           + rel(edges_path) + "#/api_end/L0,/api_end/scrape/start")
    return groups, t1 - t0, None, src


def s3_rows(campaign):
    runs = []
    per = collections.defaultdict(dict)        # (size, variant, pair) -> {group: rate}
    for e, rec, run_dir in valid_runs(campaign, tuple(S3)):
        size = S3[e["config"]]
        groups, window, reason, src = s3_run(rec, run_dir)
        head = {k: "" for k in S3_RUN_FIELDS}
        head.update(fleet_size=size, variant=e["variant"], pair=e["pair"], seq=e["seq"])
        if reason:
            runs.append({**head, "status": "not measurable", "reason": reason, "source": src})
            continue
        for (res, verb, sub), n in sorted(groups.items()):
            if n <= 0:
                continue                         # a series with no request in the window: the counter did not move
            runs.append({**head, "status": "measured", "resource": res or "(none)", "verb": verb,
                         "subresource": sub, "requests": f"{n:.0f}", "window_s": fmt(window),
                         "rate_per_s": fmt(n / window, 4), "source": src})
        per[(size, e["variant"], e["pair"])] = {g: n / window for g, n in groups.items()}
    summary = []
    for size in sorted(set(S3.values())):
        groups = sorted({g for (s, _, _), rates in per.items() if s == size for g in rates})
        pairs = sorted({p for (s, v, p) in per if s == size and (s, "a", p) in per and (s, "b", p) in per})
        for g in groups:
            ra = [rates.get(g, 0.0) for (s, v, _), rates in per.items() if s == size and v == "a"]
            rb = [rates.get(g, 0.0) for (s, v, _), rates in per.items() if s == size and v == "b"]
            if max(ra + rb) <= 0:
                continue
            diffs = [per[(size, "b", p)].get(g, 0.0) - per[(size, "a", p)].get(g, 0.0) for p in pairs]
            summary.append({"fleet_size": size, "resource": g[0] or "(none)", "verb": g[1], "subresource": g[2],
                            "runs_a": len(ra), "runs_b": len(rb), "median_rate_a": fmt(statistics.median(ra), 4),
                            "median_rate_b": fmt(statistics.median(rb), 4), "pairs": len(pairs),
                            "median_paired_difference_b_minus_a": fmt(statistics.median(diffs), 4) if diffs else "",
                            "kind": "direct reading (API server counters; they do not identify the client)",
                            "window": S3_WINDOW,
                            "source": f"data/measured/discussion_s3_requests_runs.csv: fleet_size={size}, "
                                      f"resource={g[0] or '(none)'}, verb={g[1]}, subresource={g[2]}"})
        summary.sort(key=lambda r: (r["fleet_size"], -float(r["median_rate_b"]), r["resource"], r["verb"]))
    return runs, summary


# --- S4: edge instance after the node restart -------------------------------------------------------------------

S4E_FIELDS = ["config", "variant", "pair", "seq", "status", "reason", "node_back_s", "horizon_s",
              "answers_after_node_back", "positive_after_node_back", "first_positive_after_node_back_s",
              "last_answer_s", "last_result", "last_lifecycle_state", "kind", "resolution", "source"]


def s4_edge_rows(campaign):
    rows = []
    for e, rec, _ in valid_runs(campaign, S4_EDGE):
        row = {**base(e, S4E_FIELDS), "kind": "direct reading (health answers of the prober on the edge node)"}
        rd = rec.get("result_dir") or ""
        marks = read_marks(os.path.join(rd, "phases.jsonl"))
        t0, back, hor = (first_mark(marks or [], m) for m in ("t0", "node_back", "horizon_end"))
        path = os.path.join(rd, "prober-health.jsonl")
        answers = read_marks(path)
        if t0 is None or back is None or hor is None or answers is None:
            rows.append({**row, "status": "not recorded", "reason": "t0, node_back, horizon_end or prober-health.jsonl "
                                                                     "missing", "source": rel(path)})
            continue
        edge = [a for a in answers if a.get("event") == "health" and a.get("target") == EDGE_TARGET
                and a.get("outcome_utc") is not None and t0["utc"] <= a["outcome_utc"] <= hor["utc"]]
        after = [a for a in edge if a["outcome_utc"] >= back["utc"]]
        positive = [a for a in after if a.get("result") == "positive"]
        last = edge[-1] if edge else None
        rows.append({**row, "status": "recorded" if last else "no answer in the window",
                     "node_back_s": fmt(back["utc"] - t0["utc"]), "horizon_s": fmt(hor["utc"] - t0["utc"]),
                     "answers_after_node_back": len(after), "positive_after_node_back": len(positive),
                     "first_positive_after_node_back_s": fmt(positive[0]["outcome_utc"] - t0["utc"]) if positive else "",
                     "last_answer_s": fmt(last["outcome_utc"] - t0["utc"]) if last else "",
                     "last_result": last.get("result", "") if last else "",
                     "last_lifecycle_state": ((last or {}).get("answer") or {}).get("lifecycle_state", ""),
                     "resolution": "instants in seconds after t0 (phases.jsonl), from the answers' outcome_utc",
                     "source": rel(path) + f"#target={EDGE_TARGET}/outcome_utc,result,answer.lifecycle_state; "
                               + rel(os.path.join(rd, "phases.jsonl")) + "#mark=t0,node_back,horizon_end"})
    return rows


# --- S4: restart of the telemetry agent --------------------------------------------------------------------------

S4A_FIELDS = ["config", "variant", "pair", "seq", "status", "reason", "strategy", "host_network", "grace_s",
              "control_record", "control_record_s", "control_record_kind", "rollout_events", "new_pod",
              "new_pod_created_s", "event_resolution", "source"]


def agent_deployments(result_dir):
    """The agent Deployments in kubernetes-resources.yaml (collected at the end of the run), or None."""
    path = os.path.join(result_dir, "kubernetes-resources.yaml")
    try:
        with open(path) as f:
            docs = [d for d in yaml.safe_load_all(f) if d]
    except (OSError, yaml.YAMLError):
        return None, path
    items = []
    for d in docs:
        items += d.get("items", [d]) if isinstance(d, dict) else []
    return [i for i in items if i.get("kind") == "Deployment"
            and (i.get("metadata") or {}).get("name", "").endswith("-microxrce-agent")], path


def settings(dep):
    spec = dep.get("spec") or {}
    strategy = spec.get("strategy") or {}
    rolling = strategy.get("rollingUpdate") or {}
    pod = (spec.get("template") or {}).get("spec") or {}
    text = strategy.get("type", "")
    if rolling:
        text += f" (maxSurge {rolling.get('maxSurge')}, maxUnavailable {rolling.get('maxUnavailable')})"
    return text, str(bool(pod.get("hostNetwork"))).lower(), str(pod.get("terminationGracePeriodSeconds", ""))


def control_record(variant, logs):
    """B: the Fleet Operator's log line requesting the rollout (millisecond stamp); A: the detection instant in the
    identifier of the TelemetryHeartbeatLost event (Application Manager log; the restart request is not logged)."""
    if variant == "b":
        path = os.path.join(logs, "fleet-operator.log")
        for line in open(path) if os.path.isfile(path) else []:
            m = B_RESTART.match(line)
            if m:
                return ("rollout requested by the adaptation controller", pe.utc(m.group(1)[:26] + "Z"),
                        "direct reading (log line, ms)", rel(path) + "#'RestartComponent: triggered rollout'")
        return None, None, "", rel(path)
    path = os.path.join(logs, "application-manager-s4.log")
    for line in open(path) if os.path.isfile(path) else []:
        m = A_EVENT.search(line)
        if m:
            return ("loss detected (instant in the event identifier); the restart request is not logged",
                    int(m.group(1)) / 1e6, "direct reading (event identifier, us)",
                    rel(path) + "#drone02-TelemetryHeartbeatLost-<us>")
    return None, None, "", rel(path)


def s4_agent_rows(campaign):
    rows = []
    for e, rec, _ in valid_runs(campaign, S4_TELEMETRY):
        row = base(e, S4A_FIELDS)
        rd = rec.get("result_dir") or ""
        marks = read_marks(os.path.join(rd, "phases.jsonl"))
        t0 = first_mark(marks or [], "t0")
        events_path = os.path.join(rd, "events.json")
        doc = load_json(events_path)
        deps, dep_path = agent_deployments(rd)
        if t0 is None or doc is None:
            rows.append({**row, "status": "not recorded", "reason": "t0 or events.json missing",
                         "source": rel(events_path)})
            continue
        agent = [d for d in deps or [] if d["metadata"]["name"] == AGENT]
        strategy, host, grace = settings(agent[0]) if agent else ("not recorded", "", "")
        label, at, kind, src = control_record(e["variant"], os.path.join(rd, "logs"))
        lo = t0["utc"]
        rollout, created = [], []
        for ev in doc.get("items") or []:
            span = interval(ev)
            if span is None or span[1] < lo:
                continue
            obj = ev.get("involvedObject") or {}
            if ev.get("reason") == "ScalingReplicaSet" and obj.get("name") == AGENT:
                rollout.append((span, ev.get("message")))
            if ev.get("reason") == "SuccessfulCreate" and (pe.pod_of(ev) or "").startswith(AGENT + "-"):
                created.append((span, pe.pod_of(ev)))
        rollout.sort()
        created.sort()
        rows.append({**row, "status": "recorded", "strategy": strategy, "host_network": host, "grace_s": grace,
                     "control_record": label or "not recorded",
                     "control_record_s": fmt(at - lo, 3) if at is not None else "", "control_record_kind": kind,
                     "rollout_events": "; ".join(f"{m} ({s[0] - lo:.1f} to {s[1] - lo:.1f} s)" for s, m in rollout)
                                       or "none reported",
                     "new_pod": created[0][1] if created else "none reported",
                     "new_pod_created_s": (f"{created[0][0][0] - lo:.1f} to {created[0][0][1] - lo:.1f}"
                                           if created else ""),
                     "event_resolution": "reported by the Kubernetes Events; a second-resolution stamp T stands for "
                                         "[T, T+1); seconds after t0 (phases.jsonl)",
                     "source": f"{rel(events_path)}#reason=ScalingReplicaSet,SuccessfulCreate ({AGENT}); {src}; "
                               f"{rel(dep_path)}#Deployment/{AGENT}/spec.strategy,template.spec.hostNetwork,"
                               "terminationGracePeriodSeconds; "
                               + rel(os.path.join(rd, "phases.jsonl")) + "#mark=t0"})
    return rows


# --- the agent's Deployment in every valid execution -------------------------------------------------------------

AD_FIELDS = ["config", "variant", "runs", "strategy", "host_network", "grace_s", "status", "example_source"]


def agent_deployment_rows(campaign):
    agg = collections.OrderedDict()
    for e, rec, _ in valid_runs(campaign, tuple(c[1] for c in rs.CONFIGS)):
        deps, path = agent_deployments(rec.get("result_dir") or "")
        if not deps:
            key = (e["config"], e["variant"], "", "", "", "not recorded: no kubernetes-resources.yaml with the agent")
        else:
            kinds = {settings(d) for d in deps}
            if len(kinds) == 1:
                (strategy, host, grace), = kinds
                key = (e["config"], e["variant"], strategy, host, grace, "recorded")
            else:
                key = (e["config"], e["variant"], "mixed", "", "", "the agents of one execution differ")
        if key not in agg:
            agg[key] = [0, rel(path) + "#Deployment/*-microxrce-agent/spec.strategy,template.spec.hostNetwork,"
                                       "terminationGracePeriodSeconds"]
        agg[key][0] += 1
    return [{"config": k[0], "variant": k[1], "runs": n, "strategy": k[2], "host_network": k[3], "grace_s": k[4],
             "status": k[5], "example_source": src} for k, (n, src) in agg.items()]


def write(path, fields, rows):
    with open(path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields, lineterminator="\n")
        w.writeheader()
        w.writerows(rows)


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--campaign", required=True)
    ap.add_argument("--s1-block", required=True)
    ap.add_argument("--out", required=True)
    a = ap.parse_args(argv)
    m = os.path.join(a.out, "measured")
    os.makedirs(m, exist_ok=True)
    write(os.path.join(m, "discussion_s1_repair.csv"), S1_FIELDS, s1_rows(a.s1_block))
    write(os.path.join(m, "discussion_s2_windows.csv"), S2_FIELDS, s2_rows(a.campaign))
    runs, summary = s3_rows(a.campaign)
    write(os.path.join(m, "discussion_s3_requests_runs.csv"), S3_RUN_FIELDS, runs)
    write(os.path.join(m, "discussion_s3_requests.csv"), S3_FIELDS, summary)
    write(os.path.join(m, "discussion_s4_edge.csv"), S4E_FIELDS, s4_edge_rows(a.campaign))
    write(os.path.join(m, "discussion_s4_agent.csv"), S4A_FIELDS, s4_agent_rows(a.campaign))
    write(os.path.join(m, "discussion_agent_deployment.csv"), AD_FIELDS, agent_deployment_rows(a.campaign))
    # no table: these rows back the sentences of the discussion and of the declared asymmetry
    print("discussion_evidence")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
