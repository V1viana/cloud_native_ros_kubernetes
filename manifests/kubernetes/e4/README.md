# E4 Failed Analytics Remediation And Rollback

E4 riusa il cluster multi-node e la pipeline P2, sostituendo esclusivamente il
manifest KubeROS del target analytics edge. Il container e' valido, ma la
readiness probe fallisce intenzionalmente: il fault e' quindi locale,
riproducibile e indipendente da registry o rete esterna.

## Esecuzione

```bash
RESET_P2=1 scripts/run_e4.sh
```

Lo script salva le evidenze in `results/e4/<timestamp>/` e accetta il run solo
se:

- il dispatcher conclude `analytics_migration_failed (ROLLED_BACK)`;
- analytics onboard resta Lifecycle `active` e la route resta `onboard`;
- Deployment edge e HPA risultano assenti dopo il rollback;
- UID e restart count di PX4 restano invariati;
- il Service onboard risponde `healthy` e Lifecycle `active` dopo il rollback;
- audit e notifica contengono `rollback_performed=true`;
- il PVC audit e gli snapshot del Platform Observer sono presenti.

Il manifest fault e'
`manifests/kuberos/e4/analytics-edge-readiness-failure.yaml`. Applicarlo
direttamente con `kubectl` non costituisce una prova E4 valida, perche'
bypasserebbe KubeROS e il feedback loop.

## Ultima Verifica Live

Il run del 30 agosto 2026 e' concluso con esito `PASS` in 60 secondi. Il
Deployment edge e l'HPA risultano assenti, analytics onboard e' `active [3]`,
il Service onboard e' healthy, la route e' `onboard` e PX4 conserva UID e
restart count `0 -> 0`.

Le evidenze complete sono nel
report E4 (output locale non versionato), con la risposta
grezza in `health-snapshot.txt`.
