# U2 - Rollback Di Una Revisione KubeROS Non Valida

U2 verifica live il rollback dell'update `replace` KubeROS. Il runner crea una
baseline E0 a tre droni, applica U1 fino alla revisione 2 e invia una revisione
3 che cambia soltanto l'immagine di Companion Analytics di `drone01` con un
tag inesistente.

## Criteri Di Accettazione

- il client PATCH e il relativo Job sono autenticati;
- KubeROS registra la revisione 3 come `UPDATE FAILED`;
- la revisione attiva resta 2 e lo stato torna `running`;
- l'immagine valida `cloud-native-ros/control-plane:p2` viene ripristinata;
- gli 11 workload non target conservano UID e restart count;
- il Pod target torna Ready con restart count zero;
- il Service ROS risponde `healthy`, Lifecycle `active` e latenza 95 ms;
- nessun errore `rollback failed` compare nell'audit KubeROS.

## Esecuzione

Il comando ricrea esclusivamente `cloud-native-p2` e archivia le evidenze in
`results/u2/<timestamp>`:

```bash
scripts/run_u2.sh
```

Il runner salva report, manifest rifiutato, eventi KubeROS e Kubernetes, stato
dei workload, log e `reproducibility.json` con commit, toolchain e image ID.

## Verifica Definitiva

Il run del 1 settembre 2026 e' `PASS`:

- revisione richiesta `2 -> 3`, revisione finale 2;
- evento revision 3 `UPDATE FAILED`;
- rollback completato in 109 secondi;
- 11/11 workload non target preservati;
- analytics ripristinata `healthy/active` con restart count zero.

Le evidenze sono nel report U2 (output locale non versionato)
e nel manifest di riproducibilita' (output locale non versionato).

U2 misura il rollback interno di KubeROS. E4 resta un esperimento distinto:
verifica il rollback della policy dell'Application Manager dopo un fault di
readiness sul nodo edge.
