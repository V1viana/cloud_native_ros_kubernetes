#!/usr/bin/env python3
"""Verdict of the SECOND S3 sampler pilot (docs/S3_SAMPLER_PILOT_2_PREREGISTRATION.md),
criteria and margins fixed before the data. Pilot 1's verdicts are untouched
(s3_sampler_pilot_eval.py).

  s3_sampler_pilot2_eval.py CAMPAIGN_DIR    -> CAMPAIGN_DIR/pilot2-verdict.json

CAMPAIGN_DIR/runs/<NN>/ (one per row of ORDER, written by s3_sampler_pilot2.py):
  run.json        {"n", "variant", "pair", "incident_observer", "blocks", "exit", "s3_result",
                   "runner_end", "t0", "incident_end", "interrupted", "status"}
  samples.jsonl, account.jsonl, observer-cpu.json   (incident-ON runs only)
  hold.jsonl      {"t", "cluster_present"} after the runner's end, to T0 + 185 s
  blocks.jsonl    one line per block: {"i", "condition": "S"|"A", "start": EDGE, "end": EDGE,
                   "memory": [bytes...], "account": [entries] (A only)}
                   EDGE = {"t", "read": read_once_http line (no workload), "cgroup":
                   {"cpu_usage_usec", "memory_current"}}
"""

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import metrics_sampler as ms  # noqa: E402
import r14_stats  # noqa: E402

L_SEC = 180.0
C0_LEAD_SEC = 60.0
C1_INTERVAL_SEC, C1_SHARE, C1_SCRAPE_P95_SEC, C1_MAX_FAILED = 1.5, 0.95, 1.0, 2
C5_MAX_SHARE = 0.10                    # same value as pilot 1, now measured (Viviana: never raised)
C5X_REL, C5X_ABS = 0.20, 0.2           # cross-check: within 20% of K1 or within 0.2 req/s
C6_MAX_CORES = 0.25
MARGIN = {"D1": 0.05, "D2": 0.2}       # core; requests/s
D3_MARGIN_REL = 0.05                   # of the variant's mean S-block etcd write rate
MIN_COVERAGE = 0.95
D3_COUNTERS = ("etcd_mvcc_put_total", "etcd_mvcc_delete_total")   # etcd writes (L3)

# The order table of the preregistration (section 5), fixed for every run [D].
ORDER = [
    (1, 1, "a", 1, "A", "SAAS"), (2, 1, "a", 1, "S", "ASSA"),
    (3, 1, "b", 1, "A", "SAAS"), (4, 1, "b", 1, "S", "ASSA"),
    (5, 2, "b", 2, "S", "SAAS"), (6, 2, "b", 2, "A", "ASSA"),
    (7, 2, "a", 2, "S", "SAAS"), (8, 2, "a", 2, "A", "ASSA"),
    (9, 3, "a", 3, "A", "SAAS"), (10, 3, "a", 3, "S", "ASSA"),
    (11, 3, "b", 3, "A", "SAAS"), (12, 3, "b", 3, "S", "ASSA"),
]   # (run, round, variant, pair, observer during the incident A=on/S=off, block order)


def p95(values):
    ordered = sorted(values)
    return ordered[max(0, -(-95 * len(ordered) // 100) - 1)] if ordered else None


def incident_criteria(samples, account, t0, hold, run, cpu):
    """C0-C4, C7 as pilot 1; C5' and C6' measured (sections 3 and 6)."""
    t_end = t0 + L_SEC
    c = {}
    ok = [s for s in samples if not s.get("error")]
    first_ok = min((ms._api_at(s)[1] for s in ok), default=None)
    c["C0"] = {"pass": first_ok is not None and first_ok <= t0 - C0_LEAD_SEC,
               "first_successful_read_before_t0_s": None if first_ok is None else round(t0 - first_ok, 1)}
    span = [s for s in samples if t0 - 2 <= s["t_start"] <= t_end + 2]
    starts = sorted(s["t_start"] for s in span)
    intervals = [b - a for a, b in zip(starts, starts[1:])]
    share = sum(1 for i in intervals if i <= C1_INTERVAL_SEC) / len(intervals) if intervals else 0.0
    durations = [s["scrape"]["duration_s"] for s in span if not s.get("error") and s.get("scrape")]
    failed = sum(1 for s in span if s.get("error"))
    c["C1"] = {"pass": bool(intervals) and share >= C1_SHARE and durations != []
               and p95(durations) <= C1_SCRAPE_P95_SEC and failed <= C1_MAX_FAILED,
               "intervals_within_1_5s": round(share, 4), "attempts": len(span),
               "scrape_p95_s": p95(durations), "failed_reads": failed}
    window = ms.window(samples, t0, t_end)
    c["C2"] = {"pass": window["measurable"] and "L0" in window.get("deltas", {}), "reasons": window["reasons"]}
    l3 = window.get("L3")
    c["C3"] = {"pass": l3 is not None and l3["measurable"], "reasons": None if l3 is None else l3["reasons"]}
    checks = [h for h in hold if h["t"] <= t_end]
    runner_end = run.get("runner_end")
    covered = runner_end is not None and (runner_end >= t_end or (
        checks and all(h["cluster_present"] for h in checks) and max(h["t"] for h in hold) >= t_end))
    c["C4"] = {"pass": bool(covered), "hold_checks": len(hold),
               "cluster_absent_checks": sum(1 for h in checks if not h["cluster_present"])}
    k1 = sum(1 for e in account if e.get("counted") and t0 <= e["t_start"] <= t_end)
    l0 = window.get("deltas", {}).get("L0") if window["measurable"] else None
    share5 = k1 / l0 if l0 else None
    c["C5'"] = {"pass": share5 is not None and share5 <= C5_MAX_SHARE, "observer_counted_requests": k1,
                "l0_gross": l0, "share": None if share5 is None else round(share5, 4)}
    cores = cpu["cpu_s"] / cpu["wall_s"] if cpu and cpu.get("wall_s") and cpu.get("cpu_s") is not None else None
    c["C6'"] = {"pass": cores is not None and cores <= C6_MAX_CORES, "cores": None if cores is None else round(cores, 4),
                "processes": (cpu or {}).get("processes")}
    c["C7"] = {"pass": run.get("exit") == 0 and run.get("s3_result") == "true",
               "exit": run.get("exit"), "s3_result": run.get("s3_result")}
    c["C8"] = {"recorded": window.get("reset_detection")}
    rate = lambda d, sp: None if d is None or not sp else round(d / sp, 4)
    api_span = None
    if window["measurable"]:
        e = window["edges"]
        api_span = (sum(e["end_scrape"]) - sum(e["start_scrape"])) / 2
    return {"criteria": c, "window": window, "measured_span_s": api_span,
            "rates_per_s": ({k: rate(v, api_span) for k, v in window["deltas"].items()} if api_span else None),
            "bytes": window.get("bytes")}


def _edge_check(a, b):
    """Block quantities are differences between the two edge reads (an S block has, by design,
    no read in between, so the 1 Hz gap rule does not apply): same API server start, no
    L0/L3 series down or gone between the edges, families present."""
    reasons = []
    ra, rb = a["read"], b["read"]
    if ra.get("error") or rb.get("error"):
        return [f"edge read failed: {ra.get('error') or rb.get('error')}"]
    if ra["apiserver_start"] != rb["apiserver_start"]:
        reasons.append("API server restarted inside the block")
    for name in ("apiserver_request_total", "etcd_l3"):
        if not (ra["families"].get(name) and rb["families"].get(name)):
            reasons.append(f"family absent at an edge: {name}")
    for layer in ("L0", "L3"):
        sa, sb = ra.get(layer) or {}, rb.get(layer) or {}
        if any(k not in sb or sb[k] < sa[k] for k in sa):
            reasons.append(f"{layer}: a series went down or disappeared between the edges")
    if a["cgroup"] is None or b["cgroup"] is None:
        reasons.append("server cgroup not read")
    return reasons


def _observer_coverage(block):
    """An A block is valid only if its sampler really read throughout it: first successful
    read within GAP_SEC of the start edge, last within GAP_SEC of the end edge, no gap above
    GAP_SEC between successful reads. A sampler that died early would otherwise make the block
    look valid with almost no cost and no disturbance (review of Viviana, 1 October)."""
    t_a, t_b = ms._api_at(block["start"]["read"])[0], ms._api_at(block["end"]["read"])[0]
    times = sorted(r["scrape"]["start"] for r in block.get("observer_reads") or []
                   if not r.get("error") and r.get("scrape"))
    if not times:
        return ["A block: the observer has no successful read"]
    reasons = []
    if times[0] > t_a + ms.GAP_SEC:
        reasons.append(f"A block: first observer read {times[0] - t_a:.1f}s after the start edge")
    if times[-1] < t_b - ms.GAP_SEC:
        reasons.append(f"A block: last observer read {t_b - times[-1]:.1f}s before the end edge")
    inside = [t for t in times if t_a - ms.GAP_SEC <= t <= t_b + ms.GAP_SEC]
    gaps = [b - a for a, b in zip(inside, inside[1:]) if b - a > ms.GAP_SEC]
    if gaps:
        reasons.append(f"A block: gap of {max(gaps):.1f}s between observer reads")
    return reasons


def block_quantities(block):
    a, b = block["start"], block["end"]
    reasons = _edge_check(a, b)
    if block.get("condition") == "A" and not reasons:
        reasons = _observer_coverage(block)
    if reasons:
        return {"valid": False, "reasons": reasons}
    dt_api = ms._api_at(b["read"])[0] - ms._api_at(a["read"])[0]
    dt_etcd = b["read"]["etcd_scrape"]["start"] - a["read"]["etcd_scrape"]["start"]
    dt_cg = b["t"] - a["t"]
    l0 = sum(b["read"]["L0"].values()) - sum(a["read"]["L0"].values())
    k1 = sum(1 for e in block.get("account") or [] if e.get("counted")
             and ms._api_at(a["read"])[0] <= e["t_start"] <= ms._api_at(b["read"])[0])
    l3 = sum(b["read"]["L3"][k] - a["read"]["L3"][k] for k in D3_COUNTERS)
    mem = block.get("memory") or []
    return {"valid": True, "reasons": [], "condition": block["condition"],
            "D1_cores": (b["cgroup"]["cpu_usage_usec"] - a["cgroup"]["cpu_usage_usec"]) / 1e6 / dt_cg,
            "D2_net_requests_per_s": (l0 - k1) / dt_api,
            "D3_etcd_writes_per_s": l3 / dt_etcd,
            "D4_memory_mean": sum(mem) / len(mem) if mem else None, "D4_memory_max": max(mem) if mem else None,
            "l0_gross_per_s": l0 / dt_api, "observer_counted_per_s": k1 / dt_api,
            "durations_s": {"api": dt_api, "etcd": dt_etcd, "cgroup": dt_cg}}


def cluster_contrast(blocks):
    """ONE contrast per cluster [D]: mean of the two A blocks minus mean of the two S blocks."""
    q = [block_quantities(b) for b in blocks]
    if len(q) != 4 or not all(x["valid"] for x in q) or sorted(x["condition"] for x in q) != ["A", "A", "S", "S"]:
        return {"valid": False, "blocks": q}
    mean = lambda cond, key: sum(x[key] for x in q if x["condition"] == cond) / 2
    out = {"valid": True, "blocks": q}
    for key, name in (("D1_cores", "D1"), ("D2_net_requests_per_s", "D2"), ("D3_etcd_writes_per_s", "D3")):
        out[name] = mean("A", key) - mean("S", key)
        out[f"{name}_S_mean"] = mean("S", key)
    out["C5x"] = {"l0_gross_contrast": mean("A", "l0_gross_per_s") - mean("S", "l0_gross_per_s"),
                  "k1_A": mean("A", "observer_counted_per_s")}
    diff = out["C5x"]["l0_gross_contrast"] - out["C5x"]["k1_A"]
    out["C5x"].update({"difference": diff, "pass": abs(diff) <= max(C5X_REL * out["C5x"]["k1_A"], C5X_ABS)})
    return out


def margin_verdict(contrasts, margin):
    """Margin DEMONSTRATED only with a valid interval (exact coverage >= 95%) lying entirely in
    [-margin, +margin] [D]. With n = 6 that interval is [min, max] of the contrasts."""
    hl = r14_stats.pseudomedian_interval(contrasts)
    valid = len(contrasts) == 6 and hl["coverage_exact"] and hl["coverage"] is not None and hl["coverage"] >= MIN_COVERAGE
    out = {"n": len(contrasts), "pseudomedian": hl["hodges_lehmann_pseudomedian"], "low": hl["low"],
           "high": hl["high"], "coverage": hl["coverage"], "coverage_exact": hl["coverage_exact"],
           "interval_valid": valid, "margin": margin}
    if margin is None:
        out.update({"status": "NOT_ESTIMABLE", "why": "relative margin undefined (S-block reference is zero)"})
    elif not valid:
        out.update({"status": "NOT_ESTIMABLE", "why": "interval not valid (fewer than 6 clusters, ties or "
                                                      "zeros, or coverage below 95%): not called CI95"})
    elif -margin <= hl["low"] and hl["high"] <= margin:
        out["status"] = "WITHIN_MARGIN"
    else:
        out["status"] = "OUTSIDE_MARGIN"
    out["scope"] = ("pseudomedian under its assumptions (continuous, symmetric differences); not a general "
                    "proof of no perturbation; does not validate N=10")
    return out


def evaluate(campaign):
    """campaign: {"runs": [{"run": run.json, "incident": incident_criteria(...) or None,
    "blocks": [...]}]} -> the verdict of section 6."""
    runs = {r["run"]["n"]: r for r in campaign["runs"]}
    out = {"order_matches_preregistration": [
        (r["run"]["n"], r["run"]["variant"], r["run"]["pair"], r["run"]["incident_observer"], r["run"]["blocks"])
        for r in campaign["runs"]] == [(n, v, p, o, b) for n, _, v, p, o, b in ORDER],
        "runs": {}, "variants": {}}
    for n, r in runs.items():
        out["runs"][n] = {"run": r["run"], "incident": r.get("incident"), "contrast": cluster_contrast(r["blocks"])}
    decision_reasons = []
    for v in ("a", "b"):
        vr = [out["runs"][n] for n in sorted(out["runs"]) if out["runs"][n]["run"]["variant"] == v]
        contrasts = [x["contrast"] for x in vr if x["contrast"]["valid"]]
        res = {}
        for name in ("D1", "D2"):
            res[name] = margin_verdict([c[name] for c in contrasts], MARGIN[name])
        reference = (sum(c["D3_S_mean"] for c in contrasts) / len(contrasts)) if contrasts else None
        d3_margin = D3_MARGIN_REL * reference if reference else None
        res["D3"] = margin_verdict([c["D3"] for c in contrasts], d3_margin)
        res["D3"].update({"reference_S_mean_writes_per_s": reference,
                          "absolute_difference_reported": True})
        res["C5x"] = [c["C5x"] for c in contrasts]
        on = [x for x in vr if x["run"]["incident_observer"] == "A"]
        res["incident_criteria"] = {x["run"]["n"]: {k: c.get("pass") for k, c in x["incident"]["criteria"].items()}
                                    for x in on if x["incident"]}
        res["D_S3"] = {"on_runs_expected_verdict": all(x["run"].get("s3_result") == "true" for x in on),
                       "incident_s": {x["run"]["n"]: (x["run"].get("incident_end", 0) - x["run"].get("t0", 0))
                                      if x["run"].get("incident_end") else None for x in vr},
                       "s3_result": {x["run"]["n"]: x["run"].get("s3_result") for x in vr}}
        out["variants"][v] = res
        for n, crit in res["incident_criteria"].items():
            failed = [k for k, p in crit.items() if p is False]
            if failed:
                decision_reasons.append(f"{v} run {n}: {failed} failed")
        if len(on) != 3 or any(x["incident"] is None for x in on):
            decision_reasons.append(f"{v}: incident criteria missing for an observer-on run")
        if not all(c["pass"] for c in res["C5x"]) or len(res["C5x"]) != 6:
            decision_reasons.append(f"{v}: cross-check C5'' failed or incomplete")
        if not res["D_S3"]["on_runs_expected_verdict"]:
            decision_reasons.append(f"{v}: an S3 verdict differs from the expected one with the observer on")
    statuses = [out["variants"][v][d]["status"] for v in ("a", "b") for d in ("D1", "D2", "D3")]
    if not out["order_matches_preregistration"]:
        decision = "INVALID: executed order differs from the preregistered table"
    elif decision_reasons or "OUTSIDE_MARGIN" in statuses:
        decision = "CRITERION_FAILED: diagnosis first, then Viviana decides"
    elif "NOT_ESTIMABLE" in statuses:
        decision = ("DISTURBANCE_NOT_ESTIMABLE: declared as a limit; the measure is NOT called validated; "
                    "no automatic move to 5 pairs (Viviana decides)")
    else:
        decision = ("ALL_CRITERIA_MET: adoptable for R14 with measured cost and disturbance declared; scope "
                    "N=3, the first N=10 pair repeats the coverage check; the gaps towards the proposal stay declared")
    out.update({"decision": decision, "decision_reasons": decision_reasons,
                "bytes": "descriptive, separate, never summed; WATCH counters raw data only"})
    return out


def load_campaign(campaign_dir):
    runs = []
    for name in sorted(os.listdir(os.path.join(campaign_dir, "runs"))):
        d = os.path.join(campaign_dir, "runs", name)
        run = json.load(open(os.path.join(d, "run.json")))
        incident = None
        if run["incident_observer"] == "A" and os.path.exists(os.path.join(d, "samples.jsonl")):
            hold = ms.load(os.path.join(d, "hold.jsonl")) if os.path.exists(os.path.join(d, "hold.jsonl")) else []
            cpu = json.load(open(os.path.join(d, "observer-cpu.json"))) \
                if os.path.exists(os.path.join(d, "observer-cpu.json")) else None
            incident = incident_criteria(ms.load(os.path.join(d, "samples.jsonl")),
                                         ms.load(os.path.join(d, "account.jsonl")), run["t0"], hold, run, cpu)
        blocks = ms.load(os.path.join(d, "blocks.jsonl")) if os.path.exists(os.path.join(d, "blocks.jsonl")) else []
        runs.append({"run": run, "incident": incident, "blocks": blocks})
    return {"runs": runs}


def main(campaign_dir):
    result = evaluate(load_campaign(campaign_dir))
    json.dump(result, open(os.path.join(campaign_dir, "pilot2-verdict.json"), "w"), indent=1)
    print(json.dumps({"decision": result["decision"], "reasons": result["decision_reasons"],
                      **{f"{v}_{d}": result["variants"][v][d]["status"] for v in ("a", "b") for d in ("D1", "D2", "D3")}}))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1]))
