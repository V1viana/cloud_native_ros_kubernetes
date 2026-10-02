"""ROS 2 bridge from normalized operational events to policy actions."""

import rclpy

from cloud_native_robotics_interfaces.action import DeploymentRequest
from cloud_native_robotics_interfaces.msg import OperationalEvent
from rclpy.action import ActionClient
from rclpy.callback_groups import ReentrantCallbackGroup
from rclpy.executors import MultiThreadedExecutor
from rclpy.node import Node
from rclpy.qos import DurabilityPolicy, HistoryPolicy, QoSProfile, ReliabilityPolicy

from .dispatcher_core import DispatchAdmission
from .event_trace import EventTrace


class OperationalEventDispatcherNode(Node):
    """Forward incident entry messages to the fleet policy Action server."""

    def __init__(self):
        super().__init__("operational_event_dispatcher")
        self.declare_parameter("event_topic", "/fleet/operational_events")
        self.declare_parameter("action_name", "/fleet/deployment_request")
        self.declare_parameter("server_timeout_sec", 5.0)
        self.declare_parameter("goal_timeout_sec", 120.0)
        self.declare_parameter("incident_cache_size", 256)

        cache_size = int(self.get_parameter("incident_cache_size").value)
        self._admission = DispatchAdmission(cache_size=cache_size)
        # S2 only (OPERATIONAL_EVENT_TRACE=1): every arrival and its admission,
        # to stdout; off by default, where nothing is written (event_trace.py).
        self._event_trace = EventTrace.from_env(self.get_logger())
        self._callback_group = ReentrantCallbackGroup()
        action_name = self.get_parameter("action_name").value
        self._action_client = ActionClient(
            self,
            DeploymentRequest,
            action_name,
            callback_group=self._callback_group,
        )
        qos = QoSProfile(
            history=HistoryPolicy.KEEP_LAST,
            depth=100,
            reliability=ReliabilityPolicy.RELIABLE,
            durability=DurabilityPolicy.TRANSIENT_LOCAL,
        )
        event_topic = self.get_parameter("event_topic").value
        self._subscription = self.create_subscription(
            OperationalEvent,
            event_topic,
            self._on_event,
            qos,
            callback_group=self._callback_group,
        )
        self.get_logger().info(
            f"Dispatcher listening on '{event_topic}' and targeting "
            f"'{action_name}'"
        )

    def _on_event(self, event):
        self._event_trace.received(event)
        claimed = self._admission.claim(
            event.correlation_id,
            event.event_type,
            event.state,
        )
        self._event_trace.admission(event, claimed)
        if not claimed:
            return
        timeout = float(self.get_parameter("server_timeout_sec").value)
        if not self._action_client.wait_for_server(timeout_sec=timeout):
            self._admission.complete(event.correlation_id, accepted=False)
            self.get_logger().error(
                f"Application Manager unavailable for {event.correlation_id}"
            )
            return

        goal = DeploymentRequest.Goal()
        goal.event = event
        goal.policy_id = ""
        goal.requested_outcome = ""
        goal.timeout_sec = float(self.get_parameter("goal_timeout_sec").value)
        future = self._action_client.send_goal_async(
            goal,
            feedback_callback=self._on_feedback,
        )
        future.add_done_callback(
            lambda done, correlation_id=event.correlation_id: (
                self._on_goal_response(correlation_id, done)
            )
        )

    def _on_goal_response(self, correlation_id, future):
        try:
            goal_handle = future.result()
        except Exception as exc:
            self._admission.complete(correlation_id, accepted=False)
            self.get_logger().error(
                f"Could not send incident {correlation_id}: {exc}"
            )
            return
        self._admission.complete(correlation_id, accepted=goal_handle.accepted)
        if not goal_handle.accepted:
            self.get_logger().warning(
                f"DeploymentRequest rejected for {correlation_id}"
            )
            return
        self.get_logger().info(
            f"DeploymentRequest accepted for {correlation_id}"
        )
        result_future = goal_handle.get_result_async()
        result_future.add_done_callback(
            lambda done, incident=correlation_id: self._on_result(
                incident,
                done,
            )
        )

    def _on_feedback(self, feedback_message):
        feedback = feedback_message.feedback
        self.get_logger().info(
            f"Policy phase={feedback.phase} progress={feedback.progress:.2f}: "
            f"{feedback.message}"
        )

    def _on_result(self, correlation_id, future):
        try:
            result = future.result().result
            log = self.get_logger().info if result.success else self.get_logger().error
            log(
                f"Incident {correlation_id} completed as {result.outcome} "
                f"({result.final_phase})"
            )
        except Exception as exc:
            self.get_logger().error(
                f"Could not obtain result for {correlation_id}: {exc}"
            )


def main(args=None):
    rclpy.init(args=args)
    node = OperationalEventDispatcherNode()
    executor = MultiThreadedExecutor(num_threads=2)
    executor.add_node(node)
    try:
        executor.spin()
    except KeyboardInterrupt:
        pass
    finally:
        executor.shutdown()
        node.destroy_node()
        if rclpy.ok():
            rclpy.shutdown()
