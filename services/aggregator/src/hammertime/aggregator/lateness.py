"""Bounded lateness policy for out-of-order observations.

Spec: section 24
"""

from __future__ import annotations


# Windows are event-time (ADR 0002). Observations inside the horizon land in
# their own bucket even if it is not the newest; observations outside it are
# counted as late_messages and routed to reconciliation, never silently dropped.
