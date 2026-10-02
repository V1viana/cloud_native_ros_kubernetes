"""Where a ROSModule may run, and where it actually runs (docs/CRD_CONTRACT_AUDIT.md, R7).

The fleet/module link is spec.robotId against RobotFleet.spec.robots, in the
same namespace: the robot's fleet decides the edge node pool
(edgeNodeSelector). It is read from kopf's in-memory index over RobotFleets
(robotfleet_controller.robotfleet_members), fed by the watch kopf already runs,
so resolving it costs no API call. A robot no fleet lists keeps the default
edge role selector; a robot two fleets list has no defined edge pool.

observedPlacement is classified from the nodes the module's current Pods are
scheduled on, never copied from spec.placement: "onboard" on the robot's own
onboard node, "edge" on a node the edge selector in use matches, "Unknown"
otherwise (another robot's node, a node outside the pool, a mix, a node that no
longer exists, no edge pool defined because two fleets list the robot).
Nothing scheduled yet: not observed. "Current" is the same UID ownership chain
readiness uses (lifecycle_controller._current_pods: ROSModule -> Deployment ->
ReplicaSet -> Pod), so a recreated Deployment's old Pods never count.
"""

import time

from kubernetes import client

from .k8s_workloads import DEFAULT_EDGE_SELECTOR, OWNER_LABEL, _node_selector
from .lifecycle_controller import _current_pods, _owned_by

NODE_LABELS_TTL_SEC = 60
_node_cache = {}


def resolve_fleet(members, namespace, robot_id):
    """-> (fleet name or None, edge node selector or None, conflict message or None)."""
    entries = sorted((members or {}).get((namespace, robot_id), []), key=lambda e: e["fleet"])
    if len(entries) > 1:
        names = ", ".join(entry["fleet"] for entry in entries)
        return None, None, f"RobotFleets {names} all list robot {robot_id}"
    if not entries:
        return None, dict(DEFAULT_EDGE_SELECTOR), None
    return entries[0]["fleet"], dict(entries[0]["edgeNodeSelector"] or DEFAULT_EDGE_SELECTOR), None


def label_selector(selector):
    return ",".join(f"{key}={value}" for key, value in sorted(selector.items()))


def node_labels(name, now=None):
    """A node's labels, re-read at most once a minute; None if the node is gone."""
    now = time.monotonic() if now is None else now
    cached = _node_cache.get(name)
    if cached is not None and now - cached[1] < NODE_LABELS_TTL_SEC:
        return cached[0]
    try:
        labels = dict(client.CoreV1Api().read_node(name).metadata.labels or {})
    except client.ApiException as exc:
        if exc.status != 404:
            raise
        labels = None
    _node_cache[name] = (labels, now)
    return labels


def _matches(labels, selector):
    # No selector (edge pool undefined) matches nothing, never everything.
    return bool(selector) and labels is not None and all(
        labels.get(k) == v for k, v in selector.items())


def classify_pods(pods, deployment, replicasets, robot_id, edge_selector, labels_of):
    """-> (observed placement or None, sorted node names of the current scheduled Pods)."""
    nodes = sorted({pod.spec.node_name for pod in _current_pods(deployment, replicasets, pods)
                    if pod.spec.node_name})
    if not nodes:
        return None, []
    kinds = set()
    for node in nodes:
        labels = labels_of(node)
        if _matches(labels, _node_selector("onboard", robot_id)):
            kinds.add("onboard")
        elif _matches(labels, edge_selector):
            kinds.add("edge")
        else:
            kinds.add("Unknown")
    return (kinds.pop() if len(kinds) == 1 else "Unknown"), nodes


def observe_placement(namespace, deployment_name, module_uid, robot_id, edge_selector):
    apps = client.AppsV1Api()
    try:
        deployment = apps.read_namespaced_deployment(deployment_name, namespace)
    except client.ApiException as exc:
        if exc.status == 404:
            return None, []
        raise
    if (deployment.metadata.deletion_timestamp
            or not _owned_by(deployment, "ROSModule", module_uid)):
        return None, []
    selector = f"{OWNER_LABEL}={deployment_name}"
    replicasets = apps.list_namespaced_replica_set(namespace, label_selector=selector).items
    pods = client.CoreV1Api().list_namespaced_pod(namespace, label_selector=selector).items
    return classify_pods(pods, deployment, replicasets, robot_id, edge_selector, node_labels)
