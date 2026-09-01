"""The control panel.

Player callbacks arrive on the render thread, so nothing in this file touches
a widget from one: a callback is appended to a deque and replayed on the Tk
tick. Modal dialogs are avoided for the same reason -- the prototype's
off-thread `messagebox.showerror` deadlocked -- so failures land in the status
bar, which is also where you can still read them with 200 topmost windows on
screen.

The other thing this file has to get right is which knob needs which refresh:
`cfg.render.*` and `cfg.effects.*` are live (`refresh_render`), while
`cfg.video.*` rebuilds the frame source (`refresh_video`) and is therefore
debounced -- dragging the grid slider must not spawn an ffmpeg per pixel.
"""
from __future__ import annotations

import json
import os
import threading
import tkinter as tk
from collections import deque
from collections.abc import Callable, Sequence
from dataclasses import dataclass, replace
from pathlib import Path
from tkinter import filedialog, ttk
from typing import Any

from ..cache import store
from ..cache.format import BakeHeader, write_bake
from ..config import PRESETS, AppConfig, apply_preset, cache_dir, config_dir
from ..core import palette
from ..engine.pipeline import bake_frames
from ..engine.player import Player
from ..media.probe import MediaInfo, have_ffmpeg, probe
from ..render import win32 as w32
from . import widgets
from .region_picker import RegionPicker
from .widgets import (COLORS, Debouncer, FormGrid, LabeledSlider, MappedCombo,
                      ScrollFrame, SeekBar, StatReadout, SwatchStrip,
                      format_bytes, format_time)

APP_VERSION = "1.0"
TICK_MS = 200                    # stats refresh; also drains the callback queue
VIDEO_DEBOUNCE_MS = 250          # a source rebuild is expensive
RENDER_DEBOUNCE_MS = 60          # a palette/gap change is not
BAKE_STEP = 8                    # progress posts per N frames

VIDEO_EXTS = (".mp4", ".mkv", ".avi", ".mov", ".webm", ".m4v", ".wmv", ".flv",
              ".mpg", ".mpeg", ".ts", ".gif")
FILETYPES = [("Video files", " ".join("*" + e for e in VIDEO_EXTS)),
             ("All files", "*.*")]

_ALGOS = [("fast", "fast - row runs, ~0.15 ms"),
          ("balanced", "balanced - run merge"),
          ("quality", "quality - fewest boxes")]
_THRESHOLDS = [("otsu", "otsu - automatic"), ("fixed", "fixed level"),
               ("adaptive", "adaptive - local"), ("edge", "edge")]
_LOOPS = [("off", "off - stop at end"), ("loop", "loop"),
          ("pingpong", "ping-pong")]
_FITS = [("contain", "contain - letterbox"), ("cover", "cover - crop"),
         ("stretch", "stretch")]
_BACKENDS = [("waveout", "waveout - real pause, A/V sync"),
             ("ffplay", "ffplay - fallback process"), ("none", "none - silent")]
# Honest labels: "fast" is faster and visibly wrong, and the config comment
# says so. Hiding that behind the word "fast" alone is how people pick it.
_REDRAWS = [("accurate", "accurate - every pixel correct"),
            ("fast", "fast - ~1.4x, smears vacated cells (85% correct)")]

_DPI_DONE = False


def _ensure_dpi_awareness() -> None:
    """Once per process, and before Tk creates its first HWND."""
    global _DPI_DONE
    if _DPI_DONE:
        return
    _DPI_DONE = True
    try:
        w32.enable_dpi_awareness()
    except Exception:
        pass


class _BakeCancelled(RuntimeError):
    """Raised out of the progress callback so `write_bake` unwinds and
    deletes its .tmp instead of leaving a partial cache entry behind."""


@dataclass
class _UiState:
    """Window geometry and the open tab.

    Deliberately not in `AppConfig`: that file is the documented settings
    contract shared with the CLI, and where a window sat is not a setting.
    """

    geometry: str = ""
    tab: int = 0

    @staticmethod
    def path() -> Path:
        return config_dir() / "ui.json"

    @classmethod
    def load(cls) -> _UiState:
        try:
            raw = json.loads(cls.path().read_text("utf-8"))
            return cls(geometry=str(raw.get("geometry", "")),
                       tab=int(raw.get("tab", 0)))
        except Exception:
            return cls()

    def save(self) -> None:
        try:
            self.path().write_text(json.dumps(self.__dict__, indent=2), "utf-8")
        except Exception:
            pass                 # window position is a convenience, never fatal


class TimeLeapApp(tk.Tk):
    """The whole UI. Owns one `Player` and outlives every file it opens."""

    def __init__(self, cfg: AppConfig, initial: str | None = None) -> None:
        _ensure_dpi_awareness()
        super().__init__()
        self.cfg = cfg
        self.player = Player(cfg)
        self.ui_state = _UiState.load()

        self._tk_thread = threading.get_ident()
        self._events: deque[Callable[[], None]] = deque()
        self._ctls: list[tuple[str, str, Any]] = []
        self._syncing = False
        self._resume_after_scrub = False
        self._path = ""
        self._status_text = "Ready."
        self._tick_job: str | None = None
        self._hotkeys: Any = None
        self._bake_thread: threading.Thread | None = None
        self._bake_cancel = threading.Event()
        self._closing = False
        self._page_parent: tk.Misc | None = None

        self.title("TimeLeapPlayer")
        self.minsize(900, 640)
        self.geometry(self._safe_geometry(self.ui_state.geometry, "1040x780"))
        self.style = widgets.apply_theme(self)
        self.debounce = Debouncer(self)

        self._build()
        self._sync_from_cfg()
        self._wire_player()
        self._bind_keys()
        self._start_hotkeys()
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self._tick()
        if initial:
            self.after(150, lambda: self.open_path(initial))

    def run(self) -> None:
        self.mainloop()

    # ---- layout ------------------------------------------------------
    def _build(self) -> None:
        header = ttk.Frame(self, style="Bar.TFrame", padding=(12, 8))
        header.pack(side="top", fill="x")
        self.title_label = ttk.Label(header, text="No file loaded",
                                     style="Head.TLabel",
                                     background=COLORS["panel"])
        self.title_label.pack(side="left")
        ttk.Button(header, text="STOP", style="Stop.TButton",
                   command=self._on_stop).pack(side="right")
        ttk.Button(header, text="Panic", command=self._on_panic).pack(
            side="right", padx=(0, 8))

        self.status = ttk.Label(self, text=self._status_text, style="Status.TLabel",
                                anchor="w", padding=(12, 5))
        self.status.pack(side="bottom", fill="x")

        strip = ttk.Frame(self, style="TL.TFrame", padding=(12, 6))
        strip.pack(side="bottom", fill="x")
        self.stats = StatReadout(strip, (
            ("fps", "render", 7), ("decode", "decode", 7), ("boxes", "boxes", 6),
            ("windows", "windows", 6), ("dropped", "dropped", 6),
            ("buffer", "buffer", 6), ("drift", "drift", 8),
            ("batch", "batch", 8), ("source", "source", 8)))
        self.stats.pack(side="left")

        self.nb = ttk.Notebook(self)
        self.nb.pack(side="top", fill="both", expand=True, padx=10, pady=(8, 0))
        for builder, label in ((self._tab_playback, "Playback"),
                               (self._tab_visual, "Visual"),
                               (self._tab_effects, "Effects"),
                               (self._tab_audio, "Audio"),
                               (self._tab_cache, "Cache")):
            self._add_page(builder, label)
        self._add_page(self._tab_about, "About")
        try:
            self.nb.select(self.ui_state.tab)
        except Exception:
            pass

    def _add_page(self, builder: Callable[[], ttk.Frame], label: str) -> None:
        """Wrap a tab in a scroller so it stays reachable at any window size."""
        page = ScrollFrame(self.nb)
        self._page_parent = page.body
        try:
            content = builder()
        finally:
            self._page_parent = None
        content.pack(fill="both", expand=True)
        self.nb.add(page, text=label)

    def _tab_playback(self) -> ttk.Frame:
        page = ttk.Frame(self._page_parent, style="TL.TFrame", padding=14)

        files = ttk.Frame(page, style="TL.TFrame")
        files.pack(fill="x")
        ttk.Button(files, text="Open…", style="Accent.TButton",
                   command=self._on_open).pack(side="left")
        ttk.Button(files, text="Next in folder", command=self._on_next).pack(
            side="left", padx=6)
        ttk.Label(files, text="Preset", style="Field.TLabel").pack(
            side="left", padx=(24, 6))
        self.preset_combo = MappedCombo(
            files, [(name, name) for name in PRESETS], value=None,
            command=self._on_preset, width=20)
        self.preset_combo.pack(side="left")

        self.seek = SeekBar(page, on_scrub_start=self._on_scrub_start,
                            on_scrub=self._on_scrub, on_seek=self._on_seek)
        self.seek.pack(fill="x", pady=(16, 0))

        times = ttk.Frame(page, style="TL.TFrame")
        times.pack(fill="x")
        self.time_label = ttk.Label(times, text="0:00 / 0:00", style="Time.TLabel")
        self.time_label.pack(side="left")
        self.frame_label = ttk.Label(times, text="frame 0 / 0", style="Field.TLabel")
        self.frame_label.pack(side="right")

        transport = ttk.Frame(page, style="TL.TFrame")
        transport.pack(fill="x", pady=(14, 0))
        self.play_btn = ttk.Button(transport, text="Play", width=10,
                                   style="Transport.TButton",
                                   command=self._on_playpause)
        self.play_btn.pack(side="left")
        ttk.Button(transport, text="Stop", style="Transport.TButton",
                   command=self._on_stop).pack(side="left", padx=6)
        ttk.Button(transport, text="Restart", style="Transport.TButton",
                   command=self._on_restart).pack(side="left")

        form = FormGrid(page)
        form.pack(fill="x", pady=(18, 0))
        speed = ttk.Frame(form, style="TL.TFrame")
        self.speed_slider = LabeledSlider(
            speed, lo=0.1, hi=4.0, value=self.cfg.playback.speed, step=0.05,
            default=1.0, unit="x", command=self._on_speed, length=240)
        self.speed_slider.pack(side="left")
        ttk.Button(speed, text="1.0x", width=5,
                   command=lambda: self.speed_slider.set(1.0, notify=True)).pack(
            side="left", padx=8)
        form.add("Speed", speed, "double-click the value to reset")
        form.add("Loop", self._combo(form, "playback", "loop", _LOOPS,
                                     on_change=self._on_loop))
        form.add("Direction", self._check(form, "Play in reverse (audio muted)",
                                          "playback", "reverse",
                                          on_change=self._on_reverse))
        self.media_label = ttk.Label(page, text="", style="Hint.TLabel")
        self.media_label.pack(anchor="w", pady=(18, 0))
        return page

    def _tab_visual(self) -> ttk.Frame:
        page = ttk.Frame(self._page_parent, style="TL.TFrame", padding=14)
        left = FormGrid(page)
        left.pack(side="left", anchor="n")
        ttk.Separator(page, orient="vertical").pack(side="left", fill="y", padx=18)
        right = FormGrid(page)
        right.pack(side="left", anchor="n")

        left.heading("Source grid  (rebuilds the pipeline)")
        left.add("Grid width", self._slider(left, "video", "grid_w", 32, 256,
                                            integer=True, step=2))
        self.grid_h_slider = self._slider(left, "video", "grid_h", 18, 256,
                                          integer=True)
        left.add("Grid height", self.grid_h_slider)
        left.add("", self._check(left, "Follow source aspect ratio",
                                 "video", "auto_grid"))
        left.add("Max windows", self._slider(left, "video", "max_windows",
                                             10, 400, integer=True, step=2))
        left.add("Levels", self._slider(left, "video", "levels", 1, 16,
                                        integer=True))
        left.add("Algorithm", self._combo(left, "video", "algo", _ALGOS))
        left.add("Threshold", self._combo(left, "video", "threshold_mode",
                                          _THRESHOLDS))
        left.add("Fixed level", self._slider(left, "video", "fixed_threshold",
                                             0, 255, integer=True))
        left.add("", self._check(left, "Invert", "video", "invert"))
        left.add("", self._check(left, "Denoise speckle", "video", "denoise"))
        left.heading("Tone")
        left.add("Gamma", self._slider(left, "video", "gamma", 0.2, 3.0, step=0.05))
        left.add("Contrast", self._slider(left, "video", "contrast", 0.2, 3.0,
                                          step=0.05))
        left.add("Brightness", self._slider(left, "video", "brightness",
                                            -128, 127, integer=True))

        right.heading("Output  (live)")
        self.palette_combo = self._combo(
            right, "render", "palette",
            [(name, name) for name in palette.names()])
        right.add("Palette", self.palette_combo)
        self.swatch = SwatchStrip(right)
        right.add("", self.swatch)
        self.monitor_combo = MappedCombo(right, self._monitor_options(),
                                         value=self.cfg.render.monitor,
                                         command=self._on_monitor, width=26)
        right.add("Monitor", self.monitor_combo)
        # Placement rows span the whole grid: four spin boxes and six snap
        # buttons do not fit in the narrow control column beside a label.
        right.heading("Placement  (live)")
        place = ttk.Frame(right, style="TL.TFrame")
        ttk.Button(place, text="Move / resize on screen  (Ctrl+M)",
                   command=self._on_pick_region).pack(side="left")
        ttk.Button(place, text="Clear", width=7,
                   command=self._on_clear_region).pack(side="left", padx=6)
        right.add_wide(place)

        boxes = ttk.Frame(right, style="TL.TFrame")
        self.region_vars: dict[str, tk.StringVar] = {}
        for key in ("x", "y", "w", "h"):
            ttk.Label(boxes, text=key.upper(), style="Hint.TLabel").pack(
                side="left", padx=(0, 2))
            var = tk.StringVar()
            spin = ttk.Spinbox(boxes, from_=-32000, to=32000, width=6,
                               textvariable=var, command=self._on_region_fields)
            spin.bind("<Return>", lambda _e: self._on_region_fields())
            spin.bind("<FocusOut>", lambda _e: self._on_region_fields())
            spin.pack(side="left", padx=(0, 10))
            self.region_vars[key] = var
        right.add_wide(boxes, pady=(2, 2))

        # Two rows of three, so the snaps still fit at the 900 px minimum width.
        for row in (("Centre", "centre"), ("Fill", "fill"), ("Left", "left")), \
                   (("Right", "right"), ("Top", "top"), ("Bottom", "bottom")):
            quick = ttk.Frame(right, style="TL.TFrame")
            for text, spec in row:
                ttk.Button(quick, text=text, width=8,
                           command=lambda s=spec: self._on_quick_region(s)).pack(
                    side="left", padx=(0, 4))
            right.add_wide(quick, pady=(2, 0))
        self.region_label = ttk.Label(right, text="", style="Hint.TLabel")
        right.add_wide(self.region_label, pady=(0, 6))
        right.add("Fit", self._combo(right, "render", "fit", _FITS))
        right.add("Gap", self._slider(right, "render", "gap", 0, 12,
                                      integer=True, length=140))
        right.add("Min window px", self._slider(right, "render", "min_window_px",
                                                1, 24, integer=True, length=140))
        right.add("Redraw", self._combo(right, "render", "redraw", _REDRAWS,
                                        width=32))
        right.add("", self._check(right, "Black backdrop behind the show",
                                  "render", "background_blackout"))
        right.add("", self._check(right, "Click-through windows",
                                  "render", "click_through"))
        right.add("", self._check(right, "Always on top", "render", "topmost"))
        right.add("", self._check(right, "Diff frames (skip unchanged slots)",
                                  "render", "diff_frames"))
        right.add("", self._check(right, "Stable slots (kills jitter)",
                                  "render", "stable_slots"))
        return page

    def _tab_effects(self) -> ttk.Frame:
        page = ttk.Frame(self._page_parent, style="TL.TFrame", padding=14)
        form = FormGrid(page)
        form.pack(side="left", anchor="n")
        form.heading("Temporal effects  (live)")
        form.add("Trails", self._slider(form, "effects", "trails", 0, 16,
                                        integer=True), "frames kept behind")
        form.add("Echo offset", self._slider(form, "effects", "echo_offset",
                                             0, 60, integer=True),
                 "frames of delay")
        form.add("Ghost", self._slider(form, "effects", "ghost", 0.0, 1.0,
                                       step=0.05), "echo strength")
        form.add("Slit-scan", self._slider(form, "effects", "slitscan", 0, 64,
                                           integer=True), "bands; 0-1 = off")
        form.add("Jitter", self._slider(form, "effects", "jitter", 0, 8,
                                        integer=True), "grid cells")
        form.add("Strobe", self._slider(form, "effects", "strobe", 0, 16,
                                        integer=True), "blank every Nth; 0-1 = off")
        form.add("Shuffle", self._slider(form, "effects", "shuffle", 0, 64,
                                         integer=True), "boxes scrambled")
        form.add("Time warp", self._slider(form, "effects", "time_warp", 0.0, 1.0,
                                           step=0.05), "speed modulation depth")
        form.add("Warp period", self._slider(form, "effects", "warp_period",
                                             0.25, 20.0, step=0.25),
                 "seconds per cycle")
        form.add_wide(ttk.Button(form, text="None - reset every effect",
                                 command=self._on_effects_none), pady=(16, 0))
        return page

    def _tab_audio(self) -> ttk.Frame:
        page = ttk.Frame(self._page_parent, style="TL.TFrame", padding=14)
        form = FormGrid(page)
        form.pack(side="left", anchor="n")
        form.heading("Audio")
        form.add("", self._check(form, "Mute", "playback", "mute",
                                 on_change=self._on_mute))
        form.add("Volume", self._slider(form, "playback", "volume", 0, 100,
                                        integer=True, on_change=self._on_volume,
                                        length=240))
        form.add("Backend", self._combo(form, "playback", "audio_backend",
                                        _BACKENDS, on_change=self._on_backend,
                                        width=30))
        form.add("", self._check(form, "A/V sync (slave video to the audio clock)",
                                 "playback", "av_sync", on_change=self._on_av_sync))
        form.add("Max frame skip", self._slider(form, "playback", "max_frame_skip",
                                                0, 16, integer=True,
                                                on_change=self._on_playback_reload))
        form.add("Prefetch", self._slider(form, "playback", "prefetch_frames",
                                          16, 512, integer=True, step=8,
                                          on_change=self._on_playback_reload),
                 "ring buffer depth")
        hint = ttk.Label(page, style="Hint.TLabel", justify="left",
                         text=("Checking PATH…\n"
                               "Backend and A/V sync apply the next time a "
                               "file is opened; mute and volume are live."))
        hint.pack(side="bottom", anchor="w")
        self._probe_path_async(hint)
        return page

    def _probe_path_async(self, hint: ttk.Label) -> None:
        """Resolve which ffmpeg binaries exist without blocking the build.

        `have_ffmpeg()` runs three `-version` subprocesses. Called inline it
        added ~240 ms to window construction -- a third of the total -- for a
        line of text nobody reads before the window is even on screen.
        """
        def work() -> None:
            try:
                names = ("ffmpeg", "ffprobe", "ffplay")
                found = ", ".join(n for n, ok in zip(names, have_ffmpeg())
                                  if ok) or "nothing"
            except Exception:
                found = "unknown"
            self._post(lambda: self._set_path_hint(hint, found))

        threading.Thread(target=work, name="timeleap-path-probe",
                         daemon=True).start()

    def _set_path_hint(self, hint: ttk.Label, found: str) -> None:
        if self._closing:
            return
        try:
            hint.configure(text=(f"On PATH: {found}.\n"
                                 "Backend and A/V sync apply the next time a "
                                 "file is opened; mute and volume are live."))
        except tk.TclError:
            pass                      # window went away while we were probing

    def _tab_cache(self) -> ttk.Frame:
        page = ttk.Frame(self._page_parent, style="TL.TFrame", padding=14)
        bar = ttk.Frame(page, style="TL.TFrame")
        bar.pack(fill="x")
        self.bake_btn = ttk.Button(bar, text="Bake this video",
                                   style="Accent.TButton", command=self._on_bake)
        self.bake_btn.pack(side="left")
        ttk.Button(bar, text="Prune", command=self._on_prune).pack(side="left",
                                                                  padx=6)
        ttk.Button(bar, text="Open cache folder",
                   command=self._on_open_cache).pack(side="left")
        ttk.Button(bar, text="Refresh", command=self._refresh_cache).pack(
            side="left", padx=6)
        self.use_cache_var = tk.BooleanVar(self, self.cfg.use_cache)
        ttk.Checkbutton(bar, text="Use cache", variable=self.use_cache_var,
                        command=self._on_use_cache).pack(side="right")

        self.bake_progress = ttk.Progressbar(page, mode="determinate", maximum=100)
        self.bake_progress.pack(fill="x", pady=(12, 4))
        self.bake_label = ttk.Label(page, text="", style="Hint.TLabel")
        self.bake_label.pack(anchor="w")

        holder = ttk.Frame(page, style="TL.TFrame")
        holder.pack(fill="both", expand=True, pady=(10, 0))
        self.cache_tree = ttk.Treeview(holder, columns=("size", "when"),
                                       show="tree headings", height=8)
        self.cache_tree.heading("#0", text="Bake")
        self.cache_tree.heading("size", text="Size")
        self.cache_tree.heading("when", text="Modified")
        self.cache_tree.column("#0", width=430, anchor="w")
        self.cache_tree.column("size", width=90, anchor="e")
        self.cache_tree.column("when", width=140, anchor="e")
        scroll = ttk.Scrollbar(holder, orient="vertical",
                               command=self.cache_tree.yview)
        self.cache_tree.configure(yscrollcommand=scroll.set)
        self.cache_tree.pack(side="left", fill="both", expand=True)
        scroll.pack(side="right", fill="y")
        self.cache_total = ttk.Label(page, text="", style="Field.TLabel")
        self.cache_total.pack(anchor="w", pady=(6, 0))
        self._refresh_cache()
        return page

    def _tab_about(self) -> ttk.Frame:
        page = ttk.Frame(self._page_parent, style="TL.TFrame", padding=18)
        ttk.Label(page, text=f"TimeLeapPlayer {APP_VERSION}",
                  style="Head.TLabel").pack(anchor="w")
        body = (
            "Plays video by using real Windows windows as pixels. Each frame is\n"
            "thresholded to a coarse grid, decomposed into rectangles, and those\n"
            "rectangles are pushed to a pool of pooled top-level windows in one\n"
            "DeferWindowPos batch per frame.\n\n"
            "Inspired by mon/bad_apple_virus. Slot tracking is the addition its\n"
            "README asks for: boxes keep the same HWND between frames, so the\n"
            "picture stops flickering.\n\n"
            "Baking a video stores the geometry in an indexed .tlp file, so a\n"
            "replay costs no decoding at all."
        )
        ttk.Label(page, text=body, style="TLabel", justify="left").pack(
            anchor="w", pady=(10, 16))
        ttk.Label(page, text="HOTKEYS", style="Cap.TLabel",
                  background=COLORS["bg"]).pack(anchor="w")
        keys = (
            ("Space", "play / pause"),
            ("Ctrl+O", "open a file"),
            ("Ctrl+N", "next file in the folder"),
            ("Esc", "stop and clear the screen"),
            ("F5", "restart from the beginning"),
            ("Left / Right", "seek 5 seconds"),
            ("Ctrl+Alt+Q", "PANIC - global, works when the panel is buried"),
            ("Ctrl+Alt+Space", "play / pause - global"),
        )
        grid = FormGrid(page)
        grid.pack(anchor="w", pady=(6, 0))
        for key, what in keys:
            grid.add(key, ttk.Label(grid, text=what, style="TLabel"))
        self.hotkey_label = ttk.Label(page, text="", style="Hint.TLabel")
        self.hotkey_label.pack(anchor="w", pady=(14, 0))
        ttk.Label(page, text=f"Config: {AppConfig.path()}", style="Hint.TLabel").pack(
            anchor="w", pady=(10, 0))
        return page

    # ---- control factories -------------------------------------------
    def _slider(self, parent: tk.Misc, section: str, field: str, lo: float,
                hi: float, *, on_change: Callable[[Any], None] | None = None,
                **kw: Any) -> LabeledSlider:
        value = getattr(getattr(self.cfg, section), field)
        widget = LabeledSlider(parent, lo=lo, hi=hi, value=value,
                               command=on_change or self._setter(section, field),
                               **kw)
        self._ctls.append((section, field, widget))
        return widget

    def _combo(self, parent: tk.Misc, section: str, field: str,
               options: Sequence[tuple[Any, str]], *,
               on_change: Callable[[Any], None] | None = None,
               **kw: Any) -> MappedCombo:
        widget = MappedCombo(parent, options,
                             value=getattr(getattr(self.cfg, section), field),
                             command=on_change or self._setter(section, field),
                             **kw)
        self._ctls.append((section, field, widget))
        return widget

    def _check(self, parent: tk.Misc, text: str, section: str, field: str, *,
               on_change: Callable[[Any], None] | None = None) -> ttk.Checkbutton:
        var = tk.BooleanVar(parent, bool(getattr(getattr(self.cfg, section), field)))
        handler = on_change or self._setter(section, field)
        widget = ttk.Checkbutton(parent, text=text, variable=var,
                                 command=lambda: handler(bool(var.get())))
        self._ctls.append((section, field, var))
        return widget

    def _setter(self, section: str, field: str) -> Callable[[Any], None]:
        return lambda value: self._set(section, field, value)

    def _set(self, section: str, field: str, value: Any) -> None:
        """One knob changed: store it, then schedule the refresh it needs."""
        if self._syncing:
            return
        setattr(getattr(self.cfg, section), field, value)
        if section == "video":
            if field in ("grid_w", "auto_grid", "grid_h"):
                self._resolve_grid()
                self._show_grid()
            if field == "levels":
                self._show_swatch()
            self.debounce.call("video", VIDEO_DEBOUNCE_MS, self._apply_video)
        else:
            if field == "palette":
                self._show_swatch()
            self.debounce.call("render", RENDER_DEBOUNCE_MS, self._apply_render)

    def _sync_from_cfg(self) -> None:
        """Push every config value back into the widgets (after a preset)."""
        self._syncing = True
        try:
            for section, field, widget in self._ctls:
                value = getattr(getattr(self.cfg, section), field)
                if isinstance(widget, LabeledSlider):
                    widget.set(value)
                elif isinstance(widget, MappedCombo):
                    widget.set_value(value)
                elif isinstance(widget, tk.Variable):
                    widget.set(value)
            self.monitor_combo.set_value(self.cfg.render.monitor)
            self.use_cache_var.set(self.cfg.use_cache)
        finally:
            self._syncing = False
        self._show_grid()
        self._show_swatch()
        self._show_region()

    # ---- derived display ---------------------------------------------
    def _resolve_grid(self) -> bool:
        """Auto-grid lives here: nothing downstream derives `grid_h`.

        Returns whether it changed. Safe to call from the render thread --
        it only touches config.
        """
        video = self.cfg.video
        if not video.auto_grid:
            return False
        info = self.player.media
        aspect = info.aspect if info and info.aspect > 0 else 16 / 9
        wanted = max(8, min(256, int(round(video.grid_w / aspect))))
        if wanted == video.grid_h:
            return False
        video.grid_h = wanted
        return True

    def _show_grid(self) -> None:
        auto = self.cfg.video.auto_grid
        self.grid_h_slider.enable(not auto)
        self._syncing, previous = True, self._syncing
        try:
            self.grid_h_slider.set(self.cfg.video.grid_h)
        finally:
            self._syncing = previous

    def _show_swatch(self) -> None:
        self.swatch.show(self.cfg.render.palette, self.cfg.video.levels)

    def _show_region(self) -> None:
        region = self.cfg.render.region
        if region:
            x, y, rw, rh = (int(v) for v in region)
            self.region_label.configure(text=f"region {rw}x{rh} at {x}, {y}")
        else:
            mon = self._effective_rect()
            self.region_label.configure(
                text=f"no region - using the monitor picker "
                     f"({mon[2]}x{mon[3]} at {mon[0]}, {mon[1]})")
            x, y, rw, rh = mon
        for key, val in zip(("x", "y", "w", "h"), (x, y, rw, rh)):
            self.region_vars[key].set(str(val))

    def _effective_rect(self) -> tuple[int, int, int, int]:
        """Where the video actually lands right now, region or not."""
        try:
            return w32.target_rect(self.cfg.render.monitor, self.cfg.render.region)
        except Exception:
            return (0, 0, 1920, 1080)

    def _set_region(self, rect: tuple[int, int, int, int] | None,
                    note: str = "") -> None:
        self.cfg.render.region = tuple(int(v) for v in rect) if rect else None
        self._show_region()
        self._apply_render()
        if note:
            self._status(note)

    def _on_region_fields(self) -> None:
        try:
            vals = [int(float(self.region_vars[k].get()))
                    for k in ("x", "y", "w", "h")]
        except (TypeError, ValueError):
            return                      # mid-edit; wait for a complete number
        if vals[2] < 16 or vals[3] < 16:
            return
        rect = (vals[0], vals[1], vals[2], vals[3])
        if rect != self.cfg.render.region:
            self._set_region(rect)

    def _on_quick_region(self, spec: str) -> None:
        """Snap into a half or the whole of the monitor the video is on."""
        base = w32.target_rect(self.cfg.render.monitor, None)
        bx, by, bw, bh = base
        if spec == "fill":
            rect = base
        elif spec == "left":
            rect = (bx, by, bw // 2, bh)
        elif spec == "right":
            rect = (bx + bw // 2, by, bw - bw // 2, bh)
        elif spec == "top":
            rect = (bx, by, bw, bh // 2)
        elif spec == "bottom":
            rect = (bx, by + bh // 2, bw, bh - bh // 2)
        else:                                    # centre at half size
            cw, ch = bw // 2, bh // 2
            rect = (bx + (bw - cw) // 2, by + (bh - ch) // 2, cw, ch)
        self._set_region(rect, f"Region: {spec}")

    def _on_pick_region(self) -> None:
        """Open the on-screen handle. Applies live, so the video follows."""
        existing = getattr(self, "_picker", None)
        if existing is not None and existing.winfo_exists():
            existing.lift()
            return
        before = self.cfg.render.region
        start = self.cfg.render.region or self._effective_rect()

        def change(rect):
            self._set_region(rect)

        def commit(rect):
            self._picker = None
            self._set_region(rect, f"Region set to {rect[2]}x{rect[3]} "
                                   f"at {rect[0]}, {rect[1]}.")

        def cancel():
            self._picker = None
            self._set_region(before, "Region unchanged.")

        self._picker = RegionPicker(self, start, on_change=change,
                                    on_commit=commit, on_cancel=cancel)
        self._status("Drag the frame to move the video; edges resize it. "
                     "Enter applies, Esc cancels.")

    def _monitor_options(self) -> list[tuple[int, str]]:
        options: list[tuple[int, str]] = [(-1, "All monitors (virtual desktop)")]
        try:
            options += [(m.index, m.label()) for m in w32.monitors()]
        except Exception:
            pass
        return options

    # ---- transport handlers ------------------------------------------
    def _on_open(self) -> None:
        path = filedialog.askopenfilename(
            parent=self, title="Open video", filetypes=FILETYPES,
            initialdir=self.cfg.last_dir or os.path.expanduser("~"))
        if path:
            self.open_path(path)

    def open_path(self, path: str) -> None:
        source = Path(path)
        if not source.is_file():
            self._status(f"No such file: {path}", error=True)
            return
        self._path = str(source)
        self.cfg.last_dir = str(source.parent)
        self.title_label.configure(text=source.name)
        self.title(f"TimeLeapPlayer - {source.name}")
        self._status(f"Loading {source.name}…")
        self._reset_position_display()
        self.debounce.cancel_all()
        self.player.open(str(source))

    def _reset_position_display(self) -> None:
        """Blank the transport before a load.

        `Player.stats()` hands back the same object across files, so until the
        render thread rebuilds it still describes the *previous* video; showing
        its frame counter would be a lie for a second or so.
        """
        self.seek.set_fraction(0.0)
        self.seek.enable(False)
        self.time_label.configure(text="0:00 / 0:00")
        self.frame_label.configure(text="frame 0 / 0")
        self.play_btn.configure(text="Play")
        self.stats.update_values({key: "-" for key in
                                  ("fps", "decode", "boxes", "windows",
                                   "dropped", "buffer", "drift", "batch",
                                   "source")})

    def _on_next(self) -> None:
        base = Path(self._path) if self._path else Path(self.cfg.last_dir or ".")
        folder = base.parent if base.is_file() else base
        try:
            files = sorted(p for p in folder.iterdir()
                           if p.suffix.lower() in VIDEO_EXTS and p.is_file())
        except OSError as exc:
            self._status(f"Cannot list {folder}: {exc}", error=True)
            return
        if not files:
            self._status(f"No videos in {folder}", error=True)
            return
        index = files.index(base) + 1 if base in files else 0
        self.open_path(str(files[index % len(files)]))

    def _on_playpause(self) -> None:
        if not self._path:
            self._status("Open a file first.", error=True)
            return
        if self.player.state in ("idle", "error"):
            self.open_path(self._path)
            return
        self.debounce.flush()
        self.player.toggle()

    def _on_stop(self) -> None:
        self.player.stop()
        self._status("Stopped.")

    def _on_restart(self) -> None:
        if not self._path:
            return
        self.player.seek_frame(0)
        self.player.play()

    def _on_panic(self) -> None:
        self.player.panic()
        self._status("Panic: every window hidden, playback stopped.", error=True)

    def _on_scrub_start(self) -> None:
        self._resume_after_scrub = self.player.state == "playing"
        if self._resume_after_scrub:
            self.player.pause()

    def _on_scrub(self, fraction: float) -> None:
        stats = self.player.stats()
        total = max(1, stats.total_frames)
        frame = int(fraction * total)
        self.time_label.configure(
            text=f"{format_time(fraction * stats.duration)} / "
                 f"{format_time(stats.duration)}")
        self.frame_label.configure(text=f"frame {frame} / {stats.total_frames}")

    def _on_seek(self, fraction: float) -> None:
        self.player.seek_fraction(fraction)
        if self._resume_after_scrub:
            self._resume_after_scrub = False
            self.player.play()

    def _seek_by(self, seconds: float) -> None:
        stats = self.player.stats()
        self.player.seek_time(max(0.0, stats.position + seconds))

    def _on_speed(self, value: float) -> None:
        self.player.set_speed(float(value))
        self._status(f"Speed {value:.2f}x")

    def _on_loop(self, value: str) -> None:
        self.cfg.playback.loop = value
        self._status(f"Loop: {value}")

    def _on_reverse(self, value: bool) -> None:
        self.player.set_reverse(bool(value))
        self._status("Reverse playback." if value else "Forward playback.")

    def _on_mute(self, value: bool) -> None:
        self.player.set_mute(bool(value))

    def _on_volume(self, value: float) -> None:
        self.player.set_volume(int(value))

    def _on_backend(self, value: str) -> None:
        self.cfg.playback.audio_backend = value
        self._status(f"Audio backend '{value}' applies on the next open.")

    def _on_av_sync(self, value: bool) -> None:
        self.cfg.playback.av_sync = bool(value)
        self._status("A/V sync applies on the next open.")

    def _on_playback_reload(self, value: float) -> None:
        """Buffer knobs are read when the source is built, so rebuild it."""
        self.debounce.call("video", VIDEO_DEBOUNCE_MS, self._apply_video)

    def _on_monitor(self, value: int) -> None:
        self.cfg.render.monitor = int(value)
        self._apply_render()

    def _on_clear_region(self) -> None:
        self._set_region(None,
                         "Region cleared - the monitor picker is in charge again.")

    def _on_preset(self, name: str) -> None:
        apply_preset(self.cfg, name)
        self._resolve_grid()
        self._sync_from_cfg()
        self._apply_render()
        self._apply_video()
        self._status(f"Preset applied: {name}")

    def _on_effects_none(self) -> None:
        for field in ("trails", "echo_offset", "slitscan", "jitter", "strobe",
                      "shuffle"):
            setattr(self.cfg.effects, field, 0)
        self.cfg.effects.ghost = 0.0
        self.cfg.effects.time_warp = 0.0
        self._sync_from_cfg()
        self._apply_render()
        self._status("Effects cleared.")

    def _on_use_cache(self) -> None:
        self.cfg.use_cache = bool(self.use_cache_var.get())
        state = "on" if self.cfg.use_cache else "off"
        self._status(f"Bake cache {state} - applies on the next open.")

    # ---- refresh routing ---------------------------------------------
    def _apply_video(self) -> None:
        self._resolve_grid()
        self._show_grid()
        video = self.cfg.video
        self._status(f"Grid {video.grid_w}x{video.grid_h}, "
                     f"{video.max_windows} windows, {video.levels} levels")
        if self.player.state not in ("idle", "error"):
            self.player.refresh_video()

    def _apply_render(self) -> None:
        if self.player.state not in ("idle", "error"):
            self.player.refresh_render()

    # ---- player callbacks (render thread!) ---------------------------
    def _wire_player(self) -> None:
        self.player.on_state = lambda state, msg: self._post(
            lambda: self._on_player_state(state, msg))
        self.player.on_error = lambda msg: self._post(
            lambda: self._status(msg, error=True))
        self.player.on_ready = self._player_ready
        self.player.on_end = lambda: self._post(
            lambda: self._status("End of video."))

    def _player_ready(self, info: MediaInfo) -> None:
        """Runs on the render thread, before the frame source is built.

        Resolving the grid here rather than in the posted UI update is what
        avoids an immediate rebuild: `_build_source` has not run yet, so the
        source is created with the aspect-correct height first time.
        """
        self._resolve_grid()
        self._post(lambda: self._show_media(info))

    def _show_media(self, info: MediaInfo) -> None:
        self._show_grid()
        estimated = " (estimated)" if info.frames_estimated else ""
        self.media_label.configure(
            text=f"{Path(info.path).name} - {info.width}x{info.height} "
                 f"{info.codec}, {info.fps:.3f} fps, "
                 f"{format_time(info.duration)}, {info.frame_count} frames"
                 f"{estimated}, audio: {'yes' if info.has_audio else 'no'}, "
                 f"{format_bytes(info.size_bytes)}")
        self.seek.enable(True)
        self._status(f"Playing {Path(info.path).name}")

    _STATE_TEXT = {"loading": "Loading…", "playing": "Playing", "paused": "Paused",
                   "stopped": "Stopped", "idle": "Ready."}

    def _on_player_state(self, state: str, message: str) -> None:
        self.play_btn.configure(text="Pause" if state == "playing" else "Play")
        if state == "error":
            self._status(message or "Playback error", error=True)
            return
        if message:
            self._status(message)
            return
        # Transport changes carry no message, so without a per-state default
        # the bar keeps showing the previous line -- reading "Playing name.mp4"
        # while the player is paused.
        text = self._STATE_TEXT.get(state, state.title())
        name = Path(self._path).name if self._path else ""
        self._status(f"{text} {name}".strip() if name and state in
                     ("playing", "paused", "stopped") else text)

    def _post(self, fn: Callable[[], None]) -> None:
        """Marshal a callback onto the Tk thread.

        The deque is the contract; `_tick` drains it. The `after(0)` is only a
        latency shortcut and is taken *solely* when we are already on the Tk
        thread. Calling `after` from a foreign thread does not raise on this
        Tcl build -- it blocks the caller for a full second, and since every
        player state change posts from the render thread, startup was stalling
        three seconds before the first frame appeared.
        """
        self._events.append(fn)
        if threading.get_ident() == self._tk_thread:
            try:
                self.after(0, self._drain)
            except Exception:
                pass

    def _drain(self) -> None:
        while self._events and not self._closing:
            try:
                self._events.popleft()()
            except Exception as exc:               # a UI bug must not wedge us
                self._status(f"UI error: {type(exc).__name__}: {exc}", error=True)

    # ---- periodic tick -----------------------------------------------
    def _tick(self) -> None:
        self._drain()
        if not self._closing:
            self._refresh_stats()
            self._tick_job = self.after(TICK_MS, self._tick)

    def _refresh_stats(self) -> None:
        stats = self.player.stats()
        if self.player.state == "loading":
            return                   # stats still describe the previous file
        total = max(1, stats.total_frames)
        if not self.seek.dragging:
            self.seek.set_fraction(stats.frame / total)
            self.time_label.configure(
                text=f"{format_time(stats.position)} / {format_time(stats.duration)}")
            self.frame_label.configure(
                text=f"frame {stats.frame} / {stats.total_frames}")
        self.play_btn.configure(
            text="Pause" if self.player.state == "playing" else "Play")
        self.stats.update_values({
            "fps": f"{stats.render_fps:5.1f}",
            "decode": f"{stats.decode_fps:5.1f}",
            "boxes": f"{stats.boxes:d}",
            "windows": f"{stats.windows:d}",
            "dropped": f"{stats.dropped:d}",
            "buffer": f"{stats.buffered:d}",
            "drift": f"{stats.drift_ms:+.0f} ms",
            "batch": f"{stats.batch_ms:.2f} ms",
            "source": stats.source or "-",
        })
        if stats.message and self.player.state != "error":
            self.status.configure(text=stats.message, style="Status.TLabel")

    def _status(self, text: str, error: bool = False) -> None:
        self._status_text = text
        try:
            self.status.configure(text=text,
                                  style="Error.TLabel" if error else "Status.TLabel")
        except tk.TclError:
            pass                     # status arriving during teardown
        if error:
            try:
                self.bell()
            except tk.TclError:
                pass

    # ---- cache tab ----------------------------------------------------
    def _refresh_cache(self) -> None:
        for item in self.cache_tree.get_children():
            self.cache_tree.delete(item)
        total = 0
        try:
            rows = store.entries()
        except Exception as exc:
            self._status(f"Cache unreadable: {exc}", error=True)
            return
        import time as _time
        for path, size, mtime in reversed(rows):
            total += size
            self.cache_tree.insert(
                "", "end", text=path.name,
                values=(format_bytes(size),
                        _time.strftime("%Y-%m-%d %H:%M", _time.localtime(mtime))))
        self.cache_total.configure(
            text=f"{len(rows)} bake(s), {format_bytes(total)} in {cache_dir()}")

    def _on_prune(self) -> None:
        freed = store.prune()
        self._refresh_cache()
        self._status(f"Pruned {format_bytes(freed)}.")

    def _on_open_cache(self) -> None:
        try:
            os.startfile(str(cache_dir()))       # no subprocess, no console flash
        except Exception as exc:
            self._status(f"Cannot open {cache_dir()}: {exc}", error=True)

    def _on_bake(self) -> None:
        if self._bake_thread is not None and self._bake_thread.is_alive():
            self._bake_cancel.set()
            self.bake_label.configure(text="Cancelling…")
            return
        if not self._path:
            self._status("Open a video before baking it.", error=True)
            return
        self._resolve_grid()
        video = replace(self.cfg.video)          # frozen copy: the UI keeps moving
        existing = store.lookup(self._path, video.fingerprint())
        if existing is not None:
            # Re-baking would also mean replacing a file the player may have
            # mmap'd for playback, which Windows refuses outright. An entry
            # only survives `lookup` if it matches this source and these
            # settings, so there is nothing to gain by rewriting it.
            frames = len(existing)
            existing.close()
            self.bake_label.configure(
                text=f"Already baked: {frames} frames for these settings.")
            self._status("Already baked - change the grid to make a new bake.")
            return
        self._bake_cancel.clear()
        self.bake_btn.configure(text="Cancel bake")
        self.bake_progress.configure(value=0, maximum=100)
        self.bake_label.configure(text="Starting…")
        self._bake_thread = threading.Thread(
            target=self._bake_worker, args=(self._path, video),
            name="timeleap-bake", daemon=True)
        self._bake_thread.start()

    def _bake_worker(self, path: str, video: Any) -> None:
        try:
            info = probe(path)
            fingerprint = video.fingerprint()
            header = BakeHeader(
                fps=info.fps, grid_w=video.grid_w, grid_h=video.grid_h,
                levels=max(1, video.levels), frame_count=info.frame_count,
                duration=info.duration, source=path,
                source_hash=store.source_hash(path), fingerprint=fingerprint,
                created="", algo=video.algo)
            total = max(1, info.frame_count)

            def progress(done: int) -> None:
                if self._bake_cancel.is_set():
                    raise _BakeCancelled()
                if done % BAKE_STEP == 0:
                    self._post(lambda d=done: self._bake_tick(d, total))

            write_bake(store.cache_path(path, fingerprint), header,
                       bake_frames(path, video, info.fps, video.grid_w,
                                   video.grid_h),
                       progress=progress)
        except _BakeCancelled:
            self._post(lambda: self._bake_done("Bake cancelled."))
        except Exception as exc:
            message = f"Bake failed: {type(exc).__name__}: {exc}"
            self._post(lambda: self._bake_done(message, error=True))
        else:
            self._post(lambda: self._bake_done(
                "Bake complete - the next play of this file will use it."))

    def _bake_tick(self, done: int, total: int) -> None:
        self.bake_progress.configure(value=min(100.0, 100.0 * done / total))
        self.bake_label.configure(text=f"Baking… {done} / {total} frames")

    def _bake_done(self, message: str, error: bool = False) -> None:
        # Dropping the handle here, on the Tk thread, is what stops the click
        # right after "complete" from being read as a cancel: the worker is
        # still technically alive for the instant it takes to unwind.
        self._bake_thread = None
        self.bake_btn.configure(text="Bake this video")
        self.bake_progress.configure(value=0)
        self.bake_label.configure(text=message)
        self._refresh_cache()
        self._status(message, error=error)

    # ---- keys and shutdown -------------------------------------------
    def _bind_keys(self) -> None:
        def typing() -> bool:
            widget = self.focus_get()
            return isinstance(widget, (tk.Entry, ttk.Entry, ttk.Combobox))

        def guard(fn: Callable[[], None]) -> Callable[[tk.Event], str | None]:
            def handler(_event: tk.Event) -> str | None:
                if typing():
                    return None
                fn()
                return "break"
            return handler

        self.bind("<space>", guard(self._on_playpause))
        self.bind("<Escape>", guard(self._on_stop))
        self.bind("<F5>", guard(self._on_restart))
        self.bind("<Control-o>", guard(self._on_open))
        self.bind("<Control-n>", guard(self._on_next))
        self.bind("<Left>", guard(lambda: self._seek_by(-5.0)))
        self.bind("<Right>", guard(lambda: self._seek_by(5.0)))
        self.bind("<Control-m>", guard(self._on_pick_region))

    def _start_hotkeys(self) -> None:
        """Global panic key. Optional: a missing `ui.hotkeys` must not stop
        the panel from opening, so every failure here is only reported."""
        if not self.cfg.panic_hotkey:
            self.hotkey_label.configure(text="Global hotkeys disabled in config.")
            return
        try:
            from .hotkeys import HotkeyManager
        except Exception:
            self.hotkey_label.configure(
                text="Global hotkeys unavailable (ui.hotkeys not installed).")
            return
        try:
            manager = HotkeyManager(
                on_panic=lambda: self._post(self._on_panic),
                on_playpause=lambda: self._post(self._on_playpause),
                on_next=lambda: self._post(self._on_next))
            ok = bool(manager.start())
        except Exception as exc:
            self.hotkey_label.configure(text=f"Global hotkeys failed: {exc}")
            return
        self._hotkeys = manager if ok else None
        self.hotkey_label.configure(
            text="Global hotkeys registered."
            if ok else "Global hotkeys refused (another app owns them).")

    def _safe_geometry(self, geometry: str, fallback: str) -> str:
        """Reject a stored geometry that would open off the current desktop."""
        try:
            size, _, offset = geometry.partition("+")
            width, height = (int(v) for v in size.split("x"))
            if not offset:
                return f"{width}x{height}"
            x, y = (int(v) for v in offset.split("+"))
            screen = w32.virtual_screen_monitor()
            if not (screen.x - 32 <= x <= screen.x + screen.width - 120):
                return fallback
            if not (screen.y - 32 <= y <= screen.y + screen.height - 120):
                return fallback
            return f"{width}x{height}+{x}+{y}"
        except Exception:
            return fallback

    def _on_close(self) -> None:
        self._closing = True
        picker = getattr(self, "_picker", None)
        if picker is not None and picker.winfo_exists():
            picker.cancel()          # a stray top-most overlay outlives us otherwise
        self._picker = None
        self.debounce.cancel_all()
        if self._tick_job is not None:
            try:
                self.after_cancel(self._tick_job)
            except Exception:
                pass
            self._tick_job = None
        self._bake_cancel.set()
        self.player.on_state = self.player.on_error = None
        self.player.on_ready = self.player.on_end = None
        try:
            self.player.close()
        except Exception:
            pass
        if self._bake_thread is not None and self._bake_thread.is_alive():
            self._bake_thread.join(timeout=2.0)
        if self._hotkeys is not None:
            try:
                self._hotkeys.stop()
            except Exception:
                pass
        try:
            self.ui_state.geometry = self.winfo_geometry()
            self.ui_state.tab = self.nb.index(self.nb.select())
        except Exception:
            pass
        self.ui_state.save()
        self.cfg.save()
        self.destroy()


def main(argv: Sequence[str] | None = None) -> int:
    """`python -m timeleap.ui.app [file]` -- a standalone entry point."""
    import sys
    args = list(sys.argv[1:] if argv is None else argv)
    TimeLeapApp(AppConfig.load(), args[0] if args else None).run()
    return 0


if __name__ == "__main__":          # pragma: no cover
    raise SystemExit(main())
