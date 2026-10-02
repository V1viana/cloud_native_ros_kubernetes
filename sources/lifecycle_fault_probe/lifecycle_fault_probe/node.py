"""A lifecycle node whose transitions fail on request (checklist R2, retry test).

Test fixture, not a workload of either variant: companion_analytics is the
workload A and B are compared on and stays untouched. Here a ChangeState can
be made to fail (the callback returns FAILURE, so the node falls back to its
previous state) while GetState keeps answering -- the case the State Bridge
must tell apart from an unreachable lifecycle endpoint. Every call of a
failing transition is logged with a running count, so a test can compare the
RPCs the node really received with the attempts the bridge recorded.

Parameters: fail_transitions (comma-separated: configure, activate,
deactivate, cleanup; empty = none fails) and health_topic (the per-Pod
readiness topic the operator passes; True every second once configured, like
companion_analytics, so the readiness gate opens and only the transition
itself fails).

tcp_port (R1, Service check; 0 = off): while Active, a plain TCP responder on
that port answers every connection with one line naming the Pod, so a test can
tell a Service in front of a real endpoint from one in front of nothing.

hang_transitions / hang_sec / hang_calls (lifecycle driven by the LifecycleController,
live checks C3, C4 and operator restart; docs/LIFECYCLE_LIVE_PREREGISTRATION.md): the
first hang_calls calls of each listed transition sleep hang_sec inside the callback,
then complete as usual -- an RPC that really reached the node and outlives the
client's timeout. Every callback logs START on entry (the RPC arrived), HANG before
sleeping, and its result (SUCCESS / FAILURE) on exit, so a test can tell an RPC that
really left the bridge from a bridge that died before sending it. While a callback
sleeps the node's single-threaded executor answers nothing, GetState included.
"""

import os
import socketserver
import threading
import time

import rclpy
from rclpy.lifecycle import LifecycleNode, State, TransitionCallbackReturn
from rclpy.qos import QoSProfile, ReliabilityPolicy
from std_msgs.msg import Bool

LOG_MARKER = "LIFECYCLE_FAULT_PROBE"


class LifecycleFaultProbe(LifecycleNode):
    def __init__(self):
        super().__init__("lifecycle_fault_probe")
        self.declare_parameter("fail_transitions", "")
        self.declare_parameter("health_topic", "")
        self.declare_parameter("tcp_port", 0)
        self.declare_parameter("hang_transitions", "")
        self.declare_parameter("hang_sec", 0.0)
        self.declare_parameter("hang_calls", 1)
        self.calls = {}
        self._tcp_server = None
        self._health_publisher = None
        self._health_timer = None

    def _failing(self):
        raw = str(self.get_parameter("fail_transitions").value)
        return {name.strip() for name in raw.split(",") if name.strip()}

    def _names(self, parameter):
        raw = str(self.get_parameter(parameter).value)
        return {name.strip() for name in raw.split(",") if name.strip()}

    def _transition(self, name):
        self.calls[name] = self.calls.get(name, 0) + 1
        count = self.calls[name]
        self.get_logger().info(f"{LOG_MARKER} {name} call {count}: START")
        hang = float(self.get_parameter("hang_sec").value)
        if (name in self._names("hang_transitions") and hang > 0
                and count <= int(self.get_parameter("hang_calls").value)):
            self.get_logger().info(f"{LOG_MARKER} {name} call {count}: HANG {hang}s")
            time.sleep(hang)
        failed = name in self._failing()
        self.get_logger().info(
            f"{LOG_MARKER} {name} call {self.calls[name]}: "
            + ("FAILURE (injected)" if failed else "SUCCESS"))
        return TransitionCallbackReturn.FAILURE if failed else TransitionCallbackReturn.SUCCESS

    def on_configure(self, state: State) -> TransitionCallbackReturn:
        result = self._transition("configure")
        health_topic = str(self.get_parameter("health_topic").value)
        if result == TransitionCallbackReturn.SUCCESS and health_topic:
            self._health_publisher = self.create_publisher(
                Bool, health_topic, QoSProfile(depth=1, reliability=ReliabilityPolicy.RELIABLE))
            self._health_timer = self.create_timer(1.0, self._publish_health)
        return result

    def on_activate(self, state: State) -> TransitionCallbackReturn:
        result = self._transition("activate")
        port = int(self.get_parameter("tcp_port").value)
        if result == TransitionCallbackReturn.SUCCESS and port > 0:
            self._start_tcp(port)
        return result

    def on_deactivate(self, state: State) -> TransitionCallbackReturn:
        result = self._transition("deactivate")
        if result == TransitionCallbackReturn.SUCCESS:
            self._stop_tcp()
        return result

    def _start_tcp(self, port):
        answer = f"{LOG_MARKER} {os.environ.get('POD_NAME', 'unknown')}\n".encode()

        class Handler(socketserver.BaseRequestHandler):
            def handle(self):
                self.request.sendall(answer)

        socketserver.ThreadingTCPServer.allow_reuse_address = True
        self._tcp_server = socketserver.ThreadingTCPServer(("0.0.0.0", port), Handler)
        threading.Thread(target=self._tcp_server.serve_forever, daemon=True).start()
        self.get_logger().info(f"{LOG_MARKER} tcp responder on port {port}")

    def _stop_tcp(self):
        if self._tcp_server is not None:
            self._tcp_server.shutdown()
            self._tcp_server.server_close()
            self._tcp_server = None

    def on_cleanup(self, state: State) -> TransitionCallbackReturn:
        if self._health_timer is not None:
            self.destroy_timer(self._health_timer)
            self._health_timer = None
        if self._health_publisher is not None:
            self.destroy_publisher(self._health_publisher)
            self._health_publisher = None
        return self._transition("cleanup")

    def _publish_health(self):
        if self._health_publisher is not None:
            self._health_publisher.publish(Bool(data=True))


def main(args=None):
    rclpy.init(args=args)
    node = LifecycleFaultProbe()
    try:
        rclpy.spin(node)
    except KeyboardInterrupt:
        pass
    finally:
        node.destroy_node()
        if rclpy.ok():
            rclpy.shutdown()
