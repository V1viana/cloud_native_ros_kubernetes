#!/usr/bin/env python3
"""S4 cell driver (R13, docs/R13_S4_CONTRACT_DRAFT.md): one cell of the matrix.

  l1-battery    the battery fault alone          l1-edge  the edge node alone
  l1-telemetry  the telemetry fault alone        l3       the three together

Every cell: drone01-04 nominal; drone03 prepared on the edge by the P2 migration
(recorded apart, stable, or NOT_STARTED -- s4_pilot.Pilot.prepare); nothing foreign
on the edge; the sampler of the edge node running (its state is context in every
cell). T0 is fixed t0_lead_sec ahead; before it: the battery harness applied with
start_at_utc = T0 and seen ready, drone02's micro_ros_agent PID read, the guard seen
alive (edge cells). At T0 the commands run together:
  battery    the harness fires by itself at T0;
  telemetry  SIGSTOP to the real micro_ros_agent process from its node's PID namespace
             (one process found by pidof; node, PID and Pod recorded), its state read
             back from /proc in the same command -- it must be T;
  edge       docker stop --time 0.
The EFFECT of each is recorded -- not the reaction, which the judge times apart:
  battery    the first low BatteryStatus received by the fault observer;
  telemetry  the process in state T (stopped), with the node's UTC;
  edge       container stopped and kubelet not connected (outside the cluster).
Edge cells: docker start at T0 + 90 s. Horizon 180 s after T0, or after the start in
edge cells. Nothing is restored for telemetry: the control plane under test does it,
or not. Exit: 0 completed, 2 not started, 3 interrupted, 4 aborted.
Tested offline: operator/tests/test_s4_cell.py.
"""

import argparse
import os
import signal
import sys
import threading

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import s4_edge  # noqa: E402
import s4_pilot  # noqa: E402
from s4_pilot import Aborted, NotStarted  # noqa: E402
from s2_phase import Marks  # noqa: E402

CELLS = {"l1-battery": ("battery",), "l1-telemetry": ("telemetry",), "l1-edge": ("edge",),
         "l3": ("battery", "telemetry", "edge")}
PARAMS = {**s4_pilot.PARAMS, "t0_lead_sec": 45.0, "harness_ready_before_sec": 5.0, "effect_wait_sec": 5.0,
          "low_remaining_max": 0.2, "premise_timeout_sec": 60.0}


class Cell(s4_pilot.Pilot):
    def __init__(self, ops, variant, cell, marks, params=PARAMS):
        super().__init__(ops, variant, marks, params)
        if cell not in CELLS:
            raise ValueError(f"unknown cell {cell}")
        self.cell, self.faults = cell, CELLS[cell]

    def _premise(self):
        """drone01 armed and hovering in Hold (E1's commander sequence), in every cell:
        the battery fault's RTL needs it, and the bench is the same across cells."""
        ok, detail = self.ops.arm_drone01()
        self.mark("drone01_arm_command", ok=ok, detail=detail)
        state = self._until(lambda: (lambda s: s if s and s.get("arming_state") == "2"
                                     and s.get("nav_state") == "4" else None)(self.ops.px4_state("drone01")),
                            self.p["premise_timeout_sec"])
        self.mark("premise", ok=bool(state), state=state or self.ops.px4_state("drone01"))
        if not state:
            raise NotStarted("drone01 not armed in Hold")

    def _arm(self):
        """Before T0: drone01 armed in Hold, nothing foreign on the edge, the edge's
        state, the guard (edge cells), the sampler, drone02's agent PID, the battery
        harness armed for T0."""
        self._premise()
        pods = self.ops.edge_pods()
        foreign = [p for p in pods if not p.get("drone03_edge")]
        self.mark("edge_pods", pods=pods, foreign=foreign)
        if foreign:
            raise Aborted(f"the edge node hosts foreign Pods: {[p.get('name') for p in foreign]}")
        inspect, kubelet = self.ops.inspect(), self.ops.kubelet_probe()
        self.mark("edge_before", inspect=inspect, kubelet=kubelet)
        if inspect.get("running") is not True or kubelet.get("result") != "connected":
            raise Aborted("the edge node is not running with its kubelet reachable before T0")
        # T0 is fixed before the guard: its deadline is T0 + guard_sec whenever it is
        # started (round 1 on 741305d: a guard armed 45 s before T0 with a deadline
        # relative to its own start fired at T0 + 74 s, before the planned start)
        t0_utc = round(self.ops.utc() + self.p["t0_lead_sec"], 3)
        t0_mono = self.ops.mono() + (t0_utc - self.ops.utc())
        if "edge" in self.faults:
            if self.p["guard_sec"] <= self.p["stop_sec"]:
                raise Aborted("the guard's deadline would come before the planned start")
            deadline_utc = round(t0_utc + self.p["guard_sec"], 3)
            pid = self.ops.guard_start(self.p["guard_sec"], deadline_utc=deadline_utc)
            alive = self.ops.guard_alive(pid)
            self.mark("guard_started", pid=pid, alive=alive, max_sec=self.p["guard_sec"], deadline_utc=deadline_utc,
                      t0_utc=t0_utc)
            if not alive:
                raise Aborted("the guard is not running: no stop without it")
        self.ops.sampler_start()
        self.mark("sampler_started")
        self.mark("t0_planned", t0_utc=t0_utc, t0_mono=t0_mono, faults=list(self.faults))
        state = {"t0_utc": t0_utc, "t0_mono": t0_mono}
        if "telemetry" in self.faults:
            target = self.ops.agent_target()
            pids = target.get("pids") or []
            self.mark("agent_target", node=target.get("node"), pod=target.get("pod"), pids=pids,
                      detail=target.get("error") or target.get("pidof"))
            if len(pids) != 1:
                raise NotStarted(f"drone02's micro_ros_agent: {len(pids)} process(es) on its node, one needed "
                                 f"({target.get('error') or target.get('pidof')})")
            state["agent"] = {"node": target["node"], "pod": target["pod"], "pid": pids[0]}
        if "battery" in self.faults:
            ok, detail = self.ops.apply_battery_harness(t0_utc)
            self.mark("battery_harness_applied", ok=ok, detail=detail)
            if not ok:
                raise NotStarted(f"battery harness not applied: {detail}")
            ready = self._until(self.ops.battery_harness_ready,
                                max(0.0, t0_mono - self.p["harness_ready_before_sec"] - self.ops.mono()))
            self.mark("battery_harness_ready", ok=bool(ready), detail=ready or None)
            if not ready:
                raise NotStarted("battery harness not ready before T0")
        if self.ops.mono() >= t0_mono:
            raise NotStarted("T0 passed during the preparation of the faults")
        return state

    def _inject(self, state):
        """At T0, the commands together; each one's own times recorded."""
        self.ops.hold_until(state["t0_mono"])
        t0 = self.mark("t0", planned_utc=state["t0_utc"])
        results = {}

        def telemetry():
            results["telemetry"] = self.ops.stop_agent(state["agent"])

        def edge():
            self.stopped = True
            results["edge"] = self.ops.stop_edge()
        threads = [threading.Thread(target=f) for name, f in (("telemetry", telemetry), ("edge", edge))
                   if name in self.faults]
        for t in threads:
            t.start()
        for t in threads:
            t.join(60)
        for name, result in sorted(results.items()):
            self.mark(f"{name}_command_end", result=result)
        return t0

    def _effects(self, t0):
        """The injected effect of each fault, observed, with its lag from T0."""
        effects = {}
        deadline_mono = t0["mono"] + self.p["effect_wait_sec"]
        if "edge" in self.faults:
            down = self._until(lambda: s4_edge.first(self.ops.samples(), s4_edge.down, t0["mono"]),
                               max(0.0, deadline_mono - self.ops.mono()))
            effects["edge"] = None if not down else {"observed_utc": down["w1"], "source": "edge sampler",
                                                     "sample": down}
        if "telemetry" in self.faults:
            stop = self.ops.last_agent_stop()
            ok = bool(stop and stop.get("state") == "T")
            effects["telemetry"] = None if not ok else {"observed_utc": stop["utc"],
                                                        "source": "/proc state of micro_ros_agent", "stop": stop}
        if "battery" in self.faults:
            def low():
                return next((r for r in self.ops.fault_records() if r.get("event") == "battery"
                             and r.get("recv_utc", 0) >= t0["utc"] - 1.0
                             and r.get("remaining", 1.0) <= self.p["low_remaining_max"]), None)
            received = self._until(low, max(0.0, deadline_mono - self.ops.mono()))
            effects["battery"] = None if not received else {"observed_utc": received["recv_utc"],
                                                            "source": "fault observer", "record": received}
        for name, effect in effects.items():
            if effect is not None:
                effect["lag_sec"] = round(effect["observed_utc"] - t0["utc"], 3)
        self.mark("effects", effects=effects)
        return effects

    def run(self):
        status = "completed"
        try:
            self.mark("cell_start", variant=self.variant, cell=self.cell, faults=list(self.faults), params=self.p)
            self.nominal()
            self.prepare()
            state = self._arm()
            t0 = self._inject(state)
            self._effects(t0)
            horizon = t0["mono"] + self.p["horizon_sec"]
            if "edge" in self.faults:
                self.ops.hold_until(t0["mono"] + self.p["stop_sec"])
                start = self.start_edge("end of the stop")
                back = self._until(lambda: s4_edge.first(self.ops.samples(), s4_edge.up, start["mono"]),
                                   self.p["up_wait_sec"])
                self.mark("node_back", ok=bool(back), sample=back,
                          after_start_sec=None if not back else round(back["m1"] - start["mono"], 3))
                horizon = start["mono"] + self.p["horizon_sec"]
            self.mark("horizon_fixed", horizon_mono=horizon)
            self.ops.hold_until(horizon)
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
            if "edge" in self.faults and self.ops.guard_fired():
                status = "interrupted"
                self.mark("guard_fired", record=self.ops.guard_record())
            self.ops.sampler_stop()
            self.mark("driver_end", status=status)
        return status


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--result-dir", required=True)
    parser.add_argument("--variant", required=True, choices=("a", "b"))
    parser.add_argument("--cell", required=True, choices=sorted(CELLS))
    parser.add_argument("--discovery-address", required=True)
    parser.add_argument("--context", default="k3d-cloud-native-s4")
    parser.add_argument("--namespace", default="cloud-native-p2")
    parser.add_argument("--edge-node", default="k3d-cloud-native-s4-agent-4")
    parser.add_argument("--prober-node", default="k3d-cloud-native-s4-server-0")
    args = parser.parse_args(argv)
    marks = Marks(os.path.join(args.result_dir, "phases.jsonl"))
    cell = Cell(s4_pilot.LiveOps(args), args.variant, args.cell, marks)

    def stop(signum, _frame):
        raise RuntimeError(f"signal {signum}")
    signal.signal(signal.SIGTERM, stop)
    try:
        return s4_pilot.EXIT[cell.run()]
    finally:
        marks.close()


if __name__ == "__main__":
    sys.exit(main())
