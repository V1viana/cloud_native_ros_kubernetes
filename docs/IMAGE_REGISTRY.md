# Immagini Di Progetto E Registry

## Come Vengono Usate Le Immagini

I runner degli scenari costruiscono le immagini dai Dockerfile in `containers/`
e le importano nel cluster k3d. In questo modo un'esecuzione e' identificata
dall'ID locale dell'immagine, che pero' non puo' essere scaricato da un altro
host. Il flusso descritto qui pubblica le immagini su un registry e ne salva i
digest, cosi' un'esecuzione puo' partire da riferimenti immutabili.

Le immagini di terze parti (ROS 2, PX4, Micro XRCE-DDS Agent) sono gia'
identificate dal digest del loro registry.

## Catalogo

[`config/project_images.json`](../config/project_images.json) elenca le immagini
che lo script sa costruire e pubblicare:

| Immagine | Dockerfile | Contenuto |
| --- | --- | --- |
| `control-plane` | `containers/control-plane` | Dispatcher, Application Manager, Companion Analytics, osservabilita' |
| `event-detector` | `containers/event-detector` | Event Detector con le regole PX4 |
| `kuberos` | `containers/kuberos` | KubeROS adattato |
| `state-bridge` | `containers/state-bridge` | sidecar della variante dichiarativa |
| `lifecycle-fault-probe` | `containers/lifecycle-fault-probe` | modulo di prova del lifecycle |
| `mission-observer` | `containers/mission-observer` | osservatore dello stato PX4 |

Le immagini `fleet-operator` e `s2-harness` hanno un Dockerfile in `containers/`
e vengono costruite dai runner, ma non sono nel catalogo.

## Prova Senza Effetti

Il comando predefinito non costruisce e non pubblica:

```bash
python3 scripts/publish_project_images.py \
  --repository docker.io/UTENTE/cloud-native-ros-kubernetes \
  --release NOME-RELEASE
```

Mostra i comandi `docker tag` e `docker push`. Lo script non legge, non salva e
non stampa credenziali: l'autenticazione resta al client Docker.

## Build E Pubblicazione

Dopo l'autenticazione di Docker verso il registry:

```bash
python3 scripts/publish_project_images.py \
  --repository docker.io/UTENTE/cloud-native-ros-kubernetes \
  --release NOME-RELEASE \
  --build \
  --push
```

`--push` e' obbligatorio per ogni operazione remota. Le immagini finiscono nello
stesso repository con un tag per immagine, `<immagine>-<release>`. Al termine
viene scritto `results/registry/<release>.lock.json` con:

- commit del repository;
- ID dell'immagine costruita;
- tag pubblicato;
- digest immutabile `repository@sha256:...`;
- nomi locali sostituiti da quell'immagine.

## Uso Di Un Lock Negli Scenari

Un runner usa i digest di un lock quando riceve il file:

```bash
IMAGE_LOCK_FILE=results/registry/NOME-RELEASE.lock.json scripts/run_e0.sh
```

`scripts/render_image_lock.py` sostituisce i nomi locali nei manifest e
`scripts/verify_image_lock_runtime.py` confronta i digest con le immagini
effettivamente eseguite dai container.

[`config/project-image-lock.json`](../config/project-image-lock.json) e' il lock
di una release del 2 settembre 2026. Contiene le tre immagini della variante
imperativa (`control-plane`, `event-detector`, `kuberos`) e precede la variante
dichiarativa: e' un esempio del formato, non lo stato attuale del codice.

## Registry Privato

Per un repository pubblico non serve un Secret di pull. Per uno privato:

1. creare in KubeROS un token di accesso al registry con permesso di pull;
2. sincronizzarlo nel namespace del cluster gestito;
3. dichiarare il registry nel manifest KubeROS;
4. selezionarlo nel modulo con `containerRegistryName`.

```yaml
containerRegistry:
  - name: project-registry
    imagePullSecretName: project-registry-pull
    imagePullPolicy: IfNotPresent

rosModules:
  - name: companion-analytics
    containerRegistryName: project-registry
    image: docker.io/UTENTE/cloud-native-ros-kubernetes@sha256:DIGEST
```

Il Secret non va versionato. La documentazione riporta il digest, mai token,
password o il contenuto di `.docker/config.json`.
