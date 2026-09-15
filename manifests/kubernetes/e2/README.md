# E2 Telemetry Recovery

Namespace isolato: `cloud-native-e2`. ROS domain isolato: `229`.

Il test usa PX4 SITL come publisher reale di
`px4_msgs/msg/VehicleStatus`, trasportato dal Micro XRCE-DDS Agent. Il
framework Event Detector upstream carica con `pluginlib` soltanto
`px4_event_detector_plugin::TelemetryHeartbeatRule`. La policy P1 riavvia
il bridge e non dispone di permessi `patch` su altri Deployment.

Poiche' il test single-node usa `hostNetwork` e la porta UDP fissa `8888`, il
Deployment dell'Agent usa la strategia `Recreate`. In questo modo il nuovo Pod
parte soltanto dopo la terminazione del precedente e non puo' verificarsi una
contesa sulla porta host.

Flusso atteso:

```text
terminate real micro_ros_agent process
  -> TelemetryHeartbeatLost STATE_ENTER
  -> dispatcher DeploymentRequest
  -> Kubernetes Event
  -> PATCH drone01-microxrce-agent
  -> Job diagnostico
  -> nuovo Pod e heartbeat
  -> STATE_RECOVERED con correlation_id invariato
  -> telemetry_recovered (STABLE)
```

Il Job diagnostico dura cinque minuti e registra Pod ed Event API in JSON ogni
dieci secondi. Il runner corrente arma PX4, avvia un hover in Hold e campiona
lo stato interno dell'autopilota prima, durante e dopo il fault. La prova passa
soltanto se PX4 resta armato, senza failsafe, con quota valida e stabile, UID e
restart count invariati, mentre viene sostituito esclusivamente il Pod Agent.

Prima dell'iniezione il gate richiede tre snapshot consecutivi con
`arming_state=2`, `nav_state=4` (Hold), nessun failsafe, quota tra 1 e 4 m e
velocita' verticale assoluta non superiore a 0,5 m/s. Se il gate non converge
entro 60 s, il runner non inietta il fault, scrive un report `invalid` e termina
con exit code 2. Un superamento dello SLO dopo il gate e' invece un `FAIL`
valido e viene incluso nelle statistiche.

## Esecuzione

Esecuzione completa su un cluster k3d single-node dedicato:

```bash
RESET_E2=1 ./scripts/run_e2.sh
```

Per riusare cluster e immagini gia' importate:

```bash
SKIP_IMAGE_BUILD_IMPORT=1 ./scripts/run_e2.sh
```

Il runner termina il vero processo Agent figlio, non PX4, e valida lo stato
uORB interno con `vehicle_status` e `vehicle_local_position`. Il run armato definitivo e' in
`results/e2/20260831T135149Z/REPORT.md` (output locale non versionato).
Il replacement valido della campagna definitiva e' in
`results/e2/20260901T070822Z/REPORT.md` (output locale non versionato):
la missione e' rimasta continua, ma il recovery ha superato lo SLO di 45 s ed
e' quindi correttamente registrato come fallimento.
