"""ROS wrapper of ContinuityTracker: subscribe to PX4's VehicleStatus, log markers.

Passive: it publishes nothing and commands nothing. Markers use the E1 fault
harness format ("MARKER timestamp_ns=<wall clock> key=value ...") so scenario
scripts can place them on the same timeline as their own host timestamps.
"""

import time

from px4_msgs.msg import VehicleLocalPosition, VehicleStatus
import rclpy
from rclpy.node import Node
from rclpy.qos import qos_profile_sensor_data

from .continuity import ContinuityTracker, PositionTracker


class MissionObserver(Node):
    """Log PX4 status continuity markers and a periodic summary."""

    def __init__(self):
        super().__init__("mission_observer")
        self.declare_parameter(
            "vehicle_status_topic", "/fmu/out/vehicle_status_v4"
        )
        self.declare_parameter("gap_report_sec", 2.0)
        self.declare_parameter("summary_period_sec", 5.0)
        # Position estimate (R8, position and altitude in E1); "" disables it.
        self.declare_parameter(
            "local_position_topic", "/fmu/out/vehicle_local_position_v1"
        )
        self.declare_parameter("position_log_period_sec", 0.5)

        topic = self.get_parameter("vehicle_status_topic").value
        summary_period_sec = float(
            self.get_parameter("summary_period_sec").value
        )
        if summary_period_sec <= 0.0:
            raise ValueError("summary_period_sec must be positive")
        self._tracker = ContinuityTracker(
            gap_report_sec=float(self.get_parameter("gap_report_sec").value)
        )
        self._subscription = self.create_subscription(
            VehicleStatus, topic, self._on_status, qos_profile_sensor_data
        )
        position_topic = self.get_parameter("local_position_topic").value
        self._position = None
        if position_topic:
            self._position = PositionTracker(
                log_period_sec=float(
                    self.get_parameter("position_log_period_sec").value
                ),
                gap_report_sec=float(self.get_parameter("gap_report_sec").value),
            )
            self._position_subscription = self.create_subscription(
                VehicleLocalPosition, position_topic, self._on_position,
                qos_profile_sensor_data,
            )
        self._timer = self.create_timer(summary_period_sec, self._summary)
        self._log_marker("MISSION_OBSERVER_READY", topic=topic,
                         position_topic=position_topic or "none")

    def _on_status(self, message):
        state = {
            "nav_state": message.nav_state,
            "arming_state": message.arming_state,
            "failsafe": message.failsafe,
        }
        for marker, fields in self._tracker.on_status(
            time.monotonic_ns(), message.timestamp, state
        ):
            self._log_marker(marker, **fields)

    def _on_position(self, message):
        sample = {key: getattr(message, key) for key in (
            "x", "y", "z", "xy_valid", "z_valid", "dead_reckoning",
            "xy_reset_counter", "z_reset_counter")}
        for marker, fields in self._position.on_position(time.monotonic_ns(), sample):
            self._log_marker(marker, **fields)

    def _summary(self):
        now = time.monotonic_ns()
        marker, fields = self._tracker.summary(now)
        self._log_marker(marker, **fields)
        if self._position is not None:
            marker, fields = self._position.summary(now)
            self._log_marker(marker, **fields)

    def _log_marker(self, marker, **fields):
        values = [marker, f"timestamp_ns={self.get_clock().now().nanoseconds}"]
        values.extend(
            f"{key}={str(value).lower()}" for key, value in fields.items()
        )
        self.get_logger().info(" ".join(values))


def main(args=None):
    rclpy.init(args=args)
    node = MissionObserver()
    try:
        rclpy.spin(node)
    except KeyboardInterrupt:
        pass
    finally:
        node.destroy_node()
        if rclpy.ok():
            rclpy.shutdown()
