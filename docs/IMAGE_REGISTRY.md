# Pubblicazione Delle Immagini Di Progetto

## Obiettivo

Le immagini della campagna storica sono fissate tramite Docker image ID locale.
Questo e' sufficiente a identificare i byte osservati sulla VM, ma non permette
a un altro host di scaricarli. Il workflow registry pubblica i tre build di
progetto e salva i digest OCI restituiti dal registry.

Le immagini upstream ROS 2, PX4, Micro XRCE-DDS Agent e Redis hanno gia' un
repo digest nel manifest di riproducibilita' della campagna.

## Immagini Pubblicate

Il catalogo [`config/project_images.json`](../config/project_images.json)
definisce tre build:

| Build | Alias locali coperti |
| --- | --- |
| control-plane | `control-plane:p2`, `control-plane:e2` |
| event-detector | `event-detector:p2`, `event-detector:e2-upstream` |
| kuberos | `kuberos:p2` |

Le tre immagini vengono pubblicate nello stesso repository privato con tag distinti:
`control-plane-<release>`, `event-detector-<release>` e `kuberos-<release>`.
Questo formato rispetta il modello Docker Hub `namespace/repository` e richiede
un solo repository privato.

P2 ed E2 usano lo stesso Dockerfile per control plane ed Event Detector. Una
release nuova li ricostruisce una volta e associa entrambi gli alias allo stesso
digest; questo non modifica gli identificatori archiviati della campagna 2026.

## Dry-run Sicuro

Il comando predefinito non costruisce e non pubblica:

```bash
python3 scripts/publish_project_images.py \
  --repository docker.io/USERNAME/cloud-native-ros-kubernetes \
  --release project-20260902
```

Mostra i comandi `docker tag` e `docker push`. Le credenziali non vengono
lette, salvate o stampate dallo script; l'autenticazione resta responsabilita'
del client Docker.

## Build E Pubblicazione

Dopo avere autenticato Docker verso il registry:

```bash
python3 scripts/publish_project_images.py \
  --repository docker.io/USERNAME/cloud-native-ros-kubernetes \
  --release project-20260902 \
  --build \
  --push
```

`--push` e' obbligatorio per qualsiasi operazione remota. Al termine viene
creato `results/registry/<release>.lock.json` con:

- commit del repository;
- image ID della build locale;
- tag pubblicato;
- digest OCI immutabile `repository@sha256:...`;
- alias locali sostituiti da quella immagine.

Un push non retroattivo produce una release del commit corrente. Non deve
essere presentato come pubblicazione delle immagini originali della campagna,
che restano identificate dai valori in `reproducibility.json`.

## Registry Privato E KubeROS

Per un repository pubblico non serve un pull secret. Per un repository privato:

1. creare in KubeROS un Container Registry Access Token con permesso pull;
2. sincronizzare il token nel namespace del cluster gestito;
3. dichiarare il registry nel manifest KubeROS;
4. selezionarlo nel modulo con `containerRegistryName`.

Esempio logico:

```yaml
containerRegistry:
  - name: project-registry
    imagePullSecretName: project-registry-pull
    imagePullPolicy: IfNotPresent

rosModules:
  - name: companion-analytics
    containerRegistryName: project-registry
    image: docker.io/USERNAME/cloud-native-ros-kubernetes@sha256:DIGEST
```

Il Secret non deve essere committato nel repository. La documentazione tecnica deve riportare il
digest, non token, password o il contenuto di `.docker/config.json`.

## Esito Della Release Project-093e686

Il 2 settembre 2026 i tre tag sono stati pubblicati nel repository Docker Hub
privato `vivianacasale/cloud-native-ros-kubernetes`. Il lock machine-readable e'
nel file [`config/project-image-lock.json`](../config/project-image-lock.json).

I tre pull autenticati tramite digest sono riusciti e una richiesta anonima alla
repository API ha restituito `404`. R1 ha inoltre creato tramite KubeROS un
Companion Analytics temporaneo dal digest control-plane, verificato
`Ready`/imageID/placement onboard e infine eliminato lo stesso
ApplicationDeployment tramite KubeROS. Le evidenze sono nel
[report R1](../results/evidence/runs/REGISTRY.md).

Una successiva esecuzione E0 pulita ha consumato lo stesso lock nei runner:
KubeROS, control plane, osservabilita', bootstrap, tre Event Detector e tre
Companion Analytics sono stati avviati dai digest privati. Il gate runtime ha
confrontato repository e SHA-256 di 16/16 container con gli `imageID` osservati.
Il risultato e' nel [report E0 da registry](../results/evidence/runs/E0_REGISTRY.md)
e nella relativa `image-provenance.csv` (output locale non versionato).

Un tentativo preliminare ha rilevato la scadenza del token Knox sul cluster
rimasto attivo per 13 ore. Il runner ora rinnova il token con il bootstrap
idempotente prima della prova e non salva il valore nei risultati.

## Criterio Di Chiusura

L'incremento e' completo quando:

1. i tre push terminano con successo;
2. il lock contiene tre digest del registry;
3. un pull autenticato risolve i tre digest;
4. un banco breve usa i riferimenti immutabili tramite KubeROS;
5. il nuovo report distingue la release pubblicata dalla campagna storica.

Tutti e cinque i criteri sono soddisfatti dalla release, dal lock, da R1 e
dalla prova E0 completa per digest.
