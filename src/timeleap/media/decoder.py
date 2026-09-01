"""Streaming grey-frame source: one ffmpeg process piping raw gray8.

The prototype decoded the whole file into `boxes.bin` before anything
appeared on screen, so a three-minute clip meant a ten-minute wait.
`FrameDecoder` hands the first frame over in well under a second and lets
the pipeline stay a fixed distance ahead of the playhead, which is what
makes seeking cost one process restart instead of a re-bake.

ffmpeg does the scaling: `area` downsampling to a 96x54 grid averages
whole blocks of source pixels, so the grid reads like the video rather
than like point-sampled noise, and it costs nothing on our side.
"""
from __future__ import annotations

import subprocess
import sys
import threading
from collections import deque
from types import TracebackType
from typing import IO, Iterator

import numpy as np

from .probe import MediaError, probe

CREATE_NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0

_DRAIN_CHUNK = 1 << 16
_DRAIN_LIMIT = 256          # 16 MB is far more than a pipe can hold back
_STOP_TIMEOUT = 2.0
# -ss lands on the first frame at or after the requested time, so asking for
# exactly frame_index/fps can round up onto the next frame. Back off a
# fraction of a frame; a quarter is well inside one frame interval.
_SEEK_BACKOFF = 0.25


def _drain_stderr(stream: IO[bytes], sink: deque[str]) -> None:
    """Keep ffmpeg's stderr moving. Deliberately not a method.

    A thread running a bound method holds the decoder alive, so `__del__`
    could never fire -- and `__del__` is what kills the process the thread
    is waiting on. That deadlock leaks one ffmpeg per dropped decoder.
    """
    try:
        for line in iter(stream.readline, b""):
            text = line.decode("utf-8", "replace").strip()
            if text:
                sink.append(text)
    except (ValueError, OSError):
        pass                          # closed from under us by _stop()


class FrameDecoder:
    """Grey frames of shape (grid_h, grid_w) from `path`, seekable.

    One consumer at a time, but `seek()` and `close()` are safe from another
    thread: the pipeline seeks from the UI while its worker sits blocked in
    a read. The lock covers publishing and unpublishing the process, never a
    read and never the blocking wait, so a seek interrupts a blocked reader
    instead of queueing behind it.

    The invariant that keeps frame numbers honest: `_generation` is bumped
    and the pipe is detached under one lock acquisition, and the reader
    samples both together. A generation that still matches after a read
    therefore proves the frame came from the stream the reader is still
    supposed to be on -- so a seek can neither be mistaken for the end of the
    video nor leave a stale frame wearing the new position's index.
    """

    def __init__(self, path: str, grid_w: int, grid_h: int,
                 start_frame: int = 0, fps: float | None = None) -> None:
        # Validate *after* truncating: a fractional grid such as 0.5 passes a
        # `> 0` test and then truncates to a zero-byte frame, which reads as an
        # endless stream of empty arrays instead of an error.
        self.grid_w = int(grid_w)
        self.grid_h = int(grid_h)
        if self.grid_w <= 0 or self.grid_h <= 0:
            raise ValueError(f"grid must be positive, got {grid_w}x{grid_h}")
        self.path = str(path)
        self.frame_bytes = self.grid_w * self.grid_h
        self._fps = float(fps) if fps and fps > 0 else 0.0
        self._start = max(0, int(start_frame))
        self._emitted = 0
        self._generation = 0
        self._closed = False
        self._lock = threading.RLock()
        self._proc: subprocess.Popen[bytes] | None = None
        self._stdout: IO[bytes] | None = None
        self._stderr_tail: deque[str] = deque(maxlen=8)
        self._stderr_thread: threading.Thread | None = None

    # ---- introspection ------------------------------------------------
    @property
    def frame_index(self) -> int:
        """Absolute index of the next frame this decoder will yield."""
        return self._start + self._emitted

    @property
    def fps(self) -> float:
        """Rate used for seek arithmetic; probed on first use if not given."""
        if self._fps <= 0:
            self._fps = probe(self.path).fps
        return self._fps

    # ---- process lifecycle --------------------------------------------
    def _command(self) -> list[str]:
        cmd = ["ffmpeg", "-nostdin", "-loglevel", "error", "-sws_flags", "area"]
        if self._start > 0:
            # -ss *before* -i seeks on keyframes instead of decoding and
            # discarding everything from zero: milliseconds, not seconds.
            seconds = max(0.0, (self._start - _SEEK_BACKOFF) / self.fps)
            cmd += ["-ss", f"{seconds:.6f}"]
        cmd += ["-i", self.path, "-an",
                "-vf", f"scale={self.grid_w}:{self.grid_h}:flags=area,format=gray",
                "-f", "rawvideo", "-pix_fmt", "gray", "pipe:1"]
        return cmd

    def _spawn(self) -> None:
        self._stderr_tail = deque(maxlen=8)
        try:
            proc = subprocess.Popen(
                self._command(),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                bufsize=max(_DRAIN_CHUNK, self.frame_bytes * 4),
                creationflags=CREATE_NO_WINDOW,
            )
        except FileNotFoundError as exc:
            raise MediaError("ffmpeg not found on PATH") from exc
        except OSError as exc:
            raise MediaError(f"ffmpeg failed to start: {exc}") from exc
        self._proc = proc
        self._stdout = proc.stdout
        # stderr must be drained continuously: -loglevel error is usually
        # silent, but a corrupt stream can fill the 64 KB pipe and deadlock
        # ffmpeg mid-frame while we sit blocked reading stdout.
        self._stderr_thread = threading.Thread(
            target=_drain_stderr, args=(proc.stderr, self._stderr_tail),
            name="ffmpeg-stderr", daemon=True)
        self._stderr_thread.start()

    def _detach(self) -> tuple[subprocess.Popen[bytes] | None, IO[bytes] | None,
                               threading.Thread | None]:
        """Unpublish the current process. Caller must hold `_lock`.

        Detaching has to happen under the same lock acquisition that bumps
        `_generation`, or a reader can observe the new generation while
        `_stdout` still points at the old pipe and stamp the new frame number
        onto the old position's pixels.
        """
        proc, self._proc = self._proc, None
        stdout, self._stdout = self._stdout, None
        thread, self._stderr_thread = self._stderr_thread, None
        return proc, stdout, thread

    def _reap(self, proc: subprocess.Popen[bytes] | None, stdout: IO[bytes] | None,
              thread: threading.Thread | None) -> None:
        """Wait out a detached process. Blocking, so never called under `_lock`."""
        if proc is None:
            return
        if proc.poll() is None:
            try:
                proc.terminate()
            except OSError:
                pass
        if stdout is not None:
            # Drain before closing: ffmpeg may have queued bytes we never
            # read, and a full pipe would block its exit forever.
            try:
                for _ in range(_DRAIN_LIMIT):
                    if not stdout.read1(_DRAIN_CHUNK):
                        break
            except (ValueError, OSError):
                pass
            try:
                stdout.close()
            except OSError:
                pass
        try:
            proc.wait(timeout=_STOP_TIMEOUT)
        except subprocess.TimeoutExpired:
            proc.kill()
            try:
                proc.wait(timeout=_STOP_TIMEOUT)
            except subprocess.TimeoutExpired:
                pass
        if thread is not None:
            thread.join(timeout=1.0)
        if proc.stderr is not None:
            try:
                proc.stderr.close()
            except OSError:
                pass

    def _stop(self) -> None:
        """Tear the process down without ever leaving a zombie or a blocked pipe."""
        with self._lock:
            detached = self._detach()
        self._reap(*detached)

    # ---- reading -------------------------------------------------------
    def _read_frame(self, stream: IO[bytes] | None) -> np.ndarray | None:
        """Exactly one frame, or None at end of stream.

        A pipe read returns whatever happens to be buffered, so a single
        read() is not a frame -- the prototype's `if len(raw) < size: break`
        is why it silently lost the tail of every video.

        The stream is passed in rather than read off `self`: it must be the
        one that was current when the caller sampled `_generation`.
        """
        if stream is None:
            return None
        buf = bytearray(self.frame_bytes)
        view = memoryview(buf)
        got = 0
        while got < self.frame_bytes:
            try:
                n = stream.readinto(view[got:])
            except (ValueError, OSError):
                return None           # closed by seek()/close() mid-read
            if not n:
                return None           # EOF; a partial tail is not a frame
            got += n
        return np.frombuffer(buf, np.uint8).reshape(self.grid_h, self.grid_w)

    def _check_exit(self) -> None:
        """Report a start-up failure, but never a mid-stream one.

        Zero frames plus a non-zero exit means the file, the path or the
        filter chain is wrong and the caller must hear about it. A failure
        after N frames is a damaged tail: play what decoded and stop.
        """
        proc = self._proc
        if proc is None or self._emitted:
            return
        try:
            code = proc.wait(timeout=_STOP_TIMEOUT)
        except subprocess.TimeoutExpired:
            return
        if code:
            detail = "; ".join(self._stderr_tail) or f"exit code {code}"
            raise MediaError(f"ffmpeg could not decode {self.path}: {detail}")

    def __iter__(self) -> Iterator[tuple[int, np.ndarray]]:
        """Yield (absolute frame index, uint8 (grid_h, grid_w))."""
        while not self._closed:
            if self._proc is None and self.frame_index > 0:
                _ = self.fps          # resolve the rate outside the lock: a seek
                                      # must not queue behind an ffprobe round trip
            with self._lock:
                if self._closed:      # closed while we waited for the lock
                    return
                if self._proc is None:
                    # Fold what this decoder already emitted into the start
                    # position. A restart re-seeks to `_start`, so leaving
                    # `_emitted` behind would decode the same frames again and
                    # number them as if they came later in the video.
                    self._start += self._emitted
                    self._emitted = 0
                    self._spawn()
                # Sampled under the lock that `_detach` swaps them under, so a
                # generation that still matches after the read proves this
                # frame came from the stream we are still supposed to be on.
                generation = self._generation
                stream = self._stdout
            frame = self._read_frame(stream)
            if frame is None:
                if generation != self._generation:
                    continue          # seek()/close() replaced the stream mid-read
                self._check_exit()
                self._stop()          # reap now; the caller may never close()
                return
            with self._lock:
                if generation != self._generation:
                    # A seek landed while this frame was in flight. Numbering
                    # it now would stamp the new position onto pixels from the
                    # old one -- and run the count off the end of the video.
                    continue
                index = self._start + self._emitted
                self._emitted += 1
            yield index, frame

    # ---- transport ------------------------------------------------------
    def seek(self, frame_index: int) -> None:
        """Restart the stream at `frame_index`.

        The new process is spawned lazily on the next read, so dragging a
        seek bar costs one ffmpeg launch rather than one per mouse move.
        The generation bump and the detach share one lock acquisition: that
        pairing is what stops a reader from either mistaking the resulting
        EOF for the end of the video, or pulling a leftover frame off the old
        pipe and numbering it from the new position.
        """
        with self._lock:
            self._generation += 1
            self._start = max(0, int(frame_index))
            self._emitted = 0
            self._closed = False
            detached = self._detach()
        self._reap(*detached)

    def close(self) -> None:
        with self._lock:
            self._closed = True
            self._generation += 1
            detached = self._detach()
        self._reap(*detached)

    def __enter__(self) -> FrameDecoder:
        return self

    def __exit__(self, exc_type: type[BaseException] | None,
                 exc: BaseException | None, tb: TracebackType | None) -> None:
        self.close()

    def __del__(self) -> None:
        try:
            self.close()
        except Exception:
            pass                      # interpreter teardown, nothing to report
