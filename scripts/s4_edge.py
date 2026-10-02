#!/usr/bin/env python3
"""S4 edge-node fault (R13, docs/R13_S4_EDGE_PILOT_PROTOCOL.md): the node's k3d
container stopped with `docker stop --time 0` and started again, measured from
OUTSIDE the cluster.

  guard_script   a detached host process: at its deadline (max_sec after its own
                 start, or an absolute UTC deadline: the cells give T0 + 120 s, so
                 the instant it was started never moves it), unless the run wrote the
                 `edge-restored` marker, it runs `docker start`, records its own
                 intervention (instant, command exit) and then checks with
                 `docker inspect` that the container is Running, recording that too;
  inspect        `docker inspect` of the node: Running, StartedAt, FinishedAt, Pid;
  kubelet_probe  a TCP connect from the host to the node's kubelet (10250), with a
                 deadline: connected, refused, timeout or error;
  Sampler        both, every 0.2 s on its own thread, each record with the host's
                 monotonic clock and UTC around it: the fault's start is the first
                 read with the container stopped AND the kubelet not connected; the
                 node's return the first with it running AND the kubelet connected.
The API's NotReady is read elsewhere and is never the injection's timestamp.
Tested offline: operator/tests/test_s4_edge.py.
"""

import json
import math
import os
import shlex
import socket
import subprocess
import threading
import time

GUARD_MAX_SEC = 120
PROBE_TIMEOUT_SEC = 0.5
SAMPLE_PERIOD_SEC = 0.2
KUBELET_PORT = 10250


def run(cmd, timeout=10.0):
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return p.returncode, p.stdout, p.stderr
    except subprocess.TimeoutExpired:
        return None, "", "timeout"


def guard_script(node, result_dir, max_sec=GUARD_MAX_SEC, deadline_utc=None):
    """Whole-second clock, as the S2 guard; with deadline_utc the end is that instant
    (rounded up to the second), not max_sec after the guard's start. On firing:
    `guard-fired` gets the instant, `guard-start-exit` the exit of `docker start`,
    `guard-running` the Running flag read afterwards (with its instant)."""
    q = shlex.quote
    restored = q(f"{result_dir}/edge-restored")
    fired, start_exit, running = (q(f"{result_dir}/{n}") for n in ("guard-fired", "guard-start-exit",
                                                                    "guard-running"))
    end = str(math.ceil(deadline_utc)) if deadline_utc is not None else f"$(($(date +%s) + {int(max_sec)}))"
    return "\n".join([
        "#!/bin/sh",
        f"end={end}",
        f'while [ "$(date +%s)" -lt "$end" ]; do [ -e {restored} ] && exit 0; sleep 1; done',
        f"date -u +%s.%N > {fired}",
        f"docker start {q(node)} >/dev/null 2>&1; echo $? > {start_exit}",
        "i=0",
        "while [ $i -lt 30 ]; do",
        f"  r=$(docker inspect -f '{{{{.State.Running}}}}' {q(node)} 2>/dev/null)",
        '  [ "$r" = true ] && break',
        "  i=$((i + 1)); sleep 1",
        "done",
        f'echo "$(date -u +%s.%N) ${{r:-unknown}}" > {running}',
    ]) + "\n"


def parse_inspect(stdout):
    state = json.loads(stdout)[0]["State"]
    return {"running": bool(state.get("Running")), "status": state.get("Status"), "pid": state.get("Pid"),
            "started_at": state.get("StartedAt"), "finished_at": state.get("FinishedAt")}


def inspect(node, runner=run):
    rc, out, err = runner(["docker", "inspect", node], timeout=5.0)
    if rc != 0:
        return {"error": (err or f"rc {rc}").strip()[:200]}
    try:
        return parse_inspect(out)
    except (ValueError, KeyError, IndexError) as exc:
        return {"error": f"unreadable inspect: {exc}"[:200]}


def kubelet_probe(address, port=KUBELET_PORT, timeout=PROBE_TIMEOUT_SEC):
    s = socket.socket()
    s.settimeout(timeout)
    m = time.monotonic()
    try:
        s.connect((address, port))
        result = "connected"
    except socket.timeout:
        result = "timeout"
    except ConnectionRefusedError:
        result = "refused"
    except OSError as exc:
        result = "oserror:" + str(exc).replace(" ", "_")
    finally:
        s.close()
    return {"result": result, "elapsed": round(time.monotonic() - m, 4)}


def node_address(node, runner=run):
    rc, out, _ = runner(["docker", "inspect", "-f", "{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}",
                         node], timeout=5.0)
    address = out.strip() if rc == 0 else ""
    if not address:
        raise RuntimeError(f"no address for {node}")
    return address


def down(record):
    """The fault is under way: container stopped and kubelet not connected."""
    return (record.get("inspect") or {}).get("running") is False and \
        (record.get("kubelet") or {}).get("result") not in (None, "connected")


def up(record):
    """The node is back: container running and kubelet connected."""
    return (record.get("inspect") or {}).get("running") is True and \
        (record.get("kubelet") or {}).get("result") == "connected"


def first(records, predicate, after_mono=float("-inf")):
    return next((r for r in records if r["m0"] >= after_mono and predicate(r)), None)


class Sampler:
    """docker inspect + kubelet probe every SAMPLE_PERIOD_SEC, on its own thread,
    into edge-samples.jsonl; stop() is a signal, join() bounded."""

    def __init__(self, node, address, out_path, period=SAMPLE_PERIOD_SEC, runner=run,
                 probe=kubelet_probe):
        self.node, self.address, self.period = node, address, period
        self._runner, self._probe = runner, probe
        self._out = open(out_path, "a", buffering=1)
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._loop, name="s4-edge-sampler", daemon=True)
        self.records = []
        self._lock = threading.Lock()

    def start(self):
        self._thread.start()

    def _loop(self):
        while not self._stop.is_set():
            started = time.monotonic()
            record = {"m0": started, "w0": time.time()}
            record["inspect"] = inspect(self.node, self._runner)
            record["kubelet"] = self._probe(self.address)
            record.update(m1=time.monotonic(), w1=time.time())
            with self._lock:
                self.records.append(record)
            self._out.write(json.dumps(record) + "\n")
            self._stop.wait(max(0.0, self.period - (time.monotonic() - started)))

    def snapshot(self):
        with self._lock:
            return list(self.records)

    def stop(self):
        self._stop.set()

    def join(self, timeout=10.0):
        self._stop.set()
        if self._thread.is_alive():
            self._thread.join(timeout)
        self._out.close()


def guard_start(node, directory, max_sec=GUARD_MAX_SEC, deadline_utc=None):
    """Detached twice (the S2 guard's pattern): its own session, not the driver's
    child; returns its pid once it is running the script."""
    os.makedirs(directory, exist_ok=True)
    path = os.path.join(directory, "edge-guard.sh")
    with open(path, "w") as h:
        h.write(guard_script(node, directory, max_sec, deadline_utc))
    started = subprocess.run(["sh", "-c", 'setsid sh "$1" >>"$2" 2>&1 </dev/null & echo $!', "edge-guard", path,
                              os.path.join(directory, "edge-guard.log")],
                             capture_output=True, text=True, timeout=10)
    pid = int(started.stdout.strip())
    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline and not guard_alive(pid, directory):
        time.sleep(0.02)
    return pid


def guard_alive(pid, directory):
    try:
        with open(f"/proc/{pid}/cmdline", "rb") as h:
            cmdline = h.read().split(b"\0")
        return os.getsid(pid) == pid and os.path.join(directory, "edge-guard.sh").encode() in cmdline
    except OSError:
        return False


def mark_restored(directory):
    with open(os.path.join(directory, "edge-restored"), "w") as h:
        h.write(f"{time.time():.6f}\n")


def guard_record(directory):
    """What the guard recorded, if it fired."""
    def read(name):
        try:
            with open(os.path.join(directory, name)) as h:
                return h.read().strip()
        except OSError:
            return None
    fired = read("guard-fired")
    if fired is None:
        return None
    running = (read("guard-running") or "").split()
    return {"fired_utc": float(fired), "start_exit": read("guard-start-exit"),
            "running_checked_utc": float(running[0]) if running else None,
            "running": running[1] if len(running) > 1 else None}
