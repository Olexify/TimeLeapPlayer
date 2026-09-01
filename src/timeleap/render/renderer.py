"""Boxes -> windows on screen, in one batched transaction per frame.

Three things make this fast enough to hold 30 fps with a couple of hundred
windows:

* `DeferWindowPos` batching. The original project's README puts it best --
  batching is what takes "even the most naive of projects from 1 fps to
  15 fps", because each loose `SetWindowPos` is its own trip through the
  window manager.
* Diffing. Most rectangles are identical from one frame to the next; those
  windows are skipped entirely rather than re-submitted.
* Stable slots. A box keeps the same HWND across frames (see
  `core.tracker`), which both maximises the diff hit-rate and removes the
  location jitter the original repo calls out, where windows were assigned
  largest-to-smallest and swapped places whenever the sort order changed.
"""
from __future__ import annotations

import time
from dataclasses import dataclass

import numpy as np

from ..core import boxgen
from ..core.tracker import SlotTracker
from . import win32 as w
from .window_pool import Blackout, WindowPool, ex_style_for


@dataclass
class RenderStats:
    boxes: int = 0
    moved: int = 0
    shown: int = 0
    hidden: int = 0
    skipped: int = 0
    batch_ms: float = 0.0

    @property
    def active(self) -> int:
        return self.moved + self.shown + self.skipped


class FrameRenderer:
    """Owns the window pool. Create and destroy on the render thread."""

    def __init__(self) -> None:
        self.pool = WindowPool()
        self.blackout = Blackout()
        self._trackers: list[SlotTracker] = []
        self._active: list[set[int]] = []
        self._target = (0, 0, 1920, 1080)
        self._colors: list[int] = []
        self._cfg = None
        self._levels = 1
        self._grid = (0, 0)
        self._xmap: np.ndarray | None = None
        self._ymap: np.ndarray | None = None

    # ---- configuration -----------------------------------------------
    def configure(self, cfg, colors: list[int], grid_w: int, grid_h: int,
                  aspect: float) -> None:
        """Apply a RenderConfig + palette. Safe to call between frames."""
        self._cfg = cfg
        target = w.target_rect(cfg.monitor, cfg.region)
        target = w.fit_rect(target, aspect, cfg.fit)

        rebuilt = self.pool.configure(
            colors, topmost=cfg.topmost, click_through=cfg.click_through,
            no_activate=cfg.no_activate)
        invalidated = (rebuilt or target != self._target
                       or (grid_w, grid_h) != self._grid)
        if invalidated and not rebuilt and self.pool.layers:
            # Moving to a different monitor or grid orphans the currently
            # visible windows: forgetting which slots were active would leave
            # them stranded at their old coordinates with nothing tracking
            # them. Hide them first, while we still know where they are.
            # (A rebuild is safe already -- those windows were destroyed.)
            self.clear()
        if invalidated:
            self._active = [set() for _ in colors]
            self._trackers = [SlotTracker(cfg_capacity(cfg))
                              for _ in colors]
        self._colors = list(colors)
        self._levels = len(colors)
        self._target = target
        self._grid = (grid_w, grid_h)
        self._build_maps(grid_w, grid_h, target)

        if cfg.background_blackout:
            self.blackout.show(target, ex_style_for(
                cfg.topmost, cfg.click_through, cfg.no_activate))
        else:
            self.blackout.hide()

    def _build_maps(self, grid_w: int, grid_h: int,
                    target: tuple[int, int, int, int]) -> None:
        """Precompute grid-edge -> pixel-edge tables.

        Mapping each *edge* rather than each origin+size is what makes
        adjacent boxes tile seamlessly: box A's right edge and box B's left
        edge resolve to the identical pixel, so no hairline gaps appear
        between neighbouring windows at non-integer scales.
        """
        tx, ty, tw, th = target
        self._xmap = tx + np.round(
            np.arange(grid_w + 1) * (tw / float(grid_w))).astype(np.int32)
        self._ymap = ty + np.round(
            np.arange(grid_h + 1) * (th / float(grid_h))).astype(np.int32)

    # ---- the frame ---------------------------------------------------
    def render(self, boxes: np.ndarray) -> RenderStats:
        """Draw one frame. `boxes` is (N,5) int32 in grid units."""
        stats = RenderStats(boxes=int(boxes.shape[0]))
        if self._cfg is None or not self.pool.layers:
            return stats
        t0 = time.perf_counter()

        plans = self._plan(boxes, stats)
        total = sum(len(p[1]) for p in plans) + sum(len(p[2]) for p in plans)
        if total:
            self._commit(plans, stats)

        w.pump_messages()
        stats.batch_ms = (time.perf_counter() - t0) * 1000.0
        return stats

    def _plan(self, boxes: np.ndarray, stats: RenderStats):
        """Work out, per layer, which windows to move and which to hide."""
        cfg = self._cfg
        gap = max(0, int(cfg.gap))
        floor = max(1, int(cfg.min_window_px))
        xmap, ymap = self._xmap, self._ymap
        gw, gh = self._grid
        plans = []

        for level in range(self._levels):
            layer = self.pool.layer(level)
            sel = boxes[boxes[:, boxgen.L] == level] if boxes.shape[0] else boxes
            if sel.shape[0] == 0:
                plans.append((layer, [], list(self._active[level])))
                self._active[level] = set()
                continue

            n = min(sel.shape[0], layer.ensure(min(sel.shape[0], _MAX_PER_LAYER)))
            if n < sel.shape[0]:
                sel = sel[:n]
            slots = (self._trackers[level].assign(sel) if cfg.stable_slots
                     else np.arange(sel.shape[0], dtype=np.int32))

            # Clip to the grid so an effect that nudged a box cannot index
            # past the edge tables.
            x0 = np.clip(sel[:, boxgen.X], 0, gw)
            y0 = np.clip(sel[:, boxgen.Y], 0, gh)
            x1 = np.clip(sel[:, boxgen.X] + sel[:, boxgen.W], 0, gw)
            y1 = np.clip(sel[:, boxgen.Y] + sel[:, boxgen.H], 0, gh)
            px0, px1 = xmap[x0], xmap[x1]
            py0, py1 = ymap[y0], ymap[y1]
            pw = np.maximum(px1 - px0 - gap, floor)
            ph = np.maximum(py1 - py0 - gap, floor)

            moves = []
            used: set[int] = set()
            for i in range(sel.shape[0]):
                slot = int(slots[i])
                # SlotTracker returns -1 for boxes it could not place (more
                # boxes than slots). Without the lower bound this indexes
                # hwnds[-1] and two boxes end up fighting over the last window.
                if slot < 0 or slot >= len(layer.hwnds):
                    continue
                rect = (int(px0[i]), int(py0[i]), int(pw[i]), int(ph[i]))
                used.add(slot)
                prev = layer.rects[slot]
                if cfg.diff_frames and prev == rect:
                    stats.skipped += 1          # unchanged: do not touch it
                    continue
                moves.append((slot, rect, prev is None))
            hides = list(self._active[level] - used)
            self._active[level] = used
            plans.append((layer, moves, hides))
        return plans

    def _commit(self, plans, stats: RenderStats) -> None:
        """One DeferWindowPos transaction across every layer."""
        cfg = self._cfg
        count = sum(len(m) for _, m, _ in plans) + sum(len(h) for _, _, h in plans)
        hdwp = w.BeginDeferWindowPos(count)
        base = w.SWP_NOZORDER | w.SWP_NOACTIVATE | w.SWP_NOOWNERZORDER
        if getattr(cfg, "redraw", "accurate") == "fast":
            base |= w.SWP_NOREDRAW

        for layer, moves, hides in plans:
            for slot, rect, was_hidden in moves:
                x, y, width, height = rect
                flags = base | (w.SWP_SHOWWINDOW if was_hidden else 0)
                if hdwp:
                    hdwp = w.DeferWindowPos(hdwp, layer.hwnds[slot], None,
                                            x, y, width, height, flags)
                if not hdwp:
                    w.SetWindowPos(layer.hwnds[slot], None, x, y, width,
                                   height, flags)
                layer.rects[slot] = rect
                if was_hidden:
                    stats.shown += 1
                else:
                    stats.moved += 1
            for slot in hides:
                if hdwp:
                    hdwp = w.DeferWindowPos(hdwp, layer.hwnds[slot], None,
                                            w.PARK_X, w.PARK_Y, 1, 1,
                                            base | w.SWP_HIDEWINDOW)
                if not hdwp:
                    w.SetWindowPos(layer.hwnds[slot], None, w.PARK_X, w.PARK_Y,
                                   1, 1, base | w.SWP_HIDEWINDOW)
                layer.rects[slot] = None
                stats.hidden += 1

        if hdwp:
            w.EndDeferWindowPos(hdwp)

    # ---- teardown ----------------------------------------------------
    def clear(self) -> None:
        """Hide every window without destroying the pool."""
        if not self.pool.layers:
            return
        total = self.pool.window_count
        hdwp = w.BeginDeferWindowPos(total) if total else None
        flags = (w.SWP_NOZORDER | w.SWP_NOACTIVATE | w.SWP_HIDEWINDOW
                 | w.SWP_NOOWNERZORDER)
        for layer in self.pool.layers:
            for i, hwnd in enumerate(layer.hwnds):
                if hdwp:
                    hdwp = w.DeferWindowPos(hdwp, hwnd, None, w.PARK_X,
                                            w.PARK_Y, 1, 1, flags)
                if not hdwp:
                    w.SetWindowPos(hwnd, None, w.PARK_X, w.PARK_Y, 1, 1, flags)
                layer.rects[i] = None
        if hdwp:
            w.EndDeferWindowPos(hdwp)
        self._active = [set() for _ in self.pool.layers]
        for t in self._trackers:
            t.reset()
        self.blackout.hide()
        w.pump_messages()

    def destroy(self) -> None:
        try:
            self.clear()
        except Exception:
            pass
        self.blackout.destroy()
        self.pool.destroy()
        self._active = []
        self._trackers = []


# A hard ceiling per layer. Windows has a finite desktop heap; running out
# of it degrades the whole session, not just this process.
_MAX_PER_LAYER = 512


def cfg_capacity(cfg) -> int:
    return _MAX_PER_LAYER
