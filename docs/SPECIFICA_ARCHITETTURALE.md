# Specifica Architetturale

Questo documento descrive l'architettura della piattaforma com'e' implementata
nel repository. Sostituisce la specifica progettuale dell'agosto 2026, che
copriva la sola variante imperativa.

## 1. Obiettivo

Valutare fino a che punto workload robotici ROS 2 possano essere orchestrati con
tecnologie cloud-native senza trasferire al cluster le decisioni che devono
restare locali al robot.

La piattaforma esiste in due varianti che condividono simulatore, workload,
soglie e scenari, e differiscono nel modo in cui lo stato di orchestrazione e'
rappresentato ed eseguito:

- **Variante A, imperativa.** Un evento ROS 2 diventa una richiesta di azione,
  eseguita da un Application Manager tramite KubeROS e le API Kubernetes.
- **Variante B, dichiarativa.** Lo stato desiderato e osservato e' contenuto in
  risorse Kubernetes, riconciliate da un Fleet Operator.

In un'esecuzione una sola variante governa il workload in prova.

## 2. Ambito

Incluso:

- una flotta parametrica di droni simulati con PX4 SITL, un nodo Kubernetes per
  drone;
- un modulo ROS 2 non critico, Companion Analytics, eseguibile onboard e
  sull'edge;
- la reazione locale alla batteria bassa, il recupero della telemetria, la
  migrazione verso l'edge per violazione di uno SLO di latenza, il ritorno
  onboard quando la migrazione fallisce, l'aggiornamento e il ripristino di un
  modulo;
- scenari di drift, partizione di rete, scala e guasti concorrenti.

Escluso:

- certificazione safety e validazione su droni reali;
- controllo di volo affidato a Kubernetes e migrazione dello stato di PX4;
- scheduling hard real-time nel cluster;
- pacchetti ROS 2 arbitrari: il profilo supportato e' Companion Analytics, piu'
  un modulo di prova usato per iniettare guasti di lifecycle.

## 3. Principi

1. **Safety locale.** Una decisione urgente non dipende dalla raggiungibilita'
   dell'edge o del control plane.
2. **Separazione dei piani.** PX4 mantiene il controllo del volo; il cluster
   governa moduli accessori e operazioni.
3. **Kubernetes cosi' com'e'.** Probe, Deployment, Job, Event, HPA e RBAC usano
   le API native.
4. **Interfacce tipizzate.** Eventi e richieste non usano stringhe JSON come
   contratto.
5. **Esito verificabile.** Ogni azione ha un risultato atteso, un limite di
   tempo e una strategia di ritorno.
6. **Idempotenza.** Lo stesso incidente non genera azioni duplicate.
7. **Audit separato dagli Event.** Gli Event di Kubernetes danno visibilita'
   immediata, non sono un archivio.

## 4. Piani E Componenti

I tre piani sono ruoli di deployment, realizzati come etichette dei nodi
(`kuberos.io/role` con valori `onboard`, `edge`, `control_plane`).

### 4.1 Onboard, uno per drone

| Componente | Responsabilita' |
| --- | --- |
| PX4 SITL | Autopilota simulato |
| Micro XRCE-DDS Agent | Collegamento fra PX4 e il grafo ROS 2 |
| Event Detector | Regole locali ed eventi operativi (variante A) |
| Companion Analytics | Modulo ROS 2 non critico con lifecycle gestito |
| State Bridge | Sidecar del modulo (variante B) |

### 4.2 Edge

Il nodo edge ospita Companion Analytics dopo una migrazione, con un HPA. Non
contiene logica decisionale. La sua perdita non interrompe PX4 ne' la reazione
locale.

### 4.3 Control plane

| Componente | Variante | Responsabilita' |
| --- | --- | --- |
| KubeROS | A | Flotta, deployment ROS-aware, aggiornamenti e ripristino |
| Operational Event Dispatcher | A | Dal topic degli eventi alla richiesta di azione |
| Application Manager | A | Policy, azione, verifica, ritorno |
| Fleet Operator | B | Riconciliazione delle quattro risorse |
| Fast DDS Discovery Server | comuni | Discovery ROS 2 condivisa dalla flotta |
| Audit Writer | comuni | Registro persistente degli incidenti |
| Operator Notifier | comuni | Notifica all'operatore |
| Platform Observer | comuni | Lettura di Pod, Deployment, Event e metriche |

## 5. Variante A: Percorso Evento-Azione

```text
PX4 / metriche ROS 2
        |
Event Detector con le regole PX4
        |  OperationalEvent su /fleet/operational_events
Operational Event Dispatcher
        |  DeploymentRequest su /fleet/deployment_request
Application Manager
        |--> KubeROS (API REST)
        |--> API Kubernetes
        v
verifica dell'esito, audit e notifica
```

- L'Event Detector e' il framework RobotKube, fissato a un commit, esteso dal
  plugin `sources/px4_event_detector_plugin`.
- Ogni incidente ha un `correlation_id` stabile dall'evento al risultato. Il
  Dispatcher inoltra solo l'ingresso nello stato di incidente e scarta i
  duplicati con lo stesso identificatore.
- L'Application Manager applica tre policy: osservazione della reazione locale
  alla batteria, ripristino della telemetria, ripristino dello SLO di analytics.
  Il ritorno allo stato precedente fa parte di ciascuna policy.
- Verso KubeROS usa creazione, lettura, aggiornamento differenziale ed
  eliminazione di un deployment. Verso Kubernetes crea Event, riavvia un
  Deployment, crea un Job diagnostico, sposta l'instradamento di analytics e
  crea l'HPA del modulo edge.
- I resoconti verso Audit Writer e Notifier passano da una coda persistente con
  consegna almeno una volta, quando lo scenario la configura.

### 5.1 Regole

| Regola | Ingresso | Uscita | Azione |
| --- | --- | --- | --- |
| Batteria bassa | carica residua sotto 0,20 per tre campioni | sopra 0,25 per tre campioni | comando di ritorno al punto di decollo inviato a PX4 dal drone stesso; l'evento e' propagato in modo asincrono |
| Telemetria persa | nessuno stato del veicolo oltre il limite configurato | campioni di nuovo ricevuti e stabili | riavvio del solo Micro XRCE-DDS Agent e Job diagnostico |
| Latenza di analytics | 95esimo percentile sopra 250 ms per tre finestre consecutive | sotto 150 ms per tre finestre consecutive | migrazione di Companion Analytics verso l'edge |

Soglie, finestre e limiti sono parametri delle regole e vengono fissati dai
manifest di ciascuno scenario. PX4 non viene mai riavviato da una regola.

### 5.2 Migrazione verso l'edge

1. L'istanza onboard resta attiva.
2. L'Application Manager chiede a KubeROS un deployment sull'edge.
3. Attende che il Pod sia pronto e che il nodo ROS 2 sia attivo.
4. Sposta l'instradamento e disattiva l'istanza onboard.
5. Crea un HPA per il modulo edge, da una a tre repliche.
6. Se l'istanza edge non diventa pronta entro il limite, riattiva quella
   onboard ed elimina il deployment edge.

## 6. Variante B: Risorse E Riconciliazione

### 6.1 Risorse

Quattro risorse del gruppo `dronekube.io/v1alpha1`, definite in
`operator/crds`:

| Risorsa | Stato desiderato | Stato osservato |
| --- | --- | --- |
| `RobotFleet` | robot della flotta, selettore dei nodi edge | robot pronti, nodi edge |
| `ROSModule` | pacchetto, robot, placement, stato di lifecycle richiesto, parametri ROS, probe | placement effettivo, stato di ogni istanza, comandi e ricevute di lifecycle, metriche, revisione |
| `ROSLifecyclePolicy` | limite di tempo e tentativi delle transizioni, gate di readiness | nessuno: e' configurazione |
| `AdaptationPolicy` | modulo osservato, soglie di apertura e chiusura, azione, limiti del ritorno | fase dell'incidente e suo progresso |

### 6.2 Fleet Operator

Scritto in Python con Kopf, una replica, limitato a un namespace.

| Controller | Attivazione | Compito |
| --- | --- | --- |
| `RobotFleetController` | creazione, modifica, ogni 30 s | disponibilita' della flotta e nodi edge |
| `ROSModuleController` | creazione, modifica, ogni 30 s | Deployment e Service del modulo, revisioni, ripristino dell'ultima versione valida, correzione delle modifiche fuori banda |
| `LifecycleController` | ogni 5 s | decide un passo di lifecycle per istanza e lo scrive come comando |
| `AdaptationController` | ogni 1 s | apre e chiude gli incidenti ed esegue l'azione |

I periodi sono intervalli nominali, non garanzie di tempo. Nessun controller
chiama ROS 2: la risorsa `ROSModule` e' l'unico confine fra Operator e moduli.

### 6.3 State Bridge

Ogni Pod generato contiene il modulo ROS 2 e il sidecar State Bridge. Il bridge:

- legge lo stato di lifecycle, la readiness e le metriche del modulo e li scrive
  nello stato della risorsa;
- esegue il comando di lifecycle indirizzato alla propria istanza;
- registra una ricevuta di accettazione prima di inviare la richiesta ROS 2 e
  una ricevuta di esito dopo. Senza accettazione registrata non parte alcuna
  richiesta, e un comando non viene eseguito due volte.

La logica del bridge dipende da un livello di astrazione del middleware. Le
dipendenze ROS 2 stanno in un adattatore e nel processo che lo avvia.
L'astrazione riguarda solo il bridge.

Il ServiceAccount del bridge puo' leggere i `ROSModule` e modificarne solo lo
stato.

### 6.4 Adattamento

| Azione | Effetto |
| --- | --- |
| `MigratePlacement` | crea un secondo `ROSModule` sull'edge con il suo HPA, da una a tre repliche; disattiva il modulo onboard solo quando quello edge serve; se l'edge non serve entro i limiti, riattiva onboard e poi rimuove l'edge |
| `RestartComponent` | riavvia il Deployment selezionato e crea un Job diagnostico |

Fasi dell'incidente: `Nominal`, `Triggered`, `Migrating`, `Remediating`,
`Recovered`, `FallingBack`, `RolledBack`, `FallbackFailed`, `Escalated`. Lo
stato della policy conserva il progresso, cosi' un riavvio dell'Operator
riprende dall'API.

Limiti noti:

- dopo `Recovered` la policy non riporta da sola il modulo onboard e non si
  riarma; il riarmo avviene solo dopo un ritorno riuscito o dopo un intervento
  scaduto;
- i resoconti verso Audit Writer e Notifier sono chiamate dirette con limite di
  tempo e senza nuovo tentativo: un record puo' andare perso prima di arrivare.

## 7. Interfacce ROS 2

Definite in `interfaces/cloud_native_robotics_interfaces`.

| Nome | Tipo | Uso |
| --- | --- | --- |
| `/fleet/operational_events` | topic `OperationalEvent`, affidabile, persistenza locale, ultimi 100 | eventi normalizzati (variante A) |
| `/fleet/deployment_request` | action `DeploymentRequest` | richiesta, avanzamento ed esito di un'azione (variante A) |
| `/<robot>/companion/<istanza>/health` | service `GetHealthSnapshot` | stato dell'istanza di Companion Analytics |
| metriche di analytics | topic `MetricSample` | latenza, profondita' della coda, CPU dichiarata |

`OperationalEvent` contiene identificatore dell'evento e dell'incidente,
sorgente, robot, componente, tipo, gravita', stato (ingresso, attivo,
recuperato, fallito), valore osservato, soglia e finestra.

`DeploymentRequest` trasporta l'evento, la policy e l'esito richiesto.
L'avanzamento riporta la fase: accettato, in azione, in verifica, in ritorno.
Il risultato dice se l'esito e' stato raggiunto e se c'e' stato un ritorno.

La telemetria PX4 usa il profilo dei dati dei sensori, senza garanzia di
consegna.

## 8. Discovery E Probe

- Un solo Fast DDS Discovery Server, sul nodo di control plane, condiviso dalla
  flotta. I droni restano distinti per namespace ROS 2 e placement.
- Companion Analytics ha una probe di avvio che interroga lo stato di lifecycle
  del nodo e passa quando e' attivo. La stessa probe e' usata nelle due
  varianti.
- Gli aggiornamenti dei moduli ROS 2 non sovrappongono due repliche con la
  stessa identita' (`maxSurge: 0`).

## 9. Sicurezza

- ServiceAccount separati per KubeROS, Application Manager, Fleet Operator,
  State Bridge, Audit Writer, Operator Notifier, Platform Observer e Job
  diagnostico.
- Nessun componente applicativo usa `cluster-admin`.
- Il Fleet Operator ha un Role di namespace per risorse e workload che gestisce
  e permessi di sola lettura a livello di cluster per nodi e discovery delle
  API.

## 10. Osservabilita'

Un incidente lascia tracce indipendenti:

1. lo stato delle risorse Kubernetes;
2. gli Event di Kubernetes, senza garanzia di conservazione;
3. il record nel registro persistente dell'Audit Writer, quando la consegna
   riesce;
4. i log dei componenti;
5. le osservazioni degli osservatori di scenario, indipendenti dal control
   plane.

Il Platform Observer legge anche le metriche di CPU e memoria dei Pod. Queste
metriche sono contesto di una decisione, non la innescano.

## 11. Scenari

| ID | Intervento | Cosa si osserva |
| --- | --- | --- |
| E0 | nessun guasto | avvio, servizio stabile, nessuna azione senza causa |
| E1 | batteria bassa con control plane non raggiungibile | reazione locale e continuita' dello stato PX4 |
| E2 | sospensione del Micro XRCE-DDS Agent | ritorno della telemetria |
| P2 | ritardo di analytics oltre soglia | migrazione verso l'edge |
| E4 | come P2, con modulo edge che non diventa pronto | ritorno onboard |
| U1, U2 | aggiornamento valido e aggiornamento difettoso | convergenza e ripristino |
| S1 | Deployment gestito cancellato o portato a zero repliche | ripristino del servizio |
| S2 | transitorio di latenza dentro una partizione di rete | riconoscimento dell'incidente dopo il ripristino |
| S3 | flotta di dimensione crescente, un incidente | avvio, gestione dell'incidente, carico sul control plane |
| S4 | guasti di batteria, telemetria ed edge, singoli e combinati | esito di ciascun guasto e robot non coinvolti |
| TTR | flotta ricostruita da un cluster vuoto | tempo di ricostruzione |

I comandi sono in [EXPERIMENT_CAMPAIGN.md](EXPERIMENT_CAMPAIGN.md).

## 12. KPI E Definizioni

Le definizioni di base sono quelle della specifica originale, dove occupavano
la sezione 21.

| KPI | Definizione |
| --- | --- |
| Detection time | `event_observed - fault_injected` |
| Reaction time | `action_started - event_observed` |
| Recovery time | `stable_recovered - fault_injected` |
| MTTR | media del recovery time per tipo di incidente |
| Convergence time | `final_stable_state - remediation_goal_accepted` |
| Safety command latency | `vehicle_command_sent - battery_threshold_crossed` |
| Ack latency | `vehicle_command_ack - vehicle_command_sent` |
| Analytics latency | media, p95 e p99 per finestra |
| Mission continuity | assenza di restart PX4 e continuita' di heartbeat/nav state |
| Remediation success rate | remediation concluse con outcome / remediation avviate |
| Rollback rate | rollback / remediation avviate |
| Audit completeness | campi/artifact presenti / campi/artifact attesi |
| Duplicate action rate | azioni duplicate / incidenti unici |

Tutti i timestamp usano clock monotonic per le durate e UTC per la correlazione
tra processi.

Queste definizioni nascono per il percorso evento-azione. Nella variante
dichiarativa alcune operazioni interne non esistono: il protocollo di ogni
scenario fissa i marcatori effettivi di inizio e fine, e quando un'operazione
esiste in una sola variante l'intervallo parte da un marcatore esterno a
entrambe.

## 13. Banco Di Prova

- Cluster k3d su un solo host, creato da zero per ogni esecuzione.
- k3s con etcd incorporato.
- ROS 2 Humble con Fast DDS.
- Le immagini sono costruite dai Dockerfile in `containers/` e importate nel
  cluster dal runner.

I nodi condividono le risorse fisiche dell'host: il banco riproduce la struttura
dei tre piani, non le condizioni di rete fra di essi.
