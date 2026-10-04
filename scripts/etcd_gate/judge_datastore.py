#!/usr/bin/env python3
"""etcd gate (docs/ETCD_GATE_PREREGISTRATION.md): per step, was every cluster embedded etcd?

  judge_datastore.py STEPS DATASTORE_LOG WATCH OUT

Fail-closed (Viviana). Everything is judged INSIDE the step's window:
- the clusters to judge are all those named by the runner checks and by the watcher's
  reads in the window; a record without the server container's identity (creation time)
  is a gap;
- a read with ready=false is PENDING (API or node still coming up) and counts neither way;
- a read on a READY cluster that is not embedded etcd is a failure, even if a later read
  says etcd;
- each cluster needs, in the window, at least one independent watcher read that is ready
  and etcd, and at least one runner check, every runner check saying etcd;
- teardown exception (Viviana, tied to the INSTANCE, not the name): a watcher read without
  identity that found no server container (server_absent) is ignored only when the last
  identity observed for that name before it (watcher or runner) had already been read
  ready and etcd before the absence, and NO identity at all for that name -- not even the
  same container -- appears after it in the window, in the watcher or in the runner
  checks. server_absent itself is set only when Docker explicitly says the container does
  not exist; a timeout or any other Docker error is a gap. It is recorded as an ignored
  teardown read, never as a verification; without that chain it stays a gap.
VERIFIED only when all of this holds; anything else, including a step with no cluster,
is INCONCLUSIVE -- never VERIFIED for lack of data.
"""

import json
import sys


def read_jsonl(path):
    out = []
    try:
        with open(path, errors="replace") as h:
            for line in h:
                try:
                    out.append(json.loads(line))
                except ValueError:
                    pass
    except FileNotFoundError:
        pass
    return out


def judge_step(step, checks, watched):
    lo, hi = step["start"], step["end"]
    reads = [w for w in watched if lo <= w.get("t", -1) <= hi]
    runs = [c for c in checks if lo <= c.get("verified_at", -1) <= hi]
    problems, teardown = [], []
    for record in runs:
        if not record.get("server_created"):
            problems.append(f"{record.get('cluster')}: runner record without the server's identity (gap)")
    for record in reads:
        if record.get("server_created"):
            continue
        if record.get("server_absent") and teardown_chain(record, reads, runs):
            teardown.append({"cluster": record["cluster"], "t": record["t"],
                             "after_instance": teardown_chain(record, reads, runs)})
        else:
            problems.append(f"{record.get('cluster')}: watcher read without the server's identity (gap)")
    instances = sorted({(r["cluster"], r["server_created"]) for r in reads + runs if r.get("server_created")})
    for cluster, created in instances:
        mine = [r for r in reads if r["cluster"] == cluster and r.get("server_created") == created]
        if any(r.get("ready") and not r.get("is_etcd") for r in mine):
            problems.append(f"{cluster}@{created}: watcher check failed on a ready cluster")
        if not any(r.get("ready") and r.get("is_etcd") for r in mine):
            problems.append(f"{cluster}@{created}: no ready, etcd watcher read in the step")
        own = [c for c in runs if c["cluster"] == cluster and c.get("server_created") == created]
        if not own:
            problems.append(f"{cluster}@{created}: no runner check in the step")
        elif not all(c.get("is_etcd") for c in own):
            problems.append(f"{cluster}@{created}: a runner check failed")
    if not instances:
        problems.append("no cluster in the step's records")
    return {"step": step["step"], "verdict": "INCONCLUSIVE" if problems else "VERIFIED",
            "clusters": [f"{c}@{t}" for c, t in instances], "problems": problems,
            "teardown_reads_ignored": teardown}


def _when(record):
    return record.get("t", record.get("verified_at", -1))


def teardown_chain(absent, reads, runs):
    """The created-time of the instance whose teardown `absent` is, or None when the chain
    does not hold (see the module docstring)."""
    name, t = absent["cluster"], absent["t"]
    identified = [r for r in reads + runs if r.get("cluster") == name and r.get("server_created")]
    before = [r for r in identified if _when(r) < t]
    if not before:
        return None                                     # absent before any identity
    last = max(before, key=_when)["server_created"]
    verified = any(r.get("server_created") == last and r.get("ready") and r.get("is_etcd") and r["t"] < t
                   for r in reads if r.get("cluster") == name)
    if not verified:
        return None                                     # absence before the verification
    if any(_when(r) > t for r in identified):
        return None                                     # any identity after the absence: the container
        #                                                 came back or the name was reused -- a gap
    return last


def judge(steps, checks, watched):
    return [judge_step(step, checks, watched) for step in steps]


def main(argv):
    steps, checks, watched, out = (read_jsonl(argv[0]), read_jsonl(argv[1]), read_jsonl(argv[2]), argv[3])
    results = judge([s for s in steps if "end" in s], checks, watched)
    with open(out, "w") as h:
        json.dump(results, h, indent=1)
    for r in results:
        print(json.dumps({"step": r["step"], "verdict": r["verdict"]}))
    return 0 if results and all(r["verdict"] == "VERIFIED" for r in results) else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
