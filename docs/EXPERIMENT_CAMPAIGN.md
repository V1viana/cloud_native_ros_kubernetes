# Scenari E Campagna Sperimentale

Questo documento spiega come eseguire gli scenari e una campagna ripetuta. Non
contiene risultati.

## Scenari

Ogni scenario ha un runner in `scripts/`. La variante si sceglie con
`VARIANT=a` (imperativa) oppure `VARIANT=b` (dichiarativa).

| ID | Runner | Intervento |
| --- | --- | --- |
| E0 | `run_e0.sh` | flotta di tre droni, nessun guasto |
| E1 | `run_e1.sh` | batteria bassa con control plane non raggiungibile |
| E2 | `run_e2.sh` | sospensione del Micro XRCE-DDS Agent durante un hover |
| P2 | `run_p2.sh` | ritardo di analytics oltre la soglia di latenza |
| E4 | `run_e4.sh` | come P2, con modulo edge che non diventa pronto |
| U1 | `run_u1.sh` | aggiornamento della configurazione di un modulo |
| U2 | `run_u2.sh` | aggiornamento difettoso dopo una revisione valida |
| S1 | `run_s1.sh` | Deployment gestito cancellato o portato a zero repliche |
| S2 | `run_s2.sh` | transitorio di latenza dentro una partizione di rete |
| S3 | `run_s3.sh` | flotta di `N_ROBOTS` droni e un incidente di latenza |
| S4 | `run_s4_matrix.sh` | guasti di batteria, telemetria ed edge, singoli e combinati |
| TTR | `ttr/run_ttr.sh` | ricostruzione della flotta da un cluster vuoto |

Ogni runner crea un cluster k3d nuovo, costruisce e importa le immagini, applica
i manifest, inietta il guasto e raccoglie le evidenze in `results/`, che non e'
versionata.

```bash
VARIANT=b scripts/run_p2.sh
VARIANT=a S1_CASE=delete scripts/run_s1.sh
N_ROBOTS=10 VARIANT=b scripts/run_s3.sh
```

## Campagna Ripetuta

`scripts/run_campaign.sh` ripete gli scenari E0, E1, E2, P2, E4, S1, U1 e U2
per le due varianti. S2, S3, S4 e TTR hanno un proprio runner e non ne fanno
parte.

Verifica senza cluster:

```bash
RUNS=2 CAMPAIGN_DRY_RUN=1 scripts/run_campaign.sh
```

Il dry-run controlla la matrice e l'ordine dei comandi senza invocare k3d,
Kubernetes o PX4.

Campagna:

```bash
RUNS=10 \
SCENARIOS=e0,e1,e2,p2,e4,s1,u1,u2 \
VARIANTS=a,b \
CAMPAIGN_ID=campaign-$(date -u +%Y%m%dT%H%M%SZ) \
scripts/run_campaign.sh
```

Per riusare immagini Docker gia' costruite, mantenendo l'import nel cluster
ricreato:

```bash
RUNS=10 SKIP_IMAGE_BUILD=1 scripts/run_campaign.sh
```

Il runner prosegue dopo un fallimento e restituisce un codice d'uscita diverso
da zero alla fine. Le esecuzioni fallite restano nel conteggio.

## Output Di Una Campagna

In `results/campaigns/<campaign-id>/`:

- `campaign.json`: configurazione e istante di avvio;
- `runs/<scenario>-<numero>/runner.log`: output completo del runner;
- `runs/<scenario>-<numero>/result-dir.txt`: puntatore alle evidenze dello
  scenario;
- `runs/<scenario>-<numero>/status.json`: codice d'uscita e scenario;
- `analysis/runs.csv`: una riga per esecuzione con le grandezze misurate;
- `analysis/summary.json` e `analysis/REPORT.md`: numero di campioni,
  fallimenti, tentativi non validi, media, mediana, 95esimo percentile e
  intervallo di confidenza della media;
- `reproducibility.json`: commit, submodule, versioni degli strumenti e
  identificatori delle immagini;
- `status.json`: esecuzioni completate e fallite.

## Validita' Di Un'Esecuzione

Un'esecuzione e' non valida solo quando fallisce una precondizione definita
prima, e prima che il guasto sia iniettato. Resta nel registro dei tentativi ma
e' esclusa dai riepiloghi. Un'esecuzione valida che non raggiunge l'obiettivo e'
un fallimento, non un'esclusione.

Esempio: prima del guasto di E2 il runner richiede tre campioni consecutivi con
PX4 armato, in Hold, senza failsafe, quota fra 1 e 4 m e velocita' verticale
entro 0,5 m/s. Se la condizione non converge entro il limite, il guasto non
viene iniettato.

## Requisiti

Docker, k3d, kubectl, Python 3 e i submodule inizializzati. Gli scenari con
molti droni richiedono spazio disco proporzionale al numero di nodi: `run_s3.sh`
lo verifica prima di creare il cluster.
