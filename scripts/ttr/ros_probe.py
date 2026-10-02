#!/usr/bin/env python3
"""Time-to-rebuild ROS probe: runs in the observer Pod (namespace ttr-observer, the
control-plane image both variants share), one read attempt per second, one JSON line per
attempt on stdout.

  ros_probe.py SPEC_JSON      SPEC_JSON = {robot: {key: node-name regex}} (spec.probe_spec)

Each attempt reads the ROS graph (a super client of the bench's discovery server sees every
node), matches each expected node in /<robot> by full regex, and asks every match its state
through the lifecycle GetState service -- the service `ros2 lifecycle get` calls -- all in
parallel, with a 0.8 s deadline. Per node: the state label, or absent / ambiguous (more
than one match) / no_service / timeout / error. Never the variant's status.

Line: {"t": wall clock at the end of the attempt, "t_start", "seq", "nodes": {"drone01/
analytics": {"name", "state"}}, "error": null or the exception of a failed attempt}. The
Pod shares the host's kernel clock, so "t" is comparable with the runner's t0.
"""

import json
import re
import sys
import threading
import time

import rclpy
from lifecycle_msgs.srv import GetState
from rclpy.executors import MultiThreadedExecutor

DEADLINE_SEC = 0.8
PERIOD_SEC = 1.0


def main(spec):
    rclpy.init()
    node = rclpy.create_node("ttr_probe")
    executor = MultiThreadedExecutor(num_threads=4)
    executor.add_node(node)
    threading.Thread(target=executor.spin, daemon=True).start()
    clients = {}
    start = time.monotonic()
    seq = 0
    while rclpy.ok():  # rclpy turns SIGTERM/SIGINT into a shutdown: stop there
        t_start = time.time()
        line = {"seq": seq, "t_start": t_start, "nodes": {}, "error": None}
        try:
            graph = node.get_node_names_and_namespaces()
            pending = {}
            for robot, keys in spec.items():
                for key, pattern in keys.items():
                    names = sorted(n for n, ns in graph if ns == f"/{robot}" and re.fullmatch(pattern, n))
                    entry = {"name": names[0] if len(names) == 1 else names, "state": None}
                    line["nodes"][f"{robot}/{key}"] = entry
                    if not names:
                        entry["state"] = "absent"
                        continue
                    if len(names) > 1:
                        entry["state"] = "ambiguous"
                        continue
                    service = f"/{robot}/{names[0]}/get_state"
                    if service not in clients:
                        clients[service] = node.create_client(GetState, service)
                    if not clients[service].service_is_ready():
                        entry["state"] = "no_service"
                        continue
                    pending[f"{robot}/{key}"] = clients[service].call_async(GetState.Request())
            deadline = time.monotonic() + DEADLINE_SEC
            while pending and time.monotonic() < deadline and not all(f.done() for f in pending.values()):
                time.sleep(0.01)
            for key, future in pending.items():
                if not future.done():
                    future.cancel()
                    line["nodes"][key]["state"] = "timeout"
                elif future.exception() is not None:
                    line["nodes"][key]["state"] = "error"
                else:
                    line["nodes"][key]["state"] = future.result().current_state.label
        except Exception as exc:  # an attempt that fails is recorded, never fatal
            line["error"] = repr(exc)
        line["t"] = time.time()
        print(json.dumps(line), flush=True)
        seq += 1
        # one attempt per second; an attempt that overran shifts the schedule, never a burst
        if start + seq * PERIOD_SEC < time.monotonic():
            start = time.monotonic() - seq * PERIOD_SEC
        time.sleep(max(0.0, start + seq * PERIOD_SEC - time.monotonic()))


if __name__ == "__main__":
    main(json.loads(sys.argv[1]))
