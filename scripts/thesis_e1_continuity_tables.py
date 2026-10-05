#!/usr/bin/env python3
"""Detailed continuity of PX4 status in E1, copied from the judge's own output (mission-continuity.txt,
written by scripts/mission_continuity.py over the window in which the remote control path is unavailable).

Denominator: the executions of E1 in the calendar (scripts/r14_schedule.py). For each one and each quantity a
row of measured/e1_continuity_runs.csv with `source` (file#field); an execution that is not valid, or whose
file is missing, gives "not measurable" rows with the reason, never empty values. measured/e1_continuity.csv
summarises per variant (from the per-run CSV only) and tables/e1_continuity.tex is generated from it.

Usage: python3 scripts/thesis_e1_continuity_tables.py --campaign CAMPAIGN_DIR --out DATA_DIR
"""
import argparse
import csv
import json
import os
import statistics
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import r14_schedule as rs  # noqa: E402

FILE = "mission-continuity.txt"
RUN_FIELDS = ["seq", "pair", "variant", "quantity", "value", "unit", "status", "reason", "source"]
SUM_FIELDS = ["variant", "quantity", "unit", "denominator", "measured", "satisfied", "median", "min", "max", "total",
              "source"]
# quantity, field of the judge's file, unit, kind of summary
QUANTITIES = [
    ("continuity verdict", "verdict", "", "true"),
    ("state at window start: armed, no failsafe, Hold", "state_at_window_start", "", "start"),
    ("first navigation change after the window start", "transitions", "", "first_nav"),
    ("fault to return-to-launch state", "expected_change_after_not_before_ms", "ms", "spread"),
    ("status samples received", "status_count", "", "spread"),
    ("longest status gap in the recording", "max_gap_ms", "ms", "spread"),
    ("status gaps reaching the limit and touching the window", "gaps_over_limit_in_window", "", "total"),
    ("PX4 clock regressions in the window", "clock_regressions", "", "total"),
    ("PX4 clock regressions in the recording", "clock_regressions_total", "", "total"),
    ("unexpected state changes in the window", "unexpected_changes", "", "total"),
    ("observer restarts", "observer_restarts", "", "total"),
]
START_OK = "arming_state=ARMED failsafe=false nav_state=AUTO_LOITER"
RTL = "AUTO_LOITER->AUTO_RTL"


def rel(path):
    """From `results/` on for the repository's results; the full path for the runner's files (frozen worktree)."""
    path = os.path.abspath(path)
    marker = "/cloud_native_ros_kubernetes/results/"
    return path[path.index(marker) + len("/cloud_native_ros_kubernetes/"):] if marker in path else path


def parse(path):
    with open(path) as f:
        return dict(line.split("=", 1) for line in f.read().splitlines() if "=" in line)


def first_nav(transitions):
    """First nav_state change at or after the window start ('t+...'), from the judge's transitions."""
    for item in transitions.split("; "):
        parts = item.split(" ", 2)
        if len(parts) == 3 and parts[0].startswith("t+") and parts[1] == "nav_state":
            return parts[2]
    return None


def run_rows(campaign):
    rows = []
    for entry in [e for e in rs.build() if e["config"] == "e1"]:
        run_dir = os.path.join(campaign, "runs", f"{entry['seq']:03d}-e1-{entry['variant']}")
        rec_path = os.path.join(run_dir, "run.json")
        base = {"seq": entry["seq"], "pair": entry["pair"], "variant": entry["variant"]}
        reason, data, path = None, None, None
        if not os.path.isfile(rec_path):
            reason, src = "execution not recorded", rel(rec_path)
        else:
            with open(rec_path) as f:
                rec = json.load(f)
            path = os.path.join(rec.get("result_dir") or "", FILE)
            if rec.get("valid") is not True:
                reason, src = f"execution not valid ({rec.get('verdict')})", rel(rec_path) + "#/valid"
            elif not os.path.isfile(path):
                reason, src = f"{FILE} missing", rel(path)
            else:
                data = parse(path)
        for quantity, field, unit, kind in QUANTITIES:
            row = {**base, "quantity": quantity, "unit": unit, "value": "", "status": "", "reason": ""}
            if data is None:
                rows.append({**row, "status": "not measurable", "reason": reason, "source": src})
                continue
            row["source"] = f"{rel(path)}#{field}"
            raw = data.get(field)
            if raw is None:
                rows.append({**row, "status": "not measurable", "reason": f"field {field} absent"})
            elif kind == "first_nav":
                nav = first_nav(raw)
                rows.append({**row, "value": nav or "", "status": "measured" if nav else "not measurable",
                             "reason": "" if nav else "no navigation change after the window start"})
            elif kind == "spread" and raw == "none":
                rows.append({**row, "status": "not measurable",
                             "reason": "no return-to-launch state change after the fault in the window"})
            elif kind in ("spread", "total") and not raw.lstrip("-").isdigit():
                rows.append({**row, "status": "not measurable", "reason": f"{field}={raw}"})
            else:
                rows.append({**row, "value": raw, "status": "measured"})
    return rows


def summary_rows(rows):
    out = []
    for variant in ("a", "b"):
        for quantity, field, unit, kind in QUANTITIES:
            rs_ = [r for r in rows if r["variant"] == variant and r["quantity"] == quantity]
            valid = [r for r in rs_ if not r["reason"].startswith("execution")]   # invalid/unrecorded: out of n
            got = [r for r in rs_ if r["status"] == "measured"]
            row = {"variant": variant, "quantity": quantity, "unit": unit, "denominator": len(valid),
                   "measured": len(got), "satisfied": "", "median": "", "min": "", "max": "", "total": "",
                   "source": f"data/measured/e1_continuity_runs.csv: variant={variant}, quantity={quantity}"}
            if kind == "true":
                row["satisfied"] = sum(r["value"] == "true" for r in got)
            elif kind == "start":
                row["satisfied"] = sum(r["value"] == START_OK for r in got)
            elif kind == "first_nav":
                row["satisfied"] = sum(r["value"] == RTL for r in got)
            elif got:
                vals = [int(r["value"]) for r in got]
                if kind == "spread":
                    row.update(median=statistics.median(vals), min=min(vals), max=max(vals))
                else:
                    row["total"] = sum(vals)
            out.append(row)
    return out


def num(x):
    x = float(x)
    return f"{x:.0f}" if x == int(x) else f"{x:.1f}"


def tex(summary):
    by = {(r["variant"], r["quantity"]): r for r in summary}

    def cell(variant, quantity):
        r = by[(variant, quantity)]
        d, m = int(r["denominator"]), int(r["measured"])
        if d == 0 or m == 0:
            return "not measurable"
        if r["satisfied"] != "":
            return f"{r['satisfied']} of {d}"
        if r["total"] != "":
            return r["total"] + ("" if m == d else f" ({m} of {d})")
        out = f"{num(r['median'])} ({num(r['min'])}--{num(r['max'])})"
        return out + ("" if m == d else f", {m} of {d}")

    labels = [
        ("continuity verdict", "Continuity criterion satisfied"),
        ("state at window start: armed, no failsafe, Hold", "Armed in Hold, no failsafe, at the window start"),
        ("first navigation change after the window start", "First navigation change: return to launch"),
        ("fault to return-to-launch state", "Fault to return-to-launch state (ms), median (min--max)"),
        # "status samples received" stays in the CSV only (decision of Viviana, 4 October): it counts the whole
        # recording of the observer, whose length differs between the variants, not the continuity
        ("longest status gap in the recording", "Longest status gap in the recording (ms), median (min--max)"),
        ("status gaps reaching the limit and touching the window", "Status gaps of 5~s or more overlapping the window"),
        ("PX4 clock regressions in the window", "PX4 clock regressions in the window"),
        ("PX4 clock regressions in the recording", "PX4 clock regressions in the whole recording"),
        ("unexpected state changes in the window", "State changes before the return to launch"),
        ("observer restarts", "Restarts of the status observer"),
    ]
    den = {v: by[(v, "continuity verdict")]["denominator"] for v in ("a", "b")}
    lines = [f"{label} & {cell('a', q)} & {cell('b', q)} \\\\" for q, label in labels]
    return ("% GENERATED by scripts/thesis_e1_continuity_tables.py from data/measured/e1_continuity.csv\n"
            "\\begin{tabular}{@{}>{\\raggedright\\arraybackslash}p{6.6cm}"
            ">{\\raggedright\\arraybackslash}p{3.4cm}>{\\raggedright\\arraybackslash}p{3.4cm}@{}}\n\\toprule\n"
            f"\\textbf{{Quantity}} & \\shortstack[l]{{\\textbf{{A}}\\\\({den['a']} valid runs)}} & "
            f"\\shortstack[l]{{\\textbf{{B}}\\\\({den['b']} valid runs)}} \\\\\n\\midrule\n"
            + "\n".join(lines) + "\n\\bottomrule\n\\end{tabular}\n")


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
    runs_csv, sum_csv = os.path.join(measured, "e1_continuity_runs.csv"), os.path.join(measured, "e1_continuity.csv")
    write_csv(runs_csv, RUN_FIELDS, run_rows(a.campaign))
    write_csv(sum_csv, SUM_FIELDS, summary_rows(read_csv(runs_csv)))
    with open(os.path.join(tables, "e1_continuity.tex"), "w") as f:
        f.write(tex(read_csv(sum_csv)))
    print("e1_continuity")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
