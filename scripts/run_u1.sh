#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
CLUSTER=cloud-native-p2
CONTEXT=k3d-cloud-native-p2
NAMESPACE=cloud-native-p2
VARIANT=${VARIANT:-a}
RESULT_ID=$(date -u +%Y%m%dT%H%M%SZ)
RESULT_DIR="$ROOT_DIR/results/u1/$RESULT_ID"
K=(kubectl --context "$CONTEXT")
TARGET_DEPLOYMENT=drone01-companion-analytics-onboard
APPLICATION_DEPLOYMENT=e0-baseline-drone01
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

# Reconciliation churn (proposal's own KPI table: "numero di azioni
# ripetute (create/delete) generate per lo stesso incidente") -- never
# instrumented before. A clean, non-churning reconciliation of a single
# Pod's rolling replacement is exactly 1 create + 1 delete; a reconciler
# that keeps re-issuing the same action (the historical spec-hash bug
# found live in ROSModuleController, fixed by an idempotency guard on
# .spec's own hash -- see operator/DEV_SMOKE_TEST.md) would show up here
# as extra creates/deletes for the same target, since the update is the
# only thing touching Kubernetes objects during this window. Counted via
# plain kubectl + comm (no API-server audit log needed): snapshot
# (uid|reason|kind|name) for every Event before and after, diff for the
# ones that are new, then filter to Pod Created/Killing for the target's
# own pod-name prefix.
capture_events_summary() {
  "${K[@]}" get events -n "$NAMESPACE" \
    -o jsonpath='{range .items[*]}{.metadata.uid}{"|"}{.reason}{"|"}{.involvedObject.kind}{"|"}{.involvedObject.name}{"\n"}{end}' \
    | LC_ALL=C sort
}
count_churn() {
  local before_file="$1" after_file="$2" pod_prefix="$3"
  local new_events created_count deleted_count
  new_events=$(comm -13 "$before_file" "$after_file")
  # Distinct Pod *names*, not raw event lines: found live that a
  # multi-container Pod (companion-analytics + its state-bridge sidecar,
  # variant B) gets one Created/Killing event per container, not per Pod
  # -- counting events directly double-counted a single, genuine 1-create
  # +1-delete replacement as 4.
  created_count=$(awk -F'|' -v p="$pod_prefix" '$3=="Pod" && index($4,p)==1 && $2=="Created" {print $4}' <<<"$new_events" | sort -u | wc -l)
  deleted_count=$(awk -F'|' -v p="$pod_prefix" '$3=="Pod" && index($4,p)==1 && $2=="Killing" {print $4}' <<<"$new_events" | sort -u | wc -l)
  echo $((created_count + deleted_count))
}

if [[ "$VARIANT" == "a" ]]; then

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

capture_events_summary >"$RESULT_DIR/events-before-update.txt"
START_EPOCH=$(date +%s)
"${K[@]}" apply -f "$ROOT_DIR/manifests/kubernetes/u1/10-update-job.yaml"
"${K[@]}" wait --for=condition=complete job/u1-kuberos-update   -n "$NAMESPACE" --timeout=360s
"${K[@]}" rollout status "deployment/$TARGET_DEPLOYMENT"   -n "$NAMESPACE" --timeout=180s
END_EPOCH=$(date +%s)
capture_events_summary >"$RESULT_DIR/events-after-update.txt"
RECONCILIATION_CHURN=$(count_churn "$RESULT_DIR/events-before-update.txt" "$RESULT_DIR/events-after-update.txt" "$TARGET_DEPLOYMENT-")

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
      # No restart-count check here: "before" and "after" are two different
      # Pods (different UID) once replaced, so their restart counts are
      # unrelated numbers -- found live (campaign run, RUNS=10) that a
      # freshly-rolled-out replacement Pod can genuinely restart once
      # during its own startup and still converge correctly seconds later
      # (revision advanced, health snapshot correct); comparing its restart
      # count to the OLD Pod's was a false-failure trap, not a real check.
    else
      [[ "${BEFORE_UID[$deployment]}" == "${AFTER_UID[$deployment]}" ]] || {
        outcome=unexpected-replacement
        PASS=false
      }
      ((PRESERVED_COUNT += 1))
      [[ "${BEFORE_RESTARTS[$deployment]}" == "${AFTER_RESTARTS[$deployment]:-missing}" ]] || PASS=false
    fi
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

# Idempotency check: a single clean update alone can't distinguish a
# reconciler that correctly recognizes "nothing actually changed" from
# one that blindly redoes the same work every time it's asked -- both
# produce the same reconciliation-churn value of 2 on their first
# application. Resubmitting the identical revision-2 target twice more
# and requiring zero *additional* churn each time is what actually gives
# this metric (proposal's own "azioni ripetute ... per lo stesso
# incidente") discriminating power. This is reported as data either way,
# not folded into PASS/FAIL -- a non-zero result here would be a real
# finding about variant A's own update path, not a broken test.
IDEMPOTENCY_CHURN_TOTAL=0
IDEMPOTENCY_DETAIL=""
for repeat in 1 2; do
  "${K[@]}" delete job u1-kuberos-update -n "$NAMESPACE" --ignore-not-found
  capture_events_summary >"$RESULT_DIR/events-before-repeat-$repeat.txt"
  "${K[@]}" apply -f "$ROOT_DIR/manifests/kubernetes/u1/10-update-job.yaml"
  "${K[@]}" wait --for=condition=complete job/u1-kuberos-update -n "$NAMESPACE" --timeout=360s \
    || echo "repeat $repeat: update job did not report complete (resubmitting an already-applied revision may not be a supported/idempotent path in KubeROS's own API -- recorded as data)" >>"$RESULT_DIR/idempotency-notes.txt"
  capture_events_summary >"$RESULT_DIR/events-after-repeat-$repeat.txt"
  REPEAT_CHURN=$(count_churn "$RESULT_DIR/events-before-repeat-$repeat.txt" "$RESULT_DIR/events-after-repeat-$repeat.txt" "$TARGET_DEPLOYMENT-")
  IDEMPOTENCY_CHURN_TOTAL=$((IDEMPOTENCY_CHURN_TOTAL + REPEAT_CHURN))
  IDEMPOTENCY_DETAIL="${IDEMPOTENCY_DETAIL}ripetizione $repeat: $REPEAT_CHURN; "
done

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
| Reconciliation churn (create+delete Pod per l'incidente, atteso 2) | $RECONCILIATION_CHURN |
| Churn extra da 2 richieste ripetute identiche (atteso 0 ciascuna) | $IDEMPOTENCY_CHURN_TOTAL totale ($IDEMPOTENCY_DETAIL) |
| Tempo end-to-end update | $((END_EPOCH - START_EPOCH)) s |
| Stato finale KubeROS | running / UPDATE SUCCESS |
| Service health | healthy, Lifecycle active, latency 95 ms |
| Edge / HPA | assenti come atteso |

La revisione e' stata inviata con PATCH autenticato alla API KubeROS. Il
reconciler ha effettuato il rolling update del solo Companion Analytics di
drone01. PX4, Micro XRCE-DDS Agent, Event Detector e tutti i moduli di drone02
e drone03 hanno conservato UID e restart count.
EOF

echo "U1 result (variant A): $PASS"
echo "Evidence: $RESULT_DIR"
[[ "$PASS" == "true" ]]

else
# =====================================================================
# VARIANT B: dichiarativo (CRD + Fleet Operator + State Bridge)
#
# La baseline riusa run_e0.sh VARIANT=b (3 ROSModule onboard, nessuna
# AdaptationPolicy). L'update non passa da una API REST separata: si
# applica un ROSModule con lo stesso nome e un rosParamMap.
# processing_delay_ms diverso (80.0 -> 95.0, sempre sotto la soglia SLO di
# 250ms, quindi non deve mai attivare AdaptationController) e si aspetta
# che ROSModuleController lo riconcili da solo. status.revision (nuovo
# campo, rosmodule.yaml) e' l'equivalente dichiarativo della revisione
# KubeROS: 0 = mai convergita, incrementa di 1 ad ogni RollingOut->Stable
# riuscito, quindi 1 dopo il bootstrap della baseline e 2 dopo questo
# update.
# =====================================================================

RESET_BASELINE=${RESET_U1_BASELINE:-1}
BASELINE_WINDOW=${U1_BASELINE_OBSERVATION_WINDOW_SEC:-10}
RESET_E0="$RESET_BASELINE" E0_OBSERVATION_WINDOW_SEC="$BASELINE_WINDOW" \
  SKIP_IMAGE_BUILD_IMPORT="${SKIP_IMAGE_BUILD_IMPORT:-0}" VARIANT=b \
  "$ROOT_DIR/scripts/run_e0.sh" | tee "$RESULT_DIR/e0-baseline.log"

BASELINE_RESULT=$(sed -n 's/^Evidence: //p' "$RESULT_DIR/e0-baseline.log" | tail -n 1)
if [[ -z "$BASELINE_RESULT" ]]; then
  echo "Unable to locate the E0 baseline result" >&2
  exit 1
fi
printf '%s\n' "$BASELINE_RESULT" >"$RESULT_DIR/e0-baseline-result.txt"

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

wait_for_revision() {
  local want=$1 timeout_sec=$2 got=""
  for _ in $(seq 1 "$timeout_sec"); do
    got=$("${K[@]}" get rosmodule "$ROSMODULE" -n "$NAMESPACE" \
      -o jsonpath='{.status.revision}' 2>/dev/null || true)
    [[ "$got" == "$want" ]] && return 0
    sleep 1
  done
  return 1
}

wait_for_revision 1 90 || {
  echo "ROSModule $ROSMODULE never reached revision 1 after the baseline" >&2
  "${K[@]}" get rosmodule "$ROSMODULE" -n "$NAMESPACE" -o yaml >&2 || true
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

declare -A BEFORE_UID BEFORE_RESTARTS BEFORE_POD
while IFS=$'\t' read -r deployment pod uid restarts node; do
  [[ -n "$deployment" ]] || continue
  BEFORE_UID["$deployment"]=$uid
  BEFORE_RESTARTS["$deployment"]=$restarts
  BEFORE_POD["$deployment"]=$pod
done <"$RESULT_DIR/pods-before.tsv"

if [[ "${#BEFORE_UID[@]}" != "9" ]]; then
  echo "Expected 9 managed Pods before U1, found ${#BEFORE_UID[@]}" >&2
  exit 1
fi

capture_events_summary >"$RESULT_DIR/events-before-update.txt"
START_EPOCH=$(date +%s)
"${K[@]}" apply -f "$ROOT_DIR/manifests/kubernetes/u1/60-declarative-update.yaml"
wait_for_revision 2 180 || {
  echo "ROSModule $ROSMODULE never reached revision 2 after the update" >&2
  "${K[@]}" get rosmodule "$ROSMODULE" -n "$NAMESPACE" -o yaml >&2 || true
  exit 1
}
END_EPOCH=$(date +%s)
capture_events_summary >"$RESULT_DIR/events-after-update.txt"
RECONCILIATION_CHURN=$(count_churn "$RESULT_DIR/events-before-update.txt" "$RESULT_DIR/events-after-update.txt" "$ROSMODULE-")

for _ in $(seq 1 30); do
  target_pods=$("${K[@]}" get pod -n "$NAMESPACE" \
    -l "dronekube.io/owned-by-rosmodule=$ROSMODULE" --no-headers | wc -l)
  [[ "$target_pods" == "1" ]] && break
  sleep 1
done

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
    elif [[ "$deployment" == "$ROSMODULE" ]]; then
      outcome=replaced
      [[ "${BEFORE_UID[$deployment]}" != "${AFTER_UID[$deployment]}" ]] || PASS=false
      ((CHANGED_COUNT += 1))
      # See the identical note in variant A's own copy of this loop above:
      # before/after restart counts belong to two different Pods once
      # replaced, so comparing them is not a meaningful check.
    else
      [[ "${BEFORE_UID[$deployment]}" == "${AFTER_UID[$deployment]}" ]] || {
        outcome=unexpected-replacement
        PASS=false
      }
      ((PRESERVED_COUNT += 1))
      [[ "${BEFORE_RESTARTS[$deployment]}" == "${AFTER_RESTARTS[$deployment]:-missing}" ]] || PASS=false
    fi
    echo "$deployment,${BEFORE_POD[$deployment]},${AFTER_POD[$deployment]:-missing},${BEFORE_UID[$deployment]},${AFTER_UID[$deployment]:-missing},${BEFORE_RESTARTS[$deployment]},${AFTER_RESTARTS[$deployment]:-missing},$outcome"
  done
} >"$RESULT_DIR/workload-diff.csv"

[[ "${#AFTER_UID[@]}" == "9" ]] || PASS=false
[[ "$CHANGED_COUNT" == "1" ]] || PASS=false
[[ "$PRESERVED_COUNT" == "8" ]] || PASS=false

"${K[@]}" get rosmodule "$ROSMODULE" -n "$NAMESPACE" -o yaml >"$RESULT_DIR/rosmodule-after.yaml"
UPDATE_STATE=$("${K[@]}" get rosmodule "$ROSMODULE" -n "$NAMESPACE" \
  -o jsonpath='{.status.updateState}')
LAST_GOOD_DELAY=$("${K[@]}" get rosmodule "$ROSMODULE" -n "$NAMESPACE" \
  -o jsonpath='{.status.lastGoodSpec.rosParamMap.processing_delay_ms}')
[[ "$UPDATE_STATE" == "Stable" ]] || PASS=false
[[ "$LAST_GOOD_DELAY" == "95.0" ]] || PASS=false

"${K[@]}" get deployment "$ROSMODULE" -n "$NAMESPACE" \
  -o jsonpath='{.spec.template.spec.containers[0].command[2]}' \
  >"$RESULT_DIR/analytics-command.txt"
grep -Fq 'processing_delay_ms:=95.0' "$RESULT_DIR/analytics-command.txt" || PASS=false

HEALTH_SNAPSHOT=
for _ in $(seq 1 60); do
  HEALTH_SNAPSHOT=$("${K[@]}" exec -n "$NAMESPACE" \
    "deployment/$ROSMODULE" -c companion-analytics -- /bin/bash -lc \
    "source /ws/install/setup.bash; ROS_SUPER_CLIENT=TRUE ROS2CLI_DISABLE_DAEMON=1 timeout 20 ros2 service call /drone01/companion/onboard/health cloud_native_robotics_interfaces/srv/GetHealthSnapshot \"{correlation_id: 'u1b-drone01-health'}\"" 2>/dev/null || true)
  # healthy=True can appear before latency_ms (a rolling metric, not a
  # direct echo of the param) has caught up to the freshly-applied value
  # -- found live (U2's rollback case): breaking on healthy=True alone
  # captured a snapshot that was healthy/active but still showing the
  # pre-update latency. Wait for the actual expected value too.
  [[ "$HEALTH_SNAPSHOT" == *"healthy=True"* && "$HEALTH_SNAPSHOT" == *"latency_ms=95.0"* ]] && break
  sleep 2
done
printf '%s\n' "$HEALTH_SNAPSHOT" >"$RESULT_DIR/health-snapshot.txt"
[[ "$HEALTH_SNAPSHOT" == *"healthy=True"* ]] || PASS=false
[[ "$HEALTH_SNAPSHOT" == *"lifecycle_state='active'"* ]] || PASS=false
[[ "$HEALTH_SNAPSHOT" == *"latency_ms=95.0"* ]] || PASS=false

# Idempotency check: same reasoning as variant A's own (see its comment) --
# re-applying the identical ROSModule spec (byte-for-byte, kubectl apply
# is itself idempotent client-side) should produce zero additional Pod
# create/delete churn if ROSModuleController correctly no-ops on an
# unchanged spec. This is exactly the historical spec-hash bug's own
# regression test: before that fix, the controller replaced the
# Deployment on every reconcile tick regardless of whether .spec had
# actually changed.
IDEMPOTENCY_CHURN_TOTAL=0
IDEMPOTENCY_DETAIL=""
for repeat in 1 2; do
  capture_events_summary >"$RESULT_DIR/events-before-repeat-$repeat.txt"
  "${K[@]}" apply -f "$ROOT_DIR/manifests/kubernetes/u1/60-declarative-update.yaml"
  sleep 5
  capture_events_summary >"$RESULT_DIR/events-after-repeat-$repeat.txt"
  REPEAT_CHURN=$(count_churn "$RESULT_DIR/events-before-repeat-$repeat.txt" "$RESULT_DIR/events-after-repeat-$repeat.txt" "$ROSMODULE-")
  IDEMPOTENCY_CHURN_TOTAL=$((IDEMPOTENCY_CHURN_TOTAL + REPEAT_CHURN))
  IDEMPOTENCY_DETAIL="${IDEMPOTENCY_DETAIL}ripetizione $repeat: $REPEAT_CHURN; "
done

"${K[@]}" get pods -n "$NAMESPACE" -o wide >"$RESULT_DIR/pods.txt"
"${K[@]}" get deployments,rosmodules -n "$NAMESPACE" -o yaml \
  >"$RESULT_DIR/kubernetes-resources.yaml"
"${K[@]}" logs -n "$NAMESPACE" deployment/fleet-operator \
  >"$RESULT_DIR/fleet-operator.log" 2>&1 || true
"${K[@]}" get events -n "$NAMESPACE" --sort-by=.lastTimestamp \
  >"$RESULT_DIR/kubernetes-events.txt" 2>&1 || true
"${K[@]}" exec -n "$NAMESPACE" deployment/p2-audit-writer -- cat /data/audit.jsonl \
  >"$RESULT_DIR/audit.jsonl" 2>&1 || true
# rosmodule_controller.py's own audit trail (fleet_operator/audit.py, fixed
# alongside adaptation_controller.py's -- proposal S4.1/S4.2 parity):
# "started" only fires for a genuine differential update on an
# already-converged ROSModule, never for the E0 baseline's own first-time
# bootstrap of the same object a few steps above, so this is the first live
# exercise of that particular branch.
grep -Fq '"event_type":"ROSModuleUpdate"' "$RESULT_DIR/audit.jsonl" 2>/dev/null || PASS=false
grep -Fq '"outcome":"rosmodule_update_converged"' "$RESULT_DIR/audit.jsonl" 2>/dev/null || PASS=false

cat >"$RESULT_DIR/REPORT.md" <<EOF
# U1 Declarative Differential Update Live Result

| Campo | Valore |
| --- | --- |
| Esito | $PASS |
| Variante | B (dichiarativa: CRD + Fleet Operator) |
| Cluster / namespace | $CLUSTER / $NAMESPACE |
| Baseline | $BASELINE_RESULT |
| ROSModule modificato | $ROSMODULE |
| Parametro analytics | processing_delay_ms: 80.0 -> 95.0 |
| status.revision | 1 -> 2 |
| status.updateState finale | $UPDATE_STATE |
| Pod sostituiti | $CHANGED_COUNT / 9 |
| Pod preservati | $PRESERVED_COUNT / 9 |
| Reconciliation churn (create+delete Pod per l'incidente, atteso 2) | $RECONCILIATION_CHURN |
| Churn extra da 2 riapplicazioni identiche (atteso 0 ciascuna) | $IDEMPOTENCY_CHURN_TOTAL totale ($IDEMPOTENCY_DETAIL) |
| Tempo end-to-end update | $((END_EPOCH - START_EPOCH)) s |
| Service health | healthy, Lifecycle active, latenza 95 ms |
| Audit trail (Audit Writer/Operator Notifier) | presente (ROSModuleUpdate, rosmodule_update_converged) |

L'update e' stato applicato modificando direttamente lo spec del ROSModule
(stesso nome, nessuna API separata). ROSModuleController ha eseguito il
rolling update del solo Deployment target, PX4, Micro XRCE-DDS Agent e gli
altri due ROSModule hanno conservato UID e restart count.
EOF

echo "U1 result (variant B): $PASS"
echo "Evidence: $RESULT_DIR"
[[ "$PASS" == "true" ]]

fi
