"""Publish deterministic analytics metrics only while the node is Active."""

from cloud_native_robotics_interfaces.msg import MetricSample
from cloud_native_robotics_interfaces.srv import GetHealthSnapshot
import rclpy
from rclpy.executors import MultiThreadedExecutor
from rclpy.lifecycle import LifecycleNode, State, TransitionCallbackReturn
from rclpy.qos import QoSProfile, ReliabilityPolicy


class CompanionAnalyticsNode(LifecycleNode):
    """A small controllable workload used to measure onboard-to-edge recovery."""

    def __init__(self):
        super().__init__("companion_analytics")
        self.declare_parameter("robot_id", "drone01")
        self.declare_parameter("instance_id", "onboard")
        self.declare_parameter("metrics_topic", "/drone01/analytics/metrics")
        self.declare_parameter("processing_delay_ms", 300.0)
        self.declare_parameter("queue_depth", 12)
        self.declare_parameter("cpu_percent", 85.0)
        self.declare_parameter("sample_period_ms", 250)
        self.declare_parameter("health_service", "")

        self._publisher = None
        self._timer = None
        self._active = False
        self._lifecycle_state = "unconfigured"

        health_service = str(self.get_parameter("health_service").value)
        if not health_service:
            robot_id = str(self.get_parameter("robot_id").value)
            instance_id = str(self.get_parameter("instance_id").value)
            health_service = f"/{robot_id}/companion/{instance_id}/health"
        self._health_service = self.create_service(
            GetHealthSnapshot,
            health_service,
            self._get_health_snapshot,
        )

    def on_configure(self, state: State) -> TransitionCallbackReturn:
        del state
        qos = QoSProfile(depth=50, reliability=ReliabilityPolicy.RELIABLE)
        self._publisher = self.create_lifecycle_publisher(
            MetricSample,
            str(self.get_parameter("metrics_topic").value),
            qos,
        )
        period_sec = max(
            0.05,
            float(self.get_parameter("sample_period_ms").value) / 1000.0,
        )
        self._timer = self.create_timer(period_sec, self._publish_metric)
        self.get_logger().info(
            "Configured analytics instance '%s' with %.1f ms latency"
            % (
                self.get_parameter("instance_id").value,
                self.get_parameter("processing_delay_ms").value,
            )
        )
        self._lifecycle_state = "inactive"
        return TransitionCallbackReturn.SUCCESS

    def on_activate(self, state: State) -> TransitionCallbackReturn:
        result = super().on_activate(state)
        if result == TransitionCallbackReturn.SUCCESS:
            self._active = True
            self._lifecycle_state = "active"
            self.get_logger().info(
                "Analytics instance '%s' is Active"
                % self.get_parameter("instance_id").value
            )
        return result

    def on_deactivate(self, state: State) -> TransitionCallbackReturn:
        self._active = False
        self._lifecycle_state = "inactive"
        self.get_logger().info(
            "Analytics instance '%s' is Inactive"
            % self.get_parameter("instance_id").value
        )
        return super().on_deactivate(state)

    def on_cleanup(self, state: State) -> TransitionCallbackReturn:
        del state
        self._active = False
        self._lifecycle_state = "unconfigured"
        if self._timer is not None:
            self.destroy_timer(self._timer)
            self._timer = None
        if self._publisher is not None:
            self.destroy_publisher(self._publisher)
            self._publisher = None
        return TransitionCallbackReturn.SUCCESS

    def _get_health_snapshot(self, request, response):
        response.header.stamp = self.get_clock().now().to_msg()
        response.correlation_id = request.correlation_id
        response.healthy = self._active and self._publisher is not None
        response.robot_id = str(self.get_parameter("robot_id").value)
        response.instance_id = str(self.get_parameter("instance_id").value)
        response.component = "companion-analytics-" + response.instance_id
        response.lifecycle_state = self._lifecycle_state
        response.latency_ms = float(
            self.get_parameter("processing_delay_ms").value
        )
        response.queue_depth = int(self.get_parameter("queue_depth").value)
        response.cpu_percent = float(self.get_parameter("cpu_percent").value)
        if response.healthy:
            response.detail = "analytics metric publisher is active"
        else:
            response.detail = (
                "analytics metric publisher is " + self._lifecycle_state
            )
        return response

    def _publish_metric(self):
        if not self._active or self._publisher is None:
            return
        message = MetricSample()
        message.header.stamp = self.get_clock().now().to_msg()
        message.robot_id = str(self.get_parameter("robot_id").value)
        message.component = (
            "companion-analytics-"
            + str(self.get_parameter("instance_id").value)
        )
        message.latency_ms = float(
            self.get_parameter("processing_delay_ms").value
        )
        message.queue_depth = int(self.get_parameter("queue_depth").value)
        message.cpu_percent = float(self.get_parameter("cpu_percent").value)
        self._publisher.publish(message)


def main(args=None):
    rclpy.init(args=args)
    node = CompanionAnalyticsNode()
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
