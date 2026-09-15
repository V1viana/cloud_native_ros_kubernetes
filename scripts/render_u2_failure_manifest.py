#!/usr/bin/env python3
"""Render a U2 invalid-image update from the verified U1 revision."""

import argparse
from copy import deepcopy
from pathlib import Path

import yaml


MODULE_NAME = "companion-analytics-onboard"
INVALID_IMAGE = "cloud-native-ros/companion-analytics:u2-image-does-not-exist"


def render_failure_update(manifest, invalid_image=INVALID_IMAGE):
    updated = deepcopy(manifest)
    metadata = updated.get("metadata")
    if not isinstance(metadata, dict) or metadata.get("targetRobots") != ["drone01"]:
        raise ValueError("U2 must target only drone01")
    modules = updated.get("rosModules")
    if not isinstance(modules, list):
        raise ValueError("ApplicationDeployment rosModules must be a list")
    matches = [item for item in modules if item.get("name") == MODULE_NAME]
    if len(matches) != 1:
        raise ValueError(f"Expected exactly one {MODULE_NAME} module")
    if not matches[0].get("image"):
        raise ValueError(f"{MODULE_NAME} image is required")
    matches[0]["image"] = invalid_image
    metadata["appVersion"] = "u2-invalid-image-v3"
    return updated


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    manifest = yaml.safe_load(args.input.read_text(encoding="utf-8"))
    rendered = render_failure_update(manifest)
    args.output.write_text(
        yaml.safe_dump(rendered, sort_keys=False), encoding="utf-8"
    )
    print(args.output)


if __name__ == "__main__":
    main()
