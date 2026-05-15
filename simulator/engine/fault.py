"""
simulator/engine/fault.py
-------------------------
Fault injection for the Vitals Simulator.

Simulates real-world wearable sensor failure scenarios:

  dropout       — Sensor stops publishing (wearable removed, BLE drop)
  flatline      — Sensor reads a constant stuck value (sensor freeze)
  spike         — Sudden large transient (motion artifact, ESD)
  noise_burst   — Signal with amplified noise (electrode movement, EMI)

FaultEvent objects are created by the ScenarioEngine when a scenario
event of type fault is triggered. The FaultController tracks which
faults are currently active and applies them each tick.

Unlike the original implementation which used a DB schedule,
faults here are driven entirely by scenario YAML events and
manual triggers from the demo API. No persistence needed.
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field
from typing import Optional

from simulator.utils.logger import get_logger

logger = get_logger(__name__)


# ---------------------------------------------------------------------------
# Result type
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class FaultResult:
    """
    Result of fault evaluation for one sensor tick.

    Attributes:
        value:        Transformed sensor value (equals input if no fault active).
        is_dropout:   True if this sensor should not publish this tick.
        fault_type:   Name of the active fault, or None.
        fault_active: True if any fault is currently active.
    """
    value:        Optional[float]
    is_dropout:   bool
    fault_type:   Optional[str]
    fault_active: bool

    @property
    def quality(self) -> str:
        """Signal quality string for the MQTT payload."""
        if self.is_dropout:
            return "bad"
        if self.fault_active:
            return "uncertain"
        return "good"


# ---------------------------------------------------------------------------
# Fault event
# ---------------------------------------------------------------------------

@dataclass
class FaultEvent:
    """A single active fault on one sensor."""
    sensor_name:      str
    fault_type:       str
    end_sim_seconds:  float          # simulated time when this fault expires
    fault_factor:     float = 3.0    # spike/noise magnitude multiplier


# ---------------------------------------------------------------------------
# Fault controller
# ---------------------------------------------------------------------------

class FaultController:
    """
    Tracks active faults and applies them to sensor values each tick.

    Faults are injected by the ScenarioEngine via inject() when a scenario
    event fires. They expire automatically when the simulated time passes
    their end_sim_seconds.

    One FaultController is shared across all sensors for a running scenario.
    """

    def __init__(self) -> None:
        # sensor_name → list of active FaultEvent
        self._active: dict[str, list[FaultEvent]] = {}
        # sensor_name → last known good value (for flatline)
        self._last_known: dict[str, float] = {}

    def inject(self, event: FaultEvent) -> None:
        """
        Activate a fault on a sensor.

        Args:
            event: FaultEvent describing the fault type, sensor, and duration.
        """
        self._active.setdefault(event.sensor_name, []).append(event)
        logger.info(
            "Fault injected",
            extra={
                "event":      "fault_injected",
                "sensor":     event.sensor_name,
                "fault_type": event.fault_type,
                "ends_at":    round(event.end_sim_seconds, 1),
            },
        )

    def evaluate(
        self,
        sensor_name:      str,
        clean_value:      float,
        sim_time_seconds: float,
    ) -> FaultResult:
        """
        Apply any active fault to a clean sensor value.

        Expired faults are removed automatically.

        Args:
            sensor_name:      The sensor being evaluated.
            clean_value:      Signal value before any fault.
            sim_time_seconds: Current simulated time in seconds.

        Returns:
            FaultResult with transformed value and metadata.
        """
        # Track last known good value for flatline
        self._last_known[sensor_name] = clean_value

        # Expire finished faults
        active = self._active.get(sensor_name, [])
        active = [f for f in active if sim_time_seconds < f.end_sim_seconds]
        self._active[sensor_name] = active

        if not active:
            return FaultResult(
                value=        clean_value,
                is_dropout=   False,
                fault_type=   None,
                fault_active= False,
            )

        # Apply the first active fault (one fault per sensor at a time)
        fault = active[0]
        return self._apply(fault, clean_value, sensor_name)

    def clear_all(self) -> None:
        """Remove all active faults (called on scenario reset)."""
        self._active.clear()

    # ------------------------------------------------------------------
    # Fault application
    # ------------------------------------------------------------------

    def _apply(
        self,
        fault:        FaultEvent,
        clean_value:  float,
        sensor_name:  str,
    ) -> FaultResult:
        """Apply the fault transformation to the clean signal value."""
        ft = fault.fault_type

        if ft == "dropout":
            return FaultResult(
                value=        None,
                is_dropout=   True,
                fault_type=   "dropout",
                fault_active= True,
            )

        if ft == "flatline":
            stuck = self._last_known.get(sensor_name, clean_value)
            return FaultResult(
                value=        stuck,
                is_dropout=   False,
                fault_type=   "flatline",
                fault_active= True,
            )

        if ft == "spike":
            spike = clean_value * fault.fault_factor * random.uniform(0.8, 1.2)
            return FaultResult(
                value=        spike,
                is_dropout=   False,
                fault_type=   "spike",
                fault_active= True,
            )

        if ft == "noise_burst":
            perturbation = clean_value * random.gauss(0.0, 0.1 * fault.fault_factor)
            return FaultResult(
                value=        clean_value + perturbation,
                is_dropout=   False,
                fault_type=   "noise_burst",
                fault_active= True,
            )

        # Unknown fault type — pass through
        logger.warning(
            "Unknown fault type — passing clean value",
            extra={"event": "unknown_fault_type", "fault_type": ft, "sensor": sensor_name},
        )
        return FaultResult(
            value=        clean_value,
            is_dropout=   False,
            fault_type=   ft,
            fault_active= True,
        )
