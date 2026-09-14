"""HOT/COLD state machine (spec sections 6, 7, 30, 38)."""

from hammertime.core.state.enums import IpState, PrefixState
from hammertime.core.state.machine import evaluate_ip_state

__all__ = ["IpState", "PrefixState", "evaluate_ip_state"]
