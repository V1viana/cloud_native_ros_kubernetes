"""S4 fault observer (R13, docs/R13_S4_CONTRACT_DRAFT.md): a persistent ROS 2 process
on the control-plane node that records, independently of the control plane under
test, every message received on:

  battery topics         the fault harness's BatteryStatus (the injected value is
                         observed: `remaining` at each receipt);
  vehicle_status topics  a drone's PX4 status through its Agent (its interruption is
                         the stopped Agent's effect, its return the Agent's).

One JSON line per receipt, with the host kernel's monotonic clock and UTC (one kernel
with the host in k3d), on a hostPath of its node read from the host. It observes the
injected effect only; detection and actions are read elsewhere.
Record logic without ROS: record_battery/record_status (tested offline,
operator/tests/test_s4_fault_observer.py).
"""

import json
import os
import time


def record_battery(topic, message, mono, utc):
    return {"event": "battery", "topic": topic, "recv_mono": mono, "recv_utc": utc,
            "remaining": round(float(message.remaining), 4), "stamp_us": int(message.timestamp)}


def record_status(topic, message, mono, utc):
    return {"event": "vehicle_status", "topic": topic, "recv_mono": mono, "recv_utc": utc,
            "nav_state": int(message.nav_state), "arming_state": int(message.arming_state),
            "stamp_us": int(message.timestamp)}


class JsonlSink:
    def __init__(self, path):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        self._h = open(path, "a", buffering=1)

    def write(self, record):
        self._h.write(json.dumps(record) + "\n")

    def close(self):
        self._h.close()


def main(args=None):
    from px4_msgs.msg import BatteryStatus, VehicleStatus
    import rclpy
    from rclpy.node import Node
    from rclpy.qos import qos_profile_sensor_data

    class FaultObserver(Node):
        def __init__(self):
            super().__init__("s4_fault_observer")
            self.declare_parameter("out_file", "/var/lib/s4-observer/faults.jsonl")
            self.declare_parameter("battery_topics", ["/drone01/s4/fault/battery_status"])
            self.declare_parameter("status_topics", ["/drone02/fmu/out/vehicle_status_v4"])
            self.sink = JsonlSink(self.get_parameter("out_file").value)
            for topic in self.get_parameter("battery_topics").value:
                self.create_subscription(BatteryStatus, topic, self._on(topic, record_battery),
                                         qos_profile_sensor_data)
            for topic in self.get_parameter("status_topics").value:
                self.create_subscription(VehicleStatus, topic, self._on(topic, record_status),
                                         qos_profile_sensor_data)
            self.sink.write({"event": "started", "recv_mono": time.monotonic(), "recv_utc": time.time(),
                             "pid": os.getpid()})

        def _on(self, topic, make):
            def callback(message):
                self.sink.write(make(topic, message, time.monotonic(), time.time()))
            return callback

    rclpy.init(args=args)
    node = FaultObserver()
    try:
        rclpy.spin(node)
    except KeyboardInterrupt:
        pass
    finally:
        node.sink.close()
        node.destroy_node()
        if rclpy.ok():
            rclpy.shutdown()
