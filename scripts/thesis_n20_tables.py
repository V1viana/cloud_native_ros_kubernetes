#!/usr/bin/env python3
"""Thesis data of the separate diagnosis at twenty robots (block of six planned attempts), copied from the
block's own records: the planned order (manifest.json), the status of each attempt (block.json, cell.json)
and the facts recorded by the runner (failure-summary.json, the KubeROS bootstrap Job, the readiness series
of B, the runner output). Nothing is estimated and no cause is inferred: the table says where an attempt
stopped, not why. Every CSV row carries `source` (paths from `results/` on, or from the worktree of the
block for the runner's files).

Usage: python3 scripts/thesis_n20_tables.py --block BLOCK_DIR --out DATA_DIR
(writes DATA_DIR/measured/s3_n20_block.csv and DATA_DIR/tables/s3_n20_block.tex)
"""
import argparse
import csv
import json
import os
import re
from datetime import datetime

FIELDS = ["attempt", "variant", "status", "valid", "elapsed_s", "robots_expected", "robots_with_pods",
          "robots_all_pods_ready", "deployments", "job_failed_after_s", "b_barrier_s", "b_readiness_wait_s",
          "b_ever_ready", "image_import_errors", "recorded_deviation", "source"]


def rel(path):
    """From `results/` on for the repository's results; the full path for the runner's files, which live in
    the frozen worktree of the block."""
    path = os.path.abspath(path)
    marker = "/cloud_native_ros_kubernetes/results/"
    return path[path.index(marker) + len("/cloud_native_ros_kubernetes/"):] if marker in path else path


def load(path):
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def ts(text):
    return datetime.fromisoformat(text.replace("Z", "+00:00"))


def job_failed_after(summary):
    for job in (summary or {}).get("jobs") or []:
        failed = [c for c in job.get("conditions") or [] if c.get("type") == "Failed"]
        if job.get("failed") and failed and job.get("start"):
            return (ts(failed[0]["lastTransitionTime"]) - ts(job["start"])).total_seconds()
    return None


def readiness(result_dir):
    path = os.path.join(result_dir, "bootstrap-readiness.jsonl")
    if not os.path.isfile(path):
        return None, None
    rows = [json.loads(line) for line in open(path) if line.strip()]
    if not rows:
        return None, None
    return (ts(rows[-1]["timestamp"]) - ts(rows[0]["timestamp"])).total_seconds(), any(r.get("ready") for r in rows)


def barrier(result_dir):
    for name in sorted(os.listdir(result_dir)) if os.path.isdir(result_dir) else []:
        p = os.path.join(result_dir, name)
        if os.path.isfile(p) and name.endswith(".txt"):
            m = re.search(r"^b_bootstrap_barrier_timeout_sec=(\d+)", open(p, errors="replace").read(), re.M)
            if m:
                return int(m.group(1))
    return None


def deviations(block):
    """Attempts named in the hand-written register of deviations: in each '## N.' section, the line
    '**Cella interessata:** cella K'. A section without that line names no attempt."""
    path = os.path.join(block, "DEVIATIONS.md")
    if not os.path.isfile(path):
        return {}
    out = {}
    for section in re.split(r"^(?=## )", open(path).read(), flags=re.M):
        head = re.match(r"## (\d+)\.", section)
        cell = re.search(r"\*\*Cella interessata:\*\*\s*cella\s+(\d+)", section)
        if head and cell:
            out.setdefault(int(cell.group(1)), []).append(f"DEVIATIONS.md {head.group(1)}")
    return out


def rows_of(block):
    manifest = load(os.path.join(block, "manifest.json"))
    status = {c["n"]: c for c in (load(os.path.join(block, "block.json")) or {}).get("cells", [])}
    dev = deviations(block)
    out = []
    for n, variant in manifest["order"]:
        cdir = os.path.join(block, "cells", f"{n:02d}-{variant}")
        row = {k: "" for k in FIELDS}
        row.update(attempt=n, variant=variant, robots_expected=manifest["robots"],
                   recorded_deviation="; ".join(dev.get(n, [])))
        if n not in status:
            row.update(status="NOT_RUN", valid=False, source=rel(os.path.join(block, "block.json")) + "#/cells (absent)")
            out.append(row)
            continue
        st = status[n]["status"]
        cell = load(os.path.join(cdir, "cell.json")) or {}
        rd = cell.get("result_dir") or ""
        summary = load(os.path.join(rd, "failure-summary.json"))
        fleet = (summary or {}).get("fleet") or {}
        wait, ever = readiness(rd)
        runner_out = os.path.join(cdir, "runner.out")
        imports = len(re.findall(r"failed to import images", open(runner_out, errors="replace").read())) \
            if os.path.isfile(runner_out) else ""
        row.update(status=st.split(":")[0], valid=st in ("PASS", "FAIL"), elapsed_s=cell.get("elapsed_sec", ""),
                   robots_with_pods=fleet.get("robots_with_pods", ""),
                   robots_all_pods_ready=fleet.get("robots_all_pods_ready", ""),
                   deployments=((summary or {}).get("deployments") or {}).get("total", ""),
                   job_failed_after_s="" if job_failed_after(summary) is None else job_failed_after(summary),
                   b_barrier_s="" if variant != "b" or barrier(rd) is None else barrier(rd),
                   b_readiness_wait_s="" if wait is None else wait, b_ever_ready="" if ever is None else ever,
                   image_import_errors=imports,
                   source=f"{rel(os.path.join(block, 'block.json'))}#/cells/{n - 1}; {rel(os.path.join(cdir, 'cell.json'))}; "
                          f"{rel(os.path.join(rd, 'failure-summary.json'))}")
        out.append(row)
    return out


def where(r):
    if r["status"] == "NOT_RUN":
        return "not run: the block stopped after the previous attempt"
    if r["status"] == "FAIL" and r["job_failed_after_s"] != "":
        return f"bootstrap Job of KubeROS failed after {float(r['job_failed_after_s']):.0f}~s"
    if r["status"] == "FAIL" and r["variant"] == "b" and r["b_ever_ready"] == "False" and r["b_barrier_s"] != "":
        return f"fleet not ready within {int(r['b_barrier_s'])}~s"
    if r["status"] == "INCONCLUSIVE" and str(r["deployments"]) == "0" and r["image_import_errors"] not in ("", "0"):
        return "cluster preparation: image import failed on a node; no workload deployed"
    return r["status"].lower()


def tex(rows):
    lines = []
    for r in rows:
        outcome = {"FAIL": "FAIL", "INCONCLUSIVE": "inconclusive", "NOT_RUN": "not run", "PASS": "PASS"}[r["status"]]
        deployed = r["status"] in ("FAIL", "PASS") or str(r["deployments"]) not in ("", "0")
        pods = f"{r['robots_with_pods']}" if deployed else "--"
        ready = f"{r['robots_all_pods_ready']}" if deployed else "--"
        lines.append(f"{r['attempt']} & {r['variant'].upper()} & {outcome} & {where(r)} & {pods} & {ready} \\\\")
    return ("% GENERATED by scripts/thesis_n20_tables.py from data/measured/s3_n20_block.csv\n"
            "\\begin{tabular}{@{}rlp{1.8cm}p{6.0cm}rr@{}}\n\\toprule\n"
            "\\textbf{Attempt} & \\textbf{Variant} & \\textbf{Outcome} & \\textbf{Where it stopped} & "
            "\\textbf{Robots with Pods} & \\textbf{All Pods Ready} \\\\\n\\midrule\n"
            + "\n".join(lines) + "\n\\bottomrule\n\\end{tabular}\n")


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--block", required=True)
    ap.add_argument("--out", required=True)
    a = ap.parse_args(argv)
    measured, tables = os.path.join(a.out, "measured"), os.path.join(a.out, "tables")
    os.makedirs(measured, exist_ok=True)
    os.makedirs(tables, exist_ok=True)
    path = os.path.join(measured, "s3_n20_block.csv")
    with open(path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS, lineterminator="\n")
        w.writeheader()
        w.writerows(rows_of(a.block))
    with open(path, newline="") as f:
        rows = list(csv.DictReader(f))
    with open(os.path.join(tables, "s3_n20_block.tex"), "w") as f:
        f.write(tex(rows))
    print("s3_n20_block")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
