# E4 Failed Analytics Remediation And Rollback

E4 riusa il cluster multi-node e la pipeline P2, sostituendo esclusivamente il
manifest KubeROS del target analytics edge. Dal protocollo R9 (2026-09-26) il
fault e' lo stesso della variante B: l'edge riceve `sample_period_ms` non numerico,
rclpy rifiuta il parametro all'avvio e il container va in crash loop, quindi
l'edge non diventa mai pronto. Il fault resta locale, riproducibile e indipendente
da registry o rete esterna. Il limite e' il timeout del goal, 45 s, lo stesso da cui
B ricava `onReadinessFailureSec`.

Fino ad allora il fault era una readiness probe che fallisce sempre, con il container
valido: le prove fatte cosi' restano valide per quel protocollo.

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
`manifests/kuberos/e4/analytics-edge-invalid-parameter.yaml` (prima:
`analytics-edge-readiness-failure.yaml`). Applicarlo
direttamente con `kubectl` non costituisce una prova E4 valida, perche'
bypasserebbe KubeROS e il feedback loop.

## Ultima Verifica Live

Con il fault precedente: il run del 30 agosto 2026 e' concluso con esito `PASS` in 60 secondi. Il
Deployment edge e l'HPA risultano assenti, analytics onboard e' `active [3]`,
il Service onboard e' healthy, la route e' `onboard` e PX4 conserva UID e
restart count `0 -> 0`.

Le evidenze complete sono nel
report E4 (output locale non versionato), con la risposta
grezza in `health-snapshot.txt`.
