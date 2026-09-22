"""Every IP maps to exactly one owner; a claim inherits the shard's HOT set and holds its lease.

Spec: section 20 (`hash(IP) -> shard`, one owner per IP), section 26
(retention bounds the store), section 32 (the HOT/COLD state is the
authoritative information), section 47.2 (readiness is "shard claims held").

ADR-0013 is the design this file is written against (ADR-0011 Amendment 7
records what it supersedes): decision 6 (`parse_shard_ids`: `all` or an
explicit set within `0..127`; `auto` rejected), decision 7 (the per-shard
lease in the state store: taken in `on_assigned` before `load`,
`ShardOwnedElsewhereError`; renewed by `renew_leases()` first thing in
`run_maintenance()`, `ShardLeaseLostError`; released by `release()`, which
`stop()` calls after the final flush-and-ack), decision 8 (the `ShardClaims`
surface: `handled_position`, `mark_handled`, `commit_handled()` as
`producer.flush()` then `consumer.ack(<handled, unacked>)`, no `on_revoked`;
`UNCLAIMED` unchanged for a partition this member holds no window for), and
decision 3's `ack()` rule, which shapes the harness (ASSUMPTION 4). Still in
force from ADR-0011: Amendment 1 item A3 (a set-but-empty value is a
configuration error), decision 5's warm-up rule, Amendment 2 item A5 (an
inherited HOT IP is a tracked entry from construction) and item A7 (the two
per-window eviction counters), Amendment 3 item A13 (`window_evictions` and
`shards_claimed` are computed on read), Amendment 5 item A19 (`UNCLAIMED`:
logged, counted under no series, neither decoded nor diverted, the store
untouched). ADR-0001 Amendment 1 (the consistency model: clause 1, the owner
is a shard and not a process; clause 2, a shard's transition stream is
totally ordered and its identity is continued, not restarted, across owners;
clause 5, what a handover does to the stream; clause 6, no double count and
no loss at a handover) and ADR-0003 Amendment 3 (item 2: what the aggregator
acknowledges is exactly the set of messages `handle()` finished, never a
message merely fetched) are what `TestHandoverBetweenTwoMembers` is written
against.

Two interfaces are under test. The first is pinned by ADR-0013 decision 6:

    hammertime.aggregator.config.parse_shard_ids(text) -> frozenset[int]

(`all`, case-insensitive, is `frozenset(range(128))`; tokens are `n` or
`lo-hi` with `lo <= hi`, inclusive, comma-separated; every id is
`0 <= id < 128`; `auto`, set-but-empty and whitespace-only raise; it "no
longer returns `None`").

The second is ADR-0013 decision 8's block:

    class ShardClaims:                                   # satisfies AssignmentListener
        def __init__(self, *, state_store, producer, consumer, clock, config,
                     member_id: str, lease_ttl_s: float,
                     max_tracked_ips: int = 1_000_000,
                     instance_id: str | None = None) -> None: ...   # None -> secrets.token_hex(8)
        config: DetectionConfig
        shards: frozenset[int]
        member_id: str        # property (Amendment 6)
        instance_id: str      # property (Amendment 6): this process's token
        lease_owner: str      # property (Amendment 6): f"{member_id}/{instance_id}",
                              # the owner passed to every lease call
        def window(self, shard: int) -> ShardWindow | None: ...
        def windows(self) -> tuple[ShardWindow, ...]: ...
        def adopt_config(self, config: DetectionConfig) -> None: ...
        def handled_position(self, shard: int) -> int | None: ...
        def mark_handled(self, message: ConsumedMessage) -> None: ...
        async def commit_handled(self) -> None: ...
        async def renew_leases(self) -> None: ...
        async def release(self) -> None: ...
        async def on_assigned(self, partitions: frozenset[tuple[str, int]]) -> None: ...

with `ShardOwnedElsewhereError(shard, owner)`, `ShardLeaseLostError(shard,
owner)` and -- since Amendment 6 ruling 2 --
`ShardHeldBySameMemberError(shard, owner)`, a subclass of
`ShardOwnedElsewhereError`, in `hammertime.aggregator.sharding.assignment`
(decision 7).

ADR-0013 Amendment 6 (2026-09-22) is what the lease owner means here: "The
`owner` argument is an opaque token, not a member id ...: the store grants
iff no live lease exists or the live lease's value equals the caller's
token, releases iff equal, and reads nothing into the value. `ShardClaims`
passes `lease_owner = f"{member_id}/{instance_id}"`, where `instance_id` is
a random token generated once per process, so that a second process started
under the same `member_id` is refused rather than treated as a renewal; the
holder a refusal returns is that token, which `ShardClaims` splits at its
last `/` to tell another member from another instance of itself." Ruling
2(b) classifies the holder; `TestTheInstanceTokenTellsProcessesOfOneMemberApart`
is written against it.

ASSUMPTIONS -- details the ADRs do not pin. Adjust the helpers below, not
the meaning of the assertions:

1. `AggregatorWorker(*, bus, state_store, clock, config, metrics,
   shard_ids=None, max_tracked_ips=1_000_000, member_id="aggregator",
   lease_ttl_s=30.0, instance_id=None)`, and `await worker.start()` / `await
   worker.handle(message)` / `await worker.run()` / `await
   worker.run_maintenance()` / `await worker.stop()`. ADR-0013 decision 8
   names the two lease keywords and their defaults; Amendment 6 names the
   third ("`AggregatorWorker` gains the same `instance_id: str | None =
   None` keyword and an `instance_id` property"). `shard_ids=None` "is
   passed to the bus as `partitions=None`, 'every partition as the bus
   defines it' -- `{0}` on `InMemoryBus`" (decision 6). See
   `test_worker.py`, which states the same assumption.
2. `claims.window(shard) -> ShardWindow | None` and `claims.shards` are how
   a caller reaches a claimed shard's window; `AggregatorWorker` exposes the
   same two, plus `claims`.
3. `ShardOwnedElsewhereError(shard, owner)` and `ShardLeaseLostError(shard,
   owner)` are asserted by type only; the ADR gives their constructor
   arguments and log records, not attribute names.
4. **The message handed to `handle()` is read on the worker's own
   consumer.** ADR-0013 decision 3: `ack()` accepts only a message "delivered
   by this consumer instance under its durable subscription", so a message
   read on a separate test group (the shape the previous version of this
   file used) can no longer be marked handled and acknowledged at `stop()`.
   `_TappedBus` therefore hands back the subscription the worker's consumer
   opened, and `_Feed.deliver` reads the next message on it before calling
   `handle()`. `_TappedBus.consumer` returns `Any` for the same reason
   `test_worker.py`'s bus doubles always have: a wrapper is not a
   `MemoryConsumer`.
5. Ruled, no longer assumed (Amendment 1 ruling T6; assumption 35).
   Decision 7 says `acquire_lease(p, ...)` is called "for each shard in
   sorted order and **before** `state_store.load(p)` -- strictly
   sequentially per shard: acquire `p`, load `p`, build `p`'s window, and
   only then the next shard's acquire". What is asserted is exactly that
   ordered `acquire_lease`/`load` subsequence
   (`test_every_shard_is_leased_in_sorted_order_before_its_load`); the
   earlier, weaker form -- per shard the acquire precedes the load, and the
   acquires come in ascending order -- is kept alongside it, as the ruling
   permits.
6. `worker.stop()` does not close the consumer -- decision 8: "the service
   closes the bus ... afterwards". On the memory bus an unacknowledged
   message is deliverable to the next consumer whether or not the previous
   one was closed, so the handover tests do not depend on it either way.
7. **Members are distinguished by their owner token, not by their id**
   (Amendment 6 ruling 2: the store "reads nothing into the value"). The
   handover members keep distinct `member_id`s all the same -- two
   successive processes of one deployment normally share an id, but a
   handover test that gave them one would be asserting the same-member path
   of `TestTheInstanceTokenTellsProcessesOfOneMemberApart` instead of the
   handover -- and a lease TTL (`LONG_LEASE_SECONDS`) far longer than any
   clock advance in this file, and the state store shares the members'
   `ManualClock`; so the only way B can acquire shard 0 is A's `stop()`
   having released it. Lease-lapse tests use `LEASE_TTL_SECONDS` (30, the
   ADR's default) and advance past it.
8. The `ShardLeaseLostError` from `run_maintenance()` leaves the sweep
   unrun. Decision 7 orders the renewal "**first**, before the expiry sweep
   ... so a member that has lost a shard emits nothing more for it"; that is
   asserted as "no demotion was recorded or published after a refused
   renewal", with the demotion otherwise due in that very sweep.
9. Ruled, no longer assumed (Amendment 1 rulings T3 and T4; assumption 32).
   Decision 8 as amended: "`handled_position(shard)` returns `None` for a
   shard this object does not hold, exactly as for a held shard nothing has
   been handled on: the read surface (`window`, `handled_position`) is total
   and the write surface (`mark_handled`) raises `KeyError`"; and "The
   position belongs to the claim for the life of the process:
   `commit_handled()` clears the handled *list* and leaves the position
   where it is ... and nothing ever lowers it". So `handled_position(p)`
   survives `commit_handled()`, and asking about an unheld `p` -- before any
   claim, or for a partition outside the claimed set -- is `None`, not
   `KeyError`.
10. Ruled, no longer assumed (Amendment 1 ruling T5; assumption 31).
    Decision 8 as amended: "`on_assigned(frozenset())` is a `ValueError`. It
    is unreachable through the bus -- `static_partitions` refuses an empty
    set before the listener is called, and `partitions=None` always resolves
    to a non-empty set -- so only a direct call can reach it; a member
    holding nothing must not report itself ready ... and the direct call is
    refused for the same reason." The direct call is exercised
    (`test_on_assigned_with_an_empty_set_is_a_value_error`): it raises, holds
    nothing, and touches the store not at all.
11. **A two-shard worker on the memory bus.** `InMemoryBus` refuses any
    static set other than `{0}` (decision 3), so the `load()`-fault tests of
    Amendment 3 ruling (d) -- "shards `{p, q}`, `p < q`, `load(q)` raises" --
    run over `_AnyPartitionsBus`, whose consumer accepts any static set the
    way `NatsConsumer` does ("accepts any non-negative partition"): it awaits
    `listener.on_assigned` with the full set before subscribing the inner
    `MemoryConsumer`, and if the listener raises the inner never subscribed,
    which is decision 3's "the consumer is left as if `subscribe()` had never
    been called". Its `ack()` is the real `MemoryConsumer.ack()`, so
    `stop()`'s `ack(())` on the failed start is exactly the call assumption
    56 describes -- nothing is mocked away.
12. **A re-claimed shard gets a new window** (Amendment 4 assumption 70:
    "not the surviving one ... a fresh load and warm-up is what any new claim
    gets"). `TestReclaimingAShardAfterStop` asserts object identity changes
    and that the handled position survives, which is the R6 sentence "a new
    window built in place of the old one; its handled position is kept".
13. **`_FailingAckBus`** models a consumer whose acknowledgement fails at the
    final commit (a broker outage at `ack_sync`, decision 3): its `ack()`
    records itself in the trace and raises. That is the case R4's `finally`
    exists for, and the trace shows the release following the failed ack.
14. **The helpers inject a *known* instance token by default.** Amendment 6
    makes `instance_id=None` mean `secrets.token_hex(8)`, which no assertion
    on `_RecordingStore.calls` could name; `_claims`, `_claims_and_consumer`
    and `_worker` therefore default to `INSTANCE_A` and pass whatever they
    are given straight through, so `instance_id=None` given explicitly still
    reaches the ADR's generated default (which
    `test_two_claims_built_without_an_instance_id_get_distinct_tokens` is
    what exercises). Every owner in a store assertion is
    `_owner(member, instance)` -- `f"{member}/{instance}"`, the `lease_owner`
    the ADR names.
15. **A pre-seeded holder standing for "another member" is written either
    bare (`OTHER_MEMBER`) or as a token (`OTHER_MEMBER/<instance>`).** Ruling
    2(b) and assumption 99: "a value with no `/` is a member id with an empty
    instance", so both are another member's holder and neither may be
    classified as the same member. The tests that care assert the refusal is
    a `ShardOwnedElsewhereError` and *not* a `ShardHeldBySameMemberError`
    (assumption 100: "The tests that distinguish the two assert `isinstance`
    against the subclass"), and the parametrised
    `test_a_shard_leased_elsewhere_is_refused` covers both spellings.
16. **`MEMBER_C` and `OTHER_MEMBER` as probe owners** -- the value a test
    passes to `state_store.acquire_lease` to read back who holds a shard --
    stay bare ids. Assumption 109: the store "reads nothing into the value",
    and a probe is refused (or granted) by equality alone, whatever its
    shape.

Amendment 3 (2026-09-21), Amendment 4 (2026-09-22) and Amendment 5
(2026-09-22) rulings pinned here, each by the class named after it:

* Ruling (c): "after `stop()`, `run_maintenance()` returns normally without
  renewing, sweeping, emitting or writing" (assumption 53: a silent no-op,
  not an error) -- `TestNothingActsOnAShardAfterStop`, with the next owner
  already holding the lease and a demotion due in the sweep that must not run.
* Ruling (d): "A `load()` that raises inside this call is a fault, not a
  refusal ...: `ShardClaims` logs nothing, releases no lease inside the call,
  and lets the exception propagate the same way, to exit 1. The leases the
  call acquired -- the failed shard's included, since it was acquired before
  its `load()` -- stay in the held set, so `release()` drops them when
  `run_service` calls `stop()` on the failed start ..., and they lapse after
  `lease_ttl_s` when the store cannot be reached for that either." Ruling R5
  reaffirms it "with the replacement consequence stated": "a replacement
  under any `member_id` starts at once" after the release; when the leases
  merely lapse, "a replacement with a *different* id is refused with
  `shard_owned_elsewhere` ... a replacement with the same id reacquires at
  once either way" -- `TestAFaultingLoadInsideOnAssigned`.
* Ruling R2: "`stop()`'s order is total ... From the moment `stop()` sets
  the stop flag, every path that could act on a shard checks the flag under
  the worker lock and stands down: `handle()` returns `UNCLAIMED` without
  decoding, applying, emitting or marking, so the message stays
  unacknowledged and `close()` hands it to the next member;
  `run_maintenance()` and `apply_config()` return without renewing,
  sweeping, re-evaluating, emitting or writing (`apply_config()` leaves the
  worker's `config` as it was ...)" -- `TestNothingActsOnAShardAfterStop`.
* Ruling R6 as corrected by Amendment 5 ruling 1: "the test is the lease
  **and** the window -- a claim is both, and `on_assigned` skips a shard only
  when this member holds its lease and has its window". R6's own case is
  unchanged: "A shard whose window survived a `release()` but whose lease is
  gone is claimed afresh: lease acquired, state loaded, a new window built in
  place of the old one; its handled position is kept" --
  `TestReclaimingAShardAfterStop`. The mirror case the correction closes is a
  shard ruling (d) left leased with no window: "Such a shard is claimed to
  completion exactly as an unleased one is claimed: `acquire_lease` is called
  again (a renewal of this member's own lease, or a refusal if the lease
  lapsed meanwhile and another member took it -- which skipping the acquire
  would miss), the state is loaded, the window built, `shard_claimed` logged;
  it counts among the shards the call claimed, so a refusal later in the same
  call rolls it back with the others. Ruling (d) is unchanged: the faulting
  call itself still releases nothing." --
  `TestALeasedButUnwindowedShardIsClaimedToCompletion`.
* Ruling R4: "`stop()` itself runs its sequence once: under the lock,
  `commit_handled()` inside a `try` whose `finally` is `release()`, so the
  leases are released even when the final acknowledgement fails -- the
  exception propagates after the release ... A later `stop()` -- the
  runner's `_stop_quietly` after a crash exit, by which time the service has
  closed the bus and `ack(())` would be a `ValueError` -- acquires the lock
  and returns without touching the bus or the store; that is what
  "idempotent" means for it." -- `TestStopRunsOnceAndReleasesInAFinally`,
  and the second-`stop()` trace test in `TestReleasingLeases`.
* Amendment 6 (2026-09-22) ruling 2: the lease owner is the process's token,
  and a holder that names this member with another instance is
  `ShardHeldBySameMemberError`, rolled back like any refusal --
  `TestTheInstanceTokenTellsProcessesOfOneMemberApart`. Ruling 2 also
  supersedes Amendment 4 ruling R5's "a replacement with the same id
  reacquires at once": in `TestAFaultingLoadInsideOnAssigned`, the two tests
  named `..._a_replacement_with_the_same_id_waits_for_the_lease` and
  `..._a_claimant_with_the_same_token_reacquires_at_once` pin the amended
  form -- a replacement is a different instance and waits out the
  predecessor's leases, while the same token is granted at once.

NOT asserted here, and why:

* **The `shard_claimed` / `shard_owned_elsewhere` / `shard_held_by_same_member`
  / `shard_lease_lost` log records** of decisions 7 and 8 (the third added by
  Amendment 6 ruling 2, `WARNING ... owner=<token> member=<self>
  instance=<self instance>`). ADR-0009 decision 5 and section 47.7 fix
  the event names and fields but not a record shape a unit test can assert
  against without a configured logger -- the same reason
  `packages/hammertime-core/.../tests/test_runtime.py` gives for omitting
  its own lifecycle records. The observable half of A5's claim assertion
  (`inherited_hot=2`) is asserted on the window instead: `hot_count == 2`.
  Ruling (d)'s "logs nothing" is likewise asserted by its observable half:
  the exception is the fault itself and not `ShardOwnedElsewhereError`, and
  no `release_lease` reaches the store inside the call.
* **The `unclaimed_partition` log record** (A19), for the same reason. Its
  counterpart -- that A19's record comes with no counter -- *is* asserted,
  on the metrics object.
* **Two members holding claims concurrently on the bus.** `InMemoryBus` has
  one partition and "at most one live member per group per topic"
  (ADR-0011 assumption 22, kept by ADR-0013 decision 3), so members here are
  sequential: A is stopped before B is constructed. The one concurrent case
  the lease exists for -- B starting while A is live -- is observable
  through the state store alone and is asserted
  (`test_a_second_member_cannot_start_while_the_first_holds_the_lease`).
* **A message the previous member fetched into its queue but never
  handled** reaching the next member: that needs the fetch-batch double of
  `test_worker.py::TestTheMessageInTheQueueReachesTheNextMember`; at the
  `ShardClaims` level the same rule is `TestAcknowledgingHandledMessages`'s
  "fetched but not handled is not acknowledged".

`BASE` is `1_800_000_000`, the `T0` of `docs/spec/integration-scenarios.md`
section 2. Nothing in this module sleeps for wall time: `_yield_until` and
`_Members.stop_all` only yield to the event loop with `asyncio.sleep(0)`.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable, Iterable
from contextlib import suppress
from datetime import UTC, datetime
from typing import Any

import pytest
from hammertime.aggregator.config import parse_shard_ids
from hammertime.aggregator.lateness import ObservationOutcome
from hammertime.aggregator.metrics import AggregatorMetrics
from hammertime.aggregator.sharding.assignment import (
    ShardClaims,
    ShardHeldBySameMemberError,
    ShardLeaseLostError,
    ShardOwnedElsewhereError,
)
from hammertime.aggregator.worker import AggregatorWorker
from hammertime.bus.interface import AssignmentListener, ConsumedMessage, Consumer
from hammertime.bus.memory import InMemoryBus, MemoryProducer
from hammertime.bus.topics import OBSERVATIONS
from hammertime.core.addressing.address import Address
from hammertime.core.config.models import DetectionConfig
from hammertime.core.events.codec import EventPayload, decode, encode
from hammertime.core.events.envelope import EventEnvelope
from hammertime.core.events.models import HotIpRemoved, Observation, RequestObservation
from hammertime.core.state.enums import IpState
from hammertime.core.time.clock import ManualClock
from hammertime.store.interface import ShardState, ShardStateStore
from hammertime.store.memory import MemoryShardStateStore

HOT_IP_TOPIC = "hammertime.hot-ip.v1"
OBSERVATIONS_TOPIC = OBSERVATIONS.name
# Spelled out rather than imported: section 24's ADR-0011 note and
# docs/spec/integration-scenarios.md section 2.4 both name this literal.
RECONCILIATION_TOPIC = "hammertime.observations-reconciliation.v1"
# ADR-0009 decision 9: one fixed consumer group for the aggregator.
GROUP = "hammertime-aggregator"

WINDOW_SECONDS = 300
BUCKET_SECONDS = 10
STATE_RETENTION_SECONDS = 600

BASE = 1_800_000_000
# How long the shard sits unowned between member A's stop and member B's
# claim in `TestHandoverBetweenTwoMembers`: ADR-0001 Amendment 1 clause 5's
# handover time. Non-zero so that B's `warm_until` is visibly B's claim
# time and not A's start.
HANDOVER_SECONDS = 40

# ADR-0013 decision 7: `HAMMERTIME_AGGREGATOR_LEASE_TTL_S` defaults to 30.
LEASE_TTL_SECONDS = 30.0
# ASSUMPTION 7: longer than any clock advance below, so only a release frees it.
LONG_LEASE_SECONDS = 3_600.0

MEMBER_A = "aggregator-a"
MEMBER_B = "aggregator-b"
MEMBER_C = "aggregator-c"
OTHER_MEMBER = "aggregator-elsewhere"

# ADR-0013 Amendment 6 ruling 2(a): the per-process token, "generated once
# per construction; injectable for tests". Two of them stand for two
# processes started under one `member_id` -- a predecessor and its
# replacement, or a live twin.
INSTANCE_A = "instance-aaaa"
INSTANCE_B = "instance-bbbb"


def _owner(member_id: str, instance_id: str) -> str:
    """The `lease_owner` of Amendment 6 ruling 2(a): `f"{member_id}/{instance_id}"`.

    "the `owner` of every `acquire_lease` and `release_lease` call".
    """

    return f"{member_id}/{instance_id}"


# The owner token of every claim the helpers build unless a test says otherwise
# (ASSUMPTION 14).
OWNER_A = _owner(MEMBER_A, INSTANCE_A)

IP_A = Address.parse("198.51.100.1")
IP_B = Address.parse("198.51.100.2")
IP_C = Address.parse("198.51.100.3")

# ADR-0011 decision 4 step 4 / ADR-0003 Amendment 2: the envelope `agent_id`
# of every transition shard 0 emits, whichever worker holds the shard.
SHARD_AGENT_ID = "aggregator-shard-0"


def _config(
    *,
    config_version: int = 1,
    window_seconds: int = WINDOW_SECONDS,
    bucket_seconds: int = BUCKET_SECONDS,
    hot_threshold: int = 1000,
    cold_threshold: int = 800,
    allowed_lateness_seconds: int = 30,
    state_retention_seconds: int = STATE_RETENTION_SECONDS,
) -> DetectionConfig:
    """The shipped defaults of `config/detection.v1.json`, overridable."""

    return DetectionConfig(
        config_version=config_version,
        window_seconds=window_seconds,
        bucket_seconds=bucket_seconds,
        hot_threshold=hot_threshold,
        cold_threshold=cold_threshold,
        allowed_lateness_seconds=allowed_lateness_seconds,
        state_retention_seconds=state_retention_seconds,
    )


DEFAULTS = _config()


class _RecordingProducer(MemoryProducer):
    """A `MemoryProducer` that appends `"flush"` to a shared trace."""

    def __init__(self, bus: InMemoryBus, trace: list[str]) -> None:
        super().__init__(bus)
        self._trace = trace

    async def flush(self) -> None:
        self._trace.append("flush")
        await super().flush()


class _RecordingConsumer:
    """A `Consumer` that appends `"ack"` to a shared trace on every `ack()`.

    Delegates everything else to a real `MemoryConsumer`, so nothing here
    depends on that class's constructor; `__getattr__` forwards any member
    this wrapper does not name. `subscribe()` stores the stream it returned,
    so a test can read on the very consumer the claims acknowledge on
    (ASSUMPTION 4).
    """

    def __init__(self, inner: Consumer, trace: list[str]) -> None:
        self._inner = inner
        self._trace = trace
        self.stream: AsyncIterator[ConsumedMessage] | None = None

    async def subscribe(
        self,
        topic: str,
        *,
        partitions: Iterable[int] | None = None,
        listener: AssignmentListener | None = None,
        start_offset: int | None = None,
    ) -> AsyncIterator[ConsumedMessage]:
        self.stream = await self._inner.subscribe(
            topic, partitions=partitions, listener=listener, start_offset=start_offset
        )
        return self.stream

    async def ack(self, messages: Iterable[ConsumedMessage]) -> None:
        self._trace.append("ack")
        await self._inner.ack(messages)

    async def close(self) -> None:
        await self._inner.close()

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)


class _TappedBus(InMemoryBus):
    """An `InMemoryBus` whose producer records flushes and whose consumers
    record acks and expose their subscription stream (ASSUMPTION 4).

    `latest_stream` is the subscription of the most recently created
    consumer -- the current member's, since every member creates exactly one
    consumer in `start()` and members are sequential.
    """

    def __init__(self, trace: list[str] | None = None) -> None:
        super().__init__()
        self.trace: list[str] = trace if trace is not None else []
        self.consumers: list[_RecordingConsumer] = []

    def producer(self) -> MemoryProducer:
        return _RecordingProducer(self, self.trace)

    def consumer(self, *args: Any, **kwargs: Any) -> Any:
        wrapped = _RecordingConsumer(super().consumer(*args, **kwargs), self.trace)
        self.consumers.append(wrapped)
        return wrapped

    @property
    def latest_stream(self) -> AsyncIterator[ConsumedMessage]:
        assert self.consumers, "no consumer has been created on this bus"
        stream = self.consumers[-1].stream
        assert stream is not None, "the latest consumer has not subscribed"
        return stream


class _FailingAckConsumer(_RecordingConsumer):
    """A `_RecordingConsumer` whose `ack()` records itself and then fails (ASSUMPTION 13)."""

    async def ack(self, messages: Iterable[ConsumedMessage]) -> None:
        self._trace.append("ack")
        raise RuntimeError("ack failed")


class _FailingAckBus(_TappedBus):
    """A `_TappedBus` whose consumers cannot acknowledge: the `commit_handled()` inside
    `stop()` raises, which is the case Amendment 4 ruling R4's `finally` exists for."""

    def consumer(self, *args: Any, **kwargs: Any) -> Any:
        wrapped = _FailingAckConsumer(InMemoryBus.consumer(self, *args, **kwargs), self.trace)
        self.consumers.append(wrapped)
        return wrapped


class _AnyPartitionsConsumer:
    """A `Consumer` that accepts any static partition set, as `NatsConsumer` does
    (ASSUMPTION 11), on top of a real `MemoryConsumer`.

    The listener is awaited with the full set before the inner consumer
    subscribes (decision 3: "`subscribe()` awaits `listener.on_assigned(...)`
    exactly once -- with the full static set ... before it returns"); if it
    raises, the inner never subscribed and stays neither closed nor
    subscribed, so its `ack([])` returns normally.
    """

    def __init__(self, inner: Consumer) -> None:
        self._inner = inner

    async def subscribe(
        self,
        topic: str,
        *,
        partitions: Iterable[int] | None = None,
        listener: AssignmentListener | None = None,
        start_offset: int | None = None,
    ) -> AsyncIterator[ConsumedMessage]:
        assigned = frozenset(partitions) if partitions is not None else frozenset({0})
        if not assigned:
            raise ValueError("an empty partition set is refused before the listener is called")
        if listener is not None:
            await listener.on_assigned(frozenset((topic, p) for p in assigned))
        return await self._inner.subscribe(
            topic, partitions=None, listener=None, start_offset=start_offset
        )

    async def ack(self, messages: Iterable[ConsumedMessage]) -> None:
        await self._inner.ack(messages)

    async def close(self) -> None:
        await self._inner.close()

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)


class _AnyPartitionsBus(InMemoryBus):
    """An `InMemoryBus` whose consumers accept any static partition set (ASSUMPTION 11)."""

    def consumer(self, *args: Any, **kwargs: Any) -> Any:
        return _AnyPartitionsConsumer(super().consumer(*args, **kwargs))


class _StoreDown(Exception):
    """The fault Amendment 3 ruling (d) names: "the store unreachable" during `load()`."""


class _RecordingStore:
    """A `ShardStateStore` that records every call, delegating to a real
    `MemoryShardStateStore` (which takes the test's clock, ADR-0013 decision
    7). `calls` holds `(method, *args)`; `trace` receives the method name so
    that store, producer and consumer calls can be ordered against each
    other in one list."""

    def __init__(self, clock: ManualClock, trace: list[str] | None = None) -> None:
        self._inner = MemoryShardStateStore(clock=clock)
        self.calls: list[tuple[Any, ...]] = []
        self.trace: list[str] = trace if trace is not None else []

    def _note(self, method: str, *args: Any) -> None:
        self.calls.append((method, *args))
        self.trace.append(method)

    async def load(self, shard: int) -> ShardState:
        self._note("load", shard)
        return await self._inner.load(shard)

    async def record_transition(
        self, shard: int, ip: Address, state: IpState, sequence: int
    ) -> None:
        self._note("record_transition", shard)
        await self._inner.record_transition(shard, ip, state, sequence)

    async def acquire_lease(self, shard: int, owner: str, ttl_seconds: float) -> str | None:
        self._note("acquire_lease", shard, owner, ttl_seconds)
        return await self._inner.acquire_lease(shard, owner, ttl_seconds)

    async def release_lease(self, shard: int, owner: str) -> None:
        self._note("release_lease", shard, owner)
        await self._inner.release_lease(shard, owner)

    def clear(self) -> None:
        self.calls.clear()
        self.trace.clear()


class _FaultingLoadStore(_RecordingStore):
    """A `_RecordingStore` whose `load()` raises `_StoreDown` for one shard (ruling (d)).

    The attempt is recorded before the fault, so the call sequence shows the
    lease taken and the load tried; `heal()` makes the shard loadable again,
    for the replacement that starts after the fault.
    """

    def __init__(self, clock: ManualClock, *, faulty_shard: int) -> None:
        super().__init__(clock)
        self._faulty_shard: int | None = faulty_shard

    def heal(self) -> None:
        self._faulty_shard = None

    async def load(self, shard: int) -> ShardState:
        if shard == self._faulty_shard:
            self._note("load", shard)
            raise _StoreDown(f"shard {shard}: store unreachable")
        return await super().load(shard)


async def _claims_and_consumer(
    *,
    clock: ManualClock,
    bus: InMemoryBus | None = None,
    state_store: ShardStateStore | None = None,
    config: DetectionConfig | None = None,
    trace: list[str] | None = None,
    member_id: str = MEMBER_A,
    instance_id: str | None = INSTANCE_A,
    lease_ttl_s: float = LEASE_TTL_SECONDS,
    max_tracked_ips: int = 1_000_000,
) -> tuple[ShardClaims, _RecordingConsumer]:
    """ADR-0013 decision 8's constructor, with the claim's own consumer.

    The inner consumer is subscribed first, the way the worker's own consumer
    is by the time any assignment callback can run -- `commit_handled()`
    acknowledges on it, and the acknowledgement tests read on its stream
    (ASSUMPTION 4). `instance_id` is passed through as given, `None`
    included, and defaults to a known token (ASSUMPTION 14).
    """

    resolved_bus = bus if bus is not None else InMemoryBus()
    resolved_trace = trace if trace is not None else []
    consumer = _RecordingConsumer(resolved_bus.consumer(GROUP), resolved_trace)
    await consumer.subscribe(OBSERVATIONS_TOPIC)
    claims = ShardClaims(
        state_store=state_store if state_store is not None else MemoryShardStateStore(clock=clock),
        producer=_RecordingProducer(resolved_bus, resolved_trace),
        consumer=consumer,
        clock=clock,
        config=config if config is not None else DEFAULTS,
        member_id=member_id,
        lease_ttl_s=lease_ttl_s,
        max_tracked_ips=max_tracked_ips,
        instance_id=instance_id,
    )
    return claims, consumer


async def _claims(
    *,
    clock: ManualClock,
    bus: InMemoryBus | None = None,
    state_store: ShardStateStore | None = None,
    config: DetectionConfig | None = None,
    trace: list[str] | None = None,
    member_id: str = MEMBER_A,
    instance_id: str | None = INSTANCE_A,
    lease_ttl_s: float = LEASE_TTL_SECONDS,
    max_tracked_ips: int = 1_000_000,
) -> ShardClaims:
    """`_claims_and_consumer` without the consumer, for tests that never read."""

    claims, _consumer = await _claims_and_consumer(
        clock=clock,
        bus=bus,
        state_store=state_store,
        config=config,
        trace=trace,
        member_id=member_id,
        instance_id=instance_id,
        lease_ttl_s=lease_ttl_s,
        max_tracked_ips=max_tracked_ips,
    )
    return claims


def _worker(
    *,
    bus: InMemoryBus,
    clock: ManualClock,
    state_store: ShardStateStore | None = None,
    config: DetectionConfig | None = None,
    metrics: AggregatorMetrics | None = None,
    member_id: str = MEMBER_A,
    instance_id: str | None = INSTANCE_A,
    lease_ttl_s: float = LONG_LEASE_SECONDS,
) -> AggregatorWorker:
    """ASSUMPTION 1 (see the module docstring); mirrored in `test_worker.py`.

    `instance_id` defaults to a known token (ASSUMPTION 14) so that the owner
    of every lease this worker takes is nameable in an assertion.
    """

    return AggregatorWorker(
        bus=bus,
        state_store=state_store if state_store is not None else MemoryShardStateStore(clock=clock),
        clock=clock,
        config=config if config is not None else DEFAULTS,
        metrics=metrics if metrics is not None else AggregatorMetrics(),
        shard_ids=None,
        member_id=member_id,
        lease_ttl_s=lease_ttl_s,
        instance_id=instance_id,
    )


def _observation(ip: Address, count: int, *, window_start: int) -> bytes:
    """One single-entry `RequestObservation`, exactly as ingest publishes it.

    ADR-0004: same header as the accepted batch, `observations=(entry,)`,
    `subject` set to the entry's IP text.
    """

    payload = RequestObservation(
        agent_id="edge-17",
        sequence=1,
        window_start=datetime.fromtimestamp(window_start, tz=UTC),
        window_seconds=BUCKET_SECONDS,
        observations=(Observation(ip=ip, request_count=count),),
    )
    return encode(
        EventEnvelope(
            agent_id="edge-17",
            sequence=1,
            event_type="RequestObservation",
            config_version=1,
            timestamp=datetime.fromtimestamp(window_start, tz=UTC),
            subject=str(ip),
            payload=payload,
        )
    )


def _message(partition: int, offset: int, ip: Address = IP_C) -> ConsumedMessage:
    """A well-formed observation message on an arbitrary partition/offset."""

    return ConsumedMessage(
        topic=OBSERVATIONS_TOPIC,
        partition=partition,
        offset=offset,
        key=str(ip).encode(),
        value=_observation(ip, 1200, window_start=BASE),
    )


async def _take_one(stream: AsyncIterator[ConsumedMessage]) -> ConsumedMessage:
    async for message in stream:
        return message
    raise AssertionError("the subscription ended without yielding a message")


def _records(bus: InMemoryBus, topic: str) -> list[tuple[bytes | None, bytes]]:
    return [(record.key, record.value) for record in bus._logs.get(topic, [])]


def _hot_ip_events(bus: InMemoryBus) -> list[EventEnvelope[EventPayload]]:
    """Every transition on `hammertime.hot-ip.v1`, decoded, in log order."""

    return [decode(value) for _key, value in _records(bus, HOT_IP_TOPIC)]


def _identities(bus: InMemoryBus) -> list[tuple[str, str, str, int]]:
    """`(event_type, subject, agent_id, sequence)` per emitted transition.

    ADR-0003 (amended): `event_id` derives from `(agent_id, sequence,
    event_type, subject)`, so these four are the identity a handover has to
    continue rather than restart (ADR-0001 Amendment 1 clause 2). The
    envelope's `subject` is optional in general (ADR-0004 decision 4), but
    decision 4 step 4 sets `subject=str(ip)` on every transition, so a
    missing one is a failure here.
    """

    identities: list[tuple[str, str, str, int]] = []
    for envelope in _hot_ip_events(bus):
        assert envelope.subject is not None, "a hot-ip event always carries its IP as subject"
        identities.append(
            (envelope.event_type, envelope.subject, envelope.agent_id, envelope.sequence)
        )
    return identities


def _is_hot(worker: AggregatorWorker, ip: Address) -> bool:
    """The predicate a consume-loop test waits on.

    Decision 4 orders one transition as `record_transition` -> publish ->
    `set_state`, so an IP reading HOT means the store write and the hot-ip
    record it asserts on already exist; `is_tracked` would become true at
    `observe`, before any of them.
    """

    window = worker.window(0)
    return window is not None and window.state(ip) is IpState.HOT


async def _yield_until(predicate: Callable[[], bool], *, steps: int = 10_000) -> None:
    """Yield to the event loop until `predicate` holds. Never waits on the wall clock."""

    for _ in range(steps):
        if predicate():
            return
        await asyncio.sleep(0)
    raise AssertionError("the condition was never reached")


class _Feed:
    """Publishes observations and hands each one to a worker off the worker's
    own subscription (ASSUMPTION 4).

    `publish` without `deliver` leaves a message in the log that no member
    has fetched. Observations are published with `message_id=None` on
    purpose: some tests publish byte-identical observations to two successive
    members, and ADR-0013 assumption 22 is what keeps the log from
    deduplicating them.
    """

    def __init__(self, bus: _TappedBus) -> None:
        self._bus = bus
        self._producer = bus.producer()

    async def publish(self, ip: Address, count: int) -> None:
        await self._producer.publish(
            OBSERVATIONS_TOPIC,
            key=str(ip),
            value=_observation(ip, count, window_start=BASE),
            message_id=None,
        )

    async def deliver(
        self, worker: AggregatorWorker, ip: Address, count: int
    ) -> ObservationOutcome:
        await self.publish(ip, count)
        message = await _take_one(self._bus.latest_stream)
        return await worker.handle(message)


class _Members:
    """The successive members of one `hammertime-aggregator` group.

    One bus, one state store, one clock; a fresh `AggregatorMetrics` and a
    distinct `member_id` per member (ASSUMPTION 7), so a transition can be
    attributed to the worker that emitted it and a lease to the member that
    holds it. Members are strictly sequential (see the module docstring):
    `hand_over` stops a member, and only then may the next be started.
    `stop_all` is the `finally` -- it stops exactly the members still
    running and lets their consume loops return, never sleeping for wall
    time.
    """

    def __init__(
        self, *, bus: _TappedBus, clock: ManualClock, state_store: ShardStateStore
    ) -> None:
        self._bus = bus
        self._clock = clock
        self._state_store = state_store
        self._running: list[AggregatorWorker] = []
        self._tasks: list[asyncio.Task[None]] = []
        self._started = 0

    def build(self, *, metrics: AggregatorMetrics | None = None) -> AggregatorWorker:
        """ASSUMPTION 1: construct the next member, with the next member id."""

        self._started += 1
        return _worker(
            bus=self._bus,
            clock=self._clock,
            state_store=self._state_store,
            metrics=metrics if metrics is not None else AggregatorMetrics(),
            member_id=f"aggregator-{self._started}",
        )

    async def start(self, *, metrics: AggregatorMetrics | None = None) -> AggregatorWorker:
        """Construct and `start()` the next member."""

        worker = self.build(metrics=metrics)
        await worker.start()
        self._running.append(worker)
        return worker

    def run(self, worker: AggregatorWorker) -> None:
        """ASSUMPTION 1: the member's consume loop, as a task `stop_all` drains."""

        self._tasks.append(asyncio.create_task(worker.run()))

    async def hand_over(self, worker: AggregatorWorker) -> None:
        """ADR-0013 decision 8: `stop()` is the way a shard changes hands.

        "`AggregatorWorker.stop()` is: stop fetching, finish the message in
        hand, `commit_handled()`, `release()`" -- the acknowledgement is what
        moves the group's position to the handled messages, and the release
        is what lets the next owner take the lease at once.
        """

        await worker.stop()
        self._running = [member for member in self._running if member is not worker]

    async def stop_all(self) -> None:
        for worker in self._running:
            await worker.stop()
        self._running = []
        for _ in range(1_000):
            if all(task.done() for task in self._tasks):
                break
            await asyncio.sleep(0)
        for task in self._tasks:
            if not task.done():
                task.cancel()
            with suppress(asyncio.CancelledError):
                await task
        self._tasks = []


class TestParseShardIds:
    """ADR-0013 decision 6: `HAMMERTIME_SHARD_IDS` is `all` or an explicit set
    within `0..127`; `parse_shard_ids` "no longer returns `None`"."""

    def test_all_is_the_explicit_full_set(self) -> None:
        # "expanded at parse time, not passed to the bus as
        # `partitions=None`, so that the aggregator always subscribes with an
        # explicit set and gets one durable per shard".
        assert parse_shard_ids("all") == frozenset(range(OBSERVATIONS.partitions))
        assert parse_shard_ids("all") == frozenset(range(128))

    @pytest.mark.parametrize("text", ["ALL", "All", "aLL", " all "])
    def test_all_is_case_insensitive(self, text: str) -> None:
        assert parse_shard_ids(text) == frozenset(range(128))

    def test_the_result_is_never_none(self) -> None:
        for text in ("all", "0", "0-127"):
            assert parse_shard_ids(text) is not None

    def test_a_single_id(self) -> None:
        assert parse_shard_ids("0") == frozenset({0})

    def test_an_inclusive_range(self) -> None:
        assert parse_shard_ids("0-3") == frozenset({0, 1, 2, 3})

    def test_a_mixture_of_ids_and_ranges(self) -> None:
        assert parse_shard_ids("0,2,5-7") == frozenset({0, 2, 5, 6, 7})

    def test_a_single_element_range_is_that_element(self) -> None:
        # "inclusive ranges" with `lo <= hi`; `3-3` is not a special case.
        assert parse_shard_ids("3-3") == frozenset({3})

    def test_duplicates_collapse(self) -> None:
        assert parse_shard_ids("1,1,0-2,2") == frozenset({0, 1, 2})

    def test_the_full_range_equals_all(self) -> None:
        assert parse_shard_ids("0-127") == parse_shard_ids("all")

    def test_the_highest_valid_id_is_accepted(self) -> None:
        assert parse_shard_ids("127") == frozenset({127})

    def test_the_result_is_a_frozenset(self) -> None:
        assert isinstance(parse_shard_ids("0-3"), frozenset)
        assert isinstance(parse_shard_ids("all"), frozenset)

    def test_auto_is_a_value_error_naming_the_accepted_forms(self) -> None:
        # "`auto` -> `ValueError` whose message says that shard assignment is
        # static since ADR-0013 and names the two accepted forms, so a
        # deployment carrying the old default fails loudly". As amended
        # (Amendment 1 ruling T10), the message "MUST contain the variable
        # name, the word `all`, `ADR-0013` and at least one explicit-set
        # example, and tests pin those four substrings rather than the whole
        # sentence". The examples the ADR's own wording carries are `'0'`,
        # `'0-3'` and `'0,2,5-7'`; the bare `0` would be satisfied by
        # `ADR-0013` itself, so the single-id example is matched quoted.
        with pytest.raises(ValueError, match="HAMMERTIME_SHARD_IDS") as excinfo:
            parse_shard_ids("auto")

        message = str(excinfo.value)
        assert "all" in message
        assert "ADR-0013" in message
        assert any(example in message for example in ("0-3", "0,2,5-7", "'0'"))

    @pytest.mark.parametrize("text", ["AUTO", "Auto"])
    def test_auto_is_rejected_whatever_its_case(self, text: str) -> None:
        with pytest.raises(ValueError):
            parse_shard_ids(text)

    @pytest.mark.parametrize("text", ["128", "0-128", "0,200", "127-130", "1000"])
    def test_an_id_at_or_above_the_partition_count_is_a_value_error(self, text: str) -> None:
        # "Every id MUST satisfy `0 <= id < OBSERVATIONS.partitions`; an id at
        # or above the count is a `ValueError` naming the variable and the
        # count".
        with pytest.raises(ValueError, match="HAMMERTIME_SHARD_IDS") as excinfo:
            parse_shard_ids(text)

        assert str(OBSERVATIONS.partitions) in str(excinfo.value)

    @pytest.mark.parametrize(
        "text",
        ["", "   ", "a", "3-1", "-1"],
        ids=["empty", "whitespace-only", "not-a-number", "descending-range", "negative"],
    )
    def test_a_rejected_value_raises(self, text: str) -> None:
        # ADR-0011 Amendment 1 item A3 rules the two empty cases, unchanged
        # by ADR-0013: "`parse_shard_ids("")` and `parse_shard_ids("   ")`
        # raise `ValueError`", so that `load_settings` can exit 2 before any
        # bus, store or socket is opened.
        with pytest.raises(ValueError):
            parse_shard_ids(text)


class TestShardClaimsIsTheAssignmentListener:
    """ADR-0013 decision 8: `ShardClaims` "satisfies AssignmentListener", the
    protocol of decision 3 -- which has `on_assigned` only."""

    async def test_it_satisfies_the_assignment_listener_protocol(self) -> None:
        claims = await _claims(clock=ManualClock(initial=BASE))
        assert isinstance(claims, AssignmentListener)

    async def test_it_has_no_on_revoked(self) -> None:
        # "**`on_revoked` is gone** from `AssignmentListener`, from
        # `ShardClaims` and from `AggregatorWorker`."
        claims = await _claims(clock=ManualClock(initial=BASE))
        assert not hasattr(claims, "on_revoked")

    def test_the_worker_has_no_on_revoked_either(self) -> None:
        worker = _worker(bus=InMemoryBus(), clock=ManualClock(initial=BASE))
        assert not hasattr(worker, "on_revoked")


class TestClaimingAShard:
    """ADR-0011 decision 5, `on_assigned` (still in force): load the shard's
    state, build its window. The lease taken first is
    `TestShardLeasesAreTakenOnClaim`'s."""

    async def test_a_claim_creates_a_window_for_the_partition(self) -> None:
        clock = ManualClock(initial=BASE)
        claims = await _claims(clock=clock)

        await claims.on_assigned(frozenset({(OBSERVATIONS_TOPIC, 0)}))

        window = claims.window(0)
        assert window is not None
        assert window.shard == 0
        assert claims.shards == frozenset({0})
        assert claims.windows() == (window,)

    async def test_a_never_claimed_shard_starts_empty_and_never_warms_up(self) -> None:
        clock = ManualClock(initial=BASE)
        claims = await _claims(clock=clock)

        await claims.on_assigned(frozenset({(OBSERVATIONS_TOPIC, 0)}))

        window = claims.window(0)
        assert window is not None
        assert window.tracked_count == 0
        assert window.hot_ips() == frozenset()
        assert window.next_sequence == 0
        # "warm_until is clock.now() + window_seconds at construction iff
        # inherited_hot is non-empty".
        assert window.warm_until is None
        assert window.in_warmup is False

    async def test_a_claim_inherits_the_persisted_hot_set_and_sequence(self) -> None:
        clock = ManualClock(initial=BASE)
        state_store = MemoryShardStateStore(clock=clock)
        await state_store.record_transition(0, IP_A, IpState.HOT, 4)
        await state_store.record_transition(0, IP_B, IpState.HOT, 9)
        claims = await _claims(clock=clock, state_store=state_store)

        await claims.on_assigned(frozenset({(OBSERVATIONS_TOPIC, 0)}))

        window = claims.window(0)
        assert window is not None
        assert window.hot_ips() == frozenset({IP_A, IP_B})
        assert window.next_sequence == 10

    async def test_every_inherited_ip_is_a_tracked_entry_from_construction(self) -> None:
        # Amendment 2 item A5, and the observable half of the `shard_claimed
        # ... inherited_hot=2` log record.
        clock = ManualClock(initial=BASE)
        state_store = MemoryShardStateStore(clock=clock)
        await state_store.record_transition(0, IP_A, IpState.HOT, 0)
        await state_store.record_transition(0, IP_B, IpState.HOT, 1)
        claims = await _claims(clock=clock, state_store=state_store)

        await claims.on_assigned(frozenset({(OBSERVATIONS_TOPIC, 0)}))

        window = claims.window(0)
        assert window is not None
        assert window.tracked_count == 2
        assert window.hot_count == 2
        # Not active: an inherited IP's ring is empty until its first applied
        # observation, so its total is 0.
        assert window.active_count == 0
        assert window.is_inherited(IP_A) is True
        assert window.is_inherited(IP_B) is True
        assert window.state(IP_A) is IpState.HOT
        assert window.total(IP_A) == 0

    async def test_warm_up_runs_for_one_window_from_the_claim(self) -> None:
        clock = ManualClock(initial=BASE)
        state_store = MemoryShardStateStore(clock=clock)
        await state_store.record_transition(0, IP_A, IpState.HOT, 0)
        claims = await _claims(clock=clock, state_store=state_store)

        await claims.on_assigned(frozenset({(OBSERVATIONS_TOPIC, 0)}))

        window = claims.window(0)
        assert window is not None
        assert window.warm_until == BASE + WINDOW_SECONDS
        assert window.in_warmup is True

    async def test_the_claim_time_is_the_clock_not_the_process_start(self) -> None:
        # A later claim warms up from *its* moment: `warm_until` is
        # `clock.now() + window_seconds` at construction.
        clock = ManualClock(initial=BASE)
        state_store = MemoryShardStateStore(clock=clock)
        await state_store.record_transition(0, IP_A, IpState.HOT, 0)
        claims = await _claims(clock=clock, state_store=state_store)
        clock.advance(1_000)

        await claims.on_assigned(frozenset({(OBSERVATIONS_TOPIC, 0)}))

        window = claims.window(0)
        assert window is not None
        assert window.warm_until == BASE + 1_000 + WINDOW_SECONDS

    async def test_several_partitions_are_claimed_independently(self) -> None:
        # Section 20: a shard owns its own state. Two claimed partitions get
        # two windows, each seeded from its own `ShardState`.
        clock = ManualClock(initial=BASE)
        state_store = MemoryShardStateStore(clock=clock)
        await state_store.record_transition(0, IP_A, IpState.HOT, 0)
        await state_store.record_transition(1, IP_B, IpState.HOT, 3)
        claims = await _claims(clock=clock, state_store=state_store)

        await claims.on_assigned(frozenset({(OBSERVATIONS_TOPIC, 0), (OBSERVATIONS_TOPIC, 1)}))

        assert claims.shards == frozenset({0, 1})
        first = claims.window(0)
        second = claims.window(1)
        assert first is not None
        assert second is not None
        assert first.shard == 0
        assert second.shard == 1
        assert first.hot_ips() == frozenset({IP_A})
        assert second.hot_ips() == frozenset({IP_B})
        assert first.next_sequence == 1
        assert second.next_sequence == 4

    async def test_on_assigned_with_an_empty_set_is_a_value_error(self) -> None:
        # ASSUMPTION 10 (ruled, T5; assumption 31): "`on_assigned(frozenset())`
        # is a `ValueError`" -- "a member holding nothing must not report
        # itself ready", so the direct call is refused as the bus refuses it.
        # It holds nothing afterwards and never reaches the store: no lease
        # is taken and nothing is loaded.
        clock = ManualClock(initial=BASE)
        store = _RecordingStore(clock)
        claims = await _claims(clock=clock, state_store=store)
        store.clear()

        with pytest.raises(ValueError):
            await claims.on_assigned(frozenset())

        assert claims.shards == frozenset()
        assert claims.windows() == ()
        assert store.calls == []


class TestShardLeasesAreTakenOnClaim:
    """ADR-0013 decision 7: "Before a member builds a `ShardWindow` for shard
    `p`, it must hold the shard's lease" -- `acquire_lease(p, <owner token>,
    lease_ttl_s)` in `on_assigned`, before `state_store.load(p)`; a
    non-`None` result is a refusal that releases every lease this call
    acquired and raises `ShardOwnedElsewhereError`. The owner is the
    process's token since Amendment 6 ruling 2: "`ShardClaims` passes
    `lease_owner = f"{member_id}/{instance_id}"`"."""

    async def test_the_lease_is_acquired_before_the_shard_is_loaded(self) -> None:
        # ASSUMPTION 5.
        clock = ManualClock(initial=BASE)
        store = _RecordingStore(clock)
        claims = await _claims(clock=clock, state_store=store)

        await claims.on_assigned(frozenset({(OBSERVATIONS_TOPIC, 0)}))

        methods = [call[0] for call in store.calls]
        assert methods.index("acquire_lease") < methods.index("load")
        assert ("acquire_lease", 0, OWNER_A, LEASE_TTL_SECONDS) in store.calls
        assert ("load", 0) in store.calls

    async def test_the_lease_carries_the_owner_token_and_ttl(self) -> None:
        # Amendment 6 ruling 2(a): the `owner` argument is the claim's
        # `lease_owner`, member id and instance token, and nothing else.
        clock = ManualClock(initial=BASE)
        store = _RecordingStore(clock)
        claims = await _claims(
            clock=clock,
            state_store=store,
            member_id="aggregator-7",
            instance_id=INSTANCE_B,
            lease_ttl_s=45.0,
        )

        await claims.on_assigned(frozenset({(OBSERVATIONS_TOPIC, 0)}))

        assert claims.lease_owner == _owner("aggregator-7", INSTANCE_B)
        assert ("acquire_lease", 0, _owner("aggregator-7", INSTANCE_B), 45.0) in store.calls

    async def test_the_claimed_shard_is_leased_to_this_member(self) -> None:
        clock = ManualClock(initial=BASE)
        state_store = MemoryShardStateStore(clock=clock)
        claims = await _claims(clock=clock, state_store=state_store)

        await claims.on_assigned(frozenset({(OBSERVATIONS_TOPIC, 0)}))

        assert await state_store.acquire_lease(0, OTHER_MEMBER, 1.0) == OWNER_A

    async def test_every_shard_is_leased_in_sorted_order_before_its_load(self) -> None:
        # ASSUMPTION 5 (ruled, T6): "strictly sequentially per shard: acquire
        # `p`, load `p`, build `p`'s window, and only then the next shard's
        # acquire" -- the ordered `acquire_lease`/`load` subsequence is
        # exactly `acquire 0, load 0, acquire 1, load 1, acquire 2, load 2`.
        clock = ManualClock(initial=BASE)
        store = _RecordingStore(clock)
        claims = await _claims(clock=clock, state_store=store)

        await claims.on_assigned(
            frozenset({(OBSERVATIONS_TOPIC, 2), (OBSERVATIONS_TOPIC, 0), (OBSERVATIONS_TOPIC, 1)})
        )

        acquires = [call for call in store.calls if call[0] == "acquire_lease"]
        assert [call[1] for call in acquires] == [0, 1, 2]
        for shard in (0, 1, 2):
            acquire_at = store.calls.index(("acquire_lease", shard, OWNER_A, LEASE_TTL_SECONDS))
            load_at = store.calls.index(("load", shard))
            assert acquire_at < load_at
        sequence = [call[:2] for call in store.calls if call[0] in ("acquire_lease", "load")]
        assert sequence == [
            ("acquire_lease", 0),
            ("load", 0),
            ("acquire_lease", 1),
            ("load", 1),
            ("acquire_lease", 2),
            ("load", 2),
        ]
        assert claims.shards == frozenset({0, 1, 2})

    @pytest.mark.parametrize(
        "holder",
        [OTHER_MEMBER, _owner(OTHER_MEMBER, INSTANCE_B)],
        ids=["a-bare-member-id", "a-member-id-with-an-instance"],
    )
    async def test_a_shard_leased_elsewhere_is_refused(self, holder: str) -> None:
        # ASSUMPTION 15. Amendment 6 ruling 2(b): "A holder whose member id
        # differs is decision 7's refusal, unchanged", whether the holder is
        # a token or a bare id left by a build before the amendment -- and it
        # is never the same-member case, which is the only one `start()`
        # waits out.
        clock = ManualClock(initial=BASE)
        state_store = MemoryShardStateStore(clock=clock)
        await state_store.acquire_lease(0, holder, LEASE_TTL_SECONDS)
        claims = await _claims(clock=clock, state_store=state_store)

        with pytest.raises(ShardOwnedElsewhereError) as excinfo:
            await claims.on_assigned(frozenset({(OBSERVATIONS_TOPIC, 0)}))

        assert not isinstance(excinfo.value, ShardHeldBySameMemberError)

    async def test_a_refused_claim_holds_no_window(self) -> None:
        clock = ManualClock(initial=BASE)
        state_store = MemoryShardStateStore(clock=clock)
        await state_store.acquire_lease(0, OTHER_MEMBER, LEASE_TTL_SECONDS)
        claims = await _claims(clock=clock, state_store=state_store)

        with pytest.raises(ShardOwnedElsewhereError):
            await claims.on_assigned(frozenset({(OBSERVATIONS_TOPIC, 0)}))

        assert claims.window(0) is None
        assert claims.shards == frozenset()
        assert claims.windows() == ()

    async def test_a_refused_claim_does_not_load_the_shard(self) -> None:
        # The lease is checked *before* `load`, so a refused shard is never
        # read.
        clock = ManualClock(initial=BASE)
        store = _RecordingStore(clock)
        await store.acquire_lease(0, OTHER_MEMBER, LEASE_TTL_SECONDS)
        store.clear()
        claims = await _claims(clock=clock, state_store=store)

        with pytest.raises(ShardOwnedElsewhereError):
            await claims.on_assigned(frozenset({(OBSERVATIONS_TOPIC, 0)}))

        assert ("load", 0) not in store.calls

    async def test_a_refused_claim_releases_the_leases_it_took_in_the_same_call(self) -> None:
        # "releases every lease this call acquired so far, and raises":
        # shard 0 was free and taken; shard 1 is elsewhere; afterwards shard 0
        # is free again for anyone.
        clock = ManualClock(initial=BASE)
        state_store = MemoryShardStateStore(clock=clock)
        await state_store.acquire_lease(1, OTHER_MEMBER, LONG_LEASE_SECONDS)
        claims = await _claims(clock=clock, state_store=state_store)

        with pytest.raises(ShardOwnedElsewhereError):
            await claims.on_assigned(frozenset({(OBSERVATIONS_TOPIC, 0), (OBSERVATIONS_TOPIC, 1)}))

        assert await state_store.acquire_lease(0, MEMBER_C, 1.0) is None
        assert await state_store.acquire_lease(1, MEMBER_C, 1.0) == OTHER_MEMBER
        assert claims.window(0) is None
        assert claims.window(1) is None
        assert claims.shards == frozenset()

    async def test_the_release_after_a_refusal_goes_through_the_store(self) -> None:
        clock = ManualClock(initial=BASE)
        store = _RecordingStore(clock)
        await store.acquire_lease(1, OTHER_MEMBER, LONG_LEASE_SECONDS)
        store.clear()
        claims = await _claims(clock=clock, state_store=store)

        with pytest.raises(ShardOwnedElsewhereError):
            await claims.on_assigned(frozenset({(OBSERVATIONS_TOPIC, 0), (OBSERVATIONS_TOPIC, 1)}))

        assert ("release_lease", 0, OWNER_A) in store.calls
        assert ("release_lease", 1, OWNER_A) not in store.calls

    async def test_the_refusal_propagates_out_of_subscribe(self) -> None:
        # "The exception propagates out of `subscribe()`": the listener is
        # awaited inside it (decision 3), and a failing listener "holds
        # nothing".
        clock = ManualClock(initial=BASE)
        bus = InMemoryBus()
        state_store = MemoryShardStateStore(clock=clock)
        await state_store.acquire_lease(0, OTHER_MEMBER, LEASE_TTL_SECONDS)
        consumer = bus.consumer(GROUP)
        claims = ShardClaims(
            state_store=state_store,
            producer=bus.producer(),
            consumer=consumer,
            clock=clock,
            config=DEFAULTS,
            member_id=MEMBER_A,
            lease_ttl_s=LEASE_TTL_SECONDS,
            instance_id=INSTANCE_A,
        )

        with pytest.raises(ShardOwnedElsewhereError):
            await consumer.subscribe(OBSERVATIONS_TOPIC, listener=claims)

        assert claims.window(0) is None

    async def test_the_refusal_propagates_out_of_the_workers_start(self) -> None:
        # "... out of `worker.start()` and out of `service.start()`; it is
        # not in any transient tuple, so `run_service` logs `start_failed`
        # and the process exits **1**".
        clock = ManualClock(initial=BASE)
        bus = InMemoryBus()
        state_store = MemoryShardStateStore(clock=clock)
        await state_store.acquire_lease(0, OTHER_MEMBER, LEASE_TTL_SECONDS)
        worker = _worker(bus=bus, clock=clock, state_store=state_store)

        with pytest.raises(ShardOwnedElsewhereError):
            await worker.start()

        assert worker.window(0) is None
        assert worker.shards == frozenset()
        # The other member still holds it: nothing was taken from it.
        assert await state_store.acquire_lease(0, MEMBER_C, 1.0) == OTHER_MEMBER

    async def test_a_lapsed_lease_no_longer_refuses(self) -> None:
        # Decision 7: "A crashed member's leases expire after `lease_ttl_s`
        # ... one with a different id waits at most `lease_ttl_s`".
        clock = ManualClock(initial=BASE)
        state_store = MemoryShardStateStore(clock=clock)
        await state_store.acquire_lease(0, OTHER_MEMBER, LEASE_TTL_SECONDS)
        claims = await _claims(clock=clock, state_store=state_store)
        clock.advance(int(LEASE_TTL_SECONDS) + 1)

        await claims.on_assigned(frozenset({(OBSERVATIONS_TOPIC, 0)}))

        assert claims.window(0) is not None
        assert await state_store.acquire_lease(0, OTHER_MEMBER, 1.0) == OWNER_A


class TestTheInstanceTokenTellsProcessesOfOneMemberApart:
    """ADR-0013 decision 7 as amended (Amendment 6 ruling 2): "the holder a
    refused `acquire_lease` returns is split at its last `/` into a member id
    and an instance id (a value with no `/` is a member id with an empty
    instance). A holder whose member id is not this process's is the refusal
    above, unchanged. A holder with this process's member id and another
    instance -- a predecessor that died without releasing, or a live twin
    started under the same `HAMMERTIME_AGGREGATOR_MEMBER_ID`; the lease cannot
    tell which at that moment -- is logged `WARNING
    event=shard_held_by_same_member ...`, rolled back exactly as above, and
    raised as `ShardHeldBySameMemberError(shard, owner)`, a subclass of
    `ShardOwnedElsewhereError`. ... The same-instance re-acquire ... is
    unaffected: it passes the same token and is a renewal."

    Ruling 2(a) is what makes the two cases distinguishable at all: the three
    behaviours it calls load-bearing -- `renew_leases()` "passes the token the
    process acquired under and is granted as before", the re-acquire of a
    leased-but-unwindowed shard "passes the same token and is a renewal", and
    "`release()` deletes only this process's own leases -- which the member-id
    value did not guarantee: a stopping twin released the survivor's shards"
    -- are each pinned below.

    The `shard_held_by_same_member` record itself is not asserted, for the
    reason the module docstring gives for every log record; what is asserted
    is the exception type, the rollback and what the store holds afterwards.
    """

    def _assigned(self, *shards: int) -> frozenset[tuple[str, int]]:
        return frozenset((OBSERVATIONS_TOPIC, shard) for shard in shards)

    async def test_a_live_lease_under_this_claims_own_token_is_a_renewal(self) -> None:
        # "the same instance is a renewal the store grants, so this is the
        # only other case": the store "grants iff no live lease exists or the
        # live lease's value equals the caller's token".
        clock = ManualClock(initial=BASE)
        state_store = MemoryShardStateStore(clock=clock)
        await state_store.acquire_lease(0, OWNER_A, LONG_LEASE_SECONDS)
        claims = await _claims(clock=clock, state_store=state_store)

        await claims.on_assigned(self._assigned(0))

        assert claims.window(0) is not None
        assert claims.shards == frozenset({0})
        assert await state_store.acquire_lease(0, OTHER_MEMBER, 1.0) == OWNER_A

    async def test_a_second_on_assigned_after_release_passes_the_same_token(self) -> None:
        # The token is "stable for the process's life": one `ShardClaims`
        # acquires under one owner, whatever it has released in between.
        clock = ManualClock(initial=BASE)
        store = _RecordingStore(clock)
        claims = await _claims(clock=clock, state_store=store)
        await claims.on_assigned(self._assigned(0))
        await claims.release()
        store.clear()

        await claims.on_assigned(self._assigned(0))

        assert claims.lease_owner == OWNER_A
        assert ("acquire_lease", 0, OWNER_A, LEASE_TTL_SECONDS) in store.calls
        assert claims.window(0) is not None

    async def test_another_instance_of_the_same_member_is_refused(self) -> None:
        # "A holder with this process's member id and another instance ... is
        # ... `ShardHeldBySameMemberError(shard, owner)`, a subclass of
        # `ShardOwnedElsewhereError`" (assumption 100).
        clock = ManualClock(initial=BASE)
        state_store = MemoryShardStateStore(clock=clock)
        await state_store.acquire_lease(0, _owner(MEMBER_A, INSTANCE_B), LONG_LEASE_SECONDS)
        claims = await _claims(clock=clock, state_store=state_store)

        with pytest.raises(ShardHeldBySameMemberError) as excinfo:
            await claims.on_assigned(self._assigned(0))

        assert isinstance(excinfo.value, ShardOwnedElsewhereError)

    async def test_the_same_member_refusal_holds_no_window_and_leaves_the_holder(self) -> None:
        # "rolled back exactly as above": the refused claim holds nothing, and
        # the twin -- or the dead predecessor -- is still the holder.
        clock = ManualClock(initial=BASE)
        state_store = MemoryShardStateStore(clock=clock)
        twin = _owner(MEMBER_A, INSTANCE_B)
        await state_store.acquire_lease(0, twin, LONG_LEASE_SECONDS)
        claims = await _claims(clock=clock, state_store=state_store)

        with pytest.raises(ShardHeldBySameMemberError):
            await claims.on_assigned(self._assigned(0))

        assert claims.window(0) is None
        assert claims.shards == frozenset()
        assert claims.windows() == ()
        assert await state_store.acquire_lease(0, MEMBER_C, 1.0) == twin

    async def test_the_same_member_refusal_releases_the_leases_taken_earlier_in_the_call(
        self,
    ) -> None:
        # The mirror of the other-member rollback above ("releases every lease
        # this call acquired so far, and raises"): shard 0 was free and taken,
        # shard 1 is the twin's, and the rollback frees shard 0 for anyone.
        clock = ManualClock(initial=BASE)
        store = _RecordingStore(clock)
        twin = _owner(MEMBER_A, INSTANCE_B)
        await store.acquire_lease(1, twin, LONG_LEASE_SECONDS)
        store.clear()
        claims = await _claims(clock=clock, state_store=store)

        with pytest.raises(ShardHeldBySameMemberError):
            await claims.on_assigned(self._assigned(0, 1))

        assert ("release_lease", 0, OWNER_A) in store.calls
        assert ("release_lease", 1, OWNER_A) not in store.calls
        assert claims.shards == frozenset()
        assert await store.acquire_lease(0, MEMBER_C, 1.0) is None
        assert await store.acquire_lease(1, MEMBER_C, 1.0) == twin

    async def test_a_bare_holder_equal_to_this_member_id_is_the_same_member_case(self) -> None:
        # Assumption 99: "A lease value written by a build before this
        # amendment (a plain member id) ... is then classified by member
        # equality -- waited out if it names this member".
        clock = ManualClock(initial=BASE)
        state_store = MemoryShardStateStore(clock=clock)
        await state_store.acquire_lease(0, MEMBER_A, LONG_LEASE_SECONDS)
        claims = await _claims(clock=clock, state_store=state_store)

        with pytest.raises(ShardHeldBySameMemberError):
            await claims.on_assigned(self._assigned(0))

        assert claims.window(0) is None
        assert await state_store.acquire_lease(0, MEMBER_C, 1.0) == MEMBER_A

    async def test_a_member_id_containing_a_slash_is_split_at_the_last_one(self) -> None:
        # Ruling 2(b) and assumption 99: "an operator-set `member_id` may
        # [contain `/`], which is why the split is from the right and the
        # instance never contains one" -- so `region-1/aggregator-a` is one
        # member, and its two instances are told apart rather than read as two
        # members.
        clock = ManualClock(initial=BASE)
        state_store = MemoryShardStateStore(clock=clock)
        member = "region-1/aggregator-a"
        await state_store.acquire_lease(0, _owner(member, INSTANCE_B), LONG_LEASE_SECONDS)
        claims = await _claims(
            clock=clock, state_store=state_store, member_id=member, instance_id=INSTANCE_A
        )

        with pytest.raises(ShardHeldBySameMemberError):
            await claims.on_assigned(self._assigned(0))

    async def test_the_predecessors_lease_is_claimed_once_it_lapses(self) -> None:
        # Ruling 3: the replacement "is refused with `shard_held_by_same_member`,
        # retries under the startup deadline, takes the shards the moment the
        # predecessor's leases lapse". Here the retry is the second direct
        # call; `test_service.py` owns the retry loop itself.
        clock = ManualClock(initial=BASE)
        state_store = MemoryShardStateStore(clock=clock)
        await state_store.acquire_lease(0, _owner(MEMBER_A, INSTANCE_B), LEASE_TTL_SECONDS)
        claims = await _claims(clock=clock, state_store=state_store)
        with pytest.raises(ShardHeldBySameMemberError):
            await claims.on_assigned(self._assigned(0))

        clock.advance(int(LEASE_TTL_SECONDS) + 1)
        await claims.on_assigned(self._assigned(0))

        assert claims.window(0) is not None
        assert claims.shards == frozenset({0})
        assert await state_store.acquire_lease(0, OTHER_MEMBER, 1.0) == OWNER_A

    async def test_renew_leases_passes_the_token(self) -> None:
        # Ruling 2(a): "`renew_leases()` passes the token the process acquired
        # under and is granted as before".
        clock = ManualClock(initial=BASE)
        store = _RecordingStore(clock)
        claims = await _claims(clock=clock, state_store=store)
        await claims.on_assigned(self._assigned(0))
        store.clear()

        await claims.renew_leases()

        assert ("acquire_lease", 0, OWNER_A, LEASE_TTL_SECONDS) in store.calls

    async def test_a_twins_token_in_the_store_is_a_lease_lost_error(self) -> None:
        # Decision 7: a non-`None` result from the renewal "logs `ERROR
        # event=shard_lease_lost ...` and raises `ShardLeaseLostError(shard,
        # owner)`" -- and since Amendment 6 a twin of this member's own id is
        # such an owner, because its token is not this process's.
        clock = ManualClock(initial=BASE)
        state_store = MemoryShardStateStore(clock=clock)
        claims = await _claims(clock=clock, state_store=state_store)
        await claims.on_assigned(self._assigned(0))
        clock.advance(int(LEASE_TTL_SECONDS) + 1)
        assert (
            await state_store.acquire_lease(0, _owner(MEMBER_A, INSTANCE_B), LONG_LEASE_SECONDS)
            is None
        )

        with pytest.raises(ShardLeaseLostError):
            await claims.renew_leases()

    async def test_release_leaves_the_other_instances_lease_in_place(self) -> None:
        # Ruling 2(a): "`release()` deletes only this process's own leases --
        # which the member-id value did not guarantee: a stopping twin
        # released the survivor's shards." This claim's lease lapses, the twin
        # takes the shard, and this claim's `release()` must not free it.
        clock = ManualClock(initial=BASE)
        state_store = MemoryShardStateStore(clock=clock)
        claims = await _claims(clock=clock, state_store=state_store)
        await claims.on_assigned(self._assigned(0))
        clock.advance(int(LEASE_TTL_SECONDS) + 1)
        twin = _owner(MEMBER_A, INSTANCE_B)
        assert await state_store.acquire_lease(0, twin, LONG_LEASE_SECONDS) is None

        await claims.release()

        assert await state_store.acquire_lease(0, MEMBER_C, 1.0) == twin

    async def test_two_claims_built_without_an_instance_id_get_distinct_tokens(self) -> None:
        # Ruling 2(a): the default is `secrets.token_hex(8)`, "generated once
        # per construction", so "a second process started under the same
        # `member_id` is refused rather than treated as a renewal"; the owner
        # every lease call carries is `f"{member_id}/{instance_id}"`.
        clock = ManualClock(initial=BASE)
        first = await _claims(clock=clock, instance_id=None)
        second = await _claims(clock=clock, instance_id=None)

        assert first.member_id == MEMBER_A
        assert second.member_id == MEMBER_A
        assert first.instance_id
        assert second.instance_id
        assert first.instance_id != second.instance_id
        assert first.lease_owner == f"{first.member_id}/{first.instance_id}"
        assert second.lease_owner == f"{second.member_id}/{second.instance_id}"
        assert first.lease_owner != second.lease_owner

    async def test_a_generated_token_refuses_a_second_process_of_the_same_member(self) -> None:
        # The two halves together: two claims built as two processes are,
        # sharing one `member_id` and a store, and the second is refused with
        # the same-member error rather than granted a renewal -- #90's
        # residual, "both load the same `next_sequence`, build independent
        # windows and publish under one identity with colliding sequences".
        clock = ManualClock(initial=BASE)
        state_store = MemoryShardStateStore(clock=clock)
        first = await _claims(clock=clock, state_store=state_store, instance_id=None)
        second = await _claims(clock=clock, state_store=state_store, instance_id=None)
        await first.on_assigned(self._assigned(0))

        with pytest.raises(ShardHeldBySameMemberError):
            await second.on_assigned(self._assigned(0))

        assert second.window(0) is None
        assert first.window(0) is not None
        assert await state_store.acquire_lease(0, MEMBER_C, 1.0) == first.lease_owner


class TestRenewingLeases:
    """ADR-0013 decision 7: "`ShardClaims.renew_leases()` calls `acquire_lease`
    for every held shard; `AggregatorWorker.run_maintenance()` calls it
    **first**, before the expiry sweep"; a refusal is `ShardLeaseLostError`."""

    async def test_renew_leases_reacquires_every_held_shard(self) -> None:
        clock = ManualClock(initial=BASE)
        store = _RecordingStore(clock)
        claims = await _claims(clock=clock, state_store=store)
        await claims.on_assigned(frozenset({(OBSERVATIONS_TOPIC, 0), (OBSERVATIONS_TOPIC, 1)}))
        store.clear()

        await claims.renew_leases()

        assert sorted(call for call in store.calls if call[0] == "acquire_lease") == [
            ("acquire_lease", 0, OWNER_A, LEASE_TTL_SECONDS),
            ("acquire_lease", 1, OWNER_A, LEASE_TTL_SECONDS),
        ]
        assert ("load", 0) not in store.calls
        assert ("load", 1) not in store.calls

    async def test_a_renewal_keeps_the_lease_past_its_original_expiry(self) -> None:
        clock = ManualClock(initial=BASE)
        state_store = MemoryShardStateStore(clock=clock)
        claims = await _claims(clock=clock, state_store=state_store)
        await claims.on_assigned(frozenset({(OBSERVATIONS_TOPIC, 0)}))
        clock.advance(20)

        await claims.renew_leases()
        clock.advance(20)  # 40 s after the claim, 20 s after the renewal

        assert await state_store.acquire_lease(0, OTHER_MEMBER, 1.0) == OWNER_A

    async def test_renew_leases_with_nothing_held_touches_nothing(self) -> None:
        clock = ManualClock(initial=BASE)
        store = _RecordingStore(clock)
        claims = await _claims(clock=clock, state_store=store)

        await claims.renew_leases()

        assert store.calls == []

    async def test_a_lease_taken_by_another_member_is_a_lease_lost_error(self) -> None:
        # The lease lapsed (the member stalled longer than `lease_ttl_s`) and
        # another member took the shard; the next renewal is refused.
        clock = ManualClock(initial=BASE)
        state_store = MemoryShardStateStore(clock=clock)
        claims = await _claims(clock=clock, state_store=state_store)
        await claims.on_assigned(frozenset({(OBSERVATIONS_TOPIC, 0)}))
        clock.advance(int(LEASE_TTL_SECONDS) + 1)
        assert await state_store.acquire_lease(0, OTHER_MEMBER, LONG_LEASE_SECONDS) is None

        with pytest.raises(ShardLeaseLostError):
            await claims.renew_leases()

    async def test_a_lapsed_but_untaken_lease_is_simply_reacquired(self) -> None:
        # "a lease is granted iff no live lease exists": nobody else took it,
        # so the renewal is a fresh grant, not a loss.
        clock = ManualClock(initial=BASE)
        state_store = MemoryShardStateStore(clock=clock)
        claims = await _claims(clock=clock, state_store=state_store)
        await claims.on_assigned(frozenset({(OBSERVATIONS_TOPIC, 0)}))
        clock.advance(int(LEASE_TTL_SECONDS) + 1)

        await claims.renew_leases()

        assert await state_store.acquire_lease(0, OTHER_MEMBER, 1.0) == OWNER_A

    async def test_run_maintenance_renews_the_leases_before_it_sweeps(self) -> None:
        # ASSUMPTION 8. The demotion of IP_A is due in this sweep (its only
        # bucket left the window at BASE + 300), so the sweep's first store
        # write is observable and the renewal must come before it.
        clock = ManualClock(initial=BASE)
        trace: list[str] = []
        bus = _TappedBus(trace)
        store = _RecordingStore(clock, trace)
        feed = _Feed(bus)
        worker = _worker(bus=bus, clock=clock, state_store=store, lease_ttl_s=LEASE_TTL_SECONDS)
        await worker.start()
        try:
            assert await feed.deliver(worker, IP_A, 1200) is ObservationOutcome.APPLIED
            clock.advance(310)
            store.clear()

            await worker.run_maintenance()

            assert trace[0] == "acquire_lease"
            assert "record_transition" in trace
            assert trace.index("acquire_lease") < trace.index("record_transition")
            assert ("acquire_lease", 0, OWNER_A, LEASE_TTL_SECONDS) in store.calls
            assert [envelope.event_type for envelope in _hot_ip_events(bus)] == [
                "HotIpAdded",
                "HotIpRemoved",
            ]
        finally:
            await worker.stop()

    async def test_run_maintenance_renews_even_when_nothing_is_due(self) -> None:
        clock = ManualClock(initial=BASE)
        store = _RecordingStore(clock)
        bus = _TappedBus()
        worker = _worker(bus=bus, clock=clock, state_store=store, lease_ttl_s=LEASE_TTL_SECONDS)
        await worker.start()
        try:
            store.clear()

            await worker.run_maintenance()

            assert ("acquire_lease", 0, OWNER_A, LEASE_TTL_SECONDS) in store.calls
        finally:
            await worker.stop()

    async def test_a_lost_lease_fails_run_maintenance_before_the_sweep(self) -> None:
        # ASSUMPTION 8: "so a member that has lost a shard emits nothing more
        # for it". The demotion of IP_A is due, the lease was taken by
        # another member during the stall, and the sweep never runs: no
        # `HotIpRemoved`, the store's HOT set unchanged.
        clock = ManualClock(initial=BASE)
        bus = _TappedBus()
        state_store = MemoryShardStateStore(clock=clock)
        feed = _Feed(bus)
        worker = _worker(
            bus=bus, clock=clock, state_store=state_store, lease_ttl_s=LEASE_TTL_SECONDS
        )
        await worker.start()
        try:
            assert await feed.deliver(worker, IP_A, 1200) is ObservationOutcome.APPLIED
            clock.advance(310)  # the lease (30 s) lapsed during the stall
            assert await state_store.acquire_lease(0, OTHER_MEMBER, LONG_LEASE_SECONDS) is None

            with pytest.raises(ShardLeaseLostError):
                await worker.run_maintenance()

            assert [envelope.event_type for envelope in _hot_ip_events(bus)] == ["HotIpAdded"]
            assert (await state_store.load(0)).hot_ips == frozenset({IP_A})
            window = worker.window(0)
            assert window is not None
            assert window.state(IP_A) is IpState.HOT
        finally:
            await worker.stop()


class TestReleasingLeases:
    """ADR-0013 decision 7: "`ShardClaims.release()` calls `release_lease` for
    every held shard; `AggregatorWorker.stop()` calls it after the final
    flush-and-ack (decision 8), so a clean stop hands the shards over
    immediately."""

    async def test_release_frees_every_held_lease(self) -> None:
        clock = ManualClock(initial=BASE)
        state_store = MemoryShardStateStore(clock=clock)
        claims = await _claims(clock=clock, state_store=state_store, lease_ttl_s=LONG_LEASE_SECONDS)
        await claims.on_assigned(frozenset({(OBSERVATIONS_TOPIC, 0), (OBSERVATIONS_TOPIC, 1)}))

        await claims.release()

        assert await state_store.acquire_lease(0, OTHER_MEMBER, 1.0) is None
        assert await state_store.acquire_lease(1, OTHER_MEMBER, 1.0) is None

    async def test_release_goes_through_the_store_for_each_shard(self) -> None:
        clock = ManualClock(initial=BASE)
        store = _RecordingStore(clock)
        claims = await _claims(clock=clock, state_store=store)
        await claims.on_assigned(frozenset({(OBSERVATIONS_TOPIC, 0), (OBSERVATIONS_TOPIC, 1)}))
        store.clear()

        await claims.release()

        assert sorted(call for call in store.calls if call[0] == "release_lease") == [
            ("release_lease", 0, OWNER_A),
            ("release_lease", 1, OWNER_A),
        ]

    async def test_release_writes_nothing_and_emits_nothing(self) -> None:
        # The handover itself is silent: "nothing is emitted; the next owner
        # inherits the HOT set and warms up" (ADR-0011 decision 5, still in
        # force through `stop()`).
        clock = ManualClock(initial=BASE)
        bus = InMemoryBus()
        state_store = MemoryShardStateStore(clock=clock)
        await state_store.record_transition(0, IP_A, IpState.HOT, 0)
        claims = await _claims(clock=clock, bus=bus, state_store=state_store)
        await claims.on_assigned(frozenset({(OBSERVATIONS_TOPIC, 0)}))
        before = await state_store.load(0)

        await claims.release()

        assert await state_store.load(0) == before
        assert _records(bus, HOT_IP_TOPIC) == []

    async def test_release_with_nothing_held_touches_nothing(self) -> None:
        clock = ManualClock(initial=BASE)
        store = _RecordingStore(clock)
        claims = await _claims(clock=clock, state_store=store)

        await claims.release()

        assert store.calls == []

    async def test_stop_releases_after_the_final_flush_and_ack(self) -> None:
        # Decision 8's `stop()`: "stop fetching, finish the message in hand,
        # `commit_handled()`, `release()`" -- one trace across the producer,
        # the consumer and the store.
        clock = ManualClock(initial=BASE)
        trace: list[str] = []
        bus = _TappedBus(trace)
        store = _RecordingStore(clock, trace)
        feed = _Feed(bus)
        worker = _worker(bus=bus, clock=clock, state_store=store)
        await worker.start()
        assert await feed.deliver(worker, IP_A, 1200) is ObservationOutcome.APPLIED
        trace.clear()

        await worker.stop()

        assert trace == ["flush", "ack", "release_lease"]

    async def test_a_second_stop_appends_nothing_to_the_trace(self) -> None:
        # Amendment 4 ruling R4: "`stop()` itself runs its sequence once ... A
        # later `stop()` ... acquires the lock and returns without touching
        # the bus or the store" -- no flush, no ack, no release.
        clock = ManualClock(initial=BASE)
        trace: list[str] = []
        bus = _TappedBus(trace)
        store = _RecordingStore(clock, trace)
        feed = _Feed(bus)
        worker = _worker(bus=bus, clock=clock, state_store=store)
        await worker.start()
        assert await feed.deliver(worker, IP_A, 1200) is ObservationOutcome.APPLIED
        trace.clear()
        await worker.stop()
        assert trace == ["flush", "ack", "release_lease"]
        trace.clear()

        await worker.stop()

        assert trace == []

    async def test_stop_frees_the_lease_for_the_next_member(self) -> None:
        clock = ManualClock(initial=BASE)
        bus = _TappedBus()
        state_store = MemoryShardStateStore(clock=clock)
        worker = _worker(bus=bus, clock=clock, state_store=state_store)
        await worker.start()
        assert await state_store.acquire_lease(0, OTHER_MEMBER, 1.0) == OWNER_A

        await worker.stop()

        assert await state_store.acquire_lease(0, OTHER_MEMBER, 1.0) is None

    async def test_stop_keeps_the_window_in_memory(self) -> None:
        # Decision 8: "windows are kept in memory (nothing reads them after
        # `stop()` except tests)".
        clock = ManualClock(initial=BASE)
        bus = _TappedBus()
        feed = _Feed(bus)
        worker = _worker(bus=bus, clock=clock)
        await worker.start()
        await feed.deliver(worker, IP_A, 1200)

        await worker.stop()

        window = worker.window(0)
        assert window is not None
        assert window.total(IP_A) == 1200


class TestAcknowledgingHandledMessages:
    """ADR-0013 decision 8: `mark_handled(message)` records the message for
    the next acknowledgement and raises the handled position to
    `max(<current>, message.offset + 1)`; `commit_handled()` is
    `producer.flush()` then `consumer.ack(<every handled, unacknowledged
    message, in delivery order>)`, "both calls ... made even when the list is
    empty", and "the aggregator's **only** acknowledgement path". ADR-0003
    Amendment 3 item 2: what is acknowledged is exactly what `handle()`
    finished, "never a message merely fetched"."""

    async def _publish_two(self, bus: InMemoryBus) -> None:
        producer = bus.producer()
        await producer.publish(
            OBSERVATIONS_TOPIC,
            key=str(IP_A),
            value=_observation(IP_A, 1200, window_start=BASE),
            message_id="m-a",
        )
        await producer.publish(
            OBSERVATIONS_TOPIC,
            key=str(IP_B),
            value=_observation(IP_B, 1200, window_start=BASE),
            message_id="m-b",
        )

    async def _next_owner_sees_first(self, bus: InMemoryBus) -> ConsumedMessage:
        next_owner = bus.consumer(GROUP)
        return await _take_one(await next_owner.subscribe(OBSERVATIONS_TOPIC))

    async def test_commit_handled_flushes_then_acks(self) -> None:
        # The ordering rule ADR-0009 decision 7 states for every commit,
        # "flush before ack" after ADR-0013: "so a committed position never
        # precedes the transitions it produced".
        clock = ManualClock(initial=BASE)
        trace: list[str] = []
        bus = InMemoryBus()
        claims, consumer = await _claims_and_consumer(clock=clock, bus=bus, trace=trace)
        await claims.on_assigned(frozenset({(OBSERVATIONS_TOPIC, 0)}))
        await self._publish_two(bus)
        assert consumer.stream is not None
        claims.mark_handled(await _take_one(consumer.stream))
        trace.clear()

        await claims.commit_handled()

        assert trace == ["flush", "ack"]

    async def test_commit_handled_flushes_and_acks_even_with_nothing_handled(self) -> None:
        # "Both calls are made even when the list is empty (`ack([])` returns
        # normally without touching the broker), so the flush-then-ack trace
        # is uniform and testable."
        clock = ManualClock(initial=BASE)
        trace: list[str] = []
        claims = await _claims(clock=clock, trace=trace)
        await claims.on_assigned(frozenset({(OBSERVATIONS_TOPIC, 0)}))
        trace.clear()

        await claims.commit_handled()

        assert trace == ["flush", "ack"]

    async def test_a_handled_message_is_acknowledged(self) -> None:
        # A fresh consumer for the group skips it and is handed the next one.
        clock = ManualClock(initial=BASE)
        bus = InMemoryBus()
        claims, consumer = await _claims_and_consumer(clock=clock, bus=bus)
        await claims.on_assigned(frozenset({(OBSERVATIONS_TOPIC, 0)}))
        await self._publish_two(bus)
        assert consumer.stream is not None
        fetched = await _take_one(consumer.stream)
        assert fetched.key == str(IP_A).encode()
        claims.mark_handled(fetched)

        await claims.commit_handled()

        received = await self._next_owner_sees_first(bus)
        assert received.key == str(IP_B).encode()
        assert received.offset == fetched.offset + 1

    async def test_a_fetched_but_unhandled_message_is_not_acknowledged(self) -> None:
        # The loss ADR-0011 A20 closed and ADR-0003 Amendment 3 item 2
        # restates: the claim fetched the message and never marked it
        # handled, so the acknowledgement does not cover it and the next
        # owner is handed it first.
        clock = ManualClock(initial=BASE)
        bus = InMemoryBus()
        claims, consumer = await _claims_and_consumer(clock=clock, bus=bus)
        await claims.on_assigned(frozenset({(OBSERVATIONS_TOPIC, 0)}))
        await self._publish_two(bus)
        assert consumer.stream is not None
        fetched = await _take_one(consumer.stream)

        await claims.commit_handled()

        received = await self._next_owner_sees_first(bus)
        assert received.offset == fetched.offset
        assert received.key == str(IP_A).encode()

    async def test_only_the_handled_messages_are_acknowledged(self) -> None:
        # Two fetched, the first handled: the next owner starts at the second.
        clock = ManualClock(initial=BASE)
        bus = InMemoryBus()
        claims, consumer = await _claims_and_consumer(clock=clock, bus=bus)
        await claims.on_assigned(frozenset({(OBSERVATIONS_TOPIC, 0)}))
        await self._publish_two(bus)
        assert consumer.stream is not None
        first = await _take_one(consumer.stream)
        second = await _take_one(consumer.stream)
        claims.mark_handled(first)

        await claims.commit_handled()

        received = await self._next_owner_sees_first(bus)
        assert received.offset == second.offset
        assert received.key == str(IP_B).encode()

    async def test_commit_handled_clears_the_list(self) -> None:
        # A second `commit_handled()` acknowledges nothing new: on the bus a
        # message "already acknowledged" is a `ValueError` (decision 3), so
        # a list that was not cleared would fail here.
        clock = ManualClock(initial=BASE)
        trace: list[str] = []
        bus = InMemoryBus()
        claims, consumer = await _claims_and_consumer(clock=clock, bus=bus, trace=trace)
        await claims.on_assigned(frozenset({(OBSERVATIONS_TOPIC, 0)}))
        await self._publish_two(bus)
        assert consumer.stream is not None
        claims.mark_handled(await _take_one(consumer.stream))
        await claims.commit_handled()
        trace.clear()

        await claims.commit_handled()

        assert trace == ["flush", "ack"]

    async def test_handled_position_is_none_before_anything_was_handled(self) -> None:
        clock = ManualClock(initial=BASE)
        claims = await _claims(clock=clock)
        await claims.on_assigned(frozenset({(OBSERVATIONS_TOPIC, 0)}))

        assert claims.handled_position(0) is None

    async def test_handled_position_is_one_past_the_handled_offset(self) -> None:
        clock = ManualClock(initial=BASE)
        bus = InMemoryBus()
        claims, consumer = await _claims_and_consumer(clock=clock, bus=bus)
        await claims.on_assigned(frozenset({(OBSERVATIONS_TOPIC, 0)}))
        await self._publish_two(bus)
        assert consumer.stream is not None
        fetched = await _take_one(consumer.stream)

        claims.mark_handled(fetched)

        assert claims.handled_position(0) == fetched.offset + 1

    async def test_handled_position_is_the_max_of_the_handled_offsets_plus_one(self) -> None:
        # "raises the claim's handled position to `max(<current>,
        # message.offset + 1)`": marking a lower offset afterwards (a
        # redelivery) does not lower it.
        clock = ManualClock(initial=BASE)
        bus = InMemoryBus()
        claims, consumer = await _claims_and_consumer(clock=clock, bus=bus)
        await claims.on_assigned(frozenset({(OBSERVATIONS_TOPIC, 0)}))
        await self._publish_two(bus)
        assert consumer.stream is not None
        first = await _take_one(consumer.stream)
        second = await _take_one(consumer.stream)
        claims.mark_handled(second)
        assert claims.handled_position(0) == second.offset + 1

        claims.mark_handled(first)

        assert claims.handled_position(0) == second.offset + 1

    async def test_handled_position_for_an_unheld_partition_is_none(self) -> None:
        # ASSUMPTION 9 (ruled, T3; assumption 32): "`handled_position(shard)`
        # returns `None` for a shard this object does not hold, exactly as
        # for a held shard nothing has been handled on: the read surface
        # (`window`, `handled_position`) is total and the write surface
        # (`mark_handled`) raises `KeyError`" -- before any claim, and for a
        # partition outside the claimed set.
        clock = ManualClock(initial=BASE)
        claims = await _claims(clock=clock)
        assert claims.handled_position(0) is None

        await claims.on_assigned(frozenset({(OBSERVATIONS_TOPIC, 0)}))

        assert claims.handled_position(1) is None
        assert claims.handled_position(0) is None

    async def test_handled_position_survives_the_acknowledgement(self) -> None:
        # ASSUMPTION 9 (ruled, T4): "`commit_handled()` clears the handled
        # *list* and leaves the position where it is ... and nothing ever
        # lowers it".
        clock = ManualClock(initial=BASE)
        bus = InMemoryBus()
        claims, consumer = await _claims_and_consumer(clock=clock, bus=bus)
        await claims.on_assigned(frozenset({(OBSERVATIONS_TOPIC, 0)}))
        await self._publish_two(bus)
        assert consumer.stream is not None
        fetched = await _take_one(consumer.stream)
        claims.mark_handled(fetched)

        await claims.commit_handled()

        assert claims.handled_position(0) == fetched.offset + 1

    async def test_handled_positions_are_per_partition(self) -> None:
        clock = ManualClock(initial=BASE)
        claims = await _claims(clock=clock)
        await claims.on_assigned(frozenset({(OBSERVATIONS_TOPIC, 0), (OBSERVATIONS_TOPIC, 1)}))

        claims.mark_handled(_message(1, 41))

        assert claims.handled_position(1) == 42
        assert claims.handled_position(0) is None

    async def test_mark_handled_for_an_unheld_partition_is_a_key_error(self) -> None:
        # ADR-0011 A20, unchanged by ADR-0013 decision 8: "A partition this
        # object does not hold is a `KeyError`".
        clock = ManualClock(initial=BASE)
        claims = await _claims(clock=clock)
        await claims.on_assigned(frozenset({(OBSERVATIONS_TOPIC, 0)}))

        with pytest.raises(KeyError):
            claims.mark_handled(_message(1, 0))

    async def test_mark_handled_before_any_claim_is_a_key_error(self) -> None:
        clock = ManualClock(initial=BASE)
        claims = await _claims(clock=clock)

        with pytest.raises(KeyError):
            claims.mark_handled(_message(0, 0))


class TestInheritedRetention:
    """Amendment 2 item A5: an inherited IP's `last_seen` is `clock.now()` at
    construction, so retention runs from the claim -- an IP demoted at warm-up
    end becomes evictable at `claim + state_retention_seconds`, the same
    deadline an IP observed at claim time would get."""

    async def _claimed_window(self, clock: ManualClock) -> ShardClaims:
        state_store = MemoryShardStateStore(clock=clock)
        await state_store.record_transition(0, IP_A, IpState.HOT, 0)
        claims = await _claims(clock=clock, state_store=state_store)
        await claims.on_assigned(frozenset({(OBSERVATIONS_TOPIC, 0)}))
        return claims

    async def test_a_demoted_inherited_ip_survives_until_the_claim_plus_retention(self) -> None:
        clock = ManualClock(initial=BASE)
        claims = await self._claimed_window(clock)
        window = claims.window(0)
        assert window is not None

        clock.advance(WINDOW_SECONDS)
        assert window.finish_warmup_if_due() == frozenset({IP_A})
        window.set_state(IP_A, IpState.COLD)  # what the warm-up evaluation does

        clock.advance(STATE_RETENTION_SECONDS - WINDOW_SECONDS - 1)  # claim + 599
        window.expire_due()
        assert window.evict_due() == 0
        assert window.is_tracked(IP_A) is True

    async def test_a_demoted_inherited_ip_is_evicted_at_the_claim_plus_retention(self) -> None:
        clock = ManualClock(initial=BASE)
        claims = await self._claimed_window(clock)
        window = claims.window(0)
        assert window is not None

        clock.advance(WINDOW_SECONDS)
        window.finish_warmup_if_due()
        window.set_state(IP_A, IpState.COLD)

        clock.advance(STATE_RETENTION_SECONDS - WINDOW_SECONDS)  # claim + 600
        window.expire_due()
        assert window.evict_due() == 1
        assert window.is_tracked(IP_A) is False
        assert window.tracked_count == 0

    async def test_an_inherited_ip_still_hot_is_never_evicted(self) -> None:
        clock = ManualClock(initial=BASE)
        claims = await self._claimed_window(clock)
        window = claims.window(0)
        assert window is not None

        clock.advance(STATE_RETENTION_SECONDS * 2)
        window.expire_due()

        assert window.evict_due() == 0
        assert window.is_tracked(IP_A) is True


class TestEvictionCountersArePerClaimedShard:
    """Amendment 2 item A7: `window_evictions{shard,reason}` is read from the
    two counters of each claimed shard, so the series lives with the window.
    Amendment 3 item A13 pins the read path A7 left open -- the series is
    computed on each `metrics.get`, from the windows the worker bound -- so
    the last test here reads it through `AggregatorMetrics`."""

    async def test_each_claimed_shard_counts_its_own_evictions(self) -> None:
        clock = ManualClock(initial=BASE)
        claims = await _claims(clock=clock)
        await claims.on_assigned(frozenset({(OBSERVATIONS_TOPIC, 0), (OBSERVATIONS_TOPIC, 1)}))
        first = claims.window(0)
        second = claims.window(1)
        assert first is not None
        assert second is not None

        assert first.observe(IP_A, BASE, 5) is not None
        clock.advance(STATE_RETENTION_SECONDS)
        first.expire_due()
        first.evict_due()

        assert first.retention_evictions == 1
        assert first.capacity_evictions == 0
        assert second.retention_evictions == 0
        assert second.capacity_evictions == 0

    async def test_a_claimed_shards_counters_are_readable_through_the_metrics(self) -> None:
        # A13: `AggregatorWorker.__init__` binds the claimed windows as the
        # source of the derived series, and `get` computes the answer from
        # them on the call -- so an eviction needs no bookkeeping of its own to
        # become readable, and `shards_claimed` is the number of windows the
        # bound source yields.
        clock = ManualClock(initial=BASE)
        bus = InMemoryBus()
        metrics = AggregatorMetrics()
        worker = _worker(bus=bus, clock=clock, metrics=metrics)

        await worker.start()
        try:
            window = worker.window(0)
            assert window is not None
            assert window.observe(IP_A, BASE, 5) is not None
            clock.advance(STATE_RETENTION_SECONDS)
            window.expire_due()
            assert window.evict_due() == 1

            assert metrics.get("window_evictions", shard=0, reason="retention") == 1
            assert metrics.get("window_evictions", shard=0, reason="capacity") == 0
            assert metrics.get("shards_claimed") == 1
        finally:
            await worker.stop()


class TestTheWorkerClaimsShardZero:
    """ADR-0009's readiness rule made observable: `start()` returns once
    `subscribe()` has delivered the assignment (ADR-0013 decision 3), which
    on the single-partition `InMemoryBus` is always `{(topic, 0)}` -- and,
    after decision 7, once that shard's lease is held."""

    async def test_start_completes_with_shard_zero_claimed_and_leased(self) -> None:
        clock = ManualClock(initial=BASE)
        bus = InMemoryBus()
        state_store = MemoryShardStateStore(clock=clock)
        worker = _worker(bus=bus, clock=clock, state_store=state_store)

        await worker.start()

        try:
            assert worker.shards == frozenset({0})
            assert worker.window(0) is not None
            assert await state_store.acquire_lease(0, OTHER_MEMBER, 1.0) == OWNER_A
        finally:
            await worker.stop()

    async def test_the_claim_inherits_the_shards_persisted_hot_set(self) -> None:
        clock = ManualClock(initial=BASE)
        bus = InMemoryBus()
        state_store = MemoryShardStateStore(clock=clock)
        await state_store.record_transition(0, IP_A, IpState.HOT, 6)
        worker = _worker(bus=bus, clock=clock, state_store=state_store)

        await worker.start()

        try:
            window = worker.window(0)
            assert window is not None
            assert window.hot_ips() == frozenset({IP_A})
            assert window.next_sequence == 7
            assert window.in_warmup is True
        finally:
            await worker.stop()

    async def test_a_message_on_a_partition_without_a_window_is_unclaimed(self) -> None:
        # ADR-0013 decision 8: "`UNCLAIMED` stays as the outcome for a message
        # on a partition this member holds no window for (reachable only
        # through a direct `handle()` call now; ADR-0011 A19's record and
        # no-counter rule unchanged)". The memory bus never delivers
        # partition 1, so the message is built directly.
        clock = ManualClock(initial=BASE)
        bus = InMemoryBus()
        metrics = AggregatorMetrics()
        worker = _worker(bus=bus, clock=clock, metrics=metrics)
        await worker.start()
        try:
            message = _message(1, 0, IP_C)
            assert worker.window(1) is None

            outcome = await worker.handle(message)

            assert outcome is ObservationOutcome.UNCLAIMED
            assert worker.window(1) is None
            window = worker.window(0)
            assert window is not None
            assert window.is_tracked(IP_C) is False
            assert _records(bus, HOT_IP_TOPIC) == []
            assert _records(bus, RECONCILIATION_TOPIC) == []
            # A19: no counter increment under any series. The message is well
            # formed, so `observations_rejected{reason=malformed}` -- a signal
            # about producers -- must not tick; the series keeps its two
            # reasons, and this message is neither of them.
            assert metrics.get("observations_rejected", reason="malformed") == 0
            assert metrics.get("observations_rejected", reason="window_too_long") == 0
            for reason in ("late", "future", "expired_bucket"):
                assert metrics.get("late_messages", reason=reason) == 0
        finally:
            await worker.stop()


class TestHandoverBetweenTwoMembers:
    """Shard 0 changes hands from worker A to worker B over one bus and one
    state store: ADR-0013 decision 8 ("the way a shard changes hands is a
    `stop()` on one member and a `start()` on another") made observable end
    to end, and the handover clauses of ADR-0001 Amendment 1 (1, 2, 5, 6)
    and ADR-0003 Amendment 3 (item 2, and the surviving points of Amendment
    2) checked against what B actually inherits, applies and emits.

    The two members are sequential -- A is stopped before B is constructed
    -- which is the only shape the memory bus supports (module docstring).
    The clock is advanced by `HANDOVER_SECONDS` between the two so that B's
    claim time is distinguishable from A's; the lease TTL is
    `LONG_LEASE_SECONDS`, so that advance can never lapse A's lease and only
    A's `stop()` can free it (ASSUMPTION 7).

    A message handed to `handle()` twice under one live claim is
    `REDELIVERED` since ADR-0003 Amendment 3 and is pinned in
    `test_worker.py`; every redelivery in this class crosses a claim
    boundary and is applied.
    """

    async def _first_owner_records_ip_a(
        self, a: AggregatorWorker, feed: _Feed, bus: InMemoryBus
    ) -> None:
        """Member A handles m1 -- `IP_A`, 1200 requests at `BASE` -- and, per
        decision 4, records `IP_A` HOT in the store (`sequence` 0) and
        announces it once. Every test in this class starts from here."""

        outcome = await feed.deliver(a, IP_A, 1200)
        assert outcome is ObservationOutcome.APPLIED
        window = a.window(0)
        assert window is not None
        assert window.state(IP_A) is IpState.HOT
        assert _identities(bus) == [("HotIpAdded", str(IP_A), SHARD_AGENT_ID, 0)]

    async def test_a_second_member_cannot_start_while_the_first_holds_the_lease(self) -> None:
        # #90, ADR-0013 decision 7: "Overlap at start: `shard_owned_elsewhere`
        # (ERROR), `start_failed`, exit 1, `/readyz` never 200."
        clock = ManualClock(initial=BASE)
        bus = _TappedBus()
        state_store = MemoryShardStateStore(clock=clock)
        feed = _Feed(bus)
        members = _Members(bus=bus, clock=clock, state_store=state_store)
        try:
            a = await members.start()
            await self._first_owner_records_ip_a(a, feed, bus)
            clock.advance(HANDOVER_SECONDS)

            b = members.build()
            with pytest.raises(ShardOwnedElsewhereError) as excinfo:
                await b.start()

            # Another member, not another instance of this one: Amendment 6
            # ruling 2(b) leaves this refusal "unchanged" and it is not the
            # one `AggregatorService.start()` waits out.
            assert not isinstance(excinfo.value, ShardHeldBySameMemberError)
            assert b.window(0) is None
            assert b.shards == frozenset()
            # A is untouched: still the holder, its window intact.
            assert a.window(0) is not None
            assert await state_store.acquire_lease(0, OTHER_MEMBER, 1.0) == _owner(
                "aggregator-1", INSTANCE_A
            )
            assert _identities(bus) == [("HotIpAdded", str(IP_A), SHARD_AGENT_ID, 0)]
        finally:
            await members.stop_all()

    async def test_a_clean_stop_hands_the_shard_over_at_once(self) -> None:
        # "so a clean stop hands the shards over immediately": no clock
        # advance between A's stop and B's start, and the lease TTL is
        # hours, so B's success can only be A's release.
        clock = ManualClock(initial=BASE)
        bus = _TappedBus()
        state_store = MemoryShardStateStore(clock=clock)
        feed = _Feed(bus)
        members = _Members(bus=bus, clock=clock, state_store=state_store)
        try:
            a = await members.start()
            await self._first_owner_records_ip_a(a, feed, bus)
            await members.hand_over(a)

            b = await members.start()

            assert b.window(0) is not None
            assert await state_store.acquire_lease(0, OTHER_MEMBER, 1.0) == _owner(
                "aggregator-2", INSTANCE_A
            )
        finally:
            await members.stop_all()

    async def test_the_next_owner_inherits_the_hot_set_the_first_owner_recorded(self) -> None:
        # ADR-0011 decision 5 `on_assigned`: B loads shard 0's `ShardState`
        # and builds its window with `inherited_hot=state.hot_ips`,
        # `next_sequence=state.next_sequence`; Amendment 2 item A5 makes the
        # inherited IP a tracked HOT entry with an empty ring; `warm_until`
        # is `clock.now() + window_seconds` at *B's* construction. ADR-0001
        # Amendment 1 clause 5: "the new owner inherits the shard's durable
        # HOT set, exempts inherited IPs from demotion for `window_seconds`".
        clock = ManualClock(initial=BASE)
        bus = _TappedBus()
        state_store = MemoryShardStateStore(clock=clock)
        feed = _Feed(bus)
        members = _Members(bus=bus, clock=clock, state_store=state_store)
        try:
            a = await members.start()
            await self._first_owner_records_ip_a(a, feed, bus)
            await members.hand_over(a)
            clock.advance(HANDOVER_SECONDS)

            b = await members.start()

            window = b.window(0)
            assert window is not None
            assert window.hot_ips() == frozenset({IP_A})
            assert window.next_sequence == 1
            assert window.is_inherited(IP_A) is True
            assert window.in_warmup is True
            assert window.warm_until == BASE + HANDOVER_SECONDS + WINDOW_SECONDS
            # A5: inherited means an empty ring -- A's 1200 did not travel.
            assert window.total(IP_A) == 0
            assert window.state(IP_A) is IpState.HOT
            # The handover itself emitted nothing -- no demotion on the way
            # out, no re-announce on the way in.
            assert _identities(bus) == [("HotIpAdded", str(IP_A), SHARD_AGENT_ID, 0)]
        finally:
            await members.stop_all()

    async def test_the_next_owner_resumes_after_the_first_owners_handled_messages_and_applies_what_follows(  # noqa: E501
        self,
    ) -> None:
        # ADR-0013 decision 8 / ADR-0003 Amendment 3 item 2: A's `stop()`
        # acknowledges exactly the messages it handled -- m1 -- so B's consume
        # loop is handed m2, which A never fetched, and not m1 again.
        # ADR-0001 Amendment 1 clause 2: B continues shard 0's `agent_id` and
        # `sequence` where A left them -- continued, not restarted; strictly
        # increasing, not promised dense -- so no `event_id` is reused across
        # the handover. The exact values, #0 then #1, follow from decision 4
        # step 1 (`sequence = window.next_sequence`, then `+= 1`), decision 5
        # (`next_sequence=state.next_sequence` at B's claim) and Amendment 1
        # item A2 (the store holds `sequence + 1` after A's record). Clause 6:
        # m1 is not counted again (B's ring for IP_A stays empty) and m2 is
        # not lost.
        clock = ManualClock(initial=BASE)
        bus = _TappedBus()
        state_store = MemoryShardStateStore(clock=clock)
        feed = _Feed(bus)
        members = _Members(bus=bus, clock=clock, state_store=state_store)
        try:
            a = await members.start()
            await self._first_owner_records_ip_a(a, feed, bus)  # m1, handled by A
            await feed.publish(IP_B, 1200)  # m2; A never fetches it
            await members.hand_over(a)  # acknowledges m1, releases the lease
            clock.advance(HANDOVER_SECONDS)

            b = await members.start()
            members.run(b)
            await _yield_until(lambda: _is_hot(b, IP_B))

            window = b.window(0)
            assert window is not None
            assert window.total(IP_B) == 1200
            assert window.state(IP_B) is IpState.HOT
            # B resumed *after* m1, not before it: m1 was not re-read into
            # B's window, whose entry for IP_A is still the inherited one
            # with an empty ring.
            assert window.total(IP_A) == 0
            assert window.is_inherited(IP_A) is True
            assert _identities(bus) == [
                ("HotIpAdded", str(IP_A), SHARD_AGENT_ID, 0),
                ("HotIpAdded", str(IP_B), SHARD_AGENT_ID, 1),
            ]
            assert len({envelope.event_id for envelope in _hot_ip_events(bus)}) == 2
            assert await state_store.load(0) == ShardState(
                hot_ips=frozenset({IP_A, IP_B}), next_sequence=2
            )
        finally:
            await members.stop_all()

    async def test_an_ip_that_stays_busy_across_a_handover_is_announced_exactly_once(self) -> None:
        # ADR-0011 decision 3, last paragraph: the HOT set "is idempotent
        # under redelivery: an IP the store already has as HOT is not
        # re-announced"; ADR-0003 Amendment 2 point 2 says the same of the
        # durable set. B counts IP_A from scratch (A5: the ring starts empty)
        # and reaches `hot_threshold` again, but IP_A is already HOT in the
        # window it inherited, so there is no COLD -> HOT edge to emit.
        clock = ManualClock(initial=BASE)
        bus = _TappedBus()
        state_store = MemoryShardStateStore(clock=clock)
        feed = _Feed(bus)
        members = _Members(bus=bus, clock=clock, state_store=state_store)
        try:
            a = await members.start()
            await self._first_owner_records_ip_a(a, feed, bus)
            await members.hand_over(a)
            clock.advance(HANDOVER_SECONDS)
            b = await members.start()

            outcome = await feed.deliver(b, IP_A, 1200)

            assert outcome is ObservationOutcome.APPLIED
            window = b.window(0)
            assert window is not None
            assert window.total(IP_A) == 1200
            assert window.state(IP_A) is IpState.HOT
            for_ip_a = [
                envelope.event_type
                for envelope in _hot_ip_events(bus)
                if envelope.subject == str(IP_A)
            ]
            assert for_ip_a == ["HotIpAdded"]
            assert len(_records(bus, HOT_IP_TOPIC)) == 1
            assert await state_store.load(0) == ShardState(
                hot_ips=frozenset({IP_A}), next_sequence=1
            )
        finally:
            await members.stop_all()

    async def test_the_next_owner_does_not_demote_an_inherited_ip_before_its_warm_up_ends(
        self,
    ) -> None:
        # Decision 5, warm-up: "Inherited IPs are therefore exempt from
        # HOT -> COLD until `warm_until`, whatever the trigger." Two triggers
        # that would each demote IP_A outside warm-up are fired inside it.
        # First an observation of 5 -- well below `cold_threshold` -- on the
        # observation path, Amendment 2 item A11's HOT -> COLD with
        # `reason="observation"`. Then, one second before `warm_until`, a
        # sweep in which that bucket has left the window (BASE + 339: bucket
        # age 330 >= 300), so `expire_due()` reports IP_A's total dropping to
        # 0 and decision 6 step 1 evaluates it with `reason="expiry"`. Neither
        # may demote: the under-count is B's, not the IP's. (A bare sweep with
        # an empty ring would evaluate nothing and prove nothing.)
        clock = ManualClock(initial=BASE)
        bus = _TappedBus()
        state_store = MemoryShardStateStore(clock=clock)
        feed = _Feed(bus)
        b_metrics = AggregatorMetrics()
        members = _Members(bus=bus, clock=clock, state_store=state_store)
        try:
            a = await members.start()
            await self._first_owner_records_ip_a(a, feed, bus)
            await members.hand_over(a)
            clock.advance(HANDOVER_SECONDS)
            b = await members.start(metrics=b_metrics)
            window = b.window(0)
            assert window is not None

            outcome = await feed.deliver(b, IP_A, 5)

            assert outcome is ObservationOutcome.APPLIED
            assert window.total(IP_A) == 5
            assert window.state(IP_A) is IpState.HOT
            assert window.is_inherited(IP_A) is True

            clock.advance(WINDOW_SECONDS - 1)  # BASE + 339: the bucket at BASE has expired
            await b.run_maintenance()

            assert window.total(IP_A) == 0
            assert [envelope.event_type for envelope in _hot_ip_events(bus)] == ["HotIpAdded"]
            assert window.state(IP_A) is IpState.HOT
            assert window.is_inherited(IP_A) is True
            assert window.in_warmup is True
            for reason in ("observation", "expiry", "warmup"):
                demotions = b_metrics.get(
                    "hot_to_cold_transitions", shard=0, config_version=1, reason=reason
                )
                assert demotions == 0
            assert await state_store.load(0) == ShardState(
                hot_ips=frozenset({IP_A}), next_sequence=1
            )
        finally:
            await members.stop_all()

    async def test_the_next_owner_demotes_a_quiet_inherited_ip_after_warm_up_with_the_shards_next_sequence(  # noqa: E501
        self,
    ) -> None:
        # Decision 5: at `warm_until` the sweep's `finish_warmup_if_due()`
        # yields the inherited IPs still HOT and each is evaluated once with
        # `reason="warmup"`; decision 6 orders that step inside
        # `run_maintenance()` (after ADR-0013 decision 7's renewal); decision
        # 8 labels the counter
        # `hot_to_cold_transitions{shard,config_version,reason="warmup"}`.
        # ADR-0001 Amendment 1 clause 2: the `HotIpRemoved` carries shard 0's
        # `agent_id` and the *next* sequence after A's `HotIpAdded` -- the
        # counter B loaded from the store -- so the two events, from two
        # workers, have distinct `event_id`s and form one ordered stream;
        # clause 5: the demotion arrives `window_seconds` plus the handover
        # time after A last counted the IP, and no earlier.
        clock = ManualClock(initial=BASE)
        bus = _TappedBus()
        state_store = MemoryShardStateStore(clock=clock)
        feed = _Feed(bus)
        a_metrics = AggregatorMetrics()
        b_metrics = AggregatorMetrics()
        members = _Members(bus=bus, clock=clock, state_store=state_store)
        try:
            a = await members.start(metrics=a_metrics)
            await self._first_owner_records_ip_a(a, feed, bus)
            await members.hand_over(a)
            clock.advance(HANDOVER_SECONDS)
            b = await members.start(metrics=b_metrics)

            clock.advance(WINDOW_SECONDS)
            await b.run_maintenance()

            assert _identities(bus) == [
                ("HotIpAdded", str(IP_A), SHARD_AGENT_ID, 0),
                ("HotIpRemoved", str(IP_A), SHARD_AGENT_ID, 1),
            ]
            events = _hot_ip_events(bus)
            assert len({envelope.event_id for envelope in events}) == 2
            removed = events[1].payload
            assert isinstance(removed, HotIpRemoved)
            assert removed.ip == IP_A
            assert removed.sequence == 1
            assert removed.window_count == 0  # B never counted IP_A
            assert removed.attributes is None  # decision 4 step 3
            assert await state_store.load(0) == ShardState(hot_ips=frozenset(), next_sequence=2)
            window = b.window(0)
            assert window is not None
            assert window.state(IP_A) is IpState.COLD
            assert window.is_inherited(IP_A) is False
            assert window.in_warmup is False
            assert (
                b_metrics.get("hot_to_cold_transitions", shard=0, config_version=1, reason="warmup")
                == 1
            )
            # The demotion is B's: A's counters never saw it.
            assert (
                a_metrics.get("hot_to_cold_transitions", shard=0, config_version=1, reason="warmup")
                == 0
            )
        finally:
            await members.stop_all()

    async def test_a_third_owner_continues_the_sequence(self) -> None:
        # After B's warm-up demotion the store holds no HOT IP and
        # `next_sequence == 2`. Decision 5: C inherits exactly that -- an
        # empty set, so "`warm_until` is `clock.now() + window_seconds` at
        # construction iff `inherited_hot` is non-empty" gives no warm-up --
        # and the shard's next transition, whoever emits it, would be #2
        # (ADR-0001 Amendment 1 clause 2; ADR-0011 Amendment 1 item A2: the
        # persisted counter never moves backwards).
        clock = ManualClock(initial=BASE)
        bus = _TappedBus()
        state_store = MemoryShardStateStore(clock=clock)
        feed = _Feed(bus)
        members = _Members(bus=bus, clock=clock, state_store=state_store)
        try:
            a = await members.start()
            await self._first_owner_records_ip_a(a, feed, bus)
            await members.hand_over(a)
            clock.advance(HANDOVER_SECONDS)
            b = await members.start()
            clock.advance(WINDOW_SECONDS)
            await b.run_maintenance()
            assert await state_store.load(0) == ShardState(hot_ips=frozenset(), next_sequence=2)
            await members.hand_over(b)
            clock.advance(HANDOVER_SECONDS)

            c = await members.start()

            window = c.window(0)
            assert window is not None
            assert window.next_sequence == 2
            assert window.hot_ips() == frozenset()
            assert window.tracked_count == 0
            assert window.warm_until is None
            assert window.in_warmup is False
            # Two handovers, two workers' worth of transitions, one stream.
            assert _identities(bus) == [
                ("HotIpAdded", str(IP_A), SHARD_AGENT_ID, 0),
                ("HotIpRemoved", str(IP_A), SHARD_AGENT_ID, 1),
            ]
            assert await state_store.acquire_lease(0, OTHER_MEMBER, 1.0) == _owner(
                "aggregator-3", INSTANCE_A
            )
        finally:
            await members.stop_all()


class TestNothingActsOnAShardAfterStop:
    """ADR-0013 decision 8 as amended (Amendment 4 ruling R2): "`stop()`'s order is
    total. From the moment `stop()` sets the stop flag, every path that could
    act on a shard checks the flag under the worker lock and stands down";
    decision 7 as amended (Amendment 3 ruling (c)): "After `stop()` has
    released the leases, `run_maintenance()` renews nothing and sweeps
    nothing: it returns normally without touching the store, the windows or
    the producer." Decision 7's rule underneath both: "a member acts on a
    shard only while it holds the lease"."""

    async def test_run_maintenance_after_stop_sweeps_nothing_while_the_next_owner_holds_the_lease(
        self,
    ) -> None:
        # Ruling (c)'s scenario: A stopped, B holding the lease, and a
        # `HotIpRemoved` due in any sweep A ran (its only bucket left the
        # window at BASE + 300) -- "after the next owner may have loaded that
        # shard's HOT set". A's sweep must not run: no `ShardLeaseLostError`
        # (it renews nothing), nothing emitted, nothing written, A's window
        # left as it was, B still the holder.
        clock = ManualClock(initial=BASE)
        trace: list[str] = []
        bus = _TappedBus(trace)
        store = _RecordingStore(clock, trace)
        feed = _Feed(bus)
        a = _worker(bus=bus, clock=clock, state_store=store, member_id=MEMBER_A)
        await a.start()
        assert await feed.deliver(a, IP_A, 1200) is ObservationOutcome.APPLIED
        await a.stop()
        b = _worker(bus=bus, clock=clock, state_store=store, member_id=MEMBER_B)
        await b.start()
        try:
            assert await store.acquire_lease(0, OTHER_MEMBER, 1.0) == _owner(MEMBER_B, INSTANCE_A)
            clock.advance(310)
            trace.clear()

            await a.run_maintenance()

            assert trace == []
            assert [envelope.event_type for envelope in _hot_ip_events(bus)] == ["HotIpAdded"]
            window = a.window(0)
            assert window is not None
            assert window.state(IP_A) is IpState.HOT
            assert window.is_tracked(IP_A) is True
            assert await store.acquire_lease(0, OTHER_MEMBER, 1.0) == _owner(MEMBER_B, INSTANCE_A)
            assert (await store.load(0)).hot_ips == frozenset({IP_A})
        finally:
            await b.stop()

    async def test_run_maintenance_after_stop_returns_normally_with_nobody_holding_the_lease(
        self,
    ) -> None:
        # The same without a successor: the lease is free, and a sweep that
        # "re-takes nothing" must not quietly reacquire it either.
        clock = ManualClock(initial=BASE)
        trace: list[str] = []
        bus = _TappedBus(trace)
        store = _RecordingStore(clock, trace)
        feed = _Feed(bus)
        worker = _worker(bus=bus, clock=clock, state_store=store)
        await worker.start()
        assert await feed.deliver(worker, IP_A, 1200) is ObservationOutcome.APPLIED
        await worker.stop()
        clock.advance(310)
        trace.clear()

        await worker.run_maintenance()

        assert trace == []
        assert [envelope.event_type for envelope in _hot_ip_events(bus)] == ["HotIpAdded"]
        assert await store.acquire_lease(0, OTHER_MEMBER, 1.0) is None

    async def test_run_maintenance_on_a_never_started_worker_returns_normally(self) -> None:
        # No claim, no lease, no window: nothing to renew and nothing to sweep,
        # and the store is not consulted.
        clock = ManualClock(initial=BASE)
        store = _RecordingStore(clock)
        worker = _worker(bus=_TappedBus(), clock=clock, state_store=store)

        await worker.run_maintenance()

        assert store.calls == []
        assert worker.shards == frozenset()

    async def test_apply_config_after_stop_is_a_total_no_op(self) -> None:
        # Ruling R2 (closing assumption 54; assumption 63: "a total no-op,
        # leaving the worker's `config` unchanged and logging nothing"): a
        # tracked IP at 600 is COLD under v1 and would be promoted by v2's
        # thresholds, and nothing of that happens -- no transition, the
        # worker's and the window's config as they were, the window
        # untouched, the store and the producer untouched.
        clock = ManualClock(initial=BASE)
        trace: list[str] = []
        bus = _TappedBus(trace)
        store = _RecordingStore(clock, trace)
        feed = _Feed(bus)
        worker = _worker(bus=bus, clock=clock, state_store=store)
        await worker.start()
        assert await feed.deliver(worker, IP_A, 600) is ObservationOutcome.APPLIED
        window = worker.window(0)
        assert window is not None
        assert window.state(IP_A) is IpState.COLD
        assert worker.config.config_version == 1
        await worker.stop()
        trace.clear()
        lower = _config(config_version=2, hot_threshold=500, cold_threshold=400)

        await worker.apply_config(lower)

        assert trace == []
        assert _records(bus, HOT_IP_TOPIC) == []
        assert worker.config.config_version == 1
        assert worker.config.hot_threshold == 1000
        assert worker.config.cold_threshold == 800
        assert window.config.config_version == 1
        assert window.config.hot_threshold == 1000
        assert window.state(IP_A) is IpState.COLD
        assert window.hot_count == 0
        assert window.total(IP_A) == 600
        assert (await store.load(0)).hot_ips == frozenset()

    async def test_handle_after_stop_is_unclaimed_and_the_message_stays_unacknowledged(
        self,
    ) -> None:
        # Ruling R2 / decision 8's `UNCLAIMED` sentence as amended: "for any
        # message handed to `handle()` after `stop()` has begun, whatever its
        # partition: after `stop()` the member holds no lease, so every
        # partition is one it must not act on" -- here partition 0, for which
        # the member still holds a window. "`handle()` returns `UNCLAIMED`
        # without decoding, applying, emitting or marking, so the message
        # stays unacknowledged and `close()` hands it to the next member."
        clock = ManualClock(initial=BASE)
        bus = _TappedBus()
        metrics = AggregatorMetrics()
        feed = _Feed(bus)
        worker = _worker(bus=bus, clock=clock, metrics=metrics)
        await worker.start()
        await worker.stop()

        outcome = await feed.deliver(worker, IP_B, 1200)

        assert outcome is ObservationOutcome.UNCLAIMED
        window = worker.window(0)
        assert window is not None
        assert window.is_tracked(IP_B) is False
        assert window.tracked_count == 0
        # Not marked: the handled position never moved.
        assert worker.claims.handled_position(0) is None
        assert _records(bus, HOT_IP_TOPIC) == []
        assert _records(bus, RECONCILIATION_TOPIC) == []
        # A19's no-counter rule, unchanged for this `UNCLAIMED`.
        assert metrics.get("observations_rejected", reason="malformed") == 0
        for reason in ("late", "future", "expired_bucket"):
            assert metrics.get("late_messages", reason=reason) == 0
        # Unacknowledged: the next consumer of the group is handed it first.
        next_owner = bus.consumer(GROUP)
        message = await _take_one(await next_owner.subscribe(OBSERVATIONS_TOPIC))
        assert message.key == str(IP_B).encode()

    async def test_handle_after_stop_is_unclaimed_for_a_message_that_would_have_been_applied(
        self,
    ) -> None:
        # The same message is `APPLIED` on a live member: the stand-down is
        # the stop flag's doing and not the message's.
        clock = ManualClock(initial=BASE)
        bus = _TappedBus()
        feed = _Feed(bus)
        live = _worker(bus=bus, clock=clock, member_id=MEMBER_A)
        await live.start()
        assert await feed.deliver(live, IP_B, 1200) is ObservationOutcome.APPLIED
        await live.stop()

        assert await feed.deliver(live, IP_B, 1200) is ObservationOutcome.UNCLAIMED
        window = live.window(0)
        assert window is not None
        assert window.total(IP_B) == 1200


class TestAFaultingLoadInsideOnAssigned:
    """ADR-0013 decision 7 as amended (Amendment 3 ruling (d); Amendment 4 ruling R5):
    "A `load()` that raises inside this call is a fault, not a refusal ...:
    `ShardClaims` logs nothing, releases no lease inside the call, and lets
    the exception propagate the same way, to exit 1. The leases the call
    acquired -- the failed shard's included, since it was acquired before its
    `load()` -- stay in the held set, so `release()` drops them when
    `run_service` calls `stop()` on the failed start ..., and they lapse
    after `lease_ttl_s` when the store cannot be reached for that either."
    Shards `{0, 1}` over `_AnyPartitionsBus` (ASSUMPTION 11); `load(1)`
    raises `_StoreDown`."""

    def _worker(
        self, clock: ManualClock, store: ShardStateStore, *, instance_id: str = INSTANCE_A
    ) -> AggregatorWorker:
        return AggregatorWorker(
            bus=_AnyPartitionsBus(),
            state_store=store,
            clock=clock,
            config=DEFAULTS,
            metrics=AggregatorMetrics(),
            shard_ids=frozenset({0, 1}),
            member_id=MEMBER_A,
            lease_ttl_s=LEASE_TTL_SECONDS,
            instance_id=instance_id,
        )

    async def test_the_fault_propagates_as_itself_out_of_start(self) -> None:
        # "a fault, not a refusal": the exception is the store's own, not
        # `ShardOwnedElsewhereError`, and it reaches `worker.start()`.
        clock = ManualClock(initial=BASE)
        store = _FaultingLoadStore(clock, faulty_shard=1)
        worker = self._worker(clock, store)

        with pytest.raises(_StoreDown) as excinfo:
            await worker.start()

        assert not isinstance(excinfo.value, ShardOwnedElsewhereError)

    async def test_shard_zero_was_leased_and_loaded_and_shard_one_leased_before_its_load(
        self,
    ) -> None:
        # Decision 7 (ruling T6): "acquire `p`, load `p`, build `p`'s window,
        # and only then the next shard's acquire" -- the fault lands after
        # shard 1's acquire, so its lease is in the held set too.
        clock = ManualClock(initial=BASE)
        store = _FaultingLoadStore(clock, faulty_shard=1)
        worker = self._worker(clock, store)

        with pytest.raises(_StoreDown):
            await worker.start()

        sequence = [call[:2] for call in store.calls if call[0] in ("acquire_lease", "load")]
        assert sequence == [
            ("acquire_lease", 0),
            ("load", 0),
            ("acquire_lease", 1),
            ("load", 1),
        ]
        assert worker.window(0) is not None
        assert worker.window(1) is None

    async def test_no_lease_is_released_inside_the_call_and_both_stay_held(self) -> None:
        # "releases no lease inside the call": the observable half of "logs
        # nothing" (module docstring) -- nothing reaches the store's
        # `release_lease`, and both shards are still this member's.
        clock = ManualClock(initial=BASE)
        store = _FaultingLoadStore(clock, faulty_shard=1)
        worker = self._worker(clock, store)

        with pytest.raises(_StoreDown):
            await worker.start()

        assert [call for call in store.calls if call[0] == "release_lease"] == []
        assert await store.acquire_lease(0, OTHER_MEMBER, 1.0) == OWNER_A
        assert await store.acquire_lease(1, OTHER_MEMBER, 1.0) == OWNER_A

    async def test_stop_after_the_failed_start_returns_normally_and_releases_both(self) -> None:
        # Ruling (d): "`release()` drops them when `run_service` calls
        # `stop()` on the failed start ... the failed shard's included";
        # assumption 56: that `stop()` "acknowledges nothing and cannot raise
        # from the bus" -- `ack(())` on the never-subscribed consumer returns
        # normally (ruling (a)), and the flush is a no-op.
        clock = ManualClock(initial=BASE)
        store = _FaultingLoadStore(clock, faulty_shard=1)
        worker = self._worker(clock, store)
        with pytest.raises(_StoreDown):
            await worker.start()
        store.clear()

        await worker.stop()

        assert sorted(call for call in store.calls if call[0] == "release_lease") == [
            ("release_lease", 0, OWNER_A),
            ("release_lease", 1, OWNER_A),
        ]
        assert await store.acquire_lease(0, MEMBER_C, 1.0) is None
        assert await store.acquire_lease(1, MEMBER_C, 1.0) is None

    async def test_a_replacement_under_any_id_starts_at_once_after_the_release(self) -> None:
        # Ruling R5: "a `load()` ... that raises while the store answers is
        # followed by an immediate release from the runner's `stop()`, and a
        # replacement under any `member_id` starts at once."
        clock = ManualClock(initial=BASE)
        store = _FaultingLoadStore(clock, faulty_shard=1)
        failed = self._worker(clock, store)
        with pytest.raises(_StoreDown):
            await failed.start()
        await failed.stop()
        store.heal()
        replacement = AggregatorWorker(
            bus=_AnyPartitionsBus(),
            state_store=store,
            clock=clock,
            config=DEFAULTS,
            metrics=AggregatorMetrics(),
            shard_ids=frozenset({0, 1}),
            member_id=MEMBER_B,
            lease_ttl_s=LEASE_TTL_SECONDS,
            instance_id=INSTANCE_B,
        )

        await replacement.start()

        try:
            assert replacement.shards == frozenset({0, 1})
            assert await store.acquire_lease(0, OTHER_MEMBER, 1.0) == _owner(MEMBER_B, INSTANCE_B)
            assert await store.acquire_lease(1, OTHER_MEMBER, 1.0) == _owner(MEMBER_B, INSTANCE_B)
        finally:
            await replacement.stop()

    async def test_without_a_stop_the_leases_lapse_after_the_ttl(self) -> None:
        # "they lapse after `lease_ttl_s` when the store cannot be reached for
        # that either": held until the TTL, then acquirable by anyone.
        clock = ManualClock(initial=BASE)
        store = _FaultingLoadStore(clock, faulty_shard=1)
        worker = self._worker(clock, store)
        with pytest.raises(_StoreDown):
            await worker.start()
        assert await store.acquire_lease(0, OTHER_MEMBER, 1.0) == OWNER_A
        assert await store.acquire_lease(1, OTHER_MEMBER, 1.0) == OWNER_A

        clock.advance(int(LEASE_TTL_SECONDS) + 1)

        assert await store.acquire_lease(0, OTHER_MEMBER, LONG_LEASE_SECONDS) is None
        assert await store.acquire_lease(1, OTHER_MEMBER, LONG_LEASE_SECONDS) is None

    async def test_without_a_stop_a_replacement_with_a_different_id_is_refused_until_then(
        self,
    ) -> None:
        # Ruling R5's crash outcome: "a replacement with a *different* id is
        # refused with `shard_owned_elsewhere` and exits 1 on each attempt
        # for at most `lease_ttl_s`" -- and, since Amendment 6 ruling 2(b),
        # that refusal is the plain one, not the same-member subclass.
        clock = ManualClock(initial=BASE)
        store = _FaultingLoadStore(clock, faulty_shard=1)
        failed = self._worker(clock, store)
        with pytest.raises(_StoreDown):
            await failed.start()
        store.heal()
        replacement = AggregatorWorker(
            bus=_AnyPartitionsBus(),
            state_store=store,
            clock=clock,
            config=DEFAULTS,
            metrics=AggregatorMetrics(),
            shard_ids=frozenset({0, 1}),
            member_id=MEMBER_B,
            lease_ttl_s=LEASE_TTL_SECONDS,
            instance_id=INSTANCE_B,
        )

        with pytest.raises(ShardOwnedElsewhereError) as excinfo:
            await replacement.start()

        assert not isinstance(excinfo.value, ShardHeldBySameMemberError)
        assert replacement.shards == frozenset()

    async def test_without_a_stop_a_replacement_with_the_same_id_waits_for_the_lease(self) -> None:
        # Ruling R5 said "a replacement with the same id reacquires at once
        # either way"; Amendment 6 rulings 2 and 3 supersede exactly that
        # sentence -- "the lease value is the process's token, so a
        # replacement with the same `member_id` is a different instance and
        # does **not** reacquire at once ... the replacement is refused with
        # `shard_held_by_same_member` and waits inside `start()` for the
        # predecessor's leases to lapse -- at most `lease_ttl_s`". Here the
        # wait is the clock advance; `test_service.py` owns the retry loop.
        clock = ManualClock(initial=BASE)
        store = _FaultingLoadStore(clock, faulty_shard=1)
        failed = self._worker(clock, store)
        with pytest.raises(_StoreDown):
            await failed.start()
        store.heal()
        replacement = self._worker(clock, store, instance_id=INSTANCE_B)

        with pytest.raises(ShardHeldBySameMemberError):
            await replacement.start()

        assert replacement.shards == frozenset()
        clock.advance(int(LEASE_TTL_SECONDS) + 1)

        await replacement.start()

        try:
            assert replacement.shards == frozenset({0, 1})
            assert await store.acquire_lease(0, OTHER_MEMBER, 1.0) == _owner(MEMBER_A, INSTANCE_B)
        finally:
            await replacement.stop()

    async def test_without_a_stop_a_claimant_with_the_same_token_reacquires_at_once(self) -> None:
        # The half of ruling R5 Amendment 6 leaves standing: the store "grants
        # iff no live lease exists or the live lease's value equals the
        # caller's token", so a claimant presenting the token the faulted
        # start acquired under is granted a renewal at once.
        clock = ManualClock(initial=BASE)
        store = _FaultingLoadStore(clock, faulty_shard=1)
        failed = self._worker(clock, store)
        with pytest.raises(_StoreDown):
            await failed.start()
        store.heal()
        same_token = self._worker(clock, store, instance_id=INSTANCE_A)

        await same_token.start()

        try:
            assert same_token.shards == frozenset({0, 1})
            assert await store.acquire_lease(0, OTHER_MEMBER, 1.0) == OWNER_A
        finally:
            await same_token.stop()

    async def test_the_fault_propagates_out_of_subscribe_and_the_consumer_holds_nothing(
        self,
    ) -> None:
        # At the `ShardClaims` level, on the plain memory bus with the one
        # shard it has: decision 3's "If `on_assigned` raises, `subscribe()`
        # propagates the exception and holds nothing".
        clock = ManualClock(initial=BASE)
        store = _FaultingLoadStore(clock, faulty_shard=0)
        bus = InMemoryBus()
        consumer = bus.consumer(GROUP)
        claims = ShardClaims(
            state_store=store,
            producer=bus.producer(),
            consumer=consumer,
            clock=clock,
            config=DEFAULTS,
            member_id=MEMBER_A,
            lease_ttl_s=LEASE_TTL_SECONDS,
            instance_id=INSTANCE_A,
        )

        with pytest.raises(_StoreDown):
            await consumer.subscribe(OBSERVATIONS_TOPIC, listener=claims)

        assert claims.window(0) is None
        assert ("acquire_lease", 0, OWNER_A, LEASE_TTL_SECONDS) in store.calls
        assert [call for call in store.calls if call[0] == "release_lease"] == []
        # "as if `subscribe()` had never been called": the empty ack is accepted.
        await consumer.ack([])
        # And the held set still covers the shard: `release()` drops it.
        await claims.release()
        assert ("release_lease", 0, OWNER_A) in store.calls
        assert await store.acquire_lease(0, OTHER_MEMBER, 1.0) is None


class TestReclaimingAShardAfterStop:
    """ADR-0013 decision 7 as amended (Amendment 4 ruling R6): "Which shards
    `on_assigned` skips ...: a shard is skipped only while this member holds
    its lease -- the held-lease set is the test, not the windows. A shard
    whose window survived a `release()` but whose lease is gone is claimed
    afresh: lease acquired, state loaded, a new window built in place of the
    old one; its handled position is kept, as nothing ever lowers it.
    Reachable only by a direct call after `stop()`"."""

    async def _stopped_after_one_promotion(
        self, clock: ManualClock, store: _RecordingStore, *, lease_ttl_s: float
    ) -> tuple[AggregatorWorker, _TappedBus]:
        bus = _TappedBus()
        feed = _Feed(bus)
        worker = _worker(bus=bus, clock=clock, state_store=store, lease_ttl_s=lease_ttl_s)
        await worker.start()
        assert await feed.deliver(worker, IP_A, 1200) is ObservationOutcome.APPLIED
        await worker.stop()
        assert ("release_lease", 0, OWNER_A) in store.calls
        return worker, bus

    async def test_on_assigned_after_stop_takes_the_lease_again(self) -> None:
        clock = ManualClock(initial=BASE)
        store = _RecordingStore(clock)
        worker, _bus = await self._stopped_after_one_promotion(
            clock, store, lease_ttl_s=LONG_LEASE_SECONDS
        )
        store.clear()

        await worker.claims.on_assigned(frozenset({(OBSERVATIONS_TOPIC, 0)}))

        try:
            # "lease acquired, state loaded", in decision 7's order.
            sequence = [call[:2] for call in store.calls if call[0] in ("acquire_lease", "load")]
            assert sequence == [("acquire_lease", 0), ("load", 0)]
            assert ("acquire_lease", 0, OWNER_A, LONG_LEASE_SECONDS) in store.calls
            assert await store.acquire_lease(0, OTHER_MEMBER, 1.0) == OWNER_A
        finally:
            await worker.claims.release()

    async def test_the_reclaim_builds_a_new_window_and_keeps_the_handled_position(self) -> None:
        # ASSUMPTION 12 (assumption 70): a fresh window, seeded from the
        # store's current HOT set with a warm-up, and the position kept.
        clock = ManualClock(initial=BASE)
        store = _RecordingStore(clock)
        worker, _bus = await self._stopped_after_one_promotion(
            clock, store, lease_ttl_s=LONG_LEASE_SECONDS
        )
        old_window = worker.window(0)
        assert old_window is not None
        position = worker.claims.handled_position(0)
        assert position is not None
        clock.advance(HANDOVER_SECONDS)

        await worker.claims.on_assigned(frozenset({(OBSERVATIONS_TOPIC, 0)}))

        try:
            new_window = worker.window(0)
            assert new_window is not None
            assert new_window is not old_window
            assert new_window.hot_ips() == frozenset({IP_A})
            assert new_window.is_inherited(IP_A) is True
            assert new_window.total(IP_A) == 0
            assert new_window.in_warmup is True
            assert new_window.warm_until == BASE + HANDOVER_SECONDS + WINDOW_SECONDS
            assert worker.claims.handled_position(0) == position
            assert worker.shards == frozenset({0})
            assert worker.claims.windows() == (new_window,)
        finally:
            await worker.claims.release()

    async def test_renew_leases_after_the_reclaim_renews_it(self) -> None:
        # The re-taken lease is back in the held set: `renew_leases()` renews
        # it, and the renewal keeps it past its original expiry.
        clock = ManualClock(initial=BASE)
        store = _RecordingStore(clock)
        worker, _bus = await self._stopped_after_one_promotion(
            clock, store, lease_ttl_s=LEASE_TTL_SECONDS
        )
        await worker.claims.on_assigned(frozenset({(OBSERVATIONS_TOPIC, 0)}))
        try:
            clock.advance(20)
            store.clear()

            await worker.claims.renew_leases()

            assert ("acquire_lease", 0, OWNER_A, LEASE_TTL_SECONDS) in store.calls
            assert ("load", 0) not in store.calls
            clock.advance(20)  # 40 s after the reclaim, 20 s after the renewal
            assert await store.acquire_lease(0, OTHER_MEMBER, 1.0) == OWNER_A
        finally:
            await worker.claims.release()

    async def test_a_shard_whose_lease_is_held_is_skipped(self) -> None:
        # The other half of the same sentence: "a shard is skipped only while
        # this member holds its lease" -- on a live member a second
        # `on_assigned` for the held shard loads nothing and keeps the window.
        clock = ManualClock(initial=BASE)
        store = _RecordingStore(clock)
        bus = _TappedBus()
        worker = _worker(bus=bus, clock=clock, state_store=store)
        await worker.start()
        try:
            window = worker.window(0)
            assert window is not None
            store.clear()

            await worker.claims.on_assigned(frozenset({(OBSERVATIONS_TOPIC, 0)}))

            assert ("load", 0) not in store.calls
            assert worker.window(0) is window
        finally:
            await worker.stop()

    async def test_a_shard_whose_lease_another_member_took_is_refused(self) -> None:
        # After `stop()` the lease is free and another member may hold it; the
        # surviving window does not make the shard this member's.
        clock = ManualClock(initial=BASE)
        store = _RecordingStore(clock)
        worker, _bus = await self._stopped_after_one_promotion(
            clock, store, lease_ttl_s=LONG_LEASE_SECONDS
        )
        assert await store.acquire_lease(0, OTHER_MEMBER, LONG_LEASE_SECONDS) is None

        with pytest.raises(ShardOwnedElsewhereError):
            await worker.claims.on_assigned(frozenset({(OBSERVATIONS_TOPIC, 0)}))

        assert await store.acquire_lease(0, MEMBER_C, 1.0) == OTHER_MEMBER


class TestALeasedButUnwindowedShardIsClaimedToCompletion:
    """ADR-0013 decision 7 as corrected (Amendment 5 ruling 1): "`on_assigned` skips
    a shard only when this member holds its lease and has its window ... Such a
    shard is claimed to completion exactly as an unleased one is claimed:
    `acquire_lease` is called again (a renewal of this member's own lease, or a
    refusal if the lease lapsed meanwhile and another member took it -- which
    skipping the acquire would miss), the state is loaded, the window built,
    `shard_claimed` logged; it counts among the shards the call claimed, so a
    refusal later in the same call rolls it back with the others. Ruling (d) is
    unchanged: the faulting call itself still releases nothing."

    The shard that is leased with no window is the one Amendment 3 ruling (d)
    leaves behind, built exactly as `TestAFaultingLoadInsideOnAssigned` builds
    it: shards `{0, 1}` over `_AnyPartitionsBus` (ASSUMPTION 11) with `load(1)`
    raising `_StoreDown` inside `start()`, after which `heal()` lets the store
    answer again. The second `on_assigned` is the direct call the ruling says
    is the only way to reach the case ("Reachable only by a direct second
    call, since the normal path exits 1 after the fault"). Ruling (d) itself
    stays pinned, unmodified, by that class's
    `test_no_lease_is_released_inside_the_call_and_both_stay_held`.

    The `shard_claimed` and `shard_owned_elsewhere` records are not asserted,
    for the reason the module docstring gives for every log record; the
    refusal's owner is asserted where it is observable, on the store.
    """

    def _assigned(self, *shards: int) -> frozenset[tuple[str, int]]:
        """Decision 3's `partitions` argument for `shards`, on the observations topic."""

        return frozenset((OBSERVATIONS_TOPIC, shard) for shard in shards)

    async def _faulted(
        self, clock: ManualClock, store: _FaultingLoadStore, *, shard_ids: frozenset[int]
    ) -> AggregatorWorker:
        """A worker whose `start()` raised on shard 1's `load()`.

        Ruling (d)'s state afterwards: shard 0 leased and windowed, shard 1
        leased with no window, and every shard after 1 never reached -- "a
        refusal on shard `q` has loaded nothing for `q`" applies to the fault
        the same way, since the acquires are strictly sequential (decision 7,
        ruling T6).
        """

        worker = AggregatorWorker(
            bus=_AnyPartitionsBus(),
            state_store=store,
            clock=clock,
            config=DEFAULTS,
            metrics=AggregatorMetrics(),
            shard_ids=shard_ids,
            member_id=MEMBER_A,
            lease_ttl_s=LEASE_TTL_SECONDS,
            instance_id=INSTANCE_A,
        )
        with pytest.raises(_StoreDown):
            await worker.start()
        assert worker.window(0) is not None
        assert worker.window(1) is None
        return worker

    async def test_the_shard_is_acquired_again_and_loaded_while_the_windowed_one_is_skipped(
        self,
    ) -> None:
        # "`acquire_lease` is called again ..., the state is loaded, the window
        # built": for shard 1 only. Shard 0 is a lease *and* a window, so it is
        # skipped and nothing for it reaches the store in this call.
        clock = ManualClock(initial=BASE)
        store = _FaultingLoadStore(clock, faulty_shard=1)
        worker = await self._faulted(clock, store, shard_ids=frozenset({0, 1}))
        store.heal()
        store.clear()

        await worker.claims.on_assigned(self._assigned(0, 1))

        sequence = [call[:2] for call in store.calls if call[0] in ("acquire_lease", "load")]
        assert sequence == [("acquire_lease", 1), ("load", 1)]
        assert ("acquire_lease", 1, OWNER_A, LEASE_TTL_SECONDS) in store.calls
        assert worker.window(1) is not None
        assert worker.window(0) is not None
        assert worker.shards == frozenset({0, 1})

    async def test_renew_leases_afterwards_renews_both_shards(self) -> None:
        # The claim is complete on both shards, so the renewal of decision 7 --
        # `acquire_lease` for every held shard -- covers both and raises
        # nothing; neither shard is loaded again.
        clock = ManualClock(initial=BASE)
        store = _FaultingLoadStore(clock, faulty_shard=1)
        worker = await self._faulted(clock, store, shard_ids=frozenset({0, 1}))
        store.heal()
        await worker.claims.on_assigned(self._assigned(0, 1))
        store.clear()

        await worker.claims.renew_leases()

        assert ("acquire_lease", 0, OWNER_A, LEASE_TTL_SECONDS) in store.calls
        assert ("acquire_lease", 1, OWNER_A, LEASE_TTL_SECONDS) in store.calls
        assert ("load", 0) not in store.calls
        assert ("load", 1) not in store.calls
        assert worker.shards == frozenset({0, 1})

    async def test_the_re_acquire_notices_a_lease_that_lapsed_and_moved(self) -> None:
        # "a refusal if the lease lapsed meanwhile and another member took it
        # -- which skipping the acquire would miss": the lease lapses after
        # `lease_ttl_s` (ruling (d)) and another member takes it before the
        # second call.
        clock = ManualClock(initial=BASE)
        store = _FaultingLoadStore(clock, faulty_shard=1)
        worker = await self._faulted(clock, store, shard_ids=frozenset({0, 1}))
        store.heal()
        clock.advance(int(LEASE_TTL_SECONDS) + 1)
        assert await store.acquire_lease(1, OTHER_MEMBER, LONG_LEASE_SECONDS) is None

        with pytest.raises(ShardOwnedElsewhereError):
            await worker.claims.on_assigned(self._assigned(0, 1))

        # ASSUMPTION 3: the error is asserted by type; "naming the other
        # member" is asserted where it is observable -- the lease is still
        # theirs, and this member holds no window for the shard.
        assert await store.acquire_lease(1, MEMBER_C, 1.0) == OTHER_MEMBER
        assert worker.window(1) is None
        assert 1 not in worker.shards

    async def test_a_refusal_later_in_the_same_call_rolls_the_re_claimed_shard_back(self) -> None:
        # "it counts among the shards the call claimed, so a refusal later in
        # the same call rolls it back with the others" (assumption 85). Shard 2
        # was never reached by the failed `start()`, so another member may hold
        # it; the refusal on 2 releases the lease the call re-took on 1, even
        # though this member held that one before the call.
        clock = ManualClock(initial=BASE)
        store = _FaultingLoadStore(clock, faulty_shard=1)
        worker = await self._faulted(clock, store, shard_ids=frozenset({0, 1, 2}))
        store.heal()
        assert await store.acquire_lease(2, OTHER_MEMBER, LONG_LEASE_SECONDS) is None
        store.clear()

        with pytest.raises(ShardOwnedElsewhereError):
            await worker.claims.on_assigned(self._assigned(0, 1, 2))

        assert ("release_lease", 1, OWNER_A) in store.calls
        assert worker.window(1) is None
        assert 1 not in worker.shards
        # Released for anyone: the other member can take shard 1 at once.
        assert await store.acquire_lease(1, OTHER_MEMBER, LONG_LEASE_SECONDS) is None


class TestStopRunsOnceAndReleasesInAFinally:
    """ADR-0013 decision 8 as amended (Amendment 4 ruling R4): "`stop()` itself runs
    its sequence once: under the lock, `commit_handled()` inside a `try` whose
    `finally` is `release()`, so the leases are released even when the final
    acknowledgement fails -- the exception propagates after the release, and
    the messages it failed to acknowledge reach the next member after
    `ack_wait`, at-least-once as ADR-0003 has it. A later `stop()` ...
    acquires the lock and returns without touching the bus or the store; that
    is what "idempotent" means for it." Assumptions 64-65."""

    async def test_a_second_stop_returns_normally(self) -> None:
        clock = ManualClock(initial=BASE)
        bus = _TappedBus()
        feed = _Feed(bus)
        worker = _worker(bus=bus, clock=clock)
        await worker.start()
        assert await feed.deliver(worker, IP_A, 1200) is ObservationOutcome.APPLIED
        await worker.stop()

        await worker.stop()

    async def test_a_second_stop_after_the_bus_is_closed_returns_normally(self) -> None:
        # "by which time the service has closed the bus and `ack(())` would be
        # a `ValueError`": the runner's `_stop_quietly` after a crash exit.
        clock = ManualClock(initial=BASE)
        bus = _TappedBus()
        feed = _Feed(bus)
        worker = _worker(bus=bus, clock=clock)
        await worker.start()
        assert await feed.deliver(worker, IP_A, 1200) is ObservationOutcome.APPLIED
        await worker.stop()
        await bus.consumers[-1].close()
        # The hazard is real: a second `stop()` that reached the bus would raise.
        with pytest.raises(ValueError):
            await bus.consumers[-1].ack([])

        await worker.stop()

    async def test_a_second_stop_touches_neither_the_bus_nor_the_store(self) -> None:
        clock = ManualClock(initial=BASE)
        trace: list[str] = []
        bus = _TappedBus(trace)
        store = _RecordingStore(clock, trace)
        worker = _worker(bus=bus, clock=clock, state_store=store)
        await worker.start()
        await worker.stop()
        await bus.consumers[-1].close()
        trace.clear()
        store.calls.clear()

        await worker.stop()

        assert trace == []
        assert store.calls == []

    async def test_a_failing_final_acknowledgement_still_releases_the_lease(self) -> None:
        # Assumption 64: "`release()` in `stop()`'s `finally`. When the final
        # acknowledgement fails ... releasing at once lets the next member take
        # it now". The exception still propagates.
        clock = ManualClock(initial=BASE)
        bus = _FailingAckBus()
        state_store = MemoryShardStateStore(clock=clock)
        feed = _Feed(bus)
        worker = _worker(bus=bus, clock=clock, state_store=state_store)
        await worker.start()
        assert await feed.deliver(worker, IP_A, 1200) is ObservationOutcome.APPLIED
        assert await state_store.acquire_lease(0, OTHER_MEMBER, 1.0) == OWNER_A

        with pytest.raises(RuntimeError, match="ack failed"):
            await worker.stop()

        assert await state_store.acquire_lease(0, OTHER_MEMBER, 1.0) is None

    async def test_the_release_follows_the_failed_acknowledgement(self) -> None:
        # The trace of the same failure: flush, the ack that raised, then the
        # release -- the order decision 8 gives, with the `finally` visible.
        clock = ManualClock(initial=BASE)
        trace: list[str] = []
        bus = _FailingAckBus(trace)
        store = _RecordingStore(clock, trace)
        feed = _Feed(bus)
        worker = _worker(bus=bus, clock=clock, state_store=store)
        await worker.start()
        assert await feed.deliver(worker, IP_A, 1200) is ObservationOutcome.APPLIED
        trace.clear()

        with pytest.raises(RuntimeError):
            await worker.stop()

        assert trace == ["flush", "ack", "release_lease"]

    async def test_the_messages_it_failed_to_acknowledge_reach_the_next_member(self) -> None:
        # "the messages it failed to acknowledge reach the next member ...,
        # at-least-once": on the memory bus, at once.
        clock = ManualClock(initial=BASE)
        bus = _FailingAckBus()
        feed = _Feed(bus)
        worker = _worker(bus=bus, clock=clock)
        await worker.start()
        assert await feed.deliver(worker, IP_A, 1200) is ObservationOutcome.APPLIED
        with pytest.raises(RuntimeError):
            await worker.stop()

        next_owner = bus.consumer(GROUP)
        message = await _take_one(await next_owner.subscribe(OBSERVATIONS_TOPIC))

        assert message.key == str(IP_A).encode()

    async def test_a_second_stop_after_a_failed_one_returns_normally(self) -> None:
        # "runs its sequence once": the failed attempt was the one run, and
        # the second neither retries the acknowledgement nor raises.
        clock = ManualClock(initial=BASE)
        trace: list[str] = []
        bus = _FailingAckBus(trace)
        store = _RecordingStore(clock, trace)
        feed = _Feed(bus)
        worker = _worker(bus=bus, clock=clock, state_store=store)
        await worker.start()
        assert await feed.deliver(worker, IP_A, 1200) is ObservationOutcome.APPLIED
        with pytest.raises(RuntimeError):
            await worker.stop()
        trace.clear()

        await worker.stop()

        assert trace == []

    async def test_stop_before_start_returns_normally_and_touches_nothing(self) -> None:
        # ADR-0009 decision 3: `stop()` is "safe before start()".
        clock = ManualClock(initial=BASE)
        store = _RecordingStore(clock)
        worker = _worker(bus=_TappedBus(), clock=clock, state_store=store)

        await worker.stop()

        assert store.calls == []
