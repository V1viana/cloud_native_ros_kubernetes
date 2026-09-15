#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
CLUSTER=cloud-native-e2
CONTEXT=k3d-cloud-native-e2
NAMESPACE=cloud-native-e2
RESULT_ID=$(date -u +%Y%m%dT%H%M%SZ)
RESULT_DIR="$ROOT_DIR/results/e2/$RESULT_ID"
MISSION_SETTLE_SEC=${E2_MISSION_SETTLE_SEC:-10}
HOVER_GATE_TIMEOUT_SEC=${E2_HOVER_GATE_TIMEOUT_SEC:-60}
HOVER_GATE_INTERVAL_SEC=${E2_HOVER_GATE_INTERVAL_SEC:-2}
HOVER_GATE_CONSECUTIVE=${E2_HOVER_GATE_CONSECUTIVE:-3}
FAULT_SNAPSHOT_DELAY_SEC=${E2_FAULT_SNAPSHOT_DELAY_SEC:-1}
RECOVERY_TIMEOUT_SEC=${E2_RECOVERY_TIMEOUT_SEC:-120}
K=(kubectl --context "$CONTEXT")
MISSION_ARMED=false

land_vehicle() {
  if [[ "$MISSION_ARMED" == "true" ]]; then
    "${K[@]}" exec -n "$NAMESPACE" deployment/drone01-px4-sitl -- \
      /bin/bash -lc \
      'cd /opt/px4; PATH=/opt/px4/bin:$PATH; px4-commander land' \
      >/dev/null 2>&1 || true
  fi
}
trap land_vehicle EXIT

for command in docker k3d kubectl python3; do
  command -v "$command" >/dev/null || {
    echo "Required command not found: $command" >&2
    exit 1
  }
done

mkdir -p "$RESULT_DIR"

cluster_exists() {
  k3d cluster list --no-headers | awk '{print $1}' | grep -qx "$CLUSTER"
}

if [[ "${RESET_E2:-0}" == "1" ]] && cluster_exists; then
  k3d cluster delete "$CLUSTER"
fi

if ! cluster_exists; then
  k3d cluster create --config \
    "$ROOT_DIR/manifests/kubernetes/e2/k3d-cloud-native-e2.yaml"
else
  NODE_COUNT=$("${K[@]}" get nodes --no-headers | wc -l)
  if [[ "$NODE_COUNT" != "1" ]]; then
    echo "Cluster $CLUSTER has an incompatible topology; use RESET_E2=1" >&2
    exit 1
  fi
fi

if [[ "${SKIP_IMAGE_BUILD_IMPORT:-0}" != "1" && "${SKIP_IMAGE_BUILD:-0}" != "1" ]]; then
  docker build -t cloud-native-ros/control-plane:e2 \
    -f "$ROOT_DIR/containers/control-plane/Dockerfile" "$ROOT_DIR"
  docker build -t cloud-native-ros/event-detector:e2-upstream \
    -f "$ROOT_DIR/containers/event-detector/Dockerfile" "$ROOT_DIR"
fi

if [[ "${SKIP_IMAGE_BUILD_IMPORT:-0}" != "1" && "${SKIP_IMAGE_IMPORT:-0}" != "1" ]]; then
  k3d image import -c "$CLUSTER" \
    cloud-native-ros/control-plane:e2 \
    cloud-native-ros/event-detector:e2-upstream \
    microros/micro-ros-agent:humble \
    px4io/px4-sitl:latest \
    ros:humble-ros-base
fi

"${K[@]}" apply -f "$ROOT_DIR/manifests/kubernetes/e2/stack.yaml"
"${K[@]}" apply -f "$ROOT_DIR/manifests/kubernetes/e2/px4-sitl.yaml"
for deployment in \
  application-manager-e2 \
  operational-event-dispatcher-e2 \
  telemetry-event-detector-e2 \
  drone01-microxrce-agent \
  drone01-px4-sitl; do
  "${K[@]}" rollout status "deployment/$deployment" \
    -n "$NAMESPACE" --timeout=180s
done

for _ in $(seq 1 60); do
  if "${K[@]}" exec -n "$NAMESPACE" deployment/telemetry-event-detector-e2 \
    -- /bin/bash -lc \
    'source /ws/install/setup.bash; ROS2CLI_DISABLE_DAEMON=1 timeout 5 ros2 topic echo /fmu/out/vehicle_status_v4 px4_msgs/msg/VehicleStatus --once' \
    >"$RESULT_DIR/initial-vehicle-status.txt" 2>/dev/null; then
    break
  fi
  sleep 2
done
grep -Fq 'arming_state:' "$RESULT_DIR/initial-vehicle-status.txt"

PX4_UID_BEFORE=$("${K[@]}" get pod -n "$NAMESPACE" \
  -l app.kubernetes.io/name=drone01-px4-sitl \
  -o jsonpath='{.items[0].metadata.uid}')
PX4_RESTARTS_BEFORE=$("${K[@]}" get pod -n "$NAMESPACE" \
  -l app.kubernetes.io/name=drone01-px4-sitl \
  -o jsonpath='{.items[0].status.containerStatuses[0].restartCount}')
AGENT_POD_UID_BEFORE=$("${K[@]}" get pod -n "$NAMESPACE" \
  -l app.kubernetes.io/name=drone01-microxrce-agent \
  -o jsonpath='{.items[0].metadata.uid}')

capture_snapshot() {
  local phase=$1
  "${K[@]}" exec -n "$NAMESPACE" deployment/drone01-px4-sitl -- \
    /bin/bash -lc \
    'cd /opt/px4; PATH=/opt/px4/bin:$PATH; px4-listener vehicle_status -n 1' \
    >"$RESULT_DIR/status-$phase.txt"
  "${K[@]}" exec -n "$NAMESPACE" deployment/drone01-px4-sitl -- \
    /bin/bash -lc \
    'cd /opt/px4; PATH=/opt/px4/bin:$PATH; px4-listener vehicle_local_position -n 1' \
    >"$RESULT_DIR/position-$phase.txt"
}

start_or_resume_hover() {
  "${K[@]}" exec -n "$NAMESPACE" deployment/drone01-px4-sitl -- \
    /bin/bash -lc \
    'cd /opt/px4; PATH=/opt/px4/bin:$PATH; px4-commander arm; sleep 1; px4-commander takeoff; px4-commander status' \
    >>"$RESULT_DIR/mission-start.txt" 2>&1 || true
}

: >"$RESULT_DIR/mission-start.txt"
MISSION_ARMED=true
start_or_resume_hover
sleep "$MISSION_SETTLE_SEC"

HOVER_GATE_READY=false
HOVER_GATE_STREAK=0
HOVER_GATE_ATTEMPTS=0
HOVER_GATE_DEADLINE=$((SECONDS + HOVER_GATE_TIMEOUT_SEC))
while (( SECONDS < HOVER_GATE_DEADLINE )); do
  HOVER_GATE_ATTEMPTS=$((HOVER_GATE_ATTEMPTS + 1))
  capture_snapshot gate
  if python3 "$ROOT_DIR/scripts/validate_px4_hover.py" \
    --status-current "$RESULT_DIR/status-gate.txt" \
    --position-current "$RESULT_DIR/position-gate.txt" \
    >"$RESULT_DIR/hover-gate.json"; then
    HOVER_GATE_STREAK=$((HOVER_GATE_STREAK + 1))
    if (( HOVER_GATE_STREAK >= HOVER_GATE_CONSECUTIVE )); then
      HOVER_GATE_READY=true
      break
    fi
  else
    HOVER_GATE_STREAK=0
    if ! grep -Fq '"arming_state": 2' "$RESULT_DIR/hover-gate.json"; then
      start_or_resume_hover
    fi
  fi
  sleep "$HOVER_GATE_INTERVAL_SEC"
done

"${K[@]}" exec -n "$NAMESPACE" deployment/drone01-px4-sitl -- \
  /bin/bash -lc \
  'cd /opt/px4; PATH=/opt/px4/bin:$PATH; px4-commander status' \
  >>"$RESULT_DIR/mission-start.txt" 2>&1 || true

if [[ "$HOVER_GATE_READY" != "true" ]]; then
  "${K[@]}" get pods -n "$NAMESPACE" -o wide >"$RESULT_DIR/pods.txt"
  "${K[@]}" logs -n "$NAMESPACE" deployment/drone01-px4-sitl \
    >"$RESULT_DIR/px4.log" 2>&1 || true
  cat >"$RESULT_DIR/REPORT.md" <<REPORT
# E2/P1 Armed Hover Precondition

| Campo | Valore |
| --- | --- |
| Esito | invalid |
| Validita campione | false |
| Failure phase | precondition |
| Cluster / namespace | $CLUSTER / $NAMESPACE |
| Gate armed hover | false |
| Gate tentativi | $HOVER_GATE_ATTEMPTS |
| Fault iniettato | false |

PX4 non ha raggiunto la precondizione armed/Hold/hover stabile. Il fault non
e' stato iniettato e il campione deve essere escluso dalle metriche di recovery.
REPORT
  echo "E2 armed-hover result: invalid"
  echo "Evidence: $RESULT_DIR"
  exit 2
fi

capture_snapshot before
FAULT_START_NS=$(date +%s%N)
"${K[@]}" exec -n "$NAMESPACE" deployment/drone01-microxrce-agent -- \
  /bin/bash -lc \
  'pid=$(pgrep -x micro_ros_agent); test -n "$pid"; kill -TERM "$pid"'
sleep "$FAULT_SNAPSHOT_DELAY_SEC"
capture_snapshot during

SUCCESS_PATTERN='completed as telemetry_recovered (STABLE)'
FAILURE_PATTERN='completed as telemetry_recovery_failed (ESCALATED)'
OUTCOME_TEXT='recovery_outcome_timeout (ESCALATED)'
FAILURE_PHASE=recovery
for _ in $(seq 1 "$RECOVERY_TIMEOUT_SEC"); do
  DISPATCHER_TAIL=$("${K[@]}" logs -n "$NAMESPACE" \
    deployment/operational-event-dispatcher-e2 --tail=300 2>/dev/null || true)
  if grep -Fq "$SUCCESS_PATTERN" <<<"$DISPATCHER_TAIL"; then
    OUTCOME_TEXT='telemetry_recovered (STABLE)'
    FAILURE_PHASE=none
    break
  fi
  if grep -Fq "$FAILURE_PATTERN" <<<"$DISPATCHER_TAIL"; then
    OUTCOME_TEXT='telemetry_recovery_failed (ESCALATED)'
    break
  fi
  sleep 1
done
RECOVERY_END_NS=$(date +%s%N)
capture_snapshot after

CORRELATION_ID=$("${K[@]}" logs -n "$NAMESPACE" \
  deployment/operational-event-dispatcher-e2 --tail=300 | \
  sed -n 's/.*DeploymentRequest accepted for //p' | tail -n 1)
PX4_UID_AFTER=$("${K[@]}" get pod -n "$NAMESPACE" \
  -l app.kubernetes.io/name=drone01-px4-sitl \
  -o jsonpath='{.items[0].metadata.uid}')
PX4_RESTARTS_AFTER=$("${K[@]}" get pod -n "$NAMESPACE" \
  -l app.kubernetes.io/name=drone01-px4-sitl \
  -o jsonpath='{.items[0].status.containerStatuses[0].restartCount}')
AGENT_POD_UID_AFTER=$("${K[@]}" get pod -n "$NAMESPACE" \
  -l app.kubernetes.io/name=drone01-microxrce-agent \
  -o jsonpath='{.items[0].metadata.uid}')

MISSION_VALID=true
python3 "$ROOT_DIR/scripts/validate_px4_hover.py" \
  --status-before "$RESULT_DIR/status-before.txt" \
  --position-before "$RESULT_DIR/position-before.txt" \
  --status-during "$RESULT_DIR/status-during.txt" \
  --position-during "$RESULT_DIR/position-during.txt" \
  --status-after "$RESULT_DIR/status-after.txt" \
  --position-after "$RESULT_DIR/position-after.txt" \
  >"$RESULT_DIR/mission-continuity.json" || MISSION_VALID=false

"${K[@]}" get nodes -o wide >"$RESULT_DIR/nodes.txt"
"${K[@]}" get pods -n "$NAMESPACE" -o wide >"$RESULT_DIR/pods.txt"
"${K[@]}" get deployments,jobs -n "$NAMESPACE" -o yaml \
  >"$RESULT_DIR/kubernetes-resources.yaml"
"${K[@]}" get events -n "$NAMESPACE" --sort-by=.lastTimestamp \
  >"$RESULT_DIR/kubernetes-events.txt"
"${K[@]}" logs -n "$NAMESPACE" deployment/telemetry-event-detector-e2 \
  >"$RESULT_DIR/event-detector.log" 2>&1
"${K[@]}" logs -n "$NAMESPACE" deployment/application-manager-e2 \
  >"$RESULT_DIR/application-manager.log" 2>&1
"${K[@]}" logs -n "$NAMESPACE" deployment/operational-event-dispatcher-e2 \
  >"$RESULT_DIR/dispatcher.log" 2>&1
"${K[@]}" logs -n "$NAMESPACE" deployment/drone01-px4-sitl \
  >"$RESULT_DIR/px4.log" 2>&1

DIAGNOSTIC_JOBS=$("${K[@]}" get jobs -n "$NAMESPACE" \
  -l app.kubernetes.io/name=telemetry-diagnostics --no-headers | wc -l)
DIAGNOSTIC_JOB_NAME=$("${K[@]}" get jobs -n "$NAMESPACE" -l app.kubernetes.io/name=telemetry-diagnostics -o jsonpath='{.items[0].metadata.name}')
"${K[@]}" logs -n "$NAMESPACE" "job/$DIAGNOSTIC_JOB_NAME" >"$RESULT_DIR/diagnostic-job.log"
DIAGNOSTIC_RECORDS=$(grep -c '"timestamp"' "$RESULT_DIR/diagnostic-job.log" || true)
RECOVERY_MS=$(((RECOVERY_END_NS - FAULT_START_NS) / 1000000))
ALTITUDE_SPREAD=$(python3 -c \
  "import json; print(json.load(open('$RESULT_DIR/mission-continuity.json'))['altitude_spread_m'])" \
  2>/dev/null || echo unknown)

PASS=true
[[ "$MISSION_VALID" == "true" ]] || PASS=false
[[ "$PX4_UID_BEFORE" == "$PX4_UID_AFTER" ]] || PASS=false
[[ "$PX4_RESTARTS_BEFORE" == "$PX4_RESTARTS_AFTER" ]] || PASS=false
[[ "$AGENT_POD_UID_BEFORE" != "$AGENT_POD_UID_AFTER" ]] || PASS=false
[[ -n "$CORRELATION_ID" ]] || PASS=false
(( DIAGNOSTIC_JOBS >= 1 )) || PASS=false
(( DIAGNOSTIC_RECORDS >= 1 )) || PASS=false
[[ "$OUTCOME_TEXT" == "telemetry_recovered (STABLE)" ]] || PASS=false
grep -Fq 'TelemetryHeartbeatLost' "$RESULT_DIR/kubernetes-events.txt" || PASS=false

cat >"$RESULT_DIR/REPORT.md" <<EOF
# E2/P1 Armed Hover Telemetry Recovery

| Campo | Valore |
| --- | --- |
| Esito | $PASS |
| Validita campione | true |
| Failure phase | $FAILURE_PHASE |
| Cluster / namespace | $CLUSTER / $NAMESPACE |
| Missione controllata | armed hover, PX4 Hold |
| Correlation ID | $CORRELATION_ID |
| Recovery end-to-end | $RECOVERY_MS ms |
| Continuita' missione | $MISSION_VALID |
| Variazione massima quota | $ALTITUDE_SPREAD m |
| PX4 UID prima/dopo | $PX4_UID_BEFORE / $PX4_UID_AFTER |
| PX4 restart prima/dopo | $PX4_RESTARTS_BEFORE / $PX4_RESTARTS_AFTER |
| Agent Pod sostituito | $AGENT_POD_UID_BEFORE -> $AGENT_POD_UID_AFTER |
| Job diagnostici | $DIAGNOSTIC_JOBS |
| Record diagnostici acquisiti | $DIAGNOSTIC_RECORDS |
| Outcome | $OUTCOME_TEXT |

Il gate armed-hover era valido prima del fault. Il report combina continuita'
della missione, outcome della policy P1, sostituzione del solo Pod Agent,
Kubernetes Event e completezza della diagnostica.
EOF

"${K[@]}" exec -n "$NAMESPACE" deployment/drone01-px4-sitl -- \
  /bin/bash -lc \
  'cd /opt/px4; PATH=/opt/px4/bin:$PATH; px4-commander land; sleep 12; px4-commander status' \
  >"$RESULT_DIR/mission-end.txt" || true
MISSION_ARMED=false

echo "E2 armed-hover result: $PASS"
echo "Evidence: $RESULT_DIR"
[[ "$PASS" == "true" ]]
