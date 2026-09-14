"""Binary IP trie: the hot-IP density index.

Spec: sections 8-12, section 27, section 28, section 29, section 33.

Holds no request counts. It maintains the set of currently hot addresses and the
hot_count aggregate at every prefix level. A single state transition touches at
most 32 nodes for IPv4 (128 for IPv6), which is what makes hierarchical density
maintenance effectively constant-time per transition (section 41).
"""
