"""EventEnvelope identity (spec section 4, section 23; ADR-0003 amendment).

`envelope.py` is currently a docstring-only stub (`Message identity and
provenance: agent_id, sequence, event_id, config_version.`), so this file
also functions as the executable specification for `EventEnvelope`'s public
surface, not just a check against an existing implementation. The assumed
shape, per issue #21's own wording (relayed by the architect): `EventEnvelope[T]`
carries `agent_id` / `sequence` / `event_id` / `config_version` / `timestamp`
plus the payload itself, nested (not flattened) -- i.e.:

    EventEnvelope(
        agent_id: str,
        sequence: int,
        event_type: str,
        config_version: int,
        timestamp: datetime,
        payload: RequestObservation | HotIpAdded | HotIpRemoved | PrefixStatsChanged,
    )

with a deterministic `event_id` derived *only* from
`(agent_id, sequence, event_type)` (ADR-0003 amendment: `config_version`,
`timestamp`, and `payload` are explicitly NOT part of that derivation,
since sequence numbering -- and therefore identity -- is "independent per
producer *and* per event type").

`event_type` is treated as an opaque string at the envelope layer (no
validation against a known set, and no cross-check against the actual type
of `payload`) -- ADR-0003's amendment says the codec is what treats
`event_type` as "the discriminator already carried by the codec's
schema-version-tagged wire format", implying `codec.py`, not `envelope.py`,
is responsible for rejecting unknown event types or shaping the payload
against schemas/*.json. See test_codec.py for that behavior.
"""

from __future__ import annotations

from datetime import datetime, timezone

from hammertime.core.addressing.address import Address
from hammertime.core.events.envelope import EventEnvelope
from hammertime.core.events.models import HotIpAdded

T0 = datetime(2026, 9, 14, 10, 5, 0, tzinfo=timezone.utc)
T1 = datetime(2026, 9, 14, 10, 6, 0, tzinfo=timezone.utc)

# A single reusable payload: identity tests only care about
# (agent_id, sequence, event_type), so the envelope-level `event_type`
# string need not actually match this payload's real type -- the envelope
# is a dumb identity carrier per the module docstring above.
_PAYLOAD = HotIpAdded(ip=Address.parse("192.168.1.42"), timestamp=T0, sequence=1, window_count=1000, config_version=1)


def _envelope(*, agent_id: str, sequence: int, event_type: str, config_version: int = 1, timestamp: datetime = T0, payload: object = _PAYLOAD) -> EventEnvelope:
    return EventEnvelope(
        agent_id=agent_id,
        sequence=sequence,
        event_type=event_type,
        config_version=config_version,
        timestamp=timestamp,
        payload=payload,
    )


class TestEventIdDeterminism:
    def test_same_identity_tuple_yields_the_same_event_id(self) -> None:
        e1 = _envelope(agent_id="edge-17", sequence=42, event_type="HotIpAdded")
        e2 = _envelope(agent_id="edge-17", sequence=42, event_type="HotIpAdded")
        assert e1.event_id == e2.event_id

    def test_event_id_is_a_non_empty_string(self) -> None:
        envelope = _envelope(agent_id="edge-17", sequence=42, event_type="HotIpAdded")
        assert isinstance(envelope.event_id, str)
        assert envelope.event_id

    def test_event_id_is_stable_across_repeated_access(self) -> None:
        envelope = _envelope(agent_id="edge-17", sequence=42, event_type="HotIpAdded")
        assert envelope.event_id == envelope.event_id

    def test_config_version_does_not_affect_event_id(self) -> None:
        # ADR-0003 amendment: event_id is derived from (agent_id, sequence,
        # event_type) only.
        e1 = _envelope(agent_id="edge-17", sequence=42, event_type="HotIpAdded", config_version=1)
        e2 = _envelope(agent_id="edge-17", sequence=42, event_type="HotIpAdded", config_version=99)
        assert e1.event_id == e2.event_id

    def test_timestamp_does_not_affect_event_id(self) -> None:
        e1 = _envelope(agent_id="edge-17", sequence=42, event_type="HotIpAdded", timestamp=T0)
        e2 = _envelope(agent_id="edge-17", sequence=42, event_type="HotIpAdded", timestamp=T1)
        assert e1.event_id == e2.event_id

    def test_payload_contents_do_not_affect_event_id(self) -> None:
        other_payload = HotIpAdded(ip=Address.parse("10.0.0.99"), timestamp=T1, sequence=999, window_count=1, config_version=7)
        e1 = _envelope(agent_id="edge-17", sequence=42, event_type="HotIpAdded", payload=_PAYLOAD)
        e2 = _envelope(agent_id="edge-17", sequence=42, event_type="HotIpAdded", payload=other_payload)
        assert e1.event_id == e2.event_id


class TestEventIdDistinctness:
    def test_different_sequence_yields_a_different_event_id(self) -> None:
        e1 = _envelope(agent_id="edge-17", sequence=1, event_type="HotIpAdded")
        e2 = _envelope(agent_id="edge-17", sequence=2, event_type="HotIpAdded")
        assert e1.event_id != e2.event_id

    def test_different_agent_id_yields_a_different_event_id(self) -> None:
        e1 = _envelope(agent_id="edge-17", sequence=1, event_type="HotIpAdded")
        e2 = _envelope(agent_id="edge-18", sequence=1, event_type="HotIpAdded")
        assert e1.event_id != e2.event_id

    def test_same_agent_and_sequence_but_different_event_type_yields_a_different_event_id(self) -> None:
        # This is the exact scenario called out by the ADR-0003 amendment:
        # an aggregator shard's HotIpAdded sequence counter and a trie
        # service's PrefixStatsChanged sequence counter are unrelated, so a
        # coincidental (agent_id, sequence) collision across event types
        # must not collide on event_id.
        e1 = _envelope(agent_id="shard-3", sequence=42, event_type="HotIpAdded")
        e2 = _envelope(agent_id="shard-3", sequence=42, event_type="PrefixStatsChanged")
        assert e1.event_id != e2.event_id

    def test_all_four_payload_event_types_pairwise_distinct_for_shared_identity(self) -> None:
        event_types = ["RequestObservation", "HotIpAdded", "HotIpRemoved", "PrefixStatsChanged"]
        ids = [
            _envelope(agent_id="shard-3", sequence=7, event_type=et).event_id
            for et in event_types
        ]
        assert len(ids) == len(set(ids))
