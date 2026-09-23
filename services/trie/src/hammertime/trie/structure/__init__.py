"""Trie representation (spec sections 8, 9, 27; ADR-0014 decision 11).

`BinaryTrie` is the bit-by-bit reference, `PatriciaTrie` the arena-backed
production representation; both satisfy `HotTrie`, and `invariants` checks
either one.
"""

from hammertime.trie.structure.arena import NodeArena
from hammertime.trie.structure.binary_trie import BinaryTrie
from hammertime.trie.structure.invariants import (
    check_attribute_records,
    check_hot_counts,
    check_no_orphaned_nodes,
    check_patricia,
    check_trie,
)
from hammertime.trie.structure.node import (
    NO_NODE,
    HotTrie,
    NodeId,
    NodeView,
    PrefixCount,
    TrieNode,
)
from hammertime.trie.structure.patricia import PatriciaTrie

__all__ = [
    "NO_NODE",
    "BinaryTrie",
    "HotTrie",
    "NodeArena",
    "NodeId",
    "NodeView",
    "PatriciaTrie",
    "PrefixCount",
    "TrieNode",
    "check_attribute_records",
    "check_hot_counts",
    "check_no_orphaned_nodes",
    "check_patricia",
    "check_trie",
]
