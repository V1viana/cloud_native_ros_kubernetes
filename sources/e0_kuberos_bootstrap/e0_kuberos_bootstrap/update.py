"""Apply and verify one differential KubeROS deployment update."""

import argparse
import json
import os
import time
from pathlib import Path

from cloud_native_application_manager.kuberos_adapter import (
    KuberosAdapter,
    KuberosError,
)

from .bootstrap import load_manifest


def apply_manifest_update(
    path,
    adapter,
    timeout_sec,
    clock=time.monotonic,
    emit=print,
):
    manifest, robot_id = load_manifest(path)
    deployment_id = manifest["metadata"]["name"]
    correlation_id = f"u1-update-{robot_id}"

    previous = adapter.get_deployment(deployment_id, correlation_id)
    previous_status = str(previous.get("status", "")).lower()
    if previous_status != "running":
        raise KuberosError(
            f"Deployment '{deployment_id}' is not running before update"
        )
    previous_revision = int(previous.get("revision", 0))

    started = clock()
    operation = adapter.update_deployment(manifest, correlation_id)
    if operation.target_revision != previous_revision + 1:
        raise KuberosError(
            f"Expected revision {previous_revision + 1}, "
            f"received {operation.target_revision}"
        )
    state = adapter.wait_revision(
        deployment_id,
        target_revision=operation.target_revision,
        timeout_sec=timeout_sec,
        correlation_id=correlation_id,
        event_id=operation.event_id,
    )
    event = next(
        (
            item for item in state.get("deployment_event_set", [])
            if str(item.get("uuid", "")) == operation.event_id
        ),
        None,
    )
    if not event or str(event.get("event_status", "")).lower() != "success":
        raise KuberosError(
            f"Update event '{operation.event_id}' has no successful outcome"
        )

    result = {
        "record_type": "u1_kuberos_update_ready",
        "robot_id": robot_id,
        "deployment_id": deployment_id,
        "event_id": operation.event_id,
        "previous_revision": previous_revision,
        "target_revision": operation.target_revision,
        "status": str(state.get("status", "")).lower(),
        "event_status": str(event["event_status"]).lower(),
        "elapsed_sec": round(clock() - started, 3),
    }
    emit("U1_KUBEROS_UPDATE_READY " + json.dumps(result, sort_keys=True))
    return result

def apply_manifest_update_expect_failure(
    path,
    adapter,
    timeout_sec,
    clock=time.monotonic,
    emit=print,
):
    manifest, robot_id = load_manifest(path)
    deployment_id = manifest["metadata"]["name"]
    correlation_id = f"u2-invalid-image-{robot_id}"
    previous = adapter.get_deployment(deployment_id, correlation_id)
    previous_revision = int(previous.get("revision", 0))
    if str(previous.get("status", "")).lower() != "running":
        raise KuberosError(f"Deployment '{deployment_id}' is not running")

    started = clock()
    operation = adapter.update_deployment(manifest, correlation_id)
    if operation.target_revision != previous_revision + 1:
        raise KuberosError("Unexpected target revision for failure update")
    try:
        adapter.wait_revision(
            deployment_id,
            operation.target_revision,
            timeout_sec,
            correlation_id,
            operation.event_id,
        )
    except KuberosError as failure:
        state = adapter.get_deployment(deployment_id, correlation_id)
        event = next(
            (
                item for item in state.get("deployment_event_set", [])
                if str(item.get("uuid", "")) == operation.event_id
            ),
            None,
        )
        if not event or str(event.get("event_status", "")).lower() != "failed":
            raise KuberosError("Failure update has no FAILED event") from failure
        if str(state.get("status", "")).lower() != "running":
            raise KuberosError("Deployment did not return to running") from failure
        if int(state.get("revision", 0)) != previous_revision:
            raise KuberosError("Failed update changed the active revision") from failure
        result = {
            "record_type": "u2_kuberos_rollback_ready",
            "robot_id": robot_id,
            "deployment_id": deployment_id,
            "event_id": operation.event_id,
            "preserved_revision": previous_revision,
            "rejected_revision": operation.target_revision,
            "status": "running",
            "event_status": "failed",
            "error_message": str(event.get("error_message", "")),
            "elapsed_sec": round(clock() - started, 3),
        }
        emit("U2_KUBEROS_ROLLBACK_READY " + json.dumps(result, sort_keys=True))
        return result
    raise KuberosError("Invalid-image update unexpectedly succeeded")



def main(args=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--kuberos-url", required=True)
    parser.add_argument("--timeout-sec", type=float, default=240.0)
    parser.add_argument("--expect-failure", action="store_true")
    parsed = parser.parse_args(args)

    token = os.environ.get("KUBEROS_API_TOKEN", "")
    if not token:
        raise RuntimeError("KUBEROS_API_TOKEN is required")
    adapter = KuberosAdapter(
        parsed.kuberos_url,
        token,
        poll_interval_sec=1.0,
    )
    if parsed.expect_failure:
        apply_manifest_update_expect_failure(
            parsed.manifest, adapter, parsed.timeout_sec
        )
    else:
        apply_manifest_update(parsed.manifest, adapter, parsed.timeout_sec)
