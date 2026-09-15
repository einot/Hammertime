"""Store protocols: window counters, IP state, dedup high-water marks.

Spec: section 20, section 23, section 26

Only the dedup protocol (spec section 23, ADR-0003) is defined here so far.
Sliding-window counters and IP HOT/COLD state (spec section 20, section 26)
belong to the aggregator epic and are not yet designed; add their `Protocol`s
alongside `DedupStore` when that epic needs them, rather than speculatively
shaping them now.

`DedupStore` mirrors `hammertime.bus.interface`'s `Producer`/`Consumer`
convention: a `@runtime_checkable` async `Protocol` so `memory.py` (in-process)
and `redis.py` (a real round-trip) are interchangeable behind it.
"""

from typing import Protocol, runtime_checkable


@runtime_checkable
class DedupStore(Protocol):
    """Per-agent duplicate detection for agent-originated observations.

    Identity is `(agent_id, sequence)` (spec section 23, ADR-0003) -- distinct
    from `EventEnvelope.event_id`, which is derived from `(agent_id, sequence,
    event_type)` for a different purpose (idempotent redelivery of *internal*
    events across the bus, ADR-0003's amendment). This store only ever sees
    agent-originated `RequestObservation` messages, which carry no
    `event_type`.
    """

    async def has_seen(self, agent_id: str, sequence: int) -> bool:
        """Whether `(agent_id, sequence)` has already been marked seen.

        Ingest MUST call this before publishing an observation and reject the
        message as a duplicate if it returns `True` (spec section 23).

        `has_seen` then `mark_seen` is NOT atomic: two concurrent requests for
        the same `(agent_id, sequence)` can both observe `False` before either
        calls `mark_seen`, letting both proceed. Use `claim()` below instead
        when that race matters (issue #32's ingest pipeline always does);
        `has_seen` on its own remains useful for a read-only duplicate check
        that doesn't intend to mark anything (e.g. diagnostics).
        """
        ...

    async def mark_seen(self, agent_id: str, sequence: int, *, ttl_seconds: int) -> None:
        """Record `(agent_id, sequence)` as seen for at least `ttl_seconds`.

        `ttl_seconds` SHOULD be `allowed_lateness_seconds + window_seconds`
        (ADR-0003), so a retry within the dedup retention window is always
        caught, and entries outside it are free to expire (spec section 26).
        """
        ...

    async def claim(self, agent_id: str, sequence: int, *, ttl_seconds: int) -> bool:
        """Atomically check-and-mark `(agent_id, sequence)` as seen.

        Returns `True` if this call is the one that marked it (it was not
        already seen) -- the caller now exclusively owns processing this
        sequence. Returns `False` if it was already seen, whether by an
        earlier completed call or by a concurrent caller that claimed it
        first; the caller MUST treat this as a duplicate and not proceed.

        This closes the race `has_seen()` followed by `mark_seen()` cannot:
        those are two separate calls with no atomicity between them (see
        `has_seen`'s docstring), so two concurrent callers for the same
        `(agent_id, sequence)` can both observe `False` from `has_seen()`
        before either calls `mark_seen()`. Exactly one concurrent `claim()`
        call for a given `(agent_id, sequence)` ever returns `True`. Same
        `ttl_seconds` semantics as `mark_seen` (seen for at least
        `ttl_seconds`, never shortened by a later call).
        """
        ...
