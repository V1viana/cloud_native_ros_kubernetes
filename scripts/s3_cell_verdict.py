#!/usr/bin/env python3
"""S3 input/image provenance, separate from the functional scenario outcome."""

import hashlib
import json
from pathlib import Path
import subprocess
import sys

import campaign_cell
from s3_provenance import gitlinks


def is_s3_input(name):
    return not name.startswith("results/") and campaign_cell.is_input(name)


def git_paths(root, *args):
    raw = subprocess.check_output(["git", "-C", str(root), *args])
    return {name for name in raw.decode().split("\0") if name}


def visible_inputs(root):
    return {name for name in git_paths(root, "ls-files", "-z", "--cached", "--others",
                                       "--exclude-standard") if is_s3_input(name) and
            ((Path(root) / name).is_file() or (Path(root) / name).is_symlink())}


def dirty_inputs(root):
    changed = git_paths(root, "diff", "--name-only", "-z", "HEAD")
    untracked = git_paths(root, "ls-files", "--others", "-z", "--exclude-standard")
    dirty = {name for name in changed | untracked if is_s3_input(name)}
    dirty.update(name for name, state in gitlinks(root).items() if not state["clean"])
    return sorted(dirty)


def evaluate(root, result_dir, variant, functional_exit_code, inspect=None):
    root, result_dir = Path(root), Path(result_dir)
    manifest = json.loads((result_dir / "provenance/source-manifest.json").read_text())
    initial = {entry["path"] for entry in manifest["files"] if is_s3_input(entry["path"])}
    changed = campaign_cell.changed_inputs(manifest, root)
    for entry in manifest["files"]:
        if not is_s3_input(entry["path"]) or not entry.get("symlink"):
            continue
        target = root / entry["path"]
        current = (hashlib.sha256(str(target.readlink()).encode()).hexdigest()
                   if target.is_symlink() else None)
        if current != entry["sha256"]:
            changed.append(entry["path"])
    changed.extend(sorted(visible_inputs(root) - initial))
    start_links = manifest.get("gitlinks", {})
    current_links = gitlinks(root)
    changed.extend(name for name in sorted(start_links.keys() | current_links.keys())
                   if start_links.get(name) != current_links.get(name) or
                   not current_links.get(name, {}).get("clean"))

    pods_path = result_dir / "runtime-pods.json"
    try:
        pods = json.loads(pods_path.read_text())
    except (OSError, ValueError):
        pods = {}
    executed = campaign_cell.executed_images(pods)
    project = [tag for tag in executed if tag.startswith(campaign_cell.PROJECT_PREFIX)]
    local = campaign_cell.docker_ids(project, inspect=inspect)
    mismatches = campaign_cell.compare(executed, local)
    expected = campaign_cell.expected_problems(executed, "s3", variant)
    images_match = bool(project) and not mismatches and not expected
    inputs_unchanged = not changed
    verdict = ("INVALID" if not (images_match and inputs_unchanged) else
               "PASS" if functional_exit_code == 0 else "FAIL")
    return {
        "verdict": verdict,
        "functional_exit_code": functional_exit_code,
        "images_match": images_match,
        "inputs_unchanged": inputs_unchanged,
        "changed_inputs": sorted(set(changed)),
        "executed_images": executed,
        "local_image_ids": local,
        "image_mismatches": mismatches,
        "expected_image_problems": expected,
    }


def main(argv):
    if argv[1] == "check-clean":
        dirty = dirty_inputs(argv[2])
        print("clean" if not dirty else "\n".join(dirty))
        return 1 if dirty else 0
    if argv[1] == "verdict":
        result_dir = Path(argv[3])
        result = evaluate(argv[2], result_dir, argv[4], int(argv[5]))
        (result_dir / "provenance-verdict.json").write_text(json.dumps(result, indent=2) + "\n")
        print(result["verdict"])
        return 0
    raise SystemExit("usage: s3_cell_verdict.py check-clean ROOT | verdict ROOT RESULT_DIR VARIANT EXIT_CODE")


if __name__ == "__main__":
    sys.exit(main(sys.argv))
