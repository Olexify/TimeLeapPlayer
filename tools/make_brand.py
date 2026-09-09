"""Generate every TimeLeapPlayer brand asset from one definition.

The mark is a play head assembled from horizontal bars, because that is what
the renderer actually does: each frame is decomposed into runs of cells and
rebuilt from a few dozen rectangles. Keeping the geometry in one place here
means the SVG, the PNGs, the Windows .ico and the social card can never drift
apart, and any of them can be regenerated after a tweak.

    python tools/make_brand.py

Writes to assets/ (branding, for docs and GitHub) and
src/timeleap/assets/ (the runtime .ico, shipped inside the package).
"""
from __future__ import annotations

import os
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

ROOT = Path(__file__).resolve().parent.parent
BRAND = ROOT / "assets"
RUNTIME = ROOT / "src" / "timeleap" / "assets"

BG = (20, 22, 27)          # matches the app background
EDGE = (44, 52, 66)
ACC = (76, 194, 255)       # app accent
HI = (232, 245, 255)
MUTED = (141, 150, 168)

BANDS = 7
ICO_SIZES = [256, 128, 64, 48, 32, 16]
PNG_SIZES = [512, 256, 128, 64, 32, 16]


def bars(bands: int, small: bool) -> list[tuple[float, float, float, float, tuple[int, int, int]]]:
    """The mark's geometry in a 0..1 square: (x0, y0, x1, y1, colour).

    Shared by every renderer so the SVG and the bitmaps are the same drawing.
    """
    pad = 0.14 if small else 0.22
    left = 0.26 if small else 0.30
    apex = 0.82 if small else 0.76
    gap = 0.016 if small else 0.012
    top, bot = pad, 1.0 - pad
    h = (bot - top) / bands

    out = []
    for i in range(bands):
        y0 = top + i * h
        y1 = y0 + h - gap
        t = abs((i + 0.5) / bands - 0.5) * 2      # 0 at the middle band
        x1 = left + (apex - left) * (1.0 - t)
        if x1 - left < 0.05:
            continue
        f = 1.0 - t                                # brighter toward the tip
        col = tuple(int(ACC[c] + (HI[c] - ACC[c]) * f) for c in range(3))
        out.append((left, y0, x1, y1, col))
    return out


def render(size: int, *, plate: bool = True, supersample: int = 8) -> Image.Image:
    """The mark at `size` px. `plate` draws the rounded background."""
    small = size <= 24
    S = size * supersample
    img = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    if plate:
        radius = int(S * (0.14 if small else 0.20))
        if small:
            d.rounded_rectangle([0, 0, S - 1, S - 1], radius=radius, fill=BG)
        else:
            d.rounded_rectangle([0, 0, S - 1, S - 1], radius=radius, fill=BG,
                                outline=EDGE, width=max(1, S // 64))
    for x0, y0, x1, y1, col in bars(BANDS if size >= 32 else 5, small):
        d.rectangle([x0 * S, y0 * S, x1 * S, y1 * S], fill=col + (255,))
    return img.resize((size, size), Image.LANCZOS)


def svg() -> str:
    """Vector version, for anywhere that can scale it properly."""
    lines = [
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 512 512" '
        'width="512" height="512" role="img" aria-label="TimeLeapPlayer">',
        '  <title>TimeLeapPlayer</title>',
        f'  <rect width="512" height="512" rx="102" fill="#{BG[0]:02x}{BG[1]:02x}{BG[2]:02x}"/>',
        f'  <rect x="4" y="4" width="504" height="504" rx="99" fill="none" '
        f'stroke="#{EDGE[0]:02x}{EDGE[1]:02x}{EDGE[2]:02x}" stroke-width="8"/>',
    ]
    for x0, y0, x1, y1, col in bars(BANDS, small=False):
        lines.append(
            f'  <rect x="{x0 * 512:.1f}" y="{y0 * 512:.1f}" '
            f'width="{(x1 - x0) * 512:.1f}" height="{(y1 - y0) * 512:.1f}" '
            f'rx="3" fill="#{col[0]:02x}{col[1]:02x}{col[2]:02x}"/>')
    lines.append('</svg>')
    return "\n".join(lines) + "\n"


def font(size: int, bold: bool = False):
    """Segoe UI where it exists, otherwise whatever Pillow can find."""
    for name in (("segoeuib.ttf", "arialbd.ttf") if bold else ("segoeui.ttf", "arial.ttf")):
        path = Path(os.environ.get("WINDIR", "C:/Windows")) / "Fonts" / name
        if path.is_file():
            return ImageFont.truetype(str(path), size)
    try:
        return ImageFont.truetype("DejaVuSans-Bold.ttf" if bold else "DejaVuSans.ttf", size)
    except OSError:
        return ImageFont.load_default()


def wordmark(dark: bool, width: int = 1200, height: int = 300) -> Image.Image:
    """Icon plus name, for the top of the README.

    Two variants exist because a README is rendered on white in GitHub's
    light theme and near-black in its dark theme; one near-white wordmark on
    a transparent background disappears entirely on the former. The icon
    plate carries its own background, so it is the same in both.
    """
    ink = HI if dark else (27, 30, 37)
    sub = MUTED if dark else (85, 96, 122)
    img = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    mark = render(200)
    img.paste(mark, (24, (height - 200) // 2), mark)
    x = 24 + 200 + 36
    d.text((x, height // 2 - 62), "TimeLeapPlayer", font=font(76, bold=True), fill=ink)
    d.text((x + 4, height // 2 + 26),
           "Play video using Windows windows as pixels",
           font=font(30), fill=sub)
    return img


def social(width: int = 1280, height: int = 640) -> Image.Image:
    """GitHub social preview card (og:image), 2:1 as GitHub expects."""
    img = Image.new("RGB", (width, height), BG)
    d = ImageDraw.Draw(img)

    # A faint field of bars behind the content: the same decomposition idea,
    # scattered, so the card reads as "made of rectangles" at a glance.
    import random
    rng = random.Random(7)
    for _ in range(90):
        bw = rng.randint(40, 220)
        bh = rng.randint(8, 20)
        bx = rng.randint(-40, width)
        by = rng.randint(-20, height)
        shade = rng.randint(26, 42)
        d.rectangle([bx, by, bx + bw, by + bh], fill=(shade, shade + 3, shade + 9))

    mark = render(256)
    img.paste(mark, (96, (height - 256) // 2 - 24), mark)
    x = 96 + 256 + 56
    d.text((x, height // 2 - 118), "TimeLeapPlayer", font=font(84, bold=True), fill=HI)
    d.text((x + 4, height // 2 - 18),
           "Play any video on your desktop", font=font(38), fill=ACC)
    d.text((x + 4, height // 2 + 34),
           "using real OS windows as pixels", font=font(38), fill=ACC)
    d.text((x + 4, height // 2 + 108),
           "github.com/Olexify/TimeLeapPlayer", font=font(28), fill=MUTED)
    return img


def main() -> None:
    BRAND.mkdir(parents=True, exist_ok=True)
    RUNTIME.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []

    ico = [render(s) for s in ICO_SIZES]
    for dest in (RUNTIME / "timeleap.ico", BRAND / "icon.ico"):
        ico[0].save(dest, format="ICO", sizes=[(s, s) for s in ICO_SIZES],
                    append_images=ico[1:])
        written.append(dest)

    (BRAND / "icon.svg").write_text(svg(), encoding="utf-8")
    written.append(BRAND / "icon.svg")

    for s in PNG_SIZES:
        dest = BRAND / f"icon-{s}.png"
        render(s).save(dest)
        written.append(dest)

    for name, img in (("logo-dark.png", wordmark(dark=True)),
                      ("logo-light.png", wordmark(dark=False)),
                      ("social-preview.png", social())):
        dest = BRAND / name
        img.save(dest)
        written.append(dest)

    for p in written:
        print(f"  {p.relative_to(ROOT).as_posix():44} {p.stat().st_size:>8,} bytes")


if __name__ == "__main__":
    main()
