"""Node layout: child[0], child[1], hot_count, local_metadata, prefix_state.

Spec: section 9
"""

from __future__ import annotations


# Distinguish stored state from derived state (spec section 9). hot_count is
# stored and is the load-bearing value; prefix_state is a cache that can always
# be recomputed from hot_count, prefix_length, and configuration (section 12).
