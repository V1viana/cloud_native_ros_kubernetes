"""Shared helper for building the `status.conditions` entries every CRD exposes."""

from datetime import datetime, timezone


def build_condition(condition_type, status, reason="", message=""):
    return {
        "type": condition_type,
        "status": "True" if status else "False",
        "reason": reason,
        "message": message,
        "lastTransitionTime": datetime.now(timezone.utc)
        .isoformat(timespec="seconds")
        .replace("+00:00", "Z"),
    }


def upsert_condition(conditions, condition_type, status, reason="", message=""):
    """Replace the entry for `condition_type` in place, or append it."""
    updated = build_condition(condition_type, status, reason, message)
    for index, existing in enumerate(conditions):
        if existing.get("type") == condition_type:
            if existing.get("status") == updated["status"]:
                updated["lastTransitionTime"] = existing.get(
                    "lastTransitionTime", updated["lastTransitionTime"]
                )
            conditions[index] = updated
            return conditions
    conditions.append(updated)
    return conditions
