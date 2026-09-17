"""Per-IP attribute documents and the v1 `weight` function.

Spec: section 46.2, section 46.4

`weight` has exactly one implementation (ADR-0011 decision 6) so that the
aggregator which writes it and any consumer which verifies it compute the
same integer. The arithmetic is integral throughout: section 46.4 requires
replay to reproduce the value bit for bit, which floating point does not
guarantee across platforms.
"""

from collections.abc import Callable
from typing import Final

from hammertime.core.config.models import DetectionConfig
from hammertime.core.errors import ConfigurationError

#: Generation of the attribute registry (spec section 46.3) that documents
#: built here are written against. It changes only when the *meaning* of an
#: already-registered name changes, not when a name is added.
ATTRIBUTES_VERSION: Final = 1


def threshold_ratio(window_count: int, config: DetectionConfig) -> int:
    """Fixed-point hotness in thousandths of `hot_threshold` (section 46.4).

    ``clamp((1000 * window_count + hot_threshold // 2) // hot_threshold, 0,
    weight_max)``. The ``hot_threshold // 2`` term rounds half away from
    zero; the floor division keeps every step integral.

    A negative `window_count` is a programming error, never a value to
    clamp, so it raises `ValueError`.
    """

    if window_count < 0:
        raise ValueError(f"window_count must be >= 0, got {window_count}")
    ratio = (1000 * window_count + config.hot_threshold // 2) // config.hot_threshold
    return min(max(ratio, 0), config.weight_max)


#: The `weight_function` registry, keyed by the enum member named in
#: schemas/detection_config.v1.json. v1 has exactly one member.
_WEIGHT_FUNCTIONS: Final[dict[str, Callable[[int, DetectionConfig], int]]] = {
    "threshold_ratio": threshold_ratio,
}


def compute_weight(window_count: int, config: DetectionConfig) -> int:
    """Compute `weight` with the configured `weight_function` (section 34).

    `DetectionConfig` validates the enum, so an unknown name is unreachable
    through it -- but the dispatch refuses rather than silently defaulting,
    since defaulting would write a weight on a scale nobody configured.
    """

    weight_function = _WEIGHT_FUNCTIONS.get(config.weight_function)
    if weight_function is None:
        raise ConfigurationError(
            f"unknown weight_function {config.weight_function!r}, "
            f"expected one of {sorted(_WEIGHT_FUNCTIONS)}"
        )
    return weight_function(window_count, config)


def build_attributes(window_count: int, config: DetectionConfig) -> dict[str, object]:
    """Build the `IpAttributes` document attached at a transition.

    Spec section 46.2 defines the document; section 46.5 carries it on
    `HotIpAdded`. Returns a fresh dict on every call, so the caller owns it
    and may add experimental `x_` names to its own copy.
    """

    return {
        "attributes_version": ATTRIBUTES_VERSION,
        "weight": compute_weight(window_count, config),
    }
