#!/usr/bin/env python3
"""Time-to-rebuild Kubernetes sampler, on the host: one read attempt per second of the
Deployments of the fleet's namespace, one JSON line per attempt.

  k8s_sampler.py CONTEXT NAMESPACE OUT.jsonl STOP_FILE

Line: {"seq", "t_start", "t" (wall clock at the end of the read), "deployments": {name:
{"replicas", "ready", "updated", "available", "generation", "observed"}}, "error"}. A
missing namespace is an empty, successful read; a failed or timed-out kubectl is an error
line (a gap for the judge, never "not ready"). Stops when STOP_FILE exists.
"""

import json
import os
import subprocess
import sys
import time

PERIOD_SEC = 1.0
TIMEOUT_SEC = 3.0


def read(context, namespace):
    out = subprocess.run(["kubectl", "--context", context, "get", "deployments", "-n", namespace, "-o", "json"],
                         capture_output=True, text=True, timeout=TIMEOUT_SEC)
    if out.returncode:
        raise RuntimeError(out.stderr.strip()[:300])
    deployments = {}
    for item in json.loads(out.stdout)["items"]:
        status = item.get("status") or {}
        deployments[item["metadata"]["name"]] = {
            "replicas": (item.get("spec") or {}).get("replicas", 1),
            "ready": status.get("readyReplicas", 0), "updated": status.get("updatedReplicas", 0),
            "available": status.get("availableReplicas", 0),
            "generation": item["metadata"].get("generation"), "observed": status.get("observedGeneration")}
    return deployments


def main(context, namespace, out_path, stop_file):
    start = time.monotonic()
    seq = 0
    with open(out_path, "a") as out:
        while not os.path.exists(stop_file):
            line = {"seq": seq, "t_start": time.time(), "deployments": None, "error": None}
            try:
                line["deployments"] = read(context, namespace)
            except Exception as exc:
                line["error"] = repr(exc)
            line["t"] = time.time()
            out.write(json.dumps(line) + "\n")
            out.flush()
            seq += 1
            if start + seq * PERIOD_SEC < time.monotonic():
                start = time.monotonic() - seq * PERIOD_SEC
            time.sleep(max(0.0, start + seq * PERIOD_SEC - time.monotonic()))


if __name__ == "__main__":
    main(*sys.argv[1:5])
