"""Shared domain model for Hammertime.

Nothing in this package talks to a network, a broker, or a store. It holds the
vocabulary every service agrees on: addresses and prefixes, the event schema,
versioned configuration, time-bucket arithmetic, and the single authoritative
HOT/COLD state machine.
"""

from hammertime.core.state.enums import IpState, PrefixState
from hammertime.core.state.machine import evaluate_ip_state

__all__ = ["IpState", "PrefixState", "evaluate_ip_state"]
