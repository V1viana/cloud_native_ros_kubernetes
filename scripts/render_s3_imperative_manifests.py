#!/usr/bin/env python3
"""Render S3's own N-robot KubeROS ApplicationDeployment manifests (variant
A), reusing render_e0_manifests.py's own per-robot renderer (unmodified)
instead of duplicating the template-substitution logic -- only the robot
list/count and node names are S3's own (E0's module-level ROBOTS/
render_kuberos_runtime are hardcoded to exactly 3 robots on the p2 cluster,
so this script builds its own N-robot list and its own copy of the KubeROS
runtime env rendering, parametrized, rather than reusing those two directly).
"""

import argparse
import re
from copy import deepcopy
from pathlib import Path

from render_e0_manifests import (
    TEMPLATE,
    load_documents,
    render_robot_manifest,
    write_documents,
)

KUBEROS_BASE = Path(__file__).resolve().parents[1] / "manifests" / "kubernetes" / "p2" / "20-kuberos.yaml"

# Nominal at boot, same as every other robot -- the SLO breach is now
# live-triggered post-bootstrap (run_s3.sh, via the same KubeROS
# update-Job mechanism U1 already validates: manifests/kubernetes/u1/
# 10-update-job.yaml + render_u1_manifest.py, reused as-is). Found live,
# 2026-09-22: baking the fault in from boot (this constant used to be
# "300.0") made variant A's incident timer start only once the whole
# N-robot fleet was confirmed available, not when the fault actually
# began -- silently excluding the time between drone01's own readiness
# and that fleet-wide checkpoint, which grows with N and made A's
# reported incident time incomparable to B's own (whose live
# `kubectl patch` trigger starts the clock at the true fault moment).
# "80.0" matches render_u1_manifest.py's own OLD_LATENCY baseline
# exactly, so this line is a genuine no-op until the live trigger runs.
ONBOARD_PROCESSING_DELAY_MS = "80.0"


def robot_id(i):
    return f"drone{i + 1:02d}"


def build_robots(n_robots, cluster_name):
    robots = []
    for i in range(n_robots):
        robots.append(
            {
                "id": robot_id(i),
                "index": str(i + 1),
                "node": f"k3d-{cluster_name}-agent-{i}",
                # Small deterministic offset per robot, same spirit as E0's
                # own distinct-but-arbitrary per-robot coordinates -- not
                # used for anything beyond keeping PX4_HOME_LAT/LON distinct.
                "home_lat": f"{47.397742 + i * 0.0001:.7f}",
                "home_lon": f"{8.545594 + i * 0.0001:.7f}",
            }
        )
    return robots


def set_env(container, name, value):
    env = container.setdefault("env", [])
    item = next((entry for entry in env if entry.get("name") == name), None)
    if item is None:
        env.append({"name": name, "value": value})
    else:
        item.clear()
        item.update({"name": name, "value": value})


# S3-only: manifests/kuberos/e0/drone-baseline.template.yaml's own
# startupProbe (timeoutSeconds: 5, periodSeconds: 3-4, failureThreshold: 30)
# is shared verbatim with E0/P2/S4, all of which only ever run 3 robots and
# have never approached this timing -- found live at N=20 that a single
# "ros2 lifecycle get" probe invocation can take longer than 5s under the
# CPU contention of 20 concurrent robots, causing the kubelet to restart
# a perfectly healthy container mid-startup (KubeROS's own external
# per-robot readiness wait then times out waiting for it). Loosened here,
# for S3's own rendered manifests only, rather than in the shared template
# that E0/P2/S4 still rely on unmodified.
# timeoutSeconds raised again (10 -> 20) after the CPU-sizing story above:
# with real CPU/RAM no longer the constraint (12 -> 48 cores) and the
# SQLite write-lock bug fixed separately (KUBEROS_SQLITE_TIMEOUT), a live
# N=20 run still showed individual "ros2 lifecycle get" probe attempts
# occasionally exceeding 10s -- traced to ros2's own DDS discovery
# overhead growing with the number of nodes already on the domain
# (measured live: a plain `ros2 node list` from an unrelated, idle pod
# went from ~1.3s at N~3 to ~2.3-2.8s at N~15), not to contention on the
# probed container itself. failureThreshold is left alone: the individual
# attempts were already succeeding on retry, just slowly.
S3_STARTUP_PROBE_PERIOD_SECONDS = 10
S3_STARTUP_PROBE_TIMEOUT_SECONDS = 20
S3_STARTUP_PROBE_FAILURE_THRESHOLD = 40


# Keep the command deadline and its diagnostic message aligned when S3
# widens the shared template's startup probe. Setup runs inside the deadline.
S3_STARTUP_PROBE_KILL_AFTER_SECONDS = S3_STARTUP_PROBE_TIMEOUT_SECONDS - 2
_TEMPLATE_KILL_AFTER = re.compile(r"(?m)^probe_deadline=\d+$")


def loosen_startup_probes(manifest):
    for module in manifest["rosModules"]:
        probe = module.get("startupProbe")
        if probe is None:
            continue
        probe["periodSeconds"] = S3_STARTUP_PROBE_PERIOD_SECONDS
        probe["timeoutSeconds"] = S3_STARTUP_PROBE_TIMEOUT_SECONDS
        probe["failureThreshold"] = S3_STARTUP_PROBE_FAILURE_THRESHOLD
        command = probe.get("exec", {}).get("command")
        if not command:
            continue
        replacement = f"probe_deadline={S3_STARTUP_PROBE_KILL_AFTER_SECONDS}"
        rewritten = [
            _TEMPLATE_KILL_AFTER.sub(replacement, part) for part in command
        ]
        if sum(len(_TEMPLATE_KILL_AFTER.findall(part)) for part in command) != 1:
            raise ValueError(
                "startupProbe command must have exactly one probe_deadline assignment "
                "to rescale -- the shared template changed shape (see Bug #65)"
            )
        probe["exec"]["command"] = rewritten


def raise_onboard_processing_delay(manifest):
    for module in manifest["rosModules"]:
        if module["name"] != "companion-analytics-onboard":
            continue
        module["entrypoint"] = [
            entry.replace(
                "-p processing_delay_ms:=80.0",
                f"-p processing_delay_ms:={ONBOARD_PROCESSING_DELAY_MS}",
            )
            for entry in module["entrypoint"]
        ]
        return
    raise ValueError("companion-analytics-onboard rosModule not found")


def set_onboard_analytics_cpu_limit(manifest, millicores):
    if millicores <= 0:
        raise ValueError("onboard analytics CPU limit must be positive")
    for module in manifest["rosModules"]:
        if module["name"] == "companion-analytics-onboard":
            module["resources"]["limits"]["cpu"] = f"{millicores}m"
            return
    raise ValueError("companion-analytics-onboard rosModule not found")


def render_kuberos_runtime(documents, robots, edge_node, namespace, cluster_name, control_node):
    rendered = deepcopy(documents)
    # The base p2/20-kuberos.yaml (Service + Deployment) hardcodes
    # metadata.namespace: cloud-native-p2 -- unlike the other p2/*.yaml
    # files (re-namespaced via run_s3.sh's own sed-based render_p2_style),
    # this file goes through Python rendering instead, so it needs the
    # same substitution done explicitly here. Found live: applying the
    # unmodified namespace produced "namespaces \"cloud-native-p2\" not
    # found" on the cloud-native-s3 cluster.
    for item in rendered:
        if item.get("metadata", {}).get("namespace") == "cloud-native-p2":
            item["metadata"]["namespace"] = namespace
    deployment = next(
        item for item in rendered
        if item.get("kind") == "Deployment" and item.get("metadata", {}).get("name") == "kuberos"
    )
    pod_spec = deployment["spec"]["template"]["spec"]
    containers = pod_spec.get("initContainers", []) + pod_spec.get("containers", [])
    robot_ids = ",".join(robot["id"] for robot in robots)
    onboard_nodes = ",".join(robot["node"] for robot in robots)
    for container in containers:
        set_env(container, "KUBEROS_ROBOT_ID", robot_ids)
        set_env(container, "KUBEROS_ONBOARD_NODE", onboard_nodes)
        set_env(container, "KUBEROS_EDGE_NODE", edge_node)
        # E0's own render_kuberos_runtime never touches these three: it
        # only ever renders onto the p2 cluster/namespace, where the base
        # manifest's own hardcoded values already match. S3 uses its own
        # cluster/namespace, so these three would otherwise silently keep
        # pointing kuberos at cloud-native-p2 -- found while validating this
        # script's own output, before ever applying it live.
        set_env(container, "KUBEROS_TARGET_NAMESPACE", namespace)
        set_env(container, "KUBEROS_CLUSTER_NAME", cluster_name)
        set_env(container, "KUBEROS_CONTROL_NODE", control_node)
    return rendered


def main(args=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--n-robots", type=int, required=True)
    parser.add_argument("--cluster-name", default="cloud-native-s3")
    parser.add_argument("--namespace", default="cloud-native-s3")
    parser.add_argument("--discovery-address", required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--onboard-analytics-cpu-limit-millicores", type=int)
    parsed = parser.parse_args(args)
    parsed.output_dir.mkdir(parents=True, exist_ok=True)

    robots = build_robots(parsed.n_robots, parsed.cluster_name)
    edge_node = f"k3d-{parsed.cluster_name}-agent-{parsed.n_robots}"
    control_node = f"k3d-{parsed.cluster_name}-server-0"

    template = load_documents(TEMPLATE)[0]
    for index, robot in enumerate(robots):
        manifest = render_robot_manifest(deepcopy(template), robot, parsed.discovery_address)
        loosen_startup_probes(manifest)
        if parsed.onboard_analytics_cpu_limit_millicores is not None:
            set_onboard_analytics_cpu_limit(
                manifest, parsed.onboard_analytics_cpu_limit_millicores
            )
        # No metadata.namespace field here: ApplicationDeployment's own
        # schema (manifests/kuberos/e0/drone-baseline.template.yaml) never
        # has one -- KubeROS takes its target namespace from
        # KUBEROS_TARGET_NAMESPACE on its own Deployment instead, rendered
        # below.
        if index == 0:
            # drone01 only: S3's own single incident (reaction/recovery
            # time and control-plane load under N-robot load) is a
            # successful SLO migration on this one robot; the other N-1
            # stay nominal, providing load without an incident of their own.
            raise_onboard_processing_delay(manifest)
        path = parsed.output_dir / f"{robot['id']}.yaml"
        write_documents(path, [manifest])
        print(path)

    kuberos_docs = render_kuberos_runtime(
        load_documents(KUBEROS_BASE), robots, edge_node,
        parsed.namespace, parsed.cluster_name, control_node,
    )
    kuberos_path = parsed.output_dir / "20-kuberos.yaml"
    write_documents(kuberos_path, kuberos_docs)
    print(kuberos_path)


if __name__ == "__main__":
    main()
