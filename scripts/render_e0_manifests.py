#!/usr/bin/env python3
"""Render the three E0 KubeROS requests and multi-robot KubeROS runtime."""

import argparse
import re
from copy import deepcopy
from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[1]
TEMPLATE = ROOT / "manifests" / "kuberos" / "e0" / "drone-baseline.template.yaml"
KUBEROS_BASE = ROOT / "manifests" / "kubernetes" / "p2" / "20-kuberos.yaml"

ROBOTS = (
    {"id": "drone01", "index": "1", "node": "k3d-cloud-native-p2-agent-0", "home_lat": "47.3977420", "home_lon": "8.5455940"},
    {"id": "drone02", "index": "2", "node": "k3d-cloud-native-p2-agent-1", "home_lat": "47.3977420", "home_lon": "8.5456072"},
    {"id": "drone03", "index": "3", "node": "k3d-cloud-native-p2-agent-2", "home_lat": "47.3986420", "home_lon": "8.5455940"},
)


def replace_strings(value, replacements):
    if isinstance(value, str):
        for old, new in replacements.items():
            value = value.replace(old, new)
        return value
    if isinstance(value, list):
        return [replace_strings(item, replacements) for item in value]
    if isinstance(value, dict):
        return {
            replace_strings(key, replacements): replace_strings(item, replacements)
            for key, item in value.items()
        }
    return value


def render_robot_manifest(template, robot, discovery_address):
    robot_id = robot["id"]
    replacements = {
        "__ROBOT_ID__": robot_id,
        "__ROBOT_INDEX__": robot["index"],
        "__AGENT_SERVICE_ENV__": f"{robot_id.upper()}_XRCE_AGENT_SERVICE_HOST",
        "__HOME_LAT__": robot["home_lat"],
        "__HOME_LON__": robot["home_lon"],
        "__DISCOVERY_ADDRESS__": discovery_address,
    }
    rendered = replace_strings(deepcopy(template), replacements)
    if re.search(r"__[A-Z][A-Z0-9_]+__", yaml.safe_dump(rendered, sort_keys=False)):
        raise ValueError(f"Unresolved placeholder in manifest for {robot_id}")
    return rendered


def set_env(container, name, value):
    env = container.setdefault("env", [])
    item = next((entry for entry in env if entry.get("name") == name), None)
    if item is None:
        env.append({"name": name, "value": value})
    else:
        item.clear()
        item.update({"name": name, "value": value})


def render_kuberos_runtime(documents):
    rendered = deepcopy(documents)
    deployment = next(
        item for item in rendered
        if item.get("kind") == "Deployment"
        and item.get("metadata", {}).get("name") == "kuberos"
    )
    pod_spec = deployment["spec"]["template"]["spec"]
    containers = pod_spec.get("initContainers", []) + pod_spec.get("containers", [])
    robot_ids = ",".join(robot["id"] for robot in ROBOTS)
    onboard_nodes = ",".join(robot["node"] for robot in ROBOTS)
    for container in containers:
        set_env(container, "KUBEROS_ROBOT_ID", robot_ids)
        set_env(container, "KUBEROS_ONBOARD_NODE", onboard_nodes)
        set_env(container, "KUBEROS_EDGE_NODE", "k3d-cloud-native-p2-agent-3")
    return rendered


def load_documents(path):
    with Path(path).open(encoding="utf-8") as stream:
        return [item for item in yaml.safe_load_all(stream) if item]


def write_documents(path, documents):
    with Path(path).open("w", encoding="utf-8") as stream:
        yaml.safe_dump_all(documents, stream, sort_keys=False, explicit_start=True)


def render(output_dir, discovery_address):
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    template = load_documents(TEMPLATE)[0]
    paths = []
    for robot in ROBOTS:
        path = output_dir / f"{robot['id']}.yaml"
        write_documents(path, [render_robot_manifest(template, robot, discovery_address)])
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
