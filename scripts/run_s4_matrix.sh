#!/usr/bin/env bash
# S4 matrix (R13, docs/R13_S4_CELLS_PREREGISTRATION.md): one round of the eight cells,
# L1 x3 and L3 in A and B, each on a fresh cluster (scripts/run_s4_bench.sh), in a
# fixed order alternating A and B within each cell (A first in round 1, B first in round 2). A VALID trial and a FUNCTIONAL
# success are distinct: PASS and FAIL (a valid functional failure) both continue to the
# next cell; the round stops -- to be read -- on INTERRUPTED, NOT_STARTED,
# INCONCLUSIVE, a setup failure (no judgement) or a cluster left behind.
#
#   S4_ROUND=1|2 bash scripts/run_s4_matrix.sh
#
# Status and gate in results/s4/matrix-round<N>-<UTC>/ (status.txt, gate.jsonl).
set -uo pipefail
ROOT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
ROUND=${S4_ROUND:?set S4_ROUND=1 or 2}
RUNNER=${S4_BENCH_RUNNER:-$ROOT_DIR/scripts/run_s4_bench.sh}   # tests only
# round 1: A first in each cell; round 2: B first (Viviana: A does not always go first)
if [[ "$ROUND" == 1 ]]; then
  DEFAULT_ORDER="a:l1-battery b:l1-battery a:l1-telemetry b:l1-telemetry a:l1-edge b:l1-edge a:l3 b:l3"
else
  DEFAULT_ORDER="b:l1-battery a:l1-battery b:l1-telemetry a:l1-telemetry b:l1-edge a:l1-edge b:l3 a:l3"
fi
ORDER=${S4_ORDER:-$DEFAULT_ORDER}
DIR="${S4_RESULTS_ROOT:-$ROOT_DIR/results/s4}/matrix-round$ROUND-$(date -u +%Y%m%dT%H%M%SZ)"
mkdir -p "$DIR"
export S4_MATRIX_PID=$$            # the cells' runner excuses this one ancestor, nothing else
STATUS="$DIR/status.txt"
n=0
for step in $ORDER; do
  n=$((n + 1)); variant=${step%%:*}; cell=${step#*:}
  echo "start $n $variant $cell $(date -u +%FT%TZ)" >>"$STATUS"
  VARIANT=$variant S4_MODE=cell S4_CELL=$cell bash "$RUNNER" >"$DIR/cell-$n.out" 2>&1
  rc=$?
  result=$(sed -n 's/^Result dir: //p' "$DIR/cell-$n.out" | head -n 1)
  verdict=$(python3 -c 'import json,sys
try: print(json.load(open(sys.argv[1]+"/s4-judge.json"))["verdict"])
except Exception: print("NO_JUDGEMENT")' "$result")
  left=$(k3d cluster list --no-headers 2>/dev/null | awk '{print $1}' | grep -x cloud-native-s4 || true)
  gate=CONTINUE
  case "$verdict" in PASS|FAIL) ;; *) gate=STOP;; esac
  [[ -n "$left" ]] && gate=STOP
  printf '{"cell": %d, "variant": "%s", "name": "%s", "exit": %d, "verdict": "%s", "dir": "%s", "cluster_left": %s, "gate": "%s"}\n' \
    "$n" "$variant" "$cell" "$rc" "$verdict" "$result" "$([[ -n "$left" ]] && echo true || echo false)" "$gate" >>"$DIR/gate.jsonl"
  echo "end $n $variant $cell exit $rc verdict $verdict gate $gate $(date -u +%FT%TZ)" >>"$STATUS"
  if [[ "$gate" == STOP ]]; then echo "round stopped after cell $n" >>"$STATUS"; exit 0; fi
done
echo "round complete" >>"$STATUS"
