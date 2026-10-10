"""Transport: the object the UI and the CLI both drive.

One render thread owns everything Win32: it creates the window pool, pumps
the message queue and destroys the pool on the way out, because a window
belongs to the thread that made it. Every public method here is callable
from any thread and simply records intent; the render thread picks it up on
its next pass. That is what keeps the Tk UI responsive -- the prototype ran
`preprocess_video()` inline and froze for minutes.

Frame timing is pulled, not pushed: each pass asks the clock which frame is
due *now* and draws that one. A slow frame therefore drops rather than
accumulating lag, and the audio track can act as master without the video
loop needing to know.
"""
from __future__ import annotations

import threading
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Callable

import numpy as np

from ..cache import store
from ..config import HARD_WINDOW_CEILING, AppConfig
from ..core import boxgen, palette
from ..core.effects import EffectStack
from ..media import audio as audio_mod
from ..media.probe import MediaError, MediaInfo, probe
from ..render import win32 as w
from ..render.renderer import FrameRenderer
from .clock import SyncedClock, begin_high_resolution, end_high_resolution
from .pipeline import BakeSource, FrameSource, StreamSource

State = str  # "idle" | "loading" | "playing" | "paused" | "stopped" | "error"


@dataclass
class PlayerStats:
    state: State = "idle"
    frame: int = 0
    total_frames: int = 0
    position: float = 0.0
    duration: float = 0.0
    render_fps: float = 0.0
    decode_fps: float = 0.0
    boxes: int = 0
    windows: int = 0
    dropped: int = 0
    buffered: int = 0
    drift_ms: float = 0.0
    batch_ms: float = 0.0
    clamped: int = 0
    source: str = ""
    message: str = ""


@dataclass
class _Intent:
    """Cross-thread request slots. Guarded by Player._lock."""

    seek: float | None = None
    clear: bool = False
    reconfigure_render: bool = False
    restart_source: bool = False
    speed: float | None = None
    volume: int | None = None
    mute: bool | None = None


class Player:
    def __init__(self, cfg: AppConfig) -> None:
        self.cfg = cfg
        self.media: MediaInfo | None = None
        self.state: State = "idle"
        self.path: str = ""

        self.on_state: Callable[[State, str], None] | None = None
        self.on_error: Callable[[str], None] | None = None
        self.on_ready: Callable[[MediaInfo], None] | None = None
        self.on_end: Callable[[], None] | None = None

        self._lock = threading.Lock()
        self._intent = _Intent()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._stats = PlayerStats()
        self._source: FrameSource | None = None
        self._audio = None
        self._source_kind = ""

        self._playing = False
        self._base_frame = 0.0
        self._direction = 1
        self._last_boxes = boxgen.EMPTY
        self._pending_draw: int | None = None
        self._discontinuity = True
        self._history: deque[np.ndarray] = deque(maxlen=64)
        self._effects = EffectStack(cfg.effects)
        self._last_stats = None

    # ---- public transport --------------------------------------------
    def open(self, path: str) -> None:
        """Load a file and begin playing. Returns immediately."""
        self.close()
        self.path = path
        self._set_state("loading", "Probing…")
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="timeleap-render",
                                        daemon=True)
        self._thread.start()

    def play(self) -> None:
        if self.state in ("paused", "stopped"):
            self._playing = True
            self._set_state("playing", "")
        elif self.state == "playing":
            return
        self._playing = True

    def pause(self) -> None:
        if self.state == "playing":
            self._playing = False
            self._set_state("paused", "")

    def toggle(self) -> None:
        self.pause() if self.state == "playing" else self.play()

    def stop(self) -> None:
        """Stop playback and clear the screen, but keep the file loaded."""
        self._playing = False
        with self._lock:
            self._intent.seek = 0.0
            self._intent.clear = True      # Stop blanks the screen; Pause holds
        if self.state not in ("idle", "loading"):
            self._set_state("stopped", "")

    def panic(self) -> None:
        """Emergency exit. Must work even if everything else is wedged.

        Setting `_stop` ends the render loop, whose `finally` destroys the
        window pool. The state has to be published too, or the UI keeps
        reporting "playing" for a player that no longer has a thread.
        """
        self._playing = False
        self._stop.set()
        try:
            if self._audio is not None:
                self._audio.stop()
        except Exception:
            pass
        self._set_state("stopped", "Stopped by panic key")

    def seek_time(self, seconds: float) -> None:
        fps = self.media.fps if self.media else 24.0
        self.seek_frame(seconds * fps)

    def seek_fraction(self, frac: float) -> None:
        total = self._stats.total_frames or 1
        self.seek_frame(max(0.0, min(1.0, frac)) * total)

    def seek_frame(self, frame: float) -> None:
        with self._lock:
            self._intent.seek = float(frame)

    def set_speed(self, speed: float) -> None:
        self.cfg.playback.speed = float(speed)
        with self._lock:
            self._intent.speed = float(speed)

    def set_volume(self, pct: int) -> None:
        self.cfg.playback.volume = int(pct)
        with self._lock:
            self._intent.volume = int(pct)

    def set_mute(self, mute: bool) -> None:
        self.cfg.playback.mute = bool(mute)
        with self._lock:
            self._intent.mute = bool(mute)

    def set_reverse(self, reverse: bool) -> None:
        """Reverse play. Audio is silenced -- it cannot run backwards."""
        self.cfg.playback.reverse = bool(reverse)
        self._direction = -1 if reverse else 1
        self._base_frame = self._stats.frame
        self._discontinuity = True
        with self._lock:
            self._intent.seek = float(self._stats.frame)

    def refresh_render(self, effects: bool = True) -> None:
        """Apply RenderConfig / palette / effect changes without reloading.

        `effects=False` is for pure geometry changes -- a window being dragged
        fires this per mouse event, and rebuilding the effect stack each time
        would wipe trails and echo history mid-drag.
        """
        if effects:
            self._effects = EffectStack(self.cfg.effects)
        with self._lock:
            self._intent.reconfigure_render = True

    def refresh_video(self) -> None:
        """Apply VideoConfig changes. Needs the frame source rebuilt."""
        with self._lock:
            self._intent.restart_source = True

    def stats(self) -> PlayerStats:
        return self._stats

    def close(self) -> None:
        self._stop.set()
        self._playing = False
        t = self._thread
        if t is not None and t.is_alive():
            t.join(timeout=4.0)
        self._thread = None
        self.state = "idle"

    # ---- internals ---------------------------------------------------
    def _set_state(self, state: State, message: str) -> None:
        self.state = state
        self._stats.state = state
        self._stats.message = message
        if self.on_state:
            try:
                self.on_state(state, message)
            except Exception:
                pass

    def _fail(self, message: str) -> None:
        self._set_state("error", message)
        if self.on_error:
            try:
                self.on_error(message)
            except Exception:
                pass

    def _build_source(self) -> FrameSource:
        """Prefer a cached bake (instant, seekable); otherwise stream."""
        v = self.cfg.video
        assert self.media is not None
        if self.cfg.use_cache:
            reader = store.lookup(self.path, v.fingerprint())
            if reader is not None:
                self._source_kind = "cache"
                return BakeSource(reader)
        self._source_kind = "stream"
        src = StreamSource(self.path, v, self.media.fps, self.media.frame_count,
                           depth=self.cfg.playback.prefetch_frames)
        src.start(0)
        return src

    def _open_audio(self):
        pb = self.cfg.playback
        if pb.mute or not (self.media and self.media.has_audio):
            return audio_mod.open_track(self.path, "none", pb.volume)
        return audio_mod.open_track(self.path, pb.audio_backend, pb.volume)

    def _run(self) -> None:
        renderer = FrameRenderer()
        begin_high_resolution()
        try:
            try:
                self.media = probe(self.path)
            except MediaError as exc:
                self._fail(str(exc))
                return
            if self.on_ready:
                try:
                    self.on_ready(self.media)
                except Exception:
                    pass

            self._source = self._build_source()
            self._stats.total_frames = (self._source.frame_count
                                        or self.media.frame_count)
            self._stats.duration = self.media.duration
            self._stats.source = self._source_kind

            clock = SyncedClock(self._source.fps or self.media.fps,
                                self.cfg.playback.speed)
            self._audio = self._open_audio()
            clock.attach_audio(self._audio)
            clock.enabled = self.cfg.playback.av_sync

            self._configure_renderer(renderer)
            self._direction = -1 if self.cfg.playback.reverse else 1
            self._base_frame = 0.0
            clock.reset(0.0)
            self._playing = True
            self._set_state("playing", "")
            try:
                self._audio.play(0.0)
            except Exception:
                pass

            self._loop(renderer, clock)
        except Exception as exc:                       # pragma: no cover
            self._fail(f"{type(exc).__name__}: {exc}")
        finally:
            try:
                if self._audio is not None:
                    self._audio.close()
            except Exception:
                pass
            try:
                renderer.destroy()
            except Exception:
                pass
            if self._source is not None:
                self._source.close()
                self._source = None
            end_high_resolution()

    def _configure_renderer(self, renderer: FrameRenderer) -> None:
        v, r = self.cfg.video, self.cfg.render
        colors = palette.resolve(r.palette, max(1, v.levels))
        aspect = self.media.aspect if self.media else 16 / 9
        renderer.configure(r, colors, v.grid_w, v.grid_h, aspect)
        self._stats.windows = renderer.pool.window_count

    def _apply_intents(self, renderer: FrameRenderer, clock: SyncedClock) -> None:
        with self._lock:
            intent, self._intent = self._intent, _Intent()

        if intent.speed is not None:
            clock.set_speed(intent.speed)
            try:
                self._audio.set_speed(intent.speed)
            except Exception:
                pass
        if intent.volume is not None:
            try:
                self._audio.set_volume(intent.volume)
            except Exception:
                pass
        if intent.mute is not None:
            try:
                if intent.mute:
                    self._audio.stop()
                    clock.detach_audio()
                else:
                    self._audio = self._open_audio()
                    clock.attach_audio(self._audio)
                    self._audio.play(self._stats.position)
            except Exception:
                pass
        if intent.reconfigure_render:
            # Redraw the current picture under the new settings rather than
            # clearing: a paused video must follow a window drag or a palette
            # change instead of vanishing until the next frame is played.
            self._configure_renderer(renderer)
            if self._last_boxes.shape[0]:
                self._last_stats = renderer.render(self._last_boxes)
        if intent.restart_source:
            at = self._stats.frame
            if self._source is not None:
                self._source.close()
            self._source = self._build_source()
            self._stats.source = self._source_kind
            self._configure_renderer(renderer)
            renderer.clear()
            intent.seek = float(at)
        if intent.seek is not None:
            self._seek_to(intent.seek, clock)
        if intent.clear:
            # Runs after the seek: Stop rewinds *and* blanks, and must not be
            # undone by the redraw a seek would otherwise schedule.
            renderer.clear()
            self._last_boxes = boxgen.EMPTY
            self._pending_draw = None

    def _seek_to(self, frame: float, clock: SyncedClock) -> None:
        total = max(1, self._stats.total_frames)
        frame = max(0.0, min(float(frame), total - 1.0))
        self._base_frame = frame
        clock.reset(0.0)
        self._effects.reset()
        self._history.clear()
        if self._source is not None:
            self._source.hint(int(frame), self._direction < 0)
        # Scrubbing while paused must still show the frame you scrubbed to,
        # and must report it -- otherwise the seek bar and the screen both lie.
        self._pending_draw = int(frame)
        self._discontinuity = True
        try:
            self._audio.seek(frame / max(0.1, clock.clock.fps))
        except Exception:
            pass

    def _advance(self, clock: SyncedClock) -> tuple[int, bool]:
        """Which frame is due now, and whether we hit the end."""
        elapsed = clock.position_frames()
        raw = self._base_frame + self._direction * elapsed
        total = max(1, self._stats.total_frames)
        mode = self.cfg.playback.loop
        ended = False

        if raw >= total - 1 or raw < 0:
            if mode == "loop":
                raw = raw % total
                self._base_frame = raw
                clock.reset(0.0)
                self._discontinuity = True
            elif mode == "pingpong":
                self._direction *= -1
                self._base_frame = max(0.0, min(raw, total - 1.0))
                clock.reset(0.0)
                raw = self._base_frame
                self._discontinuity = True
            else:
                raw = max(0.0, min(raw, total - 1.0))
                ended = True
        return int(raw), ended

    def _loop(self, renderer: FrameRenderer, clock: SyncedClock) -> None:
        fps_window: deque[float] = deque(maxlen=60)
        last = time.perf_counter()
        prev_index = -1

        while not self._stop.is_set():
            self._apply_intents(renderer, clock)

            if not self._playing:
                if not clock.paused:
                    clock.pause()
                    try:
                        self._audio.pause()
                    except Exception:
                        pass
                if self._pending_draw is not None:
                    idx = self._pending_draw
                    self._pending_draw = None
                    if self._draw(idx, renderer):
                        prev_index = idx
                        self._stats.frame = idx
                        self._stats.position = idx / max(0.1, clock.clock.fps)
                w.pump_messages()
                time.sleep(0.02)
                continue
            if clock.paused:
                clock.resume()
                try:
                    self._audio.resume()
                except Exception:
                    pass

            index, ended = self._advance(clock)
            if ended:
                self._playing = False
                self._set_state("stopped", "End of video")
                renderer.clear()
                if self.on_end:
                    try:
                        self.on_end()
                    except Exception:
                        pass
                continue

            if index == prev_index:
                # Rendering faster than the source frame rate: wait for the
                # next frame's deadline instead of redrawing the same picture.
                # The clock counts elapsed frames, so convert through the play
                # direction rather than assuming forward.
                nxt = self._direction * (index + self._direction - self._base_frame)
                clock.wait_for(nxt, max_wait=0.02)
                w.pump_messages()
                continue
            if self._discontinuity:
                # A seek, loop wrap or direction change moves the playhead on
                # purpose. Counting the skipped span as dropped frames made the
                # figure meaningless -- one scrub across a clip logged hundreds
                # of "drops" when nothing had failed to keep up.
                self._discontinuity = False
            elif prev_index >= 0:
                gap = abs(index - prev_index)
                if gap > 1:
                    self._stats.dropped += gap - 1
            prev_index = index

            src = self._source
            if src is None:
                break
            src.hint(index, self._direction < 0)
            self._pending_draw = None
            if not self._draw(index, renderer):
                self._stats.message = "Buffering…"
                w.pump_messages()
                continue
            self._stats.message = ""
            st = self._last_stats

            now = time.perf_counter()
            fps_window.append(now - last)
            last = now
            mean = sum(fps_window) / len(fps_window) if fps_window else 0.0
            s = self._stats
            s.frame = index
            s.position = index / max(0.1, clock.clock.fps)
            s.render_fps = (1.0 / mean) if mean > 0 else 0.0
            s.decode_fps = src.decode_fps
            s.boxes = st.boxes
            s.buffered = src.buffered
            s.batch_ms = st.batch_ms
            s.drift_ms = clock.drift * 1000.0
            s.windows = renderer.pool.window_count

    def _draw(self, index: int, renderer: FrameRenderer) -> bool:
        """Fetch, effect and render one frame. False if it is not buffered yet."""
        src = self._source
        if src is None:
            return False
        boxes = src.get(index, timeout=0.25)
        if boxes is None:
            return False
        self._history.append(boxes)
        draw = boxes
        if self.cfg.effects.active():
            draw = self._effects.apply(boxes, index, list(self._history))
            if draw.shape[0] > HARD_WINDOW_CEILING:
                area = draw[:, boxgen.W] * draw[:, boxgen.H]
                keep = np.argpartition(-area, HARD_WINDOW_CEILING)[:HARD_WINDOW_CEILING]
                draw = draw[keep]
                self._stats.clamped += 1
        self._last_stats = renderer.render(draw)
        self._last_boxes = draw
        return True
