"""Inject a typed battery fault and observe the local PX4 safety loop."""

from enum import Enum

from px4_msgs.msg import BatteryStatus, VehicleCommand, VehicleCommandAck
from px4_msgs.msg import VehicleStatus
import rclpy
from rclpy.node import Node
from rclpy.qos import qos_profile_sensor_data


class Phase(Enum):
    WAITING = "waiting"
    LOW = "low"
    RECOVERY = "recovery"
    COMPLETE = "complete"


class BatteryFaultHarness(Node):
    """Publish deterministic samples while recording PX4 command outcomes."""

    def __init__(self):
        super().__init__("e1_battery_fault_harness")
        self.declare_parameter("battery_topic", "/e1/fault/battery_status")
        self.declare_parameter("command_topic", "/fmu/in/vehicle_command")
        self.declare_parameter(
            "ack_topic", "/fmu/out/vehicle_command_ack_v1"
        )
        self.declare_parameter(
            "vehicle_status_topic", "/fmu/out/vehicle_status_v4"
        )
        self.declare_parameter("startup_delay_sec", 15.0)
        self.declare_parameter("low_duration_sec", 8.0)
        self.declare_parameter("recovery_duration_sec", 4.0)
        self.declare_parameter("publish_frequency_hz", 10.0)
        self.declare_parameter("low_remaining", 0.10)
        self.declare_parameter("recovery_remaining", 0.80)

        self._startup_delay_sec = self._float_parameter("startup_delay_sec")
        self._low_duration_sec = self._float_parameter("low_duration_sec")
        self._recovery_duration_sec = self._float_parameter(
            "recovery_duration_sec"
        )
        frequency_hz = self._float_parameter("publish_frequency_hz")
        self._low_remaining = self._float_parameter("low_remaining")
        self._recovery_remaining = self._float_parameter(
            "recovery_remaining"
        )
        if min(
            self._startup_delay_sec,
            self._low_duration_sec,
            self._recovery_duration_sec,
            frequency_hz,
        ) <= 0.0:
            raise ValueError(
                "E1 timing and frequency parameters must be positive"
            )

        self._started_ns = self.get_clock().now().nanoseconds
        self._phase = Phase.WAITING
        self._command_observed = False
        self._ack_observed = False
        self._rtl_observed = False

        self._battery_publisher = self.create_publisher(
            BatteryStatus,
            self.get_parameter("battery_topic").value,
            qos_profile_sensor_data,
        )
        self._command_subscription = self.create_subscription(
            VehicleCommand,
            self.get_parameter("command_topic").value,
            self._on_command,
            qos_profile_sensor_data,
        )
        self._ack_subscription = self.create_subscription(
            VehicleCommandAck,
            self.get_parameter("ack_topic").value,
            self._on_ack,
            qos_profile_sensor_data,
        )
        self._status_subscription = self.create_subscription(
            VehicleStatus,
            self.get_parameter("vehicle_status_topic").value,
            self._on_status,
            qos_profile_sensor_data,
        )
        self._timer = self.create_timer(1.0 / frequency_hz, self._tick)
        self._log_marker("E1_HARNESS_READY")

    def _float_parameter(self, name):
        return float(self.get_parameter(name).value)

    def _tick(self):
        elapsed_sec = (
            self.get_clock().now().nanoseconds - self._started_ns
        ) / 1_000_000_000.0
        if elapsed_sec < self._startup_delay_sec:
            return
        if elapsed_sec < self._startup_delay_sec + self._low_duration_sec:
            self._enter_phase(Phase.LOW, "E1_FAULT_STARTED")
            self._publish_battery(self._low_remaining)
            return
        recovery_end = (
            self._startup_delay_sec
            + self._low_duration_sec
            + self._recovery_duration_sec
        )
        if elapsed_sec < recovery_end:
            self._enter_phase(Phase.RECOVERY, "E1_RECOVERY_STARTED")
            self._publish_battery(self._recovery_remaining)
            return
        if self._phase is not Phase.COMPLETE:
            self._phase = Phase.COMPLETE
            self._log_marker(
                "E1_FAULT_COMPLETED",
                command=self._command_observed,
                ack=self._ack_observed,
                rtl=self._rtl_observed,
            )

    def _enter_phase(self, phase, marker):
        if self._phase is phase:
            return
        self._phase = phase
        self._log_marker(marker)

    def _publish_battery(self, remaining):
        message = BatteryStatus()
        message.timestamp = self.get_clock().now().nanoseconds // 1000
        message.connected = True
        message.remaining = remaining
        self._battery_publisher.publish(message)

    def _on_command(self, message):
        if (
            self._command_observed
            or message.command
            != VehicleCommand.VEHICLE_CMD_NAV_RETURN_TO_LAUNCH
        ):
            return
        self._command_observed = True
        self._log_marker(
            "E1_RTL_COMMAND_OBSERVED",
            confirmation=message.confirmation,
        )

    def _on_ack(self, message):
        if (
            self._ack_observed
            or message.command
            != VehicleCommand.VEHICLE_CMD_NAV_RETURN_TO_LAUNCH
            or message.result
            != VehicleCommandAck.VEHICLE_CMD_RESULT_ACCEPTED
        ):
            return
        self._ack_observed = True
        self._log_marker("E1_RTL_ACK_ACCEPTED", result=message.result)

    def _on_status(self, message):
        if (
            self._rtl_observed
            or message.nav_state != VehicleStatus.NAVIGATION_STATE_AUTO_RTL
        ):
            return
        self._rtl_observed = True
        self._log_marker("E1_RTL_STATE_OBSERVED", nav_state=message.nav_state)

    def _log_marker(self, marker, **fields):
        values = [marker, f"timestamp_ns={self.get_clock().now().nanoseconds}"]
        values.extend(
            f"{key}={str(value).lower()}" for key, value in fields.items()
        )
        self.get_logger().info(" ".join(values))


def main(args=None):
    rclpy.init(args=args)
    node = BatteryFaultHarness()
    try:
        rclpy.spin(node)
    except KeyboardInterrupt:
        pass
    finally:
        node.destroy_node()
        if rclpy.ok():
            rclpy.shutdown()
