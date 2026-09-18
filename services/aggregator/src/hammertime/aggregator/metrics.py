"""The aggregator's counters: four it is told about, five it reads off its windows.

Spec: section 37

ADR-0011 decision 8, as pinned by Amendment 3 item A13. One series is one
`(name, label values)` pair, exactly as Prometheus identifies a time series,
so label values are compared as `str(value)` and `shard=0` and `shard="0"`
name the same series.

`name` MUST be one of the nine series below and the label names passed MUST
be exactly that series' own -- no more, no fewer -- or both `increment` and
`get` raise `ValueError`, so a typo cannot quietly become an empty series.

The four *event counters* are the only series `increment` accepts; the
emitter and the worker bump them at the points decisions 3 and 4 name. The
five *window-derived* series are never stored: `get` computes them on each
call from the windows `bind_windows` was given, which is how item A7's "read
at export time" happens and why a revoked shard's series vanish with its
window. Rendering them on `/metrics` is the telemetry epic's (section 37,
ADR-0009 decision 4).
"""

from collections.abc import Callable, Iterable

from hammertime.aggregator.window.store import ShardWindow

#: `increment` accepts these four and nothing else (item A13).
EVENT_COUNTERS: dict[str, tuple[str, ...]] = {
    "cold_to_hot_transitions": ("shard", "config_version", "reason"),
    "hot_to_cold_transitions": ("shard", "config_version", "reason"),
    "late_messages": ("reason",),
    "observations_rejected": ("reason",),
}

#: Computed on each `get` from the bound windows; never stored.
WINDOW_SERIES: dict[str, tuple[str, ...]] = {
    "tracked_ips": ("shard",),
    "active_ips": ("shard",),
    "hot_ips": ("shard",),
    "window_evictions": ("shard", "reason"),
    "shards_claimed": (),
}

SERIES: dict[str, tuple[str, ...]] = {**EVENT_COUNTERS, **WINDOW_SERIES}

#: One series: its name plus its label values, in the declared label order.
_SeriesKey = tuple[str, tuple[str, ...]]


class AggregatorMetrics:
    """Decision 8's nine series, with strict names and strict label names.

    One instance per process: the service builds it, the worker binds its
    claimed windows to it, and the worker and the emitter increment it. Not
    thread-safe -- a plain dict, on the one event loop.
    """

    __slots__ = ("_counters", "_windows")

    def __init__(self) -> None:
        self._counters: dict[_SeriesKey, int] = {}
        self._windows: Callable[[], Iterable[ShardWindow]] | None = None

    def bind_windows(self, windows: Callable[[], Iterable[ShardWindow]]) -> None:
        """Point the window-derived series at the currently claimed windows.

        Called once by `AggregatorWorker.__init__`. Until it is called every
        derived series reads 0 (item A13) -- an unbound registry is empty,
        not an error.
        """
        self._windows = windows

    def increment(self, name: str, **labels: object) -> None:
        """Add 1 to one event counter."""
        if name not in EVENT_COUNTERS:
            raise ValueError(
                f"{name!r} is not an event counter; increment accepts {sorted(EVENT_COUNTERS)}"
            )
        key = self._key(name, labels)
        self._counters[key] = self._counters.get(key, 0) + 1

    def get(self, name: str, **labels: object) -> int:
        """Read one series: a counter's value, or a window-derived value computed now.

        0 for an event counter never incremented, for a derived series naming
        a shard no bound window has, and for every derived series before
        `bind_windows` (item A13).
        """
        key = self._key(name, labels)
        if name in WINDOW_SERIES:
            return self._derived(name, key[1])
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
        windows = () if self._windows is None else tuple(self._windows())
        if name == "shards_claimed":
            return len(windows)
        shard = values[0]
        for window in windows:
            if str(window.shard) != shard:
                continue
            if name == "tracked_ips":
                return window.tracked_count
            if name == "active_ips":
                return window.active_count
            if name == "hot_ips":
                return window.hot_count
            if values[1] == "retention":
                return window.retention_evictions
            if values[1] == "capacity":
                return window.capacity_evictions
            # `window_evictions` with a reason outside the two reads 0.
            return 0
        return 0
