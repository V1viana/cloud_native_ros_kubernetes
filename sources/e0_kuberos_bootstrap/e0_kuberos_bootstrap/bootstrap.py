"""Create and verify the three E0 ApplicationDeployments through KubeROS."""

import argparse
import json
import os
import time
from pathlib import Path

import yaml

from cloud_native_application_manager.kuberos_adapter import KuberosAdapter


def load_manifest(path):
    with Path(path).open(encoding="utf-8") as stream:
        manifest = yaml.safe_load(stream)
    if not isinstance(manifest, dict):
        raise ValueError(f"{path} must contain one YAML object")
    metadata = manifest.get("metadata", {})
    robots = metadata.get("targetRobots", [])
    if len(robots) != 1:
        raise ValueError(f"{path} must target exactly one robot")
    return manifest, robots[0]


def deploy_manifests(paths, adapter, timeout_sec, clock=time.monotonic, emit=print):
    results = []
    for path in sorted(Path(item) for item in paths):
        manifest, robot_id = load_manifest(path)
        deployment_id = manifest["metadata"]["name"]
        correlation_id = f"e0-bootstrap-{robot_id}"
        started = clock()
        adapter.create_deployment(manifest, correlation_id)
        state = adapter.wait_ready(
            deployment_id,
            timeout_sec=timeout_sec,
            correlation_id=correlation_id,
        )
        result = {
            "record_type": "e0_kuberos_deployment_ready",
            "robot_id": robot_id,
            "deployment_id": deployment_id,
            "status": str(state.get("status", "")).lower(),
            "elapsed_sec": round(clock() - started, 3),
        }
        emit("E0_KUBEROS_READY " + json.dumps(result, sort_keys=True))
        results.append(result)
    return results


def main(args=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--manifest-dir", type=Path, required=True)
    parser.add_argument("--kuberos-url", required=True)
    parser.add_argument("--timeout-sec", type=float, default=240.0)
    parsed = parser.parse_args(args)
    token = os.environ.get("KUBEROS_API_TOKEN", "")
    if not token:
        raise RuntimeError("KUBEROS_API_TOKEN is required")
    paths = list(parsed.manifest_dir.glob("*.yaml"))
    if len(paths) != 3:
        raise RuntimeError(
            f"Expected three E0 manifests, found {len(paths)}"
        )
    adapter = KuberosAdapter(
        parsed.kuberos_url,
        token,
        poll_interval_sec=1.0,
    )
    deploy_manifests(paths, adapter, parsed.timeout_sec)
