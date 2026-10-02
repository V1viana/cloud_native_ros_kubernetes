"""Ros2MiddlewareAdapter: ROS message, subscription and service implementation.

Reimplemented independently from cloud_native_application_manager's
lifecycle_coordinator.py rather than importing it: that package is built
only into the control-plane container, while this one must run onboard, so
sharing the dependency would pull control-plane code into the onboard
image. The transition logic is intentionally close to the same shape --
GetState/ChangeState over the lifecycle_msgs services -- since it solves
the same problem.
"""

import time

from cloud_native_robotics_interfaces.msg import MetricSample
from px4_msgs.msg import VehicleStatus
from rclpy.callback_groups import ReentrantCallbackGroup
from rclpy.qos import qos_profile_sensor_data, QoSProfile, ReliabilityPolicy
from lifecycle_msgs.msg import State, Transition
from lifecycle_msgs.srv import ChangeState, GetState
from std_msgs.msg import Bool

from .window_trace import NULL_DETAIL
from .ros_middleware_adapter import (
    LifecycleState,
    MiddlewareUnavailableError,
    RosMiddlewareAdapter,
)

_STATE_TO_LIFECYCLE = {
    State.PRIMARY_STATE_UNCONFIGURED: LifecycleState.UNCONFIGURED,
    State.PRIMARY_STATE_INACTIVE: LifecycleState.INACTIVE,
    State.PRIMARY_STATE_ACTIVE: LifecycleState.ACTIVE,
    State.PRIMARY_STATE_FINALIZED: LifecycleState.FINALIZED,
}

# Linear order of the non-terminal primary states. A target that is more
# than one hop away (e.g. Unconfigured -> Active) is resolved to the single
# NEXT step towards it, not rejected: found live against a real /lc_talker
# node that a flat direct-pair lookup left the bridge permanently stuck
# requesting a transition that doesn't exist, every tick, forever, because
# the target passed in is always the final desired state, never the
# intermediate one.
_UP_PATH = [
    State.PRIMARY_STATE_UNCONFIGURED,
    State.PRIMARY_STATE_INACTIVE,
    State.PRIMARY_STATE_ACTIVE,
]

_STEP_FORWARD = {
    State.PRIMARY_STATE_UNCONFIGURED: Transition.TRANSITION_CONFIGURE,
    State.PRIMARY_STATE_INACTIVE: Transition.TRANSITION_ACTIVATE,
}
_STEP_BACKWARD = {
    State.PRIMARY_STATE_ACTIVE: Transition.TRANSITION_DEACTIVATE,
    State.PRIMARY_STATE_INACTIVE: Transition.TRANSITION_CLEANUP,
}
_SHUTDOWN_FROM = {
    State.PRIMARY_STATE_UNCONFIGURED: Transition.TRANSITION_UNCONFIGURED_SHUTDOWN,
    State.PRIMARY_STATE_INACTIVE: Transition.TRANSITION_INACTIVE_SHUTDOWN,
    State.PRIMARY_STATE_ACTIVE: Transition.TRANSITION_ACTIVE_SHUTDOWN,
}


def _next_transition(current_id, target: LifecycleState):
    """The single transition that moves one step from current_id towards
    target, or None if current_id is already unknown/terminal."""
    if target == LifecycleState.FINALIZED:
        return _SHUTDOWN_FROM.get(current_id)
    if current_id not in _UP_PATH:
        return None
    target_id = {
        LifecycleState.UNCONFIGURED: State.PRIMARY_STATE_UNCONFIGURED,
        LifecycleState.INACTIVE: State.PRIMARY_STATE_INACTIVE,
        LifecycleState.ACTIVE: State.PRIMARY_STATE_ACTIVE,
    }.get(target)
    if target_id is None:
        return None
    current_index = _UP_PATH.index(current_id)
    target_index = _UP_PATH.index(target_id)
    if target_index > current_index:
        return _STEP_FORWARD.get(current_id)
    if target_index < current_index:
        return _STEP_BACKWARD.get(current_id)
    return None


class Ros2MiddlewareAdapter(RosMiddlewareAdapter):
    # Option 3 of the D6 diagnosis: set by bridge.py to the core's DetailRecorder
    # when the detail trace is on; records service waits and calls, no new call.
    detail = NULL_DETAIL
    """Translation to ROS 2 (rclpy). Swappable without touching the bridge
    loop, LifecycleController or any test written against RosMiddlewareAdapter."""

    def __init__(self, node, callback_group=None, service_timeout_sec: float = 5.0):
        self._node = node
        # Must differ from the callback group of whatever calls into this
        # adapter (typically a timer): _call() busy-waits on a future from
        # inside that caller's callback, so the future can only resolve if
        # a *different* callback group is free to run the service response
        # -- a MutuallyExclusiveCallbackGroup (rclpy's default) would
        # deadlock the caller against itself. Found live: the bridge timer
        # timed out against a real /lc_talker node until this was added.
        self._callback_group = callback_group or ReentrantCallbackGroup()
        self._service_timeout_sec = service_timeout_sec
        # Keyed by the absolute service name: found by an external review,
        # confirmed by reading the code, that _client() previously called
        # create_client() on every single invocation (every tick calls it
        # 2-3 times) with no cache and no destroy_client() anywhere in this
        # file -- an unbounded, slow leak of rclpy client objects over a
        # long-running State Bridge process. Node.destroy_node() (bridge.py's
        # own shutdown path) already tears down every client a Node ever
        # created, so no explicit destroy is needed here on top of this
        # cache -- reuse alone is what stops the count from growing.
        self._clients = {}

    def get_lifecycle_state(self, node_name: str) -> LifecycleState:
        client = self._client(node_name, GetState, "get_state")
        response = self._call(client, GetState.Request())
        return _STATE_TO_LIFECYCLE.get(
            response.current_state.id, LifecycleState.UNKNOWN
        )

    def set_lifecycle_state(
        self, node_name: str, target: LifecycleState, before_send=None, timeout_sec=None,
    ) -> bool:
        current_id = self._current_state_id(node_name)
        transition_id = _next_transition(current_id, target)
        if transition_id is None:
            # Already at target, or current state is unknown/terminal.
            # A multi-hop jump (e.g. Unconfigured -> Active) is not chained
            # in one call: this returns the NEXT step only, and the caller
            # (the bridge's poll loop) re-observes and re-requests on
            # successive ticks until target is reached -- one call, one
            # transition, converging over time like the rest of this
            # control plane's reconciliation loops.
            return False
        client = self._client(node_name, ChangeState, "change_state")
        request = ChangeState.Request()
        request.transition.id = transition_id
        # Reserve only after discovery and a fresh state read, before dispatch.
        if before_send is not None and not before_send(_STATE_TO_LIFECYCLE[current_id]):
            return False
        response = self._call(client, request, timeout_sec=timeout_sec)
        return bool(response.success)

    def subscribe_metric(self, topic, callback):
        # MetricSample is the only metric message type this project defines
        # (interfaces/cloud_native_robotics_interfaces/msg/MetricSample.msg)
        # so it is hardcoded here rather than resolved dynamically; the MAL
        # contract only promises the callback receives *something* with a
        # `.latency_ms` attribute, not that it is literally a MetricSample --
        # a future adapter for a different metric type keeps that contract
        # without touching bridge.py.
        self._node.create_subscription(
            MetricSample,
            topic,
            callback,
            10,
            callback_group=self._callback_group,
        )

    def subscribe_heartbeat(self, topic, callback):
        # P1-equivalent signal (proposal, "Cosa viene riusato, cosa e' nuovo":
        # "il segnale P1/P2 confluisce nello State Bridge"). Only arrival
        # matters, not content, so callback takes no arguments -- VehicleStatus
        # is hardcoded here the same way MetricSample is in subscribe_metric,
        # for the same reason (the MAL contract only promises "fires on
        # arrival", not this concrete message type). qos_profile_sensor_data
        # (BEST_EFFORT/VOLATILE), not the rclpy default (RELIABLE): matches
        # every other subscriber to a PX4 topic in this project (e.g.
        # e1_battery_fault_harness's own VehicleStatus subscription) --
        # PX4's own publisher QoS is BEST_EFFORT, and a default RELIABLE
        # subscriber would never actually receive anything.
        self._node.create_subscription(
            VehicleStatus,
            topic,
            lambda _msg: callback(),
            qos_profile_sensor_data,
            callback_group=self._callback_group,
        )

    def subscribe_readiness(self, topic, callback):
        # Contract: Bool(true) AND recent delivery, not mere topic activity.
        self._node.create_subscription(
            Bool,
            topic,
            lambda msg: callback(bool(msg.data)),
            QoSProfile(depth=1, reliability=ReliabilityPolicy.RELIABLE),
            callback_group=self._callback_group,
        )

    def call_service(self, service_name, request):
        # Not currently called by anything in this codebase, but shares
        # _client()'s exact same service-name-keyed cache rather than
        # create_client()-ing fresh every call, for the same reason.
        client = self._clients.get(service_name)
        if client is None:
            client = self._node.create_client(
                type(request), service_name, callback_group=self._callback_group
            )
            self._clients[service_name] = client
        return self._call(client, request)

    def _current_state_id(self, node_name):
        client = self._client(node_name, GetState, "get_state")
        return self._call(client, GetState.Request()).current_state.id

    def _client(self, node_name, service_type, suffix):
        service_name = f"/{node_name.strip('/')}/{suffix}"
        client = self._clients.get(service_name)
        if client is None:
            client = self._node.create_client(
                service_type, service_name, callback_group=self._callback_group
            )
            self._clients[service_name] = client
        started = time.monotonic() if self.detail.enabled else None
        available = client.wait_for_service(timeout_sec=self._service_timeout_sec)
        if started is not None:
            self.detail.item("wait_service", started, time.monotonic(), service=suffix, ok=available)
        if not available:
            raise MiddlewareUnavailableError(f"service '{service_name}' is unavailable")
        return client

    def _call(self, client, request, timeout_sec=None):
        if not self.detail.enabled:
            return self._call_untimed(client, request, timeout_sec)
        started, outcome = time.monotonic(), "error"
        try:
            result = self._call_untimed(client, request, timeout_sec)
            outcome = "ok"
            return result
        except MiddlewareUnavailableError as exc:
            outcome = "timeout" if "timed out" in str(exc) else "error"
            raise
        finally:
            self.detail.item("rpc", started, time.monotonic(), service=getattr(client, "srv_name", "?"),
                             outcome=outcome)

    def _call_untimed(self, client, request, timeout_sec=None):
        future = client.call_async(request)
        timeout = self._service_timeout_sec if timeout_sec is None else timeout_sec
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if future.done():
                error = future.exception()
                if error is not None:
                    raise MiddlewareUnavailableError(str(error))
                return future.result()
            time.sleep(0.05)
        # Local cleanup does not cancel a request already executing remotely.
        client.remove_pending_request(future)
        raise MiddlewareUnavailableError("service call timed out")
