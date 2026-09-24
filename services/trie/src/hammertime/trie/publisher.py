"""Emit PrefixStatsChanged so classification stays outside trie maintenance.

Spec: section 3, section 14, section 19, section 22, section 35; ADR-0010
decision 3; ADR-0017 decision 14 and Amendment 2

For each hot-ip event that changes the hot set, the trie publishes one
`PrefixStatsChanged` per ancestor prefix of the event's address, from the
family's minimum reporting length (ruling 1: `/8` for IPv4 and `/104` for IPv6
by default) down to the host route, to `hammertime.prefix-stats.v1`.

The work is two calls (ruling 3, "Why two calls"):

* `prepare()` is synchronous and calls no producer. It reads the event's
  ancestor counts and encodes every message in one section, so the messages
  describe the state between this event and the next, whatever is written
  afterwards (an R2 reader, decision 9).
* `publish()` starts every message's publish before it awaits any of them, and
  returns only once each has returned or raised, so no publish outlives it.
  Publishes that returned are counted as `prefix_stats_published`. If any
  raised an `Exception`, it raises one `PrefixStatsPublishError`, carrying
  only the event's `sequence` and the counts, from the first failed message's
  exception (ruling 2, ruling 5). It never retries.

Each message is keyed by the prefix text and carries it as the envelope's
subject, so the stats of one event carry distinct `event_id`s (decision 14
items 3 and 4). A replay re-publishes the same envelopes under the same ids,
which the log drops inside its duplicate window.
"""

import asyncio
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from types import MappingProxyType
from typing import Final

from hammertime.bus.interface import Producer
from hammertime.bus.topics import PREFIX_STATS
from hammertime.core.addressing.address import Address, AddressFamily
from hammertime.core.events.codec import encode
from hammertime.core.events.envelope import EventEnvelope
from hammertime.core.events.models import PrefixStatsChanged
from hammertime.trie.metadata import ancestor_stats
from hammertime.trie.metrics import TrieMetrics
from hammertime.trie.structure import HotTrie

#: The envelope's `agent_id` for every message the trie publishes (decision 14
#: item 3).
AGENT_ID: Final = "trie-primary"

#: Ruling 1's defaults: the shortest prefix reported per family.
DEFAULT_MIN_PREFIX_LENGTHS: Final[Mapping[AddressFamily, int]] = MappingProxyType(
    {AddressFamily.IPV4: 8, AddressFamily.IPV6: 104}
)

_EVENT_TYPE: Final = "PrefixStatsChanged"


@dataclass(frozen=True, slots=True)
class StatsMessage:
    """One encoded `PrefixStatsChanged`, ready for `Producer.publish`."""

    #: The prefix text: `PREFIX_STATS.key_selector(payload)`.
    key: str
    #: `hammertime.core.events.codec.encode(envelope)`.
    value: bytes
    #: The envelope's `event_id`.
    message_id: str


@dataclass(frozen=True, slots=True)
class PreparedStats:
    """One event's messages, as `prepare()` read and encoded them."""

    family: AddressFamily
    sequence: int
    #: One per `ancestor_stats` entry, in its order: shortest prefix first.
    messages: tuple[StatsMessage, ...]


class PrefixStatsPublishError(Exception):
    """At least one of an event's publishes raised (ruling 2, ruling 5).

    Raised from the first failed message's exception, which is its
    `__cause__`. The text carries the three numbers and nothing else: no
    prefix, and not the cause's text.
    """

    sequence: int
    attempted: int
    failed: int

    def __init__(self, *, sequence: int, attempted: int, failed: int) -> None:
        super().__init__(
            f"PrefixStatsChanged publish failed: sequence={sequence} "
            f"attempted={attempted} failed={failed}"
        )
        self.sequence = sequence
        self.attempted = attempted
        self.failed = failed


class PrefixStatsPublisher:
    """Prepares and publishes an event's `PrefixStatsChanged` messages."""

    def __init__(
        self,
        producer: Producer,
        *,
        metrics: TrieMetrics,
        min_prefix_lengths: Mapping[AddressFamily, int] = DEFAULT_MIN_PREFIX_LENGTHS,
    ) -> None:
        lengths: dict[AddressFamily, int] = {}
        for family in AddressFamily:
            if family not in min_prefix_lengths:
                raise ValueError(f"min_prefix_lengths has no entry for {family.value}")
            length = min_prefix_lengths[family]
            if not isinstance(length, int) or isinstance(length, bool):
                raise ValueError(f"min_prefix_lengths[{family.value}] must be an int")
            if not 0 <= length <= family.bit_length:
                raise ValueError(
                    f"min_prefix_lengths[{family.value}] must be from 0 to {family.bit_length}"
                )
            lengths[family] = length
        self._producer = producer
        self._metrics = metrics
        self._min_prefix_lengths: Mapping[AddressFamily, int] = MappingProxyType(lengths)

    def min_prefix_length(self, family: AddressFamily) -> int:
        """The shortest prefix reported for `family` (ruling 1)."""
        return self._min_prefix_lengths[family]

    def prepare(
        self,
        trie: HotTrie,
        ip: Address,
        *,
        sequence: int,
        config_version: int,
        timestamp: datetime,
    ) -> PreparedStats:
        """Read and encode one event's messages; synchronous, calls no producer.

        A `ValueError` from `ancestor_stats` (an address of the other family)
        and a `CodecError` from `encode` propagate.
        """
        min_length = self.min_prefix_length(trie.family)
        messages: list[StatsMessage] = []
        for stats in ancestor_stats(trie, ip, min_length=min_length):
            prefix_text = str(stats.prefix)
            payload = PrefixStatsChanged(
                prefix=prefix_text,
                hot_count=stats.hot_count,
                capacity=stats.capacity,
                sequence=sequence,
                timestamp=timestamp,
                hot_ratio=stats.hot_ratio,
            )
            envelope = EventEnvelope(
                agent_id=AGENT_ID,
                sequence=sequence,
                event_type=_EVENT_TYPE,
                config_version=config_version,
                timestamp=timestamp,
                subject=prefix_text,
                payload=payload,
            )
            messages.append(
                StatsMessage(
                    key=PREFIX_STATS.key_selector(payload),
                    value=encode(envelope),
                    message_id=envelope.event_id,
                )
            )
        return PreparedStats(family=trie.family, sequence=sequence, messages=tuple(messages))

    async def publish(self, prepared: PreparedStats) -> None:
        """Publish every message at once; return once each has returned or raised.

        Counts each publish that returned. A non-`Exception` `BaseException`
        from a publish, such as a cancellation, is re-raised as it is;
        otherwise any failure is one `PrefixStatsPublishError` from the first
        failed message's exception.
        """
        # `gather` schedules every publish before this awaits any of them, and
        # with `return_exceptions=True` it completes only once each has
        # returned or raised -- also when this call is itself cancelled, in
        # which case it cancels them and still waits for them.
        results = await asyncio.gather(
            *(
                self._producer.publish(
                    PREFIX_STATS.name, message.key, message.value, message_id=message.message_id
                )
                for message in prepared.messages
            ),
            return_exceptions=True,
        )
        failures = [result for result in results if isinstance(result, BaseException)]
        for _ in range(len(results) - len(failures)):
            self._metrics.increment("prefix_stats_published", family=prepared.family)
        for failure in failures:
            if not isinstance(failure, Exception):
                raise failure
        if failures:
            raise PrefixStatsPublishError(
                sequence=prepared.sequence,
                attempted=len(prepared.messages),
                failed=len(failures),
            ) from failures[0]

    async def flush(self) -> None:
        """Await the producer's `flush()`."""
        await self._producer.flush()
