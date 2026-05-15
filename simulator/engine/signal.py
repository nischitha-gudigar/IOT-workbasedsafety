"""
simulator/engine/signal.py
--------------------------
Signal behavior generators for the Vitals Simulator.

Each behavior type controls how a sensor's base value evolves within a
scenario phase as phase_progress advances from 0.0 → 1.0.

Five types are supported:

  oscillating_stable  — Value oscillates naturally around the range midpoint.
                        Models a healthy resting person with normal variation.

  monotonic_rising    — Value trends from min toward max as the phase progresses.
                        Models HR rising during fatigue, tachycardia onset,
                        or temperature climbing during heat stress.

  monotonic_falling   — Value trends from max toward min as the phase progresses.
                        Models recovery: HR dropping back to baseline,
                        SpO2 recovering after a low event.

  mean_reverting      — Value starts at the phase midpoint, can deviate, but
                        is pulled back toward the midpoint over time.
                        Models vitals that spike briefly then normalise —
                        e.g. HR after a startle reflex.

  step                — Value jumps immediately to phase_max at the start of
                        the phase and holds there with small noise.
                        Models sudden discrete events: alarm triggers,
                        sudden posture change.

Amplitude and jitter scale factors are read from simulator_config.yaml
via the config dict passed to create_signal_generator().
"""

from __future__ import annotations

import math
import random
from abc import ABC, abstractmethod
from typing import Any


# ---------------------------------------------------------------------------
# Abstract base
# ---------------------------------------------------------------------------

class SignalGenerator(ABC):
    """Abstract base for all signal behavior generators."""

    @abstractmethod
    def generate(
        self,
        phase_min:      float,
        phase_max:      float,
        phase_progress: float,
    ) -> float:
        """
        Generate one base signal value for the current tick.

        Args:
            phase_min:      Minimum value for this sensor in this phase.
            phase_max:      Maximum value for this sensor in this phase.
            phase_progress: Normalised phase completion [0.0, 1.0].

        Returns:
            Base value before noise and correlation are applied.
        """
        ...

    def reset(self) -> None:
        """Reset any internal state at phase transitions."""
        pass


# ---------------------------------------------------------------------------
# Oscillating Stable
# ---------------------------------------------------------------------------

class OscillatingStableGenerator(SignalGenerator):
    """
    Value oscillates naturally around the range midpoint.

    Amplitude is a fixed fraction of the range — configurable via
    oscillating_amplitude_fraction in simulator_config.yaml.

    Models healthy resting vitals (HR 62–78 bpm bouncing around 70).
    """

    def __init__(self, amplitude_fraction: float = 0.05):
        """
        Args:
            amplitude_fraction: Oscillation amplitude as a fraction of range width.
                                Configurable. Default 0.05 = ±5% of range.
        """
        self._amplitude_fraction = amplitude_fraction

    def generate(self, phase_min: float, phase_max: float, phase_progress: float) -> float:
        range_width = phase_max - phase_min
        midpoint    = (phase_min + phase_max) / 2.0
        amplitude   = self._amplitude_fraction * range_width
        return midpoint + random.uniform(-amplitude, amplitude)


# ---------------------------------------------------------------------------
# Monotonic Rising
# ---------------------------------------------------------------------------

class MonotonicRisingGenerator(SignalGenerator):
    """
    Value trends linearly from phase_min toward phase_max as the phase progresses.

    A small jitter is added around the trend line so the signal looks
    biological rather than mechanical. Jitter scale is configurable.

    Models: HR rising during exertion, temperature climbing, SpO2 dropping.
    """

    def __init__(self, jitter_fraction: float = 0.01):
        """
        Args:
            jitter_fraction: Local random jitter as a fraction of range width.
        """
        self._jitter_fraction = jitter_fraction

    def generate(self, phase_min: float, phase_max: float, phase_progress: float) -> float:
        range_width  = phase_max - phase_min
        trend_value  = phase_min + range_width * phase_progress
        jitter       = random.uniform(-self._jitter_fraction, self._jitter_fraction) * range_width
        return trend_value + jitter


# ---------------------------------------------------------------------------
# Monotonic Falling
# ---------------------------------------------------------------------------

class MonotonicFallingGenerator(SignalGenerator):
    """
    Value trends linearly from phase_max toward phase_min as the phase progresses.

    Models: HR recovering after exertion, SpO2 returning to normal,
    body temperature falling after cooling down.
    """

    def __init__(self, jitter_fraction: float = 0.01):
        self._jitter_fraction = jitter_fraction

    def generate(self, phase_min: float, phase_max: float, phase_progress: float) -> float:
        range_width  = phase_max - phase_min
        trend_value  = phase_max - range_width * phase_progress
        jitter       = random.uniform(-self._jitter_fraction, self._jitter_fraction) * range_width
        return trend_value + jitter


# ---------------------------------------------------------------------------
# Mean Reverting
# ---------------------------------------------------------------------------

class MeanRevertingGenerator(SignalGenerator):
    """
    Value drifts around but is continuously pulled back toward the midpoint.

    Uses an Ornstein-Uhlenbeck-like process:
        next = current + reversion_speed * (midpoint - current) + random_step

    Parameters are configurable:
        reversion_speed:  How strongly the value is pulled toward midpoint (0–1).
        volatility:       Size of random steps relative to range width.

    Models: HRV fluctuations, SpO2 micro-oscillations, skin temp hovering
    near normal with occasional small deviations.
    """

    def __init__(self, reversion_speed: float = 0.15, volatility: float = 0.03):
        """
        Args:
            reversion_speed: Strength of pull toward midpoint per tick.
            volatility:      Random step size as a fraction of range width.
        """
        self._reversion_speed = reversion_speed
        self._volatility      = volatility
        self._current: float | None = None

    def generate(self, phase_min: float, phase_max: float, phase_progress: float) -> float:
        range_width = phase_max - phase_min
        midpoint    = (phase_min + phase_max) / 2.0

        if self._current is None:
            self._current = midpoint

        # Pull toward midpoint
        reversion  = self._reversion_speed * (midpoint - self._current)
        noise_step = random.gauss(0.0, self._volatility * range_width)
        self._current = self._current + reversion + noise_step

        # Soft clamp: discourage exceeding bounds but don't hard-clip
        if self._current < phase_min:
            self._current = phase_min + abs(self._current - phase_min) * 0.1
        if self._current > phase_max:
            self._current = phase_max - abs(self._current - phase_max) * 0.1

        return self._current

    def reset(self) -> None:
        """Reset internal state at phase transitions."""
        self._current = None


# ---------------------------------------------------------------------------
# Step
# ---------------------------------------------------------------------------

class StepGenerator(SignalGenerator):
    """
    Value jumps to phase_max immediately and holds with small jitter.

    Models sudden discrete events: abrupt posture change, sudden
    tachycardia onset, alarm condition.

    The jitter_fraction keeps the value from looking like a flat line.
    """

    def __init__(self, jitter_fraction: float = 0.005):
        self._jitter_fraction = jitter_fraction

    def generate(self, phase_min: float, phase_max: float, phase_progress: float) -> float:
        range_width = phase_max - phase_min
        jitter      = random.uniform(-self._jitter_fraction, self._jitter_fraction) * range_width
        return phase_max + jitter


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------

_SIGNAL_REGISTRY: dict[str, type[SignalGenerator]] = {
    "oscillating_stable": OscillatingStableGenerator,
    "monotonic_rising":   MonotonicRisingGenerator,
    "monotonic_falling":  MonotonicFallingGenerator,
    "mean_reverting":     MeanRevertingGenerator,
    "step":               StepGenerator,
}


def create_signal_generator(
    behavior_type: str,
    params: dict[str, Any] | None = None,
) -> SignalGenerator:
    """
    Factory — creates the correct SignalGenerator by behavior type name.

    Args:
        behavior_type: One of the keys in _SIGNAL_REGISTRY.
        params:        Optional dict of constructor kwargs loaded from
                       simulator_config.yaml signal_params section.

    Returns:
        Instantiated SignalGenerator.

    Raises:
        ValueError: If behavior_type is not recognised.
    """
    cls = _SIGNAL_REGISTRY.get(behavior_type)
    if cls is None:
        raise ValueError(
            f"Unknown signal behavior '{behavior_type}'. "
            f"Valid options: {list(_SIGNAL_REGISTRY.keys())}"
        )
    return cls(**(params or {}))


def available_behaviors() -> list[str]:
    """Return list of all registered behavior type names."""
    return list(_SIGNAL_REGISTRY.keys())
