"""Per-IP attribute records beside the trie, and the single-writer step that couples them.

Spec: section 46.2, sections 46.5-46.9; ADR-0015 decisions 5, 6 and 8
(assumptions 17-23, 37-40, 53, 57-59, 68), Amendment 2 (rulings A, C and D)
and Amendment 3 (rulings 1 and 4); ADR-0005 decisions 4 and 5; ADR-0014
decisions 1, 3 and 9, and Amendment 2 A12.

`IpAttributeRecords` is a read-only, family-scoped `Mapping[Address,
IpAttributes]` -- and so already the `Collection[Address]` that
`check_attribute_records` takes (ADR-0014 decision 9). It is not a
`MutableMapping`: `record` and `discard` are its only mutators, and
`apply_hot_ip_added` / `apply_hot_ip_removed` are the only documented way to
move it in step with the trie (section 46.5: "never independently").

Every write goes through `canonicalize_ip_attributes`
(`hammertime.core.events.attributes`), the one implementation of the section
46.2 rules, so this module states none of them. The map keeps, per address, only
the canonical compact JSON text that call returned (Amendment 2 ruling D):
`serialized_bytes` -- section 46.8's `ip_attribute_bytes` -- is the sum of the
stored texts' lengths, and every read decodes a fresh, exact-typed document
behind a read-only view, so nothing a reader does reaches the map. Membership,
`a in records`, is answered from the stored keys and decodes nothing
(Amendment 3 ruling 4); since rule S8 admits only integers every legal
integer-string limit parses, a read's `ValueError` still means only "the other
family" (Amendment 3 ruling 1). `json` is
imported to decode stored text only; this module never serializes a
document. Nothing here interprets a stored value (sections 46.1, 46.9), and
nothing here counts `attributes_rejected` -- the caller that handles a
rejection does (ADR-0015 assumption 37). Not thread-safe (section 28's single
writer, decision 8).
"""

import json
from collections.abc import Iterator, Mapping
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


class IpAttributeRecords(Mapping[Address, IpAttributes]):
    """Section 46.5's `ip -> IpAttributes` map for one address family."""

    def __init__(self, family: AddressFamily) -> None:
        self._family = family
        # Per address, the canonical compact JSON text and nothing else.
        self._records: dict[Address, str] = {}
        self._bytes = 0

    @property
    def family(self) -> AddressFamily:
        return self._family

    @property
    def serialized_bytes(self) -> int:
        """Section 46.8's `ip_attribute_bytes`: the stored canonical texts' lengths, summed."""
        return self._bytes

    # -- mutation (section 46.5) --------------------------------------------

    def record(self, address: Address, attributes: Mapping[str, object] | None = None) -> None:
        """Store `attributes` (`None`: the default) as `address`'s record, replacing any.

        All-or-nothing: the family check (`ValueError`) and the document's
        canonicalization (`InvalidAttributesError`; a non-`dict`, including
        `DEFAULT_ATTRIBUTES` itself, is refused) both run before the map
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
        self._bytes -= len(removed)
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
        # A fresh document on every read (Amendment 2 ruling D): the reader
        # owns it, and the view keeps its top level read-only.
        return MappingProxyType(json.loads(self._records[address]))

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

    def _commit(self, address: Address, text: str) -> None:
        """Replace `address`'s record with canonical `text` and move the byte total.

        Cannot raise (ADR-0015 assumption 40).
        """
        previous = self._records.get(address)
        self._records[address] = text
        self._bytes += len(text) - (0 if previous is None else len(previous))

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
       selects the default, anything else goes through
       `canonicalize_ip_attributes` (`InvalidAttributesError`) and its
       canonical text is kept -- before the trie is touched, and only here.
    3. `trie.add_hot_ip(address)`, which may raise `InvariantViolation` on a
       corrupt trie, before mutating anything (ADR-0014 A12).
    4. The prepared text replaces any earlier record, whatever step 3 returned.
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


def _prepare(attributes: object) -> str:
    """The canonical text to store for `attributes`. May raise; changes nothing.

    `None` is the precomputed default; anything else goes to the validator,
    which reads it once and refuses a non-`dict` (S1) or any other violation.
    """
    if attributes is None:
        return _DEFAULT_TEXT
    return canonicalize_ip_attributes(attributes).text


def _check_families(trie: HotTrie, records: IpAttributeRecords, address: Address) -> None:
    if trie.family is records.family is address.family:
        return
    raise ValueError(
        f"trie ({trie.family}), records ({records.family}) and address ({address.family}) "
        "must share one address family"
    )
