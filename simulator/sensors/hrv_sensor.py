"""
simulator/sensors/hrv_sensor.py
---------------------------------
Heart Rate Variability (HRV) sensor generator for the Vitals Simulator.

Produces:
  - heart_rate_variability (ms)

HRV is a marker of cardiac stress and autonomic nervous system state.
Lower HRV indicates higher stress. During tachycardia, HRV drops sharply.
HRV follows HR as a leader sensor with a short lag.
"""

from __future__ import annotations

from simulator.core.condition_mapper import ConditionMapper, SensorParams
from simulator.engine.correlation import CorrelationEngine
from simulator.engine.fault import FaultController
from simulator.engine.noise import create_noise_model
from simulator.engine.signal import create_signal_generator
from simulator.sensors.base import BaseSensor, SensorReading
from simulator.utils.logger import get_logger

logger = get_logger(__name__)


class HRVSensor(BaseSensor):
    """
    Simulates heart rate variability derived from a PPG or ECG signal.

    HRV is physiologically inverse to HR — higher HR correlates with
    lower HRV. This is modelled via the correlation engine.
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
        return "heart_rate_variability"

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
        """Generate one HRV reading."""
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

        # Correlation: high HR → lower HRV (negative coupling in persona YAML)
        adjustment = self._correlation.get_adjustment("heart_rate_variability", sim_time_seconds)
        correlated = noisy + adjustment

        fault_result = self._fault_ctrl.evaluate(
            "heart_rate_variability", correlated, sim_time_seconds
        )

        if fault_result.is_dropout:
            return SensorReading(
                sensor_name=  "heart_rate_variability",
                value=        self._last_value,
                unit=         self._params.unit,
                condition=    "normal",
                quality=      "bad",
                fault_active= True,
                phase=        phase_name,
                extra=        {},
            )

        # HRV must be non-negative
        final = round(max(0.0, fault_result.value), 1)
        self._last_value = final

        condition = self._mapper.classify(
            "heart_rate_variability", final, self._threshold_multiplier
        )

        return SensorReading(
            sensor_name=  "heart_rate_variability",
            value=        final,
            unit=         self._params.unit,
            condition=    condition.condition,
            quality=      fault_result.quality,
            fault_active= fault_result.fault_active,
            phase=        phase_name,
            extra=        {},
        )
