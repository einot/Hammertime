"""Metric definitions from spec section 37, declared once and shared by all services.

Spec: section 37
"""

from __future__ import annotations


# Ingestion:  observations_received, observations_rejected,
#             duplicate_messages, late_messages
# Window:     tracked_ips, active_ips, hot_ips,
#             cold_to_hot_transitions, hot_to_cold_transitions
# Trie:       trie_nodes, hot_ip_count, prefix_queries, trie_updates
# Detection:  hot_prefixes, bot_network_candidates, classification_changes
# Latency:    observation_to_hot_transition_latency,
#             hot_transition_to_prefix_update_latency
