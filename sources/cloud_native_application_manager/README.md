# Cloud-Native Application Manager

This package adapts the RobotKube Application Manager pattern to the project
architecture. The reusable part is the ROS 2 Action control loop; concrete
robot applications and custom resources from the upstream example are not
copied into the implementation.

Implemented boundaries:

- SQLite FIFO outbox with replay after process or Pod restart;
- at-least-once audit and notification delivery on a dedicated PVC;

- `DeploymentRequest` Action server at `/fleet/deployment_request`;
- strict P0/P1/P2 event-to-outcome catalog;
- bounded deduplication by `correlation_id`;
- observable accepted, acting, verifying and rollback phases;
- KubeROS REST adapter for create, status, delete and wait-ready;
- blue/green edge creation with best-effort delete on failure.

P0 records that RTL remains an onboard action. P1 restarts only the Micro
XRCE-DDS Agent and launches a bounded diagnostic Job. P2 becomes active when
`analytics_manifest_path` is configured with a KubeROS
`ApplicationDeployment` template. The Kubernetes manifest runs the manager as
a singleton `Recreate` Deployment and mounts its outbox from
`p2-manager-outbox`.
