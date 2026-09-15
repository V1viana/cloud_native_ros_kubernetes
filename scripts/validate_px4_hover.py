#!/usr/bin/env python3
"""Validate PX4 internal state before and across an armed-hover fault."""

import argparse
import json
import re
from pathlib import Path


PHASES = ("before", "during", "after")


def parse_fields(path):
    fields = {}
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        match = re.match(r"^\s*([a-zA-Z0-9_]+):\s*(\S+)", line)
        if match:
            fields[match.group(1)] = match.group(2)
    return fields


def validate_hover_ready(
    status_path,
    position_path,
    min_altitude_m=1.0,
    max_altitude_m=4.0,
    max_vertical_speed_m_s=0.5,
):
    """Validate the pre-fault armed-hover precondition."""
    status = parse_fields(status_path)
    position = parse_fields(position_path)
    required_status = {"arming_state", "nav_state", "failsafe"}
    required_position = {"z", "z_valid", "vz"}
    if not required_status.issubset(status):
        raise ValueError("Missing PX4 status fields for hover gate")
    if not required_position.issubset(position):
        raise ValueError("Missing PX4 position fields for hover gate")

    altitude_m = -float(position["z"])
    vertical_speed_m_s = float(position["vz"])
    result = {
        "arming_state": int(status["arming_state"]),
        "nav_state": int(status["nav_state"]),
        "failsafe": status["failsafe"].lower() == "true",
        "altitude_m": round(altitude_m, 3),
        "vertical_speed_m_s": round(vertical_speed_m_s, 3),
        "z_valid": position["z_valid"].lower() == "true",
    }
    result["pass"] = all(
        (
            result["arming_state"] == 2,
            result["nav_state"] == 4,
            not result["failsafe"],
            result["z_valid"],
            min_altitude_m <= altitude_m <= max_altitude_m,
            abs(vertical_speed_m_s) <= max_vertical_speed_m_s,
        )
    )
    return result


def validate_hover(status_paths, position_paths, max_altitude_spread_m=1.0):
    snapshots = {}
    altitudes = []
    for phase in PHASES:
        status = parse_fields(status_paths[phase])
        position = parse_fields(position_paths[phase])
        required_status = {"arming_state", "nav_state", "failsafe"}
        required_position = {"z", "z_valid", "vz"}
        if not required_status.issubset(status):
            raise ValueError(f"Missing PX4 status fields for phase {phase}")
        if not required_position.issubset(position):
            raise ValueError(f"Missing PX4 position fields for phase {phase}")

        altitude_m = -float(position["z"])
        vertical_speed_m_s = float(position["vz"])
        snapshot = {
            "arming_state": int(status["arming_state"]),
            "nav_state": int(status["nav_state"]),
            "failsafe": status["failsafe"].lower() == "true",
            "altitude_m": round(altitude_m, 3),
            "vertical_speed_m_s": round(vertical_speed_m_s, 3),
            "z_valid": position["z_valid"].lower() == "true",
        }
        snapshots[phase] = snapshot
        altitudes.append(altitude_m)

    armed_all = all(item["arming_state"] == 2 for item in snapshots.values())
    hold_all = all(item["nav_state"] == 4 for item in snapshots.values())
    no_failsafe = all(not item["failsafe"] for item in snapshots.values())
    position_valid = all(item["z_valid"] for item in snapshots.values())
    altitude_in_hover_band = all(1.0 <= value <= 4.0 for value in altitudes)
    altitude_spread_m = max(altitudes) - min(altitudes)
    altitude_stable = altitude_spread_m <= max_altitude_spread_m
    passed = all(
        (
            armed_all,
            hold_all,
            no_failsafe,
            position_valid,
            altitude_in_hover_band,
            altitude_stable,
        )
    )
    return {
        "pass": passed,
        "armed_all": armed_all,
        "hold_all": hold_all,
        "no_failsafe": no_failsafe,
        "position_valid": position_valid,
        "altitude_in_hover_band": altitude_in_hover_band,
        "altitude_stable": altitude_stable,
        "altitude_spread_m": round(altitude_spread_m, 3),
        "snapshots": snapshots,
    }


def main():
    parser = argparse.ArgumentParser()
    for phase in PHASES:
        parser.add_argument(f"--status-{phase}")
        parser.add_argument(f"--position-{phase}")
    parser.add_argument("--status-current")
    parser.add_argument("--position-current")
    parser.add_argument("--max-altitude-spread-m", type=float, default=1.0)
    parser.add_argument("--max-vertical-speed-m-s", type=float, default=0.5)
    args = parser.parse_args()

    if args.status_current or args.position_current:
        if not (args.status_current and args.position_current):
            parser.error("current gate requires status and position")
        result = validate_hover_ready(
            args.status_current,
            args.position_current,
            max_vertical_speed_m_s=args.max_vertical_speed_m_s,
        )
        print(json.dumps(result, indent=2, sort_keys=True))
        raise SystemExit(0 if result["pass"] else 1)

    missing = [
        option
        for phase in PHASES
        for option in (f"status_{phase}", f"position_{phase}")
        if not getattr(args, option)
    ]
    if missing:
        parser.error("three-phase validation requires all snapshots")
    status_paths = {
        phase: getattr(args, f"status_{phase}") for phase in PHASES
    }
    position_paths = {
        phase: getattr(args, f"position_{phase}") for phase in PHASES
    }
    result = validate_hover(
        status_paths,
        position_paths,
        args.max_altitude_spread_m,
    )
    print(json.dumps(result, indent=2, sort_keys=True))
    raise SystemExit(0 if result["pass"] else 1)


if __name__ == "__main__":
    main()
