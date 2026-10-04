#!/usr/bin/env python3
"""Extractor of the S1-delete block (docs/S1_DELETE_BLOCK_PROPOSAL_DRAFT.md 6; decisions of Viviana,
4 October 2026). Written during the campaign; its tests run after it (declared).

  s1_block_extract.py BLOCK_RESULTS_DIR [--md]        JSON (default) or Markdown on stdout

The comparison STARTS FROM THE EXPECTED CALENDAR (scripts/r14_schedule.py build_s1_block: 10 pairs,
20 rows), not from what the files happen to contain: a row with no record, an incomplete row, a record
that does not match its calendar row, an unexpected record and several attempts of one row are reported,
never silently dropped or overwritten.

Per row, from the wrapper's record (`runs/*/run.json`, the format it really writes: seq, config,
variant, round, pair, attempt, valid, verdict, result_dir, row_complete, ...) and the S1 judge's record
(s1-judge.json in the run's evidence dir):
- validity = the wrapper's `valid` (judge valid AND datastore verified); an invalid run has NO measure
  and is counted apart with its verdict; nothing is ever deduced from PASS/FAIL;
- the judge's record must be COMPLETE: the keys valid, window_s, pod_ready_s, service_available_s and
  recovered all present (the judge writes an explicit null when there is nothing to report), the window
  equal to the agreed 180 s, the types sound. An ABSENT key is a collection defect, not a censored run;
- PRIMARY, readiness: the current Pod Ready within the window (pod_ready_s <= window_s): true, or
  false = censored (explicit null or beyond the window), or null for an invalid / defective row;
- SECONDARY, service: the judge's `recovered` (Pod Ready THEN positive health) and service_available_s;
  reported apart, no test;
- observed times kept for BOTH variants; a censored run is k/n with the window, never a number.
Pairs: McNemar exact (scripts/r14_stats.py) on the primary over the pairs of the calendar whose two rows
exist and are complete; a pair with an invalid or defective row is excluded by the test and listed; a pair
with a missing or incomplete row is listed as INCOMPLETE. Descriptives per variant: observed times only.
"""
import argparse, json, math, os, sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import r14_schedule as rs  # noqa: E402
import r14_stats  # noqa: E402

CONFIG = "s1-delete"
EXPECTED_WINDOW_S = 180                                   # the agreed window (proposal 3; S1_WINDOW_SEC fixed by the wrapper)
JUDGE_KEYS = ("valid", "window_s", "pod_ready_s", "service_available_s", "recovered")
RECORD_KEYS = ("seq", "config", "variant", "pair", "attempt")        # always written; the outcome keys come with row_complete
OUTCOME_KEYS = ("valid", "verdict", "result_dir")


def is_number(x):
    """A real, FINITE number. Python's json reads the literals Infinity, -Infinity and NaN: none of them is
    a time (an infinite readiness is not 'beyond the window', it is a malformed record)."""
    return isinstance(x, (int, float)) and not isinstance(x, bool) and math.isfinite(x)


def load_records(results):
    """Every run.json of the block's results dir: {seq: [records by attempt]} plus unreadable files."""
    by_seq, unreadable = {}, []
    runs = os.path.join(results, "runs")
    for d in sorted(os.listdir(runs)) if os.path.isdir(runs) else []:
        p = os.path.join(runs, d, "run.json")
        if not os.path.exists(p):
            continue
        try:
            r = json.load(open(p))
        except (OSError, ValueError) as e:
            unreadable.append({"dir": d, "error": str(e)})
            continue
        if not isinstance(r, dict):
            unreadable.append({"dir": d, "error": "record is not an object"})
            continue
        if r.get("config") != CONFIG:                          # another configuration: not this block's row
            unreadable.append({"dir": d, "error": f"config {r.get('config')!r}, not {CONFIG}"})
            continue
        r["_dir"] = d                                         # only to break ties deterministically
        by_seq.setdefault(r.get("seq"), []).append(r)
    return by_seq, unreadable


def order_key(r):
    """Chronological key of an attempt: the wrapper's own choice is by START TIME, never by name (a
    resume dir named -resume10 sorts before -resume9 and the attempt counter is the same for resumes);
    a start that is missing or not finite counts as 'never started'. Ties: attempt, then the dir name."""
    start = r.get("start")
    started = is_number(start)
    return (1 if started else 0, start if started else 0.0, r.get("attempt") if is_number(r.get("attempt")) else 0, r.get("_dir", ""))


def split_attempts(recs):
    """(the row's outcome, the earlier attempts oldest first). Like run_row/loop of the wrapper: the latest
    STARTED attempt is the outcome; an attempt that never started (a stop before the run) is never preferred
    to a started one."""
    ordered = sorted(recs, key=order_key)
    return ordered[-1], ordered[:-1]


def blank(expected, status, why=None):
    return {"seq": expected["seq"], "pair": expected["pair"], "variant": expected["variant"], "status": status, "why": why,
            "valid": False, "verdict": None, "datastore": None, "attempts": 0, "superseded": [],
            "readiness_restored": None, "readiness_s": None, "readiness_censored": None,
            "service_recovered": None, "service_s": None, "service_censored": None, "window_s": None, "judge": None}


def judge_defect(judge):
    """Why a judge record cannot be used, or None. An absent key is NOT an explicit null."""
    if not isinstance(judge, dict):
        return "judge record is not an object"
    absent = [k for k in JUDGE_KEYS if k not in judge]
    if absent:
        return f"judge record lacks {absent}"
    if not isinstance(judge["valid"], bool) or not isinstance(judge["recovered"], bool):
        return "judge valid/recovered are not booleans"
    if judge["window_s"] != EXPECTED_WINDOW_S:
        return f"judge window_s is {judge['window_s']!r}, not the agreed {EXPECTED_WINDOW_S} s"
    for k in ("pod_ready_s", "service_available_s"):
        if judge[k] is not None and not (is_number(judge[k]) and judge[k] >= 0):
            return f"judge {k} is neither null nor a finite non-negative number: {judge[k]!r}"
    return None


def measure(expected, record, superseded):
    out = blank(expected, "ok")
    out["attempts"] = 1 + len(superseded)
    out["superseded"] = [{"attempt": r.get("attempt"), "dir": r.get("dir"), "start": r.get("start"),
                          "verdict": r.get("verdict"), "valid": r.get("valid")} for r in superseded]
    absent = [k for k in RECORD_KEYS if k not in record]
    if absent:
        return {**out, "status": "defect", "why": f"record lacks {absent}", "verdict": record.get("verdict")}
    if (record["seq"], record["pair"], record["variant"]) != (expected["seq"], expected["pair"], expected["variant"]):
        return {**out, "status": "defect", "verdict": record.get("verdict"),
                "why": f"record (seq {record['seq']}, pair {record['pair']!r}, variant {record['variant']!r}) "
                       f"does not match the calendar row (seq {expected['seq']}, pair {expected['pair']}, variant {expected['variant']})"}
    out.update(verdict=record.get("verdict"), datastore=record.get("datastore"))
    if not record.get("row_complete"):
        return {**out, "status": "incomplete", "why": "row not complete (no recorded decision)"}
    lacking = [k for k in OUTCOME_KEYS if k not in record]
    if lacking:
        return {**out, "status": "defect", "why": f"complete record lacks {lacking}"}
    if not bool(record["valid"]):
        return {**out, "status": "invalid"}                    # no measure; counted apart with its verdict
    judge_path = os.path.join(record.get("result_dir") or "", "s1-judge.json")
    try:
        judge = json.load(open(judge_path))
    except (OSError, ValueError) as e:
        return {**out, "status": "defect", "why": f"judge record unreadable: {judge_path}: {e}"}
    why = judge_defect(judge)
    if why:
        return {**out, "status": "defect", "why": why, "judge": judge_path}
    if not judge["valid"]:
        return {**out, "status": "defect", "judge": judge_path,
                "why": "the wrapper says valid but the judge says not valid"}
    window, ready, service = judge["window_s"], judge["pod_ready_s"], judge["service_available_s"]
    restored = ready is not None and ready <= window
    recovered = judge["recovered"] and service is not None and service <= window
    out.update(valid=True, window_s=window, judge=judge_path,
               readiness_restored=restored, readiness_s=ready if restored else None, readiness_censored=not restored,
               service_recovered=recovered, service_s=service if recovered else None, service_censored=not recovered)
    return out


def rows_of(results):
    """One entry per row of the EXPECTED calendar, plus the records the calendar does not expect."""
    calendar = rs.build_s1_block()
    rs.check_s1_block(calendar)
    by_seq, unreadable = load_records(results)
    rows = []
    for e in calendar:
        recs = by_seq.pop(e["seq"], [])
        if not recs:
            rows.append(blank(e, "missing", "no record of this calendar row"))
            continue
        final, earlier = split_attempts(recs)                  # latest STARTED attempt is the outcome; earlier ones are listed
        rows.append(measure(e, final, earlier))
    unexpected = [{"seq": seq, "attempts": [r.get("attempt") for r in recs], "variant": recs[0].get("variant")}
                  for seq, recs in sorted(by_seq.items(), key=lambda kv: str(kv[0]))]
    return calendar, rows, unexpected, unreadable


def pair_table(calendar, rows):
    by = {(r["pair"], r["variant"]): r for r in rows}
    out = []
    for k in sorted({e["pair"] for e in calendar}):
        a, b = by[(k, "a")], by[(k, "b")]
        bad = [(r["variant"], r["status"]) for r in (a, b) if r["status"] in ("missing", "incomplete", "defect")]
        out.append({"pair": k, "incomplete": bool(bad), "why": bad,
                    "a": None if a["status"] != "ok" else a["readiness_restored"],
                    "b": None if b["status"] != "ok" else b["readiness_restored"]})
    return out


def per_variant(rows, variant):
    mine = [r for r in rows if r["variant"] == variant]
    ok = [r for r in mine if r["status"] == "ok"]
    times = [r["readiness_s"] for r in ok if r["readiness_s"] is not None]
    stimes = [r["service_s"] for r in ok if r["service_s"] is not None]
    return {"rows_expected": len(mine), "valid": len(ok),
            "invalid": [{"seq": r["seq"], "pair": r["pair"], "verdict": r["verdict"]} for r in mine if r["status"] == "invalid"],
            "missing": [r["seq"] for r in mine if r["status"] == "missing"],
            "incomplete": [r["seq"] for r in mine if r["status"] == "incomplete"],
            "defects": [{"seq": r["seq"], "why": r["why"]} for r in mine if r["status"] == "defect"],
            "window_s": EXPECTED_WINDOW_S,
            "readiness_restored": f"{sum(1 for r in ok if r['readiness_restored'])}/{len(ok)}",
            "readiness_censored": sum(1 for r in ok if r["readiness_censored"]),
            "readiness_times_s": r14_stats.describe(times) if times else None,
            "service_recovered": f"{sum(1 for r in ok if r['service_recovered'])}/{len(ok)}",
            "service_censored": sum(1 for r in ok if r["service_censored"]),
            "service_times_s": r14_stats.describe(stimes) if stimes else None}


def extract(results):
    calendar, rows, unexpected, unreadable = rows_of(results)
    pairs = pair_table(calendar, rows)
    usable = [{"pair": p["pair"], "a": p["a"], "b": p["b"]} for p in pairs if not p["incomplete"]]
    # a pair with an invalid row has a None outcome: mcnemar_exact excludes it ("invalid run")
    primary = r14_stats.mcnemar_exact(usable)
    links = json.load(open(os.path.join(results, "links.json"))) if os.path.exists(os.path.join(results, "links.json")) else None
    manifest = json.load(open(os.path.join(results, "manifest.json"))) if os.path.exists(os.path.join(results, "manifest.json")) else {}
    return {"block": {"results": os.path.abspath(results), "rev": manifest.get("rev"), "mode": manifest.get("mode"),
                      "pairs_expected": len(pairs), "rows_expected": len(calendar),
                      "reference": {k: links.get(k) for k in ("reference_dir", "reference_rev")} if links else None},
            "primary": {"measure": "workload readiness restored within the window (judge pod_ready_s <= window_s)",
                        "test": "exact McNemar on the pairs of the calendar whose two rows exist, are complete and valid", **primary,
                        "pairs_incomplete": [p["pair"] for p in pairs if p["incomplete"]],
                        "pairs_incomplete_why": {p["pair"]: p["why"] for p in pairs if p["incomplete"]}},
            "secondary": {"measure": "service available (judge recovered: Pod Ready then positive health, service_available_s)",
                          "test": None},
            "per_variant": {v: per_variant(rows, v) for v in ("a", "b")},
            "problems": {"unexpected_records": unexpected, "unreadable_records": unreadable,
                         "defects": [{"seq": r["seq"], "pair": r["pair"], "variant": r["variant"], "why": r["why"]}
                                     for r in rows if r["status"] == "defect"],
                         "rows_with_several_attempts": [{"seq": r["seq"], "attempts": r["attempts"], "superseded": r["superseded"]}
                                                        for r in rows if r["attempts"] > 1]},
            "rows": rows}


def markdown(result):
    p, pv, pr = result["primary"], result["per_variant"], result["problems"]
    lines = [f"# Blocco S1-delete: estrazione ({result['block']['results']})", "",
             f"Revisione `{result['block']['rev']}`, modalita' `{result['block']['mode']}`; riferimento: {result['block']['reference']}", "",
             "## Primaria: readiness ripristinata entro la finestra (McNemar esatto)", "",
             f"- coppie attese {result['block']['pairs_expected']}; usate {p['pairs_used']}; escluse (esecuzione non valida) {len(p['pairs_excluded'])}; "
             f"**incomplete** {p['pairs_incomplete']} {p['pairs_incomplete_why'] or ''}",
             f"- tabella: entrambe si' {p['table']['both_success']}, solo A {p['table']['a_only']}, solo B {p['table']['b_only']}, nessuna {p['table']['both_failure']}",
             f"- discordanti {p['discordant']}; p bilaterale {p['p_two_sided']}; p minimo possibile {p['p_min_possible']}",
             f"- ripristini: A {p['success']['a']}, B {p['success']['b']}", ""]
    for v in ("a", "b"):
        d = pv[v]
        lines += [f"## Variante {v.upper()}", "",
                  f"- righe attese {d['rows_expected']}, valide {d['valid']}, non valide {len(d['invalid'])}: {d['invalid'] or '-'}; "
                  f"mancanti {d['missing'] or '-'}; incomplete {d['incomplete'] or '-'}; difetti {d['defects'] or '-'}",
                  f"- readiness ripristinata {d['readiness_restored']} entro {d['window_s']} s; censurate {d['readiness_censored']}",
                  f"- tempi osservati di readiness (s): {d['readiness_times_s']}",
                  f"- servizio disponibile (secondaria) {d['service_recovered']}; censurate {d['service_censored']}; tempi: {d['service_times_s']}", ""]
    lines += ["## Problemi di raccolta", "",
              f"- record non attesi: {pr['unexpected_records'] or '-'}; illeggibili: {pr['unreadable_records'] or '-'}",
              f"- difetti: {pr['defects'] or '-'}", f"- righe con piu' tentativi: {pr['rows_with_several_attempts'] or '-'}", ""]
    return "\n".join(lines)


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("results")
    ap.add_argument("--md", action="store_true")
    a = ap.parse_args(argv)
    result = extract(a.results)
    print(markdown(result) if a.md else json.dumps(result, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
