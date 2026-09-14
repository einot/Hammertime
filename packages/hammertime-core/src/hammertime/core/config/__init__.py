"""Versioned detection configuration (spec section 34)."""

from hammertime.core.config.loader import load, watch
from hammertime.core.config.models import DetectionConfig

__all__ = ["DetectionConfig", "load", "watch"]
