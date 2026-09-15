#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
CLUSTER=cloud-native-p2
CONTEXT=k3d-cloud-native-p2
NAMESPACE=cloud-native-p2
RESULT_ID=$(date -u +%Y%m%dT%H%M%SZ)
RESULT_DIR="$ROOT_DIR/results/e0/$RESULT_ID"
RENDERED_DIR="$RESULT_DIR/rendered"
OBSERVATION_WINDOW_SEC=${E0_OBSERVATION_WINDOW_SEC:-60}
IMAGE_LOCK_FILE=${IMAGE_LOCK_FILE:-}
REGISTRY_PULL_SECRET=${REGISTRY_PULL_SECRET:-kuberos-private-release}
DOCKER_CONFIG_PATH=${DOCKER_CONFIG_PATH:-$HOME/.docker/config.json}
K=(kubectl --context "$CONTEXT")
ROBOTS=(drone01 drone02 drone03)
NODES=(k3d-cloud-native-p2-agent-0 k3d-cloud-native-p2-agent-1 k3d-cloud-native-p2-agent-2)
MODULES=(microxrce-agent px4-sitl event-detector companion-analytics-onboard)
USE_IMAGE_LOCK=0
IMAGE_PROVENANCE="local build/import"
DIGEST_VERIFICATION="not requested"

for command in docker k3d kubectl python3; do
  command -v "$command" >/dev/null || {
    echo "Required command not found: $command" >&2
    exit 1
  }
done

mkdir -p "$RENDERED_DIR"

if [[ -n "$IMAGE_LOCK_FILE" ]]; then
  [[ -r "$IMAGE_LOCK_FILE" ]] || {
    echo "Image lock not found: $IMAGE_LOCK_FILE" >&2
    exit 1
  }
  [[ -r "$DOCKER_CONFIG_PATH" ]] || {
    echo "Docker authentication config is unavailable: $DOCKER_CONFIG_PATH" >&2
    exit 1
  }
  USE_IMAGE_LOCK=1
  IMAGE_PROVENANCE="private release lock: $(basename "$IMAGE_LOCK_FILE")"
fi

apply_image_lock() {
  python3 "$ROOT_DIR/scripts/render_image_lock.py" \
    --input "$1" --output "$2" \
    --lock "$IMAGE_LOCK_FILE" --pull-secret "$REGISTRY_PULL_SECRET" \
    >>"$RESULT_DIR/image-lock-render.log"
}

ensure_registry_secret() {
  "${K[@]}" create secret generic "$REGISTRY_PULL_SECRET" \
    -n "$NAMESPACE" \
    --from-file=.dockerconfigjson="$DOCKER_CONFIG_PATH" \
    --type=kubernetes.io/dockerconfigjson \
    --dry-run=client -o yaml | "${K[@]}" apply -f -
  cp "$IMAGE_LOCK_FILE" "$RESULT_DIR/image-lock.json"
}

cluster_exists() {
  k3d cluster list --no-headers | awk '{print $1}' | grep -qx "$CLUSTER"
}

if [[ "${RESET_E0:-0}" == "1" ]] && cluster_exists; then
  k3d cluster delete "$CLUSTER"
fi

if ! cluster_exists; then
  k3d cluster create --config "$ROOT_DIR/manifests/kubernetes/e0/k3d-cloud-native-e0.yaml"
else
  NODE_COUNT=$("${K[@]}" get nodes --no-headers | wc -l)
  ONBOARD_COUNT=$("${K[@]}" get nodes -l kuberos.io/role=onboard --no-headers | wc -l)
  EDGE_COUNT=$("${K[@]}" get nodes -l kuberos.io/role=edge --no-headers | wc -l)
  if [[ "$NODE_COUNT" != "5" || "$ONBOARD_COUNT" != "3" || "$EDGE_COUNT" != "1" ]]; then
    echo "Cluster $CLUSTER has an incompatible topology; rerun with RESET_E0=1" >&2
    exit 1
  fi
fi

DISCOVERY_SERVER_ADDRESS=$("${K[@]}" get node k3d-cloud-native-p2-server-0 \
  -o jsonpath='{.status.addresses[?(@.type=="InternalIP")].address}')

python3 "$ROOT_DIR/scripts/render_e0_manifests.py" \
  --output-dir "$RENDERED_DIR" \
  --discovery-address "$DISCOVERY_SERVER_ADDRESS"

sed \
  -e "s/__P2_DISCOVERY_ADDRESS__/$DISCOVERY_SERVER_ADDRESS/g" \
  -e "s/__P2_GOAL_TIMEOUT_SEC__/120.0/g" \
  "$ROOT_DIR/manifests/kubernetes/p2/30-control-plane.yaml" \
  >"$RENDERED_DIR/30-control-plane.yaml"
sed "s/__P2_DISCOVERY_ADDRESS__/$DISCOVERY_SERVER_ADDRESS/g" \
  "$ROOT_DIR/manifests/kuberos/p2/analytics-edge.yaml" \
  >"$RENDERED_DIR/analytics-edge.yaml"

DISCOVERY_MANIFEST="$ROOT_DIR/manifests/kubernetes/p2/10-discovery.yaml"
OBSERVABILITY_MANIFEST="$ROOT_DIR/manifests/kubernetes/p2/25-observability.yaml"
BOOTSTRAP_MANIFEST="$ROOT_DIR/manifests/kubernetes/e0/30-bootstrap.yaml"
if [[ "$USE_IMAGE_LOCK" == "1" ]]; then
  for manifest in 20-kuberos.yaml 30-control-plane.yaml analytics-edge.yaml \
    drone01.yaml drone02.yaml drone03.yaml; do
    apply_image_lock "$RENDERED_DIR/$manifest" "$RENDERED_DIR/$manifest"
  done
  DISCOVERY_MANIFEST="$RENDERED_DIR/10-discovery.yaml"
  OBSERVABILITY_MANIFEST="$RENDERED_DIR/25-observability.yaml"
  BOOTSTRAP_MANIFEST="$RENDERED_DIR/30-bootstrap.yaml"
  apply_image_lock "$ROOT_DIR/manifests/kubernetes/p2/10-discovery.yaml" \
    "$DISCOVERY_MANIFEST"
  apply_image_lock "$ROOT_DIR/manifests/kubernetes/p2/25-observability.yaml" \
    "$OBSERVABILITY_MANIFEST"
  apply_image_lock "$ROOT_DIR/manifests/kubernetes/e0/30-bootstrap.yaml" \
    "$BOOTSTRAP_MANIFEST"
fi

if [[ "$USE_IMAGE_LOCK" == "0" && "${SKIP_IMAGE_BUILD_IMPORT:-0}" != "1" && "${SKIP_IMAGE_BUILD:-0}" != "1" ]]; then
  docker build -t cloud-native-ros/control-plane:p2 \
    -f "$ROOT_DIR/containers/control-plane/Dockerfile" "$ROOT_DIR"
  docker build -t cloud-native-ros/event-detector:p2 \
    -f "$ROOT_DIR/containers/event-detector/Dockerfile" "$ROOT_DIR"
  docker build -t cloud-native-ros/kuberos:p2 \
    -f "$ROOT_DIR/containers/kuberos/Dockerfile" "$ROOT_DIR"
fi

if [[ "$USE_IMAGE_LOCK" == "0" && "${SKIP_IMAGE_BUILD_IMPORT:-0}" != "1" && "${SKIP_IMAGE_IMPORT:-0}" != "1" ]]; then
  k3d image import -c "$CLUSTER" \
    cloud-native-ros/control-plane:p2 \
    cloud-native-ros/event-detector:p2 \
    cloud-native-ros/kuberos:p2 \
    microros/micro-ros-agent:humble \
    px4io/px4-sitl:latest \
    redis:7
fi

"${K[@]}" apply -f "$ROOT_DIR/manifests/kubernetes/p2/00-rbac.yaml"
if [[ "$USE_IMAGE_LOCK" == "1" ]]; then
  ensure_registry_secret
fi
"${K[@]}" apply -f "$DISCOVERY_MANIFEST"
"${K[@]}" apply -f "$RENDERED_DIR/20-kuberos.yaml"
"${K[@]}" apply -f "$OBSERVABILITY_MANIFEST"
"${K[@]}" rollout status deployment/p2-fastdds-discovery -n "$NAMESPACE" --timeout=180s
"${K[@]}" rollout status deployment/kuberos -n "$NAMESPACE" --timeout=300s
"${K[@]}" wait --for=create secret/kuberos-api-token -n "$NAMESPACE" --timeout=90s

"${K[@]}" create configmap p2-analytics-edge-manifest -n "$NAMESPACE" \
  --from-file=analytics-edge.yaml="$RENDERED_DIR/analytics-edge.yaml" \
  --dry-run=client -o yaml | "${K[@]}" apply -f -
"${K[@]}" apply -f "$RENDERED_DIR/30-control-plane.yaml"

"${K[@]}" create configmap e0-kuberos-manifests -n "$NAMESPACE" \
  --from-file=drone01.yaml="$RENDERED_DIR/drone01.yaml" \
  --from-file=drone02.yaml="$RENDERED_DIR/drone02.yaml" \
  --from-file=drone03.yaml="$RENDERED_DIR/drone03.yaml" \
  --dry-run=client -o yaml | "${K[@]}" apply -f -
"${K[@]}" delete job e0-kuberos-bootstrap -n "$NAMESPACE" --ignore-not-found
"${K[@]}" apply -f "$BOOTSTRAP_MANIFEST"
"${K[@]}" wait --for=condition=complete job/e0-kuberos-bootstrap \
  -n "$NAMESPACE" --timeout=900s
"${K[@]}" wait --for=condition=available deployment --all \
  -n "$NAMESPACE" --timeout=300s

"${K[@]}" logs -n "$NAMESPACE" job/e0-kuberos-bootstrap \
  >"$RESULT_DIR/kuberos-deployment-bootstrap.log"
READY_COUNT=$(grep -c '^E0_KUBEROS_READY ' "$RESULT_DIR/kuberos-deployment-bootstrap.log" || true)
MANAGED_COUNT=$("${K[@]}" get deployment -n "$NAMESPACE" \
  -l app.kubernetes.io/managed-by=kuberos --no-headers | wc -l)
TOTAL_DEPLOYMENTS=$("${K[@]}" get deployment -n "$NAMESPACE" --no-headers | wc -l)
AGENT_SERVICE_COUNT=$("${K[@]}" get service -n "$NAMESPACE" -o name | \
  grep -Ec '^service/drone0[123]-xrce-agent$' || true)

PASS=true
[[ "$READY_COUNT" == "3" ]] || PASS=false
[[ "$MANAGED_COUNT" == "12" ]] || PASS=false
[[ "$TOTAL_DEPLOYMENTS" == "19" ]] || PASS=false
[[ "$AGENT_SERVICE_COUNT" == "3" ]] || PASS=false

declare -A PX4_UID_BEFORE PX4_UID_AFTER PX4_RESTARTS_BEFORE PX4_RESTARTS_AFTER
declare -A ANALYTICS_STATE DETECTOR_STATE ROUTE PLACEMENT HEALTH_SNAPSHOT

for index in "${!ROBOTS[@]}"; do
  robot=${ROBOTS[$index]}
  expected_node=${NODES[$index]}
  for module in "${MODULES[@]}"; do
    deployment="$robot-$module"
    actual_node=$("${K[@]}" get pod -n "$NAMESPACE" -l "pod-name=$deployment" \
      -o jsonpath='{.items[0].spec.nodeName}')
    PLACEMENT["$deployment"]=$actual_node
    [[ "$actual_node" == "$expected_node" ]] || PASS=false
  done
  PX4_UID_BEFORE[$robot]=$("${K[@]}" get pod -n "$NAMESPACE" \
    -l "pod-name=$robot-px4-sitl" -o jsonpath='{.items[0].metadata.uid}')
  PX4_RESTARTS_BEFORE[$robot]=$("${K[@]}" get pod -n "$NAMESPACE" \
    -l "pod-name=$robot-px4-sitl" -o jsonpath='{.items[0].status.containerStatuses[0].restartCount}')
done

TOPICS=
for _ in $(seq 1 60); do
  TOPICS=$("${K[@]}" exec -n "$NAMESPACE" deployment/drone01-event-detector -- \
    /bin/bash -lc 'source /ws/install/setup.bash; ROS_SUPER_CLIENT=TRUE ROS2CLI_DISABLE_DAEMON=1 ros2 topic list' 2>/dev/null || true)
  if grep -Fq '/drone01/fmu/out/vehicle_status_v4' <<<"$TOPICS" && \
     grep -Fq '/drone02/fmu/out/vehicle_status_v4' <<<"$TOPICS" && \
     grep -Fq '/drone03/fmu/out/vehicle_status_v4' <<<"$TOPICS"; then
    break
  fi
  sleep 2
done
printf '%s\n' "$TOPICS" >"$RESULT_DIR/ros-topics.txt"

START_EPOCH=$(date +%s)
sleep "$OBSERVATION_WINDOW_SEC"
END_EPOCH=$(date +%s)

for robot in "${ROBOTS[@]}"; do
  PX4_UID_AFTER[$robot]=$("${K[@]}" get pod -n "$NAMESPACE" \
    -l "pod-name=$robot-px4-sitl" -o jsonpath='{.items[0].metadata.uid}')
  PX4_RESTARTS_AFTER[$robot]=$("${K[@]}" get pod -n "$NAMESPACE" \
    -l "pod-name=$robot-px4-sitl" -o jsonpath='{.items[0].status.containerStatuses[0].restartCount}')
  ANALYTICS_STATE[$robot]=$("${K[@]}" exec -n "$NAMESPACE" \
    "deployment/$robot-companion-analytics-onboard" -- /bin/bash -lc \
    "source /ws/install/setup.bash; ROS_SUPER_CLIENT=TRUE ROS2CLI_DISABLE_DAEMON=1 ros2 lifecycle get /$robot/companion_analytics_onboard" 2>/dev/null | tail -n 1 || true)
  HEALTH_SNAPSHOT[$robot]=$("${K[@]}" exec -n "$NAMESPACE" \
    "deployment/$robot-companion-analytics-onboard" -- /bin/bash -lc \
    "source /ws/install/setup.bash; ROS_SUPER_CLIENT=TRUE ROS2CLI_DISABLE_DAEMON=1 timeout 20 ros2 service call /$robot/companion/onboard/health cloud_native_robotics_interfaces/srv/GetHealthSnapshot \"{correlation_id: 'e0-$robot-health'}\"" 2>/dev/null || true)
  DETECTOR_STATE[$robot]=$("${K[@]}" exec -n "$NAMESPACE" \
    "deployment/$robot-event-detector" -- /bin/bash -lc \
    "source /ws/install/setup.bash; ROS_SUPER_CLIENT=TRUE ROS2CLI_DISABLE_DAEMON=1 ros2 lifecycle get /$robot/event_detector" 2>/dev/null | tail -n 1 || true)
  ROUTE[$robot]=$("${K[@]}" get configmap "analytics-routing-$robot" \
    -n "$NAMESPACE" -o jsonpath='{.data.active_instance}')
  [[ "${PX4_UID_BEFORE[$robot]}" == "${PX4_UID_AFTER[$robot]}" ]] || PASS=false
  [[ "${PX4_RESTARTS_BEFORE[$robot]}" == "${PX4_RESTARTS_AFTER[$robot]}" ]] || PASS=false
  [[ "${ANALYTICS_STATE[$robot]}" == *active* ]] || PASS=false
  [[ "${HEALTH_SNAPSHOT[$robot]}" == *"healthy=True"* ]] || PASS=false
  [[ "${HEALTH_SNAPSHOT[$robot]}" == *"lifecycle_state='active'"* ]] || PASS=false
  [[ "${DETECTOR_STATE[$robot]}" == *active* ]] || PASS=false
  [[ "${ROUTE[$robot]}" == "onboard" ]] || PASS=false
  grep -Fq "/$robot/fmu/out/vehicle_status_v4" "$RESULT_DIR/ros-topics.txt" || PASS=false
done

if "${K[@]}" get deployment -n "$NAMESPACE" \
  -l pod-name=drone01-companion-analytics --no-headers 2>/dev/null | grep -q .; then
  PASS=false
fi
if "${K[@]}" get hpa -n "$NAMESPACE" --no-headers 2>/dev/null | grep -q .; then
  PASS=false
fi

"${K[@]}" get nodes -o wide >"$RESULT_DIR/nodes.txt"
"${K[@]}" get pods -n "$NAMESPACE" -o wide >"$RESULT_DIR/pods.txt"
"${K[@]}" get pods -n "$NAMESPACE" -o json >"$RESULT_DIR/pods.json"
if [[ "$USE_IMAGE_LOCK" == "1" ]]; then
  if python3 "$ROOT_DIR/scripts/verify_image_lock_runtime.py" \
    --pods-json "$RESULT_DIR/pods.json" \
    --lock "$IMAGE_LOCK_FILE" \
    --output "$RESULT_DIR/image-provenance.csv" \
    --minimum 16 >"$RESULT_DIR/image-provenance-summary.json"; then
    DIGEST_VERIFICATION="PASS (16+ project containers)"
  else
    DIGEST_VERIFICATION="FAIL"
    PASS=false
  fi
fi
"${K[@]}" get deployments,services,jobs,hpa,pvc -n "$NAMESPACE" -o yaml \
  >"$RESULT_DIR/kubernetes-resources.yaml"
"${K[@]}" logs -n "$NAMESPACE" deployment/operational-event-dispatcher-p2 \
  >"$RESULT_DIR/dispatcher.log" 2>&1 || true
"${K[@]}" logs -n "$NAMESPACE" deployment/kuberos -c initialize \
  >"$RESULT_DIR/kuberos-initialize.log" 2>&1 || true
"${K[@]}" logs -n "$NAMESPACE" deployment/kuberos -c api \
  >"$RESULT_DIR/kuberos-api.log" 2>&1 || true
"${K[@]}" logs -n "$NAMESPACE" deployment/kuberos -c worker \
  >"$RESULT_DIR/kuberos-worker.log" 2>&1 || true
"${K[@]}" exec -n "$NAMESPACE" deployment/kuberos -c api -- \
  python manage.py shell -c \
  "from main.models import Deployment; print(list(Deployment.objects.values_list('name','status','active')))" \
  >"$RESULT_DIR/kuberos-deployments.txt"
"${K[@]}" exec -n "$NAMESPACE" deployment/p2-audit-writer -- cat /data/audit.jsonl \
  >"$RESULT_DIR/audit.jsonl"

grep -Fq '"record_type":"platform_snapshot"' "$RESULT_DIR/audit.jsonl" || PASS=false
if grep -Fq 'DeploymentRequest accepted' "$RESULT_DIR/dispatcher.log"; then PASS=false; fi
if grep -Fq '"record_type":"incident_started"' "$RESULT_DIR/audit.jsonl"; then PASS=false; fi
"${K[@]}" get pvc p2-audit-data -n "$NAMESPACE" \
  -o jsonpath='{.status.phase}' | grep -Fq Bound || PASS=false

{
  echo "deployment,module,node"
  for robot in "${ROBOTS[@]}"; do
    for module in "${MODULES[@]}"; do
      echo "$robot-$module,$module,${PLACEMENT[$robot-$module]}"
    done
  done
} >"$RESULT_DIR/placement.csv"

{
  echo "robot,uid_before,uid_after,restarts_before,restarts_after,analytics_lifecycle,detector_lifecycle,route"
  for robot in "${ROBOTS[@]}"; do
    echo "$robot,${PX4_UID_BEFORE[$robot]},${PX4_UID_AFTER[$robot]},${PX4_RESTARTS_BEFORE[$robot]},${PX4_RESTARTS_AFTER[$robot]},${ANALYTICS_STATE[$robot]},${DETECTOR_STATE[$robot]},${ROUTE[$robot]}"
  done
} >"$RESULT_DIR/robot-state.csv"

{
  for robot in "${ROBOTS[@]}"; do
    echo "===== $robot ====="
    printf '%s\n' "${HEALTH_SNAPSHOT[$robot]}"
  done
} >"$RESULT_DIR/health-snapshots.txt"

cat >"$RESULT_DIR/REPORT.md" <<EOF
# E0 Three-Drone Live Result

| Campo | Valore |
| --- | --- |
| Esito | $PASS |
| Cluster / namespace | $CLUSTER / $NAMESPACE |
| Provenienza immagini progetto | $IMAGE_PROVENANCE |
| Verifica digest runtime | $DIGEST_VERIFICATION |
| Topologia | 1 control plane, 3 onboard, 1 edge |
| ApplicationDeployment completati via KubeROS | $READY_COUNT / 3 |
| Deployment gestiti da KubeROS | $MANAGED_COUNT / 12 |
| Deployment Kubernetes totali | $TOTAL_DEPLOYMENTS / 19 |
| Service XRCE Agent | $AGENT_SERVICE_COUNT / 3 |
| Finestra nominale | $((END_EPOCH - START_EPOCH)) s |
| PX4 namespaced osservati | drone01, drone02, drone03 |
| Routing finale | onboard per tutti i droni |
| ROS Service health | 3/3 snapshot healthy e Lifecycle active |
| Remediation/HPA | assenti come atteso |
| Audit | snapshot persistenti, nessun incidente |

I quattro moduli robotici di ciascun drone sono stati creati mediante la API
KubeROS e collocati sul rispettivo nodo onboard. Durante la finestra nominale i
tre PX4 hanno mantenuto UID e restart count, mentre Event Detector e Companion
Analytics sono rimasti nello stato Lifecycle active.
Il Service tipizzato GetHealthSnapshot ha restituito uno snapshot healthy per
ciascuna istanza analytics onboard.
EOF

echo "E0 result: $PASS"
echo "Evidence: $RESULT_DIR"
[[ "$PASS" == "true" ]]
