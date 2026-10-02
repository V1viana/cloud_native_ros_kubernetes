"""Read-only S3 bootstrap barrier, run inside the Fleet Operator image."""

import argparse
import json
import time
from datetime import datetime, timezone

from .constants import GROUP, VERSION
from .k8s_workloads import deployment_name
from .lifecycle_controller import inventory_lifecycle_summary


def baseline_ready(deployment, replicasets, pods):
    if (deployment is None or deployment.metadata.deletion_timestamp
            or (deployment.status.observed_generation or 0) < deployment.metadata.generation
            or not deployment.spec.replicas
            or deployment.status.updated_replicas != deployment.spec.replicas
            or deployment.status.ready_replicas != deployment.spec.replicas
            or deployment.status.replicas != deployment.spec.replicas):
        return False
    owned_sets = {rs.metadata.uid for rs in replicasets if any(
        ref.kind == "Deployment" and ref.uid == deployment.metadata.uid
        for ref in (rs.metadata.owner_references or []))}
    current = [pod for pod in pods if not pod.metadata.deletion_timestamp and any(
        ref.kind == "ReplicaSet" and ref.uid in owned_sets
        for ref in (pod.metadata.owner_references or []))]
    return len(current) == deployment.spec.replicas and all(
        pod.status.phase == "Running" and any(c.type == "Ready" and c.status == "True"
            for c in (pod.status.conditions or [])) for pod in current)


def evaluate_fleet(modules, deployments, replicasets, pods, count, now):
    issues = {}
    modules = {item["metadata"]["name"]: item for item in modules}
    deployments = {item.metadata.name: item for item in deployments}
    fingerprint = []
    for index in range(1, count + 1):
        robot = f"drone{index:02d}"
        name = f"companion-analytics-{robot}"
        module = modules.get(name)
        if module is None:
            issues[name] = "ROSModuleMissing"
            continue
        spec, metadata = module["spec"], module["metadata"]
        if (spec.get("robotId") != robot or spec.get("placement") != "onboard"
                or spec.get("lifecycleTarget") != "Active"):
            issues[name] = "UnexpectedBootstrapSpec"
            continue
        settled, reason, _, _ = inventory_lifecycle_summary(
            module, deployments.get(deployment_name(name)), replicasets, pods, now)
        if not settled:
            issues[name] = reason
        elif module.get("status", {}).get("updateState") != "Stable":
            issues[name] = "RolloutNotStable"
        fingerprint.append((metadata["uid"], metadata["generation"]))
        for suffix in ("px4-sitl", "microxrce-agent"):
            baseline = deployments.get(f"{robot}-{suffix}")
            if not baseline_ready(baseline, replicasets, pods):
                issues[f"{robot}-{suffix}"] = "DeploymentNotReady"
    # Restart or replacement breaks the window, even if Active was briefly seen.
    for pod in pods:
        if pod.metadata.deletion_timestamp or pod.status.phase in ("Succeeded", "Failed"):
            continue
        fingerprint.append((pod.metadata.uid, tuple(
            (c.name, c.restart_count) for c in (pod.status.container_statuses or []))))
    return issues, tuple(sorted(fingerprint, key=str))


class StableWindow:
    def __init__(self, duration):
        self.duration = duration
        self.since = None
        self.fingerprint = None

    def observe(self, issues, fingerprint, now):
        if issues:
            self.since = self.fingerprint = None
            return False
        if self.since is None or fingerprint != self.fingerprint:
            self.since, self.fingerprint = now, fingerprint
        return now - self.since >= self.duration


def main():
    from kubernetes import client, config

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--namespace", required=True)
    parser.add_argument("--robots", type=int, required=True)
    parser.add_argument("--timeout", type=float, default=180)
    parser.add_argument("--stable-seconds", type=float, default=15)
    args = parser.parse_args()
    if args.robots < 1 or not 0 < args.stable_seconds < args.timeout:
        parser.error("require robots > 0 and 0 < stable-seconds < timeout")
    config.load_incluster_config()
    custom, apps, core = client.CustomObjectsApi(), client.AppsV1Api(), client.CoreV1Api()
    window = StableWindow(args.stable_seconds)
    deadline = time.monotonic() + args.timeout
    while time.monotonic() < deadline:
        try:
            options = {"_request_timeout": min(10, max(0.1, deadline - time.monotonic()))}
            modules = custom.list_namespaced_custom_object(
                GROUP, VERSION, args.namespace, "rosmodules", **options)["items"]
            deployments = apps.list_namespaced_deployment(args.namespace, **options).items
            replicasets = apps.list_namespaced_replica_set(args.namespace, **options).items
            pods = core.list_namespaced_pod(args.namespace, **options).items
            issues, fingerprint = evaluate_fleet(
                modules, deployments, replicasets, pods, args.robots, datetime.now(timezone.utc))
        except Exception as exc:
            issues, fingerprint = {"snapshot": f"{type(exc).__name__}: {exc}"}, None
        now = time.monotonic()
        ready = window.observe(issues, fingerprint, now) and now < deadline
        print(json.dumps({"timestamp": datetime.now(timezone.utc).isoformat(),
                          "ready": ready, "issues": issues,
                          "stable_seconds": 0 if window.since is None else now - window.since}),
              flush=True)
        if ready:
            return 0
        time.sleep(max(0, min(5, deadline - time.monotonic())))
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
