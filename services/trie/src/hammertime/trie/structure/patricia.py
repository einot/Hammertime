"""Path-compressed trie: the production representation.

Spec: section 27
"""

from __future__ import annotations


# A run of single-child nodes becomes one compressed edge, so a sparse /32 does
# not cost 32 nodes. The logical model MUST stay equivalent to the binary trie
# (spec section 27) -- that equivalence is enforced by differential tests.
