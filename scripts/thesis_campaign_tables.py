#!/usr/bin/env python3
"""Thesis data for the paired A/B campaign, copied from the final tables of scripts/r14_tables.py.

Only values already computed in tables.json are copied; nothing is recomputed, rounded in the
CSV, or estimated. Every CSV row carries `source`: the path of tables.json from `results/` on,
followed by the JSON pointer of the object the row comes from. The LaTeX fragments are generated
from the CSV files only, as in scripts/thesis_extract_results.py.

Usage: python3 scripts/thesis_campaign_tables.py --tables TABLES_JSON --out DATA_DIR
(writes DATA_DIR/measured/campaign_*.csv and DATA_DIR/tables/campaign_*.tex)
"""
import argparse
import csv
import json
import os

VARIANTS = ("a", "b")

# Configurations in the order of the chapter; label as printed in the thesis.
LABEL = {
    "e0": "E0", "e1": "E1", "e2": "E2", "p2": "P2", "e4": "E4", "u1": "U1", "u2": "U2",
    "s1-delete": "S1, deletion", "s2-short": "S2, short", "s2-long": "S2, long",
    "s2-control": "S2, control", "s2-partition-only": "S2, partition only",
    "s3-n3": "S3, $N=3$", "s3-n10": "S3, $N=10$", "s4-l3": "S4, three faults",   # descriptive names only:
    "s4-l1-battery": "S4, battery fault alone", "s4-l1-telemetry": "S4, telemetry fault alone",  # the internal
    "s4-l1-edge": "S4, edge fault alone", "ttr": "Time to rebuild",                     # cell names stay in the CSV
}
RUN_CRITERION = {  # field of tables.json with the per-run criterion, where one exists
    "e0": "functional_pass", "e1": "functional_pass", "e2": "functional_pass", "p2": "functional_pass",
    "e4": "functional_pass", "u1": "functional_pass", "u2": "functional_pass",
    "s2-short": "verdict_pass", "s2-long": "verdict_pass", "s2-control": "verdict_pass",
    "s2-partition-only": "verdict_pass", "s3-n3": "verdict_pass", "s3-n10": "verdict_pass",
}
BINARY = [  # configuration, primary outcome (thesis wording)
    ("e4", "Rollback completed"), ("u1", "Update applied"), ("u2", "Rollback completed"),
    ("s1-delete", "Readiness restored within 180~s"),
    ("s2-short", "Incident recognized within 180~s"), ("s2-long", "Incident recognized within 180~s"),
    ("s4-l3", "All properties of the cell satisfied"),
    ("s4-l1-battery", "All properties of the cell satisfied"),
    ("s4-l1-telemetry", "All properties of the cell satisfied"),
    ("s4-l1-edge", "All properties of the cell satisfied"),
]
DURATIONS = [  # configuration, path in the configuration, quantity, unit of the data, kind
    ("e1", ("latency_fault_to_rtl_command_ms",), "Fault to return-to-launch command", "ms", "describe"),
    ("e1", ("latency_command_to_ack_ms",), "Command to acknowledgement", "ms", "describe"),
    ("e2", ("mttr",), "Recovery declared by the variant", "s", "mttr"),
    ("p2", ("convergence_time_ms",), "Observed duration", "ms", "describe"),
    ("e4", ("convergence_time_ms",), "Observed duration", "ms", "describe"),
    ("u1", ("convergence_time_ms",), "Observed duration", "ms", "describe"),
    ("u1", ("reconciliation_churn_u1u2",), "Reconciliation churn", "count", "describe"),
    ("u2", ("convergence_time_ms",), "Observed duration", "ms", "describe"),
    ("s1-delete", ("block", "per_variant"), "Readiness restored", "s", "s1-readiness"),
    ("s1-delete", ("block", "per_variant"), "Service available", "s", "s1-service"),
    ("s4-l3", ("returns_secondary", "telemetry.returned_after_t0_sec", "mttr"), "Telemetry returned", "s", "mttr"),
    ("s4-l3", ("returns_secondary", "edge_service.first_positive_after_start_sec", "mttr"),
     "Edge service returned", "s", "mttr"),
    ("s4-l1-telemetry", ("returns_secondary", "telemetry.returned_after_t0_sec", "mttr"),
     "Telemetry returned", "s", "mttr"),
    ("s4-l1-edge", ("returns_secondary", "edge_service.first_positive_after_start_sec", "mttr"),
     "Edge service returned", "s", "mttr"),
    ("ttr", ("all_valid_runs_calendar",), "Time to rebuild", "s", "mttr"),
]
# How a quantity is printed: unit shown, divisor from the unit of the data, decimals.
SHOWN = {"Observed duration": ("s", 1000.0, 1)}
SHOWN_BY_UNIT = {"ms": ("ms", 1.0, 1), "s": ("s", 1.0, 1), "count": ("count", 1.0, 0)}  # ms: medians of 10 can be x.5
S3_UNIT = "requests/s"
S3_LEVEL = {"L0": "API requests", "L1": "API write requests", "L2": "Storage write requests",
            "L3": "etcd writes"}


def source_of(tables_path, pointer):
    path = os.path.abspath(tables_path)
    cut = path.find(os.sep + "results" + os.sep)
    rel = path[cut + 1:] if cut >= 0 else path
    return rel + "#/" + "/".join(str(p) for p in pointer)


def ratio(text):
    k, n = text.split("/")
    return int(k), int(n)


def dig(obj, path):
    for p in path:
        obj = obj[p]
    return obj


def write_csv(path, fields, rows):
    with open(path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields, lineterminator="\n")
        w.writeheader()
        w.writerows(rows)


def runs_rows(conf, src):
    rows = []
    for key in LABEL:
        if key not in conf:
            raise KeyError(f"configuration {key} missing from tables.json")
        c = conf[key]
        if key == "s1-delete":
            for v in VARIANTS:
                d = c["campaign_rows"][v]
                rows.append({"config": key, "block": "campaign", "variant": v, "expected": d["expected"],
                             "valid": d["ok"], "invalid": d["invalid"], "halted": d["halted"],
                             "missing": d["missing"], "incomplete": d["incomplete"], "defect": d["defect"],
                             "criterion_k": "", "criterion_n": "",
                             "source": src(("configurations", key, "campaign_rows", v))})
            for v in VARIANTS:
                d = c["block"]["per_variant"][v]
                rows.append({"config": key, "block": "s1-block", "variant": v, "expected": d["rows_expected"],
                             "valid": d["valid"], "invalid": len(d["invalid"]), "halted": 0,
                             "missing": len(d["missing"]), "incomplete": len(d["incomplete"]),
                             "defect": len(d["defects"]), "criterion_k": "", "criterion_n": "",
                             "source": src(("configurations", key, "block", "per_variant", v))})
            continue
        crit = RUN_CRITERION.get(key)
        for v in VARIANTS:
            d = c["denominators"][v]
            k = n = ""
            if crit:
                k, n = c[crit][v]["k"], c[crit][v]["n"]
            rows.append({"config": key, "block": "campaign", "variant": v, "expected": d["expected"],
                         "valid": d["ok"], "invalid": d["invalid"], "halted": d["halted"], "missing": d["missing"],
                         "incomplete": d["incomplete"], "defect": d["defect"], "criterion_k": k, "criterion_n": n,
                         "source": src(("configurations", key, "denominators", v))})
    return rows


def binary_rows(conf, src):
    rows = []
    for key, outcome in BINARY:
        path = ("configurations", key, "block", "primary") if key == "s1-delete" else ("configurations", key, "primary")
        p = dig({"configurations": conf}, path)
        ak, an = ratio(p["success"]["a"])
        bk, bn = ratio(p["success"]["b"])
        t = p["table"]
        rows.append({"config": key, "outcome": outcome, "pairs_expected": p.get("pairs_expected", p["pairs"]),
                     "pairs_used": p["pairs_used"], "pairs_excluded": len(p["pairs_excluded"]),
                     "pairs_incomplete": len(p["pairs_incomplete"]), "a_k": ak, "a_n": an, "b_k": bk, "b_n": bn,
                     "both_success": t["both_success"], "a_only": t["a_only"], "b_only": t["b_only"],
                     "both_failure": t["both_failure"], "discordant": p["discordant"],
                     "p_two_sided": "" if p["p_two_sided"] is None else p["p_two_sided"],
                     "p_min_possible": "" if p["p_min_possible"] is None else p["p_min_possible"],
                     "source": src(path)})
    return rows


def wilcoxon_rows(conf, src):
    rows = []
    t = conf["ttr"]["analysis"]
    w, hl = t["wilcoxon"], t["pseudomedian_difference"]
    rows.append({"config": "ttr", "n_robots": "", "level": "", "measure": "Time to rebuild", "unit": "s",
                 "pairs_used": len(t["pairs_used"]), "pairs_excluded": len(t["pairs_excluded"]),
                 "zeros": len(t["pairs_zero_difference"]), "w_plus": w["w_plus"], "p_two_sided": w["p_two_sided"],
                 "p_min_possible": w["p_min_possible"], "p_holm": "", "reject_at_alpha": "",
                 "pseudomedian": hl["hodges_lehmann_pseudomedian"], "low": hl["low"], "high": hl["high"],
                 "coverage": hl["coverage"], "is_ci95": hl["is_ci95"],
                 "median_a": t["per_variant"]["a"]["observed_times_all_valid_runs"]["median"],
                 "median_b": t["per_variant"]["b"]["observed_times_all_valid_runs"]["median"],
                 "source": src(("configurations", "ttr", "analysis"))})
    for i, s in enumerate(conf["s3"]["family"]["tests"]):
        w, hl, d = s["wilcoxon"], s["pseudomedian"], s["describe"]["all_valid_runs"]
        rows.append({"config": "s3", "n_robots": s["n"], "level": s["level"], "measure": S3_LEVEL[s["level"]],
                     "unit": S3_UNIT, "pairs_used": s["pairs_used"], "pairs_excluded": len(s["excluded"]),
                     "zeros": s["zeros"], "w_plus": w["w_plus"], "p_two_sided": w["p_two_sided"],
                     "p_min_possible": w["p_min_possible"], "p_holm": s["p_holm"],
                     "reject_at_alpha": s["reject_at_alpha"], "pseudomedian": hl["hodges_lehmann_pseudomedian"],
                     "low": hl["low"], "high": hl["high"], "coverage": hl["coverage"], "is_ci95": hl["is_ci95"],
                     "median_a": d["a"]["median"], "median_b": d["b"]["median"],
                     "source": src(("configurations", "s3", "family", "tests", i))})
    return rows


def duration_rows(conf, src):
    rows = []
    for key, path, quantity, unit, kind in DURATIONS:
        base = dig(conf[key], path)
        for v in VARIANTS:
            pointer = ("configurations", key) + path + (v,)
            unknown = 0
            if kind == "describe":
                d, observed, censored, denom = base[v], base[v]["n"], 0, conf[key]["denominators"][v]["ok"]
            elif kind == "mttr":
                m = base[v]
                d, observed, denom = m["describe_observed"], m["observed"], m["denominator_valid_runs"]
                censored = m.get("censored", 0)
                unknown = m.get("recovery_unknown_valid_fail", 0)  # E2: a valid FAIL, recovery not known
            else:  # s1-readiness, s1-service: the S1 block
                m = base[v]
                field = "readiness" if kind == "s1-readiness" else "service"
                d = m[f"{field}_times_s"] or {"n": 0, "mean": None, "median": None, "p95_nearest_rank": None,
                                              "min": None, "max": None}
                observed, censored, denom = d["n"], m[f"{field}_censored"], m["valid"]
            rows.append({"config": key, "quantity": quantity, "unit": unit, "variant": v, "valid_runs": denom,
                         "observed": observed, "censored": censored, "recovery_unknown": unknown,
                         **{f: ("" if d[f] is None else d[f]) for f in ("median", "min", "max", "mean")},
                         "source": src(pointer)})
    return rows


def property_rows(conf, src):
    rows = []
    for key in ("s4-l3", "s4-l1-battery", "s4-l1-telemetry", "s4-l1-edge"):
        for prop, per in sorted(conf[key]["properties"].items()):
            for v in VARIANTS:
                counts = per[v]
                rows.append({"config": key, "property": prop, "variant": v, "ok": counts.get("ok", 0),
                             "fail": counts.get("fail", 0),
                             "other": json.dumps({k: n for k, n in counts.items() if k not in ("ok", "fail")},
                                                 sort_keys=True),
                             "source": src(("configurations", key, "properties", prop, v))})
    return rows


PAIRED_SECONDARY = [  # configuration, key under returns_secondary, measure: per-pair values, side by side
    ("s4-l3", "telemetry.returned_after_t0_sec", "Telemetry returned"),
    ("s4-l3", "edge_service.first_positive_after_start_sec", "Edge service returned"),
    ("s4-l1-telemetry", "telemetry.returned_after_t0_sec", "Telemetry returned"),
    ("s4-l1-edge", "edge_service.first_positive_after_start_sec", "Edge service returned"),
]


def pair_rows(conf, src):
    """Per-pair values: the S4 returns (secondary, side by side; 'censored' kept as a state, never a
    time) and the B-A differences of time to rebuild that entered the test."""
    rows = []
    for key, measure, label in PAIRED_SECONDARY:
        side = conf[key]["returns_secondary"][measure]["pairs_side_by_side"]
        diffs = conf[key]["returns_secondary"][measure]["paired_differences_b_minus_a"]
        for pair in sorted(side, key=int):
            values = {}
            for v in VARIANTS:
                x = side[pair].get(v)
                values[v] = ("", "missing") if x is None else ("", x) if isinstance(x, str) else (x, "observed")
            rows.append({"config": key, "measure": label, "unit": "s", "pair": int(pair),
                         "a": values["a"][0], "a_state": values["a"][1], "b": values["b"][0], "b_state": values["b"][1],
                         "difference_b_minus_a": diffs.get(pair, ""),
                         "source": src(("configurations", key, "returns_secondary", measure, "pairs_side_by_side", pair))})
    for i, d in enumerate(conf["ttr"]["analysis"]["pairs_used"]):
        rows.append({"config": "ttr", "measure": "Time to rebuild", "unit": "s", "pair": d["pair"], "a": "",
                     "a_state": "", "b": "", "b_state": "", "difference_b_minus_a": d["difference_b_minus_a"],
                     "source": src(("configurations", "ttr", "analysis", "pairs_used", i))})
    return rows


# ---- LaTeX, from the CSV files only ----

def num(x, digits):
    value = float(x)
    text = f"{abs(value):.{digits}f}"
    return ("$-$" + text) if value < 0 and float(text) != 0 else text


def pval(x):
    return "--" if x in ("", None) else f"{float(x):.3g}"


def read_csv(path):
    with open(path, newline="") as f:
        return list(csv.DictReader(f))


def header(name, source):
    return f"% GENERATED by scripts/thesis_campaign_tables.py from data/measured/{source}\n"


def tex_runs(rows):
    by = {}
    for r in rows:
        by.setdefault((r["config"], r["block"]), {})[r["variant"]] = r
    lines = []
    for (key, block), per in by.items():
        a, b = per["a"], per["b"]
        label = LABEL[key] + (" (separate block)" if block == "s1-block" else "")
        crit = ("-- & --" if a["criterion_n"] == ""
                else f"{a['criterion_k']}/{a['criterion_n']} & {b['criterion_k']}/{b['criterion_n']}")
        excluded = []
        for r in (a, b):
            if int(r["invalid"]):
                excluded.append(f"{r['variant'].upper()}: {r['invalid']} invalid")
            if int(r["halted"]):
                excluded.append(f"{r['variant'].upper()}: {r['halted']} not run")
            for f in ("missing", "incomplete", "defect"):
                if int(r[f]):
                    excluded.append(f"{r['variant'].upper()}: {r[f]} {f}")
        lines.append(f"{label} & {a['expected']} & {a['valid']} & {b['valid']} & {crit} & "
                     f"{'; '.join(excluded) or '--'} \\\\")
    return ("\\begin{tabular}{@{}p{3.6cm}rrrrrp{3.4cm}@{}}\n\\toprule\n"
            "\\textbf{Configuration} & \\textbf{Planned} & \\multicolumn{2}{c}{\\textbf{Valid runs}} & "
            "\\multicolumn{2}{c}{\\textbf{Criteria met}} & \\textbf{Not counted} \\\\\n"
            " & \\textbf{per variant} & \\textbf{A} & \\textbf{B} & \\textbf{A} & \\textbf{B} & \\\\\n\\midrule\n"
            + "\n".join(lines) + "\n\\bottomrule\n\\end{tabular}\n")


def tex_binary(rows):
    lines = []
    for r in rows:
        used = r["pairs_used"] + ("" if r["pairs_used"] == r["pairs_expected"] else f" of {r['pairs_expected']}")
        lines.append(f"{LABEL[r['config']]} & {r['outcome']} & {used} & {r['a_k']}/{r['a_n']} & "
                     f"{r['b_k']}/{r['b_n']} & {r['a_only']} & {r['b_only']} & {pval(r['p_two_sided'])} \\\\")
    return ("\\begin{tabular}{@{}p{3.2cm}p{3.6cm}rrrrrr@{}}\n\\toprule\n"
            "\\textbf{Configuration} & \\textbf{Primary outcome} & \\textbf{Pairs} & \\textbf{A} & \\textbf{B} & "
            "\\textbf{A only} & \\textbf{B only} & \\textbf{Exact $p$} \\\\\n\\midrule\n"
            + "\n".join(lines) + "\n\\bottomrule\n\\end{tabular}\n")


def tex_wilcoxon(rows):
    lines = []
    for r in rows:
        digits = 1 if r["unit"] == "s" else 2
        what = (f"{r['measure']} (s)" if r["config"] == "ttr"
                else f"$N={r['n_robots']}$, {r['level']}: {r['measure']}")
        mark = "" if r["is_ci95"] == "True" else "\\textsuperscript{*}"
        lines.append(f"{what} & {r['pairs_used']} & {num(r['median_a'], digits)} & {num(r['median_b'], digits)} & "
                     f"{num(r['pseudomedian'], digits)} [{num(r['low'], digits)}, {num(r['high'], digits)}]{mark} & "
                     f"{pval(r['p_two_sided'])} & {pval(r['p_holm'])} \\\\")
    return ("\\begin{tabular}{@{}p{5.0cm}rrrlrr@{}}\n\\toprule\n"
            "\\textbf{Measure} & \\textbf{Pairs} & \\textbf{Median A} & \\textbf{Median B} & "
            "\\textbf{B$-$A, 95\\% interval} & \\textbf{Exact $p$} & \\textbf{Holm $p$} \\\\\n\\midrule\n"
            + "\n".join(lines) + "\n\\bottomrule\n\\end{tabular}\n")


def shown(r):
    return SHOWN.get(r["quantity"], SHOWN_BY_UNIT[r["unit"]])


def cell(r):
    extra = [f"{r[k]} {w}" for k, w in (("censored", "censored"), ("recovery_unknown", "recovery not known"))
             if int(r[k])]
    if int(r["observed"]) == 0:
        return f"none observed in {r['valid_runs']} valid runs" + (f" ({', '.join(extra)})" if extra else "")
    _, divisor, digits = shown(r)
    f = lambda k: num(float(r[k]) / divisor, digits)
    return f"{f('median')} ({f('min')}--{f('max')}), $n={r['observed']}$" + "".join(", " + e for e in extra)


def tex_durations(rows):
    by = {}
    for r in rows:
        by.setdefault((r["config"], r["quantity"]), {})[r["variant"]] = r
    lines = []
    for (key, quantity), per in by.items():
        lines.append(f"{LABEL[key]} & {quantity} ({shown(per['a'])[0]}) & {cell(per['a'])} & {cell(per['b'])} \\\\")
    return ("\\begin{tabular}{@{}p{2.6cm}p{3.8cm}p{4.0cm}p{4.0cm}@{}}\n\\toprule\n"
            "\\textbf{Configuration} & \\textbf{Quantity} & \\textbf{A: median (min--max)} & "
            "\\textbf{B: median (min--max)} \\\\\n\\midrule\n"
            + "\n".join(lines) + "\n\\bottomrule\n\\end{tabular}\n")


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--tables", required=True)
    ap.add_argument("--out", required=True)
    a = ap.parse_args(argv)
    conf = json.load(open(a.tables))["configurations"]
    src = lambda pointer: source_of(a.tables, pointer)
    measured, tables = os.path.join(a.out, "measured"), os.path.join(a.out, "tables")
    os.makedirs(measured, exist_ok=True)
    os.makedirs(tables, exist_ok=True)
    plan = [
        ("campaign_runs", runs_rows, ["config", "block", "variant", "expected", "valid", "invalid", "halted",
                                      "missing", "incomplete", "defect", "criterion_k", "criterion_n", "source"],
         tex_runs),
        ("campaign_binary", binary_rows, ["config", "outcome", "pairs_expected", "pairs_used", "pairs_excluded",
                                          "pairs_incomplete", "a_k", "a_n", "b_k", "b_n", "both_success", "a_only",
                                          "b_only", "both_failure", "discordant", "p_two_sided", "p_min_possible",
                                          "source"], tex_binary),
        ("campaign_wilcoxon", wilcoxon_rows, ["config", "n_robots", "level", "measure", "unit", "pairs_used",
                                              "pairs_excluded", "zeros", "w_plus", "p_two_sided", "p_min_possible",
                                              "p_holm", "reject_at_alpha", "pseudomedian", "low", "high", "coverage",
                                              "is_ci95", "median_a", "median_b", "source"], tex_wilcoxon),
        ("campaign_durations", duration_rows, ["config", "quantity", "unit", "variant", "valid_runs", "observed",
                                               "censored", "recovery_unknown", "median", "min", "max", "mean",
                                               "source"], tex_durations),
        ("campaign_s4_properties", property_rows, ["config", "property", "variant", "ok", "fail", "other", "source"],
         None),
        ("campaign_pairs", pair_rows, ["config", "measure", "unit", "pair", "a", "a_state", "b", "b_state",
                                       "difference_b_minus_a", "source"], None),
    ]
    for name, extract, fields, render in plan:
        path = os.path.join(measured, name + ".csv")
        write_csv(path, fields, extract(conf, src))
        if render:
            with open(os.path.join(tables, name + ".tex"), "w") as f:
                f.write(header(name, name + ".csv") + render(read_csv(path)))
        print(name)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
