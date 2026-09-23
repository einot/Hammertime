"""Per-IP attribute records beside the trie, and the single-writer step that couples them.

Spec: section 46.2, sections 46.5-46.9; ADR-0015 decisions 5, 6 and 8
(assumptions 17-23, 37-40); ADR-0005 decisions 4 and 5; ADR-0014 decisions 1,
3 and 9, and Amendment 2 A12.

`IpAttributeRecords` is a read-only, family-scoped `Mapping[Address,
IpAttributes]` -- and so already the `Collection[Address]` that
`check_attribute_records` takes (ADR-0014 decision 9). It is not a
`MutableMapping`: `record` and `discard` are its only mutators, and
`apply_hot_ip_added` / `apply_hot_ip_removed` are the only documented way to
move it in step with the trie (section 46.5: "never independently").

Every write is validated by `hammertime.core.events.attributes`, the one
implementation of the section 46.2 rules, so this module states none of them
and imports neither `json` nor the schema: the sizes behind section 46.8's
`ip_attribute_bytes` come back from the validator. A stored record is a
private deep copy; nothing here interprets a stored value (sections 46.1,
46.9), and nothing here counts `attributes_rejected` -- the caller that
handles a rejection does (ADR-0015 assumption 37). Not thread-safe (section
28's single writer, decision 8).
"""

from collections.abc import Iterator, Mapping
from types import MappingProxyType
from typing import Final, NamedTuple

from hammertime.core.addressing.address import Address, AddressFamily
from hammertime.core.errors import InvalidAttributesError
from hammertime.core.events.attributes import validate_ip_attributes
from hammertime.trie.structure.node import HotTrie

IpAttributes = Mapping[str, object]

#: Section 46.5 / read-api-v1.md: an absent document is stored, and returned,
#: as `{"attributes_version": 1}` -- 24 bytes in the compact form.
DEFAULT_ATTRIBUTES: Final[IpAttributes] = MappingProxyType({"attributes_version": 1})


class _Prepared(NamedTuple):
    """A validated, privately copied document and its serialized size.

    Building one may raise and changes nothing; committing one cannot raise
    (ADR-0015 assumption 40).
    """

    view: IpAttributes
    size: int


class IpAttributeRecords(Mapping[Address, IpAttributes]):
    """Section 46.5's `ip -> IpAttributes` map for one address family."""

    def __init__(self, family: AddressFamily) -> None:
        self._family = family
        self._records: dict[Address, _Prepared] = {}
        self._bytes = 0

    @property
    def family(self) -> AddressFamily:
        return self._family

    @property
    def serialized_bytes(self) -> int:
        """Section 46.8's `ip_attribute_bytes`: the stored records' compact sizes, summed."""
        return self._bytes

    # -- mutation (section 46.5) --------------------------------------------

    def record(self, address: Address, attributes: Mapping[str, object] | None = None) -> None:
        """Store `attributes` (`None`: the default) as `address`'s record, replacing any.

        All-or-nothing: the family check (`ValueError`) and the document's
        preparation (`InvalidAttributesError`) both run before the map
        changes.
        """
        self._check_family(address.family)
        self._commit(address, _prepare(attributes))

    def discard(self, address: Address) -> bool:
        """Remove `address`'s record; return whether there was one (never raises if none)."""
        self._check_family(address.family)
        removed = self._records.pop(address, None)
        if removed is None:
            return False
        self._bytes -= removed.size
        return True

    def clear(self) -> None:
        self._records.clear()
        self._bytes = 0

    # -- Mapping --------------------------------------------------------------

    def __getitem__(self, address: Address) -> IpAttributes:
        # A key that is not an Address at all is simply absent; an Address of
        # the other family is a routing bug, reads included (assumption 39).
        if not isinstance(address, Address):
            raise KeyError(address)
        self._check_family(address.family)
        return self._records[address].view

    def __iter__(self) -> Iterator[Address]:
        return iter(self._records)

    def __len__(self) -> int:
        return len(self._records)

    # -- internals ------------------------------------------------------------

    def _commit(self, address: Address, prepared: _Prepared) -> None:
        """Replace `address`'s record and move the byte total. Cannot raise."""
        previous = self._records.get(address)
        self._records[address] = prepared
        self._bytes += prepared.size - (0 if previous is None else previous.size)

    def _check_family(self, family: AddressFamily) -> None:
        if family is not self._family:
            raise ValueError(
                f"these records hold {self._family} addresses; got an {family} argument"
            )


def apply_hot_ip_added(
    trie: HotTrie,
    records: IpAttributeRecords,
    address: Address,
    attributes: Mapping[str, object] | None = None,
) -> bool:
    """Section 46.5's `HotIpAdded` step: validate, then the trie, then the record.

    1. `trie`, `records` and `address` must share a family (`ValueError`).
    2. The document is prepared exactly as `record()` prepares it -- `None`
       becomes `DEFAULT_ATTRIBUTES`, anything invalid is an
       `InvalidAttributesError` -- before the trie is touched, and only here.
    3. `trie.add_hot_ip(address)`, which may raise `InvariantViolation` on a
       corrupt trie, before mutating anything (ADR-0014 A12).
    4. The prepared record replaces any earlier one, whatever step 3 returned.
       Nothing here can raise.
    5. Returns the trie's answer: whether the HOT set changed.

    Every exception therefore leaves the trie and the records as they were.
    """
    _check_families(trie, records, address)
    prepared = _prepare(attributes)
    changed = trie.add_hot_ip(address)
    records._commit(address, prepared)
    return changed


def apply_hot_ip_removed(trie: HotTrie, records: IpAttributeRecords, address: Address) -> bool:
    """Section 46.5's `HotIpRemoved` step: the trie, then delete the record unconditionally.

    Returns the trie's answer. A removal's attributes are never stored.
    """
    _check_families(trie, records, address)
    changed = trie.remove_hot_ip(address)
    records.discard(address)
    return changed


def _prepare(attributes: object) -> _Prepared:
    """Normalize, validate, copy and size a document. May raise; changes nothing."""
    source = DEFAULT_ATTRIBUTES if attributes is None else attributes
    if not isinstance(source, Mapping):
        raise InvalidAttributesError(
            f"attributes must be a JSON object, got {type(source).__name__[:64]}"
        )
    candidate = dict(source)
    size = validate_ip_attributes(candidate)
    return _Prepared(view=MappingProxyType(_deep_copy(candidate)), size=size)


def _deep_copy(document: dict[str, object]) -> dict[str, object]:
    """Copy a validated document's containers, iteratively.

    Iterative because a valid document may nest a few hundred levels deep
    (every level costs two bytes of the section 46.2 size cap), which a recursive
    `copy.deepcopy` cannot always reach under the default recursion limit.
    The validator has already confirmed that every container is a `dict` or
    a `list` and that there is no cycle; leaves are immutable scalars and are
    shared. Containers are rebuilt as plain `dict` and `list`, which is what
    the same document reads back as from JSON (section 46.8's replay
    determinism).
    """
    root: dict[str, object] = {}
    pending: list[tuple[object, object]] = [(document, root)]
    while pending:
        source, target = pending.pop()
        if isinstance(source, dict) and isinstance(target, dict):
            for key, value in source.items():
                target[key] = _shell(value, pending)
        elif isinstance(source, list) and isinstance(target, list):
            target.extend(_shell(value, pending) for value in source)
    return root


def _shell(value: object, pending: list[tuple[object, object]]) -> object:
    """An empty container to be filled from `value` later, or the scalar itself."""
    if isinstance(value, dict):
        shell: object = {}
    elif isinstance(value, list):
        shell = []
    else:
        return value
    pending.append((value, shell))
    return shell


def _check_families(trie: HotTrie, records: IpAttributeRecords, address: Address) -> None:
    if trie.family is records.family is address.family:
        return
    raise ValueError(
        f"trie ({trie.family}), records ({records.family}) and address ({address.family}) "
        "must share one address family"
    )
