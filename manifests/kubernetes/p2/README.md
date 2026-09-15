# P2 Analytics SLO Migration

P2 usa esclusivamente il cluster dedicato `cloud-native-p2` e il ROS domain
`230`. Il cluster ha tre nodi distinti: control plane, onboard `drone01` ed
edge. Non riusa e non modifica il cluster DroneKube.

Il flusso verificato dal banco e':

```text
analytics onboard (> 250 ms)
  -> AnalyticsLatencySLO STATE_ENTER
  -> dispatcher / DeploymentRequest Action
  -> Application Manager policy P2
  -> POST ApplicationDeployment alla API KubeROS
  -> Deployment companion-analytics sul nodo edge
  -> configure + activate edge Lifecycle Node
  -> ConfigMap route=edge
  -> deactivate onboard
  -> HPA autoscaling/v2
  -> GetHealthSnapshot sull'istanza edge active
  -> AnalyticsLatencySLO STATE_RECOVERED correlato
  -> Audit Writer su PVC
  -> Operator Notifier + Platform Observer
```

`PX4 SITL` e il Micro XRCE-DDS Agent restano sul nodo onboard. Il test conserva
UID e restart count di PX4 prima e dopo la remediation.

## Esecuzione

```bash
scripts/run_p2.sh
```

Lo script costruisce le immagini, crea il cluster solo se non esiste, importa
le immagini, applica i manifest in ordine e salva le evidenze sotto
`results/p2/<timestamp>/`. Per ricreare deliberatamente il solo cluster P2:

```bash
RESET_P2=1 scripts/run_p2.sh
```

Il runner puo' usare la stessa release privata senza build/import locale:

```bash
IMAGE_LOCK_FILE=config/project-image-lock.json \
  RESET_P2=1 scripts/run_p2.sh
```

Il template passato alla API e'
`manifests/kuberos/p2/analytics-edge.yaml`; applicarlo direttamente con
`kubectl` non costituisce una prova P2 valida.

## Ultima Verifica Live

Il run del 30 agosto 2026 e' concluso con esito `PASS` in 27 secondi:

- analytics migrata e attivata sul nodo edge;
- route aggiornata a `edge` e HPA presente;
- Service edge `GetHealthSnapshot` healthy e Lifecycle active;
- evento di recovery `analytics_slo_recovered (STABLE)` correlato;
- UID PX4 invariato e restart count `0 -> 0`.
- record `incident_completed`, `operator_notification` e
  `platform_snapshot` persistiti sul PVC `p2-audit-data`.

Le evidenze complete sono nel
report P2 (output locale non versionato), con la risposta
grezza in `health-snapshot.txt`.


## Observability Recovery

Il 31 agosto 2026 e' stata verificata live anche la persistenza del reporting.
Con Audit Writer indisponibile, P0 ha accodato sei record; dopo il restart
`Recreate` dell'Application Manager gli stessi sei record erano ancora sul PVC
`p2-manager-outbox`. Al ripristino sono stati consegnati in ordine, senza
duplicati, e la coda e tornata a zero. Il Platform Observer ha inoltre letto
`metrics.k8s.io` per 19 Pod e 21 container, mantenendo 19/19 Deployment
disponibili e il PX4 invariato.

Le evidenze sono nel
report observability (output locale non versionato).
