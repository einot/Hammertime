"""Event contracts crossing service boundaries (spec section 19)."""

from hammertime.core.events.codec import decode, encode
from hammertime.core.events.envelope import EventEnvelope, compute_event_id
from hammertime.core.events.models import (
    HotIpAdded,
    HotIpRemoved,
    PrefixStatsChanged,
    RequestObservation,
)

__all__ = [
    "EventEnvelope",
    "HotIpAdded",
    "HotIpRemoved",
    "PrefixStatsChanged",
    "RequestObservation",
    "compute_event_id",
    "decode",
    "encode",
]
