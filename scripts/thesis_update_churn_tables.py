#!/usr/bin/env python3
"""Reconciliation churn of the updates and churn of the repeated identical requests (U1, U2), with the runner's
method (approved definitions D-B and D-C, 4 October 2026, docs/EXTRACTION_DEFINITIONS_PROPOSAL.md).

The runner (scripts/run_u1.sh, count_churn) summarises every event of the namespace (uid|reason|kind|name)
before and after the update, and before and after each of the two repeated identical requests; it counts, among
the new events, the distinct Pods of the updated workload with a container created (Created) plus those with a
container stopped (Killing). This tool recomputes that count from the summaries and checks it against the
runner's REPORT.md. The events do not prove a complete collection: a count of 0 is written "none reported"
(decision of 4 October), never 0. Where the summaries do not exist (U2: kubernetes-events.txt is a final
snapshot with relative times, no summary before and after the update) the execution is "not measurable",
with the reason; U2 does not repeat the request, so its repetitions are "not applicable".

Target Pod across the repetitions (U1; decision of 4 October): the runner keeps two readings of the target's
Pods, pods-after.tsv after the update (name, UID, restart count of the first container) and pods.txt after both
repetitions (kubectl get pods: name and the RESTARTS of all containers, no UID); none after repetition 1.
The row says what these readings show and when, and only that: the same Pod name and no restart recorded, or
a changed UID (only where both readings carry one: never in these files), a changed Pod name, a positive
restart count, or "not recorded". It is evidence limited to the executions run, not a general guarantee, and
not a statement about each repetition. Apart, for B only (decision of 4 October): the UID in the ROSModule's
own status (kubernetes-resources.yaml, status.lifecycleInstances[...].podUID, captured after repetition 2)
compared with the UID after the update, and its lastObservedTime placed against repetition 2 by the file
modification times of events-before-repeat-2.txt and events-after-repeat-2.txt. It is state written by the
variant under test, supplementary and distinct from the common check, and it does not prove that the current
Pod still had that UID after the completion (the status may be stale); for A it is "not applicable".

Usage: python3 scripts/thesis_update_churn_tables.py --campaign CAMPAIGN_DIR --out DATA_DIR
"""
import argparse
import calendar
import collections
import csv
import json
import os
import re
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import r14_schedule as rs  # noqa: E402
import yaml  # noqa: E402

# the updated workload's Pod-name prefix, as the runner passes it to count_churn (run_u1.sh, TARGET_DEPLOYMENT for
# A and ROSMODULE for B; run_u2.sh updates the same workloads)
PREFIX = {"a": "drone01-companion-analytics-onboard-", "b": "companion-analytics-drone01-"}
REPEATS = ("u1",)                     # the configurations whose protocol repeats the identical request twice
# (quantity, before summary, after summary, REPORT.md pattern)
QUANTITIES = [("reconciliation churn of the update", "events-before-update.txt", "events-after-update.txt",
               r"Reconciliation churn[^|\n]*\|\s*(\d+)"),
              ("extra churn of repeated request 1", "events-before-repeat-1.txt", "events-after-repeat-1.txt",
               r"ripetizione 1: (\d+);"),
              ("extra churn of repeated request 2", "events-before-repeat-2.txt", "events-after-repeat-2.txt",
               r"ripetizione 2: (\d+);")]
FIELDS = ["config", "variant", "pair", "seq", "quantity", "status", "reason", "reported", "runner_report",
          "pods_created", "pods_stopped", "reading_after_update", "reading_after_repetitions", "limits",
          "b_status_uid", "b_status_uid_detail", "source"]
TARGET = "target Pod across the repetitions"
SAME = "same Pod name and no restart in the kept readings"
SUM_FIELDS = ["config", "variant", "quantity", "valid_runs", "runs_reported", "runs_none_reported",
              "runs_not_measurable", "runs_not_applicable", "runs_differing_from_the_runner", "reported_min", "reported_max",
              "status_counts", "b_status_uid_counts", "source"]


def rel(path):
    """From `results/` on for the repository's results; the full path for the runner's files (frozen worktree)."""
    path = os.path.abspath(path)
    marker = "/cloud_native_ros_kubernetes/results/"
    return path[path.index(marker) + len("/cloud_native_ros_kubernetes/"):] if marker in path else path


def summary_lines(path):
    with open(path) as f:
        return {line.rstrip("\n") for line in f if line.strip()}


def churn(before, after, prefix):
    """count_churn of run_u1.sh: distinct Pod names among the new events, Created plus Killing."""
    created, stopped = set(), set()
    for line in summary_lines(after) - summary_lines(before):
        uid, reason, kind, name = (line.split("|") + ["", "", "", ""])[:4]
        if kind != "Pod" or not name.startswith(prefix):
            continue
        if reason == "Created":
            created.add(name)
        elif reason == "Killing":
            stopped.add(name)
    return sorted(created), sorted(stopped)


def reading_after_update(path, logical):
    """pods-after.tsv (snapshot_managed_pods of run_u1.sh): logical name, Pod, UID, restarts of container 0, node."""
    try:
        with open(path) as f:
            rows = [line.rstrip("\n").split("\t") for line in f if line.strip()]
    except OSError:
        return None
    pods = [r for r in rows if len(r) >= 4 and r[0] == logical]
    if len(pods) != 1:
        return None
    restarts = pods[0][3]
    return {"pod": pods[0][1], "uid": pods[0][2] or None, "restarts": int(restarts) if restarts.isdigit() else None}


def readings_after_repetitions(path, prefix):
    """pods.txt (kubectl get pods -o wide): the target's Pods, by the Deployment's Pod-name form; no UID there."""
    try:
        with open(path) as f:
            lines = [line.split() for line in f if line.strip()]
    except OSError:
        return None
    if not lines or "RESTARTS" not in lines[0]:
        return None
    col = lines[0].index("RESTARTS")
    out = []
    for r in lines[1:]:
        if re.match(rf"^{re.escape(prefix)}[a-z0-9]+-[a-z0-9]+$", r[0]):
            restarts = r[col] if len(r) > col else ""
            out.append({"pod": r[0], "uid": None, "restarts": int(restarts) if restarts.isdigit() else None})
    return out


def compare_target(after_update, after_repetitions):
    """(status, reason) of the target Pod across the repetitions."""
    if after_update is None:
        return "not recorded", "the target Pod is not read once in pods-after.tsv"
    if not after_repetitions:
        return "not recorded", "pods.txt absent, unreadable or without the target's Pod"
    if after_update["restarts"] is None or any(r["restarts"] is None for r in after_repetitions):
        return "not recorded", "a restart count is not readable"
    if any(r["uid"] and after_update["uid"] and r["uid"] != after_update["uid"] for r in after_repetitions):
        return "UID changed", ""
    if [r["pod"] for r in after_repetitions] != [after_update["pod"]]:
        return "Pod name changed", ""
    if after_update["restarts"] > 0 or after_repetitions[0]["restarts"] > 0:
        return "restart count positive", ""
    return SAME, ""


def utc_of(stamp):
    main, _, frac = stamp.rstrip("Z").partition(".")
    return calendar.timegm(time.strptime(main, "%Y-%m-%dT%H:%M:%S")) + (float("0." + frac) if frac else 0.0)


def b_status_uid(result_dir, first):
    """B only: (status, detail) of the ROSModule status' podUID for the target Pod read after the update."""
    path = os.path.join(result_dir, "kubernetes-resources.yaml")
    try:
        with open(path) as f:
            doc = yaml.safe_load(f) or {}
    except (OSError, yaml.YAMLError):
        return "not recorded", f"{rel(path)} absent or unreadable"
    if first is None:
        return "not recorded", "no target Pod read after the update to compare with"
    module = next((x for x in doc.get("items") or [] if x.get("kind") == "ROSModule"
                   and (x.get("metadata") or {}).get("name") == PREFIX["b"][:-1]), None)
    instances = ((module or {}).get("status") or {}).get("lifecycleInstances") or {}
    key = next((k for k in instances if k.endswith(first["pod"].replace("-", "_"))), None)
    if key is None:
        return "not recorded", f"no lifecycle instance of {first['pod']} in the ROSModule status"
    inst = instances[key]
    src = (f"{rel(path)}#items[kind=ROSModule,name={PREFIX['b'][:-1]}]/status/lifecycleInstances/{key}"
           "/podUID,lastObservedTime")
    if inst.get("podUID") != first["uid"]:
        return "different UID in the status", f"podUID {inst.get('podUID')} against {first['uid']}; {src}"
    before, after = (os.path.join(result_dir, f"events-{w}-repeat-2.txt") for w in ("before", "after"))
    try:
        lo, hi, seen = os.path.getmtime(before), os.path.getmtime(after), utc_of(inst["lastObservedTime"])
    except (OSError, KeyError, ValueError):
        return "same UID in the status, not placed in time", f"lastObservedTime or repetition 2 bounds missing; {src}"
    when = "during repetition 2" if lo <= seen <= hi else "outside repetition 2"
    stamp = lambda t: time.strftime("%H:%M:%S", time.gmtime(t)) + f".{int(t % 1 * 1000):03d}"
    return (f"same UID in the status, observed {when}",
            f"podUID {inst['podUID']} = UID after the update; lastObservedTime {inst['lastObservedTime']}; "
            f"repetition 2 between {stamp(lo)} and {stamp(hi)} UTC (file modification times of {rel(before)} and "
            f"{rel(after)}); state written by variant B, not proof that the current Pod still had this UID after "
            f"the completion (the status may be stale); {src}")


def target_row(base, cfg, v, result_dir):
    after_path, end_path = os.path.join(result_dir, "pods-after.tsv"), os.path.join(result_dir, "pods.txt")
    row = {**base, "quantity": TARGET}
    if cfg not in REPEATS:
        return {**row, "status": "not applicable", "reason": "the protocol does not repeat the request",
                "b_status_uid": "not applicable", "b_status_uid_detail": "the protocol does not repeat the request",
                "source": "scripts/run_u2.sh (no repeated request)"}
    first, end = reading_after_update(after_path, PREFIX[v][:-1]), readings_after_repetitions(end_path, PREFIX[v])
    status, reason = compare_target(first, end)
    b_status, b_detail = (b_status_uid(result_dir, first) if v == "b" else
                          ("not applicable", "variant A has no ROSModule status: its kubernetes-resources.yaml "
                                             "holds Deployments, Services, Jobs and PersistentVolumeClaims only"))
    return {**row, "status": status, "reason": reason, "b_status_uid": b_status, "b_status_uid_detail": b_detail,
            "reading_after_update": "" if first is None else
            f"after the update, before repetition 1: {first['pod']} uid={first['uid']} restarts={first['restarts']} "
            "(first container)",
            "reading_after_repetitions": "; ".join(f"after repetition 2: {r['pod']} restarts={r['restarts']} "
                                                   "(all containers; no UID in this reading)" for r in end or []),
            "limits": "no Pod reading after repetition 1; no UID after the repetitions, so only the name is compared; "
                      "restart count of the first container only after the update",
            "source": f"{rel(after_path)}; {rel(end_path)}"}


def execution_rows(cfg, v, pair, seq, result_dir):
    rows = []
    report_path = os.path.join(result_dir, "REPORT.md")
    try:
        with open(report_path) as f:
            report = f.read()
    except OSError:
        report = ""
    for quantity, before_name, after_name, pattern in QUANTITIES:
        base = {k: "" for k in FIELDS}
        base.update(config=cfg, variant=v, pair=pair, seq=seq, quantity=quantity)
        before, after = os.path.join(result_dir, before_name), os.path.join(result_dir, after_name)
        if cfg not in REPEATS and quantity.startswith("extra churn"):
            rows.append({**base, "status": "not applicable", "reason": "the protocol does not repeat the request",
                         "source": "scripts/run_u2.sh (no repeated request)"})
            continue
        if not (os.path.isfile(before) and os.path.isfile(after)):
            rows.append({**base, "status": "not measurable",
                         "reason": "no event summary before and after: the events of this runner are a final "
                                   "snapshot with relative times (kubernetes-events.txt)",
                         "source": f"{rel(before)} (absent); {rel(os.path.join(result_dir, 'kubernetes-events.txt'))}"})
            continue
        created, stopped = churn(before, after, PREFIX[v])
        count = len(created) + len(stopped)
        m = re.search(pattern, report)
        runner = m.group(1) if m else ""
        status = ("differs from the runner's report" if runner != str(count)
                  else "reported" if count else "none reported")
        rows.append({**base, "status": status, "reported": count or "", "runner_report": runner,
                     "pods_created": " ".join(created), "pods_stopped": " ".join(stopped),
                     "source": f"{rel(before)}; {rel(after)}; {rel(report_path)}"})
    base = {k: "" for k in FIELDS}
    base.update(config=cfg, variant=v, pair=pair, seq=seq)
    rows.append(target_row(base, cfg, v, result_dir))
    return rows


def rows_of(campaign):
    rows = []
    for e in rs.build():
        if e["config"] not in ("u1", "u2"):
            continue
        run_dir = os.path.join(campaign, "runs", f"{e['seq']:03d}-{e['config']}-{e['variant']}")
        try:
            with open(os.path.join(run_dir, "run.json")) as f:
                rec = json.load(f)
        except (OSError, ValueError):
            continue
        if rec.get("valid") is not True:
            continue                                   # denominators: valid executions only
        rows += execution_rows(e["config"], e["variant"], e["pair"], e["seq"], rec.get("result_dir") or "")
    order = [q[0] for q in QUANTITIES] + [TARGET]
    rows.sort(key=lambda r: (r["config"], r["variant"], int(r["seq"]), order.index(r["quantity"])))
    return rows


def summary_rows(rows):
    out, keys = [], []
    for r in rows:
        if (r["config"], r["variant"], r["quantity"]) not in keys:
            keys.append((r["config"], r["variant"], r["quantity"]))
    for cfg, v, q in keys:
        rs_ = [r for r in rows if (r["config"], r["variant"], r["quantity"]) == (cfg, v, q)]
        counts = [int(r["reported"]) for r in rs_ if r["status"] == "reported"]
        out.append({"config": cfg, "variant": v, "quantity": q, "valid_runs": len(rs_), "runs_reported": len(counts),
                    "runs_none_reported": sum(r["status"] == "none reported" for r in rs_),
                    "runs_not_measurable": sum(r["status"] == "not measurable" for r in rs_),
                    "runs_not_applicable": sum(r["status"] == "not applicable" for r in rs_),
                    "runs_differing_from_the_runner": sum(r["status"] == "differs from the runner's report" for r in rs_),
                    "reported_min": min(counts) if counts else "", "reported_max": max(counts) if counts else "",
                    "status_counts": "; ".join(f"{k}={n}" for k, n in sorted(collections.Counter(r["status"] for r in rs_).items())),
                    "b_status_uid_counts": "; ".join(f"{k}={n}" for k, n in sorted(collections.Counter(
                        r["b_status_uid"] for r in rs_ if r["b_status_uid"]).items())),
                    "source": f"data/measured/update_churn_runs.csv: config={cfg}, variant={v}, quantity={q}"})
    return out


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
    measured = os.path.join(a.out, "measured")
    os.makedirs(measured, exist_ok=True)
    runs_csv, sum_csv = os.path.join(measured, "update_churn_runs.csv"), os.path.join(measured, "update_churn.csv")
    write_csv(runs_csv, FIELDS, rows_of(a.campaign))
    write_csv(sum_csv, SUM_FIELDS, summary_rows(read_csv(runs_csv)))
    # no table: the reconciliation churn of U1 is already in the campaign table; these files back a sentence
    print("update_churn")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
