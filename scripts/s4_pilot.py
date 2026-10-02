#!/usr/bin/env python3
"""S4 edge-node pilot driver (R13, docs/R13_S4_EDGE_PILOT_PROTOCOL.md).

  nominal      drone01-04 onboard health positive (the prober on the control plane);
  preparation  drone03 on the edge by the P2 migration, recorded apart: its onboard
               processing_delay_ms set to 300.0 (read back), the edge health positive,
               the parameter back to 80.0 on the onboard, the action terminal (A: the
               dispatcher's incident completed; B: the policy Recovered), then 30 s
               stable -- every edge answer positive, every onboard answer 'inactive',
               no pending action. Budget 300 s from its start, else NOT_STARTED;
  pre-T0       nothing but drone03's edge instance on the edge node; container running,
               kubelet connected; the guard (120 s) seen alive; the sampler started;
  T0           `docker stop --time 0` of the edge node; its start observed outside the
               cluster (container stopped AND kubelet not connected);
  T0 + 90 s    `docker start`, the restored marker; the node's return observed;
  horizon      180 s after the start; nothing assumed about recovery.
guard-test: a 20 s guard, the stop, then the driver kills itself (SIGKILL): only the
guard can restart the node (the runner checks what it recorded).
Every mark carries the host's monotonic clock and UTC. Exit: 0 completed,
2 not started, 3 interrupted, 4 aborted.
Tested offline: operator/tests/test_s4_pilot.py.
"""

import argparse
import json
import os
import re
import signal
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import s4_edge  # noqa: E402
from s2_phase import Marks  # noqa: E402
import s2_control  # noqa: E402

PARAMS = {"nominal_timeout_sec": 180.0, "fresh_sec": 5.0, "prep_budget_sec": 300.0, "stable_sec": 30.0,
          "gap_sec": 3.0, "high_ms": 300.0, "nominal_ms": 80.0, "guard_sec": 120, "stop_sec": 90.0,
          "down_wait_sec": 5.0, "up_wait_sec": 120.0, "horizon_sec": 180.0, "poll_sec": 0.5,
          "guard_test_sec": 20}
EXIT = {"completed": 0, "not_started": 2, "interrupted": 3, "aborted": 4}
ROBOTS = ("drone01", "drone02", "drone03", "drone04")
EDGE_ROBOT = "drone03"


class NotStarted(RuntimeError):
    pass


class Aborted(RuntimeError):
    pass


def positive(record):
    return record.get("result") == "positive"


def lifecycle(record):
    return ((record.get("answer") or {}).get("lifecycle_state") or "").lower()


def pending_incidents(log_text, robot=EDGE_ROBOT):
    """A: accepted requests for the robot's SLO incidents minus the completed ones,
    and the completions (id -> outcome)."""
    accepted = set(re.findall(rf"DeploymentRequest accepted for ({robot}-AnalyticsLatencySLO-\S+)", log_text))
    completed = dict(re.findall(rf"Incident ({robot}-AnalyticsLatencySLO-\S+) completed as ([^\r\n(]+\([A-Z_]+\))",
                                log_text))
    return sorted(accepted - set(completed)), {k: v.strip() for k, v in completed.items()}


def action_state(variant, observation):
    """success | failure | pending | none, from A's dispatcher log or B's policy state."""
    if variant == "a":
        pending, completed = pending_incidents(observation or "")
        if pending:
            return "pending"
        if not completed:
            return "none"
        return "success" if list(completed.values())[-1] == "analytics_slo_recovered (STABLE)" else "failure"
    state = (observation or "").strip()
    if state == "Recovered":
        return "success"
    if state in ("RolledBack", "Escalated", "FallbackFailed"):
        return "failure"
    return "pending" if state else "none"


def parse_pidof(stdout):
    return [int(x) for x in stdout.split() if x.isdigit()]


STOP_POLLS, STOP_POLL_SEC = 10, 0.1


def stop_script(pid):
    """One kill -STOP, then the SAME PID re-read every 0.1 s for at most 1 s (busybox
    sh, coreutils date and sleep in the k3s node image). One line:
      stopped T <utc> <read>      the first read in state T
      gone <utc> <read>           the PID no longer exists
      replaced <comm> <utc> <read> the PID now belongs to another program
      not_stopped <state> <utc>   still not T after the last read
    A state is read only while the PID still runs micro_ros_agent."""
    pid = int(pid)
    return "\n".join([
        f'kill -STOP {pid} || {{ echo "kill_failed $(date +%s.%N)"; exit 0; }}',
        "i=1; s=?",
        f"while [ $i -le {STOP_POLLS} ]; do",
        f'  if [ ! -r /proc/{pid}/status ]; then echo "gone $(date +%s.%N) $i"; exit 0; fi',
        f'  c=$(cat /proc/{pid}/comm 2>/dev/null)',
        f'  if [ -n "$c" ] && [ "$c" != micro_ros_agent ]; then echo "replaced $c $(date +%s.%N) $i"; exit 0; fi',
        f'  s=$(sed -n "s/^State:[[:space:]]*\\([A-Z]\\).*/\\1/p" /proc/{pid}/status)',
        '  if [ "$s" = T ]; then echo "stopped T $(date +%s.%N) $i"; exit 0; fi',
        f"  i=$((i + 1)); sleep {STOP_POLL_SEC}",
        "done",
        'echo "not_stopped $s $(date +%s.%N)"',
    ])


def parse_stop(rc, out, err):
    """state 'T' and its utc only for the first T; every other outcome kept apart."""
    fields = out.split()
    stop = {"rc": rc, "output": (out or err).strip()[:200], "outcome": "unreadable"}
    if rc != 0 or not fields:
        return stop
    kind = fields[0]
    try:
        if kind == "stopped" and len(fields) == 4 and fields[1] == "T":
            stop.update(outcome="stopped", state="T", utc=float(fields[2]), read=int(fields[3]))
        elif kind == "gone" and len(fields) == 3:
            stop.update(outcome="gone", utc=float(fields[1]), read=int(fields[2]))
        elif kind == "replaced" and len(fields) == 4:
            stop.update(outcome="replaced", comm=fields[1], utc=float(fields[2]), read=int(fields[3]))
        elif kind == "not_stopped" and len(fields) == 3:
            stop.update(outcome="not_stopped", last_state=fields[1], utc=float(fields[2]))
        elif kind == "kill_failed" and len(fields) == 2:
            stop.update(outcome="kill_failed", utc=float(fields[1]))
    except ValueError:
        pass
    return stop


class Pilot:
    def __init__(self, ops, variant, marks, params=PARAMS):
        self.ops, self.variant, self.marks, self.p = ops, variant, marks, params
        self.stopped = False
        self.restarted = False

    def mark(self, name, **fields):
        return self.marks.write(name, **fields)

    def _until(self, predicate, seconds):
        deadline = self.ops.mono() + seconds
        while True:
            result = predicate()
            if result or self.ops.mono() >= deadline:
                return result
            self.ops.sleep(self.p["poll_sec"])

    def _latest(self, target, after_mono):
        records = [r for r in self.ops.health() if r.get("target") == target and r.get("sent_mono", -1) > after_mono]
        return records[-1] if records else None

    # -- nominal --
    def nominal(self):
        start = self.ops.mono()

        def all_positive():
            now = self.ops.mono()
            latest = {r: self._latest(f"{r}-onboard", now - self.p["fresh_sec"]) for r in ROBOTS}
            return all(rec is not None and positive(rec) for rec in latest.values()) and latest
        latest = self._until(all_positive, self.p["nominal_timeout_sec"])
        self.mark("nominal", ok=bool(latest), waited_sec=round(self.ops.mono() - start, 3),
                  latest={k: v for k, v in (latest or {}).items()})
        if not latest:
            raise NotStarted("drone01-04 onboard not all positive")

    # -- preparation: drone03 on the edge --
    def prepare(self):
        start = self.mark("prep_start")
        budget_end = start["mono"] + self.p["prep_budget_sec"]
        ok, detail = self.ops.set_delay(self.p["high_ms"])
        self.mark("prep_delay_set", value=self.p["high_ms"], ok=ok, detail=detail)
        if not ok:
            raise NotStarted(f"preparation: parameter not set/read back: {detail}")

        def edge_serving():
            rec = self._latest(f"{EDGE_ROBOT}-edge", start["mono"])
            return rec if rec is not None and positive(rec) else None
        edge = self._until(edge_serving, max(0.0, budget_end - self.ops.mono()))
        self.mark("prep_edge_serving", ok=bool(edge), health=edge)
        if not edge:
            raise NotStarted("preparation: drone03's edge instance never positive within the budget")
        ok, detail = self.ops.set_delay(self.p["nominal_ms"])
        self.mark("prep_delay_reset", value=self.p["nominal_ms"], ok=ok, detail=detail)
        if not ok:
            raise NotStarted(f"preparation: parameter not reset: {detail}")
        terminal = self._until(lambda: action_state(self.variant, self.ops.action_observation())
                               in ("success", "failure"), max(0.0, budget_end - self.ops.mono()))
        state = action_state(self.variant, self.ops.action_observation())
        self.mark("prep_action", state=state)
        if not terminal or state != "success":
            raise NotStarted(f"preparation: the migration's action is {state}")
        stable = self._until(self._stable, max(0.0, budget_end - self.ops.mono()))
        self.mark("prep_stable", ok=bool(stable), detail=stable or None)
        if not stable:
            raise NotStarted("preparation: not stable (edge positive, onboard inactive, no pending action) "
                             "within the budget")

    def _stable(self):
        now = self.ops.mono()
        lo = now - self.p["stable_sec"]
        records = [r for r in self.ops.health() if r.get("sent_mono", -1) > lo]
        edge = [r for r in records if r.get("target") == f"{EDGE_ROBOT}-edge"]
        onboard = [r for r in records if r.get("target") == f"{EDGE_ROBOT}-onboard"]
        if not edge or not onboard or not all(positive(r) for r in edge):
            return None
        if not all(lifecycle(r) == "inactive" for r in onboard):
            return None
        times = sorted(r["sent_mono"] for r in edge)
        if times[0] - lo > self.p["gap_sec"] or now - times[-1] > self.p["gap_sec"] or \
                any(b - a > self.p["gap_sec"] for a, b in zip(times, times[1:])):
            return None
        if action_state(self.variant, self.ops.action_observation()) != "success":
            return None
        return {"edge_answers": len(edge), "onboard_answers": len(onboard), "window_sec": self.p["stable_sec"]}

    # -- the fault --
    def pre_t0(self, guard_sec):
        pods = self.ops.edge_pods()
        foreign = [p for p in pods if not p.get("drone03_edge")]
        self.mark("edge_pods", pods=pods, foreign=foreign)
        if foreign:
            raise Aborted(f"the edge node hosts foreign Pods: {[p.get('name') for p in foreign]}")
        inspect, kubelet = self.ops.inspect(), self.ops.kubelet_probe()
        self.mark("edge_before", inspect=inspect, kubelet=kubelet)
        if inspect.get("running") is not True or kubelet.get("result") != "connected":
            raise Aborted("the edge node is not running with its kubelet reachable before T0")
        pid = self.ops.guard_start(guard_sec)
        alive = self.ops.guard_alive(pid)
        self.mark("guard_started", pid=pid, alive=alive, max_sec=guard_sec)
        if not alive:
            raise Aborted("the guard is not running: no stop without it")
        self.ops.sampler_start()
        self.mark("sampler_started")

    def stop_edge(self):
        t0 = self.mark("t0")
        self.stopped = True
        ok, detail = self.ops.stop_edge()
        self.mark("stop_command_end", ok=ok, detail=detail)
        down = self._until(lambda: s4_edge.first(self.ops.samples(), s4_edge.down, t0["mono"]),
                           self.p["down_wait_sec"])
        self.mark("fault_observed", ok=bool(down), sample=down,
                  lag_sec=None if not down else round(down["m1"] - t0["mono"], 3))
        return t0

    def start_edge(self, reason):
        start = self.mark("start_command_start", reason=reason)
        ok, detail = self.ops.start_edge()
        self.ops.mark_restored()
        self.restarted = True
        self.mark("start_command_end", ok=ok, detail=detail)
        return start

    def run(self):
        status = "completed"
        try:
            self.mark("pilot_start", variant=self.variant, params=self.p)
            self.nominal()
            self.prepare()
            self.pre_t0(self.p["guard_sec"])
            t0 = self.stop_edge()
            self.ops.hold_until(t0["mono"] + self.p["stop_sec"])
            start = self.start_edge("end of the stop")
            back = self._until(lambda: s4_edge.first(self.ops.samples(), s4_edge.up, start["mono"]),
                               self.p["up_wait_sec"])
            self.mark("node_back", ok=bool(back), sample=back,
                      after_start_sec=None if not back else round(back["m1"] - start["mono"], 3))
            self.ops.hold_until(start["mono"] + self.p["horizon_sec"])
            self.mark("horizon_end")
        except NotStarted as exc:
            status = "not_started"
            self.mark("not_started", reason=str(exc))
        except Aborted as exc:
            status = "aborted"
            self.mark("aborted", reason=str(exc))
        except Exception as exc:  # noqa: BLE001 -- recorded; the node still comes back
            status = "interrupted"
            self.mark("driver_error", error=f"{type(exc).__name__}: {exc}"[:300])
        finally:
            if self.stopped and not self.restarted:
                try:
                    self.start_edge("cleanup")
                except Exception as exc:  # noqa: BLE001
                    status = "interrupted"
                    self.mark("cleanup_failed", error=f"{type(exc).__name__}: {exc}"[:300])
            if self.ops.guard_fired():
                status = "interrupted"
                self.mark("guard_fired", record=self.ops.guard_record())
            self.ops.sampler_stop()
            self.mark("driver_end", status=status)
        return status

    def guard_test(self):
        """Stop the edge under a short guard, then die: the guard alone restarts it."""
        self.mark("guard_test_start", guard_sec=self.p["guard_test_sec"])
        self.pre_t0(self.p["guard_test_sec"])
        self.stop_edge()
        self.mark("driver_killing_itself")
        self.ops.sampler_stop()
        self.ops.die()


# ---- live operations --------------------------------------------------------

class LiveOps:
    mono = staticmethod(time.monotonic)
    utc = staticmethod(time.time)
    sleep = staticmethod(time.sleep)

    def __init__(self, args):
        self.a = args
        self.edge = args.edge_node
        self.address = s4_edge.node_address(self.edge)
        self.k = ["kubectl", "--context", args.context, "-n", args.namespace]
        self._health_reader = s2_control.ProgressiveReader(
            args.prober_node, "/var/lib/s4-prober/health.jsonl",
            os.path.join(args.result_dir, "prober-health.progressive.jsonl"))
        self._health = []
        self._sampler = None
        self._guard_pid = None
        self._faults_reader = s2_control.ProgressiveReader(
            args.prober_node, "/var/lib/s4-observer/faults.jsonl",
            os.path.join(args.result_dir, "faults.progressive.jsonl"))
        self._faults = []
        self._agent_stop = None

    def hold_until(self, until_mono):
        while self.mono() < until_mono:
            self.sleep(min(0.5, max(0.0, until_mono - self.mono())))

    def health(self):
        self._health += [r for r in self._health_reader.read() if r.get("event") == "health"]
        return self._health

    def _run(self, cmd, timeout=40.0):
        return s2_control.run(cmd, timeout=timeout)

    def set_delay(self, value):
        if self.a.variant == "a":
            node = f"/{EDGE_ROBOT}/companion_analytics_onboard"
            target, container = f"deployment/{EDGE_ROBOT}-companion-analytics-onboard", []
            name = node
        else:
            target, container = f"deployment/companion-analytics-{EDGE_ROBOT}", ["-c", "companion-analytics"]
            name = f'"/{EDGE_ROBOT}/companion_analytics_${{POD_NAME//-/_}}"'
        script = (f"source /ws/install/setup.bash && ROS_SUPER_CLIENT=TRUE timeout -k 1 30 setsid ros2 param set "
                  f"--no-daemon --spin-time 5 {name} processing_delay_ms {value} && ROS_SUPER_CLIENT=TRUE timeout "
                  f"-k 1 30 setsid ros2 param get --no-daemon --spin-time 5 {name} processing_delay_ms")
        rc, out, err = self._run([*self.k, "exec", target, *container, "--", "bash", "-c", script], timeout=70.0)
        text = (out + err).strip()
        ok = rc == 0 and "Set parameter successful" in text and f"Double value is: {value}" in text
        return ok, text[-400:]

    def action_observation(self):
        if self.a.variant == "a":
            rc, out, _ = self._run([*self.k, "logs", "deployment/operational-event-dispatcher-s4", "--tail=2000"],
                                   timeout=10.0)
            return out if rc == 0 else ""
        rc, out, _ = self._run([*self.k, "get", "adaptationpolicy", f"analytics-latency-slo-s4-{EDGE_ROBOT}", "-o",
                                "jsonpath={.status.state}"], timeout=10.0)
        return out if rc == 0 else ""

    def edge_pods(self):
        rc, out, err = self._run(["kubectl", "--context", self.a.context, "get", "pods", "-A", "-o", "json",
                                  "--field-selector", f"spec.nodeName={self.edge}"], timeout=15.0)
        if rc != 0:
            raise Aborted(f"cannot list the edge node's Pods: {err.strip()[:200]}")
        out_list = []
        for pod in json.loads(out).get("items") or []:
            meta = pod.get("metadata") or {}
            name, labels = meta.get("name") or "", meta.get("labels") or {}
            mine = EDGE_ROBOT in name and ("edge" in name or labels.get("dronekube.io/placement") == "edge"
                                           or "companion-analytics" in name and "onboard" not in name)
            out_list.append({"namespace": meta.get("namespace"), "name": name, "uid": meta.get("uid"),
                             "drone03_edge": bool(mine)})
        return out_list

    def inspect(self):
        return s4_edge.inspect(self.edge)

    def kubelet_probe(self):
        return s4_edge.kubelet_probe(self.address)

    def guard_start(self, max_sec, deadline_utc=None):
        self._guard_pid = s4_edge.guard_start(self.edge, self.a.result_dir, max_sec, deadline_utc)
        return self._guard_pid

    def guard_alive(self, pid):
        return s4_edge.guard_alive(pid, self.a.result_dir)

    def guard_fired(self):
        return s4_edge.guard_record(self.a.result_dir) is not None

    def guard_record(self):
        return s4_edge.guard_record(self.a.result_dir)

    def sampler_start(self):
        self._sampler = s4_edge.Sampler(self.edge, self.address, os.path.join(self.a.result_dir, "edge-samples.jsonl"))
        self._sampler.start()

    def samples(self):
        return self._sampler.snapshot() if self._sampler else []

    def sampler_stop(self):
        if self._sampler is not None:
            self._sampler.join()

    def stop_edge(self):
        rc, out, err = self._run(["docker", "stop", "--time", "0", self.edge], timeout=30.0)
        return rc == 0, (err or out).strip()[:200]

    def start_edge(self):
        rc, out, err = self._run(["docker", "start", self.edge], timeout=60.0)
        return rc == 0, (err or out).strip()[:200]

    def mark_restored(self):
        s4_edge.mark_restored(self.a.result_dir)

    def die(self):
        os.kill(os.getpid(), signal.SIGKILL)

    # -- the cells' faults (s4_cell.py) --
    AGENT_POD_PREFIX = "drone02-microxrce-agent-"

    def agent_target(self):
        """drone02's Agent Pod and its node, and the micro_ros_agent PIDs seen in the
        node's PID namespace (an ancestor of the Pod's): the signal is sent from there,
        where the kernel does not ignore it even for a container's PID 1 (S4-A's layout,
        found in round 1's cell 3)."""
        rc, out, err = self._run([*self.k, "get", "pods", "-o", "json"], timeout=15.0)
        if rc != 0:
            return {"error": (err or out).strip()[:200]}
        pods = [p for p in json.loads(out).get("items") or []
                if p["metadata"]["name"].startswith(self.AGENT_POD_PREFIX)
                and not p["metadata"].get("deletionTimestamp") and p.get("status", {}).get("phase") == "Running"]
        if len(pods) != 1:
            return {"error": f"{len(pods)} running drone02 Agent Pods", "pods": [p["metadata"]["name"] for p in pods]}
        pod, node = pods[0]["metadata"]["name"], pods[0]["spec"].get("nodeName")
        rc, out, err = self._run(["docker", "exec", node, "pidof", "micro_ros_agent"], timeout=15.0)
        return {"pod": pod, "node": node, "pids": parse_pidof(out) if rc == 0 else [],
                "pidof": (out or err).strip()[:200]}

    def stop_agent(self, target):
        """SIGSTOP to the real micro_ros_agent from its node's PID namespace, then its
        state read from /proc with the node's UTC, in one command."""
        rc, out, err = self._run(["docker", "exec", target["node"], "sh", "-c", stop_script(target["pid"])],
                                 timeout=20.0)
        stop = {**parse_stop(rc, out, err), "node": target["node"], "pid": target["pid"]}
        self._agent_stop = stop
        return stop

    def last_agent_stop(self):
        return self._agent_stop

    def apply_battery_harness(self, t0_utc):
        import render_s4_bench
        text = render_s4_bench.battery_harness(self.a.discovery_address, t0_utc)
        with open(os.path.join(self.a.result_dir, "battery-harness.yaml"), "w") as h:
            h.write(text)
        try:
            p = subprocess.run(["kubectl", "--context", self.a.context, "apply", "-f", "-"], input=text,
                               capture_output=True, text=True, timeout=30)
        except subprocess.TimeoutExpired:
            return False, "kubectl apply timed out"
        return p.returncode == 0, (p.stderr or p.stdout).strip()[:200]

    def battery_harness_ready(self):
        rc, out, _ = self._run([*self.k, "logs", "deployment/s4-battery-fault-harness-drone01", "--tail=50"],
                               timeout=10.0)
        line = next((l for l in out.splitlines() if "E1_HARNESS_READY" in l), None) if rc == 0 else None
        return line

    def arm_drone01(self):
        script = ("cd /opt/px4; PATH=/opt/px4/bin:$PATH; px4-commander arm; sleep 2; px4-commander takeoff; "
                  "sleep 8; px4-commander status")
        rc, out, err = self._run([*self.k, "exec", "deployment/drone01-px4-sitl", "--", "sh", "-c", script],
                                 timeout=60.0)
        return rc == 0, (out + err).strip()[-300:]

    def px4_state(self, robot):
        """The last complete vehicle_status read by the host's PX4 observer."""
        import px4_status_continuity
        path = os.path.join(self.a.result_dir, "observers", f"px4-{robot}.txt")
        try:
            with open(path, errors="replace") as h:
                reads = px4_status_continuity.parse_reads(h.read())
        except OSError:
            return None
        last = next((s for _, s in reversed(reads) if s is not None), None)
        return None if last is None else {k: last.get(k) for k in ("arming_state", "nav_state", "failsafe")}

    def fault_records(self):
        self._faults += self._faults_reader.read()
        return self._faults


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--result-dir", required=True)
    parser.add_argument("--variant", required=True, choices=("a", "b"))
    parser.add_argument("--mode", default="pilot", choices=("pilot", "guard-test"))
    parser.add_argument("--context", default="k3d-cloud-native-s4")
    parser.add_argument("--namespace", default="cloud-native-p2")
    parser.add_argument("--edge-node", default="k3d-cloud-native-s4-agent-4")
    parser.add_argument("--prober-node", default="k3d-cloud-native-s4-server-0")
    args = parser.parse_args(argv)
    marks = Marks(os.path.join(args.result_dir, "phases.jsonl"))
    pilot = Pilot(LiveOps(args), args.variant, marks)

    def stop(signum, _frame):
        raise RuntimeError(f"signal {signum}")
    signal.signal(signal.SIGTERM, stop)
    try:
        if args.mode == "guard-test":
            pilot.guard_test()
            return EXIT["interrupted"]          # not reached: the driver kills itself
        return EXIT[pilot.run()]
    finally:
        marks.close()


if __name__ == "__main__":
    sys.exit(main())
