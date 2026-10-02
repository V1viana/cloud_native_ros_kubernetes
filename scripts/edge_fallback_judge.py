#!/usr/bin/env python3
"""Judges of scripts/run_edge_fallback_check.sh (R5, variant B), testable offline.

Each one applies exactly the criteria of that script's header. Until the run of
14fc04c they were inline in the script, and the fallback judge applied phase 2's
ordering check ("no sample without the edge ROSModule while the onboard is not
Active") to phase 4 as well, where the fault itself deletes the edge: a
constraint that phase 4's pre-registered criteria do not contain (Viviana,
2026-09-25: keep that run as a judge error, fix the judge, repeat phase 4 only).
The same move aligned RolledBack's limit to the header's 60s (it allowed 65s).

Samples: one JSON object per line, as sample() writes them. Timings are
reported separately -- detection, onboard requested, onboard observed Active,
RolledBack -- since the edge-loss tolerance counts from detection, not from the
fault (the hung Pod of phase 3 is detected only once its records age out).
Prints "VERDICT|detail".
"""

import json
import sys
from datetime import datetime

FALLBACK_TO_ROLLEDBACK_SEC = 60


def _ts(value):
    return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp() if value else None


def _rows(path, label):
    return [r for r in map(json.loads, open(path)) if r["label"] == label]


def _onboard_active(row):
    return row["onboard_target"] == "Active" and row["onboard_observed"] == "Active"


def _timeline(rows, fault):
    unavail = next((_ts(r["edgeUnavailableSince"]) for r in rows if r["edgeUnavailableSince"]), None)
    fb = next((_ts(r["fallbackSince"]) for r in rows if r["fallbackSince"]), None)
    done = next((r for r in rows if r["state"] in ("RolledBack", "FallbackFailed")), None)
    onboard = next((r for r in rows if fb is not None and r["t"] >= fb and _onboard_active(r)), None)
    rel = lambda v: f"{v - fault:.0f}s" if v is not None else "n/d"
    detail = (f"dal guasto: perdita rilevata {rel(unavail)}, fallback richiesto {rel(fb)}"
              + (f" ({fb - unavail:.0f}s dopo la rilevazione)" if fb is not None and unavail is not None else "")
              + f", onboard Active osservato {rel(onboard['t'] if onboard else None)}, "
              f"{done['state'] if done else 'nessun esito'} {rel(done['t'] if done else None)}")
    return unavail, fb, done, detail


def _rolled_back(done, fb, problems):
    if done is None or done["state"] != "RolledBack":
        problems.append(f"esito finale {done['state'] if done else 'nessuno'}")
    elif fb is not None and done["t"] - fb > FALLBACK_TO_ROLLEDBACK_SEC:
        problems.append(f"RolledBack {done['t'] - fb:.0f}s dopo la richiesta "
                        f"(max {FALLBACK_TO_ROLLEDBACK_SEC}s)")


def _audit_ok(records, policy):
    return any(r.get("record_type") == "incident_completed" and r.get("policy_id") == policy
               and r.get("outcome") == "analytics_migration_failed"
               and r.get("rollback_performed") is True for r in records)


def _verdict(problems, detail):
    return ("FAIL|" + "; ".join(problems) + "; " if problems else "PASS|") + detail


def judge_short(rows):
    """Phase 1: loss detected, cleared while Migrating, never left Migrating."""
    lost = [i for i, r in enumerate(rows) if r["edgeUnavailableSince"]]
    cleared = lost and any(not r["edgeUnavailableSince"] and r["state"] == "Migrating"
                           for r in rows[lost[-1] + 1:])
    # a failed sample (no state read) is a missing sample, not a state change
    left = [r["state"] for r in rows if r["state"] not in ("Migrating", None)]
    onboard = [r["onboard_target"] for r in rows if r["onboard_target"] not in ("Inactive", None)]
    duration = (rows[lost[-1] + 1]["t"] - _ts(rows[lost[0]]["edgeUnavailableSince"])) if cleared else None
    if not lost:
        return "INCONCLUSIVE|perdita mai rilevata"
    if not cleared and not left:
        return "INCONCLUSIVE|perdita non rientrata entro 150s"
    if left or onboard:
        over = duration is None or duration > 90
        return (("INCONCLUSIVE" if over else "FAIL")
                + f"|stati fuori da Migrating {sorted(set(left))}, target onboard {sorted(set(onboard))}")
    return (f"PASS|perdita rilevata e rientrata in circa {duration:.0f}s; "
            "sempre Migrating, onboard sempre Inactive")


def judge_lost(rows, fault, max_detect, audit, policy):
    """Phases 2 and 3: the edge is lost, not deleted, so it must stay until the onboard serves."""
    unavail, fb, done, detail = _timeline(rows, fault)
    problems = []
    if unavail is None:
        problems.append("perdita mai rilevata")
    elif unavail - fault > max_detect:
        problems.append(f"rilevata dopo {unavail - fault:.0f}s (max {max_detect:.0f}s)")
    if fb is None:
        problems.append("FallingBack mai osservato")
    else:
        if unavail is not None and not 30 <= fb - unavail <= 45:
            problems.append(f"fallback {fb - unavail:.0f}s dopo la perdita (atteso 30-45s)")
        reason = next((r["fallbackReason"] for r in rows if r["fallbackReason"]), None)
        if reason != "EdgeLost":
            problems.append(f"motivo {reason}, atteso EdgeLost")
    _rolled_back(done, fb, problems)
    bad = [r for r in rows if fb is not None and r["t"] >= fb
           and not r["edge_exists"] and not _onboard_active(r)]
    if bad:
        problems.append(f"{len(bad)} campioni senza edge e con onboard non Active")
    if not _audit_ok(audit, policy):
        problems.append("audit incident_completed con rollback assente")
    return _verdict(problems, detail)


def judge_deleted(rows, fault, audit, policy):
    """Phase 4: the fault deletes the edge ROSModule, so its absence is not judged.
    Criteria: FallingBack/EdgeModuleMissing within 15s of the deletion, RolledBack
    within 60s of fallbackSince, onboard target and observed Active from RolledBack on."""
    _, fb, done, detail = _timeline(rows, fault)
    problems = []
    if fb is None:
        problems.append("FallingBack mai osservato")
    else:
        if fb - fault > 15:
            problems.append(f"fallback {fb - fault:.0f}s dopo la cancellazione (max 15s)")
        reason = next((r["fallbackReason"] for r in rows if r["fallbackReason"]), None)
        if reason != "EdgeModuleMissing":
            problems.append(f"motivo {reason}, atteso EdgeModuleMissing")
    _rolled_back(done, fb, problems)
    after = [r for r in rows if done is not None and r["t"] >= done["t"]]
    if any(not _onboard_active(r) for r in after):
        problems.append("onboard non Active dopo RolledBack")
    audit_note = "; audit con rollback " + ("presente" if _audit_ok(audit, policy) else "ASSENTE (riportato)")
    return _verdict(problems, detail + audit_note)


def main(argv):
    kind, samples, label = argv[1], argv[2], argv[3]
    rows = _rows(samples, label)
    if kind == "short":
        print(judge_short(rows))
        return 0
    fault = float(argv[4])
    if kind == "lost":
        max_detect, audit_path, policy = float(argv[5]), argv[6], argv[7]
    else:
        audit_path, policy = argv[5], argv[6]
    try:
        audit = [json.loads(line) for line in open(audit_path) if line.strip()]
    except OSError:
        audit = []
    print(judge_lost(rows, fault, max_detect, audit, policy) if kind == "lost"
          else judge_deleted(rows, fault, audit, policy))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
