"""Drag-and-resize overlay for choosing where the video plays.

The pixel windows are deliberately click-through (`WS_EX_TRANSPARENT`), which
is what keeps the desktop usable during playback -- but it also means you can
never grab the picture with the mouse. This is the handle: a translucent
frame you drag and resize directly on the desktop, over the top of the running
video.

It reports every change as it happens rather than only on commit, so the
video moves under your cursor instead of jumping once you let go.
"""
from __future__ import annotations

import tkinter as tk
from collections.abc import Callable

Rect = tuple[int, int, int, int]

EDGE = 14           # px from a border that counts as a resize grip
MIN_W, MIN_H = 120, 68
BORDER = "#4aa3ff"
FILL = "#0b1622"


def geometry_for(rect: Rect) -> str:
    """Tk geometry string. Negative coordinates need the `+-N` spelling."""
    x, y, w, h = rect
    return f"{max(1, w)}x{max(1, h)}{x:+d}{y:+d}".replace("+-", "+-")


class RegionPicker(tk.Toplevel):
    """A translucent, borderless rectangle the user drags and resizes."""

    def __init__(self, master: tk.Misc, rect: Rect,
                 on_change: Callable[[Rect], None] | None = None,
                 on_commit: Callable[[Rect], None] | None = None,
                 on_cancel: Callable[[], None] | None = None) -> None:
        super().__init__(master)
        self.on_change = on_change
        self.on_commit = on_commit
        self.on_cancel = on_cancel
        self._rect: Rect = self._sane(rect)
        self._mode = ""            # "" | "move" | resize edge spec like "nw"
        self._grab = (0, 0)
        self._start: Rect = self._rect
        self._done = False

        self.overrideredirect(True)
        self.attributes("-topmost", True)
        self.attributes("-alpha", 0.45)
        self.configure(bg=BORDER)
        self.geometry(geometry_for(self._rect))

        inner = tk.Frame(self, bg=FILL)
        inner.pack(fill="both", expand=True, padx=3, pady=3)
        self.readout = tk.Label(
            inner, bg=FILL, fg="#dbe9ff", justify="center",
            font=("Segoe UI", 10),
            text="")
        self.readout.place(relx=0.5, rely=0.5, anchor="center")

        for w in (self, inner, self.readout):
            w.bind("<Button-1>", self._press)
            w.bind("<B1-Motion>", self._drag)
            w.bind("<ButtonRelease-1>", self._release)
            w.bind("<Motion>", self._hover)
            w.bind("<Double-Button-1>", lambda _e: self.commit())
        self.bind("<Escape>", lambda _e: self.cancel())
        self.bind("<Return>", lambda _e: self.commit())
        for key, dx, dy in (("Left", -1, 0), ("Right", 1, 0),
                            ("Up", 0, -1), ("Down", 0, 1)):
            self.bind(f"<{key}>", lambda _e, a=dx, b=dy: self._nudge(a, b, 1))
            self.bind(f"<Shift-{key}>", lambda _e, a=dx, b=dy: self._nudge(a, b, 10))
        self.protocol("WM_DELETE_WINDOW", self.cancel)

        self._refresh_text()
        self.after(10, self._take_focus)
        self._keep_on_top()

    # ---- helpers -----------------------------------------------------
    def _take_focus(self) -> None:
        try:
            self.focus_force()
        except tk.TclError:
            pass

    def _keep_on_top(self) -> None:
        """Re-assert z-order while the handle is open.

        The pixel windows are top-most too, and every frame shows a few of
        them with `SWP_SHOWWINDOW`, which puts them above their peers. Without
        this the handle you are dragging disappears behind the video within a
        second or two.
        """
        if self._done:
            return
        try:
            self.lift()
            self.attributes("-topmost", True)
        except tk.TclError:
            return
        self.after(300, self._keep_on_top)

    @staticmethod
    def _sane(rect: Rect) -> Rect:
        x, y, w, h = (int(v) for v in rect)
        return (x, y, max(MIN_W, w), max(MIN_H, h))

    @property
    def rect(self) -> Rect:
        return self._rect

    def _apply(self, rect: Rect, live: bool = True) -> None:
        self._rect = self._sane(rect)
        self.geometry(geometry_for(self._rect))
        self._refresh_text()
        if live and self.on_change:
            self.on_change(self._rect)

    def _refresh_text(self) -> None:
        x, y, w, h = self._rect
        self.readout.configure(
            text=f"{w} x {h}   at   {x}, {y}\n\n"
                 "drag to move  •  edges to resize  •  arrows nudge\n"
                 "Enter or double-click to apply  •  Esc to cancel")

    # ---- mouse -------------------------------------------------------
    def _edge_at(self, ex: int, ey: int) -> str:
        w, h = self._rect[2], self._rect[3]
        spec = ""
        if ey <= EDGE:
            spec += "n"
        elif ey >= h - EDGE:
            spec += "s"
        if ex <= EDGE:
            spec += "w"
        elif ex >= w - EDGE:
            spec += "e"
        return spec

    def _hover(self, event: tk.Event) -> None:
        if self._mode:
            return
        spec = self._edge_at(event.x_root - self._rect[0],
                             event.y_root - self._rect[1])
        cursors = {"n": "sb_v_double_arrow", "s": "sb_v_double_arrow",
                   "w": "sb_h_double_arrow", "e": "sb_h_double_arrow",
                   "nw": "size_nw_se", "se": "size_nw_se",
                   "ne": "size_ne_sw", "sw": "size_ne_sw"}
        try:
            self.configure(cursor=cursors.get(spec, "fleur"))
        except tk.TclError:
            pass

    def _press(self, event: tk.Event) -> None:
        # Work in screen coordinates throughout: the event widget varies
        # (frame, label, toplevel) and each has its own local origin.
        self._grab = (event.x_root, event.y_root)
        self._start = self._rect
        self._mode = self._edge_at(event.x_root - self._rect[0],
                                   event.y_root - self._rect[1]) or "move"

    def _drag(self, event: tk.Event) -> None:
        if not self._mode:
            return
        dx = event.x_root - self._grab[0]
        dy = event.y_root - self._grab[1]
        x, y, w, h = self._start
        if self._mode == "move":
            self._apply((x + dx, y + dy, w, h))
            return
        if "n" in self._mode:
            dy = min(dy, h - MIN_H)
            y, h = y + dy, h - dy
        if "s" in self._mode:
            h = max(MIN_H, h + dy)
        if "w" in self._mode:
            dx = min(dx, w - MIN_W)
            x, w = x + dx, w - dx
        if "e" in self._mode:
            w = max(MIN_W, w + dx)
        self._apply((x, y, w, h))

    def _release(self, _event: tk.Event) -> None:
        self._mode = ""

    def _nudge(self, dx: int, dy: int, step: int) -> None:
        x, y, w, h = self._rect
        self._apply((x + dx * step, y + dy * step, w, h))

    # ---- finish ------------------------------------------------------
    def commit(self) -> None:
        if self._done:
            return
        self._done = True
        rect = self._rect
        self._teardown()
        if self.on_commit:
            self.on_commit(rect)

    def cancel(self) -> None:
        if self._done:
            return
        self._done = True
        self._teardown()
        if self.on_cancel:
            self.on_cancel()

    def _teardown(self) -> None:
        try:
            self.destroy()
        except tk.TclError:
            pass
