"""
simulator/sensors/heart_rate_sensor.py
----------------------------------------
Heart rate sensor generator for the Vitals Simulator.

Produces:
  - heart_rate (bpm) — primary output

Uses signal.py behavior + noise.py model + correlation.py adjustment.
Condition is classified via ConditionMapper.
"""

from __future__ import annotations

from typing import Any

from simulator.core.condition_mapper import ConditionMapper, SensorParams
from simulator.engine.correlation import CorrelationEngine
from simulator.engine.fault import FaultController
from simulator.engine.noise import create_noise_model, NoiseModel
from simulator.engine.signal import create_signal_generator, SignalGenerator
from simulator.sensors.base import BaseSensor, SensorReading
from simulator.utils.logger import get_logger

logger = get_logger(__name__)


class HeartRateSensor(BaseSensor):
    """
    Simulates a heart rate sensor (PPG-based wearable).

    Generates HR values in bpm. Participates in correlation as both
    a leader (drives temperature, SpO2) and potentially a follower
    (driven by activity/accel).
    """

    def __init__(
        self,
        params:      SensorParams,
        correlation: CorrelationEngine,
        fault_ctrl:  FaultController,
        mapper:      ConditionMapper,
        threshold_multiplier: float = 1.0,
    ) -> None:
        self._params      = params
        self._correlation = correlation
        self._fault_ctrl  = fault_ctrl
        self._mapper      = mapper
        self._threshold_multiplier = threshold_multiplier

        self._signal_gen:  SignalGenerator = create_signal_generator(
            params.behavior_type, params.signal_params
        )
        self._noise_model: NoiseModel = create_noise_model(
            params.noise_model, params.noise_intensity, params.noise_params
        )
        self._last_value: float = (params.min_value + params.max_value) / 2.0

    @property
    def sensor_name(self) -> str:
        return "heart_rate"

    @property
    def sampling_interval_ms(self) -> int:
        return self._params.sampling_interval_ms

    def update_params(self, params: SensorParams) -> None:
        """Rebuild signal and noise models when the phase changes."""
        self._params = params
        self._signal_gen = create_signal_generator(params.behavior_type, params.signal_params)
        self._noise_model = create_noise_model(
            params.noise_model, params.noise_intensity, params.noise_params
        )
        # Reset stateful models
        self._signal_gen.reset()
        self._noise_model.reset()

    def tick(
        self,
        phase_name:       str,
        sim_time_seconds: float,
        phase_progress:   float = 0.0,
        override_value:   float | None = None,
    ) -> SensorReading:
        """Generate one HR reading."""
        if override_value is not None:
            base = override_value
        else:
            base = self._signal_gen.generate(
                self._params.min_value,
                self._params.max_value,
                phase_progress,
            )

        phase_range = self._params.max_value - self._params.min_value
        noisy = self._noise_model.apply(
            base,
            max(phase_range, abs(base)) if override_value is not None else phase_range,
        )

        # Correlation: add adjustment from leader sensors (e.g. accel → HR)
        adjustment = self._correlation.get_adjustment("heart_rate", sim_time_seconds)
        correlated = noisy + adjustment

        # Record self as leader for followers (temperature, SpO2)
        self._correlation.record_leader("heart_rate", correlated, sim_time_seconds)

        # Fault injection
        fault_result = self._fault_ctrl.evaluate("heart_rate", correlated, sim_time_seconds)

        if fault_result.is_dropout:
            return SensorReading(
                sensor_name=  "heart_rate",
                value=        self._last_value,
                unit=         self._params.unit,
                condition=    "normal",
                quality=      "bad",
                fault_active= True,
                phase=        phase_name,
                extra=        {},
            )

        final = round(max(0.0, fault_result.value), 2)
        self._last_value = final

        condition = self._mapper.classify("heart_rate", final, self._threshold_multiplier)

        return SensorReading(
            sensor_name=  "heart_rate",
            value=        final,
            unit=         self._params.unit,
            condition=    condition.condition,
            quality=      fault_result.quality,
            fault_active= fault_result.fault_active,
            phase=        phase_name,
            extra=        {},
        )
