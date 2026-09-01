"""Frame supply: where the renderer's boxes come from.

The prototype decoded and boxed the *entire* video before showing anything.
At its own default grid that is around three minutes of frozen UI for a
three-minute clip, and up to thirteen at the settings its README recommends.

`StreamSource` replaces that with a producer thread that stays a second or
two ahead of the playhead, so playback starts almost immediately and memory
stays bounded no matter how long the video is. `BakeSource` reads a cached
`.tlp` file instead, which is instant and randomly seekable -- that is what
makes smooth reverse and scrubbing possible.

Both satisfy the same tiny interface, so the player does not care which it
has.
"""
from __future__ import annotations

import threading
import time
from abc import ABC, abstractmethod

import numpy as np

from ..core import boxgen
from ..media.decoder import FrameDecoder

EMPTY = boxgen.EMPTY


class FrameSource(ABC):
    """Random-ish access to a video's box arrays."""

    frame_count: int = 0
    fps: float = 24.0
    grid_w: int = 0
    grid_h: int = 0

    @abstractmethod
    def get(self, index: int, timeout: float = 0.0) -> np.ndarray | None:
        """Boxes for `index`, or None if not ready yet (caller should stall)."""

    def hint(self, index: int, reverse: bool = False) -> None:
        """Tell the source where the playhead is, so it can prefetch."""

    @property
    def buffered(self) -> int:
        return 0

    @property
    def decode_fps(self) -> float:
        return 0.0

    @property
    def complete(self) -> bool:
        return True

    def close(self) -> None:
        pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


class BakeSource(FrameSource):
    """A pre-baked .tlp file: O(1) access to any frame, no decoding at all."""

    def __init__(self, reader) -> None:
        self.reader = reader
        h = reader.header
        self.frame_count = h.frame_count
        self.fps = h.fps
        self.grid_w = h.grid_w
        self.grid_h = h.grid_h
        self.levels = h.levels

    def get(self, index: int, timeout: float = 0.0) -> np.ndarray | None:
        if index < 0 or index >= self.frame_count:
            return EMPTY
        try:
            return self.reader[index]
        except Exception:
            return EMPTY

    @property
    def buffered(self) -> int:
        return self.frame_count

    def close(self) -> None:
        try:
            self.reader.close()
        except Exception:
            pass


class StreamSource(FrameSource):
    """Decode + box on a producer thread, a bounded window at a time."""

    def __init__(self, path: str, video_cfg, fps: float, frame_count: int,
                 depth: int = 96, keep_back: int = 48) -> None:
        self.path = path
        self.cfg = video_cfg
        self.fps = max(0.1, fps)
        self.frame_count = frame_count
        self.grid_w = video_cfg.grid_w
        self.grid_h = video_cfg.grid_h
        self.depth = max(8, int(depth))
        self.keep_back = max(0, int(keep_back))

        self._cache: dict[int, np.ndarray] = {}
        self._lock = threading.Lock()
        self._wake = threading.Condition(self._lock)
        self._playhead = 0
        self._reverse = False
        self._seek_to: int | None = 0
        self._eof_at: int | None = None
        self._stop = threading.Event()
        self._error: Exception | None = None
        self._decoded = 0
        self._last_decoded = -1
        self._decode_t0 = time.perf_counter()
        self._decode_fps = 0.0
        self._thread = threading.Thread(target=self._run, name="timeleap-decode",
                                        daemon=True)

    # ---- lifecycle ---------------------------------------------------
    def start(self, at_frame: int = 0) -> None:
        with self._lock:
            self._seek_to = max(0, int(at_frame))
            self._playhead = self._seek_to
        self._thread.start()

    def close(self) -> None:
        self._stop.set()
        with self._wake:
            self._wake.notify_all()
        if self._thread.is_alive():
            self._thread.join(timeout=3.0)

    # ---- reader side -------------------------------------------------
    def get(self, index: int, timeout: float = 0.0) -> np.ndarray | None:
        if index < 0:
            return EMPTY
        if self.frame_count and index >= self.frame_count:
            # Past the known end. Without this, a request that lands after the
            # last frame but before the producer has noticed EOF queues a seek
            # to a position ffmpeg will never reach, restarting it for nothing.
            return EMPTY
        if self._eof_at is not None and index >= self._eof_at:
            return EMPTY
        deadline = time.perf_counter() + max(0.0, timeout)
        with self._wake:
            # Asking for a frame *is* the authoritative statement of where the
            # playhead is. Relying on a separate hint() call made the producer's
            # backpressure window and the reader's position drift apart, and the
            # producer would stall `depth` frames in and never wake again.
            if index > self._playhead:
                self._playhead = index
                self._wake.notify_all()
            while True:
                hit = self._cache.get(index)
                if hit is not None:
                    return hit
                if self._error is not None:
                    raise self._error
                if self._stop.is_set():
                    return EMPTY
                # Too far outside the window to ever arrive by streaming.
                if self._needs_seek_locked(index):
                    self._seek_to = self._seek_target_locked(index)
                    self._wake.notify_all()
                remaining = deadline - time.perf_counter()
                if remaining <= 0:
                    return None
                self._wake.wait(min(remaining, 0.05))

    def _needs_seek_locked(self, index: int) -> bool:
        """Can the producer ever reach `index` on its own?

        It only ever moves forward, so anything below the buffer needs a
        restart -- there is no slack to allow for. And once it has parked at
        EOF it will not produce anything at all without one.
        """
        if self._seek_to is not None:
            return False                    # a seek is already queued
        parked = self._eof_at is not None
        if not self._cache:
            return parked
        lo, hi = min(self._cache), max(self._cache)
        if index < lo:
            return True
        if index > hi:
            return parked or index > hi + self.depth
        return False

    def _seek_target_locked(self, index: int) -> int:
        """Where to restart ffmpeg for a request at `index`.

        Playing backwards, restarting exactly at `index` would buy a single
        frame and then need another restart -- roughly 50 ms of ffmpeg startup
        per frame. Landing a window *before* the target instead fills the
        history buffer in one go, so a reverse scan costs one restart per
        `keep_back` frames rather than one per frame.
        """
        if self._reverse:
            return max(0, index - self.keep_back + 1)
        return index

    def hint(self, index: int, reverse: bool = False) -> None:
        with self._wake:
            self._playhead = int(index)
            self._reverse = bool(reverse)
            self._evict_locked()
            self._wake.notify_all()

    @property
    def buffered(self) -> int:
        with self._lock:
            if not self._cache:
                return 0
            ahead = [i for i in self._cache if i >= self._playhead]
            return len(ahead)

    @property
    def decode_fps(self) -> float:
        return self._decode_fps

    @property
    def complete(self) -> bool:
        return self._eof_at is not None

    # ---- producer side -----------------------------------------------
    def _evict_locked(self) -> None:
        """Drop frames the playhead has left behind.

        Reverse playback keeps history instead of look-ahead, which is what
        lets short rewinds play smoothly without re-seeking ffmpeg.
        """
        if len(self._cache) <= self.depth + self.keep_back:
            return
        head = self._playhead
        if self._reverse:
            lo, hi = head - self.depth, head + self.keep_back
        else:
            lo, hi = head - self.keep_back, head + self.depth * 2
        for k in [k for k in self._cache if k < lo or k > hi]:
            self._cache.pop(k, None)

    def _room_locked(self) -> bool:
        ahead = sum(1 for i in self._cache if i >= self._playhead)
        return ahead < self.depth

    def _run(self) -> None:
        decoder: FrameDecoder | None = None
        stream = None
        try:
            while not self._stop.is_set():
                with self._wake:
                    seek = self._seek_to
                    self._seek_to = None
                if seek is not None:
                    if decoder is not None:
                        decoder.close()
                    decoder = FrameDecoder(self.path, self.grid_w, self.grid_h,
                                           start_frame=seek, fps=self.fps)
                    stream = iter(decoder)
                    with self._wake:
                        self._last_decoded = seek - 1
                        # A seek invalidates look-ahead but not recent history.
                        self._cache = {k: v for k, v in self._cache.items()
                                       if abs(k - seek) <= self.keep_back}
                        self._eof_at = None
                        self._wake.notify_all()

                with self._wake:
                    while (not self._room_locked() and self._seek_to is None
                           and not self._stop.is_set()):
                        self._wake.wait(0.05)
                    if self._stop.is_set():
                        break
                    if self._seek_to is not None:
                        continue

                try:
                    index, gray = next(stream)
                except StopIteration:
                    with self._wake:
                        # The last index the decoder actually produced -- not
                        # max(cache), which is only the end of the sliding
                        # window and moves as frames are evicted.
                        self._eof_at = self._last_decoded + 1
                        self._wake.notify_all()
                    # Idle until someone seeks us somewhere new.
                    with self._wake:
                        while self._seek_to is None and not self._stop.is_set():
                            self._wake.wait(0.1)
                    continue

                boxes = boxgen.frame_to_boxes(gray, self.cfg)
                with self._wake:
                    self._cache[index] = boxes
                    self._last_decoded = max(self._last_decoded, index)
                    self._evict_locked()
                    self._wake.notify_all()

                self._decoded += 1
                dt = time.perf_counter() - self._decode_t0
                if dt >= 0.5:
                    self._decode_fps = self._decoded / dt
                    self._decoded = 0
                    self._decode_t0 = time.perf_counter()
        except Exception as exc:            # surfaced to the reader thread
            self._error = exc
            with self._wake:
                self._wake.notify_all()
        finally:
            if decoder is not None:
                try:
                    decoder.close()
                except Exception:
                    pass


def bake_frames(path: str, video_cfg, fps: float, grid_w: int, grid_h: int):
    """Generator of box arrays for the whole file -- used by the bake command."""
    with FrameDecoder(path, grid_w, grid_h, fps=fps) as dec:
        for _, gray in dec:
            yield boxgen.frame_to_boxes(gray, video_cfg)
