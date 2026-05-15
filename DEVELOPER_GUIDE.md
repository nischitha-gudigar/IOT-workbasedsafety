# Vitals Simulator — Developer Guide

This document is the complete reference for any developer working on or integrating the Vitals Simulator. It covers architecture, every file's role, YAML schemas, how to extend the system, the MQTT contract, the demo API, and Docker integration.

---

## Table of Contents

1. [What This Simulator Is](#1-what-this-simulator-is)
2. [Architecture Overview](#2-architecture-overview)
3. [How a Simulation Run Works](#3-how-a-simulation-run-works)
4. [File Reference](#4-file-reference)
5. [Signal Generation Deep Dive](#5-signal-generation-deep-dive)
6. [Noise Models](#6-noise-models)
7. [Sensor Correlation](#7-sensor-correlation)
8. [Time Compression](#8-time-compression)
9. [Worker Context and Threshold Tightening](#9-worker-context-and-threshold-tightening)
10. [MQTT Contract](#10-mqtt-contract)
11. [Demo API Reference](#11-demo-api-reference)
12. [YAML Schema Reference](#12-yaml-schema-reference)
13. [Existing Scenarios and Personas](#13-existing-scenarios-and-personas)
14. [Extending the Simulator](#14-extending-the-simulator)
15. [Running Locally](#15-running-locally)
16. [Docker Integration](#16-docker-integration)
17. [Environment Variables Reference](#17-environment-variables-reference)
18. [Error Handling Rules](#18-error-handling-rules)

---

## 1. What This Simulator Is

The Vitals Simulator generates realistic biometric and motion sensor data and publishes it over MQTT. It is designed to drive two IoT proof-of-concept projects that share the same data pipeline (MQTT → InfluxDB → Grafana):

**POC 1 — Worker Safety Wearable**
Simulates a factory worker wearing a belt unit (IMU) and wrist unit (heart rate + temperature). Detects unsafe posture, fatigue, falls, and inactivity.

**POC 2 — Remote Healthcare Monitoring**
Simulates a cardiac patient wearing a wrist/chest device. Detects tachycardia, bradycardia, low SpO2, and irregular rhythm.

**Key design principles:**

- **Scenario-first, not physics-first.** The simulator tells a scripted story. It does not model human physiology from differential equations. Phases are narrative arcs; events are scripted moments. This makes demos predictable and repeatable.
- **Config over code.** Everything a demo runner needs to change lives in YAML. No Python changes are needed to create a new demo.
- **One engine, multiple personas.** The same Python engine runs a factory worker or a cardiac patient. The persona YAML defines who; the scenario YAML defines what happens.
- **Demo-safe reliability.** The simulator must never crash during a live client demo. Sensor errors are logged silently and the last known good value is returned. MQTT disconnect is retried with backoff.

---

## 2. Architecture Overview

### Layer stack

```
┌─────────────────────────────────────────────────┐
│  run.py  — single entry point, CLI args          │
│  demo/demo_api.py  — FastAPI control (--demo)    │
├─────────────────────────────────────────────────┤
│  simulator/core/scenario_engine.py               │
│   ├─ persona_loader.py   (YAML → Persona)        │
│   ├─ scenario_loader.py  (YAML → Scenario)       │
│   ├─ condition_mapper.py (phase → SensorParams)  │
│   └─ worker_context.py   (shift day multiplier)  │
├─────────────────────────────────────────────────┤
│  simulator/sensors/                              │
│   ├─ heart_rate_sensor.py                        │
│   ├─ spo2_sensor.py                              │
│   ├─ temperature_sensor.py                       │
│   ├─ hrv_sensor.py                               │
│   ├─ imu_sensor.py   (posture + 4 accel streams) │
│   └─ registry.py                                 │
├─────────────────────────────────────────────────┤
│  simulator/engine/  — signal and noise library   │
│   ├─ signal.py    (5 behavior types)             │
│   ├─ noise.py     (4 noise models)               │
│   ├─ correlation.py (leader/follower lag)        │
│   └─ fault.py     (dropout, spike, flatline)     │
├─────────────────────────────────────────────────┤
│  simulator/transport/mqtt_publisher.py           │
│   └─ publishes JSON payload to MQTT broker       │
└─────────────────────────────────────────────────┘
          │                    │
      MQTT broker          HTTP :8000
          │                    │
       InfluxDB          demo/static/index.html
          │
       Grafana
```

### Control flow for the demo API

```
Browser → POST /event/trigger
           → engine.trigger_event()
              → event pushed onto _manual_event_queue
                 → _manual_event_loop() picks it up
                    → _apply_event() injects override or fault
                       → _sensor_loop() picks up override on next tick
                          → MQTT publish
```

---

## 3. How a Simulation Run Works

### Startup sequence

1. `run.py` parses CLI args (`--persona`, `--scenario`, `--compression`, `--demo`).
2. Loads `config/simulator_config.yaml` and `config/mqtt_config.yaml` into dicts.
3. Creates `ScenarioEngine(sim_config, mqtt_config)`.
4. Calls `engine.load(persona_path, scenario_path)`:
   - `persona_loader.load_persona()` reads and validates the persona YAML → `Persona` dataclass.
   - `scenario_loader.load_scenario()` reads and validates the scenario YAML → `Scenario` dataclass.
   - `ConditionMapper` is initialised with the persona and sim_config.
   - If `poc_type == worker_safety` and `worker_context` block is present, `WorkerContextProcessor` computes a threshold multiplier.
   - `CorrelationEngine` is loaded with the persona's correlation rules.
   - `FaultController` is created (empty, no active faults).
   - `_build_sensors()` instantiates the correct sensor class for each sensor name in the persona's baseline, using `SENSOR_REGISTRY`.
5. `MQTTPublisher.connect()` establishes the broker connection.
6. If `--demo`, `_start_demo_api(engine)` starts uvicorn on `DEMO_API_PORT`.
7. `engine.start(publish_cb)` is awaited — this is the main loop.

### Main loop (inside `ScenarioEngine.start()`)

The engine runs three concurrent asyncio tasks:

- **`_heartbeat_loop()`** — fires every `heartbeat_interval_seconds` (default 0.1 real seconds). Advances the simulated clock by `compression × heartbeat_interval_seconds` seconds. Checks for phase transitions. Fires any scenario events whose `at_minute` has been reached.
- **`_sensor_loop(sensor)`** — one task per sensor. Sleeps for each sensor's `sampling_interval_ms` (adjusted for compression), calls `sensor.tick()`, and calls the publish callback.
- **`_manual_event_loop()`** — polls the `_manual_event_queue` fed by the demo API's `/event/trigger` endpoint.

### What happens on each sensor tick

1. `sensor.tick(phase_name, sim_time_seconds, override_value)` is called.
2. The signal generator produces a base value using the current phase's `min`, `max`, and `behavior` type.
3. The noise model adds realistic sensor noise.
4. The correlation engine applies a time-lagged adjustment from any leader sensors.
5. If an override is active (from a scenario event or manual trigger), the override value is used instead of the generated value.
6. `FaultController.evaluate()` checks for active faults (dropout, flatline, spike, noise_burst).
7. `ConditionMapper.classify()` compares the final value against persona thresholds (adjusted by threshold_multiplier) → `normal | warning | critical`.
8. A `SensorReading` dataclass is returned.
9. `MQTTPublisher.publish()` builds the topic and JSON payload and sends to the broker.

### Phase transitions

The heartbeat loop tracks `elapsed_sim_seconds` against each phase's `end_minute × 60`. When a phase ends, `_check_phase_transition()`:
1. Increments the phase index.
2. Calls `sensor.update_params(new_params)` on every sensor — this resets signal generators and noise models so there is no bleed-over state from the previous phase.
3. Updates `CorrelationEngine.update_phase_ranges()` so normalisation uses the new phase range.

The `phase_transition_pct` setting in `simulator_config.yaml` (default 0.10) smoothly blends the last 10% of a phase into the next — values interpolate rather than jumping.

### Scenario events

Scenario events fire when `elapsed_sim_minutes >= event.at_minute`. `_apply_event()` handles them two ways:

- **Value override:** The event's `overrides` dict maps sensor names to fixed values. The override is held for `duration_seconds` real seconds (not simulated seconds — this means an event lasts the same wall-clock time regardless of compression). The sensor loop skips signal/noise generation and returns the override value directly.
- **Fault injection:** The event's `fault_type` field (dropout, flatline, spike, noise_burst) calls `FaultController.inject()`.

---

## 4. File Reference

### Entry point

| File | Role |
|---|---|
| [run.py](run.py) | CLI entry point. Parses args, builds engine, connects MQTT, starts demo API, handles SIGTERM/SIGINT graceful shutdown. |

### Core layer — `simulator/core/`

| File | Role |
|---|---|
| [scenario_engine.py](simulator/core/scenario_engine.py) | Main orchestrator. Owns simulation clock, phase transitions, event firing, sensor task management. |
| [persona_loader.py](simulator/core/persona_loader.py) | Reads persona YAML → `Persona` dataclass. Raises `ConfigurationError` on any missing or invalid field. |
| [scenario_loader.py](simulator/core/scenario_loader.py) | Reads scenario YAML → `Scenario` dataclass. Validates phase contiguity, compression > 0, event times within bounds. |
| [condition_mapper.py](simulator/core/condition_mapper.py) | Bridges YAML world and engine world. Converts scenario phase + persona baseline → `SensorParams`. Classifies readings as normal/warning/critical. |
| [worker_context.py](simulator/core/worker_context.py) | Computes threshold multiplier from shift day and medical leave history. Only relevant for `poc_type: worker_safety`. |

### Engine layer — `simulator/engine/`

These modules form the reusable signal generation library. They have no knowledge of YAML, personas, or scenarios.

| File | Role |
|---|---|
| [signal.py](simulator/engine/signal.py) | Five signal behavior generators: `oscillating_stable`, `monotonic_rising`, `monotonic_falling`, `mean_reverting`, `step`. |
| [noise.py](simulator/engine/noise.py) | Four noise models: `gaussian`, `drift`, `burst`, `quantization`. |
| [correlation.py](simulator/engine/correlation.py) | Time-lagged leader/follower coupling. Maintains per-leader lag buffers, applies interpolated adjustments to followers. |
| [fault.py](simulator/engine/fault.py) | Fault injection: `dropout`, `flatline`, `spike`, `noise_burst`. Driven by scenario events and manual API triggers. |

### Sensor layer — `simulator/sensors/`

| File | Role |
|---|---|
| [base.py](simulator/sensors/base.py) | `BaseSensor` abstract class and `SensorReading` dataclass. All sensors implement this interface. |
| [registry.py](simulator/sensors/registry.py) | `SENSOR_REGISTRY` dict mapping sensor name strings → sensor classes. The extension point for new sensor types. |
| [heart_rate_sensor.py](simulator/sensors/heart_rate_sensor.py) | PPG heart rate (bpm). Participates in correlation as both leader (drives temperature/SpO2) and follower (driven by accel). |
| [spo2_sensor.py](simulator/sensors/spo2_sensor.py) | Pulse oximeter SpO2 (%). Uses quantization noise by default. Values clamped to [0, 100]. Follows HR as leader. |
| [temperature_sensor.py](simulator/sensors/temperature_sensor.py) | Skin temperature (°C). Uses drift noise. Follows HR as leader with ~120s lag. Changes slowly. |
| [hrv_sensor.py](simulator/sensors/hrv_sensor.py) | Heart rate variability (ms). Physiologically inverse to HR — tachycardia causes HRV to drop. Follows HR with negative coupling. |
| [imu_sensor.py](simulator/sensors/imu_sensor.py) | 6-axis IMU. Primary output: `posture_angle`. Secondary outputs in `extra` dict: `accel_magnitude`, `accel_x`, `accel_y`, `accel_z`. Fall events inject a spike fault on accel. |

### Transport layer

| File | Role |
|---|---|
| [transport/mqtt_publisher.py](simulator/transport/mqtt_publisher.py) | Async-compatible paho-mqtt wrapper. Builds topic from `prefix/poc_type/persona_id/sensor_name`. JSON-encodes and publishes `SensorReading`. Reconnects with exponential backoff. |

### Utilities

| File | Role |
|---|---|
| [utils/logger.py](simulator/utils/logger.py) | Structured JSON logger. All modules use `get_logger(__name__)`. Extra fields are merged into the JSON payload. Never use `print()`. |
| [utils/time_utils.py](simulator/utils/time_utils.py) | `TimeCompressor` — converts between real wall-clock time and simulated time using the compression factor. |

### Demo layer

| File | Role |
|---|---|
| [demo/demo_api.py](demo/demo_api.py) | FastAPI application. 6 endpoints. Engine injected at startup via `create_app(engine)`. No authentication (localhost only). |
| [demo/static/index.html](demo/static/index.html) | Vanilla JS control panel. Polls `/status` every 2 seconds. Dropdowns for persona/scenario, speed buttons, event trigger buttons, live event log. |

### Config files

| File | Role |
|---|---|
| [config/simulator_config.yaml](config/simulator_config.yaml) | Engine tick rate, noise intensity map, signal/noise model constructor params, condition threshold fractions, worker context multipliers. |
| [config/mqtt_config.yaml](config/mqtt_config.yaml) | MQTT broker host/port, connection settings, QoS, topic prefix. All overridable by env vars. |

---

## 5. Signal Generation Deep Dive

Signal generators live in [simulator/engine/signal.py](simulator/engine/signal.py). Each generator receives `phase_min`, `phase_max`, and `phase_progress` (0.0 → 1.0 over the phase duration) and returns a base value before noise is applied.

### Behavior types

| Behavior | What it models | Example use |
|---|---|---|
| `oscillating_stable` | Oscillates around midpoint ± `amplitude_fraction × range`. The person is in a steady state with natural variation. | Resting HR, normal SpO2, quiet shift work |
| `monotonic_rising` | Linearly rises from `phase_min` toward `phase_max` as phase progresses. | HR during fatigue buildup, temperature during exertion, SpO2 drop during onset |
| `monotonic_falling` | Linearly falls from `phase_max` toward `phase_min`. | HR recovery post-episode, SpO2 recovering, HRV during tachycardia onset |
| `mean_reverting` | Starts at midpoint, deviates with configured volatility, gets pulled back. Models spikes that self-correct. | HR during peak tachycardia (erratic but bounded), stress response |
| `step` | Jumps to `phase_max` at the start of the phase and holds with minimal jitter. | Discrete state changes — sensor alarm, posture threshold crossing |

### Constructor params (from `simulator_config.yaml`)

```yaml
signal_params:
  oscillating_stable:
    amplitude_fraction: 0.05    # ±5% of range width oscillation
  monotonic_rising:
    jitter_fraction: 0.01       # ±1% jitter around the trend line
  monotonic_falling:
    jitter_fraction: 0.01
  mean_reverting:
    reversion_speed: 0.15       # pull strength toward midpoint per tick
    volatility: 0.03            # random step size as fraction of range
  step:
    jitter_fraction: 0.005      # ±0.5% to avoid flat-line appearance
```

These params are global. They apply to every sensor using that behavior. Adjust here to tune the feel of all sensors at once.

---

## 6. Noise Models

Noise models live in [simulator/engine/noise.py](simulator/engine/noise.py). They run after the signal generator and add realistic sensor imperfection to the base value.

### Models

| Model | What it models | Default for |
|---|---|---|
| `gaussian` | Zero-mean white noise (sigma = `intensity × range_width`). Clean electronic noise. | HR, posture, accel, HRV |
| `drift` | Accumulated random walk with weak mean reversion. Simulates calibration drift. Stateful — offset persists across ticks. | Skin temperature |
| `burst` | Occasional large transients (motion artifacts). Background noise most of the time, rare large spikes. | High-activity scenarios, IMU during movement |
| `quantization` | Stepwise output. Value is rounded to nearest `step_size`. | SpO2 (pulse oximeters report in 1% increments) |

### Noise intensity map

Scenario YAML specifies `noise_intensity: low | medium | high`. This maps to a dimensionless float in `simulator_config.yaml`:

```yaml
noise_intensity_map:
  low:    0.02    # ±2% of range — very clean
  medium: 0.05    # ±5% of range — noticeable
  high:   0.12    # ±12% of range — clearly noisy
```

The noise model multiplies `intensity × range_width` to get the actual sigma or step size in the sensor's native units.

---

## 7. Sensor Correlation

Physiological coupling between sensors is modelled via a time-lagged leader/follower mechanism in [simulator/engine/correlation.py](simulator/engine/correlation.py).

### How it works

1. Every tick, the leader sensor records its normalised value (relative to phase range) into a circular lag buffer with a timestamp.
2. When the follower sensor is ticking, `correlation_engine.get_adjustment(follower_name, sim_time)` looks up the leader's value at `sim_time - lag_seconds` via linear interpolation.
3. The adjustment is `coupling_strength × (lagged_leader_deviation_from_midpoint)` scaled back to the follower's range.
4. This adjustment is added to the follower's base value after signal generation and before noise.

### Negative coupling

`coupling_strength` can be negative. A rising leader causes a falling follower. Used for:
- `heart_rate → spo2`: Higher HR slightly drops SpO2 under exertion.
- `heart_rate → heart_rate_variability`: Tachycardia causes HRV to drop.

### Worker safety correlations (welder_factory)

| Leader | Follower | Strength | Lag | Why |
|---|---|---|---|---|
| `accel_magnitude` | `heart_rate` | 0.4 | 30s | Physical activity drives heart rate with a short lag |
| `heart_rate` | `skin_temperature` | 0.3 | 120s | Elevated HR warms the skin slowly |
| `posture_angle` | `accel_magnitude` | 0.5 | 2s | Forward bending immediately alters accelerometer readings |

### Healthcare correlations (cardiac_patient)

| Leader | Follower | Strength | Lag | Why |
|---|---|---|---|---|
| `heart_rate` | `spo2` | −0.25 | 60s | Tachycardia slightly reduces blood oxygen saturation |
| `heart_rate` | `heart_rate_variability` | −0.4 | 10s | High HR compresses beat-to-beat variation |

---

## 8. Time Compression

Time compression allows an 8-hour shift to play in 8 real minutes (compression = 60). Implemented in [simulator/utils/time_utils.py](simulator/utils/time_utils.py).

```
simulated_seconds_elapsed = real_seconds_elapsed × compression
```

### Effect on each component

| Component | Effect |
|---|---|
| Phase durations | Phase `start_minute` / `end_minute` are in simulated minutes. At compression=60, a 180-minute phase lasts 3 real minutes. |
| Sensor sampling intervals | `sampling_interval_ms` from the persona is divided by compression. At compression=60, a 1000ms HR sensor publishes every 16.7ms real time. |
| Event `at_minute` | Simulated minute. The engine fires the event when simulated clock reaches that value. |
| Event `duration_seconds` | **Real seconds, not simulated.** A 45-second posture spike lasts 45 real seconds at any compression level. This is intentional — you want to see the event on the dashboard regardless of compression. |

### Setting compression

Three places in priority order:
1. `--compression` CLI flag (highest priority)
2. `compression` field in the scenario YAML
3. Default is whatever is in the scenario YAML (no hardcoded fallback)

The demo API's `/compression/set` endpoint can change compression at runtime without stopping the engine.

---

## 9. Worker Context and Threshold Tightening

Implemented in [simulator/core/worker_context.py](simulator/core/worker_context.py). Only active when `poc_type: worker_safety` and the scenario has a `worker_context` block.

### What it does

A worker on day 4 of a 5-day shift is more fatigued than on day 1. The same HR of 105 bpm should trigger a warning earlier for an exhausted worker. Instead of writing different thresholds per scenario, the engine applies a multiplier to all persona thresholds before classification.

```
adjusted_threshold = persona_threshold × multiplier
```

A multiplier below 1.0 tightens thresholds (alerts fire sooner).

### Default multipliers

| Shift day | Multiplier | Effect |
|---|---|---|
| 1 | 1.00 | Thresholds unchanged |
| 2 | 0.95 | 5% tighter |
| 3 | 0.90 | 10% tighter |
| 4 | 0.82 | 18% tighter (current `fatigue_escalation` scenario) |
| 5 | 0.75 | 25% tighter |

If `recent_medical_leave: true`, the multiplier is reset to day 1 regardless of `shift_day`. This represents a worker returning from medical leave who is considered rested.

These values are in `simulator_config.yaml` under `worker_context.shift_day_multipliers` and can be adjusted without changing Python code.

---

## 10. MQTT Contract

This section describes the interface contract between the simulator and the POC projects. **Breaking this contract requires coordinating with every consuming POC project.**

### Topic pattern

```
{prefix}/{poc_type}/{persona_id}/{sensor_name}
```

Default prefix is `iots`. Examples:

```
iots/worker_safety/welder_factory/heart_rate
iots/worker_safety/welder_factory/posture_angle
iots/worker_safety/welder_factory/accel_magnitude
iots/worker_safety/welder_factory/accel_x
iots/healthcare/cardiac_patient/heart_rate
iots/healthcare/cardiac_patient/spo2
iots/healthcare/cardiac_patient/heart_rate_variability
```

### Payload schema

Every sensor publishes JSON with this exact shape:

```json
{
  "timestamp_utc":   "2026-04-06T10:42:15.123456+00:00",
  "device_id":       "sim-welder_factory-001",
  "persona_id":      "welder_factory",
  "poc_type":        "worker_safety",
  "sensor_name":     "heart_rate",
  "value":           84.2,
  "unit":            "bpm",
  "phase":           "early_fatigue",
  "condition":       "normal",
  "quality":         "good",
  "fault_active":    false,
  "sequence_number": 4217
}
```

### Field reference

| Field | Type | Values | Description |
|---|---|---|---|
| `timestamp_utc` | string (ISO 8601) | — | Wall-clock UTC publish time |
| `device_id` | string | `sim-{persona_id}-001` | Simulated device identifier |
| `persona_id` | string | — | Matches persona YAML `persona_id` |
| `poc_type` | string | `worker_safety` \| `healthcare` | From persona YAML |
| `sensor_name` | string | — | Sensor identifier (matches topic last segment) |
| `value` | number \| null | — | Sensor reading in native units. `null` during dropout fault |
| `unit` | string | `bpm`, `celsius`, `degrees`, `g`, `percent`, `ms` | Physical unit |
| `phase` | string | — | Current scenario phase name |
| `condition` | string | `normal` \| `warning` \| `critical` | Classification against persona thresholds |
| `quality` | string | `good` \| `uncertain` \| `bad` | Sensor signal quality. `bad` during fault |
| `fault_active` | boolean | — | True when any fault is injected on this sensor |
| `sequence_number` | integer | — | Monotonically increasing per engine start |

### QoS and retain

- QoS 1 (at-least-once) by default. Configurable in `mqtt_config.yaml`.
- `retain: false` — vitals are real-time readings, not persistent state.

---

## 11. Demo API Reference

The demo API starts when `--demo` is passed to `run.py`. Served on `DEMO_API_PORT` (default 8000). Interactive docs available at `http://localhost:8000/docs`.

### `GET /status`

Returns current engine state. Polled every 2 seconds by the control UI.

**Response:**

```json
{
  "running":              true,
  "scenario_id":          "fatigue_escalation",
  "persona_id":           "welder_factory",
  "current_phase":        "early_fatigue",
  "elapsed_sim_minutes":  214.5,
  "total_sim_minutes":    480,
  "progress_pct":         44.7,
  "compression":          60,
  "events_fired":         1,
  "sequence_number":      12840,
  "available_events":     ["posture_spike", "inactivity", "fall"]
}
```

When no scenario is loaded, `running` is `false` and most fields are `null`.

---

### `POST /scenario/load`

Loads a new scenario and reloads the engine. Stops the current simulation, loads the new persona/scenario, and restarts. The engine does not exit — it continues running with the new config.

**Request:**

```json
{
  "persona_path":  "personas/welder_factory.yaml",
  "scenario_path": "scenarios/worker/fatigue_escalation.yaml",
  "compression":   60
}
```

`compression` is optional. If omitted, the scenario YAML's compression is used.

The API auto-resolves the persona: if the scenario YAML's `persona` field doesn't match what you sent, the matching persona file is found automatically in `personas/`.

**Response:**

```json
{
  "success": true,
  "message": "Scenario loaded successfully (auto-matched persona: welder_factory)"
}
```

---

### `POST /event/trigger`

Fires a named event immediately, independent of the scenario timeline. Useful for live demos where you want to trigger a fall or tachycardia on demand.

**Request:**

```json
{
  "event_type":       "fall",
  "duration_seconds": 5.0
}
```

`duration_seconds` is optional. If omitted, the scenario YAML's `duration_seconds` is used.

**Response:**

```json
{
  "success": true,
  "message": "Event 'fall' triggered"
}
```

Returns HTTP 404 if `event_type` is not defined in the loaded scenario.

---

### `POST /compression/set`

Changes time compression at runtime without stopping the engine. Useful to slow down during a critical demo moment.

**Request:**

```json
{ "compression": 10 }
```

---

### `GET /scenarios`

Lists all scenario YAML files and their required persona.

**Response:**

```json
{
  "scenarios": [
    { "path": "scenarios/worker/fatigue_escalation.yaml", "persona_required": "welder_factory" },
    { "path": "scenarios/health/tachycardia_episode.yaml", "persona_required": "cardiac_patient" }
  ]
}
```

---

### `GET /personas`

Lists all persona YAML files.

**Response:**

```json
{
  "personas": [
    "personas/welder_factory.yaml",
    "personas/cardiac_patient.yaml"
  ]
}
```

---

## 12. YAML Schema Reference

### Persona YAML

Full annotated schema with every field and its valid values:

```yaml
schema_version: "1.0"           # required, always "1.0"
persona_id: welder_factory      # required, must match the filename stem
description: "..."              # required, human-readable
poc_type: worker_safety         # required: worker_safety | healthcare

baseline:
  <sensor_name>:                # key must match a sensor in SENSOR_REGISTRY
    min: 62                     # required: float, minimum baseline value
    max: 78                     # required: float, maximum baseline value
    unit: bpm                   # required: physical unit string
    sampling_interval_ms: 1000  # required: int, publish frequency in ms

thresholds:
  heart_rate_high: 110          # float — threshold name is looked up by condition_mapper
  temperature_high: 38.0
  posture_angle_warning: 40
  posture_angle_critical: 55
  accel_fall_threshold: 3.5
  heart_rate_low: 50            # for healthcare: bradycardia lower bound
  spo2_low: 93
  hrv_low: 20

noise_profile:
  <sensor_name>: gaussian       # gaussian | drift | burst | quantization
                                # sensor_name must exist in baseline

correlations:                   # optional list
  - leader_sensor: accel_magnitude     # must exist in baseline
    follower_sensor: heart_rate        # must exist in baseline
    coupling_strength: 0.4             # float, can be negative
    lag_seconds: 30                    # float > 0
```

**Threshold naming convention understood by `condition_mapper.py`:**

| Threshold name | Direction | Fires when |
|---|---|---|
| `heart_rate_high` | High | HR >= threshold |
| `heart_rate_low` | Low | HR <= threshold |
| `temperature_high` | High | temp >= threshold |
| `spo2_low` | Low | SpO2 <= threshold |
| `hrv_low` | Low | HRV <= threshold |
| `posture_angle_warning` | High | posture >= threshold (warning) |
| `posture_angle_critical` | High | posture >= threshold (critical) |
| `accel_fall_threshold` | High | accel >= threshold (critical) |

---

### Scenario YAML

```yaml
schema_version: "1.0"
scenario_id: fatigue_escalation           # required, unique
description: "..."                         # required
persona: welder_factory                    # required, must match a persona_id
poc_type: worker_safety                    # required, must match persona's poc_type

total_duration_minutes: 480               # required: total simulated duration
compression: 60                           # required: simulated seconds per real second

worker_context:                           # optional, worker_safety only
  shift_day: 4                            # 1–5
  consecutive_days: 4                     # 1–5
  recent_medical_leave: false             # bool

phases:                                   # required: list, minimum 1 phase
  - name: normal_work                     # required: unique within scenario
    start_minute: 0                       # required: simulated minutes, must start at 0
    end_minute: 180                       # required: simulated minutes
    sensors:
      <sensor_name>:
        min: 65                           # required: phase min value
        max: 80                           # required: phase max value
        behavior: oscillating_stable      # required: one of the 5 behavior types
    noise_intensity: low                  # required: low | medium | high
    activity: walking                     # optional: walking | standing | bending | idle

  # Phases must be contiguous: end_minute of phase N == start_minute of phase N+1
  # The last phase must end at total_duration_minutes

events:                                   # optional list
  - at_minute: 310                        # simulated minute when event fires
    type: posture_spike                   # name used by /event/trigger endpoint
    description: "..."                    # human-readable, shown in demo UI
    overrides:
      posture_angle: 68                   # sensor_name: value to inject
    duration_seconds: 45                  # real seconds (not simulated)
    # Optional fault injection instead of value override:
    # fault_type: dropout | flatline | spike | noise_burst
    # sensor: sensor_name
    # fault_factor: 3.0
```

**Phase contiguity rule:** Phases must be contiguous and cover the full `total_duration_minutes`. The loader rejects any gap or overlap.

```
phase 1: start=0,   end=180  ✓
phase 2: start=180, end=360  ✓
phase 3: start=360, end=480  ✓ (== total_duration_minutes)
```

---

## 13. Existing Scenarios and Personas

### Personas

| File | POC type | Who |
|---|---|---|
| [personas/welder_factory.yaml](personas/welder_factory.yaml) | `worker_safety` | Male welder, 40 yrs, 10 yrs experience |
| [personas/cardiac_patient.yaml](personas/cardiac_patient.yaml) | `healthcare` | Female, 65 yrs, known cardiac risk |
| [personas/_template.yaml](personas/_template.yaml) | — | Copy this to create a new persona |

### Scenarios

| File | Persona | Duration | Compression | Story |
|---|---|---|---|---|
| [scenarios/worker/fatigue_escalation.yaml](scenarios/worker/fatigue_escalation.yaml) | welder_factory | 480 min (8h) | 60× (8 real min) | Gradual fatigue → posture spike → inactivity → fall |
| [scenarios/worker/normal_shift.yaml](scenarios/worker/normal_shift.yaml) | welder_factory | 480 min | 60× | Uneventful shift, no incidents |
| [scenarios/worker/fall_incident.yaml](scenarios/worker/fall_incident.yaml) | welder_factory | 120 min | 30× | Short shift ending in fall event with recovery |
| [scenarios/health/tachycardia_episode.yaml](scenarios/health/tachycardia_episode.yaml) | cardiac_patient | 30 min | 10× (3 real min) | Resting → onset → tachycardia peak → recovery |
| [scenarios/health/normal_resting.yaml](scenarios/health/normal_resting.yaml) | cardiac_patient | 30 min | 10× | Healthy baseline, no events |
| [scenarios/health/low_spo2_event.yaml](scenarios/health/low_spo2_event.yaml) | cardiac_patient | 30 min | 10× | SpO2 drop episode with recovery |
| [scenarios/_template.yaml](scenarios/_template.yaml) | — | — | — | Copy this to create a new scenario |

---

## 14. Extending the Simulator

### Adding a new persona (no code required)

A persona defines WHO is being simulated — their baseline sensor ranges, alert thresholds, noise characteristics, and physiological coupling.

1. Copy [personas/_template.yaml](personas/_template.yaml) to a new file, e.g. `personas/warehouse_picker.yaml`.
2. Set `persona_id` to match the filename stem exactly: `persona_id: warehouse_picker`.
3. Set `poc_type` to `worker_safety` or `healthcare`.
4. Fill in `baseline` for every sensor the persona will use. Only sensors listed in `baseline` are active.
5. Fill in `thresholds`. Use the threshold names that `condition_mapper.py` recognises (see table in Section 12).
6. Fill in `noise_profile` — one entry per sensor in `baseline`. Pick `gaussian` as the default for most sensors.
7. Add `correlations` if there are physiological links between sensors (optional).

No Python changes needed.

---

### Adding a new scenario (no code required)

A scenario defines WHAT happens — phases, events, timing, and worker context.

1. Copy [scenarios/_template.yaml](scenarios/_template.yaml) to an appropriate subdirectory.
2. Set `scenario_id` and `persona` (must match an existing `persona_id`).
3. Set `total_duration_minutes` and `compression`. The real-time duration = `total / compression` minutes.
4. Write phases. Rules:
   - Phases must be contiguous (no gaps, no overlaps).
   - First phase must start at minute 0.
   - Last phase must end at `total_duration_minutes`.
   - Each sensor in a phase must exist in the persona's `baseline`.
5. Write events for key moments. `type` is the string used by `/event/trigger`. `overrides` maps sensor names to the injected value. `duration_seconds` is real seconds.

**Choosing behavior types for phases:**

- Steady state → `oscillating_stable`
- Gradual deterioration → `monotonic_rising` (HR, posture) or `monotonic_falling` (SpO2, HRV)
- Erratic peak that self-corrects → `mean_reverting`
- Sudden jump to a new level → `step`

**Choosing noise intensity:**

- Resting / normal work → `low`
- Moderate exertion / onset → `medium`
- Heavy exertion / critical phase → `high`

No Python changes needed.

---

### Adding a new sensor type (code required)

Four steps:

**Step 1 — Implement the sensor class**

Create `simulator/sensors/new_sensor.py`:

```python
from simulator.sensors.base import BaseSensor, SensorReading
from simulator.core.condition_mapper import SensorParams

class NewSensor(BaseSensor):
    def __init__(self, params: SensorParams, ...):
        self._params = params
        # Initialise signal_gen and noise_model from params
        # ...

    @property
    def sensor_name(self) -> str:
        return "new_sensor_name"

    @property
    def sampling_interval_ms(self) -> int:
        return self._params.sampling_interval_ms

    def tick(self, phase_name: str, sim_time_seconds: float,
             override_value: float | None = None) -> SensorReading:
        # 1. Generate base value from signal_gen
        # 2. Apply noise
        # 3. Apply correlation adjustment (if any)
        # 4. Use override_value if not None
        # 5. Evaluate fault
        # 6. Classify condition
        # 7. Return SensorReading(...)
        ...

    def update_params(self, params: SensorParams) -> None:
        self._params = params
        # Reset signal_gen and noise_model
```

**Step 2 — Register it**

Add to [simulator/sensors/registry.py](simulator/sensors/registry.py):

```python
from simulator.sensors.new_sensor import NewSensor

SENSOR_REGISTRY = {
    ...
    "new_sensor_name": NewSensor,
}
```

**Step 3 — Add to a persona**

Add the sensor to the relevant persona YAML under `baseline`:

```yaml
baseline:
  new_sensor_name:
    min: 0
    max: 100
    unit: units
    sampling_interval_ms: 1000
```

Add a threshold if needed under `thresholds`, and a noise profile entry under `noise_profile`.

**Step 4 — Add condition classification (if needed)**

If the new sensor needs condition classification, add the threshold logic to [simulator/core/condition_mapper.py](simulator/core/condition_mapper.py) in the `classify()` method.

No changes to the engine, transport, or any other layer are needed.

---

### Adding a new POC use case

A new POC use case is just a new `poc_type` string. To add one:

1. Create a new persona YAML with `poc_type: your_new_poc`.
2. Create scenario YAMLs referencing that persona.
3. The engine, sensors, and transport layer handle it automatically — `poc_type` flows through to the MQTT topic and payload.
4. If the new POC needs custom threshold logic (similar to worker context), add a new module alongside [worker_context.py](simulator/core/worker_context.py) and wire it into the engine's `load()` method.

---

## 15. Running Locally

### Prerequisites

```bash
pip install -r requirements.txt
```

### Run commands

```bash
# Worker safety — 8-hour fatigue scenario (runs in 8 real minutes at 60× compression)
python run.py \
  --persona personas/welder_factory.yaml \
  --scenario scenarios/worker/fatigue_escalation.yaml

# Healthcare — 30-minute tachycardia episode (runs in 3 real minutes at 10×)
python run.py \
  --persona personas/cardiac_patient.yaml \
  --scenario scenarios/health/tachycardia_episode.yaml

# With demo API (browser control at http://localhost:8000)
python run.py \
  --persona personas/welder_factory.yaml \
  --scenario scenarios/worker/fatigue_escalation.yaml \
  --demo

# Override compression at the CLI (ignores scenario YAML value)
python run.py \
  --persona personas/welder_factory.yaml \
  --scenario scenarios/worker/fatigue_escalation.yaml \
  --compression 1 \
  --demo
```

### Start the infrastructure stack (dev environment)

```bash
docker compose -f docker-compose.dev.yml up -d
```

This starts:
- **Mosquitto** MQTT broker on port 1883
- **InfluxDB** on port 8086 (org: `iot_org`, bucket: `iot_poc`, token: `my-token`)
- **Telegraf** subscribing to MQTT and writing to InfluxDB
- **Grafana** on port 3000 (admin/admin) with dashboards pre-provisioned

The simulator runs on the host (`localhost`) and connects to the Docker services. Use the defaults in `.env.example` or set environment variables.

### Environment setup

```bash
cp .env.example .env
# Edit .env if your broker or InfluxDB is not on localhost
source .env
python run.py --persona personas/welder_factory.yaml \
              --scenario scenarios/worker/fatigue_escalation.yaml \
              --demo
```

---

## 16. Docker Integration

The simulator is designed to be embedded in a POC project's docker-compose stack as a service alongside the infrastructure it connects to.

### How it works

The simulator image does not own MQTT, InfluxDB, or Grafana. The POC project's compose file starts those. The simulator is added as one more service that connects to them.

### Dockerfile design

```
Base:       python:3.11-slim
WORKDIR:    /app
ENTRYPOINT: ["python", "run.py"]
CMD:        []   # persona/scenario passed by the POC compose file
Port:       8000 (demo API)
```

All connection details come in as environment variables — nothing is hardcoded in the image.

### Adding the simulator to a POC compose file

```yaml
# Inside the Worker Safety POC's docker-compose.yml
services:
  simulator:
    build: ./vitals_simulator         # this repo as a subdirectory
    ports:
      - "8000:8000"
    environment:
      MQTT_HOST: mosquitto            # service name in the POC's Docker network
      MQTT_PORT: 1883
      INFLUXDB_URL: http://influxdb:8086
      INFLUXDB_TOKEN: ${INFLUXDB_TOKEN}
      INFLUXDB_ORG: ${INFLUXDB_ORG}
      INFLUXDB_BUCKET: ${INFLUXDB_BUCKET}
      DEMO_API_PORT: 8000
    command: >
      --persona personas/welder_factory.yaml
      --scenario scenarios/worker/fatigue_escalation.yaml
      --compression 60
      --demo
    depends_on:
      - mosquitto
      - influxdb
```

For the healthcare POC, change `command` to point at the cardiac patient persona and scenario. No rebuild needed.

### Mounting custom scenarios

To use custom personas or scenarios without rebuilding the image:

```yaml
    volumes:
      - ./my_custom_scenarios:/app/scenarios/custom
      - ./my_custom_personas:/app/personas
```

---

## 17. Environment Variables Reference

All environment variables override their equivalent values in the YAML config files.

| Variable | Overrides | Default | Description |
|---|---|---|---|
| `MQTT_HOST` | `mqtt_config.yaml broker.host` | `localhost` | MQTT broker hostname |
| `MQTT_PORT` | `mqtt_config.yaml broker.port` | `1883` | MQTT broker port |
| `MQTT_USERNAME` | `mqtt_config.yaml broker.username` | _(empty)_ | MQTT auth username |
| `MQTT_PASSWORD` | `mqtt_config.yaml broker.password` | _(empty)_ | MQTT auth password |
| `MQTT_TOPIC_PREFIX` | `mqtt_config.yaml topic.prefix` | `iots` | Topic prefix |
| `INFLUXDB_URL` | — | — | InfluxDB URL (for future direct write integration) |
| `INFLUXDB_TOKEN` | — | — | InfluxDB auth token |
| `INFLUXDB_ORG` | — | — | InfluxDB organisation |
| `INFLUXDB_BUCKET` | — | — | InfluxDB bucket |
| `LOG_LEVEL` | — | `INFO` | Python logging level: `DEBUG`, `INFO`, `WARNING`, `ERROR` |
| `DEMO_API_PORT` | — | `8000` | Port for the FastAPI demo control UI |

---

## 18. Error Handling Rules

These rules are enforced throughout the codebase. Follow them when adding code.

| Situation | Behaviour |
|---|---|
| YAML loading error (missing field, wrong type) | Raise `ConfigurationError` with the file path and missing field name. Never silently use defaults. |
| Sensor tick error (signal/noise exception) | Log a warning, return the last known good value. Never crash the engine on a single bad tick. Backoff with `sensor_error_backoff_seconds`. |
| MQTT publish error (broker disconnect) | Log the error, buffer the point, retry next tick. Reconnect with exponential backoff. Never crash on broker disconnect. |
| Demo API error | Return HTTP 400/404/500 with a plain English message. Never expose Python stack traces to the browser. |
| Unexpected `poc_type` or sensor name | Raise `ConfigurationError` at load time. The engine should never encounter an unknown type mid-run. |
| SIGTERM / SIGINT | Graceful shutdown within 5 seconds. Stop all sensor tasks, disconnect MQTT, exit cleanly. |
