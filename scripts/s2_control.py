#!/usr/bin/env python3
"""S2 runner side of the harness channel (R11, contract s2-partition-v1).

From the host, through `docker exec` on drone01's node container -- the channel
the partition does not cut:
  clean(node, dir)               the control directory emptied before the harness starts;
  write_command(node, dir, ...)  a command written to a temporary file and renamed
                                 into place in the same directory: the harness never
                                 reads half a file;
  ProgressiveReader              a JSONL file of the harness read from an offset, only
                                 up to its last complete line, the bytes kept in a local
                                 copy as they come;
  LocalTruth                     the ground truth fed from the copied samples: only
                                 robot_id drone01 and component companion-analytics-
                                 onboard (in A the edge instance publishes on the same
                                 topic), every available line fed before advancing to
                                 the last sample -- never to the host's clock.
  alive_pods / single_alive      the Pods of an app with a running container, terminating
                                 ones included: one dispatcher really alive;
  placement                      every Pod's node, k3s's system services by node, and the
                                 Pods other than drone01's on the isolated node;
  clock_readings / clock_summary  the host/container alignment, before and after the
                                 phase: in each process the monotonic clock is read
                                 between two UTC reads (a bounded reading, microseconds
                                 wide), so the monotonic offset follows from the two
                                 relations, UTC minus monotonic, whatever the docker
                                 exec lasts; the exec's own bracket only checks that
                                 UTC is shared. Several rounds, intervals intersected.
                                 Valid where host and containers share one kernel (the
                                 k3d bench verified); not to be carried over implicitly
                                 to two VMs.
Tested offline: operator/tests/test_s2_control.py.
"""

import importlib.util
import json
import os
import shlex
import subprocess
import time

ROBOT, COMPONENT = "drone01", "companion-analytics-onboard"
_gt_spec = importlib.util.spec_from_file_location("s2_ground_truth",
                                                  os.path.join(os.path.dirname(__file__), "s2_ground_truth.py"))
s2_ground_truth = importlib.util.module_from_spec(_gt_spec)
_gt_spec.loader.exec_module(s2_ground_truth)


def run(cmd, input_text=None, timeout=15):
    try:
        p = subprocess.run(cmd, input=input_text, capture_output=True, text=True, timeout=timeout)
        return p.returncode, p.stdout, p.stderr
    except subprocess.TimeoutExpired:
        return None, "", "timeout"


class ControlError(RuntimeError):
    pass


def clean(node, directory, runner=run):
    d = shlex.quote(directory)
    rc, _, err = runner(["docker", "exec", node, "sh", "-c", f"mkdir -p {d} && find {d} -mindepth 1 -delete"])
    if rc != 0:
        raise ControlError(f"cannot clean {directory} on {node}: {err.strip()[:200]}")


def write_command(node, directory, name, command, runner=run):
    if name not in ("arm.json", "pulse.json"):
        raise ControlError(f"unknown command file {name}")
    target = os.path.join(directory, name)
    tmp = os.path.join(directory, f".{name}.tmp")
    script = f"cat > {shlex.quote(tmp)} && mv -f {shlex.quote(tmp)} {shlex.quote(target)}"
    rc, _, err = runner(["docker", "exec", "-i", node, "sh", "-c", script], input_text=json.dumps(command))
    if rc != 0:
        raise ControlError(f"cannot write {name} on {node}: {err.strip()[:200]}")


class ProgressiveReader:
    def __init__(self, node, path, local_copy, runner=run):
        self.node, self.path, self.local = node, path, local_copy
        self.offset = 0
        self.errors = []
        self._runner = runner

    def read(self):
        """New complete lines as parsed records (a line that is not JSON is kept as
        {"unparsed": ...}); a failed read is recorded, returns nothing, and is
        retried from the same offset next time."""
        rc, data, err = self._runner(["docker", "exec", self.node, "sh", "-c",
                                      f"tail -c +{self.offset + 1} {shlex.quote(self.path)} 2>&1 || true"])
        if rc != 0:
            self.errors.append((err or f"rc {rc}").strip()[:200])
            return []
        if data.startswith("tail:"):
            self.errors.append(data.strip()[:200])
            return []
        end = data.rfind("\n") + 1
        if not end:
            return []
        chunk = data[:end]
        with open(self.local, "a") as handle:
            handle.write(chunk)
        self.offset += len(chunk.encode())
        out = []
        for line in chunk.splitlines():
            if not line.strip():
                continue
            try:
                out.append(json.loads(line))
            except ValueError:
                out.append({"unparsed": line[:200]})
        return out


class LocalTruth:
    def __init__(self, reader):
        self.reader = reader
        self.truth = s2_ground_truth.GroundTruth()
        self.other_samples = 0            # the edge instance, other robots: kept in the copy, not fed
        self.samples = 0

    def update(self):
        fed = False
        for record in self.reader.read():
            if record.get("event") != "sample":
                continue
            if record.get("robot_id") != ROBOT or record.get("component") != COMPONENT:
                self.other_samples += 1
                continue
            self.truth.feed(record)
            self.samples += 1
            fed = True
        if fed and self.truth.last_sample is not None:
            self.truth.advance(self.truth.last_sample)
        return self.truth.episodes


# ---- clock alignment (contract s2-partition-v1: bounded readings, at most 100 ms) ----

CLOCK_LIMIT_SEC = 0.1
CLOCK_READ = "import time;u0=time.time();m=time.monotonic();u1=time.time();print(repr(u0),repr(m),repr(u1))"


def bounded_read(utc=time.time, mono=time.monotonic):
    u0, m, u1 = utc(), mono(), utc()
    return {"u0": u0, "m": m, "u1": u1}


def clock_readings(node, container_id, rounds=5, runner=run, local=bounded_read):
    """Host bounded read, the container's (through crictl on its node), host again."""
    out = []
    for _ in range(rounds):
        before = local()
        rc, stdout, err = runner(["docker", "exec", node, "crictl", "exec", container_id, "python3", "-c",
                                  CLOCK_READ])
        after = local()
        record = {"host_before": before, "host_after": after}
        try:
            if rc != 0:
                raise ValueError((err or f"rc {rc}").strip()[:200])
            u0, m, u1 = (float(v) for v in stdout.split()[-3:])
            record["container"] = {"u0": u0, "m": m, "u1": u1}
        except ValueError as exc:
            record["error"] = str(exc)[:200]
        out.append(record)
    return out


def clock_summary(readings, limit=CLOCK_LIMIT_SEC, min_rounds=3):
    """The container's monotonic offset from the host's, as an interval.
    With UTC shared (one kernel), u = host_mono + Rh = container_mono + Rc, so
    offset = Rh - Rc; each relation is known to its bounded read's width, the
    host's taken on both sides of the exec (a slew in between widens it)."""
    lo, hi, realtime_in_bracket, ok = float("-inf"), float("inf"), True, 0
    errors = [r["error"] for r in readings if "error" in r]
    for r in readings:
        if "error" in r:
            continue
        ok += 1
        hb, ha, c = r["host_before"], r["host_after"], r["container"]
        rh_lo = min(hb["u0"] - hb["m"], ha["u0"] - ha["m"])
        rh_hi = max(hb["u1"] - hb["m"], ha["u1"] - ha["m"])
        rc_lo, rc_hi = c["u0"] - c["m"], c["u1"] - c["m"]
        lo, hi = max(lo, rh_lo - rc_hi), min(hi, rh_hi - rc_lo)
        if not (hb["u0"] <= c["u0"] <= ha["u1"] and hb["u0"] <= c["u1"] <= ha["u1"]):
            realtime_in_bracket = False
    consistent = ok > 0 and lo <= hi
    bound = max(abs(lo), abs(hi)) if consistent else None
    return {"rounds": len(readings), "ok_rounds": ok, "errors": errors,
            "mono_offset_interval": [lo, hi] if consistent else None, "mono_offset_bound": bound,
            "realtime_in_bracket": realtime_in_bracket, "limit": limit,
            "within_limit": bool(consistent and ok >= min_rounds and realtime_in_bracket and bound <= limit)}


# ---- one process really alive (decision 4 after the first qualification round) ----

def alive_pods(items, app_name):
    """The app's Pods with a container in the running state -- a terminating Pod
    (deletionTimestamp) whose container still runs counts: Ready replicas alone do
    not prove that a single DDS process is alive."""
    out = []
    for pod in items:
        meta = pod.get("metadata") or {}
        if (meta.get("labels") or {}).get("app.kubernetes.io/name") != app_name:
            continue
        running = [c.get("name") for c in (pod.get("status") or {}).get("containerStatuses") or []
                   if "running" in (c.get("state") or {})]
        if not running:
            continue
        trace = None
        for container in (pod.get("spec") or {}).get("containers") or []:
            for env in container.get("env") or []:
                if env.get("name") == "OPERATIONAL_EVENT_TRACE":
                    trace = env.get("value")
        out.append({"name": meta.get("name"), "uid": meta.get("uid"), "terminating": "deletionTimestamp" in meta,
                    "running": running, "trace": trace})
    return out


def single_alive(alive, trace=None):
    """Exactly one alive, not terminating, with the expected configuration."""
    return len(alive) == 1 and not alive[0]["terminating"] and (trace is None or alive[0]["trace"] == trace)


# ---- the partition's perimeter (decision after the second eight-cell round) ----

SYSTEM_DEPLOYMENTS = ("coredns", "metrics-server", "local-path-provisioner")   # kube-system, k3s's own
ROBOT_LABELS = ("pod-name", "app.kubernetes.io/name", "dronekube.io/owned-by-rosmodule", "robot")


def _running(pod):
    return any("running" in (c.get("state") or {}) for c in (pod.get("status") or {}).get("containerStatuses") or [])


def _drone01s(pod):
    meta = pod.get("metadata") or {}
    values = [str((meta.get("labels") or {}).get(k, "")) for k in ROBOT_LABELS] + [meta.get("name") or ""]
    return any("drone01" in v for v in values)


def placement(items, isolated_node):
    """Every Pod's node; the k3s system Deployments' Pods by node; and the foreign
    Pods on the isolated node -- any not drone01's own with a container still running
    (a terminating one counts): the partition must cut drone01's workloads only."""
    pods, system, foreign = [], {}, []
    for pod in items:
        meta, node = pod.get("metadata") or {}, (pod.get("spec") or {}).get("nodeName")
        entry = {"namespace": meta.get("namespace"), "name": meta.get("name"), "uid": meta.get("uid"), "node": node,
                 "terminating": "deletionTimestamp" in meta, "running": _running(pod)}
        pods.append(entry)
        if meta.get("namespace") == "kube-system":
            prefix = next((d for d in SYSTEM_DEPLOYMENTS if (meta.get("name") or "").startswith(d + "-")), None)
            if prefix and entry["running"]:
                system.setdefault(prefix, []).append(node)
        if node == isolated_node and entry["running"] and not _drone01s(pod):
            foreign.append(entry)
    return {"foreign": foreign, "system": {k: sorted(v) for k, v in sorted(system.items())}, "pods": pods}


def main(argv=None):
    import argparse
    import sys
    parser = argparse.ArgumentParser(description="alive --app NAME [--trace V] < pods.json | placement --node N < pods.json")
    sub = parser.add_subparsers(dest="cmd", required=True)
    alive = sub.add_parser("alive")
    alive.add_argument("--app", required=True)
    alive.add_argument("--trace")
    place = sub.add_parser("placement")
    place.add_argument("--node", required=True)
    args = parser.parse_args(argv)
    items = json.load(sys.stdin).get("items") or []
    if args.cmd == "placement":
        print(json.dumps(placement(items, args.node)))
        return 0
    found = alive_pods(items, args.app)
    print(json.dumps({"alive": found, "single": single_alive(found, args.trace)}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
