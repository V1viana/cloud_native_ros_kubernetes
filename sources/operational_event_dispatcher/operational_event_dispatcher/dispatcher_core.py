"""Pure dispatch admission logic, independent from ROS 2 runtime types."""

from collections import OrderedDict


SUPPORTED_EVENTS = frozenset(
    {
        "BatteryLow",
        "TelemetryHeartbeatLost",
        "AnalyticsLatencySLO",
    }
)


class DispatchAdmission:
    """Deduplicate incident entry events while allowing a failed retry."""

    STATE_ENTER = 0

    def __init__(self, cache_size=256, supported_events=SUPPORTED_EVENTS):
        self._cache_size = cache_size
        self._supported_events = frozenset(supported_events)
        self._claimed = set()
        self._dispatched = OrderedDict()

    def claim(self, correlation_id, event_type, state):
        if (
            not correlation_id
            or event_type not in self._supported_events
            or state != self.STATE_ENTER
            or correlation_id in self._claimed
            or correlation_id in self._dispatched
        ):
            return False
        self._claimed.add(correlation_id)
        return True

    def complete(self, correlation_id, accepted):
        self._claimed.discard(correlation_id)
        if not accepted:
            return
        self._dispatched[correlation_id] = None
        while len(self._dispatched) > self._cache_size:
            self._dispatched.popitem(last=False)
