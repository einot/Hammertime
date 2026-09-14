"""Separate individual-IP anomalies from distributed prefix anomalies.

Spec: section 15
"""

from __future__ import annotations


# One IP at 500k req/min is an aggressive scraper. A /24 with 94 hot IPs at 36.7%
# is a distributed network -- a materially stronger signal. Both levels of
# information are preserved rather than collapsed (spec section 15).
