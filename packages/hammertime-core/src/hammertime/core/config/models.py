"""Detection configuration.

Spec: section 34 (versioned configuration), section 6 (threshold ordering),
section 5 (window/bucket relationship), section 26 (retention).

Configuration is versioned because changing a threshold mass-transitions IP
state. Every emitted event carries the config_version that produced it so that
downstream consumers can tell a real change from a re-evaluation.
"""

from dataclasses import dataclass

from hammertime.core.errors import ConfigurationError

#: Mirrors schemas/detection_config.v1.json's weight_function enum. v1 has
#: exactly one member; adding another is an enum entry here plus one there
#: and a spec section 46 definition (ADR-0005).
WEIGHT_FUNCTIONS = frozenset({"threshold_ratio"})


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
    weight_function: str = "threshold_ratio"
    weight_max: int = 1_000_000

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
        if self.weight_function not in WEIGHT_FUNCTIONS:
            raise ConfigurationError(
                f"weight_function must be one of {sorted(WEIGHT_FUNCTIONS)}, "
                f"got {self.weight_function!r}"
            )
        if not 1000 <= self.weight_max <= 1_000_000:
            raise ConfigurationError("weight_max must be within [1000, 1000000]")

    @property
    def bucket_count(self) -> int:
        return self.window_seconds // self.bucket_seconds
