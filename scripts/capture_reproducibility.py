#!/usr/bin/env python3
"""Capture immutable image and tool metadata for an experiment campaign."""

import argparse
import json
import subprocess
from datetime import datetime, timezone
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_IMAGES = (
    "cloud-native-ros/control-plane:p2",
    "cloud-native-ros/control-plane:e2",
    "cloud-native-ros/event-detector:p2",
    "cloud-native-ros/event-detector:e2-upstream",
    "cloud-native-ros/kuberos:p2",
    "microros/micro-ros-agent:humble",
    "px4io/px4-sitl:latest",
    "redis:7",
    "ros:humble-ros-base",
)


def run_command(command, cwd=PROJECT_ROOT):
    return subprocess.run(
        command,
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def inspect_image(reference, command_runner=run_command):
    raw = command_runner(["docker", "image", "inspect", reference])
    details = json.loads(raw)[0]
    repo_digests = sorted(details.get("RepoDigests") or [])
    return {
        "reference": reference,
        "image_id": details["Id"],
        "repo_digests": repo_digests,
        "immutable_reference": repo_digests[0] if repo_digests else details["Id"],
        "created": details.get("Created"),
        "architecture": details.get("Architecture"),
        "os": details.get("Os"),
        "size_bytes": details.get("Size"),
    }


def optional_command(command, command_runner=run_command):
    try:
        return command_runner(command)
    except (subprocess.CalledProcessError, FileNotFoundError):
        return None


def build_manifest(
    campaign_dir,
    images=DEFAULT_IMAGES,
    campaign_source_commit=None,
    validation_commit=None,
    artifact_id=None,
    command_runner=run_command,
):
    campaign_dir = Path(campaign_dir)
    if artifact_id:
        campaign_id = artifact_id
    else:
        campaign_file = campaign_dir / "campaign.json"
        campaign = json.loads(campaign_file.read_text(encoding="utf-8"))
        campaign_id = campaign["campaign_id"]
    current_commit = command_runner(["git", "rev-parse", "HEAD"])
    source_commit = campaign_source_commit or current_commit
    validated_at = validation_commit or current_commit
    dirty = bool(
        command_runner(
            ["git", "status", "--porcelain", "--untracked-files=no"]
        )
    )

    return {
        "schema_version": 1,
        "campaign_id": campaign_id,
        "captured_at": datetime.now(timezone.utc).isoformat(),
        "repository": {
            "campaign_source_commit": source_commit,
            "validation_commit": validated_at,
            "capture_commit": current_commit,
            "dirty_at_capture": dirty,
            "submodules": optional_command(
                ["git", "submodule", "status", "--recursive"], command_runner
            ),
        },
        "tools": {
            "docker": optional_command(
                ["docker", "version", "--format", "{{json .Client.Version}} {{json .Server.Version}}"],
                command_runner,
            ),
            "k3d": optional_command(["k3d", "version"], command_runner),
            "kubectl": optional_command(
                ["kubectl", "version", "--client", "-o", "json"],
                command_runner,
            ),
        },
        "images": [inspect_image(image, command_runner) for image in images],
        "notes": [
            "repo_digests are registry digests when available",
            "image_id is the content-addressed local image identifier",
            "locally built images without repo_digests are pinned by image_id",
        ],
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--campaign-dir", required=True)
    parser.add_argument("--output")
    parser.add_argument("--image", action="append", dest="images")
    parser.add_argument("--campaign-source-commit")
    parser.add_argument("--validation-commit")
    parser.add_argument("--artifact-id")
    args = parser.parse_args()

    campaign_dir = Path(args.campaign_dir)
    output = Path(args.output) if args.output else campaign_dir / "reproducibility.json"
    manifest = build_manifest(
        campaign_dir,
        images=args.images or DEFAULT_IMAGES,
        campaign_source_commit=args.campaign_source_commit,
        validation_commit=args.validation_commit,
        artifact_id=args.artifact_id,
    )
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(f"Wrote reproducibility manifest: {output}")


if __name__ == "__main__":
    main()
