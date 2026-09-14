"""Reject repeats of (agent_id, sequence) before they reach the counters.

Spec: section 23
"""

from __future__ import annotations


# Without this, a retried message applies count += N twice and can manufacture a
# false COLD -> HOT transition (spec section 23). Retention must cover
# allowed_lateness + window_seconds.
