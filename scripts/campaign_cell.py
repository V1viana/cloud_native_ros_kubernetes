#!/usr/bin/env python3
"""Per-cell bookkeeping of scripts/run_campaign.sh (R9, decision D6).

Viviana (2026-09-26): one run per cell, provenance, explicit handling of
interruptions, the images a cell actually ran recorded and checked. Commands:
  images PODS_JSON OUT_JSON SCENARIO VARIANT
      the (image, imageID) pairs of every container the cell's cluster ran,
      compared with the local docker image IDs of the same tags (a k3d Pod
      reports the imported image's config digest, the same sha256 as docker's
      image ID) and with the project images this scenario and variant must
      run (EXPECTED_IMAGES: A and B run different components); prints "true"
      if the project images are exactly the expected ones and all match, else
      "false";
  verdict OUTCOME IMAGES_MATCH INPUTS_UNCHANGED
      R9's verdict (Viviana, 2026-09-26): a cell whose images or inputs do not
      match cannot be an R9 PASS; it is INVALID, and the functional outcome
      and the reason are kept beside it -- a provenance problem is not turned
      into a defect of the system. JSON fragment on stdout.
  inputs-unchanged MANIFEST ROOT
      the campaign's input files (the start manifest of scripts/s3_provenance.py
      minus the thesis, docs/ and Markdown, which no runner reads) still have
      their start hashes; prints "true" or "false: <changed paths>";
  outcome EXIT_CODE HAS_REPORT
      PASS (0), RUNNER_INVALID (2, the runner declared its sample invalid,
      e.g. E2's hover gate), FAIL (other non-zero with a REPORT), NO_EVIDENCE
      (non-zero without one: to be classified by hand as FAIL or INVALID, per
      docs/R9_FUNCTIONAL_REVALIDATION.md). INTERRUPTED is written by the
      campaign's own trap, never derived here.
Tested offline: operator/tests/test_campaign_cell.py.
"""

import hashlib
import json
from pathlib import Path
import subprocess
import sys

PROJECT_PREFIX = "cloud-native-ros/"

# The project images each cell runs, from the runners' imports and the Pods of
# recent runs (docs/R9_FUNCTIONAL_REVALIDATION.md). U1 and U2 run E0's
# baseline, whose images they share, and so does S1 (R10: E0's topology); E1-B
# runs no ROSModule, so no State Bridge, and does run the onboard P0 detector.
_A = ("cloud-native-ros/control-plane:p2", "cloud-native-ros/event-detector:p2", "cloud-native-ros/kuberos:p2")
_B = ("cloud-native-ros/control-plane:p2", "cloud-native-ros/fleet-operator:p2", "cloud-native-ros/state-bridge:p2")
_OBSERVER = "cloud-native-ros/mission-observer:p2"
EXPECTED_IMAGES = {
    **{(s, "a"): set(_A) for s in ("e0", "p2", "e4", "u1", "u2", "s1", "s3")},
    **{(s, "b"): set(_B) for s in ("e0", "p2", "e4", "u1", "u2", "s1", "s3")},
    ("e1", "a"): {*_A, _OBSERVER},
    ("e1", "b"): {"cloud-native-ros/control-plane:p2", "cloud-native-ros/event-detector:p2",
                  "cloud-native-ros/fleet-operator:p2", _OBSERVER},
    ("e2", "a"): {"cloud-native-ros/control-plane:e2", "cloud-native-ros/event-detector:e2-upstream"},
    ("e2", "b"): {"cloud-native-ros/control-plane:p2", "cloud-native-ros/fleet-operator:e2",
                  "cloud-native-ros/state-bridge:p2"},
}


def _tag(image):
    """docker.io/cloud-native-ros/x:p2 -> cloud-native-ros/x:p2."""
    for prefix in ("docker.io/library/", "docker.io/"):
        if image.startswith(prefix):
            return image[len(prefix):]
    return image


def executed_images(pods):
    seen = {}
    for pod in pods.get("items", []):
        for status in (pod.get("status", {}).get("containerStatuses") or []) + \
                      (pod.get("status", {}).get("initContainerStatuses") or []):
            if status.get("imageID"):
                seen.setdefault(_tag(status["image"]), set()).add(status["imageID"].split("@")[-1])
    return {image: sorted(ids) for image, ids in sorted(seen.items())}


def compare(executed, local_ids):
    """Project images whose executed ID differs from the local one, or ran twice."""
    mismatches = {}
    for image, ids in executed.items():
        if not image.startswith(PROJECT_PREFIX):
            continue
        local = local_ids.get(image)
        if len(ids) != 1 or local != ids[0]:
            mismatches[image] = {"executed": ids, "local": local}
    return mismatches


NOT_INPUTS = ("Casale/", "docs/")


def is_input(path):
    return not path.startswith(NOT_INPUTS) and not path.endswith(".md")


def changed_inputs(manifest, root):
    changed = []
    for entry in manifest.get("files", []):
        path = entry["path"]
        if not is_input(path) or entry.get("deleted") or entry.get("symlink"):
            continue
        target = Path(root) / path
        now = hashlib.sha256(target.read_bytes()).hexdigest() if target.is_file() else None
        if now != entry.get("sha256"):
            changed.append(path)
    return changed


def docker_ids(tags, inspect=None):
    def default(tag):
        out = subprocess.run(["docker", "image", "inspect", "--format", "{{.Id}}", tag],
                             capture_output=True, text=True)
        return out.stdout.strip() or None
    inspect = inspect or default
    return {tag: inspect(tag) for tag in tags}


def expected_problems(executed, scenario, variant):
    expected = EXPECTED_IMAGES.get((scenario, variant))
    if expected is None:
        return {"unknown_cell": f"{scenario}-{variant}"}
    ran = {image for image in executed if image.startswith(PROJECT_PREFIX)}
    problems = {}
    if expected - ran:
        problems["missing"] = sorted(expected - ran)
    if ran - expected:
        problems["unexpected"] = sorted(ran - expected)
    return problems


def r9_verdict(outcome_, images_match, inputs_unchanged):
    reasons = []
    if images_match != "true":
        reasons.append("immagini non conformi alla mappa attesa o agli ID locali (images.json)")
    if inputs_unchanged != "true":
        reasons.append(f"input cambiati durante la cella ({inputs_unchanged})")
    return {"r9_verdict": "INVALID" if reasons else outcome_, "functional_outcome": outcome_,
            "invalid_reason": "; ".join(reasons)}


def outcome(exit_code, has_report):
    if exit_code == 0:
        return "PASS"
    if exit_code == 2:
        return "RUNNER_INVALID"
    return "FAIL" if has_report else "NO_EVIDENCE"


def main(argv):
    if argv[1] == "images":
        try:
            pods = json.load(open(argv[2]))
        except (OSError, ValueError):
            pods = {}
        executed = executed_images(pods)
        project = [i for i in executed if i.startswith(PROJECT_PREFIX)]
        local = docker_ids(project)
        mismatches = compare(executed, local)
        expected = expected_problems(executed, argv[4], argv[5])
        with open(argv[3], "w") as stream:
            json.dump({"executed": executed, "local": local, "mismatches": mismatches,
                       "expected": sorted(EXPECTED_IMAGES.get((argv[4], argv[5]), [])),
                       "expected_problems": expected}, stream, indent=1)
        print("true" if project and not mismatches and not expected else "false")
    elif argv[1] == "inputs-unchanged":
        changed = changed_inputs(json.load(open(argv[2])), argv[3])
        print("true" if not changed else "false: " + ", ".join(changed[:10]))
    elif argv[1] == "verdict":
        print(json.dumps(r9_verdict(argv[2], argv[3], argv[4]))[1:-1])
    elif argv[1] == "outcome":
        print(outcome(int(argv[2]), argv[3] == "true"))
    else:
        raise SystemExit(f"unknown command {argv[1]}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
