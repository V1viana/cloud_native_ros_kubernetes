#!/usr/bin/env python3
"""S2 judge (R11, contract s2-partition-v1, docs/R11_S2_PARTITION.md, "Valutatore
e artefatti"): offline and repeatable, on the evidence of one cell written by
scripts/run_s2.sh and scripts/s2_phase.py.

One JSON document (s2-judge<suffix>.json, never overwriting an earlier one):
protocol_id, variant/case, provenance, validity, ground_truth, received,
recognized, audit, action, reconvergence, isolation, px4_continuity, the
missed-transient record (short/long) and the verdict with its reasons. Every
time carries its source and reference (host monotonic `mono`, UTC `utc`, or an
interval between the last read without and the first read with); what cannot
be observed is null with the reason, never zero.

Verdict, in this order:
  INTERRUPTED   the guard fired, the driver was stopped or did not end;
  FAIL          a functional violation observed where its own measurement holds:
                positive control not recognized or not migrated, a spurious
                incident/action in the negative control, service not reconverged
                within the horizon, isolation of drone02/drone03 lost, PX4
                continuity lost -- kept even when other measurements are short;
  INCONCLUSIVE  insufficient collection, pulse not confirmed, episode not wholly
                inside the cut, partition not proven, the cut's hold not proven (a
                rules sample off the declared cut, a probe across it, a stretch not
                read within 2 s), data out of order, inputs or images not as
                declared, a required dimension unknown;
  PASS          otherwise. In short/long an unrecognized transient is a valid
                result, not a failure: missed_within_180s = !recognized_within_180s
                on eligible episodes only (valid, inside the cut, central coverage).
Exit: 0 PASS, 1 FAIL, 2 INCONCLUSIVE, 3 INTERRUPTED.
Tested offline: operator/tests/test_s2_judge.py.
"""

import argparse
from datetime import datetime
import importlib.util
import json
import math
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


s2_ground_truth = _load("s2_ground_truth", os.path.join(HERE, "s2_ground_truth.py"))
event_trace = _load("event_trace", os.path.join(
    ROOT, "sources/operational_event_dispatcher/operational_event_dispatcher/event_trace.py"))
px4_status_continuity = _load("px4_status_continuity", os.path.join(HERE, "px4_status_continuity.py"))
mission_continuity = _load("mission_continuity", os.path.join(HERE, "mission_continuity.py"))
s2_partition = _load("s2_partition", os.path.join(HERE, "s2_partition.py"))

PROTOCOL_ID = "s2-partition-v1"
ROBOT, UNINVOLVED = "drone01", ("drone02", "drone03")
DRONES = ("drone01", "drone02", "drone03")
ONBOARD_COMPONENT, EDGE_COMPONENT = "companion-analytics-onboard", "companion-analytics-edge"
A_ONBOARD, A_EDGE = "drone01-companion-analytics-onboard", "drone01-companion-analytics"
B_ONBOARD, B_EDGE = "companion-analytics-drone01", "companion-analytics-drone01-edge"
B_POLICY = "analytics-latency-slo-s2-drone01"
A_DISPATCHER, A_MANAGER = "operational-event-dispatcher-p2", "application-manager-p2"
EVENT_TYPE, STATE_ENTER, STATE_RECOVERED = "AnalyticsLatencySLO", 0, 2
OWNER_LABEL = "dronekube.io/owned-by-rosmodule"

# The bench v1 limits (contract "Raccolta, copertura e riconvergenza").
SAMPLE_GAP_SEC = 2.0             # local recorder, between valid samples
SOURCE_DELAY_SEC = 2.0           # source stamp to reception
READ_GAP_SEC = 3.0               # inventory, nodes, audit, local copy
HEALTH_GAP_SEC = 10.0            # health observations per target
FRESH_SEC = 6.0                  # fresh nominal data
NOMINAL_MS = 150.0               # nominal = below the recovery threshold
RECONVERGE_READS, RECONVERGE_SPAN_SEC = 3, 2.0
CLOCK_LIMIT_SEC = 0.1
UTC_JUMP_SEC = 0.1
EPISODE_MATCH_SEC = 2.0          # an event's source stamp around the local episode
RESTORE_AFTER_RETURN_SEC = {"short": 2.0, "long": 30.0}
# the start of the removal against its target (decision on point 5): 1 ms of numeric
# tolerance before, at most 1 s after (two nominal periods of the driver)
RESTORE_TOLERANCE_SEC = (-0.001, 1.0)
COVERAGE_MARGIN_SEC = 10.0       # the recorder's window around the episode
# The cut read every second (decision after the fifth qualification): rules samples
# and every probed path at most this far apart, the window's edges included, else a
# gap -- a resolution of observation, not a proof of continuity. With reads and
# probe slots once a second, one missed read or skipped slot already exceeds it.
HOLD_GAP_SEC = 2.0
HOLD_RESOLUTION = ("the rules read and every path probed about once a second (an ICMP echo from the node, deadline "
                   "1 s; a TCP connect from one persistent process in drone01's harness on a monotonic 1 s grid, "
                   "deadline 0.8 s): an interruption of the cut shorter than the interval between two reads or "
                   "two sends is not excluded")
BREACH, HELD = ("connected", "refused", "reply"), ("timeout", "blocked")
BRACKET_SEC = 0.05               # a node's or container's UTC within the host's bracket of that read


# ---- reading ----------------------------------------------------------------

def read_jsonl(path):
    if not os.path.exists(path):
        return None
    out = []
    with open(path, errors="replace") as h:
        for line in h:
            if line.strip():
                try:
                    out.append(json.loads(line))
                except ValueError:
                    out.append({"_unparsed": line[:200]})
    return out


def read_json(path):
    try:
        with open(path) as h:
            return json.load(h)
    except (OSError, ValueError):
        return None


def read_text(path):
    try:
        with open(path, errors="replace") as h:
            return h.read()
    except OSError:
        return None


def parse_iso(value):
    if not value or not isinstance(value, str):
        return None
    text = value.strip().replace("Z", "+00:00")
    match = re.match(r"^(.*T\d\d:\d\d:\d\d)(\.\d+)?(.*)$", text)
    if match and match.group(2) and len(match.group(2)) > 7:        # nanoseconds: datetime takes micro
        text = match.group(1) + match.group(2)[:7] + match.group(3)
    try:
        return datetime.fromisoformat(text).timestamp()
    except ValueError:
        return None


def load(result_dir):
    d = result_dir
    obs = os.path.join(d, "observers")
    logs = {}
    for name in os.listdir(obs) if os.path.isdir(obs) else []:
        if name.startswith("logs-") and name.endswith(".jsonl"):
            logs[name[5:-6]] = read_jsonl(os.path.join(obs, name))
    return {
        "dir": d,
        "run": read_json(os.path.join(d, "run.json")) or {},
        "phases": read_jsonl(os.path.join(d, "phases.jsonl")) or [],
        "samples": read_jsonl(os.path.join(d, "harness/samples.jsonl")),
        "samples_text": read_text(os.path.join(d, "harness/samples.jsonl")),
        "samples_progressive_text": read_text(os.path.join(d, "harness/samples.progressive.jsonl")),
        "events": read_jsonl(os.path.join(d, "harness/events.jsonl")),
        "copy_reads": read_jsonl(os.path.join(d, "harness/copy-reads.jsonl")),
        "truth_incremental": read_jsonl(os.path.join(d, "truth-incremental.jsonl")),
        "health": read_jsonl(os.path.join(d, "prober/health.jsonl")),
        "inventory": read_jsonl(os.path.join(obs, "inventory.jsonl")),
        "nodes": read_jsonl(os.path.join(obs, "nodes.jsonl")),
        "audit": read_jsonl(os.path.join(obs, "audit.jsonl")),
        "logs": logs,
        "px4": {drone: read_text(os.path.join(obs, f"px4-{drone}.txt")) for drone in DRONES},
        "pods_start": read_json(os.path.join(d, "pods-start.json")),
        "pods_end": read_json(os.path.join(d, "pods-end.json")),
        "k8s_events": read_json(os.path.join(d, "events.json")),
        "manager_outbox": read_json(os.path.join(d, "manager-outbox.json")),
        "guard_fired": read_text(os.path.join(d, "guard-fired")),
        "placement_start": read_json(os.path.join(d, "placement-start.json")),
        "placement_end": read_json(os.path.join(d, "placement-end.json")),
        "partition_restored": read_text(os.path.join(d, "partition-restored")),
        "cut_samples": read_jsonl(os.path.join(d, "cut-samples.jsonl")),
        "cut_probes": read_jsonl(os.path.join(d, "cut-probes.jsonl")),
    }


# ---- small helpers ----------------------------------------------------------

def at(mono=None, utc=None, source="", ref=None):
    out = {"mono": mono, "utc": utc, "source": source}
    if ref is not None:
        out["ref"] = ref
    return out


def between(before, after, source):
    """The interval from the last read without to the first read with. A read's
    API state may be taken at any instant of the call: the conservative bounds
    are the start of the last read without and the end of the first read with
    (review of a41c087, choice 12)."""
    return {"last_without": None if before is None else {"mono": before["m0"], "utc": before["w0"],
                                                         "read_end_utc": before["w1"]},
            "first_with": {"mono": after["m0"], "utc": after["w0"], "read_end_utc": after["w1"]},
            "source": source}


def unknown(reason):
    return {"value": None, "reason": reason}


class Marks:
    def __init__(self, phases):
        self.all = phases
        self.by = {}
        for record in phases:
            self.by.setdefault(record.get("mark"), []).append(record)

    def first(self, name):
        return (self.by.get(name) or [None])[0]

    def last(self, name):
        return (self.by.get(name) or [None])[-1]


def gaps(times, start, end):
    """Largest gap covering [start, end] with the given instants (one before
    start and one after end needed, else infinite)."""
    times = sorted(t for t in times if t is not None)
    before = [t for t in times if t <= start]
    after = [t for t in times if t >= end]
    inside = [t for t in times if start < t < end]
    if not before or not after:
        return math.inf
    edges = [before[-1]] + inside + [after[0]]
    return max((b - a for a, b in zip(edges, edges[1:])), default=0.0)


def ok_reads(records):
    return [r for r in records or [] if "error" not in r and "_unparsed" not in r and "m1" in r]


def read_coverage(records, start, end, limit=READ_GAP_SEC):
    good = ok_reads(records)
    errors = [r for r in records or [] if "error" in r and start <= r.get("m0", -1) <= end]
    worst = gaps([r["m1"] for r in good], start, end)
    return {"max_gap_sec": None if math.isinf(worst) else round(worst, 3), "limit_sec": limit,
            "reads": sum(1 for r in good if start <= r["m1"] <= end), "errors": len(errors),
            "ok": not math.isinf(worst) and worst <= limit}


def robot_of(pod):
    labels = pod.get("labels") or {}
    for key in ("pod-name", "app.kubernetes.io/name", OWNER_LABEL, "robot"):
        match = re.search(r"drone0[1-9]", str(labels.get(key, "")))
        if match:
            return match.group(0)
    match = re.search(r"drone0[1-9]", pod.get("name") or "")
    return match.group(0) if match else None


# ---- timeline ---------------------------------------------------------------

def timeline(run, marks):
    case = run.get("case")
    start = marks.first("phase_start")
    horizon = marks.first("horizon_fixed")
    pulse = marks.first("pulse_written")
    out = {"phase_start": None if start is None else at(start["mono"], start["utc"], "runner"),
           "horizon": None if horizon is None else at(horizon["horizon_mono"],
                                                      None if start is None else
                                                      start["utc"] + horizon["horizon_mono"] - start["mono"],
                                                      "runner", ref=horizon.get("basis")),
           "reference": None, "pulse_programmed": None, "cut": None, "restore": None}
    ref0, ref1 = marks.first("reference_start"), marks.first("reference_end")
    if ref0 and ref1:
        out["reference"] = {"from": at(ref0["mono"], ref0["utc"], "runner"), "to": at(ref1["mono"], ref1["utc"], "runner")}
    if pulse:
        out["pulse_programmed"] = {"start_utc": pulse["command"]["start_utc"],
                                   "end_utc": pulse["programmed_end_utc"], "source": "pulse command"}
    drop, verified = marks.first("drop_apply_start"), marks.first("cut_verified")
    if drop:
        out["cut"] = {"first_drop": at(drop["mono"], drop["utc"], "runner, start of the apply command"),
                      "verified": None if verified is None else at(verified["mono"], verified["utc"], "runner")}
    written = marks.first("partition_restored_written")
    if written:
        remove = marks.first("remove_start") or written
        out["restore"] = {"start": at(remove["mono"], remove["utc"], "runner, start of the remove command"),
                          "command_end": at(written["restore_end_mono"], written["restore_end_utc"],
                                            "runner, end of the remove command that left no rule")}
    out["case"] = case
    return out


# ---- harness, pulse and clocks ----------------------------------------------

def harness_section(run, events, marks, problems):
    out = {"started": 0, "armed": None, "pulse": None}
    if events is None:
        problems.append("harness: events.jsonl missing")
        return out
    out["started"] = sum(1 for e in events if e.get("event") == "started")
    if out["started"] != 1:
        problems.append(f"harness: started {out['started']} times (a restart during the run)")
    for kind in ("command_present_at_start", "command_other_run", "command_invalid", "command_failed",
                 "command_unreadable"):
        found = [e for e in events if e.get("event") == kind]
        if found:
            problems.append(f"harness: {kind} x{len(found)}")
    run_id = run.get("run_id")
    attempts = [e for e in events if e.get("event") in ("armed", "arm_failed")
                 and str(e.get("id", "")).startswith(f"{run_id}-arm-")]
    armed = next((e for e in attempts if e.get("event") == "armed"), None)
    out["armed"], out["arm_attempts"] = armed, len(attempts)
    if armed is None or abs(armed.get("value", math.nan) - armed.get("nominal", math.nan)) > 1e-9:
        problems.append("harness: arm not confirmed (80 ms read back)")
    pulse_id = f"{run_id}-pulse"
    mine = [e for e in events if e.get("id") == pulse_id]
    if run.get("case") == "partition-only":
        if any(e.get("event") == "pulse_scheduled" for e in events):
            problems.append("harness: a pulse in partition-only")
        return out

    def first(name):
        return next((e for e in mine if e.get("event") == name), None)
    done = first("pulse_done")
    high_request, high_readback = first("high_request"), first("high_readback")
    restore_readbacks = [e for e in mine if e.get("event") == "restore_readback" and e.get("ok")]
    restored = first("restored")
    written = marks.first("pulse_written")
    out["pulse"] = {
        "scheduled": first("pulse_scheduled"), "high_request": high_request, "high_readback": high_readback,
        "restored": restored, "done": done,
        "confirmed": bool(done and done.get("high_confirmed") and done.get("restored")),
        "late_start_sec": None if high_request is None or written is None
        else round(high_request["utc"] - written["command"]["start_utc"], 3),
        "restore_attempts": None if restored is None else restored.get("attempts"),
        "high_interval": None if high_request is None or not restore_readbacks
        else {"from": at(high_request["mono"], high_request["utc"], "harness, set request"),
              "to": at(restore_readbacks[0]["mono"], restore_readbacks[0]["utc"], "harness, restore read back")}}
    if not out["pulse"]["confirmed"]:
        problems.append("pulse not confirmed (high and restore read back)")
    # A UTC jump alters the programmed pulse: the relation UTC - monotonic of the
    # harness's own records around it must stay put.
    if high_request is not None and done is not None:
        relation = [e["utc"] - e["mono"] for e in events
                    if "utc" in e and "mono" in e and high_request["mono"] - 1 <= e["mono"] <= done["mono"] + 1]
        spread = max(relation) - min(relation) if relation else 0.0
        out["pulse"]["utc_mono_spread_sec"] = round(spread, 6)
        if spread > UTC_JUMP_SEC:
            problems.append(f"UTC jump around the pulse ({spread:.3f} s): its times are not valid")
    return out


def clock_section(marks, problems):
    out = {}
    for name in ("clock_before", "clock_after"):
        record = marks.first(name)
        if record is None:
            problems.append(f"{name} missing")
            out[name] = None
            continue
        out[name] = {label: v.get("summary") for label, v in (record.get("clocks") or {}).items()}
        if not out[name]:
            problems.append(f"{name}: no container measured")
        for label, summary in out[name].items():
            if not (summary or {}).get("within_limit"):
                problems.append(f"{name} {label}: alignment not within {CLOCK_LIMIT_SEC} s ({summary})")
    before, after = marks.first("clock_before"), marks.first("clock_after")
    if before and after:
        drift = (after["utc"] - after["mono"]) - (before["utc"] - before["mono"])
        out["host_utc_mono_change_sec"] = round(drift, 6)
        if abs(drift) > UTC_JUMP_SEC:
            problems.append(f"host UTC moved {drift:.3f} s against monotonic during the run")
    return out


# ---- ground truth -----------------------------------------------------------

def _mine(sample):
    return sample.get("robot_id") == ROBOT and sample.get("component") == ONBOARD_COMPONENT


def ground_truth_section(data, marks, tl, handover, problems):
    samples = data["samples"]
    if samples is None:
        problems.append("local recorder: samples.jsonl missing")
        return {"value": None, "reason": "no local samples"}
    records = [s for s in samples if s.get("event") == "sample"]
    identities = {}
    for s in records:
        key = f"{s.get('robot_id')}/{s.get('component')}"
        identities[key] = identities.get(key, 0) + 1
    truth = s2_ground_truth.GroundTruth()
    invalid = 0
    for s in records:
        if not _mine(s):
            continue
        try:
            truth.feed(s)
        except (KeyError, TypeError):
            invalid += 1
    if truth.last_sample is not None:
        truth.advance(truth.last_sample)
    mine = [s for s in records if _mine(s)]
    valid = [s for s in mine if all(isinstance(s.get(k), (int, float)) and not isinstance(s.get(k), bool)
                                    and math.isfinite(s[k]) and s[k] >= 0 for k in ("latency_ms", "recv_mono"))]
    relation = sorted(s["recv_utc"] - s["recv_mono"] for s in valid if isinstance(s.get("recv_utc"), (int, float)))
    rel = relation[len(relation) // 2] if relation else None

    def utc(mono):
        return None if mono is None or rel is None else mono + rel
    episodes = [{"first_violating_window_start": at(e["first_violating_window_start"],
                                                    utc(e["first_violating_window_start"]), "local recorder"),
                 "entered": at(e["entered"], utc(e["entered"]), "local recorder"),
                 "returned": None if e["returned"] is None else at(e["returned"], utc(e["returned"]), "local recorder")}
                for e in truth.episodes]
    out = {"identities": identities, "invalid_samples": invalid + len(mine) - len(valid),
           "out_of_order": len(truth.out_of_order), "windows": len(truth.windows), "episodes": episodes,
           "utc_mono_relation": rel,
           # failed reads of the progressive copy, counted (choice 4); the replay above
           # is of the final copy, complete whatever the progressive one missed
           "copy_read_errors": sum(1 for r in data["copy_reads"] or [] if "error" in r)}
    if truth.out_of_order:
        problems.append(f"local recorder: {len(truth.out_of_order)} samples out of order")
    # the progressive copy (what the runner acted on) is a prefix of the final one
    prog, final = data["samples_progressive_text"], data["samples_text"]
    if prog is None:
        problems.append("local copy: the progressive copy is missing")
    elif final is not None and not final.startswith(prog):
        problems.append("local copy: the progressive copy is not a prefix of the final one")
    # the incremental evaluation agrees with the replay
    case = data["run"].get("case")
    seen = marks.first("local_return_seen")
    if seen is not None:
        acted = seen["episode"]
        match = next((e for e in truth.episodes if abs(e["entered"] - acted["entered"]) <= 1e-6), None)
        if match is None or match["returned"] is None or abs(match["returned"] - acted["returned"]) > 1e-6:
            problems.append("the incremental truth the runner acted on disagrees with the replay")
    last = (data["truth_incremental"] or [None])[-1]
    if last is not None:
        replay_until = s2_ground_truth.GroundTruth()
        for s in mine:
            if s["recv_mono"] <= last.get("last_sample", -math.inf):
                replay_until.feed(s)
        if replay_until.last_sample is not None:
            replay_until.advance(replay_until.last_sample)
        if [(e["entered"], e["returned"]) for e in last.get("episodes") or []] != \
                [(e["entered"], e["returned"]) for e in replay_until.episodes]:
            problems.append("the last incremental evaluation disagrees with the replay up to the same sample")
    # episodes against the case
    start = marks.first("phase_start")
    ref0 = marks.first("reference_start")
    in_reference = [e for e in truth.episodes if ref0 and start and ref0["mono"] <= e["entered"] <= start["mono"]]
    if in_reference:
        problems.append("a local episode in the nominal reference")
    # the phase starts nominal (choice 2): the last three windows closed by then
    # below the recovery threshold, and no episode open
    if start:
        before = [w for w in truth.windows if w["end"] <= start["mono"]][-3:]
        open_at_start = [e for e in truth.episodes if e["entered"] <= start["mono"]
                         and (e["returned"] is None or e["returned"] > start["mono"])]
        out["nominal_at_phase_start"] = {"last_windows_p95": [w["p95"] for w in before],
                                         "episode_open": bool(open_at_start)}
        if len(before) < 3 or any(w["p95"] >= s2_ground_truth.RECOVERY_MS for w in before) or open_at_start:
            problems.append(f"not nominal before the phase (last windows {[w['p95'] for w in before]}, "
                            f"episode open {bool(open_at_start)})")
    phase_episodes = [e for e in truth.episodes if start and e["entered"] >= start["mono"]]
    out["phase_episodes"] = len(phase_episodes)
    high = None
    pulse = (data.get("_harness") or {}).get("pulse") or {}
    if pulse.get("high_request"):
        high = pulse["high_request"]["mono"]
    episode = None
    if case == "partition-only":
        if phase_episodes:
            problems.append(f"{len(phase_episodes)} local episode(s) in the negative control: its premise fails")
    else:
        after = [e for e in phase_episodes if high is not None and e["entered"] > high]
        if len(phase_episodes) != 1 or len(after) != 1:
            problems.append(f"expected one local episode after the pulse, found {len(phase_episodes)} in the phase")
        episode = after[0] if after else None
    out["episode"] = None if episode is None else episodes[truth.episodes.index(episode)]
    out["episode_valid"] = episode is not None
    if episode is not None:
        until = episode["returned"] if episode["returned"] is not None else math.inf
        local = [w for w in truth.windows if episode["first_violating_window_start"] <= w["start"] and w["end"] <= until]
        out["episode_windows_local"] = {"windows": len(local),
                                        "violating": sum(1 for w in local if w["p95"] > s2_ground_truth.VIOLATION_MS)}
    # wholly inside the cut (short/long): pulse and episode between the verification and the restore
    if case in ("short", "long"):
        verified, remove = marks.first("cut_verified"), marks.first("remove_start")
        restored_param = (pulse.get("high_interval") or {}).get("to", {}).get("mono")
        inside = (episode is not None and verified is not None and remove is not None and high is not None
                  and episode["returned"] is not None and restored_param is not None
                  and verified["mono"] < high and verified["mono"] < episode["first_violating_window_start"]
                  and episode["returned"] < remove["mono"] and restored_param < remove["mono"])
        out["inside_cut"] = inside
        if not inside:
            problems.append("episode not wholly inside the cut")
            out["episode_valid"] = False
    # coverage of the recorder, and the source stamps
    if case == "partition-only":
        verified, end = marks.first("cut_verified"), marks.first("remove_end")
        window = None if verified is None or end is None else (verified["mono"], end["mono"] + COVERAGE_MARGIN_SEC)
    elif episode is not None:
        horizon = tl["horizon"]["mono"] if tl.get("horizon") else math.inf
        window = (high - COVERAGE_MARGIN_SEC,
                  episode["returned"] + COVERAGE_MARGIN_SEC if episode["returned"] is not None else horizon)
    else:
        window = None
    # The onboard's silence to the window's end is excused only by the passage to the
    # edge proven within the horizon -- edge serving and onboard Inactive, not the edge
    # object alone -- and only if it began after the edge object was first seen: the
    # window then ends at the last onboard sample, every earlier gap still counted. The
    # proof may come after the window's end (decision after the eight-cell round's
    # cell 2: requiring it before made the verdict depend on the observation delay).
    truncated = False
    handover = handover or {}
    proven, edge_seen = handover.get("proven_mono"), handover.get("edge_first_seen_mono")
    if window is not None and proven is not None:
        times_all = [s["recv_mono"] for s in valid]
        silent_to_end = not any(t >= window[1] for t in times_all)
        last = max([t for t in times_all if t <= window[1]] or [None], key=lambda x: -math.inf if x is None else x)
        if silent_to_end and last is not None and edge_seen is not None and last >= edge_seen:
            window, truncated = (window[0], last), True
    out["coverage"] = {"window": window, "truncated_at_proven_handover": truncated}
    if window is None:
        problems.append("local recorder: no coverage window (no episode or no cut)")
    else:
        times = [s["recv_mono"] for s in valid]
        worst = gaps(times, window[0], window[1])
        out["coverage"]["max_gap_sec"] = None if math.isinf(worst) else round(worst, 3)
        if math.isinf(worst) or worst > SAMPLE_GAP_SEC:
            copy_errors = [r for r in data["copy_reads"] or [] if "error" in r]
            why = "copy read errors" if copy_errors else "no sample received (the recorder heard nothing)"
            problems.append(f"local recorder: gap {out['coverage']['max_gap_sec']} s > {SAMPLE_GAP_SEC} s "
                            f"in the evaluated window ({why})")
        inside = [s for s in valid if window[0] <= s["recv_mono"] <= window[1] and s.get("stamp") is not None]
        delays = [s["recv_utc"] - s["stamp"] for s in inside]
        backwards = sum(1 for a, b in zip(inside, inside[1:]) if b["stamp"] < a["stamp"])
        out["coverage"]["max_source_delay_sec"] = round(max(delays), 3) if delays else None
        out["coverage"]["source_stamp_backwards"] = backwards
        if (delays and max(delays) > SOURCE_DELAY_SEC) or backwards:
            problems.append("source stamps: delay above 2 s or going back: the transient's placement is not reliable")
    out["_episode_raw"] = episode
    return out


# ---- the central side: A --------------------------------------------------------

def log_lines(records):
    """Lines of a followed log, deduplicated (a restarted follower reads the Pod's
    log again from the start), with the runtime's timestamp and the reception."""
    seen, out = set(), []
    for r in records or []:
        if r.get("kind") != "line":
            continue
        key = (r.get("pod_uid"), r.get("line"))
        if key in seen:
            continue
        seen.add(key)
        line = r.get("line") or ""
        stamp, _, text = line.partition(" ")
        ts = parse_iso(stamp)
        out.append({"ts": ts, "text": text if ts is not None else line, "m": r.get("m"), "w": r.get("w"),
                    "pod_uid": r.get("pod_uid")})
    return out


def log_coverage(records, expected_uid, end_mono, start_mono):
    """Continuous if the followed Pod never changed and a follow session was open
    from before the start to after the end (kubectl logs -f replays the Pod's log)."""
    starts = [r for r in records or [] if r.get("kind") == "follow_start"]
    if not starts:
        return {"ok": False, "reason": "never followed"}
    uids = sorted({r.get("pod_uid") for r in starts})
    if expected_uid and uids != [expected_uid]:
        return {"ok": False, "reason": f"followed Pods {uids}, expected {expected_uid}"}
    if not any(r["m0"] <= start_mono for r in starts):
        return {"ok": False, "reason": "not followed from before the phase"}
    last_seen = max([r.get("m") or r.get("m0") or -math.inf for r in records or []])
    ended = [r for r in records or [] if r.get("kind") == "follow_end"]
    open_at_end = last_seen >= end_mono or any(r["m"] >= end_mono for r in ended)
    if not open_at_end:
        return {"ok": False, "reason": "no follow session reached the horizon"}
    return {"ok": True, "pod_uids": uids, "sessions": len(starts)}


def a_central(data, gt, tl, marks, problems):
    run = data["run"]
    records = data["logs"].get(A_DISPATCHER)
    identities = run.get("identities") or {}
    start, horizon = tl["phase_start"], tl["horizon"]
    coverage = log_coverage(records, identities.get("dispatcher_pod_uid"),
                            horizon["mono"] if horizon else math.inf, start["mono"] if start else -math.inf)
    lines = log_lines(records)
    restarts = {tuple(sorted((p.get("restarts") or {}).items())) for r in ok_reads(data["inventory"])
                if start and horizon and start["mono"] <= r["m0"] <= horizon["mono"]
                for p in r.get("pods") or [] if (p.get("labels") or {}).get("app.kubernetes.io/name") == A_DISPATCHER}
    if len(restarts) > 1:
        coverage = {"ok": False, "reason": f"the dispatcher's container restarted in the phase ({sorted(restarts)})"}
    if identities.get("dispatcher_trace") != "1":
        coverage = {"ok": False, "reason": "the dispatcher trace was not verified enabled"}
    if any("operational event trace failed" in l["text"] for l in lines):
        coverage = {"ok": False, "reason": "the dispatcher trace failed (coverage lost)"}
    trace = [(l, event_trace.parse_line(l["text"])) for l in lines]
    trace = [(l, t) for l, t in trace if t is not None]
    received = [t for _, t in trace if t.get("trace") == "received"]
    admissions = {t.get("event_id"): t for _, t in trace if t.get("trace") == "admission"}
    mine = [t for t in received if t.get("robot_id") == ROBOT and t.get("event_type") == EVENT_TYPE]
    in_phase = [t for t in mine if start and t.get("recv_mono", -1) >= start["mono"]]
    episode = gt.get("episode")
    enter = recovered = None
    if episode is not None:
        lo = episode["first_violating_window_start"]["utc"] - EPISODE_MATCH_SEC
        hi = (episode["returned"] or {}).get("utc") or (horizon or {}).get("utc") or math.inf
        candidates = [t for t in in_phase if t.get("state") == STATE_ENTER
                      and t.get("source_stamp") is not None and lo <= t["source_stamp"] <= hi + EPISODE_MATCH_SEC]
        enter = candidates[0] if candidates else None
        if enter is not None:
            recovered = next((t for t in in_phase if t.get("state") == STATE_RECOVERED
                              and t.get("correlation_id") == enter.get("correlation_id")), None)
    restore_end = ((tl.get("restore") or {}).get("command_end") or {}).get("mono")

    def reception(t):
        if t is None:
            return None
        return {"event_id": t.get("event_id"), "correlation_id": t.get("correlation_id"),
                "source_stamp_utc": t.get("source_stamp"),
                "received": at(t.get("recv_mono"), t.get("recv_utc"), "dispatcher trace, before the admission"),
                "delay_sec": None if t.get("source_stamp") is None else round(t["recv_utc"] - t["source_stamp"], 3),
                "after_restore": None if restore_end is None else t.get("recv_mono", 0) > restore_end,
                "admission": admissions.get(t.get("event_id"))}
    enters = [t for t in in_phase if t.get("state") == STATE_ENTER]
    received_out = {"coverage": coverage, "enter": reception(enter), "recovered": reception(recovered),
                    "enter_events_in_phase": len(enters),
                    "other_robot_events": sorted({t.get("robot_id") for t in received if t.get("robot_id") != ROBOT})}
    if episode is not None and enter is None:
        received_out["enter_reason"] = ("no ENTER within the episode's interval" if coverage["ok"]
                                        else f"not observable: {coverage['reason']}")
    # recognition: the dispatcher's own answer to the Action request of that event
    def answers(cid):
        out = {}
        for l in lines:
            for kind, pattern in (("accepted", f"DeploymentRequest accepted for {cid}"),
                                  ("rejected", f"DeploymentRequest rejected for {cid}"),
                                  ("manager_unavailable", f"Application Manager unavailable for {cid}"),
                                  ("send_failed", f"Could not send incident {cid}")):
                if cid and pattern in l["text"] and kind not in out:
                    out[kind] = {"utc": l["ts"], "received": at(l["m"], l["w"], "dispatcher log follower"),
                                 "source": "dispatcher log (runtime timestamp)"}
            match = re.search(rf"Incident {re.escape(cid or '#')} completed as (\S+) \((\S+)\)", l["text"])
            if cid and match and "result" not in out:
                out["result"] = {"outcome": match.group(1), "final_phase": match.group(2), "utc": l["ts"]}
        return out
    # A line seen proves presence even with partial coverage; only an absence needs coverage.
    accepted_all = [l for l in lines if "DeploymentRequest accepted for " in l["text"]]
    recognized = {"coverage": coverage, "requests_accepted_in_log": len(accepted_all)}
    if enter is None:
        if not coverage["ok"]:
            recognized.update(value=None, reason=f"not observable: {coverage['reason']}")
        else:
            recognized.update(value=False if episode is not None else None,
                              reason="no ENTER received for the episode" if episode is not None else "no episode")
    else:
        got = answers(enter.get("correlation_id"))
        recognized["answers"] = got
        accepted = got.get("accepted")
        horizon_utc = (horizon or {}).get("utc") or math.inf
        within = bool(accepted and accepted["utc"] is not None and accepted["utc"] <= horizon_utc)
        recognized["at"] = accepted
        if within:
            recognized["value"] = True
        elif accepted or coverage["ok"]:
            recognized["value"] = False
            recognized["reason"] = ("accepted after the horizon" if accepted else
                                    "Action rejected" if "rejected" in got else
                                    "Application Manager unavailable" if "manager_unavailable" in got else
                                    "request not sent" if "send_failed" in got else
                                    "no answer in the dispatcher log")
        else:
            recognized.update(value=None, reason=f"not observable: {coverage['reason']}")
    recognized["correlation_id"] = None if enter is None else enter.get("correlation_id")
    # incidents in the phase for the negative control: any accepted request, whatever the event
    phase_accepted = [l for l in accepted_all if start and (l["m"] or 0) >= start["mono"]]
    recognized["accepted_in_phase"] = [l["text"].split("for ", 1)[-1] for l in phase_accepted]
    return received_out, recognized, lines


# ---- the central side: B --------------------------------------------------------

def module_of(read, name):
    return next((m for m in read.get("rosmodules") or [] if m.get("name") == name), None)


def policy_of(read):
    return next((p for p in read.get("policies") or [] if p.get("name") == B_POLICY), None)


def b_central(data, gt, tl, marks, problems):
    reads = ok_reads(data["inventory"])
    start, horizon = tl["phase_start"], tl["horizon"]
    s_mono = start["mono"] if start else -math.inf
    h_mono = horizon["mono"] if horizon else math.inf
    coverage = read_coverage(data["inventory"], s_mono, h_mono)
    policies = [(r, policy_of(r)) for r in reads]
    uids = sorted({p.get("uid") for _, p in policies if p})
    if len(uids) != 1:
        coverage = {**coverage, "ok": False, "policy_uids": uids}
        problems.append(f"B policy: {len(uids)} identities in the run ({uids})")
    # Windows of the onboard module as the inventory saw them: the first read that
    # contained each (instance, seq). What these observations prove, and no more
    # (review of a41c087, choice 9): the controller counts only its reference
    # instance (the smallest live key) and starts a new cursor at the latest
    # window without counting it, so a cursor that passed a window does not prove
    # the window was evaluated, and a sequence missing from the reads does not
    # prove the API never had it. The exact count needs a controller trace.
    first_seen, previous = {}, None
    cursor_at, cursor_instances = {}, set()
    for r in reads:
        module = module_of(r, B_ONBOARD)
        cursor = (policy_of(r) or {}).get("windowCursor") or {}
        cursor_at[id(r)] = (cursor.get("instance"), parse_iso(cursor.get("end")))
        if cursor.get("instance") and r["m0"] >= s_mono:
            cursor_instances.add(cursor["instance"])
        for instance, v in ((module or {}).get("metricWindows") or {}).items():
            for w in v.get("windows") or []:
                key = (instance, w.get("seq"))
                if key not in first_seen:
                    first_seen[key] = {"window": w, "instance": instance, "read": r, "before": previous}
        previous = r
    windows = []
    for (instance, seq), f in sorted(first_seen.items(), key=lambda kv: (kv[0][0], kv[0][1] or 0)):
        end = parse_iso(f["window"].get("end"))
        cursor_passed = arrived_after_cursor = None
        if end is not None:
            before_instance, before_end = cursor_at.get(id(f["before"]), (None, None))
            later = [c for r in reads if r["m0"] >= f["read"]["m0"]
                     for c in [cursor_at[id(r)]] if c[0] == instance and c[1] is not None]
            if later:
                cursor_passed = any(c_end >= end for _, c_end in later)
            if before_instance == instance and before_end is not None:
                arrived_after_cursor = end > before_end
        windows.append({"instance": instance, "seq": seq, "start": f["window"].get("start"),
                        "end": f["window"].get("end"), "p95Ms": f["window"].get("p95Ms"),
                        "samples": f["window"].get("samples"),
                        "first_seen": between(f["before"], f["read"], "inventory (ROSModule status)"),
                        "reference_instance": instance in cursor_instances,
                        # both null outside the cursor's instance: nothing observed about it
                        "cursor_passed": cursor_passed, "arrived_after_cursor": arrived_after_cursor})
    phase_windows = [w for w in windows if w["first_seen"]["first_with"]["mono"] >= s_mono]
    by_instance = {}
    for w in windows:
        by_instance.setdefault(w["instance"], []).append(w["seq"])
    seq_not_observed = {}
    for instance, seqs in by_instance.items():
        seqs = sorted(s for s in seqs if isinstance(s, int))
        missing = sorted(set(range(seqs[0], seqs[-1] + 1)) - set(seqs)) if seqs else []
        if missing:
            seq_not_observed[instance] = missing
    episode = gt.get("episode")
    ep_windows, first_violating = [], None
    if episode is not None:
        lo = episode["first_violating_window_start"]["utc"] - EPISODE_MATCH_SEC
        hi = ((episode.get("returned") or {}).get("utc") or (horizon or {}).get("utc") or math.inf) + EPISODE_MATCH_SEC
        ep_windows = [w for w in windows if (parse_iso(w["end"]) or -math.inf) >= lo
                      and (parse_iso(w["start"]) or math.inf) <= hi]
        violating = [w for w in ep_windows if (w["p95Ms"] or 0) > 250.0]
        first_violating = min(violating, key=lambda w: w["first_seen"]["first_with"]["mono"]) if violating else None
    received = {"coverage": coverage, "windows_in_phase": len(phase_windows),
                "cursor_reference_instances": sorted(cursor_instances),
                "episode_windows": ep_windows,
                # sequences between observed ones that no inventory read contained
                "seq_not_observed": seq_not_observed,
                # reference-instance windows the cursor never passed; those first seen in
                # the last 5 s before the horizon are left out (a descriptive filter)
                "not_passed_by_cursor": [(w["instance"], w["seq"]) for w in phase_windows
                                         if w["cursor_passed"] is False
                                         and w["first_seen"]["first_with"]["mono"] <= h_mono - 5.0],
                "first_violating_window": first_violating,
                "local_violating_windows": (gt.get("episode_windows_local") or {}).get("violating")}
    if episode is not None and first_violating is None:
        received["reason"] = ("no violating window of the episode seen in the inventory reads" if coverage["ok"]
                              else "not observable: inventory coverage")
    # recognition: the first new correlationId, corroborated
    reference_cids = {p.get("correlationId") for r, p in policies if p and r["m1"] < s_mono}
    reference_cids.discard(None)
    reference_cids.discard("")
    new, before = None, None
    for r, p in policies:
        cid = (p or {}).get("correlationId")
        if r["m0"] >= s_mono and cid and cid not in reference_cids:
            new = (r, p)
            break
        before = r
    triggered = sum(1 for r, p in policies if p and p.get("state") == "Triggered" and not p.get("correlationId")
                    and r["m0"] >= s_mono)
    k8s_events = [e for e in (data["k8s_events"] or {}).get("items") or []
                  if (e.get("involvedObject") or {}).get("name") == B_POLICY]
    started_events = [e for e in k8s_events if e.get("reason") == "MigrationStarted"]
    audit_started = [x for x in audit_records(data["audit"]) if x["record"].get("record_type") == "incident_started"]
    recognized = {"coverage": coverage, "triggered_reads_without_incident": triggered,
                  "correlation_ids_in_phase": sorted({(p or {}).get("correlationId") for r, p in policies
                                                      if r["m0"] >= s_mono and (p or {}).get("correlationId")}
                                                     - reference_cids)}
    # A new correlationId seen and corroborated is proven whatever the coverage
    # elsewhere; only its absence needs the inventory's coverage (review of a41c087).
    if new is None and not coverage["ok"]:
        recognized.update(value=None, reason="not observable: inventory coverage")
    elif new is None:
        recognized.update(value=False if episode is not None else None,
                          reason="no new correlationId in the phase" if episode is not None else "no episode")
    else:
        r, p = new
        cid = p.get("correlationId")
        corroborated = {"event_MigrationStarted": [{"firstTimestamp": e.get("firstTimestamp"),
                                                    "lastTimestamp": e.get("lastTimestamp"),
                                                    "count": e.get("count")} for e in started_events],
                        "audit_incident_started": any(x["record"].get("correlation_id") == cid for x in audit_started)}
        ok = bool(corroborated["event_MigrationStarted"]) or corroborated["audit_incident_started"]
        horizon_utc = (horizon or {}).get("utc") or math.inf
        recognized.update(correlation_id=cid, state=p.get("state"), policy_uid=p.get("uid"),
                          at=between(before, r, "inventory (AdaptationPolicy status)"),
                          corroborated=corroborated,
                          value=(r["w0"] <= horizon_utc) if ok else None)
        if not ok:
            recognized["reason"] = "new correlationId without MigrationStarted or incident_started"
    return received, recognized


# ---- audit, action ----------------------------------------------------------

def audit_records(audit):
    out, previous = [], None
    for r in audit or []:
        if "error" in r or "m1" not in r:
            continue
        for item in r.get("new") or []:
            if "record" in item:
                out.append({"record": item["record"], "read": r, "before": previous})
        previous = r
    return out


def audit_section(data, cid, recognized_value, tl):
    start, horizon = tl["phase_start"], tl["horizon"]
    coverage = read_coverage(data["audit"], start["mono"] if start else -math.inf,
                             horizon["mono"] if horizon else math.inf)
    out = {"coverage": coverage, "correlation_id": cid}
    if cid is None:
        out["records"] = unknown("no correlation id to look for")
        return out
    records = {}
    for x in audit_records(data["audit"]):
        record = x["record"]
        if record.get("correlation_id") != cid:
            continue
        kind = record.get("record_type")
        if kind == "incident_feedback":
            kind = f"incident_feedback_{record.get('phase')}"
        if kind not in records:
            records[kind] = {"producer_timestamp": record.get("timestamp_utc"),
                             "available": between(x["before"], x["read"], "audit writer (first read with it)"),
                             "fields": {k: record.get(k) for k in ("success", "outcome", "final_phase",
                                                                    "rollback_performed", "robot_id", "event_type",
                                                                    "policy_id")}}
    out["records"] = records
    horizon_mono = horizon["mono"] if horizon else math.inf
    started = records.get("incident_started")
    if started is None or started["available"]["first_with"]["mono"] > horizon_mono:
        if recognized_value:
            out["incident_started"] = ("not observed within the horizon" if coverage["ok"]
                                       else "not observable: audit coverage")
    outbox = data["manager_outbox"]
    if isinstance(outbox, list):
        out["manager_outbox_pending"] = [o for o in outbox if o.get("correlation_id") == cid]
    return out


def first_object(reads, present):
    """(read without, first read with) of a condition on the inventory, and the uids."""
    before = None
    for r in reads:
        if present(r):
            return before, r
        before = r
    return None, None


def a_action(data, recognized, lines, tl):
    reads = ok_reads(data["inventory"])
    cid = recognized.get("correlation_id")
    start = tl["phase_start"]
    s_mono = start["mono"] if start else -math.inf
    phase_reads = [r for r in reads if r["m0"] >= s_mono]
    edge_uids = sorted({d["uid"] for r in phase_reads for d in r.get("deployments") or [] if d["name"] == A_EDGE})
    before, first = first_object(phase_reads, lambda r: any(d["name"] == A_EDGE for d in r.get("deployments") or []))
    acting = next((l for l in lines if re.search(r"Policy phase=1 ", l["text"])
                   and start and (l["m"] or 0) >= s_mono), None)
    answers = recognized.get("answers") or {}
    result = answers.get("result")
    completed = next((x["record"] for x in audit_records(data["audit"]) if cid
                      and x["record"].get("correlation_id") == cid
                      and x["record"].get("record_type") == "incident_completed"), None)
    outcome, source = None, None
    if result:
        final, source = result["final_phase"], "dispatcher log (Action result)"
    elif completed:
        final, source = completed.get("final_phase"), "audit incident_completed"
        result = {k: completed.get(k) for k in ("outcome", "final_phase", "success", "rollback_performed")}
    if source:
        outcome = "migrated" if final == "STABLE" else "rolled_back" if final == "ROLLED_BACK" else "other"
    elif edge_uids or acting or answers.get("accepted"):
        # started, no terminal outcome seen: in progress only if both sources were covered
        audit_cov = read_coverage(data["audit"], s_mono, tl["horizon"]["mono"] if tl["horizon"] else math.inf)
        outcome = "in_progress" if (recognized.get("coverage") or {}).get("ok") and audit_cov["ok"] else "unknown"
    return {"requested": None if acting is None else {"utc": acting["ts"], "source": "dispatcher log, feedback ACTING",
                                                      "received": at(acting["m"], acting["w"], "log follower")},
            "first_mutation": None if first is None else {
                **between(before, first, "inventory (edge Deployment created by KubeROS)"),
                "uid": next(d["uid"] for d in first["deployments"] if d["name"] == A_EDGE)},
            "edge_deployment_uids": edge_uids, "attempts": len(edge_uids),
            "result": result, "outcome_source": source, "outcome": outcome, "correlation_id": cid}


def b_action(data, recognized, tl):
    reads = ok_reads(data["inventory"])
    start = tl["phase_start"]
    s_mono = start["mono"] if start else -math.inf
    phase_reads = [r for r in reads if r["m0"] >= s_mono]
    edge_uids = sorted({m["uid"] for r in phase_reads for m in r.get("rosmodules") or [] if m["name"] == B_EDGE})
    before, first = first_object(phase_reads, lambda r: module_of(r, B_EDGE) is not None)
    last = phase_reads[-1] if phase_reads else None
    state = (policy_of(last) or {}).get("state") if last else None
    outcome = {"Recovered": "migrated", "RolledBack": "rolled_back"}.get(state)
    if outcome is None and state not in (None, "Nominal", "Triggered"):
        outcome = "in_progress" if state in ("Migrating", "FallingBack") else "other"
    return {"requested": recognized.get("at"),
            "first_mutation": None if first is None else {
                **between(before, first, "inventory (edge ROSModule created by the controller)"),
                "uid": module_of(first, B_EDGE)["uid"]},
            "edge_module_uids": edge_uids, "attempts": len(edge_uids),
            "policy_state_at_horizon": state, "outcome": outcome,
            "correlation_id": recognized.get("correlation_id")}


def order_vs_return(interval, returned_utc, uncertainty):
    """before / after (late) / unresolved, with both uncertainties."""
    if interval is None or returned_utc is None:
        return None
    lo, hi = interval
    if hi + uncertainty < returned_utc:
        return "before the local return"
    if lo - uncertainty > returned_utc:
        return "after the local return (late action)"
    return "temporal order not resolved"


def late_action(action, gt, clock_bound):
    episode = gt.get("episode")
    returned = (episode or {}).get("returned") or {}
    out = {}
    if not returned:
        out["reason"] = "no local return observed (no episode, or the onboard stopped publishing before it)"
    requested = action.get("requested")
    if requested is not None:
        if "first_with" in requested:
            lo = (requested.get("last_without") or {}).get("utc", requested["first_with"]["utc"])
            interval = (lo, requested["first_with"]["read_end_utc"])
        else:
            interval = (requested["utc"], requested["utc"]) if requested.get("utc") is not None else None
        out["request"] = order_vs_return(interval, returned.get("utc"), clock_bound)
    mutation = action.get("first_mutation")
    if mutation is not None:
        lo = (mutation.get("last_without") or {}).get("utc", mutation["first_with"]["utc"])
        out["first_mutation"] = order_vs_return((lo, mutation["first_with"]["read_end_utc"]), returned.get("utc"),
                                                clock_bound)
    out["note"] = "Pod creation is not the controller's decision"
    return out


# ---- reconvergence ------------------------------------------------------------

MISSING = "no observation: "      # a reason that is a missing measurement, not an observed fact


def real_reasons(reasons):
    return [r for r in reasons if not r.startswith(MISSING)]


def latest_health(health, target, until_utc, fresh_sec=HEALTH_GAP_SEC):
    found = None
    for h in health or []:
        if h.get("event") == "health" and h.get("target") == target and h.get("result") != "error" \
                and h.get("outcome_utc") is not None and h["outcome_utc"] <= until_utc:
            found = h
    if found is None or until_utc - found["outcome_utc"] > fresh_sec:
        return None
    return found


def pods_of(read, variant, module):
    key = "pod-name" if variant == "a" else OWNER_LABEL
    return [p for p in read.get("pods") or [] if (p.get("labels") or {}).get(key) == module and not p.get("terminating")]


def a_serving(read, samples, health, instance):
    module = A_EDGE if instance == "edge" else A_ONBOARD
    pods = pods_of(read, "a", module)
    t = read["w1"]
    component = EDGE_COMPONENT if instance == "edge" else ONBOARD_COMPONENT
    fresh = [s for s in samples if s.get("robot_id") == ROBOT and s.get("component") == component
             and t - FRESH_SEC <= s.get("recv_utc", -1) <= t]
    answer = latest_health(health, f"drone01-{instance}", t)
    reasons = []
    if not pods or not all(p["ready"] for p in pods):
        reasons.append(f"{module}: Pods {len(pods)}, not all Ready")
    if not fresh or fresh[-1]["latency_ms"] >= NOMINAL_MS:
        reasons.append(f"{instance}: no fresh nominal sample")
    if answer is None:
        reasons.append(f"{MISSING}{instance} health older than {HEALTH_GAP_SEC:.0f} s")
    elif answer.get("result") != "positive":
        reasons.append(f"{instance}: health {answer.get('result')} ({answer.get('reason')})")
    return reasons, {"replicas": len(pods), "health_single_responder": len(pods) > 1}


def a_inactive(read, health):
    """The onboard's own answer: identity drone01/onboard, lifecycle 'inactive' (an
    inactive analytics also answers healthy=False: the prober's reason is then
    'unhealthy', so the answer's lifecycle is what is read)."""
    answer = latest_health(health, "drone01-onboard", read["w1"])
    if answer is None:
        return [f"{MISSING}onboard health older than {HEALTH_GAP_SEC:.0f} s"]
    fields = answer.get("answer") or {}
    if (fields.get("robot_id"), fields.get("instance_id"), fields.get("lifecycle_state")) != \
            (ROBOT, "onboard", "inactive"):
        return [f"onboard not proven Inactive ({None if answer is None else (answer.get('result'), fields)})"]
    return []


def b_instances(read, module_name):
    module = module_of(read, module_name)
    pods = pods_of(read, "b", module_name)
    return module, pods


def b_serving(read, module_name):
    module, pods = b_instances(read, module_name)
    reasons = []
    if module is None:
        return [f"{module_name} absent"], {"replicas": 0}
    if not pods:
        reasons.append(f"{module_name}: no current Pod")
    lifecycle = module.get("lifecycleInstances") or {}
    windows = module.get("metricWindows") or {}
    for pod in pods:
        suffix = pod["name"].replace("-", "_")
        key = next((k for k in lifecycle if k.endswith(suffix)), None)
        if not pod["ready"]:
            reasons.append(f"{pod['name']} not Ready")
        if key is None or (lifecycle[key] or {}).get("observedLifecycleState") != "Active":
            reasons.append(f"{pod['name']}: instance not Active")
        wkey = next((k for k in windows if k.endswith(suffix)), None)
        last = ((windows.get(wkey) or {}).get("windows") or [None])[-1] if wkey else None
        end = parse_iso((last or {}).get("end"))
        if last is None or end is None or read["w0"] - end > FRESH_SEC or (last.get("p95Ms") or 0) >= NOMINAL_MS:
            reasons.append(f"{pod['name']}: no fresh nominal window")
    return reasons, {"replicas": len(pods)}


def b_inactive(read, module_name):
    module = module_of(read, module_name)
    if module is None:
        return [f"{module_name} absent"]
    states = [(v or {}).get("observedLifecycleState") for v in (module.get("lifecycleInstances") or {}).values()]
    if not states or any(s != "Inactive" for s in states):
        return [f"{module_name} not Inactive ({states})"]
    return []


def condition(variant, mode, read, samples, health):
    if variant == "a":
        if mode == "migrated":
            reasons, info = a_serving(read, samples, health, "edge")
            return reasons + a_inactive(read, health), info
        reasons, info = a_serving(read, samples, health, "onboard")
        if mode == "rolled_back" and any(d["name"] == A_EDGE for d in read.get("deployments") or []):
            reasons.append("edge Deployment still present after the rollback")
        return reasons, info
    if mode == "migrated":
        reasons, info = b_serving(read, B_EDGE)
        return reasons + b_inactive(read, B_ONBOARD), info
    reasons, info = b_serving(read, B_ONBOARD)
    if mode == "rolled_back" and module_of(read, B_EDGE) is not None:
        reasons.append("edge ROSModule still present after the rollback")
    return reasons, info


def reconvergence_section(data, variant, action, tl, marks, pulse_confirmed, anomalies):
    reads = ok_reads(data["inventory"])
    samples = [s for s in data["samples"] or [] if s.get("event") == "sample"]
    health = data["health"] or []
    outcome = action.get("outcome")
    mode = {"migrated": "migrated", "rolled_back": "rolled_back", None: "none"}.get(outcome)
    horizon = tl["horizon"]
    h_mono = horizon["mono"] if horizon else math.inf
    if tl.get("restore"):
        from_mono = tl["restore"]["command_end"]["mono"]
    else:
        programmed = tl.get("pulse_programmed")
        start = tl["phase_start"]
        from_mono = (start["mono"] + programmed["end_utc"] - start["utc"]) if programmed and start else \
            (start["mono"] if start else -math.inf)
    out = {"expected": mode, "action_outcome": outcome, "from": at(from_mono, None, "stimulus end"),
           "policy_state": action.get("policy_state_at_horizon")}
    coverage = read_coverage(data["inventory"], from_mono, h_mono)
    out["coverage"] = coverage
    if outcome == "unknown":
        out.update(value=None, reason="the action's outcome is not observable", status="unknown")
        return out
    if mode is None:
        out.update(value=False, reason=f"action {outcome} at the horizon: not reconverged")
        out["status"] = "fail" if coverage["ok"] and pulse_confirmed else "unknown"
        return out
    candidates = [r for r in reads if from_mono <= r["m0"] and r["m1"] <= h_mono]
    evaluated = [(r, *condition(variant, mode, r, samples, health)) for r in candidates]
    run_of, found, last_reasons = [], None, None
    for r, reasons, info in evaluated:
        if reasons:
            run_of, last_reasons = [], reasons
            continue
        run_of.append((r, info))
        if len(run_of) >= RECONVERGE_READS and run_of[-1][0]["w0"] - run_of[-RECONVERGE_READS][0]["w0"] >= \
                RECONVERGE_SPAN_SEC:
            found = run_of[-RECONVERGE_READS:]
            break
    # The state at the horizon, and every loss after the first recovery (review of
    # a41c087, choice 10): a recovery followed by a loss is not hidden by it. A loss
    # is an observed fact; a missing observation is not a loss.
    tail = evaluated[-RECONVERGE_READS:]
    if len(tail) < RECONVERGE_READS:
        state = "unknown"
    elif all(not reasons for _, reasons, _ in tail):
        state = "in_service"
    elif all(real_reasons(reasons) for _, reasons, _ in tail) and \
            tail[-1][0]["w0"] - tail[0][0]["w0"] >= RECONVERGE_SPAN_SEC:
        state = "lost"
    elif all(reasons and not real_reasons(reasons) for _, reasons, _ in tail):
        state = "unknown"
    else:
        state = "mixed"
    out["at_horizon"] = {"state": state, "reads": [
        {"at": at(r["m0"], r["w0"], "inventory"), "reasons": reasons} for r, reasons, _ in tail]}
    losses, current = [], None
    if found:
        for r, reasons, _ in evaluated:
            if r["m0"] <= found[-1][0]["m0"]:
                continue
            if real_reasons(reasons):
                if current is None:
                    current = {"from": at(r["m0"], r["w0"], "inventory"), "reads": 0,
                               "reasons": real_reasons(reasons)[:3]}
                current["reads"] += 1
                current["to"] = at(r["m1"], r["w1"], "inventory")
            elif not reasons and current is not None:
                losses.append(current)
                current = None
        if current is not None:
            losses.append(current)
    out["losses_after_recovery"] = losses
    if losses:
        anomalies.append({"kind": "service lost after the recovery", "detail": losses[:5]})
    if found:
        first = found[0][0]
        out.update(value=True, at=at(first["m0"], first["w0"], "inventory, first of three consecutive reads"),
                   replicas=found[-1][1].get("replicas"),
                   health_single_responder=found[-1][1].get("health_single_responder", False),
                   status="ok")
        if variant == "a":
            out["routing_configmap"] = found[-1][0].get("routing")
        if state == "lost":
            out["reason"] = "recovered, then lost: not in service at the horizon"
            out["status"] = "fail" if coverage["ok"] and pulse_confirmed else "unknown"
    else:
        out.update(value=False, reason=f"no three consecutive reads within the horizon ({last_reasons})")
        out["status"] = "fail" if coverage["ok"] and pulse_confirmed else "unknown"
    return out


def handover_proof(data, variant, action, tl):
    """The first instant, within the horizon, the passage to the edge is proven
    (edge serving, onboard Inactive), and when the edge object was first seen: the
    only silence of the onboard samples the recorder may excuse."""
    if action.get("outcome") != "migrated" or action.get("first_mutation") is None:
        return None
    samples = [s for s in data["samples"] or [] if s.get("event") == "sample"]
    horizon = (tl.get("horizon") or {}).get("mono", math.inf)
    for r in ok_reads(data["inventory"]):
        if r["m1"] > horizon:
            break
        reasons, _ = condition(variant, "migrated", r, samples, data["health"] or [])
        if not reasons:
            return {"proven_mono": r["m0"], "edge_first_seen_mono": action["first_mutation"]["first_with"]["mono"]}
    return None


# ---- isolation ----------------------------------------------------------------

def isolation_section(data, variant, tl, a_lines, anomalies):
    """drone02/drone03, three things kept apart (review of a41c087, choice 11):
      availability     health answers, Pods (UID, restarts, Ready) -- PX4 is its
                       own dimension;
      actions          no incident or action addressed to them;
      status_freshness B only: their ROSModule status seen fresh (<= 6 s) with
                       every instance Active -- a requirement on the control plane's
                       view, a documented excess is a freshness FAIL, not by itself
                       proof that the drone was unavailable.
    An ENTER of theirs reaching the dispatcher without an action is a signal
    anomaly, recorded, not an action. A read that failed is a gap (unknown)."""
    start, horizon = tl["phase_start"], tl["horizon"]
    s_mono = start["mono"] if start else -math.inf
    h_mono = horizon["mono"] if horizon else math.inf
    reads = ok_reads(data["inventory"])
    phase_reads = [r for r in reads if s_mono <= r["m0"] <= h_mono]
    baseline = next((r for r in reversed(reads) if r["m1"] <= s_mono), None)
    coverage = read_coverage(data["inventory"], s_mono, h_mono)
    availability = {"violations": [], "unknown": []}
    actions = {"violations": [], "unknown": []}
    freshness = {"violations": [], "unknown": []}
    if not coverage["ok"]:
        for part in (availability, actions, freshness):
            part["unknown"].append(f"inventory coverage (max gap {coverage['max_gap_sec']} s)")

    def workloads(read):
        return {p["name"]: (p["uid"], tuple(sorted((p.get("restarts") or {}).items())), p["ready"])
                for p in read.get("pods") or [] if robot_of(p) in UNINVOLVED}
    if baseline is None:
        availability["unknown"].append("no inventory read before the phase")
    else:
        base = workloads(baseline)
        for r in phase_reads:
            now = workloads(r)
            changed = sorted(n for n in set(base) | set(now) if (base.get(n) or (None,))[:2] != (now.get(n) or (None,))[:2])
            not_ready = sorted(n for n, v in now.items() if not v[2])
            if changed:
                availability["violations"].append(f"drone02/03 workloads changed (UID/restarts) at {r['w0']:.1f}: "
                                                  f"{changed[:4]}")
                break
            if not_ready:
                availability["violations"].append(f"drone02/03 Pods not Ready at {r['w0']:.1f}: {not_ready[:4]}")
                break
    health = data["health"] or []
    per = {}
    for robot in UNINVOLVED:
        target = f"{robot}-onboard"
        mine = [h for h in health if h.get("event") == "health" and h.get("target") == target]
        observed = [h for h in mine if h.get("result") != "error"]
        worst = gaps([h["outcome_mono"] for h in observed], s_mono, h_mono)
        bad = [h for h in observed if s_mono <= h["outcome_mono"] <= h_mono and h["result"] != "positive"]
        per[robot] = {"observations": sum(1 for h in observed if s_mono <= h["outcome_mono"] <= h_mono),
                      "collector_errors": sum(1 for h in mine if h.get("result") == "error"
                                              and s_mono <= h.get("outcome_mono", -1) <= h_mono),
                      "max_gap_sec": None if math.isinf(worst) else round(worst, 3),
                      "not_positive": [(h["result"], h.get("reason")) for h in bad][:5]}
        if bad:
            availability["violations"].append(f"{robot}: health {bad[0]['result']} ({bad[0].get('reason')})")
        if math.isinf(worst) or worst > HEALTH_GAP_SEC:
            availability["unknown"].append(f"{robot}: health gap {per[robot]['max_gap_sec']} s > {HEALTH_GAP_SEC} s")
    availability["health"] = per
    # actions addressed to them
    for x in audit_records(data["audit"]):
        record = x["record"]
        if record.get("record_type") == "incident_started" and record.get("robot_id") in UNINVOLVED:
            actions["violations"].append(f"incident for {record.get('robot_id')} in the audit")
    if variant == "a":
        robot_of_cid = {}
        for line in a_lines:
            parsed = event_trace.parse_line(line["text"])
            if parsed and parsed.get("trace") == "received":
                robot_of_cid.setdefault(parsed.get("correlation_id"), parsed.get("robot_id"))
                if parsed.get("robot_id") in UNINVOLVED and parsed.get("state") == STATE_ENTER \
                        and parsed.get("recv_mono", -1) >= s_mono:
                    anomalies.append({"kind": "ENTER of another robot reached the dispatcher (signal isolation)",
                                      "detail": {k: parsed.get(k) for k in ("robot_id", "event_id", "correlation_id",
                                                                             "recv_utc", "source_stamp")}})
        for line in a_lines:
            if "DeploymentRequest accepted for " in line["text"] and (line["m"] or 0) >= s_mono:
                cid = line["text"].split("DeploymentRequest accepted for ", 1)[1].strip()
                if robot_of_cid.get(cid) in UNINVOLVED:
                    actions["violations"].append(f"request accepted for {robot_of_cid[cid]} ({cid})")
        freshness["status"] = "not_applicable"
    else:
        for r in phase_reads:
            edges = [m["name"] for m in r.get("rosmodules") or []
                     if m["name"].endswith("-edge") and any(u in m["name"] for u in UNINVOLVED)]
            if edges:
                actions["violations"].append(f"edge modules for drone02/03: {edges}")
                break
        for r in phase_reads:
            found = []
            for robot in UNINVOLVED:
                module = module_of(r, f"companion-analytics-{robot}")
                if module is None:
                    found.append(f"{robot}: ROSModule absent from a successful read at {r['w0']:.1f}")
                    continue
                states = [(v or {}).get("observedLifecycleState")
                          for v in (module.get("lifecycleInstances") or {}).values()]
                ends = [parse_iso(w.get("end")) for v in (module.get("metricWindows") or {}).values()
                        for w in (v or {}).get("windows") or []]
                ends = [e for e in ends if e is not None]
                if not states or any(s != "Active" for s in states):
                    found.append(f"{robot}: status shows instances {states} at {r['w0']:.1f}")
                elif not ends or r["w0"] - max(ends) > FRESH_SEC:
                    age = None if not ends else round(r["w0"] - max(ends), 1)
                    found.append(f"{robot}: status freshness {age} s > {FRESH_SEC} s at {r['w0']:.1f}")
            if found:
                freshness["violations"] += found
                break
    out = {"coverage": coverage}
    for name, part in (("availability", availability), ("actions", actions), ("status_freshness", freshness)):
        if part.get("status") != "not_applicable":
            part["status"] = "fail" if part["violations"] else ("unknown" if part["unknown"] else "ok")
        out[name] = part
    statuses = [part["status"] for part in (availability, actions, freshness)]
    out["status"] = "fail" if "fail" in statuses else ("unknown" if "unknown" in statuses else "ok")
    out["violations"] = [f"{name}: {v}" for name, part in (("availability", availability), ("actions", actions),
                                                           ("status freshness", freshness))
                         for v in part["violations"]]
    out["unknown"] = [f"{name}: {v}" for name, part in (("availability", availability), ("actions", actions),
                                                        ("status freshness", freshness)) for v in part["unknown"]]
    return out


# ---- PX4 ------------------------------------------------------------------------

def px4_section(data, marks, tl):
    frozen = (marks.first("px4_frozen") or {}).get("states") or {}
    start, horizon = tl["phase_start"], tl["horizon"]
    out, fails, unknowns = {}, [], []
    if not start or not horizon:
        return {"status": "unknown", "reason": "no phase window"}
    start_ns, end_ns = int(start["utc"] * 1e9), int(horizon["utc"] * 1e9)
    for drone in DRONES:
        text = data["px4"].get(drone)
        state = frozen.get(drone)
        if text is None:
            out[drone] = {"verdict": "inconclusive", "reasons": "no uORB file"}
            unknowns.append(drone)
            continue
        if not state or "error" in state:
            out[drone] = {"verdict": "inconclusive", "reasons": "no frozen nominal state"}
            unknowns.append(drone)
            continue
        start_state = {k: state[k] for k in ("arming_state", "nav_state", "failsafe") if k in state}
        reads = px4_status_continuity.parse_reads(text)
        markers, stats = px4_status_continuity.markers_from_reads(reads)
        result = mission_continuity.evaluate(markers, start_ns, end_ns, "0", start_state=start_state)
        inconclusive = []
        ok_reads_ns = [ns for ns, s in reads if s is not None]
        before = [ns for ns in ok_reads_ns if ns < start_ns]
        after = [ns for ns in ok_reads_ns if ns > end_ns]
        inside = [ns for ns in ok_reads_ns if start_ns <= ns <= end_ns]
        edges = ([before[-1]] if before else []) + inside + ([after[0]] if after else [])
        read_gap = max(((b - a) // 1_000_000 for a, b in zip(edges, edges[1:])), default=None)
        if not before or not after or read_gap is None or read_gap > px4_status_continuity.READ_GAP_LIMIT_MS:
            inconclusive.append(f"reads do not cover the window (max gap {read_gap} ms)")
        new_inside = [ns for ns in stats["new_sample_times"] if start_ns <= ns <= end_ns]
        periods = sorted((b - a) // 1_000_000 for a, b in zip(new_inside, new_inside[1:]))
        median = periods[len(periods) // 2] if periods else None
        if median is None or median > px4_status_continuity.SOURCE_PERIOD_LIMIT_MS:
            inconclusive.append(f"source period {median} ms above the limit")
        verdict = result["verdict"]
        if verdict == "true" and inconclusive:
            verdict = "inconclusive"
        # a status gap is PX4's silence only if the reads covered it; a change of
        # state or a clock going back stays an observed failure (choice 13)
        if verdict == "false" and inconclusive and \
                all(f.strip().startswith("status gap") for f in result["reasons"].split(";")):
            verdict = "inconclusive"
        out[drone] = {"verdict": verdict, "frozen_state": start_state,
                      "reasons": "; ".join(inconclusive + ([result["reasons"]] if result["reasons"] != "none" else []))
                      or "none", "max_read_gap_ms": read_gap, "median_source_period_ms": median,
                      "new_samples": stats["new_samples"], "repeated_reads": stats["repeated_reads"],
                      "failed_reads": stats["failed_reads"], "transitions": result["transitions"]}
        if verdict == "false":
            fails.append(drone)
        elif verdict != "true":
            unknowns.append(drone)
    # PX4 Pods: same UID and restarts from before the phase to the horizon
    reads = ok_reads(data["inventory"])
    baseline = next((r for r in reversed(reads) if r["m1"] <= start["mono"]), None)

    def px4(read):
        return {p["name"]: (p["uid"], tuple(sorted((p.get("restarts") or {}).items())))
                for p in read.get("pods") or [] if "px4-sitl" in (p.get("name") or "")}
    if baseline is not None:
        base = px4(baseline)
        for r in reads:
            if start["mono"] <= r["m0"] <= horizon["mono"] and px4(r) != base:
                fails.append(f"PX4 Pods changed at {r['w0']:.1f}")
                break
    else:
        unknowns.append("no inventory before the phase for PX4 Pods")
    status = "fail" if fails else ("unknown" if unknowns else "ok")
    return {"status": status, "drones": out, "failures": fails, "unknown": unknowns}


# ---- provenance -------------------------------------------------------------

def provenance_section(data, problems, cell=True):
    run = data["run"]
    prov = dict(run.get("provenance") or {})
    if run.get("protocol_id") != PROTOCOL_ID:
        problems.append(f"protocol_id {run.get('protocol_id')}, expected {PROTOCOL_ID}")
    if prov.get("source_dirty"):
        problems.append(f"inputs not as declared: source modified ({prov['source_dirty'][:5]})")
    if prov.get("inputs_start") is None or prov.get("inputs_start") != prov.get("inputs_end"):
        problems.append("inputs changed during the run (or not recorded)")
    images = prov.get("images") or {}
    mismatched = []
    for label in ("pods_start", "pods_end"):
        pods = data[label]
        if pods is None:
            problems.append(f"{label.replace('_', '-')}.json missing")
            continue
        for pod in pods.get("items") or []:
            for c in (pod.get("status") or {}).get("containerStatuses") or []:
                name = (c.get("image") or "").replace("docker.io/library/", "").replace("docker.io/", "")
                if name in images and c.get("imageID") and c["imageID"] != images[name]:
                    mismatched.append(f"{pod['metadata']['name']}/{c.get('name')}")
    if mismatched:
        problems.append(f"images not as declared: {sorted(set(mismatched))[:5]}")
    prov["image_mismatches"] = sorted(set(mismatched))
    # a cell runs on a qualified bench, on the qualified topology (R11, choice 5)
    record = (run.get("qualification") or {}).get("record") or {}
    qualified = record.get("qualified_topology") or {}
    prov["qualification"] = {"dir": (run.get("qualification") or {}).get("dir"), "verdict": record.get("verdict")}
    if not cell:
        pass                                # a qualification qualifies itself
    elif record.get("verdict") != "QUALIFIED":
        problems.append("no qualified bench for this cell")
    else:
        topology = run.get("topology") or {}
        # literal, except the order of the k3s server's arguments (k3s_server_args: the
        # sorted list; the raw line is recorded, not compared)
        for key in ("variant", "peers", "server_version", "k3s_server_args", "roles", "system_pins",
                    "system_placement"):
            if topology.get(key) != qualified.get(key):
                problems.append(f"topology not the qualified one: {key} {topology.get(key)!r} "
                                f"vs {qualified.get(key)!r}"[:300])
        if qualified.get("bench") != (prov.get("inputs_start") or {}).get("bench"):
            problems.append("bench files not the qualified ones")
        # the application images, built for this cell, against the qualification of the
        # same variant: the bench's files alone do not certify them (eight-cell
        # pre-registration); a difference keeps the evidence, not the attribution
        built, qualified_images = images, qualified.get("images") or {}
        differ = sorted(i for i in set(built) | set(qualified_images) if built.get(i) != qualified_images.get(i))
        prov["images_vs_qualification"] = differ
        if differ:
            problems.append(f"images not those of the qualification: {differ}")
    # the partition cuts drone01's workloads only: at the start a precondition of the
    # runner, at the end checked here (decision after the second eight-cell round)
    if cell:
        end = data.get("placement_end")
        prov["placement_end"] = None if end is None else {"foreign": end.get("foreign"), "system": end.get("system")}
        if end is None:
            problems.append("placement at the end not recorded")
        elif end.get("foreign"):
            problems.append("a Pod other than drone01's on the isolated node at the end: "
                            f"{[p['namespace'] + '/' + p['name'] for p in end['foreign']]}")
    e0 = run.get("e0") or {}
    if e0.get("exit") != 0:
        problems.append(f"E0 bootstrap not passed (exit {e0.get('exit')})")
    for name, check in (run.get("preconditions") or {}).items():
        if not (check or {}).get("ok"):
            problems.append(f"precondition {name} not met: {(check or {}).get('detail')}")
    if not run.get("preconditions"):
        problems.append("preconditions not recorded")
    return prov


# ---- partition proof ---------------------------------------------------------
# The cut's intercepted traffic, counters and hold. Decision after the fifth qualification: a positive counter in at least one read
# proves that traffic was intercepted, never that the cut held; every decrease of
# a counter is recorded as a fact (a reset or a rewrite, not interpreted); the
# hold is read apart, every second, from the rules and from probes with a deadline.

def cut_window(marks):
    """[the end of the apply, the start of the first removal] in UTC, or None."""
    applied, remove = marks.first("drop_apply_end"), marks.first("remove_start")
    if applied is None or not applied.get("ok") or remove is None:
        return None
    return applied["utc"], remove["utc"]


def _placed(value, record):
    """A time read in the node or a container (one kernel, one UTC with the host),
    kept only inside the host's bracket of that read."""
    if value is None or "w0" not in record or "w1" not in record:
        return None
    return value if record["w0"] - BRACKET_SEC <= value <= record["w1"] + BRACKET_SEC else None


def _placed_stream(value, record):
    """A send instant read in the probe's container (one kernel, one UTC with the
    host: the run's clock checks bound the harness container's offset), kept only if
    the host did not receive its line before it. How late a line arrives depends on
    the exec's streaming, not on the send: no upper bound."""
    if value is None or "w" not in record:
        return None
    return value if record["w"] - value >= -BRACKET_SEC else None


def _span(record):
    at, end = record.get("read_utc") or [None, None]
    at, end = _placed(at, record), _placed(end, record)
    return (at, end) if at is not None and end is not None and at <= end else (record["w0"], record["w1"])


def _widest(spans, lo, hi):
    """The widest stretch of [lo, hi] between two reads, each an interval [start, end]
    of which any instant may be the one read: from lo to the first read's end, from a
    read's start to the next one's end, from the last read's start to hi."""
    spans = sorted(spans)
    if not spans:
        return math.inf
    worst = max(spans[0][1] - lo, hi - spans[-1][0])
    for (a0, _a1), (_b0, b1) in zip(spans, spans[1:]):
        worst = max(worst, b1 - a0)
    return worst


def counter_reads(data, marks, window):
    """The chain's counters read inside the cut, in two streams each in its own order:
    the driver's reads (the verification, the qualification's reads, before the
    restore) and the monitor's samples. The streams overlap in time, a read of one
    may fall inside a read of the other: decreases are sought within a stream."""
    lo, hi = window

    def entry(status, t, source):
        drops = {f"{r['source']} -> {r['destination']}": r.get("pkts", 0) for r in status.get("rules") or []}
        return {"utc": t, "source": source, "accounting": {k: v.get("pkts", 0) for k, v in
                                                            (status.get("accounting") or {}).items()},
                "drops": drops}
    driver = []
    for m in marks.all:
        if m.get("mark") in ("cut_status", "cut_counters", "restore_start") and isinstance(m.get("status"), dict) \
                and lo <= m["utc"] <= hi:
            driver.append(entry(m["status"], m["utc"], m["mark"] + (f" {m['at']}" if m.get("at") else "")))
    samples = []
    for r in data.get("cut_samples") or []:
        if "error" in r or "w0" not in r or s2_partition.unreadable(r):
            continue
        span = _span(r)
        if lo <= span[0] and span[1] <= hi:
            samples.append(entry(r, span[0], "sample"))
    return {"driver": driver, "samples": sorted(samples, key=lambda e: e["utc"])}


def counter_decreases(stream):
    """Every counter lower than in the stream's previous read, or no longer read."""
    out = []
    for before, after in zip(stream, stream[1:]):
        for kind in ("accounting", "drops"):
            for key, value in before[kind].items():
                now = after[kind].get(key)
                if now is None or now < value:
                    out.append({"counter": key, "kind": kind, "from": value, "to": now,
                                "between_utc": [before["utc"], after["utc"]],
                                "reads": [before["source"], after["source"]]})
    return out


def intercepted(reads, names):
    """For each "<service> <direction>", the per-read totals over the peers: positive
    in at least one read is traffic intercepted."""
    every = reads["driver"] + reads["samples"]
    out = {}
    for name in names:
        totals = [(r["utc"], sum(v for k, v in r["accounting"].items() if k.rsplit(" ", 1)[0] == name))
                  for r in every if r["accounting"]]
        positive = sorted(t for t, v in totals if v > 0)
        out[name] = {"reads": len(totals), "max": max((v for _, v in totals), default=None),
                     "first_positive_utc": positive[0] if positive else None,
                     "status": "unknown" if not totals else ("ok" if positive else "fail")}
    return out


def _interval_stats(times, lo, hi):
    inside = sorted(t for t in times if lo <= t <= hi)
    diffs = sorted(b - a for a, b in zip(inside, inside[1:]))
    if not diffs:
        return {"samples": len(inside), "p50_sec": None, "p99_sec": None, "max_sec": None}
    return {"samples": len(inside), "p50_sec": round(diffs[len(diffs) // 2], 3),
            "p99_sec": round(diffs[min(len(diffs) - 1, int(0.99 * len(diffs)))], 3), "max_sec": round(diffs[-1], 3)}


def recorder_intervals(data, marks):
    """The intervals between drone01's onboard samples as the recorder received them,
    in the reference (no monitor, no cut), under the monitor alone (qualification) and
    in the cut with the monitor: the monitor's load read in the recorder's continuity."""
    times = [s["recv_mono"] for s in data.get("samples") or [] if s.get("event") == "sample" and _mine(s)
             and isinstance(s.get("recv_mono"), (int, float))]
    out = {}
    for name, (a, b) in (("reference", ("reference_start", "reference_end")),
                         ("monitor_alone", ("monitor_load_start", "monitor_load_end")),
                         ("cut_with_monitor", ("drop_apply_end", "remove_start"))):
        lo, hi = marks.first(a), marks.first(b)
        out[name] = None if lo is None or hi is None else _interval_stats(times, lo["mono"], hi["mono"])
    return out


def cut_hold(data, marks):
    """Whether the cut stayed the declared one, from the monitor's reads between the
    end of the apply and the start of the removal: every rules sample as declared
    (the jumps first in INPUT/OUTPUT/FORWARD, the chain, the drops), no probe across
    the cut, and both measured throughout (at most HOLD_GAP_SEC between reads)."""
    out = {"resolution": HOLD_RESOLUTION, "gap_limit_sec": HOLD_GAP_SEC}
    window, started = cut_window(marks), marks.first("cut_monitor_started")
    if window is None or started is None:
        out["reason"] = "no cut monitored (no applied cut, no monitor or no removal)"
        out["status"] = {"rules": "unknown", "probes": "unknown", "coverage": "unknown"}
        return out
    lo, hi = window
    peers = (marks.first("drop_apply_start") or {}).get("peers") or []
    spans, deviations, errors, unplaced = [], [], 0, 0
    sends = {t: [] for t in list(started.get("hold_targets") or [])
             + [f"icmp {ip}" for ip in started.get("icmp_peers") or []]}

    def attempt(a, record, placed=_placed):
        nonlocal unplaced
        t = placed(a.get("sent_utc"), record)
        if t is None:
            if lo <= record.get("w0", record.get("w", -math.inf)) < hi:
                unplaced += 1
            return
        if lo <= t < hi:
            sends.setdefault(a.get("target"), []).append((t, a.get("result")))
    for r in data.get("cut_samples") or []:
        if "w0" not in r:
            continue
        if "error" in r or s2_partition.unreadable(r):
            errors += lo <= r["w0"] < hi
        else:
            span = _span(r)
            if lo <= span[0] and span[1] <= hi:
                spans.append(span)
                reasons = s2_partition.conformity(r, peers)
                if reasons:
                    deviations.append({"read_utc": list(span), "reasons": reasons})
        for a in r.get("icmp") or []:
            attempt(a, r)
    probe_errors = 0
    process = {"starts": [], "ends": [], "host_stops": [], "host_errors": [], "unparsed": 0}
    skipped, lateness, timeouts = {}, {}, {}
    for r in data.get("cut_probes") or []:
        if "attempts" in r:                                  # one exec per round (before the persistent probe)
            probe_errors += "error" in r and lo <= r.get("w0", -math.inf) < hi
            for a in r["attempts"]:
                attempt(a, r)
        elif r.get("slot") is not None:
            target = r.get("target")
            if r.get("result") == "skipped-late":
                if lo <= (r.get("planned_utc") or -math.inf) < hi:
                    skipped.setdefault(target, []).append({"slot": r["slot"], "planned_utc": r["planned_utc"],
                                                           "late_sec": r.get("late_sec")})
                continue
            before = len(sends.get(target) or [])
            attempt(r, r, placed=_placed_stream)
            if len(sends.get(target) or []) > before:
                lateness.setdefault(target, []).append(r["sent_mono"] - r["planned_mono"])
                timeouts.setdefault(target, []).append(r.get("timeout"))
        elif r.get("event") == "start":
            process["starts"].append({k: r.get(k) for k in ("w", "t0_utc", "period", "deadline", "late_max",
                                                              "max_sec", "pid")})
        elif r.get("event") == "end":
            process["ends"].append({k: r.get(k) for k in ("w", "utc", "reason")})
        elif r.get("event") == "host-stop":
            process["host_stops"].append({k: r.get(k) for k in ("w", "how", "stop_written", "exit", "stop_sec")})
        elif r.get("event") == "host":
            process["host_errors"].append(r.get("error"))
            probe_errors += 1
        elif "unparsed" in r:
            process["unparsed"] += 1
    rules_worst = _widest(spans, lo, hi)
    probes, breaches, gaps = {}, [], []
    if math.isinf(rules_worst) or rules_worst > HOLD_GAP_SEC:
        gaps.append(f"rules: widest stretch between reads {rules_worst:.3f} s")
    for target, found in sorted(sends.items()):
        counted = sorted(t for t, result in found if result in BREACH + HELD)
        worst = _widest([(t, t) for t in counted], lo, hi)
        results = {}
        for t, result in found:
            results[result] = results.get(result, 0) + 1
            if result in BREACH:
                breaches.append({"target": target, "sent_utc": t, "result": result})
        probes[target] = {"sends": len(found), "results": results,
                          "max_interval_sec": None if math.isinf(worst) else round(worst, 3)}
        if target in lateness or target in skipped:
            late = sorted(lateness.get(target) or [])
            probes[target].update({
                "skipped_late": len(skipped.get(target) or []), "skipped": (skipped.get(target) or [])[:10],
                "late_p50_sec": round(late[len(late) // 2], 4) if late else None,
                "late_max_sec": round(late[-1], 4) if late else None,
                "timeout_min_sec": min((x for x in timeouts.get(target) or [] if x is not None), default=None)})
        if math.isinf(worst) or worst > HOLD_GAP_SEC:
            gaps.append(f"{target}: widest stretch between sends {worst:.3f} s")
    out.update({
        "window_utc": [lo, hi],
        "rules": {"samples": len(spans), "read_errors": errors,
                  "max_interval_sec": None if math.isinf(rules_worst) else round(rules_worst, 3),
                  "deviating_samples": len(deviations), "deviations": deviations[:20]},
        "probes": probes, "probe_round_errors": probe_errors, "attempts_outside_their_bracket": unplaced,
        "probe_process": process,
        "breaches": sorted(breaches, key=lambda b: b["sent_utc"])[:20], "gaps": gaps})
    out["status"] = {"rules": "fail" if deviations else ("ok" if spans else "unknown"),
                     "probes": "fail" if breaches else ("ok" if any(probes[t]["sends"] for t in probes) else "unknown"),
                     "coverage": "unknown" if gaps else "ok"}
    return out


def partition_section(data, marks, problems):
    if data["run"].get("case") == "control":
        drops = marks.by.get("drop_apply_start")
        if drops:
            problems.append("a DROP in the control case")
        return {"applied": False}
    verified = marks.first("cut_verified")
    out = {"guard": marks.first("guard_started"), "verified": verified is not None,
           "not_verified": marks.first("cut_not_verified"),
           "cut_status": (marks.first("cut_status") or {}).get("status"),
           "status_before_restore": (marks.first("restore_start") or {}).get("status"),
           "restore_statuses": [m.get("status") for m in marks.by.get("restore_status") or []],
           "restore_probes": [m.get("probes") for m in marks.by.get("restore_probe") or []]}
    if not out["guard"] or not out["guard"].get("alive"):
        problems.append("the guard was not seen alive before the DROP")
    if verified is None:
        problems.append("partition not proven (cut not verified)")
    before = out["status_before_restore"] or {}
    out["dropped_packets_before_restore"] = sum(r.get("pkts", 0) for r in before.get("rules") or [])
    # dropped packets in at least one read of the cut: the counters may be reset
    # (decision after the fifth qualification), their decreases recorded, not read
    window = cut_window(marks)
    reads = counter_reads(data, marks, window) if window else {"driver": [], "samples": []}
    dropped = max((sum(r["drops"].values()) for r in reads["driver"] + reads["samples"]), default=0)
    out["dropped_packets_max_read"] = dropped
    out["counter_decreases"] = {name: counter_decreases(stream) for name, stream in reads.items()}
    if verified is not None and not dropped:
        problems.append("partition not proven: no packet dropped during the cut")
    # the hold, in every cell (decision after the fifth qualification): a sample off
    # the declared rules, a probe across the cut or a stretch not measured within the
    # budget means the cell's own cut is not proven the declared fault -- the
    # qualification does not guarantee it cell by cell
    hold = cut_hold(data, marks) if verified is not None else None
    out["hold"] = hold
    if hold is not None:
        hold["recorder_intervals"] = recorder_intervals(data, marks)
        if hold["status"]["coverage"] != "ok":
            problems.append(f"partition hold not measured throughout: {hold.get('gaps') or hold.get('reason')}")
        if hold["status"]["rules"] == "fail":
            problems.append(f"partition not held as declared: {hold['rules']['deviating_samples']} rules sample(s) "
                            f"off it, first {hold['rules']['deviations'][0]}")
        if hold["status"]["probes"] == "fail":
            problems.append(f"partition not held: {len(hold['breaches'])} probe(s) across the cut, "
                            f"first {hold['breaches'][0]}")
    probes = marks.by.get("restore_probe") or []
    if not probes or not probes[-1].get("connected"):
        problems.append("connectivity not seen back after the restore")
    frozen = marks.first("drop_apply_start")
    end = marks.first("peers_end")
    if frozen and end and frozen.get("peers") != end.get("peers"):
        problems.append("docker network peers changed during the run: isolation not proven")
    if data["partition_restored"] is None:
        problems.append("partition-restored never written")
    return out


# ---- verdict ------------------------------------------------------------------

def judge(result_dir):
    data = load(result_dir)
    run = data["run"]
    variant, case = run.get("variant"), run.get("case")
    marks = Marks(data["phases"])
    tl = timeline(run, marks)
    problems = []
    anomalies = []           # recorded and reported, not verdicts by themselves
    out = {"protocol_id": PROTOCOL_ID, "variant": variant, "case": case, "run_id": run.get("run_id"),
           "result_dir": os.path.basename(os.path.normpath(result_dir)), "timeline": tl}
    interrupted = []
    if data["guard_fired"] is not None:
        interrupted.append("the guard fired")
    driver_end = marks.last("driver_end")
    if driver_end is None:
        interrupted.append("the phase driver did not end")
    elif driver_end.get("status") == "interrupted":
        interrupted.append(f"phase driver interrupted ({(marks.last('interrupted') or marks.last('driver_error') or {})})")
    elif driver_end.get("status") == "aborted":
        problems.append(f"phase aborted: {(marks.last('aborted') or {}).get('reason')}")
    if run.get("runner_status") not in (None, "completed"):
        interrupted.append(f"runner {run.get('runner_status')}")
    out["provenance"] = provenance_section(data, problems)
    out["clocks"] = clock_section(marks, problems)
    harness = harness_section(run, data["events"], marks, problems)
    data["_harness"] = harness
    out["harness"] = harness
    out["partition"] = partition_section(data, marks, problems)
    for name, records in (("inventory", data["inventory"]), ("nodes", data["nodes"]), ("audit", data["audit"]),
                          ("local copy", data["copy_reads"])):
        if tl["phase_start"] and tl["horizon"]:
            cov = read_coverage(records, tl["phase_start"]["mono"], tl["horizon"]["mono"])
            out.setdefault("collection", {})[name] = cov
            if not cov["ok"]:
                problems.append(f"{name}: max gap {cov['max_gap_sec']} s > {READ_GAP_SEC} s")
    pulse_confirmed = case == "partition-only" or bool((harness.get("pulse") or {}).get("confirmed"))
    # central side first (the action decides which silence of the onboard the recorder may excuse)
    gt_first = ground_truth_section(data, marks, tl, None, [])
    if variant == "a":
        received, recognized, a_lines = a_central(data, gt_first, tl, marks, problems)
    else:
        received, recognized = b_central(data, gt_first, tl, marks, problems)
        a_lines = []
    action = a_action(data, recognized, a_lines, tl) if variant == "a" else b_action(data, recognized, tl)
    handover = handover_proof(data, variant, action, tl)
    gt = ground_truth_section(data, marks, tl, handover, problems)
    gt.pop("_episode_raw", None)
    out["ground_truth"] = gt
    # short/long: the removal starts 2 s / 30 s after the local return, within
    # [target - 1 ms, target + 1 s]; outside, the cell is not the case it claims
    # (choice 1, decision on point 5). The end of the removal and the network's
    # return are recorded apart: the tolerance does not move them.
    remove, episode = marks.first("remove_start"), gt.get("episode") or {}
    written = marks.first("partition_restored_written")
    back = next((m for m in marks.by.get("restore_probe") or [] if m.get("connected")), None)
    if written:
        out["partition"]["restore_command_end"] = at(written["restore_end_mono"], written["restore_end_utc"],
                                                     "runner, end of the remove command that left no rule")
    out["partition"]["network_back"] = None if back is None else at(back["mono"], back["utc"],
                                                                    "runner, first probe connected again")
    if case in RESTORE_AFTER_RETURN_SEC and remove is not None and episode.get("returned"):
        delay = remove["mono"] - episode["returned"]["mono"]
        target = RESTORE_AFTER_RETURN_SEC[case]
        lo, hi = target + RESTORE_TOLERANCE_SEC[0], target + RESTORE_TOLERANCE_SEC[1]
        out["partition"]["restore_delay_after_return_sec"] = round(delay, 3)
        out["partition"]["restore_delay_target_sec"] = target
        out["partition"]["restore_delay_tolerance_sec"] = list(RESTORE_TOLERANCE_SEC)
        if not lo <= delay <= hi:
            problems.append(f"restore delay {delay:.3f} s after the local return, outside [{lo:.3f}, {hi:.3f}] s "
                            f"for {case}'s {target:.0f} s: not a {case} cell")
    out["received"], out["recognized"] = received, recognized
    out["audit"] = audit_section(data, recognized.get("correlation_id"), recognized.get("value"), tl)
    bound = max([((s or {}).get("mono_offset_bound") or 0.0) for v in out["clocks"].values() if isinstance(v, dict)
                 for s in v.values() if isinstance(s, dict)] + [0.0])
    # the local return is on the recorder's 0.2 s evaluation grid; the detectors
    # evaluate on their own grids (declared offsets): 0.2 s on top of the clocks
    action["order_vs_local_return"] = late_action(action, gt, bound + 0.2)
    action["handover_proven"] = None if handover is None else at(handover["proven_mono"], None,
                                                                 "inventory and health (three sources)")
    out["action"] = action
    out["reconvergence"] = reconvergence_section(data, variant, action, tl, marks, pulse_confirmed, anomalies)
    out["isolation"] = isolation_section(data, variant, tl, a_lines, anomalies)
    out["px4_continuity"] = px4_section(data, marks, tl)
    central_ok = bool((received.get("coverage") or {}).get("ok"))
    if not central_ok:
        problems.append(f"central coverage: {(received.get('coverage') or {}).get('reason') or received.get('coverage')}")
    # the case's own criterion
    fails, unknown_dims = [], []
    recognized_value = recognized.get("value")
    if case == "control":
        valid = gt.get("episode_valid") and pulse_confirmed and central_ok
        if recognized_value is False and valid:
            fails.append("positive control not recognized within the horizon")
        elif recognized_value and action.get("outcome") != "migrated" and valid:
            fails.append(f"positive control recognized but not migrated ({action.get('outcome')})")
        elif recognized_value is None:
            unknown_dims.append(f"recognition: {recognized.get('reason')}")
    elif case == "partition-only":
        spurious = []
        if variant == "a" and recognized.get("accepted_in_phase"):
            spurious.append(f"requests accepted {recognized['accepted_in_phase']}")
        if variant == "b" and recognized.get("correlation_ids_in_phase"):
            spurious.append(f"incidents {recognized['correlation_ids_in_phase']}")
        if action.get("attempts"):
            spurious.append(f"{action['attempts']} edge object(s)")
        if spurious and gt.get("phase_episodes") == 0 and central_ok:
            fails.append(f"spurious incident/action in the negative control: {spurious}")
        elif spurious:
            unknown_dims.append(f"incident/action with its premise not holding: {spurious}")
    else:
        # eligible: a valid internal episode and central coverage; any measurement
        # problem on the episode, the cut, the clocks or the harness excludes it
        eligible = bool(gt.get("episode_valid") and central_ok and pulse_confirmed and not any(
            p.startswith(("pulse", "partition", "the guard", "docker network", "episode", "expected one local episode",
                          "restore delay",
                          "a local episode", "local recorder", "source stamps", "UTC", "host UTC", "clock",
                          "the incremental", "the last incremental", "local copy", "harness", "phase aborted"))
            for p in problems))
        out["missed_transient"] = {
            "eligible": eligible,
            "recognized_within_180s": recognized_value if eligible else None,
            "missed_within_180s": (not recognized_value) if eligible and recognized_value is not None else None,
            "request_rejected": "rejected" in (recognized.get("answers") or {}),
            "audit_absent": (out["audit"].get("incident_started") == "not observed within the horizon"),
            "reason": None if eligible else "not eligible: episode, partition or central coverage not valid"}
    for name in ("reconvergence", "isolation", "px4_continuity"):
        status = out[name].get("status")
        if status == "fail":
            fails.append(f"{name}: " + "; ".join(str(x) for x in (out[name].get("violations") or
                                                                  out[name].get("failures") or
                                                                  [out[name].get("reason")]))[:300])
        elif status != "ok":
            detail = out[name].get("unknown") or out[name].get("reason") or ""
            unknown_dims.append(f"{name} not established: {detail}"[:300])
    unexpected = [e for e in gt.get("episodes") or [] if case == "partition-only"
                  and tl["phase_start"] and e["entered"]["mono"] >= tl["phase_start"]["mono"]]
    for episode in unexpected:
        anomalies.append({"kind": "unexpected local episode in the negative control", "detail": episode})
    out["anomalies"] = anomalies
    out["validity"] = {"ok": not problems, "problems": problems}
    if interrupted:
        verdict = "INTERRUPTED"
    elif fails:
        verdict = "FAIL"
    elif problems or unknown_dims:
        verdict = "INCONCLUSIVE"
    else:
        verdict = "PASS"
    out["verdict"] = verdict
    out["reasons"] = {"interrupted": interrupted, "fail": fails, "inconclusive": problems + unknown_dims,
                      "anomalies": [a["kind"] for a in anomalies]}
    return out


EXIT = {"PASS": 0, "FAIL": 1, "INCONCLUSIVE": 2, "INTERRUPTED": 3}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("result_dir")
    parser.add_argument("--suffix", default="", help="written to s2-judge<suffix>.json; an existing file is kept")
    args = parser.parse_args(argv)
    result = judge(args.result_dir)
    path = os.path.join(args.result_dir, f"s2-judge{args.suffix}.json")
    if os.path.exists(path):
        print(f"{path} exists: a re-judgement goes to a new file (--suffix=...)", file=sys.stderr)
        return 4
    with open(path, "w") as h:
        json.dump(result, h, indent=1, default=str)
    print(f"{result['verdict']}: {path}")
    for kind, reasons in result["reasons"].items():
        for reason in reasons:
            print(f"  {kind}: {reason}")
    return EXIT[result["verdict"]]


if __name__ == "__main__":
    sys.exit(main())
