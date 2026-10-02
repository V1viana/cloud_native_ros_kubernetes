#!/usr/bin/env python3
"""S2 host-side observers (R11, contract s2-partition-v1, "Raccolta, copertura").

Each runs until a stop file appears and writes JSON lines with the host's
monotonic clock (m0/m1) and UTC (w0/w1) at the start and end of every read;
a failed read is a record with its error, never dropped (a gap, not a zero):
  inventory  the namespace's Pods, Deployments and -- by variant -- ConfigMaps
             (A: analytics-routing-drone01) or ROSModules and AdaptationPolicies
             (B), summarised (nominal period 1 s);
  nodes      each node's Ready condition, heartbeat and taints, and its Lease
             renewal (nominal 1 s);
  audit      the audit writer's file read from an offset: every new record with the
             first read that contained it, and every read (with its count of new
             records), so "last read without, first read with" is known;
  logs       `kubectl logs -f --timestamps` of one Deployment's Pod, restarted
             when it ends, every line with its receive time and the Pod's UID;
             the stop seen within 0.2 s whatever the Pod writes, the child
             terminated (killed if needed) and reaped within 5 s;
  px4        one node's PX4 vehicle_status read in a loop inside the PX4 container
             through crictl on the node (the channel the partition does not cut),
             in E2's "READ <ns>" format.
Tested offline: operator/tests/test_s2_observers.py.
"""

import argparse
import json
import os
import queue
import subprocess
import sys
import threading
import time

NS = "cloud-native-p2"
KINDS = {"a": "pods,deployments,configmaps", "b": "pods,deployments,rosmodules,adaptationpolicies"}
CALL_TIMEOUT_SEC = 5.0


def run(cmd, timeout=CALL_TIMEOUT_SEC):
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return p.returncode, p.stdout, p.stderr
    except subprocess.TimeoutExpired:
        return None, "", "observer timeout"


def _ready(pod):
    return any(c.get("type") == "Ready" and c.get("status") == "True"
               for c in (pod.get("status") or {}).get("conditions") or [])


def summarize_inventory(items):
    out = {"pods": [], "deployments": [], "rosmodules": [], "policies": [], "routing": None}
    for item in items:
        kind, meta = item.get("kind"), item.get("metadata") or {}
        status, spec = item.get("status") or {}, item.get("spec") or {}
        if kind == "Pod":
            statuses = status.get("containerStatuses") or []
            out["pods"].append({
                "name": meta.get("name"), "uid": meta.get("uid"), "node": spec.get("nodeName"),
                "labels": meta.get("labels") or {}, "phase": status.get("phase"), "ready": _ready(item),
                "terminating": bool(meta.get("deletionTimestamp")),
                "restarts": {c["name"]: c.get("restartCount", 0) for c in statuses},
                "containers": {c["name"]: c.get("containerID") for c in statuses}})
        elif kind == "Deployment":
            out["deployments"].append({
                "name": meta.get("name"), "uid": meta.get("uid"), "generation": meta.get("generation"),
                "observed": status.get("observedGeneration"), "replicas": spec.get("replicas"),
                "ready": status.get("readyReplicas") or 0})
        elif kind == "ROSModule":
            windows = {k: {"windowSec": (v or {}).get("windowSec"),
                           "windows": [{f: w.get(f) for f in ("seq", "start", "end", "samples", "p95Ms")}
                                       for w in (v or {}).get("windows") or []]}
                       for k, v in (status.get("metricWindows") or {}).items()}
            lifecycle = {k: {f: (v or {}).get(f) for f in ("observedLifecycleState", "lastObservedTime", "phase")}
                         for k, v in (status.get("lifecycleInstances") or {}).items()}
            out["rosmodules"].append({
                "name": meta.get("name"), "uid": meta.get("uid"), "generation": meta.get("generation"),
                "labels": meta.get("labels") or {}, "placement": spec.get("placement"),
                "lifecycleTarget": spec.get("lifecycleTarget"),
                "observedLifecycleState": status.get("observedLifecycleState"),
                "lifecycleInstances": lifecycle, "metricWindows": windows,
                "owner": [o.get("name") for o in meta.get("ownerReferences") or []]})
        elif kind == "AdaptationPolicy":
            out["policies"].append({
                "name": meta.get("name"), "uid": meta.get("uid"), "generation": meta.get("generation"),
                **{f: status.get(f) for f in ("state", "correlationId", "edgeModuleName", "lastTransitionTime",
                                              "migratingSince", "handedOverAt", "windowCursor",
                                              "consecutiveTriggerWindows", "consecutiveRecoveryWindows",
                                              "observedGeneration")},
                "conditions": [{f: c.get(f) for f in ("type", "status", "reason", "lastTransitionTime")}
                               for c in status.get("conditions") or []]})
        elif kind == "ConfigMap" and meta.get("name") == "analytics-routing-drone01":
            out["routing"] = {"uid": meta.get("uid"), "resourceVersion": meta.get("resourceVersion"),
                              "data": item.get("data") or {}}
    return out


def summarize_nodes(nodes, leases):
    renew = {l["metadata"]["name"]: (l.get("spec") or {}).get("renewTime") for l in leases.get("items") or []}
    out = []
    for node in nodes.get("items") or []:
        name = node["metadata"]["name"]
        ready = next((c for c in (node.get("status") or {}).get("conditions") or [] if c.get("type") == "Ready"), {})
        out.append({"name": name, "ready": ready.get("status"), "reason": ready.get("reason"),
                    "heartbeat": ready.get("lastHeartbeatTime"), "since": ready.get("lastTransitionTime"),
                    "taints": [{"key": t.get("key"), "effect": t.get("effect")}
                               for t in (node.get("spec") or {}).get("taints") or []],
                    "lease_renew": renew.get(name)})
    return out


def timed(read):
    record = {"m0": time.monotonic(), "w0": time.time()}
    try:
        record.update(read())
    except Exception as exc:  # noqa: BLE001 -- a failed read is data
        record["error"] = f"{type(exc).__name__}: {exc}"[:300]
    record.update(m1=time.monotonic(), w1=time.time())
    return record


def _kubectl_json(context, args, runner=run):
    rc, out, err = runner(["kubectl", "--context", context, *args, "-o", "json"])
    if rc != 0:
        raise RuntimeError((err or out or f"rc {rc}").strip()[:300])
    return json.loads(out)


def read_inventory(context, variant, runner=run):
    items = _kubectl_json(context, ["-n", NS, "get", KINDS[variant]], runner)["items"]
    return {"kind": "inventory", **summarize_inventory(items)}


def read_nodes(context, runner=run):
    nodes = _kubectl_json(context, ["get", "nodes"], runner)
    leases = _kubectl_json(context, ["-n", "kube-node-lease", "get", "leases"], runner)
    return {"kind": "nodes", "nodes": summarize_nodes(nodes, leases)}


class AuditReader:
    """The writer's audit file from an offset; each new record with its first read."""

    def __init__(self, context, runner=run):
        self.context, self.offset, self.index = context, 0, 0
        self._runner = runner

    def read(self):
        rc, data, err = self._runner(["kubectl", "--context", self.context, "-n", NS, "exec",
                                      "deployment/p2-audit-writer", "--", "tail", "-c", f"+{self.offset + 1}",
                                      "/data/audit.jsonl"])
        if rc != 0:
            raise RuntimeError((err or f"rc {rc}").strip()[:300])
        end = data.rfind("\n") + 1
        records = []
        for line in data[:end].splitlines():
            if line.strip():
                self.index += 1
                try:
                    records.append({"index": self.index, "record": json.loads(line)})
                except ValueError:
                    records.append({"index": self.index, "unparsed": line[:300]})
        self.offset += len(data[:end].encode())
        return {"kind": "audit", "offset": self.offset, "new": records}


STOP_POLL_SEC = 0.2
STOP_WAIT_SEC = 2.0               # after TERM, then after KILL: a follow ends within 5 s of the stop
_EOF = object()


def _pump(stream, lines):
    """The child's output, line by line, into a queue (a partial last line too)."""
    try:
        for line in iter(stream.readline, ""):
            lines.put(line)
    except (OSError, ValueError):
        pass
    lines.put(_EOF)


def stop_child(proc, wait_sec=STOP_WAIT_SEC):
    """TERM, a bounded wait, KILL if needed, reaped: 'exited', 'terminated' or 'killed'."""
    if proc.poll() is not None:
        return "exited"
    proc.terminate()
    try:
        proc.wait(timeout=wait_sec)
        return "terminated"
    except subprocess.TimeoutExpired:
        proc.kill()
        try:
            proc.wait(timeout=wait_sec)
        except subprocess.TimeoutExpired:
            return "unkillable"
        return "killed"


def follow_logs(context, deployment, container, out, stop_file, runner_popen=subprocess.Popen):
    """kubectl logs -f of the Deployment's current Pod; restarted when it ends.
    The Pod is found through the Deployment's own selector (fleet-operator's is
    app=..., the others' app.kubernetes.io/name=...). The stop file is checked every
    STOP_POLL_SEC whatever the Pod writes (decision 3 after the first qualification
    round: a silent Pod blocked the follower): on the stop the child is terminated,
    killed if needed and reaped, its pipe closed, a partial last line kept, the
    outcome recorded in follow_end."""
    selector = None
    while not os.path.exists(stop_file):
        started = {"m0": time.monotonic(), "w0": time.time()}
        if selector is None:
            rc, dep_json, err = run(["kubectl", "--context", context, "-n", NS, "get", "deployment", deployment,
                                     "-o", "json"], timeout=3)
            try:
                labels = json.loads(dep_json)["spec"]["selector"]["matchLabels"]
                selector = ",".join(f"{k}={v}" for k, v in sorted(labels.items()))
            except (ValueError, KeyError, TypeError):
                out.write(json.dumps({**started, "kind": "follow_error", "error": (err or dep_json)[:300]}) + "\n")
                out.flush()
                time.sleep(1.0)
                continue
        rc, pod_json, err = run(["kubectl", "--context", context, "-n", NS, "get", "pod", "-l", selector, "-o", "json"],
                                timeout=3)
        try:
            pod = json.loads(pod_json)["items"][0]
            name, uid = pod["metadata"]["name"], pod["metadata"]["uid"]
        except (ValueError, KeyError, IndexError, TypeError):
            out.write(json.dumps({**started, "kind": "follow_error", "error": (err or pod_json)[:300]}) + "\n")
            out.flush()
            time.sleep(1.0)
            continue
        out.write(json.dumps({**started, "kind": "follow_start", "pod": name, "pod_uid": uid}) + "\n")
        out.flush()
        cmd = ["kubectl", "--context", context, "-n", NS, "logs", "-f", "--timestamps", f"pod/{name}"]
        if container:
            cmd += ["-c", container]
        proc = runner_popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        lines = queue.Queue()
        reader = threading.Thread(target=_pump, args=(proc.stdout, lines), daemon=True)
        reader.start()

        def emit(line):
            record = {"kind": "line", "m": time.monotonic(), "w": time.time(), "pod_uid": uid,
                      "line": line.rstrip("\n")}
            if not line.endswith("\n"):
                record["partial"] = True
            out.write(json.dumps(record) + "\n")
            out.flush()
        ended_by_stop = False
        while True:
            try:
                line = lines.get(timeout=STOP_POLL_SEC)
            except queue.Empty:
                line = None
            if line is _EOF:
                break
            if line is not None:
                emit(line)
            if os.path.exists(stop_file):
                ended_by_stop = True
                break
        stop_seen = {"m": time.monotonic(), "w": time.time()}
        outcome = stop_child(proc)
        reader.join(STOP_WAIT_SEC)
        try:
            proc.stdout.close()
        except (OSError, AttributeError):
            pass
        while True:                                   # what came in before the pipe closed
            try:
                line = lines.get_nowait()
            except queue.Empty:
                break
            if line is not _EOF:
                emit(line)
        out.write(json.dumps({"kind": "follow_end", "m": time.monotonic(), "w": time.time(), "pod_uid": uid,
                              "rc": proc.returncode, "stop": outcome if ended_by_stop else f"ended ({outcome})",
                              "stop_seen_m": stop_seen["m"], "stop_seen_w": stop_seen["w"]}) + "\n")
        out.flush()
        if not ended_by_stop:
            time.sleep(0.5)


def px4_command(node, period_sec=0.2, max_sec=900):
    """One long loop inside the PX4 container, through crictl on its node."""
    loop = (f"cd /opt/px4; PATH=/opt/px4/bin:$PATH; end=$(($(date +%s) + {int(max_sec)})); "
            f"while [ $(date +%s) -lt $end ]; do echo \"READ $(date +%s%N)\"; "
            f"px4-listener vehicle_status -n 1; sleep {period_sec}; done")
    return ["docker", "exec", node, "sh", "-c",
            f"cid=$(crictl ps --name px4 -q | head -n 1); [ -n \"$cid\" ] || {{ echo NO_PX4_CONTAINER; exit 3; }}; "
            f"exec crictl exec \"$cid\" sh -c '{loop}'"]


def loop(read, out_path, stop_file, period):
    with open(out_path, "a") as out:
        while not os.path.exists(stop_file):
            started = time.monotonic()
            out.write(json.dumps(timed(read)) + "\n")
            out.flush()
            time.sleep(max(0.0, period - (time.monotonic() - started)))


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("kind", choices=("inventory", "nodes", "audit", "logs", "px4"))
    parser.add_argument("--out", required=True)
    parser.add_argument("--stop-file", required=True)
    parser.add_argument("--context", default="k3d-cloud-native-p2")
    parser.add_argument("--variant", choices=("a", "b"))
    parser.add_argument("--period", type=float, default=1.0)
    parser.add_argument("--deployment")
    parser.add_argument("--container", default="")
    parser.add_argument("--node")
    args = parser.parse_args(argv)
    if args.kind == "inventory":
        loop(lambda: read_inventory(args.context, args.variant), args.out, args.stop_file, args.period)
    elif args.kind == "nodes":
        loop(lambda: read_nodes(args.context), args.out, args.stop_file, args.period)
    elif args.kind == "audit":
        reader = AuditReader(args.context)
        loop(reader.read, args.out, args.stop_file, args.period)
    elif args.kind == "logs":
        with open(args.out, "a") as out:
            follow_logs(args.context, args.deployment, args.container, out, args.stop_file)
    else:
        with open(args.out, "a") as out:
            proc = subprocess.Popen(px4_command(args.node), stdout=out, stderr=subprocess.STDOUT)
            while proc.poll() is None and not os.path.exists(args.stop_file):
                time.sleep(STOP_POLL_SEC)
            stop_child(proc)
    return 0


if __name__ == "__main__":
    sys.exit(main())
