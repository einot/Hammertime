"""Slab/arena allocation with integer node ids.

Spec: section 11, section 27
"""

from __future__ import annotations


# HOT/COLD oscillation causes allocation churn if nodes are freed eagerly
# (spec section 11). An arena with integer ids also keeps nodes contiguous and
# cache-friendly (section 27).
