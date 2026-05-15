"""
simulator/sensors/imu_sensor.py
---------------------------------
IMU (Inertial Measurement Unit) sensor generator for the Vitals Simulator.

Produces multiple streams:
  - posture_angle (degrees) — derived from accelerometer orientation
  - accel_magnitude (g)     — combined acceleration magnitude
  - accel_x, accel_y, accel_z (g) — individual axis readings

The posture_angle is the primary safety-relevant output. The accel
streams are derived from it. A fall event creates a spike in
accel_magnitude via FaultController (burst fault).

All sensors in this IMU share the same sampling interval (configured
per persona). The phase config in the scenario drives posture_angle
and accel_magnitude; the individual axis readings are derived.
"""

from __future__ import annotations

import math
import random
from typing import Any

from simulator.core.condition_mapper import ConditionMapper, SensorParams
from simulator.engine.correlation import CorrelationEngine
from simulator.engine.fault import FaultController
from simulator.engine.noise import create_noise_model, NoiseModel
from simulator.engine.signal import create_signal_generator, SignalGenerator
from simulator.sensors.base import BaseSensor, SensorReading
from simulator.utils.logger import get_logger

logger = get_logger(__name__)


class IMUSensor(BaseSensor):
    """
    Simulates a 6-axis IMU wearable sensor (belt unit for worker safety).

    Primary output is posture_angle. Accel magnitude is also tracked.
    Individual axis readings (accel_x/y/z) are derived from the posture
    angle to maintain physical consistency.

    Fall detection: when a fall event is injected, accel_magnitude
    spikes via FaultController. The spike is visible across all streams.
    """

    def __init__(
        self,
        posture_params:  SensorParams,
        accel_params:    SensorParams,
        correlation:     CorrelationEngine,
        fault_ctrl:      FaultController,
        mapper:          ConditionMapper,
        threshold_multiplier: float = 1.0,
    ) -> None:
        self._posture_params = posture_params
        self._accel_params   = accel_params
        self._correlation    = correlation
        self._fault_ctrl     = fault_ctrl
        self._mapper         = mapper
        self._threshold_multiplier = threshold_multiplier

        self._posture_gen  = create_signal_generator(
            posture_params.behavior_type, posture_params.signal_params
        )
        self._posture_noise = create_noise_model(
            posture_params.noise_model, posture_params.noise_intensity, posture_params.noise_params
        )
        self._accel_gen    = create_signal_generator(
            accel_params.behavior_type, accel_params.signal_params
        )
        self._accel_noise  = create_noise_model(
            accel_params.noise_model, accel_params.noise_intensity, accel_params.noise_params
        )

        self._last_posture: float = 10.0
        self._last_accel:   float = 1.0

    @property
    def sensor_name(self) -> str:
        return "posture_angle"

    @property
    def sampling_interval_ms(self) -> int:
        return self._posture_params.sampling_interval_ms

    def update_params(self, params: SensorParams) -> None:
        """Called with posture_angle params at phase transitions."""
        self._posture_params = params
        self._posture_gen    = create_signal_generator(params.behavior_type, params.signal_params)
        self._posture_noise  = create_noise_model(
            params.noise_model, params.noise_intensity, params.noise_params
        )
        self._posture_gen.reset()
        self._posture_noise.reset()

    def update_accel_params(self, params: SensorParams) -> None:
        """Update accel_magnitude params at phase transitions."""
        self._accel_params = params
        self._accel_gen    = create_signal_generator(params.behavior_type, params.signal_params)
        self._accel_noise  = create_noise_model(
            params.noise_model, params.noise_intensity, params.noise_params
        )
        self._accel_gen.reset()
        self._accel_noise.reset()

    def tick(
        self,
        phase_name:       str,
        sim_time_seconds: float,
        phase_progress:   float = 0.0,
        override_value:   float | None = None,
        override_accel:   float | None = None,
    ) -> SensorReading:
        """
        Generate one IMU tick — posture + accel streams.

        Returns:
            SensorReading for posture_angle.
            Additional streams (accel_x/y/z, accel_magnitude) are in extra dict.
        """
        # --- Posture angle ---
        if override_value is not None:
            posture_base = override_value
        else:
            posture_base = self._posture_gen.generate(
                self._posture_params.min_value,
                self._posture_params.max_value,
                phase_progress,
            )

        phase_range = self._posture_params.max_value - self._posture_params.min_value
        posture_noisy = self._posture_noise.apply(
            posture_base,
            max(phase_range, abs(posture_base)) if override_value is not None else phase_range,
        )

        # Correlation: posture drives accel as leader
        self._correlation.record_leader("posture_angle", posture_noisy, sim_time_seconds)
        posture_fault = self._fault_ctrl.evaluate("posture_angle", posture_noisy, sim_time_seconds)
        posture_final = round(max(0.0, posture_fault.value or posture_noisy), 2)
        self._last_posture = posture_final

        # --- Accel magnitude ---
        if override_accel is not None:
            accel_base = override_accel
        else:
            accel_base = self._accel_gen.generate(
                self._accel_params.min_value,
                self._accel_params.max_value,
                phase_progress,
            )

        accel_range = self._accel_params.max_value - self._accel_params.min_value
        accel_noisy = self._accel_noise.apply(
            accel_base,
            max(accel_range, abs(accel_base)) if override_accel is not None else accel_range,
        )
        accel_corr_adj = self._correlation.get_adjustment("accel_magnitude", sim_time_seconds)
        accel_correlated = accel_noisy + accel_corr_adj

        self._correlation.record_leader("accel_magnitude", accel_correlated, sim_time_seconds)
        accel_fault  = self._fault_ctrl.evaluate("accel_magnitude", accel_correlated, sim_time_seconds)
        accel_final  = round(max(0.0, accel_fault.value or accel_correlated), 3)
        self._last_accel = accel_final

        # --- Derive individual axes from posture angle ---
        accel_x, accel_y, accel_z = self._derive_axes(posture_final, accel_final)

        # Classify posture condition
        posture_condition = self._mapper.classify(
            "posture_angle", posture_final, self._threshold_multiplier
        )
        # Also check accel (fall detection)
        accel_condition = self._mapper.classify(
            "accel_magnitude", accel_final, self._threshold_multiplier
        )
        # Use the worse condition of the two
        condition = _worse_condition(posture_condition.condition, accel_condition.condition)

        fault_active = posture_fault.fault_active or accel_fault.fault_active
        quality = "bad" if (posture_fault.is_dropout or accel_fault.is_dropout) \
                  else ("uncertain" if fault_active else "good")

        return SensorReading(
            sensor_name=  "posture_angle",
            value=        posture_final,
            unit=         self._posture_params.unit,
            condition=    condition,
            quality=      quality,
            fault_active= fault_active,
            phase=        phase_name,
            extra={
                "accel_magnitude": accel_final,
                "accel_x":         round(accel_x, 3),
                "accel_y":         round(accel_y, 3),
                "accel_z":         round(accel_z, 3),
            },
        )

    def _derive_axes(self, posture_angle_deg: float, accel_magnitude: float) -> tuple[float, float, float]:
        """
        Derive accel_x, accel_y, accel_z from posture angle and magnitude.

        A person standing upright (angle ≈ 0°) has gravity mostly in Z.
        As they bend forward (angle increases), X increases, Z decreases.
        Small noise is added to each axis for realism.
        """
        angle_rad = math.radians(min(posture_angle_deg, 90.0))
        accel_z = accel_magnitude * math.cos(angle_rad)
        accel_x = accel_magnitude * math.sin(angle_rad)
        accel_y = random.gauss(0.0, 0.05)   # lateral sway

        return accel_x, accel_y, accel_z


def _worse_condition(a: str, b: str) -> str:
    """Return the more severe of two condition strings."""
    order = {"normal": 0, "warning": 1, "critical": 2}
    return a if order.get(a, 0) >= order.get(b, 0) else b
