#!/usr/bin/env python3
"""Bounded, read-only Job polling and failure evidence for the S3 harness."""

import argparse
import json
import re
from pathlib import Path
import shutil
import subprocess
import time


def job_outcome(job):
    conditions = {c["type"] for c in job.get("status", {}).get("conditions", [])
                  if c.get("status") == "True"}
    if conditions & {"Failed", "FailureTarget"}:
        return "failed"
    if "Complete" in conditions:
        return "complete"
    return "pending"


def wait_job(command, name, timeout, output):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            result = subprocess.run([*command, "get", "job", name, "-o", "json"],
                                    capture_output=True, text=True,
                                    timeout=max(0.1, min(10, deadline - time.monotonic())))
            if result.returncode:
                print(f"Job {name}: API read failed: {result.stderr.strip()}", flush=True)
            else:
                job = json.loads(result.stdout)
                Path(output).write_text(json.dumps(job, indent=2) + "\n")
                outcome = job_outcome(job)
                if outcome != "pending":
                    print(f"Job {name}: {outcome}", flush=True)
                    return 0 if outcome == "complete" else 1
        except (subprocess.TimeoutExpired, json.JSONDecodeError) as exc:
            print(f"Job {name}: observation error: {exc}", flush=True)
        time.sleep(max(0, min(2, deadline - time.monotonic())))
    print(f"Job {name}: timeout after {timeout}s (see last observation)", flush=True)
    return 124


def incident_outcome(variant, observation):
    if variant == "a":
        matches = re.findall(
            r"Incident drone01-AnalyticsLatencySLO-[^\s]+ completed as ([^\r\n]+)",
            observation,
        )
        if not matches:
            return "pending", ""
        outcome = matches[-1].strip()
        return ("success" if outcome == "analytics_slo_recovered (STABLE)" else "failure", outcome)
    if variant == "b":
        if observation == "Recovered":
            return "success", observation
        if observation in {"RolledBack", "Escalated", "FallbackFailed"}:
            return "failure", observation
        return "pending", ""
    raise ValueError("variant must be a or b")


def wait_incident(command, variant, timeout, started_monotonic, output,
                  clock=time.monotonic, sleeper=time.sleep, runner=subprocess.run):
    deadline = started_monotonic + timeout
    path = Path(output)
    last_error = ""
    while clock() < deadline:
        args = (["logs", "deployment/operational-event-dispatcher-p2", "--tail=500"]
                if variant == "a" else
                ["get", "adaptationpolicy", "analytics-latency-slo-s3-drone01",
                 "-o", "jsonpath={.status.state}"])
        try:
            remaining = deadline - clock()
            if remaining <= 0:
                break
            result = runner([*command, *args], capture_output=True, text=True,
                            timeout=min(5, remaining))
            if result.returncode == 0:
                state, outcome = incident_outcome(variant, result.stdout.strip())
                if state != "pending":
                    path.write_text(json.dumps({"state": state, "outcome": outcome,
                                                "elapsed_sec": clock() - started_monotonic}, indent=2) + "\n")
                    print(f"Incident: {state} ({outcome})", flush=True)
                    return 0 if state == "success" else 1
            else:
                last_error = result.stderr.strip()
        except (subprocess.TimeoutExpired, OSError) as exc:
            last_error = str(exc)
        sleeper(max(0, min(1, deadline - clock())))
    path.write_text(json.dumps({"state": "timeout", "outcome": "",
                                "elapsed_sec": clock() - started_monotonic,
                                "last_error": last_error}, indent=2) + "\n")
    print(f"Incident: no terminal outcome within {timeout}s", flush=True)
    return 124


def collect(command, output, budget=120):
    output = Path(output)
    output.mkdir(parents=True, exist_ok=True)
    deadline = time.monotonic() + budget
    index = []

    def capture(name, args):
        remaining = deadline - time.monotonic()
        if remaining <= 0 or shutil.disk_usage(output).free < 1024 ** 3:
            index.append({"file": name, "skipped": "time budget or <1 GiB free"})
            return None
        try:
            result = subprocess.run([*command, *args], capture_output=True,
                                    timeout=min(6, remaining))
            raw = result.stdout if result.returncode == 0 else result.stderr
            (output / name).write_bytes(raw[:8 * 1024 ** 2])
            index.append({"file": name, "returncode": result.returncode,
                          "truncated": len(raw) > 8 * 1024 ** 2})
            return json.loads(raw) if result.returncode == 0 and name.endswith(".json") else None
        except (subprocess.TimeoutExpired, json.JSONDecodeError, OSError) as exc:
            index.append({"file": name, "error": str(exc)})
            return None

    pods = capture("pods.json", ["get", "pods", "-o", "json"])
    for resource in ("jobs", "deployments", "replicasets", "rosmodules", "adaptationpolicies",
                     "roslifecyclepolicies", "robotfleets", "hpa", "events", "nodes"):
        capture(f"{resource}.json", ["get", resource, "-o", "json"])
    capture("top-pods.txt", ["top", "pods", "--containers"])
    capture("top-nodes.txt", ["top", "nodes"])
    capture("kuberos-failed-deployments.txt", ["exec", "deployment/kuberos", "-c", "api", "--",
            "python", "manage.py", "shell", "-c",
            "from main.models import Deployment; "
            "print(list(Deployment.objects.filter(status='failed').values('name','status')))"])
    # Capture failed workload containers before the collection budget runs out.
    def priority(pod):
        name = pod["metadata"]["name"]
        if any(r.get("kind") == "Job" for r in pod["metadata"].get("ownerReferences", [])):
            return 0, name
        if "drone" not in name:
            return 1, name
        statuses = pod.get("status", {}).get("containerStatuses", [])
        if any(not c.get("ready", False) or c.get("restartCount", 0) for c in statuses):
            return 2, name
        return (3 if "drone01" in name or "edge" in name else 4), name

    for pod in sorted((pods or {}).get("items", []), key=priority):
        name = pod["metadata"]["name"]
        restarts = {c["name"]: c.get("restartCount", 0)
                    for c in pod.get("status", {}).get("containerStatuses", [])}
        for container in pod["spec"].get("containers", []):
            cname = container["name"]
            args = ["logs", name, "-c", cname, "--timestamps", "--tail=200", "--limit-bytes=131072"]
            capture(f"{name}__{cname}.log", args)
            if restarts.get(cname):
                capture(f"{name}__{cname}__previous.log", [*args, "--previous"])
            if priority(pod)[0] in (1, 2, 3):
                capture(f"{name}__{cname}__cgroup.txt", ["exec", name, "-c", cname, "--",
                    "sh", "-c", "for f in /sys/fs/cgroup/cpu.stat /sys/fs/cgroup/cpu.max "
                    "/sys/fs/cgroup/cpu/cpu.stat /sys/fs/cgroup/memory.events "
                    "/proc/pressure/cpu /proc/pressure/io; do "
                    "if [ -r \"$f\" ]; then printf '\\n%s\\n' \"$f\"; cat \"$f\"; fi; done"])
                if priority(pod)[0] == 2:
                    capture(f"{name}__{cname}__processes.txt", ["exec", name, "-c", cname,
                            "--", "sh", "-c", "ps -eo pid,ppid,stat,args"])
    (output / "collection-index.json").write_text(json.dumps(index, indent=2) + "\n")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--context", required=True)
    parser.add_argument("--namespace", required=True)
    subs = parser.add_subparsers(dest="action", required=True)
    wait = subs.add_parser("wait-job")
    wait.add_argument("name")
    wait.add_argument("--timeout", type=float, required=True)
    wait.add_argument("--output", required=True)
    incident = subs.add_parser("wait-incident")
    incident.add_argument("--variant", choices=("a", "b"), required=True)
    incident.add_argument("--timeout", type=float, required=True)
    incident.add_argument("--started-monotonic-ns", type=int, required=True)
    incident.add_argument("--output", required=True)
    diagnostic = subs.add_parser("collect")
    diagnostic.add_argument("--output", required=True)
    diagnostic.add_argument("--budget", type=float, default=120)
    args = parser.parse_args()
    command = ["kubectl", "--context", args.context, "--namespace", args.namespace,
               "--request-timeout=5s"]
    if args.action == "wait-job":
        return wait_job(command, args.name, args.timeout, args.output)
    if args.action == "wait-incident":
        return wait_incident(command, args.variant, args.timeout,
                             args.started_monotonic_ns / 1e9, args.output)
    collect(command, args.output, args.budget)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
