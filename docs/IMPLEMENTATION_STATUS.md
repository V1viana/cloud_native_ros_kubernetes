# Stato Implementativo

Data di verifica: **8 settembre 2026**.

Questo documento separa le funzionalita' verificate offline, nei banchi live
isolati e ancora da estendere alla campagna completa. Le verifiche descritte riguardano esclusivamente questo progetto e la sua copia
KubeROS integrata.

## Stato Per Componente

| Componente | Stato | Evidenza | Limite attuale |
| --- | --- | --- | --- |
| Interfacce ROS 2 | completato su Jazzy e Humble | build delle quattro interfacce; image ID fissato | pubblicazione immagine fuori scope |
| Event Detector upstream | verificato live su Humble | lifecycle `active`, pluginlib e subscriber PX4 reale; image ID fissato | immagine completa da ottimizzare |
| Plugin PX4 | BatteryLow, Telemetry e AnalyticsLatency live | state machine C++, RTL locale con ack/stato PX4, fault Agent e migrazione analytics reale | fault batteria deterministico su topic tipizzato separato |
| Event dispatcher | implementato e verificato | deduplica `STATE_ENTER` e invia goal Action; P0 end-to-end | retry persistente dopo restart pendente |
| Application Manager | P0, P1, P2 ed E4 verificate live | policy e reporting correlato; outbox SQLite su PVC preserva 6/6 record attraverso restart | protocollo at-least-once; sink idempotente richiesto nel caso limite post-ack |
| Adapter Kubernetes | verificato live multi-node | RBAC, Event API, PATCH, HPA, Job e convergenza Deployment | watch continuo pendente |
| Adapter KubeROS | verificato live | create/info/PATCH, auth Token, correlation ID, polling stato/revisione ed evento KubeROS; delete coperto da verifica serializzabile | retry persistente pendente |
| KubeROS workload | verificato live | renderer/executor Pod, Service e Deployment; E0 crea 12 Deployment e 3 Service Agent | StatefulSet non incluso |
| KubeROS update | verificato live | U1 `UPDATE SUCCESS`; U2 `UPDATE FAILED` con rollback alla revisione 2; 16 controlli Django storici | retry persistente pendente |
| Workflow registry | verificato live | repository privato, 3 digest OCI, R1 e E0 con 16/16 container verificati a runtime | release successiva e distinta dalla campagna storica |
| Companion Analytics | Service health verificato live | E0/P2/E4/U1/U2 e R1 verificati; image ID e digest registry fissati | accesso alla release privata richiede pull secret |
| Demo manifest KubeROS | verificata live | P2 migra analytics; E0 avvia 3 stack PX4; U1 aggiorna un modulo; U2 rifiuta l'immagine invalida e ripristina la revisione | update multi-modulo non valutato |
| PX4 E2 P1 | verificato live con missione armata | gate armed/Hold, fault Agent, Event/Action/Job e recovery; 10 run validi | uno dei 10 run supera lo SLO recovery di 45 s |
| Audit Writer | verificato live | JSONL append-only su PVC; replay FIFO 6/6 senza duplicati nel run definitivo | replica singola nella demo |
| Operator Notifier | verificato live | Service HTTP e record `operator_notification` durevole | webhook esterno fuori scope |
| Platform Observer | verificato live | Pod, Deployment, Event e PodMetrics; CPU millicore e memoria byte aggregate e per Pod | watch continuo pendente |

## Funzioni KubeROS Aggiunte

- `rosModules[].workloadKind` accetta `Pod` o `Deployment`.
- `replicas`, `startupProbe`, `readinessProbe` e `livenessProbe` vengono
  trasferiti nel workload Kubernetes.
- I Deployment usano selector stabile, Pod template coerente,
  `restartPolicy: Always` e RollingUpdate senza overlap (`maxSurge: 0`,
  `maxUnavailable: 1`) per non duplicare l'identita' dei nodi ROS 2.
- L'executor usa `AppsV1Api` per create, status, delete e reconciliation.
- Readiness e `ProgressDeadlineExceeded` alimentano lo stato del DeploymentJob.
- Un aggiornamento di ConfigMap forza un nuovo Pod template del Deployment.
- `sourceRosSetup: false` e `sourceWs: null` supportano container non ROS come
  PX4 senza alterare il comportamento legacy.
- Le ConfigMap YAML possono essere incluse inline nella richiesta API.
- Il bootstrap inventory accetta coppie robot/nodo multiple; E0 usa Deployment
  controller-managed anche per PX4 SITL.

## Verifiche Eseguite

| Verifica | Risultato |
| --- | --- |
| Suite Python completa | 167/167 PASS nell'ambiente KubeROS |
| Test Django KubeROS update | 16/16 PASS |
| Test C++ state machine plugin | 10/10 PASS |
| Test upstream Event Detector | PASS |
| Build interfacce ROS 2 Jazzy e Humble | PASS |
| Smoke GetHealthSnapshot | PASS: correlation ID, `healthy=true`, Lifecycle `active` e metriche tipizzate |
| Build Event Detector + plugin | PASS |
| Build Application Manager | PASS |
| Build Event Dispatcher | PASS |
| Test ament package-locali | 2/2 PASS |
| Smoke Event Detector | lifecycle active; profilo E2 carica solo TelemetryHeartbeatRule |
| Goal Action BatteryLow | SUCCEEDED, `local_safety_observed`, owner onboard |
| Topic -> dispatcher -> Action P0 | SUCCEEDED, feedback completo e risultato STABLE |
| Manifest P1 | RBAC e Job validi; Python incorporato compilato |
| Immagine Event Detector ROS 2 Humble | core upstream, `px4_msgs` e plugin PX4 compilati; lifecycle attivo |
| P1 live su Kubernetes | PASS, `TelemetryHeartbeatLost` -> nuovo Pod -> `telemetry_recovered (STABLE)` |
| P1 live PX4 + upstream | PASS, detection 2.910 s; rollout 10.555 s; STABLE 31.314 s |
| P1 armed-hover definitivo | PASS in 32.675 s; armato/Hold/no failsafe, quota 2.642-2.800 m, PX4 UID invariato 0/0, solo Agent sostituito, 4 snapshot diagnostici |
| P2 live KubeROS multi-node | PASS in 27 s; route edge, Service edge healthy, recovery STABLE, audit/notifica/snapshot su PVC; PX4 UID invariato, restart 0/0 |
| E4 live rollback | PASS in 60 s; readiness edge fallita, onboard Active e Service healthy, edge/HPA rimossi, audit `rollback_performed=true`, PX4 0/0 |
| E0 baseline distribuita | PASS; 3/3 richieste KubeROS, 12/12 workload KubeROS, 19/19 Deployment, 3 Service health, Lifecycle active, PX4 0/0 |
| E0 da registry privato | PASS; lock `project-093e686`, 16/16 container di progetto con digest runtime corrispondente |
| U1 update differenziale KubeROS | PASS in 18 s; revisione `1 -> 2`, `UPDATE SUCCESS`, solo analytics drone01 sostituita, 11/12 Pod preservati, target con restart 0, Service health active a 95 ms |
| U2 rollback update KubeROS | PASS in 109 s; revisione 3 rifiutata con `UPDATE FAILED`, revisione 2 ripristinata, 11/11 workload non target invariati, target healthy/active |
| E1 BatteryLow con partizione | PASS; control plane fermo 35.216 s, RTL 208 ms, ack 7 ms, AUTO_RTL, P0/audit/notifica dopo recovery, PX4 UID invariato e restart 0/0 |
| Observability recovery live | PASS; 6 record persistono nel restart, replay FIFO 6/6, coda finale 0, Metrics API 19 Pod/21 container, PX4 invariato |
| Isolamento cluster | UID e generazioni dei Deployment in `default` invariati |
| Harness campagna E0-E4 | campagna definitiva: 50 run validi, 48 PASS, un E2 invalido escluso e replacement incluso; CSV/JSON/Markdown generati |
| Manifest riproducibilita' | PASS; commit, 4 submodule, toolchain e 9 riferimenti immagine fissati |
| Manifest riproducibilita' U2 | PASS; commit, 4 submodule, toolchain e 6 riferimenti immagine fissati |

La continuita' della missione armata e' documentata in
[`results/evidence/runs/E2.md`](../results/evidence/runs/E2.md).
Durante l'hover PX4 e' rimasto armato in Hold e senza failsafe nei campioni
prima, durante e dopo il fault. La quota e' variata di 0,158 m, il Pod PX4 ha
conservato UID e restart `0/0`, mentre la policy P1 ha sostituito soltanto il
Pod Agent e ha concluso `telemetry_recovered (STABLE)` in 32,675 s. Il Job
diagnostico ha conservato quattro snapshot JSON correlati.

P2 e' documentata in
[`results/evidence/runs/P2.md`](../results/evidence/runs/P2.md).
Il run usa nodi control-plane, onboard ed edge distinti, Fast DDS Discovery
Server, ApplicationDeployment KubeROS, Lifecycle ROS 2, routing ConfigMap,
HPA Kubernetes, Service health edge, audit su PVC, notifica e snapshot
read-only della piattaforma.

E0 nominale e' documentato in
[`results/evidence/runs/E0.md`](../results/evidence/runs/E0.md).
Il cluster usa un nodo control plane, tre nodi onboard e un nodo edge. Tre
richieste autenticate hanno creato tramite KubeROS PX4, Agent, Event Detector e
Analytics per ogni drone; i tre namespace DDS sono rimasti nominali per 60 s e
i tre Service onboard hanno restituito snapshot `healthy/active`.

U1 definitivo e' documentato in
[`results/evidence/runs/U1.md`](../results/evidence/runs/U1.md).
Il PATCH autenticato ha incrementato soltanto `e0-baseline-drone01` dalla
revisione 1 alla 2 in 18 s. Il reconciler ha sostituito Companion Analytics
senza sovrapporre nodi ROS con la stessa identita', preservando UID e restart
count degli altri 11 Pod; il target e' ripartito con restart count zero.

U2 e' documentato in
[`results/evidence/runs/U2.md`](../results/evidence/runs/U2.md).
La revisione 3 con immagine inesistente e' stata marcata `UPDATE FAILED` e
KubeROS ha ripristinato la revisione 2 in 109 s. I workload non target sono
rimasti invariati 11/11 e il Service ROS finale e' `healthy/active` a 95 ms.

E4 aggiornato e' documentato in
[`results/evidence/runs/E4.md`](../results/evidence/runs/E4.md):
dopo il rollback il Service onboard ha confermato lo stato `healthy/active`.

E1 e' documentata in
[`results/evidence/runs/E1.md`](../results/evidence/runs/E1.md).
Il fault BatteryLow, il comando RTL, l'ack e lo stato `AUTO_RTL` sono avvenuti
sul nodo onboard durante l'arresto del server k3d. Al ritorno del control
plane, l'evento DDS conservato dal detector ha attivato la policy P0, che ha
prodotto audit e notifica senza azioni safety Kubernetes. Il reporter esegue
ora retry limitati per indisponibilita' HTTP transitorie all'avvio.

La chiusura dei gap di osservabilita' e' documentata in
[`results/evidence/runs/OBSERVABILITY.md`](../results/evidence/runs/OBSERVABILITY.md).
Sei record sono rimasti nel PVC attraverso il cambio UID del manager e sono
stati riprodotti FIFO senza duplicati; l'observer ha raccolto live PodMetrics
per 19 Pod e 21 container.

L'harness e il protocollo della campagna sono documentati in
[`docs/EXPERIMENT_CAMPAIGN.md`](EXPERIMENT_CAMPAIGN.md). I pilot superati
sono stati rimossi dopo la validazione della campagna definitiva.

La campagna definitiva e' sintetizzata nella tabella delle verifiche; gli
output completi restano locali e non sono versionati.
Su 51 osservazioni, 50 sono valide e 48 passano. E2 contiene dieci campioni
validi e uno escluso prima dell'iniezione per precondizione disarmata. Il
replacement, incluso indipendentemente dall'esito, mantiene la missione ma
supera lo SLO di recovery di 45 s. Il gate ora impedisce l'iniezione finche'
non osserva tre snapshot consecutivi armed/Hold stabili.

## Collegamenti Ancora Mancanti

1. Retry persistente dell'adapter KubeROS e watch Kubernetes continuo restano
   estensioni future; non bloccano gli esperimenti E0-U2 completati.
2. NetworkPolicy, ResourceQuota e LimitRange sono primitive analizzate ma non
   istanziate nei manifest finali; la governance verificata e' basata su
   ServiceAccount e RBAC namespaced.

## Evoluzioni Possibili

- retry persistente dell'adapter KubeROS e watch Kubernetes continuo;
- NetworkPolicy, ResourceQuota e LimitRange nei manifest finali;
- aggiornamenti multi-modulo e repliche indipendenti degli scenari;
- ulteriore ottimizzazione dell'immagine Event Detector.
