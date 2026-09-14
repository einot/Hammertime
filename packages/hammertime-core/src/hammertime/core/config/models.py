"""Detection configuration.

Spec: section 34 (versioned configuration), section 6 (threshold ordering),
section 5 (window/bucket relationship), section 26 (retention).

Configuration is versioned because changing a threshold mass-transitions IP
state. Every emitted event carries the config_version that produced it so that
downstream consumers can tell a real change from a re-evaluation.
"""

from __future__ import annotations

from dataclasses import dataclass

from hammertime.core.errors import ConfigurationError


@dataclass(frozen=True, slots=True)
class DetectionConfig:
    config_version: int = 1
    window_seconds: int = 300
    bucket_seconds: int = 10
    hot_threshold: int = 1000
    cold_threshold: int = 800
    minimum_hot_ips: int = 16
    minimum_hot_ratio: float = 0.10
    allowed_lateness_seconds: int = 30
    state_retention_seconds: int = 600

    def __post_init__(self) -> None:
        if self.config_version < 1:
            raise ConfigurationError("config_version must be >= 1")
        if self.cold_threshold >= self.hot_threshold:
            raise ConfigurationError("cold_threshold must be strictly below hot_threshold")
        if self.window_seconds % self.bucket_seconds:
            raise ConfigurationError("window_seconds must be a whole multiple of bucket_seconds")
        if self.state_retention_seconds < self.window_seconds:
            raise ConfigurationError("state_retention_seconds must cover at least one window")
        if not 0.0 <= self.minimum_hot_ratio <= 1.0:
            raise ConfigurationError("minimum_hot_ratio must be within [0, 1]")

    @property
    def bucket_count(self) -> int:
        return self.window_seconds // self.bucket_seconds
