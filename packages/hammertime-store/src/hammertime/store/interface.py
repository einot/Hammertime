"""Store protocols: window counters, IP state, dedup high-water marks.

Spec: section 20, section 23, section 26, section 32

Two protocols so far. `DedupStore` (spec section 23, ADR-0003) is ingest's
per-agent duplicate detection. `ShardStateStore` (spec section 20, section
26, section 32; ADR-0011 decision 5) is the aggregator's durable per-shard
HOT set and transition sequence counter. Sliding-window *counters* (spec
section 26) remain in-memory only and have no protocol here: ADR-0011
decision 5 keeps only the HOT set beyond a process, because window buckets
self-heal within one `window_seconds` of a claim.

Both mirror `hammertime.bus.interface`'s `Producer`/`Consumer` convention:
`@runtime_checkable` async `Protocol`s so `memory.py` (in-process) and
`redis.py` (a real round-trip) are interchangeable behind them.
"""

from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from hammertime.core.addressing.address import Address
from hammertime.core.state.enums import IpState


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


@dataclass(frozen=True, slots=True)
class ShardState:
    """What a shard's next owner inherits: its HOT set and sequence counter.

    Spec section 32 makes the per-IP window state plus HOT/COLD state the
    *authoritative* information; the trie is derived from the transitions
    the aggregator publishes. The window counters self-heal within one
    `window_seconds` of a claim and so are not persisted, but the HOT set
    is not derivable from anything else: an owner that forgets an IP it
    announced as HOT never emits the matching `HotIpRemoved`, and the trie
    holds that IP forever (ADR-0011 context item 3).
    """

    #: IPs this shard currently has as HOT, i.e. the IPs it has told (or,
    #: per ADR-0011 decision 4's persist-before-publish order, has tried to
    #: tell) the trie about and must eventually demote.
    hot_ips: frozenset[Address]
    #: The sequence the shard's next transition will use; `0` for a shard
    #: that has never recorded one. Resuming from here is what keeps
    #: decision 4's `event_id` identity -- derived from `(agent_id,
    #: sequence, event_type)` -- from repeating across a restart.
    next_sequence: int


@runtime_checkable
class ShardStateStore(Protocol):
    """Durable per-shard HOT set and transition sequence (ADR-0011 decision 5).

    One logical record per shard (a Kafka partition of the observations
    topic), keyed by the shard id: spec section 20 gives every IP exactly
    one owner, so a shard's HOT set is never shared with another shard and
    two shards never contend for the same entry.
    """

    async def load(self, shard: int) -> ShardState:
        """Read `shard`'s current HOT set and next sequence.

        A pure read: loading a shard that has never recorded a transition
        returns `ShardState(frozenset(), 0)` and MUST NOT create or write
        anything, so a diagnostic or a claim of an idle shard leaves no
        trace. Loading does not consume the state either -- a re-claim
        after a revoke loads the same shard again.
        """
        ...

    async def record_transition(
        self, shard: int, ip: Address, state: IpState, sequence: int
    ) -> None:
        """Add (`HOT`) or remove (`COLD`) `ip`, and set next sequence to `sequence + 1`.

        Atomic: the membership change and the sequence update either both
        land or neither does. A torn write would either leave an IP
        recorded as HOT under a sequence that a later transition reuses, or
        burn a sequence without recording the membership it belongs to.

        `COLD` for an IP the shard does not hold is a membership no-op that
        still advances the sequence -- decision 4's recovery path reaches
        it whenever a new owner demotes an inherited IP the trie never
        learned about.

        The next sequence comes from the given `sequence`, not from a count
        of calls: callers own sequence allocation (`ShardWindow`), and this
        store only records where they have got to.

        Callers MUST record the transition before publishing the
        corresponding event (ADR-0011 decision 4): a failure here must
        abort the publish, because the opposite order can leave the trie
        holding an IP no owner knows about -- the permanent leak this store
        exists to close.
        """
        ...
