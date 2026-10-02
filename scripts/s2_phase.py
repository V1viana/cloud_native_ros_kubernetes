#!/usr/bin/env python3
"""S2 phase driver (R11, contract s2-partition-v1, docs/R11_S2_PARTITION.md).

The timed part of one cell, after run_s2.sh has the cluster, the harness, the
prober and the observers in place: every step marked in phases.jsonl with the
host's monotonic clock and UTC at the instant it happens.

  1. clock alignment (s2_control.clock_summary) of the measured containers;
  2. metrics flowing in the local copy, then arm (80 ms read back);
  3. the 30 s nominal reference, then a nominal state (last three local windows
     below 150 ms, no episode open: at most 30 s more, else aborted); PX4's
     state of every drone frozen;
  4. the case, from the phase start:
       control         no DROP; pulse 80 -> 300 -> 80 for 12 s at +10 s; the
                       horizon, fixed before the injection, 180 s after the
                       programmed end of the pulse;
       partition-only  the cut (guard first, then DROP, then the cut verified);
                       held 60 s from the verification; no pulse; restored;
       short / long    the cut; the pulse at +10 s from the verification; the
                       link restored 2 s / 30 s after the local return that the
                       incremental ground truth sees (at the latest 100 s after
                       the first DROP, well inside the guard's 120 s);
     with a cut, the horizon ends 180 s after the end of the restore command;
  5. held to the horizon whatever happened before (no early end);
  6. clock alignment again; the docker network's peers read again.

The partition: the guard (an independent host process) is started and seen
alive before the first DROP; a failed or partial application is removed at
once; `partition-restored` is written only after a status read shows no jump
and no chain; a cut is verified by all three jumps, each the first rule of its
hook, the chain's rules for every peer in both directions and TCP probes from
drone01's harness container that time out (a connection or a refusal means the
path is not cut). From the applied cut to the restore a monitor reads the rules
every second (cut-samples.jsonl) and one persistent process in the harness probes
the paths on a monotonic 1 s grid (cut-probes.jsonl); the restore only signals
them to stop. The restore is
retried; if it fails, the guard's removal ends the run as interrupted.
Exit: 0 completed, 3 interrupted (SIGTERM, guard fired, restore failed),
4 aborted before or at the cut (arm, metrics, guard, apply, verification).
Tested offline: operator/tests/test_s2_phase.py.
"""

import argparse
import importlib.util
import json
import math
import os
import shlex
import signal
import subprocess
import sys
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))


def _load(name):
    spec = importlib.util.spec_from_file_location(name, os.path.join(HERE, f"{name}.py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


s2_control = _load("s2_control")
s2_partition = _load("s2_partition")

PROTOCOL_ID = "s2-partition-v1"
CASES = ("control", "partition-only", "short", "long")
PARTITION_CASES = ("partition-only", "short", "long")
PARAMS = {
    "nominal_ms": 80.0, "high_ms": 300.0, "pulse_sec": 12.0, "pulse_offset_sec": 10.0,
    "reference_sec": 30.0, "stabilization_sec": 30.0, "horizon_sec": 180.0, "cut_only_sec": 60.0,
    "restore_after_return_sec": {"short": 2.0, "long": 30.0},
    "guard_sec": s2_partition.GUARD_MAX_SEC, "restore_deadline_sec": 100.0,
    "arm_timeout_sec": 20.0, "arm_attempts": 3, "metrics_timeout_sec": 60.0, "metrics_fresh_sec": 2.0,
    "poll_sec": 0.5, "restore_attempts": 3, "restore_probe_rounds": 10,
}
EXIT = {"completed": 0, "interrupted": 3, "aborted": 4}
# The guard counts whole seconds (`date +%s`): it may fire up to 1 s before its
# nominal end. A restore command is started only with RESTORE_MARGIN_SEC left and
# is bounded by what remains (review of a41c087, choice 1); past that, the guard's
# own removal ends the run as interrupted.
GUARD_TRUNCATION_SEC, RESTORE_MARGIN_SEC, COMMAND_TIMEOUT_SEC = 1.0, 2.0, 30.0
RECOVERY_MS = 150.0               # the window rule's recovery threshold (V6)


class Aborted(RuntimeError):
    """Before or at the cut: the cell cannot run (the rules, if any, are removed)."""


class Interrupted(RuntimeError):
    """SIGTERM, the guard fired, or the rules could not be removed."""


class Marks:
    def __init__(self, path, mono=time.monotonic, utc=time.time):
        self._file = open(path, "a", buffering=1)
        self._mono, self._utc = mono, utc

    def write(self, name, **fields):
        record = {"mark": name, "mono": self._mono(), "utc": self._utc(), **fields}
        self._file.write(json.dumps(record) + "\n")
        self._file.flush()
        return record

    def close(self):
        self._file.close()


def cut_check(status, peer_list, probes):
    """The reasons the cut is not verified (empty: verified)."""
    reasons = []
    missing = sorted(set(s2_partition.HOOKS) - set(status.get("jumps") or []))
    if missing:
        reasons.append(f"jumps missing: {missing}")
    reasons += s2_partition.conformity(status, peer_list)
    if not probes:
        reasons.append("no probe result")
    for target, result in sorted((probes or {}).items()):
        if result.get("result") != "timeout":
            reasons.append(f"probe {target}: {result.get('result')} (a cut path times out)")
    return reasons


class Phase:
    def __init__(self, ops, case, run_id, target_node, peer_list, cut_targets, restore_targets,
                 marks, params=PARAMS):
        if case not in CASES:
            raise ValueError(f"unknown case {case}")
        self.ops, self.case, self.run_id = ops, case, run_id
        self.target_node, self.peers = target_node, peer_list
        self.cut_targets, self.restore_targets = cut_targets, restore_targets
        self.marks, self.p = marks, params
        self.applied = False           # set before the first DROP: any partial application is removed
        self.restored_clean = False
        self.first_drop = None
        self.guard_start_mono = None

    def mark(self, name, **fields):
        return self.marks.write(name, **fields)

    # -- waiting, always with the local copy advancing and the guard watched --
    def _check_guard(self):
        if self.applied and not self.restored_clean and self.ops.guard_fired():
            raise Interrupted("the guard fired")

    def hold_until(self, until_mono):
        while True:
            now = self.ops.mono()
            if now >= until_mono:
                return
            self.ops.truth_update()
            self._check_guard()
            self.ops.sleep(min(self.p["poll_sec"], max(0.0, until_mono - self.ops.mono())))

    # -- preparation --
    def wait_metrics(self):
        deadline = self.ops.mono() + self.p["metrics_timeout_sec"]
        while self.ops.mono() < deadline:
            self.ops.truth_update()
            last = self.ops.last_sample()
            if last is not None and self.ops.mono() - last <= self.p["metrics_fresh_sec"]:
                return self.mark("metrics_flowing", last_sample=last)
            self.ops.sleep(self.p["poll_sec"])
        raise Aborted("no fresh onboard sample in the local copy")

    def arm(self):
        """Up to arm_attempts, each with its own id (the harness runs an id once):
        right after the harness starts, its parameter clients may not have
        discovered the analytics' services yet (a 2 s wait each)."""
        last = None
        for attempt in range(1, self.p["arm_attempts"] + 1):
            command = {"run": self.run_id, "id": f"{self.run_id}-arm-{attempt}", "target_node": self.target_node,
                       "parameter": "processing_delay_ms", "nominal": self.p["nominal_ms"]}
            self.ops.write_command("arm.json", command)
            self.mark("arm_written", command=command, attempt=attempt)
            deadline = self.ops.mono() + self.p["arm_timeout_sec"]
            last = None
            while last is None and self.ops.mono() < deadline:
                for event in self.ops.events():
                    if event.get("id") == command["id"] and event.get("event") in ("armed", "arm_failed"):
                        last = event
                        break
                else:
                    self.ops.truth_update()
                    self.ops.sleep(self.p["poll_sec"])
            self.mark("arm_result", event=last, attempt=attempt)
            if last is not None and last["event"] == "armed":
                return last
            self.ops.sleep(2.0)
        raise Aborted(f"arm not confirmed after {self.p['arm_attempts']} attempts: {last}")

    def wait_nominal(self):
        """The phase starts only in a nominal state (review of a41c087, choice 2): the
        last three local windows below the recovery threshold and no episode open,
        waited for at most stabilization_sec after the reference."""
        deadline = self.ops.mono() + self.p["stabilization_sec"]
        while True:
            self.ops.truth_update()
            state = self.ops.truth_state()
            windows = state["last_windows"]
            if not state["incident"] and len(windows) >= 3 and all(w["p95"] < RECOVERY_MS for w in windows[-3:]):
                return self.mark("nominal_before_phase", state=state)
            if self.ops.mono() >= deadline:
                self.mark("not_nominal_before_phase", state=state)
                raise Aborted("not nominal before the phase")
            self._check_guard()
            self.ops.sleep(self.p["poll_sec"])

    def pulse(self, start_utc):
        command = {"run": self.run_id, "id": f"{self.run_id}-pulse", "start_utc": start_utc,
                   "high": self.p["high_ms"], "duration_s": self.p["pulse_sec"], "low": self.p["nominal_ms"]}
        self.ops.write_command("pulse.json", command)
        return self.mark("pulse_written", command=command, programmed_end_utc=start_utc + self.p["pulse_sec"])

    # -- the partition --
    def guard_remaining(self):
        if self.guard_start_mono is None:
            return math.inf
        return self.guard_start_mono + self.p["guard_sec"] - GUARD_TRUNCATION_SEC - self.ops.mono()

    def cut(self):
        self.guard_start_mono = self.mark("guard_start")["mono"]
        pid = self.ops.guard_start(self.p["guard_sec"])
        alive = self.ops.guard_alive(pid)
        self.mark("guard_started", pid=pid, alive=alive, max_sec=self.p["guard_sec"])
        if not alive:
            raise Aborted("the guard is not running: no DROP without it")
        self.applied = True
        self.first_drop = self.mark("drop_apply_start", peers=self.peers)
        ok, detail = self.ops.apply()
        self.mark("drop_apply_end", ok=ok, detail=detail)
        if not ok:
            self.restore("apply failed")
            raise Aborted(f"partition apply failed: {detail}")
        self.mark("cut_monitor_started", **self.ops.monitor_start())
        status = self.ops.status()
        self.mark("cut_status", status=status)
        probes = self.ops.probe(self.cut_targets)
        reasons = cut_check(status, self.peers, probes)
        verified = self.mark("cut_verified" if not reasons else "cut_not_verified", probes=probes,
                             reasons=reasons)
        if reasons:
            self.restore("cut not verified")
            raise Aborted(f"cut not verified: {reasons}")
        return verified

    def _budget(self):
        return min(COMMAND_TIMEOUT_SEC, self.guard_remaining() - RESTORE_MARGIN_SEC)

    def restore(self, reason):
        self.ops.monitor_stop()             # signalled, not waited: the restore keeps its time
        self.mark("restore_start", reason=reason, guard_remaining_sec=self.guard_remaining(),
                  status=self.ops.status(timeout=max(1.0, self._budget())))
        end = None
        for attempt in range(1, self.p["restore_attempts"] + 1):
            budget = self._budget()
            if budget < 1.0:
                self.mark("restore_left_to_guard", attempt=attempt, guard_remaining_sec=self.guard_remaining())
                raise Interrupted("no time left before the guard: its removal ends the run")
            self.mark("remove_start", attempt=attempt, timeout_sec=budget)
            ok, detail = self.ops.remove(timeout=budget)
            removed = self.mark("remove_end", attempt=attempt, ok=ok, detail=detail)
            status = self.ops.status(timeout=max(1.0, self._budget()))
            clean = not status.get("jumps") and not status.get("chain")
            self.mark("restore_status", attempt=attempt, status=status, clean=clean)
            if clean:
                end = removed
                break
            if self._budget() > 2.0:
                self.ops.sleep(1.0)
        if end is None:
            self.mark("restore_failed")
            raise Interrupted("the partition could not be removed: the guard removes it")
        self.restored_clean = True
        self.ops.mark_restored()
        self.mark("partition_restored_written", restore_end_mono=end["mono"], restore_end_utc=end["utc"])
        for _ in range(self.p["restore_probe_rounds"]):
            probes = self.ops.probe(self.restore_targets)
            back = bool(probes) and all(r.get("result") == "connected" for r in probes.values())
            self.mark("restore_probe", probes=probes, connected=back)
            if back:
                break
            self.ops.sleep(2.0)
        return end

    def wait_return(self, after_mono, deadline_mono):
        """The local return of the first episode entered after `after_mono`."""
        while self.ops.mono() < deadline_mono:
            for episode in self.ops.truth_update():
                if episode["entered"] > after_mono and episode["returned"] is not None:
                    return episode
            self._check_guard()
            self.ops.sleep(min(self.p["poll_sec"], max(0.0, deadline_mono - self.ops.mono())))
        return None

    # -- the cell --
    def _run(self):
        self.mark("clock_before", clocks=self.ops.clock())
        self.wait_metrics()
        self.arm()
        reference = self.mark("reference_start")
        self.hold_until(reference["mono"] + self.p["reference_sec"])
        self.mark("reference_end", truth=self.ops.truth_summary())
        self.wait_nominal()
        self.mark("px4_frozen", states=self.ops.freeze_px4())
        start = self.mark("phase_start", case=self.case, params=self.p)
        offset, pulse_sec, horizon_sec = self.p["pulse_offset_sec"], self.p["pulse_sec"], self.p["horizon_sec"]
        if self.case == "control":
            horizon = start["mono"] + offset + pulse_sec + horizon_sec
            self.mark("horizon_fixed", horizon_mono=horizon, basis="programmed end of the pulse")
            self.pulse(start["utc"] + offset)
        else:
            verified = self.cut()
            if self.case == "partition-only":
                self.hold_until(verified["mono"] + self.p["cut_only_sec"])
                end = self.restore(f"end of the {self.p['cut_only_sec']:.0f} s cut")
            else:
                self.pulse(verified["utc"] + offset)
                deadline = self.first_drop["mono"] + self.p["restore_deadline_sec"]
                episode = self.wait_return(verified["mono"] + offset, deadline)
                if episode is None:
                    self.mark("local_return_not_seen", deadline_mono=deadline)
                    end = self.restore("restore deadline without a local return")
                else:
                    delay = self.p["restore_after_return_sec"][self.case]
                    self.mark("local_return_seen", episode=episode, restore_at_mono=episode["returned"] + delay,
                              late_sec=max(0.0, self.ops.mono() - (episode["returned"] + delay)))
                    self.hold_until(min(episode["returned"] + delay, deadline))
                    end = self.restore(f"{delay:.0f} s after the local return")
            horizon = end["mono"] + horizon_sec
            self.mark("horizon_fixed", horizon_mono=horizon, basis="end of the restore command")
        self.hold_until(horizon)
        self.mark("horizon_end", truth=self.ops.truth_summary())
        self.mark("clock_after", clocks=self.ops.clock())
        self.mark("peers_end", peers=self.ops.peers())

    def run(self):
        status = "completed"
        try:
            self._run()
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
            if self.applied and not self.restored_clean:
                try:
                    self.restore("cleanup")
                except Exception as exc:  # noqa: BLE001
                    status = "interrupted"
                    self.mark("cleanup_failed", error=f"{type(exc).__name__}: {exc}"[:300])
            self.mark("driver_end", status=status)
        return status


# ---- the live side effects --------------------------------------------------

class LiveOps:
    """The local copy runs on its own thread (every COPY_PERIOD_SEC, each read
    recorded in harness/copy-reads.jsonl), so a probe or a remove that blocks the
    driver for seconds never leaves the copy, and the incremental truth, behind.
    While the cut is applied, two more threads read it (monitor_start)."""

    COPY_PERIOD_SEC = 0.5
    MONITOR_PERIOD_SEC = 1.0
    PING_DEADLINE_SEC = 1.0              # the ICMP echo of each rules sample
    MONITOR_TIMEOUT_SEC = 6.0            # a rules read that blocks is killed: a gap, the thread goes on
    # the persistent TCP probe (decision after the third round's cell 5)
    PROBE_DEADLINE_SEC = 0.8             # < the period: an attempt ends before the next slot
    PROBE_LATE_MAX_SEC = 0.5             # a slot later than this is skipped, not sent
    PROBE_MAX_SEC = 130.0                # the process ends on its own: the guard's 120 s + 10
    PROBE_STOP_WAIT_SEC = 3.0            # after the stop file, then killed
    PROBE_STOP_WRITE_SEC = 5.0           # the stop file's own docker exec

    def __init__(self, args, start_copier=True):
        self.a = args
        self.result_dir = args.result_dir
        os.makedirs(os.path.join(self.result_dir, "harness"), exist_ok=True)
        self._events = s2_control.ProgressiveReader(
            args.node, f"{args.control_dir}/events.jsonl", os.path.join(self.result_dir, "harness/events.progressive.jsonl"))
        self._truth = s2_control.LocalTruth(s2_control.ProgressiveReader(
            args.node, f"{args.control_dir}/samples.jsonl",
            os.path.join(self.result_dir, "harness/samples.progressive.jsonl")))
        self._truth_log = open(os.path.join(self.result_dir, "truth-incremental.jsonl"), "a", buffering=1)
        self._copy_log = open(os.path.join(self.result_dir, "harness/copy-reads.jsonl"), "a", buffering=1)
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._peers = json.loads(args.peers)
        self._clock_targets = json.loads(args.clock_targets)
        self._px4_files = json.loads(args.px4_files)
        self._hold_targets = list(getattr(args, "hold_targets", None) or [])
        self._runner = s2_control.run
        self._copier = threading.Thread(target=self._copy_loop, name="s2-copy", daemon=True)
        self._monitor_stop = threading.Event()
        self._monitors = []
        if start_copier:
            self._copier.start()

    def _copy_once(self):
        m0, w0 = time.monotonic(), time.time()
        with self._lock:
            errors, samples, windows = len(self._truth.reader.errors), self._truth.samples, len(self._truth.truth.windows)
            self._truth.update()
            record = {"m0": m0, "w0": w0, "m1": time.monotonic(), "w1": time.time(),
                      "offset": self._truth.reader.offset, "new_samples": self._truth.samples - samples}
            if len(self._truth.reader.errors) != errors:
                record["error"] = self._truth.reader.errors[-1]
            self._copy_log.write(json.dumps(record) + "\n")
            if len(self._truth.truth.windows) != windows:
                self._truth_log.write(json.dumps({"m": record["m1"], "w": record["w1"], **self._truth.truth.summary(),
                                                  "samples": self._truth.samples,
                                                  "other_samples": self._truth.other_samples}) + "\n")

    def _copy_loop(self):
        while not self._stop.is_set():
            started = time.monotonic()
            try:
                self._copy_once()
            except Exception as exc:  # noqa: BLE001 -- recorded, the copy goes on
                self._copy_log.write(json.dumps({"m0": started, "w0": time.time(), "m1": time.monotonic(),
                                                 "w1": time.time(), "error": f"{type(exc).__name__}: {exc}"[:200]})
                                     + "\n")
            self._stop.wait(max(0.0, self.COPY_PERIOD_SEC - (time.monotonic() - started)))

    def stop_copier(self):
        self._stop.set()
        if self._copier.is_alive():
            self._copier.join(10)

    def close(self):
        self.stop_copier()
        self.monitor_join()
        for handle in (self._truth_log, self._copy_log):
            handle.close()

    # -- the cut read every second (decision after the fifth qualification) --
    def monitor_start(self):
        """Two threads until monitor_stop. Every MONITOR_PERIOD_SEC the rules (the
        jumps' positions, the chain, the drops, the counters) and an ICMP echo to every
        peer from the node's own namespace, each record bracketed by host reads (m0/w0,
        m1/w1), in cut-samples.jsonl. One TCP probe process in drone01's harness for the
        whole monitor (s2_partition.PROBE_LOOP: slots on a monotonic grid, planned
        instant, send, outcome), each line stamped at reception (m, w), in
        cut-probes.jsonl. A connection, a refusal or an echo reply crossed the cut; a
        missed read or slot is a gap. About one read a second: a shorter interruption
        of the cut is not excluded."""
        self._monitor_stop.clear()
        self._monitors = [
            threading.Thread(target=self._monitor_loop, args=(self._sample_once, "cut-samples.jsonl"),
                             name="s2-cut-rules", daemon=True),
            threading.Thread(target=self._probe_process, args=("cut-probes.jsonl",), name="s2-cut-probes",
                             daemon=True)]
        for thread in self._monitors:
            thread.start()
        return {"period_sec": self.MONITOR_PERIOD_SEC, "ping_deadline_sec": self.PING_DEADLINE_SEC,
                "probe": {"period_sec": self.MONITOR_PERIOD_SEC, "deadline_sec": self.PROBE_DEADLINE_SEC,
                          "late_max_sec": self.PROBE_LATE_MAX_SEC, "max_sec": self.PROBE_MAX_SEC},
                "hold_targets": self._hold_targets, "icmp_peers": [p["ipv4"] for p in self._peers]}

    def monitor_stop(self):
        self._monitor_stop.set()

    def monitor_join(self, timeout=15.0):
        self._monitor_stop.set()
        for thread in self._monitors:
            if thread.is_alive():
                thread.join(timeout)

    def _monitor_loop(self, read_once, name):
        with open(os.path.join(self.result_dir, name), "a", buffering=1) as out:
            while not self._monitor_stop.is_set():
                started, w0 = time.monotonic(), time.time()
                try:
                    record = read_once()
                except Exception as exc:  # noqa: BLE001 -- recorded, a gap
                    record = {"m0": started, "w0": w0, "m1": time.monotonic(), "w1": time.time(),
                              "error": f"{type(exc).__name__}: {exc}"[:200]}
                out.write(json.dumps(record) + "\n")
                self._monitor_stop.wait(max(0.0, self.MONITOR_PERIOD_SEC - (time.monotonic() - started)))

    def _sample_once(self):
        m0, w0 = time.monotonic(), time.time()
        rc, out, err = self._runner(["docker", "exec", self.a.node, "sh", "-c",
                                     s2_partition.sample_script(self._peers, self.PING_DEADLINE_SEC)],
                                    timeout=self.MONITOR_TIMEOUT_SEC)
        record = {"m0": m0, "w0": w0, "m1": time.monotonic(), "w1": time.time(), **s2_partition.parse_sample(out)}
        if rc != 0:
            record["error"] = (err or f"rc {rc}").strip()[:200]
        return record

    def _probe_process(self, name):
        """The persistent probe: started once, its lines stamped at reception; at the
        stop signal this thread -- never the restore -- writes the stop file through the
        node, waits PROBE_STOP_WAIT_SEC for the process, then kills it; how it ended is
        recorded."""
        token = f"{os.getpid()}-{time.monotonic_ns()}"
        stop_path = f"{self.a.control_dir}/probe-stop-{token}"
        cmd = s2_partition.probe_loop_command(self.a.node, self.a.harness_cid, self._hold_targets,
                                              self.MONITOR_PERIOD_SEC, self.PROBE_DEADLINE_SEC,
                                              self.PROBE_LATE_MAX_SEC, self.PROBE_MAX_SEC, stop_path)
        with open(os.path.join(self.result_dir, name), "a", buffering=1) as out, \
                open(os.path.join(self.result_dir, "cut-probes.stderr"), "a") as errors:
            lock = threading.Lock()

            def write(record):
                with lock:
                    out.write(json.dumps(record) + "\n")
            try:
                proc = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=errors,
                                        text=True)
            except OSError as exc:
                write({"event": "host", "m": time.monotonic(), "w": time.time(),
                       "error": f"{type(exc).__name__}: {exc}"[:200]})
                return

            def pump():
                for line in proc.stdout:
                    m, w = time.monotonic(), time.time()
                    try:
                        record = json.loads(line)
                    except ValueError:
                        record = {"unparsed": line.strip()[:200]}
                    write({"m": m, "w": w, **record})
            reader = threading.Thread(target=pump, name="s2-cut-probes-reader", daemon=True)
            reader.start()
            self._monitor_stop.wait()
            asked = time.monotonic()
            rc, _, err = self._runner(["docker", "exec", self.a.node, "sh", "-c", f"touch {shlex.quote(stop_path)}"],
                                      timeout=self.PROBE_STOP_WRITE_SEC)
            try:
                proc.wait(timeout=self.PROBE_STOP_WAIT_SEC)
                how = "stop file"
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()
                how = "killed"
            reader.join(5)
            proc.stdout.close()
            write({"event": "host-stop", "m": time.monotonic(), "w": time.time(), "how": how,
                   "stop_written": rc == 0, "stop_error": None if rc == 0 else (err or f"rc {rc}").strip()[:200],
                   "exit": proc.returncode, "stop_sec": round(time.monotonic() - asked, 3)})

    mono = staticmethod(time.monotonic)
    utc = staticmethod(time.time)
    sleep = staticmethod(time.sleep)

    def write_command(self, name, command):
        s2_control.write_command(self.a.node, self.a.control_dir, name, command)

    def events(self):
        return self._events.read()

    def truth_update(self):
        """The episodes as the copier last left them (a copy: the copier goes on)."""
        with self._lock:
            return [dict(e) for e in self._truth.truth.episodes]

    def truth_summary(self):
        with self._lock:
            return {**self._truth.truth.summary(), "samples": self._truth.samples,
                    "other_samples": self._truth.other_samples, "read_errors": self._truth.reader.errors[-5:]}

    def last_sample(self):
        with self._lock:
            return self._truth.truth.last_sample

    def truth_state(self):
        with self._lock:
            return {"incident": self._truth.truth.incident,
                    "last_windows": [dict(w) for w in self._truth.truth.windows[-3:]]}

    def guard_start(self, max_sec, directory=None):
        """Detached twice: an intermediate shell starts the guard in its own session
        and exits, so the guard is not even the driver's child -- it outlives it,
        and init, not the driver, collects it. Returns the guard's pid."""
        directory = directory or self.result_dir
        os.makedirs(directory, exist_ok=True)
        path = os.path.join(directory, "guard.sh")
        with open(path, "w") as h:
            h.write(s2_partition.guard_script(self.a.node, directory, max_sec))
        started = subprocess.run(
            ["sh", "-c", 'setsid sh "$1" >>"$2" 2>&1 </dev/null & echo $!', "guard", path,
             os.path.join(directory, "guard.log")],
            capture_output=True, text=True, timeout=10)
        pid = int(started.stdout.strip())
        # `$!` comes back before the child has become the guard: wait (bounded) for it
        # to run the script in a session of its own
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline and not self.guard_alive(pid, directory):
            time.sleep(0.02)
        return pid

    def guard_alive(self, pid, directory=None):
        """Running the guard's script, in a session of its own."""
        try:
            with open(f"/proc/{pid}/cmdline", "rb") as h:
                cmdline = h.read().split(b"\0")
            script = os.path.join(directory or self.result_dir, "guard.sh").encode()
            return os.getsid(pid) == pid and script in cmdline
        except OSError:
            return False

    def guard_fired(self, directory=None):
        return os.path.exists(os.path.join(directory or self.result_dir, "guard-fired"))

    def _exec(self, script, timeout=30):
        rc, out, err = self._runner(["docker", "exec", self.a.node, "sh", "-c", script], timeout=timeout)
        return rc, out, err

    def apply(self):
        rc, out, err = self._exec(s2_partition.apply_script(self._peers))
        return rc == 0, (err or out).strip()[:300]

    def remove(self, timeout=COMMAND_TIMEOUT_SEC):
        rc, out, err = self._exec(s2_partition.remove_script(), timeout=timeout)
        return rc == 0, (err or out).strip()[:300]

    def status(self, timeout=COMMAND_TIMEOUT_SEC):
        rc, out, err = self._exec(s2_partition.status_script(), timeout=timeout)
        status = s2_partition.parse_status(out)
        if rc not in (0, 1):
            status["error"] = (err or f"rc {rc}").strip()[:300]
        return status

    def probe(self, targets):
        cmd = s2_partition.probe_command(self.a.node, self.a.harness_cid, targets)
        rc, out, err = s2_control.run(cmd, timeout=10 + 4 * len(targets))
        try:
            return s2_partition.parse_probe(out)
        except (ValueError, IndexError):
            return {t: {"target": t, "result": f"probe error: {(err or out).strip()[:120]}"} for t in targets}

    def mark_restored(self):
        with open(os.path.join(self.result_dir, "partition-restored"), "w") as h:
            h.write(f"{time.time():.6f}\n")

    def clock(self):
        out = {}
        for target in self._clock_targets:
            readings = s2_control.clock_readings(target["node"], target["cid"])
            out[target["label"]] = {"node": target["node"], "cid": target["cid"], "readings": readings,
                                    "summary": s2_control.clock_summary(readings)}
        return out

    def freeze_px4(self):
        continuity = _load("px4_status_continuity")
        out = {}
        for drone, path in self._px4_files.items():
            try:
                with open(path, errors="replace") as h:
                    reads = continuity.parse_reads(h.read())
            except OSError as exc:
                out[drone] = {"error": str(exc)[:200]}
                continue
            last = next(((ns, s) for ns, s in reversed(reads) if s is not None), None)
            out[drone] = None if last is None else {"read_ns": last[0], **last[1]}
        return out

    def peers(self):
        try:
            return s2_partition.peers(s2_partition.node_network(self.a.node), self.a.node)
        except Exception as exc:  # noqa: BLE001
            return {"error": f"{type(exc).__name__}: {exc}"[:300]}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--result-dir", required=True)
    parser.add_argument("--case", required=True, choices=CASES)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--node", required=True, help="drone01's node container")
    parser.add_argument("--control-dir", default="/var/lib/s2-harness")
    parser.add_argument("--harness-cid", required=True)
    parser.add_argument("--target-node", required=True, help="the analytics' ROS node name")
    parser.add_argument("--peers", required=True, help="JSON: the peers frozen before the phase")
    parser.add_argument("--cut-targets", nargs="+", required=True)
    parser.add_argument("--hold-targets", nargs="+", required=True,
                        help="TCP targets probed every second during the cut (ports no counting rule counts)")
    parser.add_argument("--clock-targets", required=True, help='JSON: [{"label","node","cid"}]')
    parser.add_argument("--px4-files", required=True, help='JSON: {"drone01": path, ...}')
    args = parser.parse_args(argv)
    marks = Marks(os.path.join(args.result_dir, "phases.jsonl"))
    phase = Phase(LiveOps(args), args.case, args.run_id, args.target_node, json.loads(args.peers),
                  args.cut_targets, args.cut_targets, marks)

    def stop(signum, _frame):
        raise Interrupted(f"signal {signum}")
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    try:
        return EXIT[phase.run()]
    finally:
        phase.ops.close()
        marks.close()


if __name__ == "__main__":
    sys.exit(main())
