"""Temporal distortions applied to box arrays -- the "time leap" half.

Every effect here works on rectangles, never on pixels. A trail is literally
the boxes of an older frame re-emitted with a smaller `level`, so the palette
dims them for free (`palette.resolve` is ordered dim -> bright) and the only
real cost is more windows in the pool. That keeps the whole stack in the tens
of microseconds per frame, which matters because `apply` runs between decode
and render on the playback thread.

Two properties are non-negotiable:

* **Deterministic.** Jitter and shuffle are seeded from the frame index, not
  from a running RNG, so seeking back to frame 900 repaints frame 900 exactly
  and a paused frame does not strobe. A per-frame RNG would also make the
  screen flicker at random -- with a hundred flashing top-most windows that is
  a genuine photosensitivity hazard, not just an aesthetic one.
* **In bounds.** The renderer maps grid cells to screen rectangles with no
  clamping of its own, so anything leaving the grid here becomes a window
  hanging off the edge of the montage. Every path ends in `_clip`.

`EffectConfig` is mutated live by the UI sliders, so its fields are read on
every call rather than cached in the constructor.
"""
from __future__ import annotations

import math

import numpy as np

from ..config import EffectConfig
from .boxgen import EMPTY, H, L, W, X, Y

# Ceiling on what a single frame may return. Trails multiply the box count by
# (trails + 1) and every box is an OS window; without a cap a slider drag can
# ask the compositor for thousands of them and wedge the desktop.
MAX_BOXES = 2048
MAX_TRAILS = 16
MAX_BANDS = 64

MIN_SPEED, MAX_SPEED = 0.1, 4.0

# Stands in for the far grid edge before it is known, so `_clip` still bounds
# the near edge instead of passing everything through untouched.
_UNBOUNDED = int(np.iinfo(np.int32).max)

_NOISE_SIZE = 4096
_NOISE_SEED = 0x71E1EA9
_HASH = 2654435761        # Knuth's constant: frame index -> noise table offset


class EffectStack:
    """Applies the configured temporal effects to one frame's boxes.

    `grid_w`/`grid_h`/`levels` are optional because the contract only promises
    `EffectStack(cfg)`; when they are not supplied they are inferred from the
    boxes seen so far (a running maximum, never shrinking, so a frame whose
    content sits in the middle of the grid does not narrow the clip bounds).
    Pass them via `configure` when the pipeline knows the real values.
    """

    def __init__(self, cfg: EffectConfig, grid_w: int | None = None,
                 grid_h: int | None = None, levels: int | None = None,
                 max_boxes: int = MAX_BOXES) -> None:
        self.cfg = cfg
        self.max_boxes = max(1, int(max_boxes))
        self._fixed_grid = bool(grid_w and grid_h)
        self._fixed_levels = bool(levels)
        self._grid_w = int(grid_w) if grid_w else 0
        self._grid_h = int(grid_h) if grid_h else 0
        self._levels = int(levels) if levels else 1
        # One fixed noise table beats constructing an RNG per frame: jitter is
        # the hot path and only needs values that are stable for a given frame
        # index, not statistically pristine ones.
        self._noise = np.random.default_rng(_NOISE_SEED).uniform(
            -1.0, 1.0, (2, _NOISE_SIZE)).astype(np.float32)

    # ---- configuration ------------------------------------------------
    def configure(self, grid_w: int | None = None, grid_h: int | None = None,
                  levels: int | None = None, max_boxes: int | None = None) -> None:
        """Supply the real grid/palette geometry; anything omitted is untouched."""
        if grid_w and grid_h:
            self._grid_w, self._grid_h = int(grid_w), int(grid_h)
            self._fixed_grid = True
        if levels:
            self._levels = max(1, int(levels))
            self._fixed_levels = True
        if max_boxes:
            self.max_boxes = max(1, int(max_boxes))

    def reset(self) -> None:
        """Forget inferred geometry. Call on seek or when the grid changes."""
        if not self._fixed_grid:
            self._grid_w = self._grid_h = 0
        if not self._fixed_levels:
            self._levels = 1

    # ---- per-frame entry point ----------------------------------------
    def apply(self, boxes: np.ndarray, frame_index: int,
              history: list[np.ndarray]) -> np.ndarray:
        """Return the boxes to actually draw. `history` is oldest-first.

        `history[-k]` is the frame `k` steps before `boxes`, which is not
        itself in the list. Layers are concatenated with the current frame
        first so `SlotTracker` still sees the real subject at a stable
        position and only the echoes churn through spare slots.
        """
        cfg = self.cfg
        if boxes.shape[0]:
            self._observe(boxes)
        if not cfg.active():
            return boxes

        strobe = int(cfg.strobe)
        # strobe == 1 would blank every frame; treat it as off rather than
        # handing the user a black screen that looks like a crash.
        if strobe > 1 and frame_index % strobe == 0:
            return EMPTY

        base = self._slitscan(boxes, history) if int(cfg.slitscan) > 1 else boxes
        layers = [base]
        owned = base is not boxes      # may we write into the array in place?

        trails = min(int(cfg.trails), MAX_TRAILS, len(history))
        for k in range(1, trails + 1):
            src = history[-k]
            if src.shape[0]:
                layers.append(self._fade(src, (trails - k + 1) / (trails + 1.0),
                                         frame_index + k))

        echo = int(cfg.echo_offset)
        ghost = float(cfg.ghost)
        if echo > 0 and ghost > 0.0 and len(history) >= echo:
            src = history[-echo]
            if src.shape[0]:
                layers.append(self._fade(src, min(1.0, ghost), frame_index - echo))

        if len(layers) > 1:
            out, owned = np.concatenate(layers, 0), True
        else:
            out = base
        if int(cfg.jitter) > 0 and out.shape[0]:
            out, owned = self._jitter(out, frame_index), True
        # Budget before clipping: neither concatenation nor jitter reorders, so
        # the first `base.shape[0]` rows are still the live frame here, while
        # clipping can drop boxes and shift that boundary.
        out = self._budget(out, base.shape[0])
        out = self._clip(out, owned)
        if int(cfg.shuffle) > 0:
            out = self._shuffle(out, frame_index)
        return out

    def speed_scale(self, t: float) -> float:
        """Playback-rate multiplier at time `t` seconds; exactly 1.0 when off."""
        depth = float(self.cfg.time_warp)
        period = float(self.cfg.warp_period)
        if depth == 0.0 or period <= 0.0:
            return 1.0
        scale = 1.0 + depth * math.sin(2.0 * math.pi * t / period)
        # A depth above 1 would otherwise ask for a negative or stalled rate.
        return min(MAX_SPEED, max(MIN_SPEED, scale))

    # ---- effects ------------------------------------------------------
    def _slitscan(self, boxes: np.ndarray, history: list[np.ndarray]) -> np.ndarray:
        """Row band `i` comes from the frame `i` steps back -- classic slit-scan.

        Bands are cut out of the older box arrays by clipping the y extent,
        which is why this stays vectorised: no rasterising, just a clamp and a
        mask per band.
        """
        gh = self._grid_h
        if gh <= 0:
            return boxes
        bands = min(int(self.cfg.slitscan), MAX_BANDS, gh)
        edges = np.linspace(0, gh, bands + 1).astype(np.int32)
        parts: list[np.ndarray] = []
        for i in range(bands):
            j = len(history) - i
            if i == 0:
                src = boxes
            elif j >= 0:
                src = history[j]
            elif history:
                src = history[0]     # early frames: hold the oldest we have
            else:
                src = boxes
            if src.shape[0] == 0:
                continue
            y0, y1 = int(edges[i]), int(edges[i + 1])
            top = np.clip(src[:, Y], y0, y1)
            bot = np.clip(src[:, Y] + src[:, H], y0, y1)
            keep = bot > top
            if not keep.any():
                continue
            part = src[keep]         # fancy indexing copies: history stays intact
            part[:, Y] = top[keep]
            part[:, H] = (bot - top)[keep]
            parts.append(part)
        if not parts:
            return EMPTY
        return np.concatenate(parts, 0) if len(parts) > 1 else parts[0]

    def _fade(self, src: np.ndarray, weight: float, seed: int) -> np.ndarray:
        """A dimmed copy of `src`, `weight` in 0..1 (1.0 = unchanged)."""
        if self._levels > 1:
            out = src.copy()
            # Scale the brightness ordinal rather than subtracting a fixed step,
            # so the fade spans whatever palette depth the user actually chose.
            out[:, L] = np.maximum(
                0, np.rint((src[:, L] + 1) * weight).astype(np.int32) - 1)
            return out
        # One level means one colour, so a dimmer copy is not representable:
        # thin the layer instead -- fewer windows reads as fainter from a
        # distance, and it keeps the box budget under control for free.
        keep = self._noise_at(seed, src.shape[0], 0) < (weight * 2.0 - 1.0)
        return src[keep]

    def _jitter(self, boxes: np.ndarray, frame_index: int) -> np.ndarray:
        """Per-box offset of up to `cfg.jitter` **grid cells**.

        The config calls it pixels, but boxes are grid units until the render
        boundary (hard rule 5), and the renderer scales one cell to many
        pixels -- so 1 here is already a visible shove.
        """
        amp = float(int(self.cfg.jitter))
        n = boxes.shape[0]
        dx = np.rint(self._noise_at(frame_index, n, 0) * amp)
        dy = np.rint(self._noise_at(frame_index * 3, n, 1) * amp)
        out = boxes.copy()
        out[:, X] += dx.astype(np.int32)
        out[:, Y] += dy.astype(np.int32)
        return out

    def _shuffle(self, boxes: np.ndarray, frame_index: int) -> np.ndarray:
        """Scramble the order of `cfg.shuffle` boxes; >= the count is a full shuffle.

        This moves window identity, not pixels: the drawn rectangles are the
        same set either way. `SlotTracker` matches on box centres and only
        falls back to source order for boxes that matched nothing, so a
        permutation re-deals the slots of exactly those -- measured at roughly
        one slot in six per frame, which is the churn the effect is after.
        """
        n = boxes.shape[0]
        if n < 2:
            return boxes
        rng = np.random.default_rng((frame_index * _HASH) & 0xFFFFFFFF)
        k = min(n, max(2, int(self.cfg.shuffle)))
        if k >= n:
            return boxes[rng.permutation(n)]
        # Permute a subset of distinct positions among themselves: swapping
        # random index pairs can hit the same slot twice and silently drop a box.
        sel = rng.choice(n, size=k, replace=False)
        order = np.arange(n)
        order[sel] = rng.permutation(sel)
        return boxes[order]

    # ---- helpers ------------------------------------------------------
    def _noise_at(self, seed: int, n: int, col: int) -> np.ndarray:
        """`n` stable pseudo-random values in [-1, 1) for this seed."""
        base = (int(seed) * _HASH) % _NOISE_SIZE
        return self._noise[col].take(base + np.arange(n), mode="wrap")

    def _observe(self, boxes: np.ndarray) -> None:
        """Learn the grid extent and palette depth from the frames we are given."""
        if not self._fixed_grid:
            self._grid_w = max(self._grid_w, int((boxes[:, X] + boxes[:, W]).max()))
            self._grid_h = max(self._grid_h, int((boxes[:, Y] + boxes[:, H]).max()))
        if not self._fixed_levels:
            self._levels = max(self._levels, int(boxes[:, L].max()) + 1)

    def _clip(self, boxes: np.ndarray, owned: bool) -> np.ndarray:
        """Clamp to the grid and drop anything that clipped away to nothing.

        `owned` says whether `boxes` is a private array; the caller's own frame
        must never be edited in place -- the pipeline keeps it as history.
        """
        if boxes.shape[0] == 0:
            return boxes
        # The grid extent is still unknown until a non-empty frame is observed,
        # but trail and echo layers are read out of `history`, so this frame can
        # emit geometry from a grid that was never observed -- after `reset`, or
        # while the live frame is blank. Bounding only the far edge away still
        # keeps windows off negative coordinates and drops empty rectangles.
        gw = self._grid_w if self._grid_w > 0 else _UNBOUNDED
        gh = self._grid_h if self._grid_h > 0 else _UNBOUNDED
        x0 = np.clip(boxes[:, X], 0, gw)
        y0 = np.clip(boxes[:, Y], 0, gh)
        x1 = np.clip(boxes[:, X] + boxes[:, W], 0, gw)
        y1 = np.clip(boxes[:, Y] + boxes[:, H], 0, gh)
        keep = (x1 > x0) & (y1 > y0)
        if keep.all():
            out = boxes if owned else boxes.copy()
        else:
            out = boxes[keep]
            x0, y0, x1, y1 = x0[keep], y0[keep], x1[keep], y1[keep]
        out[:, X], out[:, Y] = x0, y0
        out[:, W], out[:, H] = x1 - x0, y1 - y0
        return out

    def _budget(self, boxes: np.ndarray, protect: int) -> np.ndarray:
        """Enforce `max_boxes`, sacrificing echo layers before the live frame."""
        n = boxes.shape[0]
        if n <= self.max_boxes:
            return boxes
        if protect >= self.max_boxes:
            head = boxes[:protect]
            if protect == self.max_boxes:
                return head
            area = head[:, W] * head[:, H]
            return head[np.argpartition(-area, self.max_boxes)[:self.max_boxes]]
        extra = boxes[protect:]
        room = self.max_boxes - protect
        area = extra[:, W] * extra[:, H]
        return np.concatenate(
            [boxes[:protect], extra[np.argpartition(-area, room)[:room]]], 0)
