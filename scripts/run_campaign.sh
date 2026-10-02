#!/usr/bin/env bash
set -uo pipefail

ROOT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
RUNS=${RUNS:-10}
# S4 is not a campaign scenario (R14 decision, 2026-09-29): the four-drone bench and its
# matrix (scripts/run_s4_matrix.sh) are the S4 of record, not the old three-drone run_s4.sh
SCENARIOS=${SCENARIOS:-e0,e1,e2,p2,e4,s1,u1,u2}
VARIANTS=${VARIANTS:-a,b}
CAMPAIGN_ID=${CAMPAIGN_ID:-$(date -u +%Y%m%dT%H%M%SZ)}
RESULTS_ROOT=${RESULTS_ROOT:-$ROOT_DIR/results/campaigns}
CAMPAIGN_DIR=$RESULTS_ROOT/$CAMPAIGN_ID
CAMPAIGN_DRY_RUN=${CAMPAIGN_DRY_RUN:-0}
GLOBAL_SKIP_BUILD=${SKIP_IMAGE_BUILD:-0}
GLOBAL_SKIP_IMPORT=${SKIP_IMAGE_IMPORT:-0}
SOURCE_COMMIT=$(git -C "$ROOT_DIR" rev-parse HEAD)
if [[ -n $(git -C "$ROOT_DIR" status --porcelain --untracked-files=no) ]]; then
  SOURCE_DIRTY=true
else
  SOURCE_DIRTY=false
fi

usage() {
  printf "Environment: RUNS=N SCENARIOS=e0,e1,e2,p2,e4,s1,u1,u2 VARIANTS=a,b CAMPAIGN_ID=id CAMPAIGN_DRY_RUN=0|1\n"
}

if [[ ! "$RUNS" =~ ^[1-9][0-9]*$ ]]; then
  echo "RUNS must be a positive integer" >&2
  usage >&2
  exit 2
fi

IFS=, read -r -a REQUESTED_SCENARIOS <<<"$SCENARIOS"
SCENARIO_LIST=()
for raw_scenario in "${REQUESTED_SCENARIOS[@]}"; do
  scenario=${raw_scenario//[[:space:]]/}
  case "$scenario" in
    e0|e1|e2|p2|e4|s1|u1|u2) SCENARIO_LIST+=("$scenario") ;;
    s4) echo "S4 is not run by the campaign: use scripts/run_s4_matrix.sh (the four-drone bench)" >&2; exit 2 ;;
    *) echo "Unsupported scenario: $raw_scenario" >&2; usage >&2; exit 2 ;;
  esac
done
if (( ${#SCENARIO_LIST[@]} == 0 )); then
  echo "SCENARIOS cannot be empty" >&2
  exit 2
fi

IFS=, read -r -a REQUESTED_VARIANTS <<<"$VARIANTS"
VARIANT_LIST=()
for raw_variant in "${REQUESTED_VARIANTS[@]}"; do
  variant=${raw_variant//[[:space:]]/}
  case "$variant" in
    a|b) VARIANT_LIST+=("$variant") ;;
    *) echo "Unsupported variant: $raw_variant (expected 'a' or 'b')" >&2; usage >&2; exit 2 ;;
  esac
done
if (( ${#VARIANT_LIST[@]} == 0 )); then
  echo "VARIANTS cannot be empty" >&2
  exit 2
fi

# NOTE (added when extending this pre-existing, variant-A-only harness for
# the declarative comparison -- proposal's own risk-mitigation table:
# "riusare l'harness ... parametrizzandoli per variante invece di
# riscriverli"): every one of these RUNNER scripts already accepts
# VARIANT=a|b on its own (run_e4.sh delegates to run_p2.sh, which does
# too) -- this campaign runner previously never set VARIANT at all, so
# every prior campaign run (including the pre-existing validated 50-run
# campaign cited in the proposal's abstract) only ever exercised variant A.
select_scenario() {
  case "$1" in
    e0) RUNNER=$ROOT_DIR/scripts/run_e0.sh; RESET_VARIABLE=RESET_E0; BUILD_FAMILY=p2 ;;
    e1) RUNNER=$ROOT_DIR/scripts/run_e1.sh; RESET_VARIABLE=RESET_E1; BUILD_FAMILY=p2 ;;
    e2) RUNNER=$ROOT_DIR/scripts/run_e2.sh; RESET_VARIABLE=RESET_E2; BUILD_FAMILY=e2 ;;
    p2) RUNNER=$ROOT_DIR/scripts/run_p2.sh; RESET_VARIABLE=RESET_P2; BUILD_FAMILY=p2 ;;
    e4) RUNNER=$ROOT_DIR/scripts/run_e4.sh; RESET_VARIABLE=RESET_P2; BUILD_FAMILY=p2 ;;
    s1) RUNNER=$ROOT_DIR/scripts/run_s1.sh; RESET_VARIABLE=RESET_S1; BUILD_FAMILY=p2 ;;
    u1) RUNNER=$ROOT_DIR/scripts/run_u1.sh; RESET_VARIABLE=RESET_U1_BASELINE; BUILD_FAMILY=p2 ;;
    u2) RUNNER=$ROOT_DIR/scripts/run_u2.sh; RESET_VARIABLE=RESET_U2_BASELINE; BUILD_FAMILY=p2 ;;
  esac
}

mkdir -p "$CAMPAIGN_DIR/runs"
printf "{\n  \"campaign_id\": \"%s\",\n  \"runs_per_scenario\": %s,\n  \"scenarios\": \"%s\",\n  \"variants\": \"%s\",\n  \"started_at\": \"%s\",\n  \"source_commit\": \"%s\",\n  \"source_dirty\": %s\n}\n" \
  "$CAMPAIGN_ID" "$RUNS" "$SCENARIOS" "$VARIANTS" "$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
  "$SOURCE_COMMIT" "$SOURCE_DIRTY" \
  >"$CAMPAIGN_DIR/campaign.json"

# Build reuse (R9, decision D6): keyed by "scenario:variant". It was keyed by
# "family:variant", and scenarios of one family build different image sets:
# E0 never builds the mission observer's image, yet E1 (same family "p2")
# skipped its build after E0 and ran whatever image of that tag was left
# locally. Now the first run of each scenario and variant builds its own
# images from the frozen tree; a later run skips the build only if every
# project image its first run executed is still local with the same ID
# (images_still_local), otherwise it builds again. BUILT_FAMILIES is kept only
# for capture_reproducibility.py's image list below.
declare -A BUILT_CELLS=()
declare -A BUILT_FAMILIES=()
failures=0
results=0

# Provenance (R9, decision D6): the campaign's inputs at the start, and per
# cell whether they are still the same (thesis, docs/ and Markdown excluded:
# no runner reads them, and a parallel writing session may edit them).
if [[ "$CAMPAIGN_DRY_RUN" != "1" ]]; then
  SOURCE_SHA256=$(python3 "$ROOT_DIR/scripts/s3_provenance.py" --root "$ROOT_DIR" \
    --output "$CAMPAIGN_DIR/provenance")
  printf "source_commit=%s\nsource_dirty=%s\nsource_sha256=%s\n" \
    "$SOURCE_COMMIT" "$SOURCE_DIRTY" "$SOURCE_SHA256" >"$CAMPAIGN_DIR/provenance.txt"
fi

images_still_local() {  # $1 = images.json of the first run of this scenario:variant
  local image expected actual
  [[ -f "$1" ]] || return 1
  while read -r image expected; do
    actual=$(docker image inspect --format '{{.Id}}' "$image" 2>/dev/null || true)
    [[ "$actual" == "$expected" ]] || return 1
  done < <(python3 -c 'import json, sys
d = json.load(open(sys.argv[1]))
[print(i, v[0]) for i, v in d["executed"].items() if i.startswith("cloud-native-ros/") and len(v) == 1]' "$1")
}

cluster_context() {  # the k3d context a scenario's runner leaves its cluster in
  [[ "$1" == "e2" ]] && echo k3d-cloud-native-e2 || echo k3d-cloud-native-p2
}

# Interruptions (R9, decision D6): a cell with started.json and no status.json
# is INTERRUPTED; a signal to this runner also writes that status explicitly.
CURRENT_RUN_DIR=""
CURRENT_CELL=""
RUNNER_PID=""
on_interrupt() {
  if [[ -n "$RUNNER_PID" ]]; then
    pkill -TERM -P "$RUNNER_PID" 2>/dev/null || true
    kill -TERM "$RUNNER_PID" 2>/dev/null || true
  fi
  if [[ -n "$CURRENT_RUN_DIR" && ! -f "$CURRENT_RUN_DIR/status.json" ]]; then
    printf '{%s,"outcome":"INTERRUPTED","interrupted_at":"%s"}\n' \
      "$CURRENT_CELL" "$(date -u +%Y-%m-%dT%H:%M:%SZ)" >"$CURRENT_RUN_DIR/status.json"
  fi
  printf '{"campaign_id":"%s","interrupted_at":"%s"}\n' "$CAMPAIGN_ID" \
    "$(date -u +%Y-%m-%dT%H:%M:%SZ)" >"$CAMPAIGN_DIR/status.json"
  echo "Campaign interrupted: $CAMPAIGN_DIR" >&2
  exit 130
}
trap on_interrupt INT TERM

for run_number in $(seq 1 "$RUNS"); do
  for scenario in "${SCENARIO_LIST[@]}"; do
    for variant in "${VARIANT_LIST[@]}"; do
    select_scenario "$scenario"
    family_key="${BUILD_FAMILY}:${variant}"
    cell_key="${scenario}:${variant}"
    run_id=$(printf "%s-%s-%03d" "$scenario" "$variant" "$run_number")
    run_dir=$CAMPAIGN_DIR/runs/$run_id
    mkdir -p "$run_dir"

    skip_build=$GLOBAL_SKIP_BUILD
    if [[ "${BUILT_CELLS[$cell_key]:-0}" == "1" ]]; then
      if [[ "$CAMPAIGN_DRY_RUN" == "1" ]] \
          || images_still_local "$CAMPAIGN_DIR/built-images-$scenario-$variant.json"; then
        skip_build=1
      fi
    fi

    printf "[%s] scenario=%s variant=%s run=%d/%d reset=%s skip_build=%s skip_import=%s\n" \
      "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$scenario" "$variant" "$run_number" "$RUNS" \
      "$RESET_VARIABLE" "$skip_build" "$GLOBAL_SKIP_IMPORT"

    if [[ "$CAMPAIGN_DRY_RUN" == "1" ]]; then
      printf "DRY-RUN env %s=1 VARIANT=%s SKIP_IMAGE_BUILD=%s SKIP_IMAGE_IMPORT=%s %s\n" \
        "$RESET_VARIABLE" "$variant" "$skip_build" "$GLOBAL_SKIP_IMPORT" "$RUNNER"
      BUILT_CELLS[$cell_key]=1
      BUILT_FAMILIES[$family_key]=1
      continue
    fi

    CURRENT_RUN_DIR=$run_dir
    CURRENT_CELL=$(printf '"scenario":"%s","variant":"%s","run":%d' "$scenario" "$variant" "$run_number")
    started_at=$(date -u +%Y-%m-%dT%H:%M:%SZ)
    printf '{%s,"started_at":"%s","skip_build":%s}\n' "$CURRENT_CELL" "$started_at" "$skip_build" \
      >"$run_dir/started.json"
    # In the background and waited for, so that a signal reaches the trap now.
    ( set -o pipefail
      env "${RESET_VARIABLE}=1" \
        "VARIANT=$variant" \
        "SKIP_IMAGE_BUILD=$skip_build" \
        "SKIP_IMAGE_IMPORT=$GLOBAL_SKIP_IMPORT" \
        "$RUNNER" 2>&1 | tee "$run_dir/runner.log" ) &
    RUNNER_PID=$!
    wait "$RUNNER_PID"
    runner_status=$?
    RUNNER_PID=""

    result_dir=$(sed -n "s/^Evidence: //p" "$run_dir/runner.log" | tail -n 1)
    # u1/u2 (and any future scenario that calls another scenario's own
    # script as its baseline) print THEIR OWN "Evidence: ..." line at the
    # very end -- but only if they reach it. If they instead crash early
    # (set -e, no message), the LAST "Evidence:" line still in the log is
    # the baseline's own (e.g. u2 invoking u1 invoking e0), and this naive
    # tail-1 grep would silently pick that up and misattribute the
    # baseline's PASS as this scenario's own -- found live in a real
    # campaign run (u2-a, pre-fix Bug #47: 7 rows silently pointed at u1's
    # own results/u1/... evidence instead of u2's). Guard against it: the
    # result dir must actually live under results/<scenario>/.
    if [[ -n "$result_dir" && "$result_dir" != *"/results/$scenario/"* ]]; then
      echo "Evidence path '$result_dir' does not belong to scenario '$scenario' (likely a baseline's own line from an early crash) -- discarding" >&2
      result_dir=""
    fi
    pointer_value=$result_dir
    if [[ "$result_dir" == "$ROOT_DIR/"* ]]; then
      pointer_value=${result_dir#"$ROOT_DIR/"}
    fi
    if [[ -n "$result_dir" && -f "$result_dir/REPORT.md" ]]; then
      printf "%s\n" "$pointer_value" >"$run_dir/result-dir.txt"
      # Copy (never move) the runner's full raw evidence into the campaign's
      # own tree, so results/campaigns/$CAMPAIGN_ID/ is self-contained and
      # can be read/shared without also depending on the shared, historical
      # results/<scenario>/ folders (which mix years of dev-iteration runs
      # with validated ones) -- the original copy there is left untouched.
      cp -r "$result_dir" "$run_dir/evidence"
      results=$((results + 1))
    fi
    # The images the cell actually ran (from its cluster, left by the runner)
    # against the local IDs of the same tags; the inputs still unchanged.
    kubectl --context "$(cluster_context "$scenario")" get pods -A -o json \
      >"$run_dir/pods.json" 2>/dev/null || true
    images_match=$(python3 "$ROOT_DIR/scripts/campaign_cell.py" images "$run_dir/pods.json" \
      "$run_dir/images.json" "$scenario" "$variant" 2>/dev/null || echo false)
    inputs_unchanged=$(python3 "$ROOT_DIR/scripts/campaign_cell.py" inputs-unchanged \
      "$CAMPAIGN_DIR/provenance/source-manifest.json" "$ROOT_DIR" 2>/dev/null || echo "false: not checked")
    has_report=false
    [[ -n "$result_dir" && -f "$result_dir/REPORT.md" ]] && has_report=true
    outcome=$(python3 "$ROOT_DIR/scripts/campaign_cell.py" outcome "$runner_status" "$has_report")
    # R9 (Viviana, 2026-09-26): images or inputs that do not match make the
    # cell INVALID for R9, with the functional outcome and the reason kept.
    verdict=$(python3 "$ROOT_DIR/scripts/campaign_cell.py" verdict "$outcome" "$images_match" "$inputs_unchanged")
    printf '{%s,"exit_code":%d,"outcome":"%s",%s,"result_dir":"%s","started_at":"%s","finished_at":"%s","skip_build":%s,"images_match":%s,"inputs_unchanged":"%s"}\n' \
      "$CURRENT_CELL" "$runner_status" "$outcome" "$verdict" "$pointer_value" "$started_at" \
      "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$skip_build" "$images_match" "$inputs_unchanged" \
      >"$run_dir/status.json"
    CURRENT_RUN_DIR=""

    if (( runner_status == 0 )); then
      if [[ "${BUILT_CELLS[$cell_key]:-0}" != "1" ]]; then
        cp "$run_dir/images.json" "$CAMPAIGN_DIR/built-images-$scenario-$variant.json" 2>/dev/null || true
      fi
      BUILT_CELLS[$cell_key]=1
      BUILT_FAMILIES[$family_key]=1
    else
      failures=$((failures + 1))
      echo "Run $run_id failed with exit code $runner_status" >&2
    fi
    done
  done
done

if [[ "$CAMPAIGN_DRY_RUN" == "1" ]]; then
  echo "Dry run completed: $CAMPAIGN_DIR"
  exit 0
fi

if (( results > 0 )); then
  python3 "$ROOT_DIR/scripts/analyze_campaign.py" \
    --campaign-dir "$CAMPAIGN_DIR" \
    --output-dir "$CAMPAIGN_DIR/analysis"

  image_args=(
    --image microros/micro-ros-agent:humble
    --image px4io/px4-sitl:latest
    --image ros:humble-ros-base
  )
  if [[ "${BUILT_FAMILIES[p2:a]:-0}" == "1" ]]; then
    image_args+=(
      --image cloud-native-ros/control-plane:p2
      --image cloud-native-ros/event-detector:p2
      --image cloud-native-ros/kuberos:p2
      --image redis:7
    )
  fi
  if [[ "${BUILT_FAMILIES[p2:b]:-0}" == "1" ]]; then
    image_args+=(
      --image cloud-native-ros/control-plane:p2
      --image cloud-native-ros/fleet-operator:p2
      --image cloud-native-ros/state-bridge:p2
      --image redis:7
    )
  fi
  if [[ "${BUILT_FAMILIES[e2:a]:-0}" == "1" || "${BUILT_FAMILIES[e2:b]:-0}" == "1" ]]; then
    image_args+=(
      --image cloud-native-ros/control-plane:e2
      --image cloud-native-ros/event-detector:e2-upstream
    )
  fi
  python3 "$ROOT_DIR/scripts/capture_reproducibility.py" \
    --campaign-dir "$CAMPAIGN_DIR" \
    --campaign-source-commit "$SOURCE_COMMIT" \
    --validation-commit "$SOURCE_COMMIT" \
    "${image_args[@]}"
fi

printf "{\n  \"campaign_id\": \"%s\",\n  \"completed_runs\": %d,\n  \"failed_runs\": %d,\n  \"finished_at\": \"%s\"\n}\n" \
  "$CAMPAIGN_ID" "$results" "$failures" "$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
  >"$CAMPAIGN_DIR/status.json"

echo "Campaign evidence: $CAMPAIGN_DIR"
(( failures == 0 ))
