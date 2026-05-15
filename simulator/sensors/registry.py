"""
simulator/sensors/registry.py
-------------------------------
Maps sensor names (as they appear in persona/scenario YAMLs) to their
generator classes.

Adding a new sensor type:
  1. Create the sensor class in simulator/sensors/new_sensor.py
  2. Add an entry here
  3. Done — no changes needed in engine or transport layer
"""

from __future__ import annotations

from typing import Type

from simulator.sensors.base import BaseSensor
from simulator.sensors.heart_rate_sensor import HeartRateSensor
from simulator.sensors.hrv_sensor import HRVSensor
from simulator.sensors.imu_sensor import IMUSensor
from simulator.sensors.spo2_sensor import SpO2Sensor
from simulator.sensors.temperature_sensor import TemperatureSensor

# ---------------------------------------------------------------------------
# Registry: sensor_name → sensor class
# ---------------------------------------------------------------------------
# IMU sensor handles posture_angle AND accel_magnitude/x/y/z together.
# The engine looks up "posture_angle" to find IMUSensor and also registers
# accel streams as output from that single generator.

SENSOR_REGISTRY: dict[str, Type[BaseSensor]] = {
    "heart_rate":             HeartRateSensor,
    "skin_temperature":       TemperatureSensor,
    "spo2":                   SpO2Sensor,
    "heart_rate_variability": HRVSensor,
    "posture_angle":          IMUSensor,     # also produces accel streams

    # Future sensors:
    # "ble_rssi":     BLESensor,
    # "ecg_waveform": ECGSensor,
}

# Sensors that are produced as secondary outputs of another generator.
# The engine skips building independent generators for these — they are
# included in the primary sensor's SensorReading.extra dict.
DERIVED_SENSORS: set[str] = {
    "accel_magnitude",
    "accel_x",
    "accel_y",
    "accel_z",
}


def get_sensor_class(sensor_name: str) -> Type[BaseSensor] | None:
    """
    Look up the sensor class for a given sensor name.

    Returns None for derived sensors (they have no independent generator).
    """
    return SENSOR_REGISTRY.get(sensor_name)


def is_derived(sensor_name: str) -> bool:
    """Return True if this sensor is a derived output of another generator."""
    return sensor_name in DERIVED_SENSORS
