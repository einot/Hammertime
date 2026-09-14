"""Consistent hashing: hash(ip) -> shard. One owner per IP.

Spec: section 20
"""

from __future__ import annotations


# The owning shard holds the sliding counter, the HOT/COLD state, and issues the
# trie update for that IP. This is what keeps per-IP state free of distributed
# coordination (spec section 20).
