"""
simulator/core/scenario_engine.py
-----------------------------------
Main orchestrator for the Vitals Simulator.

Responsibilities:
  1. Owns the simulation clock
  2. Advances through scenario phases based on elapsed simulated time
  3. Fires scripted events at the correct simulated minute
  4. Constructs and ticks all sensor generators
  5. Hands readings to the transport layer (MQTT publisher)
  6. Accepts manual event triggers from the demo API
  7. Handles graceful shutdown on SIGTERM

Architecture:
  - One asyncio task per sensor (independent sampling intervals)
  - One heartbeat task advances the clock and checks phase transitions
  - Event queue for manual triggers from the demo API
  - All state is internal — no global variables

The engine does not know about MQTT topics or payloads.
It passes SensorReading objects to a publisher callback.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Awaitable

from simulator.core.condition_mapper import ConditionMapper
from simulator.core.persona_loader import Persona, load_persona, ConfigurationError
from simulator.core.scenario_loader import (
    Phase, Scenario, ScenarioEvent, load_scenario,
)
from simulator.core.worker_context import WorkerContextProcessor
from simulator.engine.correlation import CorrelationEngine
from simulator.engine.fault import FaultController, FaultEvent
from simulator.sensors.base import BaseSensor, SensorReading
from simulator.sensors.heart_rate_sensor import HeartRateSensor
from simulator.sensors.hrv_sensor import HRVSensor
from simulator.sensors.imu_sensor import IMUSensor
from simulator.sensors.spo2_sensor import SpO2Sensor
from simulator.sensors.temperature_sensor import TemperatureSensor
from simulator.utils.logger import get_logger
from simulator.utils.time_utils import TimeCompressor

logger = get_logger(__name__)

# Type alias for the publish callback
PublishCallback = Callable[[SensorReading, str, str, str], Awaitable[None]]


# ---------------------------------------------------------------------------
# Engine status snapshot
# ---------------------------------------------------------------------------

@dataclass
class EngineStatus:
    """Current state of the engine — returned by get_status()."""
    running:              bool
    scenario_id:          str
    persona_id:           str
    current_phase:        str
    elapsed_sim_minutes:  float
    total_sim_minutes:    float
    compression:          float
    events_fired:         list[str]
    sequence_number:      int
    available_events:     list[dict]


# ---------------------------------------------------------------------------
# Scenario engine
# ---------------------------------------------------------------------------

class ScenarioEngine:
    """
    Main orchestrator for the Vitals Simulator.

    Usage:
        engine = ScenarioEngine(sim_config, mqtt_config)
        engine.load(persona_path, scenario_path)
        await engine.start(publish_callback)
        await engine.stop()
    """

    def __init__(
        self,
        sim_config:  dict[str, Any],
        mqtt_config: dict[str, Any],
    ) -> None:
        self._sim_config   = sim_config
        self._mqtt_config  = mqtt_config

        self._persona:   Persona | None   = None
        self._scenario:  Scenario | None  = None
        self._compressor: TimeCompressor | None = None
        self._mapper:    ConditionMapper | None = None
        self._correlation = CorrelationEngine()
        self._fault_ctrl  = FaultController()
        self._threshold_multiplier: float = 1.0

        # Runtime state
        self._running:            bool  = False
        self._elapsed_sim_seconds: float = 0.0
        self._current_phase_idx:  int   = 0
        self._phase_entry_sim_seconds: float = 0.0
        self._sequence_number:    int   = 0
        self._events_fired:       list[str] = []
        self._pending_events:     list[ScenarioEvent] = []
        self._active_overrides:   dict[str, tuple[float, float]] = {}  # sensor → (value, end_real_time)

        # Sensor generators (built at load time)
        self._sensors: list[BaseSensor] = []

        # Task handles
        self._tasks: list[asyncio.Task] = []

        # Publish callback — stored at start() so reload() can reuse it
        self._publish_callback: PublishCallback | None = None

        # Event queue for manual triggers from demo API
        self._manual_event_queue: asyncio.Queue[tuple[str, float | None]] = asyncio.Queue()

        # Engine-level config
        engine_cfg = sim_config.get("engine", {})
        self._heartbeat_interval: float = float(
            engine_cfg.get("heartbeat_interval_seconds", 0.1)
        )
        self._transition_pct: float = float(
            engine_cfg.get("phase_transition_pct", 0.10)
        )
        self._sensor_error_backoff: float = float(
            engine_cfg.get("sensor_error_backoff_seconds", 2.0)
        )
        self._sensor_max_backoff: float = float(
            engine_cfg.get("sensor_max_backoff_seconds", 30.0)
        )

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def load(self, persona_path: Path | str, scenario_path: Path | str) -> None:
        """
        Load and validate persona and scenario YAMLs.

        Builds all sensor generators. Must be called before start().

        Raises:
            ConfigurationError: If either YAML is invalid.
        """
        self._persona   = load_persona(persona_path)
        self._scenario  = load_scenario(scenario_path)

        if self._scenario.persona != self._persona.persona_id:
            raise ConfigurationError(
                f"Scenario references persona '{self._scenario.persona}' "
                f"but loaded persona is '{self._persona.persona_id}'"
            )

        # Time compression from scenario YAML
        self._compressor = TimeCompressor(self._scenario.compression)

        # Condition mapper — bridges YAML and engine
        self._mapper = ConditionMapper(self._persona, self._sim_config)

        # Worker context threshold multiplier
        if self._scenario.worker_context is not None:
            processor = WorkerContextProcessor(self._sim_config)
            self._threshold_multiplier = processor.compute_multiplier(
                self._scenario.worker_context
            )
        else:
            self._threshold_multiplier = 1.0

        # Load correlation rules from persona
        self._correlation = CorrelationEngine()
        if self._persona.correlations:
            self._correlation.load_rules(
                [
                    {
                        "leader_sensor":    r.leader_sensor,
                        "follower_sensor":  r.follower_sensor,
                        "coupling_strength": r.coupling_strength,
                        "lag_seconds":      r.lag_seconds,
                    }
                    for r in self._persona.correlations
                ]
            )

        # Reset state
        self._fault_ctrl  = FaultController()
        self._elapsed_sim_seconds   = 0.0
        self._current_phase_idx     = 0
        self._phase_entry_sim_seconds = 0.0
        self._sequence_number       = 0
        self._events_fired          = []
        self._pending_events        = list(self._scenario.events)
        self._active_overrides      = {}

        # Build sensors for first phase
        self._sensors = self._build_sensors(self._scenario.phases[0])
        self._update_correlation_ranges(self._scenario.phases[0])

        logger.info(
            "ScenarioEngine loaded",
            extra={
                "event":      "engine_loaded",
                "persona":    self._persona.persona_id,
                "scenario":   self._scenario.scenario_id,
                "phases":     len(self._scenario.phases),
                "compression": self._scenario.compression,
                "sensors":    [s.sensor_name for s in self._sensors],
            },
        )

    async def start(self, publish_callback: PublishCallback) -> None:
        """
        Begin the simulation loop.

        Blocks until stop() is called or the scenario completes.

        Args:
            publish_callback: Async function called with each SensorReading.
                              Signature: (reading, persona_id, poc_type, device_id) → None
        """
        if self._scenario is None or self._persona is None:
            raise RuntimeError("Call load() before start()")

        self._publish_callback = publish_callback
        self._spawn_tasks()

        logger.info(
            "ScenarioEngine starting",
            extra={
                "event":    "engine_start",
                "scenario": self._scenario.scenario_id,
                "persona":  self._persona.persona_id,
            },
        )

        try:
            await asyncio.gather(*self._tasks)
        except asyncio.CancelledError:
            logger.info("Engine tasks cancelled", extra={"event": "engine_cancelled"})
        finally:
            self._running = False

    async def reload(self, persona_path: Path | str, scenario_path: Path | str) -> None:
        """
        Stop the running simulation, load a new scenario, and restart.

        Safe to call while the engine is running (from the demo API).
        Reuses the publish callback from the original start() call.
        """
        if self._publish_callback is None:
            raise RuntimeError("Engine has never been started — call start() first")
        if self._running:
            await self.stop()
        self.load(persona_path, scenario_path)
        self._spawn_tasks()
        logger.info(
            "ScenarioEngine reloaded",
            extra={
                "event":    "engine_reloaded",
                "scenario": self._scenario.scenario_id,
                "persona":  self._persona.persona_id,
            },
        )

    def _spawn_tasks(self) -> None:
        """Create and register all sensor/heartbeat/event asyncio tasks."""
        self._running = True
        sensor_tasks = [
            asyncio.create_task(
                self._sensor_loop(sensor),
                name=f"sensor_{sensor.sensor_name}",
            )
            for sensor in self._sensors
        ]
        heartbeat_task = asyncio.create_task(
            self._heartbeat_loop(), name="heartbeat"
        )
        event_task = asyncio.create_task(
            self._manual_event_loop(), name="manual_events"
        )
        self._tasks = sensor_tasks + [heartbeat_task, event_task]

    async def stop(self) -> None:
        """Gracefully stop the simulation."""
        self._running = False
        tasks = [t for t in self._tasks if not t.done()]
        self._tasks = []
        for task in tasks:
            task.cancel()
        if tasks:
            # asyncio.wait() monitors tasks without registering as a parent waiter,
            # avoiding the circular-cancellation RecursionError that asyncio.gather()
            # causes when the same task objects are already gathered in start().
            await asyncio.wait(tasks, timeout=5.0)
        logger.info("ScenarioEngine stopped", extra={"event": "engine_stopped"})

    def trigger_event(self, event_type: str, duration_seconds: float | None = None) -> bool:
        """
        Manually fire a named event immediately (from demo API).

        Returns True if the event type exists in the current scenario,
        False if not found (so the API can return a 404).
        duration_seconds overrides the event's YAML-defined duration when provided.
        """
        if self._scenario is None:
            return False
        matched = any(e.event_type == event_type for e in self._scenario.events)
        if not matched:
            return False
        self._manual_event_queue.put_nowait((event_type, duration_seconds))
        return True

    def set_compression(self, compression: float) -> None:
        """Update time compression at runtime (from demo API speed control)."""
        if self._compressor:
            self._compressor.compression_factor = compression
            logger.info(
                "Compression updated",
                extra={"event": "compression_updated", "compression": compression},
            )

    def get_status(self) -> EngineStatus:
        """Return a snapshot of current engine state for the demo API."""
        phase_name = ""
        if self._scenario and self._current_phase_idx < len(self._scenario.phases):
            phase_name = self._scenario.phases[self._current_phase_idx].name

        available = [
            {"type": e.event_type, "description": e.description}
            for e in self._scenario.events
        ] if self._scenario else []

        return EngineStatus(
            running=             self._running,
            scenario_id=         self._scenario.scenario_id if self._scenario else "",
            persona_id=          self._persona.persona_id if self._persona else "",
            current_phase=       phase_name,
            elapsed_sim_minutes= self._elapsed_sim_seconds / 60.0,
            total_sim_minutes=   self._scenario.total_duration_minutes if self._scenario else 0.0,
            compression=         self._compressor.compression_factor if self._compressor else 1.0,
            events_fired=        list(self._events_fired),
            sequence_number=     self._sequence_number,
            available_events=    available,
        )

    # ------------------------------------------------------------------
    # Heartbeat loop — advances the clock
    # ------------------------------------------------------------------

    async def _heartbeat_loop(self) -> None:
        """Advance simulated time and fire scheduled events."""
        last_tick = asyncio.get_event_loop().time()

        while self._running:
            try:
                await asyncio.sleep(self._heartbeat_interval)
                now = asyncio.get_event_loop().time()
                real_elapsed = now - last_tick
                last_tick = now

                sim_delta = self._compressor.real_to_sim_seconds(real_elapsed)
                self._elapsed_sim_seconds += sim_delta

                # Check scenario completion
                total_sim_seconds = self._scenario.total_duration_minutes * 60.0
                if self._elapsed_sim_seconds >= total_sim_seconds:
                    logger.info(
                        "Scenario complete",
                        extra={
                            "event":    "scenario_complete",
                            "scenario": self._scenario.scenario_id,
                        },
                    )
                    await self.stop()
                    return

                # Check phase transitions
                self._check_phase_transition()

                # Fire scheduled events
                self._fire_due_events()

                # Expire active overrides
                real_now = time.monotonic()
                self._active_overrides = {
                    k: v for k, v in self._active_overrides.items()
                    if real_now < v[1]
                }

            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.error(
                    "Heartbeat loop error",
                    extra={"event": "heartbeat_error", "error": str(exc)},
                )

    # ------------------------------------------------------------------
    # Sensor loop — one per sensor generator
    # ------------------------------------------------------------------

    async def _sensor_loop(self, sensor: BaseSensor) -> None:
        """Run one sensor's tick loop at its configured sampling interval."""
        backoff = self._sensor_error_backoff

        while self._running:
            try:
                await self._tick_sensor(sensor)
                backoff = self._sensor_error_backoff

                sleep_secs = self._compressor.sampling_interval_real_seconds(
                    sensor.sampling_interval_ms
                )
                await asyncio.sleep(sleep_secs)

            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.error(
                    "Sensor tick error",
                    extra={
                        "event":   "sensor_tick_error",
                        "sensor":  sensor.sensor_name,
                        "error":   str(exc),
                        "backoff": backoff,
                    },
                )
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, self._sensor_max_backoff)

    async def _tick_sensor(self, sensor: BaseSensor) -> None:
        """Execute one tick for a sensor and publish the reading."""
        phase = self._scenario.phases[self._current_phase_idx]
        phase_progress = self._compressor.phase_progress(
            (self._elapsed_sim_seconds - self._phase_entry_sim_seconds) / 60.0,
            phase.duration_minutes,
        )

        # Check for active override for this sensor
        override = self._active_overrides.get(sensor.sensor_name)
        override_value = override[0] if override else None

        # IMU sensor needs special handling (two param sets + accel override)
        if isinstance(sensor, IMUSensor):
            accel_override = None
            accel_ov = self._active_overrides.get("accel_magnitude")
            if accel_ov:
                accel_override = accel_ov[0]
            reading = sensor.tick(
                phase_name=       phase.name,
                sim_time_seconds= self._elapsed_sim_seconds,
                phase_progress=   phase_progress,
                override_value=   override_value,
                override_accel=   accel_override,
            )
        else:
            reading = sensor.tick(
                phase_name=       phase.name,
                sim_time_seconds= self._elapsed_sim_seconds,
                phase_progress=   phase_progress,
                override_value=   override_value,
            )

        self._sequence_number += 1

        device_id = f"sim-{self._persona.persona_id}-001"
        await self._publish_callback(
            reading,
            self._persona.persona_id,
            self._persona.poc_type,
            device_id,
        )

        # Publish derived streams from IMU
        if isinstance(sensor, IMUSensor) and reading.extra:
            for extra_sensor, extra_value in reading.extra.items():
                extra_reading = SensorReading(
                    sensor_name=  extra_sensor,
                    value=        extra_value,
                    unit=         "g",
                    condition=    reading.condition,
                    quality=      reading.quality,
                    fault_active= reading.fault_active,
                    phase=        reading.phase,
                    extra=        {},
                )
                await self._publish_callback(
                    extra_reading,
                    self._persona.persona_id,
                    self._persona.poc_type,
                    device_id,
                )

    # ------------------------------------------------------------------
    # Phase management
    # ------------------------------------------------------------------

    def _check_phase_transition(self) -> None:
        """Advance to the next phase if the current one has completed."""
        if not self._scenario:
            return

        phases = self._scenario.phases
        current = phases[self._current_phase_idx]
        elapsed_in_phase_minutes = (
            self._elapsed_sim_seconds - self._phase_entry_sim_seconds
        ) / 60.0

        if elapsed_in_phase_minutes < current.duration_minutes:
            return

        next_idx = self._current_phase_idx + 1
        if next_idx >= len(phases):
            return  # Engine handles scenario end in heartbeat loop

        self._current_phase_idx     = next_idx
        self._phase_entry_sim_seconds = self._elapsed_sim_seconds
        next_phase = phases[next_idx]

        # Update sensor params for new phase
        new_params = self._mapper.resolve_phase_sensors(next_phase)
        for sensor in self._sensors:
            params = new_params.get(sensor.sensor_name)
            if params:
                sensor.update_params(params)
            # IMU also needs accel params
            if isinstance(sensor, IMUSensor):
                accel_params = new_params.get("accel_magnitude")
                if accel_params:
                    sensor.update_accel_params(accel_params)

        self._update_correlation_ranges(next_phase)

        logger.info(
            "Phase transition",
            extra={
                "event":      "phase_transition",
                "from_phase": current.name,
                "to_phase":   next_phase.name,
                "sim_minute": round(self._elapsed_sim_seconds / 60.0, 1),
            },
        )

    def _update_correlation_ranges(self, phase: Phase) -> None:
        """Update correlation engine ranges from the current phase config."""
        params = self._mapper.resolve_phase_sensors(phase)
        ranges = {
            name: (p.min_value, p.max_value)
            for name, p in params.items()
        }
        self._correlation.update_phase_ranges(ranges)

    # ------------------------------------------------------------------
    # Event firing
    # ------------------------------------------------------------------

    def _fire_due_events(self) -> None:
        """Fire any scenario events whose at_minute has been reached."""
        elapsed_minutes = self._elapsed_sim_seconds / 60.0
        remaining = []
        for event in self._pending_events:
            if elapsed_minutes >= event.at_minute:
                self._apply_event(event)
            else:
                remaining.append(event)
        self._pending_events = remaining

    def _apply_event(self, event: ScenarioEvent, duration_override: float | None = None) -> None:
        """Apply a scenario event — set overrides or inject faults.

        duration_override replaces event.duration_seconds when provided (manual trigger from UI).
        """
        duration = duration_override if duration_override is not None else event.duration_seconds
        logger.info(
            "Event fired",
            extra={
                "event":       "scenario_event_fired",
                "event_type":  event.event_type,
                "description": event.description,
                "at_minute":   event.at_minute,
                "duration":    duration,
            },
        )
        self._events_fired.append(event.event_type)

        if event.event_type == "sensor_fault" and event.fault_type and event.sensor:
            # Inject a fault into the fault controller
            self._fault_ctrl.inject(FaultEvent(
                sensor_name=     event.sensor,
                fault_type=      event.fault_type,
                end_sim_seconds= self._elapsed_sim_seconds + duration,
                fault_factor=    event.fault_factor,
            ))
        else:
            # Value overrides — duration is real wall-clock seconds so
            # the override is visible in Grafana regardless of compression setting
            end_real = time.monotonic() + duration
            for sensor_name, value in event.overrides.items():
                self._active_overrides[sensor_name] = (value, end_real)

    async def _manual_event_loop(self) -> None:
        """Process manual event triggers from the demo API."""
        while self._running:
            try:
                event_type, duration_override = await asyncio.wait_for(
                    self._manual_event_queue.get(), timeout=1.0
                )
                # Find the first matching event in the scenario
                matched = next(
                    (e for e in self._scenario.events if e.event_type == event_type),
                    None,
                )
                if matched:
                    self._apply_event(matched, duration_override)
                    logger.info(
                        "Manual event triggered",
                        extra={"event": "manual_trigger", "event_type": event_type},
                    )
                else:
                    logger.warning(
                        "Manual event type not found in scenario",
                        extra={"event": "manual_trigger_not_found", "event_type": event_type},
                    )
            except asyncio.TimeoutError:
                continue
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.error(
                    "Manual event loop error",
                    extra={"event": "manual_event_error", "error": str(exc)},
                )

    # ------------------------------------------------------------------
    # Sensor construction
    # ------------------------------------------------------------------

    def _build_sensors(self, initial_phase: Phase) -> list[BaseSensor]:
        """Build all sensor generator instances for the loaded persona."""
        params_map = self._mapper.resolve_phase_sensors(initial_phase)
        sensors: list[BaseSensor] = []

        persona_sensors = set(self._persona.baseline.keys())

        for sensor_name in persona_sensors:
            params = params_map.get(sensor_name)
            if params is None:
                # Sensor exists in persona but not in this phase — use baseline
                params_map2 = self._mapper.resolve_baseline_sensors()
                params = params_map2.get(sensor_name)
                if params is None:
                    continue

            if sensor_name == "posture_angle":
                accel_params = params_map.get("accel_magnitude")
                if accel_params is None:
                    accel_params2 = self._mapper.resolve_baseline_sensors()
                    accel_params = accel_params2.get("accel_magnitude")
                if accel_params:
                    sensors.append(IMUSensor(
                        posture_params=       params,
                        accel_params=         accel_params,
                        correlation=          self._correlation,
                        fault_ctrl=           self._fault_ctrl,
                        mapper=               self._mapper,
                        threshold_multiplier= self._threshold_multiplier,
                    ))
            elif sensor_name == "accel_magnitude":
                continue  # handled by IMUSensor
            elif sensor_name in ("accel_x", "accel_y", "accel_z"):
                continue  # derived outputs of IMUSensor
            elif sensor_name == "heart_rate":
                sensors.append(HeartRateSensor(
                    params=               params,
                    correlation=          self._correlation,
                    fault_ctrl=           self._fault_ctrl,
                    mapper=               self._mapper,
                    threshold_multiplier= self._threshold_multiplier,
                ))
            elif sensor_name == "skin_temperature":
                sensors.append(TemperatureSensor(
                    params=               params,
                    correlation=          self._correlation,
                    fault_ctrl=           self._fault_ctrl,
                    mapper=               self._mapper,
                    threshold_multiplier= self._threshold_multiplier,
                ))
            elif sensor_name == "spo2":
                sensors.append(SpO2Sensor(
                    params=               params,
                    correlation=          self._correlation,
                    fault_ctrl=           self._fault_ctrl,
                    mapper=               self._mapper,
                    threshold_multiplier= self._threshold_multiplier,
                ))
            elif sensor_name == "heart_rate_variability":
                sensors.append(HRVSensor(
                    params=               params,
                    correlation=          self._correlation,
                    fault_ctrl=           self._fault_ctrl,
                    mapper=               self._mapper,
                    threshold_multiplier= self._threshold_multiplier,
                ))

        return sensors
