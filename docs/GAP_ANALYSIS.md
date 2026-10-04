# Gap Analysis: Riuso, Adattamenti E Componenti Nuovi

Questo documento confronta la piattaforma con i progetti da cui parte e dice,
per ogni parte, se e' riusata, adattata o scritta per questo progetto. Origine,
commit e licenze sono nel [registro di provenienza](THIRD_PARTY_PROVENANCE.md).

## 1. Sorgenti Di Partenza

| Sorgente | Dove si trova | Licenza | Uso |
| --- | --- | --- | --- |
| KubeROS | `integrations/kuberos`, copia adattata | Apache 2.0 | livello ROS-aware della variante A |
| Event Detector (RobotKube) | submodule `integrations/robotkube/event_detector` | MIT | framework delle regole, eseguito |
| Application Manager (RobotKube) | submodule `integrations/robotkube/application_manager` | MIT | solo riferimento, non eseguito |
| Perception Interfaces (RobotKube) | submodule `integrations/robotkube/perception_interfaces` | MIT | dipendenza dell'Event Detector |
| PX4 Messages | submodule `integrations/px4_msgs` | BSD 3-Clause | tipi dei messaggi PX4 |
| DroneKube | repository separato, non incluso | - | lavoro precedente, architettura diversa (sezione 4) |

## 2. KubeROS

KubeROS offre flotta, robot, deployment di moduli ROS 2, parametri tramite
`rosParamMap` e placement onboard o edge, attraverso un'API REST. Non definisce
risorse Kubernetes proprie.

La copia in `integrations/kuberos` parte dal commit indicato nel registro di
provenienza ed e' stata estesa per questo progetto:

| Estensione | Descrizione |
| --- | --- |
| Tipo di workload | i moduli persistenti sono Deployment, non Pod singoli |
| Aggiornamento differenziale | una modifica riavvia solo i moduli toccati e produce un nuovo numero di revisione |
| Ripristino | quando un aggiornamento non converge, KubeROS torna all'ultima revisione valida e lo registra |
| Probe e risorse | probe di avvio, readiness e liveness e limiti di risorse dichiarati nel modulo arrivano al manifest generato |
| Stato delle operazioni | esito per revisione ed evento, con identificatore di correlazione |
| Discovery | un solo Fast DDS Discovery Server condiviso dalla flotta |
| Permessi | ServiceAccount dedicato, senza `cluster-admin` |

L'HPA non fa parte di KubeROS: resta una risorsa Kubernetes creata dal control
plane.

Gli aggiornamenti dei moduli ROS 2 usano `maxSurge: 0`, per non avere due nodi
con la stessa identita' nello stesso momento.

## 3. RobotKube

| Componente | Decisione | Motivo |
| --- | --- | --- |
| Event Detector | riusato a un commit fissato, con una patch di compatibilita' in `patches/` | fornisce buffer, regole a plugin e azioni |
| Regole PX4 | scritte per questo progetto in `sources/px4_event_detector_plugin` | batteria, telemetria e latenza di analytics non esistono a monte |
| Application Manager | non eseguito | quello a monte gestisce applicazioni e connessioni per un altro dominio; il progetto usa un proprio manager con lo stesso schema evento-azione |
| Azione `DeploymentRequest` | ridefinita in `interfaces/` | porta un evento tipizzato, una policy e un esito con fasi e ritorno |

## 4. DroneKube

DroneKube e' il lavoro precedente da cui deriva l'idea di separare la reazione
rapida su DDS dal percorso gestito da Kubernetes. Aveva due livelli: una
macchina con i container degli UAV e una, sul lato cloud, con un solo Event
Detector per tutta la flotta, l'Application Manager e un cluster Kubernetes a
nodo singolo. Il suo codice non e' incluso, e i risultati di quella valutazione
non si applicano alla variante A di questo progetto, che e' un'implementazione
diversa.

| DroneKube | In questo progetto |
| --- | --- |
| Un solo Event Detector sul lato cloud, per tutta la flotta | Un Event Detector per drone, sul nodo del drone |
| Regole di prossimita' fra due UAV e di batteria | Regole di batteria, telemetria e latenza di analytics; la prossimita' e' esclusa perche' richiede geometria multi-drone e non riguarda l'orchestrazione |
| Comando di batteria pubblicato su DDS dall'Event Detector del lato cloud, senza passare da Kubernetes | Comando pubblicato dal drone stesso, senza dipendere dal percorso di orchestrazione |
| Application Manager che estende quello open source di RobotKube | Dispatcher e Application Manager scritti per questo progetto, con lo stesso schema evento-azione |
| Job temporaneo che registra i dati della missione su un evento | Ripreso come Job diagnostico a durata limitata |
| KubeROS non partecipa all'esecuzione | KubeROS nel ciclo di esecuzione, su un cluster a piu' nodi |

## 5. Componenti Scritti Per Questo Progetto

| Componente | Posizione |
| --- | --- |
| Messaggi, service e action | `interfaces/cloud_native_robotics_interfaces` |
| Regole PX4 dell'Event Detector | `sources/px4_event_detector_plugin` |
| Operational Event Dispatcher | `sources/operational_event_dispatcher` |
| Application Manager | `sources/cloud_native_application_manager` |
| Companion Analytics | `sources/companion_analytics` |
| Audit Writer, Operator Notifier, Platform Observer | `sources/platform_observability` |
| Fleet Operator e risorse `dronekube.io` | `operator/` |
| State Bridge | `sources/state_bridge` |
| Strumenti di scenario: avvio della flotta, guasto di batteria, osservatore di missione, banco della partizione, modulo di prova del lifecycle | `sources/e0_kuberos_bootstrap`, `sources/e1_battery_fault_harness`, `sources/mission_observer`, `sources/s2_harness`, `sources/lifecycle_fault_probe` |
| Runner, osservatori e giudici degli scenari | `scripts/` |

## 6. Cosa Aggiunge La Variante Dichiarativa

La variante A lascia a Kubernetes la riconciliazione dei Deployment, ma lo stato
ROS-aware (quale modulo, dove, in quale stato di lifecycle, con quale obiettivo
di servizio) vive nel database di KubeROS e nella logica dell'Application
Manager. La variante B porta quello stato in risorse Kubernetes.

| Aspetto | Variante A | Variante B |
| --- | --- | --- |
| Stato desiderato del modulo | deployment KubeROS | `ROSModule.spec` |
| Stato osservato | log e risposte dell'API KubeROS | `ROSModule.status`, scritto da State Bridge e controller |
| Innesco dell'adattamento | evento ROS 2 e richiesta di azione | soglia dichiarata in `AdaptationPolicy`, valutata sullo stato osservato |
| Lifecycle ROS 2 | comandi lanciati all'avvio del modulo e dall'Application Manager | passo deciso dal `LifecycleController`, eseguito dal bridge |
| Modifica fuori banda di un Deployment | il percorso evento-azione non prevede un confronto periodico con lo stato voluto | confronto periodico e correzione |
| Consegna dell'audit | coda persistente, quando configurata | chiamata diretta senza nuovo tentativo |

## 7. Limiti Aperti

- Il profilo supportato dal Fleet Operator e' Companion Analytics; aggiungere un
  pacchetto richiede un nuovo profilo esplicito.
- L'astrazione del middleware copre solo State Bridge ed esiste un solo
  adattatore, per ROS 2.
- Il Fleet Operator ha una sola replica.
- Dopo un incidente chiuso con successo la policy non riporta da sola il modulo
  onboard.
- La valutazione usa PX4 SITL e k3d su un solo host.
