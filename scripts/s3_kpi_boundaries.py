#!/usr/bin/env python3
"""Write the exact S3 timestamp boundaries without relabeling them as proposal KPIs."""

import json
from pathlib import Path
import sys


def boundaries(variant, bootstrap_start, bootstrap_end, incident_start, incident_end):
    if variant not in ("a", "b"):
        raise ValueError("variant must be a or b")
    times = [int(value) for value in
             (bootstrap_start, bootstrap_end, incident_start, incident_end)]
    if times[0] > times[1] or times[2] > times[3]:
        raise ValueError("end timestamp precedes start timestamp")
    return {
        "clock": "host CLOCK_REALTIME, nanoseconds since Unix epoch",
        "variant": variant,
        "bootstrap": {
            "start_utc_ns": times[0], "end_utc_ns": times[1],
            "start": ("before KubeROS bootstrap Job" if variant == "a" else
                      "before applying shared infrastructure and ROSModule fleet"),
            "end": ("after bootstrap Job and Deployment readiness" if variant == "a" else
                    "after full fleet readiness and 15s stable window"),
            "includes_cluster_creation": False,
        },
        "incident": {
            "start_utc_ns": times[2], "end_utc_ns": times[3],
            "start": "before changing processing_delay_ms via ROS parameter service",
            "end": "when runner observes a terminal outcome or reaches the deadline",
            "reaction_from_first_slo_violation_measured": False,
        },
    }


if __name__ == "__main__":
    result = boundaries(*sys.argv[2:])
    Path(sys.argv[1]).write_text(json.dumps(result, indent=2) + "\n")
