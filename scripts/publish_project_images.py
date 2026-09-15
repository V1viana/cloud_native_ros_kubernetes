#!/usr/bin/env python3
"""Build, publish, and lock project images without handling credentials."""

import argparse
import json
import re
import shlex
import subprocess
from datetime import datetime, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CATALOG = ROOT / "config/project_images.json"
TAG_PATTERN = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}$")


def run_command(command, cwd=ROOT):
    return subprocess.run(
        command,
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def validate_repository(value):
    prefix = value.strip().rstrip("/")
    if not prefix or "://" in prefix or "@" in prefix or any(
        char.isspace() for char in prefix
    ):
        raise ValueError(
            "repository reference must look like docker.io/user/cloud-native-ros"
        )
    if "/" not in prefix:
        raise ValueError("repository reference must include a namespace")
    return prefix


def validate_release(value):
    if not TAG_PATTERN.fullmatch(value):
        raise ValueError("release is not a valid OCI tag component")
    return value


def load_catalog(path=DEFAULT_CATALOG):
    catalog = json.loads(Path(path).read_text(encoding="utf-8"))
    if catalog.get("schema_version") != 1:
        raise ValueError("unsupported image catalog schema")
    images = catalog.get("images") or []
    if not images:
        raise ValueError("image catalog is empty")

    names = [image["name"] for image in images]
    aliases = [alias for image in images for alias in image["aliases"]]
    if len(names) != len(set(names)):
        raise ValueError("image names must be unique")
    if len(aliases) != len(set(aliases)):
        raise ValueError("image aliases must be unique")
    return images


def default_release(command_runner=run_command):
    commit = command_runner(["git", "rev-parse", "--short=12", "HEAD"])
    return f"project-{commit}"


def build_plan(images, repository, release, build=False):
    repository = validate_repository(repository)
    release = validate_release(release)
    plan = []
    for image in images:
        destination = f"{repository}:{image['name']}-{release}"
        commands = []
        if build:
            commands.append(
                [
                    "docker",
                    "build",
                    "-t",
                    image["source_reference"],
                    "-f",
                    image["dockerfile"],
                    image["context"],
                ]
            )
        commands.extend(
            [
                ["docker", "tag", image["source_reference"], destination],
                ["docker", "push", destination],
            ]
        )
        plan.append({**image, "published_tag": destination, "commands": commands})
    return plan


def inspect_image(reference, command_runner=run_command):
    raw = command_runner(["docker", "image", "inspect", reference])
    return json.loads(raw)[0]


def registry_digest(reference, details):
    repository = reference.rsplit(":", 1)[0]
    candidates = {repository}
    for registry in ("docker.io/", "index.docker.io/"):
        if repository.startswith(registry):
            candidates.add(repository[len(registry):])
    matches = [
        digest
        for digest in details.get("RepoDigests") or []
        if digest.rsplit("@", 1)[0] in candidates
    ]
    if len(matches) != 1:
        raise RuntimeError(
            f"registry did not return one immutable digest for {reference}"
        )
    return matches[0]


def publish(plan, release, repository, command_runner=run_command):
    records = []
    for image in plan:
        build_commands = [
            command
            for command in image["commands"]
            if command[:2] == ["docker", "build"]
        ]
        publish_commands = [
            command
            for command in image["commands"]
            if command[:2] != ["docker", "build"]
        ]
        for command in build_commands:
            command_runner(command)
        source = inspect_image(image["source_reference"], command_runner)
        for command in publish_commands:
            command_runner(command)
        published = inspect_image(image["published_tag"], command_runner)
        records.append(
            {
                "name": image["name"],
                "source_reference": image["source_reference"],
                "source_image_id": source["Id"],
                "aliases": image["aliases"],
                "published_tag": image["published_tag"],
                "immutable_reference": registry_digest(
                    image["published_tag"], published
                ),
            }
        )

    return {
        "schema_version": 1,
        "release": release,
        "repository": repository,
        "published_at": datetime.now(timezone.utc).isoformat(),
        "repository_commit": command_runner(["git", "rev-parse", "HEAD"]),
        "images": records,
    }


def write_lock(lock, destination):
    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_suffix(destination.suffix + ".tmp")
    temporary.write_text(
        json.dumps(lock, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    temporary.replace(destination)


def print_plan(plan):
    for image in plan:
        print(f"[{image['name']}] {image['source_reference']} -> {image['published_tag']}")
        for command in image["commands"]:
            print(f"  {shlex.join(command)}")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--repository", required=True)
    parser.add_argument("--release")
    parser.add_argument("--catalog", type=Path, default=DEFAULT_CATALOG)
    parser.add_argument("--lock-file", type=Path)
    parser.add_argument("--build", action="store_true")
    parser.add_argument(
        "--push",
        action="store_true",
        help="execute Docker commands; without this flag only print the plan",
    )
    args = parser.parse_args()

    prefix = validate_repository(args.repository)
    release = validate_release(args.release or default_release())
    images = load_catalog(args.catalog)
    plan = build_plan(images, prefix, release, build=args.build)
    print_plan(plan)
    if not args.push:
        print("Dry-run only. Add --push to execute the plan.")
        return

    lock = publish(plan, release, prefix)
    lock_file = args.lock_file or ROOT / "results/registry" / f"{release}.lock.json"
    write_lock(lock, lock_file)
    print(f"Wrote immutable image lock: {lock_file}")


if __name__ == "__main__":
    main()
