#!/usr/bin/env bash
set -euo pipefail

# S3: scalability sweep. One (N_ROBOTS, VARIANT) combination per invocation
# -- run repeatedly (see scripts/run_s3_sweep.sh) across N in {3,10,20,30,50} and
# both variants to build the full comparison the proposal's own S3 asks
# for (proposal KPI table: N in {3,10,20,30,50}).
#
# Capacity must be measured on the current host. The former 12-CPU VM's
# N=20 limit is not a verified ceiling on the 48-CPU host used on 2026-09-23.
#
# Full PX4 SITL fidelity per robot throughout (not a lighter stand-in):
# one dedicated onboard k3d node per robot, same as E0/S4, chosen over a
# lighter-weight alternative that could reach N=50 -- an explicit choice
# to keep every robot a real simulated drone, at the cost of a lower N
# ceiling on this hardware.
#
# One incident only, on drone01 (the rest of the fleet stays nominal,
# providing load without an incident of their own): a successful SLO
# migration, the same mechanism already validated in P2 (variant A) and
# S4 (variant B) -- S3 is not re-testing correctness, it measures how
# long that same incident takes, and how much API-server/etcd load it (and
# just running N robots at all) generates, as N grows.

ROOT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
CLUSTER=cloud-native-s3
CONTEXT=k3d-cloud-native-s3
NAMESPACE=cloud-native-s3
N_ROBOTS=${N_ROBOTS:?set N_ROBOTS (e.g. 3, 10, 20)}
VARIANT=${VARIANT:-a}
RESULT_ID=$(date -u +%Y%m%dT%H%M%SZ)
RESULT_DIR="$ROOT_DIR/results/s3/N${N_ROBOTS}-${VARIANT}-$RESULT_ID"
RENDERED_DIR="$RESULT_DIR/rendered"
INCIDENT_TIMEOUT_SEC=${S3_INCIDENT_TIMEOUT_SEC:-180}
HARNESS_PROTOCOL=s3-ros-param-v1
S3_BOOTSTRAP_BARRIER_TIMEOUT_SEC=${S3_BOOTSTRAP_BARRIER_TIMEOUT_SEC:-600}
S3_ROSMODULE_UPDATE_TIMEOUT_SEC=${S3_ROSMODULE_UPDATE_TIMEOUT_SEC:-600}
S3_MODULE_MEMORY_LIMIT=${S3_MODULE_MEMORY_LIMIT:-256Mi}
S3_SIDECAR_MEMORY_LIMIT=${S3_SIDECAR_MEMORY_LIMIT:-64Mi}
S3_A_ANALYTICS_CPU_LIMIT_M=${S3_A_ANALYTICS_CPU_LIMIT_M:-}
K=(kubectl --context "$CONTEXT")
mkdir -p "$RESULT_DIR" "$RENDERED_DIR"
echo "Result dir: $RESULT_DIR"

case "$VARIANT" in
  a|b) ;;
  *)
    echo "Unsupported VARIANT: $VARIANT (expected 'a' or 'b')" >&2
    exit 2
    ;;
esac
if [[ "$N_ROBOTS" -lt 1 ]]; then
  echo "N_ROBOTS must be >= 1" >&2
  exit 2
fi

for command in docker k3d kubectl python3 timeout; do
  command -v "$command" >/dev/null || {
    echo "Required command not found: $command" >&2
    exit 1
  }
done

for value in "$S3_BOOTSTRAP_BARRIER_TIMEOUT_SEC" "$S3_ROSMODULE_UPDATE_TIMEOUT_SEC" "$INCIDENT_TIMEOUT_SEC"; do
  [[ "$value" =~ ^[1-9][0-9]*$ ]] || { echo "S3 budgets must be positive integer seconds" >&2; exit 2; }
done
for value in "$S3_MODULE_MEMORY_LIMIT" "$S3_SIDECAR_MEMORY_LIMIT"; do
  [[ "$value" =~ ^[1-9][0-9]*(Mi|Gi)$ ]] || { echo "S3 memory limits must be positive integer Mi or Gi" >&2; exit 2; }
done
if [[ -n "$S3_A_ANALYTICS_CPU_LIMIT_M" ]]; then
  [[ "$VARIANT" == a && "$S3_A_ANALYTICS_CPU_LIMIT_M" =~ ^[1-9][0-9]*$ ]] || {
    echo "S3_A_ANALYTICS_CPU_LIMIT_M requires variant a and positive millicores" >&2
    exit 2
  }
fi
python3 "$ROOT_DIR/scripts/s3_provenance.py" --root "$ROOT_DIR" --exclude-thesis \
  --output "$RESULT_DIR/provenance" >"$RESULT_DIR/source-sha256.txt"
python3 "$ROOT_DIR/scripts/s3_cell_verdict.py" check-clean "$ROOT_DIR" \
  >"$RESULT_DIR/input-cleanliness.txt" || {
    echo "S3 requires clean project inputs; see $RESULT_DIR/input-cleanliness.txt" >&2
    exit 2
  }

metrics_snapshot() {
  python3 "$ROOT_DIR/scripts/s3_metrics.py" snapshot --context "$CONTEXT" --out "$1"
}

inject_ros_latency() {
  local target node
  if [[ "$VARIANT" == "a" ]]; then
    target=deployment/drone01-companion-analytics-onboard
    node=/drone01/companion_analytics_onboard
    "${K[@]}" exec -n "$NAMESPACE" "$target" -- bash -c \
      "source /ws/install/setup.bash && ROS_SUPER_CLIENT=TRUE timeout -k 1 30 setsid ros2 param set --no-daemon --spin-time 5 $node processing_delay_ms 300.0 && ROS_SUPER_CLIENT=TRUE timeout -k 1 30 setsid ros2 param get --no-daemon --spin-time 5 $node processing_delay_ms" \
      >"$RESULT_DIR/incident-injection.log" 2>&1 || return 1
  else
    target=deployment/companion-analytics-drone01
    "${K[@]}" exec -n "$NAMESPACE" "$target" -c companion-analytics -- bash -c \
      'source /ws/install/setup.bash && ROS_SUPER_CLIENT=TRUE timeout -k 1 30 setsid ros2 param set --no-daemon --spin-time 5 "/drone01/companion_analytics_${POD_NAME//-/_}" processing_delay_ms 300.0 && ROS_SUPER_CLIENT=TRUE timeout -k 1 30 setsid ros2 param get --no-daemon --spin-time 5 "/drone01/companion_analytics_${POD_NAME//-/_}" processing_delay_ms' \
      >"$RESULT_DIR/incident-injection.log" 2>&1 || return 1
  fi
  grep -q 'Set parameter successful' "$RESULT_DIR/incident-injection.log" &&
    grep -q 'Double value is: 300.0' "$RESULT_DIR/incident-injection.log"
}

run_incident() {
  "${K[@]}" get pods -n "$NAMESPACE" -o json >"$RESULT_DIR/pods-before-incident.json"
  metrics_snapshot "$RESULT_DIR/metrics-before-incident.json"
  INCIDENT_START_NS=$(date +%s%N)
  INCIDENT_START_MONOTONIC_NS=$(python3 -c 'import time; print(time.monotonic_ns())')
  PASS=false
  if inject_ros_latency; then
    if python3 "$ROOT_DIR/scripts/s3_runtime.py" --context "$CONTEXT" --namespace "$NAMESPACE" \
      wait-incident --variant "$VARIANT" --timeout "$INCIDENT_TIMEOUT_SEC" \
      --started-monotonic-ns "$INCIDENT_START_MONOTONIC_NS" \
      --output "$RESULT_DIR/incident-observation.json"; then
      PASS=true
    fi
  else
    echo 'ROS parameter injection or readback failed; incident inconclusive' >&2
    python3 -c 'import json,sys; from pathlib import Path; Path(sys.argv[1]).write_text(json.dumps({"state":"inconclusive","outcome":"injection failed"},indent=2)+"\n")' \
      "$RESULT_DIR/incident-observation.json"
  fi
  INCIDENT_END_NS=$(date +%s%N)
  metrics_snapshot "$RESULT_DIR/metrics-after-incident.json"
  INCIDENT_MS=$(( (INCIDENT_END_NS - INCIDENT_START_NS) / 1000000 ))
  OUTCOME=$(python3 -c 'import json,sys; d=json.load(open(sys.argv[1])); print(d["outcome"] or d["state"])' "$RESULT_DIR/incident-observation.json")
}

cluster_exists() {
  k3d cluster list --no-headers | awk '{print $1}' | grep -qx "$CLUSTER"
}

# k3d image import's default "tools-node" propagation was found live to
# silently drop locally-built (cloud-native-ros/*) images on some nodes
# once the cluster gets large enough (first seen at N=20: agent-10 onward
# never got them, while agent-0..9 did) -- k3d itself reports "Successfully
# imported" regardless, so the only sign is a later ImagePullBackOff (these
# images have no registry to fall back to, unlike the public
# microros/px4io ones, which just re-pull from the internet and never
# fail this way). Verify per node and retry the whole import rather than
# trust k3d's own success message.
image_present_on_node() {
  local node="$1" repo="$2" tag="$3"
  docker exec "$node" crictl images 2>/dev/null \
    | grep -qE "docker\.io/${repo}[[:space:]]+${tag}([[:space:]]|\$)"
}

check_images_on_nodes_once() {
  local images=("$@")
  local nodes missing=0
  nodes=$(docker ps --format '{{.Names}}' | grep -E "^k3d-${CLUSTER}-(server|agent)-[0-9]+\$")
  for node in $nodes; do
    for image in "${images[@]}"; do
      case "$image" in
        cloud-native-ros/*) ;;
        *) continue ;;
      esac
      if ! image_present_on_node "$node" "${image%%:*}" "${image##*:}"; then
        echo "Missing image $image on node $node" >&2
        missing=1
      fi
    done
  done
  return "$missing"
}

# A single check right after "k3d image import" returns was found live to
# be a false positive at N=20: containerd on a slower node (agent-10) had
# already registered the image's metadata, but not finished unpacking it,
# so it read as present, then read as gone minutes later when a pod
# actually tried to run from it (ImagePullBackOff, no registry fallback
# for these locally-built images). Require the same clean result twice,
# 60s apart, before trusting it -- (k3d image import -m direct, tried as
# an alternative to the default tools-node relay, was found live to just
# hang indefinitely at this node count, so it's not an option here).
verify_images_on_nodes() {
  local images=("$@")
  check_images_on_nodes_once "${images[@]}" || return 1
  sleep 60
  check_images_on_nodes_once "${images[@]}"
}

import_images_with_retry() {
  local images=("$@")
  local attempt
  for attempt in 1 2 3; do
    run_managed k3d image import -c "$CLUSTER" "${images[@]}"
    if verify_images_on_nodes "${images[@]}"; then
      return 0
    fi
    echo "k3d image import incomplete on some nodes (attempt $attempt/3); retrying..." >&2
  done
  echo "k3d image import still incomplete after 3 attempts" >&2
  return 1
}

# Found live at N=20: kubelet's own image GC can evict a freshly-imported
# local image (no registry fallback) at ANY point during the bootstrap
# window, not just right after "k3d image import" returns -- a one-time
# post-import settle-check can't catch a failure that only manifests
# minutes later (seen live: a robot's pod went ImagePullBackOff ~19
# minutes into a run that had already passed the settle-check cleanly).
# Blindly re-importing periodically for the duration of the bootstrap
# wait is a safe no-op for nodes that still have the image (content-
# addressed layers, nothing to re-transfer) and heals the ones that
# don't well within KubeROS's own 600s per-robot readiness timeout
# (S3's own --timeout-sec 600 in manifests/kubernetes/s3/50-bootstrap.yaml).
REIMPORT_PID=""
start_periodic_reimport() {
  python3 "$ROOT_DIR/scripts/s3_disk_guard.py" --path "$DISK_CHECK_PATH" \
    --reserve-gib "$CRITICAL_FREE_GB" --repeat 90 -- \
    k3d image import -c "$CLUSTER" "$@" >"$RESULT_DIR/periodic-import.log" 2>&1 &
  REIMPORT_PID=$!
}
stop_periodic_reimport() {
  if [[ -n "$REIMPORT_PID" ]]; then
    kill "$REIMPORT_PID" 2>/dev/null || true
    wait "$REIMPORT_PID" 2>/dev/null || true
    REIMPORT_PID=""
  fi
}

# Disk-space safety net -- found live on 2026-09-20 that a single N=20
# cluster can consume close to this machine's entire disk (each of the
# 22 nodes keeps its own independent copy of the imported images, since
# containerd snapshotters don't share content across separate node
# containers -- ~9.3GB/node was observed with the pre-optimization image
# sizes; the multi-stage rebuilds should lower that, but the check stays
# as a safety net regardless of exactly how much). This matters far more
# for an unattended multi-hour/multi-day sweep (e.g. run in screen/tmux,
# see scripts/run_s3_sweep.sh) than for an interactively-watched run,
# where a silent disk-full mid-run risks corrupting the whole cluster.
DISK_CHECK_PATH=${S3_DISK_CHECK_PATH:-/}
# Conservative capacity estimate, not a measured peak-memory/disk model.
# The former 5 GiB/node admitted N=50 and exhausted the disk during unpack.
MIN_FREE_GB_PER_NODE=${S3_MIN_FREE_GB_PER_NODE:-10}
CRITICAL_FREE_GB=${S3_CRITICAL_FREE_GB:-24}
for value in "$MIN_FREE_GB_PER_NODE" "$CRITICAL_FREE_GB"; do
  [[ "$value" =~ ^[1-9][0-9]*$ ]] || { echo "Disk settings must be positive integer GiB" >&2; exit 2; }
done

free_gb() {
  df -B1 --output=avail "$DISK_CHECK_PATH" | awk 'NR==2 {printf "%.0f", int($1/1073741824)}'
}

check_disk_preflight() {
  local total_nodes=$((N_ROBOTS + 2))
  local required=$((total_nodes * MIN_FREE_GB_PER_NODE + CRITICAL_FREE_GB))
  local available
  available=$(free_gb)
  echo "Disk preflight: ${available}GiB free on $DISK_CHECK_PATH, need ~${required}GiB for $total_nodes nodes (${MIN_FREE_GB_PER_NODE}GiB/node + ${CRITICAL_FREE_GB}GiB reserve)."
  if [[ "$available" -lt "$required" ]]; then
    echo "Disk preflight failed: provision capacity or measure the import peak before changing the estimate." >&2
    exit 1
  fi
}

DISK_MONITOR_PID=""
monitor_disk() {
  while true; do
    sleep 2
    local available
    available=$(free_gb)
    if [[ "$available" -lt "$CRITICAL_FREE_GB" ]]; then
      echo "CRITICAL: disk reserve reached (${available}GiB free); stopping this run." >&2
      kill -TERM "$$" 2>/dev/null || true
      exit 1
    fi
  done
}
start_disk_monitor() {
  monitor_disk &
  DISK_MONITOR_PID=$!
}
stop_disk_monitor() {
  if [[ -n "$DISK_MONITOR_PID" ]]; then
    kill "$DISK_MONITOR_PID" 2>/dev/null || true
    wait "$DISK_MONITOR_PID" 2>/dev/null || true
    DISK_MONITOR_PID=""
  fi
}

MANAGED_PID=""
CLUSTER_TOUCHED=0
run_managed() {
  local rc=0
  python3 "$ROOT_DIR/scripts/s3_disk_guard.py" --path "$DISK_CHECK_PATH" \
    --reserve-gib "$CRITICAL_FREE_GB" -- "$@" &
  MANAGED_PID=$!
  wait "$MANAGED_PID" || rc=$?
  MANAGED_PID=""
  return "$rc"
}

finish_s3() {
  local rc=$?
  trap - EXIT TERM INT
  set +e
  if [[ -n "$MANAGED_PID" ]]; then
    kill -TERM "$MANAGED_PID" 2>/dev/null
    wait "$MANAGED_PID" 2>/dev/null
  fi
  stop_periodic_reimport
  stop_disk_monitor
  if [[ "$CLUSTER_TOUCHED" == 1 ]]; then
    timeout 15s "${K[@]}" --request-timeout=10s get pods -n "$NAMESPACE" -o json \
      >"$RESULT_DIR/runtime-pods.json" 2>"$RESULT_DIR/runtime-pods-error.txt"
  fi
  local provenance_rc=0
  python3 "$ROOT_DIR/scripts/s3_cell_verdict.py" verdict "$ROOT_DIR" "$RESULT_DIR" "$VARIANT" "$rc" \
    >"$RESULT_DIR/provenance-check.log" 2>&1 || provenance_rc=$?
  if [[ "$rc" == 0 ]] && { [[ "$provenance_rc" != 0 ]] || grep -qx 'INVALID' "$RESULT_DIR/provenance-check.log"; }; then
    rc=2
  fi
  if [[ "$rc" != 0 && "$CLUSTER_TOUCHED" == 1 ]]; then
    printf 'exit_code=%s\n' "$rc" >"$RESULT_DIR/failure.txt"
    timeout 130s python3 "$ROOT_DIR/scripts/s3_runtime.py" \
      --context "$CONTEXT" --namespace "$NAMESPACE" collect \
      --output "$RESULT_DIR/diagnostics" --budget 120
  fi
  exit "$rc"
}
trap finish_s3 EXIT
trap 'exit 143' TERM
trap 'exit 130' INT

# A staggered, "k3d node create"-based bring-up was tried live at N=20 to
# work around what looked like a cluster-creation-time contention issue.
# It turned out unnecessary (the real cause was an unrelated bad kubelet
# flag, since removed -- plain concurrent creation worked fine before that
# flag existed) AND incompatible with variant A regardless: KubeROS's own
# "initialize" step looks up onboard/edge nodes by their literal k8s Node
# name, and "k3d node create" cannot reproduce "k3d-<cluster>-agent-N"
# exactly (it always appends its own replica suffix). Reverted to a single
# "k3d cluster create" for all agents.
EXPECTED_NODE_COUNT=$((N_ROBOTS + 2))
if cluster_exists; then
  if [[ "${RESET_S3:-0}" == "1" ]]; then
    if [[ "${S3_ALLOW_CLUSTER_DELETE:-0}" != "1" ]]; then
      echo "Refusing to delete existing $CLUSTER without S3_ALLOW_CLUSTER_DELETE=1" >&2
      exit 2
    fi
    k3d cluster delete "$CLUSTER"
  elif [[ "${S3_ALLOW_CLUSTER_REUSE:-0}" != "1" ]]; then
    echo "Refusing to reuse existing $CLUSTER without S3_ALLOW_CLUSTER_REUSE=1" >&2
    exit 2
  fi
fi
# Runs after the delete above so a RESET_S3=1 run is checked against the
# space that will actually be free once the old cluster is torn down, not
# against space that's still (about to be) occupied by it.
check_disk_preflight
start_disk_monitor
CLUSTER_TOUCHED=1
# Variant B knobs, defaulted ONCE here so harness-config.txt records exactly
# the values the run below uses. s3-readiness-v3 = v2 with both B budgets
# widened to 600s (bootstrap barrier 180s -> 600s, ROSModuleController
# convergence 120s -> 600s): found 2026-09-23 that eight N=20-B runs on
# different configurations all wrote an identical "protocol=s3-readiness-v2"
# file, making them indistinguishable afterwards -- see DEV_SMOKE_TEST.md,
# Bug #65, for the run-by-run map. The memory limits are recorded as
# "default" unless the Bug #65 measurement override is set.
{
  printf 'protocol=%s\nvariant=%s\nrobots=%s\nmin_gib_per_node=%s\nreserve_gib=%s\n' \
    "$HARNESS_PROTOCOL" "$VARIANT" "$N_ROBOTS" "$MIN_FREE_GB_PER_NODE" "$CRITICAL_FREE_GB"
  printf 'git_revision=%s\ngit_uncommitted_paths=%s\n' \
    "$(git -C "$ROOT_DIR" rev-parse --short HEAD 2>/dev/null || echo unknown)" \
    "$(git -C "$ROOT_DIR" status --porcelain 2>/dev/null | wc -l)"
  printf 'b_bootstrap_barrier_timeout_sec=%s\nb_rosmodule_update_timeout_sec=%s\n' \
    "$S3_BOOTSTRAP_BARRIER_TIMEOUT_SEC" "$S3_ROSMODULE_UPDATE_TIMEOUT_SEC"
  printf 'b_module_memory_limit=%s\nb_sidecar_memory_limit=%s\n' \
    "$S3_MODULE_MEMORY_LIMIT" "$S3_SIDECAR_MEMORY_LIMIT"
  printf 'a_analytics_cpu_limit_m=%s\n' "${S3_A_ANALYTICS_CPU_LIMIT_M:-default}"
  printf 'reset_s3=%s\nskip_build_import=%s\nskip_build=%s\nskip_import=%s\nincident_timeout_sec=%s\n' \
    "${RESET_S3:-0}" "${SKIP_IMAGE_BUILD_IMPORT:-0}" "${SKIP_IMAGE_BUILD:-0}" "${SKIP_IMAGE_IMPORT:-0}" "$INCIDENT_TIMEOUT_SEC"
  printf 'allow_cluster_delete=%s\nallow_cluster_reuse=%s\n' \
    "${S3_ALLOW_CLUSTER_DELETE:-0}" "${S3_ALLOW_CLUSTER_REUSE:-0}"
  printf 'source_sha256=%s\n' "$(<"$RESULT_DIR/source-sha256.txt")"
} >"$RESULT_DIR/harness-config.txt"
if ! cluster_exists; then
  python3 "$ROOT_DIR/scripts/render_s3_k3d_config.py" --n-robots "$N_ROBOTS" \
    --cluster-name "$CLUSTER" --output "$RENDERED_DIR/k3d-config.yaml"
  run_managed k3d cluster create --config "$RENDERED_DIR/k3d-config.yaml"
else
  NODE_COUNT=$("${K[@]}" get nodes --no-headers | wc -l)
  ONBOARD_COUNT=$("${K[@]}" get nodes -l kuberos.io/role=onboard --no-headers | wc -l)
  if [[ "$NODE_COUNT" != "$EXPECTED_NODE_COUNT" || "$ONBOARD_COUNT" != "$N_ROBOTS" ]]; then
    echo "Cluster $CLUSTER topology does not match N_ROBOTS=$N_ROBOTS; rerun with RESET_S3=1" >&2
    exit 1
  fi
fi
# datastore of the bench, embedded etcd (R14 target, docs/ETCD_GATE_PREREGISTRATION.md):
# fail-closed, before any workload or injection
python3 "$ROOT_DIR/scripts/datastore_check.py" check "$CLUSTER" "$RESULT_DIR" \
  || { echo "datastore of $CLUSTER not verified as embedded etcd: stopping before any workload" >&2; exit 2; }
"${K[@]}" wait --for=condition=Ready node --all --timeout=120s

DISCOVERY_SERVER_ADDRESS=$("${K[@]}" get node "k3d-${CLUSTER}-server-0" \
  -o jsonpath='{.status.addresses[?(@.type=="InternalIP")].address}')

if [[ "${SKIP_IMAGE_BUILD_IMPORT:-0}" != "1" && "${SKIP_IMAGE_BUILD:-0}" != "1" ]]; then
  run_managed docker build -t cloud-native-ros/control-plane:p2 \
    -f "$ROOT_DIR/containers/control-plane/Dockerfile" "$ROOT_DIR"
  if [[ "$VARIANT" == "a" ]]; then
    run_managed docker build -t cloud-native-ros/event-detector:p2 \
      -f "$ROOT_DIR/containers/event-detector/Dockerfile" "$ROOT_DIR"
    run_managed docker build -t cloud-native-ros/kuberos:p2 \
      -f "$ROOT_DIR/containers/kuberos/Dockerfile" "$ROOT_DIR"
  else
    run_managed docker build -t cloud-native-ros/fleet-operator:p2 \
      -f "$ROOT_DIR/containers/fleet-operator/Dockerfile" "$ROOT_DIR"
    run_managed docker build -t cloud-native-ros/state-bridge:p2 \
      -f "$ROOT_DIR/containers/state-bridge/Dockerfile" "$ROOT_DIR"
  fi
fi
if [[ "${SKIP_IMAGE_BUILD_IMPORT:-0}" != "1" && "${SKIP_IMAGE_IMPORT:-0}" != "1" ]]; then
  if [[ "$VARIANT" == "a" ]]; then
    import_images_with_retry \
      cloud-native-ros/control-plane:p2 cloud-native-ros/event-detector:p2 cloud-native-ros/kuberos:p2 \
      microros/micro-ros-agent:humble px4io/px4-sitl:latest redis:7
  else
    import_images_with_retry \
      cloud-native-ros/control-plane:p2 cloud-native-ros/fleet-operator:p2 cloud-native-ros/state-bridge:p2 \
      microros/micro-ros-agent:humble px4io/px4-sitl:latest
  fi
fi

for image in cloud-native-ros/control-plane:p2 cloud-native-ros/event-detector:p2 \
  cloud-native-ros/kuberos:p2 cloud-native-ros/fleet-operator:p2 \
  cloud-native-ros/state-bridge:p2 microros/micro-ros-agent:humble px4io/px4-sitl:latest; do
  docker image inspect --format '{{.Id}} {{json .RepoTags}} {{json .RepoDigests}}' "$image" \
    >>"$RESULT_DIR/local-image-ids.txt" 2>>"$RESULT_DIR/local-image-errors.txt" || true
done

render_p2_style() {
  # p2/*.yaml manifests are all hardcoded to namespace cloud-native-p2 --
  # S3 reuses them verbatim, just re-namespaced/re-clustered, same
  # technique as e0/e1/s4's own placeholder substitution.
  sed \
    -e "s/cloud-native-p2/$NAMESPACE/g" \
    -e "s/__P2_DISCOVERY_ADDRESS__/$DISCOVERY_SERVER_ADDRESS/g" \
    -e "s/__P2_GOAL_TIMEOUT_SEC__/60.0/g" \
    "$1" >"$2"
}

if [[ "$VARIANT" == "a" ]]; then
# =====================================================================
# VARIANT A: imperativo. KubeROS + Application Manager + Dispatcher su
# N robot; unico guasto: SLO su drone01 (edge di successo, riuso letterale
# di manifests/kuberos/p2/analytics-edge.yaml -- gia' drone01-specifico).
# =====================================================================

render_p2_style "$ROOT_DIR/manifests/kubernetes/p2/00-rbac.yaml" "$RENDERED_DIR/00-rbac.yaml"
render_p2_style "$ROOT_DIR/manifests/kubernetes/p2/10-discovery.yaml" "$RENDERED_DIR/10-discovery.yaml"
render_p2_style "$ROOT_DIR/manifests/kubernetes/p2/25-observability.yaml" "$RENDERED_DIR/25-observability.yaml"
render_p2_style "$ROOT_DIR/manifests/kubernetes/p2/30-control-plane.yaml" "$RENDERED_DIR/30-control-plane.yaml"
sed -e "s/__P2_DISCOVERY_ADDRESS__/$DISCOVERY_SERVER_ADDRESS/g" \
  "$ROOT_DIR/manifests/kuberos/p2/analytics-edge.yaml" >"$RENDERED_DIR/analytics-edge.yaml"

RENDER_CPU_ARGS=()
if [[ -n "$S3_A_ANALYTICS_CPU_LIMIT_M" ]]; then
  RENDER_CPU_ARGS=(--onboard-analytics-cpu-limit-millicores "$S3_A_ANALYTICS_CPU_LIMIT_M")
fi
python3 "$ROOT_DIR/scripts/render_s3_imperative_manifests.py" \
  --n-robots "$N_ROBOTS" --cluster-name "$CLUSTER" --namespace "$NAMESPACE" \
  --discovery-address "$DISCOVERY_SERVER_ADDRESS" --output-dir "$RENDERED_DIR/kuberos-manifests" \
  "${RENDER_CPU_ARGS[@]}"
sed -e "s/__S3_N_ROBOTS__/$N_ROBOTS/g" \
  "$ROOT_DIR/manifests/kubernetes/s3/50-bootstrap.yaml" >"$RENDERED_DIR/50-bootstrap.yaml"

"${K[@]}" apply -f "$RENDERED_DIR/00-rbac.yaml"
"${K[@]}" apply -f "$RENDERED_DIR/10-discovery.yaml"
"${K[@]}" apply -f "$RENDERED_DIR/kuberos-manifests/20-kuberos.yaml"
"${K[@]}" apply -f "$RENDERED_DIR/25-observability.yaml"
"${K[@]}" rollout status deployment/p2-fastdds-discovery -n "$NAMESPACE" --timeout=120s
"${K[@]}" rollout status deployment/kuberos -n "$NAMESPACE" --timeout=240s
"${K[@]}" wait --for=create secret/kuberos-api-token -n "$NAMESPACE" --timeout=90s

"${K[@]}" create configmap p2-analytics-edge-manifest -n "$NAMESPACE" \
  --from-file=analytics-edge.yaml="$RENDERED_DIR/analytics-edge.yaml" \
  --dry-run=client -o yaml | "${K[@]}" apply -f -
"${K[@]}" apply -f "$RENDERED_DIR/30-control-plane.yaml"
"${K[@]}" rollout status deployment/application-manager-p2 -n "$NAMESPACE" --timeout=120s
"${K[@]}" rollout status deployment/operational-event-dispatcher-p2 -n "$NAMESPACE" --timeout=120s

"${K[@]}" create configmap s3-kuberos-manifests -n "$NAMESPACE" \
  $(for i in $(seq 1 "$N_ROBOTS"); do printf -- "--from-file=drone%02d.yaml=%s/kuberos-manifests/drone%02d.yaml " "$i" "$RENDERED_DIR" "$i"; done) \
  --dry-run=client -o yaml | "${K[@]}" apply -f -
"${K[@]}" delete job s3-kuberos-bootstrap -n "$NAMESPACE" --ignore-not-found

BOOTSTRAP_START_NS=$(date +%s%N)
metrics_snapshot "$RESULT_DIR/metrics-before-bootstrap.json"
"${K[@]}" apply -f "$RENDERED_DIR/50-bootstrap.yaml"
start_periodic_reimport cloud-native-ros/control-plane:p2 cloud-native-ros/event-detector:p2 cloud-native-ros/kuberos:p2
# Observer budget follows the unchanged Job deadline plus controller grace.
BOOTSTRAP_WAIT=$(python3 -c 'import sys,yaml; print(yaml.safe_load(open(sys.argv[1]))["spec"]["activeDeadlineSeconds"] + 300)' "$RENDERED_DIR/50-bootstrap.yaml")
python3 "$ROOT_DIR/scripts/s3_runtime.py" --context "$CONTEXT" --namespace "$NAMESPACE" \
  wait-job s3-kuberos-bootstrap --timeout "$BOOTSTRAP_WAIT" --output "$RESULT_DIR/bootstrap-job.json"
"${K[@]}" wait --for=condition=available deployment --all -n "$NAMESPACE" --timeout=600s
stop_periodic_reimport
BOOTSTRAP_END_NS=$(date +%s%N)
metrics_snapshot "$RESULT_DIR/metrics-after-bootstrap.json"
BOOTSTRAP_MS=$(( (BOOTSTRAP_END_NS - BOOTSTRAP_START_NS) / 1000000 ))

run_incident

"${K[@]}" get pods -n "$NAMESPACE" -o wide >"$RESULT_DIR/pods.txt"
"${K[@]}" logs -n "$NAMESPACE" deployment/application-manager-p2 >"$RESULT_DIR/application-manager.log" 2>&1 || true
"${K[@]}" logs -n "$NAMESPACE" deployment/operational-event-dispatcher-p2 >"$RESULT_DIR/dispatcher.log" 2>&1 || true

else
# =====================================================================
# VARIANT B: dichiarativo. Fleet Operator su N ROSModule + RobotFleet;
# unico guasto: SLO su drone01 (MigratePlacement di successo, come P2).
# =====================================================================

render_p2_style "$ROOT_DIR/manifests/kubernetes/p2/00-rbac.yaml" "$RENDERED_DIR/00-rbac.yaml"
render_p2_style "$ROOT_DIR/manifests/kubernetes/p2/10-discovery.yaml" "$RENDERED_DIR/10-discovery.yaml"
render_p2_style "$ROOT_DIR/manifests/kubernetes/p2/25-observability.yaml" "$RENDERED_DIR/25-observability.yaml"
render_p2_style "$ROOT_DIR/manifests/kubernetes/p2/50-declarative-control-plane.yaml" "$RENDERED_DIR/50-declarative-control-plane.yaml"
render_p2_style "$ROOT_DIR/manifests/kubernetes/s3/70-declarative-adaptationpolicy.yaml" "$RENDERED_DIR/70-declarative-adaptationpolicy.yaml"
python3 "$ROOT_DIR/scripts/render_s3_declarative_manifests.py" \
  --n-robots "$N_ROBOTS" --namespace "$NAMESPACE" --discovery-address "$DISCOVERY_SERVER_ADDRESS" \
  --output-dir "$RENDERED_DIR/declarative"

"${K[@]}" apply -f "$ROOT_DIR/operator/crds/"
# The CRDs must be served before any custom resource is applied: the
# control-plane manifest applied below now carries the namespace's
# ROSLifecyclePolicy (docs/CRD_CONTRACT_AUDIT.md, D8), and no script waited
# for Established before -- a latent race that the later ROSModule applies
# only ever escaped by timing.
"${K[@]}" wait --for=condition=Established --timeout=60s \
  crd/rosmodules.dronekube.io crd/robotfleets.dronekube.io \
  crd/roslifecyclepolicies.dronekube.io crd/adaptationpolicies.dronekube.io
"${K[@]}" create namespace "$NAMESPACE" --dry-run=client -o yaml | "${K[@]}" apply -f -
"${K[@]}" apply -f "$RENDERED_DIR/00-rbac.yaml"
"${K[@]}" apply -f "$RENDERED_DIR/10-discovery.yaml"
"${K[@]}" apply -f "$RENDERED_DIR/50-declarative-control-plane.yaml"
"${K[@]}" apply -f "$RENDERED_DIR/25-observability.yaml"
"${K[@]}" rollout status deployment/p2-fastdds-discovery -n "$NAMESPACE" --timeout=120s
"${K[@]}" rollout status deployment/fleet-operator -n "$NAMESPACE" --timeout=120s
# Same escape hatch as spec.probes.startup, one level deeper: found live
# 2026-09-23 that ROSModuleController's own RollingOut->Stable convergence
# check (120s default, gates a module's very first Deployment too, not
# just later updates) becomes the binding constraint at N=20 once the
# probe widening above stops Pods from being killed before they'd
# otherwise have converged. S3-only; every other scenario's Fleet Operator
# keeps the 120s default untouched.
FLEET_OPERATOR_ENV=("ROSMODULE_UPDATE_TIMEOUT_SEC=${S3_ROSMODULE_UPDATE_TIMEOUT_SEC}"
  "MODULE_MEMORY_LIMIT=${S3_MODULE_MEMORY_LIMIT}"
  "SIDECAR_MEMORY_LIMIT=${S3_SIDECAR_MEMORY_LIMIT}")
# Bug #65 measurement instrument, unset by default so a normal run is
# byte-identical to before: raises the rendered Pods' memory LIMITS only
# (CPU and every timeout untouched) so that a run can measure how much
# memory companion_analytics and state_bridge actually want, instead of
# measuring where they get killed. Not a configuration knob -- see the
# warning on _MODULE_RESOURCES in k8s_workloads.py before setting these
# to anything you then intend to keep.
"${K[@]}" set env deployment/fleet-operator -n "$NAMESPACE" "${FLEET_OPERATOR_ENV[@]}"
"${K[@]}" rollout status deployment/fleet-operator -n "$NAMESPACE" --timeout=120s
"${K[@]}" get deployment fleet-operator -n "$NAMESPACE" -o json >"$RESULT_DIR/effective-operator.json"

BOOTSTRAP_START_NS=$(date +%s%N)
metrics_snapshot "$RESULT_DIR/metrics-before-bootstrap.json"
start_periodic_reimport cloud-native-ros/control-plane:p2 cloud-native-ros/fleet-operator:p2 cloud-native-ros/state-bridge:p2
"${K[@]}" apply -f "$RENDERED_DIR/declarative/40-shared-infra.yaml"
for i in $(seq 1 "$N_ROBOTS"); do
  rid=$(printf "drone%02d" "$i")
  "${K[@]}" rollout status "deployment/$rid-px4-sitl" -n "$NAMESPACE" --timeout=300s
  "${K[@]}" rollout status "deployment/$rid-microxrce-agent" -n "$NAMESPACE" --timeout=300s
done
"${K[@]}" apply -f "$RENDERED_DIR/declarative/70-robotfleet.yaml"
"${K[@]}" apply -f "$RENDERED_DIR/declarative/60-declarative-workload.yaml"
# One fleet-wide snapshot per tick; the same per-Pod guard as the controller.
# Third and last layer of the same finding: found live 2026-09-23 that with
# both probe timing (spec.probes.startup) and controller convergence
# (ROSMODULE_UPDATE_TIMEOUT_SEC) already loosened, N=20 still failed this
# barrier's own 180s budget with zero Pod restarts -- genuinely still
# starting, not stuck or crash-looping, just needing more wall-clock time
# to actually observe. A rough estimate, not yet a measured real N=20
# convergence time; this run is that measurement.
timeout "$((S3_BOOTSTRAP_BARRIER_TIMEOUT_SEC + 50))s" "${K[@]}" exec -n "$NAMESPACE" deployment/fleet-operator -- \
  python -m fleet_operator.s3_bootstrap_check --namespace "$NAMESPACE" \
  --robots "$N_ROBOTS" --timeout "$S3_BOOTSTRAP_BARRIER_TIMEOUT_SEC" --stable-seconds 15 \
  | tee "$RESULT_DIR/bootstrap-readiness.jsonl"
stop_periodic_reimport
BOOTSTRAP_END_NS=$(date +%s%N)
metrics_snapshot "$RESULT_DIR/metrics-after-bootstrap.json"
BOOTSTRAP_MS=$(( (BOOTSTRAP_END_NS - BOOTSTRAP_START_NS) / 1000000 ))

"${K[@]}" apply -f "$RENDERED_DIR/70-declarative-adaptationpolicy.yaml"

run_incident

"${K[@]}" get pods -n "$NAMESPACE" -o wide >"$RESULT_DIR/pods.txt"
"${K[@]}" logs -n "$NAMESPACE" deployment/fleet-operator >"$RESULT_DIR/fleet-operator.log" 2>&1 || true

fi

python3 "$ROOT_DIR/scripts/s3_metrics.py" diff \
  --before "$RESULT_DIR/metrics-before-bootstrap.json" --after "$RESULT_DIR/metrics-after-bootstrap.json" \
  --out "$RESULT_DIR/metrics-diff-bootstrap.json"
python3 "$ROOT_DIR/scripts/s3_metrics.py" diff \
  --before "$RESULT_DIR/metrics-before-incident.json" --after "$RESULT_DIR/metrics-after-incident.json" \
  --out "$RESULT_DIR/metrics-diff-incident.json"

BOOTSTRAP_API_CALLS=$(python3 -c "import json; print(json.load(open('$RESULT_DIR/metrics-diff-bootstrap.json'))['apiserver_request_total_total_delta'])")
# "scritture etcd" (proposal) means writes specifically, not every
# storage-layer request -- found live, 2026-09-22, that this used to read
# etcd_requests_total_total_delta (reads+writes summed together, mislabeled
# as writes in every S3 result collected before this fix). See s3_metrics.py.
BOOTSTRAP_STORE_WRITES=$(python3 -c "import json; print(json.load(open('$RESULT_DIR/metrics-diff-bootstrap.json'))['etcd_requests_write_total_delta'])")
INCIDENT_API_CALLS=$(python3 -c "import json; print(json.load(open('$RESULT_DIR/metrics-diff-incident.json'))['apiserver_request_total_total_delta'])")
INCIDENT_STORE_WRITES=$(python3 -c "import json; print(json.load(open('$RESULT_DIR/metrics-diff-incident.json'))['etcd_requests_write_total_delta'])")

# Proposal's own KPI table asks for "carico sull'API server (chiamate/s,
# scritture etcd)" -- a rate, not a raw total. Raw totals alone conflate
# "more calls because the window ran longer" with "more load per unit
# time", which matters a lot here since bootstrap duration itself grows
# hugely with N (seconds at N=3, tens of minutes at N=20). Both are kept
# in the report: totals for reference/debugging, rates for the actual
# comparison-with-N the proposal asks for.
metric_value() {
  python3 -c 'import json, sys; print(format(json.load(open(sys.argv[1]))[sys.argv[2]], ".2f"))' "$1" "$2"
}
BOOTSTRAP_API_RATE=$(metric_value "$RESULT_DIR/metrics-diff-bootstrap.json" apiserver_requests_per_sec)
BOOTSTRAP_STORE_RATE=$(metric_value "$RESULT_DIR/metrics-diff-bootstrap.json" storage_writes_per_sec)
INCIDENT_API_RATE=$(metric_value "$RESULT_DIR/metrics-diff-incident.json" apiserver_requests_per_sec)
INCIDENT_STORE_RATE=$(metric_value "$RESULT_DIR/metrics-diff-incident.json" storage_writes_per_sec)
BOOTSTRAP_METRICS_SEC=$(metric_value "$RESULT_DIR/metrics-diff-bootstrap.json" elapsed_sec)
INCIDENT_METRICS_SEC=$(metric_value "$RESULT_DIR/metrics-diff-incident.json" elapsed_sec)
python3 "$ROOT_DIR/scripts/s3_kpi_boundaries.py" \
  "$RESULT_DIR/timing-boundaries.json" "$VARIANT" \
  "$BOOTSTRAP_START_NS" "$BOOTSTRAP_END_NS" "$INCIDENT_START_NS" "$INCIDENT_END_NS"

cat >"$RESULT_DIR/REPORT.md" <<EOF
# S3 Scalability Sweep -- N=$N_ROBOTS, variante $VARIANT

| Campo | Valore |
| --- | --- |
| Esito | $PASS |
| N robot | $N_ROBOTS |
| Variante | $VARIANT |
| Tempo di bootstrap (N robot -> tutti Active) | ${BOOTSTRAP_MS} ms |
| Chiamate API server durante il bootstrap (totale / tasso) | $BOOTSTRAP_API_CALLS / ${BOOTSTRAP_API_RATE} chiamate/s |
| Scritture storage durante il bootstrap (totale / tasso) | $BOOTSTRAP_STORE_WRITES / ${BOOTSTRAP_STORE_RATE} scritture/s |
| Finestra dei contatori di bootstrap | ${BOOTSTRAP_METRICS_SEC} s |
| Tempo comando -> esito osservato (drone01, SLO) | ${INCIDENT_MS} ms |
| Finestra osservata senza recovery | $([[ "$PASS" == true ]] && echo no || echo si) |
| Chiamate API server durante l'incidente (totale / tasso) | $INCIDENT_API_CALLS / ${INCIDENT_API_RATE} chiamate/s |
| Scritture storage durante l'incidente (totale / tasso) | $INCIDENT_STORE_WRITES / ${INCIDENT_STORE_RATE} scritture/s |
| Finestra dei contatori dell'incidente | ${INCIDENT_METRICS_SEC} s |
| Costo del control plane per incidente (proposal) | ${INCIDENT_API_RATE} chiamate/s, ${INCIDENT_STORE_RATE} scritture/s |
| Esito incidente | $OUTCOME |
| Time-to-rebuild da cluster vuoto | Non misurato: il timer parte dopo la creazione del cluster |
| Reaction dal primo SLO violato | Non misurata separatamente |

Protocollo harness: $HARNESS_PROTOCOL. In B il bootstrap richiede tutti i
companion Active con osservazioni fresche della generazione corrente,
Pod Ready e rollout Stable, piu' i Deployment PX4/agent pronti. La finestra
di stabilita' di 15s e' inclusa nel tempo di bootstrap (budget globale ${S3_BOOTSTRAP_BARRIER_TIMEOUT_SEC}s,
non piu' attese sequenziali per ogni drone). Le misure storiche basate sul
solo campo lifecycle flat non sono direttamente equivalenti.

Budget convergenza controller B: ${S3_ROSMODULE_UPDATE_TIMEOUT_SEC}s.
Limiti memoria B (modulo/sidecar): $S3_MODULE_MEMORY_LIMIT / $S3_SIDECAR_MEMORY_LIMIT.
Sorgenti: provenance/source-manifest.json e source-snapshot.tar.gz;
immagini locali: local-image-ids.txt; immagini osservate: runtime-pods.json.
L'inventario precedente all'incidente e' in pods-before-incident.json.
Gli estremi grezzi dei cronometri e la loro definizione sono in
timing-boundaries.json.
Il verdetto di provenienza separato e' in provenance-verdict.json: un PASS
funzionale non vale come cella valida se immagini o input non coincidono.

Un solo incidente SLO su drone01, con gli altri N-1
robot nominali, per misurare tempo end-to-end e carico sul control plane
sotto un numero crescente di oggetti gestiti -- stesso meccanismo gia'
validato in P2 (variante A) e S4 (variante B), qui ripetuto a scala diversa.
Il tasso (chiamate/s, scritture/s), non solo il totale, e' la metrica che
il proposal chiede esplicitamente per il confronto al crescere di N
(totale grezzo e durata crescono insieme, il tasso isola il carico per
unita' di tempo). Lo stesso tasso durante l'incidente e' anche il
"Costo del control plane" del proposal (altra metrica differenziante mai
etichettata esplicitamente prima d'ora): non serve un nuovo scenario, i
dati sono gli stessi, va solo riconosciuto come tale nel confronto A/B.

Il tempo osservato parte subito prima della modifica via ROS parameter
service dello stesso modulo analytics gia' attivo, in entrambe le varianti.
Termina al rilevamento dell'esito terminale o alla scadenza. Se la prova non
passa, non e' un tempo di recovery. Non misura separatamente la reazione dal
primo SLO violato o dall'evento rilevato: per isolare la reazione servono
timestamp correlati di violazione, rilevamento e recovery.
I tassi usano invece l'intervallo effettivo tra gli
snapshot dei contatori, riportato sopra, non questo timer end-to-end.
Le scritture contate sono richieste al layer storage dell'API server,
non scritture fisiche su disco; k3s usa qui SQLite/Kine, non un cluster etcd.
EOF

echo "S3 result (N=$N_ROBOTS, variant $VARIANT): $PASS"
echo "Evidence: $RESULT_DIR"
[[ "$PASS" == "true" ]]
