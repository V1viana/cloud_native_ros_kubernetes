"""ROSModuleController: reconciles a ROSModule onto a Kubernetes Deployment.

Owns creation, update, drift-healing and update/rollback (U1/U2-equivalent)
of the workload; lifecycle transitions (Unconfigured/Inactive/Active) are
delegated to lifecycle_controller.py, which talks to the State Bridge
rather than to Kubernetes objects. Splitting the two keeps each controller
reconciling one kind of desired state, per the proposal's "responsabilita'
singola" design (S4.2).

Update/rollback state machine (Stable -> RollingOut -> Stable|RolledBack):
Kopf handlers are stateless between ticks, so "what spec is actually known
to work" has nowhere to live except status.lastGoodSpec -- a copy of spec,
not a reference, so a later edit to the live spec can never retroactively
change what "good" meant at the time it converged. A spec matching
status.lastFailedSpec while RolledBack is deliberately NOT retried
automatically (matches variant A's KubeROS: revision 3 stays rejected
until a human changes the request, it does not keep hammering the same
broken image); any other new spec value is always attempted.
"""

from collections.abc import Mapping

import kopf
from kubernetes import client

from .audit import audit, k8s_event, notify
from .conditions import upsert_condition
from .placement import observe_placement, resolve_fleet
from .status_conditions import ROSMODULE_OWNED, ModuleStatusClient, write_conditions
from .constants import (
    GROUP,
    RESYNC_INTERVAL_SEC,
    ROSMODULE_UPDATE_TIMEOUT_SEC,
    VERSION,
)
from .k8s_workloads import (
    SPEC_HASH_ANNOTATION,
    ImageResolutionError,
    ReservedParameterError,
    build_service_manifest,
    UnsupportedPackageError,
    check_parameters,
    check_supported,
    build_deployment_manifest,
    deployment_name,
)


def _apps_api():
    return client.AppsV1Api()


def _has_hpa(namespace, deployment):
    hpas = client.AutoscalingV2Api().list_namespaced_horizontal_pod_autoscaler(namespace)
    return any(
        hpa.spec.scale_target_ref.kind == "Deployment"
        and hpa.spec.scale_target_ref.api_version == "apps/v1"
        and hpa.spec.scale_target_ref.name == deployment
        for hpa in hpas.items
    )


# R1, decision 3 (docs/CRD_CONTRACT_AUDIT.md): the fields of a revision, the
# ones that produce the Pod template. lifecycleTarget is a control command the
# State Bridge applies from the watched spec: changing it opens no revision,
# restarts no Pod and emits no ROSModuleUpdate, and a workload rollback, which
# works on this snapshot only, can never bring an old lifecycleTarget back.
WORKLOAD_FIELDS = ("robotId", "package", "placement", "rosParamMap", "probes", "metricsWindowSec")


def _workload(spec):
    """The revision snapshot of a spec (also of an older full-spec snapshot)."""
    return {field: spec[field] for field in WORKLOAD_FIELDS if field in (spec or {})}


def _whole(new, old):
    """A merge patch that writes `new` in place of `old`: keys `new` no longer has
    become None, so a snapshot never keeps a removed key (a removed rosParamMap
    entry used to stay in lastGoodSpec and never compare equal again)."""
    if not isinstance(new, dict) or not isinstance(old, dict):
        return new
    patch = {key: _whole(value, old.get(key)) for key, value in new.items()}
    patch.update({key: None for key in old if key not in new})
    return patch


def _core_api():
    return client.CoreV1Api()


def _service_matches(existing, manifest):
    spec = existing.spec
    have = [(p.name, p.port, int(p.target_port) if str(p.target_port).isdigit() else p.target_port,
             p.protocol) for p in (spec.ports or [])]
    want = [(p["name"], p["port"], p["targetPort"], p["protocol"]) for p in manifest["spec"]["ports"]]
    return (have == want and spec.type == manifest["spec"]["type"]
            and dict(spec.selector or {}) == manifest["spec"]["selector"])


def _reconcile_service(namespace, name, body, declared, known, conditions, logger):
    """R1, decision 4: create, update, delete or repair the module's own Service;
    a Service of the same name owned by someone else is never adopted or
    modified. Returns the name of the Service this module owns, or None.
    `known` is status.serviceName: a module that neither declares nor owns a
    Service costs no API call at all."""
    if not declared and not known:
        return None
    api = _core_api()
    try:
        existing = api.read_namespaced_service(name, namespace)
    except client.ApiException as exc:
        if exc.status != 404:
            raise
        existing = None
    uid = body["metadata"].get("uid")
    owned = existing is not None and any(
        ref.kind == "ROSModule" and ref.uid == uid
        for ref in (existing.metadata.owner_references or []))
    if existing is not None and not owned:
        if declared:
            message = f"Service {name} exists and is not owned by this ROSModule: left untouched"
            entering = not any(c.get("type") == "ServiceReconciled" and c.get("reason") == "NameConflict"
                               for c in conditions)
            upsert_condition(conditions, "ServiceReconciled", False, reason="NameConflict", message=message)
            logger.error("ROSModule %s: %s", body["metadata"]["name"], message)
            if entering:
                k8s_event(body, "ServiceNameConflict", message, type_="Warning", logger=logger)
        return None
    if not declared:
        if owned:
            api.delete_namespaced_service(name, namespace)
            k8s_event(body, "ServiceDeleted", f"Service {name} removed with spec.service", logger=logger)
        if any(c.get("type") == "ServiceReconciled" for c in conditions):
            upsert_condition(conditions, "ServiceReconciled", True, reason="NoServiceDeclared")
        return None
    manifest = build_service_manifest(name, namespace, declared)
    kopf.adopt(manifest, owner=body)
    if existing is None:
        api.create_namespaced_service(namespace, manifest)
        k8s_event(body, "ServiceCreated", f"Service {name} created", logger=logger)
    elif not _service_matches(existing, manifest):
        replacement = {**manifest,
                       "metadata": {**manifest["metadata"],
                                    "resourceVersion": existing.metadata.resource_version},
                       "spec": {**manifest["spec"], "clusterIP": existing.spec.cluster_ip}}
        api.replace_namespaced_service(name, namespace, replacement)
        k8s_event(body, "ServiceUpdated", f"Service {name} set to the declared ports", logger=logger)
    upsert_condition(conditions, "ServiceReconciled", True, reason="ServiceApplied")
    return name


def _rejection(workload):
    """(reason, message) if the operator cannot run this workload, else None."""
    try:
        check_supported(workload.get("package"))
    except UnsupportedPackageError as exc:
        return "UnsupportedPackage", str(exc)
    try:
        check_parameters(workload.get("rosParamMap"))
    except ReservedParameterError as exc:
        return "ReservedParameter", str(exc)
    return None


def _plain(value):
    """Recursively convert kopf's own Mapping/Sequence view types (Spec,
    Status, ...) into plain dict/list.

    Found live: `dict(spec)` only converts the top level -- a nested value
    such as spec["rosParamMap"] can stay a kopf-internal mapping type that
    does not compare equal (`==`) to the plain dict status.lastGoodSpec
    round-trips as once it has been through the Kubernetes API as real
    JSON. That silently broke every `spec == last_good_spec` /
    `spec == last_failed_spec` check below: every tick looked like a brand
    new attempt, spec never seemed to become memory-equal-to-itself, and no
    RollingOut ever satisfied Stable/RolledBack. Applied once, at the
    boundary, on `spec`; last_good_spec/last_failed_spec already come back
    from `status` as genuinely plain dicts (real JSON through the K8s API),
    so they do not need it too.
    """
    if isinstance(value, Mapping):
        return {key: _plain(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(item) for item in value]
    return value


def _write_conditions(namespace, body, conditions, logger):
    """Every ROSModuleController condition goes here, early exits included, never into
    Kopf's patch: only Reconciled, UpdateSucceeded and ServiceReconciled, merged into the
    latest read with resourceVersion (results/evidence/runs/CONDITIONS_CONCURRENCY.md)."""
    return write_conditions(ModuleStatusClient(namespace), body["metadata"]["name"], conditions,
                            ROSMODULE_OWNED, uid=body["metadata"].get("uid"),
                            generation=body["metadata"].get("generation"), logger=logger)


@kopf.on.create(GROUP, VERSION, "rosmodules")
@kopf.on.update(GROUP, VERSION, "rosmodules")
@kopf.on.timer(GROUP, VERSION, "rosmodules", interval=RESYNC_INTERVAL_SEC)
def reconcile_rosmodule(spec, status, namespace, patch, logger, body, robotfleet_members=None, **_):
    """Create the Deployment if missing, patch it if drifted from spec,
    and track whether the current spec is actually the one running.

    The timer handler is what gives self-healing after an out-of-band
    change (scenario S1, MTTR-drift metric): every resync period the
    desired manifest is recomputed and re-applied even if nothing in
    `spec` changed, so a `kubectl delete` on the owned Deployment is
    corrected within one resync interval without any ROS-level event
    ever being involved.
    """
    rosmodule_name = body["metadata"]["name"]
    name = deployment_name(rosmodule_name)
    # Plain-ified (recursively) so equality against status.lastGoodSpec/
    # lastFailedSpec (plain dicts round-tripped through the K8s API) is a
    # straightforward content comparison at every nesting level, not
    # dependent on kopf's own Spec mapping type implementing __eq__ the
    # way we'd want.
    spec = _plain(spec)
    workload = _workload(spec)
    status = status or {}
    conditions = list(status.get("conditions", []))
    stored_good = _plain(status.get("lastGoodSpec"))
    stored_failed = _plain(status.get("lastFailedSpec"))
    last_good_spec = _workload(stored_good) if stored_good is not None else None
    last_failed_spec = _workload(stored_failed) if stored_failed is not None else None
    update_state = status.get("updateState", "Stable")
    pending_since = status.get("pendingSince", "")
    revision = status.get("revision", 0)

    # R1, decision 1: a spec the operator cannot run is rejected, not applied.
    # Creation: no Deployment. A working module: its last good workload keeps
    # being served -- an invalid input never removes a working service.
    rejected = _rejection(workload)
    if rejected:
        reason, message = rejected
        entering = not any(c.get("type") == "Reconciled" and c.get("reason") == reason
                           for c in conditions)
        kept = last_good_spec is not None and _rejection(last_good_spec) is None
        message = (f"{message}; change not applied, previous workload kept" if kept
                   else f"{message}; no Deployment created")
        upsert_condition(conditions, "Reconciled", False, reason=reason, message=message)
        logger.error("ROSModule %s: %s", rosmodule_name, message)
        if entering:
            k8s_event(body, reason, message, type_="Warning", logger=logger)
        if not kept:
            _write_conditions(namespace, body, conditions, logger)
            return
        workload = last_good_spec

    manifest_spec, update_state, pending_since, starting_new_attempt = _resolve_manifest_spec(
        workload, last_good_spec, last_failed_spec, update_state, pending_since
    )

    # R7: the robot's RobotFleet (kopf index, no API call) decides the edge
    # node pool; the link is written to status so it is visible, not implied.
    fleet, edge_selector, fleet_conflict = resolve_fleet(
        robotfleet_members, namespace, manifest_spec["robotId"])
    patch.status["fleet"] = fleet
    if fleet_conflict and manifest_spec["placement"] == "edge":
        # Two fleets, two possible edge pools: pick none. The Deployment, if
        # any, is left exactly as it is; the fleets report the conflict too.
        message = f"{fleet_conflict}: edge node pool undefined, Deployment left as is"
        entering = not any(c.get("type") == "Reconciled" and c.get("reason") == "FleetAmbiguous"
                           for c in conditions)
        upsert_condition(conditions, "Reconciled", False, reason="FleetAmbiguous", message=message)
        _write_conditions(namespace, body, conditions, logger)
        logger.error("ROSModule %s: %s", rosmodule_name, message)
        if entering:
            k8s_event(body, "FleetAmbiguous", message, type_="Warning", logger=logger)
        return
    if fleet_conflict:
        logger.warning("ROSModule %s: %s (onboard placement does not depend on it)",
                       rosmodule_name, fleet_conflict)

    try:
        manifest = build_deployment_manifest(
            name,
            namespace,
            manifest_spec,
            node_selector_role=manifest_spec["placement"],
            rosmodule_name=rosmodule_name,
            edge_node_selector=edge_selector,
        )
    except ImageResolutionError as exc:
        # Surface this in status.conditions, not only in the operator's own
        # log: `kubectl get rosmodule` should show *why* a module never
        # gets a Deployment instead of just never getting one.
        upsert_condition(
            conditions, "Reconciled", False, reason="ImageResolutionFailed", message=str(exc)
        )
        _write_conditions(namespace, body, conditions, logger)
        logger.error("ROSModule %s: %s", rosmodule_name, exc)
        return
    kopf.adopt(manifest, owner=body)
    previously_reconciled = bool(status.get("lastReconcileTime"))
    _apply_deployment(
        _apps_api(), name, namespace, manifest, logger, body, previously_reconciled
    )

    is_update, not_bootstrap = starting_new_attempt, last_good_spec is not None
    if is_update and not_bootstrap:
        pending_since = _now_iso()
        upsert_condition(
            conditions, "UpdateSucceeded", False, reason="RollingOut",
            message="update in progress",
        )
        logger.info("ROSModule %s: starting update attempt", rosmodule_name)
        k8s_event(body, "UpdateStarted", "rolling out a new spec", logger=logger)
        audit(
            {
                "record_type": "incident_started",
                "correlation_id": f"{rosmodule_name}-update-{pending_since}",
                "event_id": f"{rosmodule_name}-update-{pending_since}",
                "robot_id": manifest_spec["robotId"],
                "event_type": "ROSModuleUpdate",
                "policy_id": "U1U2",
            },
            logger=logger,
        )
    elif starting_new_attempt:
        pending_since = _now_iso()
        upsert_condition(
            conditions, "UpdateSucceeded", False, reason="RollingOut",
            message="update in progress",
        )
        logger.info("ROSModule %s: starting update attempt", rosmodule_name)

    if update_state == "RollingOut":
        deployment = _apps_api().read_namespaced_deployment(name, namespace)
        if _deployment_is_ready(deployment):
            last_good_spec = manifest_spec
            last_failed_spec = None
            revision += 1
            update_state = "Stable"
            pending_since = ""
            upsert_condition(conditions, "UpdateSucceeded", True, reason="RolloutReady")
            logger.info(
                "ROSModule %s: update succeeded, revision %s", rosmodule_name, revision
            )
            if not_bootstrap:
                k8s_event(
                    body, "UpdateSucceeded", f"revision {revision} converged", logger=logger,
                )
                _report_update_outcome(
                    rosmodule_name, manifest_spec["robotId"], revision, success=True,
                    outcome="rosmodule_update_converged", rollback_performed=False,
                    logger=logger,
                )
        else:
            elapsed = _elapsed_sec(pending_since)
            if elapsed > ROSMODULE_UPDATE_TIMEOUT_SEC:
                if last_good_spec is not None:
                    _rollback(namespace, name, rosmodule_name, last_good_spec, body, logger,
                              edge_selector)
                else:
                    logger.warning(
                        "ROSModule %s: update timed out with no previous good "
                        "spec to roll back to; leaving the failed Deployment as-is",
                        rosmodule_name,
                    )
                last_failed_spec = workload
                update_state = "RolledBack"
                pending_since = ""
                upsert_condition(
                    conditions, "UpdateSucceeded", False, reason="RolloutTimeout",
                    message=f"exceeded {ROSMODULE_UPDATE_TIMEOUT_SEC}s",
                )
                logger.warning(
                    "ROSModule %s: update timed out after %.0fs, rolled back",
                    rosmodule_name, elapsed,
                )
                k8s_event(
                    body, "UpdateRolledBack",
                    f"update did not converge within {ROSMODULE_UPDATE_TIMEOUT_SEC}s",
                    type_="Warning", logger=logger,
                )
                _report_update_outcome(
                    rosmodule_name, manifest_spec["robotId"], revision, success=False,
                    outcome="rosmodule_update_rolled_back", rollback_performed=True,
                    logger=logger,
                )

    if not rejected:
        upsert_condition(conditions, "Reconciled", True, reason="DeploymentApplied")
    # Outside the revision: the Service follows the current spec every tick.
    patch.status["serviceName"] = _reconcile_service(
        namespace, name, body, spec.get("service"), status.get("serviceName"), conditions, logger)
    _write_conditions(namespace, body, conditions, logger)
    # Observed from the nodes the current Pods run on (proposal S4: "allineato a
    # cio' che gira realmente"), not copied from spec.placement. None removes
    # the field: nothing scheduled yet is "not observed", not the declared value.
    observed, nodes = observe_placement(namespace, name, body["metadata"].get("uid"),
                                        manifest_spec["robotId"], edge_selector)
    if observed != status.get("observedPlacement"):
        # F2 (docs/F2_EVENTS_PROPOSAL.md): before Kopf's patch, best-effort -- a failed
        # patch may repeat it at the next tick
        k8s_event(body, "PlacementObserved",
                  f"{status.get('observedPlacement') or 'not observed'} -> {observed or 'not observed'}"
                  f"{' on ' + ', '.join(nodes) if nodes else ''}",
                  type_="Warning" if observed == "Unknown" else "Normal", logger=logger)
    patch.status["observedPlacement"] = observed
    patch.status["observedNodes"] = nodes or None
    patch.status["lastReconcileTime"] = _now_iso()
    # The ROSModule's own generation, the Kubernetes convention for
    # observedGeneration: "this controller has acted on this spec". It used to
    # carry the owned Deployment's generation (the pre-replace one, on the
    # replace path), while the State Bridge wrote the ROSModule's into the same
    # field -- two meanings alternating every tick, seen in the saved P2-B
    # resources (ROSModule generation 2, Deployment generation 1). The bridge
    # no longer writes it: per-Pod freshness lives in status.lifecycleInstances.
    # See docs/CRD_CONTRACT_AUDIT.md, D1/D2.
    patch.status["observedGeneration"] = body["metadata"]["generation"]
    patch.status["updateState"] = update_state
    patch.status["revision"] = revision
    if last_good_spec is not None:
        patch.status["lastGoodSpec"] = _whole(last_good_spec, stored_good)
    if last_failed_spec is not None:
        patch.status["lastFailedSpec"] = _whole(last_failed_spec, stored_failed)
    # Always patched, including when clearing it -- but as `None` (JSON
    # Merge Patch field removal), never `""`. Two bugs, found live, in
    # sequence: first, a truthy-only guard here skipped patching on clear,
    # leaving a stale timestamp on status.pendingSince forever after the
    # first attempt. Fixing that by unconditionally sending "" broke
    # something worse: status.pendingSince is `format: date-time`, and the
    # API server DOES enforce that for CRDs -- an empty string is not a
    # valid date-time, so the API server rejected the ENTIRE status patch
    # with a 422 every single time this field was being cleared, which is
    # exactly the tick that also carries the new revision/updateState. The
    # patch failing atomically silently discarded all of it: revision
    # never advanced, updateState never left RollingOut, no matter how
    # many times the deployment was actually ready. `None` here removes
    # the key from status entirely via merge-patch semantics -- "not
    # currently pending" is genuinely better represented as the field
    # being absent than as an empty string anyway.
    patch.status["pendingSince"] = pending_since or None
    # observedLifecycleState is written by lifecycle_controller.py from the
    # State Bridge report, not here -- this handler only owns the workload.


def _resolve_manifest_spec(spec, last_good_spec, last_failed_spec, update_state, pending_since):
    """Decide which spec to actually render this tick, and whether this
    tick starts tracking a brand-new update attempt."""
    if update_state == "RolledBack" and last_failed_spec is not None and spec == last_failed_spec:
        # Same bad spec the CR still declares -- keep serving the last
        # good manifest, do not retry automatically (matches variant A:
        # a rejected KubeROS revision stays rejected until the request
        # itself changes).
        manifest_spec = last_good_spec if last_good_spec is not None else spec
        return manifest_spec, update_state, "", False
    if last_good_spec is not None and spec == last_good_spec:
        return spec, "Stable", "", False
    if update_state == "RollingOut" and pending_since:
        # Already mid-attempt of this spec. Known limitation: if spec
        # changes again before this settles, this treats the newest value
        # as still "the same attempt" without resetting the clock -- rare
        # in practice, U1/U2 never overlap updates.
        return spec, update_state, pending_since, False
    # Bootstrap (never converged), a fresh update request, or the spec
    # changed again after a rollback to something new (possibly a fix).
    return spec, "RollingOut", pending_since, True


def _deployment_is_ready(deployment):
    desired = deployment.spec.replicas or 1
    deployment_status = deployment.status
    return (
        (deployment_status.observed_generation or 0) >= deployment.metadata.generation
        and (deployment_status.updated_replicas or 0) >= desired
        and (deployment_status.ready_replicas or 0) >= desired
        and (deployment_status.available_replicas or 0) >= desired
        and (deployment_status.unavailable_replicas or 0) == 0
    )


def _rollback(namespace, name, rosmodule_name, last_good_spec, body, logger, edge_selector=None):
    rollback_manifest = build_deployment_manifest(
        name, namespace, last_good_spec,
        node_selector_role=last_good_spec["placement"],
        rosmodule_name=rosmodule_name,
        edge_node_selector=edge_selector,
    )
    kopf.adopt(rollback_manifest, owner=body)
    _apps_api().replace_namespaced_deployment(name, namespace, rollback_manifest)
    logger.warning(
        "ROSModule %s: Deployment %s reverted to last good spec", rosmodule_name, name
    )


def _report_update_outcome(rosmodule_name, robot_id, revision, success, outcome, rollback_performed, logger):
    correlation_id = f"{rosmodule_name}-update-r{revision}"
    record = {
        "record_type": "incident_completed",
        "correlation_id": correlation_id,
        "event_id": correlation_id,
        "robot_id": robot_id,
        "event_type": "ROSModuleUpdate",
        "policy_id": "U1U2",
        "success": success,
        "outcome": outcome,
        "rollback_performed": rollback_performed,
    }
    audit(record, logger=logger)
    notify(
        {
            "correlation_id": correlation_id,
            "robot_id": robot_id,
            "event_type": "ROSModuleUpdate",
            "outcome": outcome,
            "success": success,
            "rollback_performed": rollback_performed,
        },
        logger=logger,
    )


def _report_drift(body, name, drift, detail, correlation_suffix, logger):
    """Event DriftHealed and audit OutOfBandDrift for an out-of-band drift the
    controller has just repaired (S1, MTTR-drift). R10, decision D3 (Viviana,
    2026-09-26): every repaired drift is observable -- a deleted Deployment,
    replicas without an HPA, a container image -- and only real drift: never
    the bootstrap, a legitimate update (the spec hash changed) or replicas an
    HPA owns, which the callers never report."""
    rosmodule_name = body["metadata"]["name"]
    k8s_event(body, "DriftHealed", f"Deployment {name}: {detail} (out-of-band change); repaired",
              type_="Warning", logger=logger)
    audit(
        {
            "record_type": "incident_completed",
            "correlation_id": f"{rosmodule_name}-drift-{correlation_suffix}",
            "event_id": f"{rosmodule_name}-drift-{correlation_suffix}",
            "robot_id": body.get("spec", {}).get("robotId", ""),   # reporting never breaks a repair
            "event_type": "OutOfBandDrift",
            "policy_id": "S1",
            "success": True,
            "outcome": "drift_healed",
            "rollback_performed": False,
            "drift": drift,
            "drift_detail": detail,
            "rosmodule": rosmodule_name,
            "deployment": name,
        },
        logger=logger,
    )


def _apply_deployment(api, name, namespace, manifest, logger, body, previously_reconciled):
    """Create-or-replace, tolerant of a concurrent invocation of this same
    handler doing the same thing first.

    Found live: @kopf.on.create and @kopf.on.timer both decorate
    reconcile_rosmodule, and Kopf does not guarantee they never overlap for
    the same object (the timer's first tick can land moments after
    creation) -- read-then-create is not atomic, so two concurrent
    invocations can both observe 404 and both attempt create; the loser
    used to raise an unhandled 409 traceback, self-healing only because
    Kopf retries the whole handler later. Treating that 409 as "someone
    else just created it" and falling through to replace makes the
    (frequent, benign) race quiet instead of noisy.

    `previously_reconciled` (status.lastReconcileTime already set on entry)
    is what tells a genuine drift-heal (scenario S1, MTTR-drift metric --
    something deleted the Deployment out from under an already-converged
    ROSModule) apart from this being the very first creation: both look
    identical from here (a 404 followed by a create), but only the former
    is the incident this proposal's own MTTR-drift metric is about.
    """
    rosmodule_name = body["metadata"]["name"]
    try:
        existing = api.read_namespaced_deployment(name, namespace)
    except client.ApiException as exc:
        if exc.status != 404:
            raise
        try:
            created = api.create_namespaced_deployment(namespace, manifest)
            if previously_reconciled:
                logger.warning(
                    "ROSModule %s: Deployment %s missing on an already-reconciled "
                    "module -- out-of-band drift, recreating", rosmodule_name, name,
                )
                _report_drift(body, name, "deleted", "Deployment was missing, recreated",
                              created.metadata.creation_timestamp, logger)
            else:
                logger.info("ROSModule %s: Deployment %s created", rosmodule_name, name)
            return created.metadata.generation
        except client.ApiException as create_exc:
            if create_exc.status != 409:
                raise
            existing = api.read_namespaced_deployment(name, namespace)

    # spec.selector.matchLabels is immutable on a Deployment once created --
    # build_deployment_manifest() bakes `placement` into it (onboard vs.
    # edge need different labels so Kubernetes never schedules one drone's
    # Pod as if it were another's), so a ROSModule whose spec.placement
    # changes in place, on an already-existing Deployment, would otherwise
    # hit a hard API rejection from replace_namespaced_deployment (the
    # error the code below used to have no handling for at all). Found on
    # review, not exercised by any scenario run so far: P2/E4's own
    # migration sidesteps this by creating a whole separate ROSModule/
    # Deployment for the edge instance rather than ever mutating placement
    # on the existing one -- but the CRD schema itself allows it, so the
    # controller needs to cope with it. A selector change can only be
    # realized by delete-then-create, never by an in-place replace.
    if existing.spec.selector.match_labels != manifest["spec"]["selector"]["matchLabels"]:
        logger.warning(
            "ROSModule %s: Deployment %s selector changed (placement moved) -- "
            "deleting and recreating, an in-place replace cannot change it",
            rosmodule_name, name,
        )
        api.delete_namespaced_deployment(name, namespace)
        created = api.create_namespaced_deployment(namespace, manifest)
        k8s_event(
            body, "PlacementChanged",
            f"Deployment {name} recreated for new placement (selector is immutable)",
            logger=logger,
        )
        return created.metadata.generation

    existing_hash = (existing.metadata.annotations or {}).get(SPEC_HASH_ANNOTATION)
    desired_hash = manifest["metadata"]["annotations"].get(SPEC_HASH_ANNOTATION)
    # The hash annotation alone is not proof the live object still matches
    # it: it is written once, at create/replace time, by this controller --
    # an out-of-band kubectl edit that changes .spec.replicas or the
    # container image but leaves that specific annotation key untouched
    # (which any edit not explicitly targeting it does) leaves existing_hash
    # unchanged, so this check alone would report "nothing to do" against a
    # genuinely drifted live object. Found live re-checking the code, not
    # from a failing test. Re-deriving a full hash from the live typed
    # object back into the exact dict shape _spec_hash() expects risks
    # spurious mismatches from server-side defaulting alone (re-introducing
    # the "replace every tick" bug the annotation itself was added to fix,
    # see above) -- so this checks the two concrete fields an out-of-band
    # edit would actually change instead of trusting the annotation alone.
    # Compares every container's image, not just the first: found by an
    # external review, confirmed by reading the code, that the original
    # check only ever looked at existing_containers[0] -- a change to the
    # state-bridge sidecar's own image (the second container) would never
    # register as drift at all.
    existing_containers = (existing.spec.template.spec.containers or [])
    desired_containers = manifest["spec"]["template"]["spec"]["containers"]
    existing_images = [c.image for c in existing_containers]
    desired_images = [c["image"] for c in desired_containers]
    image_drifted = existing_images != desired_images
    # Preserve scaling only when a real HPA targets this Deployment.
    # An edge placement alone does not prove that any autoscaler exists.
    has_hpa = _has_hpa(namespace, name)
    if has_hpa:
        manifest["spec"]["replicas"] = existing.spec.replicas
        replicas_drifted = False
    else:
        replicas_drifted = existing.spec.replicas != manifest["spec"]["replicas"]

    drifted = image_drifted or replicas_drifted
    if existing_hash is not None and existing_hash == desired_hash and not drifted:
        # Nothing to do: found live that replacing unconditionally on every
        # 30s resync tick, identical content or not, still bumps
        # metadata.generation every single time -- which then made the
        # update/rollback readiness check (observedGeneration >=
        # generation) never converge, because it re-reads the Deployment
        # in the very same tick that just bumped generation again, always
        # one step ahead of what the (separate, async) Deployment
        # controller has had time to observe.
        return existing.metadata.generation

    api.replace_namespaced_deployment(name, namespace, manifest, dry_run=None)
    logger.info("ROSModule %s: Deployment %s updated", body["metadata"]["name"], name)
    if existing_hash is not None and existing_hash == desired_hash and drifted:
        # The spec is unchanged and the live object is not what it declares:
        # an out-of-band change, repaired by the replace above (R10, D3).
        kinds, details = [], []
        if replicas_drifted:
            kinds.append("replicas")
            details.append(f"replicas {existing.spec.replicas} -> {manifest['spec']['replicas']}")
        if image_drifted:
            kinds.append("image")
            details.append(f"images {existing_images} -> {desired_images}")
        _report_drift(body, name, ",".join(kinds), "; ".join(details), _now_iso(), logger)
    return existing.metadata.generation


def _now_iso():
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace(
        "+00:00", "Z"
    )


def _elapsed_sec(since_iso):
    from datetime import datetime, timezone

    if not since_iso:
        return float("inf")
    since = datetime.fromisoformat(since_iso.replace("Z", "+00:00"))
    return (datetime.now(timezone.utc) - since).total_seconds()
