"""Prefix metadata and per-IP attribute records (spec sections 16, 17, 46).

Spec: section 3, section 12, section 16, section 17, section 46; ADR-0015
decision 7.

Re-exports the three modules' names: `combine` (the section 16 value kinds and
their combine functions, and the upward `PrefixStats` view), `local` (the
prefix-keyed `PrefixMetadataStore`) and `ip_attributes` (the section 46 record
map and the coupled single-writer step). None of them imports this package,
and nothing in `hammertime.trie.structure` imports any of them.
"""

from hammertime.trie.metadata.combine import (
    EMPTY_METADATA,
    Bitmask,
    Metadata,
    MetadataValue,
    Override,
    PrefixStats,
    Tags,
    aggregate,
    ancestor_stats,
    combine,
    combine_path,
    combine_values,
    prefix_stats,
)
from hammertime.trie.metadata.ip_attributes import (
    DEFAULT_ATTRIBUTES,
    IpAttributeRecords,
    IpAttributes,
    apply_hot_ip_added,
    apply_hot_ip_removed,
)
from hammertime.trie.metadata.local import PrefixMetadataStore

__all__ = [
    "DEFAULT_ATTRIBUTES",
    "EMPTY_METADATA",
    "Bitmask",
    "IpAttributeRecords",
    "IpAttributes",
    "Metadata",
    "MetadataValue",
    "Override",
    "PrefixMetadataStore",
    "PrefixStats",
    "Tags",
    "aggregate",
    "ancestor_stats",
    "apply_hot_ip_added",
    "apply_hot_ip_removed",
    "combine",
    "combine_path",
    "combine_values",
    "prefix_stats",
]
