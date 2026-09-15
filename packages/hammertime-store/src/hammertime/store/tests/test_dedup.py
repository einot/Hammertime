"""`SequenceWindow`/`SequenceKey`: per-agent out-of-order sequence tracking.

Spec: section 23 (agent duplicates and retries), section 26 (memory
considerations). This module is deliberately clock-free (see `dedup.py`'s
module docstring), so these tests exercise `SequenceWindow` directly rather
than through `MemoryDedupStore` -- in particular the bounded out-of-order
set's eviction path (`DEFAULT_MAX_OUT_OF_ORDER`), which `test_memory_dedup.py`
never drives more than a handful of entries into.
"""

from hammertime.store.dedup import SequenceKey, SequenceWindow


class TestSequenceKey:
    def test_cache_key_is_stable_for_the_same_agent_and_sequence(self) -> None:
        assert SequenceKey("agent-1", 42).cache_key() == SequenceKey("agent-1", 42).cache_key()

    def test_cache_key_does_not_collide_across_the_agent_sequence_boundary(self) -> None:
        # Plain concatenation would collide ("a", 1) with ("a1", <empty>) or
        # similar; the \x1f separator must prevent that.
        assert SequenceKey("a", 1).cache_key() != SequenceKey("a1", 1).cache_key()


class TestContiguousDelivery:
    def test_fresh_window_contains_nothing(self) -> None:
        window = SequenceWindow()

        assert window.contains(0) is False

    def test_adding_the_first_sequence_marks_it_seen(self) -> None:
        window = SequenceWindow()

        window.add(0)

        assert window.contains(0) is True
        assert window.contains(1) is False

    def test_adding_sequences_in_order_extends_the_contiguous_run(self) -> None:
        window = SequenceWindow()

        window.add(0)
        window.add(1)
        window.add(2)

        assert window.contains(0) is True
        assert window.contains(1) is True
        assert window.contains(2) is True
        assert window.contains(3) is False

    def test_re_adding_an_already_seen_sequence_is_a_no_op(self) -> None:
        window = SequenceWindow()

        window.add(0)
        window.add(0)

        assert window.contains(0) is True


class TestOutOfOrderDelivery:
    def test_an_agents_first_ever_sequence_arriving_out_of_order_does_not_backfill_lower_sequences(
        self,
    ) -> None:
        # Regression: a prior version special-cased "first sequence ever
        # added" to set high_water_mark directly, which meant an agent's
        # first observed message being out of order (e.g. sequence 10 right
        # after an ingest restart with a fresh store) would silently treat
        # sequences 0-9 as already seen.
        window = SequenceWindow()

        window.add(10)

        assert window.contains(10) is True
        assert window.contains(7) is False
        assert window.contains(0) is False

    def test_a_gap_is_not_treated_as_seen_until_it_actually_fills(self) -> None:
        window = SequenceWindow()

        window.add(5)

        assert window.contains(5) is True
        assert window.contains(3) is False

    def test_filling_a_gap_folds_the_out_of_order_entries_into_the_contiguous_run(self) -> None:
        window = SequenceWindow()

        window.add(2)
        window.add(1)
        window.add(0)

        assert window.contains(0) is True
        assert window.contains(1) is True
        assert window.contains(2) is True
        assert window.out_of_order_count == 0

    def test_a_partially_filled_gap_leaves_the_remaining_entries_out_of_order(self) -> None:
        window = SequenceWindow()

        window.add(3)
        window.add(1)
        # sequence 2 never arrives -- 0 is still missing too, so neither
        # 1 nor 3 can fold into the contiguous run yet.
        window.add(0)

        assert window.contains(0) is True
        assert window.contains(1) is True
        assert window.contains(2) is False
        assert window.contains(3) is True
        assert window.out_of_order_count == 1


class TestBoundedOutOfOrderEviction:
    """`_bound_out_of_order_set` via a small `max_out_of_order` for a fast test."""

    def test_set_does_not_evict_below_the_bound(self) -> None:
        window = SequenceWindow(max_out_of_order=3)

        for sequence in (10, 20, 30):
            window.add(sequence)

        assert window.out_of_order_count == 3
        assert window.contains(10) is True
        assert window.contains(20) is True
        assert window.contains(30) is True

    def test_exceeding_the_bound_drops_only_the_single_oldest_entry(self) -> None:
        window = SequenceWindow(max_out_of_order=3)

        for sequence in (10, 20, 30, 40):
            window.add(sequence)

        # The oldest (smallest) pending sequence is evicted to make room --
        # not folded into the mark, not treated as seen.
        assert window.out_of_order_count == 3
        assert window.contains(10) is False
        assert window.contains(20) is True
        assert window.contains(30) is True
        assert window.contains(40) is True

    def test_eviction_does_not_falsely_mark_never_seen_sequences_as_seen(self) -> None:
        # This is the specific failure a fast-forwarding high_water_mark
        # would cause: every sequence below the new mark, not just the
        # abandoned one, would wrongly report as a duplicate.
        window = SequenceWindow(max_out_of_order=3)

        for sequence in (1000, 2000, 3000, 4000):
            window.add(sequence)

        # Never-observed sequences between and below the tracked ones must
        # still report as not seen.
        assert window.contains(500) is False
        assert window.contains(1500) is False
        assert window.contains(2500) is False

    def test_eviction_leaves_high_water_mark_untouched(self) -> None:
        window = SequenceWindow(max_out_of_order=3)

        for sequence in (10, 20, 30, 40):
            window.add(sequence)

        assert window.high_water_mark == -1

    def test_retrying_an_evicted_sequence_is_not_falsely_reported_as_a_duplicate(self) -> None:
        # The narrow, documented cost of eviction: a dropped sequence is no
        # longer tracked, so a later add() for it is accepted as new rather
        # than rejected as a duplicate -- even though, since eviction always
        # drops the current global minimum, an immediate retry while the set
        # is still full of larger entries is itself evicted again right
        # away (it's still the minimum). That repeat eviction is the
        # documented trade-off, not a bug: the guarantee this module must
        # keep is "never a false duplicate", not "every out-of-order
        # sequence is remembered forever" -- the latter is exactly what
        # DEFAULT_MAX_OUT_OF_ORDER exists to bound.
        window = SequenceWindow(max_out_of_order=3)

        for sequence in (10, 20, 30, 40):
            window.add(sequence)
        assert window.contains(10) is False

        was_flagged_as_duplicate = window.contains(10)
        window.add(10)

        assert was_flagged_as_duplicate is False

    def test_an_evicted_sequence_sticks_once_re_added_with_room_to_spare(self) -> None:
        window = SequenceWindow(max_out_of_order=3)

        for sequence in (10, 20, 30, 40):
            window.add(sequence)
        assert window.contains(10) is False
        assert window.out_of_order_count == 3

        # Vacate a slot unrelated to 10's own position, then retry: with
        # room in the set, the retry is no longer immediately re-evicted.
        window._ahead.discard(40)
        window.add(10)

        assert window.contains(10) is True
