"""TimeLeapPlayer's windows. Three of them, each with one job:

* the **home window** (the Tk root) while nothing plays: open a video or pick
  a recent one;
* the **player overlay** (`overlay.py`) over the video itself: hover for the
  controls, drag to move, edges to resize, double-click for fullscreen,
  right-click for quick options -- like any media player;
* **settings**, on demand, for everything else.

Player callbacks arrive on the render thread, so nothing in this file touches
a widget from one: a callback is appended to a deque and replayed on the Tk
tick. Modal dialogs are avoided for the same reason -- the prototype's
off-thread `messagebox.showerror` deadlocked.

Which knob needs which refresh: `cfg.render.*` and `cfg.effects.*` are live
(`refresh_render`), while `cfg.video.*` rebuilds the frame source
(`refresh_video`) and is therefore debounced -- dragging the grid slider must
not spawn an ffmpeg per pixel.
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

from PIL import Image, ImageTk

from ..cache import store
from ..cache.format import BakeHeader, write_bake
from ..config import PRESETS, AppConfig, apply_preset, cache_dir, config_dir
from ..core import palette
from ..engine.pipeline import bake_frames
from ..engine.player import Player
from ..media.probe import MediaInfo, have_ffmpeg, probe
from ..render import win32 as w32
from . import widgets
from .overlay import (ACCENT, PANEL, HOVER, TEXT, PlayerOverlay, Rect,
                      default_rect, fit_aspect, keep_visible)
from .widgets import (COLORS, UI_CAP, Debouncer, FormGrid, LabeledSlider,
                      MappedCombo, PillButton, ScrollFrame, StatReadout,
                      SwatchStrip, format_bytes, px)

APP_VERSION = "1.1"
TICK_MS = 200                    # stats refresh; also drains the callback queue
VIDEO_DEBOUNCE_MS = 250          # a source rebuild is expensive
RENDER_DEBOUNCE_MS = 60          # a palette/gap change is not
BAKE_STEP = 8                    # progress posts per N frames
RECENT_MAX = 6

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
_SPEEDS = (0.25, 0.5, 0.75, 1.0, 1.25, 1.5, 2.0)
_MENU = {"bg": PANEL, "fg": TEXT, "activebackground": HOVER,
         "activeforeground": TEXT, "bd": 0}

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


def _short(text: str, limit: int) -> str:
    """Middle-elide, keeping both ends: drives and extensions both matter."""
    if len(text) <= limit:
        return text
    keep = (limit - 1) // 2
    return text[:keep] + "…" + text[-(limit - 1 - keep):]


def _hwnd(window: tk.Misc) -> int:
    try:
        return int(window.wm_frame(), 16)
    except (tk.TclError, ValueError):
        return 0


class _BakeCancelled(RuntimeError):
    """Raised out of the progress callback so `write_bake` unwinds and
    deletes its .tmp instead of leaving a partial cache entry behind."""


@dataclass
class _UiState:
    """Window positions and the open settings tab.

    Deliberately not in `AppConfig`: that file is the documented settings
    contract shared with the CLI, and where a window sat is not a setting.
    """

    geometry: str = ""
    settings_geometry: str = ""
    tab: int = 0

    @staticmethod
    def path() -> Path:
        return config_dir() / "ui.json"

    @classmethod
    def load(cls) -> _UiState:
        try:
            raw = json.loads(cls.path().read_text("utf-8"))
            return cls(geometry=str(raw.get("geometry", "")),
                       settings_geometry=str(raw.get("settings_geometry", "")),
                       tab=int(raw.get("tab", 0)))
        except Exception:
            return cls()

    def save(self) -> None:
        try:
            self.path().write_text(json.dumps(self.__dict__, indent=2), "utf-8")
        except Exception:
            pass                 # window position is a convenience, never fatal


class TimeLeapApp(tk.Tk):
    """Owns the one `Player` and the three windows around it."""

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
        self._menu_vars: list[tk.Variable] = []
        self.settings: tk.Toplevel | None = None
        self.fullscreen = False
        self._windowed: Rect | None = None

        self.title("TimeLeapPlayer")
        self._set_window_icon()
        self.style = widgets.apply_theme(self)
        self.debounce = Debouncer(self)
        self.resizable(False, False)

        self._build_home()
        self.overlay = PlayerOverlay(self)
        self._wire_player()
        self._bind_keys()
        self._start_hotkeys()
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self._place_home()
        self._tick()
        if initial:
            self.after(150, lambda: self.open_path(initial))

    def _icon_path(self) -> Path:
        return Path(__file__).resolve().parent.parent / "assets" / "timeleap.ico"

    def _set_window_icon(self) -> None:
        """Title bar, taskbar and Alt-Tab icon, for this and every Toplevel."""
        icon = self._icon_path()
        if icon.is_file():
            try:
                self.iconbitmap(default=str(icon))
            except tk.TclError:
                pass

    def run(self) -> None:
        self.mainloop()

    # ---- home --------------------------------------------------------
    def _build_home(self) -> None:
        bg, c = COLORS["bg"], COLORS
        body = tk.Frame(self, bg=bg, padx=px(40), pady=px(30))
        body.pack(fill="both", expand=True)
        tk.Frame(body, bg=bg, width=px(420), height=0).pack()     # width strut

        self._logo = self._logo_image(px(88))
        if self._logo is not None:
            tk.Label(body, image=self._logo, bg=bg).pack(pady=(px(2), px(12)))
        tk.Label(body, text="TimeLeapPlayer", bg=bg, fg=c["fg"],
                 font=("Segoe UI Semibold", 20)).pack()
        tk.Label(body, text="Play any video using real windows as pixels",
                 bg=bg, fg=c["muted"], font=("Segoe UI", 10)).pack(
            pady=(px(2), px(24)))
        PillButton(body, "Open video", self.on_open, width=240, height=48,
                   fill=ACCENT, hover="#86d6ff", fg="#04121c", bg=bg,
                   font=("Segoe UI Semibold", 12)).pack()
        tk.Label(body, text="or press Ctrl+O", bg=bg, fg=c["muted"],
                 font=("Segoe UI", 9)).pack(pady=(px(8), 0))

        self.recent_frame = tk.Frame(body, bg=bg)
        self.recent_frame.pack(fill="x")
        self.home_status = tk.Label(body, text="", bg=bg, fg=c["danger"],
                                    font=("Segoe UI", 9), wraplength=px(420))
        self.home_status.pack(pady=(px(10), 0))

        foot = tk.Frame(body, bg=bg)
        foot.pack(fill="x", pady=(px(14), 0))
        gear = tk.Label(foot, text="⚙  Settings", bg=bg, fg=c["muted"],
                        font=("Segoe UI", 10), cursor="hand2")
        gear.pack(side="left")
        gear.bind("<Enter>", lambda _e: gear.configure(fg=c["fg"]))
        gear.bind("<Leave>", lambda _e: gear.configure(fg=c["muted"]))
        gear.bind("<Button-1>", lambda _e: self.open_settings())
        tk.Label(foot, text=f"v{APP_VERSION}", bg=bg, fg=c["line"],
                 font=("Segoe UI", 9)).pack(side="right")
        self._refresh_recent()

    def _logo_image(self, size: int) -> ImageTk.PhotoImage | None:
        try:
            img = Image.open(self._icon_path())
            img.size = (256, 256)            # pick the largest frame of the .ico
            img = img.convert("RGBA").resize((size, size), Image.LANCZOS)
            return ImageTk.PhotoImage(img, master=self)
        except Exception:
            return None

    def _refresh_recent(self) -> None:
        frame, bg, c = self.recent_frame, COLORS["bg"], COLORS
        for child in frame.winfo_children():
            child.destroy()
        items = [p for p in self.cfg.recent if os.path.isfile(p)][:RECENT_MAX]
        if not items:
            return
        tk.Label(frame, text="RECENT", bg=bg, fg=c["muted"], font=UI_CAP).pack(
            anchor="w", pady=(px(24), px(6)))
        for path in items:
            self._recent_row(frame, path)

    def _recent_row(self, frame: tk.Frame, path: str) -> None:
        bg, hover, c = COLORS["bg"], "#1c2028", COLORS
        row = tk.Frame(frame, bg=bg, cursor="hand2", padx=px(12), pady=px(8))
        row.pack(fill="x")
        source = Path(path)
        name = tk.Label(row, text=_short(source.name, 34),
                        bg=bg, fg=c["fg"], font=("Segoe UI Semibold", 10),
                        cursor="hand2")
        name.pack(side="left")
        where = tk.Label(row, text=_short(str(source.parent), 26),
                         bg=bg, fg=c["muted"], font=("Segoe UI", 9), cursor="hand2")
        where.pack(side="right")
        parts = (row, name, where)
        for part in parts:
            part.bind("<Enter>", lambda _e: [p.configure(bg=hover) for p in parts])
            part.bind("<Leave>", lambda _e: [p.configure(bg=bg) for p in parts])
            part.bind("<Button-1>", lambda _e, p=path: self.open_path(p))

    def _place_home(self) -> None:
        """Centre on the primary monitor, or wherever the user last left it."""
        self.update_idletasks()
        width, height = self.winfo_reqwidth(), self.winfo_reqheight()
        mon = self._primary()
        fallback = (f"+{mon[0] + (mon[2] - width) // 2}"
                    f"+{mon[1] + (mon[3] - height) // 3}")
        saved = self.ui_state.geometry.partition("+")[2]
        self.geometry(self._safe_geometry(f"{width}x{height}+{saved}", "")
                      if saved else fallback)
        if not saved or self.geometry() == "1x1+0+0":
            self.geometry(fallback)
        self.after(60, lambda: w32.dark_title_bar(_hwnd(self)))

    def _show_home(self) -> None:
        self._refresh_recent()
        self.update_idletasks()
        self.geometry(f"{self.winfo_reqwidth()}x{self.winfo_reqheight()}")
        self.deiconify()
        self.lift()
        self.focus_force()
        self.after(60, lambda: w32.dark_title_bar(_hwnd(self)))

    def _primary(self) -> Rect:
        for m in w32.monitors():
            if m.primary:
                return m.rect
        return w32.virtual_screen_monitor().rect

    # ---- settings ----------------------------------------------------
    def open_settings(self) -> None:
        if self.settings is None or not self.settings.winfo_exists():
            self._build_settings()
        win = self.settings
        assert win is not None
        win.attributes("-topmost", bool(self.cfg.render.topmost
                                        and self.overlay.active))
        win.deiconify()
        win.lift()
        win.focus_force()
        win.after(60, lambda: w32.dark_title_bar(_hwnd(win)))
        self._refresh_stats()

    def _build_settings(self) -> None:
        win = tk.Toplevel(self)
        win.withdraw()
        win.title("Settings - TimeLeapPlayer")
        win.configure(bg=COLORS["bg"])
        win.minsize(px(560), px(420))
        mon = self._primary()
        width, height = min(px(700), mon[2] - px(40)), min(px(640), mon[3] - px(80))
        fallback = (f"{width}x{height}+{mon[0] + (mon[2] - width) // 2}"
                    f"+{mon[1] + (mon[3] - height) // 2}")
        win.geometry(self._safe_geometry(self.ui_state.settings_geometry, fallback))
        win.protocol("WM_DELETE_WINDOW", win.withdraw)
        win.bind("<Escape>", lambda _e: win.withdraw())
        self.settings = win

        self.status = ttk.Label(win, text=self._status_text, style="Status.TLabel",
                                anchor="w", padding=(px(12), px(6)))
        self.status.pack(side="bottom", fill="x")
        strip = ttk.Frame(win, style="TL.TFrame", padding=(px(12), px(6)))
        strip.pack(side="bottom", fill="x")
        self.stats = StatReadout(strip, (
            ("fps", "render", 6), ("boxes", "boxes", 5), ("windows", "windows", 5),
            ("dropped", "dropped", 6), ("buffer", "buffer", 5),
            ("source", "source", 7)))
        self.stats.pack(side="left")

        self.nb = ttk.Notebook(win)
        self.nb.pack(side="top", fill="both", expand=True, padx=px(10),
                     pady=(px(10), 0))
        for builder, label in ((self._tab_picture, "Picture"),
                               (self._tab_playback, "Playback"),
                               (self._tab_effects, "Effects"),
                               (self._tab_advanced, "Advanced"),
                               (self._tab_cache, "Cache"),
                               (self._tab_about, "About")):
            self._add_page(builder, label)
        try:
            self.nb.select(self.ui_state.tab)
        except Exception:
            pass
        self._sync_from_cfg()

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

    def _page(self) -> tuple[ttk.Frame, FormGrid]:
        page = ttk.Frame(self._page_parent, style="TL.TFrame", padding=px(16))
        form = FormGrid(page)
        form.pack(side="left", anchor="n")
        return page, form

    def _tab_picture(self) -> ttk.Frame:
        page, form = self._page()
        form.heading("Style")
        self.preset_combo = MappedCombo(
            form, [(name, name) for name in PRESETS], value=None,
            command=self.apply_preset, width=22)
        form.add("Preset", self.preset_combo, "a starting point")
        form.add("Palette", self._combo(form, "render", "palette",
                                        [(n, n) for n in palette.names()]))
        self.swatch = SwatchStrip(form)
        form.add("", self.swatch)
        form.add("Levels", self._slider(form, "video", "levels", 1, 16,
                                        integer=True), "1 = black & white")
        form.add("", self._check(form, "Invert", "video", "invert"))

        form.heading("Detail")
        form.add("Grid width", self._slider(form, "video", "grid_w", 32, 256,
                                            integer=True, step=2), "more = sharper")
        self.grid_h_slider = self._slider(form, "video", "grid_h", 18, 256,
                                          integer=True)
        form.add("Grid height", self.grid_h_slider)
        form.add("", self._check(form, "Follow the video's aspect ratio",
                                 "video", "auto_grid"))
        form.add("Max windows", self._slider(form, "video", "max_windows",
                                             10, 400, integer=True, step=2),
                 "more = detail, less = speed")

        form.heading("Window")
        form.add("Fit", self._combo(form, "render", "fit", _FITS))
        form.add("Gap", self._slider(form, "render", "gap", 0, 12,
                                     integer=True, length=140), "grid look")
        form.add("", self._check(form, "Black backdrop behind the picture",
                                 "render", "background_blackout"))
        form.add("", self._check(form, "Always on top", "render", "topmost"))
        form.add("", self._check(form, "Fullscreen spans every monitor",
                                 "render", "fullscreen_all"))
        return page

    def _tab_playback(self) -> ttk.Frame:
        page, form = self._page()
        form.heading("Playback")
        speed = ttk.Frame(form, style="TL.TFrame")
        self.speed_slider = self._slider(speed, "playback", "speed", 0.1, 4.0,
                                         step=0.05, unit="x", length=220,
                                         on_change=self.set_speed)
        self.speed_slider.pack(side="left")
        ttk.Button(speed, text="1.0x", width=5,
                   command=lambda: self.set_speed(1.0)).pack(side="left",
                                                             padx=px(8))
        form.add("Speed", speed)
        form.add("Loop", self._combo(form, "playback", "loop", _LOOPS,
                                     on_change=self._on_loop))
        form.add("", self._check(form, "Play in reverse (audio muted)",
                                 "playback", "reverse",
                                 on_change=self._on_reverse))
        form.heading("Audio")
        form.add("", self._check(form, "Mute", "playback", "mute",
                                 on_change=lambda v: self.toggle_mute(bool(v))))
        form.add("Volume", self._slider(form, "playback", "volume", 0, 100,
                                        integer=True, length=220,
                                        on_change=self.set_volume))
        return page

    def _tab_effects(self) -> ttk.Frame:
        page, form = self._page()
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
                                 command=self._on_effects_none), pady=(px(16), 0))
        return page

    def _tab_advanced(self) -> ttk.Frame:
        page, form = self._page()
        form.heading("Decomposition")
        form.add("Algorithm", self._combo(form, "video", "algo", _ALGOS))
        form.add("Threshold", self._combo(form, "video", "threshold_mode",
                                          _THRESHOLDS))
        form.add("Fixed level", self._slider(form, "video", "fixed_threshold",
                                             0, 255, integer=True))
        form.add("", self._check(form, "Denoise speckle", "video", "denoise"))
        form.heading("Tone")
        form.add("Gamma", self._slider(form, "video", "gamma", 0.2, 3.0, step=0.05))
        form.add("Contrast", self._slider(form, "video", "contrast", 0.2, 3.0,
                                          step=0.05))
        form.add("Brightness", self._slider(form, "video", "brightness",
                                            -128, 127, integer=True))
        form.heading("Renderer")
        form.add("Redraw", self._combo(form, "render", "redraw", _REDRAWS,
                                       width=34))
        form.add("Min window px", self._slider(form, "render", "min_window_px",
                                               1, 24, integer=True, length=140))
        form.add("", self._check(form, "Diff frames (skip unchanged slots)",
                                 "render", "diff_frames"))
        form.add("", self._check(form, "Stable slots (kills jitter)",
                                 "render", "stable_slots"))
        form.heading("Audio and buffering")
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
        hint = ttk.Label(form, style="Hint.TLabel", justify="left",
                         text="Checking PATH…")
        form.add_wide(hint, pady=(px(12), 0))
        self._probe_path_async(hint)
        return page

    def _probe_path_async(self, hint: ttk.Label) -> None:
        """Resolve which ffmpeg binaries exist without blocking the build.

        `have_ffmpeg()` runs three `-version` subprocesses. Called inline it
        added ~240 ms to window construction for a line nobody reads first.
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
            hint.configure(text=(f"On PATH: {found}.\nBackend and A/V sync apply "
                                 "the next time a file is opened."))
        except tk.TclError:
            pass                      # window went away while we were probing

    def _tab_cache(self) -> ttk.Frame:
        page = ttk.Frame(self._page_parent, style="TL.TFrame", padding=px(16))
        bar = ttk.Frame(page, style="TL.TFrame")
        bar.pack(fill="x")
        self.bake_btn = ttk.Button(bar, text="Bake this video",
                                   style="Accent.TButton", command=self._on_bake)
        self.bake_btn.pack(side="left")
        ttk.Button(bar, text="Prune", command=self._on_prune).pack(side="left",
                                                                  padx=px(6))
        ttk.Button(bar, text="Open folder",
                   command=self._on_open_cache).pack(side="left")
        self.use_cache_var = tk.BooleanVar(self, self.cfg.use_cache)
        ttk.Checkbutton(bar, text="Use cache", variable=self.use_cache_var,
                        command=self._on_use_cache).pack(side="right")
        ttk.Label(page, style="Hint.TLabel", justify="left",
                  text="Baking stores the geometry of a video, so replaying it "
                       "costs no decoding\nand scrubbing becomes instant.").pack(
            anchor="w", pady=(px(10), 0))

        self.bake_progress = ttk.Progressbar(page, mode="determinate", maximum=100)
        self.bake_progress.pack(fill="x", pady=(px(12), px(4)))
        self.bake_label = ttk.Label(page, text="", style="Hint.TLabel")
        self.bake_label.pack(anchor="w")

        holder = ttk.Frame(page, style="TL.TFrame")
        holder.pack(fill="both", expand=True, pady=(px(10), 0))
        self.cache_tree = ttk.Treeview(holder, columns=("size", "when"),
                                       show="tree headings", height=8)
        self.cache_tree.heading("#0", text="Bake")
        self.cache_tree.heading("size", text="Size")
        self.cache_tree.heading("when", text="Modified")
        self.cache_tree.column("#0", width=px(300), anchor="w")
        self.cache_tree.column("size", width=px(80), anchor="e")
        self.cache_tree.column("when", width=px(130), anchor="e")
        scroll = ttk.Scrollbar(holder, orient="vertical",
                               command=self.cache_tree.yview)
        self.cache_tree.configure(yscrollcommand=scroll.set)
        self.cache_tree.pack(side="left", fill="both", expand=True)
        scroll.pack(side="right", fill="y")
        self.cache_total = ttk.Label(page, text="", style="Field.TLabel")
        self.cache_total.pack(anchor="w", pady=(px(6), 0))
        self._refresh_cache()
        return page

    def _tab_about(self) -> ttk.Frame:
        page = ttk.Frame(self._page_parent, style="TL.TFrame", padding=px(18))
        ttk.Label(page, text=f"TimeLeapPlayer {APP_VERSION}",
                  style="Head.TLabel").pack(anchor="w")
        ttk.Label(page, style="TLabel", justify="left", text=(
            "Plays video by using real Windows windows as pixels. Each frame is\n"
            "thresholded to a coarse grid and decomposed into a few dozen\n"
            "rectangles, and every rectangle is a window. Inspired by\n"
            "mon/bad_apple_virus.")).pack(anchor="w", pady=(px(10), px(16)))
        for title, rows in (
            ("ON THE VIDEO", (("Hover", "show the controls"),
                              ("Drag", "move the window"),
                              ("Drag an edge", "resize it"),
                              ("Click", "play / pause"),
                              ("Double-click", "fullscreen"),
                              ("Wheel", "volume"),
                              ("Right-click", "quick options"))),
            ("KEYS", (("Space", "play / pause"),
                      ("Left / Right", "seek 5 seconds"),
                      ("Up / Down", "volume"),
                      ("F  /  Esc", "fullscreen / leave it"),
                      ("M", "mute"),
                      ("Ctrl+O  /  Ctrl+N", "open / next in folder"),
                      ("Ctrl+Alt+Q", "PANIC - global, hides everything"),
                      ("Ctrl+Alt+Space", "play / pause - global")))):
            ttk.Label(page, text=title, style="Cap.TLabel",
                      background=COLORS["bg"]).pack(anchor="w", pady=(px(6), 0))
            grid = FormGrid(page)
            grid.pack(anchor="w", pady=(px(4), px(8)))
            for key, what in rows:
                grid.add(key, ttk.Label(grid, text=what, style="TLabel"))
        self.hotkey_label = ttk.Label(page, text=self._hotkey_note,
                                      style="Hint.TLabel")
        self.hotkey_label.pack(anchor="w", pady=(px(8), 0))
        ttk.Label(page, text=f"Config: {AppConfig.path()}", style="Hint.TLabel").pack(
            anchor="w", pady=(px(6), 0))
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

    def _sync_field(self, section: str, field: str) -> None:
        """Mirror one config value into its settings widgets (if built)."""
        value = getattr(getattr(self.cfg, section), field)
        self._syncing = True
        try:
            for s, f, widget in self._ctls:
                if (s, f) != (section, field):
                    continue
                if isinstance(widget, LabeledSlider):
                    widget.set(value)
                elif isinstance(widget, MappedCombo):
                    widget.set_value(value)
                elif isinstance(widget, tk.Variable):
                    widget.set(value)
        except tk.TclError:
            pass
        finally:
            self._syncing = False

    def _sync_from_cfg(self) -> None:
        """Push every config value back into the widgets (after a preset)."""
        if self.settings is None:
            return
        for section, field, _widget in list(self._ctls):
            self._sync_field(section, field)
        self.use_cache_var.set(self.cfg.use_cache)
        self._show_grid()
        self._show_swatch()

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
        if self.settings is None:
            return
        self.grid_h_slider.enable(not self.cfg.video.auto_grid)
        self._sync_field("video", "grid_h")

    def _show_swatch(self) -> None:
        if self.settings is not None:
            self.swatch.show(self.cfg.render.palette, self.cfg.video.levels)

    # ---- the player window -------------------------------------------
    def aspect(self) -> float:
        media = self.player.media
        if media is not None and media.aspect > 0:
            return media.aspect
        _x, _y, width, height = self.overlay.rect
        return width / max(1, height)

    def set_window_rect(self, rect: Rect, final: bool = False) -> None:
        """Move/resize the video. The overlay follows instantly; the pixel
        windows follow on the render thread's next pass."""
        bounds = w32.virtual_screen_monitor().rect
        rect = keep_visible(tuple(int(v) for v in rect), bounds)    # type: ignore[arg-type]
        self.overlay.place(rect)
        self.cfg.render.region = rect
        if self.player.state not in ("idle", "error"):
            self.player.refresh_render(effects=False)
        if final and not self.fullscreen:
            self._windowed = rect

    def _initial_rect(self) -> Rect:
        region = self.cfg.render.region
        if region:
            return keep_visible(tuple(int(v) for v in region),     # type: ignore[arg-type]
                                w32.virtual_screen_monitor().rect)
        mon = self.cfg.render.monitor
        area = w32.target_rect(mon, None) if mon >= 0 else self._primary()
        return default_rect(area, self.aspect() if self.player.media else 16 / 9)

    def toggle_fullscreen(self) -> None:
        if not self.overlay.active:
            return
        if self.fullscreen:
            self.fullscreen = False
            self.set_window_rect(self._windowed or self._initial_rect(), final=True)
            return
        self._windowed = self.overlay.rect
        if self.cfg.render.fullscreen_all:
            target = w32.virtual_screen_monitor().rect
        else:
            x, y, width, height = self.overlay.rect
            target = w32.monitor_at(x + width // 2, y + height // 2).rect
        self.fullscreen = True
        self.set_window_rect(target)

    # ---- transport (the overlay, the menu and the keys all land here) --
    def on_open(self) -> None:
        parent = self.overlay.surface if self.overlay.active else self
        path = filedialog.askopenfilename(
            parent=parent, title="Open video", filetypes=FILETYPES,
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
        self.cfg.recent = [self._path] + [p for p in self.cfg.recent
                                          if os.path.normcase(p)
                                          != os.path.normcase(self._path)]
        del self.cfg.recent[RECENT_MAX * 2:]
        self.home_status.configure(text="")
        self.debounce.cancel_all()
        rect = self.overlay.rect if self.fullscreen else self._initial_rect()
        self.cfg.render.region = rect
        if not self.fullscreen:
            self._windowed = rect
        self.overlay.show(rect, source.name)
        self.withdraw()
        self._status(f"Loading {source.name}…")
        self.player.open(str(source))

    def close_video(self) -> None:
        """The overlay's X: stop, put the windows away, back to home."""
        if self.fullscreen:
            self.fullscreen = False
            if self._windowed:
                self.cfg.render.region = self._windowed
        self.overlay.hide()
        self._resume_after_scrub = False
        try:
            self.player.close()
        except Exception:
            pass
        self._status("Ready.")
        self._show_home()

    def next_file(self) -> None:
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

    def toggle_play(self) -> None:
        if not self._path:
            self.on_open()
            return
        if self.player.state in ("idle", "error"):
            self.open_path(self._path)
            return
        self.debounce.flush()
        stats = self.player.stats()
        if (self.player.state == "stopped"
                and stats.frame >= max(0, stats.total_frames - 2)):
            self.player.seek_frame(0)        # at the end: play again from the top
        playing = self.player.state == "playing"
        self.player.toggle()
        self.overlay.toast("Paused" if playing else "Playing", 0.7)

    def seek_by(self, seconds: float) -> None:
        if self.player.state in ("idle", "error", "loading"):
            return
        stats = self.player.stats()
        target = max(0.0, min(stats.duration, stats.position + seconds))
        self.player.seek_time(target)
        self.overlay.toast(f"{widgets.format_time(target)} / "
                           f"{widgets.format_time(stats.duration)}", 0.9)

    def begin_scrub(self) -> None:
        self._resume_after_scrub = self.player.state == "playing"
        if self._resume_after_scrub:
            self.player.pause()

    def scrub_to(self, fraction: float, final: bool) -> None:
        self.player.seek_fraction(fraction)
        if final and self._resume_after_scrub:
            self._resume_after_scrub = False
            self.player.play()

    def set_volume(self, value: float, toast: bool = False) -> None:
        value = max(0, min(100, int(round(value))))
        if self.cfg.playback.mute and value > 0:
            self.player.set_mute(False)
            self._sync_field("playback", "mute")
        self.player.set_volume(value)
        self._sync_field("playback", "volume")
        if toast:
            self.overlay.toast(f"Volume {value}%", 0.9)

    def toggle_mute(self, mute: bool | None = None) -> None:
        mute = (not self.cfg.playback.mute) if mute is None else bool(mute)
        self.player.set_mute(mute)
        self._sync_field("playback", "mute")
        self.overlay.toast("Muted" if mute else
                           f"Volume {self.cfg.playback.volume}%", 0.9)

    def set_speed(self, value: float) -> None:
        self.player.set_speed(float(value))
        self._sync_field("playback", "speed")
        self._status(f"Speed {float(value):.2f}x", toast=True)

    def apply_preset(self, name: str) -> None:
        apply_preset(self.cfg, name)
        self._resolve_grid()
        self._sync_from_cfg()
        self._apply_render()
        self._apply_video()
        self._status(f"Style: {name}", toast=True)

    def _set_live(self, section: str, field: str, value: Any, note: str) -> None:
        """A menu change: store, mirror into settings, apply, announce."""
        self._set(section, field, value)
        self._sync_field(section, field)
        self._status(note, toast=True)

    def _on_loop(self, value: str) -> None:
        self.cfg.playback.loop = value
        self._status(f"Loop: {value}", toast=True)

    def _on_reverse(self, value: bool) -> None:
        self.player.set_reverse(bool(value))
        self._status("Reverse playback" if value else "Forward playback", toast=True)

    def _on_backend(self, value: str) -> None:
        self.cfg.playback.audio_backend = value
        self._status(f"Audio backend '{value}' applies on the next open.")

    def _on_av_sync(self, value: bool) -> None:
        self.cfg.playback.av_sync = bool(value)
        self._status("A/V sync applies on the next open.")

    def _on_playback_reload(self, value: float) -> None:
        """Buffer knobs are read when the source is built, so rebuild it."""
        self.debounce.call("video", VIDEO_DEBOUNCE_MS, self._apply_video)

    def _on_panic(self) -> None:
        self.player.panic()
        self.close_video()
        self._status("Panic: every window hidden, playback stopped.", error=True)

    def _on_effects_none(self) -> None:
        for field in ("trails", "echo_offset", "slitscan", "jitter", "strobe",
                      "shuffle"):
            setattr(self.cfg.effects, field, 0)
        self.cfg.effects.ghost = 0.0
        self.cfg.effects.time_warp = 0.0
        self._sync_from_cfg()
        self._apply_render()
        self._status("Effects cleared.", toast=True)

    def _on_use_cache(self) -> None:
        self.cfg.use_cache = bool(self.use_cache_var.get())
        state = "on" if self.cfg.use_cache else "off"
        self._status(f"Bake cache {state} - applies on the next open.")

    # ---- the right-click menu ----------------------------------------
    def build_menu(self, menu: tk.Menu) -> None:
        self._menu_vars = []
        playing = self.player.state == "playing"
        menu.add_command(label="Open video…", accelerator="Ctrl+O",
                         command=self.on_open)
        recent = tk.Menu(menu, tearoff=0, **_MENU)
        for path in [p for p in self.cfg.recent if os.path.isfile(p)][:RECENT_MAX]:
            recent.add_command(label=Path(path).name,
                               command=lambda p=path: self.open_path(p))
        menu.add_cascade(label="Open recent", menu=recent,
                         state="normal" if recent.index("end") is not None
                         else "disabled")
        menu.add_command(label="Next in folder", accelerator="Ctrl+N",
                         command=self.next_file)
        menu.add_separator()
        menu.add_command(label="Pause" if playing else "Play",
                         accelerator="Space", command=self.toggle_play)
        menu.add_command(label="Leave fullscreen" if self.fullscreen
                         else "Fullscreen", accelerator="F",
                         command=self.toggle_fullscreen)
        menu.add_separator()
        menu.add_cascade(label="Style", menu=self._radio_menu(
            menu, [(n, n) for n in PRESETS], None, self.apply_preset))
        menu.add_cascade(label="Palette", menu=self._radio_menu(
            menu, [(n, n.title()) for n in palette.names()],
            self.cfg.render.palette,
            lambda v: self._set_live("render", "palette", v, f"Palette: {v}")))
        menu.add_cascade(label="Speed", menu=self._radio_menu(
            menu, [(s, f"{s:g}x") for s in _SPEEDS], self.cfg.playback.speed,
            self.set_speed))
        menu.add_cascade(label="Loop", menu=self._radio_menu(
            menu, _LOOPS, self.cfg.playback.loop, self._on_loop))
        for label, field in (("Black backdrop", "background_blackout"),
                             ("Always on top", "topmost"),
                             ("Fullscreen spans every monitor", "fullscreen_all")):
            var = tk.BooleanVar(self, bool(getattr(self.cfg.render, field)))
            self._menu_vars.append(var)
            menu.add_checkbutton(
                label=label, variable=var,
                command=lambda f=field, v=var, l=label: self._set_live(
                    "render", f, bool(v.get()),
                    f"{l}: {'on' if v.get() else 'off'}"))
        menu.add_separator()
        menu.add_command(label="Settings…", command=self.open_settings)
        menu.add_command(label="Close video", command=self.close_video)
        menu.add_command(label="Quit TimeLeapPlayer", command=self._on_close)

    def _radio_menu(self, parent: tk.Menu, options: Sequence[tuple[Any, str]],
                    current: Any, command: Callable[[Any], None]) -> tk.Menu:
        sub = tk.Menu(parent, tearoff=0, **_MENU)
        var = tk.StringVar(self, str(current))
        self._menu_vars.append(var)
        for value, label in options:
            sub.add_radiobutton(label=label, value=str(value), variable=var,
                                command=lambda v=value: command(v))
        return sub

    # ---- refresh routing ---------------------------------------------
    def _apply_video(self) -> None:
        self._resolve_grid()
        self._show_grid()
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
            lambda: self._status("End of video", toast=True))

    def _player_ready(self, info: MediaInfo) -> None:
        """Runs on the render thread, before the frame source and renderer
        are built -- so shaping the window and the grid to the video here
        means the first frame is already drawn at the right size."""
        self._resolve_grid()
        region = self.cfg.render.region
        if region and not self.fullscreen and info.aspect > 0:
            self.cfg.render.region = fit_aspect(tuple(region), info.aspect)  # type: ignore[arg-type]
        self._post(lambda: self._show_media(info))

    def _show_media(self, info: MediaInfo) -> None:
        self._show_grid()
        if self.overlay.active and not self.fullscreen and self.cfg.render.region:
            rect = tuple(int(v) for v in self.cfg.render.region)
            self.overlay.place(rect)                                   # type: ignore[arg-type]
            self._windowed = rect                                      # type: ignore[assignment]
        self._status(f"Playing {Path(info.path).name}")

    def _on_player_state(self, state: str, message: str) -> None:
        if state == "error":
            self._status(message or "Playback error", error=True)
        elif message:
            self._status(message)

    def _post(self, fn: Callable[[], None]) -> None:
        """Marshal a callback onto the Tk thread.

        The deque is the contract; `_tick` drains it. The `after(0)` is only a
        latency shortcut and is taken *solely* when we are already on the Tk
        thread. Calling `after` from a foreign thread does not raise on this
        Tcl build -- it blocks the caller for a full second.
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
        win = self.settings
        if win is None or not win.winfo_viewable() or self.player.state == "loading":
            return
        stats = self.player.stats()
        self.stats.update_values({
            "fps": f"{stats.render_fps:5.1f}",
            "boxes": f"{stats.boxes:d}",
            "windows": f"{stats.windows:d}",
            "dropped": f"{stats.dropped:d}",
            "buffer": f"{stats.buffered:d}",
            "source": stats.source or "-",
        })

    def _status(self, text: str, error: bool = False, toast: bool = False) -> None:
        self._status_text = text
        if self.settings is not None:
            try:
                self.status.configure(
                    text=text, style="Error.TLabel" if error else "Status.TLabel")
            except tk.TclError:
                pass                 # status arriving during teardown
        if self.overlay.active and (toast or error):
            self.overlay.toast(text, 2.5 if error else 1.3)
        elif error:
            self.home_status.configure(text=text)
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
        self.bind("<Control-o>", lambda _e: self.on_open())
        self.bind("<Return>", lambda _e: self.on_open())
        self.bind("<Control-n>", lambda _e: self.next_file())
        self.bind("<Control-comma>", lambda _e: self.open_settings())

    _hotkey_note = ""

    def _start_hotkeys(self) -> None:
        """Global panic key. Optional: a failure here is only reported."""
        if not self.cfg.panic_hotkey:
            self._hotkey_note = "Global hotkeys are disabled in the config."
            return
        try:
            from .hotkeys import HotkeyManager
            manager = HotkeyManager(
                on_panic=lambda: self._post(self._on_panic),
                on_playpause=lambda: self._post(self.toggle_play),
                on_next=lambda: self._post(self.next_file))
            ok = bool(manager.start())
        except Exception as exc:
            self._hotkey_note = f"Global hotkeys failed: {exc}"
            return
        self._hotkeys = manager if ok else None
        self._hotkey_note = ("Global hotkeys registered." if ok else
                             "Global hotkeys refused (another app owns them).")

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
        if self.fullscreen and self._windowed:
            self.cfg.render.region = self._windowed
        try:
            self.overlay.destroy()
        except Exception:
            pass
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
            if self.winfo_viewable():
                self.ui_state.geometry = self.geometry()
            if self.settings is not None and self.settings.winfo_exists():
                self.ui_state.settings_geometry = self.settings.geometry()
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
