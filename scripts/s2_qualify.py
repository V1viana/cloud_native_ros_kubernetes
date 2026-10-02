#!/usr/bin/env python3
"""S2 bench qualification driver (R11, docs/R11_S2_PARTITION.md, "Qualificazione
live del banco" and the decisions on the 14 choices). One run per variant, on
the bench of the cells (same runner setup, same partition, same harness); its
evidence qualifies the bench and never enters the eight cells.

  Q1 facts         the peers, the k3s version and arguments, the tolerations of
                   drone01's Pods (the effective eviction threshold must exceed the
                   guard's 120 s), the node's lease, free space for the artifacts;
  (reference)      clocks, metrics, arm, 30 s reference, nominal state, PX4 frozen
                   -- the cells' own steps (s2_phase.Phase);
  Q2 guard cycle   a guard of 10 s (in guard-cycle/, apart from the real guard's
                   markers), then a separate process applies the cut and holds it,
                   and is killed: the guard alone must remove every rule; the
                   connection back is probed; then a stabilization (node Ready,
                   lease renewing, drone01 health positive, local state nominal);
  (monitor load)   the cut's monitor alone for 30 s, no cut: its load on the
                   recorder's continuity against the reference;
  Q3 the cut       the real guard (120 s), DROP, the cut verified as in the cells,
                   counters read at the verification, 30 s after it and before the
                   restore; held 90 s from the first DROP; the rules and the paths
                   read every second by the monitor (the hold, apart from the
                   counters: a positive counter proves traffic intercepted only);
  Q4 inside it     the cells' pulse at +10 s from the verification: local metrics,
                   parameter services, PX4 go on; entry and return inside the cut;
  Q5 the restore   at first DROP + 90 s, bounded by the guard; then both ways:
                   probes from drone01's side, `kubectl exec` into the harness Pod
                   (API -> kubelet), node Ready, lease renewing, drone01 health;
                   held 180 s after the restore command, actions kept, not counted.
Judged by scripts/s2_qualify_judge.py. Exit: 0 completed, 3 interrupted,
4 aborted. Tested offline: operator/tests/test_s2_qualify.py.
"""

import argparse
from datetime import datetime
import importlib.util
import json
import math
import os
import shlex
import shutil
import signal
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))


def _load(name):
    spec = importlib.util.spec_from_file_location(name, os.path.join(HERE, f"{name}.py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


s2_phase = _load("s2_phase")
s2_partition = s2_phase.s2_partition
s2_control = s2_phase.s2_control
Aborted, Interrupted = s2_phase.Aborted, s2_phase.Interrupted

EXPECTED_PEERS = ("k3d-cloud-native-p2-agent-1", "k3d-cloud-native-p2-agent-2", "k3d-cloud-native-p2-agent-3",
                  "k3d-cloud-native-p2-server-0", "k3d-cloud-native-p2-serverlb")
UNREACHABLE, NOT_READY = "node.kubernetes.io/unreachable", "node.kubernetes.io/not-ready"


def _tolerated_sec(tolerations, taint):
    """How long a Pod tolerates a NoExecute taint: the largest tolerationSeconds of
    a matching toleration, inf with one that has none, 0 with none matching."""
    matching = [t for t in tolerations or []
                if (t.get("key") == taint or (t.get("key") in (None, "") and t.get("operator") == "Exists"))
                and t.get("effect") in (None, "", "NoExecute")]
    if not matching:
        return 0.0
    if any(t.get("tolerationSeconds") is None for t in matching):
        return float("inf")
    return float(max(t["tolerationSeconds"] for t in matching))


def eviction_threshold(tolerations_on_node):
    """The earliest a Pod of the partitioned node would be evicted. A partition
    leaves the node Ready=Unknown, tainted unreachable:NoExecute: that taint's
    tolerations decide (not-ready is for Ready=False, reported apart). None if
    the tolerations are unknown."""
    if tolerations_on_node is None:
        return None

    def shown(value):                       # JSON has no infinity: "forever"
        return "forever" if value == float("inf") else value
    unreachable = {pod: _tolerated_sec(t, UNREACHABLE) for pod, t in tolerations_on_node.items()}
    not_ready = {pod: _tolerated_sec(t, NOT_READY) for pod, t in tolerations_on_node.items()}
    lowest = min(unreachable.values()) if unreachable else None
    return {"min_sec": shown(lowest), "per_pod": {pod: shown(v) for pod, v in unreachable.items()},
            "not_ready_min_sec": shown(min(not_ready.values())) if not_ready else None}


def topology(facts, variant):
    """What a cell must match of its qualification, compared literally -- except the
    order of the k3s server's arguments, which k3d does not fix between creations
    (the third qualification): k3s_server_args is their sorted list, repetitions kept,
    so any argument added, removed or changed still differs; the raw line is kept."""
    cmdline = facts.get("k3s_server_cmdline")
    return {"variant": variant, "peers": sorted(p["name"] for p in facts.get("peers") or []),
            "server_version": facts.get("server_version"), "k3s_server_cmdline": cmdline,
            "k3s_server_args": None if cmdline is None else sorted(cmdline.split()),
            "roles": facts.get("roles"), "system_pins": facts.get("system_pins"),
            "system_placement": facts.get("system_placement")}


MONITOR_ARGS = ("node-monitor-grace-period", "node-monitor-period", "node-lease-duration", "node-status-update",
                "pod-eviction-timeout", "default-not-ready-toleration", "default-unreachable-toleration")
ROLE_LABELS = ("kuberos.io/role", "robot.kuberos.io/id")


def node_monitor_args(cmdline):
    """The node-monitoring arguments written in the k3s server command line, raw;
    None if the command line is unknown."""
    if cmdline is None:
        return None
    found = [arg for arg in cmdline.split() if any(name in arg for name in MONITOR_ARGS)]
    return found or "default (not explicit in the k3s server command line)"


QPARAMS = {**s2_phase.PARAMS, "cycle_guard_sec": 10, "cycle_wait_sec": 20.0, "stabilization_max_sec": 120.0,
           "lease_fresh_sec": 15.0, "health_fresh_sec": 10.0, "cut_sec": 90.0, "counters_at_sec": 30.0,
           "back_max_sec": 120.0, "monitor_load_sec": 30.0}
GUARD_CYCLE_DIR = "guard-cycle"


class Qualification:
    def __init__(self, phase, params=QPARAMS):
        self.phase, self.ops, self.p = phase, phase.ops, params

    def mark(self, name, **fields):
        return self.phase.mark(name, **fields)

    def _wait(self, predicate, seconds, name):
        """predicate() -> (ok, detail); polled until ok or the time is up."""
        deadline = self.ops.mono() + seconds
        while True:
            ok, detail = predicate()
            if ok or self.ops.mono() >= deadline:
                return self.mark(name, ok=ok, detail=detail)
            self.ops.truth_update()
            self.ops.sleep(self.p["poll_sec"])

    # -- Q2 --
    def guard_cycle(self):
        directory = os.path.join(self.ops.result_dir, GUARD_CYCLE_DIR)
        guard = self.ops.guard_start(self.p["cycle_guard_sec"], directory)
        self.mark("cycle_guard_started", pid=guard, alive=self.ops.guard_alive(guard, directory),
                  max_sec=self.p["cycle_guard_sec"])
        if not self.ops.guard_alive(guard, directory):
            raise Aborted("guard cycle: the guard is not running")
        def applied_now():
            status = self.ops.status()
            return bool(status.get("jumps")), status

        def removed_now():
            status = self.ops.status()
            return not status.get("jumps") and not status.get("chain"), status

        def connected_now():
            probes = self.ops.probe(self.phase.restore_targets)
            return bool(probes) and all(r.get("result") == "connected" for r in probes.values()), probes
        driver = self.ops.cut_driver_start()
        applied = self._wait(applied_now, 5.0, "cycle_applied")
        self.ops.kill(driver)
        self.mark("cycle_driver_killed", pid=driver)
        fired = self._wait(lambda: (self.ops.guard_fired(directory), None),
                           self.p["cycle_guard_sec"] + self.p["cycle_wait_sec"], "cycle_guard_fired")
        clean = self._wait(removed_now, 5.0, "cycle_clean")
        back = self._wait(connected_now, 20.0, "cycle_connected")
        if not (applied["ok"] and fired["ok"] and clean["ok"]):
            if not clean["ok"]:                     # never leave a cut behind: our own removal, recorded
                self.ops.remove(timeout=30.0)
                self.mark("cycle_emergency_remove", status=self.ops.status())
            raise Aborted("guard cycle failed: the guard did not remove the rules on its own")
        return back

    def stabilize(self):
        def ready():
            node = self.ops.node_state()
            health = self.ops.latest_health("drone01-onboard")
            ok = (node.get("ready") == "True" and node.get("lease_age_sec") is not None
                  and node["lease_age_sec"] <= self.p["lease_fresh_sec"] and health is not None
                  and health.get("result") == "positive"
                  and self.ops.utc() - health.get("outcome_utc", 0) <= self.p["health_fresh_sec"])
            return ok, {"node": node, "health": health}
        stable = self._wait(ready, self.p["stabilization_max_sec"], "stabilized")
        if not stable["ok"]:
            raise Aborted("no stabilization after the guard cycle")
        self.phase.wait_nominal()

    def monitor_load(self):
        """The cut's monitor alone, without a cut, for monitor_load_sec: its load on
        drone01's node read against the reference in the recorder's continuity
        (decision after the fifth qualification: a 250m limit does not guarantee no
        perturbation). Its reads cross no cut: outside the cut's window."""
        start = self.mark("monitor_load_start", **self.ops.monitor_start())
        self.phase.hold_until(start["mono"] + self.p["monitor_load_sec"])
        self.ops.monitor_join()
        self.mark("monitor_load_end", truth=self.ops.truth_summary())
        self.phase.wait_nominal()

    # -- Q3 to Q5 --
    def main_cut(self):
        verified = self.phase.cut()
        self.mark("cut_counters", at="verified", status=self.ops.status())
        self.phase.pulse(verified["utc"] + self.p["pulse_offset_sec"])
        first_drop = self.phase.first_drop["mono"]
        self.phase.hold_until(verified["mono"] + self.p["counters_at_sec"])
        self.mark("cut_counters", at=f"verified+{self.p['counters_at_sec']:.0f}s", status=self.ops.status())
        self.phase.hold_until(first_drop + self.p["cut_sec"])
        end = self.phase.restore(f"end of the {self.p['cut_sec']:.0f} s qualification cut")
        self._wait(lambda: self.ops.exec_check(), self.p["back_max_sec"], "back_exec")

        def node_back():
            node = self.ops.node_state()
            return (node.get("ready") == "True" and node.get("lease_age_sec") is not None
                    and node["lease_age_sec"] <= self.p["lease_fresh_sec"]), node
        self._wait(node_back, self.p["back_max_sec"], "back_node")

        def health_back():
            """The first positive answer, with drone01/onboard's identity, to a call
            started after the removal -- from the history, not the latest answer
            (decision 2 after the first round: a late migration may follow it)."""
            for health in self.ops.health_since("drone01-onboard", end["mono"]):
                answer = health.get("answer") or {}
                if health.get("result") == "positive" and health.get("sent_mono", -1) > end["mono"] \
                        and (answer.get("robot_id"), answer.get("instance_id")) == ("drone01", "onboard"):
                    return True, health
            return False, None
        self._wait(health_back, self.p["back_max_sec"], "back_health")
        return end

    def run(self):
        status = "completed"
        phase = self.phase
        try:
            self.mark("clock_before", clocks=self.ops.clock())
            self.mark("facts", facts=self.ops.facts())
            phase.wait_metrics()
            phase.arm()
            reference = self.mark("reference_start")
            phase.hold_until(reference["mono"] + self.p["reference_sec"])
            self.mark("reference_end", truth=self.ops.truth_summary())
            phase.wait_nominal()
            self.mark("px4_frozen", states=self.ops.freeze_px4())
            self.mark("phase_start", case="qualification", params=self.p)
            self.guard_cycle()
            self.stabilize()
            self.monitor_load()
            end = self.main_cut()
            horizon = end["mono"] + self.p["horizon_sec"]
            self.mark("horizon_fixed", horizon_mono=horizon, basis="end of the restore command")
            phase.hold_until(horizon)
            self.mark("horizon_end", truth=self.ops.truth_summary())
            self.mark("clock_after", clocks=self.ops.clock())
            self.mark("peers_end", peers=self.ops.peers())
        except Interrupted as exc:
            status = "interrupted"
            self.mark("interrupted", reason=str(exc))
        except Aborted as exc:
            status = "aborted"
            self.mark("aborted", reason=str(exc))
        except Exception as exc:  # noqa: BLE001 -- recorded; the rules still go
            status = "interrupted"
            self.mark("driver_error", error=f"{type(exc).__name__}: {exc}"[:300])
        finally:
            if phase.applied and not phase.restored_clean:
                try:
                    phase.restore("cleanup")
                except Exception as exc:  # noqa: BLE001
                    status = "interrupted"
                    self.mark("cleanup_failed", error=f"{type(exc).__name__}: {exc}"[:300])
            self.mark("driver_end", status=status)
        return status


# ---- the live side effects beyond the cells' -----------------------------------

class QualifyOps(s2_phase.LiveOps):
    def __init__(self, args, start_copier=True):
        super().__init__(args, start_copier=start_copier)
        self.context, self.edge_node, self.server = args.context, args.edge_node, args.server
        self.harness_pod = args.harness_pod

    def cut_driver_start(self):
        """A process of its own that applies the cut and holds it -- the one the
        guard cycle kills, as if the runner died. Started detached like the guard."""
        directory = os.path.join(self.result_dir, GUARD_CYCLE_DIR)
        path = os.path.join(directory, "cut-driver.sh")
        with open(path, "w") as h:
            h.write("#!/bin/sh\n"
                    f"docker exec {shlex.quote(self.a.node)} sh -c {shlex.quote(s2_partition.apply_script(self._peers))}\n"
                    "echo applied\nexec sleep 600\n")
        started = subprocess.run(
            ["sh", "-c", 'setsid sh "$1" >>"$2" 2>&1 </dev/null & echo $!', "cut-driver", path,
             os.path.join(directory, "cut-driver.log")],
            capture_output=True, text=True, timeout=10)
        return int(started.stdout.strip())

    def kill(self, pid):
        try:
            os.kill(pid, signal.SIGKILL)
        except OSError:
            pass

    def node_state(self):
        rc, out, err = s2_control.run(["kubectl", "--context", self.context, "get", "node", self.a.node, "-o",
                                       "json"], timeout=5)
        state = {"ready": None, "lease_age_sec": None}
        try:
            node = json.loads(out)
            ready = next(c for c in node["status"]["conditions"] if c["type"] == "Ready")
            state.update(ready=ready["status"], reason=ready.get("reason"))
        except (ValueError, KeyError, StopIteration):
            state["error"] = (err or out)[:200]
        rc, out, err = s2_control.run(["kubectl", "--context", self.context, "-n", "kube-node-lease", "get", "lease",
                                       self.a.node, "-o", "jsonpath={.spec.renewTime}"], timeout=5)
        renew = _parse_iso(out.strip()) if rc == 0 else None
        if renew is not None:
            state["lease_age_sec"] = round(time.time() - renew, 3)
        return state

    def latest_health(self, target):
        rc, out, _ = s2_control.run(["docker", "exec", self.edge_node, "sh", "-c",
                                     "tail -n 200 /var/lib/s2-prober/health.jsonl"], timeout=5)
        found = None
        for line in out.splitlines():
            try:
                record = json.loads(line)
            except ValueError:
                continue
            if record.get("event") == "health" and record.get("target") == target:
                found = record
        return found

    def health_since(self, target, mono):
        rc, out, _ = s2_control.run(["docker", "exec", self.edge_node, "sh", "-c",
                                     "tail -n 800 /var/lib/s2-prober/health.jsonl"], timeout=5)
        found = []
        for line in out.splitlines():
            try:
                record = json.loads(line)
            except ValueError:
                continue
            if record.get("event") == "health" and record.get("target") == target \
                    and record.get("sent_mono", -math.inf) > mono:
                found.append(record)
        return found

    def exec_check(self):
        rc, out, err = s2_control.run(["kubectl", "--context", self.context, "-n", "cloud-native-p2", "exec",
                                       self.harness_pod, "--", "true"], timeout=10)
        return rc == 0, (err or out).strip()[:200]

    def facts(self):
        return collect_facts(self.context, self.a.node, self.server, self._peers, self.result_dir)


def collect_facts(context, node, server, peer_list, result_dir, runner=None):
    """Q1's facts; the same in the cells, whose topology must be the qualified one."""
    run = runner or s2_control.run
    out = {"peers": peer_list, "free_bytes": shutil.disk_usage(result_dir).free}
    rc, cmdline, _ = run(["docker", "exec", server, "sh", "-c", 'tr "\\0" " " </proc/1/cmdline'], timeout=10)
    out["k3s_server_cmdline"] = cmdline.strip() if rc == 0 else None
    rc, version, _ = run(["kubectl", "--context", context, "version", "-o", "json"], timeout=10)
    try:
        out["server_version"] = json.loads(version)["serverVersion"]["gitVersion"]
    except (ValueError, KeyError, TypeError):
        out["server_version"] = None
    rc, pods, _ = run(["kubectl", "--context", context, "get", "pods", "-A", "-o", "json",
                       "--field-selector", f"spec.nodeName={node}"], timeout=10)
    tolerations = {}
    try:
        for pod in json.loads(pods)["items"]:
            key = f"{pod['metadata']['namespace']}/{pod['metadata']['name']}"
            tolerations[key] = [{k: t.get(k) for k in ("key", "operator", "effect", "tolerationSeconds")}
                                for t in pod["spec"].get("tolerations") or []]
    except (ValueError, KeyError, TypeError):
        tolerations = None
    out["tolerations_on_node"] = tolerations
    # the raw monitoring parameters and the roles (decisions on points 2 and 3)
    out["node_monitor_args"] = node_monitor_args(out["k3s_server_cmdline"])
    rc, lease, _ = run(["kubectl", "--context", context, "-n", "kube-node-lease", "get", "lease", node, "-o", "json"],
                       timeout=10)
    try:
        spec = json.loads(lease)["spec"]
        out["node_lease"] = {k: spec.get(k) for k in ("leaseDurationSeconds", "holderIdentity")}
    except (ValueError, KeyError, TypeError):
        out["node_lease"] = None
    rc, nodes, _ = run(["kubectl", "--context", context, "get", "nodes", "-o", "json"], timeout=10)
    try:
        out["roles"] = {n["metadata"]["name"]: {k: v for k, v in (n["metadata"].get("labels") or {}).items()
                                                if k in ROLE_LABELS}
                        for n in json.loads(nodes)["items"]}
    except (ValueError, KeyError, TypeError):
        out["roles"] = None
    # k3s's system services: their nodeSelector (the constraint off the isolated node)
    # and the nodes their running Pods are on (decision after the second eight-cell round)
    rc, deployments, _ = run(["kubectl", "--context", context, "-n", "kube-system", "get", "deployments", "-o", "json"],
                             timeout=10)
    try:
        out["system_pins"] = {d["metadata"]["name"]: (d["spec"]["template"]["spec"].get("nodeSelector") or {})
                              for d in json.loads(deployments)["items"]
                              if d["metadata"]["name"] in s2_control.SYSTEM_DEPLOYMENTS}
    except (ValueError, KeyError, TypeError):
        out["system_pins"] = None
    rc, pods, _ = run(["kubectl", "--context", context, "get", "pods", "-A", "-o", "json"], timeout=10)
    try:
        out["system_placement"] = s2_control.placement(json.loads(pods)["items"], node)["system"]
    except (ValueError, KeyError, TypeError):
        out["system_placement"] = None
    return out


def _parse_iso(text):
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def facts_main(argv):
    """`facts ...`: the facts and the topology of a cell, for its judge."""
    parser = argparse.ArgumentParser(prog="s2_qualify.py facts")
    for name in ("--node", "--server", "--context", "--peers", "--result-dir", "--variant", "--out"):
        parser.add_argument(name, required=True)
    args = parser.parse_args(argv)
    facts = collect_facts(args.context, args.node, args.server, json.loads(args.peers), args.result_dir)
    facts["eviction_threshold"] = eviction_threshold(facts.get("tolerations_on_node"))
    facts["topology"] = topology(facts, args.variant)
    with open(args.out, "w") as h:
        json.dump(facts, h, indent=1)
    return 0


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    if argv[:1] == ["facts"]:
        return facts_main(argv[1:])
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--result-dir", required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--node", required=True, help="drone01's node container")
    parser.add_argument("--edge-node", required=True)
    parser.add_argument("--server", required=True)
    parser.add_argument("--context", required=True)
    parser.add_argument("--control-dir", default="/var/lib/s2-harness")
    parser.add_argument("--harness-cid", required=True)
    parser.add_argument("--harness-pod", required=True)
    parser.add_argument("--target-node", required=True)
    parser.add_argument("--peers", required=True)
    parser.add_argument("--cut-targets", nargs="+", required=True)
    parser.add_argument("--hold-targets", nargs="+", required=True)
    parser.add_argument("--clock-targets", required=True)
    parser.add_argument("--px4-files", required=True)
    args = parser.parse_args(argv)
    marks = s2_phase.Marks(os.path.join(args.result_dir, "phases.jsonl"))
    ops = QualifyOps(args)
    # the cells' steps (arm, reference, cut, pulse, restore); its own case is not used
    phase = s2_phase.Phase(ops, "short", args.run_id, args.target_node, json.loads(args.peers), args.cut_targets,
                           args.cut_targets, marks)
    qualification = Qualification(phase)

    def stop(signum, _frame):
        raise Interrupted(f"signal {signum}")
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    try:
        return s2_phase.EXIT[qualification.run()]
    finally:
        ops.close()
        marks.close()


if __name__ == "__main__":
    sys.exit(main())
