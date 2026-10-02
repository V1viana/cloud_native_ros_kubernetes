"""Diagnostic Job entrypoint for RestartComponent (P1-equivalent).

Polls the Pod and Event API for a bounded window and prints JSON snapshots
to stdout -- forensic evidence collected the same shape as variant A's own
diagnostic Job (Pod + Event API, periodic snapshots), just run from the
fleet-operator's own image (already has the kubernetes client installed,
nothing ROS-specific needed) instead of a dedicated one. Invoked as
`python3 -m fleet_operator.diagnostic_job`, not imported -- this module has
no importable API of its own, only a main().
"""

import json
import os
import time
from datetime import datetime, timezone

from kubernetes import client, config


def _now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _snapshot(core, namespace, label_selector):
    pods = core.list_namespaced_pod(namespace, label_selector=label_selector or None)
    events = core.list_namespaced_event(namespace)
    return {
        "timestamp": _now_iso(),
        "pods": [
            {
                "name": pod.metadata.name,
                "uid": pod.metadata.uid,
                "phase": pod.status.phase,
                "restart_count": (
                    pod.status.container_statuses[0].restart_count
                    if pod.status.container_statuses
                    else None
                ),
            }
            for pod in pods.items
        ],
        "events": [
            {
                "reason": event.reason,
                "message": event.message,
                "involved_object": event.involved_object.name,
            }
            for event in events.items
        ],
    }


def main():
    namespace = os.environ["DIAGNOSTIC_NAMESPACE"]
    label_selector = os.environ.get("DIAGNOSTIC_LABEL_SELECTOR", "")
    duration_sec = float(os.environ.get("DIAGNOSTIC_DURATION_SEC", "90"))
    interval_sec = float(os.environ.get("DIAGNOSTIC_INTERVAL_SEC", "10"))

    config.load_incluster_config()
    core = client.CoreV1Api()

    deadline = time.monotonic() + duration_sec
    while True:
        print(json.dumps(_snapshot(core, namespace, label_selector)), flush=True)
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        time.sleep(min(interval_sec, remaining))


if __name__ == "__main__":
    main()
