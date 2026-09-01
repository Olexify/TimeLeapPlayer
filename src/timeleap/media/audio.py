"""Audio playback: waveOut through ctypes, with honest fallbacks.

The previous build shelled out to `ffplay -ss <pos>` and killed/respawned that
process on every pause, volume change and seek. That made A/V sync impossible:
an external player exposes no readable playback clock, and each restart paid
for a fresh keyframe seek plus decoder warm-up, so the audio silently drifted
away from the video within seconds.

`WaveOutTrack` opens the waveOut device once and keeps it for the whole
session. ffmpeg only ever decodes into a raw s16le pipe; a daemon feeder thread
cycles a small pool of WAVEHDR buffers through `waveOutWrite`. Pause, resume
and volume are device calls that touch neither ffmpeg nor the queue, and
`waveOutGetPosition(TIME_SAMPLES)` yields a sample-exact playback position --
which is the whole reason the video can be slaved to the audio clock instead of
to a wall clock. Only a seek or a speed change respawns ffmpeg, because those
genuinely change what has to be decoded.

The buffer pool doubles as flow control: the feeder blocks once all buffers are
queued, ffmpeg blocks on the full pipe, and nothing decodes further ahead than
the pool depth (8 x 32 KiB = ~1.4 s at 48 kHz stereo).
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import threading
import time
import weakref
from typing import Protocol

_IS_WINDOWS = sys.platform == "win32"
_CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

SAMPLE_RATE = 48000
CHANNELS = 2
BITS_PER_SAMPLE = 16
FRAME_BYTES = CHANNELS * BITS_PER_SAMPLE // 8
BYTES_PER_SECOND = SAMPLE_RATE * FRAME_BYTES
BUFFER_BYTES = 32 * 1024
BUFFER_COUNT = 8

_START_TIMEOUT = 2.0        # seconds to wait for ffmpeg's first PCM before giving up
_JOIN_TIMEOUT = 3.0
_DRAIN_EPSILON = 0.03       # device position may lag the fed total by a driver tick


class AudioError(RuntimeError):
    """A backend could not be opened, or lost its device mid-playback."""


class AudioTrack(Protocol):
    """Transport shared by every backend. `position` is the A/V sync clock."""

    def play(self, position: float = 0.0) -> None: ...
    def pause(self) -> None: ...
    def resume(self) -> None: ...
    def stop(self) -> None: ...
    def close(self) -> None: ...
    def seek(self, position: float) -> None: ...
    def set_volume(self, pct: int) -> None: ...
    def set_speed(self, speed: float) -> None: ...
    @property
    def position(self) -> float: ...
    @property
    def playing(self) -> bool: ...


# ---------------------------------------------------------------- ffmpeg glue
def atempo_chain(speed: float) -> str:
    """`-af` value for `speed`, chained because one atempo only spans 0.5..2.0."""
    s = max(0.05, min(64.0, float(speed)))
    parts: list[float] = []
    while s > 2.0:
        parts.append(2.0)
        s /= 2.0
    while s < 0.5:
        parts.append(0.5)
        s *= 2.0
    parts.append(s)
    return ",".join(f"atempo={p:g}" for p in parts)


def _pcm_cmd(path: str, position: float, speed: float) -> list[str]:
    """`-ss` goes before `-i` so ffmpeg keyframe-seeks instead of decoding to it."""
    cmd = ["ffmpeg", "-nostdin", "-loglevel", "error"]
    if position > 0.0:
        cmd += ["-ss", f"{position:.6f}"]
    cmd += ["-i", path, "-vn"]
    if abs(speed - 1.0) > 1e-6:
        cmd += ["-af", atempo_chain(speed)]
    cmd += ["-f", "s16le", "-acodec", "pcm_s16le",
            "-ac", str(CHANNELS), "-ar", str(SAMPLE_RATE), "pipe:1"]
    return cmd


def _spawn(cmd: list[str]) -> subprocess.Popen[bytes]:
    return subprocess.Popen(
        cmd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL, bufsize=BUFFER_BYTES,
        creationflags=_CREATE_NO_WINDOW,
    )


def probe_has_audio(path: str) -> bool | None:
    """True/False when ffprobe answered, None when we could not tell.

    Only used to skip a doomed backend; an unknown answer must still try
    waveOut, since being wrong here would silence a perfectly good track.
    """
    exe = shutil.which("ffprobe")
    if not exe:
        return None
    try:
        done = subprocess.run(
            [exe, "-v", "error", "-select_streams", "a:0",
             "-show_entries", "stream=codec_type", "-of", "csv=p=0", path],
            stdin=subprocess.DEVNULL, capture_output=True, timeout=10,
            creationflags=_CREATE_NO_WINDOW,
        )
    except Exception:
        return None
    if done.returncode != 0:
        return None
    return b"audio" in done.stdout


# ---------------------------------------------------------------- winmm binding
_winmm = None
_kernel32 = None

if _IS_WINDOWS:
    try:
        import ctypes
        from ctypes import wintypes

        _winmm = ctypes.WinDLL("winmm")
        _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

        DWORD_PTR = ctypes.c_size_t          # pointer-sized; c_ulong would truncate
        HWAVEOUT = ctypes.c_void_p
        MMRESULT = ctypes.c_uint

        MMSYSERR_NOERROR = 0
        WAVE_MAPPER = 0xFFFFFFFF
        WAVE_FORMAT_PCM = 1
        CALLBACK_EVENT = 0x00050000
        TIME_SAMPLES = 0x0002
        WHDR_DONE = 0x00000001
        WHDR_PREPARED = 0x00000002
        WHDR_INQUEUE = 0x00000010
        WAIT_TIMEOUT = 0x00000102

        class WAVEFORMATEX(ctypes.Structure):
            _pack_ = 1                       # mmreg.h declares it inside pshpack1
            _fields_ = [
                ("wFormatTag", wintypes.WORD),
                ("nChannels", wintypes.WORD),
                ("nSamplesPerSec", wintypes.DWORD),
                ("nAvgBytesPerSec", wintypes.DWORD),
                ("nBlockAlign", wintypes.WORD),
                ("wBitsPerSample", wintypes.WORD),
                ("cbSize", wintypes.WORD),
            ]

        class WAVEHDR(ctypes.Structure):
            pass

        WAVEHDR._fields_ = [
            # LPSTR in the SDK, but c_void_p here: ctypes would strlen() a
            # c_char_p on every read and walk off the end of binary PCM.
            ("lpData", ctypes.c_void_p),
            ("dwBufferLength", wintypes.DWORD),
            ("dwBytesRecorded", wintypes.DWORD),
            ("dwUser", DWORD_PTR),
            ("dwFlags", wintypes.DWORD),
            ("dwLoops", wintypes.DWORD),
            ("lpNext", ctypes.POINTER(WAVEHDR)),
            ("reserved", DWORD_PTR),
        ]

        class _MMTIME_U(ctypes.Union):
            _pack_ = 1
            _fields_ = [
                ("ms", wintypes.DWORD),
                ("sample", wintypes.DWORD),
                ("cb", wintypes.DWORD),
                ("ticks", wintypes.DWORD),
                ("smpte", ctypes.c_byte * 8),
            ]

        class MMTIME(ctypes.Structure):
            _pack_ = 1                       # mmsystem.h declares it inside pshpack1
            _anonymous_ = ("u",)
            _fields_ = [("wType", ctypes.c_uint), ("u", _MMTIME_U)]

        _winmm.waveOutGetNumDevs.argtypes = []
        _winmm.waveOutGetNumDevs.restype = ctypes.c_uint
        _winmm.waveOutOpen.argtypes = [ctypes.POINTER(HWAVEOUT), ctypes.c_uint,
                                       ctypes.POINTER(WAVEFORMATEX), DWORD_PTR,
                                       DWORD_PTR, wintypes.DWORD]
        _winmm.waveOutOpen.restype = MMRESULT
        _winmm.waveOutClose.argtypes = [HWAVEOUT]
        _winmm.waveOutClose.restype = MMRESULT
        _winmm.waveOutPrepareHeader.argtypes = [HWAVEOUT, ctypes.POINTER(WAVEHDR),
                                                ctypes.c_uint]
        _winmm.waveOutPrepareHeader.restype = MMRESULT
        _winmm.waveOutUnprepareHeader.argtypes = [HWAVEOUT, ctypes.POINTER(WAVEHDR),
                                                  ctypes.c_uint]
        _winmm.waveOutUnprepareHeader.restype = MMRESULT
        _winmm.waveOutWrite.argtypes = [HWAVEOUT, ctypes.POINTER(WAVEHDR), ctypes.c_uint]
        _winmm.waveOutWrite.restype = MMRESULT
        _winmm.waveOutPause.argtypes = [HWAVEOUT]
        _winmm.waveOutPause.restype = MMRESULT
        _winmm.waveOutRestart.argtypes = [HWAVEOUT]
        _winmm.waveOutRestart.restype = MMRESULT
        _winmm.waveOutReset.argtypes = [HWAVEOUT]
        _winmm.waveOutReset.restype = MMRESULT
        _winmm.waveOutGetPosition.argtypes = [HWAVEOUT, ctypes.POINTER(MMTIME),
                                              ctypes.c_uint]
        _winmm.waveOutGetPosition.restype = MMRESULT
        _winmm.waveOutSetVolume.argtypes = [HWAVEOUT, wintypes.DWORD]
        _winmm.waveOutSetVolume.restype = MMRESULT
        _winmm.waveOutGetErrorTextW.argtypes = [MMRESULT, wintypes.LPWSTR, ctypes.c_uint]
        _winmm.waveOutGetErrorTextW.restype = MMRESULT

        _kernel32.CreateEventW.argtypes = [ctypes.c_void_p, wintypes.BOOL,
                                           wintypes.BOOL, wintypes.LPCWSTR]
        _kernel32.CreateEventW.restype = wintypes.HANDLE
        _kernel32.SetEvent.argtypes = [wintypes.HANDLE]
        _kernel32.SetEvent.restype = wintypes.BOOL
        _kernel32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        _kernel32.WaitForSingleObject.restype = wintypes.DWORD
        _kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        _kernel32.CloseHandle.restype = wintypes.BOOL
    except Exception:                        # no winmm, no ctypes -- fall back
        _winmm = None
        _kernel32 = None


def _mm(code: int, what: str) -> None:
    if code == 0:
        return
    text = f"error {code}"
    try:
        buf = ctypes.create_unicode_buffer(320)
        if _winmm.waveOutGetErrorTextW(code, buf, 320) == 0:
            text = buf.value
    except Exception:
        pass
    raise AudioError(f"{what}: {text} ({code})")


def waveout_available() -> bool:
    """True when winmm loaded and the machine actually has an output device."""
    if _winmm is None:
        return False
    try:
        return _winmm.waveOutGetNumDevs() > 0
    except Exception:
        return False


# ---------------------------------------------------------------- backends
def _feed_loop(ref: weakref.ReferenceType[WaveOutTrack],
               proc: subprocess.Popen[bytes], generation: int) -> None:
    """Pump ffmpeg's PCM into the buffer pool until the stream ends.

    The track is reached through a weak reference and released before every
    blocking call. A feeder that held its track strongly would keep it alive
    for as long as ffmpeg had data -- so a caller that forgot `close()` would
    leak the device, the ffmpeg child and the thread, and the abandoned track
    would keep playing audibly over whatever replaced it.
    """
    stdout = proc.stdout
    index = 0
    self: WaveOutTrack | None = None
    try:
        while stdout is not None:
            while True:                      # wait for buffer `index` to drain
                self = ref()
                if self is None or self._closing or generation != self._generation:
                    return
                if not (self._headers[index].dwFlags & WHDR_INQUEUE):
                    break
                event = self._event
                self = None
                if not event:
                    return
                _kernel32.WaitForSingleObject(event, 50)
            self = None
            data = stdout.read(BUFFER_BYTES)
            self = ref()
            if self is None:
                return
            if not data or self._closing or generation != self._generation:
                break
            self._submit(index, data)
            self._fed_bytes += len(data)
            self._first_data.set()
            index = (index + 1) % BUFFER_COUNT
    except Exception:
        pass                                 # a torn-down device is not an error
    finally:
        self = ref()
        if self is not None and generation == self._generation:
            self._eof = True


class WaveOutTrack:
    """Primary backend: one persistent waveOut device fed from an ffmpeg pipe.

    The device is opened in the constructor so `open_track` can fall back
    immediately when there is no usable output, rather than failing later at
    the first `play()`.
    """

    def __init__(self, path: str, volume: int = 80, speed: float = 1.0) -> None:
        if _winmm is None or _kernel32 is None:
            raise AudioError("winmm is unavailable on this platform")
        if _winmm.waveOutGetNumDevs() == 0:
            raise AudioError("no waveOut output device")
        if not shutil.which("ffmpeg"):
            raise AudioError("ffmpeg not on PATH")

        self._path = path
        self._volume = int(max(0, min(100, volume)))
        self._speed = float(speed)
        self._lock = threading.RLock()

        self._hwo: object | None = None
        self._proc: subprocess.Popen[bytes] | None = None
        self._thread: threading.Thread | None = None
        self._generation = 0
        self._closing = False
        self._closed = False
        self._started = False
        self._paused = False

        self._offset = 0.0          # media time of the current stream's first sample
        self._fed_bytes = 0         # written by the feeder thread only
        self._eof = False
        self._drain_base: float | None = None
        self._drain_t0: float | None = None
        self._first_data = threading.Event()

        self._event = _kernel32.CreateEventW(None, False, False, None)
        if not self._event:
            raise AudioError("CreateEventW failed")

        # Buffers stay referenced for the object's whole life: the driver holds
        # raw pointers into them, so letting one be collected is a use-after-free.
        self._buffers = [ctypes.create_string_buffer(BUFFER_BYTES)
                         for _ in range(BUFFER_COUNT)]
        self._headers = [WAVEHDR() for _ in range(BUFFER_COUNT)]
        for hdr, buf in zip(self._headers, self._buffers):
            hdr.lpData = ctypes.addressof(buf)
            hdr.dwBufferLength = BUFFER_BYTES

        try:
            self._open_device()
        except Exception:
            _kernel32.CloseHandle(self._event)
            self._event = 0
            raise

    # ---- device ------------------------------------------------------
    def _open_device(self) -> None:
        wfx = WAVEFORMATEX(WAVE_FORMAT_PCM, CHANNELS, SAMPLE_RATE,
                           BYTES_PER_SECOND, FRAME_BYTES, BITS_PER_SAMPLE, 0)
        handle = HWAVEOUT()
        _mm(_winmm.waveOutOpen(ctypes.byref(handle), WAVE_MAPPER, ctypes.byref(wfx),
                               DWORD_PTR(self._event), DWORD_PTR(0), CALLBACK_EVENT),
            "waveOutOpen")
        self._hwo = handle
        self._apply_volume()

    def _close_device(self) -> None:
        hwo, self._hwo = self._hwo, None
        if hwo is None:
            return
        try:
            _winmm.waveOutReset(hwo)
            for hdr in self._headers:
                if hdr.dwFlags & WHDR_PREPARED:
                    _winmm.waveOutUnprepareHeader(hwo, ctypes.byref(hdr),
                                                  ctypes.sizeof(hdr))
                hdr.dwFlags = 0
        finally:
            _winmm.waveOutClose(hwo)

    def _apply_volume(self) -> None:
        if self._hwo is None:
            return
        level = int(round(0xFFFF * self._volume / 100.0)) & 0xFFFF
        both = (level << 16) | level
        _winmm.waveOutSetVolume(self._hwo, both)   # best effort: some drivers refuse

    # ---- stream ------------------------------------------------------
    def _teardown_stream(self) -> None:
        """Kill ffmpeg, flush the queue, join the feeder. Safe to repeat."""
        self._generation += 1
        proc, self._proc = self._proc, None
        thread, self._thread = self._thread, None

        if proc is not None:
            try:
                proc.kill()                  # unblocks a feeder parked in read()
            except Exception:
                pass
        if self._hwo is not None:
            _winmm.waveOutReset(self._hwo)   # unblocks a feeder waiting for a buffer
        if self._event:
            _kernel32.SetEvent(self._event)
        if thread is not None and thread is not threading.current_thread():
            thread.join(_JOIN_TIMEOUT)
        if self._hwo is not None:
            # Again, after the join. The feeder can complete a waveOutWrite
            # between its last generation check and the reset above, and that
            # buffer would then play as -- and be counted into -- the next
            # stream: one 32 KiB buffer of stale audio and a permanent 171 ms
            # error on the clock the video is slaved to.
            _winmm.waveOutReset(self._hwo)
        if proc is not None:
            try:
                proc.wait(2.0)
            except Exception:
                try:
                    proc.kill()
                    proc.wait(1.0)
                except Exception:
                    pass
            try:
                if proc.stdout is not None:
                    proc.stdout.close()
            except Exception:
                pass

    def _start_stream(self, position: float, gate: bool = False) -> None:
        self._teardown_stream()
        # waveOutReset clears the device's paused state, so a held pause has to
        # be re-asserted here -- before ffmpeg even exists. Doing it after this
        # returns leaves a window in which the new feeder writes and a track the
        # caller believes is paused audibly plays.
        if self._paused and self._hwo is not None:
            _winmm.waveOutPause(self._hwo)
        self._offset = max(0.0, float(position))
        self._fed_bytes = 0
        self._eof = False
        self._drain_base = None
        self._drain_t0 = None
        self._first_data.clear()

        self._proc = _spawn(_pcm_cmd(self._path, self._offset, self._speed))
        self._thread = threading.Thread(
            target=_feed_loop, args=(weakref.ref(self), self._proc, self._generation),
            name="timeleap-audio-feed", daemon=True)
        self._thread.start()
        if not gate:
            return
        deadline = time.perf_counter() + _START_TIMEOUT
        while time.perf_counter() < deadline:
            if self._first_data.wait(0.02) or self._eof:
                break
        if not self._first_data.is_set():
            self._teardown_stream()
            raise AudioError(f"ffmpeg produced no PCM for {self._path!r}")

    def _submit(self, index: int, data: bytes) -> None:
        hdr = self._headers[index]
        hwo = self._hwo
        if hwo is None:
            raise AudioError("device closed")
        if hdr.dwFlags & WHDR_PREPARED:
            _mm(_winmm.waveOutUnprepareHeader(hwo, ctypes.byref(hdr),
                                              ctypes.sizeof(hdr)),
                "waveOutUnprepareHeader")
        ctypes.memmove(self._buffers[index], data, len(data))
        hdr.dwBufferLength = len(data)
        hdr.dwBytesRecorded = 0
        hdr.dwFlags = 0
        hdr.dwLoops = 0
        _mm(_winmm.waveOutPrepareHeader(hwo, ctypes.byref(hdr), ctypes.sizeof(hdr)),
            "waveOutPrepareHeader")
        _mm(_winmm.waveOutWrite(hwo, ctypes.byref(hdr), ctypes.sizeof(hdr)),
            "waveOutWrite")

    # ---- transport ---------------------------------------------------
    def play(self, position: float = 0.0) -> None:
        with self._lock:
            if self._closed:
                raise AudioError("track is closed")
            if self._hwo is None:
                self._open_device()
            self._start_stream(position, gate=True)
            if self._paused:
                _winmm.waveOutRestart(self._hwo)
            self._paused = False
            self._started = True

    def pause(self) -> None:
        with self._lock:
            if self._paused or not self._started or self._hwo is None:
                return
            _mm(_winmm.waveOutPause(self._hwo), "waveOutPause")
            self._paused = True
            if self._drain_base is not None and self._drain_t0 is not None:
                self._drain_base += (time.perf_counter() - self._drain_t0) * self._speed
                self._drain_t0 = None

    def resume(self) -> None:
        with self._lock:
            if not self._paused or self._hwo is None:
                return
            _mm(_winmm.waveOutRestart(self._hwo), "waveOutRestart")
            self._paused = False
            if self._drain_base is not None:
                self._drain_t0 = time.perf_counter()

    def seek(self, position: float) -> None:
        with self._lock:
            if self._closed:
                return
            if not self._started or self._hwo is None:
                self._offset = max(0.0, float(position))
                return
            self._start_stream(position)

    def set_volume(self, pct: int) -> None:
        with self._lock:
            self._volume = int(max(0, min(100, pct)))
            self._apply_volume()

    def set_speed(self, speed: float) -> None:
        with self._lock:
            speed = max(0.05, min(64.0, float(speed)))
            if abs(speed - self._speed) < 1e-6:
                return
            here = self.position
            self._speed = speed
            if self._started and self._hwo is not None and not self._closed:
                self._start_stream(here)

    def stop(self) -> None:
        with self._lock:
            if self._closed:
                return
            here = self.position
            self._teardown_stream()
            self._close_device()
            self._offset = here
            self._fed_bytes = 0
            self._eof = False
            self._drain_base = None
            self._drain_t0 = None
            self._started = False
            self._paused = False

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            here = self.position     # read it while the device can still answer
            self._closing = True
            self._closed = True
            self._teardown_stream()
            self._close_device()
            if self._event:
                _kernel32.CloseHandle(self._event)
                self._event = 0
            self._offset = here      # a caller saving a resume point reads this
            self._fed_bytes = 0
            self._eof = False
            self._drain_base = None
            self._drain_t0 = None
            self._started = False
            self._paused = False

    def __enter__(self) -> WaveOutTrack:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def __del__(self) -> None:
        # The feeder only holds a weak reference, so a track the caller dropped
        # without closing reaches this instead of playing on to the end of the
        # file with its device and ffmpeg child still held.
        try:
            if not getattr(self, "_closed", True):
                self.close()
        except Exception:
            pass

    # ---- clock -------------------------------------------------------
    @property
    def position(self) -> float:
        """Media seconds. Device samples are wall-clock exact; the seek offset
        and the atempo factor convert them back to source time."""
        if self._drain_t0 is not None and self._drain_base is not None:
            return self._drain_base + (time.perf_counter() - self._drain_t0) * self._speed
        if self._drain_base is not None:
            return self._drain_base
        hwo = self._hwo
        if hwo is None:
            return self._offset
        mmt = MMTIME()
        mmt.wType = TIME_SAMPLES
        if _winmm.waveOutGetPosition(hwo, ctypes.byref(mmt), ctypes.sizeof(mmt)) != 0:
            return self._offset
        if mmt.wType != TIME_SAMPLES:
            return self._offset
        pos = self._offset + (mmt.sample / float(SAMPLE_RATE)) * self._speed

        # Audio often ends before the video does. Once the queue has drained,
        # hand the clock over to a wall clock so the caller's playhead keeps
        # moving instead of freezing on the last sample.
        if self._eof and self._started:
            fed = self._offset + (self._fed_bytes / BYTES_PER_SECOND) * self._speed
            if pos >= fed - _DRAIN_EPSILON:
                self._drain_base = max(pos, fed)
                self._drain_t0 = None if self._paused else time.perf_counter()
                return self._drain_base
        return pos

    @property
    def playing(self) -> bool:
        return self._started and not self._paused and not self._closed


class FFPlayTrack:
    """Fallback backend: an external `ffplay -nodisp` process.

    ffplay publishes no playback clock and, with stdin detached, accepts no
    transport commands, so this backend is a compromise on both counts:
    `position` is a wall-clock estimate anchored at the last (re)start, and
    **pause is emulated by killing ffplay and respawning it with `-ss` at the
    remembered position**. Every pause, volume change and speed change
    therefore costs a keyframe seek plus a decoder warm-up, and the estimate
    drifts against the real output by however much that start-up takes. Prefer
    `WaveOutTrack` whenever it opens; this exists so machines without a usable
    waveOut device still get sound.
    """

    def __init__(self, path: str, volume: int = 80, speed: float = 1.0) -> None:
        self._exe = shutil.which("ffplay")
        if not self._exe:
            raise AudioError("ffplay not on PATH")
        self._path = path
        self._volume = int(max(0, min(100, volume)))
        self._speed = float(speed)
        self._lock = threading.RLock()
        self._proc: subprocess.Popen[bytes] | None = None
        self._base = 0.0
        self._t0: float | None = None
        self._started = False
        self._closed = False

    def _kill(self) -> None:
        proc, self._proc = self._proc, None
        if proc is None:
            return
        try:
            proc.kill()
            proc.wait(2.0)
        except Exception:
            pass

    def _spawn_at(self, position: float) -> None:
        self._kill()
        self._base = max(0.0, float(position))
        cmd = [self._exe, "-nodisp", "-autoexit", "-loglevel", "error",
               "-volume", str(self._volume)]
        if self._base > 0.0:
            cmd += ["-ss", f"{self._base:.6f}"]
        if abs(self._speed - 1.0) > 1e-6:
            cmd += ["-af", atempo_chain(self._speed)]
        cmd += [self._path]
        self._proc = subprocess.Popen(
            cmd, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL, creationflags=_CREATE_NO_WINDOW)
        self._t0 = time.perf_counter()

    def play(self, position: float = 0.0) -> None:
        with self._lock:
            if self._closed:
                raise AudioError("track is closed")
            self._spawn_at(position)
            self._started = True

    def pause(self) -> None:
        with self._lock:
            if self._t0 is None:
                return
            self._base = self.position
            self._t0 = None
            self._kill()

    def resume(self) -> None:
        with self._lock:
            if self._t0 is not None or not self._started or self._closed:
                return
            self._spawn_at(self._base)

    def seek(self, position: float) -> None:
        with self._lock:
            if self._closed:
                return
            if self._t0 is None:
                self._base = max(0.0, float(position))
                return
            self._spawn_at(position)

    def set_volume(self, pct: int) -> None:
        with self._lock:
            pct = int(max(0, min(100, pct)))
            if pct == self._volume:
                return
            self._volume = pct
            if self._t0 is not None:
                self._spawn_at(self.position)

    def set_speed(self, speed: float) -> None:
        with self._lock:
            speed = max(0.05, min(64.0, float(speed)))
            if abs(speed - self._speed) < 1e-6:
                return
            here = self.position
            self._speed = speed
            if self._t0 is not None:
                self._spawn_at(here)

    def stop(self) -> None:
        with self._lock:
            self._base = self.position
            self._t0 = None
            self._started = False
            self._kill()

    def close(self) -> None:
        with self._lock:
            self.stop()
            self._closed = True

    def __enter__(self) -> FFPlayTrack:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    @property
    def position(self) -> float:
        if self._t0 is None:
            return self._base
        return self._base + (time.perf_counter() - self._t0) * self._speed

    @property
    def playing(self) -> bool:
        return self._started and self._t0 is not None and not self._closed


class NullTrack:
    """Silent transport. The clock still runs so silent videos stay on time."""

    def __init__(self, path: str = "", volume: int = 80, speed: float = 1.0) -> None:
        self._path = path
        self._volume = int(max(0, min(100, volume)))
        self._speed = float(speed)
        self._base = 0.0
        self._t0: float | None = None
        self._started = False
        self._closed = False

    def play(self, position: float = 0.0) -> None:
        self._base = max(0.0, float(position))
        self._t0 = time.perf_counter()
        self._started = True
        self._closed = False

    def pause(self) -> None:
        if self._t0 is None:
            return
        self._base = self.position
        self._t0 = None

    def resume(self) -> None:
        if self._t0 is None and self._started:
            self._t0 = time.perf_counter()

    def seek(self, position: float) -> None:
        self._base = max(0.0, float(position))
        if self._t0 is not None:
            self._t0 = time.perf_counter()

    def set_volume(self, pct: int) -> None:
        self._volume = int(max(0, min(100, pct)))

    def set_speed(self, speed: float) -> None:
        self._base = self.position
        if self._t0 is not None:
            self._t0 = time.perf_counter()
        self._speed = max(0.05, min(64.0, float(speed)))

    def stop(self) -> None:
        self._base = self.position
        self._t0 = None
        self._started = False

    def close(self) -> None:
        self.stop()
        self._closed = True

    def __enter__(self) -> NullTrack:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    @property
    def position(self) -> float:
        if self._t0 is None:
            return self._base
        return self._base + (time.perf_counter() - self._t0) * self._speed

    @property
    def playing(self) -> bool:
        return self._started and self._t0 is not None and not self._closed


# ---------------------------------------------------------------- factory
_ORDER: dict[str, tuple[str, ...]] = {
    "waveout": ("waveout", "ffplay", "null"),
    "ffplay": ("ffplay", "waveout", "null"),
    "none": ("null",),
}


def open_track(path: str, backend: str = "waveout", volume: int = 80,
               speed: float = 1.0) -> AudioTrack:
    """Best available track for `path`. Degrades quietly; never raises.

    Playback must survive a missing codec or a busy sound device, so every
    construction failure just moves to the next backend and the caller always
    gets something with a working clock.
    """
    order = _ORDER.get(str(backend).lower(), _ORDER["waveout"])
    if order[0] != "null":
        # A backend that cannot possibly produce sound must be skipped here, or
        # the caller gets a track whose play() raises after open_track promised
        # it would not. Anything remote or unprobeable still gets a real try.
        local = "://" not in path
        if probe_has_audio(path) is False or (local and not os.path.isfile(path)):
            return NullTrack(path, volume, speed)
    for name in order:
        try:
            if name == "waveout":
                return WaveOutTrack(path, volume, speed)
            if name == "ffplay":
                return FFPlayTrack(path, volume, speed)
            return NullTrack(path, volume, speed)
        except Exception:
            continue
    return NullTrack(path, volume, speed)
