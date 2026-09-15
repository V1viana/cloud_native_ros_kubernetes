# Third-Party Provenance Register

## 1. Scopo

Questo registro identifica origine, licenza e stato di riuso dei componenti esterni candidati per cloud_native_ros_kubernetes.

Non sostituisce i testi delle licenze. Evita che file locali, immagini container o modifiche non tracciate vengano presentati come codice riproducibile proveniente da un commit noto.

## 2. Stati

| Stato | Significato |
| --- | --- |
| APPROVED_REFERENCE | riferimento tecnico senza copia |
| APPROVED_ADAPT | copiabile/adattabile dopo registrazione della destinazione |
| EXTERNAL_PIN | dipendenza usata a versione o commit fissato |
| BLOCKED_PROVENANCE | commit o origine del contenuto non identificati |
| BLOCKED_LICENSE | licenza assente, ambigua o incoerente |
| EXCLUDED | fuori dallo scope target |

## 3. Repository Sorgente

### 3.1 KubeROS

| Campo | Valore |
| --- | --- |
| Sorgente usata per l'audit | repository KubeROS esterno alla copia pubblicabile |
| Percorso integrazione | integrations/kuberos |
| Remote | https://github.com/V1viana/kuberos.git |
| Commit | d8ab5294a4cc05d58b529ca360bc1a09d842b107 |
| Data commit | 23 giugno 2026 |
| Licenza root | Apache License 2.0 |
| Stato | file candidati tracciati dal commit |
| Stato baseline | read-only; nessuna modifica runtime del progetto |
| Modifiche integrazione | Fleet/API core, update replace v1, executor Deployment, migrazione, validazione e documentazione; prototipo Fleet Edge escluso |
| Modifiche baseline estranee | nessuna inclusa nel progetto |

Obblighi principali per una distribuzione derivata:

- includere una copia della Apache License 2.0;
- mantenere gli avvisi pertinenti;
- indicare chiaramente i file modificati;
- non usare marchi o nomi come approvazione del nuovo progetto.

### 3.2 DroneKube Originale

| Campo | Valore |
| --- | --- |
| Sorgente usata per l'audit | repository DroneKube esterno alla copia pubblicabile |
| Remote | https://github.com/V1viana/dronekube.git |
| Commit root | 96a7676947ebc456da900fbea203313563d1832d |
| Data commit | 16 luglio 2026 |
| Licenza root | MIT, copyright 2023 ika/RWTH Aachen |
| Stato root | Docker, Kubernetes e docs tracciati |
| Stato sources/* | ignorato dalla .gitignore, non presente nel commit |

Il commit root non deve essere usato come provenienza dei file locali sotto sources/*.

### 3.3 Application Manager Upstream Pinned

| Campo | Valore |
| --- | --- |
| Percorso | integrations/robotkube/application_manager |
| Remote | https://github.com/ika-rwth-aachen/application_manager.git |
| Commit | 8ddb99a70f7f4f571cbaaa0ccea19fb3432424e0 |
| Data commit | 19 marzo 2026 |
| Licenza | MIT, copyright 2025 ika/RWTH Aachen |
| Stato | EXTERNAL_PIN, detached HEAD pulito |

Il repository contiene `application_manager`, `application_manager_interfaces`
e la Action `DeploymentRequest`. La copia locale sotto DroneKube resta
BLOCKED_PROVENANCE e non viene usata come sorgente.

### 3.4 Event Detector Runtime DroneKube

Il Dockerfile tracciato usa:

~~~text
FROM vivianacasale/event-detector-mavros-battery:runtime
~~~

L'immagine fornisce event_detector, assente dal repository root. Tag, digest, sorgente e licenza non sono fissati.

Stato: BLOCKED_PROVENANCE. L'immagine non sara' usata come base del nuovo progetto.

### 3.5 Event Detector Upstream Pinned

| Campo | Valore |
| --- | --- |
| Percorso | integrations/robotkube/event_detector |
| Remote | https://github.com/ika-rwth-aachen/event_detector.git |
| Commit | 32f59d0c2ff1a8be4c48c96f10cee6f0edf6cdbf |
| Data commit | 14 aprile 2026 |
| Licenza | MIT, copyright 2025 ika/RWTH Aachen |
| Stato | EXTERNAL_PIN, detached HEAD pulito |

Questo e' il core ufficiale plugin-based. Le regole PX4 sono sviluppate in un
package separato senza copiare `px4_event_plugin` locale. La build Humble
applica una patch esplicita e tracciata per il campo timestamp di rosbag2;
il checkout upstream resta invariato.

### 3.6 Perception Interfaces Upstream Pinned

| Campo | Valore |
| --- | --- |
| Percorso | integrations/robotkube/perception_interfaces |
| Remote | https://github.com/ika-rwth-aachen/perception_interfaces.git |
| Commit | 1d1472f35f01ef2e575e75733c9bb2316099641c |
| Data commit | 13 maggio 2026 |
| Licenza | MIT, copyright 2025 ika/RWTH Aachen |
| Stato | EXTERNAL_PIN, detached HEAD pulito |

Il package `perception_msgs` e' una dipendenza di compilazione dichiarata
dall'Event Detector ufficiale. Rimane upstream invariato e non entra nella
logica PX4 del progetto.

### 3.7 PX4 Messages Pinned

| Campo | Valore |
| --- | --- |
| Percorso | integrations/px4_msgs |
| Upstream | https://github.com/PX4/px4_msgs.git |
| Sorgente verificata | checkout upstream separato, confrontato con il submodule pinned |
| Commit | 1f3cf7c2649d01c93158df8ea256a9c00611f812 |
| Licenza | BSD-3-Clause, copyright PX4 Development Team |
| Stato | EXTERNAL_PIN, checkout pulito |

La copia pinned e' compilata nell'immagine E2 e fornisce il tipo reale
`px4_msgs/msg/VehicleStatus` pubblicato da PX4 SITL e consumato dal plugin.

## 4. Inventario KubeROS

### 4.1 File KubeROS Adattato

| File al commit KubeROS | SHA-256 snapshot locale | Stato | Uso previsto |
| --- | --- | --- | --- |
| kuberos/pykuberos/scheduler/rosmodule.py | f3ecd37a9ae1adcfb3ff735a24a061e5377c7bca0cac3b7cc7689aef55ec18a7 | APPROVED_ADAPT | patch Deployment/probe |

Il prototipo monolitico con topic JSON `/kuberos/events`, Application Manager
sull'edge e relativi Dockerfile, generatori e manifest non fa parte del
progetto corrente. Rimane recuperabile nella baseline read-only e nella storia
Git precedente alla sua esclusione.

### 4.2 Altri Candidati KubeROS

| Area | Licenza | Stato | Azione |
| --- | --- | --- | --- |
| Django API/models/task | Apache-2.0 root | APPROVED_ADAPT | patch locale update v1; mantenere come fork o patch series tracciata |
| scheduler pykuberos | Apache-2.0 root | APPROVED_ADAPT | patch tracciabile o fork pinned |

## 5. Inventario Locale DroneKube

Questi hash identificano file presenti nel workspace, ma ignorati dal repository Git root.

| File locale | SHA-256 | Licenza osservata | Stato | Decisione |
| --- | --- | --- | --- | --- |
| sources/px4_event_plugin/.../Px4LowBatteryRule.cpp | a2075fb89a4024340f63b28365a561b7188665bb4ace42300eb0b6e8acf6fb06 | package dichiara TODO | BLOCKED_LICENSE | riscrivere |
| sources/px4_event_plugin/.../Px4ProximityRule.cpp | 346469126e9c5f5021f80d806877ed501a4f0d70f1b0fc9ff48a26e47d897d79 | package dichiara TODO | EXCLUDED | fuori scope |
| sources/application_manager/.../application_manager.py | 29f5e3b315a53d492cb3b4ae63f5279f2df99381f9104d36cbee11dacaacbcf2 | MIT nella directory | BLOCKED_PROVENANCE | adattare solo da upstream pinned |
| sources/application_manager/.../rosbag2_on_event_app.py | 97b1d25b8ce7824aaf729b6480f328d8b66213a74cd8b80e34d00eaa08e9ecda | MIT, modifica locale non attribuita | APPROVED_REFERENCE | riscrivere Job builder |
| sources/application_manager/.../DeploymentRequest.action | ff662516b14535a2c85daae9e3148fb72d7236f91e484cc9986f8efaae76cebc | MIT nella directory | APPROVED_REFERENCE | confrontare e adattare dall'upstream pinned |

Una licenza permissiva nella directory non risolve da sola la provenienza di modifiche locali non versionate. Il registro separa licenza e riproducibilita'.

## 6. Componenti Esclusi

| Componente | Origine | Motivo |
| --- | --- | --- |
| Proximity Rule | DroneKube locale | fuori dal caso d'uso scelto |
| MavrosLowBatteryRule | DroneKube locale | target PX4 uXRCE-DDS |
| Object Detection Fusion | Application Manager | caso C-ITS non necessario |
| MQTT custom operator | Application Manager | DDS e' il data plane baseline |
| sorgenti distributed/security | altri repository locali | esclusi esplicitamente |

## 7. Dipendenze Runtime Fissate

| Dipendenza | Riferimento immutabile o versione | Stato |
| --- | --- | --- |
| ROS 2 | `ros@sha256:5417e56962ff6e15d4cf9b2f78a71a78f3901f47cd78696b575b7eecdb54eb78` | digest registry fissato |
| PX4 SITL | `px4io/px4-sitl@sha256:b6bfb9e2aece2761ff78831c9bc6f13beb2840c36ba7e010f42b58f97924d2ab` | il tag `latest` della campagna e' risolto nel digest osservato |
| Micro XRCE-DDS Agent | `microros/micro-ros-agent@sha256:16280d0753fdce81413ed6280b93f3a800582dd2230620408c86b1b24ecb0686` | digest registry fissato |
| Control plane progetto | `vivianacasale/cloud-native-ros-kubernetes@sha256:5586f51f5be34bddf2a019d48eafb59b6df12f48a204e8f5d6c8354a6ee9bd18` | release privata `project-093e686` |
| Event Detector progetto | `vivianacasale/cloud-native-ros-kubernetes@sha256:90fef04e0feb816b8b94a6726e2f5c3bc0194712cebf8a81f2a4c98d6ac27f55` | release privata `project-093e686` |
| KubeROS adattato | `vivianacasale/cloud-native-ros-kubernetes@sha256:fc04411a21ef7ec1ee209aadc011db025778cb828cd6958843e897f9f7ac361a` | release privata `project-093e686` |
| Fast DDS | `ros-humble-fastrtps 2.6.11`, RMW `6.2.10` | versione letta nell'immagine Event Detector fissata |
| px4_msgs | `1f3cf7c2649d01c93158df8ea256a9c00611f812` | commit submodule fissato |
| Kubernetes API client | adapter HTTP Python standard library | nessuna dipendenza dal package `kubernetes` |
| k3d/k3s | k3d 5.8.3 / k3s 1.31.5-k3s1 | versioni registrate |

Il manifest completo e machine-readable e' in
`reproducibility.json` (output locale non versionato).
Le immagini originali della campagna restano fissate tramite image ID locale.
La release successiva `project-093e686` e' pubblicata nel repository privato
`vivianacasale/cloud-native-ros-kubernetes` e fissata dal lock
`project-093e686.lock.json` (output locale non versionato).
R1 ha verificato il pull autenticato del digest control-plane attraverso
KubeROS. E0 ha poi verificato live 16/16 container di progetto contro i tre
digest del lock; questa release resta distinta dagli artifact della campagna
storica. Redis e le immagini upstream sono fissate tramite repo digest.

## 8. Copy And Adapt Log

### Stato Corrente

KubeROS e' stato clonato localmente in integrations/kuberos dal commit
d8ab5294a4cc05d58b529ca360bc1a09d842b107. Il clone conserva la licenza
Apache-2.0 e contiene le modifiche del progetto, mentre la baseline originale
rimane read-only. `OperationalEvent`, `MetricSample` e la
`DeploymentRequest.action` adattata sono interfacce originali del progetto.
Event Detector e Application Manager upstream sono importati dai repository
ufficiali ika a commit fissati come dipendenze e riferimenti. Il nuovo package
`sources/cloud_native_application_manager` e' un'implementazione originale
Apache-2.0 del progetto: mantiene il pattern ROS 2 Action, ma non copia le
applicazioni object-detection, i custom operator o il codice locale DroneKube.

| Data | Destinazione | Origine | Commit/hash | Licenza | Tipo | Modifiche |
| --- | --- | --- | --- | --- | --- | --- |
| 2026-08-20 | integrations/kuberos | github.com/V1viana/kuberos | d8ab5294a4cc05d58b529ca360bc1a09d842b107 | Apache-2.0 | clone/adapt | API PATCH, revisione, audit, diff Pod/Service/ConfigMap, rollback e test |
| 2026-08-27 | integrations/kuberos | clone pinned precedente | working tree del progetto | Apache-2.0 | adapt | workload Deployment, AppsV1Api, probes, readiness, rolling reconciliation e test |
| 2026-08-27 | sources/cloud_native_application_manager | specifica del progetto + pattern Action upstream | implementazione originale | Apache-2.0 | new | catalogo P0-P2, deduplicazione, feedback, rollback e adapter REST KubeROS |
| 2026-08-31 | sources/cloud_native_application_manager/incident_reporter.py | specifica del progetto | implementazione originale | Apache-2.0 | adapt | outbox SQLite FIFO, replay dopo restart e consegna at-least-once su PVC |
| 2026-08-31 | sources/platform_observability/platform_observer.py | Kubernetes Metrics API | implementazione originale | Apache-2.0 | adapt | PodMetrics read-only, normalizzazione CPU/memoria e degradazione controllata |
| 2026-08-27 | sources/operational_event_dispatcher | specifica del progetto | implementazione originale | Apache-2.0 | new | bridge Topic OperationalEvent verso DeploymentRequest Action con deduplicazione |
| 2026-08-27 | sources/cloud_native_application_manager/kubernetes_adapter.py | documentazione API Kubernetes + specifica P1 | implementazione originale | Apache-2.0 | new | Event API, restart Deployment, Job diagnostico, polling rollout e recovery ROS correlato |
| 2026-08-21 | integrations/robotkube/event_detector | github.com/ika-rwth-aachen/event_detector | 32f59d0c2ff1a8be4c48c96f10cee6f0edf6cdbf | MIT | clone/pin | upstream invariato in detached HEAD |
| 2026-08-21 | integrations/robotkube/application_manager | github.com/ika-rwth-aachen/application_manager | 8ddb99a70f7f4f571cbaaa0ccea19fb3432424e0 | MIT | clone/pin | upstream invariato in detached HEAD |
| 2026-08-21 | integrations/robotkube/perception_interfaces | github.com/ika-rwth-aachen/perception_interfaces | 1d1472f35f01ef2e575e75733c9bb2316099641c | MIT | clone/pin | upstream invariato in detached HEAD; si usa il package perception_msgs |
| 2026-08-27 | integrations/px4_msgs | github.com/PX4/px4_msgs | 1f3cf7c2649d01c93158df8ea256a9c00611f812 | BSD-3-Clause | clone/pin | copia pulita usata per il tipo VehicleStatus nell'immagine E2 |
| 2026-08-28 | patches/robotkube-event-detector/0001-humble-rosbag2-timestamp.patch | event_detector upstream 32f59d0c | patch del progetto | MIT | adapt | compatibilita' ROS 2 Humble: `recv_timestamp` sostituito con `time_stamp` durante la build; checkout upstream invariato |
| 2026-08-21 | manifests/kuberos/update_demo/config/event-detector-discovery-drone01.xml | kuberos:kuberos_manifests/config/event-detector-discovery-drone01.xml | d8ab5294 / 510eeab67cf71d8197b0aad244333e531e8d7629af0fbea7c689d4123b91c713 | Apache-2.0 | copy | copia invariata per rendere la demo autonoma dalla baseline |
| 2026-08-21 | manifests/kuberos/update_demo/config/microxrce-agent-discovery-drone01.xml | kuberos:kuberos_manifests/config/microxrce-agent-discovery-drone01.xml | d8ab5294 / bdee2297048bc73611c576d78a921c62c8d32d5385edf589a544c919ed0887dd | Apache-2.0 | copy | copia invariata per rendere la demo autonoma dalla baseline |

### Template Per Importazioni Future

| Data | Destinazione | Origine | Commit/hash | Licenza | Tipo | Modifiche |
| --- | --- | --- | --- | --- | --- | --- |
| YYYY-MM-DD | path target | repository:path | commit/SHA-256 | SPDX | copy/adapt | descrizione |

## 9. Gate Di Conformita'

Prima di integrare un componente esterno:

- repository e commit identificati;
- file presente nel commit o hash locale classificato;
- licenza compatibile e inclusa;
- avvisi di copyright conservati;
- file modificato marcato quando richiesto;
- test target definito;
- nessuna dipendenza da immagini non pinned;
- destinazione registrata nel Copy And Adapt Log.

## 10. Questioni Aperte Residue

1. Ridurre la dimensione dell'immagine Event Detector.
2. Non copiare il vecchio px4_event_plugin locale finche' la licenza resta TODO.
3. Decidere se pubblicare `integrations/kuberos` come fork remoto o patch series.

La provenienza, i commit upstream e i digest runtime usati dalla piattaforma
sono fissati; questi punti riguardano manutenzione e distribuzione futura.
