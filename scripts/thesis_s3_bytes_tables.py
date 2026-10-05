#!/usr/bin/env python3
"""Content bytes of the API server in S3: classification against the fixed window of the load rates.

The fixed window starts at the runner's snapshot taken just before the injection (metrics-before-incident.json,
the start edge of the request rates) and ends at the observer's read after 180 s (edges.json api_end). Content
bytes are measurable in that window only if BOTH edges carry the byte counters. Decision of Viviana
(4 October 2026): when they do not, every execution is "not measurable in the fixed window", with its source
and reason; no substitute value, neither zero nor an estimate, and no volume over another window (the counters
read earlier, at api_ready, stay in the raw results). This tool never computes a byte volume.

Denominator: the S3 executions of the calendar (scripts/r14_schedule.py).
Usage: python3 scripts/thesis_s3_bytes_tables.py --campaign CAMPAIGN_DIR --out DATA_DIR
"""
import argparse
import csv
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import r14_schedule as rs  # noqa: E402

SNAPSHOT = "metrics-before-incident.json"
GROUPS = ("request_body", "response", "watch_events")
FIELDS = ["seq", "fleet_size", "pair", "variant", "status", "reason", "byte_counters_at_start_edge",
          "byte_counters_at_end_edge", "first_byte_read_before_start_edge_s", "source"]
SUM_FIELDS = ["fleet_size", "variant", "planned", "valid", "not_measurable_in_fixed_window", "other", "source"]
NOT_MEASURABLE = "not measurable in the fixed window"


def rel(path):
    """From `results/` on for the repository's results; the full path for the runner's files (frozen worktree)."""
    path = os.path.abspath(path)
    marker = "/cloud_native_ros_kubernetes/results/"
    return path[path.index(marker) + len("/cloud_native_ros_kubernetes/"):] if marker in path else path


def load(path):
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def has_bytes_snapshot(snapshot):
    return any("size" in name or "watch_events" in name for name in (snapshot.get("counters") or {}))


def has_bytes_read(read):
    return bool(read) and not read.get("error") and all((read.get("families") or {}).get(f"bytes:{g}") for g in GROUPS)


def rows_of(campaign):
    rows = []
    for e in [e for e in rs.build() if e["config"] in ("s3-n3", "s3-n10")]:
        run_dir = os.path.join(campaign, "runs", f"{e['seq']:03d}-{e['config']}-{e['variant']}")
        row = {"seq": e["seq"], "fleet_size": 3 if e["config"] == "s3-n3" else 10, "pair": e["pair"],
               "variant": e["variant"], "byte_counters_at_start_edge": "", "byte_counters_at_end_edge": "",
               "first_byte_read_before_start_edge_s": ""}
        rec = load(os.path.join(run_dir, "run.json"))
        if rec is None:
            rows.append({**row, "status": "not run", "reason": "execution not recorded",
                         "source": rel(os.path.join(run_dir, "run.json"))})
            continue
        if rec.get("valid") is not True:
            rows.append({**row, "status": "not valid", "reason": f"execution not valid ({rec.get('verdict')})",
                         "source": rel(os.path.join(run_dir, "run.json")) + "#/valid"})
            continue
        snap_path = os.path.join(rec.get("result_dir") or "", SNAPSHOT)
        edges_path = os.path.join(run_dir, "edges.json")
        snap, edges = load(snap_path), load(edges_path)
        source = f"{rel(snap_path)}#/counters; {rel(edges_path)}#/api_end/families; {rel(edges_path)}#/api_ready/scrape/start"
        if snap is None or edges is None:
            rows.append({**row, "status": NOT_MEASURABLE, "reason": "an edge of the window is missing", "source": source})
            continue
        at_start, at_end = has_bytes_snapshot(snap), has_bytes_read(edges.get("api_end"))
        ready = edges.get("api_ready") or {}
        lead = ""
        if has_bytes_read(ready) and snap.get("timestamp_utc") is not None:
            lead = round(snap["timestamp_utc"] - ready["scrape"]["start"], 1)
        row.update(byte_counters_at_start_edge=at_start, byte_counters_at_end_edge=at_end,
                   first_byte_read_before_start_edge_s=lead, source=source)
        if at_start and at_end:
            rows.append({**row, "status": "counters at both edges",
                         "reason": "not extracted by this tool (decision of 4 October: classification only)"})
            continue
        missing = [n for n, ok in (("start edge (runner snapshot before the injection)", at_start),
                                   ("end edge (observer read after 180 s)", at_end)) if not ok]
        reason = "no content-byte counters at the " + " and at the ".join(missing)
        if lead != "":
            reason += f"; byte counters were read only {lead} s before the start edge and at the end edge"
        rows.append({**row, "status": NOT_MEASURABLE, "reason": reason})
    return rows


def summary_rows(rows):
    out = []
    for size in (3, 10):
        for v in ("a", "b"):
            rs_ = [r for r in rows if r["fleet_size"] == str(size) and r["variant"] == v]
            valid = [r for r in rs_ if r["status"] not in ("not run", "not valid")]
            out.append({"fleet_size": size, "variant": v, "planned": len(rs_), "valid": len(valid),
                        "not_measurable_in_fixed_window": sum(r["status"] == NOT_MEASURABLE for r in valid),
                        "other": sum(r["status"] != NOT_MEASURABLE for r in valid),
                        "source": f"data/measured/s3_bytes_runs.csv: fleet_size={size}, variant={v}"})
    return out


def tex(summary):
    lines = []
    for r in summary:
        valid, nm = int(r["valid"]), int(r["not_measurable_in_fixed_window"])
        state = "not measurable" if valid and nm == valid else f"not measurable in {nm} of {valid}"
        lines.append(f"{r['fleet_size']} & {r['variant'].upper()} & {r['planned']} & {r['valid']} & {state} \\\\")
    return ("% GENERATED by scripts/thesis_s3_bytes_tables.py from data/measured/s3_bytes.csv\n"
            "\\begin{tabular}{@{}>{\\raggedright\\arraybackslash}p{1.6cm}>{\\raggedright\\arraybackslash}p{1.4cm}"
            ">{\\raggedright\\arraybackslash}p{1.6cm}>{\\raggedright\\arraybackslash}p{1.6cm}"
            ">{\\raggedright\\arraybackslash}p{5.6cm}@{}}\n\\toprule\n"
            "\\shortstack[l]{\\textbf{Fleet}\\\\\\textbf{size}} & \\textbf{Variant} & "
            "\\shortstack[l]{\\textbf{Planned}\\\\\\textbf{runs}} & \\shortstack[l]{\\textbf{Valid}\\\\\\textbf{runs}} & "
            "\\shortstack[l]{\\textbf{Content bytes of the API server}\\\\\\textbf{in the fixed window}} \\\\\n"
            "\\midrule\n" + "\n".join(lines) + "\n\\bottomrule\n\\end{tabular}\n")


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
    runs_csv, sum_csv = os.path.join(measured, "s3_bytes_runs.csv"), os.path.join(measured, "s3_bytes.csv")
    write_csv(runs_csv, FIELDS, rows_of(a.campaign))
    write_csv(sum_csv, SUM_FIELDS, summary_rows(read_csv(runs_csv)))
    # no table for the thesis (decision of Viviana, 4 October): a sentence there, these CSV files for traceability
    print("s3_bytes")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
