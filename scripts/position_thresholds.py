#!/usr/bin/env python3
"""Common position/altitude thresholds for E1 from the two pilots (checklist R8).

Rule fixed before the pilots (Viviana, 2026-09-25): one pilot per variant, the
same E1 timing with no fault and no partition; ONE pair of thresholds for A and
B, from the maximum over both pilots:
    threshold = max(3 x max pilot deviation, floor), floor 0.5 m horizontal, 0.3 m vertical.
A pilot whose own sampling was not sufficient (verdict other than "pilot"), or
whose vehicle was not steadily armed in Hold over the window (continuity_verdict
other than "true"), cannot set a threshold. Usage: position_thresholds.py PILOT_A.txt PILOT_B.txt OUT.json
(the pilots' position-hold.txt files, key=value).
"""

import json
import sys

FACTOR = 3.0
FLOOR_HORIZONTAL_M = 0.5
FLOOR_VERTICAL_M = 0.3


def read(path):
    return dict(line.split("=", 1) for line in open(path).read().splitlines() if "=" in line)


def thresholds(pilots):
    for path, values in pilots:
        if values.get("verdict") != "pilot":
            raise ValueError(f"{path}: pilot not usable ({values.get('verdict')}: {values.get('reasons')})")
        if values.get("continuity_verdict") != "true":
            raise ValueError(f"{path}: the vehicle was not steadily armed in Hold over the window")
    h = max(float(v["max_horizontal_m"]) for _, v in pilots)
    v = max(float(v["max_vertical_m"]) for _, v in pilots)
    return {
        "rule": f"max({FACTOR:g} x max deviation over both pilots, floor)",
        "horizontal_m": round(max(FACTOR * h, FLOOR_HORIZONTAL_M), 3),
        "vertical_m": round(max(FACTOR * v, FLOOR_VERTICAL_M), 3),
        "floor_horizontal_m": FLOOR_HORIZONTAL_M, "floor_vertical_m": FLOOR_VERTICAL_M,
        "pilots": [{"file": path, "max_horizontal_m": float(values["max_horizontal_m"]),
                    "max_vertical_m": float(values["max_vertical_m"]),
                    "truth_samples": int(values["truth_samples"]),
                    "window_sec": float(values["window_sec"])} for path, values in pilots],
    }


def main(argv):
    if len(argv) != 4:
        print(__doc__, file=sys.stderr)
        return 2
    result = thresholds([(argv[1], read(argv[1])), (argv[2], read(argv[2]))])
    with open(argv[3], "w") as handle:
        json.dump(result, handle, indent=2)
        handle.write("\n")
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
