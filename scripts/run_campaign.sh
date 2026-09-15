#!/usr/bin/env bash
set -uo pipefail

ROOT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
RUNS=${RUNS:-10}
SCENARIOS=${SCENARIOS:-e0,e1,e2,p2,e4}
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
  printf "Environment: RUNS=N SCENARIOS=e0,e1,e2,p2,e4 CAMPAIGN_ID=id CAMPAIGN_DRY_RUN=0|1\n"
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
    e0|e1|e2|p2|e4) SCENARIO_LIST+=("$scenario") ;;
    *) echo "Unsupported scenario: $raw_scenario" >&2; usage >&2; exit 2 ;;
  esac
done
if (( ${#SCENARIO_LIST[@]} == 0 )); then
  echo "SCENARIOS cannot be empty" >&2
  exit 2
fi

select_scenario() {
  case "$1" in
    e0) RUNNER=$ROOT_DIR/scripts/run_e0.sh; RESET_VARIABLE=RESET_E0; BUILD_FAMILY=p2 ;;
    e1) RUNNER=$ROOT_DIR/scripts/run_e1.sh; RESET_VARIABLE=RESET_E1; BUILD_FAMILY=p2 ;;
    e2) RUNNER=$ROOT_DIR/scripts/run_e2.sh; RESET_VARIABLE=RESET_E2; BUILD_FAMILY=e2 ;;
    p2) RUNNER=$ROOT_DIR/scripts/run_p2.sh; RESET_VARIABLE=RESET_P2; BUILD_FAMILY=p2 ;;
    e4) RUNNER=$ROOT_DIR/scripts/run_e4.sh; RESET_VARIABLE=RESET_P2; BUILD_FAMILY=p2 ;;
  esac
}

mkdir -p "$CAMPAIGN_DIR/runs"
printf "{\n  \"campaign_id\": \"%s\",\n  \"runs_per_scenario\": %s,\n  \"scenarios\": \"%s\",\n  \"started_at\": \"%s\",\n  \"source_commit\": \"%s\",\n  \"source_dirty\": %s\n}\n" \
  "$CAMPAIGN_ID" "$RUNS" "$SCENARIOS" "$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
  "$SOURCE_COMMIT" "$SOURCE_DIRTY" \
  >"$CAMPAIGN_DIR/campaign.json"

declare -A BUILT_FAMILIES=()
failures=0
results=0

for run_number in $(seq 1 "$RUNS"); do
  for scenario in "${SCENARIO_LIST[@]}"; do
    select_scenario "$scenario"
    run_id=$(printf "%s-%03d" "$scenario" "$run_number")
    run_dir=$CAMPAIGN_DIR/runs/$run_id
    mkdir -p "$run_dir"

    skip_build=$GLOBAL_SKIP_BUILD
    if [[ "${BUILT_FAMILIES[$BUILD_FAMILY]:-0}" == "1" ]]; then
      skip_build=1
    fi

    printf "[%s] scenario=%s run=%d/%d reset=%s skip_build=%s skip_import=%s\n" \
      "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$scenario" "$run_number" "$RUNS" \
      "$RESET_VARIABLE" "$skip_build" "$GLOBAL_SKIP_IMPORT"

    if [[ "$CAMPAIGN_DRY_RUN" == "1" ]]; then
      printf "DRY-RUN env %s=1 SKIP_IMAGE_BUILD=%s SKIP_IMAGE_IMPORT=%s %s\n" \
        "$RESET_VARIABLE" "$skip_build" "$GLOBAL_SKIP_IMPORT" "$RUNNER"
      BUILT_FAMILIES[$BUILD_FAMILY]=1
      continue
    fi

    env "${RESET_VARIABLE}=1" \
      "SKIP_IMAGE_BUILD=$skip_build" \
      "SKIP_IMAGE_IMPORT=$GLOBAL_SKIP_IMPORT" \
      "$RUNNER" 2>&1 | tee "$run_dir/runner.log"
    runner_status=${PIPESTATUS[0]}

    result_dir=$(sed -n "s/^Evidence: //p" "$run_dir/runner.log" | tail -n 1)
    pointer_value=$result_dir
    if [[ "$result_dir" == "$ROOT_DIR/"* ]]; then
      pointer_value=${result_dir#"$ROOT_DIR/"}
    fi
    if [[ -n "$result_dir" && -f "$result_dir/REPORT.md" ]]; then
      printf "%s\n" "$pointer_value" >"$run_dir/result-dir.txt"
      results=$((results + 1))
    fi
    printf "{\"scenario\":\"%s\",\"run\":%d,\"exit_code\":%d,\"result_dir\":\"%s\"}\n" \
      "$scenario" "$run_number" "$runner_status" "$pointer_value" \
      >"$run_dir/status.json"

    if (( runner_status == 0 )); then
      BUILT_FAMILIES[$BUILD_FAMILY]=1
    else
      failures=$((failures + 1))
      echo "Run $run_id failed with exit code $runner_status" >&2
    fi
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
  if [[ "${BUILT_FAMILIES[p2]:-0}" == "1" ]]; then
    image_args+=(
      --image cloud-native-ros/control-plane:p2
      --image cloud-native-ros/event-detector:p2
      --image cloud-native-ros/kuberos:p2
      --image redis:7
    )
  fi
  if [[ "${BUILT_FAMILIES[e2]:-0}" == "1" ]]; then
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
