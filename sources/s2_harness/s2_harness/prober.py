"""ROS wrapper of the S2 health prober (prober_core.py): one client per target,
calls made and polled from a timer, never waited on; the executor resolves them.
Its evidence goes to a hostPath on its own node, read from the host."""

import os
import time

from cloud_native_robotics_interfaces.srv import GetHealthSnapshot
import rclpy
from rclpy.callback_groups import MutuallyExclusiveCallbackGroup, ReentrantCallbackGroup
from rclpy.executors import MultiThreadedExecutor
from rclpy.node import Node

from .core import JsonlLog
from .prober_core import PERIOD_SEC, TIMEOUT_SEC, Prober, targets


class ClientPort:
    def __init__(self, node, group, run_id):
        self._node, self._group, self._run = node, group, run_id
        self._clients = {}

    def send(self, target):
        client = self._clients.get(target["service"])
        if client is None:
            client = self._node.create_client(GetHealthSnapshot, target["service"], callback_group=self._group)
            self._clients[target["service"]] = client
        request = GetHealthSnapshot.Request(correlation_id=f"s2-{self._run}-{target['name']}")
        return client, client.call_async(request)

    def poll(self, token):
        _, future = token
        if not future.done():
            return False, None, None
        return True, future.result(), future.exception()

    def cancel(self, token):
        client, future = token
        client.remove_pending_request(future)


class S2HealthProber(Node):
    def __init__(self):
        super().__init__("s2_health_prober")
        self.declare_parameter("out_dir", "/var/lib/s2-prober")
        self.declare_parameter("run_id", "")
        self.declare_parameter("period_sec", PERIOD_SEC)
        self.declare_parameter("timeout_sec", TIMEOUT_SEC)
        # the probed robots (additive, for the S4 bench: four onboard and drone03's
        # edge); the defaults are S2's own targets, unchanged
        self.declare_parameter("robots", ["drone01", "drone02", "drone03"])
        self.declare_parameter("edge_robots", ["drone01"])
        run_id = self.get_parameter("run_id").value
        if not run_id:
            raise ValueError("run_id is required")
        directory = self.get_parameter("out_dir").value
        os.makedirs(directory, exist_ok=True)
        self.log = JsonlLog(os.path.join(directory, "health.jsonl"))
        target_list = targets(tuple(self.get_parameter("robots").value),
                              tuple(self.get_parameter("edge_robots").value))
        self.prober = Prober(ClientPort(self, ReentrantCallbackGroup(), run_id), self.log, target_list,
                             period_sec=float(self.get_parameter("period_sec").value),
                             timeout_sec=float(self.get_parameter("timeout_sec").value))
        self.log.write("started", run_id=run_id, targets=[t["service"] for t in target_list], pid=os.getpid())
        self.create_timer(0.1, lambda: self.prober.tick(time.monotonic(), time.time()),
                          callback_group=MutuallyExclusiveCallbackGroup())


def main(args=None):
    rclpy.init(args=args)
    node = S2HealthProber()
    executor = MultiThreadedExecutor(num_threads=4)
    executor.add_node(node)
    try:
        executor.spin()
    except KeyboardInterrupt:
        pass
    finally:
        node.log.write("stopped")
        executor.shutdown()
        node.destroy_node()
        if rclpy.ok():
            rclpy.shutdown()
