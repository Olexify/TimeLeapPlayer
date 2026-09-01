"""Typed Win32 bindings, DPI awareness and monitor enumeration.

Every function here declares `argtypes`/`restype`. That is not pedantry: an
`HWND` is a 64-bit pointer, and ctypes defaults undeclared pointer arguments
to 32-bit `int`. The prototype this project grew out of shipped a version
where every window handle was silently truncated on the way into
`DeferWindowPos`, so the calls failed with no error and no windows ever
appeared. Declare the types.

Import is guarded so the rest of the package still imports on non-Windows
for testing; `IS_WINDOWS` tells you whether anything here is usable.
"""
from __future__ import annotations

import ctypes
import sys
from ctypes import wintypes
from dataclasses import dataclass

IS_WINDOWS = sys.platform == "win32"

# ---- constants -------------------------------------------------------
WS_POPUP = 0x80000000
WS_VISIBLE = 0x10000000
WS_DISABLED = 0x08000000

WS_EX_TOPMOST = 0x00000008
WS_EX_TRANSPARENT = 0x00000020
WS_EX_TOOLWINDOW = 0x00000080
WS_EX_NOACTIVATE = 0x08000000

SWP_NOSIZE = 0x0001
SWP_NOMOVE = 0x0002
SWP_NOZORDER = 0x0004
SWP_NOREDRAW = 0x0008
SWP_NOACTIVATE = 0x0010
SWP_SHOWWINDOW = 0x0040
SWP_HIDEWINDOW = 0x0080
SWP_NOOWNERZORDER = 0x0200
SWP_ASYNCWINDOWPOS = 0x4000

SW_HIDE = 0
SW_SHOWNA = 8

CS_HREDRAW = 0x0002
CS_VREDRAW = 0x0001

SM_XVIRTUALSCREEN = 76
SM_YVIRTUALSCREEN = 77
SM_CXVIRTUALSCREEN = 78
SM_CYVIRTUALSCREEN = 79
SM_CMONITORS = 80

MONITORINFOF_PRIMARY = 0x00000001

# Where hidden windows are parked. Far off any real virtual desktop.
PARK_X, PARK_Y = -32000, -32000

WM_HOTKEY = 0x0312
WM_QUIT = 0x0012
MOD_ALT, MOD_CONTROL, MOD_SHIFT, MOD_NOREPEAT = 0x0001, 0x0002, 0x0004, 0x4000


if IS_WINDOWS:
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    gdi32 = ctypes.WinDLL("gdi32", use_last_error=True)
    winmm = ctypes.WinDLL("winmm", use_last_error=True)
else:  # pragma: no cover - import shim so the package loads elsewhere
    user32 = kernel32 = gdi32 = winmm = None

LRESULT = ctypes.c_ssize_t
HCURSOR = wintypes.HANDLE
HBRUSH = wintypes.HANDLE
HICON = wintypes.HANDLE

if IS_WINDOWS:
    WNDPROC = ctypes.WINFUNCTYPE(LRESULT, wintypes.HWND, wintypes.UINT,
                                 wintypes.WPARAM, wintypes.LPARAM)

    class WNDCLASSEXW(ctypes.Structure):
        _fields_ = [
            ("cbSize", wintypes.UINT),
            ("style", wintypes.UINT),
            ("lpfnWndProc", WNDPROC),
            ("cbClsExtra", ctypes.c_int),
            ("cbWndExtra", ctypes.c_int),
            ("hInstance", wintypes.HINSTANCE),
            ("hIcon", HICON),
            ("hCursor", HCURSOR),
            ("hbrBackground", HBRUSH),
            ("lpszMenuName", wintypes.LPCWSTR),
            ("lpszClassName", wintypes.LPCWSTR),
            ("hIconSm", HICON),
        ]

    class MONITORINFOEXW(ctypes.Structure):
        _fields_ = [
            ("cbSize", wintypes.DWORD),
            ("rcMonitor", wintypes.RECT),
            ("rcWork", wintypes.RECT),
            ("dwFlags", wintypes.DWORD),
            ("szDevice", wintypes.WCHAR * 32),
        ]

    MONITORENUMPROC = ctypes.WINFUNCTYPE(
        wintypes.BOOL, wintypes.HMONITOR, wintypes.HDC,
        ctypes.POINTER(wintypes.RECT), wintypes.LPARAM)

    def _bind(fn, argtypes, restype):
        fn.argtypes = argtypes
        fn.restype = restype
        return fn

    CreateWindowExW = _bind(
        user32.CreateWindowExW,
        [wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD,
         ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
         wintypes.HWND, wintypes.HMENU, wintypes.HINSTANCE, wintypes.LPVOID],
        wintypes.HWND)
    DestroyWindow = _bind(user32.DestroyWindow, [wintypes.HWND], wintypes.BOOL)
    ShowWindow = _bind(user32.ShowWindow, [wintypes.HWND, ctypes.c_int], wintypes.BOOL)
    SetWindowPos = _bind(
        user32.SetWindowPos,
        [wintypes.HWND, wintypes.HWND, ctypes.c_int, ctypes.c_int,
         ctypes.c_int, ctypes.c_int, wintypes.UINT], wintypes.BOOL)
    BeginDeferWindowPos = _bind(user32.BeginDeferWindowPos, [ctypes.c_int],
                                wintypes.HANDLE)
    DeferWindowPos = _bind(
        user32.DeferWindowPos,
        [wintypes.HANDLE, wintypes.HWND, wintypes.HWND, ctypes.c_int,
         ctypes.c_int, ctypes.c_int, ctypes.c_int, wintypes.UINT],
        wintypes.HANDLE)
    EndDeferWindowPos = _bind(user32.EndDeferWindowPos, [wintypes.HANDLE],
                              wintypes.BOOL)
    RegisterClassExW = _bind(user32.RegisterClassExW,
                             [ctypes.POINTER(WNDCLASSEXW)], wintypes.ATOM)
    UnregisterClassW = _bind(user32.UnregisterClassW,
                             [wintypes.LPCWSTR, wintypes.HINSTANCE], wintypes.BOOL)
    DefWindowProcW = _bind(
        user32.DefWindowProcW,
        [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM], LRESULT)
    GetModuleHandleW = _bind(kernel32.GetModuleHandleW, [wintypes.LPCWSTR],
                             wintypes.HMODULE)
    GetSystemMetrics = _bind(user32.GetSystemMetrics, [ctypes.c_int], ctypes.c_int)
    EnumDisplayMonitors = _bind(
        user32.EnumDisplayMonitors,
        [wintypes.HDC, ctypes.POINTER(wintypes.RECT), MONITORENUMPROC,
         wintypes.LPARAM], wintypes.BOOL)
    GetMonitorInfoW = _bind(user32.GetMonitorInfoW,
                            [wintypes.HMONITOR, ctypes.POINTER(MONITORINFOEXW)],
                            wintypes.BOOL)
    CreateSolidBrush = _bind(gdi32.CreateSolidBrush, [wintypes.COLORREF], HBRUSH)
    DeleteObject = _bind(gdi32.DeleteObject, [wintypes.HGDIOBJ], wintypes.BOOL)
    InvalidateRect = _bind(user32.InvalidateRect,
                           [wintypes.HWND, ctypes.POINTER(wintypes.RECT),
                            wintypes.BOOL], wintypes.BOOL)
    RegisterHotKey = _bind(user32.RegisterHotKey,
                           [wintypes.HWND, ctypes.c_int, wintypes.UINT,
                            wintypes.UINT], wintypes.BOOL)
    UnregisterHotKey = _bind(user32.UnregisterHotKey,
                             [wintypes.HWND, ctypes.c_int], wintypes.BOOL)
    GetMessageW = _bind(user32.GetMessageW,
                        [ctypes.POINTER(wintypes.MSG), wintypes.HWND,
                         wintypes.UINT, wintypes.UINT], wintypes.BOOL)
    PeekMessageW = _bind(user32.PeekMessageW,
                         [ctypes.POINTER(wintypes.MSG), wintypes.HWND,
                          wintypes.UINT, wintypes.UINT, wintypes.UINT],
                         wintypes.BOOL)
    TranslateMessage = _bind(user32.TranslateMessage,
                             [ctypes.POINTER(wintypes.MSG)], wintypes.BOOL)
    DispatchMessageW = _bind(user32.DispatchMessageW,
                             [ctypes.POINTER(wintypes.MSG)], LRESULT)
    PostThreadMessageW = _bind(user32.PostThreadMessageW,
                               [wintypes.DWORD, wintypes.UINT, wintypes.WPARAM,
                                wintypes.LPARAM], wintypes.BOOL)
    GetCurrentThreadId = _bind(kernel32.GetCurrentThreadId, [], wintypes.DWORD)
    timeBeginPeriod = _bind(winmm.timeBeginPeriod, [wintypes.UINT], wintypes.UINT)
    timeEndPeriod = _bind(winmm.timeEndPeriod, [wintypes.UINT], wintypes.UINT)

    HWND_TOPMOST = wintypes.HWND(-1)
    HWND_NOTOPMOST = wintypes.HWND(-2)
    HWND_BOTTOM = wintypes.HWND(1)


# ---- DPI -------------------------------------------------------------
def enable_dpi_awareness() -> str:
    """Opt into per-monitor DPI awareness. Must run before any window exists.

    Without this, Windows lies about screen coordinates on a scaled display:
    `GetSystemMetrics` reports logical pixels, so on a 150%-scaled 2560x1440
    monitor the player would think the screen is 1707x960 and paint into the
    top-left two thirds of it.
    """
    if not IS_WINDOWS:
        return "n/a"
    try:  # Win10 1703+: per-monitor v2, the only one that handles mixed DPI
        ctx = ctypes.c_void_p(-4)
        user32.SetProcessDpiAwarenessContext.argtypes = [ctypes.c_void_p]
        user32.SetProcessDpiAwarenessContext.restype = wintypes.BOOL
        if user32.SetProcessDpiAwarenessContext(ctx):
            return "per-monitor-v2"
    except Exception:
        pass
    try:  # Win8.1+
        shcore = ctypes.WinDLL("shcore")
        shcore.SetProcessDpiAwareness.argtypes = [ctypes.c_int]
        shcore.SetProcessDpiAwareness.restype = ctypes.c_long
        if shcore.SetProcessDpiAwareness(2) == 0:
            return "per-monitor"
    except Exception:
        pass
    try:
        user32.SetProcessDPIAware()
        return "system"
    except Exception:
        return "none"


# ---- monitors --------------------------------------------------------
@dataclass(frozen=True)
class Monitor:
    index: int
    x: int
    y: int
    width: int
    height: int
    primary: bool
    name: str

    @property
    def rect(self) -> tuple[int, int, int, int]:
        return (self.x, self.y, self.width, self.height)

    def label(self) -> str:
        tag = " (primary)" if self.primary else ""
        return f"{self.index}: {self.width}x{self.height} @ {self.x},{self.y}{tag}"


def monitors() -> list[Monitor]:
    """Physical monitors, left-to-right then top-to-bottom."""
    if not IS_WINDOWS:
        return [Monitor(0, 0, 0, 1920, 1080, True, "virtual")]
    found: list[Monitor] = []

    def cb(hmon, hdc, lprc, lparam):
        info = MONITORINFOEXW()
        info.cbSize = ctypes.sizeof(MONITORINFOEXW)
        if GetMonitorInfoW(hmon, ctypes.byref(info)):
            r = info.rcMonitor
            found.append(Monitor(
                len(found), r.left, r.top, r.right - r.left, r.bottom - r.top,
                bool(info.dwFlags & MONITORINFOF_PRIMARY), info.szDevice))
        return True

    EnumDisplayMonitors(None, None, MONITORENUMPROC(cb), 0)
    found.sort(key=lambda m: (m.y, m.x))
    ordered = [Monitor(i, m.x, m.y, m.width, m.height, m.primary, m.name)
               for i, m in enumerate(found)]
    return ordered or [virtual_screen_monitor()]


def virtual_screen_monitor() -> Monitor:
    """The bounding box of every monitor, as a single pseudo-monitor."""
    if not IS_WINDOWS:
        return Monitor(-1, 0, 0, 1920, 1080, True, "virtual")
    return Monitor(-1,
                   GetSystemMetrics(SM_XVIRTUALSCREEN),
                   GetSystemMetrics(SM_YVIRTUALSCREEN),
                   GetSystemMetrics(SM_CXVIRTUALSCREEN),
                   GetSystemMetrics(SM_CYVIRTUALSCREEN),
                   False, "All monitors")


def target_rect(monitor_index: int,
                region: tuple[int, int, int, int] | None = None
                ) -> tuple[int, int, int, int]:
    """Resolve a RenderConfig monitor/region choice to a pixel rectangle."""
    if region:
        return tuple(int(v) for v in region)  # type: ignore[return-value]
    if monitor_index < 0:
        return virtual_screen_monitor().rect
    mons = monitors()
    if monitor_index < len(mons):
        return mons[monitor_index].rect
    return virtual_screen_monitor().rect


def fit_rect(target: tuple[int, int, int, int], aspect: float,
             mode: str = "contain") -> tuple[int, int, int, int]:
    """Letterbox/crop the video aspect ratio into the target rectangle."""
    tx, ty, tw, th = target
    if mode == "stretch" or aspect <= 0:
        return target
    want_w, want_h = tw, int(round(tw / aspect))
    if (want_h > th) if mode == "contain" else (want_h < th):
        want_h, want_w = th, int(round(th * aspect))
    return (tx + (tw - want_w) // 2, ty + (th - want_h) // 2, want_w, want_h)


def rgb(r: int, g: int, b: int) -> int:
    """Pack to a Win32 COLORREF, which is 0x00BBGGRR -- not RGB."""
    return (b << 16) | (g << 8) | r


PM_REMOVE = 0x0001


def pump_messages(limit: int = 64) -> None:
    """Drain the calling thread's message queue without blocking.

    A window belongs to the thread that created it, and it only ever repaints
    when that thread dispatches messages. The render thread owns hundreds of
    windows, so it must pump every frame -- otherwise Windows marks them
    unresponsive and paints ghost-white rectangles instead of the palette
    colour, which looks exactly like "the renderer is broken".
    """
    if not IS_WINDOWS:
        return
    msg = wintypes.MSG()
    for _ in range(limit):
        if not PeekMessageW(ctypes.byref(msg), None, 0, 0, PM_REMOVE):
            return
        TranslateMessage(ctypes.byref(msg))
        DispatchMessageW(ctypes.byref(msg))
