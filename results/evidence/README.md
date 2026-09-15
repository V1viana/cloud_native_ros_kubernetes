# Published Evidence

This directory contains a compact export of live validations performed on the
multi-node k3d architecture. It is evidence of the event-driven implementation
described in the repository, not a synthetic benchmark and not a replacement
for the complete raw logs.

## Aggregate Campaign

The campaign covers E0, E1, E2, P2 and E4. Its summary deliberately retains
unsuccessful and invalid executions:

| Scenario | Observed | Valid | Passed | Pass rate | Mission success | Rollback rate |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| E0 | 10 | 10 | 9 | 90% | 100% | 0% |
| E1 | 10 | 10 | 10 | 100% | 100% | 0% |
| E2 | 11 | 10 | 9 | 90% | 100% | 0% |
| P2 | 10 | 10 | 10 | 100% | 100% | 0% |
| E4 | 10 | 10 | 10 | 100% | 100% | 100% |

Machine-readable data and the generated statistical report are available in
`campaign/`. Confidence intervals and percentiles are descriptive for this
controlled environment and should not be generalized to physical UAVs.

## Representative Live Runs

| File | Source run | Validation |
| --- | --- | --- |
| `runs/E0.md` | `20260830T214257Z` | nominal three-drone baseline |
| `runs/E0_REGISTRY.md` | `20260902T125748Z` | baseline from private image digests |
| `runs/E1.md` | `20260830T081937Z` | onboard BatteryLow/RTL with control plane stopped |
| `runs/E2.md` | `20260831T135149Z` | telemetry recovery during armed hover |
| `runs/P2.md` | `20260830T220735Z` | analytics migration to edge |
| `runs/E4.md` | `20260830T221320Z` | failed edge readiness and rollback onboard |
| `runs/U1.md` | `20260901T194947Z` | differential KubeROS update |
| `runs/U2.md` | `20260901T194947Z` | invalid-image rollback |
| `runs/OBSERVABILITY.md` | `20260831T144812Z` | durable outbox replay and Metrics API |
| `runs/REGISTRY.md` | `20260902T093427Z` | private image digest through KubeROS |

The representative files are successful examples, while `campaign/runs.csv`
preserves the complete campaign outcome distribution. Its `result_dir`
column preserves the original local run identifiers; the complete raw directories are
not part of this compact export. Identifiers such as Pod
UIDs and correlation IDs are runtime evidence, not credentials.

## Reproduction

Scenario definitions and commands are documented under
`manifests/kubernetes/`. `scripts/run_campaign.sh` generates new raw results;
`scripts/analyze_campaign.py` produces the aggregate statistics.
