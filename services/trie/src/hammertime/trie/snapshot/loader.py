"""Load newest snapshot, then replay events after its sequence number.

Spec: section 32, section 33
"""

from __future__ import annotations


# The trie is derived state. Given the hot-IP event log it can always be rebuilt
# from empty; snapshots exist only to bound startup time (spec section 32).
