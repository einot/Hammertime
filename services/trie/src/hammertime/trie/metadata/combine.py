"""Per-type combine() used to accumulate metadata along a lookup path.

Spec: section 16
"""

from __future__ import annotations


# effective_metadata(ip) = combine(root, /1, /2, ..., /32) along the path.
# Sets union, bitmasks OR, policies use priority/override. The trie MUST NOT
# assume everything merges by union (spec section 16).
