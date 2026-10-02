#!/usr/bin/env bash
# Time-to-rebuild, one execution of one variant (docs/TTR_PILOT_PREREGISTRATION.md).
#
#   VARIANT=a|b FROZEN_IMAGES=file scripts/ttr/run_ttr.sh
#
# Before t0 (outside the measure): committed revision and clean tree, a new E0 cluster,
# datastore verified, the variant's images imported and their IDs checked on every node
# against FROZEN_IMAGES ("name id" lines), manifests rendered (placeholder substitution
# only), the external observer running and sampling.
# t0: immediately before the first command of the variant's procedure
# (scripts/ttr/procedure_<v>.sh, run_e0.sh's own commands).
# t1 and the verdict: scripts/ttr/judge.py. The cluster is deleted at the end.
set -euo pipefail

ROOT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)
VARIANT=${VARIANT:?VARIANT=a|b}
FROZEN_IMAGES=${FROZEN_IMAGES:?FROZEN_IMAGES=file with "name id" lines}
BUDGET_SEC=${TTR_BUDGET_SEC:-1800}
CLUSTER=cloud-native-p2
CONTEXT=k3d-$CLUSTER
NAMESPACE=cloud-native-p2
OBS_NS=ttr-observer
K=(kubectl --context "$CONTEXT")
ROBOTS=(drone01 drone02 drone03)
RESULT_DIR=${RESULT_DIR:-$ROOT_DIR/results/ttr/$(date -u +%Y%m%dT%H%M%SZ)-$VARIANT}
RENDERED_DIR="$RESULT_DIR/rendered"
mkdir -p "$RENDERED_DIR"
log() { echo "$(date -u +%FT%TZ) $*" | tee -a "$RESULT_DIR/runner.log"; }
verdict_not_started() { log "NOT_STARTED: $*"; echo "{\"verdict\": \"NOT_STARTED\", \"reason\": \"$*\"}" >"$RESULT_DIR/verdict.json"; exit 2; }

case "$VARIANT" in
  a) IMAGES=(cloud-native-ros/control-plane:p2 cloud-native-ros/event-detector:p2 cloud-native-ros/kuberos:p2
             microros/micro-ros-agent:humble px4io/px4-sitl:latest redis:7) ;;
  b) IMAGES=(cloud-native-ros/control-plane:p2 cloud-native-ros/fleet-operator:p2 cloud-native-ros/state-bridge:p2
             microros/micro-ros-agent:humble px4io/px4-sitl:latest) ;;
  *) echo "Unsupported VARIANT: $VARIANT" >&2; exit 2 ;;
esac
# k3s's local-path provisioner creates the audit PVC (25-observability, both variants) with a
# helper Pod of this image, pulled from the network on first use: the first pilot pulled it
# between t0 and t1 in A and in B (results/evidence/runs/TTR_PILOT.md). Imported and checked
# before t0 like the variants' own (Viviana, 1 October; pull policy IfNotPresent).
IMAGES+=(rancher/mirrored-library-busybox:1.36.1)

# -- GitOps boundary: everything applied comes from this committed revision
git -C "$ROOT_DIR" rev-parse HEAD >"$RESULT_DIR/revision.txt"
git -C "$ROOT_DIR" status --porcelain --untracked-files=no >"$RESULT_DIR/git-status.txt"
[[ -s "$RESULT_DIR/git-status.txt" ]] && verdict_not_started "tracked files modified: not a committed revision"
[[ -z "$(k3d cluster list --no-headers)" ]] || verdict_not_started "a cluster exists"

# -- images: the local IDs are the frozen ones (checked again on the nodes after the import)
python3 - "$FROZEN_IMAGES" "${IMAGES[@]}" >"$RESULT_DIR/images-local.txt" <<'EOF' || verdict_not_started "local images differ from the frozen IDs"
import subprocess, sys
frozen = dict(line.split()[:2] for line in open(sys.argv[1]) if line.strip())
bad = 0
for name in sys.argv[2:]:
    got = subprocess.run(["docker", "image", "inspect", "-f", "{{.Id}}", name], capture_output=True, text=True).stdout.strip()
    print(name, got, frozen.get(name))
    bad += got != frozen.get(name)
sys.exit(1 if bad else 0)
EOF

PROBE_PID=""
SAMPLER_PID=""
cleanup() {
  touch "$RESULT_DIR/samplers.stop"
  [[ -n "$PROBE_PID" ]] && kill "$PROBE_PID" 2>/dev/null || true
  [[ -n "$SAMPLER_PID" ]] && wait "$SAMPLER_PID" 2>/dev/null || true
  k3d cluster delete "$CLUSTER" >>"$RESULT_DIR/cleanup.log" 2>&1 || true
  log "clusters left: [$(k3d cluster list --no-headers | awk '{print $1}' | tr '\n' ' ')]"
}
trap cleanup EXIT

log "create cluster (e0 config)"
k3d cluster create --config "$ROOT_DIR/manifests/kubernetes/e0/k3d-cloud-native-e0.yaml" >"$RESULT_DIR/create.log" 2>&1 \
  || verdict_not_started "cluster create"
"${K[@]}" wait --for=condition=Ready node --all --timeout=180s >/dev/null || verdict_not_started "nodes not Ready"
python3 "$ROOT_DIR/scripts/datastore_check.py" check "$CLUSTER" "$RESULT_DIR" || verdict_not_started "datastore not verified"

log "import ${IMAGES[*]}"
k3d image import -c "$CLUSTER" "${IMAGES[@]}" >"$RESULT_DIR/import.log" 2>&1 || verdict_not_started "image import"
python3 - "$FROZEN_IMAGES" "${IMAGES[@]}" >"$RESULT_DIR/images-nodes.txt" <<'EOF' || verdict_not_started "node images differ from the frozen IDs"
import json, subprocess, sys
frozen = dict(line.split()[:2] for line in open(sys.argv[1]) if line.strip())
nodes = subprocess.run(["docker", "ps", "--format", "{{.Names}}", "--filter", "name=k3d-cloud-native-p2-"],
                       capture_output=True, text=True).stdout.split()
nodes = [n for n in nodes if "-server-" in n or "-agent-" in n]
bad = 0 if len(nodes) == 5 else 1
for node in sorted(nodes):
    ids = {i["id"] for i in json.loads(subprocess.run(["docker", "exec", node, "crictl", "images", "-o", "json"],
                                                     capture_output=True, text=True).stdout)["images"]}
    for name in sys.argv[2:]:
        present = frozen.get(name) in ids
        print(node, name, frozen.get(name), "present" if present else "MISSING")
        bad += not present
sys.exit(1 if bad else 0)
EOF

# -- rendering (placeholder substitution only, as run_e0.sh), before t0
DISCOVERY_SERVER_ADDRESS=$("${K[@]}" get node k3d-cloud-native-p2-server-0 \
  -o jsonpath='{.status.addresses[?(@.type=="InternalIP")].address}')
if [[ "$VARIANT" == "a" ]]; then
  python3 "$ROOT_DIR/scripts/render_e0_manifests.py" --output-dir "$RENDERED_DIR" \
    --discovery-address "$DISCOVERY_SERVER_ADDRESS" >/dev/null
  sed -e "s/__P2_DISCOVERY_ADDRESS__/$DISCOVERY_SERVER_ADDRESS/g" -e "s/__P2_GOAL_TIMEOUT_SEC__/120.0/g" \
    "$ROOT_DIR/manifests/kubernetes/p2/30-control-plane.yaml" >"$RENDERED_DIR/30-control-plane.yaml"
  sed "s/__P2_DISCOVERY_ADDRESS__/$DISCOVERY_SERVER_ADDRESS/g" \
    "$ROOT_DIR/manifests/kuberos/p2/analytics-edge.yaml" >"$RENDERED_DIR/analytics-edge.yaml"
  DISCOVERY_MANIFEST="$ROOT_DIR/manifests/kubernetes/p2/10-discovery.yaml"
  OBSERVABILITY_MANIFEST="$ROOT_DIR/manifests/kubernetes/p2/25-observability.yaml"
  BOOTSTRAP_MANIFEST="$ROOT_DIR/manifests/kubernetes/e0/30-bootstrap.yaml"
else
  sed -e "s/__P2_DISCOVERY_ADDRESS__/$DISCOVERY_SERVER_ADDRESS/g" \
    "$ROOT_DIR/manifests/kubernetes/p2/50-declarative-control-plane.yaml" >"$RENDERED_DIR/50-declarative-control-plane.yaml"
  sed -e "s/__P2_DISCOVERY_ADDRESS__/$DISCOVERY_SERVER_ADDRESS/g" \
    "$ROOT_DIR/manifests/kubernetes/e0/40-shared-infra.yaml" >"$RENDERED_DIR/40-shared-infra.yaml"
fi
sha256sum "$RENDERED_DIR"/*.yaml >"$RESULT_DIR/rendered.sha256"

# -- external observer, running before t0
"${K[@]}" create namespace "$OBS_NS" >/dev/null
"${K[@]}" create configmap ttr-ros-probe -n "$OBS_NS" --from-file=ros_probe.py="$ROOT_DIR/scripts/ttr/ros_probe.py" >/dev/null
sed "s/__TTR_DISCOVERY_ADDRESS__/$DISCOVERY_SERVER_ADDRESS/" "$ROOT_DIR/manifests/kubernetes/ttr/observer.yaml" \
  | "${K[@]}" apply -f - >/dev/null
"${K[@]}" wait --for=condition=Ready pod/ttr-ros-probe -n "$OBS_NS" --timeout=180s >/dev/null || verdict_not_started "observer Pod"
python3 "$ROOT_DIR/scripts/ttr/k8s_sampler.py" "$CONTEXT" "$NAMESPACE" "$RESULT_DIR/k8s-samples.jsonl" \
  "$RESULT_DIR/samplers.stop" 2>"$RESULT_DIR/k8s-sampler.err" &
SAMPLER_PID=$!
SPEC=$(cd "$ROOT_DIR/scripts/ttr" && python3 -c "import json, spec; print(json.dumps(spec.probe_spec('$VARIANT')))")
"${K[@]}" exec -n "$OBS_NS" ttr-ros-probe -- /bin/bash -c \
  "source /ws/install/setup.bash && exec python3 -u /probe/ros_probe.py '$SPEC'" \
  >"$RESULT_DIR/ros-samples.jsonl" 2>"$RESULT_DIR/ros-probe.err" &
PROBE_PID=$!
for _ in $(seq 1 60); do
  [[ $(grep -c . "$RESULT_DIR/ros-samples.jsonl" || true) -ge 5 && $(grep -c . "$RESULT_DIR/k8s-samples.jsonl" || true) -ge 5 ]] && break
  sleep 1
done
[[ $(grep -c . "$RESULT_DIR/ros-samples.jsonl" || true) -ge 5 ]] || verdict_not_started "ROS probe not sampling"
[[ $(grep -c . "$RESULT_DIR/k8s-samples.jsonl" || true) -ge 5 ]] || verdict_not_started "k8s sampler not sampling"

# -- t0 and the procedure
log "t0: procedure $VARIANT"
python3 -c 'import time; print(repr(time.time()))' >"$RESULT_DIR/t0"
set +e
( set -euo pipefail; source "$ROOT_DIR/scripts/ttr/procedure_$VARIANT.sh" ) >"$RESULT_DIR/procedure.log" 2>&1
PROCEDURE_RC=$?
set -e
python3 -c 'import time; print(repr(time.time()))' >"$RESULT_DIR/procedure.end"
echo "$PROCEDURE_RC" >"$RESULT_DIR/procedure.rc"
log "procedure rc=$PROCEDURE_RC; waiting for the stable series"
python3 "$ROOT_DIR/scripts/ttr/judge.py" wait "$RESULT_DIR" "$VARIANT" "$BUDGET_SEC" >"$RESULT_DIR/wait.json" || true

# -- stop the observer, collect, judge
touch "$RESULT_DIR/samplers.stop"
kill "$PROBE_PID" 2>/dev/null || true
wait "$SAMPLER_PID" 2>/dev/null || true
PROBE_PID=""
SAMPLER_PID=""
"${K[@]}" get events -A -o json >"$RESULT_DIR/events.json" 2>>"$RESULT_DIR/collect.err" || rm -f "$RESULT_DIR/events.json"
"${K[@]}" get pods -A -o wide >"$RESULT_DIR/pods.txt" 2>>"$RESULT_DIR/collect.err" || true
"${K[@]}" get deployments -n "$NAMESPACE" -o json >"$RESULT_DIR/deployments.json" 2>>"$RESULT_DIR/collect.err" || true
python3 "$ROOT_DIR/scripts/ttr/judge.py" final "$RESULT_DIR" "$VARIANT" "$BUDGET_SEC" | tee -a "$RESULT_DIR/runner.log"
