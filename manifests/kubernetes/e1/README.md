# E1 - BatteryLow with fleet control-plane partition

E1 verifica che la safety di volo resti onboard e non dipenda da Kubernetes.
Il fault injector pubblica `BatteryStatus` tipizzati al 10% su un topic di test
dedicato; Event Detector e plugin PX4 inviano invece il comando RTL al PX4
SITL reale e osservano i topic reali di ack e stato.

La procedura esegue queste fasi:

1. crea il cluster dedicato con nodi control-plane, onboard ed edge;
2. avvia PX4, Micro XRCE-DDS Agent, Event Detector e fault harness onboard;
3. arma PX4 SITL e avvia un takeoff controllato;
4. attende che gli endpoint DDS locali siano associati e arresta
   `k3d-cloud-native-p2-server-0`, rendendo indisponibili Kubernetes
   API, KubeROS, dispatcher e Application Manager;
5. inietta il fault e osserva localmente comando RTL, ack accepted e stato
   `AUTO_RTL`;
6. pubblica i campioni di recovery e riavvia il control plane;
7. verifica la consegna differita dell'evento tramite QoS DDS
   `reliable/transient_local`, la policy P0, audit e notifica;
8. verifica che PX4 non sia stato riavviato e che edge/HPA siano assenti.

Esecuzione pulita:

```bash
RESET_E1=1 scripts/run_e1.sh
```

Lo script contiene una trap che riavvia il server dedicato anche in caso di
errore. Non seleziona e non modifica il cluster DroneKube.

Il topic `/e1/fault/battery_status` rende il fault deterministico senza
alterare il modello batteria interno di PX4. Il percorso safety validato resta
reale: `/fmu/in/vehicle_command`, `/fmu/out/vehicle_command_ack_v1` e
`/fmu/out/vehicle_status_v4`.

## Ultima Verifica Live

La campagna del 30 agosto 2026 e' terminata con esito `true`: partizione del
control plane di 35.216 s, comando RTL 208 ms dopo il fault, ack accepted dopo
altri 7 ms, stato `AUTO_RTL`, PX4 UID invariato e restart `0/0`. Dopo il
ripristino, P0 ha registrato audit e notifica senza creare Deployment edge o
HPA. Le evidenze sono in
`results/e1/20260830T081937Z/REPORT.md` (output locale non versionato).
