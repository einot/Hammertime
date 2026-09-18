"""Controlled re-evaluation after a configuration version change.

Spec: section 34

ADR-0011 decision 7 step 2. Lowering `hot_threshold` mass-transitions
COLD -> HOT; raising it does the reverse. The pass walks the shard's tracked
IPs, re-runs the state machine under the new configuration through the one
emitter (section 30), and emits the resulting transitions tagged with the new
`config_version` and a `weight` computed under it, instead of leaving stale
state behind.

It is not rate-limited: the log between aggregator and trie is the
back-pressure (ADR-0011 assumption 17). It only yields to the event loop
every `batch_size` IPs so the admin endpoints stay responsive while a large
shard is re-evaluated.
"""

import asyncio
from collections.abc import Iterable

from hammertime.aggregator.transitions import TransitionEmitter
from hammertime.aggregator.window.store import ShardWindow
from hammertime.core.addressing.address import Address
from hammertime.core.config.models import DetectionConfig

#: HAMMERTIME_AGGREGATOR_REEVALUATION_BATCH's default (decision 9).
DEFAULT_BATCH_SIZE = 1000


async def reevaluate_shard(
    window: ShardWindow,
    emitter: TransitionEmitter,
    *,
    config: DetectionConfig,
    ips: Iterable[Address] | None = None,
    batch_size: int = DEFAULT_BATCH_SIZE,
) -> int:
    """Evaluate every IP of `window` under `config`; return how many transitioned.

    `ips` defaults to a snapshot of `window.tracked_ips()` -- a snapshot
    because `emitter.evaluate` writes back into the window it is walking. The
    warm-up exemption of decision 5 still applies, inside the emitter.

    The return value is the `transitions` field of the worker's
    `config_reevaluated` record (decision 8, renamed by Amendment 3 item
    A12).
    """
    if batch_size <= 0:
        raise ValueError(f"batch_size must be positive, got {batch_size}")
    targets = tuple(window.tracked_ips()) if ips is None else tuple(ips)
    transitions = 0
    for position, ip in enumerate(targets, start=1):
        if await emitter.evaluate(window, ip, config=config, reason="config") is not None:
            transitions += 1
        if position % batch_size == 0:
            await asyncio.sleep(0)
    return transitions
