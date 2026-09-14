"""Apply evaluate_ip_state and publish the resulting edges.

Spec: section 6, section 30
"""

from __future__ import annotations


# This module MUST call hammertime.core.state.machine.evaluate_ip_state rather
# than re-implementing the comparison. Spec section 30 requires a single
# authoritative state machine shared by ingestion, replay, and re-evaluation.
