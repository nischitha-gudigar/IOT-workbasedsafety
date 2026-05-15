"""
simulator/sensors/spo2_sensor.py
----------------------------------
SpO2 (blood oxygen saturation) sensor generator for the Vitals Simulator.

Produces:
  - spo2 (percent)

Uses quantization noise by default (pulse oximeters report whole %).
Follows HR as a leader (high HR can slightly reduce SpO2 under exertion).
"""

from __future__ import annotations

from simulator.core.condition_mapper import ConditionMapper, SensorParams
from simulator.engine.correlation import CorrelationEngine
from simulator.engine.fault import FaultController
from simulator.engine.noise import create_noise_model, NoiseModel
from simulator.engine.signal import create_signal_generator, SignalGenerator
from simulator.sensors.base import BaseSensor, SensorReading
from simulator.utils.logger import get_logger

logger = get_logger(__name__)


class SpO2Sensor(BaseSensor):
    """
    Simulates a pulse oximeter SpO2 sensor on a wearable.

    SpO2 values are clamped to [0, 100] — it is a percentage.
    Quantization noise is typical (1% steps from most devices).
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

        self._signal_gen  = create_signal_generator(params.behavior_type, params.signal_params)
        self._noise_model = create_noise_model(
            params.noise_model, params.noise_intensity, params.noise_params
        )
        self._last_value: float = (params.min_value + params.max_value) / 2.0

    @property
    def sensor_name(self) -> str:
        return "spo2"

    @property
    def sampling_interval_ms(self) -> int:
        return self._params.sampling_interval_ms

    def update_params(self, params: SensorParams) -> None:
        self._params = params
        self._signal_gen = create_signal_generator(params.behavior_type, params.signal_params)
        self._noise_model = create_noise_model(
            params.noise_model, params.noise_intensity, params.noise_params
        )
        self._signal_gen.reset()
        self._noise_model.reset()

    def tick(
        self,
        phase_name:       str,
        sim_time_seconds: float,
        phase_progress:   float = 0.0,
        override_value:   float | None = None,
    ) -> SensorReading:
        """Generate one SpO2 reading."""
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

        # Correlation: high HR can slightly reduce SpO2
        adjustment = self._correlation.get_adjustment("spo2", sim_time_seconds)
        correlated = noisy + adjustment

        fault_result = self._fault_ctrl.evaluate("spo2", correlated, sim_time_seconds)

        if fault_result.is_dropout:
            return SensorReading(
                sensor_name=  "spo2",
                value=        self._last_value,
                unit=         self._params.unit,
                condition=    "normal",
                quality=      "bad",
                fault_active= True,
                phase=        phase_name,
                extra=        {},
            )

        # SpO2 is a percentage — clamp to [0, 100]
        final = round(min(100.0, max(0.0, fault_result.value)), 1)
        self._last_value = final

        condition = self._mapper.classify("spo2", final, self._threshold_multiplier)

        return SensorReading(
            sensor_name=  "spo2",
            value=        final,
            unit=         self._params.unit,
            condition=    condition.condition,
            quality=      fault_result.quality,
            fault_active= fault_result.fault_active,
            phase=        phase_name,
            extra=        {},
        )
