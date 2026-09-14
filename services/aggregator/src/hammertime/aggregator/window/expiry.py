"""Evict IPs with no observations inside state_retention_seconds.

Spec: section 26
"""

from __future__ import annotations


# The window store holds far more IPs than the trie: every active IP versus only
# currently hot ones. Retention (default 10 minutes for a 5 minute window) is
# what keeps the store bounded (spec section 26).
