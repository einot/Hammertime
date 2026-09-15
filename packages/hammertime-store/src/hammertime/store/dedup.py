"""Per-agent sequence high-water mark plus a bounded out-of-order set.

Spec: section 23

ADR-0003 describes the dedup window as `agent_id -> seen sequence set /
high-water mark`. A high-water mark alone is not sufficient: agents may
deliver sequences out of order within the allowed-lateness horizon (spec
section 24), so a smaller sequence arriving after a larger one is not
necessarily a duplicate. `SequenceWindow` combines the two: a high-water
mark for the contiguous run of sequences confirmed seen, plus a small set of
sequences seen *ahead* of an unfilled gap. Once the gap closes, the
contiguous run folds into the mark and drops out of the set -- so, in the
common case of in-order delivery, the set never holds more than a handful of
entries at a time, regardless of how many sequences a long-lived agent has
sent overall.

This module is deliberately clock-free (contrast `memory.py`, which owns a
`Clock` and the time-based retention window from ADR-0003): `SequenceWindow`
only knows about sequence order, not wall-clock time, so it is trivial to
unit test without any time mocking. Time-based retention (evicting a whole
agent's window after `allowed_lateness_seconds + window_seconds` of
inactivity) is `memory.py`'s job, layered on top of this structure.
"""

from dataclasses import dataclass, field

#: Safety bound on the out-of-order set for a single agent. If a gap in an
#: agent's sequence numbers never closes (a permanently skipped sequence,
#: rather than a merely-delayed one), sequences seen ahead of that gap would
#: otherwise accumulate forever. Once the set exceeds this size, the window
#: drops its single oldest tracked sequence to make room, trading dedup
#: coverage for *that one sequence* for a hard memory bound (spec section
#: 26). Deliberately NOT "fast-forward the high-water mark past the whole
#: abandoned gap": doing so would make every sequence below the new mark
#: report as "seen", including the (likely enormous) range of sequences the
#: agent never actually sent -- silently rejecting the agent's own later,
#: genuinely-first-time observations in that range as false duplicates. A
#: dropped single sequence can, in the worst case, be double-counted if its
#: original message is retried after eviction; that is a much narrower and
#: less harmful failure than manufacturing false duplicates across an
#: entire unobserved range.
DEFAULT_MAX_OUT_OF_ORDER = 10_000


@dataclass(frozen=True, slots=True)
class SequenceKey:
    """The dedup identity of one agent-originated observation.

    `(agent_id, sequence)` per ADR-0003 -- distinct from
    `hammertime.core.events.envelope.EventEnvelope.event_id`, which is
    derived from `(agent_id, sequence, event_type)` for a different purpose
    (idempotent redelivery of internally-produced events across the bus,
    ADR-0003's amendment). This store only ever sees agent-originated
    `RequestObservation` messages, which carry no `event_type`.
    """

    agent_id: str
    sequence: int

    def cache_key(self) -> str:
        """A stable string form, e.g. for use as a Redis key (issue #31)."""
        # \x1f is not a legal agent_id character, so this can't collide
        # ("a", 1) with ("a1", ...) the way plain concatenation could.
        return f"{self.agent_id}\x1f{self.sequence}"


@dataclass(slots=True)
class SequenceWindow:
    """Tracks which sequence numbers have been seen for a single agent.

    `high_water_mark` is the highest sequence for which every sequence
    `<= high_water_mark` is considered part of the confirmed contiguous run
    from 0 (spec section 23: sequences start at 0, per
    `schemas/observation.v1.json`'s `minimum: 0`). It starts at `-1` --
    "nothing confirmed yet" -- rather than being unset, so that the very
    first sequence an agent sends is handled exactly like any other
    sequence: it only advances the mark directly if it *is* 0 (the
    contiguous run's first member); any other value, including an agent's
    first-ever message arriving as e.g. sequence 10, goes into the
    out-of-order set like a normal gap, per this class's own out-of-order
    design, rather than incorrectly establishing sequences 0-9 as already
    seen. (A prior version of this class special-cased "first sequence
    ever added" to set the mark directly, which had exactly that bug: an
    agent's first observed message being out of order -- plausible after
    an ingest restart with a fresh in-memory store -- would silently treat
    every lower sequence as a duplicate it had never actually seen.)

    `contains()`/`add()` treat the mark as permanent: an agent's sequence
    numbers are expected to be monotonic (spec section 23), so once the
    mark passes a sequence it is not expected to be legitimately reused.
    (An agent restarting with a lower starting sequence than one it used
    before is out of scope here -- the mark would treat the lower,
    genuinely-new sequence as a duplicate. Nothing in the spec addresses
    agent sequence resets.)
    """

    high_water_mark: int = -1
    max_out_of_order: int = DEFAULT_MAX_OUT_OF_ORDER
    _ahead: set[int] = field(default_factory=set)

    def contains(self, sequence: int) -> bool:
        """Whether `sequence` has already been recorded via `add()`."""
        if sequence <= self.high_water_mark:
            return True
        return sequence in self._ahead

    def add(self, sequence: int) -> None:
        """Record `sequence` as seen. A no-op if already seen."""
        if self.contains(sequence):
            return
        if sequence == self.high_water_mark + 1:
            self.high_water_mark = sequence
            self._fold_contiguous_run()
        else:
            self._ahead.add(sequence)
            self._bound_out_of_order_set()

    def _fold_contiguous_run(self) -> None:
        """Advance the mark through any already-seen sequences right after it."""
        next_sequence = self.high_water_mark + 1
        while next_sequence in self._ahead:
            self._ahead.discard(next_sequence)
            self.high_water_mark = next_sequence
            next_sequence += 1

    def _bound_out_of_order_set(self) -> None:
        # Drop the single oldest tracked sequence rather than advancing
        # high_water_mark: see DEFAULT_MAX_OUT_OF_ORDER's docstring for why
        # fast-forwarding the mark across the whole abandoned gap would be
        # far worse (it would falsely mark every never-seen sequence below
        # the new mark as a duplicate, not just the abandoned one).
        if len(self._ahead) > self.max_out_of_order:
            self._ahead.discard(min(self._ahead))

    @property
    def out_of_order_count(self) -> int:
        """Number of sequences currently held ahead of an unfilled gap."""
        return len(self._ahead)
