#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
CLUSTER=cloud-native-p2
CONTEXT=k3d-cloud-native-p2
NAMESPACE=cloud-native-p2
EXPERIMENT_MODE=${EXPERIMENT_MODE:-p2}
VARIANT=${VARIANT:-a}
RESULT_ID=$(date -u +%Y%m%dT%H%M%SZ)
IMAGE_LOCK_FILE=${IMAGE_LOCK_FILE:-}
REGISTRY_PULL_SECRET=${REGISTRY_PULL_SECRET:-kuberos-private-release}
DOCKER_CONFIG_PATH=${DOCKER_CONFIG_PATH:-$HOME/.docker/config.json}
K=(kubectl --context "$CONTEXT")
USE_IMAGE_LOCK=0
IMAGE_PROVENANCE="local build/import"
DIGEST_VERIFICATION="not requested"

case "$VARIANT" in
  a|b) ;;
  *)
    echo "Unsupported VARIANT: $VARIANT (expected 'a' or 'b')" >&2
    exit 2
    ;;
esac

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
    # One readiness limit for both variants: A's goal timeout and B's
    # rollback.onReadinessFailureSec both come from GOAL_TIMEOUT_SEC (R9,
    # Viviana 2026-09-26; B used a fixed 30s before). Same fault in both
    # too: the edge gets a non-numeric sample_period_ms. The earlier A fault,
    # a readinessProbe that always fails, stays in
    # manifests/kuberos/e4/analytics-edge-readiness-failure.yaml for the runs
    # made with it.
    GOAL_TIMEOUT_SEC=45.0
    ANALYTICS_MANIFEST="$ROOT_DIR/manifests/kuberos/e4/analytics-edge-invalid-parameter.yaml"
    ONBOARD_PROCESSING_DELAY_MS=300.0
    OUTCOME_PATTERN='completed as analytics_migration_failed (ROLLED_BACK)'
    MINIMUM_PROJECT_CONTAINERS=11
    ;;
  edgeloss)
    # R5 check, variant A only: scripts/run_edge_fallback_check_a.sh injects the
    # edge loss after the handover while this runs. The edge runs at 300 ms so
    # the incident cannot close first; any completion ends the wait, and the
    # rollback checks below judge it (same as e4).
    RESULT_GROUP=edgeloss
    GOAL_TIMEOUT_SEC=120.0
    ANALYTICS_MANIFEST="$ROOT_DIR/manifests/kuberos/edgeloss/analytics-edge-slow.yaml"
    ONBOARD_PROCESSING_DELAY_MS=300.0
    OUTCOME_PATTERN='completed as '
    OUTCOME_WAIT_SEC=300
    MINIMUM_PROJECT_CONTAINERS=12
    ;;
  *)
    echo "Unsupported EXPERIMENT_MODE: $EXPERIMENT_MODE" >&2
    exit 2
    ;;
esac

if [[ "$EXPERIMENT_MODE" == "edgeloss" && "$VARIANT" != "a" ]]; then
  echo "EXPERIMENT_MODE=edgeloss is variant A only (B: scripts/run_edge_fallback_check.sh)" >&2
  exit 2
fi

RESULT_DIR="$ROOT_DIR/results/$RESULT_GROUP/$RESULT_ID"

for command in docker k3d kubectl python3; do
  command -v "$command" >/dev/null || {
    echo "Required command not found: $command" >&2
    exit 1
  }
done

mkdir -p "$RESULT_DIR"

# Provenance, same tool and files as S3 (scripts/s3_provenance.py): the exact
# Git-visible inputs of this run, committed or not, plus the local image IDs
# recorded after each import below (pods.json already records what the Pods
# actually ran). Added 2026-09-23: the worktree is not committed and the
# checklist's small functional baseline requires attributable results --
# an E4-B run that evening had to be attributed after the fact.
python3 "$ROOT_DIR/scripts/s3_provenance.py" --root "$ROOT_DIR" \
  --output "$RESULT_DIR/provenance" >"$RESULT_DIR/source-sha256.txt"
record_local_images() {
  local image
  for image in "$@"; do
    docker image inspect --format '{{.Id}} {{json .RepoTags}} {{json .RepoDigests}}' "$image" \
      >>"$RESULT_DIR/local-image-ids.txt" 2>>"$RESULT_DIR/local-image-errors.txt" || true
  done
}

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
# datastore of the bench, embedded etcd (R14 target, docs/ETCD_GATE_PREREGISTRATION.md):
# fail-closed, before any workload or injection
python3 "$ROOT_DIR/scripts/datastore_check.py" check "$CLUSTER" "$RESULT_DIR" \
  || { echo "datastore of $CLUSTER not verified as embedded etcd: stopping before any workload" >&2; exit 2; }

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

if [[ "$VARIANT" == "a" ]]; then

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
record_local_images cloud-native-ros/control-plane:p2 cloud-native-ros/event-detector:p2 \
  cloud-native-ros/kuberos:p2 microros/micro-ros-agent:humble px4io/px4-sitl:latest redis:7

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

for _ in $(seq 1 "${OUTCOME_WAIT_SEC:-180}"); do
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

if [[ "$EXPERIMENT_MODE" == "e4" || "$EXPERIMENT_MODE" == "edgeloss" ]]; then
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
  [[ "${ONBOARD_STATE%% *}" == "inactive" ]] || PASS=false
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
  [[ "${ONBOARD_STATE%% *}" == "active" ]] || PASS=false
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
  if [[ "$EXPERIMENT_MODE" == "edgeloss" ]]; then
    MODE_DESCRIPTION="Il target edge e' stato perso dopo la disattivazione dell'onboard (guasto iniettato da scripts/run_edge_fallback_check_a.sh). Controlli di rollback come E4."
  else
    MODE_DESCRIPTION="Il target edge non ha superato la readiness. L'Application Manager ha eseguito il rollback mantenendo attivo il workload onboard."
  fi
fi

cat >"$RESULT_DIR/REPORT.md" <<EOF
# ${EXPERIMENT_MODE^^} Live Result

| Campo | Valore |
| --- | --- |
| Esito | $PASS |
| Variante | A (imperativa) |
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

echo "${EXPERIMENT_MODE^^} result (variant A): $PASS"
echo "Evidence: $RESULT_DIR"
[[ "$PASS" == "true" ]]

else
# =====================================================================
# VARIANT B: dichiarativo (CRD + Fleet Operator + State Bridge)
#
# Riusa dal percorso A: creazione/reset del cluster (sopra, condivisa),
# discovery server (10-discovery.yaml, invariato), PX4 SITL + uXRCE agent
# (40-shared-infra.yaml, estratto verbatim da 40-workload.yaml perche'
# applicare 40-workload.yaml per intero porterebbe con se' anche l'Event
# Detector e lo script di auto-attivazione di variant A, facendo girare
# due meccanismi di reazione in parallelo sullo stesso metrico).
#
# Non implementato in questo passaggio: EXPERIMENT_MODE=e4 (rifiutato piu'
# sopra), image lock/registry privato (USE_IMAGE_LOCK), rollback live
# osservato end-to-end (solo il timeout e' cablato in adaptation_
# controller.py, mai esercitato con un fallimento reale).
# =====================================================================

if [[ "${SKIP_IMAGE_BUILD_IMPORT:-0}" != "1" && "${SKIP_IMAGE_BUILD:-0}" != "1" ]]; then
  docker build -t cloud-native-ros/control-plane:p2 \
    -f "$ROOT_DIR/containers/control-plane/Dockerfile" "$ROOT_DIR"
  docker build -t cloud-native-ros/fleet-operator:p2 \
    -f "$ROOT_DIR/containers/fleet-operator/Dockerfile" "$ROOT_DIR"
  docker build -t cloud-native-ros/state-bridge:p2 \
    -f "$ROOT_DIR/containers/state-bridge/Dockerfile" "$ROOT_DIR"
fi

if [[ "${SKIP_IMAGE_BUILD_IMPORT:-0}" != "1" && "${SKIP_IMAGE_IMPORT:-0}" != "1" ]]; then
  k3d image import -c "$CLUSTER" \
    cloud-native-ros/control-plane:p2 \
    cloud-native-ros/fleet-operator:p2 \
    cloud-native-ros/state-bridge:p2 \
    microros/micro-ros-agent:humble \
    px4io/px4-sitl:latest
fi
record_local_images cloud-native-ros/control-plane:p2 cloud-native-ros/fleet-operator:p2 \
  cloud-native-ros/state-bridge:p2 microros/micro-ros-agent:humble px4io/px4-sitl:latest

if [[ "$EXPERIMENT_MODE" == "e4" ]]; then
  DECLARATIVE_WORKLOAD_MANIFEST="$ROOT_DIR/manifests/kubernetes/p2/60-declarative-workload-e4.yaml"
else
  DECLARATIVE_WORKLOAD_MANIFEST="$ROOT_DIR/manifests/kubernetes/p2/60-declarative-workload.yaml"
fi

render_manifest "$ROOT_DIR/manifests/kubernetes/p2/40-shared-infra.yaml" "$RENDERED_DIR/40-shared-infra.yaml"
render_manifest "$ROOT_DIR/manifests/kubernetes/p2/50-declarative-control-plane.yaml" "$RENDERED_DIR/50-declarative-control-plane.yaml"
render_manifest "$DECLARATIVE_WORKLOAD_MANIFEST" "$RENDERED_DIR/60-declarative-workload.yaml"

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
# Platform Observer + Audit Writer + Operator Notifier: variant-agnostic
# (proposal S4.1, "invariati... osservati da entrambe le varianti"), but
# only ever applied on variant A's own branch until now -- Fleet Operator
# itself never emitted anything for them to observe either (fixed in
# fleet_operator/audit.py).
"${K[@]}" apply -f "$ROOT_DIR/manifests/kubernetes/p2/00-rbac.yaml"
"${K[@]}" apply -f "$ROOT_DIR/manifests/kubernetes/p2/10-discovery.yaml"
"${K[@]}" apply -f "$RENDERED_DIR/50-declarative-control-plane.yaml"
"${K[@]}" apply -f "$ROOT_DIR/manifests/kubernetes/p2/25-observability.yaml"
"${K[@]}" rollout status deployment/p2-fastdds-discovery \
  -n "$NAMESPACE" --timeout=120s
"${K[@]}" rollout status deployment/fleet-operator \
  -n "$NAMESPACE" --timeout=120s
"${K[@]}" rollout status deployment/p2-audit-writer -n "$NAMESPACE" --timeout=120s
"${K[@]}" rollout status deployment/p2-operator-notifier -n "$NAMESPACE" --timeout=120s
"${K[@]}" rollout status deployment/p2-platform-observer -n "$NAMESPACE" --timeout=120s

# The image catalog fleet-operator reads at runtime (config/project_images.
# json, baked into the image at build time via PROJECT_IMAGES_PATH) has to
# already name the tags this script actually builds and imports -- found
# live: state-bridge used to be catalogued as :dev (this project's dev-
# cluster tag), which was never imported into this cluster, so the sidecar
# went straight to ImagePullBackOff. Fixed at the source
# (config/project_images.json now says :p2, matching every other image),
# not with a per-run rendered copy the running container would not
# actually have read anyway.

START_EPOCH=$(date +%s)
"${K[@]}" apply -f "$RENDERED_DIR/40-shared-infra.yaml"
"${K[@]}" rollout status deployment/drone01-px4-sitl \
  -n "$NAMESPACE" --timeout=120s
"${K[@]}" rollout status deployment/drone01-microxrce-agent \
  -n "$NAMESPACE" --timeout=120s

PX4_UID_BEFORE=$("${K[@]}" get pod -n "$NAMESPACE" \
  -l app.kubernetes.io/name=drone01-px4-sitl \
  -o jsonpath='{.items[0].metadata.uid}')
PX4_RESTARTS_BEFORE=$("${K[@]}" get pod -n "$NAMESPACE" \
  -l app.kubernetes.io/name=drone01-px4-sitl \
  -o jsonpath='{.items[0].status.containerStatuses[0].restartCount}')

"${K[@]}" apply -f "$RENDERED_DIR/60-declarative-workload.yaml"

ADAPTATION_STATE=""
for _ in $(seq 1 180); do
  ADAPTATION_STATE=$("${K[@]}" get adaptationpolicy analytics-latency-slo \
    -n "$NAMESPACE" -o jsonpath='{.status.state}' 2>/dev/null || true)
  [[ "$ADAPTATION_STATE" == "Recovered" || "$ADAPTATION_STATE" == "RolledBack" ]] && break
  sleep 1
done

END_EPOCH=$(date +%s)
PX4_UID_AFTER=$("${K[@]}" get pod -n "$NAMESPACE" \
  -l app.kubernetes.io/name=drone01-px4-sitl \
  -o jsonpath='{.items[0].metadata.uid}')
PX4_RESTARTS_AFTER=$("${K[@]}" get pod -n "$NAMESPACE" \
  -l app.kubernetes.io/name=drone01-px4-sitl \
  -o jsonpath='{.items[0].status.containerStatuses[0].restartCount}')
EDGE_MODULE=$("${K[@]}" get adaptationpolicy analytics-latency-slo \
  -n "$NAMESPACE" -o jsonpath='{.status.edgeModuleName}' 2>/dev/null || true)
EDGE_NODE=""
ONBOARD_LIFECYCLE=""
EDGE_LIFECYCLE=""
if [[ -n "$EDGE_MODULE" ]]; then
  EDGE_NODE=$("${K[@]}" get pod -n "$NAMESPACE" -l "dronekube.io/owned-by-rosmodule=$EDGE_MODULE" \
    -o jsonpath='{.items[0].spec.nodeName}' 2>/dev/null || true)
  EDGE_LIFECYCLE=$("${K[@]}" get rosmodule "$EDGE_MODULE" -n "$NAMESPACE" \
    -o jsonpath='{.status.observedLifecycleState}' 2>/dev/null || true)
fi
# AdaptationController flips AdaptationPolicy to Recovered as soon as the
# edge instance's OWN status confirms Active (read directly, not lagged);
# deactivating onboard is a separate, non-blocking spec write
# (lifecycleTarget: Inactive) that the onboard State Bridge only reflects
# back into status.observedLifecycleState on its own next poll tick --
# found live, a single immediate read right after the loop above breaks
# can catch onboard still reporting its PRE-deactivation state ("Active"),
# even though the deactivation request already landed successfully
# seconds earlier. Retried here, not read once, so this eventual-
# consistency lag between two independently-converging reconcile loops
# doesn't register as a false failure.
if [[ "$EXPERIMENT_MODE" == "e4" ]]; then
  ONBOARD_EXPECTED="Active"
else
  ONBOARD_EXPECTED="Inactive"
fi
ONBOARD_LIFECYCLE=""
for _ in $(seq 1 10); do
  ONBOARD_LIFECYCLE=$("${K[@]}" get rosmodule companion-analytics-drone01 -n "$NAMESPACE" \
    -o jsonpath='{.status.observedLifecycleState}' 2>/dev/null || true)
  [[ "$ONBOARD_LIFECYCLE" == "$ONBOARD_EXPECTED" ]] && break
  sleep 1
done

"${K[@]}" get nodes -o wide >"$RESULT_DIR/nodes.txt"
"${K[@]}" get pods -n "$NAMESPACE" -o wide >"$RESULT_DIR/pods.txt"
"${K[@]}" get pods -n "$NAMESPACE" -o json >"$RESULT_DIR/pods.json"
"${K[@]}" get deployments -n "$NAMESPACE" -o yaml \
  >"$RESULT_DIR/kubernetes-resources.yaml"
"${K[@]}" get rosmodule,adaptationpolicy -n "$NAMESPACE" -o yaml \
  >"$RESULT_DIR/declarative-resources.yaml"
"${K[@]}" logs -n "$NAMESPACE" deployment/fleet-operator \
  >"$RESULT_DIR/fleet-operator.log" 2>&1 || true
"${K[@]}" logs -n "$NAMESPACE" deployment/drone01-px4-sitl \
  >"$RESULT_DIR/px4.log" 2>&1 || true
if [[ -n "$EDGE_MODULE" ]]; then
  "${K[@]}" logs -n "$NAMESPACE" "deployment/$EDGE_MODULE" -c state-bridge \
    >"$RESULT_DIR/edge-state-bridge.log" 2>&1 || true
fi
"${K[@]}" logs -n "$NAMESPACE" deployment/companion-analytics-drone01 -c state-bridge \
  >"$RESULT_DIR/onboard-state-bridge.log" 2>&1 || true
"${K[@]}" exec -n "$NAMESPACE" deployment/p2-audit-writer -- cat /data/audit.jsonl \
  >"$RESULT_DIR/audit.jsonl" 2>&1 || true

PASS=true
[[ "$PX4_UID_BEFORE" == "$PX4_UID_AFTER" ]] || PASS=false
[[ "$PX4_RESTARTS_BEFORE" == "$PX4_RESTARTS_AFTER" ]] || PASS=false
grep -Fq '"event_type":"MigratePlacement"' "$RESULT_DIR/audit.jsonl" || PASS=false
grep -Fq '"record_type":"operator_notification"' "$RESULT_DIR/audit.jsonl" || PASS=false
# R9, decision D4 (G2/G3): the policy's own status.correlationId ties incident,
# outcome, notification and audit together, as A's checks tie them to the
# DeploymentRequest's id; waited for up to 30s, as A waits for its records.
B_CORRELATION_ID=$("${K[@]}" get adaptationpolicy analytics-latency-slo -n "$NAMESPACE" \
  -o jsonpath='{.status.correlationId}' 2>/dev/null || true)
if [[ "$EXPERIMENT_MODE" == "e4" ]]; then
  EXPECTED_OUTCOME=analytics_migration_failed; EXPECTED_ROLLBACK=true
else
  EXPECTED_OUTCOME=analytics_slo_recovered; EXPECTED_ROLLBACK=false
fi
INCIDENT_CHECK=""
for _ in $(seq 1 30); do
  INCIDENT_CHECK=$(python3 "$ROOT_DIR/scripts/scenario_checks.py" incident "$RESULT_DIR/audit.jsonl" \
    analytics-latency-slo "$B_CORRELATION_ID" MigratePlacement "$EXPECTED_OUTCOME" "$EXPECTED_ROLLBACK") && break
  sleep 1
  "${K[@]}" exec -n "$NAMESPACE" deployment/p2-audit-writer -- cat /data/audit.jsonl \
    >"$RESULT_DIR/audit.jsonl" 2>&1 || true
done
[[ "$INCIDENT_CHECK" == PASS* ]] || PASS=false
# The edge HPA (R7): present after a migration, gone with the edge after a
# rollback (owned by the edge ROSModule), as A checks its own.
if [[ "$EXPERIMENT_MODE" == "e4" ]]; then
  for _ in $(seq 1 60); do
    HPA_COUNT=$( { "${K[@]}" get hpa -n "$NAMESPACE" --no-headers 2>/dev/null || true; } | grep -c . || true)
    [[ "$HPA_COUNT" == "0" ]] && break
    sleep 1
  done
  [[ "$HPA_COUNT" == "0" ]] || PASS=false
  HPA_STATUS="assente ($HPA_COUNT HPA)"
else
  if [[ -n "$EDGE_MODULE" ]] && "${K[@]}" get hpa "$EDGE_MODULE" -n "$NAMESPACE" >/dev/null 2>&1; then
    HPA_STATUS="presente ($EDGE_MODULE)"
  else
    HPA_STATUS="assente"; PASS=false
  fi
fi
if [[ "$EXPERIMENT_MODE" == "e4" ]]; then
  # RolledBack, not Recovered: the edge instance was deliberately broken
  # (60-declarative-workload-e4.yaml). Onboard must still be Active -- it
  # was never deactivated, since AdaptationController only deactivates
  # onboard once the edge reports Active, which this edge never does. The
  # edge ROSModule (and its Deployment, via ownerReferences) must be gone,
  # not just marked failed -- rollback tears it down rather than leaving a
  # broken workload behind.
  [[ "$ADAPTATION_STATE" == "RolledBack" ]] || PASS=false
  [[ "$ONBOARD_LIFECYCLE" == "Active" ]] || PASS=false
  "${K[@]}" get rosmodule companion-analytics-drone01-edge -n "$NAMESPACE" \
    >/dev/null 2>&1 && PASS=false
  MODE_DESCRIPTION="Il target edge e' stato deliberatamente rotto (parametro ROS non valido, crash loop). AdaptationController ha rilevato il timeout di readiness e ha eseguito il rollback: istanza edge cancellata, onboard mai disattivato."
else
  [[ "$ADAPTATION_STATE" == "Recovered" ]] || PASS=false
  [[ "$ONBOARD_LIFECYCLE" == "Inactive" ]] || PASS=false
  [[ "$EDGE_LIFECYCLE" == "Active" ]] || PASS=false
  [[ "$EDGE_NODE" == "k3d-cloud-native-p2-agent-1" ]] || PASS=false
  MODE_DESCRIPTION="Nessun bus di eventi discreti: AdaptationController ha osservato direttamente \`status.metrics.latency_p95_ms\` scritto dallo State Bridge onboard, creato la ROSModule edge, atteso la sua convergenza ad Active e disattivato (non eliminato) l'istanza onboard."
fi

cat >"$RESULT_DIR/REPORT.md" <<EOF
# ${EXPERIMENT_MODE^^} Live Result

| Campo | Valore |
| --- | --- |
| Esito | $PASS |
| Variante | B (dichiarativa: CRD + Fleet Operator) |
| Modalita' | $EXPERIMENT_MODE |
| Cluster | $CLUSTER |
| Namespace | $NAMESPACE |
| Durata osservata | $((END_EPOCH - START_EPOCH)) s |
| Stato AdaptationPolicy finale | $ADAPTATION_STATE |
| ROSModule edge | $EDGE_MODULE |
| Nodo edge osservato | $EDGE_NODE |
| Lifecycle onboard | $ONBOARD_LIFECYCLE |
| Lifecycle edge | $EDGE_LIFECYCLE |
| PX4 UID prima | $PX4_UID_BEFORE |
| PX4 UID dopo | $PX4_UID_AFTER |
| PX4 restart prima/dopo | $PX4_RESTARTS_BEFORE / $PX4_RESTARTS_AFTER |
| Audit trail (Audit Writer/Operator Notifier) | presente (incident_started/completed, operator_notification) |
| Incidente correlato (policy, esito, audit, notifica) | ${INCIDENT_CHECK} |
| HPA edge | $HPA_STATUS |

$MODE_DESCRIPTION
EOF

echo "${EXPERIMENT_MODE^^} result (variant B): $PASS"
echo "Evidence: $RESULT_DIR"
[[ "$PASS" == "true" ]]

fi
