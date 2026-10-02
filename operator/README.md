# Fleet Operator

Control plane dichiarativo della variante B. E' scritto in Python con Kopf e
riconcilia quattro risorse del gruppo `dronekube.io/v1alpha1`, definite in
[crds/](crds/).

| Risorsa | Contenuto |
| --- | --- |
| `RobotFleet` | Robot della flotta e selettore dei nodi edge |
| `ROSModule` | Un modulo ROS 2 con lifecycle gestito: pacchetto, robot, placement, stato desiderato, parametri |
| `ROSLifecyclePolicy` | Timeout, tentativi e gate di readiness delle transizioni |
| `AdaptationPolicy` | Soglia su una metrica osservata e azione: migrazione verso l'edge o riavvio di un componente |

## Controller

| Controller | Compito |
| --- | --- |
| `RobotFleetController` | Disponibilita' della flotta e nodi edge |
| `ROSModuleController` | Deployment e Service del modulo, revisioni, ripristino dopo modifiche fuori banda |
| `LifecycleController` | Decide il passo di lifecycle di ogni istanza e lo scrive come comando nello stato del `ROSModule` |
| `AdaptationController` | Apre e chiude gli incidenti, crea il modulo edge con il suo HPA, gestisce il ritorno onboard |

Nessun controller chiama ROS 2. Ogni Pod generato contiene il modulo e il
sidecar State Bridge ([sources/state_bridge](../sources/state_bridge)), che
esegue il comando ricevuto e riporta stato, readiness e metriche nello stato
della risorsa.

## File

- [fleet_operator/](fleet_operator/): codice dei controller.
- [crds/](crds/): definizioni delle quattro risorse.
- [rbac.yaml](rbac.yaml), [deployment.yaml](deployment.yaml): installazione di
  base nel namespace `dronekube-operator`.
- [requirements.txt](requirements.txt): dipendenze Python.

Gli scenari installano l'Operator con i manifest in
[manifests/kubernetes](../manifests/kubernetes).
