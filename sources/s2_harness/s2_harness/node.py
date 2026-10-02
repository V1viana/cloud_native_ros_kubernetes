"""ROS wrapper of the S2 harness (core.py): one node, one participant.

The executor spins in the main thread; a worker thread polls the control
directory and runs the injector's blocking calls, which wait on futures the
executor resolves. SIGTERM stops a running pulse and restores the parameter
before the executor stops, so the restore can still be answered.
"""

import os
import signal
import threading
import time

from cloud_native_robotics_interfaces.msg import MetricSample
from rcl_interfaces.msg import Parameter, ParameterType, ParameterValue
from rcl_interfaces.srv import GetParameters, SetParameters
import rclpy
from rclpy.callback_groups import MutuallyExclusiveCallbackGroup, ReentrantCallbackGroup
from rclpy.executors import MultiThreadedExecutor
from rclpy.node import Node
from rclpy.qos import QoSProfile, ReliabilityPolicy

from .core import CommandPoller, Injector, JsonlLog, dispatch, write_sample


class ParameterPort:
    """The analytics node's own parameter services, called with a bounded wait."""

    def __init__(self, node, timeout_sec, group):
        self._node, self._timeout, self._group = node, timeout_sec, group
        self._get = self._set = None
        self._name = None

    def bind(self, target_node, parameter):
        self._name = parameter
        self._get = self._node.create_client(GetParameters, f"{target_node}/get_parameters",
                                             callback_group=self._group)
        self._set = self._node.create_client(SetParameters, f"{target_node}/set_parameters",
                                             callback_group=self._group)
        for client in (self._get, self._set):
            if not client.wait_for_service(timeout_sec=self._timeout):
                raise RuntimeError(f"service {client.srv_name} unavailable")

    def _call(self, client, request):
        future = client.call_async(request)
        deadline = time.monotonic() + self._timeout
        while time.monotonic() < deadline:
            if future.done():
                if future.exception() is not None:
                    raise RuntimeError(str(future.exception()))
                return future.result()
            time.sleep(0.005)
        client.remove_pending_request(future)
        raise TimeoutError(f"{client.srv_name}: no answer within {self._timeout}s")

    def get(self):
        response = self._call(self._get, GetParameters.Request(names=[self._name]))
        value = response.values[0]
        if value.type == ParameterType.PARAMETER_DOUBLE:
            return float(value.double_value)
        if value.type == ParameterType.PARAMETER_INTEGER:
            return float(value.integer_value)
        raise RuntimeError(f"{self._name} has type {value.type}, not a number")

    def set(self, value):
        parameter = Parameter(name=self._name, value=ParameterValue(
            type=ParameterType.PARAMETER_DOUBLE, double_value=float(value)))
        response = self._call(self._set, SetParameters.Request(parameters=[parameter]))
        result = response.results[0]
        return bool(result.successful), result.reason


class S2Harness(Node):
    def __init__(self):
        super().__init__("s2_harness")
        self.declare_parameter("metrics_topic", "/drone01/analytics/metrics")
        self.declare_parameter("control_dir", "/var/lib/s2-harness")
        self.declare_parameter("service_timeout_sec", 2.0)
        self.declare_parameter("poll_period_sec", 0.1)
        self.declare_parameter("run_id", "")
        directory = self.get_parameter("control_dir").value
        os.makedirs(directory, exist_ok=True)
        self.events = JsonlLog(os.path.join(directory, "events.jsonl"))
        self.samples = JsonlLog(os.path.join(directory, "samples.jsonl"))
        group = ReentrantCallbackGroup()
        port = ParameterPort(self, float(self.get_parameter("service_timeout_sec").value), group)
        self.injector = Injector(port, self.events)
        run_id = self.get_parameter("run_id").value
        self._poller = CommandPoller(directory, self.events, run_id)
        self._poller.prime()
        self._poll_period = float(self.get_parameter("poll_period_sec").value)
        topic = self.get_parameter("metrics_topic").value
        # RELIABLE like the analytics' publisher (depth 50): acknowledgements flow
        # back, so this participant and the analytics' keep hearing each other.
        # Its own mutually exclusive group: one sample at a time, recorded in
        # receive order (review of c6b6b7e).
        self._samples_group = MutuallyExclusiveCallbackGroup()
        self.create_subscription(MetricSample, topic, self._on_sample,
                                 QoSProfile(depth=50, reliability=ReliabilityPolicy.RELIABLE),
                                 callback_group=self._samples_group)
        self.stopping = threading.Event()
        self.worker = threading.Thread(target=self._work, name="s2-commands", daemon=True)
        self.events.write("started", metrics_topic=topic, control_dir=directory, pid=os.getpid(), run_id=run_id)
        self.worker.start()

    def _on_sample(self, msg):
        write_sample(self.samples, msg)

    def _work(self):
        while not self.stopping.is_set():
            for name, command in self._poller.poll():
                dispatch(self.injector, name, command, self.events)
            self.stopping.wait(self._poll_period)
        self.injector.restore_if_active()
        self.events.write("worker_stopped")

    def request_stop(self, *_):
        self.events.write("stop_requested")
        self.injector.stop()
        self.stopping.set()


def main(args=None):
    rclpy.init(args=args, signal_handler_options=rclpy.signals.SignalHandlerOptions.NO)
    node = S2Harness()
    signal.signal(signal.SIGTERM, node.request_stop)
    signal.signal(signal.SIGINT, node.request_stop)
    executor = MultiThreadedExecutor(num_threads=4)
    executor.add_node(node)
    try:
        # Keep answering while the worker restores the parameter after a stop.
        while node.worker.is_alive():
            executor.spin_once(timeout_sec=0.1)
    finally:
        node.events.write("stopped")
        executor.shutdown()
        node.destroy_node()
        if rclpy.ok():
            rclpy.shutdown()
