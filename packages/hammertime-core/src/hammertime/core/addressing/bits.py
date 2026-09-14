"""Bit helpers shared by trie traversal and Patricia edge compression.

Spec: section 8, section 27
"""

from __future__ import annotations


def common_prefix_length(a: int, b: int, bit_length: int) -> int:
    """Length of the shared most-significant bit run between two addresses."""
    diff = (a ^ b) & ((1 << bit_length) - 1)
    if diff == 0:
        return bit_length
    return bit_length - diff.bit_length()


def bit_at(value: int, index: int, bit_length: int) -> int:
    """Bit `index` counting from the most-significant bit."""
    return (value >> (bit_length - 1 - index)) & 1
