"""StateBridgeNode: the ROS 2 wrapper around StateBridgeCore.

The only part of the State Bridge that depends on rclpy (together with
Ros2MiddlewareAdapter): it declares the ROS parameters, builds the adapter and
the Kubernetes client, injects them into StateBridgeCore (bridge_core.py, no
ROS import) and drives its tick() from a ROS timer. Proposal S4.2: the bridge's
logic depends only on the RosMiddlewareAdapter interface (the MAL).

Runs a MultiThreadedExecutor, not spin(): Ros2MiddlewareAdapter's service
calls busy-wait on a future without spinning themselves (same pattern as
cloud_native_application_manager/lifecycle_coordinator.py), which only
resolves if another executor thread is free to service the response.
"""

import os
import threading
import time

import rclpy
from rclpy.callback_groups import MutuallyExclusiveCallbackGroup, ReentrantCallbackGroup
from rclpy.executors import MultiThreadedExecutor
from rclpy.node import Node

from .bridge_core import BridgeConfig, StateBridgeCore
from .k8s_status_client import K8sStatusClient
from .ros2_middleware_adapter import Ros2MiddlewareAdapter
from .spec_watch import SpecWatcher

# How often the wrapper asks the core whether to tick: a watched spec change is
# applied within this delay, the periodic ROS observation keeps poll_interval_sec.
_TICK_CHECK_SEC = 0.5


class StateBridgeNode(Node):
    def __init__(self, **kwargs):
        super().__init__("state_bridge", **kwargs)
        self.declare_parameter("rosmodule_name", "")
        self.declare_parameter("lifecycle_node_name", "")
        self.declare_parameter("kubernetes_namespace", "")
        self.declare_parameter("poll_interval_sec", 5.0)
        self.declare_parameter("metrics_topic", "")
        self.declare_parameter("metrics_window_sec", 10.0)
        # A/B temporal contract (V6): tumbling windows of the ROSModule's
        # spec.metricsWindowSec, evaluated every window_eval_sec as in variant A.
        self.declare_parameter("window_sec", 2.0)
        self.declare_parameter("window_eval_sec", 0.2)
        self.declare_parameter("telemetry_topic", "")
        # spec.probes.readiness's own two fields (proposal S3), threaded
        # through as static launch params by k8s_workloads.py the same way
        # metrics_topic/telemetry_topic already are.
        self.declare_parameter("readiness_topic", "")
        self.declare_parameter("readiness_base_topic", "")
        self.declare_parameter("readiness_timeout_sec", 5.0)

        value = lambda name: self.get_parameter(name).value  # noqa: E731
        config = BridgeConfig(
            rosmodule_name=value("rosmodule_name"),
            lifecycle_node_name=value("lifecycle_node_name"),
            metrics_topic=value("metrics_topic"),
            metrics_window_sec=value("metrics_window_sec"),
            telemetry_topic=value("telemetry_topic"),
            readiness_topic=value("readiness_topic"),
            readiness_base_topic=value("readiness_base_topic"),
            readiness_timeout_sec=value("readiness_timeout_sec"),
            # Downward API (k8s_workloads.py): commands carry the Pod's UID
            pod_uid=os.environ.get("POD_UID", ""),
            poll_interval_sec=value("poll_interval_sec"),
            window_sec=float(value("window_sec")),
            window_eval_sec=float(value("window_eval_sec")),
        )
        k8s = K8sStatusClient.from_service_account(
            namespace=value("kubernetes_namespace") or None)
        # The MAL's own callback group, deliberately distinct from the timer's
        # default group: tick() busy-waits inside Ros2MiddlewareAdapter's
        # service calls, so the response callback needs a DIFFERENT group to be
        # scheduled concurrently rather than queued behind the running timer.
        mal = Ros2MiddlewareAdapter(self, callback_group=ReentrantCallbackGroup())
        # Watch on this ROSModule's spec (proposal S4.2), in its own thread,
        # never inside the ROS executor (docs/CRD_CONTRACT_AUDIT.md, R4).
        self._watcher = SpecWatcher(k8s, config.rosmodule_name, self.get_logger())
        self._watcher.start()
        self._core = StateBridgeCore(config, mal, k8s, self.get_logger(),
                                     spec_source=self._watcher)
        # Option 3 of the D6 diagnosis (STATE_BRIDGE_DETAIL_TRACE): the MAL, the
        # Kubernetes client and the watch record into the core's DetailRecorder;
        # off, all three keep the null recorder and nothing is measured.
        mal.detail = k8s.detail = self._watcher.detail = self._core.detail
        self._core.detail.start()
        self._wrapper_end = {}
        # The tick diagnosis only (STATE_BRIDGE_TICK_TRACE, docs/R11_S2_PARTITION.md):
        # each callback timed by _traced; otherwise registered exactly as before.
        traced = self._core.traces_ticks
        self._timer = self.create_timer(
            _TICK_CHECK_SEC,
            self._traced("lifecycle", self._core.maybe_tick, lambda: self._timer)
            if traced else self._core.maybe_tick)
        if config.metrics_topic:
            # Its own group, not the default one shared with the tick: the tick
            # can wait up to the service timeout on GetState of a hung local node,
            # and windows must keep closing on the samples still arriving from the
            # other replicas (live, window-replicas/20260925T172454Z: sharing the
            # group, Y's bridge stopped publishing 2.6s after Y's node froze).
            self._window_timer = self.create_timer(
                config.window_eval_sec,
                self._traced("windows", self._core.evaluate_windows, lambda: self._window_timer)
                if traced else self._core.evaluate_windows,
                callback_group=MutuallyExclusiveCallbackGroup())
            # The windows' status goes out on its own thread, outside the ROS
            # executor: the timer only offers it, so a link that does not answer
            # cannot stretch the windows (D6, R11; test_window_transport.py).
            self._core.start_window_publisher()
        self.get_logger().info(
            f"State Bridge watching ROSModule '{config.rosmodule_name}' "
            f"-> lifecycle node '{config.lifecycle_node_name}'"
        )

    def _traced(self, name, callback, timer):
        """One timer callback, timed: raw times at entry and exit and rcl's view
        of the timer at entry (until-next, since-last, period, on the timer's own
        clock). No change to the timer, its group or its period; the record is
        written after the body. Measurement only (window_trace.py)."""
        detail = self._core.detail.enabled

        def run():
            entry_mono, entry_utc = time.monotonic(), time.time()
            t = timer()
            now_ns = t.clock.now().nanoseconds
            until_ns, since_ns = t.time_until_next_call(), t.time_since_last_call()
            cpu = time.thread_time() if detail else None
            result = callback()
            exit_mono = time.monotonic()
            extra = {}
            if detail:
                extra["body_cpu_us"] = round((time.thread_time() - cpu) * 1e6)
            if name == "windows":            # read after the body, outside the timing
                start, samples = self._core.open_window()
                result = {"closed": result, "open_start": start, "open_samples": samples}
            if detail:
                # the rest of the wrapper: the reads after the body, and (known only
                # after this record is written) the end of the previous invocation
                extra["reads_end_mono"] = time.monotonic()
                extra["prev_wrapper_end_mono"] = self._wrapper_end.get(name)
            self._core.trace_tick(timer=name, entry_utc=entry_utc, entry_mono=entry_mono,
                                  exit_mono=exit_mono, now_ns=now_ns, until_next_ns=until_ns,
                                  since_last_ns=since_ns, period_ns=t.timer_period_ns,
                                  native_id=threading.get_native_id(), result=result, **extra)
            if detail:
                self._wrapper_end[name] = time.monotonic()
        return run


def main(args=None):
    rclpy.init(args=args)
    node = StateBridgeNode()
    executor = MultiThreadedExecutor(num_threads=4)
    executor.add_node(node)
    try:
        executor.spin()
    except KeyboardInterrupt:
        pass
    finally:
        executor.shutdown()
        node._watcher.stop()
        node._core.stop_window_publisher(timeout=1.0)
        node._core.detail.stop(timeout=1.0)
        node.destroy_node()
        if rclpy.ok():
            rclpy.shutdown()
