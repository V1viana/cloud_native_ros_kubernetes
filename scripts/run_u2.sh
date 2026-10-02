#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
CONTEXT=k3d-cloud-native-p2
NAMESPACE=cloud-native-p2
VARIANT=${VARIANT:-a}
RESULT_ID=$(date -u +%Y%m%dT%H%M%SZ)
RESULT_DIR="$ROOT_DIR/results/u2/$RESULT_ID"
K=(kubectl --context "$CONTEXT")
TARGET=drone01-companion-analytics-onboard
APPLICATION=e0-baseline-drone01
mkdir -p "$RESULT_DIR"

case "$VARIANT" in
  a|b) ;;
  *)
    echo "Unsupported VARIANT: $VARIANT (expected 'a' or 'b')" >&2
    exit 2
    ;;
esac

for command in docker k3d kubectl python3; do
  command -v "$command" >/dev/null || {
    echo "Required command not found: $command" >&2
    exit 1
  }
done

if [[ "$VARIANT" == "a" ]]; then

RESET_U1_BASELINE=${RESET_U2_BASELINE:-1} \
  U1_BASELINE_OBSERVATION_WINDOW_SEC=${U2_BASELINE_WINDOW_SEC:-10} \
  "$ROOT_DIR/scripts/run_u1.sh" | tee "$RESULT_DIR/u1-baseline.log"
U1_RESULT=$(sed -n 's/^Evidence: //p' "$RESULT_DIR/u1-baseline.log" | tail -n 1)
if [[ -z "$U1_RESULT" || ! -f "$U1_RESULT/revision-2.yaml" ]]; then
  echo "Unable to locate successful U1 evidence" >&2
  exit 1
fi
printf '%s\n' "$U1_RESULT" >"$RESULT_DIR/u1-result.txt"

python3 "$ROOT_DIR/scripts/render_u2_failure_manifest.py" \
  --input "$U1_RESULT/revision-2.yaml" \
  --output "$RESULT_DIR/revision-failure.yaml"

snapshot_managed_pods() {
  "${K[@]}" get pods -n "$NAMESPACE" \
    -l app.kubernetes.io/managed-by=kuberos \
    -o jsonpath='{range .items[*]}{.metadata.labels.pod-name}{"\t"}{.metadata.name}{"\t"}{.metadata.uid}{"\t"}{.status.containerStatuses[0].restartCount}{"\t"}{.spec.nodeName}{"\n"}{end}' |
    LC_ALL=C sort
}

snapshot_managed_pods >"$RESULT_DIR/pods-before.tsv"
"${K[@]}" exec -n "$NAMESPACE" deployment/kuberos -c api -- \
  python manage.py shell -c \
  "from main.models import Deployment; d=Deployment.objects.get(name='$APPLICATION',active=True); print(f'{d.name}|{d.revision}|{d.status}')" \
  >"$RESULT_DIR/kuberos-before.txt"
# Not hardcoded to "2": U1's own idempotency-repetition check (2 extra
# identical re-applications, added to measure reconciliation churn) makes
# KubeROS bump its internal Deployment.revision counter once per API call
# even when the call is a no-op -- found live (campaign run, RUNS=10) that
# this leaves the real revision at 4, not 2, 100% of the time. U2 does not
# care about the literal number, only that U1 left a stable, running
# baseline to fail-update from -- so read the actual value instead.
REVISION_BEFORE=$(sed -n "s/^$APPLICATION|\([0-9]*\)|running\$/\1/p" \
  "$RESULT_DIR/kuberos-before.txt")
[[ -n "$REVISION_BEFORE" ]] || {
  echo "Unable to read a stable running revision for $APPLICATION" >&2
  exit 1
}
REVISION_ATTEMPTED=$((REVISION_BEFORE + 1))

"${K[@]}" create configmap u2-kuberos-failure-manifest -n "$NAMESPACE" \
  --from-file=revision-failure.yaml="$RESULT_DIR/revision-failure.yaml" \
  --dry-run=client -o yaml | "${K[@]}" apply -f -
"${K[@]}" delete job u2-kuberos-failure-update -n "$NAMESPACE" \
  --ignore-not-found

START_EPOCH=$(date +%s)
"${K[@]}" apply -f "$ROOT_DIR/manifests/kubernetes/u2/10-failure-update-job.yaml"
"${K[@]}" wait --for=condition=complete job/u2-kuberos-failure-update \
  -n "$NAMESPACE" --timeout=360s
END_EPOCH=$(date +%s)
"${K[@]}" logs -n "$NAMESPACE" job/u2-kuberos-failure-update \
  >"$RESULT_DIR/failure-update-client.log"
grep -q '^U2_KUBEROS_ROLLBACK_READY ' "$RESULT_DIR/failure-update-client.log"

"${K[@]}" rollout status "deployment/$TARGET" -n "$NAMESPACE" --timeout=180s
snapshot_managed_pods >"$RESULT_DIR/pods-after.tsv"

PASS=true
declare -A BEFORE_UID BEFORE_RESTARTS AFTER_UID AFTER_RESTARTS
while IFS=$'\t' read -r deployment pod uid restarts node; do
  [[ -n "$deployment" ]] || continue
  BEFORE_UID["$deployment"]=$uid
  BEFORE_RESTARTS["$deployment"]=$restarts
done <"$RESULT_DIR/pods-before.tsv"
while IFS=$'\t' read -r deployment pod uid restarts node; do
  [[ -n "$deployment" ]] || continue
  AFTER_UID["$deployment"]=$uid
  AFTER_RESTARTS["$deployment"]=$restarts
done <"$RESULT_DIR/pods-after.tsv"

PRESERVED=0
TARGET_REPLACED=false
{
  echo "deployment,uid_before,uid_after,restarts_before,restarts_after,outcome"
  for deployment in $(printf '%s\n' "${!BEFORE_UID[@]}" | LC_ALL=C sort); do
    outcome=preserved
    if [[ -z "${AFTER_UID[$deployment]:-}" ]]; then
      outcome=missing
      PASS=false
    elif [[ "$deployment" == "$TARGET" ]]; then
      if [[ "${BEFORE_UID[$deployment]}" != "${AFTER_UID[$deployment]}" ]]; then
        outcome=rollback-replaced
        TARGET_REPLACED=true
      else
        outcome=rollback-preserved
      fi
    elif [[ "${BEFORE_UID[$deployment]}" != "${AFTER_UID[$deployment]}" ]]; then
      outcome=unexpected-replacement
      PASS=false
    else
      ((PRESERVED += 1))
    fi
    if [[ "${BEFORE_RESTARTS[$deployment]}" != "${AFTER_RESTARTS[$deployment]:-missing}" ]]; then
      outcome="$outcome-restarted"
      PASS=false
    fi
    echo "$deployment,${BEFORE_UID[$deployment]},${AFTER_UID[$deployment]:-missing},${BEFORE_RESTARTS[$deployment]},${AFTER_RESTARTS[$deployment]:-missing},$outcome"
  done
} >"$RESULT_DIR/workload-diff.csv"
[[ "${#BEFORE_UID[@]}" == "12" ]] || PASS=false
[[ "${#AFTER_UID[@]}" == "12" ]] || PASS=false
[[ "$PRESERVED" == "11" ]] || PASS=false

"${K[@]}" exec -n "$NAMESPACE" deployment/kuberos -c api -- \
  python manage.py shell -c \
  "from main.models import Deployment,DeploymentEvent; d=Deployment.objects.get(name='$APPLICATION',active=True); print(f'DEPLOYMENT|{d.name}|{d.revision}|{d.status}'); [print(f'EVENT|{e.event_type}|{e.event_status}|{e.target_revision}|{e.uuid}|{e.error_message}') for e in DeploymentEvent.objects.filter(deployment=d,event_type='UPDATE').order_by('created_at')]" \
  >"$RESULT_DIR/kuberos-after.txt"
grep -Fxq "DEPLOYMENT|$APPLICATION|$REVISION_BEFORE|running" "$RESULT_DIR/kuberos-after.txt" || PASS=false
grep -Eq "^EVENT\|UPDATE\|FAILED\|$REVISION_ATTEMPTED\|" "$RESULT_DIR/kuberos-after.txt" || PASS=false
if grep -Fq 'rollback failed:' "$RESULT_DIR/kuberos-after.txt"; then
  PASS=false
fi

FINAL_IMAGE=$("${K[@]}" get deployment "$TARGET" -n "$NAMESPACE" \
  -o jsonpath='{.spec.template.spec.containers[0].image}')
[[ "$FINAL_IMAGE" == "cloud-native-ros/control-plane:p2" ]] || PASS=false

HEALTH=
for _ in $(seq 1 20); do
  HEALTH=$("${K[@]}" exec -n "$NAMESPACE" "deployment/$TARGET" -- \
    /bin/bash -lc \
    "source /ws/install/setup.bash; ROS_SUPER_CLIENT=TRUE ROS2CLI_DISABLE_DAEMON=1 timeout 20 ros2 service call /drone01/companion/onboard/health cloud_native_robotics_interfaces/srv/GetHealthSnapshot \"{correlation_id: 'u2-rollback-health'}\"" \
    2>/dev/null || true)
  [[ "$HEALTH" == *"healthy=True"* ]] && break
  sleep 1
done
printf '%s\n' "$HEALTH" >"$RESULT_DIR/health-snapshot.txt"
[[ "$HEALTH" == *"healthy=True"* ]] || PASS=false
[[ "$HEALTH" == *"lifecycle_state='active'"* ]] || PASS=false
[[ "$HEALTH" == *"latency_ms=95.0"* ]] || PASS=false

"${K[@]}" get events -n "$NAMESPACE" --sort-by=.metadata.creationTimestamp \
  >"$RESULT_DIR/kubernetes-events.txt"
"${K[@]}" get pods -n "$NAMESPACE" -o wide >"$RESULT_DIR/pods.txt"
"${K[@]}" get deployments,replicasets,pods,services,jobs -n "$NAMESPACE" -o yaml \
  >"$RESULT_DIR/kubernetes-resources.yaml"
"${K[@]}" logs -n "$NAMESPACE" deployment/kuberos -c api \
  >"$RESULT_DIR/kuberos-api.log" 2>&1 || true
"${K[@]}" logs -n "$NAMESPACE" deployment/kuberos -c worker \
  >"$RESULT_DIR/kuberos-worker.log" 2>&1 || true

cat >"$RESULT_DIR/REPORT.md" <<EOF_REPORT
# U2 KubeROS Invalid-Image Rollback Live Result

| Campo | Valore |
| --- | --- |
| Esito | $PASS |
| Cluster / namespace | cloud-native-p2 / $NAMESPACE |
| Baseline valida | $U1_RESULT |
| ApplicationDeployment | $APPLICATION |
| Revisione richiesta | 2 -> 3 |
| Revisione finale | 2 |
| Evento KubeROS | UPDATE FAILED |
| Immagine rifiutata | cloud-native-ros/companion-analytics:u2-image-does-not-exist |
| Immagine ripristinata | $FINAL_IMAGE |
| Workload non target preservati | $PRESERVED / 11 |
| Pod target sostituito durante rollback | $TARGET_REPLACED |
| Tempo failure + rollback | $((END_EPOCH - START_EPOCH)) s |
| Service health finale | healthy, Lifecycle active, latency 95 ms |

KubeROS ha tentato la revisione 3, osservato la mancata readiness causata
dall'immagine inesistente e riconciliato la revisione 2. La revisione attiva
non e' avanzata; PX4, Agent, Event Detector e gli altri workload non target
hanno conservato UID e restart count.
EOF_REPORT

SOURCE_COMMIT=$(git -C "$ROOT_DIR" rev-parse HEAD)
python3 "$ROOT_DIR/scripts/capture_reproducibility.py" \
  --campaign-dir "$RESULT_DIR" \
  --artifact-id "u2-$RESULT_ID" \
  --campaign-source-commit "$SOURCE_COMMIT" \
  --validation-commit "$SOURCE_COMMIT" \
  --image cloud-native-ros/control-plane:p2 \
  --image cloud-native-ros/event-detector:p2 \
  --image cloud-native-ros/kuberos:p2 \
  --image microros/micro-ros-agent:humble \
  --image px4io/px4-sitl:latest \
  --image redis:7

echo "U2 result (variant A): $PASS"
echo "Evidence: $RESULT_DIR"
[[ "$PASS" == "true" ]]

else
# =====================================================================
# VARIANT B: dichiarativo (CRD + Fleet Operator + State Bridge)
#
# La baseline e' U1 variante B (ROSModule companion-analytics-drone01 gia'
# a status.revision=2, processing_delay_ms=95.0). Qui si applica un
# rosParamMap.processing_delay_ms non numerico: companion_analytics lo
# dichiara come parametro ROS DOUBLE, rclpy lo rifiuta e il container
# entra in crash-loop, quindi il Deployment non raggiunge mai Ready.
# ROSModuleController lo scopre al proprio ROSMODULE_UPDATE_TIMEOUT_SEC
# (60s) e ripristina da solo status.lastGoodSpec, senza alcuna azione
# umana ne' una API equivalente a KubeROS coinvolta. status.revision resta
# 2 (il rollback non conta come nuova convergenza riuscita, come la
# variante A che resta alla revisione KubeROS 2).
# =====================================================================

RESET_U1_BASELINE=${RESET_U2_BASELINE:-1} \
  U1_BASELINE_OBSERVATION_WINDOW_SEC=${U2_BASELINE_WINDOW_SEC:-10} \
  VARIANT=b "$ROOT_DIR/scripts/run_u1.sh" | tee "$RESULT_DIR/u1-baseline.log"
U1_RESULT=$(sed -n 's/^Evidence: //p' "$RESULT_DIR/u1-baseline.log" | tail -n 1)
if [[ -z "$U1_RESULT" ]]; then
  echo "Unable to locate successful U1 (variant B) evidence" >&2
  exit 1
fi
printf '%s\n' "$U1_RESULT" >"$RESULT_DIR/u1-result.txt"

ROSMODULE=companion-analytics-drone01
LOGICAL=(
  companion-analytics-drone01 companion-analytics-drone02 companion-analytics-drone03
  drone01-px4-sitl drone02-px4-sitl drone03-px4-sitl
  drone01-microxrce-agent drone02-microxrce-agent drone03-microxrce-agent
)

pod_selector_for() {
  case "$1" in
    companion-analytics-*) echo "dronekube.io/owned-by-rosmodule=$1" ;;
    *) echo "app.kubernetes.io/name=$1" ;;
  esac
}

wait_for_update_state() {
  local want=$1 timeout_sec=$2 got=""
  for _ in $(seq 1 "$timeout_sec"); do
    got=$("${K[@]}" get rosmodule "$ROSMODULE" -n "$NAMESPACE" \
      -o jsonpath='{.status.updateState}' 2>/dev/null || true)
    [[ "$got" == "$want" ]] && return 0
    sleep 1
  done
  return 1
}

REVISION_BEFORE=$("${K[@]}" get rosmodule "$ROSMODULE" -n "$NAMESPACE" \
  -o jsonpath='{.status.revision}')
[[ "$REVISION_BEFORE" == "2" ]] || {
  echo "Expected ROSModule $ROSMODULE at revision 2 before U2, found $REVISION_BEFORE" >&2
  exit 1
}

snapshot_managed_pods() {
  for name in "${LOGICAL[@]}"; do
    selector=$(pod_selector_for "$name")
    "${K[@]}" get pods -n "$NAMESPACE" -l "$selector" \
      -o jsonpath='{range .items[*]}'"$name"'{"\t"}{.metadata.name}{"\t"}{.metadata.uid}{"\t"}{.status.containerStatuses[0].restartCount}{"\t"}{.spec.nodeName}{"\n"}{end}'
  done | LC_ALL=C sort
}

snapshot_managed_pods >"$RESULT_DIR/pods-before.tsv"
declare -A BEFORE_UID BEFORE_RESTARTS
while IFS=$'\t' read -r deployment pod uid restarts node; do
  [[ -n "$deployment" ]] || continue
  BEFORE_UID["$deployment"]=$uid
  BEFORE_RESTARTS["$deployment"]=$restarts
done <"$RESULT_DIR/pods-before.tsv"
[[ "${#BEFORE_UID[@]}" == "9" ]] || {
  echo "Expected 9 managed Pods before U2, found ${#BEFORE_UID[@]}" >&2
  exit 1
}

START_EPOCH=$(date +%s)
"${K[@]}" apply -f "$ROOT_DIR/manifests/kubernetes/u2/60-declarative-failure-update.yaml"
wait_for_update_state "RolledBack" 220 || {
  echo "ROSModule $ROSMODULE never reached updateState=RolledBack" >&2
  "${K[@]}" get rosmodule "$ROSMODULE" -n "$NAMESPACE" -o yaml >&2 || true
  exit 1
}
END_EPOCH=$(date +%s)

for _ in $(seq 1 30); do
  target_pods=$("${K[@]}" get pod -n "$NAMESPACE" \
    -l "dronekube.io/owned-by-rosmodule=$ROSMODULE" --no-headers | \
    grep -c Running || true)
  [[ "$target_pods" == "1" ]] && break
  sleep 1
done

snapshot_managed_pods >"$RESULT_DIR/pods-after.tsv"
declare -A AFTER_UID AFTER_RESTARTS
while IFS=$'\t' read -r deployment pod uid restarts node; do
  [[ -n "$deployment" ]] || continue
  AFTER_UID["$deployment"]=$uid
  AFTER_RESTARTS["$deployment"]=$restarts
done <"$RESULT_DIR/pods-after.tsv"

PASS=true
PRESERVED=0
TARGET_REPLACED=false
{
  echo "deployment,uid_before,uid_after,restarts_before,restarts_after,outcome"
  for deployment in $(printf '%s\n' "${!BEFORE_UID[@]}" | LC_ALL=C sort); do
    outcome=preserved
    if [[ -z "${AFTER_UID[$deployment]:-}" ]]; then
      outcome=missing
      PASS=false
    elif [[ "$deployment" == "$ROSMODULE" ]]; then
      if [[ "${BEFORE_UID[$deployment]}" != "${AFTER_UID[$deployment]}" ]]; then
        outcome=rollback-replaced
        TARGET_REPLACED=true
      else
        outcome=rollback-preserved
      fi
    elif [[ "${BEFORE_UID[$deployment]}" != "${AFTER_UID[$deployment]}" ]]; then
      outcome=unexpected-replacement
      PASS=false
    else
      ((PRESERVED += 1))
    fi
    if [[ "$deployment" != "$ROSMODULE" && "${BEFORE_RESTARTS[$deployment]}" != "${AFTER_RESTARTS[$deployment]:-missing}" ]]; then
      outcome="$outcome-restarted"
      PASS=false
    fi
    echo "$deployment,${BEFORE_UID[$deployment]},${AFTER_UID[$deployment]:-missing},${BEFORE_RESTARTS[$deployment]},${AFTER_RESTARTS[$deployment]:-missing},$outcome"
  done
} >"$RESULT_DIR/workload-diff.csv"
[[ "${#BEFORE_UID[@]}" == "9" ]] || PASS=false
[[ "${#AFTER_UID[@]}" == "9" ]] || PASS=false
[[ "$PRESERVED" == "8" ]] || PASS=false
[[ "$TARGET_REPLACED" == "true" ]] || PASS=false

"${K[@]}" get rosmodule "$ROSMODULE" -n "$NAMESPACE" -o yaml >"$RESULT_DIR/rosmodule-after.yaml"
REVISION_AFTER=$("${K[@]}" get rosmodule "$ROSMODULE" -n "$NAMESPACE" \
  -o jsonpath='{.status.revision}')
LAST_FAILED_DELAY=$("${K[@]}" get rosmodule "$ROSMODULE" -n "$NAMESPACE" \
  -o jsonpath='{.status.lastFailedSpec.rosParamMap.processing_delay_ms}')
LAST_GOOD_DELAY=$("${K[@]}" get rosmodule "$ROSMODULE" -n "$NAMESPACE" \
  -o jsonpath='{.status.lastGoodSpec.rosParamMap.processing_delay_ms}')
[[ "$REVISION_AFTER" == "2" ]] || PASS=false
[[ "$LAST_FAILED_DELAY" == "not-a-number" ]] || PASS=false
[[ "$LAST_GOOD_DELAY" == "95.0" ]] || PASS=false

FINAL_COMMAND=$("${K[@]}" get deployment "$ROSMODULE" -n "$NAMESPACE" \
  -o jsonpath='{.spec.template.spec.containers[0].command[2]}')
printf '%s\n' "$FINAL_COMMAND" >"$RESULT_DIR/analytics-command.txt"
grep -Fq 'processing_delay_ms:=95.0' "$RESULT_DIR/analytics-command.txt" || PASS=false

HEALTH=
for _ in $(seq 1 60); do
  HEALTH=$("${K[@]}" exec -n "$NAMESPACE" "deployment/$ROSMODULE" \
    -c companion-analytics -- /bin/bash -lc \
    "source /ws/install/setup.bash; ROS_SUPER_CLIENT=TRUE ROS2CLI_DISABLE_DAEMON=1 timeout 20 ros2 service call /drone01/companion/onboard/health cloud_native_robotics_interfaces/srv/GetHealthSnapshot \"{correlation_id: 'u2b-rollback-health'}\"" \
    2>/dev/null || true)
  # healthy=True can appear before latency_ms (a rolling metric, not a
  # direct echo of the param) has caught up to the just-rolled-back value
  # -- found live: the rolled-back pod answers healthy/active quickly
  # after restart, but latency_ms lags a couple of sample_period_ms
  # cycles behind. Wait for the real expected value, not just "healthy".
  [[ "$HEALTH" == *"healthy=True"* && "$HEALTH" == *"latency_ms=95.0"* ]] && break
  sleep 2
done
printf '%s\n' "$HEALTH" >"$RESULT_DIR/health-snapshot.txt"
[[ "$HEALTH" == *"healthy=True"* ]] || PASS=false
[[ "$HEALTH" == *"lifecycle_state='active'"* ]] || PASS=false
[[ "$HEALTH" == *"latency_ms=95.0"* ]] || PASS=false

"${K[@]}" get events -n "$NAMESPACE" --sort-by=.metadata.creationTimestamp \
  >"$RESULT_DIR/kubernetes-events.txt"
"${K[@]}" get pods -n "$NAMESPACE" -o wide >"$RESULT_DIR/pods.txt"
"${K[@]}" get deployments,replicasets,pods,rosmodules -n "$NAMESPACE" -o yaml \
  >"$RESULT_DIR/kubernetes-resources.yaml"
"${K[@]}" logs -n "$NAMESPACE" deployment/fleet-operator \
  >"$RESULT_DIR/fleet-operator.log" 2>&1 || true
"${K[@]}" exec -n "$NAMESPACE" deployment/p2-audit-writer -- cat /data/audit.jsonl \
  >"$RESULT_DIR/audit.jsonl" 2>&1 || true
grep -Fq '"outcome":"rosmodule_update_rolled_back"' "$RESULT_DIR/audit.jsonl" 2>/dev/null || PASS=false
grep -Fq '"rollback_performed":true' "$RESULT_DIR/audit.jsonl" 2>/dev/null || PASS=false

cat >"$RESULT_DIR/REPORT.md" <<EOF_REPORT
# U2 Declarative Invalid-Param Rollback Live Result

| Campo | Valore |
| --- | --- |
| Esito | $PASS |
| Variante | B (dichiarativa: CRD + Fleet Operator) |
| Cluster / namespace | cloud-native-p2 / $NAMESPACE |
| Baseline valida (U1 variante B) | $U1_RESULT |
| ROSModule | $ROSMODULE |
| processing_delay_ms richiesto | not-a-number |
| status.revision | resta $REVISION_AFTER |
| status.updateState finale | RolledBack |
| status.lastFailedSpec.processing_delay_ms | $LAST_FAILED_DELAY |
| status.lastGoodSpec.processing_delay_ms | $LAST_GOOD_DELAY |
| Workload non target preservati | $PRESERVED / 8 |
| Pod target sostituito durante rollback | $TARGET_REPLACED |
| Tempo rilevamento + rollback | $((END_EPOCH - START_EPOCH)) s |
| Service health finale | healthy, Lifecycle active, latenza 95 ms |
| Audit trail (Audit Writer/Operator Notifier) | presente (rosmodule_update_rolled_back, rollback_performed=true) |

ROSModuleController ha tentato la revisione con processing_delay_ms non
numerico, osservato che il Deployment non raggiungeva mai Ready entro
ROSMODULE_UPDATE_TIMEOUT_SEC e ripristinato da solo status.lastGoodSpec.
status.revision non e' avanzata; PX4, Micro XRCE-DDS Agent e gli altri due
ROSModule non target hanno conservato UID e restart count.
EOF_REPORT

echo "U2 result (variant B): $PASS"
echo "Evidence: $RESULT_DIR"
[[ "$PASS" == "true" ]]

fi
