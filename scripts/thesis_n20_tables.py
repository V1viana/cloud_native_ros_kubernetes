#!/usr/bin/env python3
"""Thesis data of the separate diagnosis at twenty robots (block of six planned attempts), copied from the
block's own records: the planned order (manifest.json), the status of each attempt (block.json, cell.json)
and the facts recorded by the runner (failure-summary.json, the KubeROS bootstrap Job, the readiness series
of B, the runner output). Nothing is estimated and no cause is inferred: the table says where an attempt
stopped, not why. Every CSV row carries `source` (paths from `results/` on, or from the worktree of the
block for the runner's files).

Usage: python3 scripts/thesis_n20_tables.py --block BLOCK_DIR --out DATA_DIR
(writes DATA_DIR/measured/s3_n20_block.csv and DATA_DIR/tables/s3_n20_block.tex, and the symptoms recorded
when each attempt was collected: DATA_DIR/measured/s3_n20_symptoms.csv and DATA_DIR/tables/s3_n20_symptoms.tex)

The symptoms are copied from the same failure-summary.json, read once after the verdict of each attempt. They
are snapshots, not a time series of the bootstrap, and each row states the window its value refers to: the
load average and the CPU pressure of the host cover the minutes or seconds before the collection; restart and
cgroup counters are cumulative since the container or the Pod started; probe failures are the Kubernetes Events
still retained at the collection, so their counts are a minimum. The ratio of throttled periods is the share of
scheduling periods in which the CPU limit throttled the cgroup, not the share of CPU time withheld.
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


SYMPTOM_FIELDS = ["attempt", "variant", "quantity", "value", "value_min", "value_max", "count", "denominator",
                  "window", "collected_at_utc", "source"]
PROBE_KINDS = (("timed out", re.compile(r"deadline|SIGKILL|timed out|timeout", re.I)),
               ("node not found", re.compile(r"Node not found")),
               ("node not active", re.compile(r"expected active")))


def is_analytics(name):
    return "companion-analytics" in (name or "")


def probe_kind(message):
    for kind, pattern in PROBE_KINDS:
        if pattern.search(message or ""):
            return kind
    return "other"


def ratio(group):
    periods, throttled = group.get("nr_periods") or 0, group.get("nr_throttled") or 0
    return throttled / periods if periods else None


def throttling(pod):
    """(container ratio, its source, Pod ratio) for the analytics container of a Pod. Container: the node read
    of its own cgroup when present, else the read made inside the container; Pod: the node read of the Pod's
    cgroup (sandbox and every container, past ones included). None where nothing was read."""
    cont = next((c for c in pod.get("containers") or [] if is_analytics(c.get("name"))), None)
    cid = ((cont or {}).get("container_id") or "").split("://")[-1]
    node_c = node_p = exec_c = None
    for entry in pod.get("cgroups") or []:
        for g in entry.get("groups") or []:
            path = g.get("cgroup") or ""
            if entry.get("source") == "node":
                if cid and path.endswith("/" + cid):
                    node_c = ratio(g)
                elif re.search(r"/pod[0-9a-f-]+$", path):
                    node_p = ratio(g)
            elif entry.get("source") == "exec" and cont and entry.get("container") == cont.get("name"):
                exec_c = ratio(g)
    if node_c is not None:
        return node_c, "node", node_p
    return exec_c, ("exec" if exec_c is not None else ""), node_p


def symptom_rows(block):
    manifest = load(os.path.join(block, "manifest.json"))
    status = {c["n"]: c for c in (load(os.path.join(block, "block.json")) or {}).get("cells", [])}
    out = []
    for n, variant in manifest["order"]:
        if n not in status or not status[n]["status"].startswith(("FAIL", "PASS")):
            continue                               # no valid attempt, no deployed fleet to describe
        cdir = os.path.join(block, "cells", f"{n:02d}-{variant}")
        rd = (load(os.path.join(cdir, "cell.json")) or {}).get("result_dir") or ""
        fs_path = os.path.join(rd, "failure-summary.json")
        summary = load(fs_path) or {}
        host, fleet, src = summary.get("host") or {}, summary.get("fleet") or {}, rel(fs_path)
        at = host.get("collected_at_utc", "")

        def add(quantity, window, pointer, value="", vmin="", vmax="", count="", denominator=""):
            out.append({"attempt": n, "variant": variant, "quantity": quantity, "value": value, "value_min": vmin,
                        "value_max": vmax, "count": count, "denominator": denominator, "window": window,
                        "collected_at_utc": at, "source": f"{src}#/{pointer}"})

        load_avg = (host.get("loadavg") or "").split()
        for i, minutes in enumerate((1, 5, 15)):
            if len(load_avg) > i:
                add(f"host load average, {minutes} min", f"{minutes} min before the collection", "host/loadavg",
                    value=load_avg[i], denominator=f"{host.get('cpu_count', '')} CPU cores")
        m = re.search(r"some avg10=([\d.]+) avg60=([\d.]+) avg300=([\d.]+)", (host.get("pressure") or {}).get("cpu", ""))
        if m:
            for value, seconds in zip(m.groups(), (10, 60, 300)):
                add(f"host CPU pressure, share of time with some task waiting, {seconds} s (%)",
                    f"{seconds} s before the collection", "host/pressure/cpu", value=value)
        pods = [p for pods_ in (summary.get("robots") or {}).values() for p in pods_]
        analytics = [p for p in pods if is_analytics(p.get("pod"))]
        not_ready = [p for p in analytics if not p.get("ready")]
        add("analytics Pods not ready", "at the collection", "robots/*/pod,ready", value=len(not_ready),
            denominator=f"{len(analytics)} analytics Pods")
        add("container restarts, all Pods of the robots", "since each Pod started", "fleet/restarts_total",
            value=fleet.get("restarts_total", ""))
        add("container restarts, analytics containers", "since each Pod started",
            "robots/*/containers/restart_count",
            value=sum(c.get("restart_count") or 0 for p in analytics for c in p.get("containers") or []
                      if is_analytics(c.get("name"))), denominator=f"{len(analytics)} analytics containers")
        oom = fleet.get("oom_killed", "")
        add("containers terminated for memory (OOM)", "since each Pod started", "fleet/oom_killed",
            value=len(oom) if isinstance(oom, list) else oom)
        kinds, first, last = {}, [], []
        for p in pods:
            ev = (p.get("events") or {}).get("startup_probe") or {}
            for msg in ev.get("messages") or []:
                kinds[probe_kind(msg.get("message"))] = kinds.get(probe_kind(msg.get("message")), 0) + msg.get("count", 0)
            if ev.get("first"):
                first.append(ev["first"])
                last.append(ev["last"])
        window = (f"Events retained at the collection, {min(first)} to {max(last)} (a minimum)" if first
                  else "Events retained at the collection (a minimum)")
        for kind in [k for k, _ in PROBE_KINDS] + ["other"]:
            add(f"startup probe failures, {kind}", window, "robots/*/events/startup_probe/messages",
                value=kinds.get(kind, 0))
        for label, group in (("not ready", not_ready), ("ready", [p for p in analytics if p.get("ready")])):
            c_vals, sources, p_vals = [], [], []
            for p in group:
                c, source, pod_ratio = throttling(p)
                if c is not None:
                    c_vals.append(c)
                    sources.append(source)
                if pod_ratio is not None:
                    p_vals.append(pod_ratio)
            read = ", ".join(f"{sources.count(k)} read {w}" for k, w in (("node", "on the node"), ("exec", "inside the container")) if sources.count(k))
            add(f"throttled periods, analytics containers {label}", "since each container started",
                "robots/*/cgroups (container cgroup)",
                vmin=f"{min(c_vals):.4f}" if c_vals else "", vmax=f"{max(c_vals):.4f}" if c_vals else "",
                count=len(c_vals), denominator=f"{len(group)} analytics containers {label}" + (f" ({read})" if read else ""))
            add(f"throttled periods, Pods of the analytics containers {label}",
                "since each Pod started (sandbox and every container, past ones included)",
                "robots/*/cgroups (Pod cgroup, node read)",
                vmin=f"{min(p_vals):.4f}" if p_vals else "", vmax=f"{max(p_vals):.4f}" if p_vals else "",
                count=len(p_vals), denominator=f"{len(group)} analytics Pods {label}")
    return out


def pct(x):
    return f"{100 * float(x):.0f}"


def tex_symptoms(rows):
    attempts = sorted({(int(r["attempt"]), r["variant"]) for r in rows})
    by = {(int(r["attempt"]), r["quantity"]): r for r in rows}

    def cells(fmt):
        return " & ".join(fmt(n) for n, _ in attempts)

    def get(n, q):
        return by.get((n, q))

    def value(q):
        return lambda n: (get(n, q) or {}).get("value", "") or "--"

    def interval(q):
        def f(n):
            r = get(n, q)
            if not r or not r["count"] or r["count"] == "0":
                return "--"
            return f"{pct(r['value_min'])}--{pct(r['value_max'])} ({r['count']})"
        return f

    def not_ready(n):
        r = get(n, "analytics Pods not ready")
        return f"{r['value']} of {r['denominator'].split()[0]}" if r else "--"

    def probes(n):
        return " / ".join(value(f"startup probe failures, {k}")(n) for k in ("timed out", "node not active", "node not found"))

    def hhmm(n):
        r = get(n, "host load average, 1 min")
        return r["collected_at_utc"][11:16] if r and r["collected_at_utc"] else "--"

    lines = [("Collected at (UTC)", cells(hhmm)),
             ("Host load average, 1 min (48 cores)", cells(value("host load average, 1 min"))),
             ("Host CPU pressure, 10 s: time with some task waiting (\\%)",
              cells(value("host CPU pressure, share of time with some task waiting, 10 s (%)"))),
             ("Analytics Pods not ready", cells(not_ready)),
             ("Container restarts, all / analytics",
              cells(lambda n: f"{value('container restarts, all Pods of the robots')(n)} / "
                              f"{value('container restarts, analytics containers')(n)}")),
             ("Containers terminated for memory", cells(value("containers terminated for memory (OOM)"))),
             ("Startup probe failures: timed out / node not active / node not found", cells(probes)),
             ("Throttled periods (\\%), analytics containers not ready",
              cells(interval("throttled periods, analytics containers not ready"))),
             ("Throttled periods (\\%), their Pods",
              cells(interval("throttled periods, Pods of the analytics containers not ready"))),
             ("Throttled periods (\\%), analytics containers ready",
              cells(interval("throttled periods, analytics containers ready")))]
    head = " & ".join(f"\\textbf{{{n}, {v.upper()}}}" for n, v in attempts)
    return ("% GENERATED by scripts/thesis_n20_tables.py from data/measured/s3_n20_symptoms.csv\n"
            "\\begin{tabular}{@{}>{\\raggedright\\arraybackslash}p{4.9cm}"
            + ">{\\raggedleft\\arraybackslash}p{2.0cm}" * len(attempts) + "@{}}\n\\toprule\n"
            "\\textbf{Attempt} & " + head + " \\\\\n\\midrule\n"
            + "\n".join(f"{label} & {row} \\\\" for label, row in lines)
            + "\n\\bottomrule\n\\end{tabular}\n")


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
            "\\begin{tabular}{@{}rll>{\\raggedright\\arraybackslash}p{4.6cm}rr@{}}\n\\toprule\n"
            "\\textbf{Attempt} & \\textbf{Variant} & \\textbf{Outcome} & \\textbf{Where it stopped} & "
            "\\shortstack[r]{\\textbf{Robots}\\\\\\textbf{with Pods}} & "
            "\\shortstack[r]{\\textbf{All Pods}\\\\\\textbf{Ready}} \\\\\n\\midrule\n"
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
    path = os.path.join(measured, "s3_n20_symptoms.csv")
    with open(path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=SYMPTOM_FIELDS, lineterminator="\n")
        w.writeheader()
        w.writerows(symptom_rows(a.block))
    with open(path, newline="") as f:
        rows = list(csv.DictReader(f))
    with open(os.path.join(tables, "s3_n20_symptoms.tex"), "w") as f:
        f.write(tex_symptoms(rows))
    print("s3_n20_symptoms")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
