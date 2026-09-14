"""Reference bit-by-bit trie: add_hot_ip, remove_hot_ip, lookup, longest match.

Spec: section 10, section 11, section 39
"""

from __future__ import annotations


# add_hot_ip:    walk the address bits, node.hot_count += 1 at every visited node
# remove_hot_ip: same path, node.hot_count -= 1, then optionally prune
#
# Simple and obviously correct. It is the oracle the Patricia implementation is
# differentially tested against.
