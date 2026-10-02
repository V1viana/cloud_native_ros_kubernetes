"""Mission continuity as PX4's own status stream shows it; no ROS import.

SPECIFICA_ARCHITETTURALE s21 defines mission continuity as "assenza di restart
PX4 e continuita' di heartbeat/nav state". The Pod UID and restart count cover
the first half from outside; this tracker covers the rest from inside the
onboard DDS domain: every VehicleStatus received becomes, when something
happens, a log marker -- first status, nav/arming/failsafe state change,
reception gap, PX4 clock going back (a PX4 restart a Pod UID would not show).
A periodic summary carries the running maxima. Judging them against a
scenario's timeline is left to scripts/mission_continuity.py.
"""

FIELDS = ("nav_state", "arming_state", "failsafe")

# PX4's timestamp is its boot time in microseconds. Only a jump back of more
# than this counts as a restart, so time-sync corrections are not mistaken
# for one.
CLOCK_REGRESSION_US = 1_000_000


class ContinuityTracker:
    """Fold received VehicleStatus samples into markers and running maxima."""

    def __init__(self, gap_report_sec=2.0):
        self._gap_report_ns = int(gap_report_sec * 1_000_000_000)
        self.count = 0
        self.max_gap_ns = 0
        self.regressions = 0
        self._last_receive_ns = None
        self._last_px4_us = None
        self._state = None

    def on_status(self, receive_ns, px4_timestamp_us, state):
        """Record one sample; receive_ns is monotonic. Return (marker, fields) pairs."""
        self.count += 1
        state = {field: state[field] for field in FIELDS}
        markers = []
        if self._state is None:
            markers.append(("MISSION_STATUS_FIRST", {"seq": self.count, **state}))
        else:
            gap_ns = receive_ns - self._last_receive_ns
            self.max_gap_ns = max(self.max_gap_ns, gap_ns)
            if gap_ns > self._gap_report_ns:
                markers.append((
                    "MISSION_STATUS_GAP",
                    {"seq": self.count, "gap_ms": gap_ns // 1_000_000},
                ))
            if px4_timestamp_us < self._last_px4_us - CLOCK_REGRESSION_US:
                self.regressions += 1
                markers.append((
                    "MISSION_PX4_CLOCK_REGRESSION",
                    {
                        "seq": self.count,
                        "before_us": self._last_px4_us,
                        "after_us": px4_timestamp_us,
                    },
                ))
            for field in FIELDS:
                if state[field] != self._state[field]:
                    markers.append((
                        "MISSION_STATE_CHANGE",
                        {
                            "seq": self.count,
                            "field": field,
                            "before": self._state[field],
                            "after": state[field],
                        },
                    ))
        self._last_receive_ns = receive_ns
        self._last_px4_us = px4_timestamp_us
        self._state = state
        return markers

    def summary(self, now_ns):
        """Running maxima; last_status_age_ms is -1 until the first sample."""
        age_ms = -1
        if self._last_receive_ns is not None:
            age_ms = (now_ns - self._last_receive_ns) // 1_000_000
        return (
            "MISSION_SUMMARY",
            {
                "count": self.count,
                "max_gap_ms": self.max_gap_ns // 1_000_000,
                "last_status_age_ms": age_ms,
                "regressions": self.regressions,
                **(self._state or {}),
            },
        )


POSITION_FLAGS = ("xy_valid", "z_valid", "dead_reckoning")
POSITION_RESETS = ("xy_reset_counter", "z_reset_counter")


class PositionTracker:
    """PX4's own position estimate (VehicleLocalPosition): flags, resets, gaps and
    a decimated trace (checklist R8, position and altitude in E1).

    The estimate is judged only on validity, continuity and resets; how far the
    vehicle really moved is judged on the simulator's ground truth, sampled from
    the node (scripts/position_hold.py). The trace here serves the estimate
    versus truth comparison, which is reported.
    """

    def __init__(self, log_period_sec=0.5, gap_report_sec=2.0):
        self._log_period_ns = int(log_period_sec * 1_000_000_000)
        self._gap_report_ns = int(gap_report_sec * 1_000_000_000)
        self.count = 0
        self.max_gap_ns = 0
        self._last_receive_ns = None
        self._last_logged_ns = None
        self._state = None

    def on_position(self, receive_ns, sample):
        """sample: x, y, z plus POSITION_FLAGS and POSITION_RESETS. Returns markers."""
        self.count += 1
        state = {key: sample[key] for key in POSITION_FLAGS + POSITION_RESETS}
        point = {key: round(float(sample[key]), 3) for key in ("x", "y", "z")}
        markers = []
        if self._state is None:
            markers.append(("MISSION_POSITION_FIRST", {"seq": self.count, **point, **state}))
            self._last_logged_ns = receive_ns
        else:
            gap_ns = receive_ns - self._last_receive_ns
            self.max_gap_ns = max(self.max_gap_ns, gap_ns)
            if gap_ns > self._gap_report_ns:
                markers.append(("MISSION_POSITION_GAP",
                                {"seq": self.count, "gap_ms": gap_ns // 1_000_000}))
            for key in POSITION_FLAGS:
                if state[key] != self._state[key]:
                    markers.append(("MISSION_POSITION_FLAG_CHANGE", {
                        "seq": self.count, "field": key,
                        "before": self._state[key], "after": state[key]}))
            for key in POSITION_RESETS:
                if state[key] != self._state[key]:
                    markers.append(("MISSION_POSITION_RESET", {
                        "seq": self.count, "field": key,
                        "before": self._state[key], "after": state[key]}))
            if receive_ns - self._last_logged_ns >= self._log_period_ns:
                markers.append(("MISSION_POSITION", {"seq": self.count, **point,
                                                     "xy_valid": state["xy_valid"],
                                                     "z_valid": state["z_valid"]}))
                self._last_logged_ns = receive_ns
        self._last_receive_ns = receive_ns
        self._state = state
        return markers

    def summary(self, now_ns):
        age_ms = -1
        if self._last_receive_ns is not None:
            age_ms = (now_ns - self._last_receive_ns) // 1_000_000
        return ("MISSION_POSITION_SUMMARY", {
            "count": self.count, "max_gap_ms": self.max_gap_ns // 1_000_000,
            "last_age_ms": age_ms})
