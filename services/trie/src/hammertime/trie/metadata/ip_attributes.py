"""Per-IP attribute records beside the trie, and the single-writer step that couples them.

Spec: section 46.2, sections 46.5-46.9; ADR-0015 decisions 5, 6 and 8
(assumptions 17-23, 37-40, 53, 57-59, 68), Amendment 2 (rulings A, C and D),
Amendment 3 (rulings 1 and 4) and Amendment 5 (rulings 2-7, assumptions
70-84); ADR-0005 decisions 4 and 5; ADR-0014 decisions 1, 3 and 9, and
Amendment 2 A12; ADR-0017 decision 17.

`IpAttributeRecords` is a read-only, family-scoped `Mapping[Address,
IpRecord]` -- and so already the `Collection[Address]` that
`check_attribute_records` takes (ADR-0014 decision 9; Amendment 5 ruling 6).
Each read returns a fresh `IpRecord`: the address's attribute document and its
`request_count`, the `window_count` of the most recent `HotIpAdded` applied
for it (Amendment 5 rulings 1 and 2). It is not a
`MutableMapping`: `record` and `discard` are its only mutators, and
`apply_hot_ip_added` / `apply_hot_ip_removed` are the only documented way to
move it in step with the trie (section 46.5: "never independently").

Every write goes through `canonicalize_ip_attributes`
(`hammertime.core.events.attributes`), the one implementation of the section
46.2 rules, so this module states none of them. The map keeps, per address, one
entry: the canonical compact JSON text that call returned (Amendment 2 ruling
D) and the `request_count`, written and removed together (Amendment 5 rulings
2 and 4); the count is never part of the document. `serialized_bytes` --
section 46.8's `ip_attribute_bytes` -- is the sum of the stored texts' lengths
only, a count adding nothing to it (Amendment 5 ruling 5), and every read
decodes a fresh, exact-typed document behind a read-only view, so nothing a
reader does reaches the map. Membership, `a in records`, is answered from the
stored keys and decodes nothing (Amendment 3 ruling 4); since rule S8 admits
only integers every legal integer-string limit parses, a read's `ValueError`
still means only "the other family" (Amendment 3 ruling 1). `json` is
imported to decode stored text only; this module never serializes a
document. Nothing here interprets a stored value (sections 46.1, 46.9), and
nothing here counts `attributes_rejected` -- the caller that handles a
rejection does (ADR-0015 assumption 37). Not thread-safe (section 28's single
writer, decision 8).
"""

import json
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Final

from hammertime.core.addressing.address import Address, AddressFamily
from hammertime.core.events.attributes import canonicalize_ip_attributes
from hammertime.trie.structure.node import HotTrie

IpAttributes = Mapping[str, object]

#: Section 46.5 / read-api-v1.md: an absent document is stored, and returned,
#: as `{"attributes_version": 1}` -- 24 bytes in the compact form.
DEFAULT_ATTRIBUTES: Final[IpAttributes] = MappingProxyType({"attributes_version": 1})

#: The canonical text a `None` document is stored as, computed once by the
#: validator from the default document (ADR-0015 decision 5).
_DEFAULT_TEXT: Final = canonicalize_ip_attributes(dict(DEFAULT_ATTRIBUTES)).text


@dataclass(frozen=True, slots=True)
class IpRecord:
    """One address's record, as read: its attribute document and its request_count.

    Checks nothing itself: the map builds one only from what a write accepted
    (ADR-0015 Amendment 5 ruling 2).
    """

    #: A read-only view over a document decoded afresh for this read.
    attributes: IpAttributes
    #: Exact `int` >= 0: the `window_count` of the most recent `HotIpAdded` applied.
    request_count: int


class IpAttributeRecords(Mapping[Address, IpRecord]):
    """Section 46.5's per-IP record map for one address family (ADR-0015 Amendment 5)."""

    def __init__(self, family: AddressFamily) -> None:
        self._family = family
        # Per address, the canonical compact JSON text and the request_count,
        # and nothing else (Amendment 5 ruling 2).
        self._records: dict[Address, tuple[str, int]] = {}
        self._bytes = 0

    @property
    def family(self) -> AddressFamily:
        return self._family

    @property
    def serialized_bytes(self) -> int:
        """Section 46.8's `ip_attribute_bytes`: the stored canonical texts' lengths, summed.

        Counts add nothing (Amendment 5 ruling 5).
        """
        return self._bytes

    # -- mutation (section 46.5) --------------------------------------------

    def record(
        self,
        address: Address,
        attributes: Mapping[str, object] | None = None,
        *,
        request_count: int,
    ) -> None:
        """Store `attributes` (`None`: the default) and `request_count` as `address`'s record.

        Replaces any earlier record whole. All-or-nothing: the family check
        (`ValueError`), the count check (`TypeError`, then `ValueError`) and
        the document's canonicalization (`InvalidAttributesError`; a
        non-`dict`, including `DEFAULT_ATTRIBUTES` itself, is refused) all
        run, in that order, before the map changes (Amendment 5 ruling 3).
        """
        self._check_family(address.family)
        _check_request_count(request_count)
        self._commit(address, _prepare(attributes), request_count)

    def discard(self, address: Address) -> bool:
        """Remove `address`'s whole record, count included; return whether there was one.

        Never raises if there is none.
        """
        self._check_family(address.family)
        removed = self._records.pop(address, None)
        if removed is None:
            return False
        self._bytes -= len(removed[0])
        return True

    def clear(self) -> None:
        self._records.clear()
        self._bytes = 0

    # -- Mapping --------------------------------------------------------------

    def __getitem__(self, address: Address) -> IpRecord:
        # A key that is not an Address at all is simply absent; an Address of
        # the other family is a routing bug, reads included (assumption 39).
        if not isinstance(address, Address):
            raise KeyError(address)
        self._check_family(address.family)
        # A fresh document on every read (Amendment 2 ruling D): the reader
        # owns it, and the view keeps its top level read-only. A new IpRecord
        # on every call, too (Amendment 5 ruling 2).
        text, request_count = self._records[address]
        return IpRecord(MappingProxyType(json.loads(text)), request_count)

    def __contains__(self, key: object) -> bool:
        # Answered from the stored keys, decoding nothing (Amendment 3 ruling
        # 4, assumption 68). The type test comes first, so anything that is
        # not an Address -- an unhashable list or dict included -- is simply
        # absent; then the family check, as for every other read.
        if not isinstance(key, Address):
            return False
        self._check_family(key.family)
        return key in self._records

    def __iter__(self) -> Iterator[Address]:
        return iter(self._records)

    def __len__(self) -> int:
        return len(self._records)

    # -- internals ------------------------------------------------------------

    def _commit(self, address: Address, text: str, request_count: int) -> None:
        """Replace `address`'s whole record with `text` and `request_count`; move the byte total.

        Both are already checked. Cannot raise (ADR-0015 assumption 40,
        Amendment 5 ruling 4).
        """
        previous = self._records.get(address)
        self._records[address] = (text, request_count)
        self._bytes += len(text) - (0 if previous is None else len(previous[0]))

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
    *,
    request_count: int,
) -> bool:
    """Section 46.5's `HotIpAdded` step: validate, then the trie, then the record.

    1. `trie`, `records` and `address` must share a family (`ValueError`);
       then `request_count` must be an exact `int` (`TypeError`) and `>= 0`
       (`ValueError`) (Amendment 5 ruling 3).
    2. The document is prepared exactly as `record()` prepares it -- `None`
       selects the default, anything else goes through
       `canonicalize_ip_attributes` (`InvalidAttributesError`) and its
       canonical text is kept -- before the trie is touched, and only here.
    3. `trie.add_hot_ip(address)`, which may raise `InvariantViolation` on a
       corrupt trie, before mutating anything (ADR-0014 A12).
    4. The prepared text and `request_count` together replace any earlier
       record, whatever step 3 returned (Amendment 5 ruling 4). Nothing here
       can raise.
    5. Returns the trie's answer: whether the HOT set changed.

    Every exception therefore leaves the trie and the records as they were.
    """
    _check_families(trie, records, address)
    _check_request_count(request_count)
    prepared = _prepare(attributes)
    changed = trie.add_hot_ip(address)
    records._commit(address, prepared, request_count)
    return changed


def apply_hot_ip_removed(trie: HotTrie, records: IpAttributeRecords, address: Address) -> bool:
    """Section 46.5's `HotIpRemoved` step: the trie, then delete the record unconditionally.

    Returns the trie's answer. A removal's attributes are never stored.
    """
    _check_families(trie, records, address)
    changed = trie.remove_hot_ip(address)
    records.discard(address)
    return changed


def _prepare(attributes: object) -> str:
    """The canonical text to store for `attributes`. May raise; changes nothing.

    `None` is the precomputed default; anything else goes to the validator,
    which reads it once and refuses a non-`dict` (S1) or any other violation.
    """
    if attributes is None:
        return _DEFAULT_TEXT
    return canonicalize_ip_attributes(attributes).text


def _check_request_count(request_count: object) -> None:
    """Amendment 5 ruling 3: an exact `int`, `>= 0`, with no maximum.

    `bool`, other `int` subclasses and integral floats are refused. The
    messages name `request_count` and never contain the value.
    """
    if type(request_count) is not int:
        raise TypeError("request_count must be an exact int")
    if request_count < 0:
        raise ValueError("request_count must be >= 0")


def _check_families(trie: HotTrie, records: IpAttributeRecords, address: Address) -> None:
    if trie.family is records.family is address.family:
        return
    raise ValueError(
        f"trie ({trie.family}), records ({records.family}) and address ({address.family}) "
        "must share one address family"
    )
