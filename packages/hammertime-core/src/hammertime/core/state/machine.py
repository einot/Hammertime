"""The authoritative HOT/COLD state machine.

Spec: section 6, section 7 (hysteresis), section 30, section 38 (invariants).

This function is the ONLY place the threshold comparison is written. Ingestion,
replay, snapshot re-evaluation, and tests all call it, so threshold semantics
cannot drift between paths (spec section 30).

The asymmetry is deliberate. A single `count >= threshold` comparison for both
directions would destroy the hysteresis and let normal statistical noise drive
COLD -> HOT -> COLD oscillation (spec section 6).

    COLD, count >= hot_threshold   -> HOT
    HOT,  count <  cold_threshold  -> COLD
    otherwise                      -> unchanged
"""

from __future__ import annotations

from hammertime.core.config.models import DetectionConfig
from hammertime.core.state.enums import IpState


def evaluate_ip_state(
    previous_state: IpState,
    count: int,
    config: DetectionConfig,
) -> IpState:
    """Return the state an IP should hold given its sliding-window count."""
    if previous_state is IpState.COLD:
        return IpState.HOT if count >= config.hot_threshold else IpState.COLD
    if count < config.cold_threshold:
        return IpState.COLD
    return IpState.HOT
