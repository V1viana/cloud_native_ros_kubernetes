# Cloud Native Robotics Interfaces

Typed ROS 2 contracts shared by the event-detection and remediation pipeline.

## Messages

- `OperationalEvent`: normalized incident and recovery event.
- `MetricSample`: latency, queue-depth and CPU observation produced by a ROS 2 workload.
- `GetHealthSnapshot`: synchronous health and lifecycle snapshot for an analytics instance.

ROS 2 contracts shared by the per-robot Event Detector, the Application
Manager and observability components.

## Interfaces

- `OperationalEvent.msg` normalizes ROS, Kubernetes and infrastructure events.
- `MetricSample.msg` carries typed workload latency, queue and CPU evidence.
- `GetHealthSnapshot.srv` echoes a correlation ID and returns the current
  lifecycle state, health decision and metric values of one analytics instance.
- `DeploymentRequest.action` adapts the upstream RobotKube Action pattern to a
  normalized event, policy outcome, feedback, timeout and rollback result.

The upstream `DeploymentRequest` was audited but cannot represent these
requirements: it is tied to object-detection applications and returns only a
text message. Its repository remains pinned and unmodified; this package owns
the project-specific contract.

## Build

The target runtime is ROS 2 Humble. Message, Service and Action generation is
validated with the local ROS 2 Jazzy toolchain and in the complete Humble
control-plane image used by the live cluster experiments.

~~~bash
# dalla root del repository
colcon --log-base /tmp/cloud-native-ros-logs build \
  --base-paths interfaces \
  --packages-select cloud_native_robotics_interfaces \
  --build-base /tmp/cloud-native-ros-build \
  --install-base /tmp/cloud-native-ros-install
~~~

No cluster, container runtime or network access is required for this build.

## Stable Names

~~~text
/fleet/operational_events
/fleet/deployment_request
/<robot_id>/companion/<instance_id>/health
~~~

Every incident keeps the same `correlation_id` from its first operational
event through the final adapted `DeploymentRequest` outcome.
