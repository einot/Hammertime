"""Counter storage: in-process, keyed by Address, bounded by retention plus a hard cap.

Spec: section 20, section 26

ADR-0011 decision 3 keeps the store in-process for M3: a durable variant is a
recovery-story decision (what an aggregator that restarts believes about IPs
the trie still holds as HOT), not a storage one, and belongs with issue #7's
shard handover.
"""

from dataclasses import dataclass

from hammertime.aggregator.window.counter import IpCounter, validate_geometry
from hammertime.core.addressing.address import Address
from hammertime.core.errors import HammertimeError
from hammertime.core.state.enums import IpState

DEFAULT_MAX_TRACKED_IPS = 1_000_000


class StoreFullError(HammertimeError):
    """The store is at `max_tracked_ips` and no COLD entry can be evicted.

    Defined here rather than in `core/errors.py`: it is a property of this
    one in-process store, not of the domain (ADR-0011 section 9).
    """


@dataclass(slots=True)
class IpEntry:
    """Everything the aggregator owns for one IP (ADR-0011 decision 3)."""

    counter: IpCounter
    state: IpState
    #: Service-clock time of the last *applied* observation, or of creation if
    #: none was applied yet. Never the observation's event time, and never
    #: refreshed by a lookup, a re-evaluation, a rebucket, an expiry sweep or
    #: an observation diverted to reconciliation.
    last_observed: int


class InMemoryWindowStore:
    """One `IpEntry` per tracked IP, in first-seen order."""

    __slots__ = ("_bucket_seconds", "_entries", "_max_tracked_ips", "_window_seconds")

    def __init__(
        self,
        *,
        window_seconds: int,
        bucket_seconds: int,
        max_tracked_ips: int = DEFAULT_MAX_TRACKED_IPS,
    ) -> None:
        # Same rule as IpCounter.__init__: a store whose geometry no counter
        # could take would otherwise fail only on the first get_or_create,
        # long after the operator could act on it.
        validate_geometry(window_seconds, bucket_seconds)
        self._window_seconds = window_seconds
        self._bucket_seconds = bucket_seconds
        self._max_tracked_ips = max_tracked_ips
        self._entries: dict[Address, IpEntry] = {}

    @property
    def window_seconds(self) -> int:
        """The window every counter this store creates will have."""
        return self._window_seconds

    @property
    def bucket_seconds(self) -> int:
        """The bucket size every counter this store creates will have."""
        return self._bucket_seconds

    def __len__(self) -> int:
        return len(self._entries)

    def __contains__(self, ip: Address) -> bool:
        return ip in self._entries

    def get(self, ip: Address) -> IpEntry | None:
        return self._entries.get(ip)

    def get_or_create(self, ip: Address, *, now: int) -> IpEntry:
        """Return the entry for `ip`, creating a COLD one at `now` if needed.

        On an existing entry nothing is touched -- `last_observed` is not
        refreshed, nothing is evicted and first-seen order is unchanged, so
        `now` is ignored (ADR-0011 decision 3). Creating an entry on a full
        store first evicts the COLD entry with the oldest `last_observed`,
        breaking ties towards the earlier-seen one, and raises
        `StoreFullError` when every entry is HOT.
        """
        existing = self._entries.get(ip)
        if existing is not None:
            return existing
        if len(self._entries) >= self._max_tracked_ips:
            self._evict_one()
        entry = IpEntry(
            counter=IpCounter(
                window_seconds=self._window_seconds, bucket_seconds=self._bucket_seconds
            ),
            state=IpState.COLD,
            last_observed=now,
        )
        self._entries[ip] = entry
        return entry

    def _evict_one(self) -> None:
        victim: Address | None = None
        oldest = 0
        for ip, entry in self._entries.items():
            if entry.state is not IpState.COLD:
                continue
            # Insertion order decides ties, so the earlier-seen entry goes
            # first: strict `<` keeps the first candidate at equal ages.
            if victim is None or entry.last_observed < oldest:
                victim = ip
                oldest = entry.last_observed
        if victim is None:
            raise StoreFullError(
                f"window store is full at {self._max_tracked_ips} tracked IPs "
                "and every entry is HOT"
            )
        del self._entries[victim]

    def remove(self, ip: Address) -> None:
        """Forget `ip`; a no-op when it is not tracked."""
        self._entries.pop(ip, None)

    def entries(self) -> list[tuple[Address, IpEntry]]:
        """Snapshot in first-seen order, safe to mutate the store under."""
        return list(self._entries.items())

    def rebucket(self, *, window_seconds: int, bucket_seconds: int, now: int) -> None:
        """Adopt a new geometry and rebuild every counter under it.

        ADR-0011 decision 7 step 2: the new geometry is validated up front --
        even on an empty store, so an invalid one cannot be latched silently
        and surface only on the next `get_or_create` -- and then becomes the
        store's own, so every entry created afterwards gets the new shape.
        Only counters are touched: `state`, `last_observed` and first-seen
        order are preserved and nothing is evicted, however many buckets die.
        """
        validate_geometry(window_seconds, bucket_seconds)
        self._window_seconds = window_seconds
        self._bucket_seconds = bucket_seconds
        for entry in self._entries.values():
            entry.counter = IpCounter.rebuild(
                entry.counter.buckets(),
                window_seconds=window_seconds,
                bucket_seconds=bucket_seconds,
                now=now,
            )

    @property
    def tracked_ips(self) -> int:
        """Every entry the store holds (spec section 37); O(1)."""
        return len(self._entries)

    @property
    def hot_ips(self) -> int:
        """Entries currently HOT. A gauge for scrapes, not the hot path."""
        return sum(1 for entry in self._entries.values() if entry.state is IpState.HOT)

    @property
    def active_ips(self) -> int:
        """Entries with a non-empty window (spec section 26); a gauge, not the hot path."""
        return sum(1 for entry in self._entries.values() if entry.counter.total > 0)
