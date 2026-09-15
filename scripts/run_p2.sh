#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
CLUSTER=cloud-native-p2
CONTEXT=k3d-cloud-native-p2
NAMESPACE=cloud-native-p2
EXPERIMENT_MODE=${EXPERIMENT_MODE:-p2}
RESULT_ID=$(date -u +%Y%m%dT%H%M%SZ)
IMAGE_LOCK_FILE=${IMAGE_LOCK_FILE:-}
REGISTRY_PULL_SECRET=${REGISTRY_PULL_SECRET:-kuberos-private-release}
DOCKER_CONFIG_PATH=${DOCKER_CONFIG_PATH:-$HOME/.docker/config.json}
K=(kubectl --context "$CONTEXT")
USE_IMAGE_LOCK=0
IMAGE_PROVENANCE="local build/import"
DIGEST_VERIFICATION="not requested"

case "$EXPERIMENT_MODE" in
  p2)
    RESULT_GROUP=p2
    GOAL_TIMEOUT_SEC=120.0
    ANALYTICS_MANIFEST="$ROOT_DIR/manifests/kuberos/p2/analytics-edge.yaml"
    ONBOARD_PROCESSING_DELAY_MS=300.0
    OUTCOME_PATTERN='completed as analytics_slo_recovered (STABLE)'
    MINIMUM_PROJECT_CONTAINERS=12
    ;;
  e4)
    RESULT_GROUP=e4
    GOAL_TIMEOUT_SEC=45.0
    ANALYTICS_MANIFEST="$ROOT_DIR/manifests/kuberos/e4/analytics-edge-readiness-failure.yaml"
    ONBOARD_PROCESSING_DELAY_MS=300.0
    OUTCOME_PATTERN='completed as analytics_migration_failed (ROLLED_BACK)'
    MINIMUM_PROJECT_CONTAINERS=11
    ;;
  *)
    echo "Unsupported EXPERIMENT_MODE: $EXPERIMENT_MODE" >&2
    exit 2
    ;;
esac

RESULT_DIR="$ROOT_DIR/results/$RESULT_GROUP/$RESULT_ID"

for command in docker k3d kubectl python3; do
  command -v "$command" >/dev/null || {
    echo "Required command not found: $command" >&2
    exit 1
  }
done

mkdir -p "$RESULT_DIR"

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

if [[ "${RESET_P2:-0}" == "1" ]] && k3d cluster list --no-headers | awk '{print $1}' | grep -qx "$CLUSTER"; then
  k3d cluster delete "$CLUSTER"
fi

if ! k3d cluster list --no-headers | awk '{print $1}' | grep -qx "$CLUSTER"; then
  k3d cluster create --config \
    "$ROOT_DIR/manifests/kubernetes/p2/k3d-cloud-native-p2.yaml"
fi

DISCOVERY_SERVER_ADDRESS=$("${K[@]}" get node \
  k3d-cloud-native-p2-server-0 -o jsonpath='{.status.addresses[?(@.type=="InternalIP")].address}')
RENDERED_DIR="$RESULT_DIR/rendered"
mkdir -p "$RENDERED_DIR"

render_manifest() {
  sed \
    -e "s/__P2_DISCOVERY_ADDRESS__/$DISCOVERY_SERVER_ADDRESS/g" \
    -e "s/__P2_GOAL_TIMEOUT_SEC__/$GOAL_TIMEOUT_SEC/g" \
    -e "s/__ONBOARD_PROCESSING_DELAY_MS__/$ONBOARD_PROCESSING_DELAY_MS/g" \
    "$1" >"$2"
}

render_manifest "$ROOT_DIR/manifests/kubernetes/p2/30-control-plane.yaml" "$RENDERED_DIR/30-control-plane.yaml"
render_manifest "$ROOT_DIR/manifests/kubernetes/p2/40-workload.yaml" "$RENDERED_DIR/40-workload.yaml"
render_manifest "$ANALYTICS_MANIFEST" "$RENDERED_DIR/analytics-edge.yaml"

DISCOVERY_MANIFEST="$ROOT_DIR/manifests/kubernetes/p2/10-discovery.yaml"
KUBEROS_MANIFEST="$ROOT_DIR/manifests/kubernetes/p2/20-kuberos.yaml"
OBSERVABILITY_MANIFEST="$ROOT_DIR/manifests/kubernetes/p2/25-observability.yaml"
if [[ "$USE_IMAGE_LOCK" == "1" ]]; then
  for manifest in 30-control-plane.yaml 40-workload.yaml analytics-edge.yaml; do
    apply_image_lock "$RENDERED_DIR/$manifest" "$RENDERED_DIR/$manifest"
  done
  DISCOVERY_MANIFEST="$RENDERED_DIR/10-discovery.yaml"
  KUBEROS_MANIFEST="$RENDERED_DIR/20-kuberos.yaml"
  OBSERVABILITY_MANIFEST="$RENDERED_DIR/25-observability.yaml"
  apply_image_lock "$ROOT_DIR/manifests/kubernetes/p2/10-discovery.yaml" \
    "$DISCOVERY_MANIFEST"
  apply_image_lock "$ROOT_DIR/manifests/kubernetes/p2/20-kuberos.yaml" \
    "$KUBEROS_MANIFEST"
  apply_image_lock "$ROOT_DIR/manifests/kubernetes/p2/25-observability.yaml" \
    "$OBSERVABILITY_MANIFEST"
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
"${K[@]}" apply -f "$KUBEROS_MANIFEST"
"${K[@]}" apply -f "$OBSERVABILITY_MANIFEST"
"${K[@]}" rollout status deployment/p2-fastdds-discovery \
  -n "$NAMESPACE" --timeout=120s
"${K[@]}" rollout status deployment/kuberos \
  -n "$NAMESPACE" --timeout=240s
"${K[@]}" rollout status deployment/p2-audit-writer \
  -n "$NAMESPACE" --timeout=120s
"${K[@]}" rollout status deployment/p2-operator-notifier \
  -n "$NAMESPACE" --timeout=120s
"${K[@]}" rollout status deployment/p2-platform-observer \
  -n "$NAMESPACE" --timeout=120s
"${K[@]}" wait --for=create secret/kuberos-api-token \
  -n "$NAMESPACE" --timeout=60s

"${K[@]}" create configmap p2-analytics-edge-manifest \
  -n "$NAMESPACE" \
  --from-file=analytics-edge.yaml="$RENDERED_DIR/analytics-edge.yaml" \
  --dry-run=client -o yaml | "${K[@]}" apply -f -
"${K[@]}" apply -f "$RENDERED_DIR/30-control-plane.yaml"
"${K[@]}" rollout status deployment/application-manager-p2 \
  -n "$NAMESPACE" --timeout=120s
"${K[@]}" rollout status deployment/operational-event-dispatcher-p2 \
  -n "$NAMESPACE" --timeout=120s

START_EPOCH=$(date +%s)
"${K[@]}" apply -f "$RENDERED_DIR/40-workload.yaml"
"${K[@]}" rollout status deployment/drone01-px4-sitl \
  -n "$NAMESPACE" --timeout=120s
"${K[@]}" wait --for=condition=available deployment --all \
  -n "$NAMESPACE" --timeout=120s

PX4_UID_BEFORE=$("${K[@]}" get pod -n "$NAMESPACE" \
  -l app.kubernetes.io/name=drone01-px4-sitl \
  -o jsonpath='{.items[0].metadata.uid}')
PX4_RESTARTS_BEFORE=$("${K[@]}" get pod -n "$NAMESPACE" \
  -l app.kubernetes.io/name=drone01-px4-sitl \
  -o jsonpath='{.items[0].status.containerStatuses[0].restartCount}')

for _ in $(seq 1 180); do
  if "${K[@]}" logs -n "$NAMESPACE" deployment/operational-event-dispatcher-p2 \
    --tail=200 2>/dev/null | grep -Fq "$OUTCOME_PATTERN"; then
    break
  fi
  sleep 1
done

END_EPOCH=$(date +%s)
PX4_UID_AFTER=$("${K[@]}" get pod -n "$NAMESPACE" \
  -l app.kubernetes.io/name=drone01-px4-sitl \
  -o jsonpath='{.items[0].metadata.uid}')
PX4_RESTARTS_AFTER=$("${K[@]}" get pod -n "$NAMESPACE" \
  -l app.kubernetes.io/name=drone01-px4-sitl \
  -o jsonpath='{.items[0].status.containerStatuses[0].restartCount}')
ROUTE=$("${K[@]}" get configmap analytics-routing-drone01 \
  -n "$NAMESPACE" -o jsonpath='{.data.active_instance}')
CORRELATION_ID=$("${K[@]}" logs -n "$NAMESPACE" \
  deployment/operational-event-dispatcher-p2 --tail=300 | \
  sed -n 's/.*DeploymentRequest accepted for //p' | tail -n 1)
EDGE_NODE=$("${K[@]}" get pod -n "$NAMESPACE" \
  -l pod-name=drone01-companion-analytics \
  -o jsonpath='{.items[0].spec.nodeName}' 2>/dev/null || true)

if [[ "$EXPERIMENT_MODE" == "e4" ]]; then
  for _ in $(seq 1 60); do
    if ! "${K[@]}" get deployment drone01-companion-analytics \
      -n "$NAMESPACE" >/dev/null 2>&1; then
      break
    fi
    sleep 1
  done
fi

ONBOARD_STATE=$("${K[@]}" exec -n "$NAMESPACE" \
  deployment/drone01-companion-analytics-onboard -- /bin/bash -lc \
  'source /ws/install/setup.bash; ROS_SUPER_CLIENT=TRUE ROS2CLI_DISABLE_DAEMON=1 ros2 lifecycle get /drone01/companion_analytics_onboard' \
  2>/dev/null | tail -n 1 || true)

if [[ "$EXPERIMENT_MODE" == "p2" ]]; then
  HEALTH_INSTANCE=edge
else
  HEALTH_INSTANCE=onboard
fi
HEALTH_SERVICE="/drone01/companion/$HEALTH_INSTANCE/health"
HEALTH_SNAPSHOT=
for _ in $(seq 1 30); do
  HEALTH_SNAPSHOT=$("${K[@]}" exec -n "$NAMESPACE" \
    deployment/drone01-companion-analytics-onboard -- /bin/bash -lc \
    "source /ws/install/setup.bash; ROS_SUPER_CLIENT=TRUE ROS2CLI_DISABLE_DAEMON=1 timeout 10 ros2 service call $HEALTH_SERVICE cloud_native_robotics_interfaces/srv/GetHealthSnapshot \"{correlation_id: '$CORRELATION_ID-health'}\"" 2>/dev/null || true)
  if [[ "$HEALTH_SNAPSHOT" == *"healthy=True"* && \
        "$HEALTH_SNAPSHOT" == *"lifecycle_state='active'"* ]]; then
    break
  fi
  sleep 1
done
printf '%s\n' "$HEALTH_SNAPSHOT" >"$RESULT_DIR/health-snapshot.txt"

for _ in $(seq 1 30); do
  if "${K[@]}" exec -n "$NAMESPACE" deployment/p2-audit-writer -- \
    grep -Fq "\"correlation_id\":\"$CORRELATION_ID\"" /data/audit.jsonl && \
    "${K[@]}" exec -n "$NAMESPACE" deployment/p2-audit-writer -- \
    grep -Fq '"record_type":"incident_completed"' /data/audit.jsonl && \
    "${K[@]}" exec -n "$NAMESPACE" deployment/p2-audit-writer -- \
    grep -Fq '"record_type":"operator_notification"' /data/audit.jsonl; then
    break
  fi
  sleep 1
done

"${K[@]}" get nodes -o wide >"$RESULT_DIR/nodes.txt"
"${K[@]}" get pods -n "$NAMESPACE" -o wide >"$RESULT_DIR/pods.txt"
"${K[@]}" get pods -n "$NAMESPACE" -o json >"$RESULT_DIR/pods.json"
"${K[@]}" get deployments,hpa,pvc -n "$NAMESPACE" -o yaml \
  >"$RESULT_DIR/kubernetes-resources.yaml"
"${K[@]}" get configmap analytics-routing-drone01 -n "$NAMESPACE" -o yaml \
  >"$RESULT_DIR/analytics-routing.yaml"
"${K[@]}" logs -n "$NAMESPACE" deployment/analytics-event-detector-p2 \
  >"$RESULT_DIR/event-detector.log" 2>&1 || true
"${K[@]}" logs -n "$NAMESPACE" deployment/application-manager-p2 \
  >"$RESULT_DIR/application-manager.log" 2>&1 || true
"${K[@]}" logs -n "$NAMESPACE" deployment/operational-event-dispatcher-p2 \
  >"$RESULT_DIR/dispatcher.log" 2>&1 || true
"${K[@]}" logs -n "$NAMESPACE" deployment/kuberos -c initialize \
  >"$RESULT_DIR/kuberos-bootstrap.log" 2>&1 || true
"${K[@]}" logs -n "$NAMESPACE" deployment/kuberos -c api \
  >"$RESULT_DIR/kuberos-api.log" 2>&1 || true
"${K[@]}" logs -n "$NAMESPACE" deployment/kuberos -c worker \
  >"$RESULT_DIR/kuberos-worker.log" 2>&1 || true
"${K[@]}" logs -n "$NAMESPACE" deployment/drone01-px4-sitl \
  >"$RESULT_DIR/px4.log" 2>&1 || true
"${K[@]}" logs -n "$NAMESPACE" deployment/p2-audit-writer \
  >"$RESULT_DIR/audit-writer.log" 2>&1 || true
"${K[@]}" logs -n "$NAMESPACE" deployment/p2-operator-notifier \
  >"$RESULT_DIR/operator-notifier.log" 2>&1 || true
"${K[@]}" logs -n "$NAMESPACE" deployment/p2-platform-observer \
  >"$RESULT_DIR/platform-observer.log" 2>&1 || true
"${K[@]}" exec -n "$NAMESPACE" deployment/p2-audit-writer -- \
  cat /data/audit.jsonl >"$RESULT_DIR/audit.jsonl"

if "${K[@]}" get deployment drone01-companion-analytics \
  -n "$NAMESPACE" >/dev/null 2>&1; then
  EDGE_FINAL=present
else
  EDGE_FINAL=absent
fi

PASS=true
[[ "$PX4_UID_BEFORE" == "$PX4_UID_AFTER" ]] || PASS=false
if [[ "$USE_IMAGE_LOCK" == "1" ]]; then
  if python3 "$ROOT_DIR/scripts/verify_image_lock_runtime.py" \
    --pods-json "$RESULT_DIR/pods.json" \
    --lock "$IMAGE_LOCK_FILE" \
    --output "$RESULT_DIR/image-provenance.csv" \
    --minimum "$MINIMUM_PROJECT_CONTAINERS" >"$RESULT_DIR/image-provenance-summary.json"; then
    DIGEST_VERIFICATION="PASS ($MINIMUM_PROJECT_CONTAINERS+ project containers)"
  else
    DIGEST_VERIFICATION="FAIL"
    PASS=false
  fi
fi
[[ "$PX4_RESTARTS_BEFORE" == "$PX4_RESTARTS_AFTER" ]] || PASS=false
grep -Fq '"record_type":"platform_snapshot"' \
  "$RESULT_DIR/audit.jsonl" || PASS=false
"${K[@]}" get pvc p2-audit-data -n "$NAMESPACE" \
  -o jsonpath='{.status.phase}' | grep -Fq Bound || PASS=false
[[ "$HEALTH_SNAPSHOT" == *"healthy=True"* ]] || PASS=false
[[ "$HEALTH_SNAPSHOT" == *"lifecycle_state='active'"* ]] || PASS=false
[[ "$HEALTH_SNAPSHOT" == *"instance_id='$HEALTH_INSTANCE'"* ]] || PASS=false

if [[ "$EXPERIMENT_MODE" == "p2" ]]; then
  "${K[@]}" logs -n "$NAMESPACE" deployment/operational-event-dispatcher-p2 \
    --tail=300 | grep -Fq "$OUTCOME_PATTERN" || PASS=false
  grep -Fq "\"correlation_id\":\"$CORRELATION_ID\"" \
    "$RESULT_DIR/audit.jsonl" || PASS=false
  grep -Fq '"record_type":"incident_completed"' \
    "$RESULT_DIR/audit.jsonl" || PASS=false
  grep -Fq '"record_type":"operator_notification"' \
    "$RESULT_DIR/audit.jsonl" || PASS=false
  [[ "$ROUTE" == "edge" ]] || PASS=false
  [[ "$EDGE_NODE" == "k3d-cloud-native-p2-agent-1" ]] || PASS=false
  [[ "$ONBOARD_STATE" == *inactive* ]] || PASS=false
  "${K[@]}" get hpa drone01-companion-analytics-hpa \
    -n "$NAMESPACE" >/dev/null || PASS=false
  AUDIT_STATUS="incident_completed su PVC"
  NOTIFICATION_STATUS="operator_notification registrata"
  MODE_DESCRIPTION="Il target edge e' stato richiesto tramite la API KubeROS. Lifecycle, routing e HPA sono stati coordinati dall'Application Manager dopo la migrazione."
else
  "${K[@]}" logs -n "$NAMESPACE" deployment/operational-event-dispatcher-p2 \
    --tail=300 | grep -Fq "$OUTCOME_PATTERN" || PASS=false
  grep -Fq "\"correlation_id\":\"$CORRELATION_ID\"" \
    "$RESULT_DIR/audit.jsonl" || PASS=false
  grep -Fq '"record_type":"incident_completed"' \
    "$RESULT_DIR/audit.jsonl" || PASS=false
  grep -Fq '"record_type":"operator_notification"' \
    "$RESULT_DIR/audit.jsonl" || PASS=false
  [[ "$ROUTE" == "onboard" ]] || PASS=false
  [[ "$ONBOARD_STATE" == *active* ]] || PASS=false
  [[ "$EDGE_FINAL" == "absent" ]] || PASS=false
  if "${K[@]}" get hpa drone01-companion-analytics-hpa \
    -n "$NAMESPACE" >/dev/null 2>&1; then
    PASS=false
  fi
  grep -Fq '"rollback_performed":true' \
    "$RESULT_DIR/audit.jsonl" || PASS=false
  grep -Fq '"outcome":"analytics_migration_failed"' \
    "$RESULT_DIR/audit.jsonl" || PASS=false
  AUDIT_STATUS="incident_completed con rollback su PVC"
  NOTIFICATION_STATUS="operator_notification registrata"
  MODE_DESCRIPTION="Il target edge non ha superato la readiness. L'Application Manager ha eseguito il rollback mantenendo attivo il workload onboard."
fi

cat >"$RESULT_DIR/REPORT.md" <<EOF
# ${EXPERIMENT_MODE^^} Live Result

| Campo | Valore |
| --- | --- |
| Esito | $PASS |
| Modalita' | $EXPERIMENT_MODE |
| Cluster | $CLUSTER |
| Namespace | $NAMESPACE |
| Provenienza immagini progetto | $IMAGE_PROVENANCE |
| Verifica digest runtime | $DIGEST_VERIFICATION |
| Correlation ID | $CORRELATION_ID |
| Durata osservata | $((END_EPOCH - START_EPOCH)) s |
| Route finale | $ROUTE |
| Nodo edge osservato | $EDGE_NODE |
| Deployment edge finale | $EDGE_FINAL |
| Lifecycle analytics onboard | $ONBOARD_STATE |
| ROS Service health | $HEALTH_SERVICE: healthy, Lifecycle active |
| PX4 UID prima | $PX4_UID_BEFORE |
| PX4 UID dopo | $PX4_UID_AFTER |
| PX4 restart prima/dopo | $PX4_RESTARTS_BEFORE / $PX4_RESTARTS_AFTER |
| Audit persistente | $AUDIT_STATUS |
| Notifica operatore | $NOTIFICATION_STATUS |
| Platform Observer | platform_snapshot registrato |

$MODE_DESCRIPTION
Audit Writer e Platform Observer hanno prodotto record durevoli sul PVC
dedicato; l'Operator Notifier interviene soltanto in presenza di un incidente.
EOF

echo "${EXPERIMENT_MODE^^} result: $PASS"
echo "Evidence: $RESULT_DIR"
[[ "$PASS" == "true" ]]
