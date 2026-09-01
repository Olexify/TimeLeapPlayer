"""Command line front end: `timeleap play|bake|info|cache|gui`.

Two things here are load-bearing rather than cosmetic.

DPI awareness is set in `main()` before anything else runs, because it is a
process-wide, one-shot decision that Windows refuses once a window exists --
and `gui` builds a Tk window microseconds later. Doing it inside the UI would
be too late for the renderer and doing it twice is an error.

Everything numpy-shaped (`engine`, `cache`, `media.decoder`) is imported inside
the command that needs it. `timeleap --list-palettes` and `timeleap --version`
then cost a few milliseconds instead of the ~400 ms an eager numpy import adds
to every invocation, including tab-completion probes.
"""
from __future__ import annotations

import argparse
import sys
import threading
import time
from pathlib import Path
from typing import Any, Callable, Iterable, Sequence

from .config import PRESETS, AppConfig, apply_preset, cache_dir
from .core import palette
from .media.probe import MediaError, MediaInfo, have_ffmpeg, probe
from .render import win32 as w

try:                                    # the package may not declare one yet
    from . import __version__ as VERSION
except ImportError:                     # pragma: no cover
    VERSION = "0.1.0"

EXIT_OK, EXIT_ERROR, EXIT_USAGE, EXIT_INTERRUPT = 0, 1, 2, 130

COMMANDS = ("play", "bake", "info", "cache", "gui")
ALGOS = ("fast", "balanced", "quality")
THRESHOLDS = ("otsu", "fixed", "adaptive", "edge")
LOOPS = ("off", "loop", "pingpong")

MAX_WINDOWS = 512               # render.renderer._MAX_PER_LAYER, the desktop-heap cap
STATUS_INTERVAL = 0.1
PIPED_INTERVAL = 2.0            # a redirected status line cannot rewrite itself

# Measured against the real renderer across grids from 48x27 to 160x90: fps
# tracks boxes per frame, not grid size, at roughly 730 / boxes -- about
# 1.37 ms per moved window. (Synthetic scatter is cheaper, near 0.8 ms; real
# footage has fewer, larger rectangles, so Windows repaints more desktop per
# window.) It is a planning figure, not a promise: the true cost tracks
# repaint area, so it moves with region size, monitor count and machine load.
MS_PER_WINDOW = 1.37

INFO_SAMPLE_POINTS = (0.05, 0.25, 0.5, 0.75, 0.95)
INFO_SAMPLE_DEPTH = 6

EPILOG = """\
examples:
  timeleap clip.mp4                     play it in the UI
  timeleap clip.mp4 --no-ui --grid 64   play it headless on a 64-wide grid
  timeleap bake clip.mp4                pre-compute geometry into the cache
  timeleap info clip.mp4 --levels 4     what it would cost to play
  timeleap cache --prune 500M           trim the bake cache

Ctrl+Alt+Q panics out of a running show from anywhere.
"""


class CliError(RuntimeError):
    """A message for the user, not a traceback."""

    def __init__(self, message: str, code: int = EXIT_ERROR) -> None:
        super().__init__(message)
        self.code = code


# ---- argument types --------------------------------------------------
def _ranged(lo: int, hi: int) -> Callable[[str], int]:
    def parse(text: str) -> int:
        try:
            value = int(text, 10)
        except ValueError:
            raise argparse.ArgumentTypeError(f"{text!r} is not a whole number") from None
        if not lo <= value <= hi:
            raise argparse.ArgumentTypeError(f"{value} is outside {lo}..{hi}")
        return value
    return parse


def _speed(text: str) -> float:
    try:
        value = float(text)
    except ValueError:
        raise argparse.ArgumentTypeError(f"{text!r} is not a number") from None
    if not 0.05 <= value <= 8.0:
        raise argparse.ArgumentTypeError("speed must be between 0.05 and 8")
    return value


def _seconds(text: str) -> float:
    try:
        value = float(text)
    except ValueError:
        raise argparse.ArgumentTypeError(f"{text!r} is not a number of seconds") from None
    if value <= 0:
        raise argparse.ArgumentTypeError("duration must be positive")
    return value


def _grid(text: str) -> tuple[int, int | None]:
    """`96` (height follows the source aspect) or `96x54` (both pinned)."""
    body = text.lower().replace("×", "x")
    parts = body.split("x")
    if len(parts) > 2:
        raise argparse.ArgumentTypeError(f"{text!r} is not N or WxH")
    try:
        dims = [int(p, 10) for p in parts]
    except ValueError:
        raise argparse.ArgumentTypeError(f"{text!r} is not N or WxH") from None
    for d in dims:
        if not 4 <= d <= 1024:
            raise argparse.ArgumentTypeError(f"grid size {d} is outside 4..1024")
    return (dims[0], dims[1] if len(dims) == 2 else None)


def _region(text: str) -> tuple[int, int, int, int]:
    parts = [p for p in text.replace("x", ",").split(",") if p.strip()]
    if len(parts) != 4:
        raise argparse.ArgumentTypeError(f"{text!r} is not X,Y,W,H")
    try:
        x, y, width, height = (int(p, 10) for p in parts)
    except ValueError:
        raise argparse.ArgumentTypeError(f"{text!r} is not four whole numbers") from None
    if width <= 0 or height <= 0:
        raise argparse.ArgumentTypeError("region width and height must be positive")
    return (x, y, width, height)


def _monitor(text: str) -> int:
    if text.strip().lower() in ("all", "virtual", "-1"):
        return -1
    try:
        index = int(text, 10)
    except ValueError:
        raise argparse.ArgumentTypeError(f"{text!r} is not a monitor index or 'all'") from None
    if index < 0:
        raise argparse.ArgumentTypeError("monitor index cannot be negative; use 'all'")
    return index


_SUFFIXES = {"k": 1 << 10, "m": 1 << 20, "g": 1 << 30, "t": 1 << 40}


def _byte_size(text: str) -> int:
    body = text.strip().lower().rstrip("b")
    scale = 1
    if body and body[-1] in _SUFFIXES:
        scale, body = _SUFFIXES[body[-1]], body[:-1]
    try:
        value = float(body)
    except ValueError:
        raise argparse.ArgumentTypeError(f"{text!r} is not a byte size (try 500M)") from None
    if value < 0:
        raise argparse.ArgumentTypeError("a byte budget cannot be negative")
    return int(value * scale)


# ---- formatting ------------------------------------------------------
def _human_bytes(n: float) -> str:
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if abs(n) < 1024 or unit == "TiB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024.0
    return f"{n:.1f} TiB"        # pragma: no cover - unreachable, keeps mypy calm


def _hms(seconds: float) -> str:
    if seconds != seconds or seconds in (float("inf"), float("-inf")) or seconds < 0:
        return "--:--"
    total = int(seconds)
    if total >= 3600:
        return f"{total // 3600}:{total // 60 % 60:02d}:{total % 60:02d}"
    return f"{total // 60}:{total % 60:02d}"


class Status:
    """A one-line status that rewrites itself, or paces itself when redirected.

    `\\r` overwriting turns a redirected log into one unreadable mega-line, so
    a non-tty gets whole lines at a much lower rate instead.
    """

    def __init__(self, stream: Any = None) -> None:
        self.stream = stream if stream is not None else sys.stdout
        try:
            self.tty = bool(self.stream.isatty())
        except (AttributeError, ValueError):
            self.tty = False
        self._width = 0
        self._last = 0.0

    def show(self, text: str, force: bool = False) -> None:
        now = time.monotonic()
        if not self.tty:
            if not force and now - self._last < PIPED_INTERVAL:
                return
            self._write(text + "\n")
        else:
            self._write("\r" + text + " " * max(0, self._width - len(text)))
            self._width = len(text)
        self._last = now

    def clear(self) -> None:
        if self.tty and self._width:
            self._write("\r" + " " * self._width + "\r")
        self._width = 0

    def line(self, text: str = "") -> None:
        """A permanent line, never overwritten by the next status."""
        self.clear()
        self._write(text + "\n")

    def _write(self, text: str) -> None:
        try:
            self.stream.write(text)
            self.stream.flush()
        except UnicodeEncodeError:
            # The player's own messages contain characters a legacy console
            # codepage cannot encode. Dropping the line would hide the state.
            encoding = getattr(self.stream, "encoding", None) or "ascii"
            self._write(text.encode(encoding, "replace").decode(encoding, "replace"))
        except (OSError, ValueError):
            pass                    # a closed or broken pipe must not kill playback


# ---- configuration ---------------------------------------------------
def _resolve_preset(name: str) -> str:
    lowered = name.strip().lower()
    for key in PRESETS:
        if key.lower() == lowered:
            return key
    hits = [k for k in PRESETS if k.lower().startswith(lowered)]
    if len(hits) == 1:
        return hits[0]
    raise CliError(f"unknown preset {name!r}; try one of: "
                   + ", ".join(repr(k) for k in PRESETS), EXIT_USAGE)


def _configure(args: argparse.Namespace) -> AppConfig:
    """User config, then the preset, then explicit flags -- most specific wins."""
    cfg = AppConfig.load()
    v, r, pb = cfg.video, cfg.render, cfg.playback

    if getattr(args, "preset", None):
        apply_preset(cfg, _resolve_preset(args.preset))

    grid = getattr(args, "grid", None)
    if grid is not None:
        v.grid_w = grid[0]
        if grid[1] is not None:
            v.grid_h, v.auto_grid = grid[1], False
        else:
            v.auto_grid = True
    for attr, target, field in (
        ("windows", v, "max_windows"), ("levels", v, "levels"),
        ("algo", v, "algo"), ("threshold", v, "threshold_mode"),
        ("palette", r, "palette"), ("monitor", r, "monitor"),
        ("region", r, "region"), ("speed", pb, "speed"), ("loop", pb, "loop"),
        ("volume", pb, "volume"),
    ):
        value = getattr(args, attr, None)
        if value is not None:
            setattr(target, field, value)
    if getattr(args, "invert", False):
        v.invert = True
    if getattr(args, "reverse", False):
        pb.reverse = True
    if getattr(args, "mute", False):
        pb.mute = True
    if getattr(args, "blackout", False):
        r.background_blackout = True
    if getattr(args, "no_cache", False):
        cfg.use_cache = False

    if r.palette not in palette.names():
        raise CliError(f"unknown palette {r.palette!r}; try one of: "
                       + ", ".join(palette.names()), EXIT_USAGE)
    return cfg


def _fit_grid(cfg: AppConfig, aspect: float) -> None:
    """Derive grid height from the source aspect unless the user pinned it."""
    v = cfg.video
    if v.auto_grid and aspect > 0:
        v.grid_h = max(4, min(1024, int(round(v.grid_w / aspect))))


def _resolve_path(raw: str) -> str:
    path = Path(raw).expanduser()
    if not path.exists():
        raise CliError(f"no such file: {raw}")
    if not path.is_file():
        raise CliError(f"not a file: {raw}")
    return str(path.resolve())


def _require_ffmpeg() -> None:
    ffmpeg, ffprobe, _ = have_ffmpeg()
    missing = [n for n, ok in (("ffmpeg", ffmpeg), ("ffprobe", ffprobe)) if not ok]
    if missing:
        raise CliError(
            f"{' and '.join(missing)} not found on PATH. TimeLeapPlayer decodes "
            "video with ffmpeg; install it from https://ffmpeg.org/download.html "
            "and reopen your terminal.", EXIT_USAGE)


def _describe(cfg: AppConfig) -> str:
    v, r = cfg.video, cfg.render
    bits = [f"{v.grid_w}x{v.grid_h} grid", f"{v.max_windows} windows",
            f"{v.levels} level{'s' if v.levels != 1 else ''}", r.palette,
            v.algo]
    if r.region:
        bits.append("region %d,%d %dx%d" % tuple(r.region))
    elif r.monitor >= 0:
        bits.append(f"monitor {r.monitor}")
    else:
        bits.append("all monitors")
    return ", ".join(bits)


# ---- helper listings -------------------------------------------------
def list_monitors() -> None:
    virtual = w.virtual_screen_monitor()
    print(f"Virtual desktop: {virtual.width}x{virtual.height} at "
          f"{virtual.x},{virtual.y}")
    for mon in w.monitors():
        print(f"  --monitor {mon.label()}")
    print("  --monitor all   span every monitor (the default)")


def list_palettes(levels: int = 5) -> None:
    print(f"Palettes (resolved at {levels} levels, dim -> bright):")
    for name in palette.names():
        colors = palette.resolve(name, levels)
        # resolve() hands back COLORREF (0x00BBGGRR); unswizzle for display.
        swatch = " ".join(f"#{c & 0xFF:02X}{(c >> 8) & 0xFF:02X}{(c >> 16) & 0xFF:02X}"
                          for c in colors)
        print(f"  {name:<8} {swatch}")


# ---- play ------------------------------------------------------------
def _status_text(stats: Any) -> str:
    note = f"  {stats.message}" if stats.message else ""
    return (f"[{stats.state:<7}] {stats.frame:>6}/{stats.total_frames:<6} "
            f"{stats.position:6.1f}/{stats.duration:.1f}s  "
            f"render {stats.render_fps:5.1f}  decode {stats.decode_fps:5.1f}  "
            f"boxes {stats.boxes:>4}  win {stats.windows:>4}  "
            f"drop {stats.dropped:>4}  buf {stats.buffered:>4}  "
            f"{stats.source}{note}")


def _play_headless(cfg: AppConfig, path: str, duration: float | None) -> int:
    from .engine.player import Player
    from .ui.hotkeys import HotkeyManager, label

    status = Status()
    done = threading.Event()
    errors: list[str] = []
    player = Player(cfg)

    # These fire on the render thread. There is no Tk here, so they only touch
    # an Event and a list -- both safe -- and never the status line, which the
    # main thread is rewriting.
    def on_error(message: str) -> None:
        errors.append(message)
        done.set()

    player.on_error = on_error
    player.on_end = done.set

    def on_panic() -> None:
        player.panic()
        done.set()

    keys = HotkeyManager(on_panic=on_panic, on_playpause=player.toggle)
    hotkeys_ok = keys.start() if cfg.panic_hotkey else False

    quit_hint = f"{label('panic')} panic, " if hotkeys_ok else ""
    status.line(f"Playing {Path(path).name}  [{_describe(cfg)}]")
    status.line(f"  {quit_hint}Ctrl+C to stop"
                + (f", stopping after {duration:g}s" if duration else ""))
    if keys.failed:
        status.line(f"  note: {keys.error}")

    interrupted = False
    started = time.perf_counter()
    player.open(path)
    try:
        while not done.wait(STATUS_INTERVAL):
            if duration is not None and time.perf_counter() - started >= duration:
                break
            status.show(_status_text(player.stats()))
    except KeyboardInterrupt:
        interrupted = True
    finally:
        final = player.stats()
        status.clear()
        keys.stop()
        player.panic()
        player.close()

    if errors:
        print(f"timeleap: {errors[0]}", file=sys.stderr)
        return EXIT_ERROR
    elapsed = time.perf_counter() - started
    status.line(f"{'Interrupted' if interrupted else 'Finished'} after "
                f"{elapsed:.1f}s: {final.frame + 1} frames shown at "
                f"{final.render_fps:.1f} fps, {final.dropped} dropped, "
                f"source {final.source or 'n/a'}")
    return EXIT_INTERRUPT if interrupted else EXIT_OK


def _launch_ui(cfg: AppConfig, initial: str | None) -> int:
    try:
        from .ui.app import TimeLeapApp
    except Exception as exc:
        raise CliError(f"the Tk control panel is unavailable ({type(exc).__name__}: "
                       f"{exc}). Use --no-ui to play from the terminal.",
                       EXIT_USAGE) from exc
    TimeLeapApp(cfg, initial).run()
    return EXIT_OK


def cmd_play(args: argparse.Namespace) -> int:
    cfg = _configure(args)
    path = _resolve_path(args.video)
    _require_ffmpeg()
    _fit_grid(cfg, probe(path).aspect)
    if not args.no_ui:
        if args.duration is not None:
            print("timeleap: --duration only applies with --no-ui; ignoring it",
                  file=sys.stderr)
        return _launch_ui(cfg, path)
    return _play_headless(cfg, path, args.duration)


# ---- bake ------------------------------------------------------------
class _BakeProgress:
    """Throttled `write_bake` progress callback: percent, rate and ETA."""

    def __init__(self, status: Status, total: int) -> None:
        self.status = status
        self.total = max(0, total)
        self.started = time.perf_counter()
        self._last = 0.0
        self.done = 0

    def __call__(self, count: int) -> None:
        self.done = count
        now = time.monotonic()
        if now - self._last < STATUS_INTERVAL:
            return
        self._last = now
        self.status.show(self.text())

    def text(self) -> str:
        elapsed = time.perf_counter() - self.started
        rate = self.done / elapsed if elapsed > 0 else 0.0
        if self.total:
            share = min(1.0, self.done / self.total)
            eta = (self.total - self.done) / rate if rate > 0 else float("inf")
            return (f"  baking {self.done:>6}/{self.total} frames  "
                    f"{share * 100:5.1f}%  {rate:6.1f} fps  ETA {_hms(eta)}")
        return f"  baking {self.done:>6} frames  {rate:6.1f} fps"


def _mean_boxes(reader: Any, limit: int = 512) -> float:
    """Average boxes per frame, sampled evenly -- box_count never decodes."""
    total = len(reader)
    if not total:
        return 0.0
    step = max(1, total // limit)
    picks = range(0, total, step)
    return sum(reader.box_count(i) for i in picks) / len(picks)


def cmd_bake(args: argparse.Namespace) -> int:
    from .cache import store
    from .cache.format import BakeHeader, BakeReader, write_bake
    from .engine.pipeline import bake_frames

    cfg = _configure(args)
    path = _resolve_path(args.video)
    _require_ffmpeg()
    media = probe(path)
    _fit_grid(cfg, media.aspect)
    v = cfg.video

    fingerprint = v.fingerprint()
    out = Path(args.out).expanduser() if args.out else store.cache_path(path, fingerprint)
    replacing = out.exists()

    status = Status()
    status.line(f"Baking {Path(path).name}  [{_describe(cfg)}]")
    status.line(f"  -> {out}" + ("  (replacing an existing bake)" if replacing else ""))

    header = BakeHeader(
        fps=media.fps, grid_w=v.grid_w, grid_h=v.grid_h, levels=max(1, v.levels),
        frame_count=media.frame_count, duration=media.duration,
        source=path, source_hash=store.source_hash(path),
        fingerprint=fingerprint, algo=v.algo,
    )
    progress = _BakeProgress(status, media.frame_count)
    try:
        write_bake(out, header, bake_frames(path, v, media.fps, v.grid_w, v.grid_h),
                   progress=progress)
    finally:
        status.clear()

    elapsed = max(1e-6, time.perf_counter() - progress.started)
    size = out.stat().st_size
    with BakeReader(out) as reader:
        frames, boxes = len(reader), _mean_boxes(reader)
    status.line(f"Baked {frames} frames in {elapsed:.1f}s "
                f"({frames / elapsed:.0f} fps, "
                f"{media.duration / elapsed:.1f}x realtime)")
    status.line(f"  {_human_bytes(size)} total, {size / max(1, frames):.0f} bytes/frame, "
                f"{boxes:.1f} boxes/frame average")
    status.line(f"  source {_human_bytes(media.size_bytes)} -> "
                f"{media.size_bytes / max(1, size):.1f}x smaller")
    if not args.out:
        status.line("  playback will pick this up automatically "
                    "(same file, same geometry settings)")
    return EXIT_OK


# ---- info ------------------------------------------------------------
def _sample_boxes(path: str, cfg: AppConfig, media: MediaInfo
                  ) -> tuple[list[int], float]:
    """Box counts from frames spread across the file, plus boxgen ms/frame.

    Spread rather than sequential: the first second of a video is very often a
    fade from black, which would report a box count nothing like the real one.
    """
    from .core import boxgen
    from .media.decoder import FrameDecoder

    v = cfg.video
    total = max(1, media.frame_count)
    starts = sorted({int(total * f) for f in INFO_SAMPLE_POINTS if int(total * f) < total})
    counts: list[int] = []
    spent = 0.0
    for start in starts or [0]:
        try:
            with FrameDecoder(path, v.grid_w, v.grid_h, start_frame=start,
                              fps=media.fps) as decoder:
                for taken, (_, gray) in enumerate(decoder):
                    mark = time.perf_counter()
                    counts.append(int(boxgen.frame_to_boxes(gray, v).shape[0]))
                    spent += time.perf_counter() - mark
                    if taken + 1 >= INFO_SAMPLE_DEPTH:
                        break
        except MediaError:
            continue                # a seek past a broken tail is not fatal here
    return counts, (spent / len(counts) * 1000.0) if counts else 0.0


def _cache_state(path: str, cfg: AppConfig) -> str:
    from .cache import store
    from .cache.format import BakeError, BakeReader

    fingerprint = cfg.video.fingerprint()
    entry = store.cache_path(path, fingerprint)
    if not entry.is_file():
        return f"miss -- `timeleap bake` would write {entry}"
    try:
        with BakeReader(entry) as reader:
            head = reader.header
            frames, size = len(reader), entry.stat().st_size
    except (BakeError, OSError) as exc:
        return f"unusable ({exc})"
    try:
        current = store.source_hash(path)
    except OSError:
        current = ""
    if head.fingerprint != fingerprint or head.source_hash != current:
        return f"stale -- {entry.name} was made from different settings or an older file"
    return (f"hit -- {entry.name}\n              {frames} frames, "
            f"{_human_bytes(size)}, {head.grid_w}x{head.grid_h} @ {head.fps:g} fps")


def cmd_info(args: argparse.Namespace) -> int:
    cfg = _configure(args)
    path = _resolve_path(args.video)
    _require_ffmpeg()
    media = probe(path)
    _fit_grid(cfg, media.aspect)
    v = cfg.video

    fps_note = "  (estimated)" if media.fps_estimated else ""
    count_note = "  (estimated)" if media.frames_estimated else ""
    rot = f", rotated {media.rotation} deg" if media.rotation else ""
    print(f"Source      {path}")
    print(f"            {media.width}x{media.height}{rot}, {media.codec}, "
          f"{_human_bytes(media.size_bytes)}")
    print(f"            {media.fps:g} fps{fps_note}, {media.duration:.2f} s, "
          f"{media.frame_count} frames{count_note}, "
          f"audio {'yes' if media.has_audio else 'no'}")

    auto = "auto from the source aspect" if v.auto_grid else "pinned"
    print(f"Geometry    grid {v.grid_w}x{v.grid_h} ({auto}), "
          f"{v.levels} level{'s' if v.levels != 1 else ''}, algo {v.algo}, "
          f"threshold {v.threshold_mode}{', inverted' if v.invert else ''}")
    print(f"            budget {v.max_windows} windows/frame "
          f"(renderer ceiling {MAX_WINDOWS} per level), palette {cfg.render.palette}")

    counts, box_ms = _sample_boxes(path, cfg, media)
    if not counts:
        print("Boxes       could not sample any frames")
        return EXIT_ERROR
    mean = sum(counts) / len(counts)
    capped = sum(1 for c in counts if c >= v.max_windows)
    print(f"Boxes       sampled {len(counts)} frames across the file: "
          f"mean {mean:.1f}, min {min(counts)}, max {max(counts)}"
          + (f", {capped} at the budget cap" if capped else ""))

    render_ms = mean * MS_PER_WINDOW
    frame_ms = render_ms + box_ms
    ceiling = 1000.0 / frame_ms if frame_ms > 0 else 0.0
    verdict = ("comfortable" if ceiling >= media.fps * 1.25 else
               "tight" if ceiling >= media.fps else "too slow -- lower --grid or --windows")
    print(f"Cost        boxgen {box_ms:.2f} ms/frame + ~{render_ms:.1f} ms of window "
          f"moves (~{MS_PER_WINDOW} ms each)")
    print(f"            ~{ceiling:.0f} fps ceiling against {media.fps:g} fps of "
          f"source: {verdict}")
    print("            diffing skips unchanged windows, so this is an upper bound")
    print(f"Cache       {_cache_state(path, cfg)}")
    return EXIT_OK


# ---- cache -----------------------------------------------------------
def _entry_line(path: Path, size: int, mtime: float) -> str:
    from .cache.format import BakeError, BakeReader
    try:
        with BakeReader(path) as reader:
            head = reader.header
            detail = (f"{len(reader):>6} frames  {head.grid_w}x{head.grid_h} "
                      f"@ {head.fps:g} fps  {Path(head.source).name}")
    except (BakeError, OSError):
        detail = "(unreadable)"
    stamp = time.strftime("%Y-%m-%d %H:%M", time.localtime(mtime))
    return f"  {_human_bytes(size):>10}  {stamp}  {detail}"


def cmd_cache(args: argparse.Namespace) -> int:
    from .cache import store

    before = store.entries()
    total = sum(size for _, size, _ in before)
    print(f"Cache       {cache_dir()}")

    if args.clear:
        # prune(0) is the tested path for this and correctly spares .tmp files
        # a bake may still be writing.
        freed = store.prune(0)
        left = store.entries()
        print(f"Cleared     {len(before) - len(left)} entries, {_human_bytes(freed)} freed")
        return EXIT_OK
    if args.prune is not None:
        freed = store.prune(args.prune)
        left = store.entries()
        print(f"Pruned      to {_human_bytes(args.prune)}: {_human_bytes(freed)} freed, "
              f"{len(left)} entries left "
              f"({_human_bytes(sum(s for _, s, _ in left))})")
        return EXIT_OK

    if not before:
        print("            empty -- `timeleap bake <video>` fills it")
        return EXIT_OK
    print(f"            {len(before)} entries, {_human_bytes(total)} total "
          f"(oldest first)")
    for path, size, mtime in before:
        print(_entry_line(path, size, mtime))
    return EXIT_OK


def cmd_gui(args: argparse.Namespace) -> int:
    cfg = _configure(args)
    initial = _resolve_path(args.video) if args.video else None
    return _launch_ui(cfg, initial)


# ---- parser ----------------------------------------------------------
def _geometry_parent() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(add_help=False)
    p.add_argument("--grid", type=_grid, metavar="N|WxH",
                   help="grid width; height follows the source aspect unless "
                        "you give WxH")
    p.add_argument("--windows", type=_ranged(1, MAX_WINDOWS), metavar="N",
                   help="maximum windows drawn per frame")
    p.add_argument("--levels", type=_ranged(1, 16), metavar="N",
                   help="1 = silhouette, 2..16 = luminance bands")
    p.add_argument("--algo", choices=ALGOS, help="box decomposition algorithm")
    p.add_argument("--threshold", choices=THRESHOLDS, help="thresholding mode")
    p.add_argument("--invert", action="store_true", help="swap dark and light")
    p.add_argument("--preset", metavar="NAME",
                   help="start from a preset: " + ", ".join(repr(k) for k in PRESETS))
    return p


def build_parser() -> argparse.ArgumentParser:
    geometry = _geometry_parent()
    parser = argparse.ArgumentParser(
        prog="timeleap",
        description="Play video using real Windows windows as pixels.",
        epilog=EPILOG, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--version", action="version", version=f"timeleap {VERSION}")
    parser.add_argument("--list-monitors", action="store_true",
                        help="show monitor indices for --monitor")
    parser.add_argument("--list-palettes", action="store_true",
                        help="show palette names for --palette")
    sub = parser.add_subparsers(dest="command", metavar="COMMAND")

    play = sub.add_parser("play", parents=[geometry],
                          help="play a video (the default; `timeleap clip.mp4` works)")
    play.add_argument("video")
    play.add_argument("--palette", choices=palette.names(), help="colour ramp")
    play.add_argument("--monitor", type=_monitor, metavar="N|all",
                      help="which monitor to cover")
    play.add_argument("--region", type=_region, metavar="X,Y,W,H",
                      help="confine the show to this pixel rectangle")
    play.add_argument("--speed", type=_speed, metavar="X", help="playback rate")
    play.add_argument("--loop", choices=LOOPS, help="what to do at the end")
    play.add_argument("--reverse", action="store_true", help="play backwards (silent)")
    play.add_argument("--mute", action="store_true", help="no audio")
    play.add_argument("--volume", type=_ranged(0, 100), metavar="N", help="0..100")
    play.add_argument("--blackout", action="store_true",
                      help="black backdrop window behind the show")
    play.add_argument("--no-cache", action="store_true",
                      help="ignore any baked geometry and stream instead")
    play.add_argument("--duration", type=_seconds, metavar="SEC",
                      help="stop after SEC seconds (with --no-ui)")
    play.add_argument("--no-ui", action="store_true",
                      help="run headless with a live status line")
    play.set_defaults(func=cmd_play)

    bake = sub.add_parser("bake", parents=[geometry],
                          help="pre-compute box geometry into a .tlp file")
    bake.add_argument("video")
    bake.add_argument("--out", metavar="FILE",
                      help="destination (default: the bake cache, found automatically)")
    bake.set_defaults(func=cmd_bake)

    info = sub.add_parser("info", parents=[geometry],
                          help="probe a video and predict what it costs to play")
    info.add_argument("video")
    info.add_argument("--palette", choices=palette.names(), help="colour ramp")
    info.set_defaults(func=cmd_info)

    cache = sub.add_parser("cache", help="inspect or trim the bake cache")
    cache.add_argument("--list", action="store_true", help="list entries (default)")
    cache.add_argument("--prune", type=_byte_size, metavar="BYTES",
                       help="evict least-recently-used bakes down to this budget")
    cache.add_argument("--clear", action="store_true", help="delete every bake")
    cache.set_defaults(func=cmd_cache)

    gui = sub.add_parser("gui", parents=[geometry], help="launch the control panel")
    gui.add_argument("video", nargs="?", help="optional file to pre-load")
    gui.add_argument("--palette", choices=palette.names(), help="colour ramp")
    gui.add_argument("--monitor", type=_monitor, metavar="N|all")
    gui.add_argument("--region", type=_region, metavar="X,Y,W,H")
    gui.set_defaults(func=cmd_gui)
    return parser


def _insert_default_command(argv: Sequence[str]) -> list[str]:
    """`timeleap clip.mp4 --grid 64` means `timeleap play clip.mp4 --grid 64`.

    Options are skipped rather than counted, so the first bare word decides.
    Every global option here is a flag with no value, so nothing an option
    consumes can be mistaken for the command.
    """
    args = list(argv)
    for token in args:
        if token == "--":
            break
        if token.startswith("-"):
            continue
        return args if token in COMMANDS else ["play", *args]
    return args


def main(argv: Iterable[str] | None = None) -> int:
    # One-shot and process-wide: Tk or the renderer creating a window first
    # would lock the process into the wrong DPI mode for its whole life.
    w.enable_dpi_awareness()

    parser = build_parser()
    args = parser.parse_args(_insert_default_command(
        list(sys.argv[1:] if argv is None else argv)))

    try:
        if args.list_monitors or args.list_palettes:
            if args.list_monitors:
                list_monitors()
            if args.list_palettes:
                list_palettes()
            return EXIT_OK
        if not getattr(args, "command", None):
            parser.print_help()
            return EXIT_USAGE
        return int(args.func(args))
    except CliError as exc:
        print(f"timeleap: {exc}", file=sys.stderr)
        return exc.code
    except MediaError as exc:
        print(f"timeleap: {exc}", file=sys.stderr)
        return EXIT_ERROR
    except KeyboardInterrupt:
        print("\nInterrupted.", file=sys.stderr)
        return EXIT_INTERRUPT
    except OSError as exc:
        print(f"timeleap: {exc}", file=sys.stderr)
        return EXIT_ERROR


if __name__ == "__main__":          # pragma: no cover
    sys.exit(main())
