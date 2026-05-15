# Vitals Simulator

A scenario-driven IoT sensor data simulator for two healthcare and safety POC projects.
Simulates factory workers and cardiac patients, publishing realistic sensor streams over MQTT
to InfluxDB and Grafana — purpose-built as a live demo tool.

---

## What This Is

This simulator serves two independent IoT proof-of-concept projects that share the same data
pipeline (MQTT → InfluxDB → Grafana).

**POC 1 — Worker Safety Wearable**
Simulates a factory worker wearing a belt unit (IMU) and wrist unit (heart rate + temperature).
Detects unsafe posture, fatigue, falls, and inactivity.

**POC 2 — Remote Healthcare Monitoring**
Simulates a cardiac patient wearing a wrist/chest device (PPG + ECG).
Detects tachycardia, bradycardia, low SpO2, and irregular rhythm.

Both POCs share the same simulation engine. The `personas/` YAML defines who is being simulated.
The `scenarios/` YAML defines what happens to them. No code changes are needed to run a new demo.

---

## Quick Start

### 1. Clone the repository

```bash
git clone https://github.com/<your-username>/vitals-simulator.git
cd vitals-simulator
```

### 2. Start the full dockerized stack (no manual Python runs)

```bash
docker compose -f docker-compose.dev.yml up -d
```

This launches:

- `simulator` (publishes sensor telemetry + demo API)
- `ingestion` (MQTT subscriber + validator + InfluxDB writer)
- `mosquitto`, `influxdb`, `telegraf`, `grafana`

### 3. Follow logs (optional)

```bash
docker compose -f docker-compose.dev.yml logs -f simulator ingestion
```

### 4. Stop the stack

```bash
docker compose -f docker-compose.dev.yml down
```

| Service | URL | Credentials |
|---|---|---|
| Demo control UI | http://localhost:8000 | — |
| Grafana dashboard | http://localhost:3002 | admin / admin |
| InfluxDB | http://localhost:8086 | — |

Notes:

- Default simulator scenario is `personas/welder_factory.yaml` + `scenarios/worker/fatigue_escalation.yaml`.
- Change the simulator scenario by editing the `simulator.command` section in `docker-compose.dev.yml`.
- The ingestion service validates MQTT messages and writes:
  - valid records -> `raw_telemetry` measurement
  - invalid records -> `dead_letter` measurement + `logs/dead_letter.log`

---

## Project Structure

```
vitals-simulator/
│
├── personas/                  WHO is being simulated (YAML config)
│   ├── welder_factory.yaml
│   ├── cardiac_patient.yaml
│   └── _template.yaml         ← copy this to create a new persona
│
├── scenarios/                 WHAT happens to them (YAML config)
│   ├── worker/
│   │   ├── normal_shift.yaml
│   │   ├── fatigue_escalation.yaml
│   │   └── fall_incident.yaml
│   └── health/
│       ├── normal_resting.yaml
│       ├── tachycardia_episode.yaml
│       └── low_spo2_event.yaml
│
├── simulator/
│   ├── core/                  Scenario engine, YAML loaders, condition mapper
│   ├── engine/                Signal, noise, correlation, fault generators
│   ├── sensors/               Per-sensor generators + registry
│   ├── transport/             MQTT publisher + MQTT ingestion service
│   └── utils/                 Logger, time compressor
│
├── demo/
│   ├── demo_api.py            FastAPI control layer (3 endpoints)
│   └── static/index.html     Browser control panel (vanilla JS, no framework)
│
├── config/
│   ├── mqtt_config.yaml
│   └── simulator_config.yaml
│
├── docker/                    Grafana dashboards, Mosquitto, Telegraf config
├── docker-compose.dev.yml     Dev infrastructure stack
├── Dockerfile                 Simulator image (connects to external stack)
├── .env.example               Environment variable template
├── requirements.txt
├── run.py                     Simulator publisher entry point
└── run_ingestion.py           MQTT ingestion entry point
```

---

## Available Personas and Scenarios

### Personas

| Persona file | Description | POC type |
|---|---|---|
| `welder_factory.yaml` | Male factory welder, 40 yrs, 10 yrs experience | Worker Safety |
| `cardiac_patient.yaml` | Female, 65 yrs, known cardiac risk | Healthcare |

### Scenarios

| Scenario file | Description | Duration |
|---|---|---|
| `worker/normal_shift.yaml` | Uneventful 8-hour shift | 8 hours |
| `worker/fatigue_escalation.yaml` | Gradual fatigue, ends with safety alert | 8 hours |
| `worker/fall_incident.yaml` | Sudden fall event with recovery | 2 hours |
| `health/normal_resting.yaml` | Patient at rest, stable vitals | 30 min |
| `health/tachycardia_episode.yaml` | Sudden tachycardia onset and recovery | 30 min |
| `health/low_spo2_event.yaml` | SpO2 drop event | 30 min |

---

## MQTT Topics and Payload

Sensor readings are published to:

```
iots/{poc_type}/{persona_id}/{sensor_name}
```

Examples:
```
iots/worker_safety/welder_factory/heart_rate
iots/worker_safety/welder_factory/posture_angle
iots/healthcare/cardiac_patient/heart_rate
iots/healthcare/cardiac_patient/spo2
```

Payload schema (all sensors, all POCs):

```json
{
  "timestamp_utc": "2026-03-31T10:42:15.123000+00:00",
  "device_id": "sim-welder_factory-001",
  "persona_id": "welder_factory",
  "poc_type": "worker_safety",
  "sensor_name": "heart_rate",
  "value": 84.2,
  "unit": "bpm",
  "phase": "early_fatigue",
  "condition": "normal",
  "quality": "good",
  "fault_active": false,
  "sequence_number": 4217
}
```

---

## Demo API

The demo API runs on `http://localhost:8000` when `--demo` is passed to `run.py`.

| Endpoint | Method | Description |
|---|---|---|
| `/status` | GET | Current scenario, phase, elapsed time |
| `/scenario/load` | POST | Load a scenario by ID, reset engine |
| `/event/trigger` | POST | Fire a named event immediately |

---

## Environment Variables

All connection details are supplied via environment variables. Copy `.env.example` to `.env` and edit:

```bash
MQTT_HOST=localhost
MQTT_PORT=1883
MQTT_USERNAME=                     # optional
MQTT_PASSWORD=                     # optional
INFLUXDB_URL=http://localhost:8086
INFLUXDB_TOKEN=my-token
INFLUXDB_ORG=iot_org
INFLUXDB_BUCKET=iot_poc
LOG_LEVEL=INFO
DEMO_API_PORT=8000
```

---

## Docker Usage

The simulator image connects to an external infrastructure stack. It does **not** own MQTT,
InfluxDB, or Grafana. Those are provided by the POC project's own `docker-compose.yml`.

Build the image:

```bash
docker build -t vitals-simulator .
```

Run against an existing stack:

```bash
docker run --rm \
  -e MQTT_HOST=mosquitto \
  -e MQTT_PORT=1883 \
  -e INFLUXDB_URL=http://influxdb:8086 \
  -e INFLUXDB_TOKEN=my-token \
  -e INFLUXDB_ORG=iot_org \
  -e INFLUXDB_BUCKET=iot_poc \
  -p 8000:8000 \
  vitals-simulator \
  --persona personas/welder_factory.yaml \
  --scenario scenarios/worker/fatigue_escalation.yaml \
  --demo
```

Typical integration inside a POC project's `docker-compose.yml`:

```yaml
services:
  simulator:
    build: ./vitals_simulator
    ports:
      - "8000:8000"
    environment:
      MQTT_HOST: mosquitto
      INFLUXDB_URL: http://influxdb:8086
      INFLUXDB_TOKEN: ${INFLUXDB_TOKEN}
      INFLUXDB_ORG: ${INFLUXDB_ORG}
      INFLUXDB_BUCKET: ${INFLUXDB_BUCKET}
    command: >
      --persona personas/welder_factory.yaml
      --scenario scenarios/worker/fatigue_escalation.yaml
      --demo
    depends_on:
      - mosquitto
      - influxdb
```

---

## Adding a New Persona (No Code Required)

1. Copy `personas/_template.yaml`
2. Fill in baseline sensor ranges and alert thresholds for the new person
3. Save with a descriptive name, e.g. `warehouse_picker.yaml`
4. Reference it in any scenario YAML

## Adding a New Scenario (No Code Required)

1. Copy `scenarios/_template.yaml`
2. Define phases (time windows with sensor ranges) and scripted events
3. Reference an existing persona
4. Save and run with `python run.py --persona ... --scenario ...`

## Adding a New Sensor Type (Code Required)

1. Create `simulator/sensors/new_sensor.py` implementing the `BaseSensor` interface
2. Register it in `simulator/sensors/registry.py`
3. Add its baseline ranges to the relevant persona YAML

---

## Running Tests

```bash
pytest tests/
```

---

## Deactivating the Virtual Environment

```bash
deactivate
```
