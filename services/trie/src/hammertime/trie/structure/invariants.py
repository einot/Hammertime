"""Runtime invariant checks, cheap enough for debug builds and the inspect tool.

Spec: section 11, section 12
"""

from __future__ import annotations


# hot_count(node) == number of currently HOT /32 addresses in its subtree, and
# hot_count(node) == hot_count(child[0]) + hot_count(child[1]).
# This invariant matters more than any cached prefix_state (spec section 12).
