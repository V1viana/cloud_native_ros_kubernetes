"""AdaptationController: declarative equivalent of policy P2 (AnalyticsLatencySLO).

Blue/green, faithful to the proposal's sequence diagram (S6.3, "Adattamento
dichiarativo a violazione SLO") and to the imperative baseline it is
compared against (AnalyticsMigrationHandler in cloud_native_application_
manager/control_loop.py): a SECOND ROSModule is created on edge while the
onboard one keeps running; only once the edge instance reports Active is
onboard deactivated (lifecycleTarget=Inactive, never deleted) -- "onboard
resta fallback disponibile". An earlier version of this controller instead
flipped spec.placement on the same ROSModule, which would have meant
deleting onboard immediately and rebuilding it from scratch on rollback,
losing exactly the speed advantage blue/green is meant to give and making
the A/B comparison against the imperative baseline unfair.

One target per policy (R5 residual, M-a, docs/CRD_CONTRACT_AUDIT.md): more
than one onboard ROSModule matched by targetModuleSelector, or more than one
Deployment by a RestartComponent's targetDeploymentSelector, is
AmbiguousTarget and no action -- no longer the silent first match.
"""

import os
import time
import uuid
from datetime import datetime, timezone

import kopf
from kubernetes import client

from .audit import audit, k8s_event, notify
from .conditions import upsert_condition
from .infrastructure import CONTEXT_FIELDS, infrastructure_context
from .constants import ADAPTATION_TICK_INTERVAL_SEC, GROUP, VERSION
from .lifecycle_controller import module_available, module_lifecycle_summary

DIAGNOSTIC_SERVICE_ACCOUNT = "telemetry-diagnostics"
DIAGNOSTIC_DURATION_SEC = 90
DIAGNOSTIC_INTERVAL_SEC = 10


def _custom_api():
    return client.CustomObjectsApi()


def _apps_api():
    return client.AppsV1Api()


def _autoscaling_api():
    return client.AutoscalingV2Api()


def _batch_api():
    return client.BatchV1Api()


def _list_modules(namespace, match_labels):
    api = _custom_api()
    selector = ",".join(f"{k}={v}" for k, v in match_labels.items())
    result = api.list_namespaced_custom_object(
        GROUP, VERSION, namespace, "rosmodules", label_selector=selector
    )
    return result.get("items", [])


def _get_module(namespace, name):
    if not name:
        return None
    try:
        return _custom_api().get_namespaced_custom_object(
            GROUP, VERSION, namespace, "rosmodules", name
        )
    except client.ApiException as exc:
        if exc.status == 404:
            return None
        raise


def _edge_active(namespace, module):
    # Convergence to Inactive/Unconfigured is not permission to stop onboard.
    if module.get("spec", {}).get("lifecycleTarget") != "Active":
        return False
    return module_lifecycle_summary(namespace, module)[0]


def _patch_module_spec(namespace, name, spec_fields):
    _custom_api().patch_namespaced_custom_object(
        GROUP, VERSION, namespace, "rosmodules", name, {"spec": spec_fields}
    )


def _delete_module(namespace, name):
    try:
        _custom_api().delete_namespaced_custom_object(
            GROUP, VERSION, namespace, "rosmodules", name
        )
    except client.ApiException as exc:
        if exc.status != 404:
            raise


def _create_edge_hpa(namespace, edge_name, owner_rosmodule):
    # Proposal S4.1: "l'Operator abilita il workload, l'HPA ne possiede lo
    # scaling" -- found on review that this half was never actually built,
    # only the edge Deployment itself. Deployment/rosmodule_controller.py
    # names the Deployment after the ROSModule verbatim (deployment_name()
    # is just an underscore-to-hyphen swap, a no-op on an already-hyphenated
    # name like this one), so scaleTargetRef can reference it by the same
    # name without importing that function here. Owned by the edge
    # ROSModule itself (not by the AdaptationPolicy directly), so it is
    # torn down on exactly the same trigger as the Deployment it scales --
    # whether the edge module is deleted by rollback or garbage-collected
    # via the AdaptationPolicy's own ownership of it.
    manifest = {
        "apiVersion": "autoscaling/v2",
        "kind": "HorizontalPodAutoscaler",
        "metadata": {"name": edge_name, "namespace": namespace},
        "spec": {
            "scaleTargetRef": {
                "apiVersion": "apps/v1",
                "kind": "Deployment",
                "name": edge_name,
            },
            "minReplicas": 1,
            "maxReplicas": 3,
            "metrics": [
                {
                    "type": "Resource",
                    "resource": {
                        "name": "cpu",
                        "target": {"type": "Utilization", "averageUtilization": 80},
                    },
                }
            ],
        },
    }
    kopf.adopt(manifest, owner=owner_rosmodule)
    _autoscaling_api().create_namespaced_horizontal_pod_autoscaler(namespace, manifest)


def _create_edge_module(namespace, onboard, edge_name, edge_ros_param_map, owner_body):
    onboard_spec = onboard["spec"]
    onboard_labels = onboard.get("metadata", {}).get("labels", {})
    # edgeRosParamMap overrides individual keys, it does not replace the
    # whole map: absent -> edge inherits onboard's rosParamMap verbatim,
    # same behaviour as before this field existed.
    ros_param_map = {**onboard_spec.get("rosParamMap", {}), **(edge_ros_param_map or {})}
    manifest = {
        "apiVersion": f"{GROUP}/{VERSION}",
        "kind": "ROSModule",
        "metadata": {
            "name": edge_name,
            "namespace": namespace,
            "labels": {**onboard_labels, "placement": "edge"},
        },
        "spec": {
            **onboard_spec,
            "placement": "edge",
            "lifecycleTarget": "Active",
            "rosParamMap": ros_param_map,
        },
    }
    # AdaptationPolicy owns the edge ROSModule it creates: deleting the
    # policy garbage-collects a leftover edge instance instead of stranding
    # it. This is a different ownership edge from ROSModule -> Deployment
    # (rosmodule_controller.py); each controller adopts only what it itself
    # creates.
    kopf.adopt(manifest, owner=owner_body)
    created = _custom_api().create_namespaced_custom_object(
        GROUP, VERSION, namespace, "rosmodules", manifest
    )
    _create_edge_hpa(namespace, edge_name, created)


def _deployment_selector(action):
    selector = action["targetDeploymentSelector"]["matchLabels"]
    return ",".join(f"{k}={v}" for k, v in selector.items())


def _match_deployments(namespace, action):
    """Names of the Deployments a RestartComponent would restart: exactly one
    may be acted on (M-a); the caller checks before opening an incident."""
    result = _apps_api().list_namespaced_deployment(
        namespace, label_selector=_deployment_selector(action))
    return sorted(d.metadata.name for d in result.items)


def _restart_component(namespace, action, owner_body, logger, deployment_name):
    """RestartComponent's own action (P1-equivalent): trigger a real
    rollout of the Deployment matched by targetDeploymentSelector, the
    same mechanism `kubectl rollout restart` uses (patching the Pod
    template's own annotations, which Kubernetes' Deployment controller
    treats as a genuine template change). Followed by a diagnostic Job,
    forensic evidence left behind on purpose (never deleted by this
    controller, unlike a stuck edge module on migration rollback).

    Tried first, and found live to be wrong: deleting the Pod(s) directly
    (delete_collection_namespaced_pod) rather than the Deployment's own
    template. That let the ReplicaSet start a replacement in parallel with
    the old Pod still terminating -- fine for a Pod with no fixed
    identity, but the Agent uses hostNetwork with a fixed UDP port
    (manifests/kubernetes/e2/40-shared-infra-declarative.yaml), so the new
    Pod crash-looped trying to bind a port the old one had not released
    yet. A genuine rollout goes through the Deployment's own `strategy`
    (Recreate here, for exactly this reason -- see that manifest's own
    comment), which Kubernetes carries out asynchronously and correctly:
    old Pod fully gone before the new one starts. No busy-wait needed here
    for that ordering; it is not this controller's job to enforce it.
    """
    label_selector = _deployment_selector(action)
    restart_patch = {
        "spec": {
            "template": {
                "metadata": {
                    "annotations": {"dronekube.io/restarted-at": _now_iso()}
                }
            }
        }
    }
    _apps_api().patch_namespaced_deployment(deployment_name, namespace, restart_patch)
    logger.info("RestartComponent: triggered rollout of Deployment %s", deployment_name)
    return _create_diagnostic_job(namespace, label_selector, owner_body)


def _create_diagnostic_job(namespace, label_selector, owner_body):
    job_name = f"telemetry-diagnostics-{int(time.time())}"
    # Reuses the fleet-operator's own image rather than a dedicated one: it
    # already has the kubernetes client installed and needs nothing
    # ROS-specific to poll the Pod/Event API (see diagnostic_job.py).
    image = os.environ.get("FLEET_OPERATOR_IMAGE", "cloud-native-ros/fleet-operator:p2")
    manifest = {
        "apiVersion": "batch/v1",
        "kind": "Job",
        "metadata": {
            "name": job_name,
            "namespace": namespace,
            "labels": {"app.kubernetes.io/name": "telemetry-diagnostics"},
        },
        "spec": {
            "backoffLimit": 0,
            "activeDeadlineSeconds": DIAGNOSTIC_DURATION_SEC + 60,
            "template": {
                "metadata": {"labels": {"app.kubernetes.io/name": "telemetry-diagnostics"}},
                "spec": {
                    "restartPolicy": "Never",
                    # Narrower, read-only ServiceAccount, separate from
                    # fleet-operator's own -- the Job only ever needs to
                    # list/watch Pods and Events, never write anything.
                    "serviceAccountName": DIAGNOSTIC_SERVICE_ACCOUNT,
                    "containers": [
                        {
                            "name": "collector",
                            "image": image,
                            "imagePullPolicy": "IfNotPresent",
                            "command": ["python3", "-m", "fleet_operator.diagnostic_job"],
                            "env": [
                                {"name": "DIAGNOSTIC_NAMESPACE", "value": namespace},
                                {"name": "DIAGNOSTIC_LABEL_SELECTOR", "value": label_selector},
                                {"name": "DIAGNOSTIC_DURATION_SEC", "value": str(DIAGNOSTIC_DURATION_SEC)},
                                {"name": "DIAGNOSTIC_INTERVAL_SEC", "value": str(DIAGNOSTIC_INTERVAL_SEC)},
                            ],
                        }
                    ],
                },
            },
        },
    }
    # AdaptationPolicy owns the diagnostic Job it creates, same as the edge
    # ROSModule in _create_edge_module: deleting the policy garbage-collects
    # it too.
    kopf.adopt(manifest, owner=owner_body)
    _batch_api().create_namespaced_job(namespace, manifest)
    return job_name


def _read_metric(module, metric_name):
    if module is None:
        return None
    metrics = module.get("status", {}).get("metrics", {})
    raw = metrics.get(metric_name)
    if raw is None:
        return None
    try:
        return float(raw)
    except (TypeError, ValueError):
        return None


def _parse_iso(value):
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (AttributeError, ValueError, TypeError):
        return None


def _fresh_metric(module, metric_name, since_iso):
    """The metric's numeric value, only if the State Bridge reported it after
    since_iso (the previous counted window); None otherwise.

    None means "no new evidence" -- a dead or frozen bridge's lingering value,
    no report ever, a report with no samples, a non-numeric value -- and the
    caller then holds both window counters instead of counting or resetting.
    That is the imperative baseline's rule: LatencySloStateMachine::tick()
    completes a window only when it holds new samples and does nothing
    otherwise (px4_event_detector_plugin/src/rule_state_machines.cpp). Before
    this, the stored value was used at any age: a bridge that stopped
    reporting left its last p95 counting as a fresh window forever
    (docs/CRD_CONTRACT_AUDIT.md, D3). Both timestamps come from the same
    cluster clock source; with replicas the value is still last-writer-wins
    (D3b, not addressed here).
    """
    if module is None:
        return None
    observed = _parse_iso(module.get("status", {}).get("metricsObservedTime"))
    since = _parse_iso(since_iso)
    if observed is None or since is None or observed <= since:
        return None
    return _read_metric(module, metric_name)


def _report_time(module):
    """The checkpoint after counting a D3a report: the report's own timestamp.
    Found preparing R9 (E2-B at windowSec 1): with the current time as
    checkpoint, truncated to the second by _now_iso, a report written within the
    same second as the tick that counted it was still newer than the checkpoint
    one tick later, and was counted again. The report's own time is never newer
    than itself."""
    return ((module or {}).get("status") or {}).get("metricsObservedTime") or _now_iso()


# Metrics under the A/B temporal contract (V6, docs/CRD_CONTRACT_AUDIT.md): the
# State Bridge publishes tumbling p95 windows per instance in
# status.metricWindows and each window is counted once. Other metrics (the
# heartbeat age of E2/P1) keep the fresh-report rule of D3a.
WINDOWED_METRICS = ("latency_p95_ms",)
WINDOW_LIVENESS_FACTOR = 3        # an instance is live if it published within 3W


def _window_values(module, policy_window_sec, cursor, now=None, since=None):
    """([(p95, end)] of each window closed after the cursor, oldest first; the
    cursor to start from; mismatch message or None) for one module. The caller
    moves the cursor's end over each window it actually counts.

    Reference instance: the smallest key among the instances that published a
    window ending within the last 3W -- every bridge of a module sees the union
    of its Active replicas' samples (D3b), so one instance is enough, and two
    never count the same span twice. On first sight of a module nothing is
    counted: the cursor starts at its latest window, as a new policy starts
    counting from now. A replaced instance stops publishing; after 3W the next
    one takes over from the last counted end. With `since` (a re-arm, R-b),
    only windows begun at or after it are new evidence.
    """
    per_instance = (module or {}).get("status", {}).get("metricWindows") or {}
    now = now or datetime.now(timezone.utc)
    live = []
    for key, entry in per_instance.items():
        windows = (entry or {}).get("windows") or []
        end = _parse_iso(windows[-1].get("end")) if windows else None
        window_sec = float((entry or {}).get("windowSec") or policy_window_sec)
        if end is not None and (now - end).total_seconds() <= WINDOW_LIVENESS_FACTOR * window_sec:
            live.append(key)
    if not live:
        return [], cursor, None
    name = module.get("metadata", {}).get("name", "")
    key = min(live)
    entry = per_instance[key]
    if abs(float(entry.get("windowSec", 0)) - float(policy_window_sec)) > 1e-9:
        return [], cursor, (f"instance {key} publishes {entry.get('windowSec')}s windows, "
                            f"policy windowSec is {policy_window_sec}s: not counted")
    windows = sorted(entry["windows"], key=lambda w: _parse_iso(w["end"]))
    if not cursor or cursor.get("module") != name:
        return [], {"module": name, "instance": key, "end": windows[-1]["end"]}, None
    after = _parse_iso(cursor.get("end"))
    since = _parse_iso(since) if since else None

    def begun_after_since(window):
        start = _parse_iso(window.get("start"))
        return since is None or (start is not None and start >= since)
    new = [w for w in windows if (after is None or _parse_iso(w["end"]) > after)
           and begun_after_since(w)]
    return ([(float(w["p95Ms"]), w["end"]) for w in new], {**cursor, "instance": key}, None)


def _audit_incident_started(body, correlation_id, action_type, target_module, logger,
                            infrastructure=None):
    policy_name = body["metadata"]["name"]
    record = {
        "record_type": "incident_started",
        "correlation_id": correlation_id,
        "event_id": correlation_id,
        "robot_id": target_module,
        "event_type": action_type,
        "policy_id": policy_name,
    }
    if infrastructure is not None:
        record["infrastructure"] = infrastructure
    audit(record, logger=logger)


def _audit_incident_completed(
    body, correlation_id, action_type, target_module, success, outcome, rollback_performed, logger,
    infrastructure=None,
):
    policy_name = body["metadata"]["name"]
    record = {
        "record_type": "incident_completed",
        "correlation_id": correlation_id,
        "event_id": correlation_id,
        "robot_id": target_module,
        "event_type": action_type,
        "policy_id": policy_name,
        "success": success,
        "outcome": outcome,
        "rollback_performed": rollback_performed,
    }
    if infrastructure is not None:
        record["infrastructure"] = infrastructure
    audit(record, logger=logger)
    notify(
        {
            "correlation_id": correlation_id,
            "robot_id": target_module,
            "event_type": action_type,
            "outcome": outcome,
            "success": success,
            "rollback_performed": rollback_performed,
        },
        logger=logger,
    )


def _now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _elapsed_sec(since_iso):
    # Found live: an empty/unset timestamp must mean "never checked yet",
    # i.e. infinitely long ago -- returning 0.0 here made window_elapsed
    # (0.0 >= windowSec) false forever on a fresh AdaptationPolicy, since
    # lastWindowCheckedAt only ever gets its first value *inside* the
    # branch this same comparison is supposed to unlock. Nominal never
    # progressed past 0 windows because of it.
    if not since_iso:
        return float("inf")
    since = datetime.fromisoformat(since_iso.replace("Z", "+00:00"))
    return (datetime.now(timezone.utc) - since).total_seconds()


# The timer is the only entry point (Viviana, 2026-09-26). With @kopf.on.create
# and @kopf.on.update on this same function too, Kopf ran two invocations on one
# object concurrently, the second on the status before the first one's patch:
# found live (rearm/20260925T212918Z, phase 4) as two AmbiguousTarget Events
# 100 ms apart, and able in principle to double any once-per-transition effect.
# Timer ticks for one object are sequential; the first comes at creation, and a
# spec change is read at the next tick (nominal interval 1s, not a guarantee:
# the tick itself and its API calls take time).
@kopf.on.timer(GROUP, VERSION, "adaptationpolicies", interval=ADAPTATION_TICK_INTERVAL_SEC)
def reconcile_adaptation_policy(spec, status, namespace, patch, logger, body, **_):
    trigger = spec["trigger"]
    action = spec["action"]
    recovery = spec.get("recovery", {})
    rollback = spec.get("rollback", {})

    status = status or {}
    state = status.get("state", "Nominal")
    trigger_windows = status.get("consecutiveTriggerWindows", 0)
    recovery_windows = status.get("consecutiveRecoveryWindows", 0)
    edge_module_name = status.get("edgeModuleName", "")
    migrating_since = status.get("migratingSince", "")
    diagnostic_job_name = status.get("diagnosticJobName", "")
    restarted_at = status.get("restartedAt", "")
    remediating_module_name = status.get("remediatingModuleName", "")
    last_window_checked_at = status.get("lastWindowCheckedAt", "")
    correlation_id = status.get("correlationId", "")
    # R5 fallback bookkeeping (review round of ccd491e): when the service was
    # handed to the edge, since when the edge has been unavailable, and when
    # and why the onboard was requested back.
    handed_over_at = status.get("handedOverAt", "")
    edge_unavailable_since = status.get("edgeUnavailableSince", "")
    fallback_since = status.get("fallbackSince", "")
    fallback_reason = status.get("fallbackReason", "")
    cleared = []
    window_cursor = dict(status.get("windowCursor") or {})
    window_mismatch = None
    conditions = list(status.get("conditions", []))
    action_type = action["type"]
    policy_name = body["metadata"]["name"]
    initial_state = state
    decision = {}

    def ambiguity():
        return next((c for c in conditions if c.get("type") == "AmbiguousTarget"
                     and c.get("status") == "True"), None)

    def ambiguous(reason, what, names):
        """M-a: more than one target -- no action and no incident. Nothing
        measured meanwhile counts later: the count restarts from now."""
        nonlocal state, trigger_windows, window_cursor, last_window_checked_at
        message = f"{what} matches {len(names)}: {', '.join(names)}; no action taken"
        if ambiguity() is None:
            k8s_event(body, "AmbiguousTarget", message, type_="Warning", logger=logger)
        upsert_condition(conditions, "AmbiguousTarget", True, reason=reason, message=message)
        logger.warning("AdaptationPolicy target ambiguous: %s", message)
        state, trigger_windows = "Nominal", 0
        if window_cursor:
            window_cursor = {}
            cleared.append("windowCursor")
        last_window_checked_at = _now_iso()

    def resolved(target):
        if ambiguity() is not None:
            upsert_condition(conditions, "AmbiguousTarget", False, reason="SingleTarget",
                             message=f"one target: {target}")
            k8s_event(body, "TargetResolved", f"one target again: {target}", logger=logger)

    def metric_values(module, metric, since=None):
        """New evidence to classify, oldest first. A windowed metric gives each
        tumbling window closed since the last counted one (V6); any other the
        D3a rule, at most one fresh report per windowSec. With `since`, nothing
        measured before it (a window begun earlier, an older report) is new."""
        nonlocal window_cursor, window_mismatch, last_window_checked_at
        if metric in WINDOWED_METRICS:
            items, window_cursor, mismatch = _window_values(
                module, trigger["windowSec"], window_cursor, since=since)
            window_mismatch = window_mismatch or mismatch

            def counted_in_order():
                # The cursor moves past a window only once the caller has taken
                # it and asked for the next (or run out): a loop that stops
                # after an action leaves the next window uncounted, not skipped.
                nonlocal window_cursor
                for p95, end in items:
                    yield p95
                    window_cursor = {**window_cursor, "end": end}
            return counted_in_order()
        after = last_window_checked_at
        if since and (not after or _parse_iso(since) > _parse_iso(after)):
            after = since
        value = _fresh_metric(module, metric, after) if window_elapsed else None
        if value is None:
            return []
        last_window_checked_at = _report_time(module)
        return [value]

    def decision_context(phase, names):
        """R8: CPU/memory from metrics-server at a decision point, for the audit
        record and status only -- nothing below ever reads it to decide."""
        context = infrastructure_context(namespace, [n for n in names if n])
        decision.clear()
        decision.update(context, phase=phase, at=_now_iso())
        return context

    # This handler ticks every ADAPTATION_TICK_INTERVAL_SEC (faster than
    # trigger.windowSec, typically), because Kopf's @kopf.on.timer only
    # takes a static interval -- there is no per-object dynamic interval to
    # tie directly to windowSec. A "window" is only counted once windowSec
    # has really elapsed since the last one, regardless of how many ticks
    # landed in between; ticks that arrive early just re-check and return.
    if not last_window_checked_at:
        # First tick: open the first window and count nothing. With no previous
        # window there is no "reported after it", so an old stored value must
        # not count here (_fresh_metric). The next tick, windowSec later,
        # evaluates normally -- unlike the bug noted in _elapsed_sec, this never
        # stalls, because the checkpoint is set right here.
        last_window_checked_at = _now_iso()
        window_elapsed = False
    else:
        window_elapsed = _elapsed_sec(last_window_checked_at) >= trigger["windowSec"]

    if state in ("Nominal", "Triggered"):
        candidates = [
            module
            for module in _list_modules(namespace, spec["targetModuleSelector"]["matchLabels"])
            if module.get("spec", {}).get("placement") == "onboard"
        ]
        if not candidates:
            logger.warning("AdaptationPolicy matches no onboard ROSModule; skipping")
            return
        # M-a: the targets are resolved before any action or incident. The
        # Deployments of a RestartComponent are listed when acting and, while
        # they are the ambiguity, on every tick, so the condition can clear.
        deployments = None
        if (action_type == "RestartComponent" and ambiguity() is not None
                and ambiguity().get("reason") == "MultipleDeployments"):
            deployments = _match_deployments(namespace, action)
        if len(candidates) > 1:
            ambiguous("MultipleModules", "targetModuleSelector (onboard ROSModules)",
                      sorted(m["metadata"]["name"] for m in candidates))
        elif deployments is not None and len(deployments) > 1:
            ambiguous("MultipleDeployments", "targetDeploymentSelector", deployments)
        else:
            resolved(candidates[0]["metadata"]["name"]
                     + (f", Deployment {deployments[0]}" if deployments else ""))
        onboard = candidates[0]
        onboard_name = onboard["metadata"]["name"]

        for metric_value in ([] if ambiguity() is not None
                             else metric_values(onboard, trigger["metric"])):
            if state not in ("Nominal", "Triggered"):
                break                   # an action was taken: later windows are moot
            if metric_value > trigger["threshold"]:
                trigger_windows += 1
            else:
                trigger_windows = 0

            if trigger_windows >= trigger["consecutiveWindows"]:
                trigger_windows = 0
                if action_type == "RestartComponent":
                    deployments = (deployments if deployments is not None
                                   else _match_deployments(namespace, action))
                    if not deployments:
                        raise RuntimeError(
                            f"no Deployment matches selector {_deployment_selector(action)}")
                    if len(deployments) > 1:
                        ambiguous("MultipleDeployments", "targetDeploymentSelector", deployments)
                        break
                upsert_condition(
                    conditions,
                    "Adapting",
                    True,
                    reason="ThresholdExceeded",
                    message=f"{trigger['metric']}={metric_value} > {trigger['threshold']}",
                )
                # Unique by construction, like variant A's (microseconds and a
                # sequence): a re-armed policy's next incident may start at once.
                correlation_id = f"{policy_name}-{action_type}-{_now_iso()}-{uuid.uuid4().hex[:8]}"
                if action_type == "MigratePlacement" and action.get("to") == "edge":
                    edge_module_name = f"{onboard_name}-edge"
                    _create_edge_module(
                        namespace, onboard, edge_module_name,
                        action.get("edgeRosParamMap"), body,
                    )
                    state = "Migrating"
                    migrating_since = _now_iso()
                    logger.info(
                        "AdaptationPolicy triggered: creating edge instance %s", edge_module_name
                    )
                    k8s_event(
                        body, "MigrationStarted",
                        f"{trigger['metric']}={metric_value} exceeded {trigger['threshold']}; "
                        f"creating edge instance {edge_module_name}",
                        logger=logger,
                    )
                    _audit_incident_started(
                        body, correlation_id, action_type, onboard["spec"]["robotId"], logger,
                        infrastructure=decision_context("IncidentStarted", [onboard_name]),
                    )
                elif action_type == "RestartComponent":
                    remediating_module_name = onboard_name
                    diagnostic_job_name = _restart_component(namespace, action, body, logger,
                                                             deployments[0])
                    state = "Remediating"
                    restarted_at = _now_iso()
                    logger.info(
                        "AdaptationPolicy triggered: restarted component for %s, "
                        "diagnostic job %s", onboard_name, diagnostic_job_name,
                    )
                    k8s_event(
                        body, "RemediationStarted",
                        f"{trigger['metric']}={metric_value} exceeded {trigger['threshold']}; "
                        f"restarted component for {onboard_name}",
                        logger=logger,
                    )
                    _audit_incident_started(
                        body, correlation_id, action_type, onboard["spec"]["robotId"], logger,
                        infrastructure=decision_context("IncidentStarted", [onboard_name]),
                    )
            elif trigger_windows > 0:
                state = "Triggered"

    elif state == "Migrating":
        edge = _get_module(namespace, edge_module_name)
        onboard_name = edge_module_name[: -len("-edge")]

        def begin_fallback(reason, message):
            """Request the onboard back; the rollback completes only once it serves."""
            nonlocal state, fallback_since, fallback_reason
            state, fallback_since, fallback_reason = "FallingBack", _now_iso(), reason
            upsert_condition(conditions, "Adapting", False, reason=reason, message=message)
            onboard = _get_module(namespace, onboard_name)
            if onboard is not None and onboard["spec"].get("lifecycleTarget") != "Active":
                _patch_module_spec(namespace, onboard_name, {"lifecycleTarget": "Active"})
            logger.warning("%s; onboard %s requested Active", message, onboard_name)
            k8s_event(body, "FallbackRequested",
                      f"{message}; onboard {onboard_name} requested Active, edge kept "
                      f"until the onboard serves", type_="Warning", logger=logger)

        if not handed_over_at:
            # Preparation: the service is still the onboard's, which is never
            # touched here. Limit counted from the start of the migration.
            timeout_sec = rollback.get("onReadinessFailureSec", 60)
            if edge is None:
                begin_fallback("EdgeModuleMissing", f"'{edge_module_name}' no longer exists")
            elif not _edge_active(namespace, edge):
                elapsed = _elapsed_sec(migrating_since)
                if elapsed > timeout_sec:
                    begin_fallback("EdgeReadinessTimeout",
                                   f"edge not Active after {elapsed:.0f}s (limit {timeout_sec}s)")
                # else: still converging, onboard is deliberately left alone.
            else:
                onboard = _get_module(namespace, onboard_name)
                # From here the service is the edge's (also if the onboard was
                # already Inactive or gone): the fallback rules below apply.
                handed_over_at = _now_iso()
                if onboard is not None and onboard.get("spec", {}).get("lifecycleTarget") != "Inactive":
                    _patch_module_spec(namespace, onboard_name, {"lifecycleTarget": "Inactive"})
                    upsert_condition(conditions, "OnboardDeactivated", True, reason="EdgeActive")
                    # The central step of the migration: an Event like every other
                    # transition (proposal S4.2), not only a condition and a log line.
                    k8s_event(
                        body, "OnboardDeactivated",
                        f"edge {edge_module_name} Active on every current Pod; onboard "
                        f"{onboard_name} set to Inactive, kept as fallback (not deleted)",
                        logger=logger,
                    )
                    logger.info(
                        "edge %s is Active; deactivated onboard %s (not deleted)",
                        edge_module_name, onboard_name,
                    )
        elif edge is None:
            # Handed over and the edge is gone: it will not come back, no tolerance.
            begin_fallback("EdgeModuleMissing",
                           f"'{edge_module_name}' no longer exists after the handover")
        elif not module_available(namespace, edge)[0]:
            # Handed over: the migration timer no longer applies. A separate
            # timer starts at the first loss of availability (R6 criterion).
            tolerance_sec = rollback.get("edgeLossToleranceSec", 30)
            if not edge_unavailable_since:
                edge_unavailable_since = _now_iso()
                k8s_event(body, "EdgeUnavailable",
                          f"edge {edge_module_name} not serving; tolerated for {tolerance_sec}s",
                          type_="Warning", logger=logger)
            elif _elapsed_sec(edge_unavailable_since) > tolerance_sec:
                begin_fallback(
                    "EdgeLost",
                    f"edge {edge_module_name} not serving for "
                    f"{_elapsed_sec(edge_unavailable_since):.0f}s (tolerance {tolerance_sec}s)")
        elif edge_unavailable_since:
            edge_unavailable_since = ""
            cleared.append("edgeUnavailableSince")
            k8s_event(body, "EdgeAvailable", f"edge {edge_module_name} serving again",
                      logger=logger)

        if state == "Migrating" and handed_over_at and not edge_unavailable_since:
            recovery_metric = recovery.get("metric", trigger.get("metric", ""))
            for metric_value in metric_values(edge, recovery_metric):
                if state != "Migrating":
                    break
                threshold = recovery.get("threshold", trigger["threshold"])
                # Strict below the threshold, as variant A, for windowed metrics (V6).
                if (metric_value < threshold if recovery_metric in WINDOWED_METRICS
                        else metric_value <= threshold):
                    recovery_windows += 1
                else:
                    recovery_windows = 0

                if recovery_windows >= recovery.get("consecutiveWindows", 1):
                    state = "Recovered"
                    upsert_condition(conditions, "Adapting", False, reason="Recovered")
                    logger.info("AdaptationPolicy recovered: migration to edge complete")
                    k8s_event(
                        body, "Recovered", f"migration to {edge_module_name} complete",
                        logger=logger,
                    )
                    _audit_incident_completed(
                        body, correlation_id, action_type, edge["spec"]["robotId"],
                        success=True, outcome="analytics_slo_recovered",
                        rollback_performed=False, logger=logger,
                        infrastructure=decision_context(
                            "IncidentCompleted", [edge_module_name, onboard_name]),
                    )

    elif state == "Remediating":
        # No new module was created to read a recovered metric back from
        # (unlike Migrating's edge module) -- the same onboard module that
        # triggered this keeps being watched, on the expectation that
        # whatever the restarted component was propping up (e.g. the
        # telemetry link) recovers on it once the Pod comes back.
        onboard = _get_module(namespace, remediating_module_name)
        timeout_sec = rollback.get("onReadinessFailureSec", 60)

        if onboard is None:
            state = "Escalated"
            upsert_condition(
                conditions, "Adapting", False, reason="TargetModuleMissing",
                message=f"'{remediating_module_name}' no longer exists",
            )
            logger.error(
                "target module %s disappeared during remediation", remediating_module_name
            )
            k8s_event(
                body, "Escalated", f"target module {remediating_module_name} disappeared",
                type_="Warning", logger=logger,
            )
            # The module itself is gone -- unlike Migrating's edge/onboard
            # pair there is no sibling object left to read a real robotId
            # from, so this one case keeps the ROSModule name as the best
            # available label rather than guessing.
            _audit_incident_completed(
                body, correlation_id, action_type, remediating_module_name,
                success=False, outcome="telemetry_recovery_failed",
                rollback_performed=False, logger=logger,
                infrastructure=decision_context("IncidentCompleted", [remediating_module_name]),
            )
        else:
            metric_value = (_fresh_metric(onboard, recovery.get("metric", trigger["metric"]),
                                          last_window_checked_at)
                            if window_elapsed else None)
            if metric_value is not None:
                last_window_checked_at = _report_time(onboard)
                threshold = recovery.get("threshold", trigger["threshold"])
                if metric_value <= threshold:
                    recovery_windows += 1
                else:
                    recovery_windows = 0

                if recovery_windows >= recovery.get("consecutiveWindows", 1):
                    state = "Recovered"
                    upsert_condition(conditions, "Adapting", False, reason="Recovered")
                    logger.info("AdaptationPolicy recovered: component remediation complete")
                    k8s_event(
                        body, "Recovered",
                        f"component remediation for {remediating_module_name} complete",
                        logger=logger,
                    )
                    _audit_incident_completed(
                        body, correlation_id, action_type, onboard["spec"]["robotId"],
                        success=True, outcome="telemetry_recovered",
                        rollback_performed=False, logger=logger,
                        infrastructure=decision_context(
                            "IncidentCompleted", [remediating_module_name]),
                    )

            if state == "Remediating":
                elapsed = _elapsed_sec(restarted_at)
                if elapsed > timeout_sec:
                    state = "Escalated"
                    upsert_condition(
                        conditions, "Adapting", False, reason="RemediationTimeout",
                        message=f"not recovered after {elapsed:.0f}s (limit {timeout_sec}s)",
                    )
                    logger.warning(
                        "component restart for %s did not recover within %ss; escalated "
                        "(diagnostic job %s left in place)",
                        remediating_module_name, timeout_sec, diagnostic_job_name,
                    )
                    k8s_event(
                        body, "Escalated",
                        f"component restart for {remediating_module_name} did not recover "
                        f"within {timeout_sec}s",
                        type_="Warning", logger=logger,
                    )
                    _audit_incident_completed(
                        body, correlation_id, action_type, onboard["spec"]["robotId"],
                        success=False, outcome="telemetry_recovery_failed",
                        rollback_performed=False, logger=logger,
                        infrastructure=decision_context(
                            "IncidentCompleted", [remediating_module_name]),
                    )
                # else: still waiting, diagnostic Job keeps collecting.

    mismatch_before = any(c.get("type") == "MetricsWindowMismatch" and c.get("status") == "True"
                          for c in conditions)
    if window_mismatch:
        upsert_condition(conditions, "MetricsWindowMismatch", True,
                         reason="WindowSecMismatch", message=window_mismatch)
        if not mismatch_before:                          # F2: both halves of the transition
            k8s_event(body, "MetricsWindowMismatch", window_mismatch, type_="Warning", logger=logger)
    elif mismatch_before:
        upsert_condition(conditions, "MetricsWindowMismatch", False, reason="WindowSecMatches")
        k8s_event(body, "MetricsWindowMatched", "windowSec matches the reported windows again",
                  logger=logger)

    if state == "FallingBack":
        # A rollback is complete only once the onboard serves again (R6
        # criterion); then the edge is removed. "Fallback requested" is not
        # "fallback restored": past fallbackReadinessSec it is FallbackFailed,
        # and the edge -- possibly the last module able to serve -- is kept.
        onboard_name = edge_module_name[: -len("-edge")]
        onboard = _get_module(namespace, onboard_name)
        limit_sec = rollback.get("fallbackReadinessSec", 60)
        if onboard is not None and onboard["spec"].get("lifecycleTarget") != "Active":
            _patch_module_spec(namespace, onboard_name, {"lifecycleTarget": "Active"})
            onboard = None if onboard is None else {
                **onboard, "spec": {**onboard["spec"], "lifecycleTarget": "Active"}}
        edge = _get_module(namespace, edge_module_name)
        robot_id = next((m["spec"]["robotId"] for m in (onboard, edge) if m is not None),
                        onboard_name)
        serving = onboard is not None and module_available(namespace, onboard)[0]
        if serving:
            _delete_module(namespace, edge_module_name)
            state = "RolledBack"
            upsert_condition(conditions, "OnboardDeactivated", False, reason="FallbackRestored",
                             message=f"onboard {onboard_name} serving again")
            logger.warning("rolled back (%s): onboard %s serving again, edge %s removed",
                           fallback_reason, onboard_name, edge_module_name)
            k8s_event(body, "RolledBack",
                      f"{fallback_reason}: onboard {onboard_name} serving again, "
                      f"edge {edge_module_name} removed", type_="Warning", logger=logger)
            _audit_incident_completed(
                body, correlation_id, action_type, robot_id,
                success=False, outcome="analytics_migration_failed",
                rollback_performed=True, logger=logger,
                infrastructure=decision_context("IncidentCompleted", [onboard_name, edge_module_name]),
            )
        elif onboard is None or _elapsed_sec(fallback_since) > limit_sec:
            why = ("onboard module missing" if onboard is None
                   else f"onboard not serving {_elapsed_sec(fallback_since):.0f}s after the "
                        f"request (limit {limit_sec}s)")
            state = "FallbackFailed"
            upsert_condition(conditions, "Adapting", False, reason="FallbackFailed",
                             message=f"{fallback_reason}: {why}")
            logger.error("fallback failed (%s): %s; edge %s kept",
                         fallback_reason, why, edge_module_name)
            k8s_event(body, "FallbackFailed", f"{fallback_reason}: {why}; edge kept",
                      type_="Warning", logger=logger)
            _audit_incident_completed(
                body, correlation_id, action_type, robot_id,
                success=False, outcome="rollback_failed",
                rollback_performed=False, logger=logger,
                infrastructure=decision_context("IncidentCompleted", [onboard_name, edge_module_name]),
            )
        # else: onboard requested and still converging.

    # R5 residual, R-b (docs/CRD_CONTRACT_AUDIT.md): back to Nominal, as variant
    # A's detector re-arms once its signal recovers -- only from the resting
    # states that leave the platform as before the incident (RolledBack: the
    # onboard serves, the edge is removed; Escalated by timeout: nothing was
    # migrated), and only after the policy's recovery windows measured after
    # the entry. Never while the violation persists: no silent retry loop.
    # Recovered, FallbackFailed and Escalated with the target missing stay
    # resting states that an explicit intervention (recreating the policy)
    # resets.
    rearmable = state == "RolledBack" or (state == "Escalated" and any(
        c.get("type") == "Adapting" and c.get("reason") == "RemediationTimeout"
        for c in conditions))
    if rearmable and state != initial_state:
        trigger_windows = recovery_windows = 0       # the re-arm count starts at the entry
    elif rearmable:
        entered_at = status.get("lastTransitionTime", "")
        # The incident's own target, not the selector's current result.
        target_name = (edge_module_name[: -len("-edge")] if state == "RolledBack"
                       else remediating_module_name)
        target = _get_module(namespace, target_name) if entered_at else None
        edge_gone = state != "RolledBack" or _get_module(namespace, edge_module_name) is None
        if target is not None and edge_gone:
            recovery_metric = recovery.get("metric", trigger["metric"])
            threshold = recovery.get("threshold", trigger["threshold"])
            for metric_value in metric_values(target, recovery_metric, since=entered_at):
                if state != initial_state:
                    break
                if (metric_value < threshold if recovery_metric in WINDOWED_METRICS
                        else metric_value <= threshold):
                    recovery_windows += 1
                else:
                    recovery_windows = 0
                if recovery_windows >= recovery.get("consecutiveWindows", 1):
                    previous = correlation_id
                    message = (f"{recovery_metric} recovered on {target_name} for "
                               f"{recovery_windows} windows after {state} ({previous})")
                    state = "Nominal"
                    trigger_windows = recovery_windows = 0
                    correlation_id = edge_module_name = remediating_module_name = ""
                    diagnostic_job_name = migrating_since = restarted_at = handed_over_at = ""
                    edge_unavailable_since = fallback_since = fallback_reason = ""
                    cleared.extend(("correlationId", "migratingSince", "restartedAt",
                                    "handedOverAt", "edgeUnavailableSince", "fallbackSince",
                                    "fallbackReason", "decisionContext"))
                    upsert_condition(conditions, "Adapting", False, reason="Rearmed",
                                     message=message)
                    logger.info("AdaptationPolicy re-armed: %s", message)
                    k8s_event(body, "Rearmed", message, logger=logger)

    if initial_state == "Nominal" and state == "Triggered":
        # F2: only a persisted Nominal -> Triggered; reaching the trigger in one tick goes
        # straight to the action, with no TriggerObserved (docs/F2_EVENTS_PROPOSAL.md, v4)
        k8s_event(body, "TriggerObserved",
                  f"{trigger_windows} window(s) over the threshold, "
                  f"{(spec.get('trigger') or {}).get('consecutiveWindows')} needed", logger=logger)
    patch.status["state"] = state
    # Declared by the CRD (lastTransitionTime also in the proposal's own
    # AdaptationPolicy example) and never written before: see
    # docs/CRD_CONTRACT_AUDIT.md, D5. Set only when the state actually changes.
    if state != initial_state:
        # To the millisecond: the re-arm counts only what was measured after it.
        patch.status["lastTransitionTime"] = (
            datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z"))
    patch.status["observedGeneration"] = body["metadata"]["generation"]
    patch.status["consecutiveTriggerWindows"] = trigger_windows
    patch.status["consecutiveRecoveryWindows"] = recovery_windows
    patch.status["edgeModuleName"] = edge_module_name
    patch.status["diagnosticJobName"] = diagnostic_job_name
    patch.status["remediatingModuleName"] = remediating_module_name
    if correlation_id:
        patch.status["correlationId"] = correlation_id
    # Found live: the CRD types this as format: date-time, and Kubernetes
    # rejects (422) an empty string against that format -- only patch it
    # once it actually holds a timestamp, never as "" while still Nominal.
    if migrating_since:
        patch.status["migratingSince"] = migrating_since
    if restarted_at:
        patch.status["restartedAt"] = restarted_at
    if handed_over_at:
        patch.status["handedOverAt"] = handed_over_at
    if edge_unavailable_since:
        patch.status["edgeUnavailableSince"] = edge_unavailable_since
    if fallback_since:
        patch.status["fallbackSince"] = fallback_since
    if fallback_reason:
        patch.status["fallbackReason"] = fallback_reason
    for field in cleared:
        patch.status[field] = None      # merge patch keeps omitted fields
    if window_cursor:
        patch.status["windowCursor"] = window_cursor
    if last_window_checked_at:
        patch.status["lastWindowCheckedAt"] = last_window_checked_at
    patch.status["conditions"] = conditions
    if decision:
        # Whole object: a field this decision does not carry is removed explicitly
        # (merge patch keeps omitted fields -- review of 7f2a55b, P2).
        patch.status["decisionContext"] = {
            **{field: None for field in CONTEXT_FIELDS}, **decision}
