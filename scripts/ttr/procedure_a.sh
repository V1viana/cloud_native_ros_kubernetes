# Time-to-rebuild, variant A: the imperative bootstrap of run_e0.sh (variant A, no image
# lock), command for command, from the first apply to the end of the bootstrap. Sourced by
# run_ttr.sh between t0 and the procedure's end; every command line here must appear
# verbatim in run_e0.sh (operator/tests/test_ttr.py). Not a copy of E0's checks: the
# readiness end point is the external observer's, not these waits.
# Needs: K, NAMESPACE, ROOT_DIR, RENDERED_DIR, DISCOVERY_MANIFEST, OBSERVABILITY_MANIFEST,
# BOOTSTRAP_MANIFEST.

"${K[@]}" apply -f "$ROOT_DIR/manifests/kubernetes/p2/00-rbac.yaml"
"${K[@]}" apply -f "$DISCOVERY_MANIFEST"
"${K[@]}" apply -f "$RENDERED_DIR/20-kuberos.yaml"
"${K[@]}" apply -f "$OBSERVABILITY_MANIFEST"
"${K[@]}" rollout status deployment/p2-fastdds-discovery -n "$NAMESPACE" --timeout=180s
"${K[@]}" rollout status deployment/kuberos -n "$NAMESPACE" --timeout=300s
"${K[@]}" wait --for=create secret/kuberos-api-token -n "$NAMESPACE" --timeout=90s

"${K[@]}" create configmap p2-analytics-edge-manifest -n "$NAMESPACE" \
  --from-file=analytics-edge.yaml="$RENDERED_DIR/analytics-edge.yaml" \
  --dry-run=client -o yaml | "${K[@]}" apply -f -
"${K[@]}" apply -f "$RENDERED_DIR/30-control-plane.yaml"

"${K[@]}" create configmap e0-kuberos-manifests -n "$NAMESPACE" \
  --from-file=drone01.yaml="$RENDERED_DIR/drone01.yaml" \
  --from-file=drone02.yaml="$RENDERED_DIR/drone02.yaml" \
  --from-file=drone03.yaml="$RENDERED_DIR/drone03.yaml" \
  --dry-run=client -o yaml | "${K[@]}" apply -f -
"${K[@]}" delete job e0-kuberos-bootstrap -n "$NAMESPACE" --ignore-not-found
"${K[@]}" apply -f "$BOOTSTRAP_MANIFEST"
"${K[@]}" wait --for=condition=complete job/e0-kuberos-bootstrap \
  -n "$NAMESPACE" --timeout=900s
"${K[@]}" wait --for=condition=available deployment --all \
  -n "$NAMESPACE" --timeout=300s
