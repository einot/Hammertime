"""Publish validated observations, keyed by IP shard to preserve per-IP order.

Spec: section 19, section 20; ADR-0004 (per-IP observation fan-out)

`ObservationPublisher.publish` implements ADR-0004's decision exactly:

1. Coalesce `observation.observations` by `Address`, summing `request_count`
   per distinct IP and preserving first-appearance order (deltas are
   additive, ADR-0003). A coalesced total that overflows
   `schemas/observation.v1.json`'s `request_count` maximum raises
   `RequestCountOverflowError` for the whole request rather than truncating
   or partially publishing.
2. Build one single-entry `RequestObservation` `EventEnvelope` per coalesced
   entry, with `subject` set to that entry's IP text (so each split message
   gets its own distinct, deterministic `event_id`).
3. Publish each envelope to `hammertime.observations.v1`, keyed via
   `topic.key_selector(entry)` -- never a hard-coded `str(entry.ip)` at the
   call site, even though the two happen to agree.
4. Publish all N messages concurrently, then `flush()` once for the whole
   batch. Any failure propagates (never swallowed): `202` means every
   message is durably recorded, and a publish failure must map to `503`
   with nothing recorded (docs/protocol/observation-v1.md).
"""

import asyncio
from datetime import UTC, datetime

from hammertime.bus.interface import Producer
from hammertime.bus.topics import TOPICS
from hammertime.core.addressing.address import Address
from hammertime.core.events.codec import encode
from hammertime.core.events.envelope import EventEnvelope
from hammertime.core.events.models import Observation, RequestObservation
from hammertime.ingest.validation import IngestValidationError

#: schemas/observation.v1.json: observations[].request_count maximum. A
#: coalesced per-IP total (ADR-0004 §3) must not exceed this either.
_MAX_REQUEST_COUNT = 1_000_000_000

_OBSERVATIONS_TOPIC = TOPICS["hammertime.observations.v1"]


class RequestCountOverflowError(IngestValidationError):
    """A coalesced per-IP request_count total exceeds the schema's maximum.

    ADR-0004 §3: duplicate IPs within one batch are summed before
    publishing; a batch is never partially applied, so an overflowing sum
    rejects the whole request (mapped to 400 by `api/routes.py`) instead of
    being silently truncated.
    """


def _coalesce(observations: tuple[Observation, ...]) -> tuple[Observation, ...]:
    """Sum `request_count` per distinct `Address`, preserving first-appearance order."""
    totals: dict[Address, int] = {}
    for entry in observations:
        totals[entry.ip] = totals.get(entry.ip, 0) + entry.request_count

    coalesced: list[Observation] = []
    for ip, total in totals.items():
        if total > _MAX_REQUEST_COUNT:
            raise RequestCountOverflowError(
                f"coalesced request_count for {ip} is {total}, exceeding the "
                f"maximum of {_MAX_REQUEST_COUNT} (schemas/observation.v1.json)"
            )
        coalesced.append(Observation(ip=ip, request_count=total))
    return tuple(coalesced)


class ObservationPublisher:
    """Fans an accepted batch out into one bus message per distinct IP (ADR-0004)."""

    def __init__(self, producer: Producer, *, config_version: int) -> None:
        self._producer = producer
        self._config_version = config_version

    def coalesce(self, observation: RequestObservation) -> tuple[Observation, ...]:
        """Sum `request_count` per distinct `Address` in `observation.observations`.

        Raises `RequestCountOverflowError` if a coalesced total overflows the
        schema maximum. Pure and synchronous: callers (`api/routes.py`) use
        this to validate a batch *before* claiming its `(agent_id, sequence)`
        in the dedup store, so an overflowing batch is rejected as a 400
        without ever consuming the dedup claim a corrected retry would need.
        """
        return _coalesce(observation.observations)

    async def publish(self, observation: RequestObservation) -> None:
        """Envelope and publish `observation`'s coalesced entries, then flush once.

        Re-derives the same coalesced entries `coalesce()` above would (cheap
        and pure -- see its docstring for why callers validate with it
        first), then propagates whatever the underlying `Producer` raises on
        `publish`/`flush` failure. Never publishes a partial batch: all N
        per-IP publishes are awaited with `return_exceptions=True` so a
        sibling's success or failure is never left running in the background
        after this call returns, and `flush()` runs only once every sibling
        has settled.
        """
        coalesced = self.coalesce(observation)
        timestamp = datetime.now(UTC)

        results = await asyncio.gather(
            *(self._publish_one(observation, entry, timestamp) for entry in coalesced),
            return_exceptions=True,
        )
        for result in results:
            if isinstance(result, BaseException):
                raise result
        await self._producer.flush()

    async def _publish_one(
        self, observation: RequestObservation, entry: Observation, timestamp: datetime
    ) -> None:
        envelope: EventEnvelope[RequestObservation] = EventEnvelope(
            agent_id=observation.agent_id,
            sequence=observation.sequence,
            event_type="RequestObservation",
            config_version=self._config_version,
            timestamp=timestamp,
            payload=RequestObservation(
                agent_id=observation.agent_id,
                sequence=observation.sequence,
                window_start=observation.window_start,
                window_seconds=observation.window_seconds,
                observations=(entry,),
            ),
            subject=str(entry.ip),
        )
        await self._producer.publish(
            _OBSERVATIONS_TOPIC.name,
            _OBSERVATIONS_TOPIC.key_selector(entry),
            encode(envelope),
        )
