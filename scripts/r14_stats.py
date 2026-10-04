#!/usr/bin/env python3
"""R14 paired statistics (docs/R14_METRICS_CONTRACT_DRAFT.md 6.3; docs/R14_PREREGISTRATION_DRAFT.md 5).

  r14_stats.py PAIRS.json              -> the paired-times summary as JSON on stdout
  r14_stats.py --binary BINARY.json     -> the paired binary summary (exact McNemar)
  r14_stats.py --s3 S3.json             -> the S3 family of 8 tests with Holm (s3_family)

BINARY.json: [{"pair": id, "a": true|false|null, "b": true|false|null}, ...]; true = success
(e.g. transient recognized, readiness restored within the window), null = invalid run.
Exact McNemar (PROPOSAL approved by Viviana, 1 October): only discordant pairs inform it;
with b = A success / B failure and c = A failure / B success, p = min(1, 2 * P(X <= min(b, c)))
for X ~ Binomial(b + c, 1/2). The 2x2 table of concordant and discordant pairs is always
reported. With n discordant pairs all in one direction the smallest possible p is
2 / 2^n (10 pairs: ~0.002) -- a floor, not a result any number of runs guarantees.

PAIRS.json: [{"pair": id, "a": {"status": s, "t": seconds}, "b": {...}}, ...] with status
"observed" (t given), "censored" (no recovery within the window: never a number) or
"invalid" (not a valid run: out of every count).

Rules (fixed before the data):
- Only pairs with BOTH times observed give a difference d = t_B - t_A.
- A pair with one or two censored times is excluded from the test and reported; the
  censored times are counted as k/n of recoveries within the window, per variant, over
  the valid runs only.
- Zero differences are excluded (Wilcoxon's convention) and declared.
- Wilcoxon signed-rank, exact, two-sided: W+ = sum of the ranks of |d| of the positive
  differences; ties get average ranks and the exact distribution is the one of the 2^n
  sign assignments of those ranks (conditional on the ties), declared.
  p = min(1, 2 * min(P(W+ <= w), P(W+ >= w))). With 5 non-zero differences the smallest
  possible p is 0.0625. The p is reported as it is; no "significant".
- Always reported: every pair's difference, pairs used and excluded with the reason. A
  censored pair is listed as such: never a zero difference, never "no difference".
- Descriptives per variant, in two explicit sets: (1) every valid run with an observed
  time, INCLUDING runs whose pair is excluded from the test; (2) only the runs of the pairs
  used in the test. Each: n, mean, median, p95 (nearest rank: with n <= 20 it is the maximum,
  declared), min, max.
- Pseudomedian of the paired differences (PROPOSAL, docs/R14_PREREGISTRATION_DRAFT.md 5):
  the Hodges-Lehmann estimate (median of the M = n(n+1)/2 Walsh averages) estimates the
  PSEUDOMEDIAN, which equals the median of the differences only if their distribution is
  symmetric. The interval [W_(k), W_(M+1-k)] takes k from the exact signed-rank
  distribution; its coverage holds for continuous differences symmetric about the
  pseudomedian (an assumption, declared -- not distribution-free).
  - Without ties and without zero differences: the coverage is exact under that
    assumption; the narrowest interval with coverage >= 95% when attainable, otherwise the
    widest (the full range; with 5 pairs 93.75%). "is_ci95" only when coverage >= 95%.
  - With ties or zero differences the exact distribution does not apply: the interval is
    reported as APPROXIMATE (coverage_exact false) and is NEVER a CI95, whatever its
    nominal coverage. Zero differences enter the Walsh averages (unlike the test), declared.
"""

import itertools
import json
import math
import statistics
import sys


def ranks(values):
    """Average ranks of values (1-based), ties sharing the mean of their positions."""
    order = sorted(range(len(values)), key=lambda i: values[i])
    out = [0.0] * len(values)
    i = 0
    while i < len(order):
        j = i
        while j + 1 < len(order) and values[order[j + 1]] == values[order[i]]:
            j += 1
        for k in range(i, j + 1):
            out[order[k]] = (i + j) / 2 + 1
        i = j + 1
    return out


def wilcoxon_exact(diffs):
    nonzero = [d for d in diffs if d != 0]
    n = len(nonzero)
    if n == 0:
        return {"n": 0, "w_plus": None, "p_two_sided": None, "ties": False, "p_min_possible": None}
    r = ranks([abs(d) for d in nonzero])
    w = sum(rk for rk, d in zip(r, nonzero) if d > 0)
    totals = [sum(rk for rk, sign in zip(r, signs) if sign) for signs in itertools.product((0, 1), repeat=n)]
    eps = 1e-9
    low = sum(1 for t in totals if t <= w + eps) / len(totals)
    high = sum(1 for t in totals if t >= w - eps) / len(totals)
    return {"n": n, "w_plus": w, "p_two_sided": min(1.0, 2 * min(low, high)),
            "ties": len(set(abs(d) for d in nonzero)) < n, "p_min_possible": min(1.0, 2 / 2 ** n)}


def p95_nearest_rank(times):
    ordered = sorted(times)
    return ordered[max(0, -(-95 * len(ordered) // 100) - 1)]


def describe(times):
    if not times:
        return {"n": 0, "mean": None, "median": None, "p95_nearest_rank": None, "min": None, "max": None}
    return {"n": len(times), "mean": statistics.fmean(times), "median": statistics.median(times),
            "p95_nearest_rank": p95_nearest_rank(times), "min": min(times), "max": max(times)}


def signed_rank_cdf(n):
    """P(W+ <= w) for w = 0..n(n+1)/2 under H0, integer ranks (no ties)."""
    counts = [0] * (n * (n + 1) // 2 + 1)
    for signs in itertools.product((0, 1), repeat=n):
        counts[sum(rank for rank, sign in zip(range(1, n + 1), signs) if sign)] += 1
    total, cdf, acc = 2 ** n, [], 0
    for c in counts:
        acc += c
        cdf.append(acc / total)
    return cdf


def pseudomedian_interval(diffs, nominal=0.95):
    n = len(diffs)
    if n == 0:
        return {"n": 0, "hodges_lehmann_pseudomedian": None, "low": None, "high": None, "coverage": None,
                "coverage_exact": False, "is_ci95": False, "ties": False, "zeros": 0}
    walsh = sorted((diffs[i] + diffs[j]) / 2 for i in range(n) for j in range(i, n))
    cdf = signed_rank_cdf(n)
    k, coverage = 1, 1 - 2 * cdf[0]   # the widest interval, the full range of the Walsh averages
    for candidate in range(1, (len(walsh) + 1) // 2 + 1):   # interval [W_(k), W_(M+1-k)], coverage 1 - 2 P(W+ <= k-1)
        cov = 1 - 2 * cdf[candidate - 1]
        if cov >= nominal or candidate == 1:
            k, coverage = candidate, cov
        if cov < nominal:
            break
    ties = len(set(abs(d) for d in diffs if d != 0)) < sum(1 for d in diffs if d != 0)
    zeros = sum(1 for d in diffs if d == 0)
    exact = not ties and zeros == 0
    return {"n": n, "hodges_lehmann_pseudomedian": statistics.median(walsh),
            "low": walsh[k - 1], "high": walsh[len(walsh) - k],
            "coverage": coverage, "coverage_exact": exact, "is_ci95": exact and coverage >= nominal,
            "ties": ties, "zeros": zeros,
            "assumption": "continuous differences symmetric about the pseudomedian"}


def analyse(pairs):
    used, excluded, zeros = [], [], []
    per_variant = {"a": {"observed": [], "censored": 0, "valid": 0, "invalid": 0},
                   "b": {"observed": [], "censored": 0, "valid": 0, "invalid": 0}}
    for p in pairs:
        for v in ("a", "b"):
            run = p[v]
            if run["status"] == "invalid":
                per_variant[v]["invalid"] += 1
                continue
            per_variant[v]["valid"] += 1
            if run["status"] == "observed":
                per_variant[v]["observed"].append(run["t"])
            elif run["status"] == "censored":
                per_variant[v]["censored"] += 1
            else:
                raise ValueError(f"unknown status {run['status']!r} in pair {p['pair']}")
        statuses = (p["a"]["status"], p["b"]["status"])
        if statuses == ("observed", "observed"):
            d = p["b"]["t"] - p["a"]["t"]
            (zeros if d == 0 else used).append({"pair": p["pair"], "difference_b_minus_a": d})
        else:
            reason = ("invalid run" if "invalid" in statuses else
                      "both censored" if statuses == ("censored", "censored") else "one time censored")
            excluded.append({"pair": p["pair"], "reason": reason, "a": statuses[0], "b": statuses[1]})
    test = wilcoxon_exact([u["difference_b_minus_a"] for u in used])
    in_test = {u["pair"] for u in used} | {z["pair"] for z in zeros}
    for e in excluded:
        if "censored" in e["reason"]:
            e["note"] = "censored: not a zero difference, not interpreted as no difference"
    return {
        "pairs": len(pairs), "pairs_used": used, "pairs_zero_difference": zeros, "pairs_excluded": excluded,
        "wilcoxon": test,
        "pseudomedian_difference": pseudomedian_interval(
            [u["difference_b_minus_a"] for u in used] + [0] * len(zeros)),
        "per_variant": {v: {
            "recovered_within_window": f"{len(x['observed'])}/{x['valid']}",
            "censored": x["censored"], "invalid_runs": x["invalid"],
            "observed_times_all_valid_runs": describe(x["observed"]),
            "observed_times_pairs_in_test": describe(
                [p[v]["t"] for p in pairs if p["pair"] in in_test]),
        } for v, x in per_variant.items()},
    }


def mcnemar_exact(pairs):
    table = {"both_success": 0, "a_only": 0, "b_only": 0, "both_failure": 0}
    excluded = []
    for p in pairs:
        a, b = p["a"], p["b"]
        if a is None or b is None:
            excluded.append({"pair": p["pair"], "reason": "invalid run"})
            continue
        if not isinstance(a, bool) or not isinstance(b, bool):
            raise ValueError(f"pair {p['pair']}: outcomes must be true, false or null")
        key = "both_success" if a and b else "a_only" if a else "b_only" if b else "both_failure"
        table[key] += 1
    n_disc = table["a_only"] + table["b_only"]
    used = n_disc + table["both_success"] + table["both_failure"]
    if n_disc == 0:
        p_value = None
    else:
        k = min(table["a_only"], table["b_only"])
        tail = sum(math.comb(n_disc, i) for i in range(k + 1)) / 2 ** n_disc
        p_value = min(1.0, 2 * tail)
    return {"pairs": len(pairs), "pairs_used": used, "pairs_excluded": excluded, "table": table,
            "discordant": n_disc, "p_two_sided": p_value,
            "p_min_possible": min(1.0, 2 / 2 ** n_disc) if n_disc else None,
            "success": {"a": f"{table['both_success'] + table['a_only']}/{used}",
                        "b": f"{table['both_success'] + table['b_only']}/{used}"}}


# ---- S3: one family of 8 exact Wilcoxon tests with Holm (Viviana, 2 October) ----

S3_NS = (3, 10)
S3_LEVELS = ("L0", "L1", "L2", "L3")
S3_ALPHA = 0.05


def holm(pvalues):
    """Holm step-down adjusted p-values, in the input order, monotone and capped at 1. A test
    that cannot be computed (None) counts as p = 1: it stays in the family (conservative)."""
    m = len(pvalues)
    ps = [1.0 if p is None else p for p in pvalues]
    adjusted, running = [0.0] * m, 0.0
    for rank, i in enumerate(sorted(range(m), key=lambda j: ps[j])):
        running = max(running, min(1.0, (m - rank) * ps[i]))
        adjusted[i] = running
    return adjusted


def s3_family(pairs, alpha=S3_ALPHA):
    """pairs: [{"pair": id, "n": 3|10, "a": {"measurable": bool, "rates": {"L0".."L3": float}},
    "b": {...}}] -- rates from the edges-only observer E over the fixed window [T0, T0 + 180 s].
    For each N in (3, 10) and level in L0..L3: d = rate_B - rate_A over the pairs with BOTH
    windows measurable; exact two-sided Wilcoxon (zeros excluded and declared, ties with the
    exact conditional distribution); Hodges-Lehmann pseudomedian and its interval; per-variant
    descriptives in TWO explicit sets (section 5): (1) every valid run, i.e. every run with a
    measurable window, INCLUDING runs whose pair is excluded from the test; (2) only the runs
    of the pairs used in the test. The 8 tests are ONE family: Holm-adjusted p-values, rejection at alpha on
    the adjusted p only. Rates are continuous (counts over each window's own measured span):
    zeros and ties are exact equalities of the rates themselves."""
    tests = []
    for n_level in S3_NS:
        for level in S3_LEVELS:
            used, excluded, diffs, rates_a, rates_b = [], [], [], [], []
            valid_a, valid_b = [], []
            for p in pairs:
                if p["n"] != n_level:
                    continue
                a, b = p["a"], p["b"]
                if a.get("measurable"):
                    valid_a.append(a["rates"][level])
                if b.get("measurable"):
                    valid_b.append(b["rates"][level])
                if not (a.get("measurable") and b.get("measurable")):
                    excluded.append({"pair": p["pair"], "reason": "a window not measurable: "
                                     + ", ".join(v for v, x in (("A", a), ("B", b)) if not x.get("measurable"))})
                    continue
                d = b["rates"][level] - a["rates"][level]
                used.append({"pair": p["pair"], "a": a["rates"][level], "b": b["rates"][level], "d": d})
                diffs.append(d)
                rates_a.append(a["rates"][level])
                rates_b.append(b["rates"][level])
            w = wilcoxon_exact(diffs)
            tests.append({"n": n_level, "level": level, "pairs_used": len(used), "pairs": used, "excluded": excluded,
                          "zeros": sum(1 for d in diffs if d == 0), "wilcoxon": w,
                          "pseudomedian": pseudomedian_interval(diffs),
                          "describe": {
                              "all_valid_runs": {"a": describe(valid_a) if valid_a else None,
                                                 "b": describe(valid_b) if valid_b else None,
                                                 "note": "every run with a measurable window, including runs "
                                                         "whose pair is excluded from the test"},
                              "test_pairs": {"a": describe(rates_a) if rates_a else None,
                                             "b": describe(rates_b) if rates_b else None,
                                             "note": "only the runs of the pairs used in the test"}}})
    adjusted = holm([t["wilcoxon"]["p_two_sided"] for t in tests])
    for t, p_adj in zip(tests, adjusted):
        t["p_holm"] = p_adj
        t["reject_at_alpha"] = t["wilcoxon"]["p_two_sided"] is not None and p_adj <= alpha
    return {"family": "S3: 2 levels of N x 4 levels of measure = 8 correlated tests, Holm", "alpha": alpha,
            "tests": tests,
            "scope": ("p-values reported as they are; effects (pseudomedian, interval) always reported; two levels of N "
                      "(3 and 10) do not demonstrate a scaling law by themselves; a fixed window after T0, not 'per "
                      "incident'")}


# ---- MTTR per variant (Viviana, 2 October): an observation, never an A/B test ----

def median_interval(times, nominal=0.95):
    """Distribution-free interval for the median of the OBSERVED times: order statistics
    [x_(j), x_(n+1-j)] with exact binomial coverage 1 - 2 P(Bin(n, 1/2) <= j - 1); the largest
    j reaching `nominal`, else the widest interval with its (lower) coverage, never called CI95."""
    x = sorted(times)
    n = len(x)
    if n == 0:
        return {"low": None, "high": None, "coverage": None, "is_ci95": False}
    cdf, acc = [], 0.0
    for k in range(n + 1):
        acc += math.comb(n, k) / 2 ** n
        cdf.append(acc)
    best = 1
    for j in range(1, n // 2 + 1):
        if 1 - 2 * cdf[j - 1] >= nominal:
            best = j
    coverage = 1 - 2 * cdf[best - 1]
    return {"low": x[best - 1], "high": x[n - best], "coverage": coverage, "is_ci95": coverage >= nominal}


def mttr(runs):
    """runs: [{"status": "observed"|"censored"|"invalid", "t": seconds}] of ONE variant and
    scenario. Median and interval of the OBSERVED times only; censored runs and the denominator
    (valid runs) always reported; no observed time -> median NOT AVAILABLE (None), never
    infinite. A missing value is never a zero."""
    for r in runs:
        if r["status"] not in ("observed", "censored", "invalid"):
            raise ValueError(f"unknown status {r['status']!r}")
    valid = [r for r in runs if r["status"] != "invalid"]
    observed = [r["t"] for r in valid if r["status"] == "observed"]
    out = {"denominator_valid_runs": len(valid), "observed": len(observed),
           "censored": sum(1 for r in valid if r["status"] == "censored"),
           "invalid": len(runs) - len(valid), "describe_observed": describe(observed),
           "median_interval": median_interval(observed)}
    out["median"] = out["describe_observed"]["median"]
    out["median_status"] = "available" if observed else "not available (no observed time)"
    out["interpretation"] = ("median and interval CONDITIONAL ON AN OBSERVED RECOVERY (observed times only), not the "
                             "median of all incidents: censored runs are reported apart, never as values")
    return out


if __name__ == "__main__":
    if sys.argv[1] == "--s3":
        print(json.dumps(s3_family(json.load(open(sys.argv[2]))), indent=1))
    elif sys.argv[1] == "--binary":
        print(json.dumps(mcnemar_exact(json.load(open(sys.argv[2]))), indent=1))
    else:
        print(json.dumps(analyse(json.load(open(sys.argv[1]))), indent=1))
