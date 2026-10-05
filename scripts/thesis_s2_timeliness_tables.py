#!/usr/bin/env python3
"""Timeliness of the reaction in S2: observed completion of the passage to the edge (definitions 4, 4-bis and
4-ter of docs/EXTRACTION_DEFINITIONS_PROPOSAL.md, approved by Viviana on 5 October 2026 after the request of the
co-supervisor). Descriptive, added after the collection: the preregistered primary of S2 does not change.

For every valid S2 execution of the calendar (scripts/r14_schedule.py), from files the runner kept:
  - references: T_open and T_end, the local episode opened (third consecutive window over the threshold) and
    its local return, from the recorder on the robot's node (s2-judge.json ground_truth.episodes); T_restore_cmd,
    the end of the restore command (timeline.restore.command_end); T_conn, the observed connectivity, the first
    restore probe that connected to the API server and the kubelet (phases.jsonl, mark restore_probe);
    horizon: the judge's (180 s from the end of the restore command; in the control case from the programmed
    end of the pulse), not 180 s from each reference;
  - common criterion, the S2 health prober only (prober/health.jsonl: same manifest, targets and period in A
    and B, on the edge node, independent of the control plane). A usable answer is positive or negative with
    an answer from robot drone01 and the target's own instance; a timeout, an error, a missing answer or
    another identity is not usable and proves nothing. Edge active: a usable edge answer, healthy and
    lifecycle 'active'. Onboard deactivated: a usable onboard answer with lifecycle exactly 'inactive' (it
    answers healthy=false). The state of a target at t is its latest usable answer received by t, if not older
    than 10 s (HEALTH_GAP_SEC of the judge, fixed before this analysis), else unknown;
  - observed completion of the passage to the edge: the first receipt of a usable answer, between T_open and
    the horizon, at which the edge state is active and the onboard state is deactivated; reported with the
    last earlier instant at which the condition was known false (a known state excluding it), and the width.
    It is not a proof of continuity between the answers. After it, up to the horizon, the usable answers that
    contradict it and the time with a target's state unknown are recorded, not ignored;
  - absence: "no handover observed within the observation horizon" only if, from T_conn (from the start of the
    phase in the control case) to the horizon, the known state of at least one target excludes the completion
    at every instant; otherwise "insufficient observations", with the uncovered time. For information only,
    not the status: the delay of the first usable answer after that start, and the uncovered time from it;
  - kept apart, per variant and declared different: recognition and start of the action (A: dispatcher log;
    B: AdaptationPolicy status read by the inventory observer, first read showing it);
  - context with sources: the restore command's delay after the local return and the duration of the cut.
The control case has one execution per variant: an indication, not a stable reference. The partition-only
case has no episode: not applicable.

Usage: python3 scripts/thesis_s2_timeliness_tables.py --campaign CAMPAIGN_DIR --out DATA_DIR
"""
import argparse
import csv
import json
import os
import statistics
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import r14_schedule as rs  # noqa: E402

CONFIGS = ("s2-short", "s2-long", "s2-control", "s2-partition-only")
ROBOT, EDGE, ONBOARD = "drone01", "drone01-edge", "drone01-onboard"
FRESH_SEC = 10.0                       # HEALTH_GAP_SEC of scripts/s2_judge.py
COMPLETED, NO_HANDOVER, INSUFFICIENT = ("completion observed", "no handover observed within the observation horizon",
                                        "insufficient observations")
DELAYS = [("completion_after_end_s", "completion", "t_end"), ("completion_after_conn_s", "completion", "t_conn"),
          ("completion_after_restore_cmd_s", "completion", "t_restore_cmd"),
          ("completion_after_open_s", "completion", "t_open"),
          ("recognition_after_end_s", "recognition", "t_end"), ("action_start_after_end_s", "action_start", "t_end"),
          ("restore_cmd_after_end_s", "t_restore_cmd", "t_end"), ("cut_duration_s", "t_restore_cmd", "t_cut")]
FIELDS = (["config", "variant", "pair", "seq", "status", "reason", "t_open_utc", "t_end_utc", "t_restore_cmd_utc",
           "t_conn_utc", "horizon_utc", "completion_utc", "completion_lower_bound_utc", "completion_interval_s"]
          + [d[0] for d in DELAYS]
          + ["usable_answers_after_completion", "contradicting_answers_after_completion",
             "first_contradiction_after_completion_s", "unknown_state_after_completion_s", "uncovered_s",
             "first_usable_answer_after_coverage_start_s", "uncovered_from_first_usable_answer_s",
             "recognition_source", "action_start_source", "notes", "source"])
SUM_FIELDS = ["config", "variant", "quantity", "valid_runs", "observed", "median", "min", "max", "status_counts",
              "source"]


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


def read_jsonl(path):
    try:
        with open(path) as f:
            return [json.loads(line) for line in f if line.strip()]
    except (OSError, ValueError):
        return None


def usable(record):
    answer = record.get("answer") or {}
    return (record.get("result") in ("positive", "negative") and bool(record.get("answer"))
            and answer.get("robot_id") == ROBOT and answer.get("instance_id") == record.get("instance")
            and record.get("outcome_utc") is not None)


def wanted(target, record):
    """True when the usable answer shows the target's state of a completed passage."""
    answer = record["answer"]
    if target == EDGE:
        return answer.get("healthy") is True and answer.get("lifecycle_state") == "active"
    return answer.get("lifecycle_state") == "inactive"


def state(answers, t):
    """(wanted?, answer) of the latest usable answer received by t if not older than FRESH_SEC, else (None, None)."""
    last = None
    for a in answers:
        if a["outcome_utc"] > t:
            break
        last = a
    if last is None or t - last["outcome_utc"] > FRESH_SEC:
        return None, None
    return last["wanted"], last


def evaluate(health, t_from, horizon, coverage_from):
    """The observed completion, or its absence, from the prober's records alone."""
    answers = {target: sorted(({**r, "wanted": wanted(target, r)} for r in health
                               if r.get("event") == "health" and r.get("target") == target and usable(r)),
                              key=lambda r: r["outcome_utc"]) for target in (EDGE, ONBOARD)}
    instants = sorted({a["outcome_utc"] for xs in answers.values() for a in xs if t_from <= a["outcome_utc"] <= horizon})
    out = {"completion": None, "lower": None}
    for t in instants:
        states = [state(answers[target], t)[0] for target in (EDGE, ONBOARD)]
        if all(s is True for s in states):
            out["completion"] = t
            break
        if any(s is False for s in states):
            out["lower"] = t                   # a known state excluded the completion
    if out["completion"] is not None:
        after = [a for xs in answers.values() for a in xs if out["completion"] < a["outcome_utc"] <= horizon]
        contra = sorted(a["outcome_utc"] for a in after if not a["wanted"])
        unknown = 0.0
        for target in (EDGE, ONBOARD):
            times = [out["completion"]] + [a["outcome_utc"] for a in answers[target]
                                           if out["completion"] < a["outcome_utc"] <= horizon] + [horizon]
            unknown += sum(max(0.0, b - a - FRESH_SEC) for a, b in zip(times, times[1:]))
        out.update(usable_after=len(after), contradicting=len(contra),
                   first_contradiction=(contra[0] - out["completion"]) if contra else None, unknown=unknown)
        return out
    # absence: some target's known state excludes the completion at every instant of [coverage_from, horizon].
    # An excluding answer counts for FRESH_SEC at most, and only until the next usable answer of the same target:
    # a later answer replaces it, as in state() (review of 1c64878, 5 October).
    covered = []
    for xs in answers.values():
        for a, following in zip(xs, xs[1:] + [None]):
            if a["wanted"] or a["outcome_utc"] > horizon:
                continue
            hi = a["outcome_utc"] + FRESH_SEC
            if following is not None:
                hi = min(hi, following["outcome_utc"])
            covered.append((a["outcome_utc"], hi))
    covered.sort()
    out["uncovered"] = uncovered_time(covered, coverage_from, horizon)
    # information only, not the status: the same coverage from the first usable answer after coverage_from
    first = min((a["outcome_utc"] for xs in answers.values() for a in xs if a["outcome_utc"] >= coverage_from),
                default=None)
    out["first_usable"] = None if first is None else first - coverage_from
    out["uncovered_from_first"] = None if first is None else uncovered_time(covered, first, horizon)
    return out


def uncovered_time(covered, start, horizon):
    cursor, uncovered = start, 0.0
    for lo, hi in covered:
        if hi <= cursor:
            continue
        if lo > cursor:
            uncovered += min(lo, horizon) - cursor
        cursor = max(cursor, hi)
        if cursor >= horizon:
            break
    if cursor < horizon:
        uncovered += horizon - cursor
    return uncovered


def secondary(judge, variant, key):
    """(utc, source) of recognition or of the action's start, as the judge recorded it for this variant."""
    node = (judge.get("recognized") or {}).get("at") if key == "recognition" else (judge.get("action") or {}).get("requested")
    if not node:
        return None, ""
    if variant == "a":
        return node.get("utc"), node.get("source") or ""
    first = node.get("first_with") or {}
    return first.get("utc"), (node.get("source") or "") + ", first read showing it"


def execution_row(cfg, v, pair, seq, result_dir):
    row = {k: "" for k in FIELDS}
    row.update(config=cfg, variant=v, pair=pair, seq=seq)
    judge_path = os.path.join(result_dir, "s2-judge.json")
    health_path = os.path.join(result_dir, "prober", "health.jsonl")
    marks_path = os.path.join(result_dir, "phases.jsonl")
    row["source"] = (f"{rel(judge_path)}#/ground_truth/episodes,/timeline,/recognized/at,/action/requested; "
                     f"{rel(health_path)}; {rel(marks_path)}#mark=restore_probe")
    if cfg == "s2-partition-only":
        return {**row, "status": "not applicable", "reason": "no episode injected"}
    judge, health, marks = load_json(judge_path), read_jsonl(health_path), read_jsonl(marks_path)
    if judge is None or health is None or marks is None:
        return {**row, "status": "not measurable", "reason": "s2-judge.json, prober/health.jsonl or phases.jsonl missing"}
    episodes = (judge.get("ground_truth") or {}).get("episodes") or []
    tl = judge.get("timeline") or {}
    if len(episodes) != 1 or not (episodes[0].get("entered") or {}).get("utc"):
        return {**row, "status": "not measurable", "reason": "not exactly one local episode with its opening"}
    if not (tl.get("horizon") or {}).get("utc"):
        return {**row, "status": "not measurable", "reason": "no horizon in the judge's timeline"}
    refs = {"t_open": episodes[0]["entered"]["utc"], "t_end": (episodes[0].get("returned") or {}).get("utc"),
            "horizon": tl["horizon"]["utc"]}
    notes = []
    if refs["t_end"] is None:
        notes.append("no local return observed: delays relative to the end not measurable")
    control = cfg == "s2-control"
    if control:
        notes.append("control case: no cut, one execution per variant, indicative only")
        coverage_from = (tl.get("phase_start") or {}).get("utc")
    else:
        refs["t_restore_cmd"] = ((tl.get("restore") or {}).get("command_end") or {}).get("utc")
        refs["t_cut"] = ((tl.get("cut") or {}).get("first_drop") or {}).get("utc")
        probe = next((m for m in marks if m.get("mark") == "restore_probe" and m.get("connected") is True), None)
        refs["t_conn"] = (probe or {}).get("utc")
        if refs["t_restore_cmd"] is None or refs["t_conn"] is None:
            return {**row, "status": "not measurable",
                    "reason": "no end of the restore command or no observed connectivity (restore_probe)"}
        coverage_from = refs["t_conn"]
    result = evaluate(health, refs["t_open"], refs["horizon"], coverage_from)
    rec, rec_src = secondary(judge, v, "recognition")
    act, act_src = secondary(judge, v, "action_start")
    instants = {**refs, "completion": result["completion"], "recognition": rec, "action_start": act}
    row.update(t_open_utc=refs["t_open"], t_end_utc=refs["t_end"] or "", t_restore_cmd_utc=refs.get("t_restore_cmd") or "",
               t_conn_utc=refs.get("t_conn") or "", horizon_utc=refs["horizon"],
               recognition_source=rec_src, action_start_source=act_src)
    for name, later, earlier in DELAYS:
        if instants.get(later) is not None and instants.get(earlier) is not None:
            row[name] = round(instants[later] - instants[earlier], 3)
    if result["completion"] is not None:
        row.update(status=COMPLETED, completion_utc=result["completion"],
                   completion_lower_bound_utc=result["lower"] if result["lower"] is not None else "",
                   completion_interval_s=round(result["completion"] - result["lower"], 3) if result["lower"] is not None else "",
                   usable_answers_after_completion=result["usable_after"],
                   contradicting_answers_after_completion=result["contradicting"],
                   first_contradiction_after_completion_s=("" if result["first_contradiction"] is None
                                                           else round(result["first_contradiction"], 3)),
                   unknown_state_after_completion_s=round(result["unknown"], 3))
        if result["lower"] is None:
            notes.append("no earlier instant with the condition known false: no lower bound")
    else:
        row.update(first_usable_answer_after_coverage_start_s=("" if result["first_usable"] is None
                                                               else round(result["first_usable"], 3)),
                   uncovered_from_first_usable_answer_s=("" if result["uncovered_from_first"] is None
                                                         else round(result["uncovered_from_first"], 3)))
        if result["uncovered"] <= 0:
            row.update(status=NO_HANDOVER, uncovered_s=0)
        else:
            row.update(status=INSUFFICIENT, uncovered_s=round(result["uncovered"], 3),
                       reason=f"{round(result['uncovered'], 1)} s without a known state excluding the completion")
    row["notes"] = "; ".join(notes)
    return row


def rows_of(campaign):
    rows = []
    for e in rs.build():
        if e["config"] not in CONFIGS:
            continue
        run_dir = os.path.join(campaign, "runs", f"{e['seq']:03d}-{e['config']}-{e['variant']}")
        rec = load_json(os.path.join(run_dir, "run.json"))
        if rec is None or rec.get("valid") is not True:
            continue                                   # denominators: valid executions only
        rows.append(execution_row(e["config"], e["variant"], e["pair"], e["seq"], rec.get("result_dir") or ""))
    rows.sort(key=lambda r: (CONFIGS.index(r["config"]), r["variant"], int(r["seq"])))
    return rows


def summary_rows(rows):
    out = []
    for cfg in CONFIGS:
        for v in ("a", "b"):
            mine = [r for r in rows if r["config"] == cfg and r["variant"] == v]
            if not mine:
                continue
            counts = "; ".join(f"{s}={n}" for s, n in sorted(
                {r["status"]: sum(x["status"] == r["status"] for x in mine) for r in mine}.items()))
            for name, _, _ in DELAYS:
                values = [float(r[name]) for r in mine if r[name] != ""]
                out.append({"config": cfg, "variant": v, "quantity": name, "valid_runs": len(mine),
                            "observed": len(values),
                            "median": round(statistics.median(values), 3) if values else "",
                            "min": round(min(values), 3) if values else "", "max": round(max(values), 3) if values else "",
                            "status_counts": counts,
                            "source": f"data/measured/s2_timeliness_runs.csv: config={cfg}, variant={v}, column={name}"})
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
    runs_csv, sum_csv = os.path.join(measured, "s2_timeliness_runs.csv"), os.path.join(measured, "s2_timeliness.csv")
    write_csv(runs_csv, FIELDS, rows_of(a.campaign))
    write_csv(sum_csv, SUM_FIELDS, summary_rows(read_csv(runs_csv)))
    # no table: these files back the sentences of the Discussion
    print("s2_timeliness")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
