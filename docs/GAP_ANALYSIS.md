# Gap Analysis: Specifica Target, KubeROS E DroneKube

## 1. Scopo

Questo documento confronta la specifica di cloud_native_ros_kubernetes con:

- KubeROS baseline originale;
- copia KubeROS adattata in `integrations/kuberos`;
- DroneKube originale usato come riferimento storico;
- Event Detector ufficiale pinned in integrations/robotkube/event_detector;
- Application Manager ufficiale pinned in integrations/robotkube/application_manager.
- Perception Interfaces ufficiale pinned in integrations/robotkube/perception_interfaces.

Le decisioni possibili sono:

| Decisione | Significato |
| --- | --- |
| REUSE | usare un componente con modifiche minime e provenienza verificata |
| ADAPT | modificare sostanzialmente codice o comportamento verificato |
| REWRITE | implementare nel nuovo progetto usando i requisiti come riferimento |
| EXCLUDE | non includere nel caso d'uso |
| BLOCKED | non copiare finche' provenienza, licenza o dipendenza non e' risolta |

## 2. Snapshot Analizzati

| Sorgente | Commit root | Remote | Stato workspace rilevante |
| --- | --- | --- | --- |
| KubeROS baseline | d8ab5294a4cc05d58b529ca360bc1a09d842b107 | github.com/V1viana/kuberos | read-only |
| KubeROS integration | d8ab5294a4cc05d58b529ca360bc1a09d842b107 + working tree | clone locale in integrations/kuberos | update replace differenziale v1 verificato live in U1 |
| Event Detector upstream | 32f59d0c2ff1a8be4c48c96f10cee6f0edf6cdbf | github.com/ika-rwth-aachen/event_detector | detached HEAD, MIT |
| Application Manager upstream | 8ddb99a70f7f4f571cbaaa0ccea19fb3432424e0 | github.com/ika-rwth-aachen/application_manager | detached HEAD, MIT |
| Perception Interfaces upstream | 1d1472f35f01ef2e575e75733c9bb2316099641c | github.com/ika-rwth-aachen/perception_interfaces | detached HEAD, MIT; dipendenza perception_msgs |
| DroneKube originale | 96a7676947ebc456da900fbea203313563d1832d | github.com/V1viana/dronekube | documentazione modificata; sources/* interamente ignorato da Git |

Data dell'audit: 21 agosto 2026.

Il commit DroneKube non identifica il contenuto locale di sources/*: la .gitignore contiene infatti sources/*. Per questi file sono registrati hash locali separati nel documento di provenienza.

## 3. Esito Sintetico

L'architettura target e' realizzabile, ma non tramite copia diretta della baseline.

1. KubeROS e' adatto come control layer ROS-aware per fleet, parametri e placement.
2. Lo scheduler upstream genera Pod; la copia di integrazione materializza anche Deployment con strategia adatta alle identita ROS 2.
3. La baseline iniziale offriva create, list/info e delete. La copia
   di integrazione implementa PATCH come update replace differenziale e
   asincrono, con revisione, audit e rollback verificato live.
4. L'Event Detector KubeROS e' un prototipo monolitico che pubblica JSON in std_msgs/String.
5. L'Application Manager KubeROS dimostra Kubernetes Event e Job, ma non Action, feedback, rollback o audit durevole.
6. DroneKube offre pattern migliori per plugin e ROS 2 Action, ma i sorgenti locali non appartengono al commit root.
7. La BatteryLow locale contiene soglia, isteresi e RTL, ma non ack, retry controllato, campioni consecutivi o evento tipizzato.
8. L'Application Manager DroneKube conclude il goal con successo anche quando singole operazioni hanno prodotto feedback di errore.
9. Il core Event Detector e' riusato dall'upstream pinned; plugin PX4, normalizzazione, dispatcher e Application Manager della baseline sono componenti del progetto. L'Application Manager upstream e' conservato come riferimento ma non e' il runtime eseguito.
10. Il plugin PX4 e' implementato come package separato con tre regole, state machine testabili e nessuna dipendenza dal control plane Kubernetes.

## 4. Verifica Della Baseline KubeROS

### 4.1 Validazione Storica Della Copia D'Integrazione

I controlli riportati di seguito documentano l'ultima validazione completa
eseguita prima della pulizia del repository. Le relative suite non sono incluse
nella versione pubblicabile del progetto.

| Controllo | Esito |
| --- | --- |
| Django system check KubeROS | PASS, nessun issue Django |
| Suite KubeROS | PASS: 16 controlli, inclusi diff selettivo, ConfigMap, migrazione e convergenza completa dei Deployment |
| Update-demo offline | PASS: 13 controlli su rendering, parser KubeROS, invarianti safety, ConfigMap, placement e rollout senza identita' ROS duplicate |
| U1 update live | PASS: revisione `1 -> 2`, evento `UPDATE SUCCESS`, 1/12 Pod sostituito, 11/12 preservati, Service ROS `healthy/active` |
| U2 rollback live | PASS: revisione 3 `FAILED`, revisione 2 ripristinata, 11/11 workload non target invariati, Service ROS `healthy/active` |
| Contratto OperationalEvent | PASS: build, import del tipo e controlli ament lint |
| Migrazione update | PASS: 0002_deployment_update applicata nell'ambiente di validazione |
| registrazione Celery | PASS: main.tasks.deployment_update.apply_deployment_update registrato |
| makemigrations --check --dry-run | resta il drift preesistente del default casuale KuberosJob.slug, escluso dalla migrazione update |
| tracking del core KubeROS | Fleet, API, scheduler ed executor presenti nel commit KubeROS |
| ispezione scheduler | genera apiVersion v1, kind Pod |
| ispezione API | create/delete/info presenti; PATCH replace update implementato localmente |

Il 1 settembre 2026 i banchi U1 e U2 hanno verificato sul cluster dedicato sia il PATCH differenziale sia il rollback live della revisione non valida.

### 4.2 Procedura Corrente

Il prototipo storico `RESET_AND_DEPLOY.sh`, i container event-driven monolitici
e i manifest Fleet Edge appartengono alla baseline KubeROS esterna usata per
l'audit. Non sono inclusi nella copia d'integrazione.

Il progetto usa runner separati e idempotenti nelle directory radice
`scripts/` e `manifests/`, con fasi osservabili di bootstrap, deploy, fault e
raccolta delle evidenze.

## 5. Gap Analysis KubeROS

### 5.1 Core E API

| Componente | Stato corrente | Gap target | Decisione |
| --- | --- | --- | --- |
| Django models/API | fleet, robot, deployment, revisione ed eventi con outcome | uniformare ancora error model e correlation ID | ADAPT nel KubeROS pinned |
| Create deployment | scheduling e task Celery | manca correlation ID e outcome uniforme | REUSE via adapter, poi ADAPT |
| List/info | stato deployment e job | readiness funzionale ROS non esposta | REUSE come polling iniziale |
| Delete deployment | elimina moduli e ConfigMap | idempotenza da gestire nell'adapter | REUSE via adapter |
| Update | PATCH replace differenziale, revisione, audit e rollback | solo Fast DDS; fleet e robot immutabili | ADAPT verificato live in U1 e U2 |
| Scale | enum presente nel modello | implementazione non osservata | EXCLUDE dalla prima iterazione |
| Kubernetes client | CoreV1Api + AppsV1Api | HPA, Job/Event e watch ancora pendenti | ADAPT completato per Deployment |

La prima versione dell'adapter puo' usare:

~~~text
create -> poll info -> PATCH replace -> poll event/revision -> delete
~~~

Il PATCH e' verificato sia offline sia live. U1 aggiorna il comando del solo
Companion Analytics di drone01, incrementa la revisione KubeROS da 1 a 2 e
preserva UID e restart count degli altri 11 Pod. I Deployment ROS usano
`maxSurge: 0` per evitare due nodi con la stessa identita' DDS. U2 invia una
revisione 3 con immagine inesistente: KubeROS osserva la mancata convergenza,
registra `UPDATE FAILED` e riconcilia la revisione 2, verificata healthy.

### 5.2 Scheduler E Workload

| Capacita' | Stato KubeROS | Target | Decisione |
| --- | --- | --- | --- |
| ROS module | Pod legacy o Deployment esplicito | Deployment per moduli persistenti | IMPLEMENTED e verificato live |
| Discovery server | Deployment + Service condiviso | Deployment + Service | ADAPT verificato live |
| Placement onboard | nodeSelector hostname | selector validato | REUSE/ADAPT |
| Placement edge | selector resource group | selector/affinity e readiness | ADAPT verificato live |
| Fleet scope | singleton tramite nome | singleton controller-managed | REUSE con test |
| Service | ClusterIP opzionale | Service ed EndpointSlice osservabili | REUSE con selector aggiornato |
| Probes | campi AS-IS supportati dal renderer | probe funzionali per ogni modulo | SUPPORTO verificato live |
| Resources | supporto base | request/limit obbligatori | ADAPT verificato live |
| HPA | assente in KubeROS | HPA analytics edge | risorsa Kubernetes separata verificata live |
| Rollback | replace Pod o rollout Deployment | validazione end-to-end | IMPLEMENTED e verificato live in U2 |

### 5.3 Parametri E DDS

| Componente | Valore corrente | Gap | Decisione |
| --- | --- | --- | --- |
| rosParamMap key-value | ConfigMap ed env | validazione debole, update dinamico TODO | REUSE/ADAPT |
| rosParamMap file | ConfigMap montata | percorsi assoluti locali | ADAPT |
| discovery per robot | opzione con profilo Fast DDS dedicato | non necessaria nella demo finale | EXCLUDE; namespace e placement con server fleet condiviso |
| discovery fleet | Fast DDS server condiviso | raggiungibilita' multi-node | ADAPT verificato live |
| QoS | soprattutto nel codice | manca catalogo dichiarativo | REWRITE nei package ROS 2 |

P2 usa un Fast DDS Discovery Server condiviso. L'indirizzo del nodo control
plane viene renderizzato a runtime; per campagne multi-cluster andra'
sostituito con un endpoint stabile o con una topologia di discovery
dichiarativa.

### 5.4 Baseline PX4

| Area | Stato | Decisione | Motivazione |
| --- | --- | --- | --- |
| template PX4 per-drone legacy | PX4, agent e detector monolitico | EXCLUDE | sostituito dai manifest E0/E1/E2 correnti |
| template manager Fleet Edge | discovery e manager edge | EXCLUDE | sostituito da dispatcher e manager nel control plane |
| generatori fleet/inventory legacy | parametrici su N droni | EXCLUDE | sostituiti da bootstrap KubeROS e renderer E0 validati |
| generatori manifest legacy | sostituzione testuale | EXCLUDE | sostituiti da parser YAML e template testati |
| XRCE Service generator legacy | Service per drone | EXCLUDE | Service dichiarati nei manifest correnti |
| Event Detector Python | topic PX4 e JSON | EXCLUDE | sostituito da core plugin-based e interfaccia tipizzata |
| Application Manager Python | Event, audit ConfigMap, Job | EXCLUDE | sostituito da package ROS 2 con Action e adapter KubeROS |
| Dockerfile prototipali | dipendenze e tag non fissati | EXCLUDE | sostituiti dalle immagini del progetto |
| RBAC prototipale | privilegi estesi e account condivisi | EXCLUDE | sostituito da ServiceAccount e Role dedicati |

## 6. Gap Analysis DroneKube Originale

### 6.1 Vincolo Di Provenienza

Il repository root e' MIT, ma sources/* e' ignorato da Git. Quindi:

- il commit root traccia Dockerfile e configurazioni che consumano i sorgenti;
- non traccia Application Manager e plugin PX4 locali;
- un checkout pulito non ricostruisce lo stesso codice;
- le estensioni locali non sono attribuibili a un commit.

Il codice locale puo' essere studiato, ma non copiato finche' la provenienza non e' risolta.

### 6.2 Event Detector E Plugin PX4

| Componente | Valore corrente | Gap | Decisione |
| --- | --- | --- | --- |
| framework event_detector locale | pluginlib e AnalysisRule | sorgente assente, immagine custom | EXCLUDE a favore dell'upstream ufficiale |
| Px4LowBatteryRule | 20%, isteresi 5%, freshness, connected, RTL | licenza TODO; niente ack o tre campioni | REWRITE |
| supporto multi-client | topic namespaced per UAV | target ha un detector per drone | EXCLUDE dal core |
| fault batteria integrato | fault temporizzato | fault e produzione mescolati | REWRITE come injector |
| Px4ProximityRule | client Action e rosbag | fuori scope | EXCLUDE |
| MavrosLowBatteryRule | regola MAVROS | target usa uXRCE | EXCLUDE |
| Docker event-detector | base image custom | sorgente e digest non dichiarati | REWRITE |

La nuova BatteryLowRule avra':

- detector per singolo robot;
- tre campioni consecutivi;
- VehicleCommandAck;
- retry limitato;
- OperationalEvent tipizzato;
- buffer locale;
- test della state machine senza PX4 reale.

### 6.3 Application Manager

| Componente | Valore corrente | Gap | Decisione |
| --- | --- | --- | --- |
| ROS 2 Action server | goal, feedback, result, cancel callback | result sempre success; cancel non osservato | ADAPT da upstream verificato |
| DeploymentRequest.action | contratto applicazioni specifico object detection | non esprime evento, outcome, timeout o rollback | ADAPT come contratto originale nel package di interfacce |
| dispatcher applicazioni | estensibile per tipo | policy e risorse accoppiate | ADAPT come handler registry |
| Kubernetes API wrapper | CRUD ConfigMap, Job, custom resource | niente Deployment/Event e error model debole | REWRITE |
| Rosbag2OnEventApp locale | ConfigMap, Job, PVC, metadati | estensione ignorata e nomi hardcoded | REWRITE dal comportamento |
| custom operator object detection | pattern RobotKube | non necessario | EXCLUDE |
| MQTT connection | bridge multi-nodo | non richiesto nella baseline DDS | EXCLUDE |

Il README locale identifica github.com/ika-rwth-aachen/application_manager come origine MIT. Prima dell'adattamento serve un commit upstream esatto e un confronto con lo snapshot locale.

## 7. Confronto Delle Interfacce

| Requisito | KubeROS corrente | DroneKube locale | Target |
| --- | --- | --- | --- |
| evento normalizzato | JSON in String | implicito nella regola | OperationalEvent.msg implementato |
| incidente | coppia drone/evento in memoria | ID DeploymentRequest | event_id e correlation_id |
| remediation | chiamata interna | DeploymentRequest Action | DeploymentRequest adattata |
| feedback | log | stringhe | fase tipizzata e osservazioni |
| result | nessuno | stringa sempre success | outcome, metriche e rollback |
| cancel | nessuno | accettato, non applicato | verificato tra le fasi |
| recovery | evento JSON dedicato | shutdown request | stato RECOVERED correlato |
| deduplicazione | set in memoria | non esplicita | archivio idempotente |

Decisione aggiornata: `OperationalEvent` resta un contratto originale per la
normalizzazione. Il pattern Action `DeploymentRequest` dell'Application
Manager upstream e' mantenuto, ma il contratto e' adattato nel package del
progetto dopo l'audit dei gap. `MetricSample` e Companion Analytics Lifecycle
sono implementati; il service health resta opzionale e fuori dalla P2.

## 8. Componenti Target

| Componente target | Origine concettuale | Strategia |
| --- | --- | --- |
| cloud_native_robotics_interfaces | ROS 2 e pattern DroneKube | OperationalEvent, MetricSample e DeploymentRequest adattata originali |
| Event Detector per-drone | upstream ika event_detector | REUSE pinned + plugin PX4 nuovo |
| BatteryLowRule | comportamento PX4 locale | REWRITE verificata live: tre campioni, RTL, ack, AUTO_RTL e recovery durante partizione |
| TelemetryHeartbeatRule | detector KubeROS | REWRITE verificata live con PX4 SITL, Event Detector upstream e fault Agent reale |
| AnalyticsLatencySLORule | specifica target | REWRITE verificata live con analytics onboard ed edge |
| Application Manager | implementazione originale ispirata al pattern upstream | Action e policy P1/P2 verificate live |
| KubeROS adapter | API create/info/PATCH/delete | create/info/PATCH e rollback verificati live; delete testato |
| Platform Observer | Kubernetes watch/metrics | REWRITE verificata live su Pod, Deployment, Event e PodMetrics con RBAC read-only |
| Companion Analytics | ROS Lifecycle | REWRITE implementata e verificata live |
| Audit Writer | requisiti di persistenza | REWRITE verificata live con JSONL append-only su PVC |
| Diagnostic Job builder | entrambi i prototipi | REWRITE con test manifest |
| Operator Notifier | specifica target | REWRITE verificata live con sink HTTP e record durevole |

## 9. Estensione KubeROS Necessaria

Per soddisfare gli obiettivi del progetto, KubeROS non puo' restare un semplice generatore di Pod.

### K1. Workload Controller - verificato live

- workloadKind Deployment come default per moduli persistenti;
- Pod solo per debug o compatibilita';
- selector e Pod template coerenti;
- nodeSelector, securityContext, env, volumi e Service selector preservati.

### K2. Probes E Risorse - supporto renderer implementato

- schema rosModules per startup/readiness/liveness;
- request e limit validati;
- campi conservati nel manifest materializzato.

### K3. Stato Operativo - implementato per Pod e Deployment

- stato di Deployment e Pod controllati;
- fasi accepted, progressing, ready, failed e deleted;
- correlation_id come annotation e label.

### K4. Sicurezza - verificata per P2

- RBAC minimo senza cluster-admin;
- ServiceAccount dedicati a KubeROS, Application Manager, observer, audit e notifier;
- token API generato al bootstrap nel Secret del namespace.

### K5. Test - unitari e integrazione P2 completati

- import test mancante riparato;
- migrazione revision/event outcome aggiunta;
- test unitari dell'update differenziale e delle dipendenze ConfigMap aggiunti;
- test del renderer Deployment;
- create/info e placement edge verificati nel namespace isolato P2.

Queste estensioni costituiscono il contributo tecnico del progetto.

## 10. Ordine Di Implementazione

### Milestone 0: Provenienza E Baseline

1. inizializzare Git nel nuovo progetto;
2. scegliere la licenza del nuovo lavoro;
3. fissare KubeROS come submodule, fork o dependency pinned;
4. fissare l'Application Manager upstream come riferimento architetturale;
5. importare e fissare Event Detector e dipendenze upstream.

### Milestone 1: Contratti

1. workspace ROS 2 e OperationalEvent: completati;
2. MetricSample e GetHealthSnapshot completati; chiamata live integrata negli harness;
3. interfacce, Service e Action compilati su ROS 2 Jazzy e nell'immagine Humble;
4. DeploymentRequest adattata e Action server verificato con goal P0 end-to-end.

### Milestone 2: KubeROS Cloud-Native Ed E0

1. supporto Pod/Deployment completato offline;
2. probe, container non ROS e ConfigMap YAML inline supportati;
3. inventory multi-robot e DDS namespaced completati;
4. E0 a tre droni completato live tramite tre richieste KubeROS: 12/12 workload
   KubeROS e 19/19 Deployment disponibili;
5. tre PX4 invariati, Lifecycle active, routing onboard, zero remediation e
   snapshot persistenti verificati per 60 secondi;
6. GetHealthSnapshot verificato live sulle tre istanze analytics onboard.

### Milestone 3: Safety Locale Ed E1

1. Event Detector per-drone: build e smoke runtime completati;
2. BatteryLowRule e injector tipizzato separato completati;
3. ack, retry limitato e history DDS transient-local completati;
4. E1 completata live con PX4 armato, control plane arrestato per 35.216 s,
   RTL/ack/AUTO_RTL onboard e audit fleet dopo il ripristino.

### Milestone 4: Recovery Ed E2

1. TelemetryHeartbeatRule, fault Agent reale e recovery correlata completati live;
2. Application Manager minimo completato;
3. dispatcher, restart agent, Event, Job, feedback e recovery correlato
   completati live nel namespace isolato;
4. E2/P1 completata anche con PX4 armato in hover controllato: stato Hold,
   nessun failsafe, quota stabile entro 0,158 m, PX4 invariato e recovery P1
   `STABLE` in 32,675 s con quattro snapshot diagnostici correlati.

### Milestone 5: Placement E Rollback E3-E4

1. analytics Lifecycle Node con Service tipizzato di health snapshot;
2. adapter KubeROS completato e verificato live;
3. HPA e routing verificati live;
4. Platform Observer e rollback KubeROS verificati live;
5. E3 completato come P2 ed E4 completato con fault readiness controllato;
6. GetHealthSnapshot verificato sull'edge dopo placement e sull'onboard dopo rollback.
7. U2 completato live: immagine inesistente rifiutata, evento `UPDATE FAILED`,
   revisione 2 ripristinata e 11/11 workload non target invariati.

### Milestone 6: Observability Recovery

1. outbox SQLite FIFO montato su PVC dedicato;
2. replay 6/6 verificato attraverso il restart del manager con Audit Writer indisponibile;
3. Application Manager singleton con strategia Recreate;
4. Metrics API verificata live con CPU e memoria aggregate e per Pod.

### Milestone 7: Campagna Statistica

1. schema KPI comune E0-E4 completato;
2. parser delle evidenze native e output CSV, JSON e Markdown completato;
3. media, mediana, p95 e CI95 Student-t implementati e testati;
4. runner multi-run con reset indipendente, build per famiglia e import per
   cluster completato;
5. dry-run dell'intera matrice completato;
6. pilot live E2 completato: PASS, recovery 32.744 s, mission success 1.0,
   quota 0.211 m e diagnostica completa;
7. campagna definitiva completata: 51 osservazioni, 50 campioni validi e
   48 PASS; dieci campioni validi per scenario;
8. gate E2 armed/Hold applicato prima del fault; un campione disarmato escluso
   e replacement incluso come FAIL valido per superamento dello SLO recovery.
9. manifest di riproducibilita' completato con commit, submodule, versioni della
   toolchain, repo digest upstream e image ID delle build locali.

## 11. Gate Prima Della Copia

La copia e' ammessa solo quando:

- il file appartiene a un commit identificato o e' marcato come nuova implementazione;
- la licenza del file/package e' chiara;
- destinazione e decisione sono nel registro di provenienza;
- esiste un test che giustifica il riuso;
- non vengono introdotte assunzioni DroneKube escluse;
- gli avvisi richiesti dalla licenza sono conservati.

Nessun sorgente runtime locale DroneKube e' stato copiato. Gli upstream
ufficiali restano pinned e invariati; i nuovi package sono contributi originali.
