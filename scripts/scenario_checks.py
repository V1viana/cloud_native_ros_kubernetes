#!/usr/bin/env python3
"""Scenario checks that need correlation (R9, decisions D4 and D4b).

Viviana (2026-09-26): B's runners get the checks A already has (G1-G4 in
docs/R9_FUNCTIONAL_REVALIDATION.md), with correlation between policy,
incident, outcome and audit; E2 gets an audit trail in both variants (D4b),
checked with the same incident check in A and in B:
  no-incident AUDIT
      no incident record at all (E0: nothing happened);
  incident AUDIT POLICY CORRELATION_ID EVENT_TYPE OUTCOME ROLLBACK
      the incident's id (B: the policy's status.correlationId; A: the
      DeploymentRequest's) has exactly one incident_started and one
      incident_completed of that policy and event type, the expected outcome
      and rollback_performed, and the operator notification with the same id;
  labelled-event EVENTS_JSON REASON LABEL VALUE
      A (E2): the Kubernetes Event A's Application Manager emits with its
      action, with this reason and this correlation-id label;
  event EVENTS_JSON OBJECT REASON
      at least one Kubernetes Event with this reason on the object;
  remediation EVENTS_JSON POLICY_JSON AGENT_DEPLOYMENT_JSON FAULT_START_EPOCH
      E2 (G4): the E2 cluster has no audit writer in either variant, so the
      response to the fault is correlated across objects instead -- the
      policy's RemediationStarted Event, its status (correlationId,
      restartedAt and diagnosticJobName, both after the fault) and the
      restart annotation the policy put on the Agent's Deployment after the
      fault. It attests the response, not the detection Event variant A emits;
  cadence SAMPLES_JSONL FAULT_START_EPOCH
      reported, never judged: the State Bridge's report cadence seen in the
      samples (distinct metricsObservedTime) and the times from the fault.
Each check prints "PASS|detail" or "FAIL|problems" and exits 0 or 1.
Tested offline: operator/tests/test_scenario_checks.py.
"""

import json
import sys


def load_audit(path):
    try:
        with open(path) as stream:
            return [json.loads(line) for line in stream if line.strip()]
    except (OSError, ValueError):
        return []


def no_incident(audit):
    found = [r for r in audit if r.get("record_type") in ("incident_started", "incident_completed")]
    return [f"{len(found)} record d'incidente: {sorted({r.get('policy_id') for r in found}, key=str)}"] if found else []


def incident(audit, policy, correlation_id, event_type, outcome, rollback):
    if not correlation_id:
        return [f"la policy {policy} non riporta un correlationId"]
    problems = []
    mine = [r for r in audit if r.get("correlation_id") == correlation_id]
    for kind, expected in (("incident_started", 1), ("incident_completed", 1), ("operator_notification", 1)):
        records = [r for r in mine if r.get("record_type") == kind]
        if len(records) != expected:
            problems.append(f"{len(records)} {kind} con {correlation_id}")
    for r in (r for r in mine if r.get("record_type") in ("incident_started", "incident_completed")):
        if r.get("policy_id") != policy or r.get("event_type") != event_type:
            problems.append(f"{r.get('record_type')} di {r.get('policy_id')}/{r.get('event_type')}")
    for r in (r for r in mine if r.get("record_type") in ("incident_completed", "operator_notification")):
        if r.get("outcome") != outcome:
            problems.append(f"{r.get('record_type')} con esito {r.get('outcome')}, atteso {outcome}")
        if r.get("rollback_performed") is not rollback:
            problems.append(f"{r.get('record_type')} con rollback_performed {r.get('rollback_performed')}")
    return problems


def event(events, name, reason):
    count = sum(int(e.get("count") or 1) for e in events
                if e.get("involvedObject", {}).get("name") == name and e.get("reason") == reason)
    return [] if count else [f"nessun Event {reason} su {name}"]


def labelled_event(events, reason, label, value):
    found = [e for e in events if e.get("reason") == reason
             and (e.get("metadata", {}).get("labels") or {}).get(label) == value]
    return [] if found else [f"nessun Event {reason} con {label}={value}"]


def _epoch(value):
    from datetime import datetime
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def remediation(events, policy, agent_deployment, fault_start):
    name = policy.get("metadata", {}).get("name", "")
    status = policy.get("status", {})
    problems = event(events, name, "RemediationStarted")
    for field in ("correlationId", "restartedAt", "diagnosticJobName"):
        if not status.get(field):
            problems.append(f"status.{field} assente")
    # second resolution on both sides: the fault's second counts as after it
    restarted = _epoch(status.get("restartedAt"))
    if restarted is not None and restarted < int(fault_start):
        problems.append(f"restartedAt {status['restartedAt']} prima del guasto")
    annotation = ((agent_deployment.get("spec", {}).get("template", {}).get("metadata") or {})
                  .get("annotations") or {}).get("dronekube.io/restarted-at")
    if not annotation:
        problems.append("Deployment dell'Agent senza annotazione di riavvio")
    elif (_epoch(annotation) or 0) < int(fault_start):
        problems.append(f"annotazione di riavvio {annotation} prima del guasto")
    return problems


def cadence(samples, fault_start):
    import statistics
    times = sorted({_epoch(s["observed"]) for s in samples if s.get("observed") and _epoch(s["observed"])})
    gaps = [b - a for a, b in zip(times, times[1:])]
    first = lambda state: next((s["t"] for s in samples if s.get("state") == state), None)
    fmt = lambda t: f"{t - fault_start:.1f}s" if t is not None else "n/d"
    return (f"{len(times)} report distinti, intervallo mediano "
            f"{statistics.median(gaps):.1f}s (min {min(gaps):.1f}, max {max(gaps):.1f})" if gaps
            else f"{len(times)} report distinti") + \
        f"; dal guasto: Remediating {fmt(first('Remediating'))}, Recovered {fmt(first('Recovered'))}"


def main(argv):
    command = argv[1]
    if command == "no-incident":
        problems, detail = no_incident(load_audit(argv[2])), "nessun record d'incidente"
    elif command == "incident":
        _, _, path, policy, cid, event_type, outcome, rollback = argv
        problems = incident(load_audit(path), policy, cid, event_type, outcome, rollback == "true")
        detail = f"{cid}: incidente, esito {outcome} e notifica correlati"
    elif command == "event":
        try:
            items = json.load(open(argv[2])).get("items", [])
        except (OSError, ValueError):
            items = []
        problems, detail = event(items, argv[3], argv[4]), f"Event {argv[4]} su {argv[3]}"
    elif command == "labelled-event":
        try:
            items = json.load(open(argv[2])).get("items", [])
        except (OSError, ValueError):
            items = []
        problems = labelled_event(items, argv[3], argv[4], argv[5])
        detail = f"Event {argv[3]} con {argv[4]}={argv[5]}"
    elif command == "remediation":
        def read(path):
            try:
                return json.load(open(path))
            except (OSError, ValueError):
                return {}
        problems = remediation(read(argv[2]).get("items", []), read(argv[3]), read(argv[4]), float(argv[5]))
        detail = "Event RemediationStarted, status della policy e riavvio dell'Agent correlati dopo il guasto"
    elif command == "cadence":
        samples = load_audit(argv[2])          # same JSON-lines reader
        print("PASS|" + cadence(samples, float(argv[3])))
        return 0
    else:
        raise SystemExit(f"unknown check {command}")
    print(("FAIL|" + "; ".join(problems)) if problems else ("PASS|" + detail))
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
