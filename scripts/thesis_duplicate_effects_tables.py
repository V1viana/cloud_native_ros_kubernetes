#!/usr/bin/env python3
"""Proven duplicate effects (D-B (iii), decision of Viviana, 4 October 2026): "no duplicate effect demonstrated
by the available data". The events show an effect, not the request that caused it.

For every valid S2 and S4 execution of the calendar, in the window of scripts/thesis_pod_events_tables.py (the
judge's, with the same measurability checks), from the test namespace's events.json:
  - a candidate is a Pod creation reported in the window (SuccessfulCreate, "Created pod: NAME") by a
    controller (ReplicaSet or Job) that had already reported creating another Pod, at the same second or
    earlier, whose removal is not reported before it (SuccessfulDelete or Killing of that Pod, or the taint
    manager's "Marking for deletion"): an additional Pod of the same controller, not a replacement;
  - for a candidate: the Deployment's ScalingReplicaSet event for that ReplicaSet ("Scaled up replica set ...
    to N from M"), and whether a SuccessfulRescale event of the HorizontalPodAutoscaler of the same name is
    present in events.json and, where collected (S4), events-all.json, with pointers. A rescale event, present
    or absent, documents or not the increase; it is not taken as a proof of its cause;
  - every candidate's outcome is "no duplicate effect demonstrated"; an execution without candidates is
    "none reported", never 0; the instants have a resolution of one second.

Usage: python3 scripts/thesis_duplicate_effects_tables.py --campaign CAMPAIGN_DIR --out DATA_DIR
"""
import argparse
import csv
import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import r14_schedule as rs  # noqa: E402
import thesis_pod_events_tables as pe  # noqa: E402

FIELDS = ["config", "variant", "pair", "seq", "status", "reason", "controller", "second_pod", "second_pod_created",
          "seconds_after_window_start", "earlier_pods", "deployment_scaling_event", "rescale_event", "rescale_source",
          "outcome", "source"]
CANDIDATE = "additional Pod of the same controller reported in the window"
OUTCOME = "no duplicate effect demonstrated"
REMOVALS = {"SuccessfulDelete": "", "Killing": "", "TaintManagerEviction": "Marking for deletion"}


def removed_pod(event):
    """The Pod whose removal the event reports, or None."""
    reason = event.get("reason")
    if reason not in REMOVALS or not (event.get("message") or "").startswith(REMOVALS[reason]):
        return None
    return pe.pod_of(event)


def execution_rows(cfg, v, pair, seq, result_dir):
    base = {k: "" for k in FIELDS}
    base.update(config=cfg, variant=v, pair=pair, seq=seq)
    w = pe.measurable_window(cfg, result_dir)
    if "reason" in w:
        return [{**base, "status": "not measurable", "reason": w["reason"], "outcome": "not measurable",
                 "source": w["source"]}]
    lo, hi, items, events_path = w["lo"], w["hi"], w["items"], w["events_path"]
    events_all = os.path.join(result_dir, "events-all.json")
    creates, removals = [], {}
    for e in items:
        span = pe.interval(e)
        if span is None:
            continue
        if e.get("reason") == "SuccessfulCreate" and pe.pod_of(e):
            obj = e.get("involvedObject") or {}
            creates.append({"span": span, "controller": f"{obj.get('kind')}/{obj.get('name')}", "pod": pe.pod_of(e),
                            "stamp": e.get("firstTimestamp") or e.get("eventTime"), "uid": e["metadata"].get("uid")})
        pod = removed_pod(e)
        if pod:
            removals.setdefault(pod, []).append(span[0])
    rows = []
    for c in creates:
        if c["span"][1] < lo or c["span"][0] > hi:
            continue
        earlier = [x for x in creates if x["controller"] == c["controller"] and x["pod"] != c["pod"]
                   and x["span"][0] <= c["span"][0]]
        alive = [x for x in earlier if not any(t <= c["span"][0] for t in removals.get(x["pod"], []))]
        if not alive:
            continue
        inside = lo <= c["span"][0] and c["span"][1] <= hi
        rs_name = c["controller"].split("/", 1)[1]
        scaling = [e for e in items if e.get("reason") == "ScalingReplicaSet"
                   and re.match(rf"^Scaled up replica set {re.escape(rs_name)} to \d+ from \d+$", e.get("message") or "")]
        deployment = (scaling[0]["involvedObject"]["name"] if scaling else rs_name.rsplit("-", 1)[0])
        rescales, looked = [], []
        for path in (events_path, events_all):
            doc = pe.load_json(path)
            if doc is None:
                continue
            looked.append(pe.rel(path))
            rescales += [(path, e) for e in doc.get("items") or [] if e.get("reason") == "SuccessfulRescale"
                         and (e.get("involvedObject") or {}).get("kind") == "HorizontalPodAutoscaler"
                         and (e.get("involvedObject") or {}).get("name") == deployment]
        seen, unique = set(), []
        for path, e in rescales:                      # events-all.json repeats the namespace's events
            if e["metadata"].get("uid") not in seen:
                seen.add(e["metadata"].get("uid"))
                unique.append((path, e))
        rows.append({**base,
                     "status": CANDIDATE if inside else "not attributable at an edge",
                     "controller": c["controller"], "second_pod": c["pod"], "second_pod_created": c["stamp"],
                     "seconds_after_window_start": f"{c['span'][0] - lo:.1f} to {c['span'][1] - lo:.1f}",
                     "earlier_pods": "; ".join(f"{x['pod']} (created {x['stamp']}, "
                                               f"{'before' if x['span'][1] < lo else 'at the start edge of' if x['span'][0] < lo else 'in'}"
                                               " the window; removal: none reported)" for x in alive),
                     "deployment_scaling_event": "; ".join(f"{e['message']} ({e.get('firstTimestamp')}, "
                                                           f"{pe.rel(events_path)}#items[metadata.uid={e['metadata'].get('uid')}])"
                                                           for e in scaling) or "none reported",
                     "rescale_event": ("present: " + "; ".join(f"{e.get('message')} ({e.get('firstTimestamp')})"
                                                              for _, e in unique)) if unique else "absent",
                     "rescale_source": ("; ".join(f"{pe.rel(p)}#items[metadata.uid={e['metadata'].get('uid')}]"
                                                  for p, e in unique) if unique else
                                        f"no SuccessfulRescale of HorizontalPodAutoscaler/{deployment} in "
                                        + " and ".join(looked)
                                        + ("" if os.path.isfile(events_all) else " (events-all.json not collected)")),
                     "outcome": OUTCOME,
                     "source": f"{pe.rel(events_path)}#items[metadata.uid in "
                               f"{','.join([c['uid']] + [x['uid'] for x in alive])}]"})
    return rows or [{**base, "status": "none reported", "outcome": "none reported",
                     "source": f"{pe.rel(events_path)}#items[reason=SuccessfulCreate] "
                               "(no additional Pod of a controller in the window)"}]


def rows_of(campaign):
    rows = []
    for e in rs.build():
        if e["config"] not in pe.S2 + pe.S4:
            continue
        run_dir = os.path.join(campaign, "runs", f"{e['seq']:03d}-{e['config']}-{e['variant']}")
        rec = pe.load_json(os.path.join(run_dir, "run.json"))
        if rec is None or rec.get("valid") is not True:
            continue                                   # denominators: valid executions only
        rows += execution_rows(e["config"], e["variant"], e["pair"], e["seq"], rec.get("result_dir") or "")
    order = list(pe.S2 + pe.S4)
    rows.sort(key=lambda r: (order.index(r["config"]), r["variant"], int(r["seq"])))
    return rows


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--campaign", required=True)
    ap.add_argument("--out", required=True)
    a = ap.parse_args(argv)
    measured = os.path.join(a.out, "measured")
    os.makedirs(measured, exist_ok=True)
    with open(os.path.join(measured, "duplicate_effects.csv"), "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS, lineterminator="\n")
        w.writeheader()
        w.writerows(rows_of(a.campaign))
    # no table: these rows back the sentence "no duplicate effect demonstrated by the available data"
    print("duplicate_effects")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
