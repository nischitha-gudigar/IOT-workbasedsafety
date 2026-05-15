"""
simulator/engine/correlation.py
--------------------------------
Leader/follower sensor correlation engine for the Vitals Simulator.

Physical rationale for human vitals:
    - Activity (accel magnitude) increases → HR rises with a lag (~30s)
    - HR increases → skin temperature rises with a lag (~120s)
    - HR increases → SpO2 may decrease slightly under exertion (~60s lag)
    - Posture angle increases → accel magnitude increases (~0s lag)

Without correlation, sensors would appear statistically independent.
With correlation, the multi-sensor story is physiologically coherent —
a cardiac event or fall has ripple effects across all sensors.

Correlation model (unchanged from original):
    1. Leader sensor records its normalised deviation each tick into a
       time-indexed rolling lag buffer.
    2. Follower sensor queries the buffer at its configured lag (seconds).
    3. Follower adds: leader_deviation * coupling_strength to its base value.

Correlation rules are defined per persona in the persona YAML under
the correlations: block. The CorrelationEngine is populated from
those rules at scenario load time.
"""

from __future__ import annotations

import collections
from dataclasses import dataclass
from typing import Optional

from simulator.utils.logger import get_logger

logger = get_logger(__name__)


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------

@dataclass
class CorrelationRule:
    """A single leader→follower coupling rule from persona YAML."""
    leader_sensor:    str
    follower_sensor:  str
    coupling_strength: float   # 0.0 (no effect) → 1.0 (full coupling)
    lag_seconds:      float    # simulated seconds of delay


@dataclass
class SensorRange:
    """Current phase min/max range for a sensor — used for normalisation."""
    min_value: float
    max_value: float

    @property
    def midpoint(self) -> float:
        """Midpoint of the range."""
        return (self.min_value + self.max_value) / 2.0

    @property
    def half_range(self) -> float:
        """Half the range width, guarded against zero-width ranges."""
        hw = (self.max_value - self.min_value) / 2.0
        return hw if hw > 0 else 1.0


# ---------------------------------------------------------------------------
# Lag buffer
# ---------------------------------------------------------------------------

class LagBuffer:
    """
    Time-indexed circular buffer holding recent (sim_time, normalised_value) pairs.

    Enables leader values to be retrieved at a specified lag in simulated seconds.
    Buffer size is set to accommodate the maximum lag defined for this leader.
    """

    def __init__(self, max_seconds: float = 300.0):
        """
        Args:
            max_seconds: Maximum history to retain in simulated seconds.
                         Set to max lag + safety margin at construction time.
        """
        self._buffer: collections.deque[tuple[float, float]] = collections.deque()
        self._max_seconds = max_seconds

    def record(self, sim_time: float, normalised_value: float) -> None:
        """Append a (sim_time, normalised_value) entry and evict stale entries."""
        self._buffer.append((sim_time, normalised_value))
        cutoff = sim_time - self._max_seconds
        while self._buffer and self._buffer[0][0] < cutoff:
            self._buffer.popleft()

    def get_lagged_value(self, sim_time: float, lag_seconds: float) -> Optional[float]:
        """
        Retrieve the normalised value recorded at approximately (sim_time - lag_seconds).

        Uses linear interpolation between bracketing entries for smooth output.

        Returns:
            Normalised value, or None if insufficient history exists.
        """
        target_time = sim_time - lag_seconds
        if not self._buffer:
            return None

        prev: Optional[tuple[float, float]] = None
        for t, v in self._buffer:
            if t >= target_time:
                if prev is None:
                    return v
                t0, v0 = prev
                t1, v1 = t, v
                if t1 == t0:
                    return v0
                alpha = (target_time - t0) / (t1 - t0)
                return v0 + alpha * (v1 - v0)
            prev = (t, v)

        return prev[1] if prev else None


# ---------------------------------------------------------------------------
# Correlation engine
# ---------------------------------------------------------------------------

class CorrelationEngine:
    """
    Manages all leader/follower sensor coupling for one persona.

    One CorrelationEngine is shared across all sensor generators for a
    running scenario. Leaders record their values each tick; followers
    query for their adjustment.

    Phase-aware: call update_phase_ranges() at each phase transition so
    normalisation is relative to the current phase's expected range.
    """

    def __init__(self) -> None:
        self._rules_by_follower: dict[str, list[CorrelationRule]] = {}
        self._lag_buffers:       dict[str, LagBuffer]             = {}
        self._phase_ranges:      dict[str, SensorRange]           = {}

    def load_rules(self, rules: list[dict]) -> None:
        """
        Load correlation rules from parsed persona YAML.

        Args:
            rules: List of dicts, each with keys:
                   leader_sensor, follower_sensor, coupling_strength, lag_seconds.
        """
        self._rules_by_follower.clear()
        self._lag_buffers.clear()

        for row in rules:
            rule = CorrelationRule(
                leader_sensor=     row["leader_sensor"],
                follower_sensor=   row["follower_sensor"],
                coupling_strength= float(row["coupling_strength"]),
                lag_seconds=       float(row["lag_seconds"]),
            )
            self._rules_by_follower.setdefault(rule.follower_sensor, []).append(rule)

            if rule.leader_sensor not in self._lag_buffers:
                # Size buffer to the maximum lag for this leader + margin
                max_lag = max(
                    float(r["lag_seconds"]) for r in rules
                    if r["leader_sensor"] == rule.leader_sensor
                )
                self._lag_buffers[rule.leader_sensor] = LagBuffer(
                    max_seconds=max_lag * 1.5 + 30.0
                )

        logger.info(
            "Correlation rules loaded",
            extra={
                "event":       "correlations_loaded",
                "leaders":     list(self._lag_buffers.keys()),
                "followers":   list(self._rules_by_follower.keys()),
                "total_rules": sum(len(v) for v in self._rules_by_follower.values()),
            },
        )

    def update_phase_ranges(self, ranges: dict[str, tuple[float, float]]) -> None:
        """
        Update sensor ranges for normalisation at phase transitions.

        Args:
            ranges: Dict mapping sensor_name → (min_value, max_value).
        """
        for sensor_name, (min_v, max_v) in ranges.items():
            self._phase_ranges[sensor_name] = SensorRange(min_value=min_v, max_value=max_v)

    def record_leader(self, sensor_name: str, value: float, sim_time_seconds: float) -> None:
        """
        Record a leader sensor's current value for future follower queries.

        Call once per tick for every sensor that is a leader in any rule,
        before querying followers in the same tick.
        """
        buf = self._lag_buffers.get(sensor_name)
        if buf is None:
            return

        sensor_range = self._phase_ranges.get(sensor_name)
        if sensor_range is None:
            return

        normalised = (value - sensor_range.midpoint) / sensor_range.half_range
        buf.record(sim_time_seconds, normalised)

    def get_adjustment(self, sensor_name: str, sim_time_seconds: float) -> float:
        """
        Calculate the total correlation adjustment for a follower sensor.

        Returns 0.0 if no correlation rules apply or insufficient history.
        """
        rules = self._rules_by_follower.get(sensor_name)
        if not rules:
            return 0.0

        follower_range = self._phase_ranges.get(sensor_name)
        if follower_range is None:
            return 0.0

        total = 0.0
        for rule in rules:
            buf = self._lag_buffers.get(rule.leader_sensor)
            if buf is None:
                continue
            lagged = buf.get_lagged_value(sim_time_seconds, rule.lag_seconds)
            if lagged is None:
                continue
            total += lagged * rule.coupling_strength * follower_range.half_range

        return total

    def is_leader(self, sensor_name: str) -> bool:
        """Return True if this sensor acts as a leader in any correlation rule."""
        return sensor_name in self._lag_buffers
