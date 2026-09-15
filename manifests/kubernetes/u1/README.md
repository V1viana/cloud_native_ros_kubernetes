# U1 - Update Differenziale KubeROS

U1 verifica live l'update `replace` introdotto nella copia KubeROS del
progetto. Il banco parte dalla baseline E0 a tre droni e modifica soltanto il
parametro `processing_delay_ms` del modulo
`drone01-companion-analytics-onboard`, da 80 a 95 ms. Il valore resta sotto
la soglia SLO di 250 ms, quindi non deve attivare una migrazione P2.

La revisione viene inviata con una richiesta `PATCH` autenticata alla API
KubeROS. Il client attende con il GET di stato che:

- `e0-baseline-drone01` passi dalla revisione 1 alla 2;
- il relativo `DeploymentEvent UPDATE` termini con `SUCCESS`;
- lo stato finale torni `running`.

## Criteri Di Accettazione

- il Pod analytics di `drone01` riceve un nuovo UID;
- gli altri 11 workload KubeROS conservano UID e restart count;
- i tre PX4, gli Agent e gli Event Detector non vengono sostituiti;
- `drone02` e `drone03` restano alla revisione KubeROS 1;
- il comando del Deployment aggiornato contiene 95 ms;
- il Service ROS health dell'analytics aggiornata risponde
  `healthy`, Lifecycle `active` e latenza 95 ms;
- non compaiono workload edge o HPA.

## Esecuzione

Il comando seguente ricrea esclusivamente il cluster dedicato E0/P2 e produce
le evidenze in `results/u1/<timestamp>`:

```bash
scripts/run_u1.sh
```

`RESET_U1_BASELINE=0 SKIP_IMAGE_BUILD_IMPORT=1` e' riservato a un cluster
dedicato con topologia compatibile, namespace `cloud-native-p2` assente e
immagini gia' importate. Il run ufficiale usa il comando predefinito e ricrea
una baseline pulita.

## Ultima Verifica Live

Il run definitivo del 1 settembre 2026 e' concluso con `PASS` in 18 secondi:

- revisione KubeROS `1 -> 2` ed evento `UPDATE SUCCESS`;
- 1/12 Pod sostituito e 11/12 preservati;
- tutti i PX4 con UID e restart count invariati;
- Pod analytics aggiornato con restart count zero;
- Service health `healthy/active` con latenza 95 ms;
- nessun workload edge o HPA.

I Deployment ROS usano `maxSurge: 0` e `maxUnavailable: 1`, evitando la
sovrapposizione temporanea di nodi con la stessa identita' DDS. Le evidenze
sono nel report U1 (output locale non versionato).
