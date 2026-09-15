"""ROS 2 Action server wrapping the testable policy control loop."""

import os
from datetime import datetime, timezone
from copy import deepcopy

from cloud_native_robotics_interfaces.action import DeploymentRequest
from cloud_native_robotics_interfaces.msg import OperationalEvent
from diagnostic_msgs.msg import KeyValue
import rclpy
from rclpy.action import ActionServer, CancelResponse, GoalResponse
from rclpy.callback_groups import ReentrantCallbackGroup
from rclpy.executors import MultiThreadedExecutor
from rclpy.node import Node
from rclpy.qos import DurabilityPolicy, HistoryPolicy, QoSProfile, ReliabilityPolicy
import yaml

from .control_loop import (
    AnalyticsMigrationHandler,
    ApplicationManager,
    EventRecord,
    ExecutionRequest,
    ExecutionResult,
    LocalSafetyHandler,
    RecoveryRegistry,
    TelemetryRecoveryHandler,
    UnavailableHandler,
)
from .kubernetes_adapter import KubernetesAdapter
from .kuberos_adapter import KuberosAdapter
from .lifecycle_coordinator import RosLifecycleCoordinator
from .incident_reporter import IncidentReporter


class ApplicationManagerNode(Node):
    """Expose the fleet policy control loop as a ROS 2 Action server."""

    def __init__(self):
        super().__init__("application_manager")
        self.declare_parameter("action_name", "/fleet/deployment_request")
        self.declare_parameter("kuberos_base_url", "http://kuberos-api:8000")
        self.declare_parameter("kuberos_token_env", "KUBEROS_API_TOKEN")
        self.declare_parameter("analytics_manifest_path", "")
        self.declare_parameter(
            "analytics_onboard_node_pattern",
            "/{robot_id}/companion_analytics_onboard",
        )
        self.declare_parameter(
            "analytics_edge_node_pattern",
            "/{robot_id}/companion_analytics_edge",
        )
        self.declare_parameter("analytics_hpa_cpu_target", 70)
        self.declare_parameter("analytics_rollback_timeout_sec", 30.0)
        self.declare_parameter("poll_interval_sec", 1.0)
        self.declare_parameter("default_timeout_sec", 120.0)
        self.declare_parameter("event_topic", "/fleet/operational_events")
        self.declare_parameter("kubernetes_namespace", "")
        self.declare_parameter(
            "telemetry_deployment_name_pattern",
            "{robot_id}-microxrce-agent",
        )
        self.declare_parameter("diagnostic_job_template_path", "")
        self.declare_parameter("audit_url", "")
        self.declare_parameter("notifier_url", "")
        self.declare_parameter("report_spool_path", "")
        self.declare_parameter("report_delivery_attempts", 20)
        self.declare_parameter("report_retry_delay_sec", 0.5)

        self._default_timeout_sec = self.get_parameter(
            "default_timeout_sec"
        ).value
        self._callback_group = ReentrantCallbackGroup()
        self._recovery_registry = RecoveryRegistry()
        self._manager = ApplicationManager()
        self._incident_reporter = IncidentReporter(
            audit_url=self.get_parameter("audit_url").value,
            notifier_url=self.get_parameter("notifier_url").value,
            spool_path=self.get_parameter("report_spool_path").value,
            delivery_attempts=self.get_parameter(
                "report_delivery_attempts"
            ).value,
            retry_delay_sec=self.get_parameter(
                "report_retry_delay_sec"
            ).value,
            on_error=lambda message: self.get_logger().warning(message),
        )
        self._manager.register("observe_local_safety", LocalSafetyHandler())
        self._configure_telemetry_handler()
        self._configure_analytics_handler()

        event_qos = QoSProfile(
            history=HistoryPolicy.KEEP_LAST,
            depth=100,
            reliability=ReliabilityPolicy.RELIABLE,
            durability=DurabilityPolicy.TRANSIENT_LOCAL,
        )
        event_topic = self.get_parameter("event_topic").value
        self._event_subscription = self.create_subscription(
            OperationalEvent,
            event_topic,
            self._on_operational_event,
            event_qos,
            callback_group=self._callback_group,
        )

        action_name = self.get_parameter("action_name").value
        self._action_server = ActionServer(
            self,
            DeploymentRequest,
            action_name,
            execute_callback=self._execute,
            goal_callback=self._goal,
            cancel_callback=self._cancel,
            callback_group=self._callback_group,
        )
        self.get_logger().info(
            f"Application Manager ready on action '{action_name}'"
        )

    def destroy_node(self):
        self._incident_reporter.close()
        return super().destroy_node()

    def _configure_telemetry_handler(self):
        template_path = self.get_parameter(
            "diagnostic_job_template_path"
        ).value
        if not template_path:
            self._manager.register(
                "restore_telemetry",
                UnavailableHandler(
                    "diagnostic_job_template_path is not configured"
                ),
            )
            return
        try:
            with open(template_path, encoding="utf-8") as stream:
                template = yaml.safe_load(stream)
            adapter = KubernetesAdapter.from_service_account(
                poll_interval_sec=self.get_parameter(
                    "poll_interval_sec"
                ).value
            )
        except Exception as exc:
            self._manager.register(
                "restore_telemetry",
                UnavailableHandler(f"Kubernetes backend unavailable: {exc}"),
            )
            return

        namespace = self.get_parameter("kubernetes_namespace").value
        if not namespace:
            namespace = KubernetesAdapter.namespace_from_service_account()
        name_pattern = self.get_parameter(
            "telemetry_deployment_name_pattern"
        ).value

        def deployment_factory(event):
            return name_pattern.format(
                robot_id=event.robot_id,
                component=event.component,
            )

        def diagnostic_job_factory(event):
            manifest = deepcopy(template)
            metadata = manifest.setdefault("metadata", {})
            metadata.setdefault("generateName", f"telemetry-diag-{event.robot_id}-")
            labels = metadata.setdefault("labels", {})
            labels["cloud-native-robotics.io/robot-id"] = event.robot_id
            containers = manifest["spec"]["template"]["spec"]["containers"]
            env = containers[0].setdefault("env", [])
            env.extend(
                [
                    {"name": "ROBOT_ID", "value": event.robot_id},
                    {
                        "name": "INCIDENT_CORRELATION_ID",
                        "value": event.correlation_id,
                    },
                ]
            )
            return manifest

        self._manager.register(
            "restore_telemetry",
            TelemetryRecoveryHandler(
                adapter,
                self._recovery_registry,
                namespace,
                deployment_factory,
                diagnostic_job_factory,
            ),
        )

    def _on_operational_event(self, event):
        if event.state != OperationalEvent.STATE_RECOVERED:
            return
        self._recovery_registry.mark_recovered(
            event.correlation_id,
            event.event_type,
        )
        self.get_logger().info(
            f"Observed ROS recovery for {event.correlation_id}"
        )

    def _configure_analytics_handler(self):
        manifest_path = self.get_parameter("analytics_manifest_path").value
        if not manifest_path:
            self._manager.register(
                "restore_analytics_slo",
                UnavailableHandler("analytics_manifest_path is not configured"),
            )
            return
        with open(manifest_path, encoding="utf-8") as stream:
            template = yaml.safe_load(stream)
        token_env = self.get_parameter("kuberos_token_env").value
        try:
            adapter = KuberosAdapter(
                self.get_parameter("kuberos_base_url").value,
                os.environ.get(token_env, ""),
                poll_interval_sec=self.get_parameter("poll_interval_sec").value,
            )
            kubernetes = KubernetesAdapter.from_service_account(
                poll_interval_sec=self.get_parameter(
                    "poll_interval_sec"
                ).value
            )
        except Exception as exc:
            self._manager.register(
                "restore_analytics_slo",
                UnavailableHandler(f"Analytics backend unavailable: {exc}"),
            )
            return
        lifecycle = RosLifecycleCoordinator(
            self,
            callback_group=self._callback_group,
            poll_interval_sec=0.1,
        )
        namespace = self.get_parameter("kubernetes_namespace").value
        if not namespace:
            namespace = KubernetesAdapter.namespace_from_service_account()
        onboard_pattern = self.get_parameter(
            "analytics_onboard_node_pattern"
        ).value
        edge_pattern = self.get_parameter("analytics_edge_node_pattern").value

        def manifest_factory(event):
            manifest = deepcopy(template)
            metadata = manifest.setdefault("metadata", {})
            metadata["name"] = f"analytics-edge-{event.robot_id}"
            metadata["targetRobots"] = [event.robot_id]
            return manifest

        self._manager.register(
            "restore_analytics_slo",
            AnalyticsMigrationHandler(
                adapter,
                manifest_factory,
                lifecycle=lifecycle,
                kubernetes=kubernetes,
                recovery_registry=self._recovery_registry,
                namespace=namespace,
                onboard_node_factory=lambda event: onboard_pattern.format(
                    robot_id=event.robot_id
                ),
                edge_node_factory=lambda event: edge_pattern.format(
                    robot_id=event.robot_id
                ),
                hpa_cpu_target=self.get_parameter(
                    "analytics_hpa_cpu_target"
                ).value,
                rollback_timeout_sec=self.get_parameter(
                    "analytics_rollback_timeout_sec"
                ).value,
            ),
        )

    def _goal(self, goal):
        event = goal.event
        if not event.correlation_id or not event.robot_id or not event.event_type:
            self.get_logger().warning("Rejected incomplete DeploymentRequest")
            return GoalResponse.REJECT
        return GoalResponse.ACCEPT

    def _cancel(self, goal_handle):
        self.get_logger().info(
            f"Cancellation requested for {goal_handle.request.event.correlation_id}"
        )
        return CancelResponse.ACCEPT

    def _execute(self, goal_handle):
        goal = goal_handle.request
        event = EventRecord(
            event_id=goal.event.event_id,
            correlation_id=goal.event.correlation_id,
            robot_id=goal.event.robot_id,
            event_type=goal.event.event_type,
            component=goal.event.component,
        )
        request = ExecutionRequest(
            event,
            policy_id=goal.policy_id,
            requested_outcome=goal.requested_outcome,
            timeout_sec=goal.timeout_sec or self._default_timeout_sec,
        )
        audit_request = request
        try:
            policy = self._manager.resolve_policy(request)
            audit_request = ExecutionRequest(
                event,
                policy_id=policy.policy_id,
                requested_outcome=policy.outcome,
                timeout_sec=request.timeout_sec,
            )
        except ValueError:
            pass
        audit_session = self._incident_reporter.begin(
            audit_request,
            self._source_timestamp(goal.event),
        )

        def publish(update):
            audit_session.feedback(update)
            message = DeploymentRequest.Feedback()
            message.phase = int(update.phase)
            message.progress = update.progress
            message.message = update.message
            message.observations = self._key_values(update.observations)
            goal_handle.publish_feedback(message)

        result_message = DeploymentRequest.Result()
        try:
            result = self._manager.execute(
                request,
                feedback=publish,
                cancel_requested=lambda: goal_handle.is_cancel_requested,
            )
            result_message.success = result.success
            result_message.outcome = result.outcome
            result_message.final_phase = result.final_phase
            result_message.rollback_performed = result.rollback_performed
            result_message.metrics = self._key_values(result.metrics)
            if goal_handle.is_cancel_requested:
                goal_handle.canceled()
            elif result.success:
                goal_handle.succeed()
            else:
                goal_handle.abort()
            audit_session.complete(result)
        except Exception as exc:
            result_message.success = False
            result_message.outcome = "manager_error"
            result_message.final_phase = "FAILED"
            result_message.metrics = self._key_values({"error": str(exc)})
            goal_handle.abort()
            self.get_logger().error(str(exc))
            audit_session.complete(
                ExecutionResult(
                    False,
                    "manager_error",
                    "FAILED",
                    metrics={"error": str(exc)},
                )
            )
        result_message.finished_at = self.get_clock().now().to_msg()
        return result_message

    @staticmethod
    def _key_values(values):
        return [KeyValue(key=str(key), value=str(value)) for key, value in values.items()]

    @staticmethod
    def _source_timestamp(event):
        stamp = event.header.stamp
        if not stamp.sec and not stamp.nanosec:
            return ""
        value = stamp.sec + stamp.nanosec / 1_000_000_000
        return datetime.fromtimestamp(value, timezone.utc).isoformat(
            timespec="microseconds"
        ).replace("+00:00", "Z")


def main(args=None):
    rclpy.init(args=args)
    node = ApplicationManagerNode()
    executor = MultiThreadedExecutor(num_threads=4)
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
