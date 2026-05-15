"""
simulator/sensors/base.py
--------------------------
Abstract base class for all sensor generators in the Vitals Simulator.

Every sensor implements this interface. The ScenarioEngine only knows
about BaseSensor — it never imports concrete sensor types directly.
This is how new sensors get added without changing engine code.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any


@dataclass
class SensorReading:
    """
    A single sensor reading ready for MQTT publication.

    All fields are populated by the sensor generator. The transport
    layer publishes this as-is.
    """
    sensor_name:   str
    value:         float
    unit:          str
    condition:     str         # normal | warning | critical
    quality:       str         # good | uncertain | bad
    fault_active:  bool
    phase:         str
    extra:         dict[str, Any]   # sensor-specific additional streams


class BaseSensor(ABC):
    """
    Abstract base for all sensor generators.

    Each concrete subclass generates one logical sensor (which may
    produce multiple data streams — e.g. IMU produces 6 streams).

    The engine calls tick() once per sampling interval. The sensor
    uses its internal signal generator, noise model, and correlation
    engine to produce a SensorReading.
    """

    @abstractmethod
    def tick(
        self,
        phase_name:       str,
        sim_time_seconds: float,
        override_value:   float | None = None,
    ) -> SensorReading:
        """
        Generate one sensor reading for the current tick.

        Args:
            phase_name:       Name of the current scenario phase.
            sim_time_seconds: Elapsed simulated time in seconds.
            override_value:   If set, use this value instead of generating one.
                              Used by scenario event injection.

        Returns:
            SensorReading ready for MQTT publication.
        """
        ...

    @abstractmethod
    def update_params(self, params: Any) -> None:
        """
        Update signal/noise parameters when the phase changes.

        Called by the engine at every phase transition.

        Args:
            params: SensorParams dataclass from condition_mapper.
        """
        ...

    @property
    @abstractmethod
    def sensor_name(self) -> str:
        """The canonical name of this sensor (matches YAML keys)."""
        ...

    @property
    @abstractmethod
    def sampling_interval_ms(self) -> int:
        """How often this sensor should be ticked (in simulated milliseconds)."""
        ...
