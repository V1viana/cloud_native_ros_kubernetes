#!/usr/bin/env python3
"""The datastore of a scenario cluster (docs/ETCD_GATE_PREREGISTRATION.md): embedded etcd is
the R14 target backend; the check also recognises SQLite/Kine, the k3s default.

  check CLUSTER RESULT_DIR   synchronous and fail-closed, called by every scenario runner
                             right after creating or reusing its cluster, before any
                             workload or injection. Exit 0 only when the three signals of
                             embedded etcd are all present; otherwise 2 and the runner
                             stops. Writes RESULT_DIR/datastore.json and, with
                             DATASTORE_LOG set, appends one line there.
  watch OUT [--stop-file F]  independent cross-check: every k3d cluster seen, verified
                             once, recorded with its server container's creation time.

Signals (the C2 pilot): /var/lib/rancher/k3s/server/db has etcd/ and no state.db; the
server node carries node-role.kubernetes.io/etcd=true; etcd_server_version answers on
:2381 (needs --etcd-expose-metrics).
"""

import ipaddress
import json
import os
import subprocess
import sys
import time
import urllib.request


def _run(*cmd):
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=15).stdout
    except (subprocess.TimeoutExpired, OSError):
        return ""


def inspect_created(container):
    """(creation time or None, absent). absent is True ONLY when Docker explicitly answers
    that the container does not exist; a timeout, an unreachable daemon or any other error
    is (None, False): a gap, never an absence (Viviana)."""
    try:
        p = subprocess.run(["docker", "inspect", "-f", "{{.Created}}", container],
                           capture_output=True, text=True, timeout=15)
    except (subprocess.TimeoutExpired, OSError):
        return None, False
    if p.returncode == 0 and p.stdout.strip():
        return p.stdout.strip(), False
    # the installed Docker says "Error: no such object: <name>" (lower case), others
    # "No such container": matched without regard to case
    err = p.stderr.lower()
    return None, p.returncode != 0 and ("no such object" in err or "no such container" in err)


def signals(cluster):
    server = f"k3d-{cluster}-server-0"                  # k3d names the node as its container
    db = _run("docker", "exec", server, "ls", "/var/lib/rancher/k3s/server/db/").split()
    labels = _run("kubectl", "--context", f"k3d-{cluster}", "get", "node", server,
                  "-o", "jsonpath={.metadata.labels}")
    ip = _run("docker", "inspect", "-f", "{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}",
              server).strip()
    version, probe_error = None, None
    try:
        ipaddress.ip_address(ip)
    except ValueError:
        # e.g. docker answered "invalid IP" while a container went away (etcd gate on
        # 59a05ae: an uncaught InvalidURL stopped the watcher); a gap, never etcd
        probe_error = f"no valid IP for {server}: {ip!r}"
    if probe_error is None:
        try:
            text = urllib.request.urlopen(f"http://{ip}:2381/metrics", timeout=5).read().decode()
            version = next((line.split('"')[1] for line in text.splitlines()
                            if line.startswith("etcd_server_version{")), None)
        except Exception as exc:                        # any failure of the probe is a gap
            probe_error = f"{type(exc).__name__}: {exc}"[:200]
    created, absent = inspect_created(server)
    # "ready": the server node reports Ready=True. Before that a probe is PENDING (the API or
    # the node are still coming up) and counts neither way; a failed check on a ready
    # cluster is a failure (etcd gate judge, fail-closed).
    ready = _run("kubectl", "--context", f"k3d-{cluster}", "get", "node", server, "-o",
                 'jsonpath={.status.conditions[?(@.type=="Ready")].status}').strip() == "True"
    result = {"cluster": cluster, "server": server, "server_created": created, "server_absent": absent,
              "ready": ready, "db_dir": db,
              "etcd_role_label": '"node-role.kubernetes.io/etcd":"true"' in labels,
              "etcd_server_version": version, "probe_error": probe_error}
    result["control_plane_label"] = '"node-role.kubernetes.io/control-plane":"true"' in labels
    # "ready" = node Ready AND the control-plane role label (Viviana, after the D1 diagnosis):
    # on etcd, k3s set the etcd label before control-plane and master in 2 of 2 creations
    # (results/evidence/runs/ETCD_DIAGNOSIS.md); on 59a05ae s1-a the node was Ready 7 s before
    # its role labels, and that read must be PENDING, not a failed check
    result["node_ready"] = result["ready"]
    result["ready"] = bool(result["ready"] and result["control_plane_label"])
    result["is_etcd"] = bool("etcd" in db and "state.db" not in db and result["etcd_role_label"] and version)
    # SQLite/Kine: state.db, no etcd role label, nothing answering on :2381 (a k3s server
    # also keeps an empty etcd/ directory, seen in the D2 diagnosis)
    result["is_sqlite"] = bool("state.db" in db and not result["etcd_role_label"] and version is None
                               and probe_error is not None)
    result["backend"] = "etcd" if result["is_etcd"] else ("sqlite" if result["is_sqlite"] else "unknown")
    return result


def check(cluster, result_dir, attempts=20, delay=3.0, probe=signals, sleep=time.sleep, expected=None):
    """Retries while the API and the node label come up; fail-closed on anything else.
    `expected` is the decided backend: DATASTORE_BACKEND, else etcd (the R14 target; SQLite
    was withdrawn as the final choice on 2026-10-01)."""
    expected = expected or os.environ.get("DATASTORE_BACKEND", "etcd")
    started = time.time()
    result = {"cluster": cluster, "is_etcd": False, "backend": "unknown", "error": "no probe ran"}
    for n in range(1, attempts + 1):
        result = probe(cluster)
        if result.get("backend", "etcd" if result.get("is_etcd") else "unknown") == expected:
            break
        if n < attempts:
            sleep(delay)
    ok = result.get("backend", "etcd" if result.get("is_etcd") else "unknown") == expected
    result.update({"verified_at": time.time(), "started_at": started, "attempts": n,
                   "expected_backend": expected, "verified": ok})
    os.makedirs(result_dir, exist_ok=True)
    with open(os.path.join(result_dir, "datastore.json"), "w") as h:
        json.dump(result, h, indent=1)
    if os.environ.get("DATASTORE_LOG"):
        with open(os.environ["DATASTORE_LOG"], "a") as h:
            h.write(json.dumps({**result, "result_dir": result_dir}) + "\n")
    print((f"datastore: {expected} verified " if ok else f"datastore: NOT verified as {expected} ")
          + json.dumps(result), file=sys.stderr)
    return 0 if ok else 2


def watch(out, stop_file=None, period=5.0, probe=signals):
    done = set()
    with open(out, "a", buffering=1) as h:
        while not (stop_file and os.path.exists(stop_file)):
            listed = _run("k3d", "cluster", "list", "--no-headers")
            for name in [line.split()[0] for line in listed.splitlines() if line.strip()]:
                created, _ = inspect_created(f"k3d-{name}-server-0")
                if created and (name, created) in done:
                    continue
                try:
                    result = probe(name)                # carries server_absent (Docker's own answer)
                except Exception as exc:                # a gap; the watcher keeps sampling
                    result = {"cluster": name, "server_created": None, "server_absent": False,
                              "ready": False, "is_etcd": False,
                              "probe_error": f"{type(exc).__name__}: {exc}"[:200]}
                h.write(json.dumps({"t": time.time(), **result}) + "\n")
                if result.get("is_etcd"):
                    done.add((name, created))
            time.sleep(period)


def main(argv):
    if argv[0] == "check":
        return check(argv[1], argv[2])
    if argv[0] == "watch":
        stop = argv[argv.index("--stop-file") + 1] if "--stop-file" in argv else None
        return watch(argv[1], stop)
    raise SystemExit(__doc__)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
