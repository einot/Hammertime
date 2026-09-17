"""Executable spec invariants, used by tests and by the trie-inspect tool.

Spec: section 12, section 38
"""

from __future__ import annotations

from collections.abc import Iterable

from hammertime.core.config.models import DetectionConfig
from hammertime.core.state.enums import IpState

# One evaluation of the HOT/COLD state machine: the state held before the
# evaluation, the window count it was evaluated against, and the state it
# produced (spec section 6, section 30).
HysteresisStep = tuple[IpState, int, IpState]


def assert_hot_count_consistent(trie: object) -> None:
    """Recompute hot_count bottom-up and compare with stored values (section 12)."""
    raise NotImplementedError


def assert_no_negative_counts(trie: object) -> None:
    """hot_count >= 0 at every node (section 11)."""
    raise NotImplementedError


def assert_hysteresis_holds(history: Iterable[HysteresisStep], *, config: DetectionConfig) -> None:
    """No COLD->HOT below hot_threshold, no HOT->COLD at or above cold_threshold (section 38).

    ``history`` is a contiguous fold of the state machine: one
    ``(previous, count, current)`` step per evaluation, each step's
    ``previous`` being the preceding step's ``current``. Every step is
    checked against section 6's transition table and section 38's
    ``state == HOT => count >= cold_threshold`` invariant. Raises
    ``AssertionError`` naming the first offending step; returns ``None``
    when the whole history is consistent.
    """
    hot = config.hot_threshold
    cold = config.cold_threshold
    expected_previous: IpState | None = None
    for index, (previous, count, current) in enumerate(history):
        if count < 0:
            raise AssertionError(f"step {index}: negative count {count}")
        if expected_previous is not None and previous is not expected_previous:
            raise AssertionError(f"step {index}: {previous} does not follow {expected_previous}")
        if previous is IpState.COLD and current is IpState.HOT and count < hot:
            raise AssertionError(f"step {index}: COLD->HOT at {count} < hot_threshold {hot}")
        if previous is IpState.HOT and current is IpState.COLD and count >= cold:
            raise AssertionError(f"step {index}: HOT->COLD at {count} >= cold_threshold {cold}")
        if current is IpState.HOT and count < cold:
            raise AssertionError(f"step {index}: HOT with count {count} < cold_threshold {cold}")
        expected_previous = current
