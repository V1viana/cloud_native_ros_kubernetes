#!/usr/bin/env python3
"""R14 campaign wrapper (docs/R14_WRAPPER_DESIGN_DRAFT.md, decisions of Viviana of 2 October;
calendar: r14_schedule.py). It EXECUTES the fixed calendar, never decides the order.

  r14_campaign.py --results DIR --rev REV                   preflight, S2 qualifications, calendar
  r14_campaign.py --results DIR --rev REV --resume [--halt CONFIG ...]
                                                            after a pause: same revision, same image
                                                            IDs; the paused run is never rerun
  r14_campaign.py --plan                                    calendar summary, limits, and exit

Reused, unchanged: the fail-closed bench controls of the pilot 2 wrapper (worktree with
untracked files, only the runner's own cluster deleted, any other cluster or a k3d error
stops, frozen image IDs) and, for S3, the edges-only observer E with the rule of
s3_observer_probe_eval.b1. Runners and judges are called from the frozen revision (ROOT).

Stop rule (R14 4.1 + decisions of 2 October), checked after EVERY run:
- images different from the frozen IDs, a cluster that cannot be deleted, another cluster, a
  k3d or git error, a dirty worktree: STOP;
- INCONCLUSIVE or INTERRUPTED (timeout included) WITH data: PAUSE of the whole campaign,
  results kept; resume only after an explicit decision (optionally halting that config);
- without data (the runner stopped before creating its cluster): ONE rerun, recorded;
- datastore not verified: the run is invalid; two in a row: STOP;
- first S3 N=10 pair (round 1): window E not measurable with L3 in A or B -> both S3 levels
  halted, the other configurations continue (a common cause is caught by the rules above).
A timeout is recorded as INTERRUPTED, never as a functional FAIL.
"""

import argparse
import json
import os
import subprocess
import sys
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import r14_schedule as rs  # noqa: E402
import s3_edge_observer as eo  # noqa: E402
import s3_observer_probe_eval as pe  # noqa: E402
import s3_sampler_pilot2 as p2  # noqa: E402
from s3_sampler_pilot2 import Stop, run, sha256  # noqa: E402

PROJECT_BUILDS = [
    ("cloud-native-ros/control-plane:p2", "containers/control-plane/Dockerfile"),
    ("cloud-native-ros/event-detector:p2", "containers/event-detector/Dockerfile"),
    ("cloud-native-ros/kuberos:p2", "containers/kuberos/Dockerfile"),
    ("cloud-native-ros/fleet-operator:p2", "containers/fleet-operator/Dockerfile"),
    ("cloud-native-ros/state-bridge:p2", "containers/state-bridge/Dockerfile"),
    ("cloud-native-ros/s2-harness:p2", "containers/s2-harness/Dockerfile"),
    ("cloud-native-ros/mission-observer:p2", "containers/mission-observer/Dockerfile"),
    ("cloud-native-ros/control-plane:e2", "containers/control-plane/Dockerfile"),
    ("cloud-native-ros/event-detector:e2-upstream", "containers/event-detector/Dockerfile"),
    ("cloud-native-ros/fleet-operator:e2", "containers/fleet-operator/Dockerfile"),
]
THIRD_PARTY = ["microros/micro-ros-agent:humble", "px4io/px4-sitl:latest", "redis:7", "ros:humble-ros-base",
               "rancher/mirrored-library-busybox:1.36.1", "rancher/k3s:v1.31.5-k3s1"]   # k3s: every cluster
LIMITS = {"campaign": 3600.0, "s2": 3600.0, "s2-qualification": 5400.0, "s4": 2700.0,
          "s3-3": 5400.0, "s3-10": 7200.0, "ttr": 3000.0}        # [D] 2 October; no overall limit
TIMING = {"poll_sec": 2.0, "window_sec": 180.0, "end_read_delay_sec": 0.2, "kill_grace_sec": 180.0,
          "proxy_port": 18004, "etcd_port": 2381, "api_check_sec": 2.0,
          # unused by this wrapper, required by the reused pilot 2 Campaign
          "check_sec": 30.0, "observer_lead_sec": 0.0, "hold_after_t0_sec": 0.0, "block_sec": 0.0,
          "settle_sec": 0.0, "runner_limit_sec": 0.0}
# the images run_s2.sh records in images.json, per variant (= its IMAGES arrays, run_s2.sh
# "if [[ $VARIANT == a ]]"): a reused qualification must list EXACTLY this set, each with the
# frozen ID (an empty or partial images.json is never a match -- review of Viviana)
S2_IMAGES = {
    "a": ["cloud-native-ros/control-plane:p2", "cloud-native-ros/event-detector:p2", "cloud-native-ros/kuberos:p2",
          "cloud-native-ros/s2-harness:p2", "microros/micro-ros-agent:humble", "px4io/px4-sitl:latest", "redis:7"],
    "b": ["cloud-native-ros/control-plane:p2", "cloud-native-ros/fleet-operator:p2", "cloud-native-ros/state-bridge:p2",
          "cloud-native-ros/s2-harness:p2", "microros/micro-ros-agent:humble", "px4io/px4-sitl:latest"],
}
PREFLIGHT_CLUSTER = "r14-preflight"
S1_REFERENCE_REV = "2387d5d"        # the campaign whose frozen images the S1 block reuses, without any build
# The block fixes the runner's environment explicitly (review of Viviana, 4 October): an inherited
# S1_WINDOW_SEC would change the 180 s window, an inherited SKIP_IMAGE_IMPORT or SKIP_IMAGE_BUILD_IMPORT
# would skip the import of the frozen images into the cluster. Recorded in the block's manifest.
S1_BLOCK_ENV = {"SKIP_IMAGE_BUILD": "1", "SKIP_IMAGE_IMPORT": "0", "SKIP_IMAGE_BUILD_IMPORT": "0", "S1_WINDOW_SEC": "180"}
BASE_IMAGES = "config/base_images.json"       # registry digests of the bases the Dockerfiles pin
HASHED = ["scripts/r14_campaign.py", "scripts/r14_schedule.py", "scripts/r14_stats.py", "scripts/s3_edge_observer.py",
          "scripts/s3_observer_probe_eval.py", "scripts/s3_sampler_pilot2.py", "scripts/metrics_sampler.py",
          "scripts/s3_metrics.py", "scripts/run_campaign.sh", "scripts/run_s2.sh", "scripts/run_s4_bench.sh",
          "scripts/run_s3.sh", "scripts/ttr/run_ttr.sh", "scripts/datastore_check.py",
          "scripts/etcd_gate/judge_datastore.py", BASE_IMAGES,
          "scripts/run_s1.sh", "scripts/s1_drift_judge.py", "scripts/s1_drift_observer.py", "scripts/campaign_cell.py"]
VALID = "VALID"


def canonical(tag):
    """Docker's full form of a reference: "redis:7" -> "docker.io/library/redis:7"."""
    name, _, version = tag.rpartition(":")
    first = name.split("/")[0]
    if "/" not in name:
        name = f"docker.io/library/{name}"
    elif "." not in first and ":" not in first and first != "localhost":
        name = f"docker.io/{name}"
    return f"{name}:{version}"


class Pause(Exception):
    """The whole campaign pauses; resume only after an explicit decision of Viviana."""


def cluster_of(how):
    if how["runner"] == "campaign":
        return "cloud-native-e2" if how["scenario"] == "e2" else "cloud-native-p2"
    return {"s2": "cloud-native-p2", "s4": "cloud-native-s4", "s3": "cloud-native-s3", "ttr": "cloud-native-p2"}[how["runner"]]


def limit_of(how):
    if how["runner"] == "s3":
        return LIMITS[f"s3-{how['n_robots']}"]
    return LIMITS[how["runner"]]


def base_images(root):
    """Every FROM of the built Dockerfiles names a base pinned by its REGISTRY digest, the one
    recorded in config/base_images.json (decision of Viviana, 2 October: a tag can move during
    the campaign -- python:3.11-slim had four different indexes in two weeks, ros:humble-ros-base
    is newer upstream than the local one -- and a moved base gives other image IDs). Returns
    what the manifest records; fail-closed on an unpinned, unknown or differently pinned base."""
    try:
        with open(os.path.join(root, BASE_IMAGES)) as f:
            table = json.load(f)
        pins = {tag: v["index_digest"] for tag, v in table["images"].items()}
        platform = table["platform"]
    except (OSError, ValueError, KeyError, TypeError) as e:
        raise Stop(f"NOT_STARTED: {BASE_IMAGES} unreadable: {e}")
    used = {}
    for dockerfile in sorted({d for _, d in PROJECT_BUILDS}):
        stages = set()
        try:
            lines = open(os.path.join(root, dockerfile)).read().splitlines()
        except OSError as e:
            raise Stop(f"NOT_STARTED: {dockerfile} unreadable: {e}")
        for line in lines:
            parts = line.split()
            if not parts or parts[0].upper() != "FROM":
                continue
            args = [x for x in parts[1:] if not x.startswith("--")]
            ref, earlier = args[0], set(stages)
            if len(args) == 3 and args[1].upper() == "AS":
                stages.add(args[2])
            if ref in earlier:                         # FROM an earlier stage of the same file
                continue
            tag, _, digest = ref.partition("@")
            if not digest.startswith("sha256:") or len(digest) != 71 or pins.get(tag) != digest:
                raise Stop(f"NOT_STARTED: {dockerfile}: base {ref} is not pinned to the digest in {BASE_IMAGES}")
            used.setdefault(tag, []).append(dockerfile)
    return {"platform": platform,
            "images": {tag: dict(table["images"][tag], reference=f"{canonical(tag)}@{pins[tag]}",
                                 dockerfiles=sorted(set(files))) for tag, files in sorted(used.items())}}


def check_local_bases(bases, images):
    """A pinned base that is also a frozen local image (ros:humble-ros-base, used by the runners
    too) must BE that version: its local registry digests include the pinned one."""
    for tag, rec in bases["images"].items():
        if tag in images and not any(d.endswith("@" + rec["index_digest"]) for d in images[tag]["repo_digests"]):
            raise Stop(f"NOT_STARTED: the local {tag} is not the pinned base {rec['index_digest']}")


def separate_dirs(results, reference):
    """The block's results dir and the reference campaign must be two different trees (symbolic
    links resolved): the same dir, or one inside the other, would let the block write into the
    frozen campaign's files. Checked BEFORE anything is created or opened (review of Viviana)."""
    a, b = os.path.realpath(results), os.path.realpath(reference)
    if a == b or a.startswith(b + os.sep) or b.startswith(a + os.sep):
        return f"NOT_STARTED: results dir {a} and reference campaign {b} are not separate trees"
    return None


def precheck(args):
    """Read-only checks BEFORE anything is created or opened (review of Viviana, 4 October): a
    wrong --results must not get a single line appended, not even the refusal. Returns the
    refusal message or None.
    - new run of the block: results and reference are separate trees, the reference is a campaign;
    - resume (any mode): state.json exists and is readable, revision and mode match the arguments,
      the frozen set exists; for the block, the saved reference is still a separate tree."""
    if args.s1_block and args.r14_reference:
        problem = separate_dirs(args.results, args.r14_reference)
        if problem:
            return problem
        if not os.path.isfile(os.path.join(os.path.realpath(args.r14_reference), "state.json")):
            return f"NOT_STARTED: {args.r14_reference} has no state.json: not a campaign results dir"
    if args.resume:
        try:
            state = json.load(open(os.path.join(args.results, "state.json")))
        except (OSError, ValueError) as e:
            return f"NOT_STARTED: resume needs a readable state.json in {args.results}: {e}"
        if state.get("rev") != args.rev or not state.get("frozen"):
            return "NOT_STARTED: resume needs the same revision and a frozen image set"
        mode = state.get("mode", "campaign")
        if (mode == "s1-block") != args.s1_block:
            return f"NOT_STARTED: resume in the mode of the results dir ({mode}), not {'s1-block' if args.s1_block else 'campaign'}"
        if mode == "s1-block":
            try:
                saved = json.load(open(os.path.join(args.results, "manifest.json")))["reference"]["reference_dir"]
            except (OSError, ValueError, KeyError, TypeError) as e:
                return f"NOT_STARTED: the block's manifest has no readable reference: {e}"
            problem = separate_dirs(args.results, saved)
            if problem:
                return problem
            if args.r14_reference and os.path.realpath(args.r14_reference) != os.path.realpath(saved):
                return f"NOT_STARTED: --r14-reference {args.r14_reference} is not the block's saved reference {saved}"
    return None


def write(path, data):
    with open(path, "w") as f:
        json.dump(data, f, indent=1, default=str)


class R14(p2.Campaign):
    def __init__(self, results, rev, timing):
        super().__init__(results, rev, timing)
        self.state_path = os.path.join(results, "state.json")
        self.state = json.load(open(self.state_path)) if os.path.exists(self.state_path) else {
            "rev": rev, "next_seq": 1, "halted": {}, "paused": None, "datastore_fail_streak": 0,
            "retried": [], "qualifications": {}, "frozen": None, "mode": "campaign"}
        self.frozen = self.state.get("frozen")
        self.steps = os.path.join(results, "steps.jsonl")
        self.datastore_log = os.path.join(results, "datastore-log.jsonl")
        self.watch_file = os.path.join(results, "datastore-watch.jsonl")
        self.watch_stop = os.path.join(results, "watch.stop")

    def save(self):
        write(self.state_path, self.state)

    def start_watch(self):
        """The datastore watcher of this session: a stale stop file from an earlier session is
        removed first (it would end the watcher at once), and the watcher must be alive."""
        if os.path.exists(self.watch_stop):
            os.remove(self.watch_stop)
        watch = subprocess.Popen([sys.executable, os.path.join(p2.ROOT, "scripts", "datastore_check.py"), "watch",
                                  self.watch_file, "--stop-file", self.watch_stop],
                                 stderr=open(os.path.join(self.results, "watch.err"), "a"))
        time.sleep(1.0)
        if watch.poll() is not None:
            raise Stop(f"NOT_STARTED: the datastore watcher exited at once (rc {watch.returncode})")
        self.say(f"datastore watcher started (pid {watch.pid})")
        return watch

    def stop_watch(self, watch):
        open(self.watch_stop, "w").close()
        try:
            watch.wait(30)
        except subprocess.TimeoutExpired:
            watch.kill()

    def image_ids(self):
        out = {}
        for tag in [t for t, _ in PROJECT_BUILDS] + THIRD_PARTY:
            p = run(["docker", "image", "inspect", "-f", "{{.Id}}", tag])
            out[tag] = p.stdout.strip() if p.returncode == 0 else None
        return out

    # ---- preparation (fail-closed) ----
    def bench_checks(self):
        if self.clusters():
            raise Stop("NOT_STARTED: a k3d cluster exists")
        head = run(["git", "rev-parse", "--short", "HEAD"], cwd=p2.ROOT).stdout.strip()
        dirty = self.worktree_dirty()
        if dirty or head != self.rev:
            raise Stop(f"NOT_STARTED: worktree not clean or not at {self.rev} (HEAD {head}; status: {dirty[:200]})")
        subs = run(["git", "submodule", "status"], cwd=p2.ROOT).stdout
        open(os.path.join(self.results, "submodules.txt"), "w").write(subs)
        if not subs.strip() or any(line[:1] in "-+U" for line in subs.splitlines()):
            raise Stop("NOT_STARTED: submodules not initialised at the revision")
        for line in subs.splitlines():
            path = line.split()[1]
            expected = run(["git", "ls-tree", "HEAD", path], cwd=p2.ROOT).stdout.split()
            actual = run(["git", "-C", os.path.join(p2.ROOT, path), "rev-parse", "HEAD"]).stdout.strip()
            if not expected or expected[2] != actual:
                raise Stop(f"NOT_STARTED: submodule {path} not at the revision's commit")
        if not os.listdir(os.path.join(p2.ROOT, "integrations", "px4_msgs")):
            raise Stop("NOT_STARTED: px4_msgs is empty")

    def preflight(self):
        self.bench_checks()
        bases = base_images(p2.ROOT)                  # before any build: pinned bases only
        write(os.path.join(self.results, "base-images.json"), bases)
        log = open(os.path.join(self.results, "image-build.log"), "w")
        for tag, dockerfile in PROJECT_BUILDS:        # built ONCE; the runners' builds must return these IDs
            p = run(["docker", "build", "-q", "-t", tag, "-f", dockerfile, "."], timeout=3600, cwd=p2.ROOT)
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
        frozen = self.image_ids()
        if any(v is None for v in frozen.values()):
            raise Stop(f"NOT_STARTED: image missing: {[k for k, v in frozen.items() if v is None]}")
        self.frozen = self.state["frozen"] = frozen
        write(os.path.join(self.results, "frozen-images.json"), frozen)
        with open(os.path.join(self.results, "frozen-images.txt"), "w") as f:     # run_ttr.sh: "name id" lines
            for tag, image_id in frozen.items():
                f.write(f"{tag} {image_id}\n")
        images = self.image_records()
        write(os.path.join(self.results, "images.json"), images)
        check_local_bases(bases, images)
        other = sorted(t for t, r in images.items() if r["local_build"] and r["platform"] != bases["platform"])
        if other:
            raise Stop(f"NOT_STARTED: images not built for {bases['platform']}: {other}")
        self.start_time_preflight()
        write(os.path.join(self.results, "manifest.json"),
              {"rev": self.rev, "frozen_images": frozen, "images": images, "base_images": bases,
               "limits": LIMITS, "timing": self.timing,
               "calendar_sha256": sha256(os.path.join(HERE, "r14_schedule.py")),
               "sha256": {p: sha256(os.path.join(p2.ROOT, p)) for p in HASHED if os.path.exists(os.path.join(p2.ROOT, p))}})
        self.save()
        self.say("bench checks ok")

    def image_records(self):
        """Per frozen image (R14_WRAPPER_DESIGN_DRAFT.md 13.1, point 3): tag, full reference, local ID
        (the config digest), the registry digests known locally and the platform. For an upstream
        multi-arch image, RepoDigests is usually the INDEX digest; the digest of the platform manifest
        and the digests of the published project images are recorded by the publication step, after
        the push. Fail-closed: an image that cannot be inspected or whose ID is not the frozen one."""
        out = {}
        for tag, frozen_id in self.frozen.items():
            p = run(["docker", "image", "inspect", tag])
            try:
                d = json.loads(p.stdout)[0] if p.returncode == 0 else None
            except (ValueError, IndexError):
                d = None
            if not d or d.get("Id") != frozen_id:
                raise Stop(f"NOT_STARTED: image not inspectable or not the frozen one: {tag}")
            out[tag] = {"reference": canonical(tag), "id": d["Id"], "repo_digests": d.get("RepoDigests") or [],
                        "platform": "/".join(x for x in (d.get("Os"), d.get("Architecture"), d.get("Variant")) if x),
                        "local_build": tag.startswith("cloud-native-ros/")}
        return out

    def s1_block_preflight(self, reference_dir):
        """Block S1-delete after the campaign (docs/S1_DELETE_BLOCK_PROPOSAL_DRAFT.md 5, 5.1): the
        SAME images as the campaign, NO build, NO px4_msgs import check (no image is built), NO
        S2 qualification (S1 does not use them). The 16 local image IDs must be the ones frozen by
        the reference campaign; the runner then imports them into each cluster and the usual ID
        checks after every run stay in force. Writes links.json (reference results, the rows of
        the original S1-delete runs). Fail-closed: NOT_STARTED on any doubt."""
        self.bench_checks()
        bases = base_images(p2.ROOT)
        write(os.path.join(self.results, "base-images.json"), bases)
        ref = os.path.abspath(reference_dir)
        try:
            ref_manifest = json.load(open(os.path.join(ref, "manifest.json")))
            ref_frozen = json.load(open(os.path.join(ref, "frozen-images.json")))
        except (OSError, ValueError) as e:
            raise Stop(f"NOT_STARTED: the reference campaign is unreadable: {ref}: {e}")
        if ref_manifest.get("rev") != S1_REFERENCE_REV or ref_manifest.get("frozen_images") != ref_frozen:
            raise Stop(f"NOT_STARTED: {ref} is not the frozen campaign of revision {S1_REFERENCE_REV}")
        local = self.image_ids()
        if local != ref_frozen:
            diff = sorted(t for t in set(local) | set(ref_frozen) if local.get(t) != ref_frozen.get(t))
            raise Stop(f"NOT_STARTED: local image IDs differ from the reference campaign's: {diff}")
        rows = []
        for d in sorted(os.listdir(os.path.join(ref, "runs"))):
            rp = os.path.join(ref, "runs", d, "run.json")
            if not os.path.exists(rp):
                continue
            r = json.load(open(rp))
            if r.get("config") == "s1-delete":
                rows.append({k: r.get(k) for k in ("seq", "config", "variant", "verdict", "valid", "decision", "dir", "result_dir")})
        if not rows:
            raise Stop(f"NOT_STARTED: no s1-delete row in the reference campaign {ref}")
        links = {"proposal": "docs/S1_DELETE_BLOCK_PROPOSAL_DRAFT.md", "reference_dir": ref, "reference_rev": S1_REFERENCE_REV,
                 "reference_frozen_images_sha256": sha256(os.path.join(ref, "frozen-images.json")),
                 "reference_manifest_sha256": sha256(os.path.join(ref, "manifest.json")),
                 "reason": "S1-delete was suspended in the reference campaign (row 18, B: baseline not ready; the "
                           "run is invalid and its times are never recovered); this block collects 10 new A/B pairs "
                           "with the Deployment wait added before the baseline in B",
                 "reference_rows": rows}
        write(os.path.join(self.results, "links.json"), links)
        self.frozen = self.state["frozen"] = ref_frozen             # same shape as preflight(): records for the checks
        write(os.path.join(self.results, "frozen-images.json"), ref_frozen)
        with open(os.path.join(self.results, "frozen-images.txt"), "w") as f:
            for tag, image_id in ref_frozen.items():
                f.write(f"{tag} {image_id}\n")
        images = self.image_records()
        write(os.path.join(self.results, "images.json"), images)
        check_local_bases(bases, images)
        other = sorted(t for t, r in images.items() if r["local_build"] and r["platform"] != bases["platform"])
        if other:
            raise Stop(f"NOT_STARTED: images not built for {bases['platform']}: {other}")
        self.state["mode"] = "s1-block"
        write(os.path.join(self.results, "manifest.json"),
              {"rev": self.rev, "mode": "s1-block", "frozen_images": ref_frozen, "images": images, "base_images": bases,
               "reference": links, "runner_env": S1_BLOCK_ENV, "limits": LIMITS, "timing": self.timing,
               "calendar_sha256": sha256(os.path.join(HERE, "r14_schedule.py")),
               "sha256": {p: sha256(os.path.join(p2.ROOT, p)) for p in HASHED if os.path.exists(os.path.join(p2.ROOT, p))}})
        self.save()
        self.say(f"bench checks ok; S1 block: images of {S1_REFERENCE_REV} reused, no build, no S2 qualification")

    def start_time_preflight(self):
        """E needs process_start_time_seconds from the API server AND etcd (prereg of the probe)."""
        name, ctx = PREFLIGHT_CLUSTER, f"k3d-{PREFLIGHT_CLUSTER}"
        try:
            p = run(["k3d", "cluster", "create", name, "--image", "rancher/k3s:v1.31.5-k3s1", "--servers", "1",
                     "--agents", "0", "--api-port", "127.0.0.1:6557", "--wait", "--timeout", "300s",
                     "--kubeconfig-update-default", "--kubeconfig-switch-context=false",
                     "--k3s-arg", "--cluster-init@server:0", "--k3s-arg", "--etcd-expose-metrics@server:0"], timeout=400)
            if p.returncode:
                raise Stop("NOT_STARTED: preflight cluster not created")
            raw = run(["kubectl", "--context", ctx, "get", "--raw", "/metrics"], timeout=30).stdout
            api = p2.ms.s3_metrics.parse_counters(raw, ["process_start_time_seconds"])["process_start_time_seconds"]
            ip = run(["docker", "inspect", "-f", "{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}",
                      f"k3d-{name}-server-0"]).stdout.strip()
            etcd = eo.read_etcd(f"http://{ip}:{self.timing['etcd_port']}/metrics", [])
        finally:
            run(["k3d", "cluster", "delete", name], timeout=300)
        if name in self.clusters():
            raise Stop("NOT_STARTED: the preflight cluster was not deleted")
        if not api or not etcd.get("etcd_start"):
            raise Stop("NOT_STARTED: process_start_time_seconds not exposed by the API server and etcd")

    def qualifications(self, reuse):
        """[D] both variants at the start, after the image freeze, before the first counted run;
        reuse only with identical bench hashes AND image IDs."""
        sys.path.insert(0, os.path.join(p2.ROOT, "scripts"))
        import s2_record
        bench = s2_record.inputs(p2.ROOT)["bench"]
        for v in ("a", "b"):
            q = reuse.get(v)
            ok = False
            if q:
                try:
                    record = json.load(open(os.path.join(q, "s2-qualify.json")))
                    images = json.load(open(os.path.join(q, "images.json")))
                    ok = (record.get("verdict") == "QUALIFIED" and (record.get("qualified_topology") or {}).get("bench") == bench
                          and set(images) == set(S2_IMAGES[v])
                          and all(self.frozen.get(k) == images[k] for k in S2_IMAGES[v]))
                except (OSError, ValueError):
                    ok = False
                self.say(f"s2 qualification {v} reuse {'accepted' if ok else 'refused'}: {q}")
            if not ok:
                n = len([d for d in os.listdir(os.path.join(self.results, "runs")) if d.startswith(f"000-s2-qualification-{v}-")])
                rec = self.execute({"seq": 0, "config": f"s2-qualification-{v}", "variant": v, "round": 0,
                                    "how": {"runner": "s2", "case": "qualification"}, "dir_suffix": f"-q{n + 1}"},
                                   limit=LIMITS["s2-qualification"], counted=False)   # never overwrites an earlier one
                # the step itself must be clean before its QUALIFIED counts (review of Viviana):
                # bench checks after the step (images, clusters, worktree) and the datastore
                if rec.get("status") == "STOP":
                    raise Stop(f"S2 qualification {v}: {rec['reason']}")
                if rec.get("status") == "PAUSE":
                    raise Pause(f"S2 qualification {v}: {rec['reason']}")
                if rec.get("datastore") != "VERIFIED":
                    raise Pause(f"S2 qualification {v}: datastore {rec.get('datastore')}")
                q = rec.get("result_dir")
                verdict = None
                try:
                    verdict = json.load(open(os.path.join(q, "s2-qualify.json"))).get("verdict")
                except (OSError, TypeError, ValueError):
                    pass
                if verdict != "QUALIFIED":
                    raise Pause(f"S2 qualification {v}: {verdict} (exit {rec.get('exit')})")
            self.state["qualifications"][v] = q
            self.save()

    # ---- one run ----
    def command(self, entry):
        how, v = entry["how"], entry["variant"]
        env = dict(os.environ, DATASTORE_LOG=self.datastore_log)
        if how["runner"] == "campaign":
            env.update(RUNS="1", SCENARIOS=how["scenario"], VARIANTS=v, RESULTS_ROOT=os.path.join(self.results, "campaign"),
                       CAMPAIGN_ID=f"{entry['seq']:03d}-{entry['config']}-{v}")
            if "S1_CASE" in how:
                env["S1_CASE"] = how["S1_CASE"]
            if self.state.get("mode") == "s1-block":
                env.update(S1_BLOCK_ENV)           # run_campaign.sh -> run_s1.sh: no build; import, window and ID checks fixed
            return ["bash", "scripts/run_campaign.sh"], env
        if how["runner"] == "s2":
            env.update(VARIANT=v, S2_CASE=how["case"])
            if how["case"] != "qualification":
                env["S2_QUALIFICATION"] = self.state["qualifications"][v]
            return ["bash", "scripts/run_s2.sh"], env
        if how["runner"] == "s4":
            env.update(VARIANT=v, S4_MODE="cell", S4_CELL=how["cell"])
            return ["bash", "scripts/run_s4_bench.sh"], env
        if how["runner"] == "ttr":                 # run_ttr.sh does not print its result dir: we choose it
            env.update(VARIANT=v, FROZEN_IMAGES=os.path.join(self.results, "frozen-images.txt"),
                       RESULT_DIR=os.path.join(self.results, "ttr", f"{entry['seq']:03d}-{v}-attempt{entry.get('attempt', 0)}"))
            return ["bash", "scripts/ttr/run_ttr.sh"], env
        if how["runner"] == "s3":
            env.update(RESET_S3="1", N_ROBOTS=str(how["n_robots"]), VARIANT=v)
            return ["bash", "scripts/run_s3.sh"], env
        raise ValueError(how)

    def execute(self, entry, limit=None, counted=True):
        """Runs one row; returns its record. Never raises Stop/Pause itself: decide() does."""
        how = entry["how"]
        name = f"{entry['seq']:03d}-{entry['config']}-{entry['variant']}"
        suffix = (entry.get("dir_suffix") or "") + (f"-retry{entry['attempt']}" if entry.get("attempt") else "")
        d = os.path.join(self.results, "runs", name + suffix)
        os.makedirs(d, exist_ok=True)
        record = {**{k: entry[k] for k in ("seq", "config", "variant", "round")}, "pair": entry.get("pair"),
                  "how": how, "counted": counted,
                  "attempt": entry.get("attempt", 0), "dir_suffix": entry.get("dir_suffix") or "", "dir": d,
                  "cluster": cluster_of(how)}
        if self.clusters():
            record.update(status="STOP", reason=f"a cluster exists before the run: {self.clusters()}")
            write(os.path.join(d, "run.json"), record)
            return record
        cmd, env = self.command(entry)
        out_path = os.path.join(d, "runner.out")
        start = time.time()
        runner = subprocess.Popen(cmd, cwd=p2.ROOT, env=env, start_new_session=True,
                                  stdout=open(out_path, "w"), stderr=subprocess.STDOUT)
        e = {"proxy": None, "edges": {}, "burst": {}, "thread": None, "account": []} if how["runner"] == "s3" else None
        cluster_seen, timed_out, next_api = False, False, 0.0
        limit = limit or limit_of(how)
        try:
            while runner.poll() is None:
                if not cluster_seen and record["cluster"] in self.clusters():
                    cluster_seen = True
                if e is not None:
                    next_api = self.observe_s3(e, d, out_path, next_api)
                if time.time() - start > limit:
                    timed_out = True
                    self.kill(runner)
                    break
                time.sleep(eo.POLL_SEC if e is not None and e["proxy"] is not None else self.timing["poll_sec"])
            runner.wait()
            record.update(exit=runner.returncode, timed_out=timed_out, start=start, end=time.time(),
                          cluster_seen=cluster_seen or record["cluster"] in self.clusters())
            record.update(self.verdict(entry, out_path, runner.returncode, timed_out))
            if e is not None and not timed_out:
                record["e_window"] = self.finish_s3(e, d, record)
        except Stop as exc:
            record.update(status="STOP", reason=str(exc), end=time.time())
        except Exception as exc:          # the wrapper's own failure: recorded, the campaign pauses
            record.update(status="PAUSE", reason=f"wrapper error: {exc!r}", end=time.time(), cluster_seen=True)
        finally:
            if e is not None and e["proxy"] is not None:
                e["proxy"].terminate()
                try:
                    e["proxy"].wait(10)
                except subprocess.TimeoutExpired:
                    e["proxy"].kill()
            if runner.poll() is None:
                self.kill(runner)
        with open(self.steps, "a") as f:
            f.write(json.dumps({"step": name, "start": start, "end": record.get("end", time.time()),
                                "rc": record.get("exit", -1), "dir": record.get("result_dir") or ""}) + "\n")
        self.after(record)
        write(os.path.join(d, "run.json"), record)
        return record

    def kill(self, runner):
        try:
            os.killpg(runner.pid, 15)
        except ProcessLookupError:
            return
        deadline = time.time() + self.timing["kill_grace_sec"]
        while runner.poll() is None and time.time() < deadline:
            time.sleep(1)
        if runner.poll() is None:
            os.killpg(runner.pid, 9)
            runner.wait()

    # ---- S3 with E ----
    def observe_s3(self, e, d, out_path, next_api):
        if e["proxy"] is None and time.time() >= next_api:
            next_api = time.time() + self.timing["api_check_sec"]
            if self.api_ready():
                e["proxy"] = self.start_proxy(d)
                e["etcd_url"] = self.etcd_url()
                e["edges"]["api_ready"] = eo.read_api(self.proxy_url, e["account"])
        rd = e.get("result_dir") or next((line.split("Result dir: ", 1)[1].strip() for line in open(out_path).read().splitlines()
                                         if line.startswith("Result dir: ")), None)
        e["result_dir"] = rd
        if e["proxy"] is not None and rd and e["thread"] is None and os.path.exists(os.path.join(rd, eo.AFTER_BOOTSTRAP)):
            e["thread"] = threading.Thread(target=lambda: e["burst"].update(
                reads=eo.burst(e["etcd_url"], rd, e["account"])), daemon=True)
            e["thread"].start()
        return next_api

    def finish_s3(self, e, d, record):
        if e["thread"] is not None:
            e["thread"].join(5)
        burst = e["burst"].get("reads", [])
        with open(os.path.join(d, "burst.jsonl"), "w") as f:
            for r in burst:
                f.write(json.dumps(r) + "\n")
        rd = e.get("result_dir")
        try:
            tb = json.load(open(os.path.join(rd, "timing-boundaries.json")))["incident"]
            t0 = tb["start_utc_ns"] / 1e9
        except (OSError, KeyError, TypeError, ValueError):
            return {"measurable": False, "reasons": ["no T0"]}
        if e["proxy"] is None:
            return {"measurable": False, "reasons": ["the observer never started"]}
        time.sleep(max(0.0, t0 + self.timing["window_sec"] + self.timing["end_read_delay_sec"] - time.time()))
        e["edges"].update(api_end=eo.read_api(self.proxy_url, e["account"]), etcd_end=eo.read_etcd(e["etcd_url"], e["account"]),
                          workload_end=eo.read_workload(self.proxy_url, p2.NAMESPACE, e["account"]))
        write(os.path.join(d, "edges.json"), e["edges"])
        snapshot = eo.runner_snapshot(rd) if os.path.exists(os.path.join(rd, eo.BEFORE_INCIDENT)) else None
        reads = [x for x in (e["edges"].get("api_ready"), e["edges"].get("api_end")) if x and not x.get("error")]
        w = pe.b1({"t0": t0, "window_sec": self.timing["window_sec"]}, snapshot, burst, e["edges"].get("api_end"),
                  e["edges"].get("etcd_end"), e["edges"].get("workload_end"), e["edges"].get("api_ready"),
                  max((x["scrape"]["end"] - x["scrape"]["start"] for x in reads), default=None))
        if w.get("rates_per_s"):
            l3 = w["rates_per_s"]["L3"]
            w["rates"] = {"L0": w["rates_per_s"]["L0"], "L1": w["rates_per_s"]["L1"], "L2": w["rates_per_s"]["L2"],
                          "L3": l3.get("etcd_mvcc_put_total", 0.0) + l3.get("etcd_mvcc_delete_total", 0.0)}
        return w

    # ---- verdicts ----
    def verdict(self, entry, out_path, rc, timed_out):
        """The runner's OWN verdict name (read from its real files: checked on the gate 2, pilots
        and probe results) and, apart, whether the run is valid (valid_run). A timeout is
        INTERRUPTED, never a functional FAIL."""
        how = entry["how"]
        text = open(out_path).read()
        rd = next((line.split("Result dir: ", 1)[1].strip() for line in text.splitlines()
                   if line.startswith("Result dir: ")), None)
        absolute = lambda path: None if not path else path if os.path.isabs(path) else os.path.join(p2.ROOT, path)
        if timed_out:
            return {"verdict": "INTERRUPTED", "valid_run": False, "result_dir": rd, "reason": "timeout"}
        if how["runner"] == "campaign":         # run_campaign.sh: runs/<scenario>-<v>-001/status.json, r9_verdict
            cid = f"{entry['seq']:03d}-{entry['config']}-{entry['variant']}"
            path = os.path.join(self.results, "campaign", cid, "runs", f"{how['scenario']}-{entry['variant']}-001", "status.json")
            try:
                status = json.load(open(path))
            except (OSError, ValueError):
                return {"verdict": "NO_JUDGEMENT", "valid_run": False, "result_dir": rd}
            v = status.get("r9_verdict") or "NO_JUDGEMENT"
            return {"verdict": v, "valid_run": v in ("PASS", "FAIL"), "functional": status.get("functional_outcome"),
                    "result_dir": absolute(status.get("result_dir")) or rd, "reason": status.get("invalid_reason")}
        if how["runner"] == "s2":               # run_s2.sh: exit 0..4; the cell's own judge in s2-judge.json
            if how["case"] == "qualification":
                names = {0: "QUALIFIED", 1: "NOT_QUALIFIED", 3: "INTERRUPTED"}
                return {"verdict": names.get(rc, "NO_JUDGEMENT"), "valid_run": rc == 0, "result_dir": rd}
            by_rc = {0: "PASS", 1: "FAIL", 2: "INCONCLUSIVE", 3: "INTERRUPTED", 4: "NOT_STARTED"}.get(rc, "NO_JUDGEMENT")
            try:
                judged = json.load(open(os.path.join(rd, "s2-judge.json"))).get("verdict")
            except (OSError, TypeError, ValueError):
                judged = None
            if judged is not None and judged != by_rc:
                return {"verdict": "INCONCLUSIVE", "valid_run": False, "result_dir": rd,
                        "reason": f"judge {judged} and exit {rc} ({by_rc}) disagree"}
            v = judged or by_rc
            return {"verdict": v, "valid_run": v in ("PASS", "FAIL") and judged is not None, "result_dir": rd}
        if how["runner"] == "s4":               # run_s4_bench.sh: s4-judge.json verdict
            try:
                v = json.load(open(os.path.join(rd, "s4-judge.json")))["verdict"]
            except (OSError, KeyError, TypeError, ValueError):
                v = "NO_JUDGEMENT"
            return {"verdict": v, "valid_run": v in ("PASS", "FAIL"), "result_dir": rd}
        if how["runner"] == "ttr":              # run_ttr.sh: verdict.json; validity from the judge's own "valid"
            rd = os.path.join(self.results, "ttr", f"{entry['seq']:03d}-{entry['variant']}-attempt{entry.get('attempt', 0)}")
            try:
                v = json.load(open(os.path.join(rd, "verdict.json")))
            except (OSError, TypeError, ValueError):
                return {"verdict": "NO_JUDGEMENT", "valid_run": False, "result_dir": rd}
            name = v.get("verdict") or "NO_JUDGEMENT"
            return {"verdict": name, "valid_run": name in ("MEASURED", "CENSORED") and v.get("valid") is True,
                    "result_dir": rd, "reason": v.get("reasons") or v.get("reason")}
        if how["runner"] == "s3":               # run_s3.sh: "S3 result (N=.., variant v): true|false"; rc 2 = INVALID
            s3 = next((line.rsplit(" ", 1)[1] for line in text.splitlines() if line.startswith("S3 result (N=")), None)
            v = "PASS" if rc == 0 and s3 == "true" else "FAIL" if rc == 1 and s3 == "false" else "INCONCLUSIVE"
            return {"verdict": v, "valid_run": v in ("PASS", "FAIL"), "functional": s3, "result_dir": rd}
        raise ValueError(how)

    # ---- after each run: bench checks and datastore ----
    def after(self, record):
        if record.get("status") == "STOP":
            return
        record["images_match_frozen"] = self.image_ids() == self.frozen
        try:
            if record["cluster"] in self.clusters():
                record["cluster_left"] = True
                run(["k3d", "cluster", "delete", record["cluster"]], timeout=300)
            left = self.clusters()
        except Stop as exc:
            record.update(status="STOP", reason=str(exc))
            return
        dirty = self.worktree_dirty()
        record["datastore"] = self.datastore_verdict(record)
        if not record["images_match_frozen"]:
            record.update(status="STOP", reason="images differ from the frozen IDs")
        elif left:
            record.update(status="STOP", reason=f"clusters after the run: {left} (only the runner's own is deleted)")
        elif dirty:
            record.update(status="STOP", reason=f"worktree dirty: {dirty[:200]}")

    def datastore_verdict(self, record):
        out = os.path.join(self.results, "datastore-judge.json")
        run([sys.executable, os.path.join(p2.ROOT, "scripts", "etcd_gate", "judge_datastore.py"), self.steps,
             self.datastore_log, self.watch_file, out], timeout=300)
        try:
            results = json.load(open(out))
            name = f"{record['seq']:03d}-{record['config']}-{record['variant']}"
            return next((r["verdict"] for r in reversed(results) if r["step"] == name), "INCONCLUSIVE")
        except (OSError, ValueError):
            return "INCONCLUSIVE"

    # ---- decisions ----
    def decide(self, record):
        """CONTINUE, RETRY, PAUSE or STOP, with the stop rule (4.1 + 2 October)."""
        if record.get("status") in ("STOP", "PAUSE"):
            return record["status"], record["reason"]
        v = record["verdict"]
        if record.get("valid_run"):
            if record["datastore"] != "VERIFIED":
                record["valid"] = False
                self.state["datastore_fail_streak"] += 1
                if self.state["datastore_fail_streak"] >= 2:
                    return "STOP", "datastore not verified twice in a row"
                return "CONTINUE", "datastore not verified: run not valid"
            self.state["datastore_fail_streak"] = 0
            record["valid"] = True
            return "CONTINUE", "valid"
        record["valid"] = False
        if not record.get("cluster_seen"):                     # the runner stopped before creating its cluster
            if record.get("attempt", 0) == 0:
                return "RETRY", f"{v} without data: one rerun"
            return "PAUSE", f"{v} without data again after the rerun"
        return "PAUSE", f"{v} with data"

    def s3_confirmation(self, pair_records):
        """First S3 N=10 pair: the window E must be measurable WITH L3 in A and B, else both S3 levels stop."""
        bad = [r for r in pair_records if not (r.get("e_window") or {}).get("measurable")]
        if bad:
            for c in ("s3-n3", "s3-n10"):
                self.state["halted"][c] = ("first S3 N=10 pair: window E not measurable with L3 in "
                                           + ", ".join(r["variant"].upper() for r in bad))
            return False
        return True

    # ---- the calendar ----
    def check_s3_confirmation(self, entry):
        """The first S3 N=10 pair, judged from the PERSISTED records of both runs (a pause
        between A and B must not skip it -- review of Viviana); done once, kept in the state."""
        if entry["config"] != rs.FIRST_IN_ROUND_1 or entry["round"] != 1 or self.state.get("s3_confirmation"):
            return
        records = []
        for e in rs.build():
            if e["config"] == rs.FIRST_IN_ROUND_1 and e["round"] == 1:
                decided = sorted((r for r in self.attempts_on_disk(e) if r is not None and "decision" in r),
                                 key=lambda r: r.get("start") or 0.0)
                if not decided:
                    return                                   # the other run is still to come
                records.append(decided[-1])
        ok = self.s3_confirmation(records)
        self.state["s3_confirmation"] = {"ok": ok, "variants": [r["variant"] for r in records],
                                         "measurable": [bool((r.get("e_window") or {}).get("measurable")) for r in records]}
        self.save()
        if not ok:
            self.say(f"S3 halted: {self.state['halted']['s3-n10']}")

    def conclude(self, entry, record, action, reason, resumed=False):
        """All the bookkeeping of a row, in a crash-safe order (reviews of Viviana):
        1. the decision; 2. the summary (rebuilt from the records on disk, never counted twice);
        3. for PAUSE/STOP, the paused state saved at once (the cursor does NOT move yet);
        4. the row_complete marker; 5. the first S3 N=10 pair confirmation, kept in the state;
        6. ONLY THEN the cursor (next_seq). An interruption anywhere leaves the row recoverable:
        before 4 conclude() runs again on resume; after 4 the guard skips the row and redoes 5."""
        record["decision"] = {"action": action, "reason": reason}
        write(os.path.join(record["dir"], "run.json"), record)
        self.rebuild_summary()
        self.say(f"{entry['seq']} end {entry['config']} {entry['variant']}: {record.get('verdict')} "
                 f"valid={record.get('valid')} -> {action} ({reason}){' [concluded on resume]' if resumed else ''}")
        if action in ("STOP", "PAUSE"):
            self.state["paused"] = {"seq": entry["seq"], "reason": reason, "kind": action}
            self.save()
        record["row_complete"] = True
        write(os.path.join(record["dir"], "run.json"), record)
        self.check_s3_confirmation(entry)
        if action != "STOP":                                   # a PAUSE row is never rerun automatically
            self.state["next_seq"] = entry["seq"] + 1
        self.save()
        if action == "STOP":
            raise Stop(reason)
        if action == "PAUSE":
            raise Pause(reason)

    def run_row(self, entry, attempt=0):
        """Executes the row (or its single rerun without data) and concludes it."""
        record = self.execute({**entry, "attempt": attempt} if attempt else entry)
        action, reason = self.decide(record)
        if action == "RETRY":
            # recorded BEFORE the rerun: a resume after a crash here runs the rerun, never skips the row
            record["decision"] = {"action": action, "reason": reason}
            write(os.path.join(record["dir"], "run.json"), record)
            self.state["retried"].append(entry["seq"])
            self.save()
            self.say(f"{entry['seq']} {reason}")
            record = self.execute({**entry, "attempt": 1})
            action, reason = self.decide(record)
        self.conclude(entry, record, action, reason)

    def loop(self, calendar):
        if self.state.get("mode") != "s1-block" and not all(v in self.state.get("qualifications", {}) for v in ("a", "b")):
            raise Stop("NOT_STARTED: S2 qualifications of A and B not both in place before the first counted run")
        for entry in calendar:
            if entry["seq"] < self.state["next_seq"]:
                continue
            if entry["config"] in self.state["halted"]:
                self.say(f"{entry['seq']} {entry['config']} {entry['variant']} skipped: halted ({self.state['halted'][entry['config']]})")
                self.state["next_seq"] = entry["seq"] + 1
                self.save()
                continue
            on_disk = self.attempts_on_disk(entry)
            if any(r is None for r in on_disk):
                self.state["paused"] = {"seq": entry["seq"], "reason": "attempt on disk without a record", "kind": "PAUSE"}
                self.save()
                raise Pause(f"{entry['seq']} {entry['config']} {entry['variant']}: an attempt on disk has no record "
                            f"(interrupted wrapper?): decide before going on")
            started = sorted((r for r in on_disk if r.get("start") is not None), key=lambda r: r["start"])   # by time, not name
            latest = started[-1] if started else None
            if latest is not None and "decision" not in latest:
                # the wrapper stopped between the run and its decision: never an automatic step on
                self.state["paused"] = {"seq": entry["seq"], "reason": "attempt on disk without a recorded decision",
                                        "kind": "PAUSE"}
                self.save()
                raise Pause(f"{entry['seq']} {entry['config']} {entry['variant']}: an attempt on disk has no recorded "
                            f"decision (interrupted wrapper?): decide before going on")
            if latest is not None and latest.get("row_complete"):
                self.say(f"{entry['seq']} {entry['config']} {entry['variant']}: row already concluded on disk "
                         f"({len(on_disk)} attempt(s)): never rerun automatically")
                self.state["next_seq"] = entry["seq"] + 1
                self.save()
                self.check_s3_confirmation(entry)
                continue
            if latest is not None and latest["decision"]["action"] == "RETRY":
                if latest.get("attempt", 0) >= 1:
                    raise Pause(f"{entry['seq']}: a RETRY recorded on the rerun itself: decide")
                self.say(f"{entry['seq']} {entry['config']} {entry['variant']}: RETRY recorded, rerun not done: running it now")
                record = self.execute({**entry, "dir_suffix": latest.get("dir_suffix") or "", "attempt": 1})
                action, reason = self.decide(record)
                self.conclude(entry, record, action, reason)
                continue
            if latest is not None:                              # final decision, bookkeeping not concluded
                self.conclude(entry, latest, latest["decision"]["action"], latest["decision"]["reason"], resumed=True)
                continue
            if on_disk:                                         # only pre-run stops: no runner started, no data
                entry = {**entry, "dir_suffix": f"-resume{len(on_disk)}"}
            self.say(f"{entry['seq']} start {entry['config']} pair {entry['pair']} {entry['variant']}")
            self.run_row(entry)
        self.say("calendar complete")

    def attempts_on_disk(self, entry):
        """Every attempt of this calendar row already on disk: its run.json, or None when the
        directory has no record (a wrapper interrupted between the runner and the record)."""
        name = f"{entry['seq']:03d}-{entry['config']}-{entry['variant']}"
        runs = os.path.join(self.results, "runs")
        out = []
        for d in sorted(os.listdir(runs)) if os.path.isdir(runs) else []:
            if d == name or d.startswith(name + "-retry") or d.startswith(name + "-resume"):
                path = os.path.join(runs, d, "run.json")
                try:
                    out.append(json.load(open(path)))
                except (OSError, ValueError):
                    out.append(None)
        return out

    def rebuild_summary(self):
        """The cumulative summary rebuilt from the records on disk: per calendar row, the latest
        started attempt with a final decision (not RETRY); qualifications (seq 0) apart. Rebuilt,
        never incremented, so an interruption can never count a run twice or lose one."""
        runs = os.path.join(self.results, "runs")
        rows = {}
        for d in sorted(os.listdir(runs)):
            try:
                r = json.load(open(os.path.join(runs, d, "run.json")))
            except (OSError, ValueError):
                continue
            if not r.get("seq") or r.get("start") is None or (r.get("decision") or {}).get("action") in (None, "RETRY"):
                continue
            key = (r["seq"], r["config"], r["variant"])
            if key not in rows or r["start"] > rows[key]["start"]:
                rows[key] = r
        s = {}
        for (_, config, variant), r in sorted(rows.items()):
            c = s.setdefault(config, {"a": {}, "b": {}, "valid": {"a": 0, "b": 0}, "invalid": {"a": 0, "b": 0}})
            name = r.get("verdict") or r.get("status")
            c[variant][name] = c[variant].get(name, 0) + 1
            c["valid" if r.get("valid") else "invalid"][variant] += 1
        write(os.path.join(self.results, "summary.json"), s)

def main(argv=None):
    a = argparse.ArgumentParser()
    a.add_argument("--results")
    a.add_argument("--rev")
    a.add_argument("--plan", action="store_true")
    a.add_argument("--resume", action="store_true")
    a.add_argument("--halt", action="append", default=[], help="on --resume: configurations left halted (local cause)")
    a.add_argument("--reuse-qualification", action="append", default=[], help="v=DIR, checked before reuse")
    a.add_argument("--test-timing", help="ONLY for the offline tests: JSON overriding TIMING; recorded")
    a.add_argument("--s1-block", action="store_true",
                   help="block S1-delete after the campaign: 10 new A/B pairs, no build, no S2 qualification")
    a.add_argument("--r14-reference", help="with --s1-block: results dir of the frozen campaign whose image IDs are reused")
    args = a.parse_args(argv)
    if args.r14_reference and not args.s1_block:
        a.error("--r14-reference is only for --s1-block")
    if args.s1_block and not (args.r14_reference or args.resume or args.plan):
        a.error("--s1-block needs --r14-reference (the frozen campaign whose images are reused)")
    timing = dict(TIMING, **(json.loads(args.test_timing) if args.test_timing else {}))
    if args.s1_block:
        calendar = rs.build_s1_block()
        rs.check_s1_block(calendar)
    else:
        calendar = rs.build()
        rs.check(calendar)
    if args.plan:
        print(json.dumps({"mode": "s1-block" if args.s1_block else "campaign", "summary": rs.summary(calendar),
                          "limits": LIMITS, "timing": timing, "first": calendar[:2],
                          "images": [t for t, _ in PROJECT_BUILDS] + THIRD_PARTY}, indent=1))
        return 0
    problem = precheck(args)
    if problem:                                       # before any file of either tree is created or opened
        print(problem, file=sys.stderr)
        return 3
    os.makedirs(os.path.join(args.results, "runs"), exist_ok=True)
    c = R14(args.results, args.rev, timing)
    watch = None
    try:
        if args.resume:                               # revision, mode and frozen set already verified read-only by precheck()
            c.bench_checks()
            if c.image_ids() != c.state["frozen"]:
                raise Stop("NOT_STARTED: local image IDs differ from the frozen ones")
            for cfg in args.halt:
                c.state["halted"][cfg] = "halted on resume by decision (local cause)"
            c.say(f"resume after {c.state['paused']}; halted {c.state['halted']}")
            c.state["paused"] = None
            c.save()
        else:
            if c.state.get("frozen"):
                raise Stop("NOT_STARTED: results dir already used: use --resume")
            if args.s1_block:
                c.s1_block_preflight(args.r14_reference)
            else:
                c.preflight()
        watch = c.start_watch()            # BEFORE the qualifications: they are datastore steps too
        # on a resume too: a preparation interrupted between A and B is completed, and the
        # recorded ones are REVALIDATED with the reuse rule, before any counted run (review of Viviana)
        if not args.s1_block:
            c.qualifications({**c.state.get("qualifications", {}), **dict(x.split("=", 1) for x in args.reuse_qualification)})
        c.loop(calendar)
    except Pause as pause:
        c.say(f"campaign PAUSED: {pause} -- resume only after diagnosis and an explicit decision")
        return 4
    except Stop as stop:
        c.say(f"campaign STOPPED: {stop}")
        return 3
    finally:
        if watch is not None:
            c.stop_watch(watch)
    c.say("r14 campaign done")
    return 0


if __name__ == "__main__":
    sys.exit(main())
