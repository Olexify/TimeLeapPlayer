"""Rectangle decomposition invariants.

Two properties are load-bearing for the renderer and are asserted here for
every algorithm that claims them: rectangles must not overlap (each one is
an opaque top-most window, so an overlap is a window painting over another
for no reason) and must not cover a cell outside the mask (overspill is a
lit window where the frame is black).
"""
from __future__ import annotations

import numpy as np
import pytest

from timeleap.config import VideoConfig
from timeleap.core import boxgen as B


# ---- helpers ---------------------------------------------------------
def cover_counts(rects: np.ndarray, shape: tuple[int, int]) -> np.ndarray:
    """How many rectangles land on each grid cell."""
    canvas = np.zeros(shape, np.int32)
    for row in np.asarray(rects):
        x, y, w, h = (int(v) for v in row[:4])
        canvas[y:y + h, x:x + w] += 1
    return canvas


def random_mask(shape: tuple[int, int], density: float, seed: int) -> np.ndarray:
    rng = np.random.default_rng(seed)
    return rng.random(shape) < density


def blob_mask(shape: tuple[int, int], seed: int) -> np.ndarray:
    """Silhouette-ish mask: a few filled discs, which is what real footage
    looks like far more than uniform noise does."""
    h, w = shape
    yy, xx = np.mgrid[0:h, 0:w]
    rng = np.random.default_rng(seed)
    mask = np.zeros(shape, bool)
    for _ in range(4):
        cy, cx = rng.integers(0, h), rng.integers(0, w)
        r = rng.integers(2, max(3, min(h, w) // 3))
        mask |= (yy - cy) ** 2 + (xx - cx) ** 2 <= r * r
    return mask


MASKS = {
    "sparse": random_mask((24, 32), 0.15, 1),
    "medium": random_mask((24, 32), 0.45, 2),
    "dense": random_mask((24, 32), 0.85, 3),
    "blobs": blob_mask((28, 40), 4),
    "stripes": np.tile(np.array([True, True, False, False] * 8), (20, 1)),
}


# ---- exact cover -----------------------------------------------------
@pytest.mark.parametrize("name", sorted(MASKS))
def test_boxes_fast_is_an_exact_cover(name: str) -> None:
    mask = MASKS[name]
    rects = B.boxes_fast(mask)
    counts = cover_counts(rects, mask.shape)
    assert counts.max(initial=0) <= 1, "rectangles overlap"
    assert np.array_equal(counts > 0, mask), "cover differs from the mask"


@pytest.mark.parametrize("name", sorted(MASKS))
def test_boxes_quality_is_an_exact_cover(name: str) -> None:
    mask = MASKS[name]
    rects = B.boxes_quality(mask, budget=4096)
    counts = cover_counts(rects, mask.shape)
    assert counts.max(initial=0) <= 1
    assert np.array_equal(counts > 0, mask)


@pytest.mark.parametrize("name", ["sparse", "medium", "blobs", "stripes"])
def test_quality_uses_no_more_rectangles_on_silhouettes(name: str) -> None:
    """The point of the slow path: fewer windows for the same picture.

    Only claimed for silhouette-shaped content. Greedy maximal-rectangle is
    not an optimal cover, and on near-uniform noise it loses to run-merge --
    see `test_quality_can_lose_on_dense_noise`.
    """
    mask = MASKS[name]
    assert B.boxes_quality(mask, budget=4096).shape[0] <= B.boxes_fast(mask).shape[0]


def test_quality_can_lose_on_dense_noise() -> None:
    """Documents the exception to the module docstring's "fewest boxes".

    At 86% density the greedy cover carves the mask into more rectangles than
    the run-merge does, so `quality` is not a strict improvement -- which
    matters because it is 100x slower for the privilege.
    """
    mask = MASKS["dense"]
    assert B.boxes_quality(mask, budget=4096).shape[0] > B.boxes_fast(mask).shape[0]


@pytest.mark.parametrize("algo", ["fast", "quality"])
@pytest.mark.parametrize("name", sorted(MASKS))
def test_frame_to_boxes_never_overspills(algo: str, name: str) -> None:
    """End to end: no rectangle touches a cell the threshold did not set."""
    mask = MASKS[name]
    gray = np.where(mask, 255, 0).astype(np.uint8)
    cfg = VideoConfig(algo=algo, levels=1, threshold_mode="fixed",
                      fixed_threshold=128, max_windows=4096, denoise=False)
    boxes = B.frame_to_boxes(gray, cfg)
    counts = cover_counts(boxes, mask.shape)
    assert counts.max(initial=0) <= 1
    assert not ((counts > 0) & ~mask).any(), "lit cells outside the mask"
    assert np.array_equal(counts > 0, mask), "mask cells left dark"


def test_fast_reproduces_the_mask_exactly_when_under_budget() -> None:
    mask = MASKS["blobs"]
    gray = np.where(mask, 200, 20).astype(np.uint8)
    cfg = VideoConfig(algo="fast", levels=1, threshold_mode="fixed",
                      fixed_threshold=128, max_windows=10_000, denoise=False)
    boxes = B.frame_to_boxes(gray, cfg)
    assert boxes.shape[0] <= cfg.max_windows
    assert np.array_equal(cover_counts(boxes, mask.shape) > 0, mask)


def test_balanced_is_identical_to_fast_while_under_budget() -> None:
    mask = MASKS["blobs"]
    fast = B._decompose(mask, "fast", 10_000, 1)
    balanced = B._decompose(mask, "balanced", 10_000, 1)
    assert np.array_equal(np.sort(fast, axis=0), np.sort(balanced, axis=0))


# ---- budgets ---------------------------------------------------------
@pytest.mark.parametrize("algo", ["fast", "balanced", "quality"])
@pytest.mark.parametrize("budget", [1, 4, 12, 40])
def test_budget_is_respected(algo: str, budget: int) -> None:
    mask = MASKS["medium"]
    rects = B._decompose(mask, algo, budget, 1)
    assert rects.shape[0] <= budget
    assert rects.shape[1] == 4


@pytest.mark.parametrize("algo", ["fast", "balanced", "quality"])
@pytest.mark.parametrize("levels", [1, 4])
def test_frame_to_boxes_respects_max_windows(algo: str, levels: int) -> None:
    gray = (np.tile(np.linspace(0, 255, 48), (32, 1))
            + np.tile(np.linspace(0, 60, 32), (48, 1)).T).astype(np.uint8)
    for budget in (1, 5, 25):
        cfg = VideoConfig(algo=algo, levels=levels, max_windows=budget,
                          denoise=False)
        boxes = B.frame_to_boxes(gray, cfg)
        assert boxes.shape[0] <= budget, (algo, levels, budget)
        assert boxes.shape[1] == 5
        assert boxes.dtype == np.int32


def test_balanced_keeps_more_of_the_picture_than_dropping() -> None:
    """`balanced` merges neighbours instead of deleting small rectangles, so
    at a tight budget it must still cover far more of the mask than the
    largest-N truncation `fast` falls back to."""
    mask = B.despeckle(random_mask((24, 32), 0.30, 3))
    total = int(mask.sum())
    for budget in (10, 20, 40):
        bal = B._decompose(mask, "balanced", budget, 1)
        fast = B._decompose(mask, "fast", budget, 1)
        bal_hit = int((cover_counts(bal, mask.shape) > 0)[mask].sum())
        fast_hit = int((cover_counts(fast, mask.shape) > 0)[mask].sum())
        assert bal.shape[0] <= budget and fast.shape[0] <= budget
        assert bal_hit > fast_hit, budget
        assert bal_hit <= total


def test_min_box_area_filters_small_rectangles() -> None:
    mask = MASKS["sparse"]
    rects = B._decompose(mask, "fast", 10_000, 4)
    if rects.size:
        assert (rects[:, 2] * rects[:, 3] >= 4).all()
    unfiltered = B._decompose(mask, "fast", 10_000, 1)
    assert rects.shape[0] < unfiltered.shape[0]


# ---- threshold modes -------------------------------------------------
def test_fixed_threshold_is_exactly_a_comparison() -> None:
    gray = np.arange(256, dtype=np.uint8).reshape(16, 16)
    mask = B.build_mask(gray, "fixed", fixed=100, denoise=False)
    assert np.array_equal(mask, gray > 100)
    assert mask.dtype == np.bool_


def test_otsu_splits_a_bimodal_frame_on_the_real_boundary() -> None:
    gray = np.zeros((16, 16), np.uint8)
    gray[:, 8:] = 200
    assert 0 <= B.otsu_threshold(gray) < 200
    mask = B.build_mask(gray, "otsu", denoise=False)
    assert not mask[:, :8].any()
    assert mask[:, 8:].all()


def test_otsu_on_a_flat_frame_does_not_explode() -> None:
    for value in (0, 128, 255):
        gray = np.full((8, 8), value, np.uint8)
        t = B.otsu_threshold(gray)
        assert 0 <= t <= 255


def test_adaptive_finds_local_contrast_a_global_threshold_misses() -> None:
    """A bar 45 grey above its surroundings, sitting in the dark end of an
    illumination gradient: a fixed threshold cannot see it at all."""
    base = np.tile(np.linspace(10, 200, 64).astype(np.uint8), (32, 1))
    gray = base.copy()
    gray[12:20, :] = np.clip(base[12:20, :].astype(int) + 45, 0, 255).astype(np.uint8)

    adaptive = B.build_mask(gray, "adaptive", block=9, bias=4, denoise=False)
    fixed = B.build_mask(gray, "fixed", fixed=128, denoise=False)

    dark_bar = (slice(12, 20), slice(0, 16))
    assert adaptive[dark_bar].all(), "adaptive missed the local bar"
    assert not fixed[dark_bar].any(), "fixed threshold should not see it"


def test_edge_mode_outlines_rather_than_fills() -> None:
    gray = np.zeros((24, 24), np.uint8)
    gray[6:18, 6:18] = 255
    mask = B.build_mask(gray, "edge", fixed=128, denoise=False)
    assert not mask[9:15, 9:15].any(), "solid interior should not be an edge"
    assert mask.sum() > 0
    assert mask[5:8, 8:16].any(), "the top border should be an edge"


@pytest.mark.parametrize("mode", ["otsu", "fixed", "adaptive", "edge"])
def test_invert_is_the_exact_complement(mode: str) -> None:
    gray = np.zeros((16, 16), np.uint8)
    gray[4:12, 4:12] = 255
    normal = B.build_mask(gray, mode, fixed=128, block=5, invert=False, denoise=False)
    flipped = B.build_mask(gray, mode, fixed=128, block=5, invert=True, denoise=False)
    assert np.array_equal(flipped, ~normal)


@pytest.mark.parametrize("mode", ["otsu", "fixed", "adaptive", "edge"])
def test_every_mode_reaches_frame_to_boxes_cleanly(mode: str) -> None:
    gray = (np.tile(np.linspace(0, 255, 40), (24, 1))).astype(np.uint8)
    cfg = VideoConfig(threshold_mode=mode, levels=1, max_windows=500,
                      adaptive_block=5, denoise=False)
    boxes = B.frame_to_boxes(gray, cfg)
    assert boxes.shape[1] == 5 and boxes.dtype == np.int32
    if boxes.shape[0]:
        assert (boxes[:, [B.X, B.Y]] >= 0).all()
        assert (boxes[:, B.X] + boxes[:, B.W] <= 40).all()
        assert (boxes[:, B.Y] + boxes[:, B.H] <= 24).all()
        assert (boxes[:, [B.W, B.H]] > 0).all()


# ---- levels ----------------------------------------------------------
@pytest.mark.parametrize("levels", [2, 3, 5, 8, 16])
def test_quantize_bands_are_a_partition(levels: int) -> None:
    gray = np.tile(np.linspace(0, 255, 64).astype(np.uint8), (32, 1))
    band = B.quantize(gray, levels, "otsu", denoise=False)
    assert band.shape == gray.shape
    assert band.min() >= 0 and band.max() <= levels
    # Each cell carries exactly one band, so the masks are disjoint by
    # construction -- assert it anyway, it is what lets the renderer skip
    # z-ordering entirely.
    stack = np.stack([(band == lv) for lv in range(0, levels + 1)])
    assert (stack.sum(axis=0) == 1).all()


@pytest.mark.parametrize("levels", [2, 3, 5, 8])
def test_multi_level_boxes_never_overlap(levels: int) -> None:
    gray = np.tile(np.linspace(0, 255, 64).astype(np.uint8), (36, 1))
    cfg = VideoConfig(grid_w=64, grid_h=36, levels=levels, max_windows=800,
                      algo="fast", denoise=False)
    boxes = B.frame_to_boxes(gray, cfg)
    assert boxes.shape[0] > 0
    counts = cover_counts(boxes, gray.shape)
    assert counts.max() == 1, "bands of different levels overlap"
    present = set(boxes[:, B.L].tolist())
    assert present, "no levels emitted"
    assert min(present) >= 0 and max(present) <= levels - 1


def test_levels_below_one_degrade_to_a_silhouette() -> None:
    gray = np.where(blob_mask((20, 20), 5), 255, 0).astype(np.uint8)
    for levels in (0, -3, 1):
        cfg = VideoConfig(levels=levels, max_windows=500, denoise=False)
        boxes = B.frame_to_boxes(gray, cfg)
        assert (boxes[:, B.L] == 0).all()


# ---- degenerate frames -----------------------------------------------
def test_empty_frame_returns_no_boxes() -> None:
    cfg = VideoConfig(levels=1, denoise=False)
    for shape in [(8, 8), (1, 1), (1, 16), (16, 1)]:
        boxes = B.frame_to_boxes(np.zeros(shape, np.uint8), cfg)
        assert boxes.shape == (0, 5)
        assert boxes.dtype == np.int32


def test_full_frame_is_one_rectangle() -> None:
    cfg = VideoConfig(levels=1, max_windows=64, denoise=False)
    boxes = B.frame_to_boxes(np.full((12, 20), 255, np.uint8), cfg)
    assert boxes.shape == (1, 5)
    assert boxes[0, :4].tolist() == [0, 0, 20, 12]


def test_one_by_one_frame() -> None:
    cfg = VideoConfig(levels=1, denoise=False)
    lit = B.frame_to_boxes(np.full((1, 1), 255, np.uint8), cfg)
    assert lit.shape == (1, 5) and lit[0, :4].tolist() == [0, 0, 1, 1]
    assert B.frame_to_boxes(np.zeros((1, 1), np.uint8), cfg).shape == (0, 5)


def test_single_row_and_single_column() -> None:
    row = np.array([[True, True, False, True, True, True]])
    assert sorted(B.boxes_fast(row).tolist()) == [[0, 0, 2, 1], [3, 0, 3, 1]]
    col = np.array([[True], [True], [False], [True]])
    assert sorted(B.boxes_fast(col).tolist()) == [[0, 0, 1, 2], [0, 3, 1, 1]]


def test_empty_mask_shapes_are_still_two_dimensional() -> None:
    for fn in (B.boxes_fast, lambda m: B.boxes_quality(m, 16),
               lambda m: B._decompose(m, "fast", 16, 1)):
        out = fn(np.zeros((6, 6), bool))
        assert out.shape == (0, 4)


# ---- preprocess ------------------------------------------------------
RAMP = np.arange(256, dtype=np.uint8).reshape(16, 16)


def test_preprocess_identity_is_a_passthrough() -> None:
    assert B.preprocess(RAMP, 1.0, 1.0, 0) is RAMP


@pytest.mark.parametrize("gamma", [0.05, 0.4, 1.0, 2.5, 6.0])
def test_gamma_is_monotonic_and_fixes_the_endpoints(gamma: float) -> None:
    out = B.preprocess(RAMP, gamma=gamma).ravel().astype(int)
    assert np.all(np.diff(out) >= 0), "gamma must not reorder greys"
    assert out[0] == 0 and out[-1] == 255
    assert out.dtype == np.int64 and B.preprocess(RAMP, gamma=gamma).dtype == np.uint8


def test_gamma_direction() -> None:
    dark = B.preprocess(RAMP, gamma=0.4).ravel().astype(int)
    bright = B.preprocess(RAMP, gamma=2.5).ravel().astype(int)
    plain = RAMP.ravel().astype(int)
    assert (dark[1:-1] <= plain[1:-1]).all() and dark[128] < plain[128]
    assert (bright[1:-1] >= plain[1:-1]).all() and bright[128] > plain[128]


@pytest.mark.parametrize("contrast", [0.0, 0.2, 1.0, 3.0, 8.0])
def test_contrast_is_monotonic_and_pivots_on_mid_grey(contrast: float) -> None:
    out = B.preprocess(RAMP, contrast=contrast).ravel().astype(int)
    assert np.all(np.diff(out) >= 0)
    # The LUT pivots on 0.5, which falls between grey 127 and 128, so the
    # nearest sample drifts by half a step times the gain and no further.
    assert abs(int(out[128]) - 128) <= 1.0 + contrast / 2.0, "mid grey must stay put"
    if contrast > 1.0:
        assert out[64] < 64 and out[192] > 192
    elif 0.0 < contrast < 1.0:
        assert out[64] > 64 and out[192] < 192


@pytest.mark.parametrize("brightness", [-120, -30, 0, 30, 120])
def test_brightness_shifts_monotonically(brightness: int) -> None:
    out = B.preprocess(RAMP, brightness=brightness).ravel().astype(int)
    plain = RAMP.ravel().astype(int)
    assert np.all(np.diff(out) >= 0)
    assert out.min() >= 0 and out.max() <= 255
    interior = (plain + brightness > 0) & (plain + brightness < 255)
    assert np.array_equal(out[interior], plain[interior] + brightness)


def test_preprocess_output_is_always_clipped_uint8() -> None:
    out = B.preprocess(RAMP, gamma=0.05, contrast=9.0, brightness=200)
    assert out.dtype == np.uint8 and out.min() >= 0 and out.max() <= 255
    assert out.shape == RAMP.shape


# ---- despeckle -------------------------------------------------------
def test_despeckle_removes_only_isolated_cells() -> None:
    mask = np.zeros((9, 9), bool)
    mask[0, 0] = True                       # lone corner
    mask[4, 4] = True                       # lone centre
    mask[8, 8] = True                       # lone opposite corner
    mask[2, 2] = mask[2, 3] = True          # horizontal pair: survives
    mask[6, 1] = mask[7, 1] = True          # vertical pair: survives
    mask[0, 5] = mask[1, 5] = mask[0, 6] = True   # L on the border: survives

    out = B.despeckle(mask)
    assert out.dtype == np.bool_ and out.shape == mask.shape
    assert not (out & ~mask).any(), "despeckle must never add cells"
    removed = mask & ~out
    assert sorted(map(tuple, np.argwhere(removed).tolist())) == [(0, 0), (4, 4), (8, 8)]


def test_despeckle_leaves_diagonals_alone_because_they_are_not_4_connected() -> None:
    mask = np.zeros((7, 7), bool)
    mask[2, 2] = mask[3, 3] = True
    assert not B.despeckle(mask).any()


def test_despeckle_skips_frames_too_small_to_have_neighbours() -> None:
    for shape in [(1, 1), (2, 2), (2, 9), (9, 2)]:
        mask = np.ones(shape, bool)
        assert B.despeckle(mask) is mask


def test_denoise_flag_reaches_frame_to_boxes() -> None:
    gray = np.zeros((12, 12), np.uint8)
    gray[0, 0] = 255                        # speckle
    gray[5:8, 5:8] = 255                    # real blob
    noisy = B.frame_to_boxes(gray, VideoConfig(levels=1, threshold_mode="fixed",
                                               denoise=False, max_windows=99))
    clean = B.frame_to_boxes(gray, VideoConfig(levels=1, threshold_mode="fixed",
                                               denoise=True, max_windows=99))
    assert cover_counts(noisy, gray.shape)[0, 0] == 1
    assert cover_counts(clean, gray.shape)[0, 0] == 0
    assert cover_counts(clean, gray.shape)[5:8, 5:8].all()


# ---- normalize -------------------------------------------------------
def test_normalize_maps_the_grid_onto_the_unit_square() -> None:
    boxes = np.array([[0, 0, 8, 6, 0], [4, 3, 4, 3, 2]], np.int32)
    out = B.normalize(boxes, 8, 6)
    assert out.dtype == np.float32 and out.shape == boxes.shape
    assert out[0].tolist() == [0.0, 0.0, 1.0, 1.0, 0.0]
    assert out[1].tolist() == [0.5, 0.5, 0.5, 0.5, 2.0]


def test_normalize_stays_within_zero_and_one_for_in_grid_boxes() -> None:
    rng = np.random.default_rng(11)
    gw, gh = 96, 54
    boxes = np.zeros((200, 5), np.int32)
    boxes[:, 0] = rng.integers(0, gw, 200)
    boxes[:, 1] = rng.integers(0, gh, 200)
    boxes[:, 2] = np.minimum(rng.integers(1, 20, 200), gw - boxes[:, 0])
    boxes[:, 3] = np.minimum(rng.integers(1, 20, 200), gh - boxes[:, 1])
    out = B.normalize(boxes, gw, gh)
    assert (out[:, :4] >= 0.0).all()
    assert (out[:, B.X] + out[:, B.W] <= 1.0 + 1e-6).all()
    assert (out[:, B.Y] + out[:, B.H] <= 1.0 + 1e-6).all()


def test_normalize_of_an_empty_frame() -> None:
    out = B.normalize(B.EMPTY, 96, 54)
    assert out.shape == (0, 5) and out.dtype == np.float32


def test_normalize_does_not_mutate_its_input() -> None:
    boxes = np.array([[4, 2, 4, 6, 1]], np.int32)
    original = boxes.copy()
    B.normalize(boxes, 8, 8)
    assert np.array_equal(boxes, original)
