"""Durable event log abstraction.

Spec: section 19 (event boundary), section 32 (replayability), section 33.

The trie is derived state. Everything that reconstructs it flows through this
package, so the log interface deliberately exposes offsets/sequences rather than
hiding them.
"""
