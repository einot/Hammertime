"""Prefix classification and scoring.

Spec: section 13, section 14, section 15, section 31, section 44.

Separate from trie maintenance on purpose (section 14): the trie keeps counts
exact and cheap, the detector decides what those counts mean and may grow
arbitrarily complex without touching the hot path.
"""
