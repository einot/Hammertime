"""The trie service's series: four counters, one gauge, five read off the state.

Spec: section 37, section 46.8; ADR-0017 decision 12 and Amendment 2 ruling 7.

Follows `AggregatorMetrics` (ADR-0011 decision 8, Amendment 3 A13). One series
is one `(name, label values)` pair, and label values are compared as
`str(value)`, so `family=AddressFamily.IPV4` and `family="ipv4"` name the same
series. The name must be one of the series below and the label names exactly
that series' own, or `increment`, `set` and `get` raise `ValueError`, so a
typo cannot quietly become an empty series. `increment` accepts counters only,
`set` gauges only.

The state-derived series are never stored: `get` computes them on each call
from the `TrieState` that `bind_state` was given (the worker binds it in its
constructor). `get` reads `0` for a counter never incremented, a gauge never
set, any series before `bind_state`, and a `family` the state does not serve.

Rendering on `/metrics` is the telemetry epic's; until then `/metrics` is
empty, as the aggregator's is.
"""

from hammertime.trie.state import TrieState

#: `increment` accepts these and nothing else.
COUNTERS: dict[str, tuple[str, ...]] = {
    "trie_updates": ("family", "event_type", "result"),
    "hot_ip_events_skipped": ("reason",),
    "attributes_rejected": ("stage",),
    #: `PrefixStatsChanged` publishes that returned, duplicates included
    #: (ADR-0017 Amendment 2 ruling 7).
    "prefix_stats_published": ("family",),
}

#: `set` accepts these and nothing else.
GAUGES: dict[str, tuple[str, ...]] = {
    "trie_recovery_seconds": (),
}

#: Computed on each `get` from the bound state; never stored.
STATE_SERIES: dict[str, tuple[str, ...]] = {
    "trie_nodes": ("family",),
    "hot_ip_count": ("family",),
    "ip_attribute_records": ("family",),
    "ip_attribute_bytes": ("family",),
    "event_sequence": (),
}

SERIES: dict[str, tuple[str, ...]] = {**COUNTERS, **GAUGES, **STATE_SERIES}

#: One series: its name plus its label values, in the declared label order.
_SeriesKey = tuple[str, tuple[str, ...]]


class TrieMetrics:
    """Decision 12's series, with strict names and strict label names.

    One instance per process, on the one event loop; not thread-safe.
    """

    __slots__ = ("_counters", "_gauges", "_state")

    def __init__(self) -> None:
        self._counters: dict[_SeriesKey, int] = {}
        self._gauges: dict[_SeriesKey, float] = {}
        self._state: TrieState | None = None

    def bind_state(self, state: TrieState) -> None:
        """Point the state-derived series at `state`; called by `TrieWorker.__init__`."""
        self._state = state

    def increment(self, name: str, **labels: object) -> None:
        """Add 1 to one counter."""
        if name not in COUNTERS:
            raise ValueError(f"{name!r} is not a counter; increment accepts {sorted(COUNTERS)}")
        key = self._key(name, labels)
        self._counters[key] = self._counters.get(key, 0) + 1

    def set(self, name: str, value: float, **labels: object) -> None:
        """Set one gauge."""
        if name not in GAUGES:
            raise ValueError(f"{name!r} is not a gauge; set accepts {sorted(GAUGES)}")
        key = self._key(name, labels)
        self._gauges[key] = value

    def get(self, name: str, **labels: object) -> int | float:
        """Read one series: a counter, a gauge, or a value derived from the state now."""
        key = self._key(name, labels)
        if name in STATE_SERIES:
            return self._derived(name, key[1])
        if name in GAUGES:
            return self._gauges.get(key, 0)
        return self._counters.get(key, 0)

    # --- internals -----------------------------------------------------------

    @staticmethod
    def _key(name: str, labels: dict[str, object]) -> _SeriesKey:
        """Validate the name and the label *names*; label values are not validated."""
        declared = SERIES.get(name)
        if declared is None:
            raise ValueError(f"unknown metric {name!r}; known series are {sorted(SERIES)}")
        if set(labels) != set(declared):
            raise ValueError(f"{name} carries labels {list(declared)}, got {sorted(labels)}")
        return name, tuple(str(labels[label]) for label in declared)

    def _derived(self, name: str, values: tuple[str, ...]) -> int:
        state = self._state
        if state is None:
            return 0
        if name == "event_sequence":
            return state.event_sequence
        family_text = values[0]
        for family in state.families:
            if str(family) != family_text:
                continue
            fs = state.of(family)
            if name == "trie_nodes":
                return fs.trie.node_count
            if name == "hot_ip_count":
                return fs.trie.hot_ip_count
            if name == "ip_attribute_records":
                return len(fs.records)
            return fs.records.serialized_bytes
        return 0
