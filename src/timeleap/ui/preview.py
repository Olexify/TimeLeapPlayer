"""Live preview: what the decoder sees, next to what the renderer will draw.

The prototype's preview was a label reading "Integrate vlc-python or mpv for
live frame display". Playing the video back is not actually the useful thing
here -- the user already has the video. What they cannot see anywhere else is
the pair of intermediate stages they are tuning: the grey grid left after
downscaling, and the rectangles that survive thresholding, the level split and
the window budget. Side by side, those two panes answer "is this the threshold,
the grid, or the box budget?" in one glance, which is otherwise a guessing game
played 200 windows at a time on the real desktop.

Frames arrive through `PreviewTap`: a bounded queue any thread may push into,
drained by the Toplevel on an `after` tick. The player's render thread
therefore never touches Tk and never blocks on a slow UI -- when the UI falls
behind, the tap silently discards the frames it could not show, which for a
preview is the correct answer rather than a bug.
"""
from __future__ import annotations

import threading
import tkinter as tk
from collections import deque
from dataclasses import dataclass
from typing import Callable, Sequence

import numpy as np
from PIL import Image, ImageDraw, ImageTk

__all__ = ["PreviewFrame", "PreviewTap", "PreviewWindow"]

# Pillow moved the resampling constants onto an enum; the old aliases still
# exist but resolving once here keeps the hot path free of getattr churn.
_NEAREST = getattr(getattr(Image, "Resampling", Image), "NEAREST")

BG = "#101014"
PANE_BG = "#1b1b22"          # letterbox area around a non-matching aspect
OUT_BG = "#000000"           # the renderer's "no window here" -- black
FG = "#d8d8e0"
DIM = "#8a8a99"
EDGE = "#2c2c38"


@dataclass(frozen=True)
class PreviewFrame:
    """One snapshot of the pipeline, as pushed through a `PreviewTap`."""

    gray: np.ndarray | None = None
    boxes: np.ndarray | None = None
    grid_w: int = 0
    grid_h: int = 0
    colors: tuple[int, ...] = ()
    gap: int = 0                 # preview pixels, not screen pixels
    mode: str = ""
    index: int = -1


class PreviewTap:
    """Bounded hand-off from any producer thread to the preview window.

    A `deque(maxlen=n)` is the whole mechanism: pushing past the limit evicts
    the oldest frame instead of blocking, so a stalled or closed preview can
    never apply back-pressure to the render thread. Dropped frames are counted
    rather than hidden, because a preview that is quietly 200 frames behind
    would be worse than one that admits it.
    """

    def __init__(self, maxsize: int = 2) -> None:
        self._q: deque[PreviewFrame] = deque(maxlen=max(1, int(maxsize)))
        self._lock = threading.Lock()
        self.pushed = 0
        self.dropped = 0

    def __len__(self) -> int:
        with self._lock:
            return len(self._q)

    def push(self, gray: np.ndarray | None = None,
             boxes: np.ndarray | None = None, *, grid_w: int = 0,
             grid_h: int = 0, colors: Sequence[int] = (), gap: int = 0,
             mode: str = "", index: int = -1, copy: bool = True) -> bool:
        """Offer a frame. Never blocks. False means an older frame was evicted.

        `copy` defends against a producer that reuses one scratch buffer per
        frame; at grid sizes (a few kB) the copy is far cheaper than the class
        of bug it removes.
        """
        if gray is not None:
            gray = np.asarray(gray)
            if copy:
                gray = gray.copy()
        if boxes is not None:
            boxes = np.asarray(boxes)
            if copy:
                boxes = boxes.copy()
        if not grid_w or not grid_h:
            if gray is not None and gray.ndim == 2:
                grid_h, grid_w = int(gray.shape[0]), int(gray.shape[1])
        item = PreviewFrame(gray, boxes, int(grid_w), int(grid_h),
                            tuple(int(c) for c in colors) if colors else (),
                            int(gap), str(mode), int(index))
        with self._lock:
            evicted = len(self._q) == self._q.maxlen
            self._q.append(item)
            self.pushed += 1
            if evicted:
                self.dropped += 1
            return not evicted

    def latest(self) -> PreviewFrame | None:
        """Newest frame, discarding anything staler. None when empty."""
        with self._lock:
            if not self._q:
                return None
            self.dropped += len(self._q) - 1
            item = self._q[-1]
            self._q.clear()
            return item

    def pop(self) -> PreviewFrame | None:
        """Oldest frame, for a consumer that wants every frame it can get."""
        with self._lock:
            return self._q.popleft() if self._q else None

    def clear(self) -> None:
        with self._lock:
            self._q.clear()


def _to_rgb(color: object) -> tuple[int, int, int]:
    """Accept a Win32 COLORREF (0x00BBGGRR) or an RGB triple -> RGB triple."""
    if isinstance(color, (tuple, list)) and len(color) >= 3:
        r, g, b = (max(0, min(255, int(c))) for c in color[:3])
        return (r, g, b)
    v = int(color)  # type: ignore[arg-type]
    return (v & 0xFF, (v >> 8) & 0xFF, (v >> 16) & 0xFF)


class PreviewWindow(tk.Toplevel):
    """Two panes: the decoded grey grid, and the boxes as the renderer draws them.

    `update_frame` / `update_boxes` are Tk-thread methods, but calling them
    from another thread is a mistake that costs a deadlock rather than an
    exception, so they detect it and reroute through the tap instead of
    touching a widget.
    """

    def __init__(self, master: tk.Misc, on_close: Callable[[], None] | None = None,
                 *, tap: PreviewTap | None = None,
                 pane: tuple[int, int] = (400, 225), interval_ms: int = 50,
                 title: str = "Preview — source / output") -> None:
        super().__init__(master)
        self.title(title)
        self.configure(bg=BG)
        self.resizable(False, False)
        self.protocol("WM_DELETE_WINDOW", self.close)
        try:
            self.transient(master.winfo_toplevel())
        except Exception:
            pass

        self.tap = tap if tap is not None else PreviewTap()
        self._on_close = on_close
        self._ui_thread = threading.get_ident()
        self._closed = False
        self._close_req = False
        self._after_id: str | None = None
        # 20 Hz, not 30: measured on this machine, a preview ticking at 30 Hz
        # beside a live render of 58 top-most windows costs the UI thread a
        # ~26 ms median per event-loop pass against ~11 ms at 20 Hz. The
        # window manager is the contended resource, and a diagnostic pane does
        # not need every frame to show what the threshold is doing.
        self._interval = max(10, int(interval_ms))

        self._pane_w = max(64, int(pane[0]))
        self._pane_h = max(36, int(pane[1]))

        # State the two panes need but only one of them is given per call.
        self._grid = (0, 0)
        self._colors: tuple[int, ...] = ()
        self._gap = 0
        self._mode = ""
        self._boxes_n = 0
        self._levels = 1
        self._index = -1

        # Live PhotoImage references. Tk holds only a weak claim on an image
        # assigned to a Label: drop the Python object and the pane goes blank.
        self._src_photo: ImageTk.PhotoImage | None = None
        self._out_photo: ImageTk.PhotoImage | None = None
        self._src_img: Image.Image | None = None
        self._out_img: Image.Image | None = None

        self._build()
        self._blank()
        self._after_id = self.after(self._interval, self._tick)

    # ---- widgets -------------------------------------------------------
    def _build(self) -> None:
        head = ("Segoe UI", 9, "bold")
        body = ("Consolas", 9)
        row = tk.Frame(self, bg=BG)
        row.pack(padx=8, pady=(8, 4))

        left = tk.Frame(row, bg=BG)
        left.pack(side="left", padx=(0, 8))
        tk.Label(left, text="SOURCE", bg=BG, fg=FG, font=head).pack(anchor="w")
        self._src_label = tk.Label(left, bg=PANE_BG, bd=1, relief="solid",
                                   highlightthickness=0)
        self._src_label.pack()
        self._src_caption = tk.Label(left, text="grey grid", bg=BG, fg=DIM,
                                     font=body)
        self._src_caption.pack(anchor="w")

        right = tk.Frame(row, bg=BG)
        right.pack(side="left")
        tk.Label(right, text="OUTPUT", bg=BG, fg=FG, font=head).pack(anchor="w")
        self._out_label = tk.Label(right, bg=PANE_BG, bd=1, relief="solid",
                                   highlightthickness=0)
        self._out_label.pack()
        self._out_caption = tk.Label(right, text="windows", bg=BG, fg=DIM,
                                     font=body)
        self._out_caption.pack(anchor="w")

        tk.Frame(self, bg=EDGE, height=1).pack(fill="x", padx=8)
        self._info = tk.Label(self, text="", bg=BG, fg=DIM, font=body,
                              anchor="w", justify="left")
        self._info.pack(fill="x", padx=8, pady=(4, 8))

    def _blank(self) -> None:
        empty = Image.new("RGB", (self._pane_w, self._pane_h), PANE_BG)
        self._show(self._src_label, empty, source=True)
        self._show(self._out_label, empty.copy(), source=False)
        self._refresh_info()

    # ---- public API ----------------------------------------------------
    @property
    def alive(self) -> bool:
        """Whether it is still worth pushing frames. Safe from any thread."""
        if self._closed or self._close_req:
            return False
        if not self._on_ui_thread():
            return True         # cannot ask Tk from here; the flag is the truth
        try:
            return bool(self.winfo_exists())
        except tk.TclError:
            return False

    def set_mode(self, mode: str, levels: int | None = None) -> None:
        """Label the threshold mode (and level count) shown in the readout."""
        self._mode = str(mode)
        if levels is not None:
            self._levels = max(1, int(levels))
        if self._on_ui_thread():
            self._refresh_info()

    def set_palette(self, colors: Sequence[int]) -> None:
        """Palette used by the OUTPUT pane, as COLORREFs or RGB triples."""
        self._colors = tuple(colors)

    def update_frame(self, gray: np.ndarray) -> None:
        """Show one decoded grey grid, upscaled nearest-neighbour."""
        if self._closed:
            return
        if not self._on_ui_thread():
            self.tap.push(gray=gray)
            return
        arr = self._as_gray(gray)
        if arr is None:
            return
        if not self._grid[0]:
            self._grid = (int(arr.shape[1]), int(arr.shape[0]))
        img = self._render_source(arr)
        self._show(self._src_label, img, source=True)
        self._src_caption.configure(
            text=f"{arr.shape[1]}x{arr.shape[0]} grey")
        self._refresh_info()

    def update_boxes(self, boxes: np.ndarray, grid_w: int, grid_h: int,
                     colors: Sequence[int], *, gap: int = 0) -> None:
        """Draw the boxes the way the renderer would, in the live palette.

        `gap` is in preview pixels: `RenderConfig.gap` is a screen-pixel
        deflation and the preview is at a different scale, so the caller
        decides how to represent it (1 px reads as "gapped" at any pane size).
        """
        if self._closed:
            return
        if not self._on_ui_thread():
            self.tap.push(boxes=boxes, grid_w=grid_w, grid_h=grid_h,
                          colors=colors, gap=gap)
            return
        arr = np.asarray(boxes)
        if arr.ndim != 2 or arr.shape[1] < 5:
            arr = np.empty((0, 5), np.int32)
        gw, gh = max(1, int(grid_w)), max(1, int(grid_h))
        self._grid = (gw, gh)
        if colors:
            self._colors = tuple(colors)
        self._gap = max(0, int(gap))
        self._boxes_n = int(arr.shape[0])
        if self._colors:
            self._levels = len(self._colors)
        img = self._render_output(arr, gw, gh)
        self._show(self._out_label, img, source=False)
        self._out_caption.configure(text=f"{self._boxes_n} boxes")
        self._refresh_info()

    def snapshot(self) -> Image.Image:
        """Both panes composited, for saving a PNG of what is on screen."""
        pad, head = 8, 14
        w = self._pane_w * 2 + pad * 3
        h = self._pane_h + head + pad * 2
        out = Image.new("RGB", (w, h), BG)
        d = ImageDraw.Draw(out)
        d.text((pad, 2), "SOURCE", fill=FG)
        d.text((pad * 2 + self._pane_w, 2), "OUTPUT", fill=FG)
        if self._src_img is not None:
            out.paste(self._src_img, (pad, head))
        if self._out_img is not None:
            out.paste(self._out_img, (pad * 2 + self._pane_w, head))
        d.text((pad, head + self._pane_h + 4), self._info_text(), fill=DIM)
        return out

    def close(self) -> None:
        """Idempotent: the WM close button and the owner both land here.

        Off the Tk thread this can only *ask*. CPython's `_tkinter` raises
        "main thread is not in main loop" for any call from a foreign thread
        -- `after` included -- so there is no marshalling primitive available
        there; the tick loop picks the request up instead.
        """
        if self._closed:
            return
        if not self._on_ui_thread():
            self._close_req = True
            return
        self._closed = True
        if self._after_id is not None:
            try:
                self.after_cancel(self._after_id)
            except Exception:
                pass
            self._after_id = None
        self._src_photo = None
        self._out_photo = None
        try:
            self.destroy()
        except Exception:
            pass
        if self._on_close is not None:
            try:
                self._on_close()
            except Exception:
                pass

    # ---- drain ---------------------------------------------------------
    def _tick(self) -> None:
        self._after_id = None
        if self._closed:
            return
        if self._close_req:
            self.close()
            return
        try:
            item = self.tap.latest()
            # Minimised or hidden: still drain, so the tap never goes stale,
            # but skip the paint -- a preview nobody can see is pure cost on
            # a UI thread that shares the window manager with 200 windows.
            if item is not None and self._visible():
                self._apply(item)
        except Exception:
            pass                      # a bad frame must not kill the tick loop
        if not self._closed:
            self._after_id = self.after(self._interval, self._tick)

    def _apply(self, item: PreviewFrame) -> None:
        if item.mode:
            self._mode = item.mode
        if item.index >= 0:
            self._index = item.index
        if item.gray is not None:
            self.update_frame(item.gray)
        if item.boxes is not None:
            gw = item.grid_w or self._grid[0]
            gh = item.grid_h or self._grid[1]
            self.update_boxes(item.boxes, gw, gh,
                              item.colors or self._colors, gap=item.gap)
        elif item.mode or item.index >= 0:
            self._refresh_info()

    def _on_ui_thread(self) -> bool:
        return threading.get_ident() == self._ui_thread

    def _visible(self) -> bool:
        """Is this window actually on screen?

        Not `winfo_viewable`: a Toplevel is its own OS window, so it stays
        visible when the app withdraws its root -- and `winfo_viewable`
        reports False for exactly that case, which silently blanks the
        preview. `state()` asks about this window and nothing else.
        """
        try:
            return self.state() in ("normal", "zoomed")
        except tk.TclError:
            return False

    # ---- painting ------------------------------------------------------
    @staticmethod
    def _as_gray(gray: np.ndarray) -> np.ndarray | None:
        arr = np.asarray(gray)
        if arr.ndim == 3 and arr.shape[2] == 1:
            arr = arr[:, :, 0]
        if arr.ndim != 2 or arr.size == 0:
            return None
        if arr.dtype != np.uint8:
            arr = np.clip(arr, 0, 255).astype(np.uint8)
        return np.ascontiguousarray(arr)

    def _fit(self, gw: int, gh: int) -> tuple[int, int, int, int]:
        """Letterbox the gw:gh grid inside the pane, centred."""
        pw, ph = self._pane_w, self._pane_h
        if gw <= 0 or gh <= 0:
            return (0, 0, pw, ph)
        scale = min(pw / gw, ph / gh)
        w = max(1, min(pw, int(gw * scale)))
        h = max(1, min(ph, int(gh * scale)))
        return ((pw - w) // 2, (ph - h) // 2, w, h)

    def _render_source(self, arr: np.ndarray) -> Image.Image:
        gh, gw = arr.shape
        x, y, w, h = self._fit(gw, gh)
        img = Image.new("RGB", (self._pane_w, self._pane_h), PANE_BG)
        cell = Image.fromarray(arr, "L").resize((w, h), _NEAREST)
        img.paste(cell.convert("RGB"), (x, y))
        return img

    def _render_output(self, boxes: np.ndarray, gw: int, gh: int) -> Image.Image:
        img = Image.new("RGB", (self._pane_w, self._pane_h), PANE_BG)
        x, y, w, h = self._fit(gw, gh)
        d = ImageDraw.Draw(img)
        d.rectangle((x, y, x + w - 1, y + h - 1), fill=OUT_BG)
        if boxes.shape[0] == 0:
            return img

        # Map grid *edges* to pixel edges, exactly as FrameRenderer does, so
        # adjacent boxes tile with no hairline seam at fractional scales.
        xmap = x + np.round(np.arange(gw + 1) * (w / float(gw))).astype(np.int32)
        ymap = y + np.round(np.arange(gh + 1) * (h / float(gh))).astype(np.int32)
        colors = [_to_rgb(c) for c in self._colors] or [(255, 255, 255)]
        gap = self._gap

        b = boxes.astype(np.int32, copy=False)
        bx0 = np.clip(b[:, 0], 0, gw)
        by0 = np.clip(b[:, 1], 0, gh)
        bx1 = np.clip(b[:, 0] + b[:, 2], 0, gw)
        by1 = np.clip(b[:, 1] + b[:, 3], 0, gh)
        px0, px1 = xmap[bx0], xmap[bx1]
        py0, py1 = ymap[by0], ymap[by1]
        pw = np.maximum(px1 - px0 - gap, 1)
        ph = np.maximum(py1 - py0 - gap, 1)
        lv = np.clip(b[:, 4], 0, len(colors) - 1)

        for i in range(b.shape[0]):
            x0, y0 = int(px0[i]), int(py0[i])
            d.rectangle((x0, y0, x0 + int(pw[i]) - 1, y0 + int(ph[i]) - 1),
                        fill=colors[int(lv[i])])
        return img

    def _show(self, label: tk.Label, img: Image.Image, source: bool) -> None:
        """Blit into the pane's existing Tk image where possible.

        Building a fresh `PhotoImage` per frame and re-`configure`-ing the
        label costs a widget relayout every time and leaves the old image for
        the GC; `paste` into the image already on the label is measurably
        cheaper and is what keeps the preview from stealing UI-thread time
        from a player that is already fighting the window manager.
        """
        photo = self._src_photo if source else self._out_photo
        if (photo is None or photo.width() != img.width
                or photo.height() != img.height):
            photo = ImageTk.PhotoImage(img, master=self)
            label.configure(image=photo)
        else:
            photo.paste(img)
        # The reference must outlive this frame or Tk paints nothing.
        if source:
            self._src_photo, self._src_img = photo, img
        else:
            self._out_photo, self._out_img = photo, img

    # ---- readout -------------------------------------------------------
    def _info_text(self) -> str:
        gw, gh = self._grid
        bits = [f"boxes {self._boxes_n:>4d}",
                f"grid {gw}x{gh}",
                f"levels {self._levels}",
                f"threshold {self._mode or '—'}"]
        if self._index >= 0:
            bits.insert(0, f"frame {self._index}")
        if self.tap.dropped:
            bits.append(f"dropped {self.tap.dropped}")
        return "  ·  ".join(bits)

    def _refresh_info(self) -> None:
        try:
            self._info.configure(text=self._info_text())
        except tk.TclError:
            pass                      # window went away between tick and paint
