#!/usr/bin/env python3
"""Continuous control-plane counter sampler (docs/R14_PREREGISTRATION_DRAFT.md 6.1 and 6.2), for the
cost per incident and for S3's A/B load with the SAME window. Started from outside the runner
(the campaign wrapper); no runner is modified.

  metrics_sampler.py sample CONTEXT NAMESPACE OUT.jsonl STOP_FILE [ETCD_METRICS_URL]
      one read attempt per second, one JSON line per attempt
  metrics_sampler.py sample-http PROXY_URL NAMESPACE OUT.jsonl STOP_FILE ACCOUNT.jsonl [ETCD_METRICS_URL]
      second S3 sampler pilot (docs/S3_SAMPLER_PILOT_2_PREREGISTRATION.md): the API server
      read through ONE persistent `kubectl proxy` instead of three kubectl processes per
      read; metrics and etcd at 1 Hz, the workload check every WORKLOAD_EVERY reads; every
      request the sampler sends is accounted in ACCOUNT.jsonl (method, path, code, bytes,
      times, counted or not in apiserver_request_total)
  metrics_sampler.py window OUT.jsonl T_FROM T_TO
      the deltas over [T_FROM, T_TO] (wall clock), or "not measurable" with the reasons

A line: {"seq", "t_start", "t", "error", "scrape": {"start", "end", "duration_s"},
"apiserver_start", "L1": {series: value}, "L2": {series: value}, "L3": {counter: value} or
null, "workload": {"namespace_present", "deployments"}}. Cumulative counters, as read, ONE
ENTRY PER SERIES (the full label set as sorted JSON): a series that goes down or disappears
is never masked by another that grows (review of Viviana, 1 October).
- L1: apiserver_request_total series with a mutating verb (per resource, verb, code, ...).
  The sampler's own reads are GETs and never count.
- L2: etcd_requests_total series with a write operation (create/update/delete/patch).
- L3: etcd's own counters on :2381 (only with embedded etcd; null otherwise).
- L0 (sample-http only): EVERY apiserver_request_total series, all verbs (proposal: API
  calls/s); L1 stays the write detail. GETs to /metrics are not counted there
  (results/evidence/runs/METRICS_GET_PROBE.md).
- Content bytes (sample-http only, descriptive, never summed into a "total bandwidth"):
  request bodies, responses, WATCH events, judged apart; WATCH counters are raw data only
  (results/evidence/runs/WATCH_PROBE.md: NOT_DETERMINED).
- The window edges use the TIME OF THE METRICS SCRAPE ("scrape" start and end, duration
  recorded), not the end of the whole read: the kubectl calls that follow can be slow.

A window [T_FROM, T_TO] is MEASURABLE only if (Viviana, 1 October) -- otherwise it is "not
measurable", never shortened nor interpolated:
- edges on the metrics scrape: the start edge is the last read whose scrape ENDED by T_FROM,
  the end edge the first read whose scrape STARTED at or after T_TO, each within GAP_SEC;
  the measured span therefore contains [T_FROM, T_TO] and its excess is reported
  (start/end uncertainty); no two consecutive successful scrapes further apart than GAP_SEC
  in between (failed reads are gaps);
- no counter reset: the API server's start time unchanged, and NO SINGLE SERIES going down
  or disappearing between consecutive reads (a series born inside counts from zero);
- no full teardown: the namespace present with at least one Deployment in every read (in
  every workload check, at most WORKLOAD_MAX_SPACING_SEC apart, with sample-http: a short
  teardown between two checks can escape). This says nothing about the target workload's
  health;
- every required metric family present in every read: an ABSENT metric is not measurable,
  never a zero (zero is valid only when the metric is present and does not grow);
- L3 is judged apart, on etcd's own read times (read after the API server), with the same
  rules, and reported separately: it is not "the same window" as L1/L2.
L is fixed in the preregistration before any data; this tool only applies it.
"""

import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "c2_pilot"))
import s3_metrics  # noqa: E402
import layers  # noqa: E402

PERIOD_SEC = 1.0
GAP_SEC = 2.0
WRITE_OPERATIONS = {"create", "update", "delete", "patch"}
WORKLOAD_EVERY = 5                               # sample-http: one workload check every 5 reads
WORKLOAD_MAX_SPACING_SEC = 5.0 + GAP_SEC         # 5 s nominal + the gap tolerance
BYTES_FAMILIES = {
    "request_body": ("apiserver_request_body_size_bytes_sum", "apiserver_request_body_size_bytes_count"),
    "response": ("apiserver_response_sizes_sum", "apiserver_response_sizes_count"),
    "watch_events": ("apiserver_watch_events_sizes_sum", "apiserver_watch_events_sizes_count",
                     "apiserver_watch_events_total"),
}


def read_once(context, namespace, etcd_url=None):
    scrape_start = time.time()
    raw = s3_metrics.scrape(context)
    scrape_end = time.time()
    parsed = s3_metrics.parse_counters(raw, ["apiserver_request_total", "etcd_requests_total",
                                             "process_start_time_seconds"])
    api_all = sum(parsed["apiserver_request_total"].values())   # every verb: the observer-load denominator
    l1 = {key: value for key, value in parsed["apiserver_request_total"].items()
          if json.loads(key).get("verb", "").upper() in layers.MUTATING}
    l2 = {key: value for key, value in parsed["etcd_requests_total"].items()
          if json.loads(key).get("operation") in WRITE_OPERATIONS}
    start = next(iter(parsed["process_start_time_seconds"].values()), None)
    families = {name: bool(parsed[name]) for name in ("apiserver_request_total", "etcd_requests_total",
                                                       "process_start_time_seconds")}
    l3, etcd_scrape = None, None
    if etcd_url:
        etcd_start = time.time()
        text = urllib.request.urlopen(etcd_url, timeout=0.8).read().decode()
        etcd_end = time.time()
        etcd_scrape = {"start": etcd_start, "end": etcd_end, "duration_s": round(etcd_end - etcd_start, 3)}
        present = {name for name in layers.ETCD if any(line.startswith(name) for line in text.splitlines())}
        l3 = {k: v for k, v in layers.counters(text, layers.ETCD).items() if k in present}
        families["etcd_l3"] = present == set(layers.ETCD)
    out = subprocess.run(["kubectl", "--context", context, "get", "deployments", "-n", namespace, "-o", "name"],
                         capture_output=True, text=True, timeout=3)
    if out.returncode and "NotFound" not in out.stderr and "not found" not in out.stderr:
        raise RuntimeError(out.stderr.strip()[:200])
    namespace_present = subprocess.run(["kubectl", "--context", context, "get", "namespace", namespace],
                                       capture_output=True, text=True, timeout=3).returncode == 0
    deployments = len([line for line in out.stdout.splitlines() if line.strip()])
    return {"scrape": {"start": scrape_start, "end": scrape_end, "duration_s": round(scrape_end - scrape_start, 3)},
            "etcd_scrape": etcd_scrape, "families": families,
            "apiserver_start": start, "api_requests_all": api_all, "L1": l1, "L2": l2, "L3": l3,
            "workload": {"namespace_present": namespace_present, "deployments": deployments}}


def http_get(url, account, source, counted, timeout=3.0):
    """One GET, accounted on the client side: the requests SENT, not necessarily all those
    the server handled (hence the cross-check of the second pilot)."""
    entry = {"method": "GET", "url": url, "source": source, "counted": counted, "t_start": time.time()}
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            body, entry["code"] = resp.read(), resp.status
    except urllib.error.HTTPError as exc:
        body, entry["code"] = exc.read(), exc.code
    except Exception as exc:
        entry.update({"code": None, "error": repr(exc), "bytes": 0, "t_end": time.time()})
        account.append(entry)
        raise
    entry.update({"bytes": len(body), "t_end": time.time()})
    account.append(entry)
    return entry["code"], body


def read_once_http(proxy_url, namespace, etcd_url=None, check_workload=True, account=None):
    """read_once through a persistent `kubectl proxy`: /metrics (not counted in
    apiserver_request_total), etcd :2381 (not through the API server), and -- only when
    check_workload -- the namespace GET and the Deployment LIST (both counted)."""
    account = [] if account is None else account
    scrape_start = time.time()
    code, raw = http_get(proxy_url + "/metrics", account, "apiserver", counted=False, timeout=5.0)
    scrape_end = time.time()
    if code != 200:
        raise RuntimeError(f"/metrics: HTTP {code}")
    raw = raw.decode()
    byte_names = [n for names in BYTES_FAMILIES.values() for n in names]
    parsed = s3_metrics.parse_counters(raw, ["apiserver_request_total", "etcd_requests_total",
                                             "process_start_time_seconds", *byte_names])
    l0 = dict(parsed["apiserver_request_total"])
    l1 = {key: value for key, value in l0.items() if json.loads(key).get("verb", "").upper() in layers.MUTATING}
    l2 = {key: value for key, value in parsed["etcd_requests_total"].items()
          if json.loads(key).get("operation") in WRITE_OPERATIONS}
    start = next(iter(parsed["process_start_time_seconds"].values()), None)
    families = {name: bool(parsed[name]) for name in ("apiserver_request_total", "etcd_requests_total",
                                                       "process_start_time_seconds")}
    families.update({f"bytes:{group}": all(parsed[n] for n in names) for group, names in BYTES_FAMILIES.items()})
    l3, etcd_scrape = None, None
    if etcd_url:
        etcd_start = time.time()
        code, text = http_get(etcd_url, account, "etcd", counted=False, timeout=0.8)
        etcd_end = time.time()
        if code != 200:
            raise RuntimeError(f"etcd metrics: HTTP {code}")
        text = text.decode()
        etcd_scrape = {"start": etcd_start, "end": etcd_end, "duration_s": round(etcd_end - etcd_start, 3)}
        present = {name for name in layers.ETCD if any(line.startswith(name) for line in text.splitlines())}
        l3 = {k: v for k, v in layers.counters(text, layers.ETCD).items() if k in present}
        families["etcd_l3"] = present == set(layers.ETCD)
    workload = None
    if check_workload:
        code, _ = http_get(f"{proxy_url}/api/v1/namespaces/{namespace}", account, "apiserver", counted=True)
        if code not in (200, 404):
            raise RuntimeError(f"namespace GET: HTTP {code}")
        namespace_present = code == 200
        code, body = http_get(f"{proxy_url}/apis/apps/v1/namespaces/{namespace}/deployments", account,
                              "apiserver", counted=True)
        if code not in (200, 404):
            raise RuntimeError(f"Deployment LIST: HTTP {code}")
        deployments = len(json.loads(body).get("items", [])) if code == 200 else 0
        workload = {"namespace_present": namespace_present, "deployments": deployments}
    return {"scrape": {"start": scrape_start, "end": scrape_end, "duration_s": round(scrape_end - scrape_start, 3)},
            "etcd_scrape": etcd_scrape, "families": families,
            "apiserver_start": start, "api_requests_all": sum(l0.values()), "L0": l0, "L1": l1, "L2": l2, "L3": l3,
            "bytes": {group: {n: parsed[n] for n in names} for group, names in BYTES_FAMILIES.items()},
            "workload": workload}


def sample_http(proxy_url, namespace, out_path, stop_file, account_path, etcd_url=None,
                workload_every=WORKLOAD_EVERY):
    """One read per second through the proxy; the workload check on reads 0, 5, 10, ...;
    every request sent goes to account_path (one JSON line each, with the read's seq)."""
    start = time.monotonic()
    seq = 0
    with open(out_path, "a") as out, open(account_path, "a") as acc:
        while not os.path.exists(stop_file):
            line = {"seq": seq, "t_start": time.time(), "error": None}
            account = []
            try:
                line.update(read_once_http(proxy_url, namespace, etcd_url, seq % workload_every == 0, account))
            except Exception as exc:   # a failed read is a gap, recorded, never fatal
                line["error"] = repr(exc)
            line["t"] = time.time()
            out.write(json.dumps(line) + "\n")
            out.flush()
            for entry in account:
                acc.write(json.dumps({"seq": seq, **entry}) + "\n")
            acc.flush()
            seq += 1
            if start + seq * PERIOD_SEC < time.monotonic():
                start = time.monotonic() - seq * PERIOD_SEC
            time.sleep(max(0.0, start + seq * PERIOD_SEC - time.monotonic()))


def sample(context, namespace, out_path, stop_file, etcd_url=None):
    start = time.monotonic()
    seq = 0
    with open(out_path, "a") as out:
        while not os.path.exists(stop_file):
            line = {"seq": seq, "t_start": time.time(), "error": None}
            try:
                line.update(read_once(context, namespace, etcd_url))
            except Exception as exc:   # a failed read is a gap, recorded, never fatal
                line["error"] = repr(exc)
            line["t"] = time.time()
            out.write(json.dumps(line) + "\n")
            out.flush()
            seq += 1
            if start + seq * PERIOD_SEC < time.monotonic():
                start = time.monotonic() - seq * PERIOD_SEC
            time.sleep(max(0.0, start + seq * PERIOD_SEC - time.monotonic()))


REQUIRED_API_FAMILIES = ("apiserver_request_total", "etcd_requests_total")


def _api_series(sample):
    out = {f"L1|{k}": v for k, v in sample["L1"].items()}
    out.update({f"L2|{k}": v for k, v in sample["L2"].items()})
    out.update({f"L0|{k}": v for k, v in (sample.get("L0") or {}).items()})   # sample-http only
    return out


def _etcd_series(sample):
    return {f"L3|{k}": v for k, v in (sample.get("L3") or {}).items()}


def _group(series_deltas, layer, label):
    out = {}
    for key, delta in series_deltas.items():
        name, labels = key.split("|", 1)
        if name == layer:
            group = json.loads(labels).get(label, "") or "(none)"
            out[group] = out.get(group, 0.0) + delta
    return dict(sorted(out.items()))


def _api_at(sample):
    """When the API server's counters were read: the metrics scrape (start, end)."""
    scrape = sample.get("scrape") or {"start": sample["t"], "end": sample["t"]}
    return scrape["start"], scrape["end"]


def _etcd_at(sample):
    """When etcd's counters were read: their own scrape, after the API server's."""
    scrape = sample.get("etcd_scrape")
    return (scrape["start"], scrape["end"]) if scrape else None


def _edges_and_checks(reads, t_from, t_to, gap_sec, at, series, label):
    """Common rules: edges on the scrape, gaps, series that go down or disappear."""
    reasons = []
    reads = sorted(reads, key=lambda s: at(s)[0])
    before = [s for s in reads if at(s)[1] <= t_from]       # scrape finished before the window opened
    after = [s for s in reads if at(s)[0] >= t_to]          # scrape started after the window closed
    if not before or t_from - at(before[-1])[0] > gap_sec:
        reasons.append(f"{label}: no read within {gap_sec}s before the window start")
    if not after or at(after[0])[1] - t_to > gap_sec:
        reasons.append(f"{label}: no read within {gap_sec}s after the window end (cluster or sampler gone?)")
    if reasons:
        return None, None, [], reasons
    first, last = before[-1], after[0]
    inside = [s for s in reads if at(first)[0] <= at(s)[0] <= at(last)[0]]
    for a, b in zip(inside, inside[1:]):
        if at(b)[0] - at(a)[1] > gap_sec:
            reasons.append(f"{label}: gap of {at(b)[0] - at(a)[1]:.1f}s between reads at {at(a)[1]:.1f} "
                           f"and {at(b)[0]:.1f}")
        sa, sb = series(a), series(b)
        gone = sorted(k for k in sa if k not in sb)
        dropped = sorted(k for k in sa if k in sb and sb[k] < sa[k])
        if gone:
            reasons.append(f"{label}: series disappeared between {at(a)[1]:.1f} and {at(b)[0]:.1f}: {gone[:5]}")
        if dropped:
            reasons.append(f"{label}: series went down between {at(a)[1]:.1f} and {at(b)[0]:.1f}: {dropped[:5]}")
    return first, last, inside, reasons


def _deltas(first, last, series):
    sf, sl = series(first), series(last)
    return {k: sl[k] - sf.get(k, 0.0) for k in sl}     # a series born inside counts from zero


def window(samples, t_from, t_to, gap_sec=GAP_SEC):
    ok = [s for s in samples if not s.get("error")]
    errors = [s for s in samples if s.get("error") and t_from - gap_sec <= s["t"] <= t_to + gap_sec]
    first, last, inside, reasons = _edges_and_checks(ok, t_from, t_to, gap_sec, _api_at, _api_series, "API")
    if first is None:
        return {"measurable": False, "reasons": reasons, "read_errors": len(errors), "L3": None}
    for a, b in zip(inside, inside[1:]):
        if b["apiserver_start"] != a["apiserver_start"]:
            reasons.append(f"API server restarted between {_api_at(a)[1]:.1f} and {_api_at(b)[0]:.1f}: counters reset")
    for s in inside:
        families = s.get("families") or {}
        missing = [f for f in REQUIRED_API_FAMILIES if not families.get(f)]
        if missing:   # an absent metric is not a zero
            reasons.append(f"metric family absent at {_api_at(s)[0]:.1f}: {missing}")
            break
    every_read = all("workload" in s and s["workload"] is not None for s in inside)
    checks = [s for s in inside if s.get("workload") is not None]
    for s in checks:
        w = s["workload"]
        if not w.get("namespace_present") or not w.get("deployments"):
            reasons.append(f"full teardown observed at {s['t']:.1f} (namespace or every Deployment gone)")
            break
    if not every_read:   # sample-http: sparse checks, at most WORKLOAD_MAX_SPACING_SEC apart, edges included
        times = [_api_at(first)[0]] + [s["t"] for s in checks] + [_api_at(last)[1]]
        if not checks:
            reasons.append("no workload check inside the window")
        for a, b in zip(times, times[1:]):
            if b - a > WORKLOAD_MAX_SPACING_SEC:
                reasons.append(f"workload checks {b - a:.1f}s apart (limit {WORKLOAD_MAX_SPACING_SEC}s) "
                               f"between {a:.1f} and {b:.1f}")
                break
    no_start_time = any(not (s.get("families") or {}).get("process_start_time_seconds") for s in inside)
    result = {"measurable": not reasons, "reasons": reasons, "read_errors": len(errors),
              "reset_detection": ("series only (API server start time not exposed)" if no_start_time
                                  else "API server start time and series"),
              "workload_check": ("namespace present with at least one Deployment in every read: no full "
                                 "teardown -- says nothing about the target workload's health" if every_read else
                                 f"namespace present with at least one Deployment in every workload check, "
                                 f"checks at most {WORKLOAD_MAX_SPACING_SEC}s apart: no full teardown seen -- a "
                                 f"short teardown between two checks can escape; says nothing about the target "
                                 f"workload's health"),
              "L3": _l3_window(ok, t_from, t_to, gap_sec)}
    if reasons:
        return result
    deltas = _deltas(first, last, _api_series)
    result.update({
        "edges": {"start_scrape": list(_api_at(first)), "end_scrape": list(_api_at(last)),
                  "start_uncertainty_s": round(t_from - _api_at(first)[0], 3),
                  "end_uncertainty_s": round(_api_at(last)[1] - t_to, 3)},
        "deltas": {layer: sum(d for k, d in deltas.items() if k.startswith(layer + "|"))
                   for layer in ("L0", "L1", "L2") if layer != "L0" or any(k.startswith("L0|") for k in deltas)},
        "L1_by_resource": _group(deltas, "L1", "resource"), "L2_by_type": _group(deltas, "L2", "type"),
        # observer load (an ESTIMATE): all API requests over the same window, and the sampler's
        # own read attempts in it (3 requests each: /metrics, Deployments, namespace; kubectl
        # discovery calls, if any, are not counted)
        "api_requests_all_verbs": (last.get("api_requests_all") - first.get("api_requests_all")
                                   if last.get("api_requests_all") is not None
                                   and first.get("api_requests_all") is not None else None),
        "sampler_attempts_in_window": sum(1 for s in samples if t_from <= s.get("t_start", -1) <= t_to)})
    if any(k.startswith("L0|") for k in deltas):
        result.update({"L0_by_verb": _group(deltas, "L0", "verb"), "L0_by_resource": _group(deltas, "L0", "resource"),
                       "series_deltas": {k: d for k, d in deltas.items() if k.startswith("L0|")},
                       "bytes": bytes_window(first, last, inside)})
    return result


def bytes_window(first, last, inside):
    """Content bytes, descriptive and judged APART (never part of "measurable"): per family
    group, the per-series deltas between the edge reads, not measurable if a series goes down
    or disappears or the family is absent. Never summed across groups: request, response and
    WATCH events can overlap and exclude network overhead. WATCH counters: raw data only."""
    out = {}
    for group in BYTES_FAMILIES:
        reasons = []
        if not all((s.get("families") or {}).get(f"bytes:{group}") for s in inside):
            reasons.append(f"{group}: family absent in a read")
        series = lambda s: {f"{n}|{k}": v for n, values in ((s.get("bytes") or {}).get(group) or {}).items()
                            for k, v in values.items()}
        for a, b in zip(inside, inside[1:]):
            sa, sb = series(a), series(b)
            if any(k not in sb or sb[k] < sa[k] for k in sa):
                reasons.append(f"{group}: a series went down or disappeared between {_api_at(a)[1]:.1f} "
                               f"and {_api_at(b)[0]:.1f}")
                break
        entry = {"measurable": not reasons, "reasons": reasons}
        if not reasons:
            entry["series_deltas"] = _deltas(first, last, series)
            entry["totals"] = {n: sum(d for k, d in entry["series_deltas"].items() if k.split("|", 1)[0] == n)
                               for n in BYTES_FAMILIES[group]}
        if group == "watch_events" or group == "response":
            entry["watch"] = "raw data only: WATCH counters NOT_DETERMINED (WATCH_PROBE.md), no A/B conclusion"
        out[group] = entry
    return out


def _l3_window(ok, t_from, t_to, gap_sec):
    """L3 on etcd's OWN read times, reported apart: it is read after the API server, so its
    window is not the API server's (review of Viviana, 1 October). None without etcd."""
    reads = [s for s in ok if s.get("L3") is not None and _etcd_at(s)]
    if not any(s.get("L3") is not None for s in ok):
        return None
    first, last, inside, reasons = _edges_and_checks(reads, t_from, t_to, gap_sec, _etcd_at, _etcd_series, "L3")
    if first is not None:
        for s in inside:
            if not (s.get("families") or {}).get("etcd_l3"):
                reasons.append(f"L3: etcd counters absent at {_etcd_at(s)[0]:.1f}")
                break
    if reasons:
        return {"measurable": False, "reasons": reasons}
    deltas = _deltas(first, last, _etcd_series)
    return {"measurable": True, "reasons": [],
            "edges": {"start_scrape": list(_etcd_at(first)), "end_scrape": list(_etcd_at(last)),
                      "start_uncertainty_s": round(t_from - _etcd_at(first)[0], 3),
                      "end_uncertainty_s": round(_etcd_at(last)[1] - t_to, 3)},
            "deltas": {k.split("|", 1)[1]: d for k, d in deltas.items()}}


def load(path):
    out = []
    for line in open(path):
        try:
            out.append(json.loads(line))
        except ValueError:
            pass
    return out


def main(argv):
    if argv[0] == "sample":
        sample(*argv[1:5], argv[5] if len(argv) > 5 else None)
        return 0
    if argv[0] == "sample-http":
        sample_http(argv[1], argv[2], argv[3], argv[4], argv[5], argv[6] if len(argv) > 6 else None)
        return 0
    if argv[0] == "window":
        print(json.dumps(window(load(argv[1]), float(argv[2]), float(argv[3])), indent=1))
        return 0
    print(__doc__, file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
