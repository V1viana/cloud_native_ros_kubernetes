#!/usr/bin/env python3
"""Pod creations and deletions reported by the Kubernetes events in S2 and S4 (approved definition D-C,
4 October 2026, docs/EXTRACTION_DEFINITIONS_PROPOSAL.md): "reported by the events", never a certain total.

For every valid execution of the calendar (scripts/r14_schedule.py), from the test namespace's events.json
(collected by the runner after the driver ends: run_s2.sh waits for the driver, run_s4_bench.sh collects
10 s after it):
  - the window is the judge's: S4 the first t0 and horizon_end marks of phases.jsonl (s4_judge.judge),
    S2 the timeline's phase_start and horizon of s2-judge.json;
  - separate quantities, never summed, by reason and message: SuccessfulCreate and SuccessfulDelete (a
    controller, "Created/Deleted pod: NAME"); TaintManagerEviction split by its message, "Marking for
    deletion" (the taint manager deletes the Pod) and "Cancelling deletion" (a scheduled deletion cancelled
    when the taint goes: no change of Pods), any other message apart; Killing (the kubelet stops a
    container: containers, not Pods);
  - attribution by the Pod name: the test instrumentation (Deployments of the S2/S4 manifests, below)
    first, then the robot named in it, involved (S2: the harness target's robot; S4: the robots of the
    injected faults, ROBOT_OF as s4_judge's) or not; a Pod without a robot in its name is reported apart;
  - placement: firstTimestamp/lastTimestamp have a resolution of one second (the instant lies in [T, T+1));
    an event whose interval crosses an edge of the window (a single one at the edge, or an aggregated one,
    count > 1, across it) is "not attributable at an edge", its count not split;
  - the events show neither which request caused a change nor that the collection is complete (the
    recorder may drop or merge events): where nothing is in the window the row says "none reported", never 0.
The window must close before the collection (driver_end after the window's end) and the collection must come
within the events' time to live (one hour, the API server's default: the clusters do not set --event-ttl),
otherwise the execution is "not measurable" with the reason.

Usage: python3 scripts/thesis_pod_events_tables.py --campaign CAMPAIGN_DIR --out DATA_DIR
"""
import argparse
import calendar
import csv
import json
import os
import re
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import r14_schedule as rs  # noqa: E402
import thesis_audit_tables as at  # noqa: E402  (printed names of the configurations)

EVENTS = "events.json"
# the robot of each S4 fault, as scripts/s4_judge.py ROBOT_OF (not imported: the judge needs the ROS sources;
# the tests check the two are equal)
ROBOT_OF = {"battery": "drone01", "telemetry": "drone02", "edge": "drone03"}
EVENT_TTL_S = 3600.0
# (measure, event reason, message prefix, quantity, printed name): the first match wins; the order of the table
QUANTITIES = [
    ("created", "SuccessfulCreate", "", "Pod creations reported by a controller", "created"),
    ("deleted", "SuccessfulDelete", "", "Pod deletions reported by a controller", "deleted"),
    ("taint_marked", "TaintManagerEviction", "Marking for deletion", "Pods marked for deletion by the taint manager",
     "marked for deletion by the taint manager"),
    ("taint_cancelled", "TaintManagerEviction", "Cancelling deletion",
     "scheduled Pod deletions cancelled by the taint manager (no change of Pods)",
     "deletion cancelled by the taint manager"),
    ("taint_other", "TaintManagerEviction", "", "other taint manager events", "other taint manager events"),
    ("container_stops", "Killing", "", "container stops reported by the kubelet (containers, not Pods)",
     "container stops")]
MEASURES = [q[0] for q in QUANTITIES]
INVOLVED, OTHERS, NO_ROBOT, INSTRUMENTATION, UNNAMED = (
    "involved robots", "other robots", "no robot in the Pod name", "test instrumentation", "Pod not named in the event")
GROUPS = [INVOLVED, OTHERS, NO_ROBOT, INSTRUMENTATION, UNNAMED]
# test instrumentation, not the system under test: manifests/kubernetes/s2/10-s2-harness.yaml,
# 20-s2-health-prober.yaml; manifests/kubernetes/s4/25-s4-health-prober.yaml, 26-s4-fault-observer.yaml,
# 40-battery-fault-harness-drone01-imperative.yaml and 80-battery-fault-drone01.yaml (the harness only:
# s4-battery-event-detector-drone01 is variant B's detector, part of the system)
INSTRUMENTS = ("drone01-s2-harness", "s2-health-prober", "s4-health-prober", "s4-fault-observer",
               "s4-battery-fault-harness-drone01")
ROBOT = re.compile(r"(?<![a-z0-9])(drone\d\d)(?![0-9])")
POD_IN_MESSAGE = re.compile(r"^(?:Created|Deleted) pod: (\S+)$")
FIELDS = ["config", "variant", "pair", "seq", "group", "robots", "measure", "quantity", "event_reason",
          "event_message", "status", "reason",
          "reported_in_window", "not_attributable_at_an_edge", "controllers", "pods", "window_start_utc",
          "window_end_utc", "window_source", "source"]
SUM_FIELDS = ["config", "variant", "group", "measure", "quantity", "event_reason", "event_message", "valid_runs",
              "measurable_runs",
              "runs_reported", "runs_none_reported", "runs_not_measurable", "per_run_reported_min",
              "per_run_reported_max", "runs_with_not_attributable_at_an_edge", "robots", "source"]
S2 = ("s2-short", "s2-long", "s2-control", "s2-partition-only")
S4 = ("s4-l3", "s4-l1-battery", "s4-l1-telemetry", "s4-l1-edge")


def rel(path):
    """From `results/` on for the repository's results; the full path for the runner's files (frozen worktree)."""
    path = os.path.abspath(path)
    marker = "/cloud_native_ros_kubernetes/results/"
    return path[path.index(marker) + len("/cloud_native_ros_kubernetes/"):] if marker in path else path


def load_json(path):
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def read_marks(path):
    try:
        with open(path) as f:
            return [json.loads(line) for line in f if line.strip()]
    except (OSError, ValueError):
        return None


def first_mark(marks, name):
    """The first mark of that name, as s4_judge.mark."""
    return next((m for m in marks if m.get("mark") == name), None)


def utc(stamp):
    """RFC 3339 UTC (seconds, or microseconds for eventTime) to epoch seconds."""
    main, _, frac = stamp.rstrip("Z").partition(".")
    return calendar.timegm(time.strptime(main, "%Y-%m-%dT%H:%M:%S")) + (float("0." + frac) if frac else 0.0)


def interval(event):
    """[first, last] instant of the occurrences: a second-resolution stamp T stands for [T, T+1)."""
    if event.get("firstTimestamp"):
        return utc(event["firstTimestamp"]), utc(event.get("lastTimestamp") or event["firstTimestamp"]) + 1 - 1e-6
    if event.get("eventTime"):
        start = utc(event["eventTime"])
        last = (event.get("series") or {}).get("lastObservedTime")
        return start, utc(last) if last else start
    return None


def measure_of(event):
    for measure, reason, prefix, _, _ in QUANTITIES:
        if event.get("reason") == reason and (event.get("message") or "").startswith(prefix):
            return measure
    return None


def occurrences(event):
    return int(event.get("count") or (event.get("series") or {}).get("count") or 1)


def pod_of(event):
    if event.get("reason") in ("SuccessfulCreate", "SuccessfulDelete"):
        m = POD_IN_MESSAGE.match(event.get("message") or "")
        return m.group(1) if m else None
    obj = event.get("involvedObject") or {}
    return obj.get("name") if obj.get("kind") == "Pod" else None


def group_of(pod, involved):
    if pod is None:
        return UNNAMED, ""
    if any(pod.startswith(name + "-") for name in INSTRUMENTS):
        return INSTRUMENTATION, ""
    m = ROBOT.search(pod)
    if not m:
        return NO_ROBOT, ""
    return (INVOLVED if m.group(1) in involved else OTHERS), m.group(1)


def window_of(cfg, result_dir):
    """(start, end, driver_end, involved robots, window source) or a reason why the execution is not measurable."""
    marks = read_marks(os.path.join(result_dir, "phases.jsonl"))
    if marks is None:
        return "phases.jsonl missing"
    driver_end = next((m.get("utc") for m in reversed(marks) if m.get("mark") == "driver_end"), None)
    if cfg in S4:
        judge = load_json(os.path.join(result_dir, "s4-judge.json"))
        t0, horizon = first_mark(marks, "t0"), first_mark(marks, "horizon_end")
        if judge is None or not judge.get("faults"):
            return "s4-judge.json without the injected faults"
        if t0 is None or horizon is None:
            return "t0 or horizon_end mark missing"
        involved = sorted({ROBOT_OF[f] for f in judge["faults"]})
        src = f"{rel(os.path.join(result_dir, 'phases.jsonl'))}#mark=t0/utc,mark=horizon_end/utc"
        return t0["utc"], horizon["utc"], driver_end, involved, src
    judge = load_json(os.path.join(result_dir, "s2-judge.json"))
    tl = (judge or {}).get("timeline") or {}
    if not (tl.get("phase_start") or {}).get("utc") or not (tl.get("horizon") or {}).get("utc"):
        return "phase_start or horizon missing in the judge's timeline"
    target = ((judge.get("harness") or {}).get("armed") or {}).get("target_node") or ""
    m = re.match(r"^/(drone\d\d)/", target)
    if not m:
        return "harness target robot not recorded"
    src = f"{rel(os.path.join(result_dir, 's2-judge.json'))}#/timeline/phase_start/utc,/timeline/horizon/utc"
    return tl["phase_start"]["utc"], tl["horizon"]["utc"], driver_end, [m.group(1)], src


def measurable_window(cfg, result_dir):
    """The execution's window and events: {lo, hi, involved, window_source, items, events_path}, or, when it is
    not measurable, {reason, source} (with the window's keys once the window is known)."""
    w = window_of(cfg, result_dir)
    if isinstance(w, str):
        return {"reason": w, "source": rel(result_dir)}
    lo, hi, driver_end, involved, window_source = w
    out = {"lo": lo, "hi": hi, "involved": involved, "window_source": window_source}
    if driver_end is None or driver_end < hi:
        return {**out, "reason": "the collection is not shown to follow the window's end (driver_end)",
                "source": window_source.split("#")[0] + "#mark=driver_end"}
    if driver_end - lo >= EVENT_TTL_S:
        return {**out, "reason": "the window starts more than the events' time to live before the collection",
                "source": window_source.split("#")[0] + "#mark=driver_end"}
    events_path = os.path.join(result_dir, EVENTS)
    doc = load_json(events_path)
    if doc is None or not isinstance(doc.get("items"), list):
        return {**out, "reason": "events.json missing or unreadable", "source": rel(events_path)}
    return {**out, "items": doc["items"], "events_path": events_path}


def execution_rows(cfg, v, pair, seq, result_dir):
    base = {k: "" for k in FIELDS}
    base.update(config=cfg, variant=v, pair=pair, seq=seq)
    w = measurable_window(cfg, result_dir)
    if "lo" in w:
        base.update(window_start_utc=w["lo"], window_end_utc=w["hi"], window_source=w["window_source"])
    if "reason" in w:
        return [{**base, "group": g, "measure": m, "quantity": q, "event_reason": er, "event_message": prefix,
                 "status": "not measurable", "reason": w["reason"], "source": w["source"]}
                for g in GROUPS for m, er, prefix, q, _ in QUANTITIES]
    lo, hi, involved, events_path = w["lo"], w["hi"], w["involved"], w["events_path"]
    cells = {(g, m): {"in": 0, "edge": 0, "uids": [], "pods": set(), "kinds": set(), "robots": set()}
             for g in GROUPS for m in MEASURES}
    for e in w["items"]:
        measure = measure_of(e)
        if measure is None:
            continue
        span = interval(e)
        if span is None or span[1] < lo or span[0] > hi:
            continue                                   # outside the window (or no instant at all: not placed)
        g, robot = group_of(pod_of(e), involved)
        c = cells[(g, measure)]
        if lo <= span[0] and span[1] <= hi:
            c["in"] += occurrences(e)
            c["pods"].add(pod_of(e) or "")
            c["kinds"].add((e.get("involvedObject") or {}).get("kind") or "")
            if robot:
                c["robots"].add(robot)
        else:
            c["edge"] += 1                             # crosses an edge: the event is not split
        c["uids"].append((e.get("metadata") or {}).get("uid") or "")
    rows = []
    for g in GROUPS:
        for m, er, prefix, q, _ in QUANTITIES:
            c = cells[(g, m)]
            status = ("reported" if c["in"] else
                      "only events not attributable at an edge" if c["edge"] else "none reported")
            robots = involved if g == INVOLVED else sorted(c["robots"])
            source = (f"{rel(events_path)}#items[metadata.uid in {','.join(c['uids'])}]" if c["uids"]
                      else f"{rel(events_path)}#items[reason={er}{', message^=' + prefix if prefix else ''}] "
                           "(none in the window)")
            rows.append({**base, "group": g, "robots": " ".join(robots), "measure": m, "quantity": q,
                         "event_reason": er, "event_message": prefix, "status": status, "reported_in_window": c["in"] or "",
                         "not_attributable_at_an_edge": c["edge"] or "",
                         "controllers": " ".join(sorted(k for k in c["kinds"] if k)),
                         "pods": " ".join(sorted(p for p in c["pods"] if p)), "source": source})
    return rows


def rows_of(campaign):
    rows = []
    for e in rs.build():
        if e["config"] not in S2 + S4:
            continue
        run_dir = os.path.join(campaign, "runs", f"{e['seq']:03d}-{e['config']}-{e['variant']}")
        rec = load_json(os.path.join(run_dir, "run.json"))
        if rec is None or rec.get("valid") is not True:
            continue                                   # denominators: valid executions only
        rows += execution_rows(e["config"], e["variant"], e["pair"], e["seq"], rec.get("result_dir") or "")
    order = list(S2 + S4)
    rows.sort(key=lambda r: (order.index(r["config"]), r["variant"], int(r["seq"]), GROUPS.index(r["group"]),
                             MEASURES.index(r["measure"])))
    return rows


def summary_rows(rows):
    out, keys = [], []
    for r in rows:
        k = (r["config"], r["variant"], r["group"], r["measure"])
        if k not in keys:
            keys.append(k)
    for cfg, v, g, m in keys:
        rs_ = [r for r in rows if (r["config"], r["variant"], r["group"], r["measure"]) == (cfg, v, g, m)]
        measurable = [r for r in rs_ if r["status"] != "not measurable"]
        counts = [int(r["reported_in_window"]) for r in measurable if r["reported_in_window"]]
        out.append({"config": cfg, "variant": v, "group": g, "measure": m, "quantity": rs_[0]["quantity"],
                    "event_reason": rs_[0]["event_reason"], "event_message": rs_[0]["event_message"],
                    "valid_runs": len(rs_), "measurable_runs": len(measurable), "runs_reported": len(counts),
                    "runs_none_reported": sum(not r["reported_in_window"] for r in measurable),
                    "runs_not_measurable": len(rs_) - len(measurable),
                    "per_run_reported_min": min(counts) if counts else "",
                    "per_run_reported_max": max(counts) if counts else "",
                    "runs_with_not_attributable_at_an_edge": sum(bool(r["not_attributable_at_an_edge"]) for r in rs_),
                    "robots": " ".join(sorted({x for r in rs_ for x in r["robots"].split()})),
                    "source": f"data/measured/pod_events_runs.csv: config={cfg}, variant={v}, group={g}, "
                              f"measure={m}"})
    return out


# printed header lines of each quantity (every line fits its column at \footnotesize in the thesis' class) and
# printed names of the groups
HEADER = {"created": ["Created"], "deleted": ["Deleted"],
          "taint_marked": ["Marked for", "deletion by", "the taint", "manager"],
          "taint_cancelled": ["Deletion", "cancelled", "by the taint", "manager"],
          "taint_other": ["Other taint", "manager", "events"], "container_stops": ["Container", "stops"]}
GROUP_TEXT = {INVOLVED: "involved", OTHERS: "other robots", NO_ROBOT: "no robot in the Pod name",
              INSTRUMENTATION: "test instrumentation", UNNAMED: "Pod not named in the event"}
TEXT_WIDTH_PT, TABCOLSEP_PT, CM_PT = 412.0, 3.0, 28.4528      # the thesis' text width is 412.56 pt
FIRST_CM, GROUP_CM, MAX_CM = 2.55, 2.25, 3.0


def tex(summary, configs):
    """Rows: configuration and variant x group, for the groups with an event in this table; columns: the
    quantities with an event in this table. The groups and quantities without any event are named in the last
    line: no event reported, which is not the same as no operation."""
    mine = [r for r in summary if r["config"] in configs]
    has = lambda r: int(r["runs_reported"]) or int(r["runs_with_not_attributable_at_an_edge"])
    groups = [g for g in GROUPS if any(has(r) for r in mine if r["group"] == g)] or [INVOLVED]
    measures = [m for m in MEASURES if any(has(r) for r in mine if r["measure"] == m)] or ["created"]
    ncols = 2 + len(measures)
    each_cm = min(MAX_CM, int(((TEXT_WIDTH_PT - 2 * TABCOLSEP_PT * (ncols - 1)) / CM_PT - FIRST_CM - GROUP_CM)
                              / len(measures) * 20) / 20)
    widths = [FIRST_CM, GROUP_CM] + [each_cm] * len(measures)

    def cell(r):
        k, m, n = int(r["runs_reported"]), int(r["measurable_runs"]), int(r["valid_runs"])
        lo, hi = r["per_run_reported_min"], r["per_run_reported_max"]
        parts = [f"{lo if lo == hi else f'{lo}--{hi}'} in {k}~of~{m}" if k else "none reported"]
        if int(r["runs_with_not_attributable_at_an_edge"]):
            parts.append(f"not attributable at an edge in {r['runs_with_not_attributable_at_an_edge']}")
        if m < n:
            parts.append(f"not measurable in {n - m}~of~{n}")
        return "; ".join(parts)

    lines = []
    for cfg in configs:
        for v in ("a", "b"):
            rows = [r for r in mine if r["config"] == cfg and r["variant"] == v]
            if not rows:
                continue
            for i, g in enumerate(groups):
                by = {r["measure"]: r for r in rows if r["group"] == g}
                label = f"{at.LABEL[cfg]}, {v.upper()} ({rows[0]['valid_runs']})" if i == 0 else ""
                who = GROUP_TEXT[g] + (f" ({by[measures[0]]['robots'].replace(' ', ', ')})"
                                       if g == INVOLVED and by[measures[0]]["robots"] else "")
                lines.append(f"{label} & {who} & " + " & ".join(cell(by[m]) for m in measures) + " \\\\")
    quiet_groups = [GROUP_TEXT[g] for g in GROUPS if g not in groups]
    quiet_measures = [q for m, _, _, q, _ in QUANTITIES if m not in measures]
    quiet = ([f"{', '.join(quiet_groups)} (any quantity)"] if quiet_groups else []) + \
            ([f"{', '.join(quiet_measures)} (any group)"] if quiet_measures else [])
    if quiet:
        lines.append("\\midrule")
        lines.append(f"\\multicolumn{{{ncols}}}{{@{{}}>{{\\raggedright\\arraybackslash}}p{{\\dimexpr "
                     f"{sum(widths):.2f}cm+{2 * (ncols - 1)}\\tabcolsep\\relax}}@{{}}}}"
                     "{No event reported in any execution, which does not mean that no operation happened: "
                     + "; ".join(quiet) + ".} \\\\")
    col = lambda w: f">{{\\raggedright\\arraybackslash}}p{{{w:.2f}cm}}"
    head = lambda xs: (f"\\textbf{{{xs[0]}}}" if len(xs) == 1 else
                       "\\shortstack[l]{" + "\\\\".join(f"\\textbf{{{x}}}" for x in xs) + "}")
    return ("% GENERATED by scripts/thesis_pod_events_tables.py from data/measured/pod_events.csv\n"
            "\\begin{tabular}{@{}" + "".join(col(w) for w in widths) + "@{}}\n\\toprule\n"
            "\\textbf{Configuration} & \\textbf{Robots} & " + " & ".join(head(HEADER[m]) for m in measures)
            + " \\\\\n\\midrule\n" + "\n".join(lines) + "\n\\bottomrule\n\\end{tabular}%\n")    # no space token after the table


def write_csv(path, fields, rows):
    with open(path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields, lineterminator="\n")
        w.writeheader()
        w.writerows(rows)


def read_csv(path):
    with open(path, newline="") as f:
        return list(csv.DictReader(f))


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--campaign", required=True)
    ap.add_argument("--out", required=True)
    a = ap.parse_args(argv)
    measured, tables = os.path.join(a.out, "measured"), os.path.join(a.out, "tables")
    os.makedirs(measured, exist_ok=True)
    os.makedirs(tables, exist_ok=True)
    runs_csv, sum_csv = os.path.join(measured, "pod_events_runs.csv"), os.path.join(measured, "pod_events.csv")
    write_csv(runs_csv, FIELDS, rows_of(a.campaign))
    write_csv(sum_csv, SUM_FIELDS, summary_rows(read_csv(runs_csv)))
    summary = read_csv(sum_csv)
    for name, configs in (("pod_events_partition.tex", S2), ("pod_events_faults.tex", S4)):
        with open(os.path.join(tables, name), "w") as f:
            f.write(tex(summary, configs))
    print("pod_events")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
