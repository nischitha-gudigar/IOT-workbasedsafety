"""
simulator/core/condition_mapper.py
------------------------------------
Bridges the YAML world (personas, scenario phases) and the engine world
(signal.py, noise.py parameters).

Two responsibilities:
  1. SensorParams — convert a scenario phase sensor config + persona baseline
     into the concrete parameters that signal.py and noise.py consume.

  2. ConditionClassifier — classify a sensor reading as normal/warning/critical
     by comparing it against persona thresholds (adjusted by worker context).

All tuneable values (noise intensity map, signal params, noise params,
condition threshold fractions) come from simulator_config.yaml.
Nothing is hardcoded here.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from simulator.core.persona_loader import Persona, SensorBaseline
from simulator.core.scenario_loader import Phase, SensorPhaseConfig
from simulator.utils.logger import get_logger

logger = get_logger(__name__)


# ---------------------------------------------------------------------------
# Output types
# ---------------------------------------------------------------------------

@dataclass
class SensorParams:
    """
    Fully resolved parameters for one sensor in one phase.
    Ready to be consumed by signal.py and noise.py factories.
    """
    sensor_name:          str
    min_value:            float
    max_value:            float
    behavior_type:        str
    noise_model:          str
    noise_intensity:      float       # resolved float, not 'low'/'medium'/'high'
    signal_params:        dict[str, Any]  # passed to create_signal_generator()
    noise_params:         dict[str, Any]  # passed to create_noise_model()
    unit:                 str
    sampling_interval_ms: int


@dataclass
class ConditionResult:
    """Classification of a single sensor reading."""
    condition: str          # normal | warning | critical
    threshold_name: str | None   # which threshold was crossed, or None


# ---------------------------------------------------------------------------
# Condition mapper
# ---------------------------------------------------------------------------

class ConditionMapper:
    """
    Converts scenario phase configs into engine-ready sensor parameters
    and classifies readings against persona thresholds.

    Args:
        persona:    Loaded Persona dataclass.
        sim_config: Parsed simulator_config.yaml dict.
    """

    def __init__(self, persona: Persona, sim_config: dict[str, Any]) -> None:
        self._persona    = persona
        self._sim_config = sim_config

        self._noise_intensity_map: dict[str, float] = sim_config.get(
            "noise_intensity_map", {"low": 0.02, "medium": 0.05, "high": 0.12}
        )
        self._signal_params: dict[str, dict] = sim_config.get("signal_params", {})
        self._noise_params:  dict[str, dict] = sim_config.get("noise_params", {})
        self._condition_cfg: dict[str, Any]  = sim_config.get("condition", {})

    def resolve_phase_sensors(self, phase: Phase) -> dict[str, SensorParams]:
        """
        Resolve all sensor parameters for a scenario phase.

        For each sensor defined in the phase:
          - Uses the phase YAML for min/max/behavior
          - Uses the persona for noise model, unit, sampling_interval_ms
          - Resolves noise_intensity label to float via sim_config
          - Merges signal/noise tuning params from sim_config

        Args:
            phase: A Phase dataclass from the loaded scenario.

        Returns:
            Dict mapping sensor_name → SensorParams.
        """
        result: dict[str, SensorParams] = {}
        noise_intensity_float = self._noise_intensity_map.get(phase.noise_intensity, 0.05)

        for sensor_name, phase_cfg in phase.sensors.items():
            baseline = self._persona.baseline.get(sensor_name)
            if baseline is None:
                logger.warning(
                    "Phase references sensor not in persona baseline — skipping",
                    extra={
                        "event":   "sensor_not_in_baseline",
                        "sensor":  sensor_name,
                        "phase":   phase.name,
                        "persona": self._persona.persona_id,
                    },
                )
                continue

            noise_model = self._persona.noise_profile.get(sensor_name, "gaussian")

            result[sensor_name] = SensorParams(
                sensor_name=          sensor_name,
                min_value=            phase_cfg.min_value,
                max_value=            phase_cfg.max_value,
                behavior_type=        phase_cfg.behavior,
                noise_model=          noise_model,
                noise_intensity=      noise_intensity_float,
                signal_params=        dict(self._signal_params.get(phase_cfg.behavior, {})),
                noise_params=         dict(self._noise_params.get(noise_model, {})),
                unit=                 baseline.unit,
                sampling_interval_ms= baseline.sampling_interval_ms,
            )

        return result

    def resolve_baseline_sensors(self) -> dict[str, SensorParams]:
        """
        Resolve sensor parameters from the persona baseline alone (no phase).

        Used when no scenario is loaded yet or as a fallback.
        """
        result: dict[str, SensorParams] = {}
        noise_intensity_float = self._noise_intensity_map.get("low", 0.02)

        for sensor_name, baseline in self._persona.baseline.items():
            noise_model = self._persona.noise_profile.get(sensor_name, "gaussian")
            result[sensor_name] = SensorParams(
                sensor_name=          sensor_name,
                min_value=            baseline.min_value,
                max_value=            baseline.max_value,
                behavior_type=        "oscillating_stable",
                noise_model=          noise_model,
                noise_intensity=      noise_intensity_float,
                signal_params=        dict(self._signal_params.get("oscillating_stable", {})),
                noise_params=         dict(self._noise_params.get(noise_model, {})),
                unit=                 baseline.unit,
                sampling_interval_ms= baseline.sampling_interval_ms,
            )
        return result

    def classify(
        self,
        sensor_name: str,
        value: float,
        threshold_multiplier: float = 1.0,
    ) -> ConditionResult:
        """
        Classify a sensor reading against persona thresholds.

        Args:
            sensor_name:          The sensor being classified.
            value:                The reading value.
            threshold_multiplier: Applied to all thresholds (from worker_context).

        Returns:
            ConditionResult with 'normal', 'warning', or 'critical'.
        """
        thresholds = self._persona.thresholds
        warning_frac  = float(self._condition_cfg.get("warning_threshold_fraction", 1.0))
        critical_frac = float(self._condition_cfg.get("critical_threshold_fraction", 1.15))

        # HR high
        hr_high = thresholds.get("heart_rate_high")
        if sensor_name == "heart_rate" and hr_high is not None:
            adjusted = hr_high * threshold_multiplier
            if value >= adjusted * critical_frac:
                return ConditionResult("critical", "heart_rate_high")
            if value >= adjusted * warning_frac:
                return ConditionResult("warning", "heart_rate_high")

        # HR low (healthcare only)
        hr_low = thresholds.get("heart_rate_low")
        if sensor_name == "heart_rate" and hr_low is not None:
            adjusted = hr_low * threshold_multiplier
            if value <= adjusted / critical_frac:
                return ConditionResult("critical", "heart_rate_low")
            if value <= adjusted / warning_frac:
                return ConditionResult("warning", "heart_rate_low")

        # SpO2 low
        spo2_low = thresholds.get("spo2_low")
        if sensor_name == "spo2" and spo2_low is not None:
            adjusted = spo2_low * threshold_multiplier
            if value <= adjusted / critical_frac:
                return ConditionResult("critical", "spo2_low")
            if value <= adjusted / warning_frac:
                return ConditionResult("warning", "spo2_low")

        # Temperature high
        temp_high = thresholds.get("temperature_high")
        if sensor_name == "skin_temperature" and temp_high is not None:
            adjusted = temp_high * threshold_multiplier
            if value >= adjusted * critical_frac:
                return ConditionResult("critical", "temperature_high")
            if value >= adjusted * warning_frac:
                return ConditionResult("warning", "temperature_high")

        # Posture angle warning / critical
        posture_warning = thresholds.get("posture_angle_warning")
        posture_critical = thresholds.get("posture_angle_critical")
        if sensor_name == "posture_angle":
            if posture_critical is not None and value >= posture_critical * threshold_multiplier:
                return ConditionResult("critical", "posture_angle_critical")
            if posture_warning is not None and value >= posture_warning * threshold_multiplier:
                return ConditionResult("warning", "posture_angle_warning")

        # Accel fall
        fall_threshold = thresholds.get("accel_fall_threshold")
        if sensor_name == "accel_magnitude" and fall_threshold is not None:
            if value >= fall_threshold * threshold_multiplier:
                return ConditionResult("critical", "accel_fall_threshold")

        # HRV low
        hrv_low = thresholds.get("hrv_low")
        if sensor_name == "heart_rate_variability" and hrv_low is not None:
            adjusted = hrv_low * threshold_multiplier
            if value <= adjusted / critical_frac:
                return ConditionResult("critical", "hrv_low")
            if value <= adjusted / warning_frac:
                return ConditionResult("warning", "hrv_low")

        return ConditionResult("normal", None)
