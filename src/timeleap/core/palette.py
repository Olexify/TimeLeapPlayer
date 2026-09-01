"""Named colour ramps -> one Win32 COLORREF per luminance level.

`boxgen.quantize` emits band indices 0..levels-1, dimmest first, and the
window pool needs one solid brush colour per band. Ramps are written here as
ordinary RGB triples so they stay readable and the UI can show swatches;
`resolve` does the byte swap, which is the part that silently goes wrong:
COLORREF is 0x00BBGGRR, not 0x00RRGGBB. A reversed one never raises -- it
just paints amber as blue -- so the swap lives in exactly one function.

Ramps are keyframes, not lookup tables: a ramp of six colours serves any
`levels` from 2 to 16 by linear interpolation, so adding a palette means
picking half a dozen colours rather than sixteen.
"""
from __future__ import annotations

RGB = tuple[int, int, int]

# Dark -> bright. The last entry is what a 1-bit (levels == 1) render uses.
PALETTES: dict[str, list[RGB]] = {
    "mono": [(0, 0, 0), (64, 64, 64), (128, 128, 128), (192, 192, 192),
             (255, 255, 255)],
    "amber": [(20, 8, 0), (92, 40, 0), (170, 88, 4), (255, 150, 20),
              (255, 200, 90), (255, 238, 180)],
    "matrix": [(0, 12, 4), (0, 60, 20), (0, 120, 40), (0, 200, 70),
               (80, 255, 140), (200, 255, 220)],
    "ice": [(2, 8, 24), (10, 40, 90), (20, 90, 160), (60, 150, 220),
            (140, 205, 245), (225, 245, 255)],
    "fire": [(16, 0, 0), (90, 10, 0), (180, 40, 0), (240, 110, 10),
             (255, 190, 40), (255, 245, 190)],
    # A spectrum rather than a gradient: hue carries the level, and luminance
    # peaks at yellow rather than rising all the way, so unlike every other
    # ramp a higher level here is not reliably a brighter one.
    "rgb": [(72, 0, 128), (0, 64, 224), (0, 176, 200), (0, 216, 72),
            (224, 220, 0), (255, 128, 0), (255, 48, 48)],
    "inferno": [(0, 0, 4), (40, 11, 84), (101, 21, 110), (159, 42, 99),
                (212, 72, 66), (245, 125, 21), (250, 193, 39), (252, 255, 164)],
    "vapor": [(20, 10, 50), (70, 20, 110), (150, 30, 140), (230, 60, 150),
              (255, 120, 190), (140, 230, 255), (235, 245, 255)],
}

FALLBACK = "mono"


def names() -> list[str]:
    """Palette names, sorted -- the UI's combo box order."""
    return sorted(PALETTES)


def ramp(name: str) -> list[RGB]:
    """Look up a ramp, case- and whitespace-insensitively.

    Never raises: a palette name arrives from a JSON config file that may
    predate a rename, and refusing to draw is a worse answer than mono.

    Returns a copy, so a caller that reverses or trims the ramp for a UI
    swatch cannot corrupt `PALETTES` for the rest of the process.
    """
    try:
        key = str(name).strip().lower()
    except Exception:
        key = ""
    return list(PALETTES.get(key, PALETTES[FALLBACK]))


def to_colorref(rgb: RGB) -> int:
    """RGB triple -> COLORREF 0x00BBGGRR, clamped to 0..255 per channel."""
    r, g, b = (0 if c < 0 else 255 if c > 255 else int(c) for c in rgb)
    return (b << 16) | (g << 8) | r


def resolve(name: str, levels: int) -> list[int]:
    """`levels` COLORREFs sampled evenly across the named ramp, dim -> bright.

    `levels == 1` is the 1-bit silhouette case and gets the ramp's brightest
    colour, not its darkest, so a mono render is white on the desktop.

    Multi-level sampling deliberately starts *above* the bottom of the ramp.
    Level 0 is the dimmest band that `boxgen.quantize` still considers
    visible -- the true background is dropped before boxing -- so mapping it
    to the ramp's darkest entry would spend real windows painting black
    rectangles that nobody can see. Sampling over (0, 1] keeps every level
    distinguishable.
    """
    keys = ramp(name)
    n = max(1, int(levels))
    if n == 1:
        return [to_colorref(keys[-1])]

    last = len(keys) - 1
    out: list[int] = []
    for i in range(n):
        t = (i + 1) * last / n
        lo = min(int(t), last)
        hi = min(lo + 1, last)
        f = t - lo
        a, b = keys[lo], keys[hi]
        out.append(to_colorref((
            round(a[0] + (b[0] - a[0]) * f),
            round(a[1] + (b[1] - a[1]) * f),
            round(a[2] + (b[2] - a[2]) * f),
        )))
    return out
