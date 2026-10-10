"""The player window: hover over the video and the controls appear.

The pixel windows are click-through, so the picture itself cannot be grabbed.
Two borderless Tk windows sit over the video's rectangle instead:

* the *surface*, at 1% alpha. Invisible, but a layered window with any alpha
  is hit-tested, so it receives every click, drag, double-click and wheel turn
  over the picture: drag to move, edges to resize, click to pause,
  double-click for fullscreen, wheel for volume, right-click for the menu.
* the *chrome*, colour-keyed. The key colour is transparent *and* passes
  clicks through to the surface, so only the drawn controls are solid. It
  fades in on hover and out when the mouse goes idle, like any media player.

The geometry helpers at the top are pure, so they are unit-tested.
"""
from __future__ import annotations

import time
import tkinter as tk
import tkinter.font as tkfont
from typing import Any

from PIL import ImageTk

from ..render import win32 as w
from .widgets import format_time, px, rounded_image

Rect = tuple[int, int, int, int]

KEY = "#010203"          # colour-keyed: see-through and click-through
PANEL = "#15171d"
PANEL_EDGE = "#2b303b"
HOVER = "#2c313c"
TRACK = "#3b4251"
ACCENT = "#4cc2ff"
TEXT = "#eef4ff"
MUTED = "#9aa4b5"
DANGER = "#e5484d"
BORDER = "#3b4456"

MDL2 = {"play": "", "pause": "", "close": "", "full": "",
        "unfull": "", "gear": "", "vol": "", "mute": ""}
PLAIN = {"play": "▶", "pause": "❚❚", "close": "✕",
         "full": "⛶", "unfull": "⛶", "gear": "⚙", "vol": "♪",
         "mute": "✕"}

CHROME_ALPHA = 0.95
SURFACE_ALPHA = 0.01     # invisible, yet still hit-tested
POLL_MS = 60
IDLE_S = 2.5             # controls hide after this long without movement
LEAVE_S = 0.45           # ...or this long after the pointer leaves
CLICK_MS = 240           # a click waits this long in case it becomes a double
SEEK_EVERY_S = 0.12      # live-scrub throttle; each streaming seek restarts ffmpeg
MIN_W = 240              # logical px
EDGE = 8                 # logical px of resize grip along each border


# ---- geometry --------------------------------------------------------
def fit_aspect(rect: Rect, aspect: float) -> Rect:
    """Same width and centre, height from the aspect ratio."""
    x, y, width, height = rect
    if aspect <= 0:
        return rect
    new_h = max(1, round(width / aspect))
    return (x, y + (height - new_h) // 2, width, new_h)


def resize_rect(start: Rect, spec: str, dx: int, dy: int, aspect: float,
                min_w: int) -> Rect:
    """Resize from an edge or corner, keeping the aspect ratio.

    The opposite edge stays put; dragging a side edge keeps the window
    centred on the other axis, the way video players resize.
    """
    sx, sy, sw, sh = start
    aspect = aspect if aspect > 0 else sw / max(1, sh)
    if "e" in spec:
        new_w = sw + dx
    elif "w" in spec:
        new_w = sw - dx
    elif "s" in spec:
        new_w = (sh + dy) * aspect
    else:
        new_w = (sh - dy) * aspect
    new_w = max(min_w, int(round(new_w)))
    new_h = max(1, int(round(new_w / aspect)))
    x = sx + sw - new_w if "w" in spec else sx if "e" in spec else sx + (sw - new_w) // 2
    y = sy + sh - new_h if "n" in spec else sy if "s" in spec else sy + (sh - new_h) // 2
    return (x, y, new_w, new_h)


def default_rect(area: Rect, aspect: float, frac: float = 0.5) -> Rect:
    """A window `frac` of the area's width, centred, at the given aspect."""
    ax, ay, aw, ah = area
    aspect = aspect if aspect > 0 else 16 / 9
    width = int(aw * frac)
    height = int(round(width / aspect))
    if height > ah * 0.8:
        height = int(ah * 0.8)
        width = int(round(height * aspect))
    return (ax + (aw - width) // 2, ay + (ah - height) // 2, width, height)


def keep_visible(rect: Rect, bounds: Rect, margin: int = 64) -> Rect:
    """Allow a window partly off screen, but never lose its top edge or all of it."""
    x, y, width, height = rect
    bx, by, bw, bh = bounds
    x = min(max(x, bx - width + margin), bx + bw - margin)
    y = min(max(y, by), by + bh - margin)
    return (x, y, width, height)


def edge_at(lx: int, ly: int, width: int, height: int, grip: int) -> str:
    """Which resize edge(s) a local point is on: '', 'n', 'se', ..."""
    spec = ("n" if ly < grip else "s" if ly >= height - grip else "")
    spec += ("w" if lx < grip else "e" if lx >= width - grip else "")
    return spec


def geometry(rect: Rect) -> str:
    """Tk geometry. Negative coordinates must be spelled `+-N`; `-N` would
    mean "N pixels from the right edge"."""
    x, y, width, height = rect
    return f"{max(1, width)}x{max(1, height)}+{x}+{y}"


_CURSORS = {"n": "size_ns", "s": "size_ns", "w": "size_we", "e": "size_we",
            "nw": "size_nw_se", "se": "size_nw_se", "ne": "size_ne_sw",
            "sw": "size_ne_sw"}


# ---- the window ------------------------------------------------------
class PlayerOverlay:
    """Hover controls and direct manipulation for the playing video.

    Talks to the app directly (`toggle_play`, `set_window_rect`, ...): there
    is exactly one controller, so an interface would only add indirection.
    """

    def __init__(self, app: Any) -> None:
        self.app = app
        self.rect: Rect = (0, 0, 1, 1)
        self.active = False
        self._shown = False
        self._alpha = 0.0
        self._target = 0.0
        self._anim: str | None = None
        self._poll_job: str | None = None
        self._polls = 0
        self._drag: dict | None = None
        self._ctl: str | None = None
        self._hover_ctl: str | None = None
        self._scrub_frac: float | None = None
        self._last_seek = 0.0
        self._click_job: str | None = None
        self._menu_open = False
        self._last_ptr: tuple[int, int] | None = None
        self._last_move = 0.0
        self._outside_since: float | None = None
        self._toast: tuple[str, float] | None = None
        self._title = ""
        self._size: tuple[int, int] | None = None
        self._imgs: list[ImageTk.PhotoImage] = []
        self._geo: dict[str, Any] = {}

        families = set(tkfont.families(app))
        self.glyph = MDL2 if "Segoe MDL2 Assets" in families else PLAIN
        icon_family = "Segoe MDL2 Assets" if self.glyph is MDL2 else "Segoe UI Symbol"
        self.f_title = tkfont.Font(app, family="Segoe UI Semibold", size=10)
        self.f_time = tkfont.Font(app, family="Segoe UI", size=9)
        self.f_toast = tkfont.Font(app, family="Segoe UI Semibold", size=11)
        self.f_icon = tkfont.Font(app, family=icon_family, size=11)
        self.f_play = tkfont.Font(app, family=icon_family, size=14)

        self.surface = self._toplevel(SURFACE_ALPHA, "#000000")
        self.chrome = self._toplevel(0.0, KEY, key=KEY)
        self.canvas = tk.Canvas(self.chrome, bg=KEY, highlightthickness=0, bd=0)
        self.canvas.pack(fill="both", expand=True)
        self.surface.update_idletasks()
        try:
            w.show_in_taskbar(int(self.surface.wm_frame(), 16))
        except Exception:
            pass

        for widget in (self.surface, self.canvas):
            widget.bind("<ButtonPress-1>", self._press)
            widget.bind("<B1-Motion>", self._motion)
            widget.bind("<ButtonRelease-1>", self._release)
            widget.bind("<Double-Button-1>", self._double)
            widget.bind("<MouseWheel>", self._wheel)
            widget.bind("<ButtonPress-3>", self._menu)
        self.surface.bind("<Motion>", self._surface_hover)
        self.canvas.bind("<Motion>", self._canvas_hover)
        self.canvas.bind("<Leave>", lambda _e: self._set_hover(None))
        for top in (self.surface, self.chrome):
            self._bind_keys(top)

    def _toplevel(self, alpha: float, bg: str, key: str | None = None) -> tk.Toplevel:
        top = tk.Toplevel(self.app)
        top.withdraw()
        top.overrideredirect(True)
        top.configure(bg=bg)
        top.attributes("-topmost", True)
        if key:
            top.attributes("-transparentcolor", key)
        top.attributes("-alpha", alpha)
        return top

    # ---- lifecycle ---------------------------------------------------
    def show(self, rect: Rect, title: str) -> None:
        self.active = True
        self._title = title
        self.surface.title(f"{title} - TimeLeapPlayer" if title else "TimeLeapPlayer")
        self.place(rect)
        self.surface.deiconify()
        self.surface.attributes("-topmost", bool(self.app.cfg.render.topmost))
        self._last_move = time.monotonic()
        if self._poll_job is None:
            self._poll()

    def hide(self) -> None:
        self.active = False
        self._drag = self._ctl = self._scrub_frac = None
        self._toast = None
        self._cancel_click()
        for job in (self._poll_job, self._anim):
            if job:
                try:
                    self.surface.after_cancel(job)
                except tk.TclError:
                    pass
        self._poll_job = self._anim = None
        self._shown = False
        self._alpha = self._target = 0.0
        for top in (self.chrome, self.surface):
            try:
                top.attributes("-alpha", 0.0 if top is self.chrome else SURFACE_ALPHA)
                top.withdraw()
            except tk.TclError:
                pass

    def destroy(self) -> None:
        self.hide()
        for top in (self.chrome, self.surface):
            try:
                top.destroy()
            except tk.TclError:
                pass

    def place(self, rect: Rect) -> None:
        rect = tuple(int(v) for v in rect)                 # type: ignore[assignment]
        if rect == self.rect and self._size is not None:
            return
        self.rect = rect
        geo = geometry(self.rect)
        self.surface.geometry(geo)
        self.chrome.geometry(geo)
        if (self.rect[2], self.rect[3]) != self._size:
            self._layout()

    def set_title(self, title: str) -> None:
        self._title = title
        self.surface.title(f"{title} - TimeLeapPlayer")
        self._layout()

    def toast(self, text: str, seconds: float = 1.3) -> None:
        """Briefly show `text` over the video (volume, speed, preset...)."""
        if not self.active:
            return
        self._toast = (text, time.monotonic() + seconds)
        self._draw_toast()
        self._fade_to(CHROME_ALPHA)

    # ---- hover / fade ------------------------------------------------
    def _poll(self) -> None:
        self._poll_job = None
        if not self.active:
            return
        now = time.monotonic()
        try:
            ptr = self.surface.winfo_pointerxy()
        except tk.TclError:
            return
        if ptr != self._last_ptr:
            self._last_ptr, self._last_move = ptr, now
        rx, ry, rw, rh = self.rect
        inside = rx <= ptr[0] < rx + rw and ry <= ptr[1] < ry + rh
        busy = (self._drag is not None or self._ctl is not None or self._menu_open)
        if inside or busy:
            self._outside_since = None
        elif self._outside_since is None:
            self._outside_since = now
        near = self._outside_since is None or now - self._outside_since < LEAVE_S
        idle = now - self._last_move > IDLE_S
        paused = self.app.player.state != "playing"
        self._set_controls(busy or (near and (paused or not idle)))

        # Fullscreen hides the pointer once it goes idle, like any player.
        hide_cursor = self.app.fullscreen and inside and idle and not paused
        cursor = self.surface.cget("cursor")
        if hide_cursor and cursor != "none":
            self.surface.configure(cursor="none")
        elif not hide_cursor and cursor == "none":
            self.surface.configure(cursor="")

        if self._toast is not None and now >= self._toast[1]:
            self._toast = None
            self.canvas.delete("toast")
            if not self._shown:
                self._fade_to(0.0)
        if self._alpha > 0:
            self._refresh()
            # New pixel windows are created at the top of the top-most band;
            # without re-asserting every poll, the controls sink behind them.
            self.chrome.lift()
            self.chrome.attributes("-topmost", bool(self.app.cfg.render.topmost))
        self._poll_job = self.surface.after(POLL_MS, self._poll)

    def _set_controls(self, show: bool) -> None:
        if show == self._shown:
            return
        self._shown = show
        if show:
            self.canvas.itemconfigure("ctl", state="normal")
            self._refresh()
            self._fade_to(CHROME_ALPHA)
        elif self._toast is not None:
            self.canvas.itemconfigure("ctl", state="hidden")
        else:
            self._fade_to(0.0)

    def _fade_to(self, target: float) -> None:
        self._target = target
        if self._anim is None:
            self._anim = self.chrome.after(0, self._fade_step)

    def _fade_step(self) -> None:
        self._anim = None
        if not self.active:
            return
        rising = self._target > self._alpha
        step = 0.24 if rising else 0.16
        alpha = (min(self._target, self._alpha + step) if rising
                 else max(self._target, self._alpha - step))
        if self._alpha == 0.0 and alpha > 0.0:
            self.chrome.attributes("-alpha", 0.0)
            self.chrome.deiconify()
            self.chrome.lift()
            self.chrome.attributes("-topmost", True)
        self._alpha = alpha
        self.chrome.attributes("-alpha", alpha)
        if alpha == 0.0:
            self.chrome.withdraw()
            if not self._shown:
                self.canvas.itemconfigure("ctl", state="normal")
        if alpha != self._target:
            self._anim = self.chrome.after(16, self._fade_step)

    # ---- drawing -----------------------------------------------------
    def _image(self, width: int, height: int, radius: float, fill: str,
               bg: str, outline: str | None = None) -> ImageTk.PhotoImage:
        img = ImageTk.PhotoImage(rounded_image(width, height, radius, fill, bg,
                                               outline), master=self.chrome)
        self._imgs.append(img)
        return img

    def _layout(self) -> None:
        """Rebuild every item for the current size. Runs on resize only."""
        c = self.canvas
        c.delete("all")
        self._imgs.clear()
        width, height = self.rect[2], self.rect[3]
        self._size = (width, height)
        c.configure(width=width, height=height)
        state = "normal" if self._shown or self._alpha == 0 else "hidden"
        pad, band = px(12), px(40)
        g: dict[str, Any] = {}

        c.create_rectangle(0, 0, width - 1, height - 1, outline=BORDER,
                           tags=("ctl",), state=state)

        # top right: settings, fullscreen, close
        slot = px(38)
        gw = 3 * slot + px(8)
        gx0 = width - pad - gw
        c.create_image(gx0, pad, anchor="nw", tags=("ctl",), state=state,
                       image=self._image(gw, band, band / 2, PANEL, KEY, PANEL_EDGE))
        disc = px(32)
        for i, (name, color) in enumerate((("gear", HOVER), ("full", HOVER),
                                           ("close", DANGER))):
            cx, cy = gx0 + px(4) + slot // 2 + i * slot, pad + band // 2
            self._button(name, cx, cy, slot, band, disc, color, self.f_icon, state)

        # top left: title, which doubles as a drag handle
        room = gx0 - pad - px(10) - px(28)
        if room > px(60) and self._title:
            text = self._elide(self._title, self.f_title, room)
            tw = self.f_title.measure(text) + px(28)
            c.create_image(pad, pad, anchor="nw", tags=("ctl",), state=state,
                           image=self._image(tw, band, band / 2, PANEL, KEY, PANEL_EDGE))
            c.create_text(pad + px(14), pad + band // 2, anchor="w", text=text,
                          fill=TEXT, font=self.f_title, tags=("ctl",), state=state)

        # bottom bar: play, time, seek, duration, volume
        bar = px(56)
        bx0, bx1, by0 = pad, width - pad, height - pad - bar
        cy = by0 + bar // 2
        c.create_image(bx0, by0, anchor="nw", tags=("ctl",), state=state,
                       image=self._image(bx1 - bx0, bar, px(18), PANEL, KEY, PANEL_EDGE))
        self._button("play", bx0 + px(32), cy, px(46), bar, px(42), HOVER,
                     self.f_play, state)

        vol_track = (bx1 - bx0) >= px(440)
        if vol_track:
            vx1 = bx1 - px(22)
            vx0 = vx1 - px(76)
            vic = vx0 - px(22)
        else:
            vx0 = vx1 = 0
            vic = bx1 - px(28)
        self._button("vol", vic, cy, px(38), bar, px(34), HOVER, self.f_icon, state)

        time_w = self.f_time.measure("0:00:00")
        tx = bx0 + px(62)
        dx = vic - px(24)
        sx0, sx1 = tx + time_w + px(12), dx - time_w - px(12)
        show_times = sx1 - sx0 >= px(60)
        if not show_times:
            sx0, sx1 = bx0 + px(62), vic - px(24)
        else:
            c.create_text(tx, cy, anchor="w", text="0:00", fill=TEXT,
                          font=self.f_time, tags=("ctl", "time"), state=state)
            c.create_text(dx, cy, anchor="e", text="0:00", fill=MUTED,
                          font=self.f_time, tags=("ctl", "dur"), state=state)
        thick = max(2, px(4))
        if sx1 - sx0 > px(20):
            c.create_rectangle(sx0 - px(8), cy - px(14), sx1 + px(8), cy + px(14),
                               fill=PANEL, outline="", tags=("ctl", "hit:seek"),
                               state=state)
            c.create_line(sx0, cy, sx1, cy, fill=TRACK, width=thick,
                          capstyle="round", tags=("ctl",), state=state)
            c.create_line(sx0, cy, sx0, cy, fill=ACCENT, width=thick,
                          capstyle="round", tags=("ctl", "played"), state=state)
            knob = px(14)
            c.create_image(sx0, cy, tags=("ctl", "knob"), state=state,
                           image=self._image(knob, knob, knob / 2, ACCENT, PANEL))
            g["seek"] = (sx0, sx1)
        if vol_track:
            c.create_rectangle(vx0 - px(6), cy - px(12), vx1 + px(6), cy + px(12),
                               fill=PANEL, outline="", tags=("ctl", "hit:volbar"),
                               state=state)
            c.create_line(vx0, cy, vx1, cy, fill=TRACK, width=thick,
                          capstyle="round", tags=("ctl",), state=state)
            c.create_line(vx0, cy, vx0, cy, fill=TEXT, width=thick,
                          capstyle="round", tags=("ctl", "volfill"), state=state)
            g["vol"] = (vx0, vx1)
        g["cy"] = cy
        g["bar_top"] = by0
        self._geo = g
        self._hover_ctl = None
        self._draw_toast()
        self._refresh()

    def _button(self, name: str, cx: int, cy: int, hit_w: int, hit_h: int,
                disc: int, color: str, font: tkfont.Font, state: str) -> None:
        c = self.canvas
        c.create_rectangle(cx - hit_w // 2, cy - hit_h // 2, cx + hit_w // 2,
                           cy + hit_h // 2, fill=PANEL, outline="",
                           tags=("ctl", f"hit:{name}"), state=state)
        c.create_image(cx, cy, tags=(f"hov:{name}",), state="hidden",
                       image=self._image(disc, disc, disc / 2, color, PANEL))
        c.create_text(cx, cy, text=self.glyph[name], fill=TEXT, font=font,
                      tags=("ctl", f"icon:{name}"), state=state)

    @staticmethod
    def _elide(text: str, font: tkfont.Font, room: int) -> str:
        if font.measure(text) <= room:
            return text
        while text and font.measure(text + "…") > room:
            text = text[:-1]
        return text + "…"

    def _draw_toast(self) -> None:
        c = self.canvas
        c.delete("toast")
        if self._toast is None:
            return
        text = self._toast[0]
        tw, th = self.f_toast.measure(text) + px(36), px(44)
        x = (self.rect[2] - tw) // 2
        y = max(px(64), int(self.rect[3] * 0.16))
        c.create_image(x, y, anchor="nw", tags=("toast",),
                       image=self._image(tw, th, th / 2, PANEL, KEY, PANEL_EDGE))
        c.create_text(x + tw // 2, y + th // 2, text=text, fill=TEXT,
                      font=self.f_toast, tags=("toast",))

    def _refresh(self) -> None:
        """Update the moving parts from the player. Cheap; runs every poll."""
        c, g = self.canvas, self._geo
        player = self.app.player
        stats = player.stats()
        playing = player.state == "playing"
        c.itemconfigure("icon:play", text=self.glyph["pause" if playing else "play"])
        c.itemconfigure("icon:full",
                        text=self.glyph["unfull" if self.app.fullscreen else "full"])
        volume = self.app.cfg.playback.volume
        muted = self.app.cfg.playback.mute or volume <= 0
        c.itemconfigure("icon:vol", text=self.glyph["mute" if muted else "vol"])

        loading = player.state == "loading"
        duration = 0.0 if loading else stats.duration
        if self._scrub_frac is not None:
            frac = self._scrub_frac
            position = frac * duration
        elif loading:
            frac, position = 0.0, 0.0
        else:
            frac = stats.frame / max(1, stats.total_frames)
            position = stats.position
        c.itemconfigure("time", text=format_time(position))
        c.itemconfigure("dur", text=format_time(duration))
        cy = g.get("cy", 0)
        if "seek" in g:
            sx0, sx1 = g["seek"]
            x = sx0 + (sx1 - sx0) * max(0.0, min(1.0, frac))
            c.coords("played", sx0, cy, x, cy)
            c.coords("knob", x, cy)
        if "vol" in g:
            vx0, vx1 = g["vol"]
            v = 0.0 if self.app.cfg.playback.mute else volume / 100
            c.coords("volfill", vx0, cy, vx0 + (vx1 - vx0) * v, cy)

    # ---- input: the picture ------------------------------------------
    def _local(self, event: tk.Event) -> tuple[int, int]:
        return event.x_root - self.rect[0], event.y_root - self.rect[1]

    def _pointer(self) -> tuple[int, int]:
        """The real cursor position, straight from the OS.

        Not `event.x_root`: Tk derives that from its cached idea of where the
        window is, which is stale the moment the window moves under the
        cursor. Dragging with it feeds each move a wrong delta, the move makes
        Windows send another mouse-move, and the window chases its own tail.
        """
        return self.surface.winfo_pointerxy()

    def _press(self, event: tk.Event) -> None:
        self.surface.focus_force()
        self._last_move = time.monotonic()
        if event.widget is self.canvas:
            ctl = self._control_at(event)
            if ctl:
                self._ctl_press(ctl, event)
                return
        x, y = self._pointer()
        mode = ("" if self.app.fullscreen else
                edge_at(x - self.rect[0], y - self.rect[1], self.rect[2],
                        self.rect[3], px(EDGE)))
        self._drag = {"mode": mode or "move", "x": x, "y": y,
                      "rect": self.rect, "moved": False}

    def _motion(self, event: tk.Event) -> None:
        self._last_move = time.monotonic()
        if self._ctl is not None:
            self._ctl_drag(event)
            return
        d = self._drag
        if d is None:
            return
        x, y = self._pointer()
        dx, dy = x - d["x"], y - d["y"]
        if not d["moved"] and abs(dx) + abs(dy) < px(4):
            return
        d["moved"] = True
        if self.app.fullscreen:
            return
        sx, sy, sw, sh = d["rect"]
        if d["mode"] == "move":
            rect = (sx + dx, sy + dy, sw, sh)
        else:
            rect = resize_rect(d["rect"], d["mode"], dx, dy, self.app.aspect(),
                               px(MIN_W))
        self.app.set_window_rect(rect)

    def _release(self, event: tk.Event) -> None:
        if self._ctl is not None:
            self._ctl_release(event)
            return
        d, self._drag = self._drag, None
        if d is None:
            return
        if d["moved"]:
            if not self.app.fullscreen:
                self.app.set_window_rect(self.rect, final=True)
            return
        if d["mode"] == "move":
            # A plain click on the picture is play/pause -- after a short
            # wait, so a double-click can claim it for fullscreen instead.
            self._cancel_click()
            self._click_job = self.surface.after(CLICK_MS, self._click)

    def _click(self) -> None:
        self._click_job = None
        self.app.toggle_play()

    def _cancel_click(self) -> None:
        if self._click_job:
            try:
                self.surface.after_cancel(self._click_job)
            except tk.TclError:
                pass
            self._click_job = None

    def _double(self, event: tk.Event) -> None:
        if event.widget is self.canvas and self._control_at(event):
            return
        self._cancel_click()
        self._drag = None
        self.app.toggle_fullscreen()

    def _wheel(self, event: tk.Event) -> None:
        step = 5 if event.delta > 0 else -5
        self.app.set_volume(self.app.cfg.playback.volume + step, toast=True)

    def _menu(self, event: tk.Event) -> None:
        menu = tk.Menu(self.surface, tearoff=0, bg=PANEL, fg=TEXT,
                       activebackground=HOVER, activeforeground=TEXT, bd=0)
        self.app.build_menu(menu)
        self._menu_open = True
        try:
            menu.tk_popup(event.x_root, event.y_root)
        finally:
            menu.grab_release()
            self._menu_open = False

    def _surface_hover(self, event: tk.Event) -> None:
        if self._drag is not None or self.app.fullscreen:
            return
        lx, ly = self._local(event)
        spec = edge_at(lx, ly, self.rect[2], self.rect[3], px(EDGE))
        cursor = _CURSORS.get(spec, "")
        if self.surface.cget("cursor") != cursor:
            self.surface.configure(cursor=cursor)

    # ---- input: the controls -----------------------------------------
    def _control_at(self, event: tk.Event) -> str | None:
        if not self._shown:
            return None
        for item in reversed(self.canvas.find_overlapping(event.x, event.y,
                                                          event.x, event.y)):
            for tag in self.canvas.gettags(item):
                if tag.startswith("hit:"):
                    return tag[4:]
        return None

    def _set_hover(self, name: str | None) -> None:
        if name == self._hover_ctl:
            return
        if self._hover_ctl:
            self.canvas.itemconfigure(f"hov:{self._hover_ctl}", state="hidden")
        self._hover_ctl = name
        if name and self._shown and self.canvas.find_withtag(f"hov:{name}"):
            self.canvas.itemconfigure(f"hov:{name}", state="normal")
            self.canvas.tag_lower(f"hov:{name}", f"icon:{name}")
        self.canvas.configure(cursor="hand2" if name else "")
        if name != "seek":
            self.canvas.delete("tip")

    def _canvas_hover(self, event: tk.Event) -> None:
        self._last_move = time.monotonic()
        name = self._control_at(event)
        self._set_hover(name)
        if name == "seek":
            self._draw_tip(self._frac_at(event, "seek"))

    def _draw_tip(self, frac: float) -> None:
        """Time under the pointer, floating above the seek bar."""
        c = self.canvas
        c.delete("tip")
        if "seek" not in self._geo:
            return
        duration = self.app.player.stats().duration
        text = format_time(frac * duration)
        sx0, sx1 = self._geo["seek"]
        tw, th = self.f_time.measure(text) + px(20), px(28)
        x = int(sx0 + (sx1 - sx0) * frac) - tw // 2
        y = self._geo["bar_top"] - th - px(8)
        c.create_image(x, y, anchor="nw", tags=("tip",),
                       image=self._image(tw, th, th / 2, PANEL, KEY, PANEL_EDGE))
        c.create_text(x + tw // 2, y + th // 2, text=text, fill=TEXT,
                      font=self.f_time, tags=("tip",))

    def _frac_at(self, event: tk.Event, which: str) -> float:
        x0, x1 = self._geo[which]
        return max(0.0, min(1.0, (event.x - x0) / max(1, x1 - x0)))

    def _ctl_press(self, ctl: str, event: tk.Event) -> None:
        self._ctl = ctl
        if ctl == "seek":
            self.app.begin_scrub()
            self._scrub(event, final=False)
        elif ctl == "volbar":
            self.app.set_volume(round(self._frac_at(event, "vol") * 100))

    def _ctl_drag(self, event: tk.Event) -> None:
        if self._ctl == "seek":
            self._scrub(event, final=False)
            self._draw_tip(self._scrub_frac or 0.0)
        elif self._ctl == "volbar":
            self.app.set_volume(round(self._frac_at(event, "vol") * 100))

    def _ctl_release(self, event: tk.Event) -> None:
        ctl, self._ctl = self._ctl, None
        if ctl == "seek":
            self._scrub(event, final=True)
            self._scrub_frac = None
            self.canvas.delete("tip")
            return
        if ctl == "volbar" or self._control_at(event) != ctl:
            return                       # released off the button: no action
        {"play": self.app.toggle_play, "gear": self.app.open_settings,
         "full": self.app.toggle_fullscreen, "close": self.app.close_video,
         "vol": self.app.toggle_mute}[ctl]()

    def _scrub(self, event: tk.Event, final: bool) -> None:
        if "seek" not in self._geo:
            return
        self._scrub_frac = self._frac_at(event, "seek")
        now = time.monotonic()
        if final or now - self._last_seek >= SEEK_EVERY_S:
            self._last_seek = now
            self.app.scrub_to(self._scrub_frac, final)
        self._refresh()

    # ---- keys --------------------------------------------------------
    def _bind_keys(self, top: tk.Misc) -> None:
        app = self.app

        def vol(step: int) -> None:
            app.set_volume(app.cfg.playback.volume + step, toast=True)

        def escape() -> None:
            if app.fullscreen:
                app.toggle_fullscreen()

        for key, fn in (("<space>", app.toggle_play), ("<k>", app.toggle_play),
                        ("<Left>", lambda: app.seek_by(-5.0)),
                        ("<Right>", lambda: app.seek_by(5.0)),
                        ("<Up>", lambda: vol(5)), ("<Down>", lambda: vol(-5)),
                        ("<f>", app.toggle_fullscreen), ("<Escape>", escape),
                        ("<m>", app.toggle_mute), ("<Control-o>", app.on_open),
                        ("<Control-n>", app.next_file)):
            top.bind(key, lambda _e, f=fn: f())
