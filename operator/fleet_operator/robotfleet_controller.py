"""RobotFleetController: tracks fleet membership and overall readiness.

Membership is spec.robots (robot ids unique by schema); a ROSModule belongs to
a robot through its own spec.robotId, not through a label: nothing enforces
that a hand-set `robot` label agrees with spec.robotId.

A robot is ready when every function declared for it -- each distinct
spec.package among its ROSModules -- is served by at least one of them
(docs/CRD_CONTRACT_AUDIT.md, R6). "Served" is lifecycle_controller.
module_serving: target Active and at least one current, Ready Pod of the
Deployment that module owns, confirmed Active by a fresh per-Pod record. The
flat observedLifecycleState is not used: it has no freshness and with several
Pods the last writer wins. Onboard and edge twins of the same package are
alternatives, so a healthy blue/green migration (adaptation_controller.py)
keeps the robot ready while onboard is Inactive on purpose and edge is Active,
and so does an edge that is still starting while onboard serves.

Availability, not convergence: a Pod still starting does not unserve the
Pods already Active. With the testbed's maxSurge 0 rollout a single-replica
module is really unserved while its Pod is replaced, and readyRobots says so.

Edge nodes (R7): status.edgeNodes lists the nodes edgeNodeSelector matches
(the default edge role when none is declared), the pool the ROSModule
controller schedules this fleet's edge modules on (placement.py). A robot
another RobotFleet in the namespace also lists is not ready here, with the
other fleet named: its edge pool, and so its membership, is ambiguous.
"""

from datetime import datetime, timezone

import kopf
from kubernetes import client

from .audit import k8s_event
from .conditions import upsert_condition
from .constants import GROUP, RESYNC_INTERVAL_SEC, VERSION
from .k8s_workloads import OWNER_LABEL, deployment_name
from .lifecycle_controller import module_serving
from .placement import DEFAULT_EDGE_SELECTOR, label_selector

_MESSAGE_ROBOTS = 5


@kopf.index(GROUP, VERSION, "robotfleets")
def robotfleet_members(namespace, name, spec, **_):
    """(namespace, robot id) -> the fleets listing that robot, with their edge selector.

    In memory, fed by the watch kopf already runs on RobotFleets; kopf passes it
    to every handler as the `robotfleet_members` kwarg.
    """
    selector = dict(spec.get("edgeNodeSelector") or {})
    return {(namespace, robot["id"]): {"fleet": name, "edgeNodeSelector": selector}
            for robot in spec.get("robots", [])}


def _fleet_snapshot(namespace):
    """Four LISTs per reconcile whatever the fleet size, not three calls per module."""
    modules = client.CustomObjectsApi().list_namespaced_custom_object(
        GROUP, VERSION, namespace, "rosmodules"
    ).get("items", [])
    apps = client.AppsV1Api()
    deployments = apps.list_namespaced_deployment(namespace, label_selector=OWNER_LABEL).items
    replicasets = apps.list_namespaced_replica_set(namespace, label_selector=OWNER_LABEL).items
    pods = client.CoreV1Api().list_namespaced_pod(namespace, label_selector=OWNER_LABEL).items
    return modules, deployments, replicasets, pods


def fleet_readiness(robots, modules, deployments, replicasets, pods, now):
    """{robot id: None if ready, else why not} for every member robot."""
    deployments = {item.metadata.name: item for item in deployments}
    result = {}
    for robot in robots:
        functions = {}
        for module in modules:
            spec, metadata = module.get("spec", {}), module["metadata"]
            if spec.get("robotId") != robot["id"] or metadata.get("deletionTimestamp"):
                continue
            serving, reason = module_serving(
                module, deployments.get(deployment_name(metadata["name"])), replicasets, pods, now)
            functions.setdefault(spec.get("package"), []).append(
                (serving, f"{metadata['name']}={reason}"))
        if not functions:
            result[robot["id"]] = "NoModules"
            continue
        unserved = [f"{package}: {', '.join(reason for _, reason in entries)}"
                    for package, entries in sorted(functions.items())
                    if not any(serving for serving, _ in entries)]
        result[robot["id"]] = "; ".join(unserved) or None
    return result


def _message(readiness):
    ready = sum(1 for why in readiness.values() if why is None)
    text = f"{ready}/{len(readiness)} robots served by an Active module on a current Pod"
    unready = [f"{robot} ({why})" for robot, why in readiness.items() if why is not None]
    if unready:
        more = len(unready) - _MESSAGE_ROBOTS
        text += "; not ready: " + ", ".join(unready[:_MESSAGE_ROBOTS])
        text += f" and {more} more" if more > 0 else ""
    return ready, text


@kopf.on.create(GROUP, VERSION, "robotfleets")
@kopf.on.update(GROUP, VERSION, "robotfleets")
@kopf.on.timer(GROUP, VERSION, "robotfleets", interval=RESYNC_INTERVAL_SEC)
def reconcile_robotfleet(spec, status, namespace, patch, logger, body, robotfleet_members=None, **_):
    robots = spec.get("robots", [])
    readiness = fleet_readiness(robots, *_fleet_snapshot(namespace), datetime.now(timezone.utc))
    fleet_name = body["metadata"]["name"]
    for robot in robots:
        others = sorted({entry["fleet"] for entry in (robotfleet_members or {}).get(
            (namespace, robot["id"]), []) if entry["fleet"] != fleet_name})
        if others:
            readiness[robot["id"]] = f"ClaimedByOtherFleet: {', '.join(others)}"
    edge_selector = dict(spec.get("edgeNodeSelector") or DEFAULT_EDGE_SELECTOR)
    edge_nodes = sorted(node.metadata.name for node in client.CoreV1Api().list_node(
        label_selector=label_selector(edge_selector)).items)
    ready_robots, message = _message(readiness)
    was_available = any(
        c.get("type") == "Available" and c.get("status") == "True"
        for c in (status or {}).get("conditions", [])
    )
    is_available = ready_robots == len(robots) and len(robots) > 0

    conditions = list((status or {}).get("conditions", []))
    upsert_condition(
        conditions,
        "Available",
        is_available,
        reason="AllRobotsActive" if is_available else "RobotsNotReady",
        message=message,
    )
    if is_available != was_available:
        k8s_event(
            body,
            "AllRobotsActive" if is_available else "RobotsNotReady",
            message,
            type_="Normal" if is_available else "Warning",
            logger=logger,
        )

    previous_ready = (status or {}).get("readyRobots")
    if previous_ready is not None and previous_ready != ready_robots and is_available == was_available:
        # F2: a count change the Available Event does not already report (the first
        # value after creation is not a change)
        k8s_event(body, "ReadyRobotsChanged", f"{previous_ready} -> {ready_robots} ready: {message}",
                  logger=logger)
    patch.status["readyRobots"] = ready_robots
    patch.status["edgeNodes"] = edge_nodes
    patch.status["observedGeneration"] = body["metadata"]["generation"]
    patch.status["conditions"] = conditions
    logger.info("RobotFleet reconciled: %s", message)
