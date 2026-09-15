# E0 - Baseline Nominale Distribuita

E0 verifica il comportamento nominale della piattaforma su tre droni PX4 SITL.
Ogni drone dispone di un nodo onboard distinto con quattro moduli:

- PX4 SITL;
- Micro XRCE-DDS Agent;
- Event Detector upstream con plugin PX4;
- Companion Analytics Lifecycle Node.

Il cluster comprende inoltre un nodo control plane e un nodo edge. I 12
workload robotici non vengono applicati direttamente con `kubectl`: un Job
autenticato invia tre `ApplicationDeployment` alla API KubeROS e attende lo
stato `running`. Kubernetes gestisce AS-IS namespace, RBAC, Service, ConfigMap,
Deployment, Job, probe, PVC ed Event API.

I tre PX4 condividono ROS domain e Fast DDS Discovery Server, ma pubblicano nei
namespace `/drone01`, `/drone02` e `/drone03`. L'analytics simula 80 ms di
latenza, sotto la soglia SLO di 250 ms; non e' quindi attesa alcuna remediation.

## Criteri Di Accettazione

- 3/3 `ApplicationDeployment` KubeROS raggiungono `running`;
- 12/12 Deployment robotici hanno label `managed-by=kuberos`;
- i quattro moduli di ogni drone sono collocati sul rispettivo nodo onboard;
- i topic `/droneXX/fmu/out/vehicle_status_v4` sono osservabili;
- Event Detector e Companion Analytics sono Lifecycle `active`;
- il Service `/<robot>/companion/onboard/health` risponde `healthy` e `active`;
- UID e restart count dei tre PX4 restano invariati per 60 secondi;
- routing `onboard`, workload edge e HPA assenti;
- nessuna `DeploymentRequest` o incidente, con snapshot persistenti sul PVC.

## Esecuzione

Per ricreare esclusivamente il cluster dedicato E0/P2:

```bash
RESET_E0=1 scripts/run_e0.sh
```

Per usare la release privata immutabile, con Docker gia' autenticato:

```bash
IMAGE_LOCK_FILE=config/project-image-lock.json \
  RESET_E0=1 scripts/run_e0.sh
```

Per una verifica locale si puo' ridurre `E0_OBSERVATION_WINDOW_SEC`; l'evidenza
ufficiale mantiene 60 secondi. `SKIP_IMAGE_BUILD_IMPORT=1` e' utilizzabile solo
quando le immagini corrette sono gia' presenti su tutti i nodi del cluster.

## Ultima Verifica Live

Il run del 30 agosto 2026 e' concluso con `PASS`:

- topologia 1 control plane, 3 onboard e 1 edge;
- 3/3 richieste KubeROS in `running`;
- 12/12 Deployment KubeROS e 19/19 Deployment complessivi disponibili;
- tre namespace PX4 distinti osservati tramite ROS 2 discovery;
- tutti i Lifecycle `active`, route `onboard`, edge e HPA assenti;
- tre risposte GetHealthSnapshot `healthy/active` con correlation ID per-drone;
- tre UID PX4 invariati e restart count `0 -> 0`;
- snapshot durevoli sul PVC, zero richieste e zero incidenti.

Le evidenze complete sono nel
report E0 (output locale non versionato), con le risposte
grezze in `health-snapshots.txt`.

Il 2 settembre 2026 una seconda prova E0 abbreviata e' terminata `PASS`
usando il lock della release privata: 16/16 container di progetto hanno
mostrato un `imageID` coerente con il digest richiesto. Questa prova di
distribuzione non sostituisce ne' altera la campagna statistica precedente.
Le evidenze sono nel report E0 digest (output locale non versionato).
