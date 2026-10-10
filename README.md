<p align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="assets/logo-dark.png">
    <img src="assets/logo-light.png" alt="TimeLeapPlayer" width="620">
  </picture>
</p>

<img width="2174" height="1067" alt="image" src="https://github.com/user-attachments/assets/cdd592ff-134d-4987-a28b-5cbc0c5cf5c9" />

Play any video on your Windows desktop using **real OS windows as pixels**.
Each frame is decoded to a small grey grid, thresholded, and decomposed into a
few dozen rectangles; each rectangle becomes a pooled, top-most, click-through
window, and the whole frame is committed to the desktop in a single
`DeferWindowPos` transaction. The result is a video playing in the window
manager itself — with real transport controls, audio in sync, colour palettes,
frame-accurate seeking and a set of temporal effects. Inspired by
[mon/bad_apple_virus](https://github.com/mon/bad_apple_virus), which
established the idea; this is an independent Python rewrite built around a
vectorised decomposition algorithm and a streaming pipeline, so it plays
arbitrary video in real time instead of pre-rendered silhouette footage.

---

## Requirements

* **Windows 10 or newer** (64-bit). The renderer is Win32; nothing else works.
* **Python 3.10+**
* **numpy** and **pillow** — installed for you by `pip`
* **ffmpeg** and **ffprobe** on `PATH` — the only dependency pip cannot supply.
  `ffplay` is optional (fallback audio backend only).

Get ffmpeg from [gyan.dev/ffmpeg/builds](https://www.gyan.dev/ffmpeg/builds/)
(take a *full* build, unzip it, add its `bin` folder to `PATH`), or:

```
winget install Gyan.FFmpeg
```

## Install

```
git clone https://github.com/Olexify/TimeLeapPlayer
cd TimeLeapPlayer
install.bat
```

`install.bat` does an editable install and then checks for ffmpeg, telling you
exactly what is missing. The manual equivalent:

```
pip install -e .
```

## Run

```
TimeLeapPlayer.bat
```

Double-click it, or point a desktop shortcut at it. It checks the environment,
hands off to `pythonw.exe` and exits, so no console window sits behind the
player. Given arguments it stays on the console instead, so
`TimeLeapPlayer.bat info clip.mp4` still prints its output.

`run.bat` does the same thing but keeps the console attached for the whole
session — useful when you want to watch stdout. Both work from any directory
and neither needs the package to be pip-installed.

Equivalently, once installed:

```
timeleap gui
timeleap clip.mp4
python -m timeleap gui
```

---

## Usage

### The player window

Open a video and it plays in a window-sized region of your desktop — no
frame, just the pixels. **Hover over it** and the controls fade in, like any
media player; leave it alone for a couple of seconds and they fade out.

| On the video | Does |
|---|---|
| Hover | Show the controls: title, settings, fullscreen, close, and a play bar with seek, time and volume |
| Drag | Move the window |
| Drag an edge or corner | Resize it (the aspect ratio stays locked) |
| Click | Play / pause |
| Double-click | Fullscreen, and back |
| Wheel | Volume |
| Right-click | Quick menu: open, recent, style, palette, speed, loop, backdrop, always on top, settings |

Hover over the seek bar to see the time under the pointer; drag it to scrub
and the picture follows. Volume, speed and style changes show a short
on-screen message. In fullscreen the pointer hides when idle. The player has
a taskbar button and an Alt-Tab entry, and its position and size are
remembered between sessions.

With nothing playing you get the **home window**: an *Open video* button and
your recent files. **Settings** (the gear on the video or on the home window)
holds everything else, split into *Picture*, *Playback*, *Effects*,
*Advanced*, *Cache* and *About*; changes apply **live** without reloading.

| Key | Action |
|---|---|
| `Space` / `K` | Play / pause |
| `Left` / `Right` | Seek 5 seconds |
| `Up` / `Down` | Volume |
| `F` / `Esc` | Fullscreen / leave fullscreen |
| `M` | Mute |
| `Ctrl+O` / `Ctrl+N` | Open a file / next file in the folder |
| **`Ctrl+Alt+Q`** | **PANIC — global**, hides everything and stops playback |
| `Ctrl+Alt+Space` | Play / pause — global |
| `Ctrl+Alt+Right` | Next file — global |

The last three are registered system-wide on their own message thread, so they
work even when the player is buried or the app is unresponsive.

### Click-through

The pixel windows themselves are click-through: they are layered
(`WS_EX_LAYERED | WS_EX_TRANSPARENT`), so clicks pass straight through them.
An earlier build set `WS_EX_TRANSPARENT` alone, which Windows ignores for
hit-testing on a non-layered top-level window — a real click over a white box
was swallowed. The player window's own input comes from an invisible 1%-alpha
surface laid over the picture, which is what makes drag, click and hover work.

From the command line, `--region X,Y,W,H` places the window; coordinates are
virtual-desktop pixels, so negative values work on multi-monitor setups.

<img width="2021" height="1196" alt="image" src="https://github.com/user-attachments/assets/2c1f96a7-7e78-4efa-8d3e-6f1baa479790" />

### The CLI

```
timeleap clip.mp4                     play it in the UI
timeleap clip.mp4 --no-ui --grid 64   play it headless on a 64-wide grid
timeleap bake clip.mp4                pre-compute geometry into the cache
timeleap info clip.mp4 --levels 4     what it would cost to play
timeleap cache --prune 500M           trim the bake cache
```

`play` is the default command, so a bare filename works.

| Command | Purpose |
|---|---|
| `play VIDEO` | Play a video. Opens the UI unless `--no-ui` |
| `bake VIDEO` | Pre-compute box geometry into a `.tlp` file |
| `info VIDEO` | Probe a video and predict what it costs to play |
| `cache` | Inspect or trim the bake cache |
| `gui [VIDEO]` | Launch the app, optionally pre-loading a file |

Global options: `--version`, `--list-monitors`, `--list-palettes`.

**Geometry options**, accepted by `play`, `bake`, `info` and `gui`:

| Option | Meaning |
|---|---|
| `--grid N\|WxH` | Grid width; height follows the source aspect unless you give `WxH` |
| `--windows N` | Maximum windows drawn per frame |
| `--levels N` | 1 = silhouette, 2..16 = luminance bands |
| `--algo {fast,balanced,quality}` | Box decomposition algorithm |
| `--threshold {otsu,fixed,adaptive,edge}` | Thresholding mode |
| `--invert` | Swap dark and light |
| `--preset NAME` | Start from a preset (prefixes work: `--preset matrix`) |

**`play` also takes:**

| Option | Meaning |
|---|---|
| `--palette NAME` | Colour ramp (`--list-palettes` shows them) |
| `--monitor N\|all` | Which monitor to cover |
| `--region X,Y,W,H` | Confine the show to this pixel rectangle |
| `--speed X` | Playback rate |
| `--loop {off,loop,pingpong}` | What to do at the end |
| `--reverse` | Play backwards (silent) |
| `--mute` | No audio |
| `--volume N` | 0..100 |
| `--blackout` | Black backdrop window behind the show |
| `--no-cache` | Ignore any baked geometry and stream instead |
| `--no-ui` | Run headless with a live status line |
| `--duration SEC` | Stop after SEC seconds (with `--no-ui`) |

`bake` additionally takes `--out FILE` (default: the bake cache, found
automatically). `info` additionally takes `--palette`. `gui` additionally takes
`--palette`, `--monitor` and `--region`. `cache` takes `--list` (the default),
`--prune BYTES` (accepts `500M`, `2G`) and `--clear`.

`info` is the one to reach for before a long render — it samples frames from
across the file rather than the first second, which is so often a fade from
black that it would report a box count nothing like the real one:

```
$ timeleap info clip.mp4 --levels 4 --grid 96
Source      ...\clip.mp4
            640x360, h264, 253.7 KiB
            30 fps, 10.00 s, 300 frames, audio yes
Geometry    grid 96x54 (auto from the source aspect), 4 levels, algo fast, threshold otsu
            budget 90 windows/frame (renderer ceiling 512 per level), palette mono
Boxes       sampled 30 frames across the file: mean 45.8, min 37, max 74
Cost        boxgen 0.70 ms/frame + ~62.7 ms of window moves (~1.37 ms each)
            ~16 fps ceiling against 30 fps of source: too slow -- lower --grid or --windows
            diffing skips unchanged windows, so this is an upper bound
Cache       miss -- `timeleap bake` would write ...\anim-086cdeae449f21f6-9b520e3a0fb5c51e983182d6.tlp
```

Note where the time goes: decomposition is well under a millisecond, and
moving windows is sixty times that. Past a certain grid the bottleneck is the
desktop, not Python.

---

## Features

| Area | Feature |
|---|---|
| **Playback** | Play / pause / stop, frame-accurate seek, 0.1x–4.0x speed, real reverse, loop and ping-pong, automatic frame dropping |
| **Sync** | Absolute-deadline scheduling with 1 ms timer resolution; audio acts as master clock via `waveOutGetPosition` |
| **Audio** | waveOut through ctypes (default), ffplay fallback, or silent. Real pause/resume/volume without respawning ffmpeg. Pitch-preserving speed change |
| **Geometry** | Configurable grid (default 96x54), auto grid height from the source aspect ratio, per-frame window budget, minimum box area |
| **Preview** | Side-by-side pane showing the grey grid the decoder produced and the rectangles that survived thresholding, levels and the window budget |
| **Thresholding** | Otsu (per-frame adaptive), fixed, local-adaptive, edge/outline; invert; gamma, contrast, brightness; speckle removal |
| **Colour** | 1-bit silhouette or 2–16 luminance bands, 8 palettes (`mono`, `amber`, `matrix`, `ice`, `fire`, `rgb`, `inferno`, `vapor`) |
| **Decomposition** | Three algorithms: `fast` (vectorised run-merge), `balanced` (budget-aware merging), `quality` (greedy maximal rectangle) |
| **Rendering** | Batched `DeferWindowPos`, per-slot frame diffing, temporally stable box→window assignment, click-through, no focus stealing, per-monitor-v2 DPI awareness |
| **Targeting** | Whole virtual desktop, one monitor, or an explicit pixel region; contain / cover / stretch; grid gap; optional blackout backdrop |
| **Effects** | Trails, echo + ghost blend, slit-scan, jitter, strobe, shuffle, sinusoidal time warp — all deterministic and clipped to the grid |
| **Cache** | Indexed `.tlp` bake format with O(1) random access, content-addressed and verified, LRU pruned |
| **Presets** | Bad Apple (classic), High detail, Greyscale bands, Matrix, Performance, Time leap |
| **Safety** | Global panic hotkey, close button on the player, truly click-through pixel windows, no console flashes |

The full annotated list is in [docs/FEATURES.md](docs/FEATURES.md); the module
contracts are in [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md).

---

## How it works

```
video file
   │
   ├─ probe          ffprobe → fps, duration, frame count, rotation, audio
   │
   ├─ decode         one ffmpeg process piping raw gray8 at grid resolution,
   │                 `-sws_flags area` so each cell averages a block of source
   │                 pixels instead of point-sampling it
   │
   ├─ threshold      Otsu (or fixed / adaptive / edge) → boolean mask, or
   │                 quantised into 2–16 disjoint luminance bands
   │
   ├─ decompose      mask → (N,5) int32 rectangles: x, y, w, h, level,
   │                 in grid cells. Run-length extraction, then a vectorised
   │                 merge of identical column spans in consecutive rows
   │
   ├─ pipeline       worker threads keep a bounded ring buffer ahead of the
   │                 playhead, so playback starts immediately and memory does
   │                 not grow with video length
   │
   ├─ effects        optional temporal distortions, applied to rectangles
   │
   ├─ clock          absolute deadlines from a fixed anchor; audio is master
   │                 when audible; late frames drop instead of accumulating lag
   │
   ├─ track          each rectangle is matched to the window slot that drew
   │                 the nearest rectangle last frame
   │
   └─ render         one BeginDeferWindowPos / EndDeferWindowPos batch per
                     frame, with unchanged slots skipped entirely
```

Four decisions do most of the work:

**Rectangles, not cells.** A 96x54 grid is 5184 cells, but a typical frame
needs only about 33 rectangles to cover its mask exactly. One window per cell
is both impossibly slow and visually meaningless.

**Run-merge instead of greedy.** The classic approach repeatedly finds the
single largest all-set rectangle and blanks it, up to the window budget — that
is `O(K·W·H)` of interpreted work per frame. Extracting horizontal runs with
one `np.diff`, then coalescing identical column spans in consecutive rows with
one `lexsort` and a boundary mask, is `O(W·H)` **once**, with no Python loop.
It is an exact cover, never paints a cell outside the mask, and on real
silhouette footage produces *fewer* rectangles than the greedy version.

**Streaming, not pre-processing.** A producer thread stays a second or two
ahead of the playhead. The first frame appears in about a tenth of a second
regardless of how long the video is, seeking costs one ffmpeg restart, and
memory is bounded.

**Stable slots.** Each rectangle keeps the HWND that drew the nearest
rectangle last frame, matched over a coarse bucket grid in `O(N)`. That
removes the position jitter caused by re-assigning windows by sort order, and
it is what makes frame diffing effective — a slot whose rectangle did not
change is skipped entirely.

---

## Performance

All figures measured on one machine (Windows 10 x64, Python 3.14, numpy 2.4).

### Box decomposition, per frame

| Grid | Greedy largest-rectangle | Run-merge |
|---|---|---|
| 64x36 | 6.9 ms | **0.30 ms** |
| 96x54 | 22.6 ms | **0.28 ms** |
| 128x72 | 56.6 ms | **0.37 ms** |
| 192x108 | 190.7 ms | **0.94 ms** |
| 256x144 | 484.3 ms | **1.26 ms** |

That is **22x to 383x faster** across the range. The greedy column is why the
prototype could not hold frame rate above a 64x36 grid: at 96x54 it spent
22.6 ms per frame on decomposition alone, before decoding or drawing anything.

Worst case — a fully fragmented frame, the input the run-merge has least to
coalesce — at 64x36: **44.5 ms → 0.38 ms**.

### Time to first frame

**~130 ms** from opening a file to the first rendered frame, streaming, and
independent of video length — about **1.3 s** measured from a cold `run.bat`,
of which ~0.7 s is Python import plus building the Tk window. The previous
build decoded and boxed the entire video before showing anything, which is
minutes for a typical clip.

### Rendering

Throughput is set by **boxes per frame**, not by grid size. Each window costs
roughly 1.4 ms of Win32 time, so the useful rule is *fps ≈ 730 / boxes*.
Measured uncapped into a 960x540 region, median of three runs:

| Grid | Window budget | Boxes/frame | fps |
|---|---|---|---|
| 48x27 | 60 | 17.6 | 41.7 |
| **64x36** (default) | **90** | **19.3** | **33.7** |
| 80x45 | 100 | 23.6 | 26.9 |
| 96x54 | 120 | 31.3 | 19.9 |
| 128x72 | 200 | 30.5 | 23.7 |
| 160x90 | 260 | 44.8 | 17.7 |

The default is 64x36 because it clears 30 fps with headroom. Larger grids look
better and drop frames; the stats strip shows render fps, boxes and dropped
frames live, so the trade is visible while you drag the slider. These are
whole-desktop numbers and move with machine load — treat the *ratios* as the
stable part.

**Redraw mode**, same content:

| Redraw mode | Relative speed | Pixel accuracy |
|---|---|---|
| `accurate` (default) | 1.0x | **100.00%** |
| `fast` (`SWP_NOREDRAW`) | ~1.4x | 85% |

Accuracy was verified by screenshotting the desktop and comparing it against
the expected rectangle mask. `SWP_NOREDRAW` is commonly recommended as an
anti-tearing measure; in practice it *causes* smearing, because nothing
repaints the area a window vacates. That 15-point accuracy loss is why
`accurate` is the default. Hand-invalidating the vacated region was tried and
is slower than `fast` while no more accurate, so it is not offered.

### Cache

**500 frames of random access in 2.0 ms** from a baked `.tlp` file — the index
table gives an absolute file offset per frame, so any frame is one seek away.
This is what makes scrubbing and reverse playback smooth.

---

## Tuning

**Grid** is the decode resolution and the main quality/cost dial.
`96x54` (default) is a good balance. `64x36` is the classic look and very
cheap. `128x72` and up resolve fine detail but need more windows, and every
window is real OS work — the cost is in the window manager, not in the
decomposition, which stays under a millisecond even at 256x144.

**Max windows** caps rectangles per frame. Lower (60–90) is faster and
cleaner; higher (150–260) keeps detail on busy frames. If you are hitting the
budget often, switch `algo` to `balanced`: it merges neighbouring rectangles
into their bounding box instead of deleting the smallest ones, which preserves
eyes, fingers and thin limbs that a size-based cull removes first.

**Levels** turns the silhouette into greyscale. `1` is the classic 1-bit look.
`3–5` reads as real greyscale. Above about 8 the extra bands cost windows
without adding much. Combine with a palette for colour.

**Algorithm.** `fast` for live playback — it is an exact cover and the
default. `balanced` when the window budget is tight. `quality` produces the
fewest rectangles but is hundreds of times slower; use it only when baking.

**Threshold mode.** `otsu` adapts per frame and handles almost anything.
`fixed` is stable across a cut-heavy edit where Otsu would flicker.
`adaptive` copes with uneven lighting. `edge` traces outlines instead of
filling shapes.

**Bake** (`timeleap bake`) when you want instant start, perfectly smooth
scrubbing, smooth reverse, or the `quality` algorithm without paying for it at
playback time. Streaming is fast enough that baking is otherwise optional.

**Confine it while experimenting.** Set an explicit render region (a few
hundred pixels wide) so the montage does not take over your desktop while you
are dialling settings in.

---

## Troubleshooting

**No windows appear at all — only the home window.**
Check the stats readout. If `boxes` is 0, the mask is empty: the frame is too
uniform for the current threshold, or `invert` is backwards for this footage.
Try `invert`, or switch `threshold_mode` to `fixed` and move the threshold. If
`boxes` is non-zero but nothing is on screen, the render target is likely off
your visible desktop — set `monitor` back to `-1` (whole virtual desktop) and
clear any custom region.

**The video looks inverted — the background lights up instead of the subject.**
Toggle **invert**. Otsu decides *where* to split light from dark, but not
which side is the subject; dark-on-light footage needs the flip.

**Playback is choppy.**
Look at the stats. High `dropped` with low `decode fps` means decoding is the
bottleneck — lower the grid. High `batch_ms` means the window manager is the
bottleneck — lower `max_windows`, raise `min_window_px`, or switch to a
smaller grid; the desktop compositor, not Python, is the limit. A large
`drift_ms` with audio playing means the video is chasing the audio clock,
which usually resolves itself; if not, raise `prefetch_frames`. Effects with
`trails` set multiply the box count by `trails + 1`, so they cost real frame
rate. Closing other topmost/overlay software (game bars, screen recorders,
GPU overlays) helps more than you would expect.

**No audio.**
Confirm the file actually has an audio track — the stats and `timeleap info`
both report it. Check `mute` and `volume`. Reverse playback is silent by
design: audio cannot run backwards. If the waveOut backend failed to open
(another exclusive-mode application holding the device is the usual cause),
switch the backend to `ffplay`, which needs `ffplay` on `PATH`.

**"ffmpeg not found" / nothing loads.**
`ffmpeg` and `ffprobe` must both be on `PATH`. Run `ffmpeg -version` in a
*new* terminal — a PATH change does not reach terminals that were already
open. Install with `winget install Gyan.FFmpeg`, or unzip a full build from
[gyan.dev](https://www.gyan.dev/ffmpeg/builds/) and add its `bin` folder.

**Everything is covered in windows and I cannot get out.**
Press **`Ctrl+Alt+Q`**. That is the panic hotkey: it hides every window and
stops playback immediately, and it is registered on its own message thread so
it works even if the UI is busy. The windows are click-through and never take
focus, so your normal `Alt+Tab` and taskbar still work underneath them.

---

## Project layout

```
src/timeleap/
  config.py     settings model, presets, JSON persistence
  core/         thresholding, box decomposition, palettes, slot tracking, effects
  media/        ffprobe wrapper, streaming ffmpeg decoder, waveOut audio
  render/       typed ctypes bindings, window pool, DeferWindowPos renderer
  engine/       frame clock and A/V sync, decode pipeline, player transport
  cache/        .tlp bake container and content-addressed store
  ui/           home window, player overlay, settings, widgets, global hotkeys
  cli.py        command line entry point
tests/          unit tests (pytest)
docs/           ARCHITECTURE.md, FEATURES.md
```

Windows-specific code is confined to `render/` and `media/audio.py`;
everything else imports and unit-tests on any platform.

```
pip install -e ".[dev]"
pytest
```

---

## Brand assets

`assets/` holds the mark in every form: `icon.svg`, `icon.ico`, PNGs from 16
to 512 px, the light and dark README wordmarks, and the GitHub social card.
`src/timeleap/assets/timeleap.ico` is the copy the app itself loads for its
window, shipped inside the package.

All of it is generated from one geometry definition:

```
python tools/make_brand.py
```

Editing that script and re-running it keeps the SVG, the bitmaps, the `.ico`
and the social card from drifting apart.


<img width="1046" height="837" alt="image" src="https://github.com/user-attachments/assets/f619b80b-e697-45e7-8551-662ab6d41f82" />


## Credits

The idea of using OS windows as a video display comes from
[mon/bad_apple_virus](https://github.com/mon/bad_apple_virus), whose README
also documents the two facts this project's renderer is built around: that
batching window moves is the difference between 1 fps and something watchable,
and that naive box-to-window assignment makes windows jitter.

## License

MIT — see [LICENSE](LICENSE).
