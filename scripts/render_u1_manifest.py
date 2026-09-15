#!/usr/bin/env python3
"""Render the U1 revision by changing only drone01 onboard analytics."""

import argparse
from copy import deepcopy
from pathlib import Path

import yaml


MODULE_NAME = "companion-analytics-onboard"
OLD_LATENCY = "processing_delay_ms:=80.0"


def render_update(manifest, processing_delay_ms=95.0):
    updated = deepcopy(manifest)
    metadata = updated.get("metadata")
    if not isinstance(metadata, dict) or not metadata.get("name"):
        raise ValueError("ApplicationDeployment metadata.name is required")
    robots = metadata.get("targetRobots")
    if robots != ["drone01"]:
        raise ValueError("U1 must target only drone01")

    modules = updated.get("rosModules")
    if not isinstance(modules, list):
        raise ValueError("ApplicationDeployment rosModules must be a list")
    matches = [item for item in modules if item.get("name") == MODULE_NAME]
    if len(matches) != 1:
        raise ValueError(f"Expected exactly one {MODULE_NAME} module")
    entrypoints = matches[0].get("entrypoint")
    if not isinstance(entrypoints, list) or len(entrypoints) != 1:
        raise ValueError(f"{MODULE_NAME} must expose exactly one entrypoint")
    if entrypoints[0].count(OLD_LATENCY) != 1:
        raise ValueError(f"Expected exactly one '{OLD_LATENCY}' token")

    new_latency = f"processing_delay_ms:={float(processing_delay_ms):.1f}"
    entrypoints[0] = entrypoints[0].replace(OLD_LATENCY, new_latency)
    metadata["appVersion"] = "u1-differential-update-v2"
    return updated


def load_manifest(path):
    with Path(path).open(encoding="utf-8") as stream:
        manifest = yaml.safe_load(stream)
    if not isinstance(manifest, dict):
        raise ValueError(f"{path} must contain one YAML object")
    return manifest


def write_manifest(path, manifest):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as stream:
        yaml.safe_dump(manifest, stream, sort_keys=False)


def main(args=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--processing-delay-ms", type=float, default=95.0)
    parsed = parser.parse_args(args)
    updated = render_update(
        load_manifest(parsed.input),
        processing_delay_ms=parsed.processing_delay_ms,
    )
    write_manifest(parsed.output, updated)
    print(parsed.output)


if __name__ == "__main__":
    main()
