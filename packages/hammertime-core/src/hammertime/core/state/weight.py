"""The per-IP `weight` attribute produced at a HOT/COLD transition.

Spec: section 46.4 (the `threshold_ratio` formula and the `weight_max` clamp),
section 46.2/46.3 (the attribute document and the registered `weight` name),
section 34 (`weight_function` and `weight_max` as configuration), ADR-0005,
ADR-0011 decision 4.

The arithmetic is integer throughout. Spec section 46.4 requires replay to
reproduce the same value bit for bit, which floating point does not guarantee
across platforms, and `weight` travels on the wire as a JSON integer
(schemas/ip_attributes.v1.json) -- a float here would be a wire-format change,
not an internal detail.

Adding `hot_threshold // 2` to the numerator before the floor division is what
makes the rounding half-up in thousandths of `hot_threshold`, without ever
leaving the integers.
"""

from hammertime.core.config.models import DetectionConfig
from hammertime.core.errors import ConfigurationError

#: Generation of the spec section 46.3 attribute registry this module writes
#: against. It is the registry's version, NOT `DetectionConfig.config_version`:
#: adding a registered name leaves it at 1, only a change to the *meaning* of an
#: existing name moves it (spec section 46.2).
ATTRIBUTES_VERSION = 1


def threshold_ratio(window_count: int, config: DetectionConfig) -> int:
    """Return the v1 weight: `window_count` in thousandths of `hot_threshold`.

    Spec section 46.4::

        clamp(
            (1000 * window_count + config.hot_threshold // 2) // config.hot_threshold,
            0,
            config.weight_max,
        )

    So a count exactly at `hot_threshold` is 1000 and 2.5x it is 2500, whatever
    the configured threshold happens to be.
    """
    ratio = (1000 * window_count + config.hot_threshold // 2) // config.hot_threshold
    return min(max(ratio, 0), config.weight_max)


def compute_weight(window_count: int, config: DetectionConfig) -> int:
    """Dispatch on `config.weight_function`; v1 knows only `threshold_ratio`.

    `DetectionConfig` rejects any other value at construction (spec section 34),
    so the fallback is unreachable for a validly built config; it raises rather
    than silently returning a weight on the wrong scale should a future enum
    member be added to the config without a function to match.
    """
    if config.weight_function == "threshold_ratio":
        return threshold_ratio(window_count, config)
    raise ConfigurationError(f"unknown weight_function: {config.weight_function!r}")


def transition_attributes(window_count: int, config: DetectionConfig) -> dict[str, object]:
    """Return the attribute document to attach to a transition event.

    Exactly the registry generation plus the weight (ADR-0011 decision 4). A
    weight of 0 is a value, not an absence, so the key is always present: spec
    section 46.2's "absent attributes is equivalent to `{"attributes_version":
    1}`" must not be reachable by dropping a falsy weight.
    """
    return {
        "attributes_version": ATTRIBUTES_VERSION,
        "weight": compute_weight(window_count, config),
    }
