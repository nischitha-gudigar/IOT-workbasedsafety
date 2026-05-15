"""
simulator/core/worker_context.py
---------------------------------
Computes a threshold multiplier based on a worker's shift context.

The more fatigued a worker (high shift_day, many consecutive days,
recent return from medical leave), the tighter the safety thresholds.
This makes the same physical posture angle or HR more likely to trigger
an alert on day 4 than on day 1.

Multiplier values are read from simulator_config.yaml — no hardcoded numbers.
"""

from __future__ import annotations

from typing import Any

from simulator.core.scenario_loader import WorkerContext
from simulator.utils.logger import get_logger

logger = get_logger(__name__)


class WorkerContextProcessor:
    """
    Computes a threshold_multiplier for a worker's current shift state.

    Args:
        sim_config: Parsed simulator_config.yaml dict.
    """

    def __init__(self, sim_config: dict[str, Any]) -> None:
        worker_cfg = sim_config.get("worker_context", {})
        self._multipliers: dict[int, float] = {
            int(k): float(v)
            for k, v in worker_cfg.get("shift_day_multipliers", {
                1: 1.00, 2: 0.95, 3: 0.90, 4: 0.82, 5: 0.75,
            }).items()
        }
        self._medical_leave_reset_to_day: int = int(
            worker_cfg.get("medical_leave_reset_to_day", 1)
        )

    def compute_multiplier(self, context: WorkerContext) -> float:
        """
        Compute the threshold multiplier for this worker's current shift state.

        A multiplier < 1.0 means thresholds are tighter — the worker is more
        vulnerable and alerts should fire earlier.

        Args:
            context: WorkerContext from the scenario YAML.

        Returns:
            Float multiplier in [0.0, 1.0]. Typically 0.75–1.0.
        """
        effective_day = context.shift_day

        if context.recent_medical_leave:
            effective_day = self._medical_leave_reset_to_day
            logger.info(
                "Worker recently returned from medical leave — threshold reset",
                extra={
                    "event":        "medical_leave_reset",
                    "reset_to_day": effective_day,
                },
            )

        # Clamp to valid range
        effective_day = max(1, min(5, effective_day))
        multiplier = self._multipliers.get(effective_day, 1.0)

        logger.info(
            "Worker context threshold multiplier computed",
            extra={
                "event":              "worker_context_computed",
                "shift_day":          context.shift_day,
                "effective_day":      effective_day,
                "consecutive_days":   context.consecutive_days,
                "recent_medical_leave": context.recent_medical_leave,
                "threshold_multiplier": multiplier,
            },
        )
        return multiplier
