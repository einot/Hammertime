"""Topic names, partition counts, retention, and key selection per topic.

Spec: section 20, section 33; ADR-0001; ADR-0002

Partition counts and retention below are the initial operational defaults,
not spec-mandated values; they are expected to be tuned per deployment.
What IS load-bearing is:

* `HOT_IP.name` == `"hammertime.hot-ip.v1"` exactly -- the Makefile's
  `replay` target and `docs/runbook.md`'s trie-restart procedure hard-code
  this string.
* IP-keyed topics (observations, its reconciliation topic, and hot-ip) use
  the same key-selection rule so that spec section 20's "one owner per IP"
  shard assignment is stable across all three.
* `PREFIX_STATS` is keyed by prefix, not IP: ADR-0001 makes the trie
  service a single writer, so there is nothing to shard by IP there.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

_ONE_DAY_SECONDS = 24 * 60 * 60


def _utf8_key(identifier: str) -> bytes:
    """Encode a partitioning identifier (IP text or CIDR prefix text) to key bytes.

    Deterministic and collision-free for distinct identifiers, which is what
    gives an IP (or prefix) stable partition ownership across producers
    (spec section 20).
    """
    return identifier.encode("utf-8")


@dataclass(frozen=True, slots=True)
class TopicSpec:
    """Static configuration for one topic in the canonical registry."""

    name: str
    partitions: int
    retention_seconds: int
    #: Maps this topic's partitioning identifier (documented per topic below)
    #: to the raw key bytes passed to `Producer.publish`.
    key_selector: Callable[[str], bytes]
    description: str


OBSERVATIONS = TopicSpec(
    name="hammertime.observations.v1",
    partitions=32,
    retention_seconds=_ONE_DAY_SECONDS,
    key_selector=_utf8_key,
    description=(
        "Deduplicated agent observations (RequestObservation), keyed by IP "
        "text for stable per-IP shard ownership (spec section 20)."
    ),
)

OBSERVATIONS_RECONCILIATION = TopicSpec(
    name="hammertime.observations-reconciliation.v1",
    partitions=8,
    retention_seconds=7 * _ONE_DAY_SECONDS,
    key_selector=_utf8_key,
    description=(
        "Observations whose window_start fell outside allowed_lateness "
        "(ADR-0002): written here instead of being silently dropped from "
        "the hot path. Keyed by IP text, same rule as OBSERVATIONS."
    ),
)

HOT_IP = TopicSpec(
    name="hammertime.hot-ip.v1",
    partitions=32,
    retention_seconds=30 * _ONE_DAY_SECONDS,
    key_selector=_utf8_key,
    description=(
        "HotIpAdded/HotIpRemoved transitions. The trie is derived state, "
        "reconstructed by replaying this topic (spec section 32, section "
        "33). Keyed by IP text. This exact name is referenced by `make "
        "replay` and docs/runbook.md's trie-restart procedure -- do not "
        "rename."
    ),
)

PREFIX_STATS = TopicSpec(
    name="hammertime.prefix-stats.v1",
    partitions=4,
    retention_seconds=_ONE_DAY_SECONDS,
    key_selector=_utf8_key,
    description=(
        "PrefixStatsChanged, emitted by the single-writer trie service "
        "(ADR-0001). Keyed by CIDR prefix text, not IP -- there is no "
        "cross-shard aggregation to preserve here."
    ),
)

_REGISTRY: dict[str, TopicSpec] = {
    spec.name: spec for spec in (OBSERVATIONS, OBSERVATIONS_RECONCILIATION, HOT_IP, PREFIX_STATS)
}


def lookup(name: str) -> TopicSpec:
    """Return the registered `TopicSpec` for `name`.

    Raises `KeyError` if `name` is not a registered topic.
    """
    try:
        return _REGISTRY[name]
    except KeyError:
        raise KeyError(f"unknown topic: {name!r}") from None


def all_topics() -> tuple[TopicSpec, ...]:
    """Every registered topic, e.g. for provisioning/admin tooling."""
    return tuple(_REGISTRY.values())
