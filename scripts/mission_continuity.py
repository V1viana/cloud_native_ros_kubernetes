#!/usr/bin/env python3
"""Judge mission continuity from a mission observer log (checklist R8).

SPECIFICA_ARCHITETTURALE s21: "assenza di restart PX4 e continuita' di
heartbeat/nav state". The observer (sources/mission_observer) logs markers;
this script places them on the scenario's timeline. Same code for variants A
and B. Verdict:

  inconclusive  the measurement does not hold: observer restarted (its log
                is partial), no status before the window, no summary after
                it, or the vehicle was not in the declared starting state;
  false         inside the window [start, end]: PX4 clock went back
                (restart), a status gap reaching GAP_LIMIT_MS touched it, or
                the vehicle state changed other than as expected (by default:
                not at all; with --expect-nav, only that nav_state, first, not
                before --not-before);
  true          otherwise.

Only the window decides (review of f21c5b6). Changes, gaps and clock jumps
wholly before or after it are reported, not judged, and so are changes after
the expected one (e.g. PX4 landing and disarming after RTL). A gap counts if
any part of it falls in the window: an interruption that starts just before
the window and lasts into it is not excused. max_gap_ms is the whole
recording's, reported. PX4 restarts seen from outside (Pod UID/restartCount)
stay the scenario's own check.
"""

import argparse
import re
import sys

# TelemetryHeartbeatRule's timeout_sec (px4_event_detector_plugin params):
# continuity means the system's own P1 heartbeat rule would never have fired.
GAP_LIMIT_MS = 5000

NAV_NAMES = {
    "0": "MANUAL", "2": "POSCTL", "4": "AUTO_LOITER", "5": "AUTO_RTL",
    "17": "AUTO_TAKEOFF", "18": "AUTO_LAND",
}
ARMING_NAMES = {"1": "DISARMED", "2": "ARMED"}

MARKER = re.compile(r"\b(MISSION_[A-Z0-9_]+) timestamp_ns=(\d+)((?: [a-z_]+=\S+)*)")


def parse(text):
    """Return [(name, timestamp_ns, fields)] in log order."""
    markers = []
    for match in MARKER.finditer(text):
        fields = dict(item.split("=", 1) for item in match.group(3).split())
        markers.append((match.group(1), int(match.group(2)), fields))
    return markers


def _name(field, value):
    names = {"nav_state": NAV_NAMES, "arming_state": ARMING_NAMES}.get(field, {})
    return names.get(value, value)


def evaluate(markers, window_start_ns, window_end_ns, observer_restarts,
             start_state=None, expect_nav=None, not_before_ns=0):
    """Apply the criteria above; start_state maps field -> raw value."""
    inconclusive, failures = [], []
    if observer_restarts != "0":
        inconclusive.append(f"observer restarts {observer_restarts}")

    first = next((m for m in markers if m[0] == "MISSION_STATUS_FIRST"), None)
    if first is None or first[1] >= window_start_ns:
        inconclusive.append("no status before the window")
    summaries = [m for m in markers if m[0] == "MISSION_SUMMARY"]
    last = summaries[-1][2] if summaries else {}
    if not summaries or summaries[-1][1] <= window_end_ns:
        inconclusive.append("no summary after the window")

    changes = [m for m in markers if m[0] == "MISSION_STATE_CHANGE"]
    state = dict(first[2]) if first else {}
    state.pop("seq", None)
    for _, ts, fields in changes:
        if ts < window_start_ns:
            state[fields["field"]] = fields["after"]
    for field, value in (start_state or {}).items():
        if state.get(field) != value:
            inconclusive.append(
                f"{field} at window start {_name(field, state.get(field))}, "
                f"declared {_name(field, value)}"
            )

    def in_window(ts):
        return window_start_ns <= ts <= window_end_ns

    inside = [m for m in changes if in_window(m[1])]
    expected = None
    if expect_nav is not None:
        expected = next((m for m in inside if m[2]["field"] == "nav_state"), None)
        if expected is None or expected[2]["after"] != expect_nav:
            failures.append(
                f"first nav_state change in the window is not {_name('nav_state', expect_nav)}"
            )
            expected = None
        elif expected[1] < not_before_ns:
            failures.append("expected nav_state change came before its cause")
    unexpected = [
        m for m in inside
        if expected is None or int(m[2]["seq"]) < int(expected[2]["seq"])
    ]
    if unexpected:
        failures.append(f"{len(unexpected)} unexpected state change(s) in the window")

    # Silences as (start, end): closed ones from GAP markers (logged over 2s, so
    # every one reaching the limit is there), open ones from each summary's age.
    silences = [(ts - int(f["gap_ms"]) * 1_000_000, ts)
                for name, ts, f in markers if name == "MISSION_STATUS_GAP"]
    silences += [(ts - int(f["last_status_age_ms"]) * 1_000_000, ts)
                 for name, ts, f in markers
                 if name == "MISSION_SUMMARY" and int(f["last_status_age_ms"]) >= 0]
    over_limit = [(a, b) for a, b in silences
                  if b - a >= GAP_LIMIT_MS * 1_000_000
                  and a <= window_end_ns and b >= window_start_ns]
    if over_limit:
        longest = max(b - a for a, b in over_limit) // 1_000_000
        failures.append(f"status gap {longest} ms >= {GAP_LIMIT_MS} ms in the window")
    max_gap_ms = max(int(last.get("max_gap_ms", 0)), int(last.get("last_status_age_ms", 0)))
    jumps = [m for m in markers if m[0] == "MISSION_PX4_CLOCK_REGRESSION"]
    regressions = sum(1 for m in jumps if in_window(m[1]))
    if regressions:
        failures.append("PX4 clock went back (restart) in the window")

    verdict = "inconclusive" if inconclusive else ("false" if failures else "true")
    transitions = [
        f"t{(ts - window_start_ns) / 1e9:+.1f}s {f['field']} "
        f"{_name(f['field'], f['before'])}->{_name(f['field'], f['after'])}"
        for _, ts, f in changes
    ]
    return {
        "verdict": verdict,
        "reasons": "; ".join(inconclusive + failures) or "none",
        "observer_restarts": observer_restarts,
        "status_count": last.get("count", "0"),
        "max_gap_ms": max_gap_ms,
        "gap_limit_ms": GAP_LIMIT_MS,
        "gaps_reported": sum(1 for m in markers if m[0] == "MISSION_STATUS_GAP"),
        "gaps_over_limit_in_window": len({b for _, b in over_limit}),
        "clock_regressions": regressions,
        "clock_regressions_total": len(jumps),
        "state_at_window_start": " ".join(
            f"{k}={_name(k, v)}" for k, v in sorted(state.items())
        ),
        "unexpected_changes": len(unexpected),
        "expected_change_after_not_before_ms": (
            (expected[1] - not_before_ns) // 1_000_000 if expected else "none"
        ),
        "transitions": "; ".join(transitions) or "none",
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("log")
    parser.add_argument("--window-start-ns", type=int, required=True)
    parser.add_argument("--window-end-ns", type=int, required=True)
    parser.add_argument("--observer-restarts", required=True)
    parser.add_argument("--start-nav")
    parser.add_argument("--start-arming")
    parser.add_argument("--expect-nav")
    parser.add_argument("--not-before-ns", type=int, default=0)
    args = parser.parse_args(argv)
    with open(args.log, encoding="utf-8", errors="replace") as handle:
        markers = parse(handle.read())
    start_state = {}
    if args.start_nav is not None:
        start_state["nav_state"] = args.start_nav
    if args.start_arming is not None:
        start_state["arming_state"] = args.start_arming
    result = evaluate(
        markers, args.window_start_ns, args.window_end_ns,
        args.observer_restarts, start_state, args.expect_nav, args.not_before_ns,
    )
    for key, value in result.items():
        print(f"{key}={value}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
