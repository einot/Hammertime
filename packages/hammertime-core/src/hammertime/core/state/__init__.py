"""HOT/COLD state machine (spec sections 6, 7, 30, 38) and per-IP weight (46.4)."""

from hammertime.core.state.enums import IpState, PrefixState
from hammertime.core.state.machine import evaluate_ip_state
from hammertime.core.state.weight import compute_weight, threshold_ratio, transition_attributes

__all__ = [
    "IpState",
    "PrefixState",
    "compute_weight",
    "evaluate_ip_state",
    "threshold_ratio",
    "transition_attributes",
]
