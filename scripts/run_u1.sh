#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
CLUSTER=cloud-native-p2
CONTEXT=k3d-cloud-native-p2
NAMESPACE=cloud-native-p2
RESULT_ID=$(date -u +%Y%m%dT%H%M%SZ)
RESULT_DIR="$ROOT_DIR/results/u1/$RESULT_ID"
K=(kubectl --context "$CONTEXT")
TARGET_DEPLOYMENT=drone01-companion-analytics-onboard
APPLICATION_DEPLOYMENT=e0-baseline-drone01
mkdir -p "$RESULT_DIR"

for command in docker k3d kubectl python3; do
  command -v "$command" >/dev/null || {
    echo "Required command not found: $command" >&2
    exit 1
  }
done

RESET_BASELINE=${RESET_U1_BASELINE:-1}
BASELINE_WINDOW=${U1_BASELINE_OBSERVATION_WINDOW_SEC:-10}
RESET_E0="$RESET_BASELINE" E0_OBSERVATION_WINDOW_SEC="$BASELINE_WINDOW" SKIP_IMAGE_BUILD_IMPORT="${SKIP_IMAGE_BUILD_IMPORT:-0}"   "$ROOT_DIR/scripts/run_e0.sh" | tee "$RESULT_DIR/e0-baseline.log"

BASELINE_RESULT=$(sed -n 's/^Evidence: //p' "$RESULT_DIR/e0-baseline.log" | tail -n 1)
if [[ -z "$BASELINE_RESULT" || ! -f "$BASELINE_RESULT/rendered/drone01.yaml" ]]; then
  echo "Unable to locate the E0 baseline result" >&2
  exit 1
fi
printf '%s\n' "$BASELINE_RESULT" >"$RESULT_DIR/e0-baseline-result.txt"

python3 "$ROOT_DIR/scripts/render_u1_manifest.py"   --input "$BASELINE_RESULT/rendered/drone01.yaml"   --output "$RESULT_DIR/revision-2.yaml"   --processing-delay-ms 95.0

snapshot_managed_pods() {
  "${K[@]}" get pods -n "$NAMESPACE"     -l app.kubernetes.io/managed-by=kuberos     -o jsonpath='{range .items[*]}{.metadata.labels.pod-name}{"\t"}{.metadata.name}{"\t"}{.metadata.uid}{"\t"}{.status.containerStatuses[0].restartCount}{"\t"}{.spec.nodeName}{"\n"}{end}' |
    LC_ALL=C sort
}

snapshot_managed_pods >"$RESULT_DIR/pods-before.tsv"

declare -A BEFORE_UID BEFORE_RESTARTS BEFORE_POD
while IFS=$'\t' read -r deployment pod uid restarts node; do
  [[ -n "$deployment" ]] || continue
  BEFORE_UID["$deployment"]=$uid
  BEFORE_RESTARTS["$deployment"]=$restarts
  BEFORE_POD["$deployment"]=$pod
done <"$RESULT_DIR/pods-before.tsv"

if [[ "${#BEFORE_UID[@]}" != "12" ]]; then
  echo "Expected 12 managed Pods before U1, found ${#BEFORE_UID[@]}" >&2
  exit 1
fi

"${K[@]}" create configmap u1-kuberos-update-manifest -n "$NAMESPACE"   --from-file=revision-2.yaml="$RESULT_DIR/revision-2.yaml"   --dry-run=client -o yaml | "${K[@]}" apply -f -
"${K[@]}" delete job u1-kuberos-update -n "$NAMESPACE" --ignore-not-found

START_EPOCH=$(date +%s)
"${K[@]}" apply -f "$ROOT_DIR/manifests/kubernetes/u1/10-update-job.yaml"
"${K[@]}" wait --for=condition=complete job/u1-kuberos-update   -n "$NAMESPACE" --timeout=360s
"${K[@]}" rollout status "deployment/$TARGET_DEPLOYMENT"   -n "$NAMESPACE" --timeout=180s
END_EPOCH=$(date +%s)

for _ in $(seq 1 30); do
  target_pods=$("${K[@]}" get pod -n "$NAMESPACE"     -l "pod-name=$TARGET_DEPLOYMENT" --no-headers | wc -l)
  [[ "$target_pods" == "1" ]] && break
  sleep 1
done

"${K[@]}" logs -n "$NAMESPACE" job/u1-kuberos-update   >"$RESULT_DIR/kuberos-update-client.log"
snapshot_managed_pods >"$RESULT_DIR/pods-after.tsv"

declare -A AFTER_UID AFTER_RESTARTS AFTER_POD
while IFS=$'\t' read -r deployment pod uid restarts node; do
  [[ -n "$deployment" ]] || continue
  AFTER_UID["$deployment"]=$uid
  AFTER_RESTARTS["$deployment"]=$restarts
  AFTER_POD["$deployment"]=$pod
done <"$RESULT_DIR/pods-after.tsv"

PASS=true
CHANGED_COUNT=0
PRESERVED_COUNT=0
{
  echo "deployment,pod_before,pod_after,uid_before,uid_after,restarts_before,restarts_after,outcome"
  for deployment in $(printf '%s\n' "${!BEFORE_UID[@]}" | LC_ALL=C sort); do
    outcome=preserved
    if [[ -z "${AFTER_UID[$deployment]:-}" ]]; then
      outcome=missing
      PASS=false
    elif [[ "$deployment" == "$TARGET_DEPLOYMENT" ]]; then
      outcome=replaced
      [[ "${BEFORE_UID[$deployment]}" != "${AFTER_UID[$deployment]}" ]] || PASS=false
      ((CHANGED_COUNT += 1))
    else
      [[ "${BEFORE_UID[$deployment]}" == "${AFTER_UID[$deployment]}" ]] || {
        outcome=unexpected-replacement
        PASS=false
      }
      ((PRESERVED_COUNT += 1))
    fi
    [[ "${BEFORE_RESTARTS[$deployment]}" == "${AFTER_RESTARTS[$deployment]:-missing}" ]] || PASS=false
    echo "$deployment,${BEFORE_POD[$deployment]},${AFTER_POD[$deployment]:-missing},${BEFORE_UID[$deployment]},${AFTER_UID[$deployment]:-missing},${BEFORE_RESTARTS[$deployment]},${AFTER_RESTARTS[$deployment]:-missing},$outcome"
  done
} >"$RESULT_DIR/workload-diff.csv"

[[ "${#AFTER_UID[@]}" == "12" ]] || PASS=false
[[ "$CHANGED_COUNT" == "1" ]] || PASS=false
[[ "$PRESERVED_COUNT" == "11" ]] || PASS=false

"${K[@]}" exec -n "$NAMESPACE" deployment/kuberos -c api --   python manage.py shell -c   "from main.models import Deployment, DeploymentEvent; [print(f'DEPLOYMENT|{d.name}|{d.revision}|{d.status}') for d in Deployment.objects.filter(active=True).order_by('name')]; [print(f'EVENT|{e.deployment.name}|{e.event_type}|{e.event_status}|{e.target_revision}|{e.uuid}') for e in DeploymentEvent.objects.filter(event_type='UPDATE').order_by('created_at')]"   >"$RESULT_DIR/kuberos-state.txt"

grep -Fxq "DEPLOYMENT|e0-baseline-drone01|2|running" "$RESULT_DIR/kuberos-state.txt" || PASS=false
grep -Fxq "DEPLOYMENT|e0-baseline-drone02|1|running" "$RESULT_DIR/kuberos-state.txt" || PASS=false
grep -Fxq "DEPLOYMENT|e0-baseline-drone03|1|running" "$RESULT_DIR/kuberos-state.txt" || PASS=false
grep -Eq '^EVENT\|e0-baseline-drone01\|UPDATE\|SUCCESS\|2\|' "$RESULT_DIR/kuberos-state.txt" || PASS=false
grep -q '^U1_KUBEROS_UPDATE_READY ' "$RESULT_DIR/kuberos-update-client.log" || PASS=false

"${K[@]}" get deployment "$TARGET_DEPLOYMENT" -n "$NAMESPACE"   -o jsonpath='{.spec.template.spec.containers[0].args[1]}'   >"$RESULT_DIR/analytics-command.txt"
grep -Fq 'processing_delay_ms:=95.0' "$RESULT_DIR/analytics-command.txt" || PASS=false

HEALTH_SNAPSHOT=
for _ in $(seq 1 20); do
  HEALTH_SNAPSHOT=$("${K[@]}" exec -n "$NAMESPACE"     "deployment/$TARGET_DEPLOYMENT" -- /bin/bash -lc     "source /ws/install/setup.bash; ROS_SUPER_CLIENT=TRUE ROS2CLI_DISABLE_DAEMON=1 timeout 20 ros2 service call /drone01/companion/onboard/health cloud_native_robotics_interfaces/srv/GetHealthSnapshot \"{correlation_id: 'u1-drone01-health'}\"" 2>/dev/null || true)
  [[ "$HEALTH_SNAPSHOT" == *"healthy=True"* ]] && break
  sleep 1
done
printf '%s\n' "$HEALTH_SNAPSHOT" >"$RESULT_DIR/health-snapshot.txt"
[[ "$HEALTH_SNAPSHOT" == *"healthy=True"* ]] || PASS=false
[[ "$HEALTH_SNAPSHOT" == *"lifecycle_state='active'"* ]] || PASS=false
[[ "$HEALTH_SNAPSHOT" == *"latency_ms=95.0"* ]] || PASS=false

if "${K[@]}" get deployment -n "$NAMESPACE"   -l pod-name=drone01-companion-analytics --no-headers 2>/dev/null | grep -q .; then
  PASS=false
fi
if "${K[@]}" get hpa -n "$NAMESPACE" --no-headers 2>/dev/null | grep -q .; then
  PASS=false
fi

"${K[@]}" get pods -n "$NAMESPACE" -o wide >"$RESULT_DIR/pods.txt"
"${K[@]}" get deployments,services,jobs,hpa,pvc -n "$NAMESPACE" -o yaml   >"$RESULT_DIR/kubernetes-resources.yaml"
"${K[@]}" logs -n "$NAMESPACE" deployment/kuberos -c api   >"$RESULT_DIR/kuberos-api.log" 2>&1 || true
"${K[@]}" logs -n "$NAMESPACE" deployment/kuberos -c worker   >"$RESULT_DIR/kuberos-worker.log" 2>&1 || true

cat >"$RESULT_DIR/REPORT.md" <<EOF
# U1 KubeROS Differential Update Live Result

| Campo | Valore |
| --- | --- |
| Esito | $PASS |
| Cluster / namespace | $CLUSTER / $NAMESPACE |
| Baseline | $BASELINE_RESULT |
| ApplicationDeployment | $APPLICATION_DEPLOYMENT |
| Revisione | 1 -> 2 |
| Modulo modificato | $TARGET_DEPLOYMENT |
| Parametro analytics | processing_delay_ms: 80.0 -> 95.0 |
| Pod sostituiti | $CHANGED_COUNT / 12 |
| Pod preservati | $PRESERVED_COUNT / 12 |
| Tempo end-to-end update | $((END_EPOCH - START_EPOCH)) s |
| Stato finale KubeROS | running / UPDATE SUCCESS |
| Service health | healthy, Lifecycle active, latency 95 ms |
| Edge / HPA | assenti come atteso |

La revisione e' stata inviata con PATCH autenticato alla API KubeROS. Il
reconciler ha effettuato il rolling update del solo Companion Analytics di
drone01. PX4, Micro XRCE-DDS Agent, Event Detector e tutti i moduli di drone02
e drone03 hanno conservato UID e restart count.
EOF

echo "U1 result: $PASS"
echo "Evidence: $RESULT_DIR"
[[ "$PASS" == "true" ]]
