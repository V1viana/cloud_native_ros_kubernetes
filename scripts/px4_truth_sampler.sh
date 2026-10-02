#!/usr/bin/env bash
# PX4 SIH ground truth sampler for scripts/position_hold.py (checklist R8, E1).
# Usage: px4_truth_sampler.sh NODE_CONTAINER PX4_CONTAINER_ID OUT; runs until killed.
# Reads uORB vehicle_local_position_groundtruth with a one-shot px4-listener
# through crictl on the k3d node, so no Kubernetes API is involved and it keeps
# working with the k3d server stopped (checked 2026-09-25). One read takes about
# 0.2s. The raw output is appended with the host clock before and after each
# read; parsing happens offline (position_hold.parse_truth). A read hung past 3s
# is abandoned and simply leaves no sample.
set -u
while :; do
  t0=$(date +%s%N)
  out=$(timeout 3 docker exec "$1" crictl exec "$2" sh -c \
    'cd /opt/px4; PATH=/opt/px4/bin:$PATH; px4-listener vehicle_local_position_groundtruth 1' 2>&1)
  t1=$(date +%s%N)
  printf 'SAMPLE %s %s\n%s\nEND\n' "$t0" "$t1" "$out" >>"$3"
done
