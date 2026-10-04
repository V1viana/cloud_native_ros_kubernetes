#!/usr/bin/env python3
"""Final tables of the R14 campaign (docs/R14_PREREGISTRATION_DRAFT.md sections 5 and 5.1, frozen at
2387d5d). Read-only on the results; written while the S1 block ran, tested after it.

  r14_tables.py --results R14_DIR [--s1-block S1_BLOCK_DIR] [--md]

Every configuration starts from the EXPECTED calendar (scripts/r14_schedule.py build(): 314 rows, 157
pairs). Each row is: ok (a valid run), invalid (a run the wrapper judged not valid: counted apart, never
a measure), halted (no run: configuration suspended by decision), missing (no record), incomplete (no
recorded decision) or defect (a valid row whose evidence is unreadable or self-contradictory). A pair
enters a paired analysis only if both rows are ok; a pair with an invalid row is EXCLUDED and listed;
a pair with a halted / missing / incomplete / defective row is INCOMPLETE and listed. Censoring (no
recovery within the window) is a property of a valid run, never of an invalid one. Nothing is deduced
from a verdict where section 5.1 names another field:
- S2 short/long, PRIMARY: missed_transient.recognized_within_180s of the S2 judge (eligible runs only);
- S4 cells, PRIMARY: every functional property of the S4 judge 'ok' (cross-checked with its verdict);
- S1-delete, PRIMARY: from the S1 block (scripts/s1_block_extract.py: readiness pod_ready_s);
- S3, PRIMARY: L0-L3 rates of the edges-only observer E (run.json e_window), 8 Wilcoxon with Holm;
- TTR, PRIMARY: time_to_rebuild_s of verdict.json (MEASURED observed, CENSORED censored);
- E4, U1, U2, PRIMARY (rollback / update correct): the scenario's functional verdict, i.e. the R9
  acceptance criteria of section 3 (outcome, rollback flag, workloads, audit). Section 5.1 names the
  metric without an operational field: this mapping is DECLARED in the output as a proposal.
Per-variant measures (E1, E2, P2, convergence, mission continuity, rollback k/n, churn) and the MTTR are
descriptive, from the runner's analysis (campaign/<row>/analysis/runs.csv) with their measured names.
"""
import argparse, csv, json, os, sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import r14_schedule as rs  # noqa: E402
import r14_stats as st  # noqa: E402
from s1_block_extract import split_attempts, is_number  # noqa: E402

CAMPAIGN = ("e0", "e1", "e2", "p2", "e4", "u1", "u2")
S2_CASES = ("s2-short", "s2-long")
S2_CONTROLS = ("s2-control", "s2-partition-only")
S3 = {"s3-n3": 3, "s3-n10": 10}
S4 = ("s4-l3", "s4-l1-battery", "s4-l1-telemetry", "s4-l1-edge")
# the properties the S4 judge produces for each cell (scripts/s4_judge.py CELLS + collateral): a judge
# record lacking one of them is incomplete, never "all ok"
S4_PROPERTIES = {"s4-l3": {"battery", "telemetry", "edge_service", "collateral"},
                 "s4-l1-battery": {"battery", "collateral"}, "s4-l1-telemetry": {"telemetry", "collateral"},
                 "s4-l1-edge": {"edge_service", "collateral"}}
S4_RETURNS = (("telemetry", "returned_after_t0_sec"), ("edge_service", "first_positive_after_start_sec"))
OPERATIONAL_PROPOSAL = ("Precisazione operativa fatta DOPO la raccolta, dichiarata (Viviana, 4 ottobre): successo = esito "
                        "funzionale PASS del runner secondo i criteri R9 preesistenti (sezione 3), fra le esecuzioni valide. "
                        "Non e' il solo rollback_performed: i criteri R9 verificano anche altri requisiti")
CHURN_NOTE = ("5.1: duplicati e reconciliation churn per U1/U2 e per S2/S4, con le due definizioni separate. "
              "U1/U2: reconciliation_churn del runner (valorizzato in U1; assente in U2 nei report). S2/S4: NESSUN campo nei "
              "giudici S2 e S4 (verificato sui record): non estratto dai giudici, che non equivale a 'non ricostruibile dai "
              "dati conservati'; un eventuale approfondimento resta separato da queste tabelle")


def num(x):
    if x in (None, ""):
        return None
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    return v if is_number(v) else None


def boolean(x):
    return {"True": True, "False": False, True: True, False: False}.get(x)


def load(path):
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


# ---- rows of the expected calendar ----------------------------------------------------------------

def rows_of(results):
    calendar = rs.build()
    rs.check(calendar)
    state = load(os.path.join(results, "state.json")) or {}
    halted = state.get("halted") or {}
    by_seq = {}
    runs = os.path.join(results, "runs")
    for d in sorted(os.listdir(runs)):
        r = load(os.path.join(runs, d, "run.json"))
        if isinstance(r, dict) and r.get("seq") not in (None, 0):
            r["_dir"] = d
            by_seq.setdefault(r["seq"], []).append(r)
    out = []
    for e in calendar:
        row = {"seq": e["seq"], "config": e["config"], "variant": e["variant"], "pair": e["pair"], "round": e["round"]}
        recs = by_seq.pop(e["seq"], [])
        if not recs:
            row["status"] = "halted" if e["config"] in halted else "missing"
            out.append(row)
            continue
        rec, earlier = split_attempts(recs)
        row.update(record=rec, attempts=1 + len(earlier), verdict=rec.get("verdict"))
        if (rec.get("config"), rec.get("variant")) != (e["config"], e["variant"]):
            row.update(status="defect", why="record does not match the calendar row")
        elif not rec.get("row_complete"):
            row["status"] = "incomplete"
        elif not rec.get("valid"):
            row["status"] = "invalid"
        else:
            row["status"] = "ok"
        out.append(row)
    unexpected = sorted(by_seq)
    return out, unexpected, halted


def by_config(rows):
    out = {}
    for r in rows:
        out.setdefault(r["config"], []).append(r)
    return out


def denominators(rows):
    c = {}
    for v in ("a", "b"):
        mine = [r for r in rows if r["variant"] == v]
        c[v] = {"expected": len(mine), **{s: sum(1 for r in mine if r["status"] == s)
                                          for s in ("ok", "invalid", "halted", "missing", "incomplete", "defect")},
                "invalid_rows": [{"seq": r["seq"], "verdict": r.get("verdict")} for r in mine if r["status"] == "invalid"],
                "defects": [{"seq": r["seq"], "why": r.get("why")} for r in mine if r["status"] == "defect"]}
    return c


def binary_pairs(rows, outcome):
    """[{pair, a, b}] for McNemar over ALL expected pairs: outcome(row) -> True/False for ok rows (None marks the
    row as a defect); invalid -> None (excluded by mcnemar_exact); other statuses -> pair incomplete."""
    pairs, incomplete = {}, {}
    for r in rows:
        p = pairs.setdefault(r["pair"], {"pair": r["pair"], "a": None, "b": None})
        if r["status"] == "ok":
            value = outcome(r)
            if value is None:
                r.update(status="defect", why=r.get("why") or "outcome field absent or malformed")
                incomplete.setdefault(r["pair"], []).append((r["variant"], "defect"))
            p[r["variant"]] = value
        elif r["status"] != "invalid":
            incomplete.setdefault(r["pair"], []).append((r["variant"], r["status"]))
    usable = [p for k, p in sorted(pairs.items()) if k not in incomplete]
    result = st.mcnemar_exact(usable)
    result["pairs_expected"] = len(pairs)
    result["pairs_incomplete"] = {k: v for k, v in sorted(incomplete.items())}
    return result


def describe_field(rows, get):
    out = {}
    for v in ("a", "b"):
        values = [x for x in (get(r) for r in rows if r["variant"] == v and r["status"] == "ok") if x is not None]
        out[v] = st.describe(values)
    return out


def k_of_n(rows, get):
    out = {}
    for v in ("a", "b"):
        ok = [r for r in rows if r["variant"] == v and r["status"] == "ok"]
        known = [x for x in (get(r) for r in ok) if x is not None]
        out[v] = {"k": sum(1 for x in known if x), "n": len(known), "valid_runs": len(ok), "field_absent": len(ok) - len(known)}
    return out


# ---- per-runner evidence ---------------------------------------------------------------------------

def campaign_row(results, r):
    path = os.path.join(results, "campaign", f"{r['seq']:03d}-{r['config']}-{r['variant']}", "analysis", "runs.csv")
    try:
        with open(path) as f:
            rows = list(csv.DictReader(f))
    except OSError:
        return None
    return rows[0] if rows else None


def judge(r, name):
    return load(os.path.join((r.get("record") or {}).get("result_dir") or "", name))


def s4_all_ok(r):
    """PRIMARY of an S4 cell: every property the cell is EXPECTED to have is 'ok'. A judge lacking a property,
    with an unknown status or contradicting its own verdict is a defect (None), never a FAIL."""
    j = judge(r, "s4-judge.json")
    if not isinstance(j, dict) or not isinstance(j.get("functional"), dict):
        r["why"] = "S4 judge record unreadable"
        return None
    expected = S4_PROPERTIES[r["config"]]
    missing = expected - set(j["functional"])
    if missing:
        r["why"] = f"S4 judge lacks the properties {sorted(missing)} expected for {r['config']}"
        return None
    statuses = {k: (p.get("status") if isinstance(p, dict) else None) for k, p in j["functional"].items() if k in expected}
    if any(v not in ("ok", "fail") for v in statuses.values()):
        r["why"] = f"S4 judge property with an unknown status: {statuses}"
        return None
    all_ok = all(v == "ok" for v in statuses.values())
    if all_ok != (j.get("verdict") == "PASS"):
        r["why"] = f"S4 judge verdict {j.get('verdict')} contradicts its properties"
        return None
    return all_ok


def s4_return(r, prop, key):
    """Return time of a property for the secondary tables: {"status": observed|censored|defect|n/a, "t"}.
    observed = property ok with a finite time; censored = property fail (no return within the horizon);
    defect = property ok but the time key absent or malformed; n/a = the cell has no such property."""
    j = judge(r, "s4-judge.json") or {}
    p = (j.get("functional") or {}).get(prop)
    if prop not in S4_PROPERTIES[r["config"]]:
        return {"status": "n/a"}
    if not isinstance(p, dict):
        return {"status": "defect"}
    if p.get("status") == "fail":
        return {"status": "censored"}
    detail = p.get("detail") if isinstance(p.get("detail"), dict) else {}
    if key not in detail or num(detail.get(key)) is None:
        return {"status": "defect"}
    return {"status": "observed", "t": num(detail[key])}


def s2_recognized(r):
    j = judge(r, "s2-judge.json")
    if not isinstance(j, dict) or not isinstance(j.get("missed_transient"), dict):
        return None
    m = j["missed_transient"]
    if not (j.get("validity") or {}).get("ok") or m.get("eligible") is not True:
        r["why"] = "S2 judge: run not eligible for the missed-transient rate"
        return None
    return m.get("recognized_within_180s") if isinstance(m.get("recognized_within_180s"), bool) else None


def e2_recovery(r):
    """E2: the runner reports recovery_time_ms only for a PASS. A valid FAIL does NOT prove a missed recovery (the
    functional verdict depends on other checks too): its recovery is UNKNOWN, reported apart, never censored. A PASS
    without a time is a defect."""
    if r["status"] != "ok":
        return {"status": "invalid"}
    t = num((r.get("csv") or {}).get("recovery_time_ms"))
    if t is not None:
        return {"status": "observed", "t": t / 1000.0}
    if r.get("verdict") == "PASS":
        r.update(status="defect", why="E2 PASS without recovery_time_ms")
        return {"status": "defect"}
    return {"status": "recovery_unknown"}


def e2_mttr(rows, variant):
    statuses = [e2_recovery(r) for r in rows if r["variant"] == variant and r["status"] in ("ok", "invalid")]
    observed = [x["t"] for x in statuses if x["status"] == "observed"]
    return {"denominator_valid_runs": sum(1 for x in statuses if x["status"] in ("observed", "recovery_unknown")),
            "observed": len(observed), "recovery_unknown_valid_fail": sum(1 for x in statuses if x["status"] == "recovery_unknown"),
            "invalid": sum(1 for x in statuses if x["status"] == "invalid"), "defects": sum(1 for x in statuses if x["status"] == "defect"),
            "describe_observed": st.describe(observed), "median_interval": st.median_interval(observed),
            "median_status": "available" if observed else "not available (no observed time)",
            "interpretation": "median and interval of the OBSERVED declared recoveries only; a valid FAIL is 'recovery unknown', not censored"}


def ttr_status(r):
    """TTR: MEASURED with a finite time -> observed; CENSORED -> censored; an invalid row -> invalid; a VALID row
    whose verdict.json is unreadable or inconsistent is a DEFECT of the collection: the row's status changes, so
    the denominators show it, and the pair becomes incomplete (never an 'invalid run')."""
    if r["status"] == "invalid":
        return {"status": "invalid"}
    j = judge(r, "verdict.json")
    if isinstance(j, dict) and j.get("verdict") == "MEASURED" and num(j.get("time_to_rebuild_s")) is not None:
        return {"status": "observed", "t": num(j["time_to_rebuild_s"])}
    if isinstance(j, dict) and j.get("verdict") == "CENSORED":
        return {"status": "censored"}
    r.update(status="defect", why="TTR verdict.json unreadable or inconsistent with a valid row")
    return {"status": "defect"}


# ---- tables -----------------------------------------------------------------------------------------

def tables(results, s1_block=None):
    rows, unexpected, halted = rows_of(results)
    cfg = by_config(rows)
    out = {"results": os.path.abspath(results), "rows_expected": len(rows), "unexpected_records": unexpected,
           "halted_configurations": halted, "configurations": {}}
    C = out["configurations"]
    for name in CAMPAIGN:
        rs_ = cfg[name]
        for r in rs_:
            if r["status"] == "ok":
                r["csv"] = campaign_row(results, r)
                if r["csv"] is None:
                    r.update(status="defect", why="runner analysis (runs.csv) unreadable")
        f = lambda key: (lambda r: num((r.get("csv") or {}).get(key)))
        b = lambda key: (lambda r: boolean((r.get("csv") or {}).get(key)))
        entry = {"denominators": denominators(rs_), "functional_pass": k_of_n(rs_, lambda r: r.get("verdict") == "PASS"),
                 "mission_continuity": k_of_n(rs_, b("mission_continuity"))}
        if name == "e0":
            entry["class"] = "Controllo: esito funzionale nominale, k/n per variante (atteso tutto PASS)"
        elif name == "e1":
            entry.update({"class": "Per variante: latenza del comando di sicurezza e criterio funzionale; nessuna "
                                   "rivendicazione di non interferenza",
                          "latency_fault_to_rtl_command_ms": describe_field(rs_, f("reaction_time_ms")),
                          "latency_command_to_ack_ms": describe_field(rs_, f("ack_latency_ms"))})
        elif name == "e2":
            entry.update({"class": "Per variante: recovery dichiarato dalla variante (nessun endpoint esterno promosso)",
                          "recovery_time_ms": describe_field(rs_, f("recovery_time_ms")),
                          # convergence_time_ms of E2 is the SAME report field ('Recovery end-to-end') as recovery_time_ms
                          # (analyze_campaign.py, E2 branch): not shown twice, declared
                          "recovery_and_convergence_note": "in E2 il runner ricava recovery_time_ms e convergence_time_ms dallo "
                                                           "stesso campo del report ('Recovery end-to-end'): riportato una volta",
                          "manager_duration_ms": describe_field(rs_, f("manager_duration_ms")),
                          "rollback_performed": k_of_n(rs_, b("rollback_performed")),
                          "mttr": {v: e2_mttr(rs_, v) for v in ("a", "b")},
                          "mttr_note": "recovery dichiarato dalla variante (recovery_time_ms del runner), in secondi"})
        elif name == "p2":
            entry.update({"class": "Per variante: durata osservata (convergenza col nome misurato)",
                          "convergence_time_ms": describe_field(rs_, f("convergence_time_ms"))})
        else:
            entry.update({"class": "A/B, primaria: McNemar", "primary_operationalization": OPERATIONAL_PROPOSAL,
                          "primary": binary_pairs(rs_, lambda r: r.get("verdict") == "PASS"),
                          "convergence_time_ms": describe_field(rs_, f("convergence_time_ms"))})
            if name in ("e4", "u2"):
                entry["rollback_performed"] = k_of_n(rs_, b("rollback_performed"))
            if name in ("u1", "u2"):
                entry["reconciliation_churn_u1u2"] = describe_field(rs_, f("reconciliation_churn"))
                entry["reconciliation_churn_present"] = k_of_n(rs_, lambda r: f("reconciliation_churn")(r) is not None)
                entry["churn_note"] = CHURN_NOTE
        entry["denominators"] = denominators(rs_)
        C[name] = entry
    for name in S2_CASES:
        rs_ = cfg[name]
        primary = binary_pairs(rs_, s2_recognized)
        C[name] = {"class": "A/B, primaria: transitorio riconosciuto entro 180 s (missed_transient.recognized_within_180s), McNemar",
                   "primary": primary, "verdict_pass": k_of_n(rs_, lambda r: r.get("verdict") == "PASS"),
                   "churn_note": CHURN_NOTE, "denominators": denominators(rs_)}
    for name in S2_CONTROLS:
        rs_ = cfg[name]
        C[name] = {"class": "Controllo: verifica, nessun test", "verdict_pass": k_of_n(rs_, lambda r: r.get("verdict") == "PASS"),
                   "denominators": denominators(rs_)}
    s3_pairs, s3_incomplete = [], {}
    for name, n in S3.items():
        pairs = {}
        for r in cfg[name]:
            p = pairs.setdefault(r["pair"], {"pair": f"{name}-{r['pair']}", "n": n, "a": None, "b": None})
            if r["status"] == "ok":
                w = (r["record"] or {}).get("e_window") or {}
                rates = w.get("rates") or {}
                good = w.get("measurable") is True and all(num(rates.get(L)) is not None for L in ("L0", "L1", "L2", "L3"))
                p[r["variant"]] = {"measurable": good, "rates": {L: num(rates.get(L)) for L in ("L0", "L1", "L2", "L3")}}
            elif r["status"] == "invalid":
                p[r["variant"]] = {"measurable": False, "rates": {}}
            else:
                s3_incomplete.setdefault(p["pair"], []).append((r["variant"], r["status"]))
        s3_pairs += [p for p in pairs.values() if p["pair"] not in s3_incomplete]
        C[name] = {"class": "Controllo: sonde PASS (validita')", "verdict_pass": k_of_n(cfg[name], lambda r: r.get("verdict") == "PASS"),
                   "denominators": denominators(cfg[name])}
    all_valid = {}
    for name, n in S3.items():
        for v in ("a", "b"):
            for L in ("L0", "L1", "L2", "L3"):
                vals = [num(((r["record"] or {}).get("e_window") or {}).get("rates", {}).get(L)) for r in cfg[name]
                        if r["variant"] == v and r["status"] == "ok" and ((r["record"] or {}).get("e_window") or {}).get("measurable") is True]
                vals = [x for x in vals if x is not None]
                all_valid[f"N={n} {L} {v.upper()}"] = st.describe(vals) if vals else None
    C["s3"] = {"class": "A/B, primaria: tassi L0-L3 nella finestra fissa dopo T0 (osservatore E), 8 Wilcoxon esatti, Holm",
               "family": st.s3_family(s3_pairs), "pairs_incomplete": s3_incomplete,
               "describe_all_valid_runs_calendar": all_valid,
               "describe_note": "insieme 1 della sezione 5 calcolato su TUTTE le righe valide del calendario, comprese quelle di coppie incomplete; "
                                "la famiglia riporta a parte l'insieme 1 delle sole coppie complete e l'insieme 2 delle coppie nel test"}
    for name in S4:
        rs_ = cfg[name]
        primary = binary_pairs(rs_, s4_all_ok)
        props = {prop: {"a": {}, "b": {}} for prop in sorted(S4_PROPERTIES[name])}
        for r in rs_:
            if r["status"] != "ok":
                continue
            functional = (judge(r, "s4-judge.json") or {}).get("functional") or {}
            for prop in S4_PROPERTIES[name]:
                status = (functional.get(prop) or {}).get("status", "absent") if isinstance(functional.get(prop), dict) else "absent"
                props[prop][r["variant"]][status] = props[prop][r["variant"]].get(status, 0) + 1
        returns = {}
        for prop, key in S4_RETURNS:
            if prop not in S4_PROPERTIES[name]:
                continue
            per_pair, per_variant = {}, {"a": [], "b": []}
            for r in rs_:
                if r["status"] == "ok":
                    x = s4_return(r, prop, key)
                    per_pair.setdefault(r["pair"], {})[r["variant"]] = x
                    per_variant[r["variant"]].append(x)
                elif r["status"] == "invalid":
                    per_variant[r["variant"]].append({"status": "invalid"})
            returns[f"{prop}.{key}"] = {
                "pairs_side_by_side": {k: {v: x.get("t") if x["status"] == "observed" else x["status"] for v, x in d.items()}
                                       for k, d in sorted(per_pair.items())},
                "paired_differences_b_minus_a": {k: d["b"]["t"] - d["a"]["t"] for k, d in sorted(per_pair.items())
                                                 if d.get("a", {}).get("status") == "observed" and d.get("b", {}).get("status") == "observed"},
                # a DEFECT of this secondary time stays out of the MTTR entirely (not 'invalid': the row and its primary
                # may be valid); it is counted in 'defects'
                "mttr": {v: st.mttr([x for x in xs if x["status"] in ("observed", "censored", "invalid")])
                         for v, xs in per_variant.items()},
                "defects": {v: sum(1 for x in xs if x["status"] == "defect") for v, xs in per_variant.items()},
                "note": "secondaria descrittiva: nessun test, nessun p (5.1); nome del giudice S4; 'defect' = proprieta' ok senza il tempo, "
                        "contata a parte e NON come censura; nell'MTTR i difetti sono fuori dal denominatore"}
        C[name] = {"class": ("A/B, primaria: resilienza composta" if name == "s4-l3" else "A/B, riferimento a guasto singolo")
                   + f" = tutte le proprieta' attese {sorted(S4_PROPERTIES[name])} 'ok', McNemar",
                   "primary": primary, "properties": props, "returns_secondary": returns, "churn_note": CHURN_NOTE,
                   "denominators": denominators(rs_)}
    ttr_pairs, ttr_runs = {}, {"a": [], "b": []}
    for r in cfg["ttr"]:
        if r["status"] in ("ok", "invalid"):
            x = ttr_status(r)                                 # may turn a valid row into a defect
            if r["status"] != "defect":
                ttr_pairs.setdefault(r["pair"], {"pair": r["pair"]})[r["variant"]] = x
                ttr_runs[r["variant"]].append(x)
    ttr_incomplete = sorted({r["pair"] for r in cfg["ttr"] if r["status"] not in ("ok", "invalid")})
    C["ttr"] = {"class": "A/B, primaria: time-to-rebuild, Wilcoxon esatto e intervallo",
                "analysis": st.analyse([p for k, p in sorted(ttr_pairs.items()) if k not in ttr_incomplete and "a" in p and "b" in p]),
                "pairs_incomplete": ttr_incomplete,
                "all_valid_runs_calendar": {v: st.mttr(xs) for v, xs in ttr_runs.items()},
                "all_valid_runs_note": "insieme 1 su TUTTE le righe valide del calendario, comprese quelle di coppie incomplete "
                                       "(mediana condizionata ai tempi osservati; censurati e denominatore riportati)",
                "denominators": denominators(cfg["ttr"])}
    C["s1-delete"] = {"class": "A/B, primaria: readiness ripristinata entro 180 s, McNemar (dal blocco S1)",
                      "campaign_rows": denominators(cfg["s1-delete"]),
                      "note": "nella campagna la configurazione e' sospesa per decisione (riga 18 non valida); i dati sono del blocco S1"}
    if s1_block:
        import s1_block_extract
        C["s1-delete"]["block"] = s1_block_extract.extract(s1_block)
    return out


def fmt(x):
    if isinstance(x, float):
        return f"{x:.4g}"
    return str(x)


def desc(d):
    if not d or d.get("n") in (0, None):
        return "n=0"
    return (f"n={d['n']}, media {fmt(d['mean'])}, mediana {fmt(d['median'])}, p95 {fmt(d['p95_nearest_rank'])}, "
            f"min {fmt(d['min'])}, max {fmt(d['max'])}")


def interval(i):
    if not i or i.get("low") is None:
        return "intervallo non disponibile"
    return f"[{fmt(i['low'])}, {fmt(i['high'])}] copertura {fmt(i['coverage'])}{' (CI95)' if i.get('is_ci95') else ' (non CI95)'}"


def mttr_md(m):
    return (f"denominatore {m['denominator_valid_runs']}, osservati {m['observed']}, censurati {m.get('censored', '-')}, "
            f"non validi {m['invalid']}; mediana {m['median_status']}: {desc(m['describe_observed'])}; {interval(m['median_interval'])}")


def markdown(t):
    C = t["configurations"]
    L = [f"# R14: tabelle finali ({t['results']})", "",
         "Sezioni 5 e 5.1 congelate. Denominatore = calendario atteso; stati per riga: valida, non valida, sospesa, mancante, "
         "incompleta, difetto. Una coppia con una riga non valida e' ESCLUSA dal test; con una riga sospesa/mancante/incompleta/"
         "difettosa e' INCOMPLETA. La censura e' un attributo di una riga valida, mai di una non valida.", "",
         f"Righe attese {t['rows_expected']}; record non attesi {t['unexpected_records'] or '-'}; "
         f"configurazioni sospese {t['halted_configurations'] or '-'}", ""]

    def den(d):
        return "; ".join(f"{v.upper()}: valide {x['ok']}/{x['expected']}, non valide {x['invalid']} {[i['seq'] for i in x['invalid_rows']] or ''}, "
                         f"sospese {x['halted']}, mancanti {x['missing']}, incomplete {x['incomplete']}, difetti {x['defect']} {x['defects'] or ''}"
                         for v, x in d.items())

    def mc(p):
        return (f"coppie attese {p['pairs_expected']}, usate {p['pairs_used']}, escluse (riga non valida) {[e['pair'] for e in p['pairs_excluded']]}, "
                f"incomplete {list(p['pairs_incomplete'])} {p['pairs_incomplete'] or ''}; tabella {p['table']}; discordanti {p['discordant']}; "
                f"p bilaterale {fmt(p['p_two_sided'])} (minimo possibile {fmt(p['p_min_possible'])}); successi A {p['success']['a']}, B {p['success']['b']}")

    def kn(e, k, label=None):
        if k in e:
            L.append(f"- {label or k}: " + ", ".join(f"{v.upper()} {x['k']}/{x['n']} (valide {x['valid_runs']}, campo assente {x['field_absent']})"
                                                     for v, x in e[k].items()))

    def dv(e, k, label=None):
        if k in e:
            L.append(f"- {label or k}: " + "; ".join(f"{v.upper()} {desc(d)}" for v, d in e[k].items()))

    for name, e in C.items():
        L += [f"## {name}", "", f"- classe: {e['class']}"]
        if "denominators" in e:
            L.append(f"- denominatori: {den(e['denominators'])}")
        if "campaign_rows" in e:
            L.append(f"- righe della campagna: {den(e['campaign_rows'])}; {e.get('note', '')}")
        if "primary_operationalization" in e:
            L.append(f"- **{e['primary_operationalization']}**")
        if "primary" in e:
            L.append(f"- primaria (McNemar): {mc(e['primary'])}")
        kn(e, "functional_pass", "esito funzionale PASS")
        kn(e, "verdict_pass", "verdetto PASS")
        kn(e, "mission_continuity")
        kn(e, "rollback_performed")
        for k, label in (("latency_fault_to_rtl_command_ms", "latenza guasto -> comando RTL (ms)"), ("latency_command_to_ack_ms", "latenza comando -> ack (ms)"),
                         ("recovery_time_ms", "recovery dichiarato (ms)"), ("convergence_time_ms", "convergenza, durata osservata (ms)"),
                         ("manager_duration_ms", "durata del manager (ms)"), ("reconciliation_churn_u1u2", "reconciliation churn (U1/U2)")):
            dv(e, k, label)
        kn(e, "reconciliation_churn_present", "churn valorizzato")
        if "recovery_and_convergence_note" in e:
            L.append(f"- nota: {e['recovery_and_convergence_note']}")
        if "churn_note" in e:
            L.append(f"- churn/duplicati: {e['churn_note']}")
        if "mttr" in e:
            for v, m in e["mttr"].items():
                L.append(f"- MTTR {v.upper()} ({e.get('mttr_note', '')}): {mttr_md(m)}; recupero ignoto (FAIL valido) {m.get('recovery_unknown_valid_fail', 0)}, difetti {m.get('defects', 0)}")
        if "properties" in e:
            for prop, d in e["properties"].items():
                L.append(f"- proprieta' {prop}: A {d['a'] or {}}, B {d['b'] or {}}")
        for key, ret in (e.get("returns_secondary") or {}).items():
            L.append(f"- ritorno {key} (secondaria, nessun test): coppie affiancate {ret['pairs_side_by_side']}; "
                     f"differenze B-A dove entrambi osservati {ret['paired_differences_b_minus_a']}; difetti {ret['defects']}")
            for v, m in ret["mttr"].items():
                L.append(f"  - {v.upper()}: {mttr_md(m)}")
        if name == "s3":
            for test in e["family"]["tests"]:
                w, pm = test["wilcoxon"], test["pseudomedian"]
                L.append(f"- N={test['n']} {test['level']}: coppie usate {test['pairs_used']}, escluse {len(test['excluded'])} {test['excluded'] or ''}, "
                         f"zeri {test['zeros']}, W+ {fmt(w['w_plus'])}, p {fmt(w['p_two_sided'])} (minimo {fmt(w['p_min_possible'])}, parita' {w['ties']}), "
                         f"p Holm {fmt(test.get('p_holm'))}, rifiuto {test.get('reject_at_alpha')}; pseudomediana B-A {fmt(pm['hodges_lehmann_pseudomedian'])} "
                         f"[{fmt(pm['low'])}, {fmt(pm['high'])}] copertura {fmt(pm['coverage'])} {'CI95' if pm['is_ci95'] else 'non CI95'}")
                for setname, d in test["describe"].items():
                    L.append(f"  - {setname}: A {desc(d['a'])}; B {desc(d['b'])} ({d['note']})")
                L.append(f"  - coppie usate: {[(u['pair'], fmt(u['a']), fmt(u['b']), fmt(u['d'])) for u in test['pairs']]}")
            L.append(f"- insieme 1 su tutto il calendario: " + "; ".join(f"{k} {desc(d)}" for k, d in e["describe_all_valid_runs_calendar"].items()))
            L.append(f"- coppie incomplete: {e['pairs_incomplete'] or '-'}; {e['describe_note']}; {e['family']['scope']}")
        if name == "ttr":
            a = e["analysis"]
            L.append(f"- coppie usate {[(u['pair'], fmt(u['difference_b_minus_a'])) for u in a['pairs_used']]}; zeri {a['pairs_zero_difference']}; "
                     f"escluse {a['pairs_excluded']}; incomplete {e['pairs_incomplete']}")
            w = a["wilcoxon"]
            L.append(f"- Wilcoxon esatto: n {w['n']}, W+ {fmt(w['w_plus'])}, p {fmt(w['p_two_sided'])} (minimo {fmt(w['p_min_possible'])}, parita' {w['ties']})")
            pm = a["pseudomedian_difference"]
            L.append(f"- pseudomediana B-A {fmt(pm['hodges_lehmann_pseudomedian'])} [{fmt(pm['low'])}, {fmt(pm['high'])}] copertura {fmt(pm['coverage'])} "
                     f"{'CI95' if pm['is_ci95'] else 'non CI95'}; zeri {pm['zeros']}, parita' {pm['ties']}; ipotesi: {pm.get('assumption')}")
            for v, d in a["per_variant"].items():
                L.append(f"- {v.upper()} nelle coppie: recuperi {d['recovered_within_window']}, censurati {d['censored']}, non validi {d['invalid_runs']}; "
                         f"insieme 1 {desc(d['observed_times_all_valid_runs'])}; insieme 2 {desc(d['observed_times_pairs_in_test'])}")
            for v, m in e["all_valid_runs_calendar"].items():
                L.append(f"- {v.upper()} su tutto il calendario: {mttr_md(m)}")
        if name == "s1-delete" and "block" in e:
            b = e["block"]; p = b["primary"]
            L.append(f"- blocco S1 ({b['block']['results']}, rev {b['block']['rev']}): coppie attese {b['block']['pairs_expected']}, usate {p['pairs_used']}, "
                     f"escluse {[x['pair'] for x in p['pairs_excluded']]}, incomplete {p['pairs_incomplete']}; tabella {p['table']}; discordanti {p['discordant']}; "
                     f"p {fmt(p['p_two_sided'])} (minimo {fmt(p['p_min_possible'])}); readiness A {p['success']['a']}, B {p['success']['b']}")
            for v, d in b["per_variant"].items():
                L.append(f"  - {v.upper()}: valide {d['valid']}/{d['rows_expected']}, non valide {len(d['invalid'])}, mancanti {d['missing'] or '-'}, difetti {d['defects'] or '-'}; "
                         f"readiness {d['readiness_restored']} entro {d['window_s']} s, censurate {d['readiness_censored']}, tempi {desc(d['readiness_times_s'])}; "
                         f"servizio (secondaria) {d['service_recovered']}, censurate {d['service_censored']}, tempi {desc(d['service_times_s'])}")
        L.append("")
    return "\n".join(L)


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--results", required=True)
    ap.add_argument("--s1-block")
    ap.add_argument("--md", action="store_true")
    a = ap.parse_args(argv)
    t = tables(a.results, a.s1_block)
    print(markdown(t) if a.md else json.dumps(t, indent=1, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
