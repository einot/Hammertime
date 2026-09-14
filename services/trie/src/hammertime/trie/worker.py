"""Single-writer event loop applying HotIpAdded / HotIpRemoved.

Spec: section 28
"""

from __future__ import annotations


# Single-writer ownership is the chosen atomicity mechanism (spec section 28):
# updates are small, deterministic, and 32 steps long. Readers must never observe
# a partially updated path, so the writer publishes a versioned snapshot pointer
# rather than mutating structure readers are walking.
