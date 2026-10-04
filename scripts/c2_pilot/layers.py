#!/usr/bin/env python3
"""C2 pilot (docs/C2_ETCD_PILOT_PREREGISTRATION.md): write traffic in three layers.

  verify   CONTAINER CONTEXT OUT       evidence that the active datastore is embedded etcd
  etcd     URL OUT                     one scrape of etcd's own counters (:2381/metrics)
  report   S3_DIFF ETCD_BEFORE ETCD_AFTER OUT
           the three layers over one window:
             L1 API writes            apiserver_request_total, mutating verbs
             L2 storage requests      etcd_requests_total, write operations -- what S3 calls
                                      "scritture storage" (s3_metrics.py diff)
             L3 datastore writes      etcd_mvcc_put/delete/txn_total (logical), WAL fsyncs
                                      and backend commits (physical)
L3 exists only on etcd: with SQLite/Kine there are no etcd counters, which is why the two
backends' results are never merged.
"""

import json
import re
import subprocess
import sys
import urllib.request

ETCD = ("etcd_mvcc_put_total", "etcd_mvcc_delete_total", "etcd_mvcc_txn_total",
        "etcd_disk_wal_fsync_duration_seconds_count", "etcd_disk_backend_commit_duration_seconds_count")
MUTATING = {"POST", "PUT", "PATCH", "DELETE", "APPLY", "DELETECOLLECTION"}


def counters(text, names):
    out = {n: 0.0 for n in names}
    for line in text.splitlines():
        m = re.match(r"^([a-zA-Z_:]+)(?:\{[^}]*\})?\s+([0-9.eE+-]+)$", line)
        if m and m.group(1) in out:
            out[m.group(1)] += float(m.group(2))
    return out


def version(text):
    m = re.search(r'etcd_server_version\{server_version="([^"]+)"\}', text)
    return m.group(1) if m else None


def node_name(container):
    """k3d names each Kubernetes node exactly as its container (k3d-<cluster>-server-0): the
    first pilot attempt stripped the k3d- prefix, found no node and read no etcd label."""
    return container


def verify(container, context, out):
    db = subprocess.run(["docker", "exec", container, "ls", "/var/lib/rancher/k3s/server/db/"],
                        capture_output=True, text=True).stdout.split()
    labels = subprocess.run(["kubectl", "--context", context, "get", "node", node_name(container),
                             "-o", "jsonpath={.metadata.labels}"], capture_output=True, text=True).stdout
    ip = subprocess.run(["docker", "inspect", "-f", "{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}",
                         container], capture_output=True, text=True).stdout.strip()
    try:
        text = urllib.request.urlopen(f"http://{ip}:2381/metrics", timeout=5).read().decode()
    except OSError as exc:
        text = f"unreachable: {exc}"
    result = {"db_dir": db, "etcd_role_label": '"node-role.kubernetes.io/etcd":"true"' in labels,
              "etcd_server_version": version(text), "metrics_url": f"http://{ip}:2381/metrics"}
    result["is_etcd"] = bool("etcd" in db and "state.db" not in db and result["etcd_role_label"]
                             and result["etcd_server_version"])
    with open(out, "w") as h:
        json.dump(result, h, indent=1)
    print(json.dumps(result))
    return 0 if result["is_etcd"] else 2


def etcd(url, out):
    text = urllib.request.urlopen(url, timeout=10).read().decode()
    with open(out, "w") as h:
        json.dump({"counters": counters(text, ETCD), "version": version(text)}, h, indent=1)
    return 0


def labelled(deltas, metric):
    """{JSON label string: delta} from s3_metrics.py diff's deltas."""
    return deltas.get(metric, {}) if isinstance(deltas.get(metric), dict) else {}


def report(s3_diff, before, after, out):
    with open(s3_diff) as h:
        s3 = json.load(h)
    with open(before) as h:
        b = json.load(h)["counters"]
    with open(after) as h:
        a = json.load(h)["counters"]
    api = labelled(s3.get("deltas", {}), "apiserver_request_total")
    l1 = sum(v for k, v in api.items() if json.loads(k).get("verb") in MUTATING)   # keys: JSON labels
    l3 = {k: a[k] - b[k] for k in ETCD}
    result = {
        "window_sec": s3.get("elapsed_sec"),
        "L1_api_mutating_requests": l1,
        "L2_storage_write_requests": s3.get("etcd_requests_write_total_delta"),
        "L2_by_operation": s3.get("etcd_requests_by_operation"),
        "L3_datastore_logical_writes": l3["etcd_mvcc_put_total"] + l3["etcd_mvcc_delete_total"],
        "L3_detail": l3,
        "L3_physical_wal_fsyncs": l3["etcd_disk_wal_fsync_duration_seconds_count"],
        "L3_physical_backend_commits": l3["etcd_disk_backend_commit_duration_seconds_count"],
    }
    with open(out, "w") as h:
        json.dump(result, h, indent=1)
    print(json.dumps({k: v for k, v in result.items() if k != "L2_by_operation"}))
    return 0


def main(argv):
    cmd, args = argv[0], argv[1:]
    return {"verify": verify, "etcd": etcd, "report": report}[cmd](*args)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
