#!/usr/bin/env python3
"""Inventory of the original KubeROS and classification of integrations/kuberos against it.

Apache-2.0 section 4(b): every file of integrations/kuberos modified with respect to the original
KubeROS must carry a notice (decision of Viviana, 2 October 2026, R14_WRAPPER_DESIGN_DRAFT.md 13.2-13.8).
The reference is the original kuberos-io/kuberos at its full commit, NOT d8ab529 (that is the head of
Viviana's fork and already carries her changes, 13.8).

The inventory is versioned (licenses/kuberos-<commit>.json), so the check is portable: the
classification needs no network and no clone. A configurable reference (KUBEROS_UPSTREAM_REPO,
KUBEROS_UPSTREAM_COMMIT) verifies the inventory itself: regenerated from the reference, it must be
identical (13.5).

  kuberos_inventory.py generate --repo PATH --commit FULL --remote URL --out FILE
  kuberos_inventory.py verify   --inventory FILE --repo PATH          (regenerate and compare)
  kuberos_inventory.py classify --inventory FILE [--root ROOT]        (classes as JSON)
  kuberos_inventory.py check-notices [--root ROOT]                    (notices = modified files)

Entries record path, git mode, git type and object hash; a symbolic link is hashed on its link
text, as git does (13.6).
"""

import argparse
import hashlib
import json
import os
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
COPY = "integrations/kuberos"
DEFAULT_REMOTE = "https://github.com/kuberos-io/kuberos.git"
DEFAULT_COMMIT = "0253c9ee459145568053bd5c20f96dc1f3eed26c"
INVENTORY = os.path.join(ROOT, "licenses", f"kuberos-{DEFAULT_COMMIT}.json")


def entries_at(repo, commit):
    """Every entry of the commit's tree: {path: {"mode", "type", "sha"}}."""
    if len(commit) != 40:
        raise SystemExit(f"the reference commit must be the full 40-character hash, got {commit!r}")
    out = subprocess.run(["git", "-C", repo, "ls-tree", "-r", "--full-tree", commit],
                         capture_output=True, text=True, check=True).stdout
    entries = {}
    for line in out.splitlines():
        meta, path = line.split("\t", 1)
        mode, kind, sha = meta.split()
        entries[path] = {"mode": mode, "type": kind, "sha": sha}
    return entries


def generate(repo, commit, remote):
    return {"remote": remote, "commit": commit,
            "command": f"scripts/kuberos_inventory.py generate --commit {commit} --remote {remote}",
            "entries": entries_at(repo, commit)}


def blob_sha(data):
    return hashlib.sha1(b"blob %d\0" % len(data) + data).hexdigest()


def local_entry(path):
    """The same triple git would record for a file on disk, computed without git."""
    if os.path.islink(path):
        return {"mode": "120000", "type": "blob", "sha": blob_sha(os.readlink(path).encode())}
    data = open(path, "rb").read()
    mode = "100755" if os.stat(path).st_mode & 0o111 else "100644"
    return {"mode": mode, "type": "blob", "sha": blob_sha(data)}


def tracked_copy(root):
    """The files of integrations/kuberos that the revision tracks (untracked or ignored files are
    not part of the copy; a dirty worktree is refused elsewhere)."""
    out = subprocess.run(["git", "-C", root, "ls-files", "-z", "--", COPY],
                         capture_output=True, text=True, check=True).stdout
    return sorted(p[len(COPY) + 1:] for p in out.split("\0") if p)


def classify(inventory, root=ROOT):
    """Classes of 13.6: unchanged, modified, mode_change, new, not_copied."""
    original = inventory["entries"]
    classes = {"unchanged": [], "modified": [], "mode_change": [], "new": [], "not_copied": []}
    present = tracked_copy(root)
    for rel in present:
        mine = local_entry(os.path.join(root, COPY, rel))
        ref = original.get(rel)
        if ref is None:
            classes["new"].append(rel)
        elif ref["sha"] == mine["sha"] and ref["mode"] == mine["mode"]:
            classes["unchanged"].append(rel)
        elif ref["sha"] == mine["sha"]:
            classes["mode_change"].append(rel)
        else:
            classes["modified"].append(rel)
    classes["not_copied"] = sorted(set(original) - set(present))
    return classes


def needs_notice(classes):
    return sorted(classes["modified"] + classes["mode_change"])


# The notice of Apache-2.0 section 4(b), one per modified file (decision of Viviana, 2 October 2026,
# R14_WRAPPER_DESIGN_DRAFT.md 13.4, 13.8, 13.10). The first line is what the checks look for.
NOTICE = ("Modified by the cloud_native_ros_kubernetes project (2026) from KubeROS "
          f"(kuberos-io/kuberos commit {DEFAULT_COMMIT[:7]}).")
NOTICE_SEE = ("See THIRD_PARTY_NOTICES (repository root; in the container images: "
              "/usr/share/licenses/cloud-native-ros/THIRD_PARTY_NOTICES).")
NOTICE_WITHIN_LINES = 6            # after a shebang or a Markdown title, never deeper


def has_notice(path):
    """The notice is in the first lines of the file (a symbolic link cannot carry one)."""
    if os.path.islink(path):
        return False
    with open(path, errors="replace") as f:
        head = [next(f, "") for _ in range(NOTICE_WITHIN_LINES)]
    return any(NOTICE in line for line in head)


def notice_report(inventory, root=ROOT):
    """4(b): the files carrying the notice must be EXACTLY the modified ones."""
    need = set(needs_notice(classify(inventory, root)))
    have = {rel for rel in tracked_copy(root) if has_notice(os.path.join(root, COPY, rel))}
    return {"missing": sorted(need - have), "unexpected": sorted(have - need), "ok": sorted(need & have)}


def load(path):
    with open(path) as f:
        return json.load(f)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    g = sub.add_parser("generate")
    g.add_argument("--repo", required=True)
    g.add_argument("--commit", default=DEFAULT_COMMIT)
    g.add_argument("--remote", default=DEFAULT_REMOTE)
    g.add_argument("--out", default=INVENTORY)
    v = sub.add_parser("verify")
    v.add_argument("--inventory", default=INVENTORY)
    v.add_argument("--repo", required=True)
    c = sub.add_parser("classify")
    c.add_argument("--inventory", default=INVENTORY)
    c.add_argument("--root", default=ROOT)
    n = sub.add_parser("check-notices")
    n.add_argument("--inventory", default=INVENTORY)
    n.add_argument("--root", default=ROOT)
    a = ap.parse_args(argv)
    if a.cmd == "generate":
        os.makedirs(os.path.dirname(os.path.abspath(a.out)), exist_ok=True)
        with open(a.out, "w") as f:
            json.dump(generate(a.repo, a.commit, a.remote), f, indent=1, sort_keys=True)
            f.write("\n")
        return 0
    if a.cmd == "verify":
        inv = load(a.inventory)
        same = generate(a.repo, inv["commit"], inv["remote"]) == inv
        print("inventory matches the reference" if same else "INVENTORY DIFFERS FROM THE REFERENCE")
        return 0 if same else 1
    if a.cmd == "check-notices":
        report = notice_report(load(a.inventory), a.root)
        json.dump(report, sys.stdout, indent=1)
        print()
        return 1 if report["missing"] or report["unexpected"] else 0
    classes = classify(load(a.inventory), a.root)
    json.dump({k: v for k, v in classes.items()}, sys.stdout, indent=1)
    print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
