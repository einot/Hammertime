"""Apply evaluate_ip_state and publish the resulting edges.

Spec: section 6, section 19, section 30, section 46.4

ADR-0011 decision 5: one decision function, one event shape, one identity
rule. `decide` is the only place the aggregator asks "did this count move the
IP?", `build_event` the only place a `StateTransition` becomes a wire event,
and `TransitionPublisher` the only place a hot-ip envelope is built.
"""

import dataclasses
from datetime import UTC, datetime

from hammertime.bus.interface import Producer
from hammertime.bus.topics import HOT_IP
from hammertime.core.addressing.address import Address
from hammertime.core.attributes import build_attributes
from hammertime.core.config.models import DetectionConfig
from hammertime.core.events.codec import encode
from hammertime.core.events.envelope import EventEnvelope
from hammertime.core.events.models import HotIpAdded, HotIpRemoved
from hammertime.core.state import IpState, evaluate_ip_state
from hammertime.core.state.transitions import StateTransition

#: The payloads this module publishes. Both travel the same topic under the
#: same sequence counter (ADR-0011 decision 5).
HotIpEvent = HotIpAdded | HotIpRemoved


# This module MUST call hammertime.core.state.machine.evaluate_ip_state rather
# than re-implementing the comparison. Spec section 30 requires a single
# authoritative state machine shared by ingestion, replay, and re-evaluation.
def decide(previous: IpState, window_count: int, config: DetectionConfig) -> StateTransition | None:
    """The edge `window_count` produces from `previous`, or `None` for no edge.

    Delegates the whole of spec section 6's table to `evaluate_ip_state`, so
    the observation path, maintenance and re-evaluation cannot disagree about
    a threshold with each other or with replay (spec section 30).
    """

    current = evaluate_ip_state(previous, window_count, config)
    if current is previous:
        return None
    return StateTransition(previous=previous, current=current)


def build_event(
    ip: Address,
    transition: StateTransition,
    *,
    window_count: int,
    config: DetectionConfig,
    now: int,
) -> HotIpEvent:
    """Turn an edge into the section 19 event that reports it.

    `became_hot` yields `HotIpAdded` carrying the section 46.5 attribute
    document computed under `config`; `became_cold` yields `HotIpRemoved`,
    whose attributes are always absent because a removal deletes the
    attribute record rather than restating it (spec section 46.5).

    `sequence` is `0`: a placeholder meaning "not yet published".
    `TransitionPublisher.publish` rewrites it with the sequence it assigns
    (ADR-0011 decision 5), so the placeholder never reaches the wire.

    A `StateTransition` whose `previous is current` is not a transition at
    all -- `decide` never returns one -- and section 19 has no event for it,
    so it raises `ValueError` instead of being assigned an event type
    arbitrarily.
    """

    timestamp = datetime.fromtimestamp(now, UTC)
    if transition.became_hot:
        return HotIpAdded(
            ip=ip,
            timestamp=timestamp,
            sequence=0,
            window_count=window_count,
            config_version=config.config_version,
            attributes=build_attributes(window_count, config),
        )
    if transition.became_cold:
        return HotIpRemoved(
            ip=ip,
            timestamp=timestamp,
            sequence=0,
            window_count=window_count,
            config_version=config.config_version,
            attributes=None,
        )
    raise ValueError(
        f"{transition.previous} -> {transition.current} is not a transition; "
        "spec section 19 defines no event for it"
    )


class TransitionPublisher:
    """Envelopes and publishes hot-ip events under one per-producer sequence.

    ADR-0003's amendment makes `agent_id` the producing shard rather than the
    agent that observed the traffic, and `sequence` that producer's own
    counter. ADR-0011 decision 5 keeps **one** counter for the whole hot-ip
    stream, shared by `HotIpAdded` and `HotIpRemoved`, so a shard's output has
    a total order a consumer can follow. `subject` is the IP text (ADR-0004
    decision 4), which makes `event_id` unique per
    `(producer, sequence, type, ip)`.

    The counter restarts at `initial_sequence` on every process start, so
    cross-restart `event_id` uniqueness is neither guaranteed nor relied on:
    the trie's redelivery idempotence is by state, not by dedup (ADR-0011
    decision 5).
    """

    __slots__ = ("_next_sequence", "_producer", "_producer_id")

    def __init__(self, producer: Producer, *, producer_id: str, initial_sequence: int = 0) -> None:
        self._producer = producer
        self._producer_id = producer_id
        self._next_sequence = initial_sequence

    @property
    def next_sequence(self) -> int:
        """The sequence the next `publish` will assign."""
        return self._next_sequence

    async def publish(self, event: HotIpEvent) -> EventEnvelope[HotIpEvent]:
        """Publish `event` to `hammertime.hot-ip.v1`; return the envelope sent.

        The event handed in is frozen and is left untouched, placeholder
        `sequence` and all: what is wrapped, encoded and published is
        `dataclasses.replace(event, sequence=n)`, so the wire form satisfies
        `envelope.sequence == envelope.payload.sequence == n` -- the same
        relation ingest keeps between a `RequestObservation` envelope and its
        payload. The codec encodes the two fields separately, so this rewrite
        is what stops them disagreeing (ADR-0011 decision 5).
        """

        sequence = self._next_sequence
        # Claimed before the await so two concurrent publishes can never be
        # handed the same sequence.
        self._next_sequence = sequence + 1
        payload = dataclasses.replace(event, sequence=sequence)
        envelope: EventEnvelope[HotIpEvent] = EventEnvelope(
            agent_id=self._producer_id,
            sequence=sequence,
            event_type="HotIpAdded" if isinstance(payload, HotIpAdded) else "HotIpRemoved",
            config_version=payload.config_version,
            timestamp=payload.timestamp,
            payload=payload,
            subject=str(payload.ip),
        )
        await self._producer.publish(HOT_IP.name, HOT_IP.key_selector(payload), encode(envelope))
        return envelope

    async def flush(self) -> None:
        """Wait until every transition published so far is acknowledged."""
        await self._producer.flush()
