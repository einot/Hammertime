"""Reject repeats of (agent_id, sequence) before they reach the counters.

Spec: section 23; ADR-0003 (ttl_seconds = allowed_lateness_seconds + window_seconds)

Without this, a retried message applies count += N twice and can manufacture
a false COLD -> HOT transition (spec section 23). Retention must cover
allowed_lateness + window_seconds, which is what `mark_seen` below derives
before delegating to the injected `hammertime.store.interface.DedupStore`.
"""

from hammertime.store.interface import DedupStore


class DedupService:
    """Ingest's dedup policy, layered on top of an injected `DedupStore`.

    `DedupStore` itself only knows `has_seen`/`mark_seen` with an explicit
    `ttl_seconds`; this service owns deriving that TTL from the loaded
    `DetectionConfig`'s `allowed_lateness_seconds` (ADR-0003) so callers
    (`api/routes.py`) never compute it themselves.
    """

    def __init__(self, store: DedupStore, *, allowed_lateness_seconds: int) -> None:
        self._store = store
        self._allowed_lateness_seconds = allowed_lateness_seconds

    async def is_duplicate(self, agent_id: str, sequence: int) -> bool:
        """Whether `(agent_id, sequence)` has already been marked seen (spec section 23)."""
        return await self._store.has_seen(agent_id, sequence)

    async def mark_seen(self, agent_id: str, sequence: int, *, window_seconds: int) -> None:
        """Mark `(agent_id, sequence)` seen for `allowed_lateness_seconds + window_seconds`.

        ADR-0003's recommended retention: long enough that a retry within
        the allowed-lateness horizon is always caught, short enough that
        entries outside it are free to expire (spec section 26).
        """
        ttl_seconds = self._allowed_lateness_seconds + window_seconds
        await self._store.mark_seen(agent_id, sequence, ttl_seconds=ttl_seconds)
