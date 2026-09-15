# R1 Private Registry KubeROS Smoke Result

| Campo | Valore |
| --- | --- |
| Esito | true |
| Cluster / namespace | cloud-native-p2 / cloud-native-p2 |
| ApplicationDeployment | registry-smoke-drone01 |
| Immagine richiesta | vivianacasale/cloud-native-ros-kubernetes@sha256:5586f51f5be34bddf2a019d48eafb59b6df12f48a204e8f5d6c8354a6ee9bd18 |
| Image ID osservato | docker.io/vivianacasale/cloud-native-ros-kubernetes@sha256:5586f51f5be34bddf2a019d48eafb59b6df12f48a204e8f5d6c8354a6ee9bd18 |
| Pull secret nel Deployment KubeROS | kuberos-test-repo |
| Nodo | k3d-cloud-native-p2-agent-0 |
| Workload finale | eliminato tramite API KubeROS |

KubeROS ha creato un Deployment ROS 2 temporaneo usando esclusivamente il
digest del repository Docker Hub privato. Kubernetes ha risolto il manifest
con il pull secret dichiarato nell'ApplicationDeployment; il Pod e' diventato
Ready sul nodo onboard e il workload e' stato poi eliminato tramite KubeROS.
