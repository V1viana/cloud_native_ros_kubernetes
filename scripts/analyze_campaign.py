#!/usr/bin/env python3
"""Normalize experiment reports and compute campaign statistics."""

import argparse
import csv
import json
import math
import re
import statistics
from datetime import datetime
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]

FIELDS = [
    "scenario",
    "run_id",
    "result_dir",
    "passed",
    "valid_trial",
    "failure_phase",
    "correlation_id",
    "reaction_time_ms",
    "ack_latency_ms",
    "recovery_time_ms",
    "convergence_time_ms",
    "manager_duration_ms",
    "altitude_spread_m",
    "rollback_performed",
    "mission_continuity",
    "diagnostics_complete",
]
NUMERIC_FIELDS = [
    "reaction_time_ms",
    "ack_latency_ms",
    "recovery_time_ms",
    "convergence_time_ms",
    "manager_duration_ms",
    "altitude_spread_m",
]
T_CRITICAL_95 = {
    1: 12.706,
    2: 4.303,
    3: 3.182,
    4: 2.776,
    5: 2.571,
    6: 2.447,
    7: 2.365,
    8: 2.306,
    9: 2.262,
    10: 2.228,
    11: 2.201,
    12: 2.179,
    13: 2.160,
    14: 2.145,
    15: 2.131,
    16: 2.120,
    17: 2.110,
    18: 2.101,
    19: 2.093,
    20: 2.086,
    21: 2.080,
    22: 2.074,
    23: 2.069,
    24: 2.064,
    25: 2.060,
    26: 2.056,
    27: 2.052,
    28: 2.048,
    29: 2.045,
    30: 2.042,
}


def parse_table(path):
    """Return first-column to second-column values from a Markdown table."""
    values = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.startswith("|"):
            continue
        cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
        if len(cells) >= 2 and cells[0] not in {"Campo", "---"}:
            values[cells[0]] = cells[1]
    return values


def table_value(table, prefix, default=""):
    for key, value in table.items():
        if key.startswith(prefix):
            return value
    return default


def parse_bool(value):
    normalized = str(value).strip().lower()
    if normalized in {"true", "yes", "pass", "passed"}:
        return True
    if normalized in {"false", "no", "fail", "failed"}:
        return False
    return None


def parse_number(value):
    match = re.search(r"[-+]?[0-9]+(?:[.][0-9]+)?", str(value))
    return float(match.group(0)) if match else None


def same_px4_identity(table):
    combined = table_value(table, "PX4 UID prima/dopo")
    if combined and "/" in combined:
        before, after = [item.strip() for item in combined.split("/", 1)]
    else:
        before = table_value(table, "PX4 UID prima")
        after = table_value(table, "PX4 UID dopo")
    restarts = table_value(table, "PX4 restart prima/dopo")
    restart_ok = True
    if restarts and "/" in restarts:
        left, right = [item.strip() for item in restarts.split("/", 1)]
        restart_ok = left == right
    return bool(before and after and before == after and restart_ok)


def parse_timestamp(value):
    if not value:
        return None
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def latest_incident(result_dir, correlation_id):
    audit_path = result_dir / "audit.jsonl"
    if not audit_path.exists():
        return None
    matches = []
    for line in audit_path.read_text(encoding="utf-8").splitlines():
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        if record.get("record_type") != "incident_completed":
            continue
        if correlation_id and record.get("correlation_id") != correlation_id:
            continue
        matches.append(record)
    return matches[-1] if matches else None


def manager_duration_ms(record):
    if not record:
        return None
    start = parse_timestamp(record.get("manager_received_timestamp"))
    end = parse_timestamp(record.get("completed_timestamp"))
    if not start or not end:
        return None
    return round((end - start).total_seconds() * 1000.0, 3)


def e0_continuity(result_dir):
    path = result_dir / "robot-state.csv"
    if not path.exists():
        return None
    with path.open(encoding="utf-8", newline="") as stream:
        rows = list(csv.DictReader(stream))
    return bool(rows) and all(
        row["uid_before"] == row["uid_after"]
        and row["restarts_before"] == row["restarts_after"]
        for row in rows
    )

def e2_precondition_valid(result_dir):
    """Infer whether a legacy E2 run met the pre-fault hover gate."""
    path = result_dir / "mission-continuity.json"
    if not path.exists():
        return None
    try:
        before = json.loads(path.read_text(encoding="utf-8"))["snapshots"]["before"]
    except (KeyError, TypeError, json.JSONDecodeError):
        return None
    required = {
        "arming_state",
        "nav_state",
        "failsafe",
        "altitude_m",
        "vertical_speed_m_s",
        "z_valid",
    }
    if not required.issubset(before):
        return None
    return all(
        (
            before["arming_state"] == 2,
            before["nav_state"] == 4,
            not before["failsafe"],
            before["z_valid"],
            1.0 <= before["altitude_m"] <= 4.0,
            abs(before["vertical_speed_m_s"]) <= 0.5,
        )
    )


def parse_result(scenario, result_dir):
    """Normalize one result directory into a stable campaign row."""
    scenario = scenario.lower()
    result_dir = Path(result_dir)
    if not result_dir.is_absolute():
        result_dir = PROJECT_ROOT / result_dir
    result_dir = result_dir.resolve()
    report = result_dir / "REPORT.md"
    if not report.exists():
        raise ValueError(f"missing report: {report}")
    table = parse_table(report)
    correlation_id = table_value(table, "Correlation ID")
    incident = latest_incident(result_dir, correlation_id)
    explicit_validity = parse_bool(table_value(table, "Validita campione"))
    try:
        portable_result_dir = str(result_dir.relative_to(PROJECT_ROOT))
    except ValueError:
        portable_result_dir = str(result_dir)
    row = {field: None for field in FIELDS}
    row.update(
        {
            "scenario": scenario,
            "run_id": result_dir.name,
            "result_dir": portable_result_dir,
            "passed": parse_bool(table_value(table, "Esito")),
            "valid_trial": (
                True if explicit_validity is None else explicit_validity
            ),
            "failure_phase": table_value(table, "Failure phase") or None,
            "correlation_id": correlation_id or None,
            "manager_duration_ms": manager_duration_ms(incident),
            "rollback_performed": (
                bool(incident.get("rollback_performed")) if incident else False
            ),
        }
    )

    if scenario == "e0":
        row["mission_continuity"] = e0_continuity(result_dir)
    elif scenario == "e1":
        row["reaction_time_ms"] = parse_number(
            table_value(table, "Latenza fault -> comando RTL")
        )
        row["ack_latency_ms"] = parse_number(
            table_value(table, "Latenza comando -> ack")
        )
        row["mission_continuity"] = same_px4_identity(table)
    elif scenario == "e2":
        if explicit_validity is None and e2_precondition_valid(result_dir) is False:
            row["valid_trial"] = False
            row["failure_phase"] = "precondition"
        elapsed_ms = parse_number(table_value(table, "Recovery end-to-end"))
        row["recovery_time_ms"] = (
            elapsed_ms if row["passed"] is True else None
        )
        row["convergence_time_ms"] = elapsed_ms
        row["altitude_spread_m"] = parse_number(
            table_value(table, "Variazione massima quota")
        )
        row["mission_continuity"] = parse_bool(
            table_value(table, "Continuita")
        ) and same_px4_identity(table)
        jobs = parse_number(table_value(table, "Job diagnostici")) or 0
        records = parse_number(table_value(table, "Record diagnostici")) or 0
        row["diagnostics_complete"] = jobs >= 1 and records >= 1
    elif scenario in {"p2", "e3", "e4"}:
        seconds = parse_number(table_value(table, "Durata osservata"))
        row["convergence_time_ms"] = seconds * 1000.0 if seconds is not None else None
        row["mission_continuity"] = same_px4_identity(table)
    else:
        raise ValueError(f"unsupported scenario: {scenario}")
    return row


def percentile(values, quantile):
    ordered = sorted(values)
    if not ordered:
        return None
    if len(ordered) == 1:
        return ordered[0]
    position = (len(ordered) - 1) * quantile
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return ordered[lower]
    weight = position - lower
    return ordered[lower] * (1.0 - weight) + ordered[upper] * weight


def numeric_summary(values):
    values = [float(value) for value in values if value is not None]
    if not values:
        return None
    mean = statistics.fmean(values)
    result = {
        "count": len(values),
        "mean": round(mean, 6),
        "median": round(statistics.median(values), 6),
        "p95": round(percentile(values, 0.95), 6),
        "min": round(min(values), 6),
        "max": round(max(values), 6),
        "ci95_low": None,
        "ci95_high": None,
    }
    if len(values) >= 2:
        critical = T_CRITICAL_95.get(len(values) - 1, 1.96)
        margin = critical * statistics.stdev(values) / math.sqrt(len(values))
        result["ci95_low"] = round(mean - margin, 6)
        result["ci95_high"] = round(mean + margin, 6)
    return result


def summarize(rows):
    summary = {}
    for scenario in sorted({row["scenario"] for row in rows}):
        selected = [row for row in rows if row["scenario"] == scenario]
        valid = [row for row in selected if row.get("valid_trial") is not False]
        mission = [
            row["mission_continuity"]
            for row in valid
            if row["mission_continuity"] is not None
        ]
        valid_count = len(valid)
        data = {
            "runs": len(selected),
            "valid_runs": valid_count,
            "invalid_runs": len(selected) - valid_count,
            "passed": sum(row["passed"] is True for row in valid),
            "pass_rate": (
                sum(row["passed"] is True for row in valid) / valid_count
                if valid_count else None
            ),
            "rollback_rate": (
                sum(row["rollback_performed"] is True for row in valid)
                / valid_count if valid_count else None
            ),
            "mission_success_rate": (
                sum(value is True for value in mission) / len(mission)
                if mission else None
            ),
            "metrics": {},
        }
        for field in NUMERIC_FIELDS:
            metric = numeric_summary([row[field] for row in valid])
            if metric:
                data["metrics"][field] = metric
        summary[scenario] = data
    return summary


def read_campaign(campaign_dir):
    rows = []
    for pointer in sorted((Path(campaign_dir) / "runs").glob("*/result-dir.txt")):
        result_dir = Path(pointer.read_text(encoding="utf-8").strip())
        status_path = pointer.parent / "status.json"
        status = json.loads(status_path.read_text(encoding="utf-8"))
        rows.append(parse_result(status["scenario"], result_dir))
    return rows


def write_outputs(rows, output_dir):
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    with (output_dir / "runs.csv").open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=FIELDS)
        writer.writeheader()
        writer.writerows(rows)
    summary = summarize(rows)
    (output_dir / "summary.json").write_text(
        json.dumps(summary, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    lines = ["# Campaign KPI Summary", ""]
    for scenario, data in summary.items():
        mission_rate = data["mission_success_rate"]
        mission_text = "n/a" if mission_rate is None else str(mission_rate)
        pass_rate = data["pass_rate"]
        pass_text = "n/a" if pass_rate is None else f"{pass_rate:.3f}"
        rollback_rate = data["rollback_rate"]
        rollback_text = (
            "n/a" if rollback_rate is None else f"{rollback_rate:.3f}"
        )
        lines.extend(
            [
                f"## {scenario.upper()}",
                "",
                "- Runs observed: {}".format(data["runs"]),
                "- Valid runs: {}".format(data["valid_runs"]),
                "- Invalid runs: {}".format(data["invalid_runs"]),
                "- Pass rate: {}".format(pass_text),
                "- Rollback rate: {}".format(rollback_text),
                "- Mission success rate: {}".format(mission_text),
                "",
                "| Metric | N | Mean | Median | P95 | CI95 |",
                "| --- | ---: | ---: | ---: | ---: | --- |",
            ]
        )
        for metric, stats in data["metrics"].items():
            ci = "n/a"
            if stats["ci95_low"] is not None:
                ci = "[{}, {}]".format(
                    stats["ci95_low"], stats["ci95_high"]
                )
            lines.append(
                "| {} | {} | {} | {} | {} | {} |".format(
                    metric, stats["count"], stats["mean"],
                    stats["median"], stats["p95"], ci,
                )
            )
        lines.append("")
    (output_dir / "REPORT.md").write_text("\n".join(lines), encoding="utf-8")
    return summary


def parse_explicit(values):
    rows = []
    for value in values:
        if "=" not in value:
            raise ValueError("--result requires SCENARIO=PATH")
        scenario, path = value.split("=", 1)
        rows.append(parse_result(scenario, path))
    return rows


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--campaign-dir", type=Path)
    parser.add_argument("--result", action="append", default=[])
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    if bool(args.campaign_dir) == bool(args.result):
        parser.error("use exactly one of --campaign-dir or --result")
    rows = read_campaign(args.campaign_dir) if args.campaign_dir else parse_explicit(args.result)
    if not rows:
        parser.error("no result directories found")
    write_outputs(rows, args.output_dir)
    print(f"Analyzed {len(rows)} runs into {args.output_dir}")


if __name__ == "__main__":
    main()
