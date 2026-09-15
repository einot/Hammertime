"""Durable event log abstraction.

Spec: section 19 (event boundary), section 32 (replayability), section 33.

The trie is derived state. Everything that reconstructs it flows through this
package, so the log interface deliberately exposes offsets/sequences rather than
hiding them.
"""

from hammertime.bus.interface import ConsumedMessage, Consumer, Producer
from hammertime.bus.kafka import KafkaConsumer, KafkaProducer
from hammertime.bus.memory import InMemoryBus, MemoryConsumer, MemoryProducer
from hammertime.bus.topics import TOPICS, TopicSpec, all_topics

__all__ = [
    "TOPICS",
    "ConsumedMessage",
    "Consumer",
    "InMemoryBus",
    "KafkaConsumer",
    "KafkaProducer",
    "MemoryConsumer",
    "MemoryProducer",
    "Producer",
    "TopicSpec",
    "all_topics",
]
