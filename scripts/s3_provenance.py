#!/usr/bin/env python3
"""Archive Git-visible project inputs locally; never read ignored credentials/results."""

import argparse
import hashlib
import json
from pathlib import Path
import subprocess
import tarfile


def gitlinks(root):
    """Pinned submodules and their checked-out state (not archived as files)."""
    root = Path(root)
    raw = subprocess.check_output(["git", "-C", str(root), "ls-files", "--stage", "-z"])
    links = {}
    for entry in raw.decode().split("\0"):
        if not entry:
            continue
        metadata, name = entry.split("\t", 1)
        mode, pinned, _stage = metadata.split()
        if mode != "160000":
            continue
        path = root / name
        head = subprocess.run(["git", "-C", str(path), "rev-parse", "HEAD"],
                              capture_output=True, text=True)
        status = subprocess.run(["git", "-C", str(path), "status", "--porcelain"],
                                capture_output=True, text=True)
        worktree = head.stdout.strip() if head.returncode == 0 else None
        clean = bool(worktree == pinned and status.returncode == 0 and
                     not status.stdout.strip())
        links[name] = {"index_commit": pinned, "worktree_commit": worktree,
                       "clean": clean}
    return links


def snapshot(root, output, exclude_thesis=False):
    root, output = Path(root).resolve(), Path(output).resolve()
    output.mkdir(parents=True, exist_ok=True)

    def git(*args):
        return subprocess.check_output(["git", "-C", str(root), *args])

    names = sorted(set(git("ls-files", "-z", "--cached", "--others", "--exclude-standard")
                       .decode().rstrip("\0").split("\0")))
    files = []
    archive = output / "source-snapshot.tar.gz"
    with tarfile.open(archive, "w:gz", dereference=False) as bundle:
        for name in names:
            if not name or name.startswith("results/") or (exclude_thesis and name.startswith("Casale/")):
                continue
            path = root / name
            if path == output or output in path.parents:
                continue
            if path.is_symlink():
                data = str(path.readlink()).encode()
            elif path.is_file():
                data = path.read_bytes()
            elif not path.exists():
                files.append({"path": name, "deleted": True})
                continue
            else:
                continue
            files.append({"path": name, "sha256": hashlib.sha256(data).hexdigest(),
                          "mode": path.lstat().st_mode, "symlink": path.is_symlink()})
            bundle.add(path, arcname=name, recursive=False)
    links = gitlinks(root)
    inputs = json.dumps({"files": files, "gitlinks": links}, sort_keys=True).encode()
    result = {"git_revision": git("rev-parse", "HEAD").decode().strip(),
              "git_status": git("status", "--porcelain").decode(),
              "gitlinks": links,
              "input_sha256": hashlib.sha256(inputs).hexdigest(), "files": files,
              "archive_sha256": hashlib.sha256(archive.read_bytes()).hexdigest()}
    (output / "source-manifest.json").write_text(json.dumps(result, indent=2) + "\n")
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--exclude-thesis", action="store_true")
    args = parser.parse_args()
    print(snapshot(args.root, args.output, exclude_thesis=args.exclude_thesis)["input_sha256"])
