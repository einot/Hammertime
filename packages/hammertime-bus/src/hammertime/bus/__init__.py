"""Durable event log abstraction.

Spec: section 19 (event boundary), section 32 (replayability), section 33.

The trie is derived state. Everything that reconstructs it flows through this
package, so the log interface deliberately exposes offsets/sequences rather than
hiding them -- including `MessageBus.end_offset(topic)`, the offset the next
appended message will receive, which readiness reads at `start()` (ADR-0013
decision 9). Two implementations: `InMemoryBus` for tests and `NatsBus` over
NATS JetStream for the reference deployment (ADR-0013).
"""

from hammertime.bus.interface import (
    AssignmentListener,
    ConsumedMessage,
    Consumer,
    MessageBus,
    Producer,
)
from hammertime.bus.memory import InMemoryBus, MemoryConsumer, MemoryProducer
from hammertime.bus.nats import (
    TRANSIENT_ERRORS,
    NatsBus,
    NatsConsumer,
    NatsProducer,
    StreamConfigConflictError,
    StreamNotProvisionedError,
    bus_endpoints,
    validate_bus_url,
)
from hammertime.bus.topics import TOPICS, TopicSpec, all_topics, partition_for

__all__ = [
    "TOPICS",
    "TRANSIENT_ERRORS",
    "AssignmentListener",
    "ConsumedMessage",
    "Consumer",
    "InMemoryBus",
    "MemoryConsumer",
    "MemoryProducer",
    "MessageBus",
    "NatsBus",
    "NatsConsumer",
    "NatsProducer",
    "Producer",
    "StreamConfigConflictError",
    "StreamNotProvisionedError",
    "TopicSpec",
    "all_topics",
    "bus_endpoints",
    "partition_for",
    "validate_bus_url",
]
