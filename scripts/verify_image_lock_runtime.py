#!/usr/bin/env python3
"""Verify that running Pod image IDs match a project image release lock."""

import argparse
import csv
import json
from pathlib import Path


def normalize_reference(reference):
    value = str(reference or "")
    for prefix in ("docker-pullable://", "docker://"):
        if value.startswith(prefix):
            value = value[len(prefix):]
    if value.startswith("docker.io/"):
        value = value[len("docker.io/"):]
    return value


def lock_references(lock_path):
    lock = json.loads(Path(lock_path).read_text(encoding="utf-8"))
    references = {
        normalize_reference(item["immutable_reference"]): item["name"]
        for item in lock.get("images", [])
    }
    aliases = {
        normalize_reference(alias)
        for item in lock.get("images", [])
        for alias in item.get("aliases", [])
    }
    if not references or any("@sha256:" not in item for item in references):
        raise ValueError("image lock does not contain immutable references")
    return lock, references, aliases


def container_pairs(pod):
    spec = pod.get("spec", {})
    status = pod.get("status", {})
    specs = spec.get("initContainers", []) + spec.get("containers", [])
    statuses = {
        item.get("name"): item
        for item in (
            status.get("initContainerStatuses", [])
            + status.get("containerStatuses", [])
        )
    }
    return ((item, statuses.get(item.get("name"), {})) for item in specs)


def verify(pod_list, lock_path, minimum):
    lock, references, aliases = lock_references(lock_path)
    rows = []
    failures = []
    for pod in pod_list.get("items", []):
        metadata = pod.get("metadata", {})
        for spec, status in container_pairs(pod):
            image = normalize_reference(spec.get("image"))
            if image in aliases:
                failures.append(
                    f"mutable project alias remains in {metadata.get('name')}/{spec.get('name')}: {image}"
                )
                continue
            if image not in references:
                continue
            image_id = normalize_reference(status.get("imageID"))
            expected_repository, expected_digest = image.rsplit("@", 1)
            actual_parts = image_id.rsplit("@", 1)
            matched = (
                len(actual_parts) == 2
                and actual_parts[0] == expected_repository
                and actual_parts[1] == expected_digest
            )
            rows.append(
                {
                    "pod": metadata.get("name", ""),
                    "container": spec.get("name", ""),
                    "component": references[image],
                    "node": pod.get("spec", {}).get("nodeName", ""),
                    "phase": pod.get("status", {}).get("phase", ""),
                    "requested_image": image,
                    "runtime_image_id": image_id,
                    "matched": str(matched).lower(),
                }
            )
            if not matched:
                failures.append(
                    f"runtime image mismatch in {metadata.get('name')}/{spec.get('name')}"
                )
    if len(rows) < minimum:
        failures.append(f"expected at least {minimum} project containers, found {len(rows)}")
    return lock, rows, failures


def write_csv(path, rows):
    fields = (
        "pod", "container", "component", "node", "phase",
        "requested_image", "runtime_image_id", "matched",
    )
    with Path(path).open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=fields, lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)


def main(args=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--pods-json", type=Path, required=True)
    parser.add_argument("--lock", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--minimum", type=int, default=1)
    parsed = parser.parse_args(args)

    pod_list = json.loads(parsed.pods_json.read_text(encoding="utf-8"))
    lock, rows, failures = verify(pod_list, parsed.lock, parsed.minimum)
    write_csv(parsed.output, rows)
    summary = {
        "release": lock.get("release"),
        "verified_containers": len(rows),
        "result": "PASS" if not failures else "FAIL",
        "failures": failures,
    }
    print(json.dumps(summary, indent=2, sort_keys=True))
    if failures:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
