# TimeLeapPlayer — architecture & module contract

Play any video by using **Windows windows as pixels**. Rewritten from the
`GlitchedAppleVidPlayer` prototype; inspired by
[mon/bad_apple_virus](https://github.com/mon/bad_apple_virus).

## Data flow

```
video file
   │
   ├─ media.probe.probe()          ffprobe → MediaInfo (fps, duration, size, audio)
   │
   ├─ media.decoder.FrameDecoder   ffmpeg → raw gray frames on a pipe, seekable
   │        │                      (streaming: playback starts in <1 s)
   │        ▼
   ├─ core.boxgen.frame_to_boxes   grey grid → (N,5) int32 x,y,w,h,level
   │        │                      run-merge decomposition, ~0.15 ms/frame
   │        ▼
   ├─ engine.pipeline.FramePipeline  worker threads + bounded ring buffer
   │        │                        (prefetch ahead of the playhead)
   │        ▼
   ├─ engine.clock.Clock           absolute-deadline scheduling, 1 ms timer,
   │        │                      audio-as-master A/V sync, frame dropping
   │        ▼
   ├─ core.tracker.SlotTracker     box → window-slot assignment, temporally
   │        │                      stable (this is what kills the jitter the
   │        │                      original repo calls out in its README)
   │        ▼
   └─ render.renderer.FrameRenderer  DeferWindowPos batch, diffed against the
                                     previous frame → screen
```

Audio runs in parallel (`media.audio`) and, when present, is the master clock.

## Package layout

```
src/timeleap/
  config.py          AppConfig / VideoConfig / RenderConfig / PlaybackConfig /
                     EffectConfig, presets, JSON persistence      [DONE]
  core/
    boxgen.py        thresholds, quantisation, 3 decomposition algorithms [DONE]
    palette.py       named palettes → per-level COLORREF list
    tracker.py       temporally stable box→slot assignment
    effects.py       trails / echo / slit-scan / jitter / strobe / time-warp
  media/
    probe.py         ffprobe wrapper → MediaInfo
    decoder.py       streaming ffmpeg gray-frame reader, seekable
    audio.py         waveOut (ctypes) primary + ffplay fallback
  render/
    win32.py         typed ctypes bindings, DPI awareness, monitor enumeration
    window_pool.py   one window class per palette colour, pooled HWNDs
    renderer.py      DeferWindowPos batching + per-slot diffing
  engine/
    clock.py         high-resolution scheduler + A/V sync
    pipeline.py      decode→boxgen worker pool, ring buffer, seek
    player.py        transport: play/pause/seek/speed/loop/reverse
  cache/
    format.py        .tlp container read/write (indexed, seekable)
    store.py         content-addressed bake cache
  ui/
    app.py           home window, settings, transport (thread-safe marshalling)
    overlay.py       the player window: hover controls, drag/resize, fullscreen
    widgets.py       seek bar, sliders, stat readout, theming
    preview.py       live source preview (Pillow)
    hotkeys.py       global panic hotkey via RegisterHotKey
  cli.py             `timeleap play|bake|info|clean`
  __main__.py        `python -m timeleap`
```

## Hard rules for every module

1. **Windows-only code lives in `render/` and `media/audio.py`.** Everything
   else must import and unit-test on any OS.
2. **Never call Tk from a worker thread.** Marshal with `widget.after(0, fn)`.
   The old build called `messagebox.showerror` off-thread; that deadlocks.
3. **Every `subprocess` call passes `creationflags=CREATE_NO_WINDOW`** and
   `-nostdin` for ffmpeg, or console windows flash on every launch.
4. **All ctypes functions declare `argtypes`/`restype`.** An `HWND` is 64-bit;
   an undeclared one is silently truncated and the call fails invisibly.
5. **Boxes are `(N,5) int32` in grid units** — `x, y, w, h, level`. Normalise
   only at the render boundary.
6. **Nothing blocks the UI thread for more than ~16 ms.**

## Module contracts

### `core/palette.py`
```python
PALETTES: dict[str, list[tuple[int, int, int]]]   # name -> ramp, dark→bright
def resolve(name: str, levels: int) -> list[int]  # -> COLORREF (0x00BBGGRR), len == levels
def names() -> list[str]
```
Palettes: `mono` (black→white), `amber`, `matrix`, `ice`, `fire`, `rgb`,
`inferno`, `vapor`. For `levels == 1` the single colour is the ramp's brightest.

### `core/tracker.py`
```python
class SlotTracker:
    def __init__(self, capacity: int) -> None: ...
    def assign(self, boxes: np.ndarray) -> np.ndarray:
        """boxes (N,5) -> (N,) int32 slot index, each unique, < capacity.

        Boxes near a slot's previous rectangle keep that slot, so a window
        that represents 'the character's head' stays the same HWND frame to
        frame instead of being reused for an unrelated rectangle. Greedy
        nearest-centre matching on a coarse spatial grid; O(N) in practice.
        """
    def reset(self) -> None: ...
```

### `core/effects.py`
```python
class EffectStack:
    def __init__(self, cfg: EffectConfig) -> None: ...
    def apply(self, boxes: np.ndarray, frame_index: int,
              history: list[np.ndarray]) -> np.ndarray:
        """Return the boxes to actually draw. May append echo/trail copies
        (with reduced `level` so the palette dims them), jitter positions,
        blank on strobe frames, or splice rows from older frames (slit-scan)."""
    def speed_scale(self, t: float) -> float:
        """Multiplier for `time_warp`; 1.0 when the effect is off."""
    def reset(self) -> None: ...
```

### `media/probe.py`
```python
@dataclass
class MediaInfo:
    path: str; width: int; height: int; fps: float; duration: float
    frame_count: int          # estimated when the container does not say
    has_audio: bool; codec: str; rotation: int; size_bytes: int
    @property
    def aspect(self) -> float: ...

def probe(path: str) -> MediaInfo          # raises MediaError
def have_ffmpeg() -> tuple[bool, bool, bool]   # (ffmpeg, ffprobe, ffplay)
class MediaError(RuntimeError): ...
```
`nb_frames` is absent in mkv/webm — fall back to `duration * fps`, and mark
the count as an estimate rather than reporting 0.

### `media/decoder.py`
```python
class FrameDecoder:
    """Streaming grey-frame source. Owns one ffmpeg process."""
    def __init__(self, path: str, grid_w: int, grid_h: int,
                 start_frame: int = 0, fps: float | None = None) -> None: ...
    def __iter__(self) -> Iterator[tuple[int, np.ndarray]]:
        """Yields (frame_index, uint8 array shape (grid_h, grid_w))."""
    def seek(self, frame_index: int) -> None:   # restarts ffmpeg with -ss
    def close(self) -> None: ...
    def __enter__/__exit__
```
Use `-ss <seconds>` **before** `-i` for fast keyframe seek. Always
`-nostdin -loglevel error -an -sws_flags area` (area scaling is what makes a
64-wide grid look like the source instead of aliased noise).

### `media/audio.py`
```python
class AudioTrack(Protocol):
    def play(self, position: float = 0.0) -> None
    def pause(self) -> None
    def resume(self) -> None
    def stop(self) -> None
    def seek(self, position: float) -> None
    def set_volume(self, pct: int) -> None
    def set_speed(self, speed: float) -> None
    @property
    def position(self) -> float      # seconds, for A/V sync
    @property
    def playing(self) -> bool

class WaveOutTrack(AudioTrack)   # winmm waveOut* via ctypes, ffmpeg → s16le pipe
class FFPlayTrack(AudioTrack)    # fallback: ffplay -nodisp -ss <pos>
class NullTrack(AudioTrack)      # no audio
def open_track(path, backend, volume) -> AudioTrack
```
`WaveOutTrack` is the reason A/V sync can work: `waveOutGetPosition` gives an
exact playback position, so the video can be slaved to it. Real pause/resume
and volume, no process restart. Fall back to `FFPlayTrack` on any failure.

### `cache/format.py`
```python
MAGIC = b"TLPC"; VERSION = 1

@dataclass
class BakeHeader:
    fps: float; grid_w: int; grid_h: int; levels: int; frame_count: int
    duration: float; source: str; source_hash: str; fingerprint: str
    created: str; algo: str

def write_bake(path, header: BakeHeader, frames: Iterable[np.ndarray],
               progress: Callable[[int], None] | None = None) -> None
class BakeReader:
    header: BakeHeader
    def __len__(self) -> int
    def __getitem__(self, i: int) -> np.ndarray   # (N,5) int32, O(1) seek
    def close(self) -> None
```
Layout: `MAGIC | u32 version | u32 json_len | json header | u64 index[n+1] |
frame blobs`. Each frame blob is `u16 count` then `count × 5 × u16`. The index
table makes seeking O(1); the old `boxes.bin` had neither an index nor an fps
field, so nothing could ever load it.

### `cache/store.py`
```python
def source_hash(path: str) -> str        # size + mtime + head/tail sample
def cache_path(path: str, fingerprint: str) -> Path
def lookup(path: str, fingerprint: str) -> BakeReader | None
def prune(max_bytes: int = 2 << 30) -> int
```

### `engine/*` and `render/*`
Owned by the main session; treat their public names above as fixed.

### `ui/app.py`
```python
class TimeLeapApp(tk.Tk):
    def __init__(self, cfg: AppConfig, initial: str | None = None) -> None
    def run(self) -> None
```
Tabs: **Playback** (open/play/pause/stop/seek bar/speed/loop/reverse),
**Visual** (grid, max windows, levels, palette, threshold mode, invert, gap,
algorithm, monitor picker), **Effects** (the `EffectConfig` set),
**Audio** (mute, volume, backend), **About/Stats**.
Must show live stats: render fps, boxes/frame, dropped frames, buffer depth.
A close button on the player and the panic hotkey are mandatory — 200
top-most windows can otherwise cover every way to quit.

### `ui/hotkeys.py`
```python
class HotkeyManager:
    def __init__(self, on_panic, on_playpause=None, on_next=None) -> None
    def start(self) -> bool     # RegisterHotKey on a private message thread
    def stop(self) -> None
```
Panic = `Ctrl+Alt+Q`, play/pause = `Ctrl+Alt+Space`. Panic must hide every
window and stop playback even if the Tk thread is wedged.
