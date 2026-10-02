#!/usr/bin/env python3
"""Render S4 variant A's three KubeROS ApplicationDeployment manifests.

Reuses E0's own per-robot template and renderer verbatim (render_e0_manifests
module, untouched) -- S4 variant A's three drones sit on the very same
E0 topology/placement, just with each drone's single event-detector rule
swapped for the fault it exercises concurrently, instead of E0's own uniform
AnalyticsLatencySloRule:
  - drone01: BatteryLowRule (P0, same topics as manifests/kubernetes/s4/
    80-battery-fault-drone01.yaml's variant-B harness, reused as-is here too
    since the harness itself is a plain ROS 2 node, not K8s-API-aware).
  - drone02: TelemetryHeartbeatRule (P1), namespaced topics -- same plugin
    E2's own variant A used, just retargeted from /fmu/... to /drone02/fmu/...
  - drone03: kept at E0's own default AnalyticsLatencySloRule, but its
    onboard companion-analytics processing_delay_ms is raised above the
    250ms violation threshold from boot, mirroring how P2/E4's own variant A
    already starts the onboard instance already-slow rather than injecting
    the SLO breach at runtime (there is no live-patch path for a
    KubeROS-managed rosModule's own entrypoint args).
"""

import argparse
from copy import deepcopy
from pathlib import Path

import yaml

from render_e0_manifests import (
    ROBOTS,
    load_documents,
    render_kuberos_runtime,
    render_robot_manifest,
    write_documents,
    KUBEROS_BASE,
    TEMPLATE,
)

ONBOARD_PROCESSING_DELAY_MS = "300.0"

BATTERY_RULE_PARAMS = {
    "enabled": True,
    "period": 0.1,
    "parameters": {
        "robot_id": "drone01",
        "battery_topic": "/drone01/s4/fault/battery_status",
        "command_topic": "/drone01/fmu/in/vehicle_command",
        "ack_topic": "/drone01/fmu/out/vehicle_command_ack_v1",
        "vehicle_status_topic": "/drone01/fmu/out/vehicle_status_v4",
        "event_topic": "/fleet/operational_events",
        "threshold": 0.20,
        "reset_threshold": 0.25,
        "consecutive_samples": 3,
        "max_message_age_sec": 2.0,
        "max_attempts": 3,
        "retry_timeout_sec": 2.0,
        "target_system": 1,
        "target_component": 1,
        "source_system": 1,
        "source_component": 1,
    },
}

TELEMETRY_RULE_PARAMS = {
    "enabled": True,
    "period": 0.2,
    "parameters": {
        "robot_id": "drone02",
        "vehicle_status_topic": "/drone02/fmu/out/vehicle_status_v4",
        "event_topic": "/fleet/operational_events",
        "startup_grace_sec": 2.0,
        "timeout_sec": 3.0,
        "recovery_samples": 3,
        "recovery_window_sec": 2.0,
        "recovery_stability_sec": 3.0,
        "cooldown_sec": 5.0,
    },
}

RULE_OVERRIDES = {
    "drone01": ("px4_event_detector_plugin::BatteryLowRule", BATTERY_RULE_PARAMS),
    "drone02": ("px4_event_detector_plugin::TelemetryHeartbeatRule", TELEMETRY_RULE_PARAMS),
}


def _event_detector_entry(manifest, robot_id):
    entries = manifest["rosParamMap"]
    name = f"event-detector-{robot_id}.yaml"
    return next(entry for entry in entries if entry["name"] == name)


def _override_event_detector_rule(manifest, robot_id):
    plugin, rule_params = RULE_OVERRIDES[robot_id]
    entry = _event_detector_entry(manifest, robot_id)
    parsed = yaml.safe_load(entry["data"])
    node_key = f"/{robot_id}/event_detector"
    params = parsed[node_key]["ros__parameters"]
    params["rules"] = [plugin]
    params["rule_params"] = {plugin: rule_params}
    entry["data"] = yaml.safe_dump(parsed, sort_keys=False)


def _raise_onboard_processing_delay(manifest, robot_id):
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
    raise ValueError(f"companion-analytics-onboard rosModule not found for {robot_id}")


def render(output_dir, discovery_address):
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    template = load_documents(TEMPLATE)[0]
    paths = []
    for robot in ROBOTS:
        robot_id = robot["id"]
        manifest = render_robot_manifest(deepcopy(template), robot, discovery_address)
        if robot_id in RULE_OVERRIDES:
            _override_event_detector_rule(manifest, robot_id)
        if robot_id == "drone03":
            _raise_onboard_processing_delay(manifest, robot_id)
        path = output_dir / f"{robot_id}.yaml"
        write_documents(path, [manifest])
        paths.append(path)
    kuberos_path = output_dir / "20-kuberos.yaml"
    write_documents(kuberos_path, render_kuberos_runtime(load_documents(KUBEROS_BASE)))
    return paths, kuberos_path


def main(args=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--discovery-address", required=True)
    parsed = parser.parse_args(args)
    robot_paths, kuberos_path = render(parsed.output_dir, parsed.discovery_address)
    for path in (*robot_paths, kuberos_path):
        print(path)


if __name__ == "__main__":
    main()
