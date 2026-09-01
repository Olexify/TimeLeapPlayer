"""Reusable Tk pieces for the control panel.

Presentation only: nothing here imports `Player` or mutates `AppConfig`, so
`app.py` stays readable as layout plus handlers. The three things worth
factoring out are the dark theme (ttk needs `clam` and about forty
`style.configure` calls before it stops looking like Windows 95), the seek
bar's drag protocol, and slider debouncing -- each of which would otherwise
be duplicated in a dozen places.
"""
from __future__ import annotations

import tkinter as tk
from collections.abc import Callable, Sequence
from tkinter import ttk
from typing import Any

from ..core import palette

# ---- palette ---------------------------------------------------------
COLORS: dict[str, str] = {
    "bg": "#14161b",
    "panel": "#1b1e25",
    "raised": "#242933",
    "hover": "#2f3745",
    "field": "#0f1217",
    "line": "#333b49",
    "fg": "#e3e7ef",
    "muted": "#8d96a8",
    "accent": "#4cc2ff",
    "accent_dim": "#1d5f80",
    "danger": "#e5484d",
    "danger_dim": "#7d2226",
    "ok": "#48d597",
    "warn": "#f0b429",
}

UI = ("Segoe UI", 9)
UI_BOLD = ("Segoe UI", 9, "bold")
UI_HEAD = ("Segoe UI", 11, "bold")
UI_STOP = ("Segoe UI", 12, "bold")
UI_CAP = ("Segoe UI", 7, "bold")
MONO = ("Consolas", 9)


def apply_theme(root: tk.Misc) -> ttk.Style:
    """Dark ttk theme. Returns the Style so callers can extend it.

    Only `clam` exposes its element colours to `configure`; the stock Windows
    themes draw through the native renderer and silently ignore background.
    """
    style = ttk.Style(root)
    try:
        style.theme_use("clam")
    except tk.TclError:                      # pragma: no cover - exotic Tk build
        pass
    c = COLORS
    root.configure(bg=c["bg"])

    style.configure(".", background=c["bg"], foreground=c["fg"], font=UI,
                    bordercolor=c["line"], darkcolor=c["panel"],
                    lightcolor=c["panel"], troughcolor=c["field"],
                    fieldbackground=c["field"], focuscolor=c["accent"],
                    insertcolor=c["fg"])

    for name, bg in (("TL.TFrame", c["bg"]), ("Panel.TFrame", c["panel"]),
                     ("Card.TFrame", c["raised"]), ("Bar.TFrame", c["panel"])):
        style.configure(name, background=bg)

    style.configure("TLabel", background=c["bg"], foreground=c["fg"])
    style.configure("Field.TLabel", background=c["bg"], foreground=c["muted"])
    style.configure("Hint.TLabel", background=c["bg"], foreground=c["muted"],
                    font=("Segoe UI", 8))
    style.configure("Head.TLabel", background=c["bg"], foreground=c["fg"],
                    font=UI_HEAD)
    style.configure("Value.TLabel", background=c["bg"], foreground=c["accent"],
                    font=MONO)
    style.configure("Cap.TLabel", background=c["raised"], foreground=c["muted"],
                    font=UI_CAP)
    style.configure("Stat.TLabel", background=c["raised"], foreground=c["fg"],
                    font=MONO)
    style.configure("Status.TLabel", background=c["panel"], foreground=c["muted"])
    style.configure("Error.TLabel", background=c["panel"], foreground=c["danger"])
    style.configure("Ok.TLabel", background=c["panel"], foreground=c["ok"])
    style.configure("Time.TLabel", background=c["bg"], foreground=c["fg"],
                    font=("Consolas", 11))

    style.configure("TButton", background=c["raised"], foreground=c["fg"],
                    bordercolor=c["line"], focusthickness=0, padding=(10, 5),
                    relief="flat")
    style.map("TButton",
              background=[("pressed", c["accent_dim"]), ("active", c["hover"]),
                          ("disabled", c["panel"])],
              foreground=[("disabled", c["muted"])])
    style.configure("Accent.TButton", background=c["accent_dim"],
                    foreground=c["fg"])
    style.map("Accent.TButton", background=[("active", c["accent"]),
                                            ("pressed", c["accent_dim"])])
    style.configure("Stop.TButton", background=c["danger_dim"],
                    foreground="#ffe9ea", font=UI_STOP, padding=(18, 9))
    style.map("Stop.TButton", background=[("active", c["danger"]),
                                          ("pressed", c["danger_dim"])])
    style.configure("Transport.TButton", padding=(12, 6), font=UI_BOLD)

    style.configure("TCheckbutton", background=c["bg"], foreground=c["fg"],
                    indicatorcolor=c["field"], focusthickness=0)
    style.map("TCheckbutton",
              background=[("active", c["bg"])],
              indicatorcolor=[("selected", c["accent"]),
                              ("disabled", c["panel"])],
              foreground=[("disabled", c["muted"])])

    style.configure("TCombobox", arrowcolor=c["fg"], padding=3)
    style.map("TCombobox",
              fieldbackground=[("readonly", c["field"]), ("disabled", c["panel"])],
              background=[("readonly", c["raised"])],
              foreground=[("readonly", c["fg"]), ("disabled", c["muted"])],
              selectbackground=[("readonly", c["field"])],
              selectforeground=[("readonly", c["fg"])])
    # The dropdown is a plain Tk listbox and is only reachable via the option db.
    root.option_add("*TCombobox*Listbox.background", c["field"])
    root.option_add("*TCombobox*Listbox.foreground", c["fg"])
    root.option_add("*TCombobox*Listbox.selectBackground", c["accent_dim"])
    root.option_add("*TCombobox*Listbox.selectForeground", c["fg"])

    style.configure("Horizontal.TScale", background=c["bg"],
                    troughcolor=c["field"], bordercolor=c["line"],
                    lightcolor=c["accent"], darkcolor=c["accent_dim"])
    style.configure("Horizontal.TProgressbar", background=c["accent"],
                    troughcolor=c["field"], bordercolor=c["line"],
                    lightcolor=c["accent"], darkcolor=c["accent_dim"])

    style.configure("TNotebook", background=c["bg"], bordercolor=c["line"],
                    tabmargins=(4, 4, 4, 0))
    style.configure("TNotebook.Tab", background=c["panel"], foreground=c["muted"],
                    padding=(14, 6), bordercolor=c["line"])
    style.map("TNotebook.Tab",
              background=[("selected", c["raised"]), ("active", c["hover"])],
              foreground=[("selected", c["fg"])])

    style.configure("TLabelframe", background=c["bg"], bordercolor=c["line"],
                    relief="solid", borderwidth=1)
    style.configure("TLabelframe.Label", background=c["bg"],
                    foreground=c["accent"], font=UI_BOLD)

    style.configure("Treeview", background=c["field"], fieldbackground=c["field"],
                    foreground=c["fg"], bordercolor=c["line"], rowheight=20)
    style.configure("Treeview.Heading", background=c["raised"],
                    foreground=c["muted"], relief="flat", font=UI_BOLD)
    style.map("Treeview.Heading", background=[("active", c["hover"])])
    style.map("Treeview", background=[("selected", c["accent_dim"])],
              foreground=[("selected", c["fg"])])

    style.configure("TSeparator", background=c["line"])
    style.configure("Vertical.TScrollbar", background=c["raised"],
                    troughcolor=c["field"], bordercolor=c["line"],
                    arrowcolor=c["muted"])
    return style


# ---- formatting ------------------------------------------------------
def format_time(seconds: float) -> str:
    """m:ss, growing to h:mm:ss -- the seek bar is read at a glance."""
    try:
        value = float(seconds)
    except (TypeError, ValueError):
        value = 0.0
    if value != value or value < 0:          # NaN or negative
        value = 0.0
    total = int(value)
    hours, rest = divmod(total, 3600)
    minutes, secs = divmod(rest, 60)
    return f"{hours}:{minutes:02d}:{secs:02d}" if hours else f"{minutes}:{secs:02d}"


def format_bytes(count: float) -> str:
    size = float(count)
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024.0:
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024.0
    return f"{size:.1f} TB"


def colorref_hex(colorref: int) -> str:
    """COLORREF (0x00BBGGRR) -> '#rrggbb'. The byte order is the whole point."""
    red = colorref & 0xFF
    green = (colorref >> 8) & 0xFF
    blue = (colorref >> 16) & 0xFF
    return f"#{red:02x}{green:02x}{blue:02x}"


# ---- helpers ---------------------------------------------------------
class Debouncer:
    """Collapse a burst of widget callbacks into one delayed action.

    A grid-width drag crosses two hundred values; without this every one of
    them would tear down the frame source and spawn a new ffmpeg.
    """

    def __init__(self, widget: tk.Misc) -> None:
        self._widget = widget
        self._jobs: dict[str, tuple[str, Callable[[], None]]] = {}

    def call(self, key: str, delay_ms: int, fn: Callable[[], None]) -> None:
        self.cancel(key)
        job = self._widget.after(delay_ms, lambda: self._fire(key))
        self._jobs[key] = (job, fn)

    def cancel(self, key: str) -> None:
        job = self._jobs.pop(key, None)
        if job is not None:
            try:
                self._widget.after_cancel(job[0])
            except Exception:
                pass

    def flush(self) -> None:
        """Run every pending action now -- used before play and on close."""
        for key in list(self._jobs):
            job = self._jobs.pop(key, None)
            if job is None:
                continue
            try:
                self._widget.after_cancel(job[0])
            except Exception:
                pass
            job[1]()

    def cancel_all(self) -> None:
        for key in list(self._jobs):
            self.cancel(key)

    def pending(self) -> bool:
        return bool(self._jobs)

    def _fire(self, key: str) -> None:
        job = self._jobs.pop(key, None)
        if job is not None:
            job[1]()


class FormGrid(ttk.Frame):
    """Two-column label/control grid, so tabs line up without per-widget grids."""

    def __init__(self, master: tk.Misc, **kw: Any) -> None:
        super().__init__(master, style="TL.TFrame", **kw)
        self.columnconfigure(1, weight=1)
        self._row = 0

    def add(self, label: str, widget: tk.Widget, hint: str = "") -> tk.Widget:
        ttk.Label(self, text=label, style="Field.TLabel").grid(
            row=self._row, column=0, sticky="w", padx=(0, 12), pady=3)
        widget.grid(row=self._row, column=1, sticky="w", pady=3)
        if hint:
            ttk.Label(self, text=hint, style="Hint.TLabel").grid(
                row=self._row, column=2, sticky="w", padx=(12, 0))
        self._row += 1
        return widget

    def add_wide(self, widget: tk.Widget, pady: tuple[int, int] = (4, 4)) -> tk.Widget:
        widget.grid(row=self._row, column=0, columnspan=3, sticky="ew", pady=pady)
        self._row += 1
        return widget

    def heading(self, text: str) -> None:
        pad = (2, 4) if self._row == 0 else (12, 4)
        ttk.Label(self, text=text.upper(), style="Cap.TLabel",
                  background=COLORS["bg"]).grid(
            row=self._row, column=0, columnspan=3, sticky="w", pady=pad)
        self._row += 1


class LabeledSlider(ttk.Frame):
    """Slider plus a live numeric readout; double-click the readout to reset.

    Quantises to `step` before reporting, so an integer knob never hands the
    config a value like 95.7 and never fires twice for the same setting.
    """

    def __init__(self, master: tk.Misc, *, lo: float, hi: float, value: float,
                 command: Callable[[float], None] | None = None,
                 integer: bool = False, step: float | None = None,
                 fmt: str | None = None, length: int = 190,
                 default: float | None = None, unit: str = "") -> None:
        super().__init__(master, style="TL.TFrame")
        self._command = command
        self._integer = integer
        self._step = float(step) if step else (1.0 if integer else 0.0)
        self._fmt = fmt or ("{:.0f}" if integer else "{:.2f}")
        self._unit = unit
        self._busy = False
        self._value = self._quantize(value)
        self._default = self._quantize(value if default is None else default)

        self._var = tk.DoubleVar(self, self._value)
        self.scale = ttk.Scale(self, from_=lo, to=hi, orient="horizontal",
                               variable=self._var, length=length,
                               command=self._on_scale)
        self.readout = ttk.Label(self, style="Value.TLabel", width=8, anchor="e",
                                 text=self._text())
        self.scale.pack(side="left")
        self.readout.pack(side="left", padx=(8, 0))
        self.readout.bind("<Double-Button-1>", self._on_reset)

    # -- api
    def get(self) -> float:
        return int(self._value) if self._integer else self._value

    def set(self, value: float, notify: bool = False) -> None:
        quantised = self._quantize(value)
        self._busy = True
        try:
            self._var.set(quantised)
        finally:
            self._busy = False
        self._value = quantised
        self.readout.configure(text=self._text())
        if notify and self._command:
            self._command(self.get())

    def enable(self, on: bool) -> None:
        self.scale.state(("!disabled",) if on else ("disabled",))
        self.readout.configure(foreground=COLORS["accent"] if on else COLORS["muted"])

    # -- internals
    def _quantize(self, raw: float) -> float:
        value = float(raw)
        if self._step > 0:
            value = round(value / self._step) * self._step
        return float(int(round(value))) if self._integer else round(value, 4)

    def _text(self) -> str:
        return self._fmt.format(self._value) + self._unit

    def _on_scale(self, raw: str) -> None:
        if self._busy:
            return
        value = self._quantize(float(raw))
        if value == self._value:
            return                            # same setting: do not re-notify
        self._value = value
        self.readout.configure(text=self._text())
        if self._command:
            self._command(self.get())

    def _on_reset(self, _event: tk.Event) -> None:
        self.set(self._default, notify=True)


class MappedCombo(ttk.Combobox):
    """Combobox over (value, label) pairs.

    Keeps honest, wordy labels on screen -- "fast - smears vacated cells" --
    while the config still stores the bare literal the engine expects.
    """

    def __init__(self, master: tk.Misc, options: Sequence[tuple[Any, str]],
                 value: Any = None, command: Callable[[Any], None] | None = None,
                 width: int = 22) -> None:
        # Not `self._options`: tkinter.Misc._options is the method that renders
        # widget kwargs, and shadowing it breaks widget construction outright.
        self._pairs = list(options)
        super().__init__(master, state="readonly", width=width,
                         values=[label for _, label in self._pairs], font=UI)
        self._command = command
        self.set_value(value)
        self.bind("<<ComboboxSelected>>", self._on_pick)

    def value(self) -> Any:
        index = self.current()
        return self._pairs[index][0] if 0 <= index < len(self._pairs) else None

    def set_value(self, value: Any) -> None:
        for i, (candidate, _) in enumerate(self._pairs):
            if candidate == value:
                self.current(i)
                return
        if self._pairs:
            self.current(0)

    def set_options(self, options: Sequence[tuple[Any, str]], value: Any = None) -> None:
        self._pairs = list(options)
        self.configure(values=[label for _, label in self._pairs])
        self.set_value(value)

    def _on_pick(self, _event: tk.Event) -> None:
        self.selection_clear()
        if self._command:
            self._command(self.value())


class SeekBar(ttk.Frame):
    """Canvas scrubber with an honest drag protocol.

    A `ttk.Scale` fights the user: the 200 ms position feed calls `set` and
    snaps the thumb out from under the pointer. Here the feed is dropped for
    as long as a drag is live, and exactly one seek is issued, on release.
    """

    def __init__(self, master: tk.Misc, *,
                 on_scrub_start: Callable[[], None] | None = None,
                 on_scrub: Callable[[float], None] | None = None,
                 on_seek: Callable[[float], None] | None = None,
                 height: int = 26) -> None:
        super().__init__(master, style="TL.TFrame")
        self._on_scrub_start = on_scrub_start
        self._on_scrub = on_scrub
        self._on_seek = on_seek
        self._fraction = 0.0
        self._dragging = False
        self._enabled = True
        self._pad = 9

        self.canvas = tk.Canvas(self, height=height, bd=0, highlightthickness=0,
                                bg=COLORS["bg"], cursor="hand2")
        self.canvas.pack(fill="x", expand=True)
        self.canvas.bind("<Configure>", lambda _e: self._redraw())
        self.canvas.bind("<Button-1>", self._press)
        self.canvas.bind("<B1-Motion>", self._motion)
        self.canvas.bind("<ButtonRelease-1>", self._release)

    # -- api
    @property
    def dragging(self) -> bool:
        return self._dragging

    def fraction(self) -> float:
        return self._fraction

    def set_fraction(self, fraction: float) -> None:
        """Position feed. Ignored mid-drag; the user owns the thumb then."""
        if self._dragging:
            return
        value = 0.0 if fraction != fraction else max(0.0, min(1.0, float(fraction)))
        if abs(value - self._fraction) < 1e-4:
            return
        self._fraction = value
        self._redraw()

    def enable(self, on: bool) -> None:
        self._enabled = bool(on)
        self.canvas.configure(cursor="hand2" if on else "arrow")
        self._redraw()

    # -- events
    def _fraction_at(self, x: float) -> float:
        width = max(1, self.canvas.winfo_width() - 2 * self._pad)
        return max(0.0, min(1.0, (float(x) - self._pad) / width))

    def _press(self, event: tk.Event) -> None:
        if not self._enabled:
            return
        self._dragging = True
        self._fraction = self._fraction_at(event.x)
        self._redraw()
        if self._on_scrub_start:
            self._on_scrub_start()
        if self._on_scrub:
            self._on_scrub(self._fraction)

    def _motion(self, event: tk.Event) -> None:
        if not self._dragging:
            return
        self._fraction = self._fraction_at(event.x)
        self._redraw()
        if self._on_scrub:
            self._on_scrub(self._fraction)

    def _release(self, event: tk.Event) -> None:
        if not self._dragging:
            return
        self._fraction = self._fraction_at(event.x)
        self._dragging = False
        self._redraw()
        if self._on_seek:
            self._on_seek(self._fraction)

    def _redraw(self) -> None:
        c = self.canvas
        c.delete("all")
        width = c.winfo_width() or int(c["width"] or 300)
        height = c.winfo_height() or int(c["height"] or 26)
        pad, mid = self._pad, height / 2
        track_top, track_bottom = mid - 3, mid + 3
        played = pad + (width - 2 * pad) * self._fraction
        fill = COLORS["accent"] if self._enabled else COLORS["muted"]

        c.create_rectangle(pad, track_top, width - pad, track_bottom,
                           fill=COLORS["field"], outline=COLORS["line"])
        if played > pad:
            c.create_rectangle(pad, track_top, played, track_bottom,
                               fill=fill, outline=fill)
        radius = 8 if self._dragging else 6
        c.create_oval(played - radius, mid - radius, played + radius, mid + radius,
                      fill=COLORS["fg"] if self._dragging else fill,
                      outline=COLORS["bg"], width=2)


class SwatchStrip(ttk.Frame):
    """The actual COLORREFs the window pool will paint, at the current levels."""

    def __init__(self, master: tk.Misc, width: int = 190, height: int = 18) -> None:
        super().__init__(master, style="TL.TFrame")
        self._width = width
        self.canvas = tk.Canvas(self, width=width, height=height, bd=0,
                                highlightthickness=1,
                                highlightbackground=COLORS["line"],
                                bg="#000000")
        self.canvas.pack()
        self._name = "mono"
        self._levels = 1
        self.canvas.bind("<Configure>", lambda _e: self._redraw())

    def show(self, name: str, levels: int) -> None:
        self._name, self._levels = str(name), max(1, int(levels))
        self._redraw()

    def _redraw(self) -> None:
        c = self.canvas
        c.delete("all")
        height = c.winfo_height() or 18
        width = c.winfo_width() or self._width
        colors = palette.resolve(self._name, self._levels)
        step = width / max(1, len(colors))
        for i, colorref in enumerate(colors):
            c.create_rectangle(i * step, 0, (i + 1) * step, height,
                               fill=colorref_hex(colorref), outline="")


class StatReadout(ttk.Frame):
    """One-line telemetry strip: caption above, monospaced value below."""

    def __init__(self, master: tk.Misc,
                 fields: Sequence[tuple[str, str, int]]) -> None:
        super().__init__(master, style="TL.TFrame")
        self._labels: dict[str, ttk.Label] = {}
        for key, caption, width in fields:
            cell = ttk.Frame(self, style="Card.TFrame")
            cell.pack(side="left", padx=(0, 5))
            ttk.Label(cell, text=caption.upper(), style="Cap.TLabel").pack(
                anchor="w", padx=7, pady=(3, 0))
            value = ttk.Label(cell, text="-", style="Stat.TLabel", width=width,
                              anchor="w")
            value.pack(anchor="w", padx=7, pady=(0, 3))
            self._labels[key] = value

    def update_values(self, values: dict[str, str]) -> None:
        for key, text in values.items():
            label = self._labels.get(key)
            if label is not None and label.cget("text") != text:
                label.configure(text=text)


class ScrollFrame(ttk.Frame):
    """A vertically scrollable page. Put content in `.body`.

    The Visual tab carries more controls than fit at the 900x640 minimum
    window size, and a clipped tab is worse than a scrolled one: the
    placement controls simply could not be reached. Scrolling makes every
    tab usable at any window size instead of forcing a tall minimum.
    """

    def __init__(self, master: tk.Misc) -> None:
        super().__init__(master, style="TL.TFrame")
        self._canvas = tk.Canvas(self, bg=COLORS["bg"], highlightthickness=0,
                                 bd=0, takefocus=0)
        self._vsb = ttk.Scrollbar(self, orient="vertical",
                                  command=self._canvas.yview)
        self._canvas.configure(yscrollcommand=self._on_scroll)
        self._canvas.pack(side="left", fill="both", expand=True)

        self.body = ttk.Frame(self._canvas, style="TL.TFrame")
        self._window = self._canvas.create_window((0, 0), window=self.body,
                                                  anchor="nw")
        self.body.bind("<Configure>", self._on_body)
        self._canvas.bind("<Configure>", self._on_canvas)
        # Bind the wheel only while the pointer is over this page: a global
        # bind_all would scroll whichever page was hovered last, including
        # ones that are not even visible.
        self._canvas.bind("<Enter>", self._grab_wheel)
        self._canvas.bind("<Leave>", self._release_wheel)

    def _on_scroll(self, first: str, last: str) -> None:
        """Show the scrollbar only when there is something to scroll to."""
        if float(first) <= 0.0 and float(last) >= 1.0:
            self._vsb.pack_forget()
        else:
            self._vsb.pack(side="right", fill="y")
        self._vsb.set(first, last)

    def _on_body(self, _event: tk.Event) -> None:
        self._canvas.configure(scrollregion=self._canvas.bbox("all"))

    def _on_canvas(self, event: tk.Event) -> None:
        self._canvas.itemconfigure(self._window, width=event.width)

    def _grab_wheel(self, _event: tk.Event) -> None:
        self._canvas.bind_all("<MouseWheel>", self._wheel)

    def _release_wheel(self, _event: tk.Event) -> None:
        try:
            self._canvas.unbind_all("<MouseWheel>")
        except tk.TclError:
            pass

    def _wheel(self, event: tk.Event) -> None:
        if self._vsb.winfo_ismapped():
            self._canvas.yview_scroll(int(-event.delta / 120), "units")
