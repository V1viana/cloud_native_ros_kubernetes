#!/usr/bin/env bash
# S2: network partition of drone01's node during a latency transient (R11,
# contract s2-partition-v1, docs/R11_S2_PARTITION.md). One cell per run, on a
# fresh cluster with E0's topology:
#   VARIANT=a|b  S2_CASE=control|partition-only|short|long|qualification
#
# S2_CASE=qualification runs the bench qualification (scripts/s2_qualify.py,
# judged by scripts/s2_qualify_judge.py) on the same setup. A cell needs
# S2_QUALIFICATION=<a qualification's result dir>: QUALIFIED, same variant,
# same bench files, else it is not started; its topology is recorded and its
# judge compares it with the qualified one.
#
#   1. not started (exit 4) if a k3d cluster exists or another run is active;
#   2. inputs recorded (HEAD, every tracked file's hash, modified files);
#   3. a fresh cluster with S2's configuration (E0's plus the network policy
#      controller off, scripts/render_s2_k3d_config.py), then E0's own bootstrap,
#      unmodified (scripts/run_e0.sh of the source, reusing that cluster), its
#      outcome checked before anything of S2: a failed E0 stops the cell here
#      (exit 2); then the controller verified off (arguments, no KUBE-ROUTER rule);
#   4. the S2 harness image built from the source and imported; image IDs kept;
#   5. A: the dispatcher's opt-in reception trace (OPERATIONAL_EVENT_TRACE=1),
#      then detector Active and the Action server available;
#      B: the S2 AdaptationPolicy for drone01, then a single onboard match,
#      Nominal, no incident, no edge, the window cursor advancing;
#   6. harness (drone01's node) and health prober (edge node) with this run's id,
#      their hostPaths emptied first; the ROS name of the analytics resolved;
#   7. observers started (inventory, nodes, audit, logs, uORB of the three
#      drones, memory of drone01's analytics Pod); the three drones' health;
#   8. the timed phase, scripts/s2_phase.py (clocks, arm, the 30 s reference,
#      PX4 frozen, the case, the horizon);
#   9. evidence collected (on error too), inputs recorded again, the judge
#      (scripts/s2_judge.py) and REPORT.md; the cluster deleted
#      (S2_KEEP_CLUSTER=1 keeps it).
# The rules of a partition are removed on any exit, and `partition-restored`
# is written only after a status read shows none left; the guard started by
# the driver removes them on its own after 120 s.
# Exit: 0 PASS, 1 FAIL, 2 INCONCLUSIVE (setup failures included), 3 INTERRUPTED,
# 4 not started; a qualification: 0 QUALIFIED, 1 NOT_QUALIFIED, 3 INTERRUPTED.
set -uo pipefail

ROOT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
SOURCE=$(cd "${S2_SOURCE:-$ROOT_DIR}" && pwd)
VARIANT=${VARIANT:-}
S2_CASE=${S2_CASE:-}
case "$VARIANT" in a|b) ;; *) echo "VARIANT=a|b" >&2; exit 4 ;; esac
case "$S2_CASE" in control|partition-only|short|long|qualification) ;;
  *) echo "S2_CASE=control|partition-only|short|long|qualification" >&2; exit 4 ;; esac

CLUSTER=cloud-native-p2; CTX=k3d-$CLUSTER; NS=cloud-native-p2
NODE=k3d-cloud-native-p2-agent-0            # drone01
EDGE_NODE=k3d-cloud-native-p2-agent-3
SERVER=k3d-cloud-native-p2-server-0
RESULT_ID="$(date -u +%Y%m%dT%H%M%SZ)-$VARIANT-$S2_CASE"
RUN_ID="s2-$(echo "$RESULT_ID" | tr 'A-Z' 'a-z')"
RESULT_DIR="$ROOT_DIR/results/s2/$RESULT_ID"
OBS="$RESULT_DIR/observers"
RUN_JSON="$RESULT_DIR/run.json"
STOP="$RESULT_DIR/observers.stop"
K=(kubectl --context "$CTX" -n "$NS")
R=(python3 "$ROOT_DIR/scripts/s2_record.py")
mkdir -p "$RESULT_DIR/rendered" "$RESULT_DIR/harness" "$RESULT_DIR/prober" "$OBS"
echo "Result dir: $RESULT_DIR"
log() { echo "$(date -u +%H:%M:%S) $*" | tee -a "$RESULT_DIR/run.log"; }
OBSERVER_PIDS=(); DRIVER_PID=""; E0_PID=""; COLLECTED=""; PHASE_STARTED=""
CLUSTER_OURS=""                  # set when this run creates the cluster: only then is it deleted

ros() {  # a ros2 CLI command in a Deployment's container, bounded
  local deployment=$1; shift
  timeout 40 "${K[@]}" exec "deployment/$deployment" -- /bin/bash -lc \
    "source /ws/install/setup.bash; ROS_SUPER_CLIENT=TRUE ROS2CLI_DISABLE_DAEMON=1 timeout 25 $*" 2>&1
}
cid_of() {  # container id (no runtime prefix) of a Pod's container
  "${K[@]}" get pod "$1" -o jsonpath="{.status.containerStatuses[?(@.name==\"$2\")].containerID}" | sed 's#.*://##'
}
pod_of() {  # the newest Pod of a selector (after a rollout the old one may still be terminating)
  "${K[@]}" get pod -l "$1" --sort-by=.metadata.creationTimestamp -o jsonpath='{.items[-1:].metadata.name}' 2>/dev/null
}
precondition() { "${R[@]}" precondition "$RUN_JSON" "$1" "$2" "$3"; log "precondition $1: $2 ($3)"; }

OBSERVER_STOP_BUDGET_SEC=15      # each observer stops within 5 s; past this, TERM then KILL, recorded
stop_observers() {
  local deadline pid killed=()
  "${R[@]}" set "$RUN_JSON" observers_stop_utc "$(date -u +%s.%N)"
  touch "$STOP"
  deadline=$((SECONDS + OBSERVER_STOP_BUDGET_SEC))
  for pid in "${OBSERVER_PIDS[@]}"; do
    while kill -0 "$pid" 2>/dev/null && (( SECONDS < deadline )); do sleep 0.2; done
    if kill -0 "$pid" 2>/dev/null; then
      pkill -TERM -P "$pid" 2>/dev/null; kill -TERM "$pid" 2>/dev/null; sleep 1
      pkill -KILL -P "$pid" 2>/dev/null; kill -KILL "$pid" 2>/dev/null
      killed+=("$pid")
      log "observer $pid killed after the stop budget"
    fi
    wait "$pid" 2>/dev/null
  done
  "${R[@]}" set "$RUN_JSON" observers_killed "[$(IFS=,; echo "${killed[*]:-}" | sed 's/[0-9][0-9]*/"&"/g')]"
  OBSERVER_PIDS=()
}
remove_partition() {  # last resort: the driver removes its own rules first
  [[ -n "$PHASE_STARTED" && "$S2_CASE" != "control" ]] || return 0
  python3 "$ROOT_DIR/scripts/s2_partition.py" remove --node "$NODE" >>"$RESULT_DIR/run.log" 2>&1
  local status
  status=$(python3 "$ROOT_DIR/scripts/s2_partition.py" status --node "$NODE" 2>>"$RESULT_DIR/run.log")
  echo "$status" >"$RESULT_DIR/partition-final-status.json"
  if python3 -c "import json,sys; s=json.loads(sys.argv[1]); sys.exit(0 if not s['jumps'] and not s['chain'] else 1)" \
      "$status" 2>/dev/null; then
    [[ -e "$RESULT_DIR/partition-restored" ]] || date -u +%s.%N >"$RESULT_DIR/partition-restored"
  else
    log "partition rules still present: the guard removes them"
  fi
}
collect_evidence() {  # once, before the cluster goes: on the normal path and on error
  [[ -n "$COLLECTED" ]] && return 0
  COLLECTED=1
  sleep 3
  ps -o pid,etimes,times,rss,args -p "$(pgrep -d, -f "$RESULT_DIR" || echo 1)" >"$RESULT_DIR/observer-cost.txt" 2>&1
  stop_observers
  docker exec "$NODE" cat /var/lib/s2-harness/samples.jsonl >"$RESULT_DIR/harness/samples.jsonl" 2>>"$RESULT_DIR/run.log"
  docker exec "$NODE" cat /var/lib/s2-harness/events.jsonl >"$RESULT_DIR/harness/events.jsonl" 2>>"$RESULT_DIR/run.log"
  docker exec "$EDGE_NODE" cat /var/lib/s2-prober/health.jsonl >"$RESULT_DIR/prober/health.jsonl" 2>>"$RESULT_DIR/run.log"
  "${K[@]}" get pods -o json >"$RESULT_DIR/pods-end.json" 2>>"$RESULT_DIR/run.log"
  kubectl --context "$CTX" get pods -A -o json 2>>"$RESULT_DIR/run.log" \
    | python3 "$ROOT_DIR/scripts/s2_control.py" placement --node "$NODE" >"$RESULT_DIR/placement-end.json"
  "${K[@]}" get events -o json >"$RESULT_DIR/events.json" 2>>"$RESULT_DIR/run.log"
  "${K[@]}" get deployments,hpa,configmaps -o yaml >"$RESULT_DIR/kubernetes-resources.yaml" 2>>"$RESULT_DIR/run.log"
  "${K[@]}" exec deployment/p2-audit-writer -- cat /data/audit.jsonl >"$RESULT_DIR/audit.jsonl" 2>>"$RESULT_DIR/run.log"
  kubectl --context "$CTX" get nodes -o json >"$RESULT_DIR/nodes-end.json" 2>>"$RESULT_DIR/run.log"
  kubectl --context "$CTX" -n kube-node-lease get leases -o json >"$RESULT_DIR/leases-end.json" 2>>"$RESULT_DIR/run.log"
  local deployments
  if [[ "$VARIANT" == "a" ]]; then
    deployments="operational-event-dispatcher-p2 application-manager-p2 kuberos"
    "${K[@]}" exec deployment/application-manager-p2 -- python3 -c '
import json, sqlite3
c = sqlite3.connect("/var/lib/cloud-native-manager/outbox.sqlite3")
rows = c.execute("SELECT id, url, created_at, attempts, last_error, json_extract(payload_json, \"$.record_type\"), "
                 "json_extract(payload_json, \"$.correlation_id\") FROM delivery_outbox").fetchall()
print(json.dumps([dict(zip(("id", "url", "created_at", "attempts", "last_error", "record_type", "correlation_id"), r))
                  for r in rows]))' >"$RESULT_DIR/manager-outbox.json" 2>>"$RESULT_DIR/run.log"
  else
    deployments="fleet-operator"
    "${K[@]}" get rosmodule,adaptationpolicy,robotfleet -o yaml >"$RESULT_DIR/declarative-resources.yaml" \
      2>>"$RESULT_DIR/run.log"
  fi
  mkdir -p "$RESULT_DIR/logs"
  for d in $deployments; do
    "${K[@]}" logs "deployment/$d" --all-containers --timestamps >"$RESULT_DIR/logs/$d.log" 2>&1
    "${K[@]}" logs "deployment/$d" --all-containers --timestamps --previous >"$RESULT_DIR/logs/$d-previous.log" 2>&1
  done
  # node-local logs of drone01's containers, through crictl (not through the API)
  docker exec "$NODE" sh -c 'for c in $(crictl ps -a -q); do n=$(crictl inspect --output go-template \
    --template "{{.status.metadata.name}}" $c); echo "===== $n $c"; crictl logs --timestamps $c 2>&1 | tail -n 3000; done' \
    >"$RESULT_DIR/logs/drone01-node-containers.log" 2>&1
  "${R[@]}" inputs "$SOURCE" >"$RESULT_DIR/inputs-end.json" 2>>"$RESULT_DIR/run.log"
  "${R[@]}" set-file "$RUN_JSON" provenance.inputs_end "$RESULT_DIR/inputs-end.json"
}
cleanup() {
  local rc=$?
  if [[ -n "$E0_PID" ]] && kill -0 "$E0_PID" 2>/dev/null; then
    kill -TERM "$E0_PID"; wait "$E0_PID" 2>/dev/null
  fi
  if [[ -n "$DRIVER_PID" ]] && kill -0 "$DRIVER_PID" 2>/dev/null; then
    kill -TERM "$DRIVER_PID"; wait "$DRIVER_PID" 2>/dev/null
    "${R[@]}" set "$RUN_JSON" runner_status interrupted
  fi
  remove_partition
  if [[ -n "$PHASE_STARTED" ]]; then
    collect_evidence
  elif [[ -n "$CLUSTER_OURS" ]]; then
    "${K[@]}" get pods -o wide >"$RESULT_DIR/pods-at-setup-failure.txt" 2>&1
    "${K[@]}" get events --sort-by=.lastTimestamp >"$RESULT_DIR/events-at-setup-failure.txt" 2>&1
  fi
  if [[ -n "$CLUSTER_OURS" && "${S2_KEEP_CLUSTER:-0}" != "1" ]]; then
    k3d cluster delete "$CLUSTER" >>"$RESULT_DIR/run.log" 2>&1
  fi
  exit "$rc"
}
not_started() { log "NOT STARTED: $*"; echo "NOT STARTED: $*" >"$RESULT_DIR/REPORT.md"; exit 4; }
setup_failed() {
  log "SETUP FAILED: $*"
  "${R[@]}" set "$RUN_JSON" runner_status "setup_failed: $*"
  echo "INCONCLUSIVE: setup failed: $*" >"$RESULT_DIR/REPORT.md"
  exit 2
}

# -- 1. nothing else running
for command in docker k3d kubectl python3 git; do
  command -v "$command" >/dev/null || not_started "required command not found: $command"
done
# In this round sources and bench come from the same checkout (review of a41c087,
# choice 14): a different S2_SOURCE would need the provenance of both.
[[ "$SOURCE" == "$ROOT_DIR" ]] || not_started "S2_SOURCE $SOURCE is not the bench's checkout $ROOT_DIR: same checkout required"
if [[ -n "$(k3d cluster list --no-headers 2>/dev/null)" ]]; then
  not_started "k3d clusters present: $(k3d cluster list --no-headers | awk '{print $1}' | tr '\n' ' ')"
fi
# processes really running a test script, this runner and its descendants excluded
# (a shell whose text merely names a runner is not one: s2_record.py concurrent-runs)
OTHERS=$(python3 "$ROOT_DIR/scripts/s2_record.py" concurrent-runs "$$") \
  || not_started "cannot list the processes"
[[ "$OTHERS" == "[]" ]] || not_started "another run is active: $OTHERS"
trap cleanup EXIT
trap 'exit 3' INT TERM

# -- 2. inputs
"${R[@]}" set "$RUN_JSON" protocol_id s2-partition-v1
"${R[@]}" set "$RUN_JSON" variant "$VARIANT"
"${R[@]}" set "$RUN_JSON" case "$S2_CASE"
"${R[@]}" set "$RUN_JSON" run_id "$RUN_ID"
"${R[@]}" set "$RUN_JSON" source "$SOURCE"
"${R[@]}" inputs "$SOURCE" >"$RESULT_DIR/inputs-start.json" || not_started "cannot read the inputs of $SOURCE"
"${R[@]}" set-file "$RUN_JSON" provenance.inputs_start "$RESULT_DIR/inputs-start.json"
"${R[@]}" set "$RUN_JSON" provenance.source_dirty \
  "$(python3 -c 'import json,sys; print(json.dumps(json.load(open(sys.argv[1]))["dirty"]))' "$RESULT_DIR/inputs-start.json")"
"${R[@]}" set "$RUN_JSON" runner_status running
# a cell runs only on a qualified bench: the qualification's verdict, variant and bench files
if [[ "$S2_CASE" != "qualification" ]]; then
  QUALIFICATION=${S2_QUALIFICATION:-}
  [[ -n "$QUALIFICATION" && -f "$QUALIFICATION/s2-qualify.json" ]] \
    || not_started "a cell needs S2_QUALIFICATION=<a qualification result dir with s2-qualify.json>"
  QCHECK=$(python3 - "$QUALIFICATION/s2-qualify.json" "$RESULT_DIR/inputs-start.json" "$VARIANT" <<'PY'
import json, sys
qualification, inputs, variant = json.load(open(sys.argv[1])), json.load(open(sys.argv[2])), sys.argv[3]
topology = qualification.get("qualified_topology") or {}
problems = []
if qualification.get("verdict") != "QUALIFIED":
    problems.append(f"verdict {qualification.get('verdict')}")
if topology.get("variant") != variant:
    problems.append(f"qualified for variant {topology.get('variant')}")
if topology.get("bench") != inputs.get("bench"):
    changed = sorted(f for f in set(topology.get("bench") or {}) | set(inputs.get("bench") or {})
                     if (topology.get("bench") or {}).get(f) != (inputs.get("bench") or {}).get(f))
    problems.append(f"bench files changed since the qualification: {changed}")
print("; ".join(problems))
PY
)
  [[ -z "$QCHECK" ]] || not_started "qualification $QUALIFICATION not usable: $QCHECK"
  "${R[@]}" set "$RUN_JSON" qualification.dir "$QUALIFICATION"
  "${R[@]}" set-file "$RUN_JSON" qualification.record "$QUALIFICATION/s2-qualify.json"
fi

# -- 3. E0's bootstrap, unmodified, and its outcome
log "E0 bootstrap (variant $VARIANT) from $SOURCE"
"${R[@]}" set "$RUN_JSON" setup.e0_start_utc "$(date -u +%s.%N)"
CLUSTER_OURS=1                   # no cluster existed (checked above): created here, for this run
# S2's cluster: E0's configuration plus k3s's network policy controller disabled, on a fresh
# cluster (docs/S2_NETPOL_BENCH_PREREGISTRATION.md; S2 only). run_e0.sh then reuses it as
# it is -- no RESET_E0 -- after its own topology and datastore checks.
python3 "$SOURCE/scripts/render_s2_k3d_config.py" "$SOURCE/manifests/kubernetes/e0/k3d-cloud-native-e0.yaml" \
  "$RESULT_DIR/k3d-s2.yaml" >>"$RESULT_DIR/run.log" 2>&1 || setup_failed "render S2's cluster configuration"
k3d cluster create --config "$RESULT_DIR/k3d-s2.yaml" >"$RESULT_DIR/cluster-create.log" 2>&1 \
  || setup_failed "k3d cluster create (S2 configuration)"
kubectl --context "$CTX" wait --for=condition=Ready node --all --timeout=180s >>"$RESULT_DIR/run.log" 2>&1 \
  || setup_failed "nodes not Ready"
VARIANT="$VARIANT" "$SOURCE/scripts/run_e0.sh" >"$RESULT_DIR/e0.log" 2>&1 &
E0_PID=$!
wait "$E0_PID"
E0_EXIT=$?
E0_PID=""
"${R[@]}" set "$RUN_JSON" setup.e0_end_utc "$(date -u +%s.%N)"
"${R[@]}" set "$RUN_JSON" e0.exit "$E0_EXIT"
"${R[@]}" set "$RUN_JSON" e0.result_dir "$(sed -n 's/^Evidence: //p' "$RESULT_DIR/e0.log" | tail -n 1)"
precondition e0_verdict "$([[ $E0_EXIT == 0 ]] && echo true || echo false)" "run_e0.sh exit $E0_EXIT"
[[ "$E0_EXIT" == "0" ]] || setup_failed "E0 bootstrap exit $E0_EXIT"
{ k3d version; kubectl --context "$CTX" version -o json; docker exec "$SERVER" sh -c 'tr "\0" " " </proc/1/cmdline'; echo
  docker exec "$NODE" sh -c 'tr "\0" " " </proc/1/cmdline'; echo; } >"$RESULT_DIR/k3s-config.txt" 2>&1
DISCOVERY=$(kubectl --context "$CTX" get node "$SERVER" -o jsonpath='{.status.addresses[?(@.type=="InternalIP")].address}')
# the network policy controller really off: the server's k3s arguments, and no KUBE-ROUTER
# rule on any node (with the controller on, rules appear within seconds of the start)
NETPOL_ARGS=$(docker inspect -f '{{json .Args}}' "$SERVER" 2>>"$RESULT_DIR/run.log")
KUBE_ROUTER_RULES=$(for n in $SERVER k3d-cloud-native-p2-agent-0 k3d-cloud-native-p2-agent-1 \
                       k3d-cloud-native-p2-agent-2 "$EDGE_NODE"; do
  printf '%s=%s ' "$n" "$(docker exec "$n" sh -c 'iptables -S | grep -c KUBE-ROUTER' 2>>"$RESULT_DIR/run.log")"; done)
NETPOL_OFF=false
grep -Fq '"--disable-network-policy"' <<<"$NETPOL_ARGS" && ! grep -Eq '=([1-9]|$| )' <<<"$KUBE_ROUTER_RULES" \
  && NETPOL_OFF=true
precondition network_policy_controller_off "$NETPOL_OFF" "args: $NETPOL_ARGS; KUBE-ROUTER rules: $KUBE_ROUTER_RULES"
[[ "$NETPOL_OFF" == "true" ]] || setup_failed "network policy controller not verified off: $KUBE_ROUTER_RULES"

# k3s's system services pinned to the control-plane node, off the isolated one: the
# scheduler placed CoreDNS, metrics-server or the storage provisioner on drone01's node
# in some cells, so the partition cut them too (decision after the second eight-cell
# round). Only the experimental control changes: no framework logic.
SYSTEM_DEPLOYMENTS=(coredns metrics-server local-path-provisioner)
for d in "${SYSTEM_DEPLOYMENTS[@]}"; do
  kubectl --context "$CTX" -n kube-system patch deployment "$d" --type merge \
    -p '{"spec":{"template":{"spec":{"nodeSelector":{"kuberos.io/role":"control_plane"}}}}}' \
    >>"$RESULT_DIR/run.log" 2>&1 || setup_failed "cannot pin kube-system/$d"
  kubectl --context "$CTX" -n kube-system rollout status "deployment/$d" --timeout=120s \
    >>"$RESULT_DIR/run.log" 2>&1 || setup_failed "kube-system/$d not rolled out"
done

# -- 4. the S2 image, and every image's ID
log "build s2-harness from $SOURCE"
docker build -t cloud-native-ros/s2-harness:p2 -f "$SOURCE/containers/s2-harness/Dockerfile" "$SOURCE" \
  >"$RESULT_DIR/build.log" 2>&1 || setup_failed "docker build s2-harness"
k3d image import -c "$CLUSTER" cloud-native-ros/s2-harness:p2 >>"$RESULT_DIR/run.log" 2>&1 || setup_failed "image import"
if [[ "$VARIANT" == "a" ]]; then
  IMAGES=(cloud-native-ros/control-plane:p2 cloud-native-ros/event-detector:p2 cloud-native-ros/kuberos:p2
          cloud-native-ros/s2-harness:p2 microros/micro-ros-agent:humble px4io/px4-sitl:latest redis:7)
else
  IMAGES=(cloud-native-ros/control-plane:p2 cloud-native-ros/fleet-operator:p2 cloud-native-ros/state-bridge:p2
          cloud-native-ros/s2-harness:p2 microros/micro-ros-agent:humble px4io/px4-sitl:latest)
fi
python3 -c '
import json, subprocess, sys
print(json.dumps({i: subprocess.run(["docker", "image", "inspect", "--format", "{{.Id}}", i], capture_output=True,
                                    text=True).stdout.strip() for i in sys.argv[1:]}))' "${IMAGES[@]}" \
  >"$RESULT_DIR/images.json"
"${R[@]}" set-file "$RUN_JSON" provenance.images "$RESULT_DIR/images.json"

# -- 5. the variant's own preparation
if [[ "$VARIANT" == "a" ]]; then
  log "A: dispatcher reception trace"
  "${K[@]}" set env deployment/operational-event-dispatcher-p2 OPERATIONAL_EVENT_TRACE=1 >>"$RESULT_DIR/run.log" 2>&1 \
    || setup_failed "set env on the dispatcher"
  "${K[@]}" rollout status deployment/operational-event-dispatcher-p2 --timeout=120s >>"$RESULT_DIR/run.log" 2>&1 \
    || setup_failed "dispatcher rollout"
  # one dispatcher really alive, with the trace (decision 4 after the first qualification
  # round): the old Pod's container may still run while terminating -- it counts
  DISPATCHER_WAIT_SEC=60
  ok=false; alive=""
  for _ in $(seq 1 $((DISPATCHER_WAIT_SEC / 2))); do
    alive=$("${K[@]}" get pods -o json 2>>"$RESULT_DIR/run.log" \
      | python3 "$ROOT_DIR/scripts/s2_control.py" alive --app operational-event-dispatcher-p2 --trace 1)
    [[ "$alive" == *'"single": true'* ]] && { ok=true; break; }
    sleep 2
  done
  echo "$alive" >"$RESULT_DIR/dispatcher-alive.json"
  precondition single_dispatcher_alive "$ok" "$alive"
  [[ "$ok" == true ]] || setup_failed "not a single dispatcher alive within $DISPATCHER_WAIT_SEC s"
  DISPATCHER_POD=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["alive"][0]["name"])' \
    "$RESULT_DIR/dispatcher-alive.json")
  DISPATCHER_UID=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["alive"][0]["uid"])' \
    "$RESULT_DIR/dispatcher-alive.json")
  TRACE_ENV=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["alive"][0]["trace"])' \
    "$RESULT_DIR/dispatcher-alive.json")
  "${R[@]}" set "$RUN_JSON" identities.dispatcher_pod "$DISPATCHER_POD"
  "${R[@]}" set "$RUN_JSON" identities.dispatcher_pod_uid "$DISPATCHER_UID"
  "${R[@]}" set "$RUN_JSON" identities.dispatcher_trace "\"$TRACE_ENV\""
  precondition dispatcher_trace "$([[ $TRACE_ENV == 1 ]] && echo true || echo false)" "OPERATIONAL_EVENT_TRACE=$TRACE_ENV"
  ok=false; out=""
  for _ in $(seq 1 12); do
    out=$(ros drone01-event-detector ros2 lifecycle get /drone01/event_detector | tail -n 1)
    [[ "$out" == active* ]] && { ok=true; break; }
    sleep 5
  done
  precondition detector_active "$ok" "$out"
  ok=false
  for _ in $(seq 1 12); do
    out=$(ros operational-event-dispatcher-p2 ros2 action info /fleet/deployment_request)
    grep -q "Action servers: 1" <<<"$out" && { ok=true; break; }
    sleep 5
  done
  precondition action_server "$ok" "$(tr '\n' ' ' <<<"$out" | cut -c1-200)"
  TARGET_NODE=/drone01/companion_analytics_onboard
  METRICS_TOPIC=/drone01/analytics/metrics
  ANALYTICS_POD=$(pod_of pod-name=drone01-companion-analytics-onboard)
else
  log "B: the S2 policy"
  "${K[@]}" apply -f "$SOURCE/manifests/kubernetes/s2/70-s2-adaptationpolicy-b.yaml" >>"$RESULT_DIR/run.log" 2>&1 \
    || setup_failed "apply the S2 policy"
  P=analytics-latency-slo-s2-drone01
  MATCHES=$("${K[@]}" get rosmodule -l app=companion-analytics,robot=drone01 -o json \
    | python3 -c 'import json,sys; print(sum(1 for m in json.load(sys.stdin)["items"] if m["spec"].get("placement") == "onboard"))')
  precondition policy_single_onboard_match "$([[ $MATCHES == 1 ]] && echo true || echo false)" "$MATCHES onboard ROSModule(s)"
  ok=false; cursors=""
  for _ in $(seq 1 30); do
    snap=$("${K[@]}" get adaptationpolicy "$P" -o json 2>/dev/null | python3 -c '
import json, sys
s = json.load(sys.stdin).get("status") or {}
amb = any(c.get("type") == "AmbiguousTarget" and c.get("status") == "True" for c in s.get("conditions") or [])
print(s.get("state"), s.get("correlationId") or "-", s.get("edgeModuleName") or "-", amb,
      (s.get("windowCursor") or {}).get("end"))' 2>/dev/null)
    cursors="$cursors $(awk '{print $5}' <<<"$snap")"
    edges=$("${K[@]}" get rosmodule -o name | grep -c -- '-edge$' || true)
    read -r state cid edge amb _ <<<"$snap"
    distinct=$(tr ' ' '\n' <<<"$cursors" | grep -v -e '^$' -e None | sort -u | wc -l)
    if [[ "$state" == Nominal && "$cid" == - && "$edge" == - && "$amb" == False && "$edges" == 0 && "$distinct" -ge 3 ]]; then
      ok=true; break
    fi
    sleep 2
  done
  precondition policy_nominal_cursor_advancing "$ok" "state=$state cid=$cid edge=$edge ambiguous=$amb edges=$edges cursors=$distinct"
  ANALYTICS_POD=$(pod_of dronekube.io/owned-by-rosmodule=companion-analytics-drone01)
  TARGET_NODE="/drone01/companion_analytics_${ANALYTICS_POD//-/_}"
  METRICS_TOPIC=/drone01/analytics/metrics/onboard
  "${R[@]}" set "$RUN_JSON" identities.policy_uid "$("${K[@]}" get adaptationpolicy "$P" -o jsonpath='{.metadata.uid}')"
fi
"${R[@]}" set "$RUN_JSON" identities.target_node "$TARGET_NODE"
"${R[@]}" set "$RUN_JSON" identities.metrics_topic "$METRICS_TOPIC"
"${R[@]}" set "$RUN_JSON" identities.analytics_pod "$ANALYTICS_POD"

# -- 6. harness and prober, this run's id, their hostPaths emptied first
python3 -c "
import importlib.util, sys
spec = importlib.util.spec_from_file_location('c', '$ROOT_DIR/scripts/s2_control.py'); c = importlib.util.module_from_spec(spec)
spec.loader.exec_module(c)
c.clean('$NODE', '/var/lib/s2-harness'); c.clean('$EDGE_NODE', '/var/lib/s2-prober')" >>"$RESULT_DIR/run.log" 2>&1 \
  || setup_failed "cannot empty the hostPaths"
for m in 10-s2-harness 20-s2-health-prober; do
  sed -e "s#__S2_METRICS_TOPIC__#$METRICS_TOPIC#g" -e "s#__S2_RUN_ID__#$RUN_ID#g" \
      -e "s#__P2_DISCOVERY_ADDRESS__#$DISCOVERY#g" "$SOURCE/manifests/kubernetes/s2/$m.yaml" \
      >"$RESULT_DIR/rendered/$m.yaml"
  "${K[@]}" apply -f "$RESULT_DIR/rendered/$m.yaml" >>"$RESULT_DIR/run.log" 2>&1 || setup_failed "apply $m"
done
"${K[@]}" rollout status deployment/drone01-s2-harness --timeout=180s >>"$RESULT_DIR/run.log" 2>&1 \
  || setup_failed "harness rollout"
"${K[@]}" rollout status deployment/s2-health-prober --timeout=180s >>"$RESULT_DIR/run.log" 2>&1 \
  || setup_failed "prober rollout"
HARNESS_POD=$(pod_of app.kubernetes.io/name=drone01-s2-harness)
PROBER_POD=$(pod_of app.kubernetes.io/name=s2-health-prober)
HARNESS_CID=$(cid_of "$HARNESS_POD" harness)
PROBER_CID=$(cid_of "$PROBER_POD" prober)
"${R[@]}" set "$RUN_JSON" identities.harness_pod "$HARNESS_POD"
"${R[@]}" set "$RUN_JSON" identities.harness_pod_uid "$("${K[@]}" get pod "$HARNESS_POD" -o jsonpath='{.metadata.uid}')"
"${R[@]}" set "$RUN_JSON" identities.prober_pod "$PROBER_POD"
[[ -n "$HARNESS_CID" && -n "$PROBER_CID" ]] || setup_failed "harness/prober containers not found"
[[ "$("${K[@]}" get pod "$HARNESS_POD" -o jsonpath='{.spec.nodeName}')" == "$NODE" ]] || setup_failed "harness not on $NODE"

# -- 7. peers frozen, cut targets, observers, the three drones' health
python3 "$ROOT_DIR/scripts/s2_partition.py" peers --node "$NODE" >"$RESULT_DIR/peers.json" 2>>"$RESULT_DIR/run.log" \
  || setup_failed "docker network peers"
ip_of() { python3 -c 'import json,sys; print(next(p["ipv4"] for p in json.load(open(sys.argv[1])) if p["name"] == sys.argv[2]))' \
  "$RESULT_DIR/peers.json" "$1"; }
CUT_TARGETS=("$(ip_of "$SERVER"):6443" "$(ip_of "$EDGE_NODE"):10250")
# probed every second during the cut: the kubelets of the server and the edge, ports
# no counting rule counts (the counters stay the node's own traffic)
HOLD_TARGETS=("$(ip_of "$SERVER"):10250" "$(ip_of "$EDGE_NODE"):10250")
O=(python3 "$ROOT_DIR/scripts/s2_observers.py")
"${O[@]}" inventory --variant "$VARIANT" --context "$CTX" --out "$OBS/inventory.jsonl" --stop-file "$STOP" & OBSERVER_PIDS+=($!)
"${O[@]}" nodes --context "$CTX" --out "$OBS/nodes.jsonl" --stop-file "$STOP" & OBSERVER_PIDS+=($!)
"${O[@]}" audit --context "$CTX" --out "$OBS/audit.jsonl" --stop-file "$STOP" & OBSERVER_PIDS+=($!)
if [[ "$VARIANT" == "a" ]]; then
  FOLLOW=("operational-event-dispatcher-p2:dispatcher:operational-event-dispatcher-p2"
          "application-manager-p2:manager:application-manager-p2" "kuberos:api:kuberos-api" "kuberos:worker:kuberos-worker")
else
  FOLLOW=("fleet-operator::fleet-operator")
fi
for f in "${FOLLOW[@]}"; do
  IFS=: read -r dep container name <<<"$f"
  "${O[@]}" logs --context "$CTX" --deployment "$dep" --container "$container" --out "$OBS/logs-$name.jsonl" \
    --stop-file "$STOP" & OBSERVER_PIDS+=($!)
done
for i in 1 2 3; do
  "${O[@]}" px4 --node "k3d-cloud-native-p2-agent-$((i - 1))" --out "$OBS/px4-drone0$i.txt" --stop-file "$STOP" \
    & OBSERVER_PIDS+=($!)
done
for c in $("${K[@]}" get pod "$ANALYTICS_POD" -o jsonpath='{.spec.containers[*].name}'); do
  python3 "$ROOT_DIR/scripts/window_transport_sampler.py" memory --node-container "$NODE" \
    --container-id "$(cid_of "$ANALYTICS_POD" "$c")" --out "$OBS/memory-$c.jsonl" --stop-file "$STOP" \
    & OBSERVER_PIDS+=($!)
done
PHASE_STARTED=1
"${K[@]}" get pods -o json >"$RESULT_DIR/pods-start.json"
python3 "$ROOT_DIR/scripts/s2_qualify.py" facts --node "$NODE" --server "$SERVER" --context "$CTX" \
  --peers "$(cat "$RESULT_DIR/peers.json")" --result-dir "$RESULT_DIR" --variant "$VARIANT" \
  --out "$RESULT_DIR/facts.json" 2>>"$RESULT_DIR/run.log" || setup_failed "cannot read the facts"
"${R[@]}" set "$RUN_JSON" topology \
  "$(python3 -c 'import json,sys; print(json.dumps(json.load(open(sys.argv[1]))["topology"]))' "$RESULT_DIR/facts.json")"
ok=false; health=""
for _ in $(seq 1 24); do
  health=$(docker exec "$EDGE_NODE" cat /var/lib/s2-prober/health.jsonl 2>/dev/null | python3 -c '
import json, sys, time
last = {}
for line in sys.stdin:
    try:
        r = json.loads(line)
    except ValueError:
        continue
    if r.get("event") == "health":
        last[r["target"]] = r
now = time.time()
fresh = {t: r["result"] for t, r in last.items() if now - r["outcome_utc"] <= 10}
print(" ".join(f"{t}={fresh.get(t)}" for t in ("drone01-onboard", "drone02-onboard", "drone03-onboard")))')
  [[ "$health" == *"drone01-onboard=positive drone02-onboard=positive drone03-onboard=positive"* ]] && { ok=true; break; }
  sleep 5
done
precondition three_drones_healthy "$ok" "$health"
NOT_READY=$("${K[@]}" get pods -o json | python3 -c '
import json, sys
bad = [p["metadata"]["name"] for p in json.load(sys.stdin)["items"]
       if p["status"].get("phase") == "Running" and not any(c["type"] == "Ready" and c["status"] == "True"
                                                            for c in p["status"].get("conditions") or [])]
print(" ".join(bad))')
precondition pods_ready "$([[ -z $NOT_READY ]] && echo true || echo false)" "not Ready: ${NOT_READY:-none}"
# nothing but drone01's workloads on the isolated node (an old system Pod still
# terminating counts), waited for at most 90 s
ok=false
for _ in $(seq 1 45); do
  kubectl --context "$CTX" get pods -A -o json 2>>"$RESULT_DIR/run.log" \
    | python3 "$ROOT_DIR/scripts/s2_control.py" placement --node "$NODE" >"$RESULT_DIR/placement-start.json"
  python3 -c 'import json,sys; sys.exit(0 if not json.load(open(sys.argv[1]))["foreign"] else 1)' \
    "$RESULT_DIR/placement-start.json" 2>/dev/null && { ok=true; break; }
  sleep 2
done
precondition system_services_off_isolated_node "$ok" "$(python3 -c '
import json, sys
p = json.load(open(sys.argv[1]))
print("foreign:", [f["namespace"] + "/" + f["name"] for f in p["foreign"]], "system:", p["system"])' \
  "$RESULT_DIR/placement-start.json" 2>&1)"
if ! python3 -c 'import json,sys; sys.exit(0 if all(v["ok"] for v in json.load(open(sys.argv[1]))["preconditions"].values()) else 1)' \
    "$RUN_JSON"; then
  setup_failed "a precondition is not met (run.json)"
fi

# -- 8. the timed phase
CLOCK_TARGETS="[{\"label\": \"harness\", \"node\": \"$NODE\", \"cid\": \"$HARNESS_CID\"},
 {\"label\": \"prober\", \"node\": \"$EDGE_NODE\", \"cid\": \"$PROBER_CID\"}"
if [[ "$VARIANT" == "a" ]]; then
  CLOCK_TARGETS="$CLOCK_TARGETS, {\"label\": \"dispatcher\", \"node\": \"$("${K[@]}" get pod "$DISPATCHER_POD" \
    -o jsonpath='{.spec.nodeName}')\", \"cid\": \"$(cid_of "$DISPATCHER_POD" dispatcher)\"}"
fi
CLOCK_TARGETS="$CLOCK_TARGETS]"
PX4_FILES="{\"drone01\": \"$OBS/px4-drone01.txt\", \"drone02\": \"$OBS/px4-drone02.txt\", \"drone03\": \"$OBS/px4-drone03.txt\"}"
log "phase $S2_CASE (run $RUN_ID)"
COMMON=(--result-dir "$RESULT_DIR" --run-id "$RUN_ID" --node "$NODE" --harness-cid "$HARNESS_CID"
        --target-node "$TARGET_NODE" --peers "$(cat "$RESULT_DIR/peers.json")" --cut-targets "${CUT_TARGETS[@]}"
        --hold-targets "${HOLD_TARGETS[@]}" --clock-targets "$CLOCK_TARGETS" --px4-files "$PX4_FILES")
if [[ "$S2_CASE" == "qualification" ]]; then
  python3 "$ROOT_DIR/scripts/s2_qualify.py" "${COMMON[@]}" --edge-node "$EDGE_NODE" --server "$SERVER" \
    --context "$CTX" --harness-pod "$HARNESS_POD" >>"$RESULT_DIR/run.log" 2>&1 &
else
  python3 "$ROOT_DIR/scripts/s2_phase.py" "${COMMON[@]}" --case "$S2_CASE" >>"$RESULT_DIR/run.log" 2>&1 &
fi
DRIVER_PID=$!
wait "$DRIVER_PID"
DRIVER_EXIT=$?
DRIVER_PID=""
"${R[@]}" set "$RUN_JSON" driver_exit "$DRIVER_EXIT"
log "phase driver exit $DRIVER_EXIT"

# -- 9. evidence, inputs again, the judge
remove_partition
collect_evidence
"${R[@]}" set "$RUN_JSON" runner_status completed
if [[ "$S2_CASE" == "qualification" ]]; then
  python3 "$ROOT_DIR/scripts/s2_qualify_judge.py" "$RESULT_DIR" >>"$RESULT_DIR/run.log" 2>&1
  VERDICT=$?
  "${R[@]}" qreport "$RESULT_DIR" >>"$RESULT_DIR/run.log" 2>&1
else
  python3 "$ROOT_DIR/scripts/s2_judge.py" "$RESULT_DIR" >>"$RESULT_DIR/run.log" 2>&1
  VERDICT=$?
  "${R[@]}" report "$RESULT_DIR" >>"$RESULT_DIR/run.log" 2>&1
fi
log "verdict exit $VERDICT; evidence $RESULT_DIR"
exit "$VERDICT"
