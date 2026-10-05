#!/usr/bin/env python3
"""Audit records per incident, descriptive (approved definitions D-A and D-B (i)-(ii), 4 October 2026,
docs/EXTRACTION_DEFINITIONS_PROPOSAL.md). Not a measure of audit completeness, which stays a declared deviation.

For every valid execution of the calendar (scripts/r14_schedule.py) and of the final S1 block, from the
execution's audit.jsonl (PlatformSnapshot records ignored):
  - records per type (incident_started, incident_completed, operator_notification, others);
  - incidents against the requests the protocol injects (D-B (i) duplication of the incident);
  - whether start and end of an incident share the correlation_id (limit of the audit correlation);
  - identical requests repeated inside one incident (D-B (ii): an observed repetition, NOT a duplicate:
    it may be a legitimate retry). Proven duplicate effects (D-B (iii)) need the Kubernetes events and are
    not extracted here.
Where the scenario has no audit, "not recorded" or "not applicable" with the reason, never zero.
Every CSV row carries `source`; the table is generated from the CSV files only.

Usage: python3 scripts/thesis_audit_tables.py --campaign CAMPAIGN_DIR --s1-block S1_BLOCK_DIR --out DATA_DIR
"""
import argparse
import collections
import csv
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import r14_schedule as rs  # noqa: E402

AUDIT = "audit.jsonl"
STARTED, COMPLETED, NOTIFIED, FEEDBACK = "incident_started", "incident_completed", "operator_notification", "incident_feedback"
# requests of the protocol that open an incident, per configuration and variant; None = no incident by design
REQUESTS = {"e0": None, "e1": 1, "e2": 1, "p2": 1, "e4": 1, "u1": 1, "u2": 2, "s1-delete": 1,
            "s2-short": 1, "s2-long": 1, "s2-control": 1, "s2-partition-only": None,
            "s3-n3": 1, "s3-n10": 1, "s4-l3": 3, "s4-l1-battery": 1, "s4-l1-telemetry": 1, "s4-l1-edge": 1,
            "ttr": None}
NOT_APPLICABLE = {
    ("e0", "a"): "no fault injected", ("e0", "b"): "no fault injected",
    ("e1", "b"): "the battery alarm is kept out of the scope of Fleet Operator and State Bridge by design",
    ("s2-partition-only", "a"): "no episode injected", ("s2-partition-only", "b"): "no episode injected",
    ("ttr", "a"): "rebuild procedure, no incident", ("ttr", "b"): "rebuild procedure, no incident",
}
FIELDS = ["block", "config", "variant", "pair", "seq", "status", "reason", "requests", "started", "completed",
          "notified", "incidents", "correlation", "outcomes", "other_records", "feedback_steps", "feedback_sequence",
          "repeated_requests", "source"]
SUM_FIELDS = ["config", "variant", "valid_runs", "recorded", "not_recorded", "not_applicable", "reason", "complete",
              "completion_only", "no_incident", "consistent", "not_correlatable", "one_incident_per_request",
              "more_incidents_than_requests", "runs_with_repeated_requests", "runs_with_other_records", "source"]
# printed names of the record types outside the incident (the CSV keeps the raw type)
OTHER_TEXT = {"lifecycle_observation_lost": "lifecycle observation loss recorded"}
# scenario families printed as one name when every configuration shares the status in both variants
FAMILY = {"s3": "S3 at both fleet sizes", "s4": "S4 in all four cells"}
LABEL = {"e0": "E0", "e1": "E1", "e2": "E2", "p2": "P2", "e4": "E4", "u1": "U1", "u2": "U2",
         "s1-delete": "S1, deletion", "s2-short": "S2, short", "s2-long": "S2, long", "s2-control": "S2, control",
         "s2-partition-only": "S2, partition only", "s3-n3": "S3, $N=3$", "s3-n10": "S3, $N=10$",
         "s4-l3": "S4, three faults", "s4-l1-battery": "S4, battery fault alone",
         "s4-l1-telemetry": "S4, telemetry fault alone", "s4-l1-edge": "S4, edge fault alone", "ttr": "Time to rebuild"}


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


def read_audit(path):
    with open(path) as f:
        return [json.loads(line) for line in f if line.strip()]


def analyse(records, requests):
    recs = [r for r in records if r.get("event_type") != "PlatformSnapshot"]
    by_type = collections.Counter(r.get("record_type") for r in recs)
    started_ids = [r.get("correlation_id") for r in recs if r.get("record_type") == STARTED]
    completed_ids = [r.get("correlation_id") for r in recs if r.get("record_type") == COMPLETED]
    if started_ids:
        incidents = len(set(started_ids))
        correlation = ("consistent" if all(c in started_ids for c in completed_ids) and completed_ids
                       else "not correlatable" if completed_ids else "start only")
    elif completed_ids:
        incidents, correlation = len(set(completed_ids)), "completion only"
    else:
        incidents, correlation = 0, "no incident"
    steps = collections.Counter((r.get("correlation_id"), r.get("phase"), r.get("message"))
                                for r in recs if r.get("record_type") == FEEDBACK)
    others = {k: v for k, v in by_type.items() if k not in (STARTED, COMPLETED, NOTIFIED, FEEDBACK)}
    return {"started": by_type[STARTED], "completed": by_type[COMPLETED], "notified": by_type[NOTIFIED],
            "incidents": incidents, "correlation": correlation,
            "outcomes": "; ".join(r.get("outcome") or "" for r in recs if r.get("record_type") == COMPLETED),
            "other_records": "; ".join(f"{k}={v}" for k, v in sorted(others.items())),
            "feedback_steps": sum(steps.values()),
            # the phases in the order of the file, so that a path that differs can be named with its source
            "feedback_sequence": " > ".join(f"{r.get('phase')}: {r.get('message')}"
                                            for r in recs if r.get("record_type") == FEEDBACK),
            "repeated_requests": sum(n - 1 for n in steps.values() if n > 1)}


def execution_rows(block, run_root, entries):
    rows = []
    for e in entries:
        cfg, v = e["config"], e["variant"]
        run_dir = os.path.join(run_root, "runs", f"{e['seq']:03d}-{cfg}-{v}")
        rec = load_json(os.path.join(run_dir, "run.json"))
        base = {k: "" for k in FIELDS}
        base.update(block=block, config=cfg, variant=v, pair=e["pair"], seq=e["seq"],
                    requests="" if REQUESTS[cfg] is None else REQUESTS[cfg])
        if rec is None or rec.get("valid") is not True:
            continue                                       # denominators: valid executions only
        path = os.path.join(rec.get("result_dir") or "", AUDIT)
        if (cfg, v) in NOT_APPLICABLE:
            rows.append({**base, "status": "not applicable", "reason": NOT_APPLICABLE[(cfg, v)],
                         "source": rel(os.path.join(run_dir, "run.json")) + "#/config"})
        elif not os.path.isfile(path):
            rows.append({**base, "status": "not recorded", "reason": "audit not collected by the runner",
                         "source": rel(path) + " (absent)"})
        else:
            rows.append({**base, **analyse(read_audit(path), REQUESTS[cfg]), "status": "recorded",
                         "source": rel(path) + "#record_type,correlation_id,phase,message,outcome"})
    return rows


def summary_rows(rows):
    out = []
    keys = []
    for r in rows:
        if (r["config"], r["variant"]) not in keys:
            keys.append((r["config"], r["variant"]))
    for cfg, v in keys:
        rs_ = [r for r in rows if r["config"] == cfg and r["variant"] == v]
        rec = [r for r in rs_ if r["status"] == "recorded"]
        req = lambda r: int(r["requests"]) if r["requests"] else 0
        others = collections.Counter(k.split("=")[0] for r in rec for k in r["other_records"].split("; ") if k)
        out.append({
            "config": cfg, "variant": v, "valid_runs": len(rs_), "recorded": len(rec),
            "not_recorded": sum(r["status"] == "not recorded" for r in rs_),
            "not_applicable": sum(r["status"] == "not applicable" for r in rs_),
            "reason": "; ".join(sorted({r["reason"] for r in rs_ if r["reason"]})),
            "complete": sum(int(r["started"]) >= req(r) and int(r["completed"]) >= req(r)
                            and int(r["notified"]) >= req(r) and req(r) > 0 for r in rec),
            "completion_only": sum(r["correlation"] == "completion only" for r in rec),
            "no_incident": sum(r["correlation"] == "no incident" for r in rec),
            "consistent": sum(r["correlation"] == "consistent" for r in rec),
            "not_correlatable": sum(r["correlation"] == "not correlatable" for r in rec),
            "one_incident_per_request": sum(int(r["incidents"]) == req(r) for r in rec if req(r)),
            "more_incidents_than_requests": sum(int(r["incidents"]) > req(r) for r in rec if req(r)),
            "runs_with_repeated_requests": sum(int(r["repeated_requests"]) > 0 for r in rec),
            "runs_with_other_records": "; ".join(f"{k}={n}" for k, n in sorted(others.items())),
            "source": f"data/measured/audit_runs.csv: config={cfg}, variant={v}"})
    return out


# column widths of the table (cm): 411.3 pt with \tabcolsep 3 pt, within the 412.56 pt text width; every
# header line and "one each: 10 of 10" fit on one line (measured at \footnotesize in the thesis' class)
WIDTHS = (2.45, 1.35, 2.4, 2.7, 2.8, 1.7)


def tex(summary):
    def of(k, n):
        return f"{k}~of~{n}"

    lines, absent = [], {"not recorded": {}, "not applicable": {}}
    for r in summary:
        n, rec = int(r["valid_runs"]), int(r["recorded"])
        if not rec:                    # no audit of an incident: one full-width line per status, below
            status = "not applicable" if int(r["not_applicable"]) == n else "not recorded"
            absent[status].setdefault(r["reason"], {}).setdefault(r["config"], []).append(r["variant"].upper())
            continue
        with_incident = rec - int(r["no_incident"])
        parts = []
        for key, text in (("complete", "start, end, notification"), ("completion_only", "end only"),
                          ("no_incident", "no incident")):
            if int(r[key]):
                parts.append(f"{text}: {of(r[key], rec)}")
        for item in filter(None, r["runs_with_other_records"].split("; ")):
            kind, runs = item.split("=")
            parts.append(f"{OTHER_TEXT.get(kind, kind.replace('_', ' '))}: {of(runs, rec)}")
        if int(r["not_recorded"]):
            parts.append(f"not recorded: {of(r['not_recorded'], n)}")
        corr = [f"{t}: {of(r[k], rec)}" for k, t in (("consistent", "same id"), ("not_correlatable", "different ids"))
                if int(r[k])]
        incidents = (f"one each: {of(r['one_incident_per_request'], rec)}"
                     + (f"; more: {r['more_incidents_than_requests']}" if int(r["more_incidents_than_requests"]) else ""))
        repeated = (f"observed in {of(r['runs_with_repeated_requests'], with_incident)}"
                    if int(r["runs_with_repeated_requests"]) else f"none in {with_incident}")
        cells = ["; ".join(parts) or "--", "; ".join(corr) or "--",
                 incidents if int(r["complete"]) or int(r["completion_only"]) else "--",
                 repeated if with_incident else "--"]
        lines.append(f"{LABEL[r['config']]} & {r['variant'].upper()} ({n}) & " + " & ".join(cells) + " \\\\")
    configs = [r["config"] for r in summary]

    def names(by_config):
        # a family whose configurations all share this line in the same variants is printed once
        out, done = [], set()
        for cfg, variants in by_config.items():
            if cfg in done:
                continue
            fam = cfg.split("-")[0]
            members = [c for c in dict.fromkeys(configs) if c.split("-")[0] == fam]
            if fam in FAMILY and len(members) > 1 and all(by_config.get(c) == variants for c in members):
                out.append(f"{FAMILY[fam]} ({' and '.join(variants)})")
                done.update(members)
            else:
                out.append(f"{LABEL[cfg]} ({' and '.join(variants)})")
        return ", ".join(out)

    full = ("\\multicolumn{6}{@{}>{\\raggedright\\arraybackslash}p{\\dimexpr "
            f"{sum(WIDTHS):.2f}cm+10\\tabcolsep\\relax}}@{{}}}}")
    if any(absent.values()):
        lines.append("\\midrule")
    for status, by_reason in absent.items():
        if by_reason:
            lines.append(full + "{" + status.capitalize() + ". "
                         + "; ".join(f"{names(by)}: {reason}" for reason, by in by_reason.items()) + ".} \\\\")
    col = lambda w: f">{{\\raggedright\\arraybackslash}}p{{{w}}}"
    return ("% GENERATED by scripts/thesis_audit_tables.py from data/measured/audit.csv\n"
            "\\begin{tabular}{@{}" + "".join(col(f"{w}cm") for w in WIDTHS) + "@{}}\n\\toprule\n"
            "\\textbf{Configuration} & \\textbf{Variant} & "
            "\\shortstack[l]{\\textbf{Audit}\\\\\\textbf{records per}\\\\\\textbf{incident}} & "
            "\\shortstack[l]{\\textbf{Start and end}\\\\\\textbf{correlation}} & "
            "\\shortstack[l]{\\textbf{Incidents per}\\\\\\textbf{injection or}\\\\\\textbf{request}} & "
            "\\shortstack[l]{\\textbf{Repeated}\\\\\\textbf{requests}} \\\\\n\\midrule\n"
            + "\n".join(lines) + "\n\\bottomrule\n\\end{tabular}%\n")    # no space token after the table


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
    ap.add_argument("--s1-block", required=True)
    ap.add_argument("--out", required=True)
    a = ap.parse_args(argv)
    measured, tables = os.path.join(a.out, "measured"), os.path.join(a.out, "tables")
    os.makedirs(measured, exist_ok=True)
    os.makedirs(tables, exist_ok=True)
    campaign = [e for e in rs.build() if e["config"] != "s1-delete"]      # S1 comes from the final block
    rows = execution_rows("campaign", a.campaign, campaign) + execution_rows("s1-block", a.s1_block, rs.build_s1_block())
    order = list(REQUESTS)
    rows.sort(key=lambda r: (order.index(r["config"]), r["variant"], int(r["seq"])))
    runs_csv, sum_csv = os.path.join(measured, "audit_runs.csv"), os.path.join(measured, "audit.csv")
    write_csv(runs_csv, FIELDS, rows)
    write_csv(sum_csv, SUM_FIELDS, summary_rows(read_csv(runs_csv)))
    with open(os.path.join(tables, "audit.tex"), "w") as f:
        f.write(tex(read_csv(sum_csv)))
    print("audit")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
