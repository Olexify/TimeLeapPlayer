"""Pooled top-most windows, one window class per palette colour.

Giving every window a colour is the difference between a 1-bit silhouette and
a greyscale (or fully coloured) picture. The obvious way -- one class plus a
`WM_ERASEBKGND` handler that looks up a per-window colour -- means a Python
callback runs for every repaint of every window, several thousand times a
second, all holding the GIL.

Instead each distinct colour gets its own registered class with its own
`hbrBackground`. `DefWindowProc` then paints the background in C, with no
Python involvement at all, and a window never changes colour: it only ever
moves, shows or hides. Levels map to pools, and the renderer batches across
all pools into a single `DeferWindowPos` transaction.

Every object here must be created *and* destroyed on the render thread, since
a window belongs to the thread that created it.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field

from . import win32 as w

# Registered classes and brushes are process-global and intentionally never
# freed: a class cannot be unregistered while any of its windows still exist,
# and the cost is a handful of handles for the life of the process.
_CLASSES: dict[int, str] = {}
_BRUSHES: dict[int, int] = {}
_WNDPROC_REF = None


def _class_for_color(color: int) -> str:
    """Register (once) a window class whose background brush is `color`."""
    global _WNDPROC_REF
    name = _CLASSES.get(color)
    if name is not None:
        return name

    name = f"TimeLeapPx_{color:06X}_{os.getpid()}"
    if _WNDPROC_REF is None:
        # DefWindowProcW does everything we need; cast it to a WNDPROC so
        # ctypes marshals the pointer at full width, and keep the reference
        # alive for as long as any class points at it.
        import ctypes
        _WNDPROC_REF = w.WNDPROC(
            ctypes.cast(w.user32.DefWindowProcW, ctypes.c_void_p).value)

    brush = _BRUSHES.get(color)
    if brush is None:
        brush = w.CreateSolidBrush(color)
        _BRUSHES[color] = brush

    wc = w.WNDCLASSEXW()
    import ctypes
    wc.cbSize = ctypes.sizeof(w.WNDCLASSEXW)
    wc.style = 0
    wc.lpfnWndProc = _WNDPROC_REF
    wc.cbClsExtra = 0
    wc.cbWndExtra = 0
    wc.hInstance = w.GetModuleHandleW(None)
    wc.hIcon = None
    wc.hCursor = None
    wc.hbrBackground = brush
    wc.lpszMenuName = None
    wc.lpszClassName = name
    wc.hIconSm = None

    atom = w.RegisterClassExW(ctypes.byref(wc))
    if not atom and ctypes.get_last_error() != 1410:  # CLASS_ALREADY_EXISTS
        raise OSError(f"RegisterClassExW failed for {name}: "
                      f"{ctypes.get_last_error()}")
    _CLASSES[color] = name
    return name


def ex_style_for(topmost: bool = True, click_through: bool = True,
                 no_activate: bool = True) -> int:
    """Extended styles for a pixel window.

    TOOLWINDOW keeps it off the taskbar and out of Alt-Tab. NOACTIVATE stops
    it stealing focus. TRANSPARENT removes it from hit-testing, so the desktop
    underneath stays clickable -- without it, covering the screen in top-most
    windows makes the machine unusable until playback ends.
    """
    style = w.WS_EX_TOOLWINDOW
    if topmost:
        style |= w.WS_EX_TOPMOST
    if no_activate:
        style |= w.WS_EX_NOACTIVATE
    if click_through:
        style |= w.WS_EX_TRANSPARENT
    return style


@dataclass
class Layer:
    """One colour's worth of windows."""

    color: int
    ex_style: int
    hwnds: list[int] = field(default_factory=list)
    rects: list[tuple[int, int, int, int] | None] = field(default_factory=list)

    def ensure(self, n: int) -> int:
        """Grow the pool to at least `n` windows; return how many exist."""
        if n <= len(self.hwnds):
            return len(self.hwnds)
        cls = _class_for_color(self.color)
        hinst = w.GetModuleHandleW(None)
        while len(self.hwnds) < n:
            hwnd = w.CreateWindowExW(
                self.ex_style, cls, None, w.WS_POPUP,
                w.PARK_X, w.PARK_Y, 1, 1, None, None, hinst, None)
            if not hwnd:
                break                      # out of desktop heap; use what we got
            self.hwnds.append(hwnd)
            self.rects.append(None)
        return len(self.hwnds)

    def destroy(self) -> None:
        for hwnd in self.hwnds:
            try:
                w.DestroyWindow(hwnd)
            except Exception:
                pass
        self.hwnds.clear()
        self.rects.clear()


class WindowPool:
    """All the layers for the current palette."""

    def __init__(self) -> None:
        self.layers: list[Layer] = []
        self._colors: list[int] = []
        self._ex_style = 0

    # ---- lifecycle ---------------------------------------------------
    def configure(self, colors: list[int], *, topmost: bool = True,
                  click_through: bool = True, no_activate: bool = True) -> bool:
        """Point the pool at a palette. Returns True if windows were rebuilt.

        Colours are baked into window classes, so a palette change means new
        windows. Style changes do too (`WS_EX_TRANSPARENT` cannot be toggled
        reliably at runtime). Both are user actions, not per-frame ones.
        """
        ex = ex_style_for(topmost, click_through, no_activate)
        if colors == self._colors and ex == self._ex_style:
            return False
        self.destroy()
        self._colors = list(colors)
        self._ex_style = ex
        self.layers = [Layer(c, ex) for c in self._colors]
        return True

    def destroy(self) -> None:
        for layer in self.layers:
            layer.destroy()
        self.layers.clear()

    # ---- queries -----------------------------------------------------
    @property
    def window_count(self) -> int:
        return sum(len(l.hwnds) for l in self.layers)

    def layer(self, level: int) -> Layer:
        """Clamp out-of-range levels rather than raising mid-frame."""
        if not self.layers:
            raise RuntimeError("WindowPool.configure() was never called")
        return self.layers[min(max(level, 0), len(self.layers) - 1)]


class Blackout:
    """An optional full-screen black window behind the pixels.

    Without it the video is drawn over whatever is on the desktop, which is
    the authentic 'virus' look. With it you get a proper letterboxed picture.
    """

    BLACK = 0x000000

    def __init__(self) -> None:
        self.hwnd: int | None = None

    def show(self, rect: tuple[int, int, int, int], ex_style: int) -> None:
        x, y, width, height = rect
        if self.hwnd is None:
            cls = _class_for_color(self.BLACK)
            self.hwnd = w.CreateWindowExW(
                ex_style, cls, None, w.WS_POPUP, x, y, width, height,
                None, None, w.GetModuleHandleW(None), None)
            if not self.hwnd:
                return
        w.SetWindowPos(self.hwnd, w.HWND_TOPMOST, x, y, width, height,
                       w.SWP_SHOWWINDOW | w.SWP_NOACTIVATE)

    def hide(self) -> None:
        if self.hwnd:
            w.SetWindowPos(self.hwnd, None, w.PARK_X, w.PARK_Y, 1, 1,
                           w.SWP_HIDEWINDOW | w.SWP_NOACTIVATE | w.SWP_NOZORDER)

    def destroy(self) -> None:
        if self.hwnd:
            try:
                w.DestroyWindow(self.hwnd)
            except Exception:
                pass
            self.hwnd = None
