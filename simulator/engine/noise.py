"""
simulator/engine/noise.py
-------------------------
Noise model implementations for the Vitals Simulator.

Each model transforms a clean base signal value to produce realistic
wearable sensor output.

Four models are supported:

  gaussian     — Zero-mean white noise. Models electronic/thermal sensor noise
                 in a stable wearable. Most common for healthy resting vitals.

  drift        — Random walk accumulated offset. Models calibration drift in
                 skin temperature sensors, slow systematic sensor bias.

  burst        — Occasional large transients. Models motion artifacts —
                 sudden limb movement corrupts the PPG or IMU reading.

  quantization — Stepwise output. Models low-resolution sensors reporting
                 in whole numbers (e.g. pulse oximeter rounds to 1% SpO2).

All intensity values are dimensionless fractions. Their meaning relative
to range_width is documented per class. Intensity values are read from
simulator_config.yaml noise_intensity_map, not hardcoded here.
"""

from __future__ import annotations

import random
from abc import ABC, abstractmethod
from typing import Any


# ---------------------------------------------------------------------------
# Abstract base
# ---------------------------------------------------------------------------

class NoiseModel(ABC):
    """Abstract base for all noise models."""

    def __init__(self, intensity: float):
        """
        Args:
            intensity: Dimensionless noise scale factor (0.0–1.0 typically).
        """
        self.intensity = intensity

    @abstractmethod
    def apply(self, base_value: float, range_width: float) -> float:
        """
        Apply noise to a clean base signal value.

        Args:
            base_value:  Clean signal value before noise.
            range_width: The sensor's (phase_max - phase_min).
                         Used to scale noise relative to expected range.

        Returns:
            Noisy value. May slightly exceed phase min/max — intentional,
            real sensors occasionally read outside expected ranges.
        """
        ...

    def reset(self) -> None:
        """Reset any stateful accumulator. Called at phase transitions."""
        pass


# ---------------------------------------------------------------------------
# Gaussian
# ---------------------------------------------------------------------------

class GaussianNoise(NoiseModel):
    """
    Zero-mean Gaussian white noise.

    sigma = intensity * range_width

    Models:
      - Electronic noise in the wearable ADC
      - Thermal noise in sensor electronics
      - High-frequency measurement jitter in PPG/ECG sensors

    Appropriate for: resting HR, SpO2 in stable conditions.
    """

    def apply(self, base_value: float, range_width: float) -> float:
        sigma = self.intensity * range_width
        return base_value + random.gauss(0.0, sigma)


# ---------------------------------------------------------------------------
# Drift
# ---------------------------------------------------------------------------

class DriftNoise(NoiseModel):
    """
    Accumulated random walk drift with weak mean reversion.

    Each call adds a small random step to an accumulator. The accumulator
    reverts toward zero at a configurable rate to prevent unbounded runaway.
    Cap is applied at ±max_drift_fraction of range_width.

    Models:
      - Skin temperature sensor calibration drift over a long shift
      - Slow bias from skin contact pressure changes on a wearable

    Appropriate for: skin_temperature during sustained phases.

    Configurable via params:
        step_sigma_fraction:  Sigma of each random step (fraction of range).
        reversion_rate:       Mean-reversion pull strength per tick (0–1).
        max_drift_fraction:   Cap on accumulated drift (fraction of range).
    """

    def __init__(
        self,
        intensity:            float,
        step_sigma_fraction:  float = 0.1,
        reversion_rate:       float = 0.02,
        max_drift_fraction:   float = 0.25,
    ):
        super().__init__(intensity)
        self._step_sigma_fraction = step_sigma_fraction
        self._reversion_rate      = reversion_rate
        self._max_drift_fraction  = max_drift_fraction
        self._accumulated: float  = 0.0

    def apply(self, base_value: float, range_width: float) -> float:
        step_sigma       = self.intensity * range_width * self._step_sigma_fraction
        step             = random.gauss(0.0, step_sigma)
        self._accumulated = self._accumulated * (1.0 - self._reversion_rate) + step
        max_drift         = range_width * self._max_drift_fraction
        self._accumulated = max(-max_drift, min(max_drift, self._accumulated))
        return base_value + self._accumulated

    def reset(self) -> None:
        """Reset accumulated drift at phase transitions."""
        self._accumulated = 0.0


# ---------------------------------------------------------------------------
# Burst
# ---------------------------------------------------------------------------

class BurstNoise(NoiseModel):
    """
    Intermittent large-amplitude transients (motion artifacts).

    With a small probability each tick, a large additive spike is injected.
    Spike magnitude and burst probability scale with intensity.

    Models:
      - Motion artifact in PPG when patient moves their wrist
      - IMU spike from sudden limb movement
      - ECG baseline wander from electrode movement

    Configurable via params:
        burst_scale_min: Minimum spike magnitude (multiple of range_width).
        burst_scale_max: Maximum spike magnitude (multiple of range_width).
        background_sigma_fraction: Background gaussian sigma when no burst.
    """

    def __init__(
        self,
        intensity:                 float,
        burst_scale_min:           float = 0.5,
        burst_scale_max:           float = 2.0,
        background_sigma_fraction: float = 0.05,
    ):
        super().__init__(intensity)
        self._burst_scale_min           = burst_scale_min
        self._burst_scale_max           = burst_scale_max
        self._background_sigma_fraction = background_sigma_fraction
        # Burst probability scales directly with intensity
        self._burst_probability = min(intensity, 0.5)

    def apply(self, base_value: float, range_width: float) -> float:
        if random.random() < self._burst_probability:
            sign      = random.choice([-1, 1])
            magnitude = random.uniform(self._burst_scale_min, self._burst_scale_max)
            return base_value + sign * magnitude * range_width
        sigma = self.intensity * range_width * self._background_sigma_fraction
        return base_value + random.gauss(0.0, sigma)


# ---------------------------------------------------------------------------
# Quantization
# ---------------------------------------------------------------------------

class QuantizationNoise(NoiseModel):
    """
    Stepwise discrete output — rounds to a resolution grid.

    resolution = intensity * range_width

    Models:
      - Pulse oximeter reporting SpO2 in whole percent steps
      - Low-resolution ADC in a low-cost wearable

    Higher intensity → coarser resolution → more visible steps.
    """

    def apply(self, base_value: float, range_width: float) -> float:
        resolution = max(self.intensity * range_width, 1e-9)
        return round(base_value / resolution) * resolution


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------

_NOISE_REGISTRY: dict[str, type[NoiseModel]] = {
    "gaussian":     GaussianNoise,
    "drift":        DriftNoise,
    "burst":        BurstNoise,
    "quantization": QuantizationNoise,
}


def create_noise_model(
    noise_type: str,
    intensity:  float,
    params: dict[str, Any] | None = None,
) -> NoiseModel:
    """
    Factory — creates the correct NoiseModel by name.

    Args:
        noise_type: One of 'gaussian', 'drift', 'burst', 'quantization'.
        intensity:  Noise intensity factor, typically resolved from
                    simulator_config.yaml noise_intensity_map by the
                    condition_mapper before calling this.
        params:     Optional extra constructor kwargs from simulator_config.yaml
                    noise_params section (e.g. drift reversion_rate).

    Returns:
        Instantiated NoiseModel subclass.

    Raises:
        ValueError: If noise_type is not recognised.
    """
    cls = _NOISE_REGISTRY.get(noise_type)
    if cls is None:
        raise ValueError(
            f"Unknown noise model '{noise_type}'. "
            f"Valid options: {list(_NOISE_REGISTRY.keys())}"
        )
    return cls(intensity=intensity, **(params or {}))


def available_noise_models() -> list[str]:
    """Return list of all registered noise model names."""
    return list(_NOISE_REGISTRY.keys())
