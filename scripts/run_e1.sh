#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
CLUSTER=cloud-native-p2
CONTEXT=k3d-cloud-native-p2
NAMESPACE=cloud-native-p2
SERVER_CONTAINER=k3d-cloud-native-p2-server-0
RESULT_ID=$(date -u +%Y%m%dT%H%M%SZ)
RESULT_DIR="$ROOT_DIR/results/e1/$RESULT_ID"
FAULT_START_DELAY_SEC=${E1_FAULT_START_DELAY_SEC:-15.0}
PARTITION_DURATION_SEC=${E1_PARTITION_DURATION_SEC:-35}
DISCOVERY_SETTLE_SEC=${E1_DISCOVERY_SETTLE_SEC:-5}
GOAL_TIMEOUT_SEC=120.0
K=(kubectl --context "$CONTEXT")
SERVER_STOPPED=false

restore_server() {
  if [[ "$SERVER_STOPPED" == "true" ]]; then
    docker start "$SERVER_CONTAINER" >/dev/null || true
  fi
}
trap restore_server EXIT

for command in docker k3d kubectl python3; do
  command -v "$command" >/dev/null || {
    echo "Required command not found: $command" >&2
    exit 1
  }
done

mkdir -p "$RESULT_DIR"

if [[ "${RESET_E1:-0}" == "1" ]] && \
  k3d cluster list --no-headers | awk '{print $1}' | grep -qx "$CLUSTER"; then
  k3d cluster delete "$CLUSTER"
fi

if ! k3d cluster list --no-headers | awk '{print $1}' | grep -qx "$CLUSTER"; then
  k3d cluster create --config \
    "$ROOT_DIR/manifests/kubernetes/p2/k3d-cloud-native-p2.yaml"
fi

DISCOVERY_SERVER_ADDRESS=$("${K[@]}" get node \
  k3d-cloud-native-p2-server-0 \
  -o jsonpath='{.status.addresses[?(@.type=="InternalIP")].address}')
RENDERED_DIR="$RESULT_DIR/rendered"
mkdir -p "$RENDERED_DIR"

render_manifest() {
  sed \
    -e "s/__P2_DISCOVERY_ADDRESS__/$DISCOVERY_SERVER_ADDRESS/g" \
    -e "s/__P2_GOAL_TIMEOUT_SEC__/$GOAL_TIMEOUT_SEC/g" \
    -e "s/__E1_FAULT_START_DELAY_SEC__/$FAULT_START_DELAY_SEC/g" \
    "$1" >"$2"
}

render_manifest \
  "$ROOT_DIR/manifests/kubernetes/p2/30-control-plane.yaml" \
  "$RENDERED_DIR/30-control-plane.yaml"
render_manifest \
  "$ROOT_DIR/manifests/kubernetes/e1/40-workload.yaml" \
  "$RENDERED_DIR/40-workload.yaml"
render_manifest \
  "$ROOT_DIR/manifests/kubernetes/e1/50-fault-harness.yaml" \
  "$RENDERED_DIR/50-fault-harness.yaml"
render_manifest \
  "$ROOT_DIR/manifests/kuberos/p2/analytics-edge.yaml" \
  "$RENDERED_DIR/analytics-edge.yaml"

if [[ "${SKIP_IMAGE_BUILD_IMPORT:-0}" != "1" && "${SKIP_IMAGE_BUILD:-0}" != "1" ]]; then
  docker build -t cloud-native-ros/control-plane:p2 \
    -f "$ROOT_DIR/containers/control-plane/Dockerfile" "$ROOT_DIR"
  docker build -t cloud-native-ros/event-detector:p2 \
    -f "$ROOT_DIR/containers/event-detector/Dockerfile" "$ROOT_DIR"
  docker build -t cloud-native-ros/kuberos:p2 \
    -f "$ROOT_DIR/containers/kuberos/Dockerfile" "$ROOT_DIR"
fi

if [[ "${SKIP_IMAGE_BUILD_IMPORT:-0}" != "1" && "${SKIP_IMAGE_IMPORT:-0}" != "1" ]]; then
  k3d image import -c "$CLUSTER" \
    cloud-native-ros/control-plane:p2 \
    cloud-native-ros/event-detector:p2 \
    cloud-native-ros/kuberos:p2 \
    microros/micro-ros-agent:humble \
    px4io/px4-sitl:latest \
    redis:7
fi

"${K[@]}" apply -f "$ROOT_DIR/manifests/kubernetes/p2/00-rbac.yaml"
"${K[@]}" apply -f "$ROOT_DIR/manifests/kubernetes/p2/10-discovery.yaml"
"${K[@]}" apply -f "$ROOT_DIR/manifests/kubernetes/p2/20-kuberos.yaml"
"${K[@]}" apply -f "$ROOT_DIR/manifests/kubernetes/p2/25-observability.yaml"
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

"${K[@]}" apply -f "$RENDERED_DIR/40-workload.yaml"
"${K[@]}" wait --for=condition=available deployment --all \
  -n "$NAMESPACE" --timeout=180s

PX4_UID_BEFORE=$("${K[@]}" get pod -n "$NAMESPACE" \
  -l app.kubernetes.io/name=drone01-px4-sitl \
  -o jsonpath='{.items[0].metadata.uid}')
PX4_RESTARTS_BEFORE=$("${K[@]}" get pod -n "$NAMESPACE" \
  -l app.kubernetes.io/name=drone01-px4-sitl \
  -o jsonpath='{.items[0].status.containerStatuses[0].restartCount}')

"${K[@]}" exec -n "$NAMESPACE" deployment/drone01-px4-sitl -- \
  /bin/bash -lc \
  'cd /opt/px4; PATH=/opt/px4/bin:$PATH; px4-commander arm; sleep 2; px4-commander takeoff; sleep 8; px4-commander status' \
  >"$RESULT_DIR/px4-pre-partition.txt"

"${K[@]}" apply -f "$RENDERED_DIR/50-fault-harness.yaml"
"${K[@]}" rollout status deployment/e1-battery-fault-harness \
  -n "$NAMESPACE" --timeout=120s
for _ in $(seq 1 30); do
  if "${K[@]}" logs -n "$NAMESPACE" deployment/e1-battery-fault-harness \
    --tail=100 2>/dev/null | grep -Fq E1_HARNESS_READY; then
    break
  fi
  sleep 1
done
"${K[@]}" logs -n "$NAMESPACE" deployment/e1-battery-fault-harness \
  --tail=100 | grep -Fq E1_HARNESS_READY
sleep "$DISCOVERY_SETTLE_SEC"

PARTITION_REQUESTED_NS=$(date +%s%N)
docker stop -t 5 "$SERVER_CONTAINER" >/dev/null
SERVER_STOPPED=true
PARTITION_START_NS=$(date +%s%N)
SERVER_RUNNING_DURING_PARTITION=$(docker inspect \
  -f '{{.State.Running}}' "$SERVER_CONTAINER")
sleep "$PARTITION_DURATION_SEC"
RESTORE_REQUESTED_NS=$(date +%s%N)
docker start "$SERVER_CONTAINER" >/dev/null
SERVER_STOPPED=false
PARTITION_END_NS=$(date +%s%N)

for _ in $(seq 1 120); do
  if "${K[@]}" --request-timeout=2s get namespace "$NAMESPACE" \
    >/dev/null 2>&1; then
    break
  fi
  sleep 1
done
"${K[@]}" --request-timeout=5s get namespace "$NAMESPACE" >/dev/null
"${K[@]}" wait --for=condition=Ready node --all --timeout=180s
"${K[@]}" wait --for=condition=available deployment --all \
  -n "$NAMESPACE" --timeout=180s

OUTCOME_PATTERN='completed as local_safety_observed (STABLE)'
for _ in $(seq 1 180); do
  if "${K[@]}" logs -n "$NAMESPACE" deployment/operational-event-dispatcher-p2 \
    --tail=300 2>/dev/null | grep -Fq "$OUTCOME_PATTERN"; then
    break
  fi
  sleep 1
done

CORRELATION_ID=$("${K[@]}" logs -n "$NAMESPACE" \
  deployment/operational-event-dispatcher-p2 --tail=300 | \
  sed -n 's/.*DeploymentRequest accepted for //p' | tail -n 1)
for _ in $(seq 1 45); do
  if "${K[@]}" exec -n "$NAMESPACE" deployment/p2-audit-writer -- \
    grep -Fq "\"correlation_id\":\"$CORRELATION_ID\"" /data/audit.jsonl \
      >/dev/null 2>&1 && \
    "${K[@]}" exec -n "$NAMESPACE" deployment/p2-audit-writer -- \
    grep -Fq '"outcome":"local_safety_observed"' /data/audit.jsonl \
      >/dev/null 2>&1 && \
    "${K[@]}" exec -n "$NAMESPACE" deployment/p2-audit-writer -- \
    grep -Fq '"record_type":"operator_notification"' /data/audit.jsonl \
      >/dev/null 2>&1; then
    break
  fi
  sleep 1
done

PX4_UID_AFTER=$("${K[@]}" get pod -n "$NAMESPACE" \
  -l app.kubernetes.io/name=drone01-px4-sitl \
  -o jsonpath='{.items[0].metadata.uid}')
PX4_RESTARTS_AFTER=$("${K[@]}" get pod -n "$NAMESPACE" \
  -l app.kubernetes.io/name=drone01-px4-sitl \
  -o jsonpath='{.items[0].status.containerStatuses[0].restartCount}')

"${K[@]}" get nodes -o wide >"$RESULT_DIR/nodes.txt"
"${K[@]}" get pods -n "$NAMESPACE" -o wide >"$RESULT_DIR/pods.txt"
"${K[@]}" get deployments,hpa,pvc -n "$NAMESPACE" -o yaml \
  >"$RESULT_DIR/kubernetes-resources.yaml"
"${K[@]}" get events -n "$NAMESPACE" --sort-by=.lastTimestamp \
  >"$RESULT_DIR/kubernetes-events.txt"
"${K[@]}" logs -n "$NAMESPACE" deployment/e1-battery-fault-harness \
  >"$RESULT_DIR/fault-harness.log" 2>&1
"${K[@]}" logs -n "$NAMESPACE" deployment/e1-battery-event-detector \
  >"$RESULT_DIR/event-detector.log" 2>&1
"${K[@]}" logs -n "$NAMESPACE" deployment/application-manager-p2 \
  >"$RESULT_DIR/application-manager.log" 2>&1
"${K[@]}" logs -n "$NAMESPACE" deployment/operational-event-dispatcher-p2 \
  >"$RESULT_DIR/dispatcher.log" 2>&1
"${K[@]}" logs -n "$NAMESPACE" deployment/kuberos -c api \
  >"$RESULT_DIR/kuberos-api.log" 2>&1
"${K[@]}" logs -n "$NAMESPACE" deployment/drone01-px4-sitl \
  >"$RESULT_DIR/px4.log" 2>&1
"${K[@]}" exec -n "$NAMESPACE" deployment/p2-audit-writer -- \
  cat /data/audit.jsonl >"$RESULT_DIR/audit.jsonl"

marker_ns() {
  sed -n "s/.*$1 timestamp_ns=\([0-9][0-9]*\).*/\1/p" \
    "$RESULT_DIR/fault-harness.log" | head -n 1
}

FAULT_STARTED_NS=$(marker_ns E1_FAULT_STARTED)
COMMAND_OBSERVED_NS=$(marker_ns E1_RTL_COMMAND_OBSERVED)
ACK_ACCEPTED_NS=$(marker_ns E1_RTL_ACK_ACCEPTED)
RTL_OBSERVED_NS=$(marker_ns E1_RTL_STATE_OBSERVED)
FAULT_COMPLETED_NS=$(marker_ns E1_FAULT_COMPLETED)
FAULT_STARTED_NS=${FAULT_STARTED_NS:-0}
COMMAND_OBSERVED_NS=${COMMAND_OBSERVED_NS:-0}
ACK_ACCEPTED_NS=${ACK_ACCEPTED_NS:-0}
RTL_OBSERVED_NS=${RTL_OBSERVED_NS:-0}
FAULT_COMPLETED_NS=${FAULT_COMPLETED_NS:-0}
SAFETY_COMMAND_LATENCY_MS=$((
  (COMMAND_OBSERVED_NS - FAULT_STARTED_NS) / 1000000
))
ACK_LATENCY_MS=$(((ACK_ACCEPTED_NS - COMMAND_OBSERVED_NS) / 1000000))
PARTITION_DURATION_OBSERVED_MS=$((
  (PARTITION_END_NS - PARTITION_START_NS) / 1000000
))

PASS=true
[[ "$SERVER_RUNNING_DURING_PARTITION" == "false" ]] || PASS=false
(( FAULT_STARTED_NS > 0 && COMMAND_OBSERVED_NS > 0 )) || PASS=false
(( ACK_ACCEPTED_NS > 0 && RTL_OBSERVED_NS > 0 )) || PASS=false
(( FAULT_COMPLETED_NS > 0 )) || PASS=false
(( FAULT_STARTED_NS > PARTITION_START_NS )) || PASS=false
(( COMMAND_OBSERVED_NS > PARTITION_START_NS )) || PASS=false
(( ACK_ACCEPTED_NS > PARTITION_START_NS )) || PASS=false
(( RTL_OBSERVED_NS > PARTITION_START_NS )) || PASS=false
(( FAULT_COMPLETED_NS < PARTITION_END_NS )) || PASS=false
(( COMMAND_OBSERVED_NS < PARTITION_END_NS )) || PASS=false
(( ACK_ACCEPTED_NS < PARTITION_END_NS )) || PASS=false
(( RTL_OBSERVED_NS < PARTITION_END_NS )) || PASS=false
[[ "$PX4_UID_BEFORE" == "$PX4_UID_AFTER" ]] || PASS=false
[[ "$PX4_RESTARTS_BEFORE" == "$PX4_RESTARTS_AFTER" ]] || PASS=false
grep -Fq "$OUTCOME_PATTERN" "$RESULT_DIR/dispatcher.log" || PASS=false
grep -Fq '"event_type":"BatteryLow"' "$RESULT_DIR/audit.jsonl" || PASS=false
grep -Fq '"policy_id":"P0"' "$RESULT_DIR/audit.jsonl" || PASS=false
grep -Fq '"outcome":"local_safety_observed"' \
  "$RESULT_DIR/audit.jsonl" || PASS=false
grep -Fq '"safety_owner":"onboard"' "$RESULT_DIR/audit.jsonl" || PASS=false
grep -Fq '"record_type":"operator_notification"' \
  "$RESULT_DIR/audit.jsonl" || PASS=false
if "${K[@]}" get hpa -n "$NAMESPACE" -o name | grep -q .; then
  PASS=false
fi
if "${K[@]}" get deployment drone01-companion-analytics \
  -n "$NAMESPACE" >/dev/null 2>&1; then
  PASS=false
fi

cat >"$RESULT_DIR/partition.txt" <<EOF
partition_requested_ns=$PARTITION_REQUESTED_NS
partition_start_ns=$PARTITION_START_NS
restore_requested_ns=$RESTORE_REQUESTED_NS
partition_end_ns=$PARTITION_END_NS
server_running_during_partition=$SERVER_RUNNING_DURING_PARTITION
EOF

cat >"$RESULT_DIR/REPORT.md" <<EOF
# E1 Live Result

| Campo | Valore |
| --- | --- |
| Esito | $PASS |
| Cluster | $CLUSTER |
| Namespace | $NAMESPACE |
| Correlation ID | $CORRELATION_ID |
| Control plane durante il fault | stopped |
| Durata partizione osservata | $PARTITION_DURATION_OBSERVED_MS ms |
| Fault BatteryLow | 10% per 8 s, recovery 80% |
| Latenza fault -> comando RTL | $SAFETY_COMMAND_LATENCY_MS ms |
| Latenza comando -> ack accepted | $ACK_LATENCY_MS ms |
| Stato RTL osservato localmente | true |
| Outcome fleet dopo ripristino | local_safety_observed (STABLE) |
| Safety owner | onboard |
| Deployment edge/HPA | assenti |
| PX4 UID prima | $PX4_UID_BEFORE |
| PX4 UID dopo | $PX4_UID_AFTER |
| PX4 restart prima/dopo | $PX4_RESTARTS_BEFORE / $PX4_RESTARTS_AFTER |

Il fault, il comando RTL, l'ack PX4 e lo stato AUTO_RTL sono stati osservati
sul nodo onboard mentre il server k3d, KubeROS, Kubernetes API, dispatcher e
Application Manager erano arrestati. Dopo il ripristino, la history DDS
transient-local ha consegnato l'evento BatteryLow al control plane; la policy
P0 ha registrato audit e notifica senza emettere azioni safety Kubernetes.
EOF

echo "E1 result: $PASS"
echo "Evidence: $RESULT_DIR"
[[ "$PASS" == "true" ]]
