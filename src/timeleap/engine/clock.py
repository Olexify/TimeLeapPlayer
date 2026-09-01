"""Frame scheduling.

The prototype's playback loop was `time.sleep(delay - work_done)` after every
frame. Two things are wrong with that. It accumulates error -- every frame's
rounding is added to the next one's start, so playback drifts steadily out of
sync with the audio. And on Windows the default timer resolution is ~15.6 ms,
so at 24 fps (41.7 ms per frame) a sleep can overshoot by more than a third
of a frame, producing visible stutter even when there is CPU to spare.

`FrameClock` fixes both: deadlines are absolute offsets from a fixed anchor
(so error never accumulates), and the process asks for 1 ms timer resolution
and finishes the last millisecond with a short spin.

When an audio track is playing it becomes the master clock instead: audio
hardware cannot be told to wait, so the video follows it.
"""
from __future__ import annotations

import time

from ..render import win32 as w

_period_refs = 0


def begin_high_resolution() -> None:
    """Ask Windows for a 1 ms scheduler tick. Reference counted."""
    global _period_refs
    if w.IS_WINDOWS and _period_refs == 0:
        try:
            w.timeBeginPeriod(1)
        except Exception:
            pass
    _period_refs += 1


def end_high_resolution() -> None:
    """Release one request. An unbalanced extra call must do nothing.

    `timeBeginPeriod`/`timeEndPeriod` are reference counted by the OS across
    the whole system, so releasing more times than we requested would lower
    the timer resolution other processes are relying on -- a bug that shows up
    as stutter in unrelated applications, not in this one.
    """
    global _period_refs
    if _period_refs == 0:
        return
    _period_refs -= 1
    if w.IS_WINDOWS and _period_refs == 0:
        try:
            w.timeEndPeriod(1)
        except Exception:
            pass


def precise_sleep(seconds: float, spin: float = 0.0015) -> None:
    """Sleep, then spin the last ~1.5 ms for sub-millisecond accuracy."""
    if seconds <= 0:
        return
    deadline = time.perf_counter() + seconds
    coarse = seconds - spin
    if coarse > 0:
        time.sleep(coarse)
    while time.perf_counter() < deadline:
        pass


class FrameClock:
    """Maps wall time to a media frame index."""

    def __init__(self, fps: float = 24.0, speed: float = 1.0) -> None:
        self.fps = max(0.1, fps)
        self.speed = max(0.01, speed)
        self._anchor_time = time.perf_counter()
        self._anchor_frame = 0.0
        self._paused_at: float | None = None
        self._warp_accum = 0.0

    # ---- anchoring ---------------------------------------------------
    def reset(self, frame: float = 0.0) -> None:
        self._anchor_time = time.perf_counter()
        self._anchor_frame = float(frame)
        self._warp_accum = 0.0
        if self._paused_at is not None:
            self._paused_at = self._anchor_time

    def rebase(self) -> None:
        """Re-anchor at the current position.

        Called whenever fps or speed changes, so the change takes effect from
        now instead of retroactively rewriting where the playhead should be.

        The clock is sampled once and reused for both the position and the new
        anchor. Reading the time twice -- once to compute where we are, again
        to anchor -- discards whatever elapsed in between, so every speed
        change nudged the playhead backwards by a fraction of a frame.
        """
        now = self._now()
        dt = now - self._anchor_time
        pos = self._anchor_frame + dt * self.fps * self.speed + self._warp_accum
        self._anchor_time = now
        self._anchor_frame = pos
        self._warp_accum = 0.0

    def set_speed(self, speed: float) -> None:
        speed = max(0.01, min(16.0, float(speed)))
        if abs(speed - self.speed) > 1e-9:
            self.rebase()
            self.speed = speed

    def set_fps(self, fps: float) -> None:
        fps = max(0.1, float(fps))
        if abs(fps - self.fps) > 1e-9:
            self.rebase()
            self.fps = fps

    # ---- state -------------------------------------------------------
    def pause(self) -> None:
        if self._paused_at is None:
            self._paused_at = time.perf_counter()

    def resume(self) -> None:
        if self._paused_at is not None:
            # Shift the anchor forward by the paused duration so the playhead
            # picks up exactly where it stopped.
            self._anchor_time += time.perf_counter() - self._paused_at
            self._paused_at = None

    @property
    def paused(self) -> bool:
        return self._paused_at is not None

    # ---- queries -----------------------------------------------------
    def _now(self) -> float:
        return self._paused_at if self._paused_at is not None else time.perf_counter()

    def position_frames(self) -> float:
        """Fractional frame index the playhead is currently at."""
        dt = self._now() - self._anchor_time
        return self._anchor_frame + dt * self.fps * self.speed + self._warp_accum

    def position_seconds(self) -> float:
        return self.position_frames() / self.fps

    def deadline_for(self, frame: float) -> float:
        """Wall-clock time at which `frame` should be on screen."""
        return (self._anchor_time
                + (frame - self._anchor_frame - self._warp_accum)
                / (self.fps * self.speed))

    def advance_warp(self, extra_frames: float) -> None:
        """Nudge the playhead without re-anchoring -- used by time-warp."""
        self._warp_accum += extra_frames

    def wait_for(self, frame: float, max_wait: float = 0.25) -> float:
        """Block until `frame` is due. Returns seconds we were early by."""
        slack = self.deadline_for(frame) - time.perf_counter()
        if slack > 0:
            precise_sleep(min(slack, max_wait))
        return slack


class SyncedClock:
    """Wraps a FrameClock and lets an audio track override it.

    Audio hardware runs on its own crystal and cannot be nudged, so once a
    track is playing it defines the timeline and the video chases it. Small
    corrections are smoothed, and a large gap (a seek, or a long stall) snaps
    rather than crawling back over several seconds.
    """

    SNAP_SECONDS = 0.35

    def __init__(self, fps: float = 24.0, speed: float = 1.0) -> None:
        self.clock = FrameClock(fps, speed)
        self.audio = None
        self.enabled = True
        self.drift = 0.0

    def attach_audio(self, track) -> None:
        self.audio = track

    def detach_audio(self) -> None:
        self.audio = None
        self.drift = 0.0

    def _audio_frames(self) -> float | None:
        track = self.audio
        if track is None or not self.enabled or not getattr(track, "playing", False):
            return None
        pos = getattr(track, "position", None)
        if pos is None or pos < 0:
            return None
        return pos * self.clock.fps

    def position_frames(self) -> float:
        a = self._audio_frames()
        v = self.clock.position_frames()
        if a is None:
            self.drift = 0.0
            return v
        self.drift = (v - a) / self.clock.fps
        if abs(self.drift) > self.SNAP_SECONDS:
            self.clock.reset(a)          # seek or stall: jump, do not crawl
            return a
        return a

    def position_seconds(self) -> float:
        return self.position_frames() / self.clock.fps

    # Convenience pass-throughs so callers can hold one object.
    def reset(self, frame: float = 0.0) -> None:
        self.clock.reset(frame)

    def pause(self) -> None:
        self.clock.pause()

    def resume(self) -> None:
        self.clock.resume()

    @property
    def paused(self) -> bool:
        return self.clock.paused

    def set_speed(self, v: float) -> None:
        self.clock.set_speed(v)

    def set_fps(self, v: float) -> None:
        self.clock.set_fps(v)

    def wait_for(self, frame: float, max_wait: float = 0.25) -> float:
        return self.clock.wait_for(frame, max_wait)
