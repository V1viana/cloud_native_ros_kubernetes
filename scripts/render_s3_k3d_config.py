#!/usr/bin/env python3
"""Render an S3 k3d cluster config for N onboard robots + 1 control-plane +
1 edge node, generalizing manifests/kubernetes/e0/k3d-cloud-native-e0.yaml
(which hardcodes exactly 3 robots) to an arbitrary fleet size -- S3's own
scalability sweep (proposal S7/S8/S9: "N in {3,10,20,30,50}").

One dedicated onboard node per robot, same as E0/S4: full PX4 SITL fidelity
per drone (a deliberate choice for this sweep, see run_s3.sh's own header),
not a lighter per-robot stand-in.

All agents are created together in one "k3d cluster create" call. A
staggered, "k3d node create"-based bring-up was tried live at N=20 to
work around what looked like a cluster-creation-time contention issue,
but the actual cause turned out to be an unrelated bad kubelet flag
(see run_s3.sh's own history/comments) -- plain concurrent creation
already worked fine before that flag was introduced. Staggering was also
found to be incompatible with variant A regardless: KubeROS's own
"initialize" step looks up onboard/edge nodes by their literal k8s Node
name (KUBEROS_ONBOARD_NODE), and "k3d node create" cannot reproduce the
"k3d-<cluster>-agent-N" name exactly (it always appends its own replica
suffix) -- only variant B (label-based nodeSelectors) would have
tolerated it.
"""

import argparse


def render(n_robots, cluster_name="cloud-native-s3"):
    total_agents = n_robots + 1  # + edge
    lines = [
        "apiVersion: k3d.io/v1alpha5",
        "kind: Simple",
        "",
        "metadata:",
        f"  name: {cluster_name}",
        "",
        "servers: 1",
        f"agents: {total_agents}",
        "",
        "kubeAPI:",
        '  hostIP: "127.0.0.1"',
        '  hostPort: "6552"',
        "",
        "options:",
        "  k3d:",
        "    wait: true",
        # Found live at N=20: 600s was occasionally too tight for 21+
        # k3s agents to all finish registering when starting concurrently
        # under host CPU contention (one agent hit "context deadline
        # exceeded" waiting to register, rolling back the whole cluster) --
        # this only affects how patient cluster *creation* is, not
        # anything measured by the experiment itself.
        '    timeout: "900s"',
        "  k3s:",
        "    extraArgs:",
        # etcd embedded, R14 target backend (Viviana, 2026-10-01), as every scenario
        '      - arg: "--cluster-init"',
        '        nodeFilters: ["server:0"]',
        '      - arg: "--etcd-expose-metrics"',
        '        nodeFilters: ["server:0"]',
        '      - arg: "--disable=traefik"',
        '        nodeFilters: ["server:*"]',
        '      - arg: "--disable=servicelb"',
        '        nodeFilters: ["server:*"]',
        "    nodeLabels:",
        "      - label: kuberos.io/role=control_plane",
        '        nodeFilters: ["server:0"]',
        # device.kuberos.io/hostname must match the k3d- prefixed node
        # names KubeROS itself uses for scheduling (KUBEROS_ONBOARD_NODE,
        # rendered from render_s3_imperative_manifests.py's build_robots(),
        # which mirrors E0's own render_e0_manifests.py ROBOTS convention:
        # "k3d-<cluster>-agent-N") -- found live: without the prefix here,
        # variant A's onboard/edge pods stayed Pending forever
        # (0/5 nodes matched node affinity/selector), while variant B never
        # hit this since it schedules by robot.kuberos.io/id instead.
        f"      - label: device.kuberos.io/hostname=k3d-{cluster_name}-server-0",
        '        nodeFilters: ["server:0"]',
    ]
    onboard_filters = ", ".join(f'"agent:{i}"' for i in range(n_robots))
    lines.append("      - label: kuberos.io/role=onboard")
    lines.append(f"        nodeFilters: [{onboard_filters}]")
    for i in range(n_robots):
        robot_id = f"drone{i + 1:02d}"
        lines.append(f"      - label: device.kuberos.io/hostname=k3d-{cluster_name}-agent-{i}")
        lines.append(f'        nodeFilters: ["agent:{i}"]')
        lines.append(f"      - label: robot.kuberos.io/name={robot_id}")
        lines.append(f'        nodeFilters: ["agent:{i}"]')
        lines.append(f"      - label: robot.kuberos.io/id={robot_id}")
        lines.append(f'        nodeFilters: ["agent:{i}"]')
    lines.append("      - label: kuberos.io/role=edge")
    lines.append(f'        nodeFilters: ["agent:{n_robots}"]')
    lines.append(f"      - label: device.kuberos.io/hostname=k3d-{cluster_name}-agent-{n_robots}")
    lines.append(f'        nodeFilters: ["agent:{n_robots}"]')
    lines.append("")
    return "\n".join(lines)


def main(args=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--n-robots", type=int, required=True)
    parser.add_argument("--cluster-name", default="cloud-native-s3")
    parser.add_argument("--output", required=True)
    parsed = parser.parse_args(args)
    with open(parsed.output, "w", encoding="utf-8") as stream:
        stream.write(render(parsed.n_robots, parsed.cluster_name))
    print(parsed.output)


if __name__ == "__main__":
    main()
