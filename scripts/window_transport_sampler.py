#!/usr/bin/env python3
"""Samplers of the D6 live check (R11, docs/R11_S2_PARTITION.md, section D6).

One process per kind, until a stop file appears; one JSON line per read, with
the host's monotonic clock (m0/m1) and UTC epoch (w0/w1) at its start and end,
and the error of a failed read kept (a gap, never a zero):
  status  the target ROSModule and AdaptationPolicy through the API server
          (nominal period 1 s): windows received centrally, lifecycle, policy
          state, cursor, conditions, generations;
  memory  the state-bridge container's memory.workingSetBytes through crictl
          on the node (docker exec: independent of the API; nominal 1 s);
  node    the node's Ready condition and taints through the API (nominal 2 s);
  cpu     the bridge's CPU cgroup, read on the node (cpu.max and cpu.stat:
          usage, periods, throttled periods and time; nominal 1 s) -- the tick
          diagnosis (docs/R11_S2_PARTITION.md);
  host    the host's /proc/loadavg and the cpu line of /proc/stat (nominal 1 s);
  trace   the bridge's local trace, copied out while the run goes on (nominal
          1 s): from the node, through /proc/<pid>/root of every running
          container with that name, only up to the last complete line, one file
          per container (trace-<cid>.jsonl) -- no process is started in the
          bridge's cgroup, and a stop or a restart does not erase what was
          already copied (Viviana, 2026-09-26, after 20260926T165041Z-post).
Used by scripts/run_window_transport_check.sh; judged by
scripts/window_transport_judge.py. Tested offline:
operator/tests/test_window_transport_judge.py.
"""

import argparse
import json
import os
import subprocess
import sys
import time

CALL_TIMEOUT_SEC = 5.0


def run(cmd, timeout=CALL_TIMEOUT_SEC):
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return p.returncode, p.stdout, p.stderr
    except subprocess.TimeoutExpired:
        return None, "", "sampler timeout"


def summarize_status(items):
    out = {}
    for item in items:
        kind, status, spec = item.get("kind"), item.get("status") or {}, item.get("spec") or {}
        generation = item.get("metadata", {}).get("generation")
        if kind == "ROSModule":
            out["module"] = {
                "generation": generation, "observedLifecycleState": status.get("observedLifecycleState"),
                "placement": spec.get("placement"), "rosParamMap": spec.get("rosParamMap"),
                "metricWindows": {k: {"windowSec": (v or {}).get("windowSec"),
                                      "windows": [{f: w.get(f) for f in ("seq", "start", "end", "samples", "p95Ms")}
                                                  for w in (v or {}).get("windows") or []]}
                                  for k, v in (status.get("metricWindows") or {}).items()}}
        elif kind == "AdaptationPolicy":
            out["policy"] = {
                "generation": generation, "state": status.get("state"),
                "windowCursor": status.get("windowCursor"), "correlationId": status.get("correlationId"),
                "edgeModuleName": status.get("edgeModuleName"),
                "conditions": [{f: c.get(f) for f in ("type", "status", "reason")}
                               for c in status.get("conditions") or []]}
    return out


def summarize_memory(stdout):
    stats = json.loads(stdout)["stats"]
    memory = stats[0]["memory"]
    return {"workingSetBytes": int(memory["workingSetBytes"]["value"]),
            "statsTimestamp": memory.get("timestamp")}


def summarize_cpu(stdout):
    lines = stdout.strip().splitlines()
    quota, period = lines[0].split()
    stat = dict(line.split() for line in lines[1:] if len(line.split()) == 2)
    out = {"cpu_max_quota": None if quota == "max" else int(quota), "cpu_max_period": int(period)}
    for key in ("usage_usec", "user_usec", "system_usec", "nr_periods", "nr_throttled", "throttled_usec"):
        out[key] = int(stat[key]) if key in stat else None
    return out


def read_host():
    with open("/proc/loadavg") as h:
        load = h.read().split()
    with open("/proc/stat") as h:
        cpu = next(line for line in h if line.startswith("cpu "))
    return {"load1": float(load[0]), "runnable": load[3], "cpu_jiffies": [int(x) for x in cpu.split()[1:]]}


def summarize_node(stdout):
    node = json.loads(stdout)
    ready = next((c for c in node.get("status", {}).get("conditions") or [] if c.get("type") == "Ready"), {})
    return {"ready": ready.get("status"), "readyReason": ready.get("reason"),
            "readySince": ready.get("lastTransitionTime"),
            "taints": [{"key": t.get("key"), "effect": t.get("effect")}
                       for t in node.get("spec", {}).get("taints") or []]}


class TraceStreamer:
    def __init__(self, node, name, path, out_dir, run=run):
        self.node, self.name, self.path, self.out_dir, self.run = node, name, path, out_dir, run
        self.offsets = {}

    def list_script(self):
        return (f"for c in $(crictl ps --name {self.name} -q); do "
                f"p=$(crictl inspect --output go-template --template '{{{{.info.pid}}}}' $c); "
                f"s=$(stat -c %s /proc/$p/root{self.path} 2>/dev/null || echo -1); echo $c $p $s; done")

    def step(self):
        record = {"kind": "trace", "m0": time.monotonic(), "w0": time.time(), "containers": {}}
        rc, out, err = self.run(["docker", "exec", self.node, "sh", "-c", self.list_script()])
        if rc != 0:
            record["error"] = (err or out or f"rc {rc}").strip()[:300]
        for line in out.splitlines() if rc == 0 else []:
            try:
                cid, pid, size = line.split()
                pid, size = int(pid), int(size)
            except ValueError:
                continue
            offset = self.offsets.get(cid, 0)
            entry = {"pid": pid, "size": size, "copied": 0}
            if size > offset:
                rc2, data, err2 = self.run(["docker", "exec", self.node, "sh", "-c",
                                            f"tail -c +{offset + 1} /proc/{pid}/root{self.path} | head -c {size - offset}"])
                end = data.rfind("\n") + 1 if rc2 == 0 else 0
                if end:
                    with open(f"{self.out_dir}/trace-{cid[:12]}.jsonl", "a") as handle:
                        handle.write(data[:end])
                    self.offsets[cid] = offset + len(data[:end].encode())
                    entry["copied"] = len(data[:end].encode())
                elif rc2 != 0:
                    entry["error"] = (err2 or f"rc {rc2}").strip()[:200]
            entry["offset"] = self.offsets.get(cid, 0)
            record["containers"][cid[:12]] = entry
        record.update(m1=time.monotonic(), w1=time.time())
        return record


def read_once(args):
    if args.kind == "status":
        cmd = ["kubectl", "--context", args.context, "-n", args.namespace, "get",
               f"rosmodule/{args.module}", f"adaptationpolicy/{args.policy}", "-o", "json"]
        parse = lambda out: summarize_status(json.loads(out)["items"])  # noqa: E731
    elif args.kind == "host":
        record = {"kind": "host", "m0": time.monotonic(), "w0": time.time()}
        try:
            record.update(read_host())
        except (OSError, ValueError, StopIteration) as exc:
            record["error"] = str(exc)[:300]
        record.update(m1=time.monotonic(), w1=time.time())
        return record
    elif args.kind == "cpu":
        cmd = ["docker", "exec", args.node_container, "sh", "-c",
               f"cat {args.cgroup_dir}/cpu.max {args.cgroup_dir}/cpu.stat"]
        parse = summarize_cpu
    elif args.kind == "memory":
        cmd = ["docker", "exec", args.node_container, "crictl", "stats", "--id", args.container_id,
               "-o", "json"]
        parse = summarize_memory
    else:
        cmd = ["kubectl", "--context", args.context, "get", "node", args.node, "-o", "json"]
        parse = summarize_node
    record = {"kind": args.kind, "m0": time.monotonic(), "w0": time.time()}
    rc, out, err = run(cmd)
    record.update(m1=time.monotonic(), w1=time.time())
    try:
        if rc != 0:
            raise ValueError((err or out or f"rc {rc}").strip()[:300])
        record.update(parse(out))
    except (ValueError, KeyError, IndexError, TypeError) as exc:
        record["error"] = str(exc)[:300]
    return record


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("kind", choices=("status", "memory", "node", "trace", "cpu", "host"))
    parser.add_argument("--out", required=True)
    parser.add_argument("--stop-file", required=True)
    parser.add_argument("--period", type=float, default=1.0)
    parser.add_argument("--context", default="")
    parser.add_argument("--namespace", default="")
    parser.add_argument("--module", default="")
    parser.add_argument("--policy", default="")
    parser.add_argument("--node", default="")
    parser.add_argument("--node-container", default="")
    parser.add_argument("--container-id", default="")
    parser.add_argument("--container-name", default="state-bridge")
    parser.add_argument("--cgroup-dir", default="")
    parser.add_argument("--path", default="/tmp/window-trace.jsonl")
    parser.add_argument("--out-dir", default="")
    args = parser.parse_args(argv)
    streamer = (TraceStreamer(args.node_container, args.container_name, args.path, args.out_dir)
                if args.kind == "trace" else None)
    read = streamer.step if streamer else (lambda: read_once(args))
    with open(args.out, "a") as out:
        while not os.path.exists(args.stop_file):
            started = time.monotonic()
            out.write(json.dumps(read()) + "\n")
            out.flush()
            time.sleep(max(0.0, args.period - (time.monotonic() - started)))
        if streamer:                       # the tail written after the last read
            out.write(json.dumps(read()) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
