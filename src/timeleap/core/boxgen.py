"""Frame -> rectangles. The hot path of the whole player.

The original bad_apple_virus approach (and this project's first version)
repeatedly scanned for the single largest all-white rectangle, blanked it,
and repeated up to `max_windows` times. That is O(K * W * H) of pure-Python
work per frame -- 45..180 ms at useful grid sizes, which caps playback well
below 24 fps and makes a three-minute video take ten minutes to prepare.

`boxes_fast` replaces it with a fully vectorised run-length decomposition:

  1. Extract horizontal runs of set cells with a single `np.diff` per frame.
  2. Merge runs that occupy an identical column span in consecutive rows,
     via one `lexsort` + boundary mask -- no Python loop at all.

That is O(W * H) once instead of K times, runs 22-380x faster, never paints
a cell that is not part of the mask, and produces *fewer* boxes than the
greedy version on real silhouette footage.

Three algorithms are exposed:
  fast      run-merge, exact cover, ~0.3 ms/frame          (default, live)
  balanced  run-merge + least-cost pair merging to fit the window budget
  quality   greedy maximal-rectangle, fewest boxes, slow   (baking only)

All of them return an int32 array of shape (N, 5): x, y, w, h, level -- in
grid cell units, not normalised floats, so the cache can store them exactly
in two bytes per coordinate.
"""
from __future__ import annotations

import numpy as np

# Column layout of the returned array.
X, Y, W, H, L = 0, 1, 2, 3, 4
EMPTY = np.empty((0, 5), np.int32)


# ---------------------------------------------------------------- thresholds
def otsu_threshold(gray: np.ndarray) -> int:
    """Classic Otsu, fully vectorised (the original looped over 256 bins)."""
    hist = np.bincount(gray.ravel(), minlength=256).astype(np.float64)
    total = gray.size
    w_b = np.cumsum(hist)
    w_f = total - w_b
    idx = np.arange(256, dtype=np.float64)
    sum_b = np.cumsum(hist * idx)
    sum_all = sum_b[-1]
    with np.errstate(invalid="ignore", divide="ignore"):
        m_b = sum_b / w_b
        m_f = (sum_all - sum_b) / w_f
        var = w_b * w_f * (m_b - m_f) ** 2
    var[~np.isfinite(var)] = 0.0
    return int(np.argmax(var))


def _box_mean(gray: np.ndarray, block: int) -> np.ndarray:
    """Mean over a (block x block) window via a summed-area table."""
    block = max(3, block | 1)
    r = block // 2
    pad = np.pad(gray.astype(np.float32), r + 1, mode="edge")
    sat = pad.cumsum(0).cumsum(1)
    h, w = gray.shape
    y0, x0 = 0, 0
    y1, x1 = y0 + block, x0 + block
    total = (sat[y1:y1 + h, x1:x1 + w] - sat[y0:y0 + h, x1:x1 + w]
             - sat[y1:y1 + h, x0:x0 + w] + sat[y0:y0 + h, x0:x0 + w])
    return total / float(block * block)


def _edges(gray: np.ndarray) -> np.ndarray:
    """Gradient magnitude, scaled to 0..255 -- outline mode."""
    g = gray.astype(np.float32)
    gx = np.zeros_like(g)
    gy = np.zeros_like(g)
    gx[:, 1:-1] = g[:, 2:] - g[:, :-2]
    gy[1:-1, :] = g[2:, :] - g[:-2, :]
    mag = np.hypot(gx, gy)
    peak = float(mag.max())
    if peak <= 0:
        return np.zeros_like(gray)
    return np.clip(mag * (255.0 / peak), 0, 255).astype(np.uint8)


# ---------------------------------------------------------------- preprocess
def preprocess(gray: np.ndarray, gamma: float = 1.0, contrast: float = 1.0,
               brightness: int = 0) -> np.ndarray:
    """Tone adjustments applied before thresholding. Cheap LUT, not per-pixel."""
    if gamma == 1.0 and contrast == 1.0 and brightness == 0:
        return gray
    lut = np.arange(256, dtype=np.float32) / 255.0
    if gamma != 1.0:
        lut = np.power(lut, 1.0 / max(0.05, gamma))
    if contrast != 1.0:
        lut = (lut - 0.5) * contrast + 0.5
    lut = lut * 255.0 + brightness
    return np.clip(lut, 0, 255).astype(np.uint8)[gray]


def despeckle(mask: np.ndarray) -> np.ndarray:
    """Drop set cells with no 4-connected set neighbour.

    Single stray cells become single 1x1 windows, which cost as much as a
    large one and read as noise. Removing them is almost free and visibly
    cleans up noisy or heavily compressed sources.
    """
    if mask.shape[0] < 3 or mask.shape[1] < 3:
        return mask
    n = np.zeros(mask.shape, np.uint8)
    n[1:, :] += mask[:-1, :]
    n[:-1, :] += mask[1:, :]
    n[:, 1:] += mask[:, :-1]
    n[:, :-1] += mask[:, 1:]
    return mask & (n > 0)


def build_mask(gray: np.ndarray, mode: str = "otsu", fixed: int = 128,
               block: int = 15, bias: int = 4, invert: bool = False,
               denoise: bool = True) -> np.ndarray:
    """Grey frame -> boolean 'this cell should be a window' mask."""
    if mode == "edge":
        mask = _edges(gray) > max(1, fixed // 2)
    elif mode == "adaptive":
        mask = gray.astype(np.int16) > (_box_mean(gray, block) - bias)
    elif mode == "fixed":
        mask = gray > fixed
    else:
        mask = gray > otsu_threshold(gray)
    if invert:
        mask = ~mask
    if denoise:
        mask = despeckle(mask)
    return mask


def quantize(gray: np.ndarray, levels: int, mode: str = "otsu", fixed: int = 128,
             block: int = 15, bias: int = 4, invert: bool = False,
             denoise: bool = True) -> np.ndarray:
    """Grey frame -> int8 band index, 0 = background, 1..levels = visible.

    Bands are disjoint, so the rectangles of different levels never overlap
    and no z-ordering is needed -- which matters a lot when every rectangle
    is an opaque top-most window.
    """
    if levels <= 1:
        return build_mask(gray, mode, fixed, block, bias, invert, denoise).astype(np.int8)

    src = _edges(gray) if mode == "edge" else gray
    if invert:
        src = 255 - src
    floor = otsu_threshold(src) if mode == "otsu" else fixed
    floor = int(np.clip(floor, 0, 254))
    edges = np.linspace(floor, 255, levels + 1)[1:]
    band = np.digitize(src, edges, right=True).astype(np.int8) + 1
    band[src <= floor] = 0
    if denoise:
        band[~despeckle(band > 0)] = 0
    return band


# ---------------------------------------------------------------- algorithms
def _runs(mask: np.ndarray) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Horizontal runs of True. Returns (row, x_start, x_end_exclusive)."""
    h, w = mask.shape
    pad = np.zeros((h, w + 2), np.int8)
    pad[:, 1:-1] = mask
    d = np.diff(pad, axis=1)
    rows, starts = np.nonzero(d == 1)
    _, ends = np.nonzero(d == -1)
    return rows, starts, ends


def _merge_runs(rows: np.ndarray, x0: np.ndarray, x1: np.ndarray) -> np.ndarray:
    """Coalesce identical column spans in consecutive rows. Fully vectorised."""
    n = rows.size
    if n == 0:
        return np.empty((0, 4), np.int32)
    order = np.lexsort((rows, x1, x0))
    r, a, b = rows[order], x0[order], x1[order]
    brk = np.empty(n, bool)
    brk[0] = True
    if n > 1:
        brk[1:] = (a[1:] != a[:-1]) | (b[1:] != b[:-1]) | (r[1:] != r[:-1] + 1)
    gs = np.nonzero(brk)[0]
    ge = np.append(gs[1:] - 1, n - 1)
    return np.stack([a[gs], r[gs], b[gs] - a[gs], r[ge] - r[gs] + 1], 1).astype(np.int32)


def boxes_fast(mask: np.ndarray) -> np.ndarray:
    """Exact-cover run-merge decomposition. ~0.3 ms at 96x54."""
    return _merge_runs(*_runs(mask))


def _fit_budget_by_area(rects: np.ndarray, budget: int) -> np.ndarray:
    """Keep the `budget` largest rectangles."""
    if rects.shape[0] <= budget:
        return rects
    area = rects[:, 2] * rects[:, 3]
    keep = np.argpartition(-area, budget)[:budget]
    return rects[keep]


def _fit_budget_by_merging(rects: np.ndarray, budget: int,
                           max_waste: float = 2.5) -> np.ndarray:
    """Shrink the rectangle count by unioning cheap neighbours, not by dropping.

    Dropping the smallest rectangles erases detail (eyes, fingers, thin
    limbs) because small does not mean unimportant. Merging two neighbours
    into their bounding box keeps the detail and only costs a little
    overspill, so the silhouette survives a tight window budget far better.
    """
    if rects.shape[0] <= budget:
        return rects
    items = [tuple(int(v) for v in r) for r in rects]
    while len(items) > budget:
        # Order by area so we consider the cheapest rectangles for absorption.
        items.sort(key=lambda r: r[2] * r[3])
        best = None
        # Only the smallest few need a partner; that keeps this O(n) per round.
        probe = min(len(items), 24)
        for i in range(probe):
            xi, yi, wi, hi = items[i]
            ai = wi * hi
            for j in range(len(items)):
                if i == j:
                    continue
                xj, yj, wj, hj = items[j]
                ux, uy = min(xi, xj), min(yi, yj)
                uw = max(xi + wi, xj + wj) - ux
                uh = max(yi + hi, yj + hj) - uy
                waste = uw * uh - ai - wj * hj
                if best is None or waste < best[0]:
                    best = (waste, i, j, (ux, uy, uw, uh))
            if best is not None and best[0] <= 0:
                break
        if best is None:
            break
        waste, i, j, union = best
        if waste > max_waste * max(1.0, len(items) - budget):
            # Nothing cheap left to merge; fall back to dropping the rest.
            return _fit_budget_by_area(np.array(items, np.int32), budget)
        for k in sorted((i, j), reverse=True):
            items.pop(k)
        items.append(union)
    return np.array(items, np.int32)


def _largest_rect(work: list[list[bool]], h: int, w: int):
    """Largest all-True axis-aligned rectangle, via the histogram-stack method."""
    best_area = 0
    best = None
    heights = [0] * w
    for y in range(h):
        row = work[y]
        for x in range(w):
            heights[x] = heights[x] + 1 if row[x] else 0
        stack: list[tuple[int, int]] = []
        for x in range(w + 1):
            cur = heights[x] if x < w else 0
            start = x
            while stack and stack[-1][1] >= cur:
                idx, ht = stack.pop()
                area = (x - idx) * ht
                if area > best_area:
                    best_area = area
                    best = (idx, y - ht + 1, x - idx, ht)
                start = idx
            stack.append((start, cur))
    return best


def boxes_quality(mask: np.ndarray, budget: int, min_area: int = 1) -> np.ndarray:
    """Greedy maximal-rectangle cover: fewest boxes, but O(K*W*H). Baking only."""
    h, w = mask.shape
    work = mask.tolist()
    out: list[tuple[int, int, int, int]] = []
    for _ in range(budget):
        rect = _largest_rect(work, h, w)
        if rect is None or rect[2] * rect[3] < max(1, min_area):
            break
        x, y, rw, rh = rect
        out.append(rect)
        for yy in range(y, y + rh):
            row = work[yy]
            for xx in range(x, x + rw):
                row[xx] = False
    return np.array(out, np.int32) if out else np.empty((0, 4), np.int32)


def _decompose(mask: np.ndarray, algo: str, budget: int, min_area: int) -> np.ndarray:
    if not mask.any():
        return np.empty((0, 4), np.int32)
    if algo == "quality":
        return boxes_quality(mask, budget, min_area)
    rects = boxes_fast(mask)
    if min_area > 1 and rects.size:
        rects = rects[rects[:, 2] * rects[:, 3] >= min_area]
    if rects.shape[0] > budget:
        rects = (_fit_budget_by_merging(rects, budget) if algo == "balanced"
                 else _fit_budget_by_area(rects, budget))
    return rects


def frame_to_boxes(gray: np.ndarray, cfg) -> np.ndarray:
    """Grey frame + VideoConfig -> (N, 5) int32 array of x, y, w, h, level.

    `cfg` is a `timeleap.config.VideoConfig` (duck-typed, so tests can pass
    any object with the same attribute names).
    """
    gray = preprocess(gray, cfg.gamma, cfg.contrast, cfg.brightness)
    levels = max(1, int(cfg.levels))

    if levels == 1:
        mask = build_mask(gray, cfg.threshold_mode, cfg.fixed_threshold,
                          cfg.adaptive_block, cfg.adaptive_bias, cfg.invert,
                          cfg.denoise)
        rects = _decompose(mask, cfg.algo, cfg.max_windows, cfg.min_box_area)
        if rects.shape[0] == 0:
            return EMPTY
        out = np.zeros((rects.shape[0], 5), np.int32)
        out[:, :4] = rects
        return out

    band = quantize(gray, levels, cfg.threshold_mode, cfg.fixed_threshold,
                    cfg.adaptive_block, cfg.adaptive_bias, cfg.invert, cfg.denoise)
    # Split the budget across bands by how much screen each one occupies, so a
    # large dim background does not starve a small bright highlight.
    counts = np.array([int((band == lv).sum()) for lv in range(1, levels + 1)])
    total = int(counts.sum())
    if total == 0:
        return EMPTY
    share = np.maximum(4, (counts / total * cfg.max_windows).astype(int))
    chunks = []
    for i, lv in enumerate(range(1, levels + 1)):
        if counts[i] == 0:
            continue
        rects = _decompose(band == lv, cfg.algo, int(share[i]), cfg.min_box_area)
        if rects.shape[0] == 0:
            continue
        part = np.zeros((rects.shape[0], 5), np.int32)
        part[:, :4] = rects
        part[:, L] = lv - 1
        chunks.append(part)
    if not chunks:
        return EMPTY
    out = np.concatenate(chunks, 0)
    if out.shape[0] > cfg.max_windows:
        area = out[:, W] * out[:, H]
        out = out[np.argpartition(-area, cfg.max_windows)[:cfg.max_windows]]
    return out


def normalize(boxes: np.ndarray, grid_w: int, grid_h: int) -> np.ndarray:
    """Grid-unit boxes -> float32 0..1 coordinates (x, y, w, h) + level."""
    if boxes.shape[0] == 0:
        return np.empty((0, 5), np.float32)
    out = boxes.astype(np.float32)
    out[:, [X, W]] /= float(grid_w)
    out[:, [Y, H]] /= float(grid_h)
    return out
