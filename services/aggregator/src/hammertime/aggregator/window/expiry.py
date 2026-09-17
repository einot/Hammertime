"""Sweep expired buckets, then evict IPs idle beyond state_retention_seconds.

Spec: section 5, section 26

The window store holds far more IPs than the trie: every active IP versus only
currently hot ones. Retention (default 10 minutes for a 5 minute window) is
what keeps the store bounded (spec section 26).

ADR-0011 decision 4 runs these in this order under one lock: expiry first, so
a HOT IP whose window has just emptied emits its `HotIpRemoved` before it can
ever be considered idle.
"""

from hammertime.aggregator.window.store import InMemoryWindowStore
from hammertime.core.addressing.address import Address
from hammertime.core.state.enums import IpState


def expire_buckets(store: InMemoryWindowStore, *, now: int) -> list[Address]:
    """Expire every entry's counter; return the IPs whose total changed.

    Counts only move: nothing is evicted and no state is changed here. The
    HOT/COLD edge comes from re-evaluating the returned entries (spec
    section 5, ADR-0011 decision 3).
    """
    changed: list[Address] = []
    for ip, entry in store.entries():
        if entry.counter.expire(now):
            changed.append(ip)
    return changed


def evict_idle(store: InMemoryWindowStore, *, now: int, state_retention_seconds: int) -> int:
    """Remove COLD entries idle for at least `state_retention_seconds`.

    Retention is judged on arrival time -- `last_observed` is the service
    clock at the last *applied* observation -- not on event time. HOT entries
    are never evicted: that would orphan a HOT record in the trie (spec
    section 26, ADR-0011 decision 3).
    """
    evicted = 0
    for ip, entry in store.entries():
        if entry.state is IpState.COLD and now - entry.last_observed >= state_retention_seconds:
            store.remove(ip)
            evicted += 1
    return evicted
