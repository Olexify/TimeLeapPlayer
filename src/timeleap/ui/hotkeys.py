"""Global hotkeys, above all else the panic key.

This is a safety feature, not a convenience. A running show puts hundreds of
top-most, click-through, non-activating windows over the whole desktop: the
taskbar is buried, Alt+Tab lands on windows that refuse focus, and the Tk
control panel may be somewhere under the pile. `Ctrl+Alt+Q` is then the only
reliable way out, so it must keep working even when the UI thread is wedged --
which is why it lives on its own thread and touches nothing but the callback.

`RegisterHotKey(NULL, ...)` posts `WM_HOTKEY` to the *calling thread's* message
queue, and only that thread can ever receive it. Registering from Tk's thread
and pumping somewhere else silently swallows every press, so registration and
the `GetMessage` loop have to be the same thread. That thread is created here
and does nothing else, so a stuck renderer cannot starve it.

Callbacks therefore run on the hotkey thread. A Tk caller must marshal them
with `widget.after(0, ...)`; calling Tk from here deadlocks.
"""
from __future__ import annotations

import ctypes
import threading
from ctypes import wintypes
from typing import Callable, NamedTuple

from ..render import win32 as w

# Registration happens on a thread we just started, so `start()` waits for it
# rather than guessing; the whole handshake is three system calls.
_START_TIMEOUT = 3.0
_STOP_TIMEOUT = 3.0

PM_NOREMOVE = 0x0000
WM_NULL = 0x0000

VK_SPACE = 0x20
VK_RIGHT = 0x27
VK_Q = 0x51


class Hotkey(NamedTuple):
    name: str
    id: int
    mods: int
    vk: int
    label: str


_CTRL_ALT = w.MOD_CONTROL | w.MOD_ALT

# Ids are process-wide for hwnd=NULL registrations, so they start high enough
# to stay clear of the 0..0xBFFF range an embedded control might use.
HOTKEYS: tuple[Hotkey, ...] = (
    Hotkey("panic", 0xB001, _CTRL_ALT, VK_Q, "Ctrl+Alt+Q"),
    Hotkey("playpause", 0xB002, _CTRL_ALT, VK_SPACE, "Ctrl+Alt+Space"),
    Hotkey("next", 0xB003, _CTRL_ALT, VK_RIGHT, "Ctrl+Alt+Right"),
)

BY_ID: dict[int, Hotkey] = {h.id: h for h in HOTKEYS}
BY_NAME: dict[str, Hotkey] = {h.name: h for h in HOTKEYS}


def label(name: str) -> str:
    """Human-readable combo for `name`, for menus and status lines."""
    hk = BY_NAME.get(name)
    return hk.label if hk else ""


class HotkeyManager:
    """Owns one message-pump thread and the hotkeys registered on it.

    Never raises: a combo another application already owns is a fact about the
    machine, not an error in this program, and losing play/pause must not cost
    the user the panic key.
    """

    def __init__(self, on_panic: Callable[[], None],
                 on_playpause: Callable[[], None] | None = None,
                 on_next: Callable[[], None] | None = None) -> None:
        self._callbacks: dict[str, Callable[[], None] | None] = {
            "panic": on_panic, "playpause": on_playpause, "next": on_next,
        }
        self._thread: threading.Thread | None = None
        self._ready = threading.Event()
        self._ok = False
        self._tid = 0
        self.registered: list[str] = []
        self.failed: list[str] = []
        self.error: str = ""

    # ---- lifecycle ---------------------------------------------------
    @property
    def running(self) -> bool:
        t = self._thread
        return bool(t is not None and t.is_alive())

    @property
    def thread_id(self) -> int:
        """Win32 id of the pump thread, or 0. `PostThreadMessage` targets it."""
        return self._tid

    def start(self) -> bool:
        """Register and begin pumping. True when the panic key is live.

        The optional combos are best-effort: they land in `failed` if some
        other application owns them and playback carries on regardless.
        """
        if not w.IS_WINDOWS:
            self.error = "global hotkeys need Windows"
            return False
        if self.running:
            return self._ok

        self.registered, self.failed, self.error = [], [], ""
        self._ok = False
        self._ready.clear()
        self._thread = threading.Thread(target=self._run, name="timeleap-hotkeys",
                                        daemon=True)
        self._thread.start()
        if not self._ready.wait(_START_TIMEOUT):
            self.error = self.error or "hotkey thread did not start"
            self.stop()
            return False
        if not self._ok:
            self.stop()             # reap the thread that registered nothing
        return self._ok

    def stop(self) -> None:
        """Unregister and join. Safe to call twice, and never blocks for long."""
        t, self._thread = self._thread, None
        if t is None:
            return
        # WM_QUIT is what makes GetMessage return 0; without it the thread sits
        # blocked in the kernel until the process dies.
        if self._tid and t.is_alive():
            try:
                w.PostThreadMessageW(self._tid, w.WM_QUIT, 0, 0)
            except OSError:
                pass
        t.join(timeout=_STOP_TIMEOUT)
        if not t.is_alive():
            self._tid = 0
        self._ok = False

    def __enter__(self) -> HotkeyManager:
        self.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self.stop()

    # ---- the pump thread ---------------------------------------------
    def _wanted(self) -> list[Hotkey]:
        return [h for h in HOTKEYS if self._callbacks.get(h.name) is not None]

    def _register(self, hk: Hotkey) -> bool:
        """MOD_NOREPEAT first: holding the combo must not queue 30 panics/s."""
        for mods in (hk.mods | w.MOD_NOREPEAT, hk.mods):
            if w.RegisterHotKey(None, hk.id, mods, hk.vk):
                return True
        err = ctypes.get_last_error()
        self.error = (f"{hk.label} is already taken by another application"
                      if err == 1409 else f"{hk.label} failed (WinError {err})")
        return False

    def _run(self) -> None:
        msg = wintypes.MSG()
        # A thread has no message queue until it asks for one, and RegisterHotKey
        # against a queueless thread succeeds while delivering nothing.
        w.PeekMessageW(ctypes.byref(msg), None, 0, 0, PM_NOREMOVE)
        try:
            self._tid = int(w.GetCurrentThreadId())
            for hk in self._wanted():
                (self.registered if self._register(hk) else self.failed).append(hk.name)
            self._ok = "panic" in self.registered
        except Exception as exc:                # pragma: no cover - ctypes only
            self.error = f"{type(exc).__name__}: {exc}"
        finally:
            self._ready.set()

        try:
            if self._ok:
                self._pump(msg)
        finally:
            self._unregister()

    def _pump(self, msg: wintypes.MSG) -> None:
        while True:
            got = int(w.GetMessageW(ctypes.byref(msg), None, 0, 0))
            if got in (0, -1):      # WM_QUIT, or the queue died with us
                return
            if msg.message == w.WM_HOTKEY:
                self._fire(int(msg.wParam))

    def _fire(self, hotkey_id: int) -> None:
        hk = BY_ID.get(hotkey_id)
        cb = self._callbacks.get(hk.name) if hk else None
        if cb is None:
            return
        try:
            cb()
        except Exception:
            pass                    # a broken handler must not kill the panic key

    def _unregister(self) -> None:
        for name in self.registered:
            try:
                w.UnregisterHotKey(None, BY_NAME[name].id)
            except OSError:
                pass
        self.registered = []
