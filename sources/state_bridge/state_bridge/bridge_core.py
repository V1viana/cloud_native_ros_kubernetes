"""StateBridgeCore: the State Bridge's control logic, with no ROS import.

Mirrors one lifecycle node's state into its ROSModule status and executes the
lifecycle step the LifecycleController authorized for this instance
(command_executor.py; docs/LIFECYCLE_COMMAND_CONTRACT_DRAFT.md): the controller
decides step, timeout, retries and readiness gate, the bridge only executes the
authorized step at most once and reports. Everything it needs from the outside is
injected: the middleware as a RosMiddlewareAdapter (the proposal's MAL), the
Kubernetes status client, a logger and a BridgeConfig. bridge.py is the only
ROS-facing wrapper: it declares the ROS parameters, builds the
Ros2MiddlewareAdapter and drives tick() from a ROS timer.

Proposal S4.2: "Lo State Bridge dipende esclusivamente dall'interfaccia
RosMiddlewareAdapter, mai da rclpy o dalla CLI ros2 direttamente". Until
2026-09-23 the bridge class inherited from rclpy's Node and built
Ros2MiddlewareAdapter itself, so this logic could only be tested with rclpy
stubs (docs/CRD_CONTRACT_AUDIT.md, R3). It is now importable and runnable with
rclpy absent; operator/tests/test_bridge_core.py checks both.

Never touches local safety rules (BatteryLow/RTL): the bridge only handles
Companion-Analytics-style non-critical lifecycle modules, per the proposal's
explicit boundary (S4.1, "Local safety rules ... MAI dipende dal control
plane").
"""

from dataclasses import dataclass
from datetime import datetime, timezone
import time

from .command_executor import CommandExecutor
from .k8s_status_client import K8sStatusClientError
from .metrics_window import MetricsWindow, TumblingWindow
from .ros_middleware_adapter import LifecycleState, MiddlewareUnavailableError
from .window_publisher import LatestSnapshotPublisher
from .window_trace import NULL_DETAIL, DetailRecorder, WindowTrace, snapshot_seqs

@dataclass(frozen=True)
class BridgeConfig:
    rosmodule_name: str
    lifecycle_node_name: str
    metrics_topic: str = ""
    metrics_window_sec: float = 10.0
    telemetry_topic: str = ""
    # spec.probes.readiness's two fields (proposal S3), fixed at Pod creation
    # like the topics above -- not re-read from spec every tick.
    readiness_topic: str = ""
    readiness_base_topic: str = ""
    readiness_timeout_sec: float = 5.0
    # This Pod's UID (Downward API, POD_UID): commands for another Pod are refused.
    pod_uid: str = ""
    # Cadence of the periodic ROS observation; a watched spec change ticks at once.
    poll_interval_sec: float = 5.0
    # Tumbling p95 windows of the A/B temporal contract (V6): W from the
    # ROSModule's spec.metricsWindowSec, evaluated every window_eval_sec, the last
    # window_history published under this instance's key in status.metricWindows.
    window_sec: float = 2.0
    window_eval_sec: float = 0.2
    window_history: int = 8


class StateBridgeCore:
    def __init__(self, config, mal, k8s, logger, spec_source=None):
        """spec_source: a SpecWatcher (spec_watch.py) whose current() is used when
        trustworthy; None, or an untrustworthy watch, reads the resource instead."""
        if not config.rosmodule_name:
            raise ValueError("rosmodule_name is required")
        self._config = config
        self._spec_source = spec_source
        self._last_tick = None
        self._rosmodule_name = config.rosmodule_name
        self._lifecycle_node_name = config.lifecycle_node_name
        self._mal = mal
        self._k8s = k8s
        self._logger = logger
        self._executor = CommandExecutor(
            k8s, mal, rosmodule_name=config.rosmodule_name, instance_key=config.lifecycle_node_name,
            pod_uid=config.pod_uid, readiness=self._is_ready, logger=logger,
            observe=self._observation_after_step)

        self._latency_window = None
        self._tumbling = None
        self._windows = []
        self._window_seq = 0
        # D6 (R11): the snapshot goes out on the publisher's own thread, started by
        # the ROS wrapper; closing a window never waits for the network.
        self._window_publisher = LatestSnapshotPublisher(self._send_windows, logger)
        # Measurement only, and only in the D6 bench (window_trace.py): a no-op
        # unless STATE_BRIDGE_WINDOW_TRACE is set. The detail (option 3 of the
        # diagnosis) times the subscription callbacks below; off, they are passed
        # to the MAL unchanged.
        self._trace = WindowTrace()
        self.detail = DetailRecorder(self._trace) if self._trace.detail_enabled else NULL_DETAIL
        if config.metrics_topic:
            self._latency_window = MetricsWindow(config.metrics_window_sec)
            self._tumbling = TumblingWindow(config.window_sec)
            self._mal.subscribe_metric(config.metrics_topic, self.detail.timed("metric", self._on_metric))
            self._logger.info(
                f"subscribed to '{config.metrics_topic}', "
                f"{config.metrics_window_sec}s rolling p95"
            )

        # P1-equivalent (TelemetryHeartbeatLost): None until the first
        # message ever arrives, same "never observed yet" convention as
        # AdaptationController's own _elapsed_sec -- an absent metric, not a
        # false-zero age, until there is something real to report.
        self._last_heartbeat_monotonic = None
        # Max gap between consecutive heartbeats seen since the last tick,
        # reset every tick after being reported. Found live this matters:
        # reporting only "time since the most recent heartbeat AT TICK
        # TIME" missed a real ~10s gap entirely on more than one run,
        # because the underlying Agent process (killed as the fault
        # injection at the time; E2 now stops it with SIGSTOP, run_e2.sh)
        # restarted on its own via kubelet's ordinary "PID 1 exited"
        # handling, and that restart-and-reconnect sometimes landed
        # *between* two 5s ticks -- so by the time this node samples
        # again, a fresh heartbeat had already arrived and the
        # instantaneous age read back near zero, even though a genuine
        # multi-second gap had just happened. Proposal S2 studies exactly
        # this class of phenomenon (a transient that arises and clears
        # between polls) deliberately, as its own scenario -- E2 is not
        # meant to be that study, it is meant to show P1 remediation
        # working reliably, so the metric tracks the worst gap actually
        # observed in the window, not a single instantaneous sample.
        self._max_heartbeat_gap_sec = 0.0
        if config.telemetry_topic:
            self._mal.subscribe_heartbeat(config.telemetry_topic,
                                          self.detail.timed("heartbeat", self._on_heartbeat))
            self._logger.info(f"subscribed to heartbeat on '{config.telemetry_topic}'")

        # No sample is distinct from a positive, recent Bool observation.
        self._last_readiness_monotonic = None
        self._readiness_value = False
        if config.readiness_topic:
            self._mal.subscribe_readiness(config.readiness_topic,
                                          self.detail.timed("readiness", self._on_readiness))
            self._logger.info(f"subscribed to readiness on '{config.readiness_topic}'")

    def _on_metric(self, sample):
        self._latency_window.add(sample.latency_ms)
        self._tumbling.add(sample.latency_ms, getattr(sample, "cpu_percent", 0.0), time.time())

    def evaluate_windows(self):
        """Close a tumbling window if due (A/B temporal contract, V6) and offer
        this instance's recent windows to the publisher. Each window carries its
        sequence number, so AdaptationController counts it once; the whole short
        history goes out every time, so one failed patch is recovered by the next.
        The request is made on the publisher's thread, never here (D6, R11)."""
        if self._tumbling is None:
            return None
        window = self._tumbling.evaluate(time.time())
        if window is None:
            return None
        self._window_seq += 1
        self._windows.append({
            "seq": self._window_seq,
            "start": _iso(window["start"]), "end": _iso(window["end"]),
            "samples": window["samples"], "p95Ms": window["p95"],
        })
        self._windows = self._windows[-self._config.window_history:]
        self._trace.closed(self._window_seq, window)
        status_patch = {}
        status_patch["metricWindows"] = {self._lifecycle_node_name: {
            "windowSec": self._tumbling.window_sec, "windows": list(self._windows)}}
        self._window_publisher.offer(status_patch)
        return self._window_seq

    def open_window(self):
        """(start, samples) of the window being filled: the tick diagnosis only."""
        return self._tumbling.open_window() if self._tumbling is not None else (None, 0)

    @property
    def traces_ticks(self):
        """The tick diagnosis is on (STATE_BRIDGE_TICK_TRACE with the window trace)."""
        return self._trace.ticks

    def trace_tick(self, **fields):
        self._trace.tick(**fields)

    def _send_windows(self, status_patch):
        """The publisher's request; the D6 bench traces its start and end."""
        seqs = snapshot_seqs(status_patch)
        self._trace.send_started(seqs)
        try:
            self._k8s.patch_status(self._rosmodule_name, status_patch)
        except Exception as exc:
            self._trace.send_finished(seqs, False, f"{type(exc).__name__}: {exc}")
            raise
        self._trace.send_finished(seqs, True)

    def start_window_publisher(self):
        self._window_publisher.start()

    def stop_window_publisher(self, timeout=None):
        self._window_publisher.stop(timeout)

    def _on_heartbeat(self):
        now = time.monotonic()
        if self._last_heartbeat_monotonic is not None:
            gap = now - self._last_heartbeat_monotonic
            self._max_heartbeat_gap_sec = max(self._max_heartbeat_gap_sec, gap)
        self._last_heartbeat_monotonic = now

    def _on_readiness(self, value):
        self._readiness_value = bool(value)
        self._last_readiness_monotonic = time.monotonic()

    def _is_ready(self, spec):
        readiness = spec.get("probes", {}).get("readiness")
        if not readiness:
            return None
        # A changed probe is applied by rollout; the old Pod cannot attest it.
        if (readiness["topic"] != self._config.readiness_base_topic
                or float(readiness.get("timeoutSec", 5))
                != self._config.readiness_timeout_sec):
            return False
        if self._last_readiness_monotonic is None or not self._readiness_value:
            return False
        return (time.monotonic() - self._last_readiness_monotonic
                < float(readiness.get("timeoutSec", 5)))

    def maybe_tick(self):
        """Tick now if the watched spec changed (a declared transition applies at
        once, not at the next poll), or when the periodic ROS observation is due."""
        now = time.monotonic()
        changed = self._spec_source is not None and self._spec_source.take_change()
        if (changed or self._last_tick is None
                or now - self._last_tick >= self._config.poll_interval_sec):
            self._last_tick = now
            self.tick()

    def tick(self):
        resource = self._spec_source.current() if self._spec_source is not None else None
        if resource is None:
            # No watch, or not trustworthy right now: read it (docs/CRD_CONTRACT_AUDIT.md, R4).
            try:
                resource = self._k8s.get_resource(self._rosmodule_name)
            except K8sStatusClientError as exc:
                self._logger.warning(f"cannot read ROSModule: {exc}")
                return
        # lastReconcileTime and observedGeneration belong to ROSModuleController
        # alone (docs/CRD_CONTRACT_AUDIT.md, D1/D2). This bridge's freshness is its
        # record's lastObservedTime in status.lifecycleInstances.
        status_patch = {}
        observed = self._observe_state()
        ready = self._ready_for(resource.get("spec", {}), observed)
        status_patch["observedLifecycleState"] = observed.value
        # In a merge patch, null removes an old observation when a probe is removed.
        status_patch["observedReady"] = ready
        status_patch["lifecycleInstances"] = {
            self._lifecycle_node_name: self._observation(resource, observed, ready)}
        if observed == LifecycleState.UNKNOWN:
            self._patch_status(status_patch)
            return

        metrics = {}
        if self._latency_window is not None:
            p95 = self._latency_window.p95()
            # Explicitly remove an expired metric; omission preserves old merge-patch keys.
            metrics["latency_p95_ms"] = None if p95 is None else str(p95)
        if self._last_heartbeat_monotonic is not None:
            age_now = time.monotonic() - self._last_heartbeat_monotonic
            reported_age = max(age_now, self._max_heartbeat_gap_sec)
            metrics["telemetry_heartbeat_age_sec"] = str(round(reported_age, 3))
        if metrics:
            status_patch["metrics"] = metrics
            # Freshness of this report for AdaptationController's windows (D3a);
            # never written on the unreachable-node path above, so a silent
            # module's last value stops counting instead of lingering.
            status_patch["metricsObservedTime"] = _now_iso()
        if self._patch_status(status_patch):
            self._max_heartbeat_gap_sec = 0.0
        # The step the controller authorized for this instance, if any: at most once
        # per command ID, Accepted persisted before the RPC (command_executor.py).
        result = self._executor.run(resource, observed)
        if result.get("action") == "executed":
            self._logger.info(f"lifecycle step executed: {result.get('outcome')}")

    def _observe_state(self):
        try:
            return self._mal.get_lifecycle_state(self._lifecycle_node_name)
        except MiddlewareUnavailableError as exc:
            self._logger.warning(f"lifecycle node unreachable: {exc}")
            return LifecycleState.UNKNOWN

    def _ready_for(self, spec, observed):
        ready = self._is_ready(spec)
        if observed == LifecycleState.UNKNOWN and ready is not None:
            ready = False
        return ready

    def _observation(self, resource, observed, ready):
        """This instance's record: what was observed, and of which ROSModule
        generation, target and Pod -- the controller discards any other."""
        meta, spec = resource.get("metadata", {}), resource.get("spec", {})
        # Microseconds, not seconds: the controller decides on an observation strictly
        # newer than the outcome (contract, section 4.5), and the observation taken
        # right after a step must be.
        observed_at = datetime.fromtimestamp(time.time(), timezone.utc).isoformat(timespec="microseconds")
        return {"observedLifecycleState": observed.value, "observedReady": ready,
                "lastObservedTime": observed_at.replace("+00:00", "Z"), "generation": meta.get("generation"),
                "target": spec.get("lifecycleTarget"), "podUID": self._config.pod_uid}

    def _observation_after_step(self, resource):
        observed = self._observe_state()
        return self._observation(resource, observed, self._ready_for(resource.get("spec", {}), observed))

    def _patch_status(self, fields, resource_version=None):
        try:
            self._k8s.patch_status(self._rosmodule_name, fields,
                                   resource_version=resource_version)
            return True
        except K8sStatusClientError as exc:
            self._logger.warning(f"cannot patch ROSModule status: {exc}")
            return False


def _iso(epoch_sec):
    return (datetime.fromtimestamp(epoch_sec, timezone.utc)
            .isoformat(timespec="milliseconds").replace("+00:00", "Z"))


def _now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
