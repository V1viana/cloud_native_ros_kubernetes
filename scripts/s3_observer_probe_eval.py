#!/usr/bin/env python3
"""Judge of the targeted S3 observer probe (docs/S3_OBSERVER_PROBE_PREREGISTRATION_DRAFT.md):
the "edges only" rule E, the empirical cost gate, part A (descriptive), B2/B3, and the
zero/tie rule on integer counts for future data. Pilot 2's judge is untouched.

  s3_observer_probe_eval.py PROBE_DIR     -> PROBE_DIR/probe-verdict.json
"""

from fractions import Fraction
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import metrics_sampler as ms  # noqa: E402

L_SEC = 180.0
EDGE_SEC = 2.0
GATE_MARGIN = 0.05                      # core, the D1 margin of pilot 2
DURATION_TOL_SEC = 0.1                  # zero/tie on counts only within this tolerance [D]
OFFSET_TOL_SEC = 0.05                   # gate: each etcd start read within this of B1's offset (half the burst period)
PLAN = [   # (run, variant, cluster, part A order, gate order: A = observer E on, S = off)
    (1, "a", 1, "NMEWWEMN", "ASSA"), (2, "b", 1, "NMEWWEMN", "ASSA"),
    (3, "a", 2, "WEMNNMEW", "SAAS"), (4, "b", 2, "WEMNNMEW", "SAAS"),
]
RESTART = "restart: API server process start time changed"
RESTART_ETCD = "restart: etcd process start time changed"


# ---- the rule E ----

def _down_or_gone(start, end):
    return sorted(k for k in start if k not in end or end[k] < start[k])


def e_window(api_start, etcd_start, api_end, etcd_end, t_from, t_to, start_time_ref, workload_end,
             etcd_start_ref=None, edge_sec=EDGE_SEC):
    """Measurable only if: start edges ended by t_from (within edge_sec), end edges started at or
    after t_to (within edge_sec); the API server's start time identical between the last observer
    read before t_from (start_time_ref) and the end edge (and etcd's, if exposed); no L0/L1/L2/L3
    series lower or absent at the end; families present; workload present at the end.
    api_start: {"t_end", "L0", "L1", "L2", "families"}; etcd_start/etcd_end: read_etcd lines;
    api_end: read_api line."""
    reasons = []
    if api_start is None:
        reasons.append("L0-L2: no start edge")
    elif not (api_start["t_end"] <= t_from and t_from - api_start["t_end"] <= edge_sec):
        reasons.append(f"L0-L2: start edge {t_from - api_start['t_end']:.3f}s before the window (limit 0..{edge_sec})")
    if etcd_start is None or etcd_start.get("error"):
        reasons.append("L3: no start edge before the window")
    elif not (etcd_start["t_end"] <= t_from and t_from - etcd_start["t_end"] <= edge_sec):
        reasons.append(f"L3: start edge {t_from - etcd_start['t_end']:.3f}s before the window (limit 0..{edge_sec})")
    if api_end is None or api_end.get("error"):
        reasons.append(f"L0-L2: end edge failed: {None if api_end is None else api_end.get('error')}")
        api_end = None
    elif not (api_end["scrape"]["start"] >= t_to and api_end["scrape"]["start"] - t_to <= edge_sec):
        reasons.append(f"L0-L2: end edge {api_end['scrape']['start'] - t_to:.3f}s after the window (limit 0..{edge_sec})")
    if etcd_end is None or etcd_end.get("error"):
        reasons.append("L3: end edge failed")
        etcd_end = None
    elif not (etcd_end["t_start"] >= t_to and etcd_end["t_start"] - t_to <= edge_sec):
        reasons.append(f"L3: end edge {etcd_end['t_start'] - t_to:.3f}s after the window (limit 0..{edge_sec})")
    # restarts: an ABSENT start time is a distinct reason (rule not applicable), never a change,
    # so it can never count as B2's detected restart (review of Viviana)
    if start_time_ref is None:
        reasons.append("restart rule not applicable: no API server start time before the window")
    elif api_end is not None and api_end.get("apiserver_start") is None:
        reasons.append("restart rule not applicable: API server start time absent at the end edge")
    elif api_end is not None and api_end["apiserver_start"] != start_time_ref:
        reasons.append(RESTART)
    if etcd_end is not None:            # etcd's own start time is mandatory in this probe [D]
        if etcd_start_ref is None:
            reasons.append("restart rule not applicable: no etcd start time before the window")
        elif etcd_end.get("etcd_start") is None:
            reasons.append("restart rule not applicable: etcd start time absent at the end edge")
        elif etcd_end["etcd_start"] != etcd_start_ref:
            reasons.append(RESTART_ETCD)
    deltas = {}
    if api_start is not None and api_end is not None:
        for layer in ("L0", "L1", "L2"):
            gone = _down_or_gone(api_start[layer], api_end[layer])
            if gone:
                reasons.append(f"{layer}: series lower or absent at the end edge: {gone[:3]}")
            deltas[layer] = sum(api_end[layer].values()) - sum(api_start[layer].values())
        if not (api_start["families"].get("apiserver_request_total") and api_start["families"].get("etcd_requests_total")
                and api_end["families"].get("apiserver_request_total") and api_end["families"].get("etcd_requests_total")):
            reasons.append("L0-L2: a required family is absent at an edge")
    if etcd_start is not None and not etcd_start.get("error") and etcd_end is not None:
        gone = _down_or_gone(etcd_start["L3"], etcd_end["L3"])
        if gone:
            reasons.append(f"L3: series lower or absent at the end edge: {gone}")
        if not (etcd_start["families"]["etcd_l3"] and etcd_end["families"]["etcd_l3"]):
            reasons.append("L3: etcd counters absent at an edge")
        deltas["L3"] = {k: etcd_end["L3"].get(k, 0.0) - etcd_start["L3"].get(k, 0.0) for k in etcd_end["L3"]}
    if workload_end is None or workload_end.get("error") or not workload_end.get("namespace_present") \
            or not workload_end.get("deployments"):
        reasons.append("workload not present at the end edge")
    out = {"measurable": not reasons, "reasons": reasons, "deltas": deltas if not reasons else None}
    if not reasons:
        api_span = api_end["scrape"]["start"] - api_start["t_end"]
        etcd_span = etcd_end["t_start"] - etcd_start["t_end"]
        out["spans_s"] = {"api": api_span, "etcd": etcd_span}
        out["rates_per_s"] = {**{k: deltas[k] / api_span for k in ("L0", "L1", "L2")},
                              "L3": {k: v / etcd_span for k, v in deltas["L3"].items()}}
    return out


def b1(run, snapshot, burst_reads, api_end, etcd_end, workload_end, api_ready_read, scrape_uncertainty_s):
    """The S3 window with E: start edge of L0/L1/L2 = the runner's own snapshot; of L3 = the last
    burst read, which must have ENDED before T0; no burst read may overlap the runner's pre-T0
    snapshot or end after T0 (otherwise L3 is not measurable)."""
    t0 = run["t0"]
    bad = [r for r in burst_reads if r.get("overlap") or r.get("t_end", 0) >= t0]
    ok = [r for r in burst_reads if not r.get("error") and r["t_end"] < t0]
    etcd_start = ok[-1] if ok and not bad else None
    api_start = None if snapshot is None else {**snapshot, "t_end": snapshot["t"]}
    ref = None if api_ready_read is None or api_ready_read.get("error") else api_ready_read.get("apiserver_start")
    w = e_window(api_start, etcd_start, api_end, etcd_end, t0, t0 + run.get("window_sec", L_SEC), ref, workload_end,
                 etcd_start_ref=None if etcd_start is None else etcd_start.get("etcd_start"))
    if bad:
        w["reasons"].insert(0, f"L3: {len(bad)} burst read(s) overlapped the runner's pre-T0 snapshot or ended after T0")
        w["measurable"], w["deltas"] = False, None
    w.update({"burst": {"reads": len(burst_reads), "successful_before_t0": len(ok), "overlapping": len(bad),
                        "last_end_before_t0_s": None if not ok else round(t0 - ok[-1]["t_end"], 4)},
              "runner_snapshot_before_t0_s": None if snapshot is None else round(t0 - snapshot["t"], 4),
              "scrape_uncertainty_s": scrape_uncertainty_s,
              "scrape_note": "the runner's snapshot timestamp is taken after its scrape and parse: the counters "
                             "were read up to about one scrape duration earlier (estimated from the observer's "
                             "own /metrics reads in the same run)"})
    return w


def b_window(start_api, start_etcd, end_api, end_etcd, workload_end, t_from, t_to, window_sec=L_SEC):
    """B3 (no restart) and B2 (restart) on the SAME window length as E [D], with the observer's
    own reads as edges; the start-time reference is the start edge's own read. A window of
    another length is refused, so B2 can only fail because of the restart it must detect."""
    api_start = None if start_api.get("error") else {**start_api, "t_end": start_api["scrape"]["end"]}
    ref = None if start_api.get("error") else start_api.get("apiserver_start")
    w = e_window(api_start, None if start_etcd.get("error") else start_etcd, end_api, end_etcd, t_from, t_to,
                 ref, workload_end, etcd_start_ref=None if start_etcd.get("error") else start_etcd.get("etcd_start"))
    if abs((t_to - t_from) - window_sec) > 1e-6:
        w["reasons"].insert(0, f"B window of {t_to - t_from:.3f}s instead of {window_sec}s")
        w["measurable"], w["deltas"] = False, None
    return w


# ---- cost: gate (direct) and part A (descriptive) ----

def cores(block):
    a, b = block["start"]["cgroup"], block["end"]["cgroup"]
    if a is None or b is None:
        return None
    return (b["cpu_usage_usec"] - a["cpu_usage_usec"]) / 1e6 / (block["end"]["t"] - block["start"]["t"])


def gate(blocks, b1_offsets):
    """g = mean of the E-on blocks - mean of the E-off blocks (server CPU from the host cgroups).
    An on block reproduces E's pattern AS USED in this cluster's B1 (the number of burst reads
    and their cadence; then /metrics, etcd and the workload check at the end): it is the direct
    cost of that pattern, not a stress test (review of Viviana). Valid only if every E read
    succeeded, the burst count equals B1's and every read started within OFFSET_TOL_SEC of
    B1's offset (relative to the first read). PASS only with |g| <= 0.05 core."""
    reasons = []
    for b in blocks:
        if cores(b) is None:
            reasons.append(f"block {b['i']}: server cgroup not read")
        if b["condition"] == "A" and not b.get("e_reads_ok"):
            reasons.append(f"block {b['i']}: the E reads of an on block failed")
        if b["condition"] == "A":
            offsets = b.get("offsets") or []
            if len(offsets) != len(b1_offsets):
                reasons.append(f"block {b['i']}: {len(offsets)} etcd start reads, B1 used {len(b1_offsets)}")
            elif any(abs(x - y) > OFFSET_TOL_SEC for x, y in zip(offsets, b1_offsets)):
                worst = max(abs(x - y) for x, y in zip(offsets, b1_offsets))
                reasons.append(f"block {b['i']}: etcd start reads off B1's offsets by {worst:.3f}s "
                               f"(tolerance {OFFSET_TOL_SEC}s)")
    if len(blocks) != 4 or sorted(b["condition"] for b in blocks) != ["A", "A", "S", "S"]:
        reasons.append("gate needs 2 on and 2 off blocks")
    if reasons:
        return {"pass": False, "g": None, "reasons": reasons}
    mean = lambda c: sum(cores(b) for b in blocks if b["condition"] == c) / 2
    g = mean("A") - mean("S")
    return {"pass": abs(g) <= GATE_MARGIN, "g": g, "on": mean("A"), "off": mean("S"), "reasons": [],
            "scope": "empirical gate on 4 clusters, not a confidence interval"}


def part_a(blocks):
    """Descriptive: per source, contrast with N of the server CPU, and the cost per read."""
    by = {}
    for b in blocks:
        by.setdefault(b["condition"], []).append(b)
    valid = {c: all(cores(b) is not None and b.get("reader_ok", c == "N") for b in bs) for c, bs in by.items()}
    mean = {c: sum(cores(b) for b in bs) / len(bs) for c, bs in by.items() if valid[c]}
    freq = {"M": 1.0, "E": 1.0, "W": 0.2}
    out = {"valid": valid, "mean_cores": mean, "scope": "descriptive diagnosis of the sources, not a statistical "
                                                       "test of the candidate's cost"}
    if "N" in mean:
        out["contrast_vs_N"] = {c: mean[c] - mean["N"] for c in mean if c != "N"}
        out["core_seconds_per_read"] = {c: out["contrast_vs_N"][c] / freq[c] for c in out["contrast_vs_N"]}
    return out


# ---- zero and tie on integer counts, for future data [D] ----

def count_contrast(on_counts, off_counts):
    """Exact rational contrast of integer counts (no floating point)."""
    return Fraction(sum(on_counts), len(on_counts)) - Fraction(sum(off_counts), len(off_counts))


def zero_tie_status(contrasts, tol=DURATION_TOL_SEC):
    """contrasts: [{"k": Fraction, "durations": [s...], "nominal": s}]. Zero and tie are judged on
    the exact count contrasts ONLY if every duration involved is within tol of its nominal;
    otherwise they are NOT DETERMINABLE for that contrast -- never float equality of rates [D].
    tie: |k| equal to the |k| of another non-zero contrast; None when an equality cannot be
    excluded because some other contrast is not determinable."""
    ok = [all(abs(d - c["nominal"]) <= tol for d in c["durations"]) for c in contrasts]
    out = []
    for i, c in enumerate(contrasts):
        if not ok[i]:
            out.append({"determinable": False, "zero": None, "tie": None, "why": "block durations outside the tolerance"})
            continue
        if c["k"] == 0:
            out.append({"determinable": True, "zero": True, "tie": False})
            continue
        others = [j for j in range(len(contrasts)) if j != i]
        if any(ok[j] and contrasts[j]["k"] != 0 and abs(contrasts[j]["k"]) == abs(c["k"]) for j in others):
            tie = True
        elif any(not ok[j] for j in others):
            tie = None
        else:
            tie = False
        out.append({"determinable": True, "zero": False, "tie": tie})
    return out


# ---- decision ----

def b2_detected(b2):
    """B2 proves the restart rule only if BOTH start times changed (API server and etcd, the
    latter mandatory [D]), no "rule not applicable" or window-length reason, and docker restart
    exited 0 (review of Viviana)."""
    return bool(b2 and not b2["measurable"] and RESTART in b2["reasons"] and RESTART_ETCD in b2["reasons"]
                and not any(x.startswith("restart rule not applicable") or x.startswith("B window of")
                            for x in b2["reasons"])
                and (b2.get("restart") or {}).get("rc") == 0)


def evaluate(probe):
    """probe: {"runs": [{"run", "b1", "gate", "part_a", "b3", "b2"}], "preflight": {...}}"""
    runs = probe["runs"]
    out = {"plan_matches_preregistration": [(r["run"]["n"], r["run"]["variant"], r["run"]["cluster"],
                                             r["run"]["part_a_order"], r["run"]["gate_order"]) for r in runs]
           == [tuple(p) for p in PLAN], "runs": {}, "reasons": []}
    for r in runs:
        n = r["run"]["n"]
        out["runs"][n] = {k: r.get(k) for k in ("b1", "gate", "part_a", "b3", "b2")}
        b1r, g, b3, b2 = r.get("b1"), r.get("gate"), r.get("b3"), r.get("b2")
        if r["run"].get("status") not in ("OK", "FUNCTIONAL_FAIL"):
            out["reasons"].append(f"run {n}: status {r['run'].get('status')}")
        if not (b1r and b1r["measurable"]):
            out["reasons"].append(f"run {n}: B1 not measurable: {None if not b1r else b1r['reasons'][:3]}")
        if not (b3 and b3["measurable"]):
            out["reasons"].append(f"run {n}: B3 (no restart) not measurable: {None if not b3 else b3['reasons'][:3]}")
        if not b2_detected(b2):
            out["reasons"].append(f"run {n}: B2 did not detect the restart: {None if not b2 else b2['reasons'][:3]}")
        if not (g and g["pass"]):
            out["reasons"].append(f"run {n}: cost gate not passed: g = {None if not g else g['g']}")
        if r["run"].get("window_sec") != L_SEC:
            out["reasons"].append(f"run {n}: window {r['run'].get('window_sec')}s, preregistered {L_SEC}s")
        if r["run"].get("s3_result") != "true":
            out["reasons"].append(f"run {n}: S3 verdict {r['run'].get('s3_result')}")
    if len(runs) != 4:
        out["reasons"].append(f"{len(runs)} runs instead of 4")
    if not out["plan_matches_preregistration"]:
        out["reasons"].insert(0, "executed plan differs from the preregistered one")
    adopt = not out["reasons"]
    out["decision"] = ("ADOPTABLE: S3 in R14 uses E" if adopt else
                       "NOT_ADOPTABLE: S3 stays descriptive in the reduced perimeter (5 pairs only by Viviana's decision)")
    out["scope"] = ("even if adoptable, S3 is NOT fully conformant to the proposal: writes in a fixed window after T0 "
                    "(including the post-recovery period), not 'per incident'; network bandwidth not measured; "
                    "reaction/recovery/convergence times vs N not covered; N = 3 and 10 only")
    out["bytes"] = ("content bytes ABSENT in S3: not measured by E (no start edge before T0); they are not "
                    "network bandwidth, which stays not measured")
    return out


def load_probe(probe_dir):
    """Raw files written by s3_observer_probe.py -> the inputs of evaluate()."""
    import s3_edge_observer as eo
    runs = []
    for name in sorted(os.listdir(os.path.join(probe_dir, "runs"))):
        d = os.path.join(probe_dir, "runs", name)
        run = json.load(open(os.path.join(d, "run.json")))
        entry = {"run": run}
        edges = json.load(open(os.path.join(d, "edges.json"))) if os.path.exists(os.path.join(d, "edges.json")) else {}
        api_reads = [x for x in (edges.get("api_ready"), edges.get("api_end")) if x and not x.get("error")]
        uncertainty = max((x["scrape"]["end"] - x["scrape"]["start"] for x in api_reads), default=None)
        if run.get("t0") is not None and edges:
            entry["burst_reads"] = ms.load(os.path.join(d, "burst.jsonl")) if os.path.exists(
                os.path.join(d, "burst.jsonl")) else []
            snap_dir = os.path.join(d, "runner-result")
            snapshot = eo.runner_snapshot(snap_dir) if os.path.exists(os.path.join(snap_dir, eo.BEFORE_INCIDENT)) else None
            entry["b1"] = b1(run, snapshot, entry["burst_reads"], edges.get("api_end"), edges.get("etcd_end"),
                edges.get("workload_end"), edges.get("api_ready"), uncertainty)
        for key, fn in (("gate", gate), ("part_a", part_a)):
            path = os.path.join(d, f"{key}.jsonl")
            if os.path.exists(path):
                burst_reads = entry.get("burst_reads", [])
                b1_offsets = [r["t_start"] - burst_reads[0]["t_start"] for r in burst_reads]
                entry[key] = fn(ms.load(path), b1_offsets) if key == "gate" else fn(ms.load(path))
        for key in ("b3", "b2"):
            path = os.path.join(d, f"{key}.json")
            if os.path.exists(path):
                x = json.load(open(path))
                entry[key] = b_window(x["start_api"], x["start_etcd"], x["end_api"], x["end_etcd"],
                                      x["workload_end"], x["t_from"], x["t_to"], run.get("window_sec", L_SEC))
                entry[key]["restart"] = x.get("restart")
        runs.append(entry)
    return {"runs": runs}


def main(probe_dir):
    result = evaluate(load_probe(probe_dir))
    json.dump(result, open(os.path.join(probe_dir, "probe-verdict.json"), "w"), indent=1, default=str)
    print(json.dumps({"decision": result["decision"], "reasons": result["reasons"]}))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1]))
