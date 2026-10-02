#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
CLUSTER=cloud-native-p2
CONTEXT=k3d-cloud-native-p2
NAMESPACE=cloud-native-p2
SERVER_CONTAINER=k3d-cloud-native-p2-server-0
VARIANT=${VARIANT:-a}
RESULT_ID=$(date -u +%Y%m%dT%H%M%SZ)
RESULT_DIR="$ROOT_DIR/results/e1/$RESULT_ID"
FAULT_START_DELAY_SEC=${E1_FAULT_START_DELAY_SEC:-15.0}
PARTITION_DURATION_SEC=${E1_PARTITION_DURATION_SEC:-35}
DISCOVERY_SETTLE_SEC=${E1_DISCOVERY_SETTLE_SEC:-5}
GOAL_TIMEOUT_SEC=120.0
# Position and altitude (checklist R8; Viviana, 2026-09-25): an optional
# extension of E1, a supplementary validation and not a requirement of the
# proposal, so it does not change the base protocol (EV24) nor invalidate the
# E1 runs made before it. With E1_POSITION_CHECK=1 the position is judged on PX4
# SIH's ground truth over [partition start, first AUTO_RTL] against thresholds
# frozen from two pilots (scripts/position_thresholds.py) and required for PASS;
# without it the ground truth is not even sampled. E1_POSITION_PILOT=1 runs a
# pilot: the same E1 up to the partition point, then E1_PILOT_WINDOW_SEC of
# hover with no fault (the harness waits 100000s) and no partition; it reports
# deviations only and exits. The position runs of A and B use longer timings
# through the existing knobs (E1_FAULT_START_DELAY_SEC, E1_PARTITION_DURATION_SEC)
# so that about 35s of hover fall inside the partition before the RTL.
POSITION_CHECK=${E1_POSITION_CHECK:-0}
POSITION_PILOT=${E1_POSITION_PILOT:-0}
PILOT_WINDOW_SEC=${E1_PILOT_WINDOW_SEC:-35}
POSITION_THRESHOLDS="$ROOT_DIR/config/e1_position_thresholds.json"
if [[ "$POSITION_PILOT" == "1" ]]; then
  POSITION_CHECK=1
  FAULT_START_DELAY_SEC=100000.0   # a double: the harness parameter is one (rclpy rejects an INTEGER)
  RESULT_DIR="$ROOT_DIR/results/e1-pilot/$RESULT_ID"
fi
K=(kubectl --context "$CONTEXT")
SERVER_STOPPED=false

case "$VARIANT" in
  a|b) ;;
  *)
    echo "Unsupported VARIANT: $VARIANT (expected 'a' or 'b')" >&2
    exit 2
    ;;
esac

restore_server() {
  declare -F stop_truth_sampler >/dev/null && stop_truth_sampler
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
# datastore of the bench, embedded etcd (R14 target, docs/ETCD_GATE_PREREGISTRATION.md):
# fail-closed, before any workload or injection
python3 "$ROOT_DIR/scripts/datastore_check.py" check "$CLUSTER" "$RESULT_DIR" \
  || { echo "datastore of $CLUSTER not verified as embedded etcd: stopping before any workload" >&2; exit 2; }

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

# Mission continuity (checklist R8, SPECIFICA s21), identical in both variants:
# a passive VehicleStatus observer on the onboard node (own image, see
# sources/mission_observer) started before PX4 takes off, judged afterwards by
# scripts/mission_continuity.py over the partition window. Expected inside the
# window: the vehicle stays armed in Hold, then the first nav_state change is
# AUTO_RTL, after the fault started; no status gap reaching 5s, no PX4 clock
# going back. What PX4 does after the RTL (landing, disarm) is reported only.
MISSION_IMAGE=cloud-native-ros/mission-observer:p2

start_mission_observer() {
  render_manifest \
    "$ROOT_DIR/manifests/kubernetes/e1/55-mission-observer.yaml" \
    "$RENDERED_DIR/55-mission-observer.yaml"
  "${K[@]}" apply -f "$RENDERED_DIR/55-mission-observer.yaml"
  "${K[@]}" rollout status deployment/drone01-mission-observer \
    -n "$NAMESPACE" --timeout=120s
  for _ in $(seq 1 60); do
    if "${K[@]}" logs -n "$NAMESPACE" deployment/drone01-mission-observer \
      --tail=200 2>/dev/null | grep -Fq MISSION_STATUS_FIRST; then
      return 0
    fi
    sleep 1
  done
  echo "Mission observer received no VehicleStatus within 60s" >&2
  return 1
}

evaluate_mission_continuity() {
  local restarts
  "${K[@]}" logs -n "$NAMESPACE" deployment/drone01-mission-observer \
    >"$RESULT_DIR/mission-observer.log" 2>&1 || true
  restarts=$("${K[@]}" get pod -n "$NAMESPACE" \
    -l app.kubernetes.io/name=drone01-mission-observer \
    -o jsonpath='{.items[0].status.containerStatuses[0].restartCount}' \
    2>/dev/null || true)
  python3 "$ROOT_DIR/scripts/mission_continuity.py" \
    "$RESULT_DIR/mission-observer.log" \
    --window-start-ns "$PARTITION_START_NS" \
    --window-end-ns "$PARTITION_END_NS" \
    --observer-restarts "${restarts:-unknown}" \
    --start-nav 4 --start-arming 2 \
    --expect-nav 5 --not-before-ns "$FAULT_STARTED_NS" \
    >"$RESULT_DIR/mission-continuity.txt"
  mission_field() {
    sed -n "s/^$1=//p" "$RESULT_DIR/mission-continuity.txt"
  }
  MISSION_CONTINUITY=$(mission_field verdict)
  MISSION_REASONS=$(mission_field reasons)
  MISSION_MAX_GAP_MS=$(mission_field max_gap_ms)
  MISSION_START_STATE=$(mission_field state_at_window_start)
  MISSION_UNEXPECTED=$(mission_field unexpected_changes)
  MISSION_REGRESSIONS=$(mission_field clock_regressions)
}

TRUTH_PID=""
start_truth_sampler() {  # PX4 SIH ground truth, read from the node (no Kubernetes API)
  local node cid
  [[ "$POSITION_CHECK" == "1" ]] || return 0
  node=$("${K[@]}" get pod -n "$NAMESPACE" -l app.kubernetes.io/name=drone01-px4-sitl \
    -o jsonpath='{.items[0].spec.nodeName}')
  cid=$(docker exec "$node" crictl ps --name px4 -q | head -n 1)
  if [[ -z "$node" || -z "$cid" ]]; then
    echo "PX4 container not found for the ground-truth sampler" >&2
    return 1
  fi
  bash "$ROOT_DIR/scripts/px4_truth_sampler.sh" "$node" "$cid" \
    "$RESULT_DIR/px4-groundtruth.raw" &
  TRUTH_PID=$!
}

stop_truth_sampler() {
  if [[ -n "$TRUTH_PID" ]]; then
    kill "$TRUTH_PID" 2>/dev/null || true
    wait "$TRUTH_PID" 2>/dev/null || true
    TRUTH_PID=""
  fi
}

position_field() {
  sed -n "s/^$1=//p" "$RESULT_DIR/position-hold.txt"
}

evaluate_position_hold() {
  if [[ "$POSITION_CHECK" != "1" ]]; then
    POSITION_HOLD="non eseguito (protocollo base; E1_POSITION_CHECK=1 per l'estensione)"
    POSITION_REASONS="-"; POSITION_WINDOW="-"; POSITION_SAMPLES="-"; POSITION_H="-"
    POSITION_V="-"; POSITION_THR_H="-"; POSITION_THR_V="-"; POSITION_EST="-"; POSITION_DIFF="-"
    return 0
  fi
  python3 "$ROOT_DIR/scripts/position_hold.py" \
    --truth "$RESULT_DIR/px4-groundtruth.raw" \
    --observer-log "$RESULT_DIR/mission-observer.log" \
    --window-start-ns "$PARTITION_START_NS" --end-at-rtl \
    --thresholds "$POSITION_THRESHOLDS" >"$RESULT_DIR/position-hold.txt"
  POSITION_HOLD=$(position_field verdict)
  POSITION_REASONS=$(position_field reasons)
  POSITION_WINDOW=$(position_field window_sec)
  POSITION_SAMPLES=$(position_field truth_samples)
  POSITION_H=$(position_field max_horizontal_m)
  POSITION_V=$(position_field max_vertical_m)
  POSITION_THR_H=$(position_field threshold_horizontal_m)
  POSITION_THR_V=$(position_field threshold_vertical_m)
  POSITION_EST=$(position_field estimate_problems)
  POSITION_DIFF=$(position_field estimate_minus_truth)
}

run_position_pilot() {  # instead of partition and fault; deviations only
  local start end
  start=$(date +%s%N)
  sleep "$PILOT_WINDOW_SEC"
  end=$(date +%s%N)
  sleep 6                                  # one more observer summary after the window
  stop_truth_sampler
  "${K[@]}" logs -n "$NAMESPACE" deployment/drone01-mission-observer \
    >"$RESULT_DIR/mission-observer.log" 2>&1 || true
  python3 "$ROOT_DIR/scripts/position_hold.py" \
    --truth "$RESULT_DIR/px4-groundtruth.raw" \
    --observer-log "$RESULT_DIR/mission-observer.log" \
    --window-start-ns "$start" --window-end-ns "$end" --pilot \
    >"$RESULT_DIR/position-hold.txt"
  # The pilot's premise: armed in Hold, no state change, over the same window.
  python3 "$ROOT_DIR/scripts/mission_continuity.py" "$RESULT_DIR/mission-observer.log" \
    --window-start-ns "$start" --window-end-ns "$end" --observer-restarts 0 \
    --start-nav 4 --start-arming 2 >"$RESULT_DIR/mission-continuity.txt"
  echo "continuity_verdict=$(sed -n 's/^verdict=//p' "$RESULT_DIR/mission-continuity.txt")" \
    >>"$RESULT_DIR/position-hold.txt"
  cat >"$RESULT_DIR/REPORT.md" <<EOF
# E1 position/altitude pilot (variant ${VARIANT^^})

| Campo | Valore |
| --- | --- |
| Esito del pilota | $(position_field verdict) ($(position_field reasons)) |
| Premessa: armato in Hold, nessun cambio di stato | $(position_field continuity_verdict) |
| Finestra (control plane acceso, nessun guasto) | $(position_field window_sec) s, $(position_field truth_samples) campioni di verita' SIH |
| Deviazione massima orizzontale / quota | $(position_field max_horizontal_m) m / $(position_field max_vertical_m) m |
| Stima PX4 | all'inizio $(position_field estimate_at_start); problemi: $(position_field estimate_problems) |
| Stima meno verita' | $(position_field estimate_minus_truth) |

Pilota per le soglie comuni (scripts/position_thresholds.py): non e' una prova E1.
EOF
  echo "E1 position pilot (variant $VARIANT): $(position_field verdict)"
  echo "Evidence: $RESULT_DIR"
}

if [[ "$VARIANT" == "a" ]]; then

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
  docker build -t "$MISSION_IMAGE" \
    -f "$ROOT_DIR/containers/mission-observer/Dockerfile" "$ROOT_DIR"
  docker build -t cloud-native-ros/kuberos:p2 \
    -f "$ROOT_DIR/containers/kuberos/Dockerfile" "$ROOT_DIR"
fi

if [[ "${SKIP_IMAGE_BUILD_IMPORT:-0}" != "1" && "${SKIP_IMAGE_IMPORT:-0}" != "1" ]]; then
  k3d image import -c "$CLUSTER" \
    cloud-native-ros/control-plane:p2 \
    cloud-native-ros/event-detector:p2 \
    "$MISSION_IMAGE" \
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
start_mission_observer

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
start_truth_sampler
if [[ "$POSITION_PILOT" == "1" ]]; then
  run_position_pilot
  exit 0
fi

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
stop_truth_sampler

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
# Only variant A has anything to time here: this is how long, after the
# control plane comes back, it takes the DDS transient-local redelivery of
# the P0 BatteryLow OperationalEvent to reach Application Manager/Dispatcher
# and land in audit.jsonl. Variant B has no equivalent measurement -- not
# because it is untested, but because the proposal explicitly keeps P0 out
# of the Fleet Operator/State Bridge's scope in both variants (S3.1: "P0
# BatteryLow ... riusato per la sola safety locale; il segnale P1/P2
# confluisce nello State Bridge"), so there is no control-plane-side event
# for it to ever catch up on there.
AUDIT_CATCHUP_END_NS=$(date +%s%N)
AUDIT_CATCHUP_MS=$(( (AUDIT_CATCHUP_END_NS - PARTITION_END_NS) / 1000000 ))

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
evaluate_mission_continuity
evaluate_position_hold
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
[[ "$MISSION_CONTINUITY" == "true" ]] || PASS=false
[[ "$POSITION_CHECK" != "1" || "$POSITION_HOLD" == "true" ]] || PASS=false
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
| Tempo di recupero audit post-partizione (solo variante A, vedi nota) | $AUDIT_CATCHUP_MS ms |
| Safety owner | onboard |
| Deployment edge/HPA | assenti |
| PX4 UID prima | $PX4_UID_BEFORE |
| PX4 UID dopo | $PX4_UID_AFTER |
| PX4 restart prima/dopo | $PX4_RESTARTS_BEFORE / $PX4_RESTARTS_AFTER |
| Continuita' missione (vehicle_status, \`mission-continuity.txt\`) | $MISSION_CONTINUITY ($MISSION_REASONS) |
| Stato all'inizio della partizione | $MISSION_START_STATE |
| Gap massimo di vehicle_status, intera registrazione (giudicati solo quelli nella partizione, limite 5000) | $MISSION_MAX_GAP_MS ms |
| Nella partizione: cambi di stato prima dell'AUTO_RTL / regressioni orologio PX4 | $MISSION_UNEXPECTED / $MISSION_REGRESSIONS |
| Posizione e quota (verita' SIH, \`position-hold.txt\`) | $POSITION_HOLD ($POSITION_REASONS) |
| Finestra: inizio partizione -> primo AUTO_RTL | $POSITION_WINDOW s, $POSITION_SAMPLES campioni |
| Deviazione massima orizzontale / quota (soglie) | $POSITION_H m / $POSITION_V m ($POSITION_THR_H / $POSITION_THR_V m) |
| Stima PX4 nella finestra (validita', reset, continuita') | $POSITION_EST |
| Stima meno verita' (riportata) | $POSITION_DIFF |

Il fault, il comando RTL, l'ack PX4 e lo stato AUTO_RTL sono stati osservati
sul nodo onboard mentre il server k3d, KubeROS, Kubernetes API, dispatcher e
Application Manager erano arrestati. Dopo il ripristino, la history DDS
transient-local ha consegnato l'evento BatteryLow al control plane; la policy
P0 ha registrato audit e notifica senza emettere azioni safety Kubernetes.

Il "tempo di recupero audit" sopra esiste solo per questa variante: e' un
comportamento residuo di Application Manager (invariato, "resta eseguibile
come baseline A" per il proposal), che resta comunque sottoscritto a ogni
OperationalEvent P0 pur non agendo mai su di esso. La variante B non ha un
equivalente da cronometrare: il proposal esclude esplicitamente P0 dal
State Bridge/Fleet Operator in entrambe le varianti (S3.1), quindi li' non
esiste alcun evento di control plane su cui recuperare.
EOF

echo "E1 result (variant A): $PASS"
echo "Evidence: $RESULT_DIR"
[[ "$PASS" == "true" ]]

else
# =====================================================================
# VARIANT B: dichiarativo (CRD + Fleet Operator, non coinvolto per P0)
#
# Stesso meccanismo di guasto e di partizione di variante A -- il fault
# harness, l'Event Detector onboard (plugin BatteryLowRule) e PX4 sono
# "invariati, riusati as-is" per design del proposal (safety locale, P0
# BatteryLow, mai sostituita dal Fleet Operator). L'unica cosa che cambia
# e' quale control plane e' presente (ma comunque spento durante la
# partizione, quindi comunque non coinvolto nel percorso di sicurezza):
# qui il Fleet Operator invece di KubeROS/Application Manager/Dispatcher.
#
# Non replicato qui: la catena di audit (Application Manager -> Dispatcher
# -> Audit Writer/Operator Notifier). Il proposal dice esplicitamente che
# il Dispatcher e' "sostituito... dal watch nativo dell'Operator" senza
# eccezioni per P0, e la variante B di E0 ha gia' fatto la stessa scelta
# (25-observability.yaml non applicato). Le asserzioni che contano per il
# confronto dichiarativo restano tutte verificate: sicurezza locale durante
# la partizione totale, PX4 mai toccato, nessuna azione del Fleet Operator
# per un evento che non gli compete.
# =====================================================================

if [[ "${SKIP_IMAGE_BUILD_IMPORT:-0}" != "1" && "${SKIP_IMAGE_BUILD:-0}" != "1" ]]; then
  docker build -t cloud-native-ros/control-plane:p2 \
    -f "$ROOT_DIR/containers/control-plane/Dockerfile" "$ROOT_DIR"
  docker build -t cloud-native-ros/event-detector:p2 \
    -f "$ROOT_DIR/containers/event-detector/Dockerfile" "$ROOT_DIR"
  docker build -t "$MISSION_IMAGE" \
    -f "$ROOT_DIR/containers/mission-observer/Dockerfile" "$ROOT_DIR"
  docker build -t cloud-native-ros/fleet-operator:p2 \
    -f "$ROOT_DIR/containers/fleet-operator/Dockerfile" "$ROOT_DIR"
  docker build -t cloud-native-ros/state-bridge:p2 \
    -f "$ROOT_DIR/containers/state-bridge/Dockerfile" "$ROOT_DIR"
fi

if [[ "${SKIP_IMAGE_BUILD_IMPORT:-0}" != "1" && "${SKIP_IMAGE_IMPORT:-0}" != "1" ]]; then
  k3d image import -c "$CLUSTER" \
    cloud-native-ros/control-plane:p2 \
    cloud-native-ros/event-detector:p2 \
    "$MISSION_IMAGE" \
    cloud-native-ros/fleet-operator:p2 \
    cloud-native-ros/state-bridge:p2 \
    microros/micro-ros-agent:humble \
    px4io/px4-sitl:latest
fi

render_manifest \
  "$ROOT_DIR/manifests/kubernetes/e1/40-workload.yaml" \
  "$RENDERED_DIR/40-workload.yaml"
render_manifest \
  "$ROOT_DIR/manifests/kubernetes/e1/50-fault-harness.yaml" \
  "$RENDERED_DIR/50-fault-harness.yaml"
sed -e "s/__P2_DISCOVERY_ADDRESS__/$DISCOVERY_SERVER_ADDRESS/g" \
  "$ROOT_DIR/manifests/kubernetes/p2/50-declarative-control-plane.yaml" \
  >"$RENDERED_DIR/50-declarative-control-plane.yaml"

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
"${K[@]}" apply -f "$ROOT_DIR/manifests/kubernetes/p2/10-discovery.yaml"
"${K[@]}" apply -f "$RENDERED_DIR/50-declarative-control-plane.yaml"
"${K[@]}" rollout status deployment/p2-fastdds-discovery -n "$NAMESPACE" --timeout=180s
"${K[@]}" rollout status deployment/fleet-operator -n "$NAMESPACE" --timeout=120s

"${K[@]}" apply -f "$RENDERED_DIR/40-workload.yaml"
"${K[@]}" wait --for=condition=available deployment --all \
  -n "$NAMESPACE" --timeout=180s
start_mission_observer

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
start_truth_sampler
if [[ "$POSITION_PILOT" == "1" ]]; then
  run_position_pilot
  exit 0
fi

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
stop_truth_sampler

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

PX4_UID_AFTER=$("${K[@]}" get pod -n "$NAMESPACE" \
  -l app.kubernetes.io/name=drone01-px4-sitl \
  -o jsonpath='{.items[0].metadata.uid}')
PX4_RESTARTS_AFTER=$("${K[@]}" get pod -n "$NAMESPACE" \
  -l app.kubernetes.io/name=drone01-px4-sitl \
  -o jsonpath='{.items[0].status.containerStatuses[0].restartCount}')

"${K[@]}" get nodes -o wide >"$RESULT_DIR/nodes.txt"
"${K[@]}" get pods -n "$NAMESPACE" -o wide >"$RESULT_DIR/pods.txt"
"${K[@]}" get deployments,rosmodules,adaptationpolicies -n "$NAMESPACE" -o yaml \
  >"$RESULT_DIR/kubernetes-resources.yaml"
"${K[@]}" get events -n "$NAMESPACE" --sort-by=.lastTimestamp \
  >"$RESULT_DIR/kubernetes-events.txt"
"${K[@]}" logs -n "$NAMESPACE" deployment/e1-battery-fault-harness \
  >"$RESULT_DIR/fault-harness.log" 2>&1
"${K[@]}" logs -n "$NAMESPACE" deployment/e1-battery-event-detector \
  >"$RESULT_DIR/event-detector.log" 2>&1
"${K[@]}" logs -n "$NAMESPACE" deployment/fleet-operator \
  >"$RESULT_DIR/fleet-operator.log" 2>&1
"${K[@]}" logs -n "$NAMESPACE" deployment/drone01-px4-sitl \
  >"$RESULT_DIR/px4.log" 2>&1

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
evaluate_mission_continuity
evaluate_position_hold
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
[[ "$MISSION_CONTINUITY" == "true" ]] || PASS=false
[[ "$POSITION_CHECK" != "1" || "$POSITION_HOLD" == "true" ]] || PASS=false
if "${K[@]}" get hpa -n "$NAMESPACE" -o name | grep -q .; then
  PASS=false
fi
if "${K[@]}" get deployment drone01-companion-analytics \
  -n "$NAMESPACE" >/dev/null 2>&1; then
  PASS=false
fi
if "${K[@]}" get rosmodule -n "$NAMESPACE" -o name | grep -q .; then
  PASS=false
fi
if "${K[@]}" get adaptationpolicy -n "$NAMESPACE" -o name | grep -q .; then
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
# E1 Declarative Live Result

| Campo | Valore |
| --- | --- |
| Esito | $PASS |
| Variante | B (dichiarativa: CRD + Fleet Operator, non coinvolto per P0) |
| Cluster | $CLUSTER |
| Namespace | $NAMESPACE |
| Control plane durante il fault | stopped (Fleet Operator incluso) |
| Durata partizione osservata | $PARTITION_DURATION_OBSERVED_MS ms |
| Fault BatteryLow | 10% per 8 s, recovery 80% |
| Latenza fault -> comando RTL | $SAFETY_COMMAND_LATENCY_MS ms |
| Latenza comando -> ack accepted | $ACK_LATENCY_MS ms |
| Stato RTL osservato localmente | true |
| ROSModule/AdaptationPolicy | assenti (P0 fuori scope del Fleet Operator) |
| Deployment edge/HPA | assenti |
| PX4 UID prima | $PX4_UID_BEFORE |
| PX4 UID dopo | $PX4_UID_AFTER |
| PX4 restart prima/dopo | $PX4_RESTARTS_BEFORE / $PX4_RESTARTS_AFTER |
| Continuita' missione (vehicle_status, \`mission-continuity.txt\`) | $MISSION_CONTINUITY ($MISSION_REASONS) |
| Stato all'inizio della partizione | $MISSION_START_STATE |
| Gap massimo di vehicle_status, intera registrazione (giudicati solo quelli nella partizione, limite 5000) | $MISSION_MAX_GAP_MS ms |
| Nella partizione: cambi di stato prima dell'AUTO_RTL / regressioni orologio PX4 | $MISSION_UNEXPECTED / $MISSION_REGRESSIONS |
| Posizione e quota (verita' SIH, \`position-hold.txt\`) | $POSITION_HOLD ($POSITION_REASONS) |
| Finestra: inizio partizione -> primo AUTO_RTL | $POSITION_WINDOW s, $POSITION_SAMPLES campioni |
| Deviazione massima orizzontale / quota (soglie) | $POSITION_H m / $POSITION_V m ($POSITION_THR_H / $POSITION_THR_V m) |
| Stima PX4 nella finestra (validita', reset, continuita') | $POSITION_EST |
| Stima meno verita' (riportata) | $POSITION_DIFF |

Il fault, il comando RTL, l'ack PX4 e lo stato AUTO_RTL sono stati osservati
sul nodo onboard mentre il server k3d -- Kubernetes API e Fleet Operator
inclusi -- era arrestato. Nessuna Custom Resource dichiarativa esiste per
questo evento: P0 resta safety locale, invariata, esattamente come in
variante A, mai intermediata dal control plane in nessuna delle due varianti.
EOF

echo "E1 result (variant B): $PASS"
echo "Evidence: $RESULT_DIR"
[[ "$PASS" == "true" ]]

fi
