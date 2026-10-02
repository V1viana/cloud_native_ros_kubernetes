"""LifecycleController: drives each ROSModule's lifecycle (the proposal, r. 510-514).

Every tick it compares spec.lifecycleTarget with what each current Pod's State
Bridge observed, decides the single next step, applies the ROSLifecyclePolicy's
timeout, retries with backoff and readiness gate, and issues one command per
instance in status.lifecycleCommands (lifecycle_commands.py, with a
resourceVersion precondition). The State Bridge executes the authorized step at
most once and reports (sources/state_bridge/state_bridge/command_executor.py).
It never sends ROS calls itself. Contract: docs/LIFECYCLE_COMMAND_CONTRACT_DRAFT.md.
Timer only (decision D2): no handler on status events.
"""

from datetime import datetime, timezone
import time

import kopf
from kubernetes import client

from . import lifecycle_commands as lc
from .audit import audit, k8s_event
from .conditions import upsert_condition
from .constants import GROUP, LIFECYCLE_TICK_INTERVAL_SEC, VERSION
from .k8s_workloads import OWNER_LABEL, deployment_name, node_name
from .status_conditions import (LIFECYCLE_OWNED, ModuleStatusClient, converged_change,
                                converged_condition, write_conditions)

OBSERVATION_MAX_AGE_SEC = lc.OBSERVATION_MAX_AGE_SEC
# The namespace's shared policy, read by name (docs/CRD_CONTRACT_AUDIT.md, V3); a
# 404 falls back to the proposal's own values (the CRD's defaults).
DEFAULT_POLICY_NAME = "default-lifecycle-policy"
# Reasons that are alarms: they win over an inventory still settling.
ALARMS = ("TransitionFailed", "CommandUndelivered", "ObservationLost")
AUDITED = {
    "TransitionFailed": ("lifecycle_transition_failed", "LifecycleTransitionFailed", "transition_failed"),
    "CommandUndelivered": ("lifecycle_command_undelivered", "LifecycleCommandUndelivered", "command_undelivered"),
    "ObservationLost": ("lifecycle_observation_lost", "LifecycleObservationLost", "observation_lost"),
}


def _fresh_records(spec, status, generation, now, expected_instances):
    """Records of the expected instances only: this Pod (UID), current generation and
    target, fresh, after the Pod's creation. expected_instances: name -> (created, uid)."""
    target = spec.get("lifecycleTarget")
    records = {}
    for name, (created_at, uid) in expected_instances.items():
        instance = status.get("lifecycleInstances", {}).get(name, {})
        try:
            observed_at = datetime.fromisoformat(
                instance["lastObservedTime"].replace("Z", "+00:00"))
            age = (now - observed_at).total_seconds()
            after_creation = created_at is None or observed_at >= created_at
        except (KeyError, ValueError, TypeError, AttributeError):
            continue
        if (instance.get("generation") == generation
                and instance.get("target") == target
                and instance.get("podUID") == uid
                and after_creation and 0 <= age <= OBSERVATION_MAX_AGE_SEC):
            records[name] = instance
    return records


def lifecycle_summary(spec, status, generation, now, *, expected_instances):
    target = spec.get("lifecycleTarget")
    records = list(_fresh_records(spec, status, generation, now, expected_instances).values())
    commands = [c for key, c in (status.get("lifecycleCommands") or {}).items()
                if key in expected_instances and c]
    retries = max((max(0, (c.get("budget") or {}).get("charged", 0) - 1) for c in commands), default=0)
    if not records or len(records) != len(expected_instances):
        return False, "ObservationPending", "Waiting for fresh observations from every current Pod", retries
    if any(r.get("observedLifecycleState") == "Unknown" for r in records):
        return False, "LifecycleUnavailable", "A lifecycle endpoint is unreachable", retries
    if all(r.get("observedLifecycleState") == target for r in records):
        return True, "StateMatches", f"{target} observed by every current Pod", 0
    if any(c.get("phase") == "Exhausted" for c in commands):
        return False, "TransitionFailed", "An instance exhausted its transition budget", retries
    if any((c.get("budget") or {}).get("undelivered", 0) >= lc.UNDELIVERED_ALERT for c in commands):
        return False, "CommandUndelivered", "Lifecycle commands are not reaching an instance's State Bridge", retries
    if any(c.get("phase") == "WaitingReadiness" for c in commands):
        return False, "ReadinessPending", "Waiting for a fresh positive per-Pod readiness signal", retries
    return False, "TransitionPending", f"Converging towards {target}", retries


def _expected(spec, pods):
    return {name: (pod.metadata.creation_timestamp, pod.metadata.uid)
            for name, pod in _instances(spec, pods).items()}


def _owned_by(resource, kind, uid):
    return any(ref.kind == kind and ref.uid == uid
               for ref in (resource.metadata.owner_references or []))


def module_lifecycle_summary(namespace, module):
    """Join fresh bridge observations to the current Deployment's Pod inventory."""
    name = deployment_name(module["metadata"]["name"])
    try:
        apps = client.AppsV1Api()
        deployment = apps.read_namespaced_deployment(name, namespace)
        if _deployment_pending(module, deployment):
            return inventory_lifecycle_summary(module, deployment, [], [])
        selector = f"{OWNER_LABEL}={name}"
        replicasets = apps.list_namespaced_replica_set(namespace, label_selector=selector)
        pods = client.CoreV1Api().list_namespaced_pod(namespace, label_selector=selector)
    except client.ApiException as exc:
        # API errors must not be interpreted as an empty but healthy replica set.
        if exc.status == 404:
            return False, "WorkloadPending", "Waiting for the current Deployment and all its Pods", 0
        raise
    return inventory_lifecycle_summary(module, deployment, replicasets.items, pods.items)


def _deployment_pending(module, deployment):
    if deployment is None:
        return True
    desired = deployment.spec.replicas
    return bool(deployment.metadata.deletion_timestamp
            or not _owned_by(deployment, "ROSModule", module["metadata"]["uid"])
            or desired is None or desired < 1
            or (deployment.status.observed_generation or 0) < deployment.metadata.generation
            or deployment.status.updated_replicas != desired
            or deployment.status.replicas != desired)


def inventory_lifecycle_summary(module, deployment, replicasets, pods, now=None):
    """Same guard for reconciliation and a read-only, fleet-wide S3 snapshot."""
    metadata, spec = module["metadata"], module["spec"]
    pending = (False, "WorkloadPending", "Waiting for the current Deployment and all its Pods", 0)
    if _deployment_pending(module, deployment):
        return pending
    current = _current_pods(deployment, replicasets, pods)
    if len(current) != deployment.spec.replicas:
        return pending

    expected = _expected(spec, current)
    summary = lifecycle_summary(spec, module.get("status", {}), metadata["generation"],
                                now or datetime.now(timezone.utc), expected_instances=expected)
    # Do not mask a Failed transition with Pod readiness: the startup probe itself
    # waits for Active, so a failed activation normally leaves that Pod unready.
    if summary[0] and spec.get("lifecycleTarget") == "Active" and not all(
            _pod_ready(pod) for pod in current):
        return pending
    return summary


def _current_pods(deployment, replicasets, pods):
    """Live Pods of this Deployment's own ReplicaSets, identified by owner UID, not by name."""
    owned_sets = {rs.metadata.uid for rs in replicasets
                  if _owned_by(rs, "Deployment", deployment.metadata.uid)}
    return [pod for pod in pods
            if not pod.metadata.deletion_timestamp
            and pod.status.phase not in ("Succeeded", "Failed")
            and any(_owned_by(pod, "ReplicaSet", uid) for uid in owned_sets)]


def _instances(spec, pods):
    """Pods by the lifecycle-instance key their State Bridge writes under."""
    return {f"{spec['robotId']}/{node_name(spec['package'], spec['placement'])}_"
            f"{pod.metadata.name.replace('-', '_')}": pod for pod in pods}


def _pod_ready(pod):
    return pod.status.phase == "Running" and any(
        condition.type == "Ready" and condition.status == "True"
        for condition in (pod.status.conditions or []))


def module_serving(module, deployment, replicasets, pods, now):
    """Is at least one current Pod of this ROSModule serving right now? -> (bool, reason).

    Availability, not convergence (docs/CRD_CONTRACT_AUDIT.md, R6): unlike
    inventory_lifecycle_summary, an HPA scale-up or a Pod still starting does
    not unserve the Pods that are already Active. Same identity and freshness
    rules: only Pods of the Deployment this ROSModule owns, only records that
    are fresh, later than the Pod's creation and for the current generation
    and target. A module whose target is not Active never serves.
    """
    spec = module["spec"]
    if spec.get("lifecycleTarget") != "Active":
        return False, "TargetNotActive"
    if (deployment is None or deployment.metadata.deletion_timestamp
            or not _owned_by(deployment, "ROSModule", module["metadata"]["uid"])):
        return False, "WorkloadPending"
    ready = {name: pod for name, pod in
             _instances(spec, _current_pods(deployment, replicasets, pods)).items()
             if _pod_ready(pod)}
    if not ready:
        return False, "NoReadyPod"
    records = _fresh_records(spec, module.get("status", {}), module["metadata"]["generation"], now,
                             {name: (pod.metadata.creation_timestamp, pod.metadata.uid)
                              for name, pod in ready.items()})
    if any(record.get("observedLifecycleState") == "Active" for record in records.values()):
        return True, "Serving"
    return False, "NotActive" if records else "ObservationPending"


def module_available(namespace, module):
    """module_serving for one ROSModule, on its own current inventory -> (bool, reason).

    The AdaptationController's availability check once the service has been
    handed to the edge, and for the onboard fallback (R5, review round of
    ccd491e): the criterion already agreed for R6, not the stored lifecycle value.
    """
    name = deployment_name(module["metadata"]["name"])
    try:
        apps = client.AppsV1Api()
        deployment = apps.read_namespaced_deployment(name, namespace)
        selector = f"{OWNER_LABEL}={name}"
        replicasets = apps.list_namespaced_replica_set(namespace, label_selector=selector).items
        pods = client.CoreV1Api().list_namespaced_pod(namespace, label_selector=selector).items
    except client.ApiException as exc:
        if exc.status == 404:
            return False, "WorkloadPending"
        raise
    return module_serving(module, deployment, replicasets, pods, datetime.now(timezone.utc))


def read_policy(namespace):
    """The shared ROSLifecyclePolicy's spec; {} (the defaults) when it does not exist.
    Other API errors propagate: no command is issued on an unknown policy."""
    try:
        policy = client.CustomObjectsApi().get_namespaced_custom_object(
            GROUP, VERSION, namespace, "roslifecyclepolicies", DEFAULT_POLICY_NAME)
    except client.ApiException as exc:
        if exc.status == 404:
            return {}
        raise
    return policy.get("spec") or {}


def _inventory(namespace, module):
    """(deployment or None, replicasets, pods) of the ROSModule's Deployment."""
    name = deployment_name(module["metadata"]["name"])
    apps = client.AppsV1Api()
    try:
        deployment = apps.read_namespaced_deployment(name, namespace)
    except client.ApiException as exc:
        if exc.status == 404:
            deployment = None
        else:
            raise
    selector = f"{OWNER_LABEL}={name}"
    replicasets = apps.list_namespaced_replica_set(namespace, label_selector=selector).items
    pods = client.CoreV1Api().list_namespaced_pod(namespace, label_selector=selector).items
    return deployment, replicasets, pods


def command_targets(module, deployment, replicasets, pods):
    """(instances, all_pod_uids): every live Pod of the Deployment gets commands,
    during a rollout or a scale-up too; any existing Pod keeps its receipts."""
    current = _current_pods(deployment, replicasets, pods) if deployment is not None else []
    instances = {}
    for key, pod in _instances(module["spec"], current).items():
        created = pod.metadata.creation_timestamp
        instances[key] = {"uid": pod.metadata.uid,
                          "created": created.timestamp() if created is not None else None}
    return instances, {pod.metadata.uid for pod in pods}


def _retries(commands, instances):
    return max((max(0, ((commands.get(k) or {}).get("budget") or {}).get("charged", 0) - 1)
                for k in instances), default=0)


@kopf.on.timer(GROUP, VERSION, "rosmodules", interval=LIFECYCLE_TICK_INTERVAL_SEC)
def watch_lifecycle_transition(spec, status, namespace, name, patch, logger, body, **_):
    status = status or {}
    module = {**body, "spec": spec, "status": status}
    conditions = list(status.get("conditions", []))
    previous = next((c for c in conditions if c["type"] == "LifecycleSettled"), {})
    deployment, replicasets, pods = _inventory(namespace, module)
    settled, reason, message, retries = inventory_lifecycle_summary(module, deployment, replicasets, pods)

    driven, policy = None, {}
    try:
        policy = read_policy(namespace)
        instances, all_uids = command_targets(module, deployment, replicasets, pods)
        driven = lc.reconcile(ModuleStatusClient(namespace), name, instances, all_uids, policy, time.time())
    except client.ApiException as exc:
        logger.warning("lifecycle not driven this tick: %s", exc)
    if driven is not None:
        # F2 (docs/F2_EVENTS_PROPOSAL.md, v4): only after the planner's conditional write
        # succeeded; best-effort, never "exactly once"
        max_attempts = 1 + int((policy or {}).get("maxTransitionRetries", 3))
        for event_reason, event_type, event_message in lc.command_events(driven, max_attempts):
            k8s_event(body, event_reason, event_message, type_=event_type, logger=logger)
        commands = dict((driven["module"].get("status") or {}).get("lifecycleCommands") or {})
        retries = _retries(commands, instances) if not driven["conflict"] else retries
        lc_settled, lc_reason = driven["condition"]
        if lc_reason in ALARMS:
            settled, reason = False, lc_reason
            message = {"TransitionFailed": "An instance exhausted its transition budget",
                       "CommandUndelivered": "Lifecycle commands are not reaching an instance's State Bridge",
                       "ObservationLost": "An instance's State Bridge stopped reporting"}[lc_reason]

    upsert_condition(conditions, "LifecycleSettled", settled, reason=reason, message=message)
    # Never through Kopf's patch: status_conditions merges LifecycleSettled and Converged
    # into the latest read, with resourceVersion (results/evidence/runs/
    # CONDITIONS_CONCURRENCY.md). Converged is judged on that same fresh read, so the
    # ROSModuleController's fields and conditions may land in any order; its Events go
    # out only after the conditional write succeeded.
    changes = []
    write_conditions(ModuleStatusClient(namespace), name,
                     [c for c in conditions if c["type"] == "LifecycleSettled"], LIFECYCLE_OWNED,
                     uid=body["metadata"].get("uid"), generation=body["metadata"].get("generation"),
                     logger=logger,
                     derive=lambda current: [converged_condition(current, settled, reason)],
                     on_written=lambda before, after: changes.append(converged_change(before, after)))
    for change in filter(None, changes):
        k8s_event(body, change[0], change[2], type_=change[1], logger=logger)
    patch.status["transitionRetryCount"] = retries
    if reason != previous.get("reason"):                   # once per episode
        k8s_event(body, "LifecycleSettled" if settled else reason, message,
                  type_="Warning" if reason in ALARMS else "Normal", logger=logger)
        if reason in AUDITED:
            record_type, event_type, outcome = AUDITED[reason]
            audit({
                "record_type": record_type,
                "event_type": event_type,
                "robot_id": spec.get("robotId"),
                "component": body["metadata"]["name"],
                "correlation_id": (
                    f"{body['metadata']['uid']}:{body['metadata']['generation']}:lifecycle"
                ),
                "detail": message,
                "retry_count": retries,
                "success": False,
                "outcome": outcome,
            }, logger=logger)
