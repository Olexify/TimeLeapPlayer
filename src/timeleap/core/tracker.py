"""Temporally stable box -> window-slot assignment.

The rectangles that come out of `boxgen` are unordered and their count
changes every frame, so the obvious mapping (slot i gets box i, or slots
handed out largest-first) reshuffles on every frame. The window that drew
the character's head becomes the window that draws a foot, and because each
slot is a real HWND being moved by `SetWindowPos`, the result is the
location jitter the original bad_apple_virus README complains about --
plus a pile of avoidable `DeferWindowPos` calls, since frame diffing only
skips a slot whose rectangle is unchanged.

So: a box whose centre is within `radius` grid cells of a slot's previous
centre keeps that slot. Matching is greedy nearest-centre, resolved by
repeated mutual-nearest rounds (each round provably accepts at least the
globally closest surviving pair, so this is the greedy result), and
candidates come from a coarse bucket grid whose cell size equals the match
radius -- only the 3x3 neighbourhood of buckets can hold a match, so the
work is O(N) in the number of boxes and no N x N distance matrix is ever
built. At 260 boxes an N x N matrix would be 68k distances per frame; the
bucket grid finds ~380 candidates instead.

The exception is duplicate rectangles, which `effects` trails and echo emit
by design: identical centres mean identical distances, and greedy can then
only settle one tie per round. Measured cost at the most the effect stack
can produce (`trails` = 16 on static content, 558 boxes into 260 slots) is
3.1 ms against 0.2 ms for an ordinary frame -- inside a frame budget, but
the one input shape where this is not O(N).

Slots keep their last centre even on frames where they draw nothing, so
intermittent detail (an eye that blinks, a limb behind an occluder) comes
back to the same window instead of a random one.
"""
from __future__ import annotations

import numpy as np

DEFAULT_RADIUS = 3.0        # grid cells; a box may drift this far and keep its slot

# Bucket coordinates are folded into one int64 key. The offset keeps the key
# injective for negative cells, which effects like `jitter` can produce.
_ORIGIN = 1 << 20
_SPAN = 1 << 21

_EMPTY_I64 = np.empty(0, np.int64)
_EMPTY_F32 = np.empty(0, np.float32)


class SlotTracker:
    """Maps boxes to a fixed pool of `capacity` window slots, stably."""

    def __init__(self, capacity: int, radius: float = DEFAULT_RADIUS) -> None:
        self.capacity = max(0, int(capacity))
        self.radius = max(0.5, float(radius))
        self._cx = np.zeros(self.capacity, np.float32)
        self._cy = np.zeros(self.capacity, np.float32)
        self._known = np.zeros(self.capacity, bool)

    def reset(self) -> None:
        """Forget every slot's history -- call on seek, so a jump cut does not
        drag matches across unrelated content."""
        self._known[:] = False
        self._cx[:] = 0.0
        self._cy[:] = 0.0

    def assign(self, boxes: np.ndarray) -> np.ndarray:
        """boxes (N,5) -> (N,) int32 slot index, each unique, < capacity.

        Returns -1 for any box that got no slot, which happens only when
        N > capacity: the `capacity` largest by area win (ties broken by
        source order, so the same frame always truncates identically) and
        the rest are dropped. The array is always length N so the caller can
        mask it against its own box array.
        """
        arr = np.asarray(boxes)
        if arr.ndim != 2:
            return np.empty(0, np.int32)
        n = int(arr.shape[0])
        out = np.full(n, -1, np.int32)
        # A too-narrow array still yields one entry per row: the promise above
        # is that the result can be masked against the caller's box array, and
        # returning a short array turns a bad frame into an IndexError.
        if n == 0 or arr.shape[1] < 4 or self.capacity == 0:
            return out

        cx = arr[:, 0].astype(np.float32) + arr[:, 2].astype(np.float32) * 0.5
        cy = arr[:, 1].astype(np.float32) + arr[:, 3].astype(np.float32) * 0.5

        if n > self.capacity:
            area = arr[:, 2].astype(np.int64) * arr[:, 3].astype(np.int64)
            keep = np.sort(np.argsort(-area, kind="stable")[:self.capacity])
        else:
            keep = np.arange(n, dtype=np.int64)
        k = int(keep.size)
        kcx, kcy = cx[keep], cy[keep]

        slot = np.full(k, -1, np.int64)
        taken = np.zeros(self.capacity, bool)

        known = np.nonzero(self._known)[0]
        if known.size:
            pb, ps, pd = self._candidates(kcx, kcy, known)
            if pb.size:
                self._greedy(pb, ps, pd, slot, taken)

        # Whatever did not match an old position takes the lowest free slots.
        need = np.nonzero(slot < 0)[0]
        if need.size:
            free = np.nonzero(~taken)[0]
            m = min(int(need.size), int(free.size))
            slot[need[:m]] = free[:m]
            taken[free[:m]] = True

        got = slot >= 0
        sid = slot[got]
        self._cx[sid] = kcx[got]
        self._cy[sid] = kcy[got]
        self._known[sid] = True
        out[keep[got]] = sid.astype(np.int32)
        return out

    # ---- internals ---------------------------------------------------
    def _candidates(self, cx: np.ndarray, cy: np.ndarray,
                    known: np.ndarray) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
        """(box, slot, distance^2) triples for every pair worth considering."""
        cell = self.radius
        skey = self._keys(self._cx[known], self._cy[known])
        order = np.argsort(skey, kind="stable")
        skey, sid = skey[order], known[order].astype(np.int64)

        bx = np.floor(cx / cell).astype(np.int64)
        by = np.floor(cy / cell).astype(np.int64)
        dy, dx = np.divmod(np.arange(9, dtype=np.int64), 3)
        qkey = (((by[:, None] + dy - 1) + _ORIGIN) * _SPAN
                + ((bx[:, None] + dx - 1) + _ORIGIN)).ravel()
        qbox = np.repeat(np.arange(cx.size, dtype=np.int64), 9)

        lo = np.searchsorted(skey, qkey, "left")
        hi = np.searchsorted(skey, qkey, "right")
        cnt = hi - lo
        total = int(cnt.sum())
        if total == 0:
            return _EMPTY_I64, _EMPTY_I64, _EMPTY_F32

        # Ragged expansion of the [lo, hi) ranges without a Python loop.
        ends = np.cumsum(cnt)
        idx = np.repeat(lo, cnt) + (np.arange(total) - np.repeat(ends - cnt, cnt))
        pb = np.repeat(qbox, cnt)
        ps = sid[idx]

        ddx = cx[pb] - self._cx[ps]
        ddy = cy[pb] - self._cy[ps]
        d2 = ddx * ddx + ddy * ddy
        near = d2 <= cell * cell
        return pb[near], ps[near], d2[near]

    def _keys(self, cx: np.ndarray, cy: np.ndarray) -> np.ndarray:
        gx = np.floor(cx / self.radius).astype(np.int64)
        gy = np.floor(cy / self.radius).astype(np.int64)
        return (gy + _ORIGIN) * _SPAN + (gx + _ORIGIN)

    @staticmethod
    def _greedy(pb: np.ndarray, ps: np.ndarray, pd: np.ndarray,
                slot: np.ndarray, taken: np.ndarray) -> None:
        """Accept pairs closest-first, one box per slot. Fills `slot`/`taken`."""
        # Sorted by distance once; ties break on slot then box so a given
        # frame always produces the same assignment.
        order = np.lexsort((pb, ps, pd))
        pb, ps = pb[order], ps[order]

        matched = np.zeros(slot.size, bool)
        alive = np.ones(pb.size, bool)
        while True:
            pos = np.nonzero(alive)[0]
            if pos.size == 0:
                break
            # Scatter in reverse so the surviving write is the lowest position,
            # i.e. the nearest partner, for each box and for each slot.
            rev = pos[::-1]
            best_b = np.full(slot.size, -1, np.int64)
            best_b[pb[rev]] = rev
            best_s = np.full(taken.size, -1, np.int64)
            best_s[ps[rev]] = rev

            cand = best_b[best_b >= 0]
            mutual = cand[best_s[ps[cand]] == cand]
            if mutual.size == 0:      # unreachable: the closest pair is mutual
                break
            mb, ms = pb[mutual], ps[mutual]
            slot[mb] = ms
            matched[mb] = True
            taken[ms] = True
            alive &= ~(matched[pb] | taken[ps])
