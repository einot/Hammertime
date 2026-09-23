"""Everything a reader of the trie service may see: tries, record maps, log position, config.

Spec: section 22, section 28, section 33, section 35, section 46.5; ADR-0017
decisions 2, 8 and 9.

`TrieState` holds one `PatriciaTrie` and one `IpAttributeRecords` per address
family served (section 35's separate roots; ADR-0014 decision 1, ADR-0015
decision 5), the log position (the last offset handled or passed), `as_of`
and the detection configuration in force. The read path, `/metrics` and the
snapshot epic depend on this module rather than on the worker, which depends
on the bus.

The log position (decision 8): `position` is the last offset the worker
has *handled* -- a record applied, unchanged, malformed or of a family not
served -- or *passed*: an offset below the first record the log retains,
which `start()` found the trie had not read (decision 4 step 3). It is
`None` before the first of either. `event_sequence` is `0` when `position`
is `None` and `position + 1` otherwise: the offset of the next record the
trie will read, which is also `end_offset`'s convention.
`as_of` is the greatest payload timestamp among applied (or unchanged)
events, so it never goes backwards.

Consistency (decision 9, R1-R3): the worker is the only production writer,
it mutates this state only between two `await`s, and every reader takes what
it reports without an `await` in between, on the one event loop. Nothing
here is thread-safe, and nothing needs to be.
"""

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime
from types import MappingProxyType

from hammertime.core.addressing.address import AddressFamily
from hammertime.core.config.models import DetectionConfig
from hammertime.trie.metadata.ip_attributes import IpAttributeRecords
from hammertime.trie.structure.patricia import PatriciaTrie


@dataclass(frozen=True, slots=True)
class FamilyState:
    """One address family's trie and its attribute record map, moved together."""

    trie: PatriciaTrie
    records: IpAttributeRecords

    @property
    def family(self) -> AddressFamily:
        """The trie's family, which is the records' family."""
        return self.trie.family


class TrieState:
    """The trie service's read state (ADR-0017 decision 2)."""

    __slots__ = ("_as_of", "_config", "_families", "_position")

    def __init__(self, *, families: Iterable[AddressFamily], config: DetectionConfig) -> None:
        served = frozenset(families)
        if not served:
            raise ValueError("TrieState needs at least one address family")
        self._families: Mapping[AddressFamily, FamilyState] = MappingProxyType(
            {
                family: FamilyState(trie=PatriciaTrie(family), records=IpAttributeRecords(family))
                for family in sorted(served)
            }
        )
        self._config = config
        self._position: int | None = None
        self._as_of: datetime | None = None

    # --- reads ---------------------------------------------------------------

    @property
    def families(self) -> frozenset[AddressFamily]:
        return frozenset(self._families)

    def serves(self, family: AddressFamily) -> bool:
        return family in self._families

    def of(self, family: AddressFamily) -> FamilyState:
        """The family's pair; `KeyError` for a family not served."""
        return self._families[family]

    @property
    def config(self) -> DetectionConfig:
        """The detection configuration in force at the trie."""
        return self._config

    @property
    def position(self) -> int | None:
        """The last offset handled or passed (decision 8); `None` before the first."""
        return self._position

    @property
    def event_sequence(self) -> int:
        """`0` if nothing has been handled, else `position + 1` (decision 8)."""
        return 0 if self._position is None else self._position + 1

    @property
    def as_of(self) -> datetime | None:
        """The newest payload timestamp among applied events; `None` before the first."""
        return self._as_of

    # --- the worker's mutators -------------------------------------------------

    def note_handled(self, offset: int) -> None:
        """Record that the record at `offset` was handled without being applied."""
        self._check_offset(offset)
        self._position = offset

    def note_applied(self, offset: int, timestamp: datetime) -> None:
        """Record that the event at `offset`, stamped `timestamp`, was applied."""
        self._check_offset(offset)
        if timestamp.tzinfo is None or timestamp.utcoffset() is None:
            raise ValueError("note_applied needs a timezone-aware timestamp")
        self._position = offset
        if self._as_of is None or timestamp > self._as_of:
            self._as_of = timestamp

    def note_passed(self, offset: int) -> None:
        """Record that the log holds no unread record at or below `offset` (decision 4 step 3).

        Sets `position` only; `as_of` does not move.
        """
        self._check_offset(offset)
        self._position = offset

    def adopt_config(self, config: DetectionConfig) -> None:
        """Replace the configuration in force; compares no versions (decision 10)."""
        self._config = config

    # --- internals -----------------------------------------------------------

    def _check_offset(self, offset: int) -> None:
        if offset < 0:
            raise ValueError(f"offset must be non-negative, got {offset}")
        if self._position is not None and offset <= self._position:
            raise ValueError(f"offset {offset} is not past the position {self._position}")
