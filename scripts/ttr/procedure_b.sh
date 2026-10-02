# Time-to-rebuild, variant B: the declarative bring-up of run_e0.sh (variant B), command
# for command, from the CRDs to the last manifest. Sourced by run_ttr.sh between t0 and the
# procedure's end; every command line here must appear verbatim in run_e0.sh
# (operator/tests/test_ttr.py). Left out on purpose: E0's PX4 uid/restart reads between
# the shared infra and the workload (a check of E0, not part of the bring-up).
# Needs: K, NAMESPACE, ROOT_DIR, RENDERED_DIR, ROBOTS.

"${K[@]}" apply -f "$ROOT_DIR/operator/crds/"
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
"${K[@]}" rollout status deployment/p2-audit-writer -n "$NAMESPACE" --timeout=120s
"${K[@]}" rollout status deployment/p2-operator-notifier -n "$NAMESPACE" --timeout=120s
"${K[@]}" rollout status deployment/p2-platform-observer -n "$NAMESPACE" --timeout=120s

"${K[@]}" apply -f "$RENDERED_DIR/40-shared-infra.yaml"
for robot in "${ROBOTS[@]}"; do
  "${K[@]}" rollout status "deployment/$robot-px4-sitl" -n "$NAMESPACE" --timeout=180s
  "${K[@]}" rollout status "deployment/$robot-microxrce-agent" -n "$NAMESPACE" --timeout=180s
done

"${K[@]}" apply -f "$ROOT_DIR/manifests/kubernetes/e0/60-declarative-workload.yaml"
"${K[@]}" apply -f "$ROOT_DIR/manifests/kubernetes/e0/70-declarative-robotfleet.yaml"
