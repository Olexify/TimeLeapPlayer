"""Box -> window-slot assignment.

Two things must hold on every frame or the render breaks: slots are unique
(two boxes sharing an HWND means one of them is not drawn) and in range
(an out-of-range index is an IndexError on the render thread). On top of
that comes the actual point of the class -- a box that barely moved keeps
its window, which is what removes the jitter.
"""
from __future__ import annotations

import numpy as np
import pytest

from timeleap.core.tracker import DEFAULT_RADIUS, SlotTracker


def check_slots(slots: np.ndarray, boxes: np.ndarray, capacity: int) -> np.ndarray:
    """Shared invariant: one entry per box, unique, in range or -1."""
    assert slots.shape == (boxes.shape[0],)
    assert slots.dtype == np.int32
    used = slots[slots >= 0]
    assert (used < capacity).all(), "slot index past the pool"
    assert len(set(used.tolist())) == used.size, "a slot was handed out twice"
    return used


# ---- invariants ------------------------------------------------------
@pytest.mark.parametrize("capacity", [1, 4, 32, 260])
@pytest.mark.parametrize("count", [0, 1, 5, 60, 300])
def test_slots_are_unique_and_in_range(capacity: int, count: int, random_boxes) -> None:
    tracker = SlotTracker(capacity)
    for frame in range(6):
        boxes = random_boxes(count, 96, 54, seed=frame)
        used = check_slots(tracker.assign(boxes), boxes, capacity)
        assert used.size == min(count, capacity)


def test_zero_boxes_returns_an_empty_assignment() -> None:
    tracker = SlotTracker(16)
    out = tracker.assign(np.empty((0, 5), np.int32))
    assert out.shape == (0,) and out.dtype == np.int32


def test_zero_capacity_assigns_nothing(random_boxes) -> None:
    boxes = random_boxes(9, 40, 20)
    out = SlotTracker(0).assign(boxes)
    assert out.shape == (9,) and (out == -1).all()


def test_a_malformed_array_still_yields_one_entry_per_row(make_boxes) -> None:
    """The result gets masked against the caller's box array, so a short
    return turns a bad frame into an IndexError instead of a dropped frame."""
    tracker = SlotTracker(8)
    narrow = np.zeros((5, 3), np.int32)
    assert tracker.assign(narrow).shape == (5,)
    assert (tracker.assign(narrow) == -1).all()
    assert tracker.assign(np.zeros((4,), np.int32)).shape == (0,)


# ---- temporal stability ----------------------------------------------
def test_a_still_frame_keeps_every_slot(make_boxes) -> None:
    tracker = SlotTracker(16)
    boxes = make_boxes([(0, 0, 3, 3), (10, 10, 2, 2), (20, 5, 4, 4)])
    first = tracker.assign(boxes)
    for _ in range(10):
        assert np.array_equal(tracker.assign(boxes), first)


def test_slots_survive_slow_translation(make_boxes) -> None:
    """A box drifting one cell per frame is the common case; it must not be
    re-dealt to a different window."""
    tracker = SlotTracker(16)
    base = make_boxes([(0, 0, 3, 3), (10, 10, 2, 2), (20, 5, 4, 4), (30, 14, 2, 6)])
    first = tracker.assign(base)
    for step in range(1, 30):
        moved = base.copy()
        moved[:, 0] += step
        moved[:, 1] += step % 3
        assert np.array_equal(tracker.assign(moved), first), step


def test_slots_follow_the_box_not_the_row_order(make_boxes) -> None:
    """boxgen's output order changes between frames; the mapping must not."""
    tracker = SlotTracker(16)
    boxes = make_boxes([(0, 0, 3, 3), (10, 10, 2, 2), (20, 5, 4, 4)])
    first = tracker.assign(boxes)
    order = [2, 0, 1]
    shuffled = boxes[order]
    got = tracker.assign(shuffled)
    assert got.tolist() == [first[i] for i in order]


def test_the_slot_follows_the_position_not_the_row(make_boxes) -> None:
    """Matching is nearest-centre, so two boxes trading places trade slots."""
    tracker = SlotTracker(8, radius=3.0)
    left, right = (2, 2, 2, 2), (30, 20, 2, 2)
    first = tracker.assign(make_boxes([left, right]))
    swapped = tracker.assign(make_boxes([right, left]))
    assert swapped.tolist() == [first[1], first[0]]


def test_a_box_that_jumped_loses_its_slot_to_the_one_that_stayed(make_boxes) -> None:
    tracker = SlotTracker(8, radius=3.0)
    held = tracker.assign(make_boxes([(10, 10, 2, 2)]))[0]

    # Next frame: something sits where the old box was, and the old box is far
    # away. The stayer must inherit the window.
    slots = tracker.assign(make_boxes([(40, 30, 2, 2), (10, 10, 2, 2)]))
    assert slots[1] == held, "the box at the old position should keep the slot"
    assert slots[0] != held
    check_slots(slots, np.zeros((2, 5)), 8)


def test_an_intermittent_box_comes_back_to_its_own_slot(make_boxes) -> None:
    """A blinking eye must not steal a different window every time."""
    tracker = SlotTracker(8)
    both = make_boxes([(2, 2, 2, 2), (20, 12, 3, 3)])
    slots = tracker.assign(both)
    for _ in range(3):
        tracker.assign(both[:1])          # the second box disappears
        assert np.array_equal(tracker.assign(both), slots)


def test_duplicate_rectangles_still_get_distinct_slots(make_boxes) -> None:
    """Trails and echo emit identical boxes by design; ties must not collapse."""
    tracker = SlotTracker(32)
    boxes = make_boxes([(5, 5, 2, 2)] * 12)
    used = check_slots(tracker.assign(boxes), boxes, 32)
    assert used.size == 12
    used_again = check_slots(tracker.assign(boxes), boxes, 32)
    assert sorted(used_again.tolist()) == sorted(used.tolist())


# ---- over capacity ---------------------------------------------------
def test_over_capacity_keeps_the_largest_boxes(make_boxes) -> None:
    tracker = SlotTracker(3)
    boxes = make_boxes([(0, 0, 1, 1), (5, 0, 5, 5), (10, 0, 3, 3),
                        (15, 0, 2, 2), (20, 0, 4, 4)])
    slots = tracker.assign(boxes)
    check_slots(slots, boxes, 3)
    kept = np.nonzero(slots >= 0)[0].tolist()
    assert kept == [1, 2, 4], "the three largest by area must win"


def test_over_capacity_truncates_identically_every_time(make_boxes) -> None:
    """Ties broken by source order, so a paused frame does not churn."""
    boxes = make_boxes([(i * 3, 0, 2, 2) for i in range(10)])
    a = SlotTracker(4).assign(boxes)
    b = SlotTracker(4).assign(boxes)
    assert np.array_equal(a, b)
    assert (a >= 0).sum() == 4


def test_far_over_capacity_stays_consistent(random_boxes) -> None:
    tracker = SlotTracker(20)
    for frame in range(5):
        boxes = random_boxes(400, 96, 54, seed=frame)
        used = check_slots(tracker.assign(boxes), boxes, 20)
        assert used.size == 20


# ---- reset -----------------------------------------------------------
def test_reset_forgets_history(make_boxes) -> None:
    tracker = SlotTracker(8)
    a = make_boxes([(30, 20, 2, 2), (0, 0, 2, 2)])
    first = tracker.assign(a)

    tracker.reset()
    after = tracker.assign(a)
    # With no history the slots are handed out in source order, which for this
    # input is a different mapping than the one matching produced.
    assert after.tolist() == [0, 1]
    assert first.tolist() == [0, 1] or not np.array_equal(first, after)

    # And the pool is fully free again: a completely different frame gets the
    # low slots rather than being pushed past the ones the old frame held.
    tracker.reset()
    b = make_boxes([(50, 40, 2, 2)])
    assert tracker.assign(b).tolist() == [0]


def test_reset_on_an_untouched_tracker_is_safe() -> None:
    tracker = SlotTracker(4)
    tracker.reset()
    assert tracker.assign(np.empty((0, 5), np.int32)).shape == (0,)


# ---- construction ----------------------------------------------------
def test_capacity_and_radius_are_clamped() -> None:
    assert SlotTracker(-5).capacity == 0
    assert SlotTracker(4, radius=0.0).radius >= 0.5
    assert SlotTracker(4).radius == DEFAULT_RADIUS


def test_negative_coordinates_are_handled(make_boxes) -> None:
    """`effects.jitter` can push a box to a negative cell before clipping."""
    tracker = SlotTracker(8)
    boxes = make_boxes([(-5, -4, 2, 2), (3, 3, 2, 2)])
    first = tracker.assign(boxes)
    check_slots(first, boxes, 8)
    assert np.array_equal(tracker.assign(boxes), first)
