"""Executable spec invariants, used by tests and by the trie-inspect tool.

Spec: section 12, section 38
"""

from __future__ import annotations


def assert_hot_count_consistent(trie: object) -> None:
    """Recompute hot_count bottom-up and compare with stored values (section 12)."""
    raise NotImplementedError


def assert_no_negative_counts(trie: object) -> None:
    """hot_count >= 0 at every node (section 11)."""
    raise NotImplementedError


def assert_hysteresis_holds(history: object) -> None:
    """No COLD->HOT below hot_threshold, no HOT->COLD at or above cold_threshold (section 38)."""
    raise NotImplementedError
