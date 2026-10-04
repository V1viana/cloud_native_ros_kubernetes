#!/usr/bin/env python3
"""Wrapper of the SECOND S3 sampler pilot (docs/S3_SAMPLER_PILOT_2_PREREGISTRATION.md),
versioned with the frozen revision (pilot 1's wrapper lived in a scratchpad).

  s3_sampler_pilot2.py --results DIR --rev REV          the 12 runs of the order table
  s3_sampler_pilot2.py --plan                           print the plan (order, timings) and exit

Per run (row of s3_sampler_pilot2_eval.ORDER), on a fresh cluster:
 1. run_s3.sh (RESET_S3=1, N_ROBOTS=3, VARIANT) in its own session; never under `timeout`;
 2. observer ON during the incident: as soon as the API answers, ONE `kubectl proxy` and the
    sampler (metrics_sampler.py sample-http); a successful read must reach samples.jsonl
    within 30 s, otherwise runner, sampler and cluster are stopped, the run is INCONCLUSIVE
    (orchestration) and the campaign stops. Observer OFF: nothing reads the API server;
 3. after the runner: the cluster held, untouched, to T0 + 185 s; the sampler stopped and the
    CPU of every observer process recorded (sampler with its children, proxy);
 4. settle 60 s, then 4 blocks of 60 s in the run's order (SAAS or ASSA): one edge read at
    each block edge (/metrics through the proxy, not counted; etcd :2381; the server's cgroup
    from the host), memory sampled each second from the host, and in A blocks the sampler
    running throughout (started OBSERVER_LEAD_SEC before the first edge);
 5. images compared with the frozen IDs, cluster deleted and verified, worktree clean.
Stop rule: a run that is not OK or a valid functional FAIL stops the campaign (Viviana decides).
"""

import argparse
import hashlib
import json
import os
import shutil
import signal
import subprocess
import sys
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)
import metrics_sampler as ms  # noqa: E402
import s3_sampler_pilot2_eval as ev  # noqa: E402

CLUSTER, CONTEXT, NAMESPACE = "cloud-native-s3", "k3d-cloud-native-s3", "cloud-native-s3"
SERVER = f"k3d-{CLUSTER}-server-0"
TIMING = {"hold_after_t0_sec": 185.0, "settle_sec": 60.0, "block_sec": 60.0, "check_sec": 30.0,
          "runner_limit_sec": 5400.0, "observer_lead_sec": 1.0, "proxy_port": 18002, "etcd_port": 2381}
BUILDS = [("cloud-native-ros/control-plane:p2", "containers/control-plane/Dockerfile"),
          ("cloud-native-ros/event-detector:p2", "containers/event-detector/Dockerfile"),
          ("cloud-native-ros/kuberos:p2", "containers/kuberos/Dockerfile"),
          ("cloud-native-ros/fleet-operator:p2", "containers/fleet-operator/Dockerfile"),
          ("cloud-native-ros/state-bridge:p2", "containers/state-bridge/Dockerfile")]
THIRD_PARTY = ["microros/micro-ros-agent:humble", "px4io/px4-sitl:latest"]
CGROUP_ROOT = "/sys/fs/cgroup/system.slice"
RUNNER = "scripts/run_s3.sh"
HASHED = ["scripts/s3_sampler_pilot2.py", "scripts/metrics_sampler.py", "scripts/s3_sampler_pilot2_eval.py",
          "scripts/r14_stats.py", "scripts/s3_metrics.py", "scripts/c2_pilot/layers.py", "scripts/run_s3.sh"]


class Stop(Exception):
    """The campaign stops here (stop rule); the message is the run's status."""


def run(cmd, timeout=60, **kw):
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, **kw)


def sha256(path):
    return hashlib.sha256(open(path, "rb").read()).hexdigest()


class Campaign:
    def __init__(self, results, rev, timing):
        self.results, self.rev, self.timing = results, rev, timing
        self.status = open(os.path.join(results, "status.txt"), "a")
        self.proxy_url = f"http://127.0.0.1:{timing['proxy_port']}"

    def say(self, msg):
        line = f"{msg} {time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())}"
        print(line, flush=True)
        self.status.write(line + "\n")
        self.status.flush()

    # ---- bench ----
    def clusters(self):
        """Fail-closed: an error of `k3d cluster list` is never an empty list."""
        try:
            p = run(["k3d", "cluster", "list", "--no-headers"])
        except subprocess.TimeoutExpired:
            raise Stop("INCONCLUSIVE: k3d cluster list timed out")
        if p.returncode != 0:
            raise Stop(f"INCONCLUSIVE: k3d cluster list failed (rc {p.returncode}): {p.stderr.strip()[:200]}")
        return [line.split()[0] for line in p.stdout.splitlines() if line.strip()]

    @staticmethod
    def worktree_dirty():
        """Untracked files INCLUDED (pilot 1's first attempt was invalidated by one); ignored
        files (results/, __pycache__) excluded. Fail-closed: a git error counts as dirty."""
        p = run(["git", "status", "--porcelain", "--untracked-files=all"], cwd=ROOT)
        return p.stdout.strip() if p.returncode == 0 else f"git status failed (rc {p.returncode})"

    def image_ids(self):
        out = {}
        for tag in [t for t, _ in BUILDS] + THIRD_PARTY:
            p = run(["docker", "image", "inspect", "-f", "{{.Id}}", tag])
            out[tag] = p.stdout.strip() if p.returncode == 0 else None
        return out

    def preflight(self):
        if self.clusters():
            raise Stop("NOT_STARTED: a k3d cluster exists")
        head = run(["git", "rev-parse", "--short", "HEAD"], cwd=ROOT).stdout.strip()
        dirty = self.worktree_dirty()
        if dirty or head != self.rev:
            raise Stop(f"NOT_STARTED: worktree not clean or not at {self.rev} (HEAD {head}; status: {dirty[:200]})")
        subs = run(["git", "submodule", "status"], cwd=ROOT).stdout
        open(os.path.join(self.results, "submodules.txt"), "w").write(subs)
        if not subs.strip() or any(line[:1] in "-+U" for line in subs.splitlines()):
            raise Stop("NOT_STARTED: submodules not initialised at the revision")
        log = open(os.path.join(self.results, "image-build.log"), "w")
        for tag, dockerfile in BUILDS:      # built ONCE; run_s3.sh rebuilds with the same cache
            p = run(["docker", "build", "-q", "-t", tag, "-f", dockerfile, "."], timeout=3600, cwd=ROOT)
            log.write(f"{tag} rc={p.returncode} {p.stdout.strip()} {p.stderr[-500:]}\n")
            if p.returncode:
                raise Stop(f"NOT_STARTED: image build failed: {tag}")
        for tag, check in (("cloud-native-ros/state-bridge:p2",
                            "from px4_msgs.msg import VehicleStatus; import state_bridge.ros2_middleware_adapter"),
                           ("cloud-native-ros/control-plane:p2", "from px4_msgs.msg import VehicleStatus")):
            p = run(["docker", "run", "--rm", "--entrypoint", "/bin/bash", tag, "-c",
                     f'. /ws/install/setup.bash && python3 -c "{check}"'], timeout=300)
            if p.returncode:
                raise Stop(f"NOT_STARTED: px4_msgs import failed in {tag}")
        self.frozen = self.image_ids()
        if any(v is None for v in self.frozen.values()):
            raise Stop(f"NOT_STARTED: image missing: {self.frozen}")
        json.dump({"rev": self.rev, "frozen_images": self.frozen, "timing": self.timing,
                   "order": ev.ORDER, "sha256": {p: sha256(os.path.join(ROOT, p)) for p in HASHED}},
                  open(os.path.join(self.results, "manifest.json"), "w"), indent=1)
        self.say("bench checks ok")

    # ---- observer ----
    def api_ready(self):
        try:
            return run(["kubectl", "--context", CONTEXT, "get", "--raw", "/readyz"], timeout=5).returncode == 0
        except subprocess.TimeoutExpired:
            return False

    def start_proxy(self, d):
        proc = subprocess.Popen(["kubectl", "--context", CONTEXT, "proxy", "--address", "127.0.0.1",
                                 "--port", str(self.timing["proxy_port"])],
                                stdout=open(os.path.join(d, "proxy.out"), "a"), stderr=subprocess.STDOUT)
        for _ in range(30):
            try:
                if ms.http_get(self.proxy_url + "/version", [], "apiserver", True)[0] == 200:
                    return proc
            except Exception:
                pass
            time.sleep(1)
        proc.kill()
        raise Stop("INCONCLUSIVE (orchestration): kubectl proxy not answering")

    def etcd_url(self):
        ip = run(["docker", "inspect", "-f", "{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}",
                  SERVER]).stdout.strip()
        return f"http://{ip}:{self.timing['etcd_port']}/metrics"

    def start_sampler(self, prefix):
        stop = prefix + "sampler.stop"
        proc = subprocess.Popen([sys.executable, os.path.join(HERE, "metrics_sampler.py"), "sample-http",
                                 self.proxy_url, NAMESPACE, prefix + "samples.jsonl", stop,
                                 prefix + "account.jsonl", self.etcd_url()],
                                stderr=open(prefix + "sampler.err", "w"))
        return proc, stop

    @staticmethod
    def stop_sampler(proc, stop):
        """Stops it and returns its CPU time, its waited-for children included (wait4); None if
        it had already been reaped (it died: the run is then stopped anyway)."""
        open(stop, "w").close()
        if proc.returncode is not None:
            return None
        try:
            deadline = time.time() + 15
            while time.time() < deadline:
                pid, status, usage = os.wait4(proc.pid, os.WNOHANG)
                if pid:
                    proc.returncode = os.waitstatus_to_exitcode(status)
                    return usage.ru_utime + usage.ru_stime
                time.sleep(0.2)
            proc.kill()
            pid, status, usage = os.wait4(proc.pid, 0)
            proc.returncode = os.waitstatus_to_exitcode(status)
            return usage.ru_utime + usage.ru_stime
        except ChildProcessError:
            return None

    @staticmethod
    def proc_cpu(pid):
        fields = open(f"/proc/{pid}/stat").read().rsplit(")", 1)[1].split()
        return (int(fields[11]) + int(fields[12])) / os.sysconf("SC_CLK_TCK")

    @staticmethod
    def ok_read(path):
        return os.path.exists(path) and any(not s.get("error") and s.get("scrape") for s in ms.load(path))

    # ---- server cgroup, from the host ----
    def cgroup_dir(self):
        cid = run(["docker", "inspect", "-f", "{{.Id}}", SERVER]).stdout.strip()
        return os.path.join(CGROUP_ROOT, f"docker-{cid}.scope")

    @staticmethod
    def cgroup(cg):
        try:
            usage = next(int(line.split()[1]) for line in open(os.path.join(cg, "cpu.stat"))
                         if line.startswith("usage_usec"))
            return {"cpu_usage_usec": usage, "memory_current": int(open(os.path.join(cg, "memory.current")).read())}
        except (OSError, StopIteration, ValueError):
            return None

    def edge(self, cg, etcd_url):
        t = time.time()
        try:
            read = {"error": None, **ms.read_once_http(self.proxy_url, NAMESPACE, etcd_url, False, [])}
        except Exception as exc:
            read = {"error": repr(exc)}
        read["t"] = time.time()
        return {"t": t, "read": read, "cgroup": self.cgroup(cg)}

    # ---- one run ----
    def run_one(self, row):
        n, rnd, variant, pair, observer, order = row
        d = os.path.join(self.results, "runs", f"{n:02d}")
        os.makedirs(d)
        info = {"n": n, "round": rnd, "variant": variant, "pair": pair, "incident_observer": observer,
                "blocks": order, "status": None}
        if self.clusters():
            raise Stop("NOT_STARTED: a cluster exists before the run")
        self.say(f"run {n} start: variant {variant} pair {pair} observer {observer} blocks {order}")
        env = dict(os.environ, RESET_S3="1", N_ROBOTS="3", VARIANT=variant)
        runner = subprocess.Popen(["bash", RUNNER], cwd=ROOT, env=env, start_new_session=True,
                                  stdout=open(os.path.join(d, "runner.out"), "w"), stderr=subprocess.STDOUT)
        started, proxy, sampler, stop, checked, t_sampler = time.time(), None, None, None, False, None
        try:
            while runner.poll() is None:
                if observer == "A" and sampler is None and self.api_ready():
                    proxy = self.start_proxy(d)
                    sampler, stop = self.start_sampler(d + "/")
                    t_sampler = time.time()
                    self.say(f"run {n} sampler started (pid {sampler.pid}, proxy pid {proxy.pid})")
                if sampler is not None and not checked:
                    if self.ok_read(d + "/samples.jsonl"):
                        checked = True
                        self.say(f"run {n} sampler check ok")
                    elif sampler.poll() is not None or time.time() - t_sampler > self.timing["check_sec"]:
                        raise Stop(f"INCONCLUSIVE (orchestration): no successful read within "
                                   f"{self.timing['check_sec']}s")
                if time.time() - started > self.timing["runner_limit_sec"]:
                    raise Stop(f"INTERRUPTED: runner running after {self.timing['runner_limit_sec']}s")
                time.sleep(2)
            if observer == "A" and not checked and not self.ok_read(d + "/samples.jsonl"):
                raise Stop("INCONCLUSIVE (orchestration): runner ended without a successful sampler read")
            rc, runner_end = runner.returncode, time.time()
            text = open(os.path.join(d, "runner.out")).read()
            result_dir = next((line.split("Result dir: ", 1)[1].strip() for line in text.splitlines()
                               if line.startswith("Result dir: ")), None)
            s3 = next((line.rsplit(" ", 1)[1] for line in text.splitlines() if line.startswith("S3 result (N=3")), None)
            info.update({"exit": rc, "s3_result": s3, "runner_end": runner_end, "result_dir": result_dir})
            try:
                tb = json.load(open(os.path.join(result_dir, "timing-boundaries.json")))["incident"]
                info.update({"t0": tb["start_utc_ns"] / 1e9, "incident_end": tb["end_utc_ns"] / 1e9})
            except (OSError, KeyError, TypeError, ValueError):
                raise Stop(f"INCONCLUSIVE: no T0 (runner rc {rc})")
            self.say(f"run {n} runner end rc={rc} s3_result={s3}")
            if rc != 0 and not (rc == 1 and s3 == "false"):
                raise Stop(f"INCONCLUSIVE: runner rc {rc}, S3 result {s3}")
            hold = open(os.path.join(d, "hold.jsonl"), "w")
            while True:
                try:
                    present = CLUSTER in self.clusters()
                except Stop:
                    present = None          # unknown: C4 fails, never read as present
                hold.write(json.dumps({"t": time.time(), "cluster_present": present}) + "\n")
                hold.flush()
                if time.time() >= info["t0"] + self.timing["hold_after_t0_sec"]:
                    break
                time.sleep(min(10.0, max(0.0, info["t0"] + self.timing["hold_after_t0_sec"] - time.time())))
            if sampler is not None:
                proxy_cpu = self.proc_cpu(proxy.pid)
                sampler_cpu = self.stop_sampler(sampler, stop)    # None if it died: C6' then fails
                json.dump({"cpu_s": None if sampler_cpu is None else sampler_cpu + proxy_cpu,
                           "wall_s": time.time() - t_sampler,
                           "processes": {"sampler (with its children)": sampler_cpu, "kubectl proxy": proxy_cpu}},
                          open(os.path.join(d, "observer-cpu.json"), "w"))
                sampler = None
            if proxy is None:
                proxy = self.start_proxy(d)
            self.blocks(n, d, order)
            info["status"] = "OK" if rc == 0 else "FUNCTIONAL_FAIL"
        except Stop as stop_exc:
            info["status"] = str(stop_exc)
        except Exception as exc:          # any other failure of the wrapper: recorded, the campaign stops
            info["status"] = f"INCONCLUSIVE (orchestration): {exc!r}"
        finally:
            if sampler is not None:
                self.stop_sampler(sampler, stop)
            if proxy is not None:
                proxy.terminate()
                try:
                    proxy.wait(10)
                except subprocess.TimeoutExpired:
                    proxy.kill()
            if runner.poll() is None:
                os.killpg(runner.pid, signal.SIGTERM)
                for _ in range(90):
                    if runner.poll() is not None:
                        break
                    time.sleep(2)
                else:
                    os.killpg(runner.pid, signal.SIGKILL)
                    runner.wait()
            self.finish(n, d, info)
        if info["status"] not in ("OK", "FUNCTIONAL_FAIL"):
            raise Stop(info["status"])

    def blocks(self, n, d, order):
        cg, etcd_url = self.cgroup_dir(), self.etcd_url()
        time.sleep(self.timing["settle_sec"])
        out = open(os.path.join(d, "blocks.jsonl"), "w")
        for i, cond in enumerate(order):
            prefix = os.path.join(d, f"block{i}-")
            sampler = stop = thread = None
            memory, done, cpu = [], threading.Event(), None

            def sample_memory():
                while not done.is_set():
                    c = self.cgroup(cg)
                    if c:
                        memory.append(c["memory_current"])
                    done.wait(1.0)
            try:
                if cond == "A":
                    sampler, stop = self.start_sampler(prefix)
                    time.sleep(self.timing["observer_lead_sec"])
                start = self.edge(cg, etcd_url)
                thread = threading.Thread(target=sample_memory, daemon=True)
                thread.start()
                time.sleep(max(0.0, start["t"] + self.timing["block_sec"] - time.time()))
                end = self.edge(cg, etcd_url)
            finally:                       # the block's sampler never outlives the block
                done.set()
                if thread is not None:
                    thread.join(3)
                if sampler is not None:
                    cpu = self.stop_sampler(sampler, stop)
            block = {"i": i, "condition": cond, "start": start, "end": end, "memory": memory}
            if cond == "A":
                block["observer_cpu_s"] = cpu
                block["account"] = ms.load(prefix + "account.jsonl") if os.path.exists(prefix + "account.jsonl") else []
                reads = ms.load(prefix + "samples.jsonl") if os.path.exists(prefix + "samples.jsonl") else []
                block["observer_reads"] = [{"t_start": r.get("t_start"), "error": bool(r.get("error")),
                                            "scrape": r.get("scrape")} for r in reads]
            out.write(json.dumps(block) + "\n")
            out.flush()
            self.say(f"run {n} block {i} {cond} done")

    def finish(self, n, d, info):
        if info.get("result_dir") and os.path.isdir(info["result_dir"]):
            shutil.copytree(info["result_dir"], os.path.join(d, "runner-result"), dirs_exist_ok=True)
        ids = self.image_ids()
        info["images_match_frozen"] = ids == self.frozen
        if not info["images_match_frozen"] and info["status"] in ("OK", "FUNCTIONAL_FAIL"):
            info["status"] = f"INCONCLUSIVE: images differ from the frozen IDs: {ids}"
        try:                               # ONLY this pilot's cluster is deleted; any other is left alone
            if CLUSTER in self.clusters():
                run(["k3d", "cluster", "delete", CLUSTER], timeout=300)
            left = self.clusters()
        except Stop as exc:
            left = [f"unknown: {exc}"]
        dirty = self.worktree_dirty()
        info.update({"clusters_left": left, "worktree_clean": not dirty, "worktree_status": dirty})
        if (left or dirty) and info["status"] in ("OK", "FUNCTIONAL_FAIL"):
            info["status"] = f"INCONCLUSIVE: cleanup (clusters left {left}, worktree dirty: {dirty[:200]})"
        json.dump(info, open(os.path.join(d, "run.json"), "w"), indent=1)
        self.say(f"run {n} end: {info['status']}; clusters [{' '.join(left)}]; worktree clean {not dirty}")


def main(argv=None):
    p = argparse.ArgumentParser()
    p.add_argument("--results")
    p.add_argument("--rev")
    p.add_argument("--plan", action="store_true")
    p.add_argument("--test-timing", help="ONLY for the offline tests: JSON overriding TIMING; recorded")
    a = p.parse_args(argv)
    timing = dict(TIMING, **(json.loads(a.test_timing) if a.test_timing else {}))
    if a.plan:
        print(json.dumps({"order": ev.ORDER, "timing": timing, "margins": ev.MARGIN,
                          "d3_margin_rel": ev.D3_MARGIN_REL, "c5_max_share": ev.C5_MAX_SHARE,
                          "c6_max_cores": ev.C6_MAX_CORES, "cross_check": [ev.C5X_REL, ev.C5X_ABS],
                          "min_coverage": ev.MIN_COVERAGE, "workload_every": ms.WORKLOAD_EVERY,
                          "workload_max_spacing_sec": ms.WORKLOAD_MAX_SPACING_SEC}, indent=1))
        return 0
    os.makedirs(os.path.join(a.results, "runs"))
    c = Campaign(a.results, a.rev, timing)
    if a.test_timing:
        c.say(f"TEST TIMING (not a pilot run): {a.test_timing}")
    try:
        c.preflight()
        for row in ev.ORDER:
            c.run_one(row)
    except Stop as stop:
        c.say(f"campaign stopped: {stop}")
        return 3
    c.say(f"verdict: {subprocess.run([sys.executable, os.path.join(HERE, 's3_sampler_pilot2_eval.py'), a.results], capture_output=True, text=True).stdout.strip()}")
    c.say("s3 pilot 2 wrapper done")
    return 0


if __name__ == "__main__":
    sys.exit(main())
