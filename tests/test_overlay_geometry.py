"""The player window's geometry: resize with aspect lock, defaults, clamping."""
from __future__ import annotations

import pytest

from timeleap.ui.overlay import (default_rect, edge_at, fit_aspect, geometry,
                                 keep_visible, resize_rect)

A = 16 / 9


def test_fit_aspect_keeps_width_and_centre():
    x, y, w, h = fit_aspect((100, 100, 1600, 400), A)
    assert (x, w, h) == (100, 1600, 900)
    assert y + h / 2 == pytest.approx(100 + 400 / 2, abs=1)


@pytest.mark.parametrize("spec,dx,dy", [("e", 160, 0), ("w", -160, 0),
                                        ("s", 0, 90), ("n", 0, -90),
                                        ("se", 160, 90), ("nw", -160, -90)])
def test_resize_grows_and_keeps_the_aspect(spec, dx, dy):
    start = (1000, 500, 1600, 900)
    x, y, w, h = resize_rect(start, spec, dx, dy, A, 240)
    assert w == 1760
    assert w / h == pytest.approx(A, abs=0.01)
    if "w" in spec:
        assert x + w == 1000 + 1600            # right edge is the anchor
    elif "e" in spec:
        assert x == 1000                       # left edge is the anchor
    else:
        assert x + w / 2 == pytest.approx(1000 + 800, abs=1)
    if "n" in spec:
        assert y + h == 500 + 900              # bottom edge is the anchor
    elif "s" in spec:
        assert y == 500


def test_resize_never_goes_below_the_minimum_width():
    assert resize_rect((0, 0, 400, 225), "e", -10_000, 0, A, 240)[2] == 240


def test_default_rect_is_centred_at_the_aspect():
    x, y, w, h = default_rect((0, 0, 2560, 1440), A)
    assert (w, h) == (1280, 720)
    assert (x, y) == (640, 360)


def test_default_rect_clamps_a_tall_video_to_the_area():
    _x, _y, w, h = default_rect((0, 0, 1920, 1080), 9 / 16)
    assert h <= 1080 * 0.8
    assert w / h == pytest.approx(9 / 16, abs=0.01)


def test_keep_visible_pulls_a_lost_window_back():
    bounds = (0, 0, 2560, 1440)
    x, y, w, _h = keep_visible((5000, -400, 800, 450), bounds, margin=64)
    assert x == 2560 - 64 and y == 0
    x, _y, w, _h = keep_visible((-5000, 100, 800, 450), bounds, margin=64)
    assert x + w == 64


def test_edge_at_corners_edges_and_interior():
    assert edge_at(2, 2, 800, 450, 8) == "nw"
    assert edge_at(799, 449, 800, 450, 8) == "se"
    assert edge_at(400, 2, 800, 450, 8) == "n"
    assert edge_at(797, 200, 800, 450, 8) == "e"
    assert edge_at(400, 200, 800, 450, 8) == ""


def test_geometry_spells_negative_offsets_absolutely():
    # "-100" would mean 100 px from the right edge in Tk.
    assert geometry((-100, -50, 640, 360)) == "640x360+-100+-50"
