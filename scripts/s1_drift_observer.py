#!/usr/bin/env python3
"""S1 collector (R10, docs/R10_S1_DRIFT.md): baseline, injection, one window.

The same in A and B. Every record is a JSON line with the host's monotonic
clock (m, seconds; durations and the deadline) and UTC (w; to correlate with
Kubernetes and the audit), for the start and the end of each read:
  inventory      the analytics Deployments, their ReplicaSets and Pods, the
                 PX4 Pods (and in B the target ROSModule's per-Pod lifecycle
                 records), read about once a second (nominal: the effective
                 cadence is what the records show);
  health         a GetHealthSnapshot on /<robot>/companion/onboard/health from
                 the observer Pod (another robot's analytics Pod, outside the
                 target): the target every round, the uninvolved robots every
                 third; positive, negative (an unhealthy answer or an RPC
                 timeout) or error (the collection itself failed);
  baseline_*     the same before the injection;
  inject         the command, its start and end, exit code and output.
One deadline: WINDOW seconds from the start of the injection, for every
thread; every call is bounded by the time left. Collection continues to the
deadline even if the target recovers before. The judge is s1_drift_judge.py.
Tested offline: operator/tests/test_s1_drift.py.
"""

import argparse
import json
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone

INVENTORY_PERIOD_SEC = 1.0
MIN_CALL_SEC = 0.5          # no call is started with less time left than this
UNINVOLVED_EVERY = 3
RPC_TIMEOUT_SEC = 15


def utc():
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _ready(pod):
    return any(c.get("type") == "Ready" and c.get("status") == "True"
               for c in pod.get("status", {}).get("conditions") or [])


def summarize(items, deployments, variant, target_module):
    """The objects the judge needs, from one `kubectl get ... -o json`."""
    by_kind = {}
    for item in items:
        by_kind.setdefault(item.get("kind"), []).append(item)
    rs_owner = {}
    for rs in by_kind.get("ReplicaSet", []):
        owner = next((o for o in rs["metadata"].get("ownerReferences") or [] if o.get("kind") == "Deployment"), {})
        if rs["metadata"].get("uid"):
            rs_owner[rs["metadata"]["uid"]] = (owner.get("name"), owner.get("uid"))
    out = {"deployments": {}, "pods": [], "px4": []}
    for d in by_kind.get("Deployment", []):
        name = d["metadata"]["name"]
        if name in deployments:
            out["deployments"][name] = {
                "uid": d["metadata"]["uid"], "generation": d["metadata"].get("generation"),
                "observedGeneration": d.get("status", {}).get("observedGeneration"),
                "replicas": d["spec"].get("replicas"),
                "readyReplicas": d.get("status", {}).get("readyReplicas") or 0}
    for p in by_kind.get("Pod", []):
        name = p["metadata"]["name"]
        restarts = sum(c.get("restartCount", 0) for c in p.get("status", {}).get("containerStatuses") or [])
        if "px4-sitl" in name:
            out["px4"].append({"name": name, "uid": p["metadata"]["uid"], "restarts": restarts})
            continue
        owner = next((o for o in p["metadata"].get("ownerReferences") or [] if o.get("kind") == "ReplicaSet"), {})
        # A recreated ReplicaSet may reuse the old name, but never its UID.
        deployment, deployment_uid = rs_owner.get(owner.get("uid"), (None, None))
        if deployment is None and name.rsplit("-", 2)[0] in deployments:
            # Its ReplicaSet is already gone (a deleted Deployment's Pods keep
            # terminating after it): attributed by name, owner unknown -- so
            # never the current Deployment's Pod.
            deployment = name.rsplit("-", 2)[0]
        if deployment in deployments:
            out["pods"].append({"name": name, "uid": p["metadata"]["uid"], "deployment": deployment,
                                "deployment_uid": deployment_uid, "ready": _ready(p),
                                "terminating": bool(p["metadata"].get("deletionTimestamp")),
                                "restarts": restarts})
    if variant == "b":
        module = next((m for m in by_kind.get("ROSModule", []) if m["metadata"]["name"] == target_module), None)
        out["lifecycle"] = (module or {}).get("status", {}).get("lifecycleInstances") or {}
    return out


def classify_health(rc, stdout, stderr):
    """positive | negative | error, from a `kubectl exec ... timeout ros2 service call`."""
    if rc is None:
        return "error", "collector timeout"
    if rc == 0 and "GetHealthSnapshot_Response" in stdout:
        ok = all(s in stdout for s in ("healthy=True", "lifecycle_state='active'", "instance_id='onboard'"))
        return ("positive" if ok else "negative"), "" if ok else "unhealthy answer"
    if rc in (124, 137):
        return "negative", "RPC timeout"
    if rc == 0 or "waiting for service" in stdout:
        return "negative", "no answer"
    return "error", (stderr or stdout).strip().splitlines()[-1][:200] if (stderr or stdout).strip() else f"rc {rc}"


class Collector:
    def __init__(self, args, run=None, mono=time.monotonic, wall=utc, sleep=time.sleep):
        self.a, self.mono, self.wall, self.sleep = args, mono, wall, sleep
        self.run = run or self._run
        self.k = ["kubectl", "--context", args.context, "-n", args.namespace]
        self.lock = threading.Lock()
        self.out = open(args.out, "a")
        self.deadline = None
        self.deployments = {args.target_deployment, *args.uninvolved_deployments}
        self.round = 0

    @staticmethod
    def _run(cmd, timeout):
        # The caller's timeout as is, never raised to a floor: the calls below are
        # only made with time left (review of 759dee9, point 2).
        try:
            p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
            return p.returncode, p.stdout, p.stderr
        except subprocess.TimeoutExpired:
            return None, "", "collector timeout"

    def emit(self, record):
        with self.lock:
            self.out.write(json.dumps(record) + "\n")
            self.out.flush()

    def left(self):
        return float("inf") if self.deadline is None else self.deadline - self.mono()

    def inventory(self, kind="inventory"):
        if self.left() < MIN_CALL_SEC:
            return
        kinds = "deployments,replicasets,pods" + (",rosmodules" if self.a.variant == "b" else "")
        m0, w0 = self.mono(), self.wall()
        rc, out, err = self.run(self.k + ["get", kinds, "-o", "json"], min(10.0, self.left()))
        record = {"kind": kind, "m0": m0, "w0": w0, "m1": self.mono(), "w1": self.wall()}
        try:
            record.update(summarize(json.loads(out)["items"], self.deployments, self.a.variant,
                                    self.a.target_module))
        except (ValueError, KeyError, TypeError):
            record["error"] = (err or out or f"rc {rc}").strip()[:200]
        self.emit(record)

    def health(self, robot, kind="health"):
        timeout = min(RPC_TIMEOUT_SEC, self.left() - 2.0)     # the RPC, inside the Pod
        if timeout < 2:
            return
        self.round += 1
        service = f"/{robot}/companion/onboard/health"
        call = (f"source /ws/install/setup.bash; ROS_SUPER_CLIENT=TRUE timeout -k 1 {int(timeout)} setsid "
                f"ros2 service call {service} cloud_native_robotics_interfaces/srv/GetHealthSnapshot "
                f"\"{{correlation_id: 's1-{robot}-{self.round}'}}\"")
        m0, w0 = self.mono(), self.wall()
        rc, out, err = self.run(self.k + ["exec", f"deployment/{self.a.observer_deployment}", "-c",
                                          self.a.observer_container, "--", "/bin/bash", "-lc", call],
                                min(timeout + 5, self.left() - 0.2))     # the exec, never past the deadline
        result, detail = classify_health(rc, out, err)
        self.emit({"kind": kind, "robot": robot, "m0": m0, "w0": w0, "m1": self.mono(), "w1": self.wall(),
                   "result": result, "detail": detail})

    def start_window(self):
        """t0: the start of the injection and the origin of the one deadline."""
        self.t0, self.w0 = self.mono(), self.wall()
        self.deadline = self.t0 + self.a.window

    def inject(self):
        if self.a.case == "delete":
            cmd = self.k + ["delete", "deployment", self.a.target_deployment]
        else:
            cmd = self.k + ["scale", "deployment", self.a.target_deployment, "--replicas=0"]
        if self.deadline is None:
            self.start_window()
        cmd_m0 = self.mono()
        rc, out, err = self.run(cmd, min(30.0, self.left()))
        self.emit({"kind": "inject", "case": self.a.case, "command": " ".join(cmd[3:]), "m0": self.t0,
                   "w0": self.w0, "command_m0": cmd_m0, "m1": self.mono(), "w1": self.wall(), "rc": rc,
                   "stdout": out.strip()[:300], "stderr": err.strip()[:300], "deadline_m": self.deadline})

    def _inventory_loop(self):
        while self.left() > 0:
            started = self.mono()
            self.inventory()
            self.sleep(max(0.0, min(INVENTORY_PERIOD_SEC - (self.mono() - started), self.left())))

    def _health_loop(self):
        turn = 0
        while self.left() > 2:
            self.health(self.a.target_robot)
            turn += 1
            if turn % UNINVOLVED_EVERY == 0:
                for robot in self.a.uninvolved_robots:
                    self.health(robot)

    def collect(self):
        self.inventory("baseline_inventory")
        for robot in [self.a.target_robot, *self.a.uninvolved_robots]:
            self.health(robot, "baseline_health")
        # The observers start with the window, before the command: the injection's
        # own duration is observed like the rest (review of 759dee9, point 3).
        self.start_window()
        threads = [threading.Thread(target=self._inventory_loop), threading.Thread(target=self._health_loop)]
        for t in threads:
            t.start()
        self.inject()
        for t in threads:
            t.join()
        self.emit({"kind": "window_end", "m": self.mono(), "w": self.wall()})


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--variant", choices=("a", "b"), required=True)
    parser.add_argument("--case", choices=("delete", "scale"), required=True)
    parser.add_argument("--context", required=True)
    parser.add_argument("--namespace", required=True)
    parser.add_argument("--target-robot", required=True)
    parser.add_argument("--target-deployment", required=True)
    parser.add_argument("--target-module", default="")
    parser.add_argument("--uninvolved-robots", nargs="+", required=True)
    parser.add_argument("--uninvolved-deployments", nargs="+", required=True)
    parser.add_argument("--observer-deployment", required=True)
    parser.add_argument("--observer-container", required=True)
    parser.add_argument("--window", type=float, default=180.0)
    parser.add_argument("--out", required=True)
    Collector(parser.parse_args(argv)).collect()
    return 0


if __name__ == "__main__":
    sys.exit(main())
