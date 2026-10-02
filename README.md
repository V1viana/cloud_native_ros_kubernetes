# Cloud-Native ROS 2 and Kubernetes

Piattaforma sperimentale per orchestrare workload robotici ROS 2 e PX4 su
Kubernetes. Il repository contiene due varianti dello stesso sistema, che
condividono simulatore, workload e scenari:

- **Variante A, imperativa.** Un Event Detector pubblica eventi operativi, un
  Dispatcher li trasforma in richieste ROS 2 Action e un Application Manager
  esegue l'azione tramite KubeROS e le API Kubernetes.
- **Variante B, dichiarativa.** Lo stato della flotta e' descritto da quattro
  risorse Kubernetes (`RobotFleet`, `ROSModule`, `ROSLifecyclePolicy`,
  `AdaptationPolicy`), riconciliate da un Fleet Operator. Un sidecar State
  Bridge collega ogni modulo ROS 2 allo stato della sua risorsa.

In entrambe le varianti la reazione di sicurezza alla batteria bassa resta sul
drone e non dipende dal control plane.

## Componenti

| Piano | Componenti |
| --- | --- |
| Onboard, uno per drone | PX4 SITL, Micro XRCE-DDS Agent, Event Detector con le regole PX4, Companion Analytics |
| Control plane, variante A | KubeROS, Operational Event Dispatcher, Application Manager |
| Control plane, variante B | Fleet Operator e risorse `dronekube.io/v1alpha1` |
| Control plane, comuni | Fast DDS Discovery Server, Audit Writer, Operator Notifier, Platform Observer |
| Edge | Companion Analytics migrato, con HPA |

Il diagramma in [docs/diagrams](docs/diagrams/DISTRIBUTED_ARCHITECTURE.png)
mostra la variante A. Il Fleet Operator e le risorse della variante B sono
descritti in [operator/README.md](operator/README.md).

## Scenari

| ID | Scenario | Runner |
| --- | --- | --- |
| E0 | Flotta nominale, nessun guasto | `scripts/run_e0.sh` |
| E1 | Batteria bassa con control plane non raggiungibile | `scripts/run_e1.sh` |
| E2 | Perdita della telemetria | `scripts/run_e2.sh` |
| P2 | Latenza di analytics oltre soglia, migrazione verso l'edge | `scripts/run_p2.sh` |
| E4 | Migrazione fallita, ritorno onboard | `scripts/run_e4.sh` |
| U1, U2 | Aggiornamento di un modulo, aggiornamento non valido | `scripts/run_u1.sh`, `scripts/run_u2.sh` |
| S1 | Modifica fuori banda di un Deployment gestito | `scripts/run_s1.sh` |
| S2 | Transitorio dentro una partizione di rete | `scripts/run_s2.sh` |
| S3 | Scala della flotta | `scripts/run_s3.sh` |
| S4 | Guasti concorrenti | `scripts/run_s4_matrix.sh` |
| TTR | Ricostruzione della flotta da cluster vuoto | `scripts/ttr/run_ttr.sh` |

I runner creano un cluster k3d, costruiscono e importano le immagini, eseguono
lo scenario e salvano gli output in `results/`, che non e' versionata. La
variante si sceglie con `VARIANT=a` oppure `VARIANT=b`:

```bash
VARIANT=b scripts/run_p2.sh
N_ROBOTS=3 VARIANT=a scripts/run_s3.sh
```

`scripts/run_campaign.sh` ripete gli scenari di base per le due varianti.

## Struttura

```text
config/          catalogo e lock delle immagini, soglie
containers/      Dockerfile dei componenti
docs/            specifica, campagna, gap analysis, immagini e provenienza
integrations/    KubeROS adattato e dipendenze upstream fissate
interfaces/      messaggi, Service e Action ROS 2
manifests/       risorse Kubernetes e input KubeROS degli scenari
operator/        Fleet Operator, CRD e RBAC della variante B
patches/         patch per le dipendenze upstream
scripts/         runner degli scenari, osservatori e giudici
sources/         componenti ROS 2 e servizi del control plane
```

## Documentazione

- [Specifica architetturale](docs/SPECIFICA_ARCHITETTURALE.md)
- [Scenari e campagna sperimentale](docs/EXPERIMENT_CAMPAIGN.md)
- [Gap analysis: riuso, adattamenti e componenti nuovi](docs/GAP_ANALYSIS.md)
- [Immagini di progetto e registry](docs/IMAGE_REGISTRY.md)
- [Provenienza dei componenti](docs/THIRD_PARTY_PROVENANCE.md)

## Requisiti

Docker, k3d, kubectl e Python 3. Dopo il clone inizializzare i submodule:

```bash
git submodule update --init --recursive
```

## Componenti di terze parti

KubeROS e' incluso in `integrations/kuberos` con gli adattamenti del progetto.
Event Detector, Application Manager, Perception Interfaces e PX4 Messages sono
submodule fissati a un commit. Origine, licenze e modifiche sono elencate in
[docs/THIRD_PARTY_PROVENANCE.md](docs/THIRD_PARTY_PROVENANCE.md) e in
[integrations/README.md](integrations/README.md).

## Licenza

Il codice del progetto e' distribuito con licenza Apache 2.0: vedere
[LICENSE](LICENSE) e [NOTICE](NOTICE). I componenti di terze parti mantengono
la propria licenza.
