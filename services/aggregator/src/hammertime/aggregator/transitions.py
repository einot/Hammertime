"""Apply evaluate_ip_state and publish the resulting edges.

Spec: section 6, section 19, section 30, section 46.4

ADR-0011 decision 4, with Amendment 2 item A11 (an applied observation can
demote) and Amendment 3 item A14 (`EmittedTransition.transition` is core's
`StateTransition`).

This module MUST call `hammertime.core.state.machine.evaluate_ip_state`
rather than re-implementing the comparison, and it is the aggregator's only
caller of it: spec section 30 requires a single authoritative state machine
shared by ingestion, replay, and re-evaluation.

The order of decision 4's five steps is load-bearing. The durable HOT set is
updated *before* the event exists, so a crash between the two leaves the
store saying HOT and the trie not told -- which the next owner's inheritance
and warm-up repair -- rather than the trie holding an IP no owner knows
about, which is the permanent section 12 leak ADR-0011 exists to close.
"""

from dataclasses import dataclass
from datetime import UTC, datetime

from hammertime.aggregator.metrics import AggregatorMetrics
from hammertime.aggregator.window.store import ShardWindow
from hammertime.bus.interface import Producer
from hammertime.bus.topics import HOT_IP
from hammertime.core.addressing.address import Address
from hammertime.core.config.models import DetectionConfig
from hammertime.core.events.codec import encode
from hammertime.core.events.envelope import EventEnvelope
from hammertime.core.events.models import HotIpAdded, HotIpRemoved
from hammertime.core.state.enums import IpState
from hammertime.core.state.machine import evaluate_ip_state
from hammertime.core.state.transitions import StateTransition
from hammertime.core.state.weight import transition_attributes
from hammertime.core.time.clock import Clock
from hammertime.store.interface import ShardStateStore


@dataclass(frozen=True, slots=True)
class EmittedTransition:
    """One published HOT/COLD edge, as the caller that triggered it sees it."""

    ip: Address
    #: `StateTransition(previous=<state read at the top of evaluate>,
    #: current=<evaluate_ip_state's result>)` -- the value type core already
    #: ships for exactly this edge (item A14).
    transition: StateTransition
    sequence: int
    window_count: int
    config_version: int


class TransitionEmitter:
    """Evaluates one IP and, on an edge, persists then publishes it (decision 4)."""

    __slots__ = ("_clock", "_metrics", "_producer", "_state_store")

    def __init__(
        self,
        *,
        producer: Producer,
        state_store: ShardStateStore,
        clock: Clock,
        metrics: AggregatorMetrics,
    ) -> None:
        self._producer = producer
        self._state_store = state_store
        self._clock = clock
        self._metrics = metrics

    async def evaluate(
        self, window: ShardWindow, ip: Address, *, config: DetectionConfig, reason: str
    ) -> EmittedTransition | None:
        """Evaluate `ip` under `config`; emit and return the edge, or None.

        None means "nothing to announce": either the state machine kept the
        IP where it was, or the result is a HOT -> COLD for an IP this owner
        inherited and has not finished warming up (decision 5, whatever the
        trigger) -- a demotion the new owner would only be making because it
        has not yet seen a window's worth of traffic.

        `reason` is the path that called: `observation`, `expiry`, `warmup`
        or `config`. It labels the transition counters and nothing else.

        A failure from the state store propagates: the transition is not
        emitted and the state is not changed in memory. The sequence number
        is consumed; gaps are harmless, because `subject` qualifies the
        `event_id` (ADR-0004).
        """
        previous = window.state(ip)
        count = window.total(ip)
        new = evaluate_ip_state(previous, count, config)
        if new is previous:
            return None
        if new is IpState.COLD and window.in_warmup and window.is_inherited(ip):
            return None

        sequence = window.next_sequence
        window.next_sequence += 1
        await self._state_store.record_transition(window.shard, ip, new, sequence)

        timestamp = datetime.fromtimestamp(self._clock.now(), tz=UTC)
        payload: HotIpAdded | HotIpRemoved
        if new is IpState.HOT:
            event_type = "HotIpAdded"
            payload = HotIpAdded(
                ip=ip,
                timestamp=timestamp,
                sequence=sequence,
                window_count=count,
                config_version=config.config_version,
                # Section 46.4 / ADR-0005: the weight is computed under the
                # configuration in force at the transition.
                attributes=transition_attributes(count, config),
            )
        else:
            event_type = "HotIpRemoved"
            # Section 46.5: a removal deletes the attribute record, so
            # carrying attributes on one would describe state nothing keeps.
            payload = HotIpRemoved(
                ip=ip,
                timestamp=timestamp,
                sequence=sequence,
                window_count=count,
                config_version=config.config_version,
                attributes=None,
            )
        envelope = EventEnvelope(
            # ADR-0011 assumption 11: the producing shard, not an agent.
            agent_id=f"aggregator-shard-{window.shard}",
            sequence=sequence,
            event_type=event_type,
            config_version=config.config_version,
            timestamp=timestamp,
            subject=str(ip),
            payload=payload,
        )
        # Both bus producers return only once the broker has acknowledged, so
        # there is no separate flush per transition.
        await self._producer.publish(
            HOT_IP.name, key=HOT_IP.key_selector(payload), value=encode(envelope)
        )

        window.set_state(ip, new)
        self._metrics.increment(
            "cold_to_hot_transitions" if new is IpState.HOT else "hot_to_cold_transitions",
            shard=window.shard,
            config_version=config.config_version,
            reason=reason,
        )
        return EmittedTransition(
            ip=ip,
            transition=StateTransition(previous=previous, current=new),
            sequence=sequence,
            window_count=count,
            config_version=config.config_version,
        )
