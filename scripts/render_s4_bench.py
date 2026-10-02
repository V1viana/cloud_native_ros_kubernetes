#!/usr/bin/env python3
"""Render the S4 bench (R13, docs/R13_S4_CONTRACT_DRAFT.md): four onboard robots
(drone01-04), one edge node, one control-plane node, in both variants.

  drone01  battery fault (BatteryLowRule; harness as the old S4, unchanged)
  drone02  telemetry fault (TelemetryHeartbeatRule in A; RestartComponent in B)
  drone03  SLO monitoring with a WORKING edge migration: prepared on the edge
           before T0 by the P2 migration, so stopping the edge node is an
           observable fault (not the old deliberately broken edge profile)
  drone04  sentinel, no fault, the same SLO monitoring as drone03

Reuses the E0/S3/S4 renderers' building blocks without modifying them (their
hashes belong to recorded runs). Deliberately NOT reused: S3's startup-probe
loosening, meant for N up to 50; at N=4 the E0 probes stay as they are (the old
S4 bench, three drones, ran with them).

Variant A's migration template is robot-specific: the Application Manager's
manifest factory rewrites only metadata.name/targetRobots, not the entrypoint
(cloud_native_application_manager/node.py), so it is rendered for drone03. An
SLO action on drone04 in A would start an instance with drone03's parameters:
still a collateral action on the sentinel, and recorded as such.

Cluster `cloud-native-s4` (own API port 6553), namespace `cloud-native-p2` so
that the p2/s4 manifests apply unchanged. Tested offline:
operator/tests/test_s4_bench.py.
"""

import argparse
from copy import deepcopy
from pathlib import Path

import yaml

from render_e0_manifests import TEMPLATE, load_documents, render_robot_manifest, write_documents
from render_s3_imperative_manifests import KUBEROS_BASE, build_robots, render_kuberos_runtime
from render_s3_k3d_config import render as render_k3d
from render_s4_manifests import BATTERY_RULE_PARAMS, TELEMETRY_RULE_PARAMS

ROOT = Path(__file__).resolve().parents[1]
P2_EDGE_TEMPLATE = ROOT / "manifests" / "kuberos" / "p2" / "analytics-edge.yaml"
S4_HARNESS = ROOT / "manifests" / "kubernetes" / "s4" / "40-battery-fault-harness-drone01-imperative.yaml"
S4_BATTERY_B = ROOT / "manifests" / "kubernetes" / "s4" / "80-battery-fault-drone01.yaml"
HARNESS_NAME = "s4-battery-fault-harness-drone01"

N_ROBOTS = 4
CLUSTER = "cloud-native-s4"
NAMESPACE = "cloud-native-p2"
API_PORT = "6553"
SLO_RULE = "px4_event_detector_plugin::AnalyticsLatencySloRule"
BATTERY_RULE = "px4_event_detector_plugin::BatteryLowRule"
TELEMETRY_RULE = "px4_event_detector_plugin::TelemetryHeartbeatRule"
ROLES = {"drone01": "battery", "drone02": "telemetry", "drone03": "edge-migrated", "drone04": "sentinel"}
RULE_OVERRIDES = {"drone01": (BATTERY_RULE, BATTERY_RULE_PARAMS),
                  "drone02": (TELEMETRY_RULE, TELEMETRY_RULE_PARAMS)}
EDGE_ROBOT = "drone03"


def k3d_config():
    text = render_k3d(N_ROBOTS, CLUSTER)
    if text.count('hostPort: "6552"') != 1:
        raise ValueError("the S3 k3d renderer changed its API port line")
    return text.replace('hostPort: "6552"', f'hostPort: "{API_PORT}"')


# ---- variant A -----------------------------------------------------------

def _override_rule(manifest, robot_id):
    plugin, rule_params = RULE_OVERRIDES[robot_id]
    entry = next(e for e in manifest["rosParamMap"] if e["name"] == f"event-detector-{robot_id}.yaml")
    parsed = yaml.safe_load(entry["data"])
    params = parsed[f"/{robot_id}/event_detector"]["ros__parameters"]
    params["rules"] = [plugin]
    params["rule_params"] = {plugin: rule_params}
    entry["data"] = yaml.safe_dump(parsed, sort_keys=False)


def imperative_robots(discovery_address):
    template = load_documents(TEMPLATE)[0]
    out = {}
    for robot in build_robots(N_ROBOTS, CLUSTER):
        manifest = render_robot_manifest(deepcopy(template), robot, discovery_address)
        if robot["id"] in RULE_OVERRIDES:
            _override_rule(manifest, robot["id"])
        out[robot["id"]] = manifest
    return out


def imperative_kuberos():
    robots = build_robots(N_ROBOTS, CLUSTER)
    return render_kuberos_runtime(load_documents(KUBEROS_BASE), robots, f"k3d-{CLUSTER}-agent-{N_ROBOTS}",
                                  NAMESPACE, CLUSTER, f"k3d-{CLUSTER}-server-0")


def imperative_edge_configmap(discovery_address):
    """P2's working edge ApplicationDeployment, for drone03, in the ConfigMap the
    S4 Application Manager mounts (s4/30-imperative-control-plane.yaml)."""
    text = P2_EDGE_TEMPLATE.read_text()
    if "drone01" not in text or "__P2_DISCOVERY_ADDRESS__" not in text:
        raise ValueError("the P2 edge template changed shape")
    text = text.replace("drone01", EDGE_ROBOT).replace("__P2_DISCOVERY_ADDRESS__", discovery_address)
    return {"apiVersion": "v1", "kind": "ConfigMap",
            "metadata": {"name": f"s4-analytics-edge-{EDGE_ROBOT}-manifest", "namespace": NAMESPACE},
            "data": {f"analytics-edge-{EDGE_ROBOT}.yaml": text}}


# ---- the battery fault (both variants) ------------------------------------

def battery_harness(discovery_address, start_at_utc):
    """The old S4 harness, identical in A and B, firing at the absolute UTC T0
    (additive start_at_utc; the relative startup_delay_sec left at E1's 15 s is then
    unused). Applied per cell: it IS the battery fault."""
    if start_at_utc <= 0:
        raise ValueError("the battery fault needs an absolute T0")
    text = S4_HARNESS.read_text()
    if text.count("-p startup_delay_sec:=__S4_FAULT_START_DELAY_SEC__ \\") != 1:
        raise ValueError("the S4 harness manifest changed shape")
    text = text.replace("-p startup_delay_sec:=__S4_FAULT_START_DELAY_SEC__ \\",
                        f"-p startup_delay_sec:=15.0 \\\n                -p start_at_utc:={start_at_utc:.3f} \\")
    return text.replace("__P2_DISCOVERY_ADDRESS__", discovery_address)


def declarative_battery_detector(discovery_address):
    """B's BatteryLowRule detector for drone01 (the old S4 file without its harness):
    part of the bench, as the rule inside drone01's ApplicationDeployment is in A."""
    docs = [d for d in yaml.safe_load_all(S4_BATTERY_B.read_text().replace("__P2_DISCOVERY_ADDRESS__",
                                                                             discovery_address)) if d]
    kept = [d for d in docs if d["metadata"]["name"] != HARNESS_NAME]
    if len(kept) != len(docs) - 1:
        raise ValueError("the S4 battery file changed shape")
    return kept


def fault_observer(discovery_address):
    return (ROOT / "manifests" / "kubernetes" / "s4" / "26-s4-fault-observer.yaml").read_text().replace(
        "__P2_DISCOVERY_ADDRESS__", discovery_address)


# ---- variant B -----------------------------------------------------------

def declarative_shared_infra(discovery_address):
    from render_s3_declarative_manifests import shared_infra_yaml
    return shared_infra_yaml(N_ROBOTS, NAMESPACE, discovery_address)


def declarative_rosmodules():
    """E0's own ROSModule values, four robots, no probe override."""
    docs = []
    for i in range(N_ROBOTS):
        rid = f"drone{i + 1:02d}"
        docs.append({"apiVersion": "dronekube.io/v1alpha1", "kind": "ROSModule",
                     "metadata": {"name": f"companion-analytics-{rid}", "namespace": NAMESPACE,
                                  "labels": {"app": "companion-analytics", "robot": rid}},
                     "spec": {"robotId": rid, "package": "companion_analytics", "lifecycleTarget": "Active",
                              "placement": "onboard",
                              "rosParamMap": {"processing_delay_ms": "80.0", "queue_depth": "4",
                                              "cpu_percent": "25.0", "sample_period_ms": "250"}}})
    return docs


def declarative_robotfleet():
    return {"apiVersion": "dronekube.io/v1alpha1", "kind": "RobotFleet",
            "metadata": {"name": "px4-fleet", "namespace": NAMESPACE},
            "spec": {"robots": [{"id": f"drone{i + 1:02d}", "role": "onboard"} for i in range(N_ROBOTS)],
                     "edgeNodeSelector": {"kuberos.io/role": "edge"}}}


def _slo_policy(robot_id):
    """P2/S3's working migration (s3/70-declarative-adaptationpolicy.yaml)."""
    return {"apiVersion": "dronekube.io/v1alpha1", "kind": "AdaptationPolicy",
            "metadata": {"name": f"analytics-latency-slo-s4-{robot_id}", "namespace": NAMESPACE},
            "spec": {"targetModuleSelector": {"matchLabels": {"app": "companion-analytics", "robot": robot_id}},
                     "trigger": {"metric": "latency_p95_ms", "threshold": 250, "consecutiveWindows": 3,
                                 "windowSec": 2},
                     "action": {"type": "MigratePlacement", "to": "edge",
                                "edgeRosParamMap": {"processing_delay_ms": "80.0"}},
                     "recovery": {"metric": "latency_p95_ms", "threshold": 150, "consecutiveWindows": 3},
                     "rollback": {"onReadinessFailureSec": 60}}}


def declarative_policies():
    """Telemetry on drone02 (the old S4's, unchanged); SLO with a working edge on
    drone03 and, identical, on the sentinel drone04."""
    telemetry = {"apiVersion": "dronekube.io/v1alpha1", "kind": "AdaptationPolicy",
                 "metadata": {"name": "telemetry-heartbeat-s4-drone02", "namespace": NAMESPACE},
                 "spec": {"targetModuleSelector": {"matchLabels": {"app": "companion-analytics", "robot": "drone02"}},
                          "trigger": {"metric": "telemetry_heartbeat_age_sec", "threshold": 3.0,
                                      "consecutiveWindows": 1, "windowSec": 5},
                          "action": {"type": "RestartComponent", "targetDeploymentSelector": {
                              "matchLabels": {"app.kubernetes.io/name": "drone02-microxrce-agent"}}},
                          "recovery": {"metric": "telemetry_heartbeat_age_sec", "threshold": 1.0,
                                       "consecutiveWindows": 3},
                          "rollback": {"onReadinessFailureSec": 90}}}
    return [telemetry, _slo_policy("drone03"), _slo_policy("drone04")]


def _dump_all(docs):
    return "---\n".join(yaml.safe_dump(d, sort_keys=False) for d in docs)


def render(output_dir, variant, discovery_address):
    out = Path(output_dir)
    out.mkdir(parents=True, exist_ok=True)
    written = [out / "k3d-config.yaml", out / "26-s4-fault-observer.yaml"]
    written[0].write_text(k3d_config())
    written[1].write_text(fault_observer(discovery_address))
    if variant == "a":
        manifests = out / "kuberos-manifests"
        manifests.mkdir(exist_ok=True)
        for robot_id, manifest in imperative_robots(discovery_address).items():
            path = manifests / f"{robot_id}.yaml"
            write_documents(path, [manifest])
            written.append(path)
        path = manifests / "20-kuberos.yaml"
        write_documents(path, imperative_kuberos())
        written.append(path)
        path = out / "20-analytics-edge-drone03.yaml"
        path.write_text(yaml.safe_dump(imperative_edge_configmap(discovery_address), sort_keys=False))
        written.append(path)
    elif variant == "b":
        for name, text in (("40-shared-infra.yaml", declarative_shared_infra(discovery_address)),
                           ("60-declarative-workload.yaml", _dump_all(declarative_rosmodules())),
                           ("70-robotfleet.yaml", yaml.safe_dump(declarative_robotfleet(), sort_keys=False)),
                           ("70-adaptationpolicies.yaml", _dump_all(declarative_policies())),
                           ("80-battery-detector.yaml", _dump_all(declarative_battery_detector(discovery_address)))):
            (out / name).write_text(text)
            written.append(out / name)
    else:
        raise ValueError(f"unknown variant {variant}")
    return written


def main(args=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--variant", required=True, choices=("a", "b"))
    parser.add_argument("--discovery-address", required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parsed = parser.parse_args(args)
    for path in render(parsed.output_dir, parsed.variant, parsed.discovery_address):
        print(path)


if __name__ == "__main__":
    main()
