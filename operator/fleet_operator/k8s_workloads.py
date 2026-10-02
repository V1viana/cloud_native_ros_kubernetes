"""Builds the Deployment manifest realizing a ROSModule.

Image resolution reads the project's real image catalog
(config/project_images.json) instead of guessing a tag: this repo does not
build one image per ROS package, it builds three bundles (control-plane,
event-detector, kuberos), each colcon-building several sources/* packages
together (see containers/*/Dockerfile). `companion_analytics`, for
instance, ships inside `control-plane`. _PACKAGE_TO_BUNDLE below is that
mapping made explicit -- it is currently implicit only in each Dockerfile's
COPY list, so it has to be kept in sync by hand until something renders it.

The container also needs an actual command: the placeholder version of this
file set only `image` with no entrypoint, so the container would start and
immediately drop into the base image's default `CMD ["bash"]` -- a
Deployment that looks Ready while running nothing. The command below
mirrors the exact invocation already used by the existing KubeROS manifests
(see manifests/kuberos/p2/analytics-edge.yaml: `. /ws/install/setup.bash &&
exec ros2 run <package> <package> --ros-args ...`).
"""

import hashlib
import json
import os
import shlex

OWNER_LABEL = "dronekube.io/owned-by-rosmodule"
# Fingerprint of the rendered Deployment .spec, used by
# rosmodule_controller._apply_deployment to skip a replace when nothing
# actually changed. Found live (U1/U2 work): the original code called
# replace_namespaced_deployment unconditionally on every reconcile tick,
# identical content or not; Kubernetes bumps metadata.generation on every
# such replace regardless, so a Deployment's generation kept climbing
# forever on its normal 30s resync tick even at rest. Harmless as long as
# nothing read metadata.generation -- but the update/rollback state
# machine's readiness check does exactly that (observedGeneration >=
# generation), and reads the Deployment back in the very same tick that
# just replaced it: the async Deployment controller hasn't caught up to
# the fresh generation our own replace just produced yet, so the
# readiness check always saw a stale observedGeneration and never
# converged, timing out and rolling back a perfectly valid update.
SPEC_HASH_ANNOTATION = "dronekube.io/spec-hash"

# package -> bundle name in config/project_images.json. Extend this
# whenever a ROSModule targets a package not yet listed here; an
# unresolvable package fails loudly (see _resolve_image) rather than
# guessing, on purpose.
_PACKAGE_TO_BUNDLE = {
    "companion_analytics": "control-plane",
    "cloud_native_application_manager": "control-plane",
    "operational_event_dispatcher": "control-plane",
    "e1_battery_fault_harness": "control-plane",
    "e0_kuberos_bootstrap": "control-plane",
    "platform_observability": "control-plane",
    "px4_event_detector_plugin": "event-detector",
    # Test fixture only (checklist R2 retry check, scripts/run_retry_budget_check.sh):
    # its own image, so companion_analytics' control-plane image is untouched.
    "lifecycle_fault_probe": "lifecycle-fault-probe",
}

_DEFAULT_CATALOG_PATH = "/operator/project_images.json"


class ImageResolutionError(RuntimeError):
    """A ROSModule targets a package this operator cannot map to a built image."""


# R1, decision 1 (docs/CRD_CONTRACT_AUDIT.md): the packages a ROSModule may run,
# each with its profile. _PACKAGE_TO_BUNDLE maps images, several of them for
# packages that are not lifecycle nodes; being in it does not make a package
# runnable as a ROSModule. Adding one means declaring its profile here: the
# executable `ros2 run <package> <executable>` starts, and whether it is a test
# fixture rather than a compared workload.
SUPPORTED_PACKAGES = {
    "companion_analytics": {"executable": "companion_analytics", "fixture": False},
    "lifecycle_fault_probe": {"executable": "lifecycle_fault_probe", "fixture": True},
}


class UnsupportedPackageError(ValueError):
    """A ROSModule names a package with no declared ROSModule profile."""


# R1, decision 2: parameters the platform sets for every module; rejected in
# rosParamMap and edgeRosParamMap by the CRDs, checked here again.
RESERVED_PARAMETERS = ("robot_id", "instance_id", "metrics_topic", "health_topic")


class ReservedParameterError(ValueError):
    """A rosParamMap sets a parameter the platform owns."""


def check_parameters(ros_param_map):
    reserved = sorted(set(ros_param_map or {}) & set(RESERVED_PARAMETERS))
    if reserved:
        raise ReservedParameterError(
            f"rosParamMap sets platform-reserved parameters: {', '.join(reserved)}")


def check_supported(package):
    if package not in SUPPORTED_PACKAGES:
        raise UnsupportedPackageError(
            f"package '{package}' is not a supported ROSModule package "
            f"(supported: {', '.join(sorted(SUPPORTED_PACKAGES))})")


_catalog_cache = None


def _load_catalog():
    global _catalog_cache
    if _catalog_cache is not None:
        return _catalog_cache
    path = os.environ.get("PROJECT_IMAGES_PATH", _DEFAULT_CATALOG_PATH)
    try:
        with open(path, encoding="utf-8") as stream:
            data = json.load(stream)
    except FileNotFoundError as exc:
        raise ImageResolutionError(
            f"image catalog not found at '{path}' (set PROJECT_IMAGES_PATH)"
        ) from exc
    _catalog_cache = {entry["name"]: entry for entry in data["images"]}
    return _catalog_cache


def _resolve_bundle_image(bundle_name):
    catalog = _load_catalog()
    entry = catalog.get(bundle_name)
    if entry is None:
        raise ImageResolutionError(f"bundle '{bundle_name}' is not in the image catalog")
    return entry["source_reference"]


def _resolve_image(package):
    bundle_name = _PACKAGE_TO_BUNDLE.get(package)
    if bundle_name is None:
        raise ImageResolutionError(
            f"package '{package}' is not in _PACKAGE_TO_BUNDLE -- "
            "add it once its container bundle is known"
        )
    return _resolve_bundle_image(bundle_name), bundle_name


def node_name(package, placement):
    """The ROS 2 node name this module runs under.

    companion_analytics.node.py hardcodes its own name
    (`super().__init__("companion_analytics")`) rather than deriving it
    from the `instance_id` parameter -- confirmed by reading the source,
    not assumed. Two instances of the same package on the same robot (the
    blue/green P2 migration: onboard + edge alive at once) would collide on
    the DDS domain without an explicit `__node` remap for the edge one.
    """
    return f"{package}_edge" if placement == "edge" else package


# Downward-API-injected Pod name (added to every container's env below),
# sanitized for ROS 2's node-name charset (letters/digits/underscore only,
# no dashes -- RFC1123 Pod names use dashes freely). Appended to every ROS
# 2 node name this module creates so that replicas of the SAME ROSModule
# never collide on the DDS graph: found live, HPA scaling a companion
# analytics edge Deployment to 3 replicas crashed companion_analytics with
# "Transition is not registered" because all three shared the identical
# node name and lifecycle service names -- a ChangeState request from one
# replica's State Bridge could be, and was, serviced by a DIFFERENT
# replica's node, whose actual current state made that transition invalid.
# Evaluated by bash at container start (this is a literal shell expansion,
# not a Python f-string placeholder), not by the operator at render time:
# the Pod's own name does not exist yet when this manifest is built.
_POD_SUFFIX_EXPR = "${POD_NAME//-/_}"


def _pod_unique_node_name(package, placement):
    return f"{node_name(package, placement)}_{_POD_SUFFIX_EXPR}"


def _pod_readiness_topic(readiness):
    # A healthy replica must never satisfy another replica's activation gate.
    return f"{readiness['topic'].rstrip('/')}/{_POD_SUFFIX_EXPR}"


def _pod_name_env():
    return {"name": "POD_NAME", "valueFrom": {"fieldRef": {"fieldPath": "metadata.name"}}}


def _pod_uid_env():
    # The State Bridge refuses a lifecycle command issued for another Pod
    # (docs/LIFECYCLE_COMMAND_CONTRACT_DRAFT.md, section 3): a replacement Pod may
    # reuse a name, never a UID.
    return {"name": "POD_UID", "valueFrom": {"fieldRef": {"fieldPath": "metadata.uid"}}}


# D6 bench only (R11, scripts/run_window_transport_check.sh): the file of the
# bridge's measurement-only window trace (state_bridge/window_trace.py), passed
# through from the operator's own env. Unset everywhere else, so every other
# Deployment -- and its spec hash -- is unchanged.
WINDOW_TRACE_ENV = "STATE_BRIDGE_WINDOW_TRACE"
TICK_TRACE_ENV = "STATE_BRIDGE_TICK_TRACE"       # the tick diagnosis, same file
DETAIL_TRACE_ENV = "STATE_BRIDGE_DETAIL_TRACE"   # its option 3, same file


def _window_trace_env():
    return [{"name": name, "value": os.environ[name]}
            for name in (WINDOW_TRACE_ENV, TICK_TRACE_ENV, DETAIL_TRACE_ENV) if os.environ.get(name)]


# Default W of the A/B temporal contract (V6): variant A's window_sec in P2.
DEFAULT_METRICS_WINDOW_SEC = 2.0


def metrics_topic(robot_id, placement):
    return f"/{robot_id}/analytics/metrics/{placement}"


def telemetry_topic(robot_id):
    # P1-equivalent signal source (proposal: "il segnale P1/P2 confluisce
    # nello State Bridge"). Onboard-only: PX4/uXRCE-DDS Agent are per-robot
    # and always onboard (manifests/kubernetes/e0/40-shared-infra.yaml),
    # there is no edge equivalent to bridge telemetry from.
    return f"/{robot_id}/fmu/out/vehicle_status_v4"


def _build_command(package, robot_id, placement, ros_param_map, readiness):
    remapped_node = _pod_unique_node_name(package, placement)
    ros_args = [f"-r __ns:=/{robot_id}", f"-r __node:={remapped_node}"]
    # instance_id and metrics_topic are platform-assigned identity, not
    # user-tunable business parameters like latency_threshold_ms -- set
    # unconditionally so onboard and edge never publish on the same topic
    # or answer the same health service name (see companion_analytics/
    # node.py: both are derived from instance_id there).
    #
    # robot_id itself was missing here entirely until found live: the
    # -r __ns:= remap above only affects RELATIVE ROS names, but
    # companion_analytics/node.py builds its health service name as an
    # ABSOLUTE one (leading "/"), which remapping never touches -- so
    # every robot's node fell back to node.py's own hardcoded default
    # ("drone01"), and onboard modules for every robot but drone01 ended
    # up answering the exact same absolute service name,
    # /drone01/companion/onboard/health, a real naming collision on the
    # DDS graph. Confirmed against a live campaign's own saved manifest
    # (results/campaigns/20260920T230236Z/runs/e0-b-001/evidence/
    # kubernetes-resources.yaml): drone02's own rendered --ros-args never
    # set -p robot_id:=, exactly as suspected.
    ros_args.append(f"-p robot_id:={robot_id}")
    ros_args.append(f"-p instance_id:={placement}")
    ros_args.append(f"-p metrics_topic:={metrics_topic(robot_id, placement)}")
    check_parameters(ros_param_map)       # defensive: the CRD rejects them first
    for key, value in (ros_param_map or {}).items():
        ros_args.append(f"-p {key}:={value}")
    if readiness:
        ros_args.append(f"-p health_topic:={_pod_readiness_topic(readiness)}")
    executable = SUPPORTED_PACKAGES[package]["executable"]
    run_line = f"exec ros2 run {package} {executable} --ros-args " + " ".join(ros_args)
    script = f". /ws/install/setup.bash && {run_line}"
    return ["/bin/bash", "-c", script]


def _dds_env_vars():
    """DDS discovery config, read from the operator's own Deployment env
    (operator/deployment.yaml) and applied to every container it creates.

    Optional and independent of the dev-cluster tests so far: those relied
    on plain multicast discovery on a single simple cluster and never
    needed this. The real P2 topology (manifests/kubernetes/p2/40-workload.
    yaml) uses a shared Fast DDS Discovery Server instead -- variant B has
    to join the same DDS domain as the PX4/uXRCE stack it reuses, or it
    would never discover it. RMW_IMPLEMENTATION is paired with the
    discovery server address, not independently configurable: a discovery
    server address is meaningless without rmw_fastrtps_cpp, matching how
    the existing KubeROS manifests always set both together.
    """
    env = []
    discovery_address = os.environ.get("ROS_DISCOVERY_SERVER_ADDRESS")
    if discovery_address:
        env.append({"name": "ROS_DISCOVERY_SERVER", "value": f"{discovery_address}:11811"})
        env.append({"name": "RMW_IMPLEMENTATION", "value": "rmw_fastrtps_cpp"})
    domain_id = os.environ.get("ROS_DOMAIN_ID")
    if domain_id:
        env.append({"name": "ROS_DOMAIN_ID", "value": domain_id})
    return env


STATE_BRIDGE_BUNDLE = "state-bridge"
STATE_BRIDGE_SERVICE_ACCOUNT = "state-bridge"


def _build_state_bridge_container(rosmodule_name, robot_id, package, placement, readiness,
                                  window_sec=2.0):
    # Same Pod as the module it bridges -> same network namespace -> DDS
    # discovery of the target Lifecycle node needs no extra configuration,
    # matching the proposal's "State Bridge, unico componente aggiuntivo
    # lato drone" (S4.1): one sidecar per module, not a separate Deployment.
    # kubernetes_namespace is left unset on purpose: K8sStatusClient.
    # from_service_account() falls back to the namespace file Kubernetes
    # already mounts for this Pod's ServiceAccount, so it never drifts
    # from wherever this Deployment actually lands.
    lifecycle_node_name = f"{robot_id}/{_pod_unique_node_name(package, placement)}"
    script = (
        ". /ws/install/setup.bash && exec ros2 run state_bridge state_bridge "
        "--ros-args "
        f"-p rosmodule_name:={rosmodule_name} "
        f"-p lifecycle_node_name:={lifecycle_node_name} "
        f"-p metrics_topic:={metrics_topic(robot_id, placement)} "
        # A/B temporal contract (V6): spec.metricsWindowSec, float() for the
        # same DOUBLE-vs-INTEGER reason as readiness_timeout_sec below.
        f"-p window_sec:={float(window_sec)}"
    )
    if placement == "onboard":
        script += f" -p telemetry_topic:={telemetry_topic(robot_id)}"
    if readiness:
        script += f" -p readiness_topic:={_pod_readiness_topic(readiness)}"
        script += f" -p readiness_base_topic:={readiness['topic']}"
        # float(...), not the raw value: bridge.py declares this parameter
        # DOUBLE (default 5.0); a YAML integer like `timeoutSec: 5` (no
        # decimal point) round-trips here as Python int 5, and `ros2 run
        # -p key:=5` infers INTEGER from that literal, which rclpy then
        # rejects outright against a DOUBLE-typed parameter -- crash-loops
        # the container. Same class of bug _build_command's own
        # rosParamMap comment already documents; caught here live the
        # same way.
        timeout_sec = float(readiness.get("timeoutSec", 5))
        script += f" -p readiness_timeout_sec:={timeout_sec}"
    return {
        "name": "state-bridge",
        "image": _resolve_bundle_image(STATE_BRIDGE_BUNDLE),
        "command": ["/bin/bash", "-c", script],
        # Needs the same DDS domain as the module it bridges to reach its
        # lifecycle services and subscribe to its metrics topic at all, and
        # its own copy of POD_NAME (same Pod, but env is per-container) to
        # resolve the exact same pod-unique node name the module container
        # just registered under -- see _pod_unique_node_name.
        "env": [_pod_name_env(), _pod_uid_env(), *_dds_env_vars(), *_window_trace_env()],
        "resources": _sidecar_resources(),
    }


def _k8s_safe(name):
    """RFC 1123 has no underscores; ROS package/executable names do
    (companion_analytics). Found live: Kubernetes rejects Deployment and
    container names built straight from the package name with a 422.
    Labels are unaffected -- their value charset already allows `_`."""
    return name.replace("_", "-")


def deployment_name(rosmodule_name):
    # Derived from the ROSModule's own (already-unique) name rather than
    # robot+package: once a robot can run two instances of the same
    # package at once (blue/green P2 migration: onboard + edge), a name
    # built only from robot_id+package would collide between them.
    return _k8s_safe(rosmodule_name)


DEFAULT_EDGE_SELECTOR = {"kuberos.io/role": "edge"}


def _node_selector(placement, robot_id, edge_selector=None):
    # Onboard modules pin to their own robot's node, not just any node with
    # the onboard role: with a single onboard node (P2, the dev cluster)
    # the role alone was never ambiguous, but a multi-robot topology (E0:
    # three separate onboard nodes, one per drone) needs the extra
    # robot.kuberos.io/id selector or Kubernetes could schedule any drone's
    # module onto any drone's node. Both P2's and E0's own k3d topologies
    # already label their onboard node(s) this way (used by KubeROS, never
    # by this operator until now) -- not a new label convention. Edge has
    # no such per-robot pinning: it is one shared resource pool, not one
    # node per robot, matching P2's own edge node. Which nodes form that pool
    # is the robot's RobotFleet's edgeNodeSelector (placement.resolve_fleet,
    # docs/CRD_CONTRACT_AUDIT.md R7); the role label when no fleet lists it.
    if placement == "onboard":
        return {"kuberos.io/role": "onboard", "robot.kuberos.io/id": robot_id}
    return dict(edge_selector or DEFAULT_EDGE_SELECTOR)


def _spec_hash(deployment_spec):
    canonical = json.dumps(deployment_spec, sort_keys=True).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()[:16]


# Same values as the existing, already-validated imperative baseline's own
# companion-analytics template (manifests/kuberos/e0/drone-baseline.
# template.yaml) -- reused, not reinvented, so the two variants' Pods are
# genuinely comparable in resource footprint, not just in ROS behavior.
# Found on review: every module built here had neither probes nor resource
# requests/limits at all, despite the CRD itself declaring a `probes`
# field (never actually set by any scenario manifest so far, but the
# ROSModule CRD's own model -- a `lifecycleTarget` enum -- assumes every
# module it manages is a Lifecycle node, so a lifecycle-state probe is the
# generically correct check here, not something specific to
# companion_analytics).
#
# The memory LIMIT alone is overridable by env var, unset (=256Mi, the
# variant A value) everywhere. It is a measurement instrument, not a
# configuration knob: Bug #65 found companion_analytics sitting at a
# 175-225Mi plateau against this 256Mi limit, with the startup probe
# adding a measured +26Mi transient inside the same cgroup -- so at N=20
# some Pods crossed the limit and were OOMKilled. You cannot measure how
# much memory a process actually wants while killing it at 256Mi, hence
# the override: raise it to non-binding, measure the real plateau, and
# only then choose a limit from a measured number. Do not commit a raised
# default -- that would also break the deliberate symmetry with variant A
# noted above.
_MODULE_RESOURCES = {
    "requests": {"cpu": "75m", "memory": "96Mi"},
    "limits": {
        "cpu": "400m",
        "memory": os.environ.get("MODULE_MEMORY_LIMIT", "256Mi"),
    },
}

# Unlike _MODULE_RESOURCES above, not copied from an already-validated
# variant A value -- state-bridge has no variant A equivalent to measure
# against, so these are a conservative estimate for a lightweight sidecar
# (status polling and a K8s status-subresource patch, no ROS computation
# of its own), not a proven number. Still required, not optional: a
# Resource-type HPA target computes Pod-level CPU utilization from the
# SUM of every container's usage over the SUM of every container's
# request: a sidecar with no request at all makes that ratio undefined
# for the whole Pod, not just for itself -- found on review, confirmed
# against the Kubernetes HPA docs, not by a live scaling test (the shared
# cluster was mid-campaign at the time).
#
# Same override, same reason, same warning as _MODULE_RESOURCES: with the
# cap lifted this sidecar was measured at 64-86Mi, so the 64Mi limit below
# IS binding today -- but that number was measured on a cluster already in
# cascade, so it is not yet a trustworthy basis for a new default either.
_SIDECAR_RESOURCES = {
    "requests": {"cpu": "20m", "memory": "32Mi"},
    "limits": {
        "cpu": "100m",
        "memory": os.environ.get("SIDECAR_MEMORY_LIMIT", "64Mi"),
    },
}

# D6 CPU-limit control only (R11, docs/R11_S2_PARTITION.md, Viviana 2026-09-26):
# with this variable set to "1" on the operator, the state-bridge container is
# built without limits.cpu -- and only that: requests and the memory limit stay,
# the other containers are untouched. Off by default: limits.cpu stays 100m.
# A diagnostic lever, not a sizing decision.
DIAGNOSTIC_NO_CPU_LIMIT_ENV = "STATE_BRIDGE_DIAGNOSTIC_NO_CPU_LIMIT"


def _sidecar_resources():
    if os.environ.get(DIAGNOSTIC_NO_CPU_LIMIT_ENV, "") != "1":
        return _SIDECAR_RESOURCES
    return {"requests": dict(_SIDECAR_RESOURCES["requests"]),
            "limits": {k: v for k, v in _SIDECAR_RESOURCES["limits"].items() if k != "cpu"}}


def _lifecycle_probes(robot_id, package, placement, startup_probe=None):
    lifecycle_node = f"{robot_id}/{_pod_unique_node_name(package, placement)}"
    # A plain `| grep -q active` would also match "inactive" (a substring
    # of it) -- found live, an external review, confirmed by checking
    # `ros2 lifecycle get`'s own output format ("active [3]", "inactive
    # [2]", ...): every pod passes through inactive before active during
    # normal startup, so this was a real, not hypothetical, false-positive
    # window, not just a theoretical risk. Extracts the exact state word
    # and compares it precisely instead; a failed/timed-out command or
    # empty output both correctly fail the probe rather than silently
    # passing (`$(...)` without `|| exit 1` would otherwise swallow a
    # non-zero exit from `ros2 lifecycle get` itself).
    # ROS_SUPER_CLIENT=TRUE restored 2026-09-22 night: removed hours earlier
    # on the theory that a named `ros2 lifecycle get` target doesn't need
    # full-graph discovery -- wrong. Checked ros2cli's own Humble source
    # (ros2lifecycle/verb/get.py): it calls get_node_names() and checks
    # membership BEFORE ever calling the service, for any target, named or
    # not. ROS2CLI_DISABLE_DAEMON=1 was also never doing anything --
    # NodeStrategy only reads the --no-daemon flag, never this env var
    # (ros2cli/node/strategy.py). The env var stays as documentation of
    # intent; SUPER_CLIENT is what actually matters. Without it, this exact
    # probe reproduced `Node not found` / CrashLoopBackOff live tonight, on
    # a freshly reset p2 cluster, variant B, first-attempt Pods included --
    # the earlier "0 restarts, twice" result was not a reliable general
    # guarantee, just what those two particular runs happened to see.
    # spec.probes.startup (optional): S3 sets this to the same
    # periodSeconds=10/timeoutSeconds=20/failureThreshold=40 values variant
    # A's own S3 rendering already uses (render_s3_imperative_manifests.py's
    # S3_STARTUP_PROBE_* constants) -- found live 2026-09-23 that variant B
    # never had an equivalent: its startupProbe stayed fixed at
    # periodSeconds=3/timeoutSeconds=5 in every scenario, including S3,
    # where `ros2 lifecycle get`'s own graph discovery (needs
    # ROS_SUPER_CLIENT=TRUE, see above -- SUPER_CLIENT itself isn't
    # optional, but it still has to finish within the probe's own window)
    # was observed live failing with "Node not found" well within 5s at
    # just N=10, at low measured CPU. Every other scenario (E0/P2/E4/U1/U2)
    # never sets this, so they keep today's defaults unmodified.
    startup_probe = startup_probe or {}
    startup = {
        "periodSeconds": 3,
        "timeoutSeconds": 5,
        "failureThreshold": 30,
    }
    startup.update(startup_probe)
    # Bug #65: `ros2 lifecycle get` has no internal deadline -- measured live
    # at N=20, a single invocation sat for 646s in __skb_wait_for_more_packets
    # (9s of CPU in 646s of wall clock: blocked waiting for a DDS reply that
    # never came, not spinning). In the observed runtime, timed-out probes
    # left processes behind; this is not a general kubelet guarantee. Each
    # timed-out attempt leaked a ~63MB Python process that never exited:
    # measured on one Pod, four of them alive at 601s/461s/321s/181s -- one
    # accumulating every 140s. That produced BOTH failure modes chased
    # separately all morning: the container never turns Ready (the barrier's
    # LifecycleUnavailable), and the leaked processes climb until the cgroup
    # limit kills the container (companion_analytics itself is only ~47MB;
    # the rest of the measured 192Mi plateau was this wreckage plus a
    # ros2-daemon). Raising the memory limit alone could not fix it -- proven
    # live: with limits at 1Gi there were 0 restarts and 0 OOMKills and the
    # bootstrap still failed with 18 LifecycleUnavailable on a fleet that was
    # 65/65 Running and perfectly stable.
    #
    # Bound setup and the CLI together, leaving nominal scheduling margin
    # before the kubelet deadline. Short budgets need a shorter kill grace.
    # Derived from timeoutSeconds rather than hardcoded so S3's widened
    # startup probe (timeoutSeconds=20) does not get a 3s inner deadline.
    # This does NOT fix DDS discovery at N=20 -- it stops a slow probe from
    # escalating into a container death, turning a crash into a legible
    # failure. Both variants were equally exposed; see drone-baseline.
    # template.yaml for variant A's copy of the same fix.
    timeout_sec = int(startup["timeoutSeconds"])
    if timeout_sec < 1:
        raise ValueError("startup timeoutSeconds must be positive")
    kill_grace = 1 if timeout_sec >= 3 else timeout_sec / 4
    kill_after_sec = timeout_sec - 2 if timeout_sec >= 3 else timeout_sec / 4
    # Bug #66: the CLI spawns the ros2 daemon without a new process group, and
    # timeout signals its whole group -- the daemon's SIGTERM handler (rclpy)
    # then shuts its context down while its XML-RPC server keeps answering
    # "!rclpy.ok()" to every later probe, until the kubelet restarts a healthy
    # node. `setsid` moves only the CLI (and the daemon it may spawn) out of the
    # timeout's group: timeout still signals the CLI directly, and setup
    # leftovers stay in the group, so both stay bounded as above.
    ros_command = shlex.quote(
        "source /ws/install/setup.bash && exec setsid ros2 lifecycle get "
        + shlex.quote("/" + lifecycle_node).replace(
            _POD_SUFFIX_EXPR, "'\"" + _POD_SUFFIX_EXPR + "\"'"))
    check = (
        "raw=$(ROS_SUPER_CLIENT=TRUE ROS2CLI_DISABLE_DAEMON=1 "
        f"timeout -k {kill_grace:g} {kill_after_sec:g} /bin/bash -c {ros_command})\n"
        "rc=$?\n"
        'if [ "$rc" -ne 0 ]; then\n'
        '  if [ "$rc" -eq 124 ] || [ "$rc" -eq 137 ]; then\n'
        f'    printf "Lifecycle probe deadline/SIGKILL (rc=%s, budget {kill_after_sec:g}s);'
        ' stdout: %s\\n" "$rc" "$raw" >&2\n'
        "  else\n"
        '    printf "Lifecycle probe RPC failed (rc=%s); stdout: %s\\n"'
        ' "$rc" "$raw" >&2\n'
        "  fi\n"
        "  exit 1\n"
        "fi\n"
        '[ "${raw%% *}" = "active" ] || '
        '{ printf "Lifecycle probe expected active, got: %s\\n" "$raw" >&2; exit 1; }'
    )
    return {
        "startupProbe": {
            "exec": {"command": ["/bin/bash", "-c", check]},
            **startup,
        },
        "readinessProbe": {
            "exec": {"command": ["/bin/bash", "-c", "kill -0 1"]},
            "periodSeconds": 3,
        },
    }


def build_service_manifest(name, namespace, service):
    """R1, decision 4 (docs/CRD_CONTRACT_AUDIT.md): the optional ClusterIP Service
    of a ROSModule -- declared ports, the module's own Pods as selector (the
    Deployment's owner label), same name as the Deployment. Not part of the
    Pod template, so changing it never rolls the Pods."""
    return {
        "apiVersion": "v1",
        "kind": "Service",
        "metadata": {"name": name, "namespace": namespace, "labels": {OWNER_LABEL: name}},
        "spec": {
            "type": service.get("type", "ClusterIP"),
            "selector": {OWNER_LABEL: name},
            "ports": [{"name": port["name"], "port": int(port["port"]),
                       "targetPort": int(port.get("targetPort", port["port"])),
                       "protocol": port.get("protocol", "TCP")}
                      for port in service["ports"]],
        },
    }


def build_deployment_manifest(name, namespace, spec, node_selector_role, rosmodule_name,
                              edge_node_selector=None):
    labels = {
        "app": spec["package"],
        "robot": spec["robotId"],
        "placement": spec["placement"],
        OWNER_LABEL: name,
    }
    check_supported(spec["package"])      # defensive: the controller rejects it first
    image, bundle_name = _resolve_image(spec["package"])
    # spec.probes.readiness (proposal S3): read once here, passed to both
    # containers below so they always agree on the topic (see
    # _build_command's own comment) -- None, not {}, when unset, so both
    # call sites can use a plain truthiness check.
    readiness = spec.get("probes", {}).get("readiness")
    startup_probe = spec.get("probes", {}).get("startup")
    deployment_spec = {
        "replicas": 1,
        "selector": {"matchLabels": labels},
        # No overlap between the old and new Pod during a rollout: two ROS 2
        # nodes with the same identity on the DDS domain at once is exactly
        # the failure mode the existing KubeROS Deployment renderer avoids
        # (docs/IMPLEMENTATION_STATUS.md, "Funzioni KubeROS Aggiunte").
        "strategy": {
            "type": "RollingUpdate",
            "rollingUpdate": {"maxSurge": 0, "maxUnavailable": 1},
        },
        "template": {
            "metadata": {"labels": labels},
            "spec": {
                "restartPolicy": "Always",
                # Kubernetes has no per-container ServiceAccount, only
                # per-Pod: the main module container ends up with the
                # state-bridge token mounted too, even though it never
                # uses it. Narrower than reusing fleet-operator's
                # ServiceAccount (which can also CRUD Deployments), but
                # not fully isolated from the module container -- note
                # for Fase 2 if that boundary needs to be tightened.
                "serviceAccountName": STATE_BRIDGE_SERVICE_ACCOUNT,
                "nodeSelector": _node_selector(node_selector_role, spec["robotId"],
                                               edge_node_selector),
                "containers": [
                    {
                        "name": _k8s_safe(spec["package"]),
                        "image": image,
                        "command": _build_command(
                            spec["package"],
                            spec["robotId"],
                            spec["placement"],
                            spec.get("rosParamMap"),
                            readiness,
                        ),
                        "env": [
                            {"name": "ROBOT_ID", "value": spec["robotId"]},
                            _pod_name_env(),
                            *_dds_env_vars(),
                        ],
                        "resources": _MODULE_RESOURCES,
                        **_lifecycle_probes(
                            spec["robotId"], spec["package"], spec["placement"],
                            startup_probe,
                        ),
                    },
                    _build_state_bridge_container(
                        rosmodule_name,
                        spec["robotId"],
                        spec["package"],
                        spec["placement"],
                        readiness,
                        spec.get("metricsWindowSec", DEFAULT_METRICS_WINDOW_SEC),
                    ),
                ],
            },
        },
    }
    return {
        "apiVersion": "apps/v1",
        "kind": "Deployment",
        "metadata": {
            "name": name,
            "namespace": namespace,
            "labels": labels,
            "annotations": {
                "dronekube.io/image-bundle": bundle_name,
                SPEC_HASH_ANNOTATION: _spec_hash(deployment_spec),
            },
        },
        "spec": deployment_spec,
    }
