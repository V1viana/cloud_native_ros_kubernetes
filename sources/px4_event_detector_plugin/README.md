# PX4 Event Detector Plugin

Project-owned rules loaded by the pinned upstream `event_detector` through
`pluginlib`. The package does not modify the upstream core.

## Rules

- `BatteryLowRule`: detects three fresh samples below the configured threshold,
  publishes a normalized event and commands PX4 RTL locally with bounded retry.
- `TelemetryHeartbeatRule`: detects missing `VehicleStatus` after startup
  grace and emits a correlated recovery only after enough samples and a
  configurable stable interval.
- `AnalyticsLatencySloRule`: computes p95 over fixed windows and detects
  sustained SLO violation and recovery.

The rules only detect events and perform the local battery safety command.
Kubernetes and KubeROS operations belong to the control-plane Application Manager.

Parameters are loaded when the Event Detector is configured. Updating rule
parameters through KubeROS therefore requires a declarative detector rollout;
hot reconfiguration is outside the initial scope.

## Compatibility

The production image builds the pinned upstream Event Detector and this plugin
on ROS 2 Humble. A separate build patch maps the newer rosbag2
`recv_timestamp` field to Humble's `time_stamp`; the upstream checkout
remains unchanged. The pinned `px4_msgs` commit is
`1f3cf7c2649d01c93158df8ea256a9c00611f812`.

## Verification Status

- static package, plugin registry and parameter contracts: PASS;
- ROS interface generation on Jazzy: PASS;
- `px4_msgs` build at the recorded commit on Jazzy: PASS;
- full Event Detector/plugin build on Humble: PASS;
- lifecycle active with the telemetry-only E2 profile: PASS;
- state-machine C++ tests: PASS;
- PX4 SITL -> Agent -> plugin runtime and fault/recovery P1: PASS;
- AnalyticsLatency live migration and rollback campaigns: PASS;
- BatteryLow live with PX4 RTL/ack/AUTO_RTL during control-plane partition:
  PASS.
