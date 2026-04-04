"""
Feature Drift Monitor — detects when live feature distributions
diverge from training-time reference ranges.

Maintains a rolling buffer of recent feature values and compares
against stored reference statistics (mean/std from training data).
Logs warnings when any feature drifts > 2 std from training reference.
"""

from __future__ import annotations

import logging
from collections import deque

logger = logging.getLogger(__name__)


class DriftMonitor:
    """Monitors feature distributions for drift from training reference.

    Args:
        reference_stats: Dict of feature_name -> {"mean": float, "std": float}
            from the training dataset. If None, monitoring is disabled.
        buffer_size: Number of recent predictions to keep for drift computation.
        alert_threshold_std: Number of standard deviations to trigger a warning.
    """

    def __init__(
        self,
        reference_stats: dict[str, dict[str, float]] | None = None,
        buffer_size: int = 50,
        alert_threshold_std: float = 2.0,
    ) -> None:
        self._reference = reference_stats or {}
        self._buffer_size = buffer_size
        self._threshold = alert_threshold_std
        self._buffers: dict[str, deque] = {
            name: deque(maxlen=buffer_size)
            for name in self._reference
        }
        self._alerted: set[str] = set()

    @property
    def enabled(self) -> bool:
        return len(self._reference) > 0

    def observe(self, features: dict[str, float]) -> list[str]:
        """Record a prediction's features and return any drift warnings.

        Returns list of warning messages (empty if no drift detected).
        """
        if not self._reference:
            return []

        warnings: list[str] = []

        for name, value in features.items():
            if name not in self._reference:
                continue

            self._buffers[name].append(value)
            buf = self._buffers[name]

            if len(buf) < max(10, self._buffer_size // 2):
                continue  # Not enough data yet

            ref = self._reference[name]
            ref_mean = ref["mean"]
            ref_std = ref["std"]

            if ref_std <= 0:
                continue

            live_mean = sum(buf) / len(buf)
            drift_z = abs(live_mean - ref_mean) / ref_std

            if drift_z > self._threshold:
                msg = (
                    f"Feature drift: {name} — live mean {live_mean:.4f} "
                    f"vs training {ref_mean:.4f} "
                    f"({drift_z:.1f} std, threshold {self._threshold})"
                )
                if name not in self._alerted:
                    logger.warning(msg)
                    self._alerted.add(name)
                warnings.append(msg)
            else:
                self._alerted.discard(name)

        return warnings

    def reset(self) -> None:
        """Clear buffers and alert state."""
        for buf in self._buffers.values():
            buf.clear()
        self._alerted.clear()
