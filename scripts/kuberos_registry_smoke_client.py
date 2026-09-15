#!/usr/bin/env python3
"""Create or delete the temporary private-registry KubeROS deployment."""

import argparse
import json
import os
import sys
from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(
    0, str(ROOT / "sources/cloud_native_application_manager")
)

from cloud_native_application_manager.kuberos_adapter import (  # noqa: E402
    KuberosAdapter,
)


def load_manifest(path):
    with Path(path).open(encoding="utf-8") as stream:
        manifest = yaml.safe_load(stream)
    if not isinstance(manifest, dict):
        raise ValueError("manifest must contain one YAML object")
    return manifest


def main(args=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("operation", choices=("create", "delete"))
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--kuberos-url", default="http://127.0.0.1:18080")
    parser.add_argument("--timeout-sec", type=float, default=180.0)
    parsed = parser.parse_args(args)

    token = os.environ.get("KUBEROS_API_TOKEN", "")
    if not token:
        raise RuntimeError("KUBEROS_API_TOKEN is required")
    manifest = load_manifest(parsed.manifest)
    deployment_id = manifest["metadata"]["name"]
    correlation_id = f"registry-smoke-{parsed.operation}"
    adapter = KuberosAdapter(
        parsed.kuberos_url,
        token,
        poll_interval_sec=1.0,
    )

    if parsed.operation == "create":
        operation = adapter.create_deployment(manifest, correlation_id)
        state = adapter.wait_ready(
            deployment_id,
            timeout_sec=parsed.timeout_sec,
            correlation_id=correlation_id,
        )
        result = {
            "operation": "create",
            "operation_id": operation.operation_id,
            "deployment_id": deployment_id,
            "status": str(state.get("status", "")).lower(),
            "revision": state.get("revision"),
        }
    else:
        operation = adapter.delete_deployment(deployment_id, correlation_id)
        result = {
            "operation": "delete",
            "operation_id": operation.operation_id,
            "deployment_id": deployment_id,
            "status": operation.state,
        }
    print(json.dumps(result, sort_keys=True))


if __name__ == "__main__":
    main()
