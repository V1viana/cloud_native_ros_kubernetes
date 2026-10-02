#!/usr/bin/env bash
# R5: the onboard fallback after the service was handed to the edge (variant B).
#
# Found by the review round of ccd491e: an edge lost after the onboard had been
# deactivated was removed and the policy declared RolledBack with nothing
# serving. Fixed in a61852e with the semantics decided by Viviana (DEV_SMOKE_TEST,
# "R5: fallback dell'onboard"). Criteria fixed here before any data is observed.
# Every phase uses its own onboard module (300 ms, so the policy triggers) and
# policy (trigger 3 x 2s windows, readiness limit 120s) whose edge twin also
# runs at 300 ms, so the incident never closes by itself; each fault is
# injected once status.handedOverAt is set (onboard Inactive). One sample about
# every second: policy state and bookkeeping, edge ROSModule present, onboard
# target and observed lifecycle state.
#   0  P2-B on a fresh cluster: PASS as run_p2.sh judges it (regression of the
#      normal migration after the fix).
#   1  Short loss, tolerated (policy edgeLossToleranceSec=90, chosen above the
#      ~30s a replaced Pod needs): the edge Pod is deleted once. PASS if within
#      150s the loss is detected (edgeUnavailableSince set), then cleared while
#      the policy is still Migrating, and in no sample the state leaves
#      Migrating or the onboard target leaves Inactive. INCONCLUSIVE if the loss
#      is never detected (e.g. a second edge replica kept serving) or lasts
#      more than 90s (the phase's premise did not hold).
#   2  Lasting loss, edge capacity gone (defaults: tolerance 30s, fallback 60s):
#      the nodes labelled kuberos.io/role=edge are cordoned and the edge Pod is
#      deleted, so its replacement stays Pending. PASS if the loss is detected
#      within 15s of the deletion, FallingBack/EdgeLost is entered 30-45s after
#      edgeUnavailableSince, RolledBack within 60s of fallbackSince, in every
#      sample without the edge ROSModule the onboard is target Active and
#      observed Active, and the audit has this incident's incident_completed with
#      analytics_migration_failed and rollback_performed=true. Uncordoned after.
#   3  Lasting loss, edge hung (defaults): every process of the edge Pod is
#      stopped (SIGSTOP from its node, matched by Pod UID in /proc/*/cgroup).
#      The Pod stays Ready (probe kill -0 1) and its records stay "fresh" up to
#      60s (OBSERVATION_MAX_AGE_SEC), so detection is expected late. PASS as
#      phase 2 but with detection within 75s of the freeze (60s + a 5s tick +
#      10s slack). INCONCLUSIVE if no process of the Pod is found to stop.
#      Reported: detection latency.
#   4  Edge ROSModule deleted after the handover (defaults): PASS if
#      FallingBack/EdgeModuleMissing within 15s (no tolerance: it will not come
#      back), RolledBack within 60s of fallbackSince, onboard target Active and
#      observed Active from RolledBack on.
# Reported, not judged: time from the fault to each step and total time with no
# serving module. Variant A: scripts/run_p2.sh EXPERIMENT_MODE=edgeloss. One
# execution.
# The judges are scripts/edge_fallback_judge.py (tested offline). Until the run
# of 14fc04c they were inline here and phase 4 was also given phase 2's ordering
# check, which its criteria above do not contain: that run stays recorded as a
# judge error. PHASES (default "1 2 3 4") runs a subset after phase 0, which
# always runs because it sets the cluster up; phases not run are NOT_RUN and
# do not count in the overall verdict.
set -uo pipefail

PHASES=${PHASES:-1 2 3 4}
ROOT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
CTX=k3d-cloud-native-p2
CLUSTER=cloud-native-p2
NS=cloud-native-p2
OWNER_LABEL=dronekube.io/owned-by-rosmodule
RESULT_DIR="$ROOT_DIR/results/edge-fallback/$(date -u +%Y%m%dT%H%M%SZ)"
SAMPLES="$RESULT_DIR/samples.jsonl"
K=(kubectl --context "$CTX" -n "$NS")
mkdir -p "$RESULT_DIR"
python3 "$ROOT_DIR/scripts/s3_provenance.py" --root "$ROOT_DIR" \
  --output "$RESULT_DIR/provenance" >"$RESULT_DIR/source-sha256.txt"
echo "Result dir: $RESULT_DIR"
log() { echo "$(date -u +%H:%M:%S) $*" | tee -a "$RESULT_DIR/run.log"; }

module_manifest() {  # $1 = module name
  cat <<EOF
apiVersion: dronekube.io/v1alpha1
kind: ROSModule
metadata: {name: $1, namespace: $NS, labels: {app: $1, robot: drone01}}
spec:
  robotId: drone01
  package: companion_analytics
  placement: onboard
  lifecycleTarget: Active
  rosParamMap:
    processing_delay_ms: "300.0"
    queue_depth: "4"
    cpu_percent: "25.0"
    sample_period_ms: "250"
    health_service: /drone01/companion/${1//-/_}/health
EOF
}
policy_manifest() {  # $1 = module, $2 = policy, $3 = extra rollback fields ("" = defaults)
  cat <<EOF
apiVersion: dronekube.io/v1alpha1
kind: AdaptationPolicy
metadata: {name: $2, namespace: $NS}
spec:
  targetModuleSelector: {matchLabels: {app: $1}}
  trigger: {metric: latency_p95_ms, threshold: 250, consecutiveWindows: 3, windowSec: 2}
  action: {type: MigratePlacement, to: edge, edgeRosParamMap: {processing_delay_ms: "300.0"}}
  recovery: {metric: latency_p95_ms, threshold: 150, consecutiveWindows: 3}
  rollback: {onReadinessFailureSec: 120${3:+, $3}}
EOF
}

sample() {  # $1 = phase label; one JSON line (also printed)
  "${K[@]}" get adaptationpolicies,rosmodules -o json 2>/dev/null | python3 -c '
import json, sys, time
label, m, p = sys.argv[1], sys.argv[2], sys.argv[3]
try:
    items = {(i["kind"], i["metadata"]["name"]): i for i in json.load(sys.stdin)["items"]}
except (ValueError, KeyError):
    items = {}
st = items.get(("AdaptationPolicy", p), {}).get("status", {})
onboard = items.get(("ROSModule", m))
line = {"label": label, "t": time.time(), "state": st.get("state"),
        "handedOverAt": st.get("handedOverAt"), "edgeUnavailableSince": st.get("edgeUnavailableSince"),
        "fallbackSince": st.get("fallbackSince"), "fallbackReason": st.get("fallbackReason"),
        "edge_exists": ("ROSModule", m + "-edge") in items,
        "onboard_target": (onboard or {}).get("spec", {}).get("lifecycleTarget"),
        "onboard_observed": (onboard or {}).get("status", {}).get("observedLifecycleState")}
print(json.dumps(line))' "$1" "$M" "$P" | tee -a "$SAMPLES"
}
field() { python3 -c 'import json,sys; v=json.loads(sys.argv[1]).get(sys.argv[2]); print("" if v is None else v)' "$1" "$2"; }
edge_pod() { "${K[@]}" get pods -l "$OWNER_LABEL=$M-edge" -o jsonpath='{.items[0].metadata.name}' 2>/dev/null; }
edge_nodes() { kubectl --context "$CTX" get nodes -l kuberos.io/role=edge -o jsonpath='{.items[*].metadata.name}'; }
pod_signal() {  # $1 = STOP | CONT, $2 = pod: signal every process of the Pod from its node
  local node uid
  node=$("${K[@]}" get pod "$2" -o jsonpath='{.spec.nodeName}' 2>/dev/null)
  uid=$("${K[@]}" get pod "$2" -o jsonpath='{.metadata.uid}' 2>/dev/null)
  [[ -n "$node" && -n "$uid" ]] || return 1
  docker exec "$node" sh -c "n=0; for p in /proc/[0-9]*; do grep -qE 'pod(${uid}|${uid//-/_})' \$p/cgroup 2>/dev/null && kill -$1 \${p#/proc/} 2>/dev/null && n=\$((n+1)); done; echo \$n"
}

declare -A RESULT DETAIL
CORDONED=""; FROZEN_POD=""
finish() {
  [[ -n "$FROZEN_POD" ]] && pod_signal CONT "$FROZEN_POD" >/dev/null 2>&1
  for n in $CORDONED; do kubectl --context "$CTX" uncordon "$n" >/dev/null 2>&1; done
  "${K[@]}" get adaptationpolicy,rosmodule -o yaml >"$RESULT_DIR/resources.yaml" 2>&1
  "${K[@]}" logs deployment/fleet-operator --tail=2000 >"$RESULT_DIR/fleet-operator.log" 2>&1
  "${K[@]}" get events --sort-by=.lastTimestamp >"$RESULT_DIR/events.txt" 2>&1
  "${K[@]}" exec deployment/p2-audit-writer -- cat /data/audit.jsonl >"$RESULT_DIR/audit.jsonl" 2>/dev/null
  local overall=PASS k
  for k in 0 $PHASES; do [[ "${RESULT[$k]:-NOT_RUN}" == PASS ]] || overall=FAIL; done
  cat >"$RESULT_DIR/REPORT.md" <<EOF
# Fallback dell'onboard dopo il passaggio all'edge (R5, variante B) -- $overall

| Fase | Esito | Dettaglio |
| --- | --- | --- |
| 0 P2-B normale | ${RESULT[0]:-NOT_RUN} | ${DETAIL[0]:-} |
| 1 perdita breve tollerata (Pod edge cancellato, tolleranza 90s) | ${RESULT[1]:-NOT_RUN} | ${DETAIL[1]:-} |
| 2 perdita duratura: nodi edge cordonati, Pod edge cancellato | ${RESULT[2]:-NOT_RUN} | ${DETAIL[2]:-} |
| 3 perdita duratura: Pod edge bloccato (SIGSTOP) | ${RESULT[3]:-NOT_RUN} | ${DETAIL[3]:-} |
| 4 ROSModule edge cancellato | ${RESULT[4]:-NOT_RUN} | ${DETAIL[4]:-} |

Procedura e criteri: \`scripts/run_edge_fallback_check.sh\` (intestazione),
fissati prima dell'esecuzione. Fasi eseguite dopo la 0: $PHASES. Campioni in
\`samples.jsonl\`. Una esecuzione, variante B.
EOF
  log "ESITO COMPLESSIVO: $overall"
  k3d cluster delete "$CLUSTER" >>"$RESULT_DIR/run.log" 2>&1
  [[ "$overall" == PASS ]] && OVERALL_RC=0 || OVERALL_RC=1
}
OVERALL_RC=1
trap 'finish; exit $OVERALL_RC' EXIT

setup_pair() {  # $1 = module, $2 = policy, $3 = extra rollback fields; returns once handed over
  M=$1; P=$2
  module_manifest "$M" | "${K[@]}" apply -f - >>"$RESULT_DIR/run.log" 2>&1
  for _ in $(seq 1 90); do
    [[ "$("${K[@]}" get rosmodule "$M" -o jsonpath='{.status.observedLifecycleState}' 2>/dev/null)" == Active ]] && break; sleep 2
  done
  policy_manifest "$M" "$P" "$3" | "${K[@]}" apply -f - >>"$RESULT_DIR/run.log" 2>&1
  local t0=$SECONDS
  while (( SECONDS - t0 <= 240 )); do
    [[ -n "$("${K[@]}" get adaptationpolicy "$P" -o jsonpath='{.status.handedOverAt}' 2>/dev/null)" ]] && return 0
    sleep 2
  done
  return 1
}
teardown_pair() {
  "${K[@]}" delete adaptationpolicy "$P" --wait=true >>"$RESULT_DIR/run.log" 2>&1
  "${K[@]}" delete rosmodule "$M" "$M-edge" --ignore-not-found --wait=true >>"$RESULT_DIR/run.log" 2>&1
  for _ in $(seq 1 40); do
    [[ -z "$("${K[@]}" get pods -o name 2>/dev/null | grep -E "/$M-")" ]] && break; sleep 3
  done
}
observe() {  # $1 = label, $2 = max seconds; samples until a terminal state (or the limit)
  local t0=$SECONDS line
  while (( SECONDS - t0 <= $2 )); do
    line=$(sample "$1")
    case "$(field "$line" state)" in RolledBack|FallbackFailed|Recovered) sleep 3; sample "$1" >/dev/null; return 0;; esac
    sleep 1
  done
}
judge() {  # scripts/edge_fallback_judge.py <kind> samples label ...
  python3 "$ROOT_DIR/scripts/edge_fallback_judge.py" "$1" "$SAMPLES" "${@:2}"
}
in_phases() { [[ " $PHASES " == *" $1 "* ]]; }

# ---- 0 -------------------------------------------------------------------
log "fase 0: P2-B su cluster nuovo"
if ! RESET_P2=1 VARIANT=b bash "$ROOT_DIR/scripts/run_p2.sh" >"$RESULT_DIR/p2b.log" 2>&1; then
  RESULT[0]=FAIL; DETAIL[0]="P2-B fallito, vedi p2b.log"; exit 1
fi
RESULT[0]=PASS; DETAIL[0]=$(grep -oE 'results/p2/[0-9TZ]+' "$RESULT_DIR/p2b.log" | head -1)
log "fase 0 PASS: ${DETAIL[0]}"
"${K[@]}" delete adaptationpolicy --all --wait=true >>"$RESULT_DIR/run.log" 2>&1
"${K[@]}" delete rosmodule --all --wait=true >>"$RESULT_DIR/run.log" 2>&1
for _ in $(seq 1 40); do
  [[ -z "$("${K[@]}" get pods -o name 2>/dev/null | grep companion-analytics)" ]] && break; sleep 3
done

# ---- 1 -------------------------------------------------------------------
if in_phases 1; then
if setup_pair fb-short fb-short-slo "edgeLossToleranceSec: 90"; then
  pod=$(edge_pod); log "fase 1: consegnato all'edge; cancello il Pod edge $pod"
  "${K[@]}" delete pod "$pod" --wait=false >>"$RESULT_DIR/run.log" 2>&1
  t0=$SECONDS
  while (( SECONDS - t0 <= 150 )); do sample fase1 >/dev/null; sleep 1; done
  CHECK=$(judge short fase1)
  RESULT[1]=${CHECK%%|*}; DETAIL[1]=${CHECK#*|}
else
  RESULT[1]=FAIL; DETAIL[1]="passaggio all'edge mai avvenuto in 240s"
fi
log "fase 1 ${RESULT[1]}: ${DETAIL[1]}"
teardown_pair
fi

# ---- 2 -------------------------------------------------------------------
if in_phases 2; then
if setup_pair fb-cordon fb-cordon-slo ""; then
  pod=$(edge_pod)
  for n in $(edge_nodes); do kubectl --context "$CTX" cordon "$n" >>"$RESULT_DIR/run.log" 2>&1 && CORDONED="$CORDONED $n"; done
  log "fase 2: consegnato all'edge; nodi edge cordonati ($CORDONED), cancello il Pod edge $pod"
  fault=$(date +%s.%N)
  "${K[@]}" delete pod "$pod" --wait=false >>"$RESULT_DIR/run.log" 2>&1
  observe fase2 240
  "${K[@]}" exec deployment/p2-audit-writer -- cat /data/audit.jsonl >"$RESULT_DIR/audit-now.jsonl" 2>/dev/null
  CHECK=$(judge lost fase2 "$fault" 15 "$RESULT_DIR/audit-now.jsonl" "$P")
  RESULT[2]=${CHECK%%|*}; DETAIL[2]=${CHECK#*|}
  for n in $CORDONED; do kubectl --context "$CTX" uncordon "$n" >>"$RESULT_DIR/run.log" 2>&1; done; CORDONED=""
else
  RESULT[2]=FAIL; DETAIL[2]="passaggio all'edge mai avvenuto in 240s"
fi
log "fase 2 ${RESULT[2]}: ${DETAIL[2]}"
teardown_pair
fi

# ---- 3 -------------------------------------------------------------------
if in_phases 3; then
if setup_pair fb-hang fb-hang-slo ""; then
  pod=$(edge_pod); fault=$(date +%s.%N)
  stopped=$(pod_signal STOP "$pod"); FROZEN_POD=$pod
  log "fase 3: consegnato all'edge; Pod edge $pod bloccato ($stopped processi)"
  if [[ -z "$stopped" || "$stopped" == 0 ]]; then
    RESULT[3]=INCONCLUSIVE; DETAIL[3]="nessun processo del Pod trovato da bloccare"
  else
    observe fase3 300
    "${K[@]}" exec deployment/p2-audit-writer -- cat /data/audit.jsonl >"$RESULT_DIR/audit-now.jsonl" 2>/dev/null
    CHECK=$(judge lost fase3 "$fault" 75 "$RESULT_DIR/audit-now.jsonl" "$P")
    RESULT[3]=${CHECK%%|*}; DETAIL[3]="${CHECK#*|}; $stopped processi bloccati"
  fi
  pod_signal CONT "$pod" >/dev/null 2>&1; FROZEN_POD=""
else
  RESULT[3]=FAIL; DETAIL[3]="passaggio all'edge mai avvenuto in 240s"
fi
log "fase 3 ${RESULT[3]}: ${DETAIL[3]}"
teardown_pair
fi

# ---- 4 -------------------------------------------------------------------
if in_phases 4; then
if setup_pair fb-deleted fb-deleted-slo ""; then
  log "fase 4: consegnato all'edge; cancello il ROSModule $M-edge"
  fault=$(date +%s.%N)
  "${K[@]}" delete rosmodule "$M-edge" --wait=false >>"$RESULT_DIR/run.log" 2>&1
  observe fase4 120
  "${K[@]}" exec deployment/p2-audit-writer -- cat /data/audit.jsonl >"$RESULT_DIR/audit-now.jsonl" 2>/dev/null
  CHECK=$(judge deleted fase4 "$fault" "$RESULT_DIR/audit-now.jsonl" "$P")
  RESULT[4]=${CHECK%%|*}; DETAIL[4]=${CHECK#*|}
else
  RESULT[4]=FAIL; DETAIL[4]="passaggio all'edge mai avvenuto in 240s"
fi
log "fase 4 ${RESULT[4]}: ${DETAIL[4]}"
teardown_pair
fi
