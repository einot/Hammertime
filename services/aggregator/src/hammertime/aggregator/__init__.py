"""Sliding-window aggregation and HOT/COLD transitions.

Spec: section 5, section 6, section 18, section 20, section 24, section 26.

Owns per-IP state: the bucketed counter, the running total, and the current
HOT/COLD state. Emits HotIpAdded / HotIpRemoved -- and nothing else. It knows
nothing about prefixes; that separation is the scalability boundary (section 18).
"""
