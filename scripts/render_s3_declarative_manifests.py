#!/usr/bin/env python3
"""Render S3's own N-robot declarative workload (variant B): N PX4+Agent
pairs (generalizing manifests/kubernetes/e0/40-shared-infra.yaml's own
hand-written 3-robot pattern), N ROSModule (generalizing e0/
60-declarative-workload.yaml), and one RobotFleet CR naming all N.

Companion Analytics only (no Event Detector/P0, no fault harness): S3
measures control-plane reconciliation behaviour and API-server/etcd load
as N grows, not a specific fault -- the one incident used to measure
reaction/recovery time under load (run_s3.sh) is the same SLO-migration
mechanism already validated in P2/E4/S4, applied to a single robot.
"""

import argparse
from pathlib import Path


def robot_id(i):
    return f"drone{i + 1:02d}"


# Mirrors render_s3_imperative_manifests.py's own S3_STARTUP_PROBE_*
# constants (variant A's own S3-only startup-probe loosening) -- variant B
# never had an equivalent until found live 2026-09-23: its Fleet-Operator-
# generated startupProbe stayed fixed at periodSeconds=3/timeoutSeconds=5
# in every scenario, and `ros2 lifecycle get`'s own graph discovery (which
# needs ROS_SUPER_CLIENT=TRUE regardless of target -- see
# k8s_workloads.py's own comment) was observed failing with "Node not
# found" well within 5s at just N=10, at low measured CPU. Scoped to S3's
# own ROSModules via spec.probes.startup; every other scenario leaves this
# field unset and keeps today's defaults.
S3_STARTUP_PROBE_PERIOD_SECONDS = 10
S3_STARTUP_PROBE_TIMEOUT_SECONDS = 20
S3_STARTUP_PROBE_FAILURE_THRESHOLD = 40


def shared_infra_yaml(n_robots, namespace, discovery_address):
    docs = []
    for i in range(n_robots):
        rid = robot_id(i)
        docs.append(f"""apiVersion: apps/v1
kind: Deployment
metadata:
  name: {rid}-microxrce-agent
  namespace: {namespace}
  labels: {{app.kubernetes.io/name: {rid}-microxrce-agent}}
spec:
  replicas: 1
  strategy: {{type: Recreate}}
  selector: {{matchLabels: {{app.kubernetes.io/name: {rid}-microxrce-agent}}}}
  template:
    metadata: {{labels: {{app.kubernetes.io/name: {rid}-microxrce-agent}}}}
    spec:
      hostNetwork: true
      dnsPolicy: ClusterFirstWithHostNet
      nodeSelector: {{kuberos.io/role: onboard, robot.kuberos.io/id: {rid}}}
      containers:
        - name: agent
          image: microros/micro-ros-agent:humble
          imagePullPolicy: IfNotPresent
          args: ["udp4", "--port", "8888"]
          env:
            - {{name: ROS_DOMAIN_ID, value: "230"}}
            - {{name: RMW_IMPLEMENTATION, value: rmw_fastrtps_cpp}}
            - {{name: ROS_DISCOVERY_SERVER, value: "{discovery_address}:11811"}}
          readinessProbe:
            exec: {{command: ["/bin/bash", "-c", "kill -0 1"]}}
            periodSeconds: 3
---
apiVersion: apps/v1
kind: Deployment
metadata:
  name: {rid}-px4-sitl
  namespace: {namespace}
  labels: {{app.kubernetes.io/name: {rid}-px4-sitl, cloud-native-robotics.io/criticality: flight-control}}
spec:
  replicas: 1
  strategy: {{type: Recreate}}
  selector: {{matchLabels: {{app.kubernetes.io/name: {rid}-px4-sitl}}}}
  template:
    metadata:
      labels: {{app.kubernetes.io/name: {rid}-px4-sitl, cloud-native-robotics.io/criticality: flight-control}}
    spec:
      hostNetwork: true
      dnsPolicy: ClusterFirstWithHostNet
      nodeSelector: {{kuberos.io/role: onboard, robot.kuberos.io/id: {rid}}}
      containers:
        - name: px4
          image: px4io/px4-sitl:latest
          imagePullPolicy: IfNotPresent
          workingDir: /opt/px4
          command: ["/bin/sh", "-c"]
          args:
            - |
              set -eu
              sed -i "s|uxrce_dds_client start -t udp|uxrce_dds_client start -t udp -h 127.0.0.1|" \\
                /opt/px4/etc/init.d-posix/rcS
              exec /opt/px4/bin/px4 -d -s /opt/px4/etc/init.d-posix/rcS /opt/px4
          env:
            - {{name: PX4_SIM_MODEL, value: sihsim_quadx}}
            - {{name: PX4_UXRCE_DDS_NS, value: {rid}}}
            - {{name: ROS_DOMAIN_ID, value: "230"}}
          resources:
            requests: {{cpu: 250m, memory: 128Mi}}
            limits: {{cpu: "1", memory: 512Mi}}
          readinessProbe:
            exec: {{command: ["/bin/sh", "-c", "kill -0 1"]}}
            periodSeconds: 5
""")
    return "---\n".join(doc.rstrip() + "\n" for doc in docs)


def rosmodules_yaml(n_robots, namespace):
    docs = []
    for i in range(n_robots):
        rid = robot_id(i)
        docs.append(f"""apiVersion: dronekube.io/v1alpha1
kind: ROSModule
metadata:
  name: companion-analytics-{rid}
  namespace: {namespace}
  labels: {{app: companion-analytics, robot: {rid}}}
spec:
  robotId: {rid}
  package: companion_analytics
  lifecycleTarget: Active
  placement: onboard
  rosParamMap:
    processing_delay_ms: "80.0"
    queue_depth: "4"
    cpu_percent: "25.0"
    sample_period_ms: "250"
  probes:
    startup:
      periodSeconds: {S3_STARTUP_PROBE_PERIOD_SECONDS}
      timeoutSeconds: {S3_STARTUP_PROBE_TIMEOUT_SECONDS}
      failureThreshold: {S3_STARTUP_PROBE_FAILURE_THRESHOLD}
""")
    return "---\n".join(doc.rstrip() + "\n" for doc in docs)


def robotfleet_yaml(n_robots, namespace):
    robots = "\n".join(f"    - {{id: {robot_id(i)}, role: onboard}}" for i in range(n_robots))
    return f"""apiVersion: dronekube.io/v1alpha1
kind: RobotFleet
metadata:
  name: px4-fleet
  namespace: {namespace}
spec:
  robots:
{robots}
  edgeNodeSelector:
    kuberos.io/role: edge
"""


def main(args=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--n-robots", type=int, required=True)
    parser.add_argument("--namespace", default="cloud-native-s3")
    parser.add_argument("--discovery-address", required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parsed = parser.parse_args(args)
    parsed.output_dir.mkdir(parents=True, exist_ok=True)

    (parsed.output_dir / "40-shared-infra.yaml").write_text(
        shared_infra_yaml(parsed.n_robots, parsed.namespace, parsed.discovery_address)
    )
    (parsed.output_dir / "60-declarative-workload.yaml").write_text(
        rosmodules_yaml(parsed.n_robots, parsed.namespace)
    )
    (parsed.output_dir / "70-robotfleet.yaml").write_text(
        robotfleet_yaml(parsed.n_robots, parsed.namespace)
    )
    for name in ("40-shared-infra.yaml", "60-declarative-workload.yaml", "70-robotfleet.yaml"):
        print(parsed.output_dir / name)


if __name__ == "__main__":
    main()
