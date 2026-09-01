# TimeLeapPlayer — feature reference

Every feature the player actually ships, grouped by area, with the reason it
exists where that is not obvious. Settings names match the fields in
`timeleap.config`, so anything here can also be set in
`%LOCALAPPDATA%/TimeLeapPlayer/config.json` or from the CLI.

---

## 1. Playback

| Feature | Setting | Notes |
|---|---|---|
| Play / pause / stop | — | Stop rewinds to frame 0 **and** blanks the screen; pause holds the current frame on screen. |
| Frame-accurate seek | — | `seek_frame`, `seek_time`, `seek_fraction`. Scrubbing while paused repaints the frame you scrubbed to, so the seek bar and the screen never disagree. |
| Speed | `playback.speed` | 0.1x to 4.0x. Audio is retimed with an `atempo` chain rather than resampled, so pitch is preserved. |
| Reverse playback | `playback.reverse` | Real reverse, not a re-decode. Audio is silenced while reversed — it cannot run backwards. |
| Loop modes | `playback.loop` | `off`, `loop`, `pingpong`. Ping-pong flips direction at each end instead of jumping. |
| Frame dropping | `playback.max_frame_skip` | Timing is *pulled*: each render pass asks the clock which frame is due now. A slow frame is dropped rather than added to a growing backlog. |
| Prefetch depth | `playback.prefetch_frames` | Ring-buffer depth ahead of the playhead (default 96 frames). |
| Live stats | `show_stats` | Render fps, decode fps, boxes/frame, window count, dropped frames, buffer depth, A/V drift in ms, batch time in ms. |
| Global hotkeys | `panic_hotkey` | `Ctrl+Alt+Q` panic, `Ctrl+Alt+Space` play/pause, `Ctrl+Alt+Right` next file. Registered on their own message thread with `MOD_NOREPEAT`, so holding the combo does not queue thirty panics a second. |
| Live preview | — | A side-by-side pane: the grey grid the decoder produced, and the rectangles that survived thresholding, the level split and the window budget. Frames reach it through a bounded queue drained on an `after` tick, so the render thread never touches Tk and never blocks on a slow UI — when the UI falls behind the tap discards frames, which for a preview is the correct answer. |

### Clock and A/V sync

* Deadlines are **absolute offsets from a fixed anchor**, so rounding error
  never accumulates. The prototype slept `delay - work_done` after each
  frame, which drifts steadily.
* The process requests a **1 ms scheduler tick** (`timeBeginPeriod`) and
  finishes the last ~1.5 ms of each wait with a short spin. Windows' default
  timer resolution is ~15.6 ms — at 24 fps that is over a third of a frame of
  overshoot, i.e. visible stutter on an otherwise idle machine.
* When audio is playing it becomes the **master clock**
  (`playback.av_sync`). Audio hardware cannot be told to wait, so the video
  follows it.

---

## 2. Video / geometry

These are the settings that change the produced rectangles, and therefore the
ones that form the bake fingerprint (`VideoConfig.fingerprint()`). Changing
the palette or the volume never invalidates a cached bake; changing the grid
does.

| Feature | Setting | Notes |
|---|---|---|
| Grid size | `video.grid_w`, `video.grid_h` | Decode resolution in cells. Default 96x54. |
| Auto grid height | `video.auto_grid` | Derive `grid_h` from the source aspect ratio, so 4:3 footage is not stretched into a 16:9 grid. |
| Window budget | `video.max_windows` | Ceiling on rectangles per frame (default 120). |
| Minimum box area | `video.min_box_area` | Discard rectangles below N cells. |
| Decomposition algorithm | `video.algo` | `fast` / `balanced` / `quality` — see below. |
| Threshold mode | `video.threshold_mode` | `otsu`, `fixed`, `adaptive`, `edge`. |
| Fixed threshold | `video.fixed_threshold` | Used by `fixed`; also the sensitivity knob for `edge`. |
| Adaptive block / bias | `video.adaptive_block`, `video.adaptive_bias` | Local mean over an NxN window (summed-area table), minus a bias. |
| Invert | `video.invert` | Swap foreground and background. Otsu picks *where* the split is; which side is the subject still needs a human. |
| Luminance levels | `video.levels` | 1 = 1-bit silhouette. 2–16 = greyscale/colour bands, each band its own disjoint set of rectangles so no z-ordering is needed. |
| Gamma / contrast / brightness | `video.gamma`, `video.contrast`, `video.brightness` | Applied through a 256-entry LUT before thresholding, not per pixel. |
| Denoise | `video.denoise` | Drops set cells with no 4-connected set neighbour. A stray cell costs a whole window and reads as noise. |

### The three decomposition algorithms

| Algo | Method | Cost at 96x54 | Use |
|---|---|---|---|
| `fast` | Run-length extraction (`np.diff`) then vectorised merge of identical column spans in consecutive rows (`lexsort` + boundary mask). Exact cover, no Python loop. | ~0.28 ms | Default, live playback |
| `balanced` | `fast`, then least-cost pair merging to fit the window budget. Two neighbours become their bounding box rather than the smaller being deleted. | `fast` + merge rounds | Tight window budgets where detail matters |
| `quality` | Greedy maximal-rectangle cover (histogram-stack). Fewest boxes, O(K·W·H). | ~485 ms at 256x144 | Baking only |

`balanced` exists because **small does not mean unimportant**. Dropping the
smallest rectangles to fit a budget erases eyes, fingers and thin limbs first.
Merging two neighbours into their union keeps the detail and only costs a
little overspill.

---

## 3. Rendering

| Feature | Setting | Notes |
|---|---|---|
| Palettes | `render.palette` | `mono`, `amber`, `matrix`, `ice`, `fire`, `rgb`, `inferno`, `vapor`. Ramps are keyframes interpolated to any `levels` from 2 to 16. |
| Monitor targeting | `render.monitor` | `-1` spans the whole virtual desktop; `0..n` picks one physical monitor. |
| Explicit region | `render.region` | `(x, y, w, h)` in virtual-screen pixels. Also the safe way to test. |
| Fit mode | `render.fit` | `contain` (letterbox), `cover` (crop), `stretch`. |
| Grid gap | `render.gap` | Deflate every window by N px for a visible tile/LED look. |
| Minimum window size | `render.min_window_px` | Below a few pixels a window is invisible and still costs a full `SetWindowPos`. |
| Click-through | `render.click_through` | `WS_EX_TRANSPARENT` — the desktop stays usable while the show is running. |
| No focus stealing | `render.no_activate` | `WS_EX_NOACTIVATE` — windows never take focus from what you are doing. |
| Always on top | `render.topmost` | |
| Redraw mode | `render.redraw` | `accurate` (default) or `fast`. See the measured trade-off below. |
| Frame diffing | `render.diff_frames` | Skip `DeferWindowPos` entirely for slots whose rectangle did not change. |
| Stable slots | `render.stable_slots` | Temporal box→window matching. |
| Blackout backdrop | `render.background_blackout` | A full-screen black window behind the show, so the wallpaper does not read as part of the picture. |
| DPI awareness | — | Per-monitor v2, requested once before any window exists. Without it Windows reports logical pixels and the montage paints into the top-left fraction of a scaled display. |

### Why one window class per colour

The obvious way to colour a window is one class plus a `WM_ERASEBKGND`
handler. That runs a Python callback for every repaint of every window —
thousands of times a second, all holding the GIL. Instead each distinct colour
gets its own registered class with its own `hbrBackground`, so `DefWindowProc`
paints in C with no Python involved. A window never changes colour; it only
moves, shows or hides.

### Why stable slots

`boxgen` returns rectangles in an arbitrary order and the count changes every
frame, so the naive mapping (slot *i* gets box *i*) reshuffles constantly: the
window drawing the character's head becomes the window drawing a foot. Since
each slot is a real HWND being moved, that is exactly the location jitter the
original project's README complains about — and it destroys the diff hit rate,
because a slot whose rectangle changed cannot be skipped.

`SlotTracker` keeps a box within `radius` grid cells of a slot's previous
centre in that slot. Matching is greedy nearest-centre over a coarse bucket
grid whose cell size equals the match radius, so only the 3x3 neighbourhood of
buckets can hold a match and no NxN distance matrix is ever built. At 260
boxes that matrix would be 68k distances per frame; the bucket grid examines
about 380 candidates instead. Slots keep their last centre even on frames
where they draw nothing, so an eye that blinks comes back to the same window.

### Redraw modes, measured

| Mode | fps | Pixel accuracy |
|---|---|---|
| `accurate` (default) | 44 | 100.00% |
| `fast` (`SWP_NOREDRAW`) | 59 | 85% |

The original project recommends `SWP_NOREDRAW` to avoid tearing. It in fact
causes smearing: nothing repaints the area a window vacates. Hand-invalidating
the vacated region was tried, and is slower than `fast` while no more
accurate, so it is not offered as a third mode.

---

## 4. Effects — the "time leap" half

Every effect works on **rectangles, never on pixels**. A trail is literally
the boxes of an older frame re-emitted with a lower `level`, so the palette
dims them for free and the only real cost is more windows in the pool. The
whole stack runs in tens of microseconds per frame.

| Effect | Setting | What it does |
|---|---|---|
| Trails | `effects.trails` | Keep N previous frames faintly on screen. |
| Echo | `effects.echo_offset`, `effects.ghost` | A delayed copy of the picture, blended at `ghost` strength. |
| Slit-scan | `effects.slitscan` | Splice rows sampled from progressively older frames. |
| Jitter | `effects.jitter` | Random per-window pixel offset. |
| Strobe | `effects.strobe` | Blank every Nth frame. |
| Shuffle | `effects.shuffle` | Randomly reorder the box→slot assignment. |
| Time warp | `effects.time_warp`, `effects.warp_period` | Sinusoidal speed modulation, depth 0–1, over a period in seconds. |

Two properties are enforced, not optional:

* **Deterministic.** Jitter and shuffle are seeded from the frame index, not
  from a running RNG, so seeking back to frame 900 repaints frame 900 exactly
  and a paused frame does not strobe. A per-frame RNG would make a hundred
  top-most windows flash at random, which is a photosensitivity hazard rather
  than an aesthetic choice.
* **In bounds.** The renderer maps grid cells to screen rectangles with no
  clamping of its own, so every effect path ends in a clip. Otherwise a
  jittered box becomes a window hanging off the edge of the montage.

A hard ceiling of 2048 boxes per frame applies: trails multiply the box count
by `trails + 1`, and without a cap a slider drag can ask the compositor for
thousands of windows and wedge the desktop.

---

## 5. Media

| Feature | Notes |
|---|---|
| Anything ffmpeg reads | mp4, mkv, webm, avi, mov, gif, image sequences. |
| Honest probing | mkv/webm carry no `nb_frames`; VFR streams have no single frame rate; `r_frame_rate` is occasionally nonsense like `1000/1`. `probe()` always returns numbers you can divide by and flags the ones it had to estimate rather than reporting a confident zero. |
| Rotation | Phone footage hides orientation in a display matrix; it is read and reported. |
| Area downscaling | `-sws_flags area` averages whole blocks of source pixels into each grid cell, so a 96-wide grid reads like the video instead of point-sampled noise. |
| Streaming decode | One ffmpeg process piping raw gray8; the pipeline stays a fixed distance ahead of the playhead. |
| Fast seek | `-ss <seconds>` **before** `-i` for keyframe seek, backed off a quarter frame so it does not round onto the next frame. A seek costs one process restart, not a re-bake. |
| No console flash | Every subprocess uses `CREATE_NO_WINDOW`; ffmpeg additionally gets `-nostdin -loglevel error`. |

### Audio

| Backend | Setting | Notes |
|---|---|---|
| waveOut | `playback.audio_backend = "waveout"` | Default. `winmm` through ctypes. |
| ffplay | `"ffplay"` | Fallback, used automatically if waveOut cannot open. |
| none | `"none"` | Silent. |

`WaveOutTrack` opens the device once for the whole session. ffmpeg only ever
decodes into a raw s16le pipe; a daemon feeder thread cycles eight 32 KiB
`WAVEHDR` buffers through `waveOutWrite`. Pause, resume and volume are device
calls that touch neither ffmpeg nor the queue, and
`waveOutGetPosition(TIME_SAMPLES)` gives a sample-exact playback position —
which is the entire reason the video can be slaved to the audio clock. Only a
seek or a speed change respawns ffmpeg, because only those change what has to
be decoded. The buffer pool doubles as flow control: the feeder blocks when
all buffers are queued, ffmpeg blocks on the full pipe, and nothing decodes
more than ~1.4 s ahead.

Mute (`playback.mute`) and volume 0–100 (`playback.volume`) are live.

---

## 6. Bake cache

| Feature | Notes |
|---|---|
| `.tlp` container | `MAGIC \| u32 version \| u32 json_len \| header JSON \| u64 index[n+1] \| frame blobs`. Each blob is `u16 count` then `count × 5 × u16`. |
| O(1) random access | The index table holds an absolute offset per frame, so frame *i* spans `index[i]..index[i+1]`. **500 frames of random access in 2.0 ms.** |
| Compact | Coordinates are grid cells, so u16 is exact for any grid up to 65535 and a box costs 10 bytes instead of the 16 a float dump used. |
| Content addressed | Keyed twice: the filename carries the source path and the `VideoConfig` fingerprint, and the stored header repeats the source hash and fingerprint, so a hit is *verified* rather than assumed. |
| Cheap source identity | `source_hash` is size + `mtime_ns` + the first and last 64 KB. Digesting a 2 GB video on every startup would take longer than baking a short clip. |
| Self-healing | A stale or corrupt entry is deleted on lookup, so a miss never leaves dead weight for `prune` to charge against the budget. |
| LRU pruning | `prune(max_bytes)` deletes least-recently-used bakes until the cache fits; `lookup` touches mtime, which doubles as the LRU stamp. |

Baking is optional. Streaming playback is fast enough at the default grid;
a bake buys instant start, perfectly smooth scrubbing and smooth reverse, and
lets you use the `quality` algorithm without paying for it at playback time.

---

## 7. Presets

| Preset | Grid | Windows | Levels | Palette | Extra |
|---|---|---|---|---|---|
| Bad Apple (classic) | 64x36 | 90 | 1 | mono | `algo=fast`, Otsu |
| High detail | 160x90 | 260 | 1 | mono | `algo=balanced` |
| Greyscale bands | 112x63 | 220 | 5 | mono | |
| Matrix | 128x72 | 200 | 4 | matrix | `gap=1` |
| Performance | 48x27 | 60 | 1 | mono | frame diffing on |
| Time leap | 96x54 | 150 | 3 | ice | trails 2, ghost 0.45, echo 6, warp 0.3 |

---

## 8. Command line

`timeleap play|bake|info|cache|gui`. `play` is the default, so
`timeleap clip.mp4 --grid 64` is `timeleap play clip.mp4 --grid 64`. The
insertion skips options rather than counting them, and every global option is
a valueless flag, so nothing an option consumes can be mistaken for a command.

| Command | What it does |
|---|---|
| `play` | Plays a file. Opens the UI unless `--no-ui`, which runs headless with a self-rewriting status line (whole lines at a lower rate when redirected, since `\r` overwriting turns a log into one unreadable mega-line). |
| `bake` | Writes a `.tlp` with a throttled percent/rate/ETA progress line, then reports size, bytes per frame, mean boxes per frame and the ratio against the source. |
| `info` | Probes the file and predicts the cost of playing it. |
| `cache` | Lists the cache, prunes it to a byte budget (`500M`, `2G`), or clears it. |
| `gui` | Launches the control panel, optionally pre-loading a file. |

Two implementation choices are load-bearing rather than cosmetic:

* **DPI awareness is set in `main()`** before anything else runs. It is a
  process-wide one-shot decision that Windows refuses once a window exists,
  and `gui` builds a Tk window microseconds later. Doing it inside the UI
  would be too late for the renderer.
* **Everything numpy-shaped is imported inside the command that needs it.**
  `timeleap --version` and `--list-palettes` then cost a few milliseconds
  instead of the ~400 ms an eager numpy import adds to every invocation,
  including shell tab-completion probes.

`info` samples frames spread across the file rather than sequentially from the
start, because the first second of a video is very often a fade from black —
which would report a box count nothing like the real one. It reports mean, min
and max box counts, how many frames hit the budget cap, the boxgen cost, an
estimate of window-move cost at ~0.8 ms per moved window, and the resulting fps
ceiling against the source frame rate, labelled comfortable / tight / too slow.
The estimate is explicitly an upper bound, since diffing skips unchanged
windows.

Exit codes: `0` success, `1` error, `2` usage, `130` interrupted.

---

## 9. Configuration and safety

* Settings round-trip to `%LOCALAPPDATA%/TimeLeapPlayer/config.json`. A save
  failure is swallowed — settings are a convenience and must never be fatal.
* Unknown keys in a config file are ignored, so a file written by an older
  build still loads.
* A big always-reachable **STOP** in the UI plus the global panic hotkey are
  mandatory: two hundred top-most windows can otherwise cover every route out.
* `render.click_through` and `render.no_activate` mean the montage cannot trap
  your mouse or steal your keystrokes.
* Windows-specific code is confined to `render/` and `media/audio.py`;
  everything else imports and unit-tests on any OS.

---

## Differences from the original prototype

The prototype was `WindowVideoPlayer` / `GlitchedAppleVidPlayer`: `src/main.py`
plus `src/boxgen.py` plus `scripts/preprocess.py`.

| Area | Prototype | TimeLeapPlayer |
|---|---|---|
| **Box decomposition** | Greedy largest-rectangle, repeated up to `max_windows` times per frame — O(K·W·H) of pure-Python work | Vectorised run-merge, O(W·H) once. **22x–383x faster**; 64x36 6.9 → 0.30 ms, 256x144 484.3 → 1.26 ms |
| **Worst case** | Fully fragmented frame at 64x36: 44.5 ms | 0.38 ms |
| **Startup** | Decoded and boxed the *whole* video before showing anything — minutes for a typical clip | Streaming: **first frame ~130 ms** after open (~1.3 s from a cold GUI launch), memory bounded regardless of length |
| **Seeking** | Re-run the whole preprocessing pass | One ffmpeg restart with `-ss`, or O(1) from a bake |
| **Redraw** | `SWP_NOREDRAW` always, described as anti-tearing | Default `accurate` at 100.00% pixel accuracy; `fast` offered explicitly as a ~1.4x speed / 85% accuracy trade |
| **Box→window mapping** | Re-assigned every frame by sort order | `SlotTracker` temporal matching — kills the jitter, maximises the diff hit rate |
| **Frame diffing** | None; every window re-submitted every frame | Unchanged slots skipped entirely |
| **Colour** | 1-bit only | 1–16 luminance bands, 8 palettes, one window class per colour so painting happens in C |
| **Cache format** | `boxes.bin`: bare float32 dump, no index, no fps, no settings record — nothing could load it back | `.tlp`: magic, version, JSON header, O(1) index, verified content-addressed store with LRU pruning |
| **Timing** | `time.sleep(delay - work)` after each frame; drift accumulates, 15.6 ms timer granularity | Absolute deadlines from a fixed anchor, 1 ms timer resolution, spin-finished waits |
| **A/V sync** | Impossible — `ffplay` respawned on every pause/seek/volume change, no readable clock | waveOut device held open; `waveOutGetPosition` is sample-exact and acts as master clock |
| **Audio control** | Kill and respawn `ffplay` | Real pause/resume/volume as device calls; only seek and speed respawn ffmpeg |
| **ctypes** | No `argtypes`/`restype`; 64-bit HWNDs silently truncated, so window positioning failed invisibly | Every function explicitly typed |
| **DPI** | Not requested; wrong coordinates on any scaled display | Per-monitor v2, before any window exists |
| **UI thread** | `preprocess_video()` ran inline and froze the UI for minutes; `messagebox.showerror` was called off-thread and deadlocked | Player methods record intent and return at once; every callback is marshalled with `widget.after(0, ...)` |
| **Preview** | A label reading "Integrate vlc-python or mpv for live frame display" | Side-by-side grey grid and surviving rectangles — the two intermediate stages you are actually tuning, which is the thing you cannot see anywhere else |
| **Effects** | None | Trails, echo/ghost, slit-scan, jitter, strobe, shuffle, time warp — deterministic and clipped |
| **Structure** | Two scripts and a helper | Installable `timeleap` package: `core` / `media` / `render` / `engine` / `cache` / `ui` / `cli`, with the Windows-only code isolated |
