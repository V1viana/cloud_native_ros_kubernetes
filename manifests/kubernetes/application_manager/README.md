# Application Manager Kubernetes Primitives

`rbac.yaml` grants the namespaced permissions required by policy P1:
Kubernetes Event creation, one Deployment restart, rollout observation and a
five-minute diagnostic Job.

The deployment name remains a ROS parameter because KubeROS resource names
depend on the selected robot and scheduler output. The default pattern is
`{robot_id}-microxrce-agent`, matching the resource rendered by KubeROS.

The Job uses the locally available `ros:humble-ros-base` image and records Pod
state plus `events.k8s.io/v1` data in JSON logs every ten seconds. Its
ServiceAccount intentionally has read-only Pod access and no Secret access.
