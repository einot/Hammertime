"""Controlled re-evaluation after a configuration version change.

Spec: section 34

Lowering hot_threshold mass-transitions COLD -> HOT. The job walks live IP
state, re-runs evaluate_ip_state under the new config, and emits the resulting
transitions, tagged with the new config_version, instead of leaving stale state
behind (spec section 34).

ADR-0011 decision 7 fixes the order: re-bucket first (so every count is
re-placed under the new geometry before anything is judged), then walk every
entry in first-seen order under the new thresholds, then flush once. There is
deliberately no rate bound on the output -- the bus is the buffer, and a
throttle would only delay the trie's view of a change an operator made on
purpose.
"""

from dataclasses import dataclass

from hammertime.aggregator.transitions import TransitionPublisher, build_event, decide
from hammertime.aggregator.window.store import InMemoryWindowStore
from hammertime.core.config.models import DetectionConfig


@dataclass(frozen=True, slots=True)
class ReevaluationReport:
    """What one `reevaluate` pass did, for logging and for `apply_config`."""

    previous_version: int
    config_version: int
    #: Whether the geometry changed and every counter was rebuilt.
    rebucketed: bool
    #: Entries visited by the threshold walk; 0 when only the descriptive
    #: fields changed and no entry needed re-judging (spec section 34).
    evaluated: int
    became_hot: int
    became_cold: int


def _requires_walk(previous: DetectionConfig, current: DetectionConfig) -> bool:
    """Whether any field that can move an IP's state changed.

    Spec section 34: `weight_function`/`weight_max` alter only the weight
    recorded on subsequent transitions, the prefix-predicate fields belong to
    the trie and detector, and `allowed_lateness_seconds`/
    `state_retention_seconds` govern subsequent decisions only -- none of them
    can change the state of an entry that already exists.
    """

    return (
        previous.hot_threshold != current.hot_threshold
        or previous.cold_threshold != current.cold_threshold
        or previous.window_seconds != current.window_seconds
        or previous.bucket_seconds != current.bucket_seconds
    )


async def reevaluate(
    store: InMemoryWindowStore,
    *,
    previous: DetectionConfig,
    current: DetectionConfig,
    now: int,
    publisher: TransitionPublisher,
) -> ReevaluationReport:
    """Re-judge every tracked IP under `current` and publish what moved.

    `current.config_version` must be strictly greater than
    `previous.config_version` -- ADR-0009 decision 6's rule, enforced by
    `AggregatorWorker.apply_config`, which returns `None` rather than calling
    this for an equal-or-lower version. Reaching here with a version that does
    not advance is a programming error, so it raises `ValueError`.
    """

    if current.config_version <= previous.config_version:
        raise ValueError(
            f"re-evaluation needs a strictly greater config_version, got "
            f"{current.config_version} after {previous.config_version}"
        )

    rebucketed = (
        previous.window_seconds != current.window_seconds
        or previous.bucket_seconds != current.bucket_seconds
    )
    if rebucketed:
        # Counts are re-placed, never invented, scaled or reset (ADR-0011
        # decision 7 step 2), and the store adopts the new geometry so every
        # entry created afterwards gets a counter of the new shape.
        store.rebucket(
            window_seconds=current.window_seconds,
            bucket_seconds=current.bucket_seconds,
            now=now,
        )

    evaluated = 0
    became_hot = 0
    became_cold = 0
    if _requires_walk(previous, current):
        for ip, entry in store.entries():
            evaluated += 1
            window_count = entry.counter.total
            transition = decide(entry.state, window_count, current)
            if transition is None:
                continue
            entry.state = transition.current
            await publisher.publish(
                build_event(ip, transition, window_count=window_count, config=current, now=now)
            )
            if transition.became_hot:
                became_hot += 1
            else:
                became_cold += 1

    await publisher.flush()
    return ReevaluationReport(
        previous_version=previous.config_version,
        config_version=current.config_version,
        rebucketed=rebucketed,
        evaluated=evaluated,
        became_hot=became_hot,
        became_cold=became_cold,
    )
