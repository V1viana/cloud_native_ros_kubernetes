#!/usr/bin/env bash
set -euo pipefail

# S1: out-of-band drift on a managed workload (proposal's own MTTR-drift
# metric). A single target Deployment -- drone01's onboard analytics, an
# arbitrary single-robot choice matching S4's own precedent -- is changed
# directly via kubectl, bypassing both control planes entirely: no ROS event,
# no fault harness, nothing application-level involved.
#
# Proposal's expectation, a hypothesis to verify and not a condition imposed
# on A's result: A never notices (KubeROS has no reconciliation loop over an
# already-created ApplicationDeployment -- its inventory is written once and
# never reconciled against live cluster state), B's Fleet Operator repairs
# it at its ROSModule resync (30s timer; the operator does not watch
# Deployments).
#
# Reuses E0's own 3-drone cluster/bootstrap unmodified (same pattern as
# run_s4.sh: base fleet up, then this scenario's own single action) --
# no new topology, no new images.
#
# R10 (docs/R10_S1_DRIFT.md, Viviana 2026-09-26): two cases, the same command
# in A and B -- S1_CASE=delete (kubectl delete deployment) or S1_CASE=scale
# (kubectl scale --replicas=0) -- each on a fresh cluster (RESET_S1=1). One
# deadline, S1_WINDOW_SEC (180) from the start of the injection, for every
# observation; collection continues to the deadline. The collector
# (scripts/s1_drift_observer.py) records the injection, the inventory about
# every second and GetHealthSnapshot on /<robot>/companion/onboard/health from
# drone02's analytics Pod, with monotonic and UTC times; the judge
# (scripts/s1_drift_judge.py) keeps validity, recovery and isolation apart.
# A not recovered is reported as such within the window -- never an infinite
# MTTR. Exit: 0 PASS, 1 FAIL, 2 INCONCLUSIVE.

ROOT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
CLUSTER=cloud-native-p2
CONTEXT=k3d-cloud-native-p2
NAMESPACE=cloud-native-p2
VARIANT=${VARIANT:-a}
S1_CASE=${S1_CASE:-delete}
WINDOW_SEC=${S1_WINDOW_SEC:-180}
RESULT_ID=$(date -u +%Y%m%dT%H%M%SZ)
RESULT_DIR="$ROOT_DIR/results/s1/$RESULT_ID"
RENDERED_DIR="$RESULT_DIR/rendered"
TARGET_ROBOT=drone01
UNINVOLVED_ROBOTS=(drone02 drone03)
# Naming differs by variant even for "the same" onboard workload: KubeROS
# names it "<robot>-companion-analytics-onboard" (drone-baseline.template.
# yaml's own module name suffix), Fleet Operator names its Deployment
# after the owning ROSModule instead, "companion-analytics-<robot>" (no
# "-onboard" suffix, no separate edge-vs-onboard distinction in the name
# itself) -- found live, run_s4.sh's own equivalent already keys off this
# same distinction per variant.
analytics_deployment() {
  if [[ "$VARIANT" == "a" ]]; then echo "$1-companion-analytics-onboard"; else echo "companion-analytics-$1"; fi
}
TARGET_DEPLOYMENT=$(analytics_deployment "$TARGET_ROBOT")
if [[ "$VARIANT" == "a" ]]; then OBSERVER_CONTAINER=companion-analytics-onboard; TARGET_MODULE=""
else OBSERVER_CONTAINER=companion-analytics; TARGET_MODULE="companion-analytics-$TARGET_ROBOT"; fi
K=(kubectl --context "$CONTEXT")
mkdir -p "$RESULT_DIR" "$RENDERED_DIR"
echo "Result dir: $RESULT_DIR"

case "$VARIANT" in
  a|b) ;;
  *)
    echo "Unsupported VARIANT: $VARIANT (expected 'a' or 'b')" >&2
    exit 2
    ;;
esac
case "$S1_CASE" in
  delete|scale) ;;
  *)
    echo "Unsupported S1_CASE: $S1_CASE (expected 'delete' or 'scale')" >&2
    exit 2
    ;;
esac

for command in docker k3d kubectl python3; do
  command -v "$command" >/dev/null || {
    echo "Required command not found: $command" >&2
    exit 1
  }
done

cluster_exists() {
  k3d cluster list --no-headers | awk '{print $1}' | grep -qx "$CLUSTER"
}

if [[ "${RESET_S1:-0}" == "1" ]] && cluster_exists; then
  k3d cluster delete "$CLUSTER"
fi
if ! cluster_exists; then
  k3d cluster create --config "$ROOT_DIR/manifests/kubernetes/e0/k3d-cloud-native-e0.yaml"
else
  NODE_COUNT=$("${K[@]}" get nodes --no-headers | wc -l)
  ONBOARD_COUNT=$("${K[@]}" get nodes -l kuberos.io/role=onboard --no-headers | wc -l)
  if [[ "$NODE_COUNT" != "5" || "$ONBOARD_COUNT" != "3" ]]; then
    echo "Cluster $CLUSTER has an incompatible topology; rerun with RESET_S1=1" >&2
    exit 1
  fi
fi
# datastore of the bench, embedded etcd (R14 target, docs/ETCD_GATE_PREREGISTRATION.md):
# fail-closed, before any workload or injection
python3 "$ROOT_DIR/scripts/datastore_check.py" check "$CLUSTER" "$RESULT_DIR" \
  || { echo "datastore of $CLUSTER not verified as embedded etcd: stopping before any workload" >&2; exit 2; }
"${K[@]}" wait --for=condition=Ready node --all --timeout=120s

DISCOVERY_SERVER_ADDRESS=$("${K[@]}" get node k3d-cloud-native-p2-server-0 \
  -o jsonpath='{.status.addresses[?(@.type=="InternalIP")].address}')

# ---- R10: one injection, one window, the same collection in A and B ----
observe_and_judge() {
  local uninvolved_deployments=() robot verdict
  for robot in "${UNINVOLVED_ROBOTS[@]}"; do uninvolved_deployments+=("$(analytics_deployment "$robot")"); done
  python3 "$ROOT_DIR/scripts/s1_drift_observer.py" --variant "$VARIANT" --case "$S1_CASE" \
    --context "$CONTEXT" --namespace "$NAMESPACE" \
    --target-robot "$TARGET_ROBOT" --target-deployment "$TARGET_DEPLOYMENT" --target-module "$TARGET_MODULE" \
    --uninvolved-robots "${UNINVOLVED_ROBOTS[@]}" --uninvolved-deployments "${uninvolved_deployments[@]}" \
    --observer-deployment "$(analytics_deployment drone02)" --observer-container "$OBSERVER_CONTAINER" \
    --window "$WINDOW_SEC" --out "$RESULT_DIR/s1-samples.jsonl"
  "${K[@]}" get events -n "$NAMESPACE" -o json >"$RESULT_DIR/events.json" 2>/dev/null || true
  "${K[@]}" get events -n "$NAMESPACE" --sort-by=.lastTimestamp >"$RESULT_DIR/kubernetes-events.txt" 2>&1 || true
  "${K[@]}" exec -n "$NAMESPACE" deployment/p2-audit-writer -- cat /data/audit.jsonl \
    >"$RESULT_DIR/audit.jsonl" 2>/dev/null || true
  "${K[@]}" get pods -n "$NAMESPACE" -o wide >"$RESULT_DIR/pods.txt"
  "${K[@]}" get pods -n "$NAMESPACE" -o json >"$RESULT_DIR/pods.json"
  "${K[@]}" get deployments -n "$NAMESPACE" -o yaml >"$RESULT_DIR/kubernetes-resources.yaml"
  if [[ "$VARIANT" == "b" ]]; then
    "${K[@]}" get rosmodule,robotfleet -n "$NAMESPACE" -o yaml >"$RESULT_DIR/declarative-resources.yaml"
    "${K[@]}" logs -n "$NAMESPACE" deployment/fleet-operator >"$RESULT_DIR/fleet-operator.log" 2>&1 || true
  else
    "${K[@]}" logs -n "$NAMESPACE" deployment/kuberos -c api >"$RESULT_DIR/kuberos-api.log" 2>&1 || true
  fi
  python3 "$ROOT_DIR/scripts/s1_drift_judge.py" "$RESULT_DIR/s1-samples.jsonl" --variant "$VARIANT" \
    --case "$S1_CASE" --target-robot "$TARGET_ROBOT" --target-deployment "$TARGET_DEPLOYMENT" \
    --target-module "$TARGET_MODULE" --uninvolved-robots "${UNINVOLVED_ROBOTS[@]}" \
    --uninvolved-deployments "${uninvolved_deployments[@]}" \
    --events "$RESULT_DIR/events.json" --audit "$RESULT_DIR/audit.jsonl" >"$RESULT_DIR/s1-judge.json"
  verdict=$(python3 -c "import json; print(json.load(open('$RESULT_DIR/s1-judge.json'))['verdict'])")
  python3 "$ROOT_DIR/scripts/s1_drift_judge.py" --report "$RESULT_DIR/s1-judge.json" "$VARIANT" "$S1_CASE" \
    "$TARGET_DEPLOYMENT" >"$RESULT_DIR/REPORT.md"
  echo "S1 result (variant ${VARIANT^^}, case $S1_CASE): $verdict"
  echo "Evidence: $RESULT_DIR"
  case "$verdict" in PASS) return 0 ;; INCONCLUSIVE) return 2 ;; *) return 1 ;; esac
}

if [[ "$VARIANT" == "a" ]]; then
# =====================================================================
# VARIANT A: imperativo. Bootstrap KubeROS a tre droni, identico a
# run_e0.sh (nessuna aggiunta S1-specifica fino al momento del drift).
# =====================================================================

python3 "$ROOT_DIR/scripts/render_e0_manifests.py" \
  --output-dir "$RENDERED_DIR" \
  --discovery-address "$DISCOVERY_SERVER_ADDRESS"

sed \
  -e "s/__P2_DISCOVERY_ADDRESS__/$DISCOVERY_SERVER_ADDRESS/g" \
  -e "s/__P2_GOAL_TIMEOUT_SEC__/120.0/g" \
  "$ROOT_DIR/manifests/kubernetes/p2/30-control-plane.yaml" \
  >"$RENDERED_DIR/30-control-plane.yaml"
sed "s/__P2_DISCOVERY_ADDRESS__/$DISCOVERY_SERVER_ADDRESS/g" \
  "$ROOT_DIR/manifests/kuberos/p2/analytics-edge.yaml" \
  >"$RENDERED_DIR/analytics-edge.yaml"

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
"${K[@]}" apply -f "$RENDERED_DIR/20-kuberos.yaml"
"${K[@]}" apply -f "$ROOT_DIR/manifests/kubernetes/p2/25-observability.yaml"
"${K[@]}" rollout status deployment/p2-fastdds-discovery -n "$NAMESPACE" --timeout=180s
"${K[@]}" rollout status deployment/kuberos -n "$NAMESPACE" --timeout=300s
"${K[@]}" wait --for=create secret/kuberos-api-token -n "$NAMESPACE" --timeout=90s

"${K[@]}" create configmap p2-analytics-edge-manifest -n "$NAMESPACE" \
  --from-file=analytics-edge.yaml="$RENDERED_DIR/analytics-edge.yaml" \
  --dry-run=client -o yaml | "${K[@]}" apply -f -
"${K[@]}" apply -f "$RENDERED_DIR/30-control-plane.yaml"
"${K[@]}" rollout status deployment/application-manager-p2 -n "$NAMESPACE" --timeout=120s
"${K[@]}" rollout status deployment/operational-event-dispatcher-p2 -n "$NAMESPACE" --timeout=120s

"${K[@]}" create configmap e0-kuberos-manifests -n "$NAMESPACE" \
  --from-file=drone01.yaml="$RENDERED_DIR/drone01.yaml" \
  --from-file=drone02.yaml="$RENDERED_DIR/drone02.yaml" \
  --from-file=drone03.yaml="$RENDERED_DIR/drone03.yaml" \
  --dry-run=client -o yaml | "${K[@]}" apply -f -
"${K[@]}" delete job e0-kuberos-bootstrap -n "$NAMESPACE" --ignore-not-found
"${K[@]}" apply -f "$ROOT_DIR/manifests/kubernetes/e0/30-bootstrap.yaml"
"${K[@]}" wait --for=condition=complete job/e0-kuberos-bootstrap -n "$NAMESPACE" --timeout=900s
"${K[@]}" wait --for=condition=available deployment --all -n "$NAMESPACE" --timeout=300s

observe_and_judge
exit $?

else
# =====================================================================
# VARIANT B: dichiarativo. Bootstrap Fleet Operator a tre droni,
# identico a run_e0.sh (nessuna aggiunta S1-specifica fino al momento
# del drift).
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

sed -e "s/__P2_DISCOVERY_ADDRESS__/$DISCOVERY_SERVER_ADDRESS/g" \
  "$ROOT_DIR/manifests/kubernetes/p2/50-declarative-control-plane.yaml" \
  >"$RENDERED_DIR/50-declarative-control-plane.yaml"
sed -e "s/__P2_DISCOVERY_ADDRESS__/$DISCOVERY_SERVER_ADDRESS/g" \
  "$ROOT_DIR/manifests/kubernetes/e0/40-shared-infra.yaml" \
  >"$RENDERED_DIR/40-shared-infra.yaml"

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
"${K[@]}" apply -f "$ROOT_DIR/manifests/kubernetes/p2/00-rbac.yaml"
"${K[@]}" apply -f "$ROOT_DIR/manifests/kubernetes/p2/10-discovery.yaml"
"${K[@]}" apply -f "$RENDERED_DIR/50-declarative-control-plane.yaml"
"${K[@]}" apply -f "$ROOT_DIR/manifests/kubernetes/p2/25-observability.yaml"
"${K[@]}" rollout status deployment/p2-fastdds-discovery -n "$NAMESPACE" --timeout=180s
"${K[@]}" rollout status deployment/fleet-operator -n "$NAMESPACE" --timeout=120s

"${K[@]}" apply -f "$RENDERED_DIR/40-shared-infra.yaml"
for robot in drone01 drone02 drone03; do
  "${K[@]}" rollout status "deployment/$robot-px4-sitl" -n "$NAMESPACE" --timeout=180s
  "${K[@]}" rollout status "deployment/$robot-microxrce-agent" -n "$NAMESPACE" --timeout=180s
done

"${K[@]}" apply -f "$ROOT_DIR/manifests/kubernetes/e0/60-declarative-workload.yaml"
"${K[@]}" apply -f "$ROOT_DIR/manifests/kubernetes/e0/70-declarative-robotfleet.yaml"

for robot in drone01 drone02 drone03; do
  STATE=""
  for _ in $(seq 1 60); do
    STATE=$("${K[@]}" get rosmodule "companion-analytics-$robot" -n "$NAMESPACE" \
      -o jsonpath='{.status.observedLifecycleState}' 2>/dev/null || true)
    [[ "$STATE" == "Active" ]] && break
    sleep 2
  done
  [[ "$STATE" == "Active" ]] || {
    echo "companion-analytics-$robot never reached Active" >&2
    exit 1
  }
done

# The baseline needs every Deployment available, as variant A already waits for. The
# lifecycle state Active does not imply it: in the R14 campaign (2 October, row 18) the Pod of
# drone03 became Ready about 1 s after the baseline reading and the judge declared the baseline
# not valid. Block S1-delete (docs/S1_DELETE_BLOCK_PROPOSAL_DRAFT.md 4): this wait only; judge,
# observer, window, workload, probes and images are unchanged. A timeout ends the run before the
# baseline and the injection (exit 1, as the Active wait above).
"${K[@]}" wait --for=condition=available deployment --all -n "$NAMESPACE" --timeout=300s || {
  echo "Deployments not available within 300 s before the baseline" >&2
  exit 1
}

observe_and_judge
exit $?

fi
