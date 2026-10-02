#!/usr/bin/env bash
# R5: the same edge loss after the handover, in variant A (Viviana, 2026-09-25).
#
# Variant B's check is scripts/run_edge_fallback_check.sh, phase 2. Here
# run_p2.sh runs P2-A with EXPERIMENT_MODE=edgeloss (edge at 300 ms, so the
# incident cannot close by itself) and this script injects the same fault as
# B's phase 2 once the Application Manager has deactivated the onboard: its
# dispatcher feedback "Waiting for the correlated analytics SLO recovery"
# (progress 0.9) comes after the onboard deactivation and the HPA. Fault: the
# nodes labelled kuberos.io/role=edge are cordoned and the edge Pod is deleted,
# so its replacement stays Pending (the KubeROS Deployment requires
# kuberos.io/role=edge). Criteria fixed here before any data is observed:
#   INCONCLUSIVE if the handover feedback never appears within 400s of the
#   start, or no edge Pod is found to delete.
#   PASS if run_p2.sh's rollback checks pass (the same ones as E4-A): route
#   back to onboard, onboard Lifecycle Node active (checked by run_p2 after the
#   incident), edge Deployment and HPA removed, incident_completed with
#   analytics_migration_failed and rollback_performed=true, notification.
# Reported, not judged: the dispatcher's final outcome line, when the rollback
# started and ended relative to the fault. Expected from the code (DEV_SMOKE_TEST,
# "R5: fallback dell'onboard"): no separate loss timer in A, the rollback starts
# when the goal times out (120s from the request); the rollback reactivates the
# onboard first and waits for its real active state, then deactivates the edge
# node -- which is gone here, so A may report rollback_failed although the
# onboard is restored: that would be FAIL, read as a reporting difference, not
# as a missing fallback, and stated as such. One execution.
# Run of 14fc04c: exactly that (rollback_failed, onboard active [3]). Fixed on
# Viviana's decision (control_loop._edge_workload_gone): the failed edge
# deactivation is excused only if the node's lifecycle services are absent and
# Kubernetes shows no ready edge Pod, every other error still fails. From the
# fix on the expected outcome is PASS, with metrics.edge_deactivation saying the
# step was not needed; the criteria above are unchanged.
set -uo pipefail

ROOT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
CTX=k3d-cloud-native-p2
CLUSTER=cloud-native-p2
NS=cloud-native-p2
RESULT_DIR="$ROOT_DIR/results/edge-fallback-a/$(date -u +%Y%m%dT%H%M%SZ)"
K=(kubectl --context "$CTX" -n "$NS")
mkdir -p "$RESULT_DIR"
python3 "$ROOT_DIR/scripts/s3_provenance.py" --root "$ROOT_DIR" \
  --output "$RESULT_DIR/provenance" >"$RESULT_DIR/source-sha256.txt"
echo "Result dir: $RESULT_DIR"
log() { echo "$(date -u +%H:%M:%S) $*" | tee -a "$RESULT_DIR/run.log"; }

CORDONED=""; VERDICT=INCONCLUSIVE; DETAIL=""
finish() {
  for n in $CORDONED; do kubectl --context "$CTX" uncordon "$n" >/dev/null 2>&1; done
  cat >"$RESULT_DIR/REPORT.md" <<EOF
# Perdita dell'edge dopo il passaggio, variante A (R5) -- $VERDICT

$DETAIL

Procedura e criteri: \`scripts/run_edge_fallback_check_a.sh\` (intestazione),
fissati prima dell'esecuzione. P2-A in modalita' edgeloss: ${P2_DIR:-n/d}. Una esecuzione.
EOF
  log "ESITO: $VERDICT"
  k3d cluster delete "$CLUSTER" >>"$RESULT_DIR/run.log" 2>&1
}
trap 'finish; [[ "$VERDICT" == PASS ]]; exit $?' EXIT

log "P2-A edgeloss su cluster nuovo"
RESET_P2=1 EXPERIMENT_MODE=edgeloss VARIANT=a bash "$ROOT_DIR/scripts/run_p2.sh" >"$RESULT_DIR/p2a.log" 2>&1 &
P2_PID=$!
T0=$SECONDS; handed=""
while (( SECONDS - T0 <= 400 )) && kill -0 "$P2_PID" 2>/dev/null; do
  if "${K[@]}" logs deployment/operational-event-dispatcher-p2 --tail=300 2>/dev/null \
      | grep -Fq "Waiting for the correlated analytics SLO recovery"; then handed=1; break; fi
  sleep 2
done
if [[ -z "$handed" ]]; then
  DETAIL="feedback di passaggio all'edge mai visto in 400s"; wait "$P2_PID"; exit 0
fi
pod=$("${K[@]}" get pods -l pod-name=drone01-companion-analytics -o jsonpath='{.items[0].metadata.name}' 2>/dev/null)
if [[ -z "$pod" ]]; then
  DETAIL="nessun Pod edge trovato da cancellare"; wait "$P2_PID"; exit 0
fi
for n in $(kubectl --context "$CTX" get nodes -l kuberos.io/role=edge -o jsonpath='{.items[*].metadata.name}'); do
  kubectl --context "$CTX" cordon "$n" >>"$RESULT_DIR/run.log" 2>&1 && CORDONED="$CORDONED $n"
done
FAULT=$(date +%s.%N)
"${K[@]}" delete pod "$pod" --wait=false >>"$RESULT_DIR/run.log" 2>&1
log "onboard disattivato da A; nodi edge cordonati ($CORDONED), Pod edge $pod cancellato"
wait "$P2_PID"; P2_RC=$?
for n in $CORDONED; do kubectl --context "$CTX" uncordon "$n" >>"$RESULT_DIR/run.log" 2>&1; done; CORDONED=""
P2_DIR=$(grep -oE 'results/edgeloss/[0-9TZ]+' "$RESULT_DIR/p2a.log" | head -1)
TIMES=$(python3 - "$ROOT_DIR/$P2_DIR/dispatcher.log" "$FAULT" <<'PY'
import re, sys
fault = float(sys.argv[2])
try:
    lines = open(sys.argv[1], errors="replace").read().splitlines()
except OSError:
    lines = []
stamp = lambda l: float(re.search(r"\[(\d+\.\d+)\]", l).group(1))
rollback = next((l for l in lines if "Restoring onboard analytics" in l), None)
done = next((l for l in lines if "completed as " in l), None)
outcome = re.search(r"completed as (.*)", done).group(1) if done else "nessun esito"
fmt = lambda l: f"{stamp(l) - fault:+.0f}s" if l else "n/d"
print(f"esito del dispatcher: {outcome}; rollback avviato {fmt(rollback)}, concluso {fmt(done)} dal guasto")
PY
)
REPORT_ROWS=$(grep -E "^\| (Esito|Route finale|Deployment edge finale|Lifecycle analytics onboard) \|" "$ROOT_DIR/$P2_DIR/REPORT.md" 2>/dev/null | tr '\n' ' ')
if [[ "$P2_RC" == 0 ]]; then VERDICT=PASS; else VERDICT=FAIL; fi
DETAIL="Controlli di rollback di run_p2.sh: $([[ "$P2_RC" == 0 ]] && echo superati || echo "NON superati") ($REPORT_ROWS). $TIMES."
