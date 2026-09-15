"""Topic names, partition counts, retention, and key selection per topic.

Spec: section 20, section 33; ADR-0001; ADR-0002

Partition counts and retention below are the initial operational defaults,
not spec-mandated values; they are expected to be tuned per deployment.
What IS load-bearing is:

* `HOT_IP.name` == `"hammertime.hot-ip.v1"` exactly -- the Makefile's
  `replay` target and `docs/runbook.md`'s trie-restart procedure hard-code
  this string.
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

from __future__ import annotations

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
    partitions=32,
    retention_seconds=_ONE_DAY_SECONDS,
    key_selector=_ip_key,
    description=(
        "Deduplicated agent observations, fanned out one message per IP: "
        "each message's payload is a single-entry RequestObservation "
        "(ADR-0004), keyed by that entry's IP for stable per-IP shard "
        "ownership (spec section 20)."
    ),
)

OBSERVATIONS_RECONCILIATION = TopicSpec(
    name="hammertime.observations-reconciliation.v1",
    partitions=8,
    retention_seconds=7 * _ONE_DAY_SECONDS,
    key_selector=_ip_key,
    description=(
        "Observations whose window_start fell outside allowed_lateness "
        "(ADR-0002): written here instead of being silently dropped from "
        "the hot path. Keyed by IP, same rule as OBSERVATIONS."
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
