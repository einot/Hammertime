"""Metadata attached to the prefix it was declared on -- never copied downward.

Spec: section 16, section 17
"""

from __future__ import annotations


# 10.0.0.0/8 -> {"internal"} is stored once on the /8 node. Materializing it into
# every descendant would mean an unbounded update fan-out (spec section 17).
