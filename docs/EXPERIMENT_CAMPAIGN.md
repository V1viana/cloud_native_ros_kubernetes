# Campagna Sperimentale E0-E4

Questa procedura automatizza i run ripetuti della matrice sperimentale senza
mescolare i risultati funzionali con le conclusioni statistiche.

## Scenari E KPI

| Scenario | Evento o condizione | KPI principali |
| --- | --- | --- |
| E0 | baseline nominale a tre droni | pass rate, mission success rate |
| E1 | BatteryLow durante partizione control plane | reaction time, ack latency, continuita missione |
| E2 | perdita Micro XRCE-DDS Agent durante hover | recovery/MTTR, convergenza, variazione quota, diagnostica |
| P2 | latenza analytics oltre SLO | convergenza placement, durata manager, continuita missione |
| E4 | placement edge non Ready | convergenza rollback, durata manager, rollback rate |

Ogni campione ricrea il cluster associato allo scenario. E0, E1, P2 ed E4
usano la famiglia multi-node `cloud-native-p2`; E2 usa il cluster isolato
`cloud-native-e2`. Le immagini applicative vengono costruite una volta per
famiglia e importate nuovamente dopo ogni reset.

## Verifica Senza Cluster

```bash
RUNS=2 \
SCENARIOS=e0,e1,e2,p2,e4 \
CAMPAIGN_DRY_RUN=1 \
scripts/run_campaign.sh
```

Il dry-run valida matrice, reset e riuso delle build senza invocare k3d,
Kubernetes o PX4.

## Campagna Completa

```bash
RUNS=10 \
SCENARIOS=e0,e1,e2,p2,e4 \
CAMPAIGN_ID=project-$(date -u +%Y%m%dT%H%M%SZ) \
scripts/run_campaign.sh
```

Per riusare immagini Docker locali gia' costruite, mantenendo obbligatorio
l'import nel cluster ricreato:

```bash
RUNS=10 SKIP_IMAGE_BUILD=1 scripts/run_campaign.sh
```

`SKIP_IMAGE_IMPORT=1` va usato soltanto se le immagini sono gia' presenti in
ciascun cluster target. La variabile legacy `SKIP_IMAGE_BUILD_IMPORT=1` resta
supportata dai singoli runner, ma non e' adatta a cluster appena ricreati.

## Evidenze

Ogni campagna scrive in `results/campaigns/<campaign-id>/`:

- `campaign.json`: configurazione e timestamp iniziale;
- `runs/<scenario>-<numero>/runner.log`: output completo del runner;
- `runs/<scenario>-<numero>/result-dir.txt`: puntatore alle evidenze native;
- `runs/<scenario>-<numero>/status.json`: exit code e scenario;
- `analysis/runs.csv`: righe KPI normalizzate;
- `analysis/summary.json`: statistiche machine-readable;
- `analysis/REPORT.md`: media, mediana, p95 e CI95 Student-t;
- `reproducibility.json`: commit, submodule, toolchain e identificatori
  immutabili delle immagini;
- `status.json`: run completati e falliti.

Il runner acquisisce automaticamente il manifest di riproducibilita' dopo
l'analisi. Le immagini upstream sono fissate tramite digest di registry; per
le build locali non pubblicate viene registrato l'image ID content-addressed.

Il runner continua dopo un fallimento e restituisce exit code non zero alla
fine. In questo modo anche i run falliti restano visibili nel pass rate.

## Validita' Dei Campioni E2

Prima del fault E2 il runner richiede tre campioni consecutivi con PX4 armato,
in Hold, senza failsafe, quota valida tra 1 e 4 m e velocita' verticale entro
0,5 m/s. Se il gate non converge entro il timeout, il fault non viene iniettato
e il run e' marcato `invalid` con `failure_phase=precondition`.

I campioni invalidi sono conservati nell'audit trail, ma esclusi da pass rate e
KPI. Un campione che supera il gate e poi non rispetta lo SLO resta invece un
fallimento valido. Questa regola impedisce di sostituire selettivamente un
risultato negativo.

## Stato Della Validazione

La campagna definitiva del 31 agosto e 1 settembre 2026 comprende 51
osservazioni: 50 campioni validi, dieci per scenario, e un E2 escluso perche'
PX4 era disarmato prima del fault. Il replacement E2 ha superato il gate ed e'
stato incluso come fallimento valido: missione continua, ma convergenza in
48,566 s oltre lo SLO di 45 s.

Il pass rate complessivo e' 48/50 (96%). E0 ed E2 sono 9/10; E1, P2 ed E4
sono 10/10. E2 conserva mission success rate 10/10 e presenta recovery time
medio di 32,460 s sui nove recovery riusciti. Il report validato, che mantiene
traccia anche del campione escluso, e' in
`risultato della campagna validata del 31 agosto 2026` (output locale non versionato).
Commit, versioni e immagini sono in
`reproducibility.json` (output locale non versionato).

Non e' necessario ripetere l'intera matrice: i nove E2 storici inclusi
soddisfano retrospettivamente il medesimo gate dai rispettivi snapshot
`before`; E0, E1, P2 ed E4 non sono interessati dalla correzione.
