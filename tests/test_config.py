"""Config model, and the fingerprint the bake cache is keyed on.

The fingerprint is the sharp edge here: it must move when the produced
geometry moves and stay still otherwise, or the cache either serves the
wrong bake or throws away good ones every time a slider is touched.
"""
from __future__ import annotations

import json
from dataclasses import fields, replace

import pytest

from timeleap.config import (
    APP_NAME,
    PRESETS,
    AppConfig,
    EffectConfig,
    PlaybackConfig,
    RenderConfig,
    VideoConfig,
    apply_preset,
    cache_dir,
    config_dir,
)

# Every VideoConfig field is by definition part of the geometry, so every one
# of them must move the fingerprint.
GEOMETRY_CHANGES = {
    "grid_w": 97, "grid_h": 55, "auto_grid": False, "max_windows": 121,
    "min_box_area": 3, "algo": "quality", "threshold_mode": "adaptive",
    "fixed_threshold": 100, "adaptive_block": 11, "adaptive_bias": 7,
    "invert": True, "levels": 4, "gamma": 1.4, "contrast": 1.2,
    "brightness": 12, "denoise": False,
}


# ---- fingerprint -----------------------------------------------------
def test_fingerprint_is_stable_and_hex() -> None:
    a, b = VideoConfig(), VideoConfig()
    assert a.fingerprint() == b.fingerprint()
    assert a.fingerprint() == a.fingerprint()
    fp = a.fingerprint()
    assert len(fp) == 24 and int(fp, 16) >= 0


def test_every_video_field_is_covered_by_the_test_matrix() -> None:
    assert {f.name for f in fields(VideoConfig)} == set(GEOMETRY_CHANGES)


@pytest.mark.parametrize("field_name,value", sorted(GEOMETRY_CHANGES.items()))
def test_fingerprint_changes_with_every_geometry_field(field_name: str, value) -> None:
    base = VideoConfig()
    assert getattr(base, field_name) != value, "pick a value that differs"
    changed = replace(base, **{field_name: value})
    assert changed.fingerprint() != base.fingerprint(), field_name


def test_fingerprint_ignores_palette_volume_and_effects() -> None:
    """The whole reason bakes survive: none of these touch the rectangles."""
    cfg = AppConfig()
    before = cfg.video.fingerprint()

    cfg.render.palette = "inferno"
    cfg.render.gap = 3
    cfg.render.monitor = 1
    cfg.render.region = (0, 0, 640, 480)
    cfg.render.redraw = "fast"
    cfg.render.background_blackout = True
    cfg.playback.volume = 3
    cfg.playback.mute = True
    cfg.playback.speed = 2.0
    cfg.playback.reverse = True
    cfg.effects.trails = 8
    cfg.effects.jitter = 4
    cfg.effects.time_warp = 0.7
    cfg.last_dir = r"D:\clips"
    cfg.use_cache = False

    assert cfg.video.fingerprint() == before


def test_fingerprint_survives_a_json_round_trip() -> None:
    """A bake written today must still be found after the config is reloaded."""
    cfg = AppConfig()
    cfg.video.grid_w, cfg.video.levels, cfg.video.algo = 128, 6, "balanced"
    before = cfg.video.fingerprint()
    reloaded = AppConfig.from_dict(json.loads(json.dumps(cfg.to_dict())))
    assert reloaded.video.fingerprint() == before


def test_fingerprint_does_not_collide_across_the_presets() -> None:
    seen = {}
    for name in PRESETS:
        cfg = AppConfig()
        apply_preset(cfg, name)
        seen.setdefault(cfg.video.fingerprint(), []).append(name)
    # "Bad Apple" and "Performance" differ in grid size, so all six presets
    # that change video geometry must hash apart.
    for fp, names in seen.items():
        assert len(names) == 1, f"{names} share fingerprint {fp}"


# ---- JSON round trip -------------------------------------------------
def test_default_config_round_trips_through_json() -> None:
    cfg = AppConfig()
    assert AppConfig.from_dict(json.loads(json.dumps(cfg.to_dict()))) == cfg


def test_fully_populated_config_round_trips_through_json() -> None:
    cfg = AppConfig(
        video=VideoConfig(grid_w=160, grid_h=90, levels=7, algo="quality",
                          threshold_mode="edge", invert=True, gamma=1.8),
        render=RenderConfig(palette="vapor", monitor=1, region=(10, 20, 300, 400),
                            gap=2, redraw="fast", topmost=False),
        playback=PlaybackConfig(speed=1.75, loop="pingpong", reverse=True,
                                volume=3, audio_backend="ffplay"),
        effects=EffectConfig(trails=4, echo_offset=9, ghost=0.6, slitscan=5,
                             jitter=2, strobe=7, shuffle=11, time_warp=0.25),
        last_dir=r"C:\videos", use_cache=False, show_stats=False,
        panic_hotkey=False,
    )
    back = AppConfig.from_dict(json.loads(json.dumps(cfg.to_dict())))
    assert back == cfg


def test_region_survives_json_as_a_tuple_not_a_list() -> None:
    """JSON has no tuples, and a list here would break `==` and the renderer."""
    cfg = AppConfig()
    cfg.render.region = (200, 150, 960, 540)
    back = AppConfig.from_dict(json.loads(json.dumps(cfg.to_dict())))
    assert isinstance(back.render.region, tuple)
    assert back.render.region == (200, 150, 960, 540)


def test_region_none_stays_none() -> None:
    back = AppConfig.from_dict({"render": {"region": None}})
    assert back.render.region is None


# ---- tolerant loading ------------------------------------------------
def test_unknown_keys_are_ignored() -> None:
    cfg = AppConfig.from_dict({
        "video": {"grid_w": 11, "bogus": 1, "algo": "quality"},
        "render": {"palette": "fire", "nonsense": [1, 2]},
        "unknown_section": {"x": 1},
        "use_cache": False,
        "not_a_field": True,
    })
    assert cfg.video.grid_w == 11 and cfg.video.algo == "quality"
    assert cfg.render.palette == "fire"
    assert cfg.use_cache is False
    assert not hasattr(cfg.video, "bogus")
    assert not hasattr(cfg, "not_a_field")


@pytest.mark.parametrize("raw", [{}, {"video": None}, {"video": {}, "render": None}])
def test_missing_sections_fall_back_to_defaults(raw: dict) -> None:
    assert AppConfig.from_dict(raw) == AppConfig()


def test_load_returns_defaults_when_the_file_is_absent_or_broken(cache_home) -> None:
    assert AppConfig.load() == AppConfig()
    AppConfig.path().write_text("{ not json", "utf-8")
    assert AppConfig.load() == AppConfig()


def test_save_then_load_round_trips_on_disk(cache_home) -> None:
    cfg = AppConfig()
    cfg.video.grid_w = 200
    cfg.render.palette = "matrix"
    cfg.render.region = (1, 2, 3, 4)
    cfg.playback.volume = 4
    cfg.effects.trails = 3
    cfg.save()

    assert AppConfig.path().is_file()
    assert AppConfig.path().parent == cache_home / APP_NAME
    assert AppConfig.load() == cfg
    # The temp file used for the atomic replace must not be left behind.
    assert not AppConfig.path().with_suffix(".tmp").exists()


def test_save_never_raises_even_when_the_target_is_unwritable(
        cache_home, monkeypatch) -> None:
    """Settings are a convenience; a failure must not take playback down."""
    def boom(*_a, **_k):
        raise OSError("disk on fire")

    monkeypatch.setattr("pathlib.Path.write_text", boom)
    AppConfig().save()          # must not raise


# ---- directories -----------------------------------------------------
def test_config_and_cache_directories_are_created(cache_home) -> None:
    assert config_dir().is_dir()
    assert cache_dir().is_dir()
    assert cache_dir().parent == config_dir()


# ---- presets ---------------------------------------------------------
@pytest.mark.parametrize("name", sorted(PRESETS))
def test_every_preset_applies_only_known_fields(name: str) -> None:
    cfg = AppConfig()
    apply_preset(cfg, name)
    for section, values in PRESETS[name].items():
        obj = getattr(cfg, section)
        known = {f.name for f in fields(obj)}
        for key, value in values.items():
            assert key in known, f"{name}.{section}.{key} is not a config field"
            assert getattr(obj, key) == value


def test_preset_changes_what_it_claims() -> None:
    cfg = AppConfig()
    apply_preset(cfg, "Matrix")
    assert (cfg.video.grid_w, cfg.video.grid_h) == (128, 72)
    assert cfg.video.levels == 4
    assert cfg.render.palette == "matrix" and cfg.render.gap == 1


def test_time_leap_preset_turns_effects_on() -> None:
    cfg = AppConfig()
    assert not cfg.effects.active()
    apply_preset(cfg, "Time leap")
    assert cfg.effects.active()
    assert cfg.effects.trails == 2 and cfg.effects.echo_offset == 6


def test_unknown_preset_is_a_no_op() -> None:
    cfg = AppConfig()
    apply_preset(cfg, "does not exist")
    assert cfg == AppConfig()


def test_presets_do_not_leak_between_configs() -> None:
    a, b = AppConfig(), AppConfig()
    apply_preset(a, "High detail")
    assert b == AppConfig(), "a preset mutated a shared default"


# ---- EffectConfig.active ---------------------------------------------
def test_effects_are_all_off_by_default() -> None:
    cfg = EffectConfig()
    assert not cfg.active()
    for f in fields(cfg):
        if f.name == "warp_period":
            continue
        assert not getattr(cfg, f.name), f.name


@pytest.mark.parametrize("field_name,value", [
    ("trails", 1), ("echo_offset", 3), ("ghost", 0.5), ("slitscan", 4),
    ("jitter", 2), ("strobe", 5), ("shuffle", 6), ("time_warp", 0.1),
])
def test_active_is_true_for_any_single_effect(field_name: str, value) -> None:
    assert EffectConfig(**{field_name: value}).active()


def test_warp_period_alone_does_not_count_as_active() -> None:
    """It is a parameter of `time_warp`, not an effect of its own."""
    assert not EffectConfig(warp_period=1.5).active()
