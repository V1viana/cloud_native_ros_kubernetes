# KubeROS Differential Update Demo

This directory defines the first isolated KubeROS update experiment for
drone01.

## Revisions

| Manifest | Analytics placement | Expected outcome |
| --- | --- | --- |
| revision-1.yaml | onboard | initial deployment, simulated latency above SLO |
| revision-2.yaml | edge | only companion-analytics is replaced |
| revision-failure.yaml | edge | invalid analytics image triggers rollback |

PX4 SITL, Micro XRCE-DDS Agent and Event Detector are identical in all three
manifests. The deployment name, fleet, robot and middleware are also immutable.

The analytics image is a project-owned artifact that still has to be built.
Both valid revisions deliberately use the same image: placement and policy
configuration are the update variables. The failure revision uses an invalid
tag by design.

## Current Evidence

The offline suite already proves that:

- all three manifests satisfy the KubeROS input contract;
- deployment identity and the three control modules are invariant;
- revision 2 changes only analytics placement and its ConfigMap;
- the scheduler binds analytics to the edge selector;
- the failure revision differs from revision 2 only by its invalid image.

The U1 harness proves differential Kubernetes convergence on the E0
multi-robot baseline. Its authenticated PATCH changes only drone01 Companion
Analytics from 80 to 95 ms: KubeROS reaches revision 2 with `UPDATE SUCCESS`,
replaces 1/12 Pods, preserves 11/12 and returns a healthy ROS Service snapshot.
The definitive report is
`results/u1/20260901T194947Z/REPORT.md` (output locale non versionato).

U2 then submits an invalid analytics image. KubeROS records revision 3 as
`UPDATE FAILED`, keeps revision 2 active and restores analytics in 109 seconds.
All 11 non-target workloads preserve UID and restart count; the final Service
is `healthy/active`. Evidence and immutable image IDs are in
`results/u2/20260901T194947Z/REPORT.md` (output locale non versionato)
and its adjacent `reproducibility.json`.

## Offline Commands

~~~bash
# dalla root del repository
python3 scripts/render_update_demo_manifests.py
python3 scripts/render_update_demo_manifests.py --check
~~~

These commands generate the manifests from the base and variant sources and then verify that the generated files are current. They do not invoke
kubectl, k3d, Docker or the KubeROS API.

## Live Sequence

The complete live resilience path is automated by:

~~~bash
# dalla root del repository
scripts/run_u2.sh
~~~

The runner recreates only `cloud-native-p2`, establishes E0, applies the valid
U1 revision and then submits the invalid U2 revision. It requires `UPDATE
FAILED`, revision 2 restored, 11/11 non-target workloads unchanged and the
analytics health Service back to `healthy/active`. This KubeROS revision
rollback is separate from the Application Manager rollback verified by E4.
