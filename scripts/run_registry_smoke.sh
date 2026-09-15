#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
CLUSTER=${CLUSTER:-cloud-native-p2}
CONTEXT=${CONTEXT:-k3d-cloud-native-p2}
NAMESPACE=${NAMESPACE:-cloud-native-p2}
LOCK_FILE=${LOCK_FILE:-$ROOT_DIR/config/project-image-lock.json}
PORT=${KUBEROS_PORT_FORWARD_PORT:-18080}
RESULT_ID=${RESULT_ID:-$(date -u +%Y%m%dT%H%M%SZ)}
RESULT_DIR=${RESULT_DIR:-$ROOT_DIR/results/registry-smoke/$RESULT_ID}
TEMPLATE=$ROOT_DIR/manifests/kuberos/registry-smoke/private-image-smoke.yaml
MANIFEST=$RESULT_DIR/private-image-smoke.yaml
APPLICATION=registry-smoke-drone01
WORKLOAD=drone01-registry-smoke
SECRET=kuberos-test-repo
K=(kubectl --context "$CONTEXT" -n "$NAMESPACE")
SECRET_CREATED=0
CREATE_SUBMITTED=0
PORT_FORWARD_PID=""

mkdir -p "$RESULT_DIR"

cleanup() {
  set +e
  if [[ "$CREATE_SUBMITTED" == "1" && -n "$PORT_FORWARD_PID" ]]; then
    python3 "$ROOT_DIR/scripts/kuberos_registry_smoke_client.py" delete \
      --manifest "$MANIFEST" \
      --kuberos-url "http://127.0.0.1:$PORT" \
      >>"$RESULT_DIR/delete-client.log" 2>&1
    "${K[@]}" wait --for=delete "deployment/$WORKLOAD" --timeout=90s \
      >>"$RESULT_DIR/delete-client.log" 2>&1
  fi
  if [[ -n "$PORT_FORWARD_PID" ]]; then
    kill "$PORT_FORWARD_PID" 2>/dev/null
    wait "$PORT_FORWARD_PID" 2>/dev/null
  fi
  if [[ "$SECRET_CREATED" == "1" ]]; then
    "${K[@]}" delete secret "$SECRET" --ignore-not-found >/dev/null
  fi
}
trap cleanup EXIT

if ! k3d cluster list --no-headers | awk '{print $1}' | grep -qx "$CLUSTER"; then
  echo "Cluster $CLUSTER is not running" >&2
  exit 1
fi
if [[ ! -r "$LOCK_FILE" ]]; then
  echo "Image lock not found: $LOCK_FILE" >&2
  exit 1
fi
if [[ ! -r "$HOME/.docker/config.json" ]]; then
  echo "Docker authentication config is unavailable" >&2
  exit 1
fi
if "${K[@]}" get deployment "$WORKLOAD" >/dev/null 2>&1; then
  echo "Temporary workload $WORKLOAD already exists" >&2
  exit 1
fi

PRIVATE_IMAGE_DIGEST=$(python3 - "$LOCK_FILE" <<'PY'
import json
import sys

lock = json.load(open(sys.argv[1], encoding="utf-8"))
matches = [item for item in lock["images"] if item["name"] == "control-plane"]
if len(matches) != 1:
    raise SystemExit("control-plane digest missing from image lock")
print(matches[0]["immutable_reference"])
PY
)
DISCOVERY_ADDRESS=$(kubectl --context "$CONTEXT" get node \
  k3d-cloud-native-p2-server-0 \
  -o jsonpath='{.status.addresses[?(@.type=="InternalIP")].address}')

sed \
  -e "s#__PRIVATE_IMAGE_DIGEST__#$PRIVATE_IMAGE_DIGEST#g" \
  -e "s#__P2_DISCOVERY_ADDRESS__#$DISCOVERY_ADDRESS#g" \
  "$TEMPLATE" >"$MANIFEST"

if ! "${K[@]}" get secret "$SECRET" >/dev/null 2>&1; then
  "${K[@]}" create secret generic "$SECRET" \
    --from-file=.dockerconfigjson="$HOME/.docker/config.json" \
    --type=kubernetes.io/dockerconfigjson >/dev/null
  SECRET_CREATED=1
fi

kubectl --context "$CONTEXT" -n "$NAMESPACE" port-forward \
  service/kuberos-api "$PORT:8000" >"$RESULT_DIR/port-forward.log" 2>&1 &
PORT_FORWARD_PID=$!
for _ in $(seq 1 30); do
  if curl -fsS "http://127.0.0.1:$PORT/api/v1/" >/dev/null 2>&1; then
    break
  fi
  sleep 1
done
kill -0 "$PORT_FORWARD_PID"

"${K[@]}" exec deployment/kuberos -c api -- \
  python manage.py bootstrap_p2 >"$RESULT_DIR/token-refresh.log"

KUBEROS_API_TOKEN=$("${K[@]}" get secret kuberos-api-token \
  -o jsonpath='{.data.token}' | base64 -d)
export KUBEROS_API_TOKEN
python3 "$ROOT_DIR/scripts/kuberos_registry_smoke_client.py" create \
  --manifest "$MANIFEST" \
  --kuberos-url "http://127.0.0.1:$PORT" \
  | tee "$RESULT_DIR/create-client.json"
CREATE_SUBMITTED=1

"${K[@]}" rollout status "deployment/$WORKLOAD" --timeout=180s
POD=$("${K[@]}" get pod -l "pod-name=$WORKLOAD" \
  -o jsonpath='{.items[0].metadata.name}')
IMAGE=$("${K[@]}" get pod "$POD" -o jsonpath='{.spec.containers[0].image}')
IMAGE_ID=$("${K[@]}" get pod "$POD" \
  -o jsonpath='{.status.containerStatuses[0].imageID}')
NODE=$("${K[@]}" get pod "$POD" -o jsonpath='{.spec.nodeName}')
PULL_SECRET=$("${K[@]}" get deployment "$WORKLOAD" \
  -o jsonpath='{.spec.template.spec.imagePullSecrets[0].name}')

PASS=true
[[ "$IMAGE" == "$PRIVATE_IMAGE_DIGEST" ]] || PASS=false
[[ "$IMAGE_ID" == *"${PRIVATE_IMAGE_DIGEST#*@}" ]] || PASS=false
[[ "$NODE" == "k3d-cloud-native-p2-agent-0" ]] || PASS=false
[[ "$PULL_SECRET" == "$SECRET" ]] || PASS=false

python3 "$ROOT_DIR/scripts/kuberos_registry_smoke_client.py" delete \
  --manifest "$MANIFEST" \
  --kuberos-url "http://127.0.0.1:$PORT" \
  | tee "$RESULT_DIR/delete-client.json"
"${K[@]}" wait --for=delete "deployment/$WORKLOAD" --timeout=90s
CREATE_SUBMITTED=0

cat >"$RESULT_DIR/REPORT.md" <<EOF
# R1 Private Registry KubeROS Smoke Result

| Campo | Valore |
| --- | --- |
| Esito | $PASS |
| Cluster / namespace | $CLUSTER / $NAMESPACE |
| ApplicationDeployment | $APPLICATION |
| Immagine richiesta | $PRIVATE_IMAGE_DIGEST |
| Image ID osservato | $IMAGE_ID |
| Pull secret nel Deployment KubeROS | $PULL_SECRET |
| Nodo | $NODE |
| Workload finale | eliminato tramite API KubeROS |

KubeROS ha creato un Deployment ROS 2 temporaneo usando esclusivamente il
digest del repository Docker Hub privato. Kubernetes ha risolto il manifest
con il pull secret dichiarato nell'ApplicationDeployment; il Pod e' diventato
Ready sul nodo onboard e il workload e' stato poi eliminato tramite KubeROS.
EOF

echo "Registry smoke result: $PASS"
echo "Evidence: $RESULT_DIR"
[[ "$PASS" == "true" ]]
