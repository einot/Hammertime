"""Load, validate, and watch detection configuration.

Spec: section 34
"""

from __future__ import annotations


from pathlib import Path

from hammertime.core.config.models import DetectionConfig


def load(path: Path) -> DetectionConfig:
    """Read and validate a config document against schemas/detection_config.v1.json."""
    raise NotImplementedError


def watch(path: Path) -> object:
    """Yield a new DetectionConfig whenever the document changes on disk.

    A version bump MUST be accompanied by a controlled re-evaluation
    (services/aggregator/reevaluate.py); silently swapping thresholds leaves
    stale HOT/COLD state behind.
    """
    raise NotImplementedError
