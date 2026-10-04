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

## Cosa contiene questa versione

Questa versione pubblica il codice con cui e' stata eseguita la campagna
sperimentale comparativa della tesi (2-4 ottobre 2026). Contiene solo cio' che
serve a rieseguirla: wrapper della campagna, runner, osservatori e giudici,
manifest, Dockerfile e sorgenti delle immagini, blocco finale di S1 e strumenti
che generano le tabelle dei risultati.

Ogni file eseguito e' identico byte per byte a quello usato negli esperimenti.
Sono documentazione aggiornata, non file eseguiti: questo README, i documenti in
`docs/`, `integrations/README.md` e `operator/README.md`. Le revisioni eseguite
appartengono alla storia di sviluppo, che non e' pubblicata:

| Uso | Revisione eseguita |
| --- | --- |
| Campagna comparativa | `2387d5df739ad8d69cbb900238939b4f260d40df` |
| Blocco finale di S1 | `84ab7b1f3df0ff0e62875c576576dd66e3d746d6` |

Quattro file sono nella versione del blocco finale di S1, che e' stata eseguita
dopo la campagna:

- `scripts/r14_campaign.py`: aggiunge la modalita' del blocco (`--s1-block`), i
  controlli in sola lettura eseguiti prima di creare file e, nel record di ogni
  esecuzione, la coppia a cui appartiene;
- `scripts/r14_schedule.py`: aggiunge il calendario del blocco;
- `scripts/run_s1.sh`: nel ramo della variante B attende che ogni Deployment sia
  disponibile prima di leggere la baseline;
- `scripts/s1_block_extract.py`: estrae i risultati del blocco; non esiste nella
  revisione della campagna.

Tutti gli altri file eseguiti coincidono con entrambe le revisioni. La
corrispondenza file per file e' documentata nell'Appendice B della tesi.

## Requisiti

Host Linux amd64 con Docker, k3d, kubectl e Python 3. La campagna e' stata
eseguita con Docker 29.2.1, k3d v5.8.3 (k3s v1.31.5-k3s1, etcd embedded),
kubectl v1.32.2 e Python 3.12.3, su un solo host con 48 core e 62 GB di RAM.
Dopo il clone inizializzare i submodule:

```bash
git submodule update --init --recursive
```

## Immagini pubblicate

Le immagini eseguite nella campagna sono pubblicate, senza ricostruirle, nel
repository pubblico `docker.io/vivianacasale/cloud-native-ros-thesis`
(linux/amd64). Si scaricano per digest e si rinominano con i nomi locali usati
dai runner:

| Immagine | Digest | Identificativo eseguito |
| --- | --- | --- |
| control-plane | `sha256:029f8736be77ec965814d0510a70f352f7d98fc3d70794ef1cf0945a355a7738` | `sha256:29cec5f6007a87549a07b559c404c517ab976c5bb3ded4e4c17fefe80bd7be36` |
| event-detector | `sha256:b0be04c65773aa6151eb25bb201dadf5d228e0e2fddc12171a4f574e073d4936` | `sha256:bba62a95d9030cb0b913b267dba7e60c83507f469b83a4341d23dd72feca8e60` |
| fleet-operator | `sha256:43daf1678f5ecf581308940b27ff3ba7090a115f74b0fcd52ba0287324bf0d4f` | `sha256:479fbcaaa53e621118a852c459b11506a357b8167aaece026c1dcf1a60014bc5` |
| kuberos | `sha256:74432b1df2784732da8d0815d589fd6618d288299db151d590a3c8c62869e3ae` | `sha256:575bb29050a1e687f3ddb1e731a4468736890409f8bbcde719ae0319d4060ca9` |
| mission-observer | `sha256:79c696c04d721dbaabc5d005f8f6ba946c779c43dfa60103d0376d4004dfdf92` | `sha256:76df864b867fb106799c7ed71a99ee3fb706a8bce3a10cf2fe6762b8633b77e8` |
| s2-harness | `sha256:ad14211786af9a688ce1ed50ba0f65bb29f2371723a6aea55e7f27e42ef46bce` | `sha256:9bb491990e06f5aaa24219a5e92e70b7bf657781cdea05b1d2f1c9773001cb16` |
| state-bridge | `sha256:610553266c6d49c7ce2b38458d8506f6e16f91d6e59f225787d8fd67a2af2139` | `sha256:92b107c697ce37fb05f9a25740c39c94860751c7fa6c0a055f3bb16d816692da` |

```bash
R=docker.io/vivianacasale/cloud-native-ros-thesis
docker pull $R@sha256:029f8736be77ec965814d0510a70f352f7d98fc3d70794ef1cf0945a355a7738
docker tag $R@sha256:029f8736be77ec965814d0510a70f352f7d98fc3d70794ef1cf0945a355a7738 cloud-native-ros/control-plane:p2
docker tag $R@sha256:029f8736be77ec965814d0510a70f352f7d98fc3d70794ef1cf0945a355a7738 cloud-native-ros/control-plane:e2
docker pull $R@sha256:b0be04c65773aa6151eb25bb201dadf5d228e0e2fddc12171a4f574e073d4936
docker tag $R@sha256:b0be04c65773aa6151eb25bb201dadf5d228e0e2fddc12171a4f574e073d4936 cloud-native-ros/event-detector:p2
docker tag $R@sha256:b0be04c65773aa6151eb25bb201dadf5d228e0e2fddc12171a4f574e073d4936 cloud-native-ros/event-detector:e2-upstream
docker pull $R@sha256:43daf1678f5ecf581308940b27ff3ba7090a115f74b0fcd52ba0287324bf0d4f
docker tag $R@sha256:43daf1678f5ecf581308940b27ff3ba7090a115f74b0fcd52ba0287324bf0d4f cloud-native-ros/fleet-operator:p2
docker tag $R@sha256:43daf1678f5ecf581308940b27ff3ba7090a115f74b0fcd52ba0287324bf0d4f cloud-native-ros/fleet-operator:e2
docker pull $R@sha256:74432b1df2784732da8d0815d589fd6618d288299db151d590a3c8c62869e3ae
docker tag $R@sha256:74432b1df2784732da8d0815d589fd6618d288299db151d590a3c8c62869e3ae cloud-native-ros/kuberos:p2
docker pull $R@sha256:79c696c04d721dbaabc5d005f8f6ba946c779c43dfa60103d0376d4004dfdf92
docker tag $R@sha256:79c696c04d721dbaabc5d005f8f6ba946c779c43dfa60103d0376d4004dfdf92 cloud-native-ros/mission-observer:p2
docker pull $R@sha256:ad14211786af9a688ce1ed50ba0f65bb29f2371723a6aea55e7f27e42ef46bce
docker tag $R@sha256:ad14211786af9a688ce1ed50ba0f65bb29f2371723a6aea55e7f27e42ef46bce cloud-native-ros/s2-harness:p2
docker pull $R@sha256:610553266c6d49c7ce2b38458d8506f6e16f91d6e59f225787d8fd67a2af2139
docker tag $R@sha256:610553266c6d49c7ce2b38458d8506f6e16f91d6e59f225787d8fd67a2af2139 cloud-native-ros/state-bridge:p2
```

Le immagini di terze parti (PX4 SITL, Micro XRCE-DDS Agent, Redis, BusyBox,
k3s) si usano dai loro registri originali; le immagini base dei Dockerfile sono
fissate per digest in `config/base_images.json`.

## Rieseguire

**Limite.** Il comando della campagna ricostruisce le immagini dai Dockerfile
prima della prima esecuzione, e i runner di S2 e S4 le ricostruiscono a ogni
esecuzione. Una ricostruzione non e' riproducibile bit per bit (per esempio i
pacchetti installati con `apt-get` non sono fissati): gli identificativi delle
immagini possono differire da quelli pubblicati, e vanno registrati come quelli
di una nuova esecuzione, non come quelli della campagna originale.

Prova a vuoto, senza cluster ne' immagini: stampa il calendario e i limiti.

```bash
python3 scripts/r14_campaign.py --plan
python3 scripts/r14_campaign.py --plan --s1-block
```

Campagna completa, come e' stata eseguita (costruisce le immagini; si riprende
con `--resume` dopo una pausa):

```bash
python3 -B scripts/r14_campaign.py --results <cartella> --rev <revisione>
python3 -B scripts/r14_campaign.py --results <cartella> --rev <revisione> --resume
```

Il wrapper parte solo da un worktree pulito alla revisione indicata, con i
submodule inizializzati, e si ferma nei casi previsti dal protocollo.

Singoli scenari con le immagini pubblicate, senza ricostruirle: i runner di E0,
E1, E2, P2, E4, U1, U2 e S1 accettano `SKIP_IMAGE_BUILD=1` e importano le
immagini locali nel cluster nuovo; il blocco del time-to-rebuild confronta gli
identificativi locali con un elenco invece di costruire. La variante si sceglie
con `VARIANT=a` oppure `VARIANT=b`:

```bash
SKIP_IMAGE_BUILD=1 VARIANT=b scripts/run_p2.sh
```

I runner di S2 (`scripts/run_s2.sh`) e S4 (`scripts/run_s4_bench.sh`)
ricostruiscono sempre le immagini, e i tassi del control plane di S3 nella
finestra fissa sono misurati dal wrapper della campagna, non da
`scripts/run_s3.sh`: questi scenari non si rieseguono sulle immagini pubblicate
senza ricostruirle.

## Risultati e tabelle

I risultati grezzi della campagna non sono pubblicati e sono disponibili su
richiesta; l'impronta SHA-256 dell'archivio e' riportata nell'Appendice B della
tesi. Le tabelle si rigenerano dai risultati grezzi:

```bash
python3 scripts/r14_tables.py --results <campagna> --s1-block <blocco S1> --md
python3 scripts/thesis_campaign_tables.py --tables <tables.json> --out <cartella>
python3 scripts/thesis_n20_tables.py --block <blocco a venti robot> --out <cartella>
```

## Struttura

```text
config/          immagini base fissate per digest, catalogo, soglie
containers/      Dockerfile dei componenti
docs/            specifica, campagna, gap analysis e provenienza
integrations/    KubeROS adattato e dipendenze upstream fissate
interfaces/      messaggi, Service e Action ROS 2
licenses/        inventario della copia di KubeROS
manifests/       risorse Kubernetes e input KubeROS degli scenari
operator/        Fleet Operator, CRD e RBAC della variante B
patches/         patch per le dipendenze upstream
scripts/         wrapper della campagna, runner, osservatori, giudici e tabelle
sources/         componenti ROS 2 e servizi del control plane
```

## Componenti di terze parti

KubeROS e' incluso in `integrations/kuberos` con gli adattamenti del progetto.
Event Detector, Application Manager, Perception Interfaces e PX4 Messages sono
submodule fissati a un commit. Origine, licenze e modifiche sono elencate in
[docs/THIRD_PARTY_PROVENANCE.md](docs/THIRD_PARTY_PROVENANCE.md),
[integrations/README.md](integrations/README.md),
[THIRD_PARTY_NOTICES](THIRD_PARTY_NOTICES) e `licenses/`.

## Licenza

Il codice del progetto e' distribuito con licenza Apache 2.0: vedere
[LICENSE](LICENSE). I componenti di terze parti mantengono la propria licenza.
