"""Time-to-rebuild: what "fleet ready" means for each variant (Viviana, 1 October 2026;
docs/TTR_PILOT_PREREGISTRATION.md). Fixed here, before the pilot, and read by the judge and
by the ROS probe -- never "every Deployment of the namespace".

Each variant is measured with its own complete fleet and its own control plane:
- A, per drone: Agent, PX4, analytics and event detector; lifecycle nodes event_detector and
  companion_analytics_onboard active.
- B, per drone: Agent, PX4 and analytics (the ROSModule's Deployment); analytics active. No
  event detector: it is not part of B's architecture.
The common subset (per drone Agent, PX4, analytics, analytics active) is a secondary
measure from the same samples, never a substitute.
"""

ROBOTS = ("drone01", "drone02", "drone03")
NAMESPACE = "cloud-native-p2"

CONTROL_PLANE = {
    "a": ("p2-fastdds-discovery", "kuberos", "application-manager-p2", "operational-event-dispatcher-p2",
          "p2-audit-writer", "p2-operator-notifier", "p2-platform-observer"),
    "b": ("p2-fastdds-discovery", "fleet-operator", "p2-audit-writer", "p2-operator-notifier",
          "p2-platform-observer"),
}


def analytics_deployment(variant, robot):
    return f"{robot}-companion-analytics-onboard" if variant == "a" else f"companion-analytics-{robot}"


def drone_deployments(variant, robot):
    common = [f"{robot}-microxrce-agent", f"{robot}-px4-sitl", analytics_deployment(variant, robot)]
    return common + ([f"{robot}-event-detector"] if variant == "a" else [])


# ROS lifecycle nodes, matched on the ROS graph in the robot's namespace (full match). B's
# analytics node carries its Pod's name (k8s_workloads._pod_unique_node_name), so it is a
# pattern; the edge instance (companion_analytics_edge_...) never matches it.
def lifecycle_nodes(variant, robot):
    if variant == "a":
        return {"event_detector": "event_detector", "analytics": "companion_analytics_onboard"}
    return {"analytics": rf"companion_analytics_companion_analytics_{robot}_[A-Za-z0-9_]+"}


def deployments(variant, subset="full"):
    """subset "full": control plane + every drone component; "common": per drone Agent, PX4,
    analytics only."""
    out = list(CONTROL_PLANE[variant]) if subset == "full" else []
    for robot in ROBOTS:
        out += drone_deployments(variant, robot) if subset == "full" else drone_deployments(variant, robot)[:3]
    return out


def lifecycle_keys(variant, subset="full"):
    return [(robot, key) for robot in ROBOTS for key in lifecycle_nodes(variant, robot)
            if subset == "full" or key == "analytics"]


def probe_spec(variant):
    """The ROS probe's input: {robot: {key: regex}}."""
    return {robot: lifecycle_nodes(variant, robot) for robot in ROBOTS}
