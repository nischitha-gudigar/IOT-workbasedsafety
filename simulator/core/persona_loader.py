"""
simulator/core/persona_loader.py
---------------------------------
Loads and validates persona YAML files into typed dataclasses.

Raises ConfigurationError with the file path and the missing/invalid
field name if anything is wrong. Never lets a malformed persona reach
the engine.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from simulator.utils.logger import get_logger

logger = get_logger(__name__)


class ConfigurationError(Exception):
    """Raised when a YAML configuration file is missing or invalid."""
    pass


# ---------------------------------------------------------------------------
# Dataclasses
# ---------------------------------------------------------------------------

@dataclass
class SensorBaseline:
    """Baseline range and sampling config for one sensor."""
    sensor_name:          str
    min_value:            float
    max_value:            float
    unit:                 str
    sampling_interval_ms: int


@dataclass
class CorrelationRule:
    """A single leader→follower coupling rule from persona YAML."""
    leader_sensor:     str
    follower_sensor:   str
    coupling_strength: float
    lag_seconds:       float


@dataclass
class Persona:
    """Fully parsed and validated persona."""
    schema_version: str
    persona_id:     str
    description:    str
    poc_type:       str                          # worker_safety | healthcare
    baseline:       dict[str, SensorBaseline]   # sensor_name → SensorBaseline
    thresholds:     dict[str, float]            # threshold_name → value
    noise_profile:  dict[str, str]              # sensor_name → noise model name
    correlations:   list[CorrelationRule]


# ---------------------------------------------------------------------------
# Loader
# ---------------------------------------------------------------------------

_REQUIRED_FIELDS = ("schema_version", "persona_id", "description", "poc_type",
                    "baseline", "thresholds", "noise_profile")

_VALID_POC_TYPES = {"worker_safety", "healthcare"}


def load_persona(path: Path | str) -> Persona:
    """
    Load and validate a persona YAML file.

    Args:
        path: Absolute or relative path to the persona YAML file.

    Returns:
        Validated Persona dataclass.

    Raises:
        ConfigurationError: If the file is missing, unreadable, or invalid.
    """
    path = Path(path)

    if not path.exists():
        raise ConfigurationError(f"Persona file not found: {path}")

    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise ConfigurationError(f"YAML parse error in {path}: {exc}") from exc

    if not isinstance(raw, dict):
        raise ConfigurationError(f"Persona file must be a YAML mapping: {path}")

    # Check all required top-level fields
    for field_name in _REQUIRED_FIELDS:
        if field_name not in raw:
            raise ConfigurationError(
                f"Missing required field '{field_name}' in persona: {path}"
            )

    if raw["poc_type"] not in _VALID_POC_TYPES:
        raise ConfigurationError(
            f"Invalid poc_type '{raw['poc_type']}' in {path}. "
            f"Must be one of: {_VALID_POC_TYPES}"
        )

    baseline   = _parse_baseline(raw["baseline"], path)
    thresholds = _parse_thresholds(raw["thresholds"], path)
    noise      = _parse_noise_profile(raw["noise_profile"], baseline, path)
    correlations = _parse_correlations(raw.get("correlations", []), path)

    persona = Persona(
        schema_version=raw["schema_version"],
        persona_id=    raw["persona_id"],
        description=   raw["description"],
        poc_type=      raw["poc_type"],
        baseline=      baseline,
        thresholds=    thresholds,
        noise_profile= noise,
        correlations=  correlations,
    )

    logger.info(
        "Persona loaded",
        extra={
            "event":    "persona_loaded",
            "persona":  persona.persona_id,
            "poc_type": persona.poc_type,
            "sensors":  list(persona.baseline.keys()),
        },
    )
    return persona


# ---------------------------------------------------------------------------
# Parsers
# ---------------------------------------------------------------------------

def _parse_baseline(
    raw: Any,
    path: Path,
) -> dict[str, SensorBaseline]:
    """Parse the baseline block into typed SensorBaseline objects."""
    if not isinstance(raw, dict):
        raise ConfigurationError(f"'baseline' must be a mapping in {path}")

    result: dict[str, SensorBaseline] = {}
    for sensor_name, cfg in raw.items():
        for required in ("min", "max", "unit", "sampling_interval_ms"):
            if required not in cfg:
                raise ConfigurationError(
                    f"Missing '{required}' in baseline.{sensor_name} in {path}"
                )
        result[sensor_name] = SensorBaseline(
            sensor_name=          sensor_name,
            min_value=            float(cfg["min"]),
            max_value=            float(cfg["max"]),
            unit=                 str(cfg["unit"]),
            sampling_interval_ms= int(cfg["sampling_interval_ms"]),
        )
    return result


def _parse_thresholds(raw: Any, path: Path) -> dict[str, float]:
    """Parse the thresholds block into a flat dict."""
    if not isinstance(raw, dict):
        raise ConfigurationError(f"'thresholds' must be a mapping in {path}")
    result: dict[str, float] = {}
    for key, value in raw.items():
        try:
            result[key] = float(value)
        except (TypeError, ValueError):
            raise ConfigurationError(
                f"Threshold '{key}' must be a number in {path}, got: {value}"
            )
    return result


def _parse_noise_profile(
    raw: Any,
    baseline: dict[str, SensorBaseline],
    path: Path,
) -> dict[str, str]:
    """Parse the noise_profile block and validate sensor names."""
    if not isinstance(raw, dict):
        raise ConfigurationError(f"'noise_profile' must be a mapping in {path}")
    valid_models = {"gaussian", "drift", "burst", "quantization"}
    result: dict[str, str] = {}
    for sensor_name, model in raw.items():
        if sensor_name not in baseline:
            raise ConfigurationError(
                f"noise_profile references unknown sensor '{sensor_name}' in {path}"
            )
        if model not in valid_models:
            raise ConfigurationError(
                f"Unknown noise model '{model}' for sensor '{sensor_name}' in {path}. "
                f"Valid options: {valid_models}"
            )
        result[sensor_name] = model
    return result


def _parse_correlations(raw: Any, path: Path) -> list[CorrelationRule]:
    """Parse the correlations block into typed CorrelationRule objects."""
    if not isinstance(raw, list):
        raise ConfigurationError(f"'correlations' must be a list in {path}")
    rules: list[CorrelationRule] = []
    for i, item in enumerate(raw):
        for required in ("leader_sensor", "follower_sensor", "coupling_strength", "lag_seconds"):
            if required not in item:
                raise ConfigurationError(
                    f"Missing '{required}' in correlations[{i}] in {path}"
                )
        rules.append(CorrelationRule(
            leader_sensor=     item["leader_sensor"],
            follower_sensor=   item["follower_sensor"],
            coupling_strength= float(item["coupling_strength"]),
            lag_seconds=       float(item["lag_seconds"]),
        ))
    return rules
