"""Topic names, partition counts, retention, and key selection per topic.

Spec: section 20, section 33; ADR-0001; ADR-0002

Retention below, and most of the partition counts, are initial operational
defaults rather than spec-mandated values; they are expected to be tuned
per deployment. What IS load-bearing is:

* `HOT_IP.name` == `"hammertime.hot-ip.v1"` exactly -- the Makefile's
  `replay` target and `docs/runbook.md`'s trie-restart procedure hard-code
  this string.
* `OBSERVATIONS.partitions` is *not* one of those free tuning knobs. An
  aggregator shard IS a partition of `hammertime.observations.v1` (epic
  #7, ADR-0011): `hash(ip) -> shard` is the bus's own key partitioner
  acting on the per-IP key ingest already publishes, and the aggregator
  reads an IP's shard off `ConsumedMessage.partition` rather than hashing
  anything itself. Two consequences, both of which are the reason this
  number is what it is:
    1. It is the hard ceiling on aggregator parallelism. At N partitions,
       replica N+1 gets no assignment and sits idle. 128 buys headroom to
       128 workers for a little per-partition broker overhead.
    2. Changing it once the topic carries data is a state-invalidating
       migration, not a retune: with key-based partitioning every IP
       remaps to a different partition, which invalidates every shard's
       persisted HOT set and in-flight window state at the same instant.
  So do not "tidy" this value up or down in passing; it is cheap to set
  correctly on an empty topic and expensive on a running one.
* Each `TopicSpec.key_selector` takes the domain event/payload it publishes
  (a dataclass from `hammertime.core.events.models`) and derives that
  topic's partition key directly, so a publisher never needs to know which
  field matters for which topic -- it just calls
  `TOPICS[name].key_selector(event)`. IP-keyed topics (observations, its
  reconciliation topic, and hot-ip) derive the key from the event's IP,
  so spec section 20's "one owner per IP" shard assignment is stable across
  all three, regardless of any other field on the event.
* `PREFIX_STATS` is keyed by prefix, not IP: ADR-0001 makes the trie
  service a single writer, so there is nothing to shard by IP there.
"""

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

from hammertime.core.events.models import (
    HotIpAdded,
    HotIpRemoved,
    Observation,
    PrefixStatsChanged,
)

_ONE_DAY_SECONDS = 24 * 60 * 60

#: Events published to the IP-keyed topics (observations, its
#: reconciliation topic, hot-ip). All three shard the same way (spec
#: section 20), so they share one key-selection rule.
IpKeyedEvent = HotIpAdded | HotIpRemoved | Observation


def _ip_key(event: IpKeyedEvent) -> str:
    """Partition key for IP-keyed topics: the event's own IP, as text.

    Deterministic per IP regardless of any other field on the event, which
    is what gives an IP stable partition ownership across producers (spec
    section 20).
    """
    return str(event.ip)


def _prefix_key(event: PrefixStatsChanged) -> str:
    """Partition key for the prefix-stats topic: the event's CIDR prefix text.

    ADR-0001: the trie service is a single writer, so this exists for
    interface symmetry with the IP-keyed topics rather than for load
    distribution.
    """
    return event.prefix


@dataclass(frozen=True, slots=True)
class TopicSpec:
    """Static configuration for one topic in the canonical registry."""

    name: str
    partitions: int
    retention_seconds: int
    #: Derives this topic's `Producer.publish` key from the domain event
    #: being published to it -- callers pass a key already derived via
    #: `TOPICS[name].key_selector(event)`, not a raw domain object, to
    #: `publish()` (see `interface.py`).
    key_selector: Callable[[Any], str]
    description: str


OBSERVATIONS = TopicSpec(
    name="hammertime.observations.v1",
    # A partition of this topic *is* an aggregator shard (epic #7,
    # ADR-0011), so this caps aggregator parallelism at 128 replicas and
    # can only be changed by a migration that rebuilds every shard's
    # state. See the module docstring before touching it.
    partitions=128,
    retention_seconds=_ONE_DAY_SECONDS,
    key_selector=_ip_key,
    description=(
        "Deduplicated agent observations, fanned out one message per IP: "
        "each message's payload is a single-entry RequestObservation "
        "(ADR-0004), keyed by that entry's IP for stable per-IP shard "
        "ownership (spec section 20). One partition == one aggregator "
        "shard, so the partition count is the aggregator's parallelism "
        "ceiling and is not freely retunable once the topic has data."
    ),
)

OBSERVATIONS_RECONCILIATION = TopicSpec(
    name="hammertime.observations-reconciliation.v1",
    partitions=8,
    retention_seconds=7 * _ONE_DAY_SECONDS,
    key_selector=_ip_key,
    description=(
        "Every observation the hot path could not use, written here instead "
        "of being silently dropped: window_start outside allowed_lateness "
        "(ADR-0002), dated in the future, older than the window it would "
        "land in, or otherwise unusable by the aggregator (ADR-0011 "
        "decision 3). Keyed by IP, same rule as OBSERVATIONS."
    ),
)

HOT_IP = TopicSpec(
    name="hammertime.hot-ip.v1",
    partitions=32,
    retention_seconds=30 * _ONE_DAY_SECONDS,
    key_selector=_ip_key,
    description=(
        "HotIpAdded/HotIpRemoved transitions. The trie is derived state, "
        "reconstructed by replaying this topic (spec section 32, section "
        "33). Keyed by IP. This exact name is referenced by `make replay` "
        "and docs/runbook.md's trie-restart procedure -- do not rename."
    ),
)

PREFIX_STATS = TopicSpec(
    name="hammertime.prefix-stats.v1",
    partitions=4,
    retention_seconds=_ONE_DAY_SECONDS,
    key_selector=_prefix_key,
    description=(
        "PrefixStatsChanged, emitted by the single-writer trie service "
        "(ADR-0001). Keyed by CIDR prefix, not IP -- there is no "
        "cross-shard aggregation to preserve here."
    ),
)

#: The canonical topic registry, by name. `TOPICS[name]` is the "lookup by
#: name" mechanism; an unknown name raises the `KeyError` a plain mapping
#: raises.
TOPICS: Mapping[str, TopicSpec] = {
    spec.name: spec for spec in (OBSERVATIONS, OBSERVATIONS_RECONCILIATION, HOT_IP, PREFIX_STATS)
}


def all_topics() -> tuple[TopicSpec, ...]:
    """Every registered topic, e.g. for provisioning/admin tooling."""
    return tuple(TOPICS.values())
