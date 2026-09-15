"""Stable mapping from normalized events to policy outcomes."""

from dataclasses import dataclass


@dataclass(frozen=True)
class Policy:
    policy_id: str
    event_type: str
    outcome: str


POLICIES = (
    Policy("P0", "BatteryLow", "observe_local_safety"),
    Policy("P1", "TelemetryHeartbeatLost", "restore_telemetry"),
    Policy("P2", "AnalyticsLatencySLO", "restore_analytics_slo"),
)


class PolicyCatalog:
    """Resolve a request while preventing event/policy mismatches."""

    def __init__(self, policies=POLICIES):
        self._by_event = {policy.event_type: policy for policy in policies}
        self._by_id = {policy.policy_id: policy for policy in policies}

    def resolve(self, event_type: str, policy_id: str, outcome: str) -> Policy:
        policy = self._by_event.get(event_type)
        if policy is None:
            raise ValueError(f"No policy is defined for event '{event_type}'")
        if policy_id and policy_id != policy.policy_id:
            raise ValueError(
                f"Policy '{policy_id}' does not match event '{event_type}'"
            )
        if outcome and outcome != policy.outcome:
            raise ValueError(
                f"Outcome '{outcome}' does not match policy '{policy.policy_id}'"
            )
        return policy
