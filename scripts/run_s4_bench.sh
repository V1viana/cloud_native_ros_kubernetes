#!/usr/bin/env bash
# S4 bench runner (R13, docs/R13_S4_CONTRACT_DRAFT.md, docs/R13_S4_EDGE_PILOT_PROTOCOL.md).
#
#   VARIANT=a|b S4_MODE=pilot|guard-test bash scripts/run_s4_bench.sh
#   VARIANT=a|b S4_MODE=cell S4_CELL=l1-battery|l1-telemetry|l1-edge|l3 bash scripts/run_s4_bench.sh
#
# One fresh cluster `cloud-native-s4` per run: four onboard robots (drone01-04), one
# edge node, one control plane (scripts/render_s4_bench.py). Refuses to start if the
# cluster exists, another campaign runs, or the project inputs are dirty. Steps:
#   1. render the bench, create the cluster, build and import the images;
#   2. pin k3s's system services to the control plane (as S2);
#   3. bootstrap the variant: A KubeROS + the S4 imperative control plane + four
#      ApplicationDeployments (drone03's migration template: P2's working edge);
#      B the CRDs + Fleet Operator + four ROSModules + RobotFleet + the policies;
#      no fault harness (the battery fault is not part of the edge pilot);
#   4. the health prober on the control plane, host observers (inventory, nodes, the
#      control plane's logs, PX4 of the four onboard nodes);
#   5. the driver (scripts/s4_pilot.py): pilot or guard-test;
#   6. evidence collected, the pilot's facts (scripts/s4_pilot_report.py), the cluster
#      deleted (only the one this run created).
# Results in results/s4/<UTC>-<variant>-<mode>/. Exit: the driver's, 1 on setup failure.
set -euo pipefail

ROOT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
VARIANT=${VARIANT:?set VARIANT=a|b}
S4_MODE=${S4_MODE:-pilot}
S4_CELL=${S4_CELL:-}
CLUSTER=cloud-native-s4
CTX=k3d-$CLUSTER
NS=cloud-native-p2
SERVER=k3d-$CLUSTER-server-0
EDGE=k3d-$CLUSTER-agent-4
RUN_ID=$(date -u +%Y%m%dT%H%M%SZ)
RESULT_DIR="${S4_RESULTS_ROOT:-$ROOT_DIR/results/s4}/$RUN_ID-$VARIANT-${S4_CELL:-$S4_MODE}"   # the root: tests only
RENDERED="$RESULT_DIR/rendered"
OBS="$RESULT_DIR/observers"
STOP="$RESULT_DIR/observers.stop"
K=(kubectl --context "$CTX" -n "$NS")
OBSERVER_PIDS=(); CLUSTER_OURS=""; COLLECTED=""

case "$VARIANT" in a|b) ;; *) echo "VARIANT must be a or b" >&2; exit 2;; esac
case "$S4_MODE" in pilot|guard-test) ;; cell)
  case "$S4_CELL" in l1-battery|l1-telemetry|l1-edge|l3) ;; *) echo "S4_CELL must be l1-battery, l1-telemetry, l1-edge or l3" >&2; exit 2;; esac;;
  *) echo "S4_MODE must be pilot, guard-test or cell" >&2; exit 2;; esac
mkdir -p "$RESULT_DIR" "$RENDERED" "$OBS"
exec > >(tee -a "$RESULT_DIR/run.log") 2>&1
log() { echo "$(date -u +%H:%M:%S) $*"; }
echo "Result dir: $RESULT_DIR"

stop_observers() {
  touch "$STOP"
  local deadline=$((SECONDS + 15))
  for pid in "${OBSERVER_PIDS[@]}"; do
    while kill -0 "$pid" 2>/dev/null && [[ $SECONDS -lt $deadline ]]; do sleep 0.2; done
    if kill -0 "$pid" 2>/dev/null; then
      kill -KILL "$pid" 2>/dev/null || true
      echo "observer $pid killed after the stop budget" >>"$RESULT_DIR/observers-killed.txt"
    fi
    wait "$pid" 2>/dev/null || true
  done
  OBSERVER_PIDS=()
}

collect_evidence() {
  [[ -n "$COLLECTED" ]] && return 0
  COLLECTED=1
  stop_observers
  mkdir -p "$RESULT_DIR/logs"
  docker exec "$SERVER" cat /var/lib/s4-prober/health.jsonl >"$RESULT_DIR/prober-health.jsonl" 2>/dev/null || true
  docker exec "$SERVER" cat /var/lib/s4-observer/faults.jsonl >"$RESULT_DIR/faults.jsonl" 2>/dev/null || true
  "${K[@]}" get pods -o json >"$RESULT_DIR/pods-end.json" 2>/dev/null || true
  kubectl --context "$CTX" get pods -A -o wide >"$RESULT_DIR/pods-end-all.txt" 2>/dev/null || true
  "${K[@]}" get events -o json >"$RESULT_DIR/events.json" 2>/dev/null || true
  kubectl --context "$CTX" get events -A -o json >"$RESULT_DIR/events-all.json" 2>/dev/null || true
  kubectl --context "$CTX" get nodes -o json >"$RESULT_DIR/nodes-end.json" 2>/dev/null || true
  "${K[@]}" get deployments,replicasets,configmaps -o yaml >"$RESULT_DIR/kubernetes-resources.yaml" 2>/dev/null || true
  local deployments
  if [[ "$VARIANT" == "a" ]]; then
    deployments="operational-event-dispatcher-s4 application-manager-s4 kuberos"
  else
    deployments="fleet-operator"
    "${K[@]}" get rosmodule,adaptationpolicy,robotfleet -o yaml >"$RESULT_DIR/declarative-resources.yaml" 2>/dev/null || true
  fi
  deployments="$deployments s4-battery-fault-harness-drone01 s4-fault-observer drone02-microxrce-agent"
  [[ "$VARIANT" == "b" ]] && deployments="$deployments s4-battery-event-detector-drone01"
  for d in $deployments; do
    "${K[@]}" logs "deployment/$d" --all-containers --timestamps >"$RESULT_DIR/logs/$d.log" 2>&1 || true
  done
  for pod in $("${K[@]}" get pods -o name 2>/dev/null | grep drone03 || true); do
    "${K[@]}" logs "$pod" --all-containers --timestamps >"$RESULT_DIR/logs/${pod#pod/}.log" 2>&1 || true
    "${K[@]}" logs "$pod" --all-containers --timestamps --previous >"$RESULT_DIR/logs/${pod#pod/}-previous.log" 2>&1 || true
  done
  docker inspect "$EDGE" >"$RESULT_DIR/edge-inspect-end.json" 2>/dev/null || true
  inputs >"$RESULT_DIR/inputs-end.json"
}

cleanup() {
  local rc=$?
  collect_evidence || true
  if [[ -n "$CLUSTER_OURS" && "${S4_KEEP_CLUSTER:-0}" != "1" ]]; then
    # the edge node may be the one left stopped by a failure: k3d deletes it anyway
    k3d cluster delete "$CLUSTER" >>"$RESULT_DIR/run.log" 2>&1 || true
  fi
  echo "exit=$rc" >"$RESULT_DIR/exit.txt"
}
trap cleanup EXIT

inputs() {  # the revision, the tracked changes outside the thesis and results, the bench's hashes
  python3 - "$ROOT_DIR" <<'EOF'
import hashlib, json, subprocess, sys
root = sys.argv[1]
def git(*a):
    return subprocess.run(["git", "-C", root, *a], capture_output=True, text=True).stdout
dirty = [l for l in git("status", "--porcelain").splitlines()
         if not l[3:].startswith(("Casale/thesis/", "results/"))]
bench = ["scripts/run_s4_bench.sh", "scripts/render_s4_bench.py", "scripts/s4_pilot.py", "scripts/s4_edge.py",
         "scripts/s2_observers.py", "scripts/s2_control.py", "scripts/s2_phase.py",
         "scripts/render_e0_manifests.py", "scripts/render_s3_imperative_manifests.py",
         "scripts/render_s3_declarative_manifests.py", "scripts/render_s3_k3d_config.py",
         "scripts/render_s4_manifests.py", "manifests/kuberos/e0/drone-baseline.template.yaml",
         "manifests/kuberos/p2/analytics-edge.yaml", "manifests/kubernetes/s4/25-s4-health-prober.yaml",
         "manifests/kubernetes/s4/30-imperative-control-plane.yaml", "manifests/kubernetes/s4/50-bootstrap.yaml",
         "sources/s2_harness/s2_harness/prober.py", "sources/s2_harness/s2_harness/prober_core.py",
         "scripts/s4_cell.py", "scripts/s4_judge.py", "scripts/s4_pilot_report.py", "scripts/mission_continuity.py",
         "scripts/px4_status_continuity.py", "manifests/kubernetes/s4/26-s4-fault-observer.yaml",
         "manifests/kubernetes/s4/40-battery-fault-harness-drone01-imperative.yaml",
         "manifests/kubernetes/s4/80-battery-fault-drone01.yaml",
         "sources/e1_battery_fault_harness/e1_battery_fault_harness/battery_fault_harness.py",
         "sources/e1_battery_fault_harness/e1_battery_fault_harness/fault_observer.py"]
hashes = {}
for f in bench:
    with open(f"{root}/{f}", "rb") as h:
        hashes[f] = hashlib.sha256(h.read()).hexdigest()
print(json.dumps({"head": git("rev-parse", "HEAD").strip(), "dirty": dirty, "bench": hashes}, indent=1))
EOF
}

# ---- preflight ----
# a JSON list of the processes really running a test script, this runner excluded
OTHERS=$(python3 "$ROOT_DIR/scripts/s2_record.py" concurrent-runs "$$") \
  || { echo "cannot list the processes" >&2; exit 1; }
# the matrix that launched this cell (S4_MATRIX_PID, set by run_s4_matrix.sh) is our
# ancestor, not another run: excused only if that PID really runs run_s4_matrix.sh
OTHERS=$(python3 -c 'import json, os, re, sys
others = json.loads(sys.argv[1])
matrix = os.environ.get("S4_MATRIX_PID", "")
print(json.dumps([o for o in others if not (str(o["pid"]) == matrix
                  and any(re.fullmatch(r"(?:\S*/)?scripts/run_s4_matrix\.sh", a) for a in o["argv"]))]))' "$OTHERS")
[[ "$OTHERS" == "[]" ]] || { echo "another run is active: $OTHERS" >&2; exit 1; }
if k3d cluster list --no-headers 2>/dev/null | awk '{print $1}' | grep -qx "$CLUSTER"; then
  echo "cluster $CLUSTER exists: not ours, refusing" >&2; exit 1
fi
inputs >"$RESULT_DIR/inputs-start.json"
if python3 -c 'import json,sys; sys.exit(0 if json.load(open(sys.argv[1]))["dirty"] else 1)' "$RESULT_DIR/inputs-start.json"; then
  echo "project inputs are dirty (see inputs-start.json): S4 runs from a committed tree" >&2; exit 1
fi
{ docker version --format '{{.Server.Version}}'; k3d version; kubectl version --client; } >"$RESULT_DIR/versions.txt" 2>&1 || true

# ---- 1. cluster and images ----
log "render and create $CLUSTER"
python3 "$ROOT_DIR/scripts/render_s4_bench.py" --variant "$VARIANT" --discovery-address 0.0.0.0 \
  --output-dir "$RENDERED/pre" >/dev/null
CLUSTER_OURS=1
k3d cluster create --config "$RENDERED/pre/k3d-config.yaml"
# datastore of the bench, embedded etcd (R14 target, docs/ETCD_GATE_PREREGISTRATION.md):
# fail-closed, before any workload or injection
python3 "$ROOT_DIR/scripts/datastore_check.py" check "$CLUSTER" "$RESULT_DIR" \
  || { echo "datastore of $CLUSTER not verified as embedded etcd: stopping before any workload" >&2; exit 2; }
DISCOVERY=$(kubectl --context "$CTX" get node "$SERVER" -o jsonpath='{.status.addresses[?(@.type=="InternalIP")].address}')
python3 "$ROOT_DIR/scripts/render_s4_bench.py" --variant "$VARIANT" --discovery-address "$DISCOVERY" \
  --output-dir "$RENDERED" >/dev/null

IMAGES=(cloud-native-ros/control-plane:p2 cloud-native-ros/event-detector:p2 cloud-native-ros/s2-harness:p2)
log "build images"
docker build -q -t cloud-native-ros/control-plane:p2 -f "$ROOT_DIR/containers/control-plane/Dockerfile" "$ROOT_DIR" >/dev/null
docker build -q -t cloud-native-ros/event-detector:p2 -f "$ROOT_DIR/containers/event-detector/Dockerfile" "$ROOT_DIR" >/dev/null
docker build -q -t cloud-native-ros/s2-harness:p2 -f "$ROOT_DIR/containers/s2-harness/Dockerfile" "$ROOT_DIR" >/dev/null
if [[ "$VARIANT" == "a" ]]; then
  docker build -q -t cloud-native-ros/kuberos:p2 -f "$ROOT_DIR/containers/kuberos/Dockerfile" "$ROOT_DIR" >/dev/null
  IMAGES+=(cloud-native-ros/kuberos:p2 redis:7 ros:humble-ros-base)
else
  docker build -q -t cloud-native-ros/fleet-operator:p2 -f "$ROOT_DIR/containers/fleet-operator/Dockerfile" "$ROOT_DIR" >/dev/null
  docker build -q -t cloud-native-ros/state-bridge:p2 -f "$ROOT_DIR/containers/state-bridge/Dockerfile" "$ROOT_DIR" >/dev/null
  IMAGES+=(cloud-native-ros/fleet-operator:p2 cloud-native-ros/state-bridge:p2)
fi
IMAGES+=(microros/micro-ros-agent:humble px4io/px4-sitl:latest)
for image in "${IMAGES[@]}"; do
  printf '%s %s\n' "$image" "$(docker image inspect -f '{{.Id}}' "$image")"
done >"$RESULT_DIR/images.txt"
log "import images"
k3d image import -c "$CLUSTER" "${IMAGES[@]}" >>"$RESULT_DIR/run.log" 2>&1

# ---- 2. system services on the control plane ----
for d in coredns metrics-server local-path-provisioner; do
  kubectl --context "$CTX" -n kube-system patch deployment "$d" --type merge \
    -p '{"spec":{"template":{"spec":{"nodeSelector":{"kuberos.io/role":"control_plane"}}}}}' >/dev/null
  kubectl --context "$CTX" -n kube-system rollout status "deployment/$d" --timeout=180s >/dev/null
done

# ---- 3. the variant ----
render_p2() { sed -e "s/__P2_DISCOVERY_ADDRESS__/$DISCOVERY/g" "$1"; }
kubectl --context "$CTX" create namespace "$NS" --dry-run=client -o yaml | kubectl --context "$CTX" apply -f - >/dev/null
if [[ "$VARIANT" == "a" ]]; then
  log "bootstrap A"
  kubectl --context "$CTX" apply -f "$ROOT_DIR/manifests/kubernetes/p2/00-rbac.yaml" >/dev/null
  kubectl --context "$CTX" apply -f "$ROOT_DIR/manifests/kubernetes/s4/00-rbac-imperative.yaml" >/dev/null
  kubectl --context "$CTX" apply -f "$ROOT_DIR/manifests/kubernetes/p2/10-discovery.yaml" >/dev/null
  kubectl --context "$CTX" apply -f "$RENDERED/kuberos-manifests/20-kuberos.yaml" >/dev/null
  kubectl --context "$CTX" apply -f "$ROOT_DIR/manifests/kubernetes/p2/25-observability.yaml" >/dev/null
  for d in p2-fastdds-discovery kuberos p2-audit-writer p2-operator-notifier p2-platform-observer; do
    "${K[@]}" rollout status "deployment/$d" --timeout=240s >/dev/null
  done
  "${K[@]}" wait --for=create secret/kuberos-api-token --timeout=90s >/dev/null
  kubectl --context "$CTX" apply -f "$ROOT_DIR/manifests/kubernetes/s4/10-diagnostic-job-template.yaml" >/dev/null
  kubectl --context "$CTX" apply -f "$RENDERED/20-analytics-edge-drone03.yaml" >/dev/null
  render_p2 "$ROOT_DIR/manifests/kubernetes/s4/30-imperative-control-plane.yaml" >"$RENDERED/30-imperative-control-plane.yaml"
  kubectl --context "$CTX" apply -f "$RENDERED/30-imperative-control-plane.yaml" >/dev/null
  "${K[@]}" rollout status deployment/application-manager-s4 --timeout=120s >/dev/null
  "${K[@]}" rollout status deployment/operational-event-dispatcher-s4 --timeout=120s >/dev/null
  "${K[@]}" create configmap s4-kuberos-manifests \
    $(for r in drone01 drone02 drone03 drone04; do printf -- '--from-file=%s.yaml=%s ' "$r" "$RENDERED/kuberos-manifests/$r.yaml"; done) \
    --dry-run=client -o yaml | kubectl --context "$CTX" apply -f - >/dev/null
  sed -e 's|--timeout-sec 240|--timeout-sec 240 --expected-count 4|' \
    "$ROOT_DIR/manifests/kubernetes/s4/50-bootstrap.yaml" >"$RENDERED/50-bootstrap.yaml"
  grep -q -- '--expected-count 4' "$RENDERED/50-bootstrap.yaml"
  kubectl --context "$CTX" apply -f "$RENDERED/50-bootstrap.yaml" >/dev/null
  "${K[@]}" wait --for=condition=complete job/s4-kuberos-bootstrap --timeout=900s >/dev/null
  "${K[@]}" wait --for=condition=available deployment --all --timeout=300s >/dev/null
else
  log "bootstrap B"
  kubectl --context "$CTX" apply -f "$ROOT_DIR/operator/crds/" >/dev/null
  kubectl --context "$CTX" wait --for=condition=Established --timeout=60s \
    crd/rosmodules.dronekube.io crd/robotfleets.dronekube.io \
    crd/roslifecyclepolicies.dronekube.io crd/adaptationpolicies.dronekube.io >/dev/null
  kubectl --context "$CTX" apply -f "$ROOT_DIR/manifests/kubernetes/p2/00-rbac.yaml" >/dev/null
  kubectl --context "$CTX" apply -f "$ROOT_DIR/manifests/kubernetes/p2/10-discovery.yaml" >/dev/null
  render_p2 "$ROOT_DIR/manifests/kubernetes/p2/50-declarative-control-plane.yaml" >"$RENDERED/50-declarative-control-plane.yaml"
  kubectl --context "$CTX" apply -f "$RENDERED/50-declarative-control-plane.yaml" >/dev/null
  kubectl --context "$CTX" apply -f "$ROOT_DIR/manifests/kubernetes/p2/25-observability.yaml" >/dev/null
  for d in p2-fastdds-discovery fleet-operator p2-audit-writer p2-operator-notifier p2-platform-observer; do
    "${K[@]}" rollout status "deployment/$d" --timeout=180s >/dev/null
  done
  kubectl --context "$CTX" apply -f "$RENDERED/40-shared-infra.yaml" >/dev/null
  for r in drone01 drone02 drone03 drone04; do
    "${K[@]}" rollout status "deployment/$r-px4-sitl" --timeout=180s >/dev/null
    "${K[@]}" rollout status "deployment/$r-microxrce-agent" --timeout=180s >/dev/null
  done
  kubectl --context "$CTX" apply -f "$RENDERED/60-declarative-workload.yaml" >/dev/null
  kubectl --context "$CTX" apply -f "$RENDERED/70-robotfleet.yaml" >/dev/null
  for r in drone01 drone02 drone03 drone04; do
    state=""
    for _ in $(seq 1 90); do
      state=$("${K[@]}" get rosmodule "companion-analytics-$r" -o jsonpath='{.status.observedLifecycleState}' 2>/dev/null || true)
      [[ "$state" == "Active" ]] && break
      sleep 2
    done
    [[ "$state" == "Active" ]] || { echo "companion-analytics-$r never reached Active" >&2; exit 1; }
  done
  kubectl --context "$CTX" apply -f "$RENDERED/70-adaptationpolicies.yaml" >/dev/null
  kubectl --context "$CTX" apply -f "$RENDERED/80-battery-detector.yaml" >/dev/null
  "${K[@]}" rollout status deployment/s4-battery-event-detector-drone01 --timeout=180s >/dev/null
fi
kubectl --context "$CTX" apply -f "$RENDERED/26-s4-fault-observer.yaml" >/dev/null
"${K[@]}" rollout status deployment/s4-fault-observer --timeout=120s >/dev/null

# ---- 4. prober and observers ----
sed -e "s/__P2_DISCOVERY_ADDRESS__/$DISCOVERY/g" -e "s/__S4_RUN_ID__/$RUN_ID/g" \
  "$ROOT_DIR/manifests/kubernetes/s4/25-s4-health-prober.yaml" >"$RENDERED/25-s4-health-prober.yaml"
kubectl --context "$CTX" apply -f "$RENDERED/25-s4-health-prober.yaml" >/dev/null
"${K[@]}" rollout status deployment/s4-health-prober --timeout=120s >/dev/null
"${K[@]}" get pods -o json >"$RESULT_DIR/pods-start.json"
kubectl --context "$CTX" get pods -A -o wide >"$RESULT_DIR/pods-start-all.txt"
O=(python3 "$ROOT_DIR/scripts/s2_observers.py")
"${O[@]}" inventory --variant "$VARIANT" --context "$CTX" --out "$OBS/inventory.jsonl" --stop-file "$STOP" & OBSERVER_PIDS+=($!)
"${O[@]}" nodes --context "$CTX" --out "$OBS/nodes.jsonl" --stop-file "$STOP" & OBSERVER_PIDS+=($!)
if [[ "$VARIANT" == "a" ]]; then
  FOLLOW=(operational-event-dispatcher-s4:dispatcher application-manager-s4:manager)
else
  FOLLOW=(fleet-operator:)
fi
for spec in "${FOLLOW[@]}"; do
  dep=${spec%%:*}; container=${spec#*:}
  "${O[@]}" logs --context "$CTX" --deployment "$dep" --container "$container" --out "$OBS/logs-$dep.jsonl" \
    --stop-file "$STOP" & OBSERVER_PIDS+=($!)
done
for i in 1 2 3 4; do
  "${O[@]}" px4 --node "k3d-$CLUSTER-agent-$((i - 1))" --out "$OBS/px4-drone0$i.txt" --stop-file "$STOP" \
    & OBSERVER_PIDS+=($!)
done

# ---- 5. the driver ----
log "driver $S4_MODE $S4_CELL"
set +e
if [[ "$S4_MODE" == "cell" ]]; then
  python3 "$ROOT_DIR/scripts/s4_cell.py" --result-dir "$RESULT_DIR" --variant "$VARIANT" --cell "$S4_CELL" \
    --discovery-address "$DISCOVERY" --context "$CTX" --namespace "$NS" --edge-node "$EDGE" --prober-node "$SERVER"
else
  python3 "$ROOT_DIR/scripts/s4_pilot.py" --result-dir "$RESULT_DIR" --variant "$VARIANT" --mode "$S4_MODE" \
    --context "$CTX" --namespace "$NS" --edge-node "$EDGE" --prober-node "$SERVER"
fi
DRIVER_RC=$?
set -e
log "driver exit $DRIVER_RC"
if [[ "$S4_MODE" == "guard-test" ]]; then
  # the driver killed itself after the stop: only the guard can restart the node
  for _ in $(seq 1 90); do
    [[ -e "$RESULT_DIR/guard-running" ]] && break
    sleep 1
  done
  ready=""
  for _ in $(seq 1 120); do
    ready=$(kubectl --context "$CTX" get node "$EDGE" -o jsonpath='{.status.conditions[?(@.type=="Ready")].status}' 2>/dev/null || true)
    [[ "$ready" == "True" ]] && break
    sleep 1
  done
  python3 - "$RESULT_DIR" "$ready" <<'EOF' | tee "$RESULT_DIR/guard-test.json"
import json, sys
sys.path.insert(0, sys.argv[1].rsplit("/results/", 1)[0] + "/scripts")
import s4_edge
record = s4_edge.guard_record(sys.argv[1])
print(json.dumps({"guard": record, "node_ready_after": sys.argv[2],
                  "ok": bool(record and record.get("start_exit") == "0" and record.get("running") == "true"
                             and sys.argv[2] == "True")}, indent=1))
EOF
fi
# the observers run 10 s past horizon_end before the collection: the PX4 continuity
# check needs a read after the window; the functional window itself does not grow
if [[ "$S4_MODE" == "pilot" || "$S4_MODE" == "cell" ]]; then
  sleep "${S4_OBSERVER_TAIL_SEC:-10}"
fi
if [[ "$S4_MODE" == "pilot" ]]; then
  collect_evidence
  python3 "$ROOT_DIR/scripts/s4_pilot_report.py" "$RESULT_DIR" --variant "$VARIANT" --edge-node "$EDGE" || true
fi
if [[ "$S4_MODE" == "cell" ]]; then
  collect_evidence
  set +e
  python3 "$ROOT_DIR/scripts/s4_judge.py" "$RESULT_DIR" --variant "$VARIANT" --cell "$S4_CELL"
  JUDGE_RC=$?
  set -e
  echo "judge=$JUDGE_RC" >"$RESULT_DIR/judge-exit.txt"
fi
exit "$DRIVER_RC"
