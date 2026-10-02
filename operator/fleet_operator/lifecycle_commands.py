"""LifecycleController's planner: the controller drives the lifecycle
(docs/LIFECYCLE_COMMAND_CONTRACT_DRAFT.md, sections 2, 4, 5 and 7; the proposal, r.
510-514). Pure: no kopf, no kubernetes import; the caller supplies the ROSModule
client, the current Pods and the ROSLifecyclePolicy.

For each current instance, from a VALID observation (fresh, of the current generation,
target and Pod, after the Pod's creation) and the bridge's receipt, the controller:
charges the budget once per command whose Accepted receipt persisted; waits while a
command is in flight (Accepted, before deadline + GRACE); decides only with an
observation newer than the outcome; advances one adjacent step, retries with backoff
until 1 + maxTransitionRetries charged attempts (then Exhausted), replaces a command
never delivered (no receipt past deadline + GRACE) with a new ID and no charge,
signalled from the third one and retried at a bounded rate (at most every 30 s);
applies the readiness gate only with a configured probe. Every issue is a patch of the
whole decision with a resourceVersion precondition, never Kopf's final patch: on 409
nothing is written and the next tick re-reads. Commands of Pods no longer current are
removed; receipts and observations of Pods that no longer exist are deleted (the one
exception to one writer per field).
Tested offline: operator/tests/test_lifecycle_commands.py.
"""

import copy
from datetime import datetime
import uuid

GRACE_SEC = 2.0                   # operational margin only: a late RPC stays possible
COOLDOWN_SEC = 6.0                # backoff base, as the bridge's former transition_retry_cooldown_sec
OBSERVATION_MAX_AGE_SEC = 60
UNDELIVERED_ALERT = 3
UNDELIVERED_MAX_INTERVAL_SEC = 30.0
ORDER = ["Unconfigured", "Inactive", "Active"]
TERMINAL = ("Succeeded", "Failed", "Unconfirmed")


class Conflict(Exception):
    """409 from the API server: the resourceVersion precondition failed."""


def next_step(from_state, target):
    """The single adjacent state from from_state towards target, or None."""
    if target == "Finalized":
        return "Finalized" if from_state in ORDER else None
    if from_state not in ORDER or target not in ORDER:
        return None
    i, j = ORDER.index(from_state), ORDER.index(target)
    return None if i == j else ORDER[i + (1 if j > i else -1)]


def epoch(value):
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def valid_observation(record, generation, target, pod_uid, created, now):
    """(state, ready, observed_at) of a valid observation, else None."""
    if not record:
        return None
    t = epoch(record.get("lastObservedTime"))
    if (t is None or record.get("generation") != generation or record.get("target") != target
            or record.get("podUID") != pod_uid or (created is not None and t < created)
            or not 0 <= now - t <= OBSERVATION_MAX_AGE_SEC):
        return None
    return record.get("observedLifecycleState"), record.get("observedReady"), t


def observation_lost(record, pod_uid, created, now):
    """The bridge stopped reporting: its last observation of this Pod (whatever its
    generation) is older than OBSERVATION_MAX_AGE_SEC, or there is none for a Pod older
    than that. A recent observation of a previous generation is only pending."""
    t = epoch((record or {}).get("lastObservedTime")) if (record or {}).get("podUID") == pod_uid else None
    if t is None:
        return created is not None and now - created > OBSERVATION_MAX_AGE_SEC
    return now - t > OBSERVATION_MAX_AGE_SEC


def _budget_key(pod_uid, generation, target, from_state):
    return {"podUID": pod_uid, "generation": generation, "target": target, "fromState": from_state}


def _new_budget(key):
    return {"key": key, "charged": 0, "lastChargedCommandId": None, "undelivered": 0, "retryAt": None}


def plan_instance(record, receipt, observation, *, pod_uid, generation, target, policy, gate, now,
                  new_id, lost=False):
    """-> (decision, record or None). record None means: remove the command."""
    timeout = float(policy.get("transitionTimeoutSec", 15))
    max_retries = int(policy.get("maxTransitionRetries", 3))
    rec = copy.deepcopy(record) if record else None
    if rec and rec.get("podUID") != pod_uid:
        rec = None                                       # another Pod behind the same key
    # Only an ISSUED record owns its receipt: a WaitingReadiness, Backoff or Exhausted
    # record is a plan, never executed (live T3 on 212172f: a WaitingReadiness record
    # still carrying the configure's commandId had that receipt charged to the
    # activate's new budget -- 4 charged, 3 activate RPCs before Exhausted).
    mine = bool(rec and rec.get("phase") == "Issued" and receipt
                and receipt.get("commandId") == rec.get("commandId"))
    phase = receipt.get("phase") if mine else None

    # 1. charge: once per command whose Accepted persisted; any receipt closes an
    #    undelivered episode
    if mine:
        budget = rec["budget"]
        budget["undelivered"] = 0
        if phase != "Rejected" and budget.get("lastChargedCommandId") != rec["commandId"]:
            budget["charged"] = budget.get("charged", 0) + 1
            budget["lastChargedCommandId"] = rec["commandId"]

    # 2. in flight: nothing replaces it before deadline + GRACE
    if rec and rec.get("phase") == "Issued" and phase == "Accepted" and now < rec["deadline"] + GRACE_SEC:
        return "in_flight", rec
    if observation is None:                              # no new issue; never counted as undelivered
        return ("observation_lost" if lost else "observation_pending"), rec
    state, ready, observed_at = observation
    if state not in ORDER + ["Finalized"]:
        return "unavailable", rec

    # 3. the current command's outcome, judged only on a NEWER valid observation
    if rec and rec.get("phase") == "Issued":
        obsolete = rec.get("generation") != generation or rec.get("target") != target
        if phase is None:                                # issued, no receipt
            if now < rec["deadline"] + GRACE_SEC:
                if state == target:
                    return "settled", None
                if not obsolete:
                    return "issued", rec
                # replaced (target or generation changed): the bridge's precondition
                # makes the replacement safe
            else:
                if observed_at <= rec["deadline"] + GRACE_SEC:
                    return "waiting_observation", rec
                budget = rec["budget"]
                budget["undelivered"] = budget.get("undelivered", 0) + 1
                budget["retryAt"] = rec["deadline"] + GRACE_SEC + min(
                    UNDELIVERED_MAX_INTERVAL_SEC, COOLDOWN_SEC * 2 ** (budget["undelivered"] - 1))
                rec["phase"] = "Backoff"
                rec["backoffReason"] = "CommandUndelivered"
        elif phase == "Rejected":
            if observed_at <= epoch(receipt.get("completedAt")):
                return "waiting_observation", rec
        else:                                            # a terminal outcome, or Accepted expired
            since = (rec["deadline"] + GRACE_SEC if phase == "Accepted"
                     else epoch(receipt.get("completedAt")))
            if observed_at <= since:
                return "waiting_observation", rec
            if state == rec["fromState"] and not obsolete:
                budget = rec["budget"]
                budget["retryAt"] = since + max(timeout, COOLDOWN_SEC * 2 ** (budget["charged"] - 1))
                rec["phase"] = "Backoff"
                rec["backoffReason"] = "TransitionFailed"

    # 4. plan from the observed state
    if state == target:
        return "settled", None
    to_state = next_step(state, target)
    if to_state is None:
        return "no_step", rec
    key = _budget_key(pod_uid, generation, target, state)
    same_key = bool(rec and rec.get("budget", {}).get("key") == key)
    budget = rec["budget"] if same_key else _new_budget(key)
    # a plan under a NEW budget carries nothing of the previous step's command
    prior = rec if same_key else {}
    base = {"podUID": pod_uid, "generation": generation, "target": target, "fromState": state,
            "toState": to_state, "readinessGate": gate, "timeoutSec": timeout, "budget": budget}
    if budget["charged"] >= 1 + max_retries:
        return "exhausted", {**prior, **base, "phase": "Exhausted"}
    if budget.get("retryAt") is not None and now < budget["retryAt"]:
        kept = {**prior, **base, "phase": "Backoff"}
        return ("undelivered_backoff" if kept.get("backoffReason") == "CommandUndelivered"
                else "backoff"), kept
    if state == "Inactive" and to_state == "Active" and gate and ready is not True:
        return "waiting_readiness", {**prior, **base, "phase": "WaitingReadiness"}
    budget["retryAt"] = None
    issued = {**base, "commandId": new_id(), "issuedAt": now, "deadline": now + timeout, "phase": "Issued"}
    return "issued_new", issued


def replacement(old, new):
    """A merge patch keeps what it does not mention: to REPLACE a record, every field of
    the old one missing from the new one is deleted explicitly (null), nested too."""
    if not isinstance(old, dict) or not isinstance(new, dict):
        return new
    out = {key: replacement(old.get(key), value) for key, value in new.items()}
    out.update({key: None for key in old if key not in new})
    return out


def plan(module, instances, all_pod_uids, policy, now, new_id):
    """-> (patch, decisions, records) for the whole ROSModule; records are the command
    records the patch leaves per key (None: removed)."""
    meta, spec = module["metadata"], module.get("spec") or {}
    status = module.get("status") or {}
    commands = status.get("lifecycleCommands") or {}
    receipts = status.get("lifecycleReceipts") or {}
    observations = status.get("lifecycleInstances") or {}
    generation, target = meta.get("generation"), spec.get("lifecycleTarget")
    gate = bool(policy.get("requireReadinessBeforeActive", True)
                and (spec.get("probes") or {}).get("readiness"))
    patch, decisions, records = {}, {}, {}

    for key, pod in sorted(instances.items()):
        observation = valid_observation(observations.get(key), generation, target, pod["uid"],
                                        pod.get("created"), now)
        lost = observation is None and observation_lost(observations.get(key), pod["uid"],
                                                        pod.get("created"), now)
        decision, record = plan_instance(commands.get(key), receipts.get(key), observation,
                                         pod_uid=pod["uid"], generation=generation, target=target,
                                         policy=policy, gate=gate, now=now, new_id=new_id, lost=lost)
        decisions[key] = decision
        records[key] = record
        if record != commands.get(key):
            patch.setdefault("lifecycleCommands", {})[key] = replacement(commands.get(key), record)
    for key in commands:
        if key not in instances:                        # its Pod is no longer current
            patch.setdefault("lifecycleCommands", {})[key] = None
            decisions.setdefault(key, "removed_not_current")
            records[key] = None
    for field, entries in (("lifecycleReceipts", receipts), ("lifecycleInstances", observations)):
        for key, entry in entries.items():
            if (entry or {}).get("podUID") not in all_pod_uids:
                patch.setdefault(field, {})[key] = None  # the Pod is gone: nobody else can
    return patch, decisions, records


SIGNAL_PRIORITY = ("TransitionFailed", "CommandUndelivered", "ObservationLost", "LifecycleUnavailable",
                   "ReadinessPending", "TransitionPending")


def instance_signal(decision, record):
    """The condition reason one instance contributes (None: settled)."""
    if decision == "settled":
        return None
    if decision == "exhausted":
        return "TransitionFailed"
    if ((record or {}).get("budget") or {}).get("undelivered", 0) >= UNDELIVERED_ALERT:
        return "CommandUndelivered"
    if decision == "observation_lost":
        return "ObservationLost"
    if decision == "unavailable":
        return "LifecycleUnavailable"
    if decision == "waiting_readiness":
        return "ReadinessPending"
    return "TransitionPending"


def module_condition(signals):
    """(settled, reason) over the current instances: the worst reason wins."""
    present = [s for s in signals.values() if s]
    if not signals:
        return False, "ObservationPending"
    if not present:
        return True, "StateMatches"
    return False, min(present, key=SIGNAL_PRIORITY.index)


def reconcile(client, name, instances, all_pod_uids, policy, now, new_id=None):
    """One controller tick on one ROSModule. The decision is written in one patch with a
    resourceVersion precondition on the read it was made on; on 409 nothing is written."""
    new_id = new_id or (lambda: str(uuid.uuid4()))
    module = client.get_resource(name)
    patch, decisions, records = plan(module, instances, all_pod_uids, policy, now, new_id)
    commands = dict((module.get("status") or {}).get("lifecycleCommands") or {})
    commands.update(patch.get("lifecycleCommands") or {})
    signals = {key: instance_signal(decisions[key], commands.get(key)) for key in instances}
    status = module.get("status") or {}
    result = {"conflict": False, "written": False, "decisions": decisions, "signals": signals,
              "condition": module_condition(signals), "module": module,
              "records_before": dict(status.get("lifecycleCommands") or {}), "records_after": records,
              "receipts": dict(status.get("lifecycleReceipts") or {})}
    if not patch:
        return result
    try:
        client.patch_status(name, patch, resource_version=module["metadata"]["resourceVersion"])
    except Conflict:
        return {**result, "conflict": True}
    return {**result, "written": True}


OUTCOME_EVENTS = {"Succeeded": "LifecycleCommandSucceeded", "Failed": "LifecycleCommandFailed",
                  "Unconfirmed": "LifecycleCommandUnconfirmed", "Rejected": "LifecycleCommandRejected",
                  "Accepted": "LifecycleCommandExpired"}


def _command_outcome(old, new, receipt, key_removed):
    """The Event reason for an Issued record `old` that the written patch replaced."""
    if receipt and receipt.get("commandId") == old.get("commandId"):
        return OUTCOME_EVENTS.get(receipt.get("phase"), "LifecycleCommandUnconfirmed")
    if key_removed:
        return "LifecycleCommandPodGone"
    if new is None:
        return "LifecycleCommandSettled"
    if new.get("backoffReason") == "CommandUndelivered" and new.get("phase") == "Backoff":
        return "LifecycleCommandUndelivered"
    return "LifecycleCommandSuperseded"


def command_events(result, max_attempts):
    """(reason, type, message) for a WRITTEN reconcile only (docs/F2_EVENTS_PROPOSAL.md, v4):
    one Issued per new commandId persisted, one outcome per Issued record replaced.
    Nothing when the conditional write did not happen: a 409 emits no Event."""
    if not result.get("written"):
        return []
    events = []
    before, after = result["records_before"], result["records_after"]
    for key in sorted(set(before) | set(after)):
        old, new = before.get(key), after.get(key) if key in after else before.get(key)
        old_id = (old or {}).get("commandId") if (old or {}).get("phase") == "Issued" else None
        new_id = (new or {}).get("commandId") if (new or {}).get("phase") == "Issued" else None
        if old_id and old_id != new_id:
            reason = _command_outcome(old, new, result["receipts"].get(key),
                                      key_removed=key in after and after[key] is None
                                      and result["decisions"].get(key) == "removed_not_current")
            receipt = result["receipts"].get(key) or {}
            detail = f" ({receipt.get('reason')})" if receipt.get("commandId") == old_id and receipt.get("reason") else ""
            events.append((reason, "Normal" if reason == "LifecycleCommandSucceeded" else "Warning",
                           f"{key}: {old.get('fromState')} -> {old.get('toState')} command {old_id}{detail}"))
        if new_id and new_id != old_id:
            charged = ((new.get("budget") or {}).get("charged") or 0) + 1
            events.append(("LifecycleCommandIssued", "Normal",
                           f"{key}: {new.get('fromState')} -> {new.get('toState')} command {new_id}, "
                           f"attempt {charged} of {max_attempts}"))
    return events
