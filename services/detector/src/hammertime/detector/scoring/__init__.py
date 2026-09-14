"""Multi-dimensional scoring beyond count and ratio.

Spec: section 14, section 44
"""

from __future__ import annotations


# Signals: total requests, requests per hot IP, persistence over time, rate of
# new hot IPs, rate of disappearance, ASN/geo, agent agreement, known CDN space,
# historical behaviour. Kept strictly out of trie maintenance (spec section 14).
