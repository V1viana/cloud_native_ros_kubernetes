#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
CONTEXT=k3d-cloud-native-p2
NAMESPACE=cloud-native-p2
RESULT_ID=$(date -u +%Y%m%dT%H%M%SZ)
RESULT_DIR="$ROOT_DIR/results/u2/$RESULT_ID"
K=(kubectl --context "$CONTEXT")
TARGET=drone01-companion-analytics-onboard
APPLICATION=e0-baseline-drone01
mkdir -p "$RESULT_DIR"

for command in docker k3d kubectl python3; do
  command -v "$command" >/dev/null || {
    echo "Required command not found: $command" >&2
    exit 1
  }
done

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
grep -Fxq "$APPLICATION|2|running" "$RESULT_DIR/kuberos-before.txt"

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
grep -Fxq "DEPLOYMENT|$APPLICATION|2|running" "$RESULT_DIR/kuberos-after.txt" || PASS=false
grep -Eq '^EVENT\|UPDATE\|FAILED\|3\|' "$RESULT_DIR/kuberos-after.txt" || PASS=false
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

echo "U2 result: $PASS"
echo "Evidence: $RESULT_DIR"
[[ "$PASS" == "true" ]]
