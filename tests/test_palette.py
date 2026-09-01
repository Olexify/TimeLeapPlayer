"""Palette resolution, and the byte order that silently ruins a render.

COLORREF is 0x00BBGGRR. Getting it backwards never raises -- amber simply
comes out blue -- so the swap is asserted here in both directions and for
every ramp the UI can offer.
"""
from __future__ import annotations

import pytest

from timeleap.core import palette as P


def luma(colorref: int) -> float:
    """Rec.601 brightness of a COLORREF, i.e. after the BGR unpack."""
    r = colorref & 0xFF
    g = (colorref >> 8) & 0xFF
    b = (colorref >> 16) & 0xFF
    return 0.299 * r + 0.587 * g + 0.114 * b


# ---- byte order ------------------------------------------------------
def test_colorref_is_bgr_not_rgb() -> None:
    assert P.to_colorref((255, 0, 0)) == 0x0000FF, "red must land in the low byte"
    assert P.to_colorref((0, 255, 0)) == 0x00FF00
    assert P.to_colorref((0, 0, 255)) == 0xFF0000, "blue must land in the high byte"
    assert P.to_colorref((0, 0, 0)) == 0x000000
    assert P.to_colorref((255, 255, 255)) == 0x00FFFFFF


def test_colorref_clamps_out_of_range_channels() -> None:
    assert P.to_colorref((300, -20, 128)) == 0x8000FF
    assert P.to_colorref((1.9, 2.2, 3.7)) == P.to_colorref((1, 2, 3))


def test_resolved_colours_fit_a_colorref() -> None:
    for name in P.names():
        for levels in range(1, 17):
            for c in P.resolve(name, levels):
                assert isinstance(c, int)
                assert 0 <= c <= 0x00FFFFFF


# ---- names and lookup ------------------------------------------------
def test_names_are_sorted_and_complete() -> None:
    assert P.names() == sorted(P.PALETTES)
    assert set(P.names()) >= {"mono", "amber", "matrix", "ice", "fire", "rgb",
                              "inferno", "vapor"}


@pytest.mark.parametrize("name", P.names())
@pytest.mark.parametrize("levels", [1, 2, 3, 4, 5, 8, 12, 16])
def test_every_name_resolves_to_exactly_levels_colours(name: str, levels: int) -> None:
    out = P.resolve(name, levels)
    assert len(out) == levels


@pytest.mark.parametrize("bogus", ["nope", "", "  ", "Mono2", None, 42, object()])
def test_unknown_names_fall_back_to_mono(bogus) -> None:
    assert P.resolve(bogus, 4) == P.resolve("mono", 4)
    assert P.ramp(bogus) == P.PALETTES[P.FALLBACK]


def test_lookup_is_case_and_whitespace_insensitive() -> None:
    assert P.ramp("  MaTrIx ") == P.PALETTES["matrix"]
    assert P.resolve(" ICE ", 5) == P.resolve("ice", 5)


def test_ramp_returns_a_private_copy() -> None:
    """A UI swatch that reverses the ramp must not corrupt the process."""
    got = P.ramp("mono")
    assert got is not P.PALETTES["mono"]
    got.reverse()
    assert P.PALETTES["mono"][0] == (0, 0, 0)


# ---- level mapping ---------------------------------------------------
@pytest.mark.parametrize("name", P.names())
def test_one_level_is_the_brightest_entry(name: str) -> None:
    """A 1-bit silhouette must be white on the desktop, not black."""
    out = P.resolve(name, 1)
    assert out == [P.to_colorref(P.PALETTES[name][-1])]


def test_mono_at_one_level_is_pure_white() -> None:
    assert P.resolve("mono", 1) == [0x00FFFFFF]


@pytest.mark.parametrize("levels", [0, -1, -99])
def test_non_positive_levels_degrade_to_one(levels: int) -> None:
    assert P.resolve("mono", levels) == P.resolve("mono", 1)


@pytest.mark.parametrize("name", [n for n in P.names() if n != "rgb"])
@pytest.mark.parametrize("levels", [2, 3, 5, 8, 16])
def test_ramps_get_brighter_with_the_level(name: str, levels: int) -> None:
    values = [luma(c) for c in P.resolve(name, levels)]
    assert all(b > a for a, b in zip(values, values[1:])), values


@pytest.mark.parametrize("levels", [2, 5, 16])
def test_rgb_is_the_documented_exception_to_monotone_brightness(levels: int) -> None:
    """`rgb` carries the level as hue, so brightness is deliberately not
    monotone -- asserting it keeps the docstring honest."""
    values = [luma(c) for c in P.resolve("rgb", levels)]
    assert any(b <= a for a, b in zip(values, values[1:]))


@pytest.mark.parametrize("name", P.names())
def test_multi_level_never_returns_the_darkest_ramp_entry(name: str) -> None:
    """Level 0 is the dimmest *visible* band, so spending a window on the
    ramp's black end would paint rectangles nobody can see."""
    darkest = P.to_colorref(P.PALETTES[name][0])
    for levels in (2, 4, 8):
        assert darkest not in P.resolve(name, levels), (name, levels)


@pytest.mark.parametrize("name", P.names())
def test_top_level_is_always_the_ramp_top(name: str) -> None:
    top = P.to_colorref(P.PALETTES[name][-1])
    for levels in (1, 2, 5, 16):
        assert P.resolve(name, levels)[-1] == top


def test_levels_are_distinguishable() -> None:
    """Two bands resolving to the same colour would waste a window."""
    for name in P.names():
        for levels in (2, 3, 4, 5, 6):
            out = P.resolve(name, levels)
            assert len(set(out)) == levels, (name, levels, [hex(c) for c in out])


def test_resolution_is_deterministic() -> None:
    assert P.resolve("inferno", 7) == P.resolve("inferno", 7)


def test_every_declared_ramp_is_usable() -> None:
    for name, keys in P.PALETTES.items():
        assert len(keys) >= 2, name
        for rgb in keys:
            assert len(rgb) == 3
            assert all(0 <= c <= 255 for c in rgb), (name, rgb)
