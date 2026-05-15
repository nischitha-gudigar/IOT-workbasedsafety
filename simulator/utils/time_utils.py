"""
simulator/utils/time_utils.py
-----------------------------
Time compression utilities for the Vitals Simulator.

The simulator runs scenarios in compressed time. A compression factor
of 60 means 1 real second = 60 simulated seconds (1 simulated minute
plays in 1 real second).

All time tracking inside the engine uses simulated minutes. This module
converts between real wall-clock time and simulated scenario time.
"""

from __future__ import annotations


class TimeCompressor:
    """
    Converts between real time and simulated scenario time.

    Compression factor is read from the scenario YAML at load time
    and can be overridden at runtime (e.g. from the demo API speed control).

    Args:
        compression_factor: How many simulated seconds pass per real second.
                            Default 1.0 means real-time (no compression).
    """

    def __init__(self, compression_factor: float = 1.0):
        self._factor = compression_factor

    @property
    def compression_factor(self) -> float:
        """Current compression factor."""
        return self._factor

    @compression_factor.setter
    def compression_factor(self, value: float) -> None:
        """Update the compression factor at runtime."""
        if value <= 0:
            raise ValueError(f"compression_factor must be > 0, got {value}")
        self._factor = value

    def real_to_sim_seconds(self, real_seconds: float) -> float:
        """Convert real elapsed seconds to simulated seconds."""
        return real_seconds * self._factor

    def real_to_sim_minutes(self, real_seconds: float) -> float:
        """Convert real elapsed seconds to simulated minutes."""
        return self.real_to_sim_seconds(real_seconds) / 60.0

    def sim_minutes_to_real_seconds(self, sim_minutes: float) -> float:
        """Convert simulated minutes to real seconds."""
        return (sim_minutes * 60.0) / self._factor

    def sampling_interval_real_seconds(self, interval_ms: int) -> float:
        """
        Convert a sensor sampling interval (in simulated milliseconds) to
        the real-world sleep duration.

        A sensor configured to sample every 1000ms simulated time should
        sleep for 1000ms / compression_factor in real time.
        """
        return (interval_ms / 1000.0) / self._factor

    def phase_progress(self, elapsed_sim_minutes: float, phase_duration_minutes: float) -> float:
        """
        Compute normalised progress through a phase [0.0, 1.0].

        Args:
            elapsed_sim_minutes:    Simulated minutes elapsed since phase start.
            phase_duration_minutes: Total duration of the phase in simulated minutes.

        Returns:
            Float clamped to [0.0, 1.0].
        """
        if phase_duration_minutes <= 0:
            return 1.0
        return max(0.0, min(1.0, elapsed_sim_minutes / phase_duration_minutes))
