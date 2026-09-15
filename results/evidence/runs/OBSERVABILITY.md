# Observability Recovery - Definitive Live Result

Date: 31 August 2026 UTC

## Outcome

**PASS** on the dedicated `cloud-native-p2` cluster.

The test stopped only the Audit Writer, emitted a normalized `BatteryLow`
incident handled by policy P0, restarted the fleet-level Application Manager,
and restored the Audit Writer. P0 remained observational: no Kubernetes safety
action was issued.

## Durable Reporter

| Check | Result |
| --- | --- |
| Manager strategy | `Recreate`, one active Pod |
| Pending before restart | 6 |
| Pending after restart | 6 |
| Pending after Audit Writer recovery | 0 |
| Replayed records | 6, FIFO, no duplicate |
| Correlation ID | `observability-replay-20260831T144812Z` |
| Manager UID | changed across restart |

The replay order was `incident_started`, three `incident_feedback` records,
`incident_completed`, and `operator_notification`. The SQLite outbox is mounted
from PVC `p2-manager-outbox`; rows are acknowledged only after successful HTTP
delivery. This provides at-least-once delivery across process and Pod restarts.

## Metrics API

The read-only Platform Observer successfully queried
`metrics.k8s.io/v1beta1` and persisted a normalized snapshot:

| Metric | Value |
| --- | ---: |
| Deployments available | 19/19 |
| Pods with metrics | 19 |
| Containers with metrics | 21 |
| CPU total | 1634.308873 millicores |
| Memory total | 842846208 bytes |

CPU and memory are also recorded per Pod. Lifecycle snapshots remain available
with `metrics_available=false` if Metrics Server is temporarily unavailable.

## Robotic Continuity

The drone01 PX4 Pod retained UID
`c9f37e20-ca52-4cfd-a2d5-f70f965a9ff3` and restart count `1 -> 1`. All 19
Deployments were Available at the end of the test.

## Evidence

- `summary.env`: machine-readable invariants.
- `metrics-snapshot.json`: live Kubernetes lifecycle and resource metrics.
- `replayed-audit.jsonl`: six correlated replayed records.
- `dispatcher.log`: Topic to Action completion.
- `manager-before-restart.log` and `manager-after-restart.log`: process boundary.
- `state-before.txt` and `state-after.txt`: cluster resources and placement.

A preceding pilot exposed overlapping consumers under `RollingUpdate`. The
Application Manager is now a singleton Deployment with `Recreate`, preventing
overlap during planned rollout; the durable protocol remains at-least-once for
an abrupt crash after a remote endpoint accepts a record but before local
acknowledgement.
