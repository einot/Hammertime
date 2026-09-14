"""Transition value object emitted when evaluate_ip_state changes the state.

Spec: section 19, section 30
"""

from __future__ import annotations


from dataclasses import dataclass

from hammertime.core.state.enums import IpState


@dataclass(frozen=True, slots=True)
class StateTransition:
    """A COLD->HOT or HOT->COLD edge. Non-transitions are never represented."""

    previous: IpState
    current: IpState

    @property
    def became_hot(self) -> bool:
        return self.previous is IpState.COLD and self.current is IpState.HOT

    @property
    def became_cold(self) -> bool:
        return self.previous is IpState.HOT and self.current is IpState.COLD
