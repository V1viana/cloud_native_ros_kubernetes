#!/usr/bin/env python3
"""S3's own "costo del control plane" tooling (proposal S7): snapshot and
diff the Kubernetes API server's own Prometheus metrics -- confirmed live
to be already exposed by this project's k3s-based clusters, no new
instrumentation needed in-cluster. `etcd_requests_total` is what the API
server calls its own storage-layer request counter regardless of the real
backend; these clusters run k3s's default embedded SQLite (via "kine"), not
real etcd (checked live: no --cluster-init/etcd flags, an essentially-empty
db/etcd/ directory, an actively-growing db/state.db) -- kine speaks the
etcd gRPC API to the apiserver, so this counter is still the apiserver's
own genuine measurement of storage-layer traffic, just not backed by real
etcd here. Documented as a known simplification, not hidden.

`etcd_requests_total` carries both a `type` label (the resource, e.g.
"pods") and an `operation` label (the verb: "get"/"list"/"listWithCount"
are reads, "create"/"update"/"delete"/"patch" are writes -- confirmed live
against a running cluster's own /metrics). The proposal's own metric table
("Metriche e KPI") asks for "scritture etcd" specifically, as a figure
distinct from "Chiamate API/s" -- so the two must not be conflated into one
sum. Found live, 2026-09-22: an earlier version of this script did exactly
that (summing every operation together, unfiltered, and reporting it as
"Scritture storage" in every S3 result collected so far) -- a real
mislabeling, not a naming quibble. Its effect on the A/B comparison must
be computed from the saved operation breakdown, not assumed from the
architecture: both variants generate reads as well as writes.

Usage:
  s3_metrics.py snapshot --context <kubectx> --out snap.json
  s3_metrics.py diff --before before.json --after after.json [--out diff.json]
"""

import argparse
import json
import math
import subprocess
import time


def scrape(kube_context):
    proc = subprocess.run(
        ["kubectl", "--context", kube_context, "get", "--raw", "/metrics"],
        check=True, capture_output=True, text=True, timeout=30,
    )
    return proc.stdout


def parse_counters(raw_text, metric_names):
    """Minimal Prometheus text-format parser for the handful of counter
    families this tool cares about -- avoids a prometheus_client dependency
    for a one-off scrape/diff script."""
    counters = {name: {} for name in metric_names}
    for line in raw_text.splitlines():
        if not line or line.startswith("#"):
            continue
        for name in metric_names:
            if not line.startswith(name + "{") and not line.startswith(name + " "):
                continue
            try:
                if "{" in line:
                    labels_part, value_part = line[len(name) + 1:].rsplit("}", 1)
                    value = float(value_part.strip())
                    labels = dict(
                        item.split("=", 1)
                        for item in _split_labels(labels_part)
                    )
                    labels = {k: v.strip('"') for k, v in labels.items()}
                else:
                    value = float(line[len(name):].strip())
                    labels = {}
            except ValueError:
                continue
            key = json.dumps(labels, sort_keys=True)
            counters[name][key] = counters[name].get(key, 0.0) + value
            break
    return counters


def _split_labels(labels_part):
    # Labels are comma-separated but values are quoted and may themselves
    # be safely comma-free here (Kubernetes API metric label values never
    # contain literal commas) -- a plain split is sufficient and avoids
    # pulling in a real Prometheus parser for this one-off tool.
    return [item for item in labels_part.split(",") if item]


METRIC_NAMES = ["apiserver_request_total", "etcd_requests_total", "apiserver_storage_objects"]
COUNTER_METRICS = {"apiserver_request_total", "etcd_requests_total"}


def cmd_snapshot(args):
    raw = scrape(args.context)
    counters = parse_counters(raw, METRIC_NAMES)
    snapshot = {"timestamp_utc": time.time(), "context": args.context, "counters": counters}
    with open(args.out, "w", encoding="utf-8") as stream:
        json.dump(snapshot, stream, indent=2, sort_keys=True)
    print(args.out)


def cmd_diff(args):
    with open(args.before, encoding="utf-8") as stream:
        before = json.load(stream)
    with open(args.after, encoding="utf-8") as stream:
        after = json.load(stream)

    result = {
        "elapsed_sec": after["timestamp_utc"] - before["timestamp_utc"],
        "deltas": {},
    }
    if not math.isfinite(result["elapsed_sec"]) or result["elapsed_sec"] <= 0:
        raise ValueError("metric snapshots must have a positive, finite time interval")
    for metric in METRIC_NAMES:
        before_counters = before["counters"].get(metric, {})
        after_counters = after["counters"].get(metric, {})
        deltas = {}
        missing = set(before_counters) - set(after_counters)
        if missing and metric in COUNTER_METRICS:
            raise ValueError(f"{metric} lost {len(missing)} counter series between snapshots")
        for key, after_value in after_counters.items():
            before_value = before_counters.get(key, 0.0)
            delta = after_value - before_value
            if not math.isfinite(delta) or (delta < 0 and metric in COUNTER_METRICS):
                raise ValueError(f"{metric} counter reset or invalid value: {key}")
            if delta:
                deltas[key] = delta
        result["deltas"][metric] = deltas
        result[f"{metric}_total_delta"] = sum(deltas.values())

    # Per-resource-type request breakdown for apiserver_request_total,
    # collapsing the verb/scope/code dimensions -- the "chiamate/s per
    # risorsa" the proposal's own metric table asks for, without needing to
    # eyeball dozens of raw label combinations by hand.
    by_resource = {}
    for key, delta in result["deltas"].get("apiserver_request_total", {}).items():
        labels = json.loads(key)
        resource = labels.get("resource") or f"(non-resource {labels.get('subresource', '')})"
        by_resource[resource] = by_resource.get(resource, 0.0) + delta
    result["apiserver_requests_by_resource"] = dict(
        sorted(by_resource.items(), key=lambda item: -item[1])
    )

    by_type = {}
    for key, delta in result["deltas"].get("etcd_requests_total", {}).items():
        labels = json.loads(key)
        rtype = labels.get("type", "?")
        by_type[rtype] = by_type.get(rtype, 0.0) + delta
    result["etcd_requests_by_type"] = dict(sorted(by_type.items(), key=lambda item: -item[1]))

    # "scritture etcd" (proposal, "Metriche e KPI") means writes, not every
    # storage-layer request -- split by the `operation` label rather than
    # reusing etcd_requests_total_total_delta, which sums reads and writes
    # together under one number.
    READ_OPERATIONS = {"get", "list", "listWithCount", "watch"}
    WRITE_OPERATIONS = {"create", "update", "delete", "patch"}
    by_operation = {}
    for key, delta in result["deltas"].get("etcd_requests_total", {}).items():
        labels = json.loads(key)
        op = labels.get("operation", "?")
        by_operation[op] = by_operation.get(op, 0.0) + delta
    result["etcd_requests_by_operation"] = dict(
        sorted(by_operation.items(), key=lambda item: -item[1]))
    result["etcd_requests_write_total_delta"] = sum(
        delta for op, delta in by_operation.items() if op in WRITE_OPERATIONS)
    result["etcd_requests_read_total_delta"] = sum(
        delta for op, delta in by_operation.items() if op in READ_OPERATIONS)
    unclassified = set(by_operation) - READ_OPERATIONS - WRITE_OPERATIONS
    if unclassified:
        result["etcd_requests_unclassified_operations"] = sorted(unclassified)

    # These rates use the counter snapshots' own interval, not the scenario's
    # separate wall-clock timer (which excludes its final metrics scrape).
    result["apiserver_requests_per_sec"] = (
        result["apiserver_request_total_total_delta"] / result["elapsed_sec"])
    result["storage_writes_per_sec"] = (
        result["etcd_requests_write_total_delta"] / result["elapsed_sec"])

    if args.out:
        with open(args.out, "w", encoding="utf-8") as stream:
            json.dump(result, stream, indent=2, sort_keys=True)
        print(args.out)
    else:
        print(json.dumps(result, indent=2, sort_keys=True))


def main(args=None):
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)

    snap = sub.add_parser("snapshot")
    snap.add_argument("--context", required=True)
    snap.add_argument("--out", required=True)
    snap.set_defaults(func=cmd_snapshot)

    diff = sub.add_parser("diff")
    diff.add_argument("--before", required=True)
    diff.add_argument("--after", required=True)
    diff.add_argument("--out", default="")
    diff.set_defaults(func=cmd_diff)

    parsed = parser.parse_args(args)
    parsed.func(parsed)


if __name__ == "__main__":
    main()
