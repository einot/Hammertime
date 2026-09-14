"""Event contracts crossing service boundaries (spec section 19)."""

from hammertime.core.events.models import (
    HotIpAdded,
    HotIpRemoved,
    PrefixStatsChanged,
    RequestObservation,
)

__all__ = ["HotIpAdded", "HotIpRemoved", "PrefixStatsChanged", "RequestObservation"]
