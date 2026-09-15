# R1 - Private Registry Smoke

R1 verifica che un `ApplicationDeployment` KubeROS possa usare un digest OCI
conservato nel repository Docker Hub privato. Il banco crea temporaneamente il
Secret `kuberos-test-repo`, avvia Companion Analytics onboard, verifica il
digest realmente osservato dal Pod e cancella il workload tramite KubeROS.

```bash
scripts/run_registry_smoke.sh
```

Le evidenze vengono salvate in `results/registry-smoke/<timestamp>/`. Il Secret
e le credenziali non vengono scritti nei risultati e il Secret viene eliminato
se e' stato creato dal runner.
