#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
CLUSTER=cloud-native-e2
CONTEXT=k3d-cloud-native-e2
NAMESPACE=cloud-native-e2
VARIANT=${VARIANT:-a}
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

case "$VARIANT" in
  a|b) ;;
  *)
    echo "Unsupported VARIANT: $VARIANT (expected 'a' or 'b')" >&2
    exit 2
    ;;
esac

land_vehicle() {
  if [[ "$MISSION_ARMED" == "true" ]]; then
    "${K[@]}" exec -n "$NAMESPACE" deployment/drone01-px4-sitl -- \
      /bin/bash -lc \
      'cd /opt/px4; PATH=/opt/px4/bin:$PATH; px4-commander land' \
      >/dev/null 2>&1 || true
  fi
}
# R9, decision D5-b (Viviana, 2026-09-26): mission continuity from PX4's own
# vehicle_status, sampled continuously from inside the PX4 container, the same
# in A and B -- E1's observer reads PX4 over DDS through the Agent this fault
# stops. A read every STATUS_SAMPLER_PERIOD_SEC plus px4-listener's own time,
# each prefixed with the container's wall clock (the host's kernel clock);
# judged by scripts/px4_status_continuity.py over [Hold confirmed by the hover
# gate, recovery confirmed], the fault included, before the landing. The
# snapshots (validate_px4_hover.py) stay, as diagnostics only.
STATUS_SAMPLER_PERIOD_SEC=${E2_STATUS_SAMPLER_PERIOD_SEC:-0.2}
STATUS_SAMPLER_MAX_SEC=${E2_STATUS_SAMPLER_MAX_SEC:-600}
STATUS_SAMPLER_PID=""
STATUS_CONTINUITY=inconclusive
start_status_sampler() {
  "${K[@]}" exec -n "$NAMESPACE" deployment/drone01-px4-sitl -- /bin/bash -lc \
    "cd /opt/px4; PATH=/opt/px4/bin:\$PATH; end=\$((\$(date +%s) + $STATUS_SAMPLER_MAX_SEC)); while [ \$(date +%s) -lt \$end ]; do echo \"READ \$(date +%s%N)\"; px4-listener vehicle_status -n 1; sleep $STATUS_SAMPLER_PERIOD_SEC; done" \
    >"$RESULT_DIR/px4-status-samples.txt" 2>&1 &
  STATUS_SAMPLER_PID=$!
}
stop_status_sampler() {
  if [[ -n "$STATUS_SAMPLER_PID" ]]; then
    kill "$STATUS_SAMPLER_PID" 2>/dev/null || true
    wait "$STATUS_SAMPLER_PID" 2>/dev/null || true
    STATUS_SAMPLER_PID=""
  fi
}
judge_status_continuity() {  # $1 = window start ns, $2 = window end ns
  python3 "$ROOT_DIR/scripts/px4_status_continuity.py" "$RESULT_DIR/px4-status-samples.txt" \
    --window-start-ns "$1" --window-end-ns "$2" >"$RESULT_DIR/px4-status-continuity.json" || true
  STATUS_CONTINUITY=$(python3 -c "import json; print(json.load(open('$RESULT_DIR/px4-status-continuity.json'))['verdict'])" 2>/dev/null || echo inconclusive)
  STATUS_CONTINUITY_DETAIL=$(python3 -c "
import json
d = json.load(open('$RESULT_DIR/px4-status-continuity.json'))
print(f\"{d['reasons']}; {d['reads']} letture, {d['new_samples']} campioni nuovi, {d['repeated_reads']} ripetute, \"
      f\"{d['failed_reads']} fallite; buco max fra letture {d['max_read_gap_ms']} ms (limite {d['read_gap_limit_ms']}), \"
      f\"periodo mediano della sorgente {d['median_source_period_ms']} ms (limite {d['source_period_limit_ms']}), \"
      f\"buco max fra campioni {d['max_gap_ms']} ms (limite {d['gap_limit_ms']}); finestra {d['window_sec']} s\")
" 2>/dev/null || echo "valutazione non disponibile")
}
# R9, decision D4b (Viviana, 2026-09-26): the project's Audit Writer and
# Operator Notifier in E2, both variants, rendered from
# manifests/kubernetes/p2/25-observability.yaml (scripts/render_observability.py)
# with the image tag this cluster imports. Each variant keeps its declared
# delivery contract (A: outbox; B: best-effort, V4/EV23); this run checks the
# recording with the writer available.
OBSERVABILITY_READY=false
OBSERVABILITY_BEFORE_FAULT="non controllata"
AUDIT_INCIDENT="non controllato"
apply_observability() {  # $1 = control-plane image, $2 = --with-outbox or empty
  python3 "$ROOT_DIR/scripts/render_observability.py" --namespace "$NAMESPACE" \
    --image "$1" --node-role onboard ${2:-} >"$RESULT_DIR/observability.yaml"
  "${K[@]}" apply -f "$RESULT_DIR/observability.yaml"
}
wait_observability() {
  local deployment
  for deployment in p2-audit-writer p2-operator-notifier p2-platform-observer; do
    "${K[@]}" rollout status "deployment/$deployment" -n "$NAMESPACE" --timeout=180s
  done
}
check_observability_before_fault() {  # audit PVC Bound, writer and notifier with ready endpoints
  local pvc writer notifier
  pvc=$("${K[@]}" get pvc p2-audit-data -n "$NAMESPACE" -o jsonpath='{.status.phase}' 2>/dev/null || true)
  writer=$("${K[@]}" get endpoints p2-audit-writer -n "$NAMESPACE" \
    -o jsonpath='{.subsets[*].addresses[*].ip}' 2>/dev/null || true)
  notifier=$("${K[@]}" get endpoints p2-operator-notifier -n "$NAMESPACE" \
    -o jsonpath='{.subsets[*].addresses[*].ip}' 2>/dev/null || true)
  OBSERVABILITY_BEFORE_FAULT="PVC ${pvc:-assente}, writer ${writer:-senza endpoint}, notifier ${notifier:-senza endpoint}"
  [[ "$pvc" == "Bound" && -n "$writer" && -n "$notifier" ]] && OBSERVABILITY_READY=true || true
}
check_incident_audit() {  # $1 policy, $2 correlation id, $3 event type: up to 30s, as A waits for its records
  local _
  for _ in $(seq 1 30); do
    "${K[@]}" exec -n "$NAMESPACE" deployment/p2-audit-writer -- cat /data/audit.jsonl \
      >"$RESULT_DIR/audit.jsonl" 2>/dev/null || true
    AUDIT_INCIDENT=$(python3 "$ROOT_DIR/scripts/scenario_checks.py" incident "$RESULT_DIR/audit.jsonl" \
      "$1" "$2" "$3" telemetry_recovered false) && return 0
    sleep 1
  done
  return 0
}
cleanup() {
  stop_status_sampler
  land_vehicle
}
trap cleanup EXIT

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
# datastore of the bench, embedded etcd (R14 target, docs/ETCD_GATE_PREREGISTRATION.md):
# fail-closed, before any workload or injection
python3 "$ROOT_DIR/scripts/datastore_check.py" check "$CLUSTER" "$RESULT_DIR" \
  || { echo "datastore of $CLUSTER not verified as embedded etcd: stopping before any workload" >&2; exit 2; }

if [[ "$VARIANT" == "a" ]]; then

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
apply_observability cloud-native-ros/control-plane:e2 --with-outbox
"${K[@]}" apply -f "$ROOT_DIR/manifests/kubernetes/e2/px4-sitl.yaml"
wait_observability
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
start_status_sampler

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

WINDOW_START_NS=$(date +%s%N)   # Hold confirmed by the hover gate
capture_snapshot before
check_observability_before_fault
FAULT_START_NS=$(date +%s%N)
# SIGSTOP, not SIGTERM: matches variant B's own fix (see run_e2.sh's other
# branch) for the identical race -- kubelet's ordinary "PID 1 exited"
# restart-on-crash can complete in ~2.7s in this environment, under the
# 3.0s threshold both variants use to declare the telemetry link lost,
# silently curing the fault via an unrelated mechanism before either
# variant's own remediation policy can ever fire. That made variant A's
# own test flaky under the same conditions that motivated variant B's fix
# (found on review: A never received the equivalent fix, so the two
# variants were being compared against genuinely different fault classes
# -- "crashed, maybe already auto-restarted" vs. "hung, never auto-
# recovers"). A stopped (not killed) process is not "exited" from the
# container runtime's point of view, so kubelet does not restart it on its
# own -- only this scenario's own remediation path can.
"${K[@]}" exec -n "$NAMESPACE" deployment/drone01-microxrce-agent -- \
  /bin/bash -lc \
  'pid=$(pgrep -x micro_ros_agent); test -n "$pid"; kill -STOP "$pid"'
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
sleep 3                           # a few reads after the window, before the landing
stop_status_sampler
judge_status_continuity "$WINDOW_START_NS" "$RECOVERY_END_NS"

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
# R9, decision D4b: the incident in the audit trail (started, completed with
# the observed outcome, notification, all with the DeploymentRequest's id) and
# the Application Manager's own evidence, its Event labelled with that id.
check_incident_audit P1 "$CORRELATION_ID" TelemetryHeartbeatLost
"${K[@]}" get events -n "$NAMESPACE" -o json >"$RESULT_DIR/events.json" 2>/dev/null || true
MANAGER_EVENT=$(python3 "$ROOT_DIR/scripts/scenario_checks.py" labelled-event "$RESULT_DIR/events.json" \
  TelemetryHeartbeatLost cloud-native-robotics.io/correlation-id "$CORRELATION_ID" || true)

PASS=true
# D5-b: the continuous uORB sampling judges continuity; inconclusive makes the
# run inconclusive (below), not failed. MISSION_VALID (snapshots) is diagnostics.
[[ "$STATUS_CONTINUITY" != "false" ]] || PASS=false
[[ "$PX4_UID_BEFORE" == "$PX4_UID_AFTER" ]] || PASS=false
[[ "$PX4_RESTARTS_BEFORE" == "$PX4_RESTARTS_AFTER" ]] || PASS=false
[[ "$AGENT_POD_UID_BEFORE" != "$AGENT_POD_UID_AFTER" ]] || PASS=false
[[ -n "$CORRELATION_ID" ]] || PASS=false
(( DIAGNOSTIC_JOBS >= 1 )) || PASS=false
(( DIAGNOSTIC_RECORDS >= 1 )) || PASS=false
[[ "$OUTCOME_TEXT" == "telemetry_recovered (STABLE)" ]] || PASS=false
grep -Fq 'TelemetryHeartbeatLost' "$RESULT_DIR/kubernetes-events.txt" || PASS=false
[[ "$OBSERVABILITY_READY" == "true" ]] || PASS=false
[[ "$AUDIT_INCIDENT" == PASS* ]] || PASS=false
[[ "$MANAGER_EVENT" == PASS* ]] || PASS=false

VERDICT=$PASS
[[ "$PASS" == "true" && "$STATUS_CONTINUITY" == "inconclusive" ]] && VERDICT=inconclusive
cat >"$RESULT_DIR/REPORT.md" <<EOF
# E2/P1 Armed Hover Telemetry Recovery

| Campo | Valore |
| --- | --- |
| Esito | $VERDICT |
| Validita campione | true |
| Failure phase | $FAILURE_PHASE |
| Cluster / namespace | $CLUSTER / $NAMESPACE |
| Missione controllata | armed hover, PX4 Hold |
| Correlation ID | $CORRELATION_ID |
| Event dell'Application Manager (correlato) | $MANAGER_EVENT |
| Recovery end-to-end | $RECOVERY_MS ms |
| Continuita' missione (campionamento uORB continuo) | $STATUS_CONTINUITY |
| Campionamento | $STATUS_CONTINUITY_DETAIL |
| Istantanee prima/durante/dopo (diagnostica) | $MISSION_VALID |
| Osservabilita' prima del guasto | $OBSERVABILITY_BEFORE_FAULT |
| Incidente nell'audit (correlato) | $AUDIT_INCIDENT |
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

echo "E2 armed-hover result (variant A): $VERDICT"
echo "Evidence: $RESULT_DIR"
[[ "$VERDICT" == "inconclusive" ]] && exit 2
[[ "$PASS" == "true" ]]

else
# =====================================================================
# VARIANT B: dichiarativo (CRD + Fleet Operator + State Bridge)
#
# Stesso cluster single-node, stessa missione armed-hover e stesso
# meccanismo di guasto (kill -STOP sul processo reale micro_ros_agent,
# vedi variante A piu' sopra per il perche' non e' kill -TERM) di
# variante A -- cambia solo cosa rileva il guasto e come rimedia:
# AdaptationController legge status.metrics.telemetry_heartbeat_age_sec
# (riportato dallo State Bridge in sidecar a companion-analytics-drone01,
# l'unico ROSModule di questo scenario) invece del plugin Event Detector +
# Application Manager, e reagisce con action.type: RestartComponent
# (elimina il Pod dell'Agent, lascia che il Deployment lo ricrei, crea un
# Job diagnostico) invece del PATCH imperativo.
# =====================================================================

if [[ "${SKIP_IMAGE_BUILD_IMPORT:-0}" != "1" && "${SKIP_IMAGE_BUILD:-0}" != "1" ]]; then
  # Tagged :p2, not :e2: k8s_workloads.py's _resolve_bundle_image() reads
  # config/project_images.json's source_reference verbatim, a single
  # catalog shared by every scenario, hardcoded to :p2 for the
  # control-plane/state-bridge bundles regardless of which cluster is
  # running -- found live (ImagePullBackOff, the Deployment asking for a
  # tag that was never built into this cluster at all). E0's own variant B
  # already made the same choice for the same reason.
  docker build -t cloud-native-ros/control-plane:p2 \
    -f "$ROOT_DIR/containers/control-plane/Dockerfile" "$ROOT_DIR"
  docker build -t cloud-native-ros/fleet-operator:e2 \
    -f "$ROOT_DIR/containers/fleet-operator/Dockerfile" "$ROOT_DIR"
  docker build -t cloud-native-ros/state-bridge:p2 \
    -f "$ROOT_DIR/containers/state-bridge/Dockerfile" "$ROOT_DIR"
fi

if [[ "${SKIP_IMAGE_BUILD_IMPORT:-0}" != "1" && "${SKIP_IMAGE_IMPORT:-0}" != "1" ]]; then
  k3d image import -c "$CLUSTER" \
    cloud-native-ros/control-plane:p2 \
    cloud-native-ros/fleet-operator:e2 \
    cloud-native-ros/state-bridge:p2 \
    microros/micro-ros-agent:humble \
    px4io/px4-sitl:latest
fi

DISCOVERY_SERVER_ADDRESS=$("${K[@]}" get node k3d-cloud-native-e2-server-0 \
  -o jsonpath='{.status.addresses[?(@.type=="InternalIP")].address}')
RENDERED_DIR="$RESULT_DIR/rendered"
mkdir -p "$RENDERED_DIR"
sed -e "s/__E2_DISCOVERY_ADDRESS__/$DISCOVERY_SERVER_ADDRESS/g" \
  "$ROOT_DIR/manifests/kubernetes/e2/50-declarative-control-plane.yaml" \
  >"$RENDERED_DIR/50-declarative-control-plane.yaml"
sed -e "s/__E2_DISCOVERY_ADDRESS__/$DISCOVERY_SERVER_ADDRESS/g" \
  "$ROOT_DIR/manifests/kubernetes/e2/40-shared-infra-declarative.yaml" \
  >"$RENDERED_DIR/40-shared-infra-declarative.yaml"

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
apply_observability cloud-native-ros/control-plane:p2
"${K[@]}" apply -f "$ROOT_DIR/manifests/kubernetes/e2/10-discovery-declarative.yaml"
"${K[@]}" apply -f "$RENDERED_DIR/50-declarative-control-plane.yaml"
"${K[@]}" rollout status deployment/e2-fastdds-discovery -n "$NAMESPACE" --timeout=180s
"${K[@]}" rollout status deployment/fleet-operator -n "$NAMESPACE" --timeout=120s
wait_observability

"${K[@]}" apply -f "$RENDERED_DIR/40-shared-infra-declarative.yaml"
"${K[@]}" rollout status deployment/drone01-px4-sitl -n "$NAMESPACE" --timeout=180s
"${K[@]}" rollout status deployment/drone01-microxrce-agent -n "$NAMESPACE" --timeout=180s

"${K[@]}" apply -f "$ROOT_DIR/manifests/kubernetes/e2/60-declarative-workload.yaml"
for _ in $(seq 1 60); do
  STATE=$("${K[@]}" get rosmodule companion-analytics-drone01 -n "$NAMESPACE" \
    -o jsonpath='{.status.observedLifecycleState}' 2>/dev/null || true)
  [[ "$STATE" == "Active" ]] && break
  sleep 2
done
[[ "$STATE" == "Active" ]] || {
  echo "companion-analytics-drone01 never reached Active" >&2
  "${K[@]}" get rosmodule companion-analytics-drone01 -n "$NAMESPACE" -o yaml >&2 || true
  exit 1
}
"${K[@]}" apply -f "$ROOT_DIR/manifests/kubernetes/e2/70-declarative-adaptationpolicy.yaml"

for _ in $(seq 1 60); do
  # ros2 daemon stop first: found live that `ros2 run` auto-spawns a
  # background ros2cli daemon on container start, as a courtesy for later
  # CLI calls -- and this one came up stuck (its internal rclpy context
  # never became ok(), likely a race with the discovery server not being
  # reachable yet at that exact moment), poisoning every later `ros2` CLI
  # invocation with an XML-RPC fault regardless of ROS2CLI_DISABLE_DAEMON=1
  # (that flag only stops a NEW daemon from being spawned, it does not
  # bypass reusing one already running). Harmless no-op if none is running.
  "${K[@]}" exec -n "$NAMESPACE" deployment/companion-analytics-drone01 \
    -c companion-analytics -- /bin/bash -lc \
    'source /ws/install/setup.bash; ros2 daemon stop' >/dev/null 2>&1 || true
  if "${K[@]}" exec -n "$NAMESPACE" deployment/companion-analytics-drone01 \
    -c companion-analytics -- /bin/bash -lc \
    'source /ws/install/setup.bash; ROS2CLI_DISABLE_DAEMON=1 timeout 5 ros2 topic echo /drone01/fmu/out/vehicle_status_v4 px4_msgs/msg/VehicleStatus --once' \
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
start_status_sampler

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
# E2/P1 Declarative Armed Hover Precondition

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
  echo "E2 armed-hover result (variant B): invalid"
  echo "Evidence: $RESULT_DIR"
  exit 2
fi

WINDOW_START_NS=$(date +%s%N)   # Hold confirmed by the hover gate
capture_snapshot before
check_observability_before_fault
FAULT_START_NS=$(date +%s%N)
# SIGSTOP, not SIGTERM -- same fault, same reasoning as variant A's own
# injection above (kubelet's restart-on-crash racing the 3.0s detection
# threshold); kept identical between variants on purpose, see there.
"${K[@]}" exec -n "$NAMESPACE" deployment/drone01-microxrce-agent -- \
  /bin/bash -lc \
  'pid=$(pgrep -x micro_ros_agent); test -n "$pid"; kill -STOP "$pid"'
sleep "$FAULT_SNAPSHOT_DELAY_SEC"
capture_snapshot during

OUTCOME_TEXT='remediation_timeout (ESCALATED)'
FAILURE_PHASE=recovery
for _ in $(seq 1 "$RECOVERY_TIMEOUT_SEC"); do
  # R9, decision D2: record the State Bridge's report cadence and the policy's
  # progress (reported in the REPORT, not judged).
  "${K[@]}" get adaptationpolicy/telemetry-heartbeat-e2 rosmodule/companion-analytics-drone01 \
    -n "$NAMESPACE" -o json 2>/dev/null | python3 -c '
import json, sys, time
try:
    items = json.load(sys.stdin)["items"]
except (ValueError, KeyError):
    items = []
policy = next((i for i in items if i["kind"] == "AdaptationPolicy"), {}).get("status", {})
module = next((i for i in items if i["kind"] == "ROSModule"), {}).get("status", {})
print(json.dumps({"t": time.time(), "state": policy.get("state"),
                  "recovery_windows": policy.get("consecutiveRecoveryWindows"),
                  "observed": module.get("metricsObservedTime"),
                  "heartbeat_age_sec": (module.get("metrics") or {}).get("telemetry_heartbeat_age_sec")}))' \
    >>"$RESULT_DIR/policy-samples.jsonl" || true
  POLICY_STATE=$("${K[@]}" get adaptationpolicy telemetry-heartbeat-e2 -n "$NAMESPACE" \
    -o jsonpath='{.status.state}' 2>/dev/null || true)
  if [[ "$POLICY_STATE" == "Recovered" ]]; then
    OUTCOME_TEXT='telemetry_recovered (STABLE)'
    FAILURE_PHASE=none
    break
  fi
  if [[ "$POLICY_STATE" == "Escalated" ]]; then
    OUTCOME_TEXT='telemetry_recovery_failed (ESCALATED)'
    break
  fi
  sleep 1
done
RECOVERY_END_NS=$(date +%s%N)
capture_snapshot after
sleep 3                           # a few reads after the window, before the landing
stop_status_sampler
judge_status_continuity "$WINDOW_START_NS" "$RECOVERY_END_NS"

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
"${K[@]}" get deployments,jobs,rosmodules,adaptationpolicies -n "$NAMESPACE" -o yaml \
  >"$RESULT_DIR/kubernetes-resources.yaml"
"${K[@]}" get adaptationpolicy telemetry-heartbeat-e2 -n "$NAMESPACE" -o yaml \
  >"$RESULT_DIR/adaptationpolicy-after.yaml"
"${K[@]}" logs -n "$NAMESPACE" deployment/fleet-operator \
  >"$RESULT_DIR/fleet-operator.log" 2>&1 || true
"${K[@]}" logs -n "$NAMESPACE" deployment/drone01-px4-sitl \
  >"$RESULT_DIR/px4.log" 2>&1 || true

DIAGNOSTIC_JOB_NAME=$("${K[@]}" get adaptationpolicy telemetry-heartbeat-e2 -n "$NAMESPACE" \
  -o jsonpath='{.status.diagnosticJobName}')
DIAGNOSTIC_JOBS=$("${K[@]}" get jobs -n "$NAMESPACE" \
  -l app.kubernetes.io/name=telemetry-diagnostics --no-headers | wc -l)
"${K[@]}" logs -n "$NAMESPACE" "job/$DIAGNOSTIC_JOB_NAME" >"$RESULT_DIR/diagnostic-job.log" 2>&1 || true
DIAGNOSTIC_RECORDS=$(grep -c '"timestamp"' "$RESULT_DIR/diagnostic-job.log" || true)
RECOVERY_MS=$(((RECOVERY_END_NS - FAULT_START_NS) / 1000000))
ALTITUDE_SPREAD=$(python3 -c \
  "import json; print(json.load(open('$RESULT_DIR/mission-continuity.json'))['altitude_spread_m'])" \
  2>/dev/null || echo unknown)
# R9, decision D4 (G4): the response to the fault, correlated across the
# policy's RemediationStarted Event, its status and the restart annotation on
# the Agent's Deployment, all after the fault; it attests the response, not
# the detection Event variant A's Application Manager emits with its action.
# Decision D4b: plus the incident in the audit trail (started, completed with
# the observed outcome, notification, all with the policy's correlationId).
"${K[@]}" get adaptationpolicy telemetry-heartbeat-e2 -n "$NAMESPACE" -o json \
  >"$RESULT_DIR/adaptationpolicy-after.json" 2>/dev/null || true
"${K[@]}" get deployment drone01-microxrce-agent -n "$NAMESPACE" -o json \
  >"$RESULT_DIR/agent-deployment.json" 2>/dev/null || true
"${K[@]}" get events -n "$NAMESPACE" -o json >"$RESULT_DIR/events.json" 2>/dev/null || true
FAULT_START_EPOCH=$(python3 -c "print($FAULT_START_NS / 1e9)")
REMEDIATION_CHECK=$(python3 "$ROOT_DIR/scripts/scenario_checks.py" remediation "$RESULT_DIR/events.json" \
  "$RESULT_DIR/adaptationpolicy-after.json" "$RESULT_DIR/agent-deployment.json" "$FAULT_START_EPOCH" || true)
B_CORRELATION_ID=$(python3 -c "import json; print(json.load(open('$RESULT_DIR/adaptationpolicy-after.json')).get('status', {}).get('correlationId', ''))" 2>/dev/null || true)
check_incident_audit telemetry-heartbeat-e2 "$B_CORRELATION_ID" RestartComponent
CADENCE=$(python3 "$ROOT_DIR/scripts/scenario_checks.py" cadence "$RESULT_DIR/policy-samples.jsonl" \
  "$FAULT_START_EPOCH" | cut -d'|' -f2- || true)
RESTART_AFTER=$(python3 -c "
import json
from datetime import datetime
s = json.load(open('$RESULT_DIR/adaptationpolicy-after.json')).get('status', {}).get('restartedAt')
print(f\"{datetime.fromisoformat(s.replace('Z', '+00:00')).timestamp() - $FAULT_START_EPOCH:.1f}s\" if s else 'n/d')
" 2>/dev/null || echo n/d)

PASS=true
# D5-b: the continuous uORB sampling judges continuity; inconclusive makes the
# run inconclusive (below), not failed. MISSION_VALID (snapshots) is diagnostics.
[[ "$STATUS_CONTINUITY" != "false" ]] || PASS=false
[[ "$PX4_UID_BEFORE" == "$PX4_UID_AFTER" ]] || PASS=false
[[ "$PX4_RESTARTS_BEFORE" == "$PX4_RESTARTS_AFTER" ]] || PASS=false
[[ "$AGENT_POD_UID_BEFORE" != "$AGENT_POD_UID_AFTER" ]] || PASS=false
[[ "$REMEDIATION_CHECK" == PASS* ]] || PASS=false
[[ "$OBSERVABILITY_READY" == "true" ]] || PASS=false
[[ "$AUDIT_INCIDENT" == PASS* ]] || PASS=false
[[ -n "$DIAGNOSTIC_JOB_NAME" ]] || PASS=false
(( DIAGNOSTIC_JOBS >= 1 )) || PASS=false
(( DIAGNOSTIC_RECORDS >= 1 )) || PASS=false
[[ "$OUTCOME_TEXT" == "telemetry_recovered (STABLE)" ]] || PASS=false
# Not checked here: a lingering "ThresholdExceeded" reason on the Adapting
# condition. upsert_condition() replaces that condition type's entry in
# place rather than appending a new one (same for every controller in
# this operator, see conditions.py) -- by the time recovery finishes, its
# reason is legitimately "Recovered", not "ThresholdExceeded" anymore.
# That the fault was ever detected is already proven more directly by the
# diagnostic Job's existence and by the STABLE outcome itself.

VERDICT=$PASS
[[ "$PASS" == "true" && "$STATUS_CONTINUITY" == "inconclusive" ]] && VERDICT=inconclusive
cat >"$RESULT_DIR/REPORT.md" <<EOF
# E2/P1 Declarative Armed Hover Telemetry Recovery

| Campo | Valore |
| --- | --- |
| Esito | $VERDICT |
| Variante | B (dichiarativa: CRD + Fleet Operator) |
| Validita campione | true |
| Failure phase | $FAILURE_PHASE |
| Cluster / namespace | $CLUSTER / $NAMESPACE |
| Missione controllata | armed hover, PX4 Hold |
| Recovery end-to-end | $RECOVERY_MS ms |
| Continuita' missione (campionamento uORB continuo) | $STATUS_CONTINUITY |
| Campionamento | $STATUS_CONTINUITY_DETAIL |
| Istantanee prima/durante/dopo (diagnostica) | $MISSION_VALID |
| Osservabilita' prima del guasto | $OBSERVABILITY_BEFORE_FAULT |
| Incidente nell'audit (correlato) | $AUDIT_INCIDENT |
| Variazione massima quota | $ALTITUDE_SPREAD m |
| PX4 UID prima/dopo | $PX4_UID_BEFORE / $PX4_UID_AFTER |
| PX4 restart prima/dopo | $PX4_RESTARTS_BEFORE / $PX4_RESTARTS_AFTER |
| Agent Pod sostituito | $AGENT_POD_UID_BEFORE -> $AGENT_POD_UID_AFTER |
| Job diagnostici | $DIAGNOSTIC_JOBS |
| Job diagnostico | $DIAGNOSTIC_JOB_NAME |
| Record diagnostici acquisiti | $DIAGNOSTIC_RECORDS |
| Outcome | $OUTCOME_TEXT |
| Risposta correlata (Event, status, riavvio dell'Agent) | ${REMEDIATION_CHECK} |
| Riavvio dell'Agent (restartedAt) dal guasto | $RESTART_AFTER |
| Cadenza dei report e tempi (riportati, non giudicati) | ${CADENCE} |

Il gate armed-hover era valido prima del fault. AdaptationController ha
rilevato la perdita di telemetria tramite status.metrics.
telemetry_heartbeat_age_sec (riportato dallo State Bridge), riavviato il
Deployment dell'Agent (rollout, come kubectl rollout restart) e creato un Job
diagnostico.
Il report combina continuita' della missione, outcome della policy P1
dichiarativa, sostituzione del solo Pod Agent e completezza della
diagnostica.
EOF

"${K[@]}" exec -n "$NAMESPACE" deployment/drone01-px4-sitl -- \
  /bin/bash -lc \
  'cd /opt/px4; PATH=/opt/px4/bin:$PATH; px4-commander land; sleep 12; px4-commander status' \
  >"$RESULT_DIR/mission-end.txt" || true
MISSION_ARMED=false

echo "E2 armed-hover result (variant B): $VERDICT"
echo "Evidence: $RESULT_DIR"
[[ "$VERDICT" == "inconclusive" ]] && exit 2
[[ "$PASS" == "true" ]]

fi
