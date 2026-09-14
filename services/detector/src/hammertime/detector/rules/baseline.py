"""The v1 rule: hot_count >= minimum_hot_ips AND hot_ratio >= minimum_hot_ratio.

Spec: section 13, section 38
"""

from __future__ import annotations


# Both conditions are required. A /32 with hot_count = 1 has ratio 1.0 and must
# not qualify -- that is what the minimum_hot_ips floor is for (spec section 13).
