"""Temporal effects.

Two properties are non-negotiable and are asserted for every effect and
combination here: output stays inside the grid (the renderer does not clamp,
so a stray box is a window hanging off the montage) and output is a pure
function of the frame index (a per-call RNG would make a hundred top-most
windows flicker at random, which is a photosensitivity hazard, and would
make a paused frame strobe).
"""
from __future__ import annotations

import math

import numpy as np
import pytest

from timeleap.config import EffectConfig
from timeleap.core.boxgen import H, L, W, X, Y
from timeleap.core.effects import MAX_SPEED, MIN_SPEED, EffectStack

GRID_W, GRID_H = 40, 24

# One entry per effect, plus a couple of combinations, all with settings that
# actually do something at this grid size.
EFFECTS = {
    "trails": EffectConfig(trails=3),
    "echo": EffectConfig(echo_offset=4, ghost=0.6),
    "slitscan": EffectConfig(slitscan=5),
    "jitter": EffectConfig(jitter=3),
    "big_jitter": EffectConfig(jitter=25),
    "strobe": EffectConfig(strobe=3),
    "shuffle": EffectConfig(shuffle=8),
    "warp": EffectConfig(time_warp=0.5),
    "everything": EffectConfig(trails=2, echo_offset=3, ghost=0.5, slitscan=4,
                               jitter=2, strobe=7, shuffle=5, time_warp=0.4),
}


@pytest.fixture
def scene(random_boxes):
    """A live frame plus eight frames of history, oldest first."""
    live = random_boxes(50, GRID_W, GRID_H, levels=4, seed=100)
    history = [random_boxes(50, GRID_W, GRID_H, levels=4, seed=i) for i in range(8)]
    return live, history


def stack(cfg: EffectConfig, **kw) -> EffectStack:
    kw.setdefault("grid_w", GRID_W)
    kw.setdefault("grid_h", GRID_H)
    kw.setdefault("levels", 4)
    return EffectStack(cfg, **kw)


def assert_in_grid(boxes: np.ndarray, gw: int = GRID_W, gh: int = GRID_H) -> None:
    assert boxes.ndim == 2 and boxes.shape[1] == 5
    if boxes.shape[0] == 0:
        return
    assert (boxes[:, X] >= 0).all() and (boxes[:, Y] >= 0).all()
    assert (boxes[:, X] + boxes[:, W] <= gw).all()
    assert (boxes[:, Y] + boxes[:, H] <= gh).all()
    assert (boxes[:, W] > 0).all() and (boxes[:, H] > 0).all(), "empty rectangle"
    assert (boxes[:, L] >= 0).all()


# ---- off by default --------------------------------------------------
def test_nothing_is_on_by_default(scene) -> None:
    live, history = scene
    es = stack(EffectConfig())
    assert not es.cfg.active()
    out = es.apply(live, 7, history)
    assert out is live, "an inactive stack must not even copy the frame"


def test_speed_is_exactly_one_when_time_warp_is_off() -> None:
    es = stack(EffectConfig())
    for t in (0.0, 0.3, 7.5, 1e4):
        assert es.speed_scale(t) == 1.0


@pytest.mark.parametrize("name", sorted(EFFECTS))
def test_each_effect_changes_something(name: str, scene) -> None:
    """A knob that does nothing when turned is a bug we would never see."""
    live, history = scene
    cfg = EFFECTS[name]
    es = stack(cfg)
    assert cfg.active()
    if name == "warp":
        assert es.speed_scale(1.0) != 1.0
        return
    changed = any(
        not np.array_equal(es.apply(live, i, history), live) for i in range(8)
    )
    assert changed, f"{name} left every frame untouched"


# ---- bounds ----------------------------------------------------------
@pytest.mark.parametrize("name", sorted(EFFECTS))
@pytest.mark.parametrize("frame", [0, 1, 5, 999])
def test_output_stays_inside_the_grid(name: str, frame: int, scene) -> None:
    live, history = scene
    out = stack(EFFECTS[name]).apply(live, frame, history)
    assert_in_grid(out)


@pytest.mark.parametrize("name", sorted(EFFECTS))
def test_output_stays_in_bounds_with_no_history(name: str, scene) -> None:
    live, _ = scene
    for frame in range(4):
        assert_in_grid(stack(EFFECTS[name]).apply(live, frame, []))


@pytest.mark.parametrize("name", sorted(EFFECTS))
def test_output_stays_in_bounds_on_an_empty_frame(name: str, scene) -> None:
    _, history = scene
    empty = np.empty((0, 5), np.int32)
    for frame in range(4):
        assert_in_grid(stack(EFFECTS[name]).apply(empty, frame, history))


def test_bounds_hold_when_the_grid_was_only_inferred(scene) -> None:
    """Without `configure`, the extent is learned from the frames seen; the
    result must still not go negative or collapse to an empty rectangle."""
    live, history = scene
    es = EffectStack(EffectConfig(jitter=9))
    out = es.apply(live, 3, history)
    assert (out[:, [X, Y]] >= 0).all()
    assert (out[:, [W, H]] > 0).all()
    assert (out[:, X] + out[:, W]).max() <= GRID_W
    assert (out[:, Y] + out[:, H]).max() <= GRID_H


def test_jitter_actually_moves_boxes_but_not_out_of_the_grid(scene) -> None:
    live, _ = scene
    out = stack(EffectConfig(jitter=3)).apply(live, 12, [])
    assert not np.array_equal(out[:, :2], live[:out.shape[0], :2])
    assert_in_grid(out)


# ---- determinism -----------------------------------------------------
@pytest.mark.parametrize("name", sorted(EFFECTS))
def test_the_same_frame_index_gives_the_same_boxes(name: str, scene) -> None:
    live, history = scene
    cfg = EFFECTS[name]
    a = stack(cfg).apply(live, 900, history)
    b = stack(cfg).apply(live, 900, history)
    assert np.array_equal(a, b), "a seek back to 900 must repaint 900"


@pytest.mark.parametrize("name", sorted(EFFECTS))
def test_repainting_a_paused_frame_does_not_flicker(name: str, scene) -> None:
    live, history = scene
    es = stack(EFFECTS[name])
    first = es.apply(live, 42, history)
    for _ in range(5):
        assert np.array_equal(es.apply(live, 42, history), first)


def test_different_frames_differ_under_jitter(scene) -> None:
    live, _ = scene
    es = stack(EffectConfig(jitter=4))
    outs = [es.apply(live, i, []) for i in range(5)]
    assert any(not np.array_equal(outs[0], o) for o in outs[1:])


@pytest.mark.parametrize("name", sorted(EFFECTS))
def test_the_input_frame_is_never_mutated(name: str, scene) -> None:
    live, history = scene
    live_before = live.copy()
    history_before = [h.copy() for h in history]
    stack(EFFECTS[name]).apply(live, 5, history)
    assert np.array_equal(live, live_before), "the pipeline keeps this as history"
    assert all(np.array_equal(a, b) for a, b in zip(history, history_before))


# ---- individual effects ----------------------------------------------
def test_strobe_blanks_every_nth_frame(scene) -> None:
    live, history = scene
    es = stack(EffectConfig(strobe=3))
    blank = [es.apply(live, i, history).shape[0] == 0 for i in range(12)]
    assert blank == [i % 3 == 0 for i in range(12)]


def test_strobe_of_one_is_treated_as_off(scene) -> None:
    """Blanking every frame is a black screen that reads as a crash."""
    live, history = scene
    es = stack(EffectConfig(strobe=1))
    assert all(es.apply(live, i, history).shape[0] > 0 for i in range(5))


def test_trails_add_layers_from_history(scene, make_boxes) -> None:
    live = make_boxes([(0, 0, 2, 2, 3)])
    history = [make_boxes([(6, 6, 2, 2, 3)]), make_boxes([(4, 4, 2, 2, 3)])]
    out = stack(EffectConfig(trails=2)).apply(live, 5, history)
    assert out.shape[0] == 3
    assert out[0, :4].tolist() == [0, 0, 2, 2], "the live frame comes first"
    assert {tuple(r[:4]) for r in out.tolist()} == {(0, 0, 2, 2), (4, 4, 2, 2), (6, 6, 2, 2)}


def test_trails_dim_with_age(make_boxes) -> None:
    """A trail is an older frame re-emitted at a lower level so the palette
    dims it; the newest trail must be the brightest."""
    live = make_boxes([(0, 0, 2, 2, 7)])
    history = [make_boxes([(4, 4, 2, 2, 7)]), make_boxes([(6, 6, 2, 2, 7)])]
    out = stack(EffectConfig(trails=2), levels=8).apply(live, 3, history)
    by_pos = {tuple(r[:2]): r[L] for r in out.tolist()}
    assert by_pos[(0, 0)] == 7, "the live frame keeps its level"
    assert by_pos[(6, 6)] < 7, "the newest trail is dimmed"
    assert by_pos[(4, 4)] < by_pos[(6, 6)], "the older trail is dimmer still"


def test_trails_thin_the_layer_when_there_is_only_one_level(scene) -> None:
    """With one colour a dimmer copy is not representable, so fewer windows
    stand in for fainter."""
    live, history = scene
    es = stack(EffectConfig(trails=3), levels=1)
    out = es.apply(live, 4, history)
    assert out.shape[0] > live.shape[0], "trails should add boxes"
    assert out.shape[0] < live.shape[0] * 4, "but not all of them"


def test_trails_beyond_the_available_history_are_ignored(scene) -> None:
    live, _ = scene
    for available in range(4):
        history = [live.copy() for _ in range(available)]
        out = stack(EffectConfig(trails=3), levels=8).apply(live, 2, history)
        assert out.shape[0] <= live.shape[0] * (available + 1)


def test_echo_needs_both_an_offset_and_a_ghost(scene) -> None:
    live, history = scene
    assert np.array_equal(stack(EffectConfig(echo_offset=3)).apply(live, 5, history), live)
    assert np.array_equal(stack(EffectConfig(ghost=0.5)).apply(live, 5, history), live)
    out = stack(EffectConfig(echo_offset=3, ghost=0.5)).apply(live, 5, history)
    assert out.shape[0] > live.shape[0]


def test_slitscan_bands_tile_the_grid_without_gaps(make_boxes) -> None:
    """Every band is a horizontal slice of some frame; together they must
    cover the full height exactly once."""
    full = make_boxes([(0, 0, GRID_W, GRID_H, 0)])
    history = [full.copy() for _ in range(6)]
    out = stack(EffectConfig(slitscan=4)).apply(full, 9, history)
    assert out.shape[0] == 4
    rows = np.zeros(GRID_H, np.int32)
    for x, y, w, h, _ in out.tolist():
        rows[y:y + h] += 1
    assert (rows == 1).all(), rows.tolist()


def test_slitscan_of_one_band_is_a_no_op(scene) -> None:
    live, history = scene
    assert np.array_equal(stack(EffectConfig(slitscan=1)).apply(live, 3, history), live)


def test_shuffle_keeps_the_same_rectangles(scene) -> None:
    """It moves window identity, not pixels."""
    live, _ = scene
    out = stack(EffectConfig(shuffle=1000)).apply(live, 4, [])
    assert out.shape == live.shape
    assert sorted(map(tuple, out.tolist())) == sorted(map(tuple, live.tolist()))


def test_shuffle_of_a_single_box_is_safe(make_boxes) -> None:
    one = make_boxes([(1, 1, 2, 2)])
    out = stack(EffectConfig(shuffle=5)).apply(one, 4, [])
    assert np.array_equal(out, one)


def test_shuffle_reorders_something(scene) -> None:
    live, _ = scene
    out = stack(EffectConfig(shuffle=1000)).apply(live, 4, [])
    assert not np.array_equal(out, live)


# ---- budget ----------------------------------------------------------
@pytest.mark.parametrize("cap", [1, 10, 55, 500])
def test_max_boxes_is_never_exceeded(cap: int, scene) -> None:
    live, history = scene
    es = stack(EFFECTS["everything"], max_boxes=cap)
    for frame in range(1, 9):
        assert es.apply(live, frame, history).shape[0] <= cap


def test_the_live_frame_outlives_the_echo_layers(scene, make_boxes) -> None:
    """A tight budget must sacrifice trails, not the picture."""
    live = make_boxes([(i, 0, 1, 1, 3) for i in range(10)])
    history = [make_boxes([(i, 5, 1, 1, 3) for i in range(10)]) for _ in range(6)]
    out = EffectStack(EffectConfig(trails=5), grid_w=GRID_W, grid_h=GRID_H,
                      levels=4, max_boxes=12).apply(live, 3, history)
    assert out.shape[0] == 12
    live_rows = sum(1 for r in out.tolist() if r[1] == 0)
    assert live_rows == 10, "every live box must survive"


def test_max_boxes_is_clamped_to_at_least_one() -> None:
    assert EffectStack(EffectConfig(), max_boxes=0).max_boxes == 1
    assert EffectStack(EffectConfig(), max_boxes=-9).max_boxes == 1


# ---- speed_scale -----------------------------------------------------
@pytest.mark.parametrize("depth", [0.1, 0.5, 1.0, 3.0, 25.0])
def test_speed_scale_stays_within_the_playable_range(depth: float) -> None:
    es = stack(EffectConfig(time_warp=depth, warp_period=4.0))
    for i in range(400):
        v = es.speed_scale(i * 0.05)
        assert MIN_SPEED <= v <= MAX_SPEED, (depth, i, v)


def test_speed_scale_is_a_sine_around_one() -> None:
    es = stack(EffectConfig(time_warp=0.5, warp_period=4.0))
    assert es.speed_scale(0.0) == pytest.approx(1.0)
    assert es.speed_scale(1.0) == pytest.approx(1.5)          # quarter period
    assert es.speed_scale(2.0) == pytest.approx(1.0)
    assert es.speed_scale(3.0) == pytest.approx(0.5)
    assert es.speed_scale(4.0) == pytest.approx(1.0, abs=1e-9)


def test_speed_scale_is_periodic() -> None:
    es = stack(EffectConfig(time_warp=0.4, warp_period=2.5))
    for t in (0.0, 0.3, 1.1, 2.4):
        assert es.speed_scale(t) == pytest.approx(es.speed_scale(t + 2.5))


@pytest.mark.parametrize("period", [0.0, -1.0])
def test_a_non_positive_period_disables_the_warp(period: float) -> None:
    assert stack(EffectConfig(time_warp=0.9, warp_period=period)).speed_scale(1.0) == 1.0


def test_a_deep_warp_never_stalls_or_reverses() -> None:
    """Depth above 1 would otherwise ask for a zero or negative frame rate."""
    es = stack(EffectConfig(time_warp=4.0, warp_period=2.0))
    lows = [es.speed_scale(t) for t in np.linspace(0, 4, 200)]
    assert min(lows) >= MIN_SPEED > 0.0
    assert max(lows) <= MAX_SPEED
    assert math.isclose(min(lows), MIN_SPEED)


# ---- configure / reset -----------------------------------------------
def test_configure_overrides_the_inferred_grid(scene) -> None:
    live, _ = scene
    es = EffectStack(EffectConfig(jitter=6))
    es.configure(grid_w=12, grid_h=8, levels=2, max_boxes=7)
    out = es.apply(live, 1, [])
    assert_in_grid(out, 12, 8)
    assert out.shape[0] <= 7


def test_reset_forgets_only_the_inferred_geometry(scene) -> None:
    live, _ = scene
    inferred = EffectStack(EffectConfig(jitter=1))
    inferred.apply(live, 0, [])
    inferred.reset()
    assert inferred._grid_w == 0 and inferred._grid_h == 0

    fixed = stack(EffectConfig(jitter=1))
    fixed.apply(live, 0, [])
    fixed.reset()
    assert (fixed._grid_w, fixed._grid_h) == (GRID_W, GRID_H)


def test_the_stack_reads_config_changes_live(scene) -> None:
    """The UI mutates EffectConfig in place while playback runs."""
    live, history = scene
    cfg = EffectConfig()
    es = stack(cfg)
    assert es.apply(live, 3, history) is live
    cfg.jitter = 4
    assert not np.array_equal(es.apply(live, 3, history), live)
    cfg.jitter = 0
    assert es.apply(live, 3, history) is live
