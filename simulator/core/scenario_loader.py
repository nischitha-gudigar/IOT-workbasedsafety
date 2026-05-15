"""
simulator/core/scenario_loader.py
----------------------------------
Loads and validates scenario YAML files into typed dataclasses.

Validates:
  - All required fields present
  - Phases are contiguous (end of one == start of next)
  - Phases are non-overlapping
  - Compression factor is positive
  - Event times fall within total_duration_minutes
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from simulator.core.persona_loader import ConfigurationError
from simulator.utils.logger import get_logger

logger = get_logger(__name__)


# ---------------------------------------------------------------------------
# Dataclasses
# ---------------------------------------------------------------------------

@dataclass
class SensorPhaseConfig:
    """Sensor value config for one phase."""
    min_value: float
    max_value: float
    behavior:  str      # oscillating_stable | monotonic_rising | etc.


@dataclass
class Phase:
    """One named phase of a scenario."""
    name:          str
    start_minute:  float
    end_minute:    float
    sensors:       dict[str, SensorPhaseConfig]   # sensor_name → config
    noise_intensity: str                           # low | medium | high
    activity:      str | None                      # optional: walking | standing | etc.

    @property
    def duration_minutes(self) -> float:
        """Duration of this phase in simulated minutes."""
        return self.end_minute - self.start_minute


@dataclass
class ScenarioEvent:
    """A scripted event that fires at a specific simulated minute."""
    at_minute:   float
    event_type:  str              # posture_spike | fall | tachycardia_onset | etc.
    description: str
    overrides:   dict[str, float]  # sensor_name → override value
    duration_seconds: float        # real seconds the override lasts
    # For fault events:
    fault_type:  str | None = None
    sensor:      str | None = None
    fault_factor: float = 3.0


@dataclass
class WorkerContext:
    """Worker shift context — only present for worker_safety scenarios."""
    shift_day:            int
    consecutive_days:     int
    recent_medical_leave: bool


@dataclass
class Scenario:
    """Fully parsed and validated scenario."""
    schema_version:         str
    scenario_id:            str
    description:            str
    persona:                str        # persona_id this scenario references
    poc_type:               str
    total_duration_minutes: float
    compression:            float
    phases:                 list[Phase]
    events:                 list[ScenarioEvent]
    worker_context:         WorkerContext | None = None


# ---------------------------------------------------------------------------
# Loader
# ---------------------------------------------------------------------------

_REQUIRED_FIELDS = (
    "schema_version", "scenario_id", "description", "persona",
    "poc_type", "total_duration_minutes", "compression", "phases",
)

_VALID_BEHAVIORS = {
    "oscillating_stable", "monotonic_rising", "monotonic_falling",
    "mean_reverting", "step",
}

_VALID_NOISE_LEVELS = {"low", "medium", "high"}


def load_scenario(path: Path | str) -> Scenario:
    """
    Load and validate a scenario YAML file.

    Args:
        path: Path to the scenario YAML file.

    Returns:
        Validated Scenario dataclass.

    Raises:
        ConfigurationError: If anything is missing or invalid.
    """
    path = Path(path)

    if not path.exists():
        raise ConfigurationError(f"Scenario file not found: {path}")

    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise ConfigurationError(f"YAML parse error in {path}: {exc}") from exc

    if not isinstance(raw, dict):
        raise ConfigurationError(f"Scenario file must be a YAML mapping: {path}")

    for field_name in _REQUIRED_FIELDS:
        if field_name not in raw:
            raise ConfigurationError(
                f"Missing required field '{field_name}' in scenario: {path}"
            )

    compression = float(raw["compression"])
    if compression <= 0:
        raise ConfigurationError(
            f"'compression' must be > 0 in {path}, got {compression}"
        )

    total = float(raw["total_duration_minutes"])
    phases = _parse_phases(raw["phases"], total, path)
    events = _parse_events(raw.get("events", []), total, path)
    worker_context = _parse_worker_context(raw.get("worker_context"), path)

    scenario = Scenario(
        schema_version=         raw["schema_version"],
        scenario_id=            raw["scenario_id"],
        description=            raw["description"],
        persona=                raw["persona"],
        poc_type=               raw["poc_type"],
        total_duration_minutes= total,
        compression=            compression,
        phases=                 phases,
        events=                 events,
        worker_context=         worker_context,
    )

    logger.info(
        "Scenario loaded",
        extra={
            "event":      "scenario_loaded",
            "scenario":   scenario.scenario_id,
            "persona":    scenario.persona,
            "phases":     len(scenario.phases),
            "events":     len(scenario.events),
            "compression": scenario.compression,
            "duration_minutes": scenario.total_duration_minutes,
        },
    )
    return scenario


# ---------------------------------------------------------------------------
# Parsers
# ---------------------------------------------------------------------------

def _parse_phases(raw: Any, total_minutes: float, path: Path) -> list[Phase]:
    """Parse and validate the phases list."""
    if not isinstance(raw, list) or not raw:
        raise ConfigurationError(f"'phases' must be a non-empty list in {path}")

    phases: list[Phase] = []
    for i, item in enumerate(raw):
        for required in ("name", "start_minute", "end_minute", "sensors", "noise_intensity"):
            if required not in item:
                raise ConfigurationError(
                    f"Missing '{required}' in phases[{i}] in {path}"
                )

        noise = item["noise_intensity"]
        if noise not in _VALID_NOISE_LEVELS:
            raise ConfigurationError(
                f"Invalid noise_intensity '{noise}' in phases[{i}] in {path}. "
                f"Must be one of: {_VALID_NOISE_LEVELS}"
            )

        sensors = _parse_phase_sensors(item["sensors"], i, path)

        phases.append(Phase(
            name=            item["name"],
            start_minute=    float(item["start_minute"]),
            end_minute=      float(item["end_minute"]),
            sensors=         sensors,
            noise_intensity= noise,
            activity=        item.get("activity"),
        ))

    # Validate contiguity
    for i in range(len(phases) - 1):
        curr = phases[i]
        nxt  = phases[i + 1]
        if curr.end_minute != nxt.start_minute:
            raise ConfigurationError(
                f"Phase gap/overlap: '{curr.name}' ends at {curr.end_minute} "
                f"but '{nxt.name}' starts at {nxt.start_minute} in {path}"
            )

    # Validate total coverage
    if phases[0].start_minute != 0:
        raise ConfigurationError(
            f"First phase must start at minute 0 in {path}, "
            f"got {phases[0].start_minute}"
        )
    if phases[-1].end_minute != total_minutes:
        raise ConfigurationError(
            f"Last phase must end at total_duration_minutes ({total_minutes}) in {path}, "
            f"got {phases[-1].end_minute}"
        )

    return phases


def _parse_phase_sensors(
    raw: Any,
    phase_index: int,
    path: Path,
) -> dict[str, SensorPhaseConfig]:
    """Parse sensor configs within a phase."""
    if not isinstance(raw, dict):
        raise ConfigurationError(
            f"'sensors' must be a mapping in phases[{phase_index}] in {path}"
        )
    result: dict[str, SensorPhaseConfig] = {}
    for sensor_name, cfg in raw.items():
        for required in ("min", "max", "behavior"):
            if required not in cfg:
                raise ConfigurationError(
                    f"Missing '{required}' in phases[{phase_index}].sensors.{sensor_name} in {path}"
                )
        behavior = cfg["behavior"]
        if behavior not in _VALID_BEHAVIORS:
            raise ConfigurationError(
                f"Unknown behavior '{behavior}' for sensor '{sensor_name}' "
                f"in phases[{phase_index}] in {path}. "
                f"Valid options: {_VALID_BEHAVIORS}"
            )
        result[sensor_name] = SensorPhaseConfig(
            min_value= float(cfg["min"]),
            max_value= float(cfg["max"]),
            behavior=  behavior,
        )
    return result


def _parse_events(
    raw: Any,
    total_minutes: float,
    path: Path,
) -> list[ScenarioEvent]:
    """Parse and validate the events list."""
    if not isinstance(raw, list):
        raise ConfigurationError(f"'events' must be a list in {path}")

    events: list[ScenarioEvent] = []
    for i, item in enumerate(raw):
        for required in ("at_minute", "type", "description", "duration_seconds"):
            if required not in item:
                raise ConfigurationError(
                    f"Missing '{required}' in events[{i}] in {path}"
                )
        at = float(item["at_minute"])
        if at < 0 or at > total_minutes:
            raise ConfigurationError(
                f"events[{i}].at_minute={at} is outside [0, {total_minutes}] in {path}"
            )

        events.append(ScenarioEvent(
            at_minute=        at,
            event_type=       item["type"],
            description=      item["description"],
            overrides=        {k: float(v) for k, v in item.get("overrides", {}).items()},
            duration_seconds= float(item["duration_seconds"]),
            fault_type=       item.get("fault_type"),
            sensor=           item.get("sensor"),
            fault_factor=     float(item.get("fault_factor", 3.0)),
        ))

    # Sort events by time so the engine can iterate in order
    events.sort(key=lambda e: e.at_minute)
    return events


def _parse_worker_context(raw: Any, path: Path) -> WorkerContext | None:
    """Parse optional worker_context block."""
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise ConfigurationError(f"'worker_context' must be a mapping in {path}")
    for required in ("shift_day", "consecutive_days", "recent_medical_leave"):
        if required not in raw:
            raise ConfigurationError(
                f"Missing '{required}' in worker_context in {path}"
            )
    return WorkerContext(
        shift_day=            int(raw["shift_day"]),
        consecutive_days=     int(raw["consecutive_days"]),
        recent_medical_leave= bool(raw["recent_medical_leave"]),
    )
