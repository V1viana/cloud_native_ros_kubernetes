"""Policy selection, feedback, deduplication, timeout and rollback."""

import threading
import time
from dataclasses import dataclass, field
from enum import IntEnum
from typing import Callable

from .catalog import PolicyCatalog


class Phase(IntEnum):
    ACCEPTED = 0
    ACTING = 1
    VERIFYING = 2
    ROLLING_BACK = 3


@dataclass(frozen=True)
class EventRecord:
    event_id: str
    correlation_id: str
    robot_id: str
    event_type: str
    component: str = ""


@dataclass(frozen=True)
class ExecutionRequest:
    event: EventRecord
    policy_id: str = ""
    requested_outcome: str = ""
    timeout_sec: float = 120.0


@dataclass(frozen=True)
class FeedbackUpdate:
    phase: Phase
    progress: float
    message: str
    observations: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class ExecutionResult:
    success: bool
    outcome: str
    final_phase: str
    rollback_performed: bool = False
    metrics: dict[str, str] = field(default_factory=dict)


class DuplicateInProgress(RuntimeError):
    """The same incident is already being processed."""


class ExecutionCancelled(RuntimeError):
    """The action client requested cancellation."""


class ExecutionContext:
    """Provide deadline, cancellation and feedback to a policy handler."""

    def __init__(self, request, policy, feedback, cancel_requested, monotonic):
        self.request = request
        self.policy = policy
        self._feedback = feedback
        self._cancel_requested = cancel_requested
        self._monotonic = monotonic
        self._deadline = monotonic() + request.timeout_sec

    def emit(self, phase, progress, message, **observations):
        self._feedback(
            FeedbackUpdate(phase, progress, message, dict(observations))
        )

    def remaining_sec(self):
        return max(0.0, self._deadline - self._monotonic())

    def check_active(self):
        if self._cancel_requested():
            raise ExecutionCancelled("DeploymentRequest was cancelled")
        if self.remaining_sec() <= 0:
            raise TimeoutError("DeploymentRequest deadline expired")

    def cancel_requested(self):
        return self._cancel_requested()


class LocalSafetyHandler:
    """Record that the flight-safety action remains owned by the robot."""

    def execute(self, context):
        context.check_active()
        context.emit(
            Phase.ACTING,
            0.5,
            "RTL remains local to PX4; no Kubernetes safety command is issued",
            owner="onboard",
        )
        context.emit(
            Phase.VERIFYING,
            0.9,
            "Local safety incident accepted for audit and diagnostics",
        )
        return ExecutionResult(
            True,
            "local_safety_observed",
            "STABLE",
            metrics={"safety_owner": "onboard"},
        )


class UnavailableHandler:
    """Return an explicit result for a policy whose backend is not ready."""

    def __init__(self, reason):
        self._reason = reason

    def execute(self, context):
        context.emit(Phase.ACTING, 0.2, self._reason)
        return ExecutionResult(False, "backend_unavailable", "FAILED")


class RecoveryRegistry:
    """Remember recovered incidents so remediation can verify ROS recovery."""

    def __init__(self, cache_size=256):
        self._cache_size = cache_size
        self._recovered = {}
        self._condition = threading.Condition()

    def mark_recovered(self, correlation_id, event_type):
        if not correlation_id:
            return
        with self._condition:
            self._recovered[correlation_id] = event_type
            while len(self._recovered) > self._cache_size:
                oldest = next(iter(self._recovered))
                del self._recovered[oldest]
            self._condition.notify_all()

    def wait_recovered(
        self,
        correlation_id,
        event_type,
        timeout_sec,
        cancel_requested=lambda: False,
        wait_step_sec=0.2,
    ):
        deadline = time.monotonic() + max(0.0, timeout_sec)
        with self._condition:
            while time.monotonic() < deadline:
                if cancel_requested():
                    raise ExecutionCancelled("Recovery verification was cancelled")
                recovered_type = self._recovered.get(correlation_id)
                if recovered_type == event_type:
                    return True
                remaining = deadline - time.monotonic()
                self._condition.wait(min(wait_step_sec, max(0.0, remaining)))
        raise TimeoutError(
            f"No ROS recovery observed for incident '{correlation_id}'"
        )


class TelemetryRecoveryHandler:
    """Restart the bridge, capture diagnostics and verify ROS telemetry."""

    def __init__(
        self,
        kubernetes,
        recovery_registry,
        namespace,
        deployment_factory,
        diagnostic_job_factory,
    ):
        self._kubernetes = kubernetes
        self._recovery_registry = recovery_registry
        self._namespace = namespace
        self._deployment_factory = deployment_factory
        self._diagnostic_job_factory = diagnostic_job_factory

    def execute(self, context):
        event = context.request.event
        correlation_id = event.correlation_id
        deployment_name = self._deployment_factory(event)
        job_name = ""
        try:
            context.check_active()
            context.emit(
                Phase.ACTING,
                0.15,
                "Recording telemetry incident in the Kubernetes Event API",
                deployment=deployment_name,
            )
            self._kubernetes.emit_incident_event(
                self._namespace,
                deployment_name,
                event,
            )
            context.check_active()
            context.emit(
                Phase.ACTING,
                0.35,
                "Restarting only the Micro XRCE-DDS Agent Deployment",
                deployment=deployment_name,
            )
            self._kubernetes.restart_deployment(
                self._namespace,
                deployment_name,
                correlation_id,
            )
            context.check_active()
            job_manifest = self._diagnostic_job_factory(event)
            job_name = self._kubernetes.create_diagnostic_job(
                self._namespace,
                job_manifest,
                correlation_id,
            )
            context.emit(
                Phase.VERIFYING,
                0.6,
                "Waiting for the bridge rollout to converge",
                deployment=deployment_name,
                diagnostic_job=job_name,
            )
            rollout = self._kubernetes.wait_deployment_ready(
                self._namespace,
                deployment_name,
                context.remaining_sec(),
                context.cancel_requested,
            )
            context.check_active()
            context.emit(
                Phase.VERIFYING,
                0.8,
                "Waiting for the matching ROS telemetry recovery event",
                correlation_id=correlation_id,
            )
            self._recovery_registry.wait_recovered(
                correlation_id,
                event.event_type,
                context.remaining_sec(),
                context.cancel_requested,
            )
            return ExecutionResult(
                True,
                "telemetry_recovered",
                "STABLE",
                metrics={
                    "deployment": deployment_name,
                    "deployment_generation": str(
                        rollout.get("metadata", {}).get("generation", "")
                    ),
                    "diagnostic_job": job_name,
                    "recovery_source": "ros_operational_event",
                },
            )
        except Exception as exc:
            return ExecutionResult(
                False,
                "telemetry_recovery_failed",
                "ESCALATED",
                metrics={
                    "deployment": deployment_name,
                    "diagnostic_job": job_name,
                    "error": str(exc),
                },
            )


class AnalyticsMigrationHandler:
    """Run the KubeROS blue/green analytics placement and verify ROS recovery."""

    def __init__(
        self,
        kuberos,
        manifest_factory,
        lifecycle=None,
        kubernetes=None,
        recovery_registry=None,
        namespace="",
        onboard_node_factory=lambda event: "",
        edge_node_factory=lambda event: "",
        hpa_cpu_target=70,
        rollback_timeout_sec=30.0,
    ):
        self._kuberos = kuberos
        self._manifest_factory = manifest_factory
        self._lifecycle = lifecycle
        self._kubernetes = kubernetes
        self._recovery_registry = recovery_registry
        self._namespace = namespace
        self._onboard_node_factory = onboard_node_factory
        self._edge_node_factory = edge_node_factory
        self._hpa_cpu_target = hpa_cpu_target
        self._rollback_timeout_sec = rollback_timeout_sec

    def execute(self, context):
        deployment_id = ""
        hpa_name = ""
        route_switched = False
        onboard_deactivated = False
        correlation_id = context.request.event.correlation_id
        event = context.request.event
        edge_node = self._edge_node_factory(event)
        onboard_node = self._onboard_node_factory(event)
        try:
            context.check_active()
            manifest = self._manifest_factory(context.request.event)
            context.emit(
                Phase.ACTING,
                0.25,
                "Creating edge analytics deployment through KubeROS",
            )
            operation = self._kuberos.create_deployment(manifest, correlation_id)
            deployment_id = operation.deployment_id
            context.check_active()
            context.emit(
                Phase.VERIFYING,
                0.65,
                "Waiting for the KubeROS deployment to become running",
                deployment_id=deployment_id,
            )
            state = self._kuberos.wait_ready(
                deployment_id,
                context.remaining_sec(),
                correlation_id,
                context.cancel_requested,
            )
            if self._lifecycle is not None:
                context.check_active()
                context.emit(
                    Phase.ACTING,
                    0.72,
                    "Configuring and activating the edge Lifecycle Node",
                    lifecycle_node=edge_node,
                )
                self._lifecycle.activate(
                    edge_node,
                    context.remaining_sec(),
                    context.cancel_requested,
                )
            if self._kubernetes is not None:
                context.check_active()
                self._kubernetes.set_analytics_route(
                    self._namespace,
                    event.robot_id,
                    "edge",
                    correlation_id,
                )
                route_switched = True
            if self._lifecycle is not None:
                context.emit(
                    Phase.ACTING,
                    0.78,
                    "Deactivating onboard analytics after edge activation",
                    lifecycle_node=onboard_node,
                )
                self._lifecycle.deactivate(
                    onboard_node,
                    context.remaining_sec(),
                    context.cancel_requested,
                )
                onboard_deactivated = True
            if self._kubernetes is not None:
                hpa_name = self._kubernetes.create_analytics_hpa(
                    self._namespace,
                    f"{event.robot_id}-companion-analytics",
                    event.robot_id,
                    correlation_id,
                    self._hpa_cpu_target,
                )
            if self._recovery_registry is not None:
                context.emit(
                    Phase.VERIFYING,
                    0.9,
                    "Waiting for the correlated analytics SLO recovery",
                    correlation_id=correlation_id,
                )
                self._recovery_registry.wait_recovered(
                    correlation_id,
                    event.event_type,
                    context.remaining_sec(),
                    context.cancel_requested,
                )
            return ExecutionResult(
                True,
                (
                    "analytics_slo_recovered"
                    if self._recovery_registry is not None
                    else "analytics_edge_ready"
                ),
                "STABLE",
                metrics={
                    "deployment_id": deployment_id,
                    "deployment_state": str(state.get("status", "running")),
                    "edge_lifecycle": "active" if self._lifecycle else "",
                    "onboard_lifecycle": (
                        "inactive" if onboard_deactivated else ""
                    ),
                    "active_route": "edge" if route_switched else "",
                    "hpa": hpa_name,
                    "recovery_source": (
                        "ros_operational_event"
                        if self._recovery_registry is not None
                        else ""
                    ),
                },
            )
        except Exception as exc:
            rollback_performed = False
            rollback_errors = []
            edge_note = ""
            context.emit(
                Phase.ROLLING_BACK,
                0.82,
                "Restoring onboard analytics and removing the edge target",
                deployment_id=deployment_id,
            )
            if self._lifecycle is not None and onboard_node:
                try:
                    self._lifecycle.activate(
                        onboard_node,
                        self._rollback_timeout_sec,
                        lambda: False,
                    )
                    rollback_performed = True
                except Exception as rollback_exc:
                    rollback_errors.append(str(rollback_exc))
            if self._kubernetes is not None and route_switched:
                try:
                    self._kubernetes.set_analytics_route(
                        self._namespace,
                        event.robot_id,
                        "onboard",
                        correlation_id,
                    )
                    rollback_performed = True
                except Exception as rollback_exc:
                    rollback_errors.append(str(rollback_exc))
            if self._kubernetes is not None and hpa_name:
                try:
                    self._kubernetes.delete_analytics_hpa(
                        self._namespace,
                        hpa_name,
                    )
                except Exception as rollback_exc:
                    rollback_errors.append(str(rollback_exc))
            if self._lifecycle is not None and edge_node:
                try:
                    self._lifecycle.deactivate(
                        edge_node,
                        self._rollback_timeout_sec,
                        lambda: False,
                    )
                except Exception as rollback_exc:
                    # R5 (review round of 14fc04c): an edge that is already
                    # gone cannot be deactivated, and that alone is not a
                    # failed rollback. Excused only when its lifecycle
                    # services are absent AND Kubernetes shows no ready edge
                    # Pod; any other error still fails the rollback, and so
                    # does a failed onboard reactivation above.
                    if (getattr(rollback_exc, "service_unavailable", False)
                            and self._edge_workload_gone(event)):
                        edge_note = f"not needed, edge already gone ({rollback_exc})"
                    else:
                        rollback_errors.append(str(rollback_exc))
            if deployment_id:
                try:
                    self._kuberos.delete_deployment(deployment_id, correlation_id)
                    rollback_performed = True
                except Exception as rollback_exc:
                    rollback_errors.append(str(rollback_exc))
            if rollback_errors:
                return ExecutionResult(
                    False,
                    "rollback_failed",
                    "FAILED",
                    rollback_performed=rollback_performed,
                    metrics={
                        "error": str(exc),
                        "rollback_error": "; ".join(rollback_errors),
                        **({"edge_deactivation": edge_note} if edge_note else {}),
                    },
                )
            return ExecutionResult(
                False,
                "analytics_migration_failed",
                "ROLLED_BACK" if rollback_performed else "FAILED",
                rollback_performed=rollback_performed,
                metrics={
                    "error": str(exc),
                    **({"edge_deactivation": edge_note} if edge_note else {}),
                },
            )

    def _edge_workload_gone(self, event):
        """No ready edge Pod left, or no edge Deployment at all, per Kubernetes.
        Anything that cannot be checked counts as not gone."""
        if self._kubernetes is None:
            return False
        try:
            deployment = self._kubernetes.get_deployment(
                self._namespace, f"{event.robot_id}-companion-analytics"
            )
        except Exception as lookup_exc:
            return "HTTP 404" in str(lookup_exc)
        return not (deployment.get("status", {}).get("readyReplicas") or 0)


class ApplicationManager:
    """Fleet-level synchronous control loop with bounded incident memory."""

    def __init__(self, catalog=None, monotonic=time.monotonic, cache_size=256):
        self._catalog = catalog or PolicyCatalog()
        self._monotonic = monotonic
        self._cache_size = cache_size
        self._handlers = {}
        self._active = set()
        self._results = {}
        self._lock = threading.Lock()

    def register(self, outcome, handler):
        self._handlers[outcome] = handler

    def resolve_policy(self, request):
        """Return the effective policy selected for an execution request."""
        return self._catalog.resolve(
            request.event.event_type,
            request.policy_id,
            request.requested_outcome,
        )

    def execute(
        self,
        request: ExecutionRequest,
        feedback: Callable[[FeedbackUpdate], None] = lambda update: None,
        cancel_requested: Callable[[], bool] = lambda: False,
    ) -> ExecutionResult:
        if not request.event.correlation_id:
            raise ValueError("event.correlation_id is required")
        if request.timeout_sec <= 0:
            raise ValueError("timeout_sec must be positive")
        policy = self.resolve_policy(request)
        correlation_id = request.event.correlation_id
        with self._lock:
            if correlation_id in self._results:
                return self._results[correlation_id]
            if correlation_id in self._active:
                raise DuplicateInProgress(
                    f"Incident '{correlation_id}' is already in progress"
                )
            self._active.add(correlation_id)

        try:
            feedback(
                FeedbackUpdate(
                    Phase.ACCEPTED,
                    0.0,
                    f"Accepted policy {policy.policy_id} for {policy.event_type}",
                )
            )
            handler = self._handlers.get(policy.outcome)
            if handler is None:
                result = ExecutionResult(False, "handler_not_registered", "FAILED")
            else:
                context = ExecutionContext(
                    request, policy, feedback, cancel_requested, self._monotonic
                )
                result = handler.execute(context)
        finally:
            with self._lock:
                self._active.discard(correlation_id)

        with self._lock:
            self._results[correlation_id] = result
            while len(self._results) > self._cache_size:
                oldest = next(iter(self._results))
                del self._results[oldest]
        return result
