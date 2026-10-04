#!/usr/bin/env python3
"""The "edges only" S3 observer E (docs/S3_OBSERVER_PROBE_PREREGISTRATION_DRAFT.md): no read
inside the window [T0, T0 + L]. Reads, all through one persistent `kubectl proxy` (API server)
or directly on etcd :2381:
- one /metrics when the API answers (the API server's process start time, for the restarts);
- a SERIAL etcd burst triggered by run_s3.sh's own files (start edge of L3):
  first read as soon as metrics-after-bootstrap.json appears; further reads every
  BURST_PERIOD_SEC only while pods-before-incident.json is absent (at most BURST_MAX_READS,
  BURST_MAX_SEC); never a new read once metrics-before-incident.json exists, and a read still
  running when it appears is flagged (L3 not measurable) -- never a read inside the window;
- at T0 + L: /metrics, etcd and the workload check (end edges).
The start edge of L0/L1/L2 is run_s3.sh's own snapshot taken right before T0
(metrics-before-incident.json): no extra read. Its timestamp is taken AFTER the scrape and
the parse, so the counters were read at an unknown instant before it (scrape uncertainty,
reported, the runner unchanged).
"""

import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "c2_pilot"))
import layers  # noqa: E402
import metrics_sampler as ms  # noqa: E402
import s3_metrics  # noqa: E402

BURST_PERIOD_SEC = 0.1
BURST_MAX_READS = 10
BURST_MAX_SEC = 1.0
POLL_SEC = 0.02
AFTER_BOOTSTRAP = "metrics-after-bootstrap.json"
PODS_BEFORE = "pods-before-incident.json"
BEFORE_INCIDENT = "metrics-before-incident.json"


def read_api(proxy_url, account):
    """/metrics through the proxy (not counted in apiserver_request_total): L0/L1/L2, the API
    server's start time, families, content bytes. Errors are recorded, never raised."""
    t = time.time()
    try:
        return {"error": None, **ms.read_once_http(proxy_url, "", None, False, account)}
    except Exception as exc:
        return {"error": repr(exc), "scrape": {"start": t, "end": time.time()}}


def read_etcd(etcd_url, account):
    """etcd :2381 directly: the L3 counters and, if exposed, etcd's own process start time."""
    start = time.time()
    try:
        code, body = ms.http_get(etcd_url, account, "etcd", counted=False, timeout=0.8)
    except Exception as exc:
        return {"error": repr(exc), "t_start": start, "t_end": time.time()}
    end = time.time()
    if code != 200:
        return {"error": f"HTTP {code}", "t_start": start, "t_end": end}
    text = body.decode()
    present = {name for name in layers.ETCD if any(line.startswith(name) for line in text.splitlines())}
    started = s3_metrics.parse_counters(text, ["process_start_time_seconds"])["process_start_time_seconds"]
    return {"error": None, "t_start": start, "t_end": end,
            "L3": {k: v for k, v in layers.counters(text, layers.ETCD).items() if k in present},
            "families": {"etcd_l3": present == set(layers.ETCD), "etcd_start_time": bool(started)},
            "etcd_start": next(iter(started.values()), None)}


def read_workload(proxy_url, namespace, account):
    """Namespace GET and Deployment LIST (both counted)."""
    try:
        code, _ = ms.http_get(f"{proxy_url}/api/v1/namespaces/{namespace}", account, "apiserver", counted=True)
        present = code == 200
        code, body = ms.http_get(f"{proxy_url}/apis/apps/v1/namespaces/{namespace}/deployments", account,
                                 "apiserver", counted=True)
        deployments = len(json.loads(body).get("items", [])) if code == 200 else 0
        return {"error": None, "namespace_present": present, "deployments": deployments, "t": time.time()}
    except Exception as exc:
        return {"error": repr(exc), "t": time.time()}


def burst(etcd_url, result_dir, account, period=BURST_PERIOD_SEC, max_reads=BURST_MAX_READS,
          max_sec=BURST_MAX_SEC, clock=time.time, sleep=time.sleep):
    """The serial etcd burst, called as soon as metrics-after-bootstrap.json exists. Reads are
    never concurrent; each records its start, end and whether metrics-before-incident.json
    existed at its end (`overlap`: then L3 is not measurable for the run)."""
    exists = lambda name: os.path.exists(os.path.join(result_dir, name))
    reads, first = [], clock()
    while len(reads) < max_reads and clock() - first <= max_sec:
        if exists(BEFORE_INCIDENT):
            break                                   # never a new read once the runner's T0 snapshot exists
        if reads and exists(PODS_BEFORE):
            break                                   # the runner is going to T0: stop well before it
        r = read_etcd(etcd_url, account)
        r["overlap"] = exists(BEFORE_INCIDENT)      # the runner's pre-T0 snapshot appeared during the read
        reads.append(r)
        sleep(max(0.0, first + len(reads) * period - clock()))
    return reads


def runner_snapshot(result_dir):
    """run_s3.sh's metrics-before-incident.json as the L0/L1/L2 start edge. `t` is its own
    timestamp, taken after the scrape AND the parse: an upper bound of the read instant."""
    d = json.load(open(os.path.join(result_dir, BEFORE_INCIDENT)))
    l0 = d["counters"].get("apiserver_request_total") or {}
    l2_all = d["counters"].get("etcd_requests_total") or {}
    return {"t": d["timestamp_utc"], "L0": l0,
            "L1": {k: v for k, v in l0.items() if json.loads(k).get("verb", "").upper() in layers.MUTATING},
            "L2": {k: v for k, v in l2_all.items() if json.loads(k).get("operation") in ms.WRITE_OPERATIONS},
            "families": {"apiserver_request_total": bool(l0), "etcd_requests_total": bool(l2_all)}}
