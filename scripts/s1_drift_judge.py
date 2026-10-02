#!/usr/bin/env python3
"""S1 judge (R10, docs/R10_S1_DRIFT.md), on s1_drift_observer.py's records.

Validity, recovery and isolation are kept apart (Viviana, 2026-09-26):
  validity   the injection ran (exit code 0) and is documented; the baseline
             was healthy (the three analytics Deployments ready, the three
             health services positive); reads cover the window: no gap between
             inventory reads above INVENTORY_GAP_SEC, no gap between the
             target's health attempts above HEALTH_GAP_SEC, both from the
             injection to the deadline. A failed collection (API or exec
             error) is a gap, not a negative observation. Not valid ->
             INCONCLUSIVE.
  recovery   times from the start of the injection (host monotonic clock):
             object repaired (delete: a Deployment with a new UID; scale: the
             declared replicas back), current Pod ready (the Deployment
             observed its generation, a Ready Pod of the current Deployment
             that did not exist before, no Pod of the target terminating or
             left from the old Deployment), service available (the first
             positive health answer whose call started after that). An answer
             from the old Pod still terminating never counts. Also the last
             negative and the first positive target answers. No recovery ->
             "not recovered within the window", no invented number.
  isolation  uninvolved robots' analytics Deployments and Pods keep UID and
             restarts, PX4 Pods too, and no uninvolved robot has two
             consecutive negative health answers (a single one is reported).
             A real impact is a FAIL, never an invalid measurement.
Verdict: A PASS if valid and isolated (its recovery reported, whatever it
is); B PASS if also recovered within the window with the drift observable
(Event DriftHealed on the ROSModule and audit OutOfBandDrift of the expected
kind, both after the injection). B's per-Pod lifecycle availability (R6) is
reported, not judged. Tested offline: operator/tests/test_s1_drift.py.
"""

import argparse
import json
import sys
from datetime import datetime

INVENTORY_GAP_SEC = 3.0
HEALTH_GAP_SEC = 25.0          # an RPC up to 15s plus the uninvolved robots' turn
UNINVOLVED_HEALTH_GAP_SEC = 60.0   # asked every third round (review of 759dee9, point 1)
UNINVOLVED_MIN_ANSWERS = 3
LIFECYCLE_MAX_AGE_SEC = 60     # lifecycle_controller.OBSERVATION_MAX_AGE_SEC
EXPECTED_DRIFT = {"delete": "deleted", "scale": "replicas"}


def load(path):
    try:
        with open(path) as stream:
            return [json.loads(line) for line in stream if line.strip()]
    except (OSError, ValueError):
        return []


def _epoch(value):
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def _gaps(times, start, end):
    edges = [start, *sorted(t for t in times if start <= t <= end), end]
    return max(b - a for a, b in zip(edges, edges[1:]))


def judge(records, variant, case, target_deployment, target_robot, uninvolved_deployments,
          uninvolved_robots, target_module="", events=(), audit=()):
    inject = next((r for r in records if r["kind"] == "inject"), None)
    validity, isolation, observability = [], [], []
    if inject is None:
        return {"verdict": "INCONCLUSIVE", "reasons": "no injection record"}
    t0, deadline = inject["m0"], inject["deadline_m"]
    rel = lambda m: None if m is None else round(m - t0, 1)
    # Only what was observed within the window counts (review of 759dee9, point 2).
    records = [r for r in records if r.get("m1") is None or r["kind"].startswith("baseline")
               or r["kind"] == "inject" or r["m1"] <= deadline]
    if inject.get("rc") != 0:
        validity.append(f"injection exit code {inject.get('rc')}: {inject.get('stderr')}")

    base_inv = next((r for r in records if r["kind"] == "baseline_inventory"), {})
    base_health = [r for r in records if r["kind"] == "baseline_health"]
    all_deployments = [target_deployment, *uninvolved_deployments]
    not_ready = [d for d in all_deployments if (base_inv.get("deployments", {}).get(d) or {}).get("readyReplicas", 0) < 1]
    if not_ready or "error" in base_inv:
        validity.append(f"baseline: Deployments not ready {not_ready}")
    unhealthy = [r["robot"] for r in base_health if r["result"] != "positive"]
    if len(base_health) != 1 + len(uninvolved_robots) or unhealthy:
        validity.append(f"baseline: health not positive for {unhealthy or 'some robot'}")

    inventories = [r for r in records if r["kind"] == "inventory"]
    good = [r for r in inventories if "error" not in r]
    # From the start of the injection: its own duration is an interval to observe
    # like any other (review of 759dee9, point 3).
    gap = _gaps([r["m1"] for r in good], t0, deadline)
    if gap > INVENTORY_GAP_SEC:
        validity.append(f"inventory reads: gap {gap:.1f}s above {INVENTORY_GAP_SEC}s")
    target_health = [r for r in records if r["kind"] == "health" and r["robot"] == target_robot]
    attempted = [r["m0"] for r in target_health if r["result"] != "error"]
    hgap = _gaps(attempted, t0, deadline)
    if hgap > HEALTH_GAP_SEC:
        validity.append(f"target health attempts: gap {hgap:.1f}s above {HEALTH_GAP_SEC}s")
    # Isolation needs observations too (review of 759dee9, point 1): each uninvolved
    # robot answered, collection errors excluded, often enough across the window.
    uninvolved_gaps = {}
    for robot in uninvolved_robots:
        answered = [r["m0"] for r in records if r["kind"] == "health" and r["robot"] == robot
                    and r["result"] != "error" and r["m0"] >= t0]
        uninvolved_gaps[robot] = round(_gaps(answered, t0, deadline), 1)
        if len(answered) < UNINVOLVED_MIN_ANSWERS or uninvolved_gaps[robot] > UNINVOLVED_HEALTH_GAP_SEC:
            validity.append(f"{robot}: {len(answered)} health answers, gap {uninvolved_gaps[robot]}s "
                            f"(needs >= {UNINVOLVED_MIN_ANSWERS}, gaps <= {UNINVOLVED_HEALTH_GAP_SEC}s)")

    # -- recovery of the target
    base_target = base_inv.get("deployments", {}).get(target_deployment) or {}
    base_pods = {p["uid"] for p in base_inv.get("pods", []) if p["deployment"] == target_deployment}
    after = [r for r in good if r["m0"] >= inject["m1"]]
    t_object = t_pod = t_service = None
    current_pod = None
    for r in after:
        d = r["deployments"].get(target_deployment)
        if t_object is None and d:
            repaired = (d["uid"] != base_target.get("uid")) if case == "delete" else \
                (d["replicas"] == base_target.get("replicas") and d["generation"] > base_target.get("generation", 0))
            if repaired:
                t_object = r["m1"]
        if t_object is not None and t_pod is None and d:
            pods = [p for p in r["pods"] if p["deployment"] == target_deployment]
            # delete: the Pod must belong to the new Deployment; scale: the original
            # Pod may be kept by a repair faster than its removal, if it is the
            # current Deployment's, Ready and not terminating (review of 759dee9, 4).
            fresh = [p for p in pods if p["deployment_uid"] == d["uid"] and p["ready"] and not p["terminating"]
                     and (case == "scale" or p["uid"] not in base_pods)]
            stale = [p for p in pods if p["terminating"] or p["deployment_uid"] != d["uid"]
                     or (case == "delete" and p["uid"] in base_pods)]
            if (fresh and not stale and (d.get("observedGeneration") or 0) >= (d.get("generation") or 0)
                    and d.get("readyReplicas", 0) >= 1):
                t_pod, current_pod = r["m1"], fresh[0]
    after_health = [r for r in target_health if r["m0"] >= inject["m1"]]
    if t_pod is not None:
        t_service = next((r["m1"] for r in after_health if r["result"] == "positive" and r["m0"] >= t_pod), None)
    first_positive = next((r for r in after_health if r["result"] == "positive"), None)
    last_negative = None
    for r in after_health:
        if r["result"] == "negative" and (t_service is None or r["m1"] <= t_service):
            last_negative = r
    recovered = t_service is not None

    lifecycle_available = None
    if variant == "b" and current_pod:
        key_part = current_pod["name"].replace("-", "_")
        for r in after:
            if r["m1"] < t_pod:
                continue
            record = next((v for k, v in (r.get("lifecycle") or {}).items() if k.endswith(key_part)), None)
            seen = _epoch(record.get("lastObservedTime")) if record else None
            if record and record.get("observedLifecycleState") == "Active" and seen is not None \
                    and _epoch(r["w1"]) - seen <= LIFECYCLE_MAX_AGE_SEC:
                lifecycle_available = r["m1"]
                break

    # -- isolation of the other robots and of PX4
    base_uninvolved = {p["uid"]: p for p in base_inv.get("pods", []) if p["deployment"] in uninvolved_deployments}
    base_px4 = {p["uid"]: p for p in base_inv.get("px4", [])}
    for r in good:
        for name in uninvolved_deployments:
            d, b = r["deployments"].get(name), base_inv.get("deployments", {}).get(name) or {}
            if not d or d["uid"] != b.get("uid"):
                isolation.append(f"{name}: Deployment changed or missing at +{rel(r['m1'])}s")
        pods = {p["uid"]: p for p in r["pods"] if p["deployment"] in uninvolved_deployments}
        if set(pods) != set(base_uninvolved) or any(pods[u]["restarts"] != base_uninvolved[u]["restarts"] for u in pods):
            isolation.append(f"uninvolved Pods changed at +{rel(r['m1'])}s")
        px4 = {p["uid"]: p for p in r["px4"]}
        if set(px4) != set(base_px4) or any(px4[u]["restarts"] != base_px4[u]["restarts"] for u in px4):
            isolation.append(f"PX4 Pods changed at +{rel(r['m1'])}s")
    single_negatives = []
    for robot in uninvolved_robots:
        answers = [r for r in records if r["kind"] == "health" and r["robot"] == robot and r["result"] != "error"]
        for a, b in zip(answers, answers[1:]):
            if a["result"] == "negative" and b["result"] == "negative":
                isolation.append(f"{robot}: two consecutive negative health answers at +{rel(b['m1'])}s")
                break
        single_negatives += [f"{robot}@+{rel(a['m1'])}s" for a in answers if a["result"] == "negative"]
    isolation = sorted(set(isolation))

    # -- B: the drift is observable
    if variant == "b":
        t_inject_w = _epoch(inject["w0"])
        drift_events = [e for e in events if e.get("reason") == "DriftHealed"
                        and e.get("involvedObject", {}).get("name") == target_module
                        and (_epoch(e.get("lastTimestamp") or e.get("eventTime") or "") or 0) >= int(t_inject_w)]
        if not drift_events:
            observability.append("no DriftHealed Event on the ROSModule after the injection")
        records_ = [a for a in audit if a.get("event_type") == "OutOfBandDrift" and a.get("rosmodule") == target_module
                    and a.get("drift") == EXPECTED_DRIFT[case]
                    and (_epoch(a.get("timestamp_utc") or "") or 0) >= int(t_inject_w)]
        if len(records_) < 1:
            observability.append(f"no OutOfBandDrift audit record '{EXPECTED_DRIFT[case]}' for {target_module} "
                                 f"after the injection")

    if validity:
        verdict = "INCONCLUSIVE"
    elif isolation or (variant == "b" and (not recovered or observability)):
        verdict = "FAIL"
    else:
        verdict = "PASS"
    return {
        "verdict": verdict,
        "reasons": "; ".join(validity + isolation + (observability if variant == "b" else [])
                             + ([] if recovered or variant == "a" else ["not recovered within the window"])) or "none",
        "valid": not validity, "isolated": not isolation, "recovered": recovered,
        "recovery": ("recovered" if recovered else f"not recovered within {round(deadline - t0)}s"),
        "object_repaired_s": rel(t_object), "pod_ready_s": rel(t_pod), "service_available_s": rel(t_service),
        "last_negative_s": rel(last_negative["m1"]) if last_negative else None,
        "first_positive_s": rel(first_positive["m1"]) if first_positive else None,
        "b_lifecycle_available_s": rel(lifecycle_available),
        "injection_command_s": round(inject["m1"] - inject["m0"], 2),
        "window_s": round(deadline - t0), "inventory_reads": len(good), "inventory_errors": len(inventories) - len(good),
        "max_inventory_gap_s": round(gap, 1), "target_health_calls": len(target_health),
        "max_target_health_gap_s": round(hgap, 1), "uninvolved_health_gaps_s": uninvolved_gaps,
        "uninvolved_single_negatives": single_negatives,
        "drift_observability": ("n/a (A)" if variant == "a" else ("ok" if not observability else "; ".join(observability))),
    }


def report(result, variant, case, target):
    """The cell's REPORT.md from the judge's result."""
    name = {"a": "A (imperativa)", "b": "B (dichiarativa: CRD + Fleet Operator)"}[variant]
    sec = lambda v: "n/d" if v is None else f"{v} s"
    rows = [
        ("Esito", result["verdict"]), ("Variante", name), ("Caso", case), ("Target", target),
        ("Motivi", result.get("reasons")),
        ("Misura valida / isolamento", f"{result.get('valid')} / {result.get('isolated')}"),
        ("Recupero", result.get("recovery")),
        ("Oggetto ripristinato", sec(result.get("object_repaired_s"))),
        ("Pod corrente pronto", sec(result.get("pod_ready_s"))),
        ("Servizio disponibile", sec(result.get("service_available_s"))),
        ("Ultima risposta negativa / prima positiva",
         f"{sec(result.get('last_negative_s'))} / {sec(result.get('first_positive_s'))}"),
        ("B: disponibilita' lifecycle per Pod (informativa)", sec(result.get("b_lifecycle_available_s"))),
        ("Osservabilita' del drift", result.get("drift_observability")),
        ("Comando d'iniezione", sec(result.get("injection_command_s"))),
        ("Finestra", sec(result.get("window_s"))),
        ("Letture d'inventario (errori), buco max",
         f"{result.get('inventory_reads')} ({result.get('inventory_errors')}), {sec(result.get('max_inventory_gap_s'))}"),
        ("Chiamate di health al target, buco max",
         f"{result.get('target_health_calls')}, {sec(result.get('max_target_health_gap_s'))}"),
        ("Negativi singoli dei robot non coinvolti",
         ", ".join(result.get("uninvolved_single_negatives") or []) or "nessuno"),
    ]
    lines = [f"# S1 Out-of-band Drift -- caso {case}, variante {variant.upper()}", "",
             "| Campo | Valore |", "| --- | --- |"]
    lines += [f"| {k} | {v} |" for k, v in rows]
    lines += ["", "Tempi dall'inizio dell'iniezione, orologio monotono dell'host. Criteri in "
              "docs/R10_S1_DRIFT.md e scripts/s1_drift_judge.py; campioni in s1-samples.jsonl. "
              "Un mancato ripristino si riporta entro la finestra osservata: nessun MTTR infinito."]
    return "\n".join(lines) + "\n"


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    if argv and argv[0] == "--report":
        print(report(json.load(open(argv[1])), argv[2], argv[3], argv[4]), end="")
        return 0
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("samples")
    parser.add_argument("--variant", choices=("a", "b"), required=True)
    parser.add_argument("--case", choices=("delete", "scale"), required=True)
    parser.add_argument("--target-robot", required=True)
    parser.add_argument("--target-deployment", required=True)
    parser.add_argument("--target-module", default="")
    parser.add_argument("--uninvolved-robots", nargs="+", required=True)
    parser.add_argument("--uninvolved-deployments", nargs="+", required=True)
    parser.add_argument("--events", default="")
    parser.add_argument("--audit", default="")
    args = parser.parse_args(argv)
    try:
        events = json.load(open(args.events)).get("items", []) if args.events else []
    except (OSError, ValueError):
        events = []
    result = judge(load(args.samples), args.variant, args.case, args.target_deployment, args.target_robot,
                   args.uninvolved_deployments, args.uninvolved_robots, args.target_module, events,
                   load(args.audit) if args.audit else [])
    print(json.dumps(result, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
