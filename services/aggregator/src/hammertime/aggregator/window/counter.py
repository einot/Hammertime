"""Per-IP ring of buckets with a maintained running total.

Spec: section 5
"""

from __future__ import annotations


# window = 300s, bucket = 10s -> 30 slots.
#   expire slot:  total -= slot.count; slot.count = 0
#   observe:      total += delta
# window_count(ip) is O(1) after maintenance; never sum the ring on read
# (spec section 5).
