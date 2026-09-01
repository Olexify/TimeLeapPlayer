"""Configuration model for TimeLeapPlayer.

Every tunable lives here as a dataclass field so the UI, the CLI and the
cache layer all speak the same vocabulary. `AppConfig` round-trips to JSON
in %LOCALAPPDATA%/TimeLeapPlayer/config.json.

`VideoConfig.fingerprint()` matters: the bake cache is keyed on the hash of
the settings that actually change the produced geometry, so tweaking volume
or the palette never invalidates a bake, while changing the grid does.
"""
from __future__ import annotations

import hashlib
import json
import os
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path
from typing import Any, Literal

APP_NAME = "TimeLeapPlayer"

# Absolute ceiling on windows drawn in one frame, after effects have had their
# say. Effects such as trails deliberately multiply the box count -- trails=3
# turns ~49 boxes into ~197 -- and each window costs roughly 1.4 ms of Win32
# time, so an unbounded setting can drive the frame time past a second and
# exhaust the desktop heap, which degrades the whole Windows session rather
# than just this process. `max_windows` budgets the decomposition; this budgets
# the screen.
HARD_WINDOW_CEILING = 600

BoxAlgo = Literal["fast", "balanced", "quality"]
ThresholdMode = Literal["otsu", "fixed", "adaptive", "edge"]
LoopMode = Literal["off", "loop", "pingpong"]
FitMode = Literal["contain", "cover", "stretch"]


def config_dir() -> Path:
    base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~/.config")
    p = Path(base) / APP_NAME
    p.mkdir(parents=True, exist_ok=True)
    return p


def cache_dir() -> Path:
    p = config_dir() / "cache"
    p.mkdir(parents=True, exist_ok=True)
    return p


@dataclass
class VideoConfig:
    """Decode + geometry settings. These form the bake fingerprint."""

    # Throughput is set by boxes per frame, not grid size: each window costs
    # roughly 1.4 ms of Win32 time, so ~24 boxes is the ceiling for a 30 fps
    # source. Measured on this machine, 64x36 yields ~19 boxes and holds
    # 33.7 fps, while the previous 96x54 default yielded ~31 and managed only
    # 19.9 -- it dropped frames on everything. Detail is one preset away, and
    # the stats strip shows the cost live.
    grid_w: int = 64
    grid_h: int = 36
    auto_grid: bool = True          # derive grid_h from the source aspect ratio
    max_windows: int = 90
    min_box_area: int = 1
    algo: BoxAlgo = "fast"
    threshold_mode: ThresholdMode = "otsu"
    fixed_threshold: int = 128
    adaptive_block: int = 15        # odd; used by threshold_mode="adaptive"
    adaptive_bias: int = 4
    invert: bool = False
    levels: int = 1                 # 1 = 1-bit silhouette, 2..16 = luminance bands
    gamma: float = 1.0
    contrast: float = 1.0
    brightness: int = 0
    denoise: bool = True            # suppress single-cell speckle before boxing

    def fingerprint(self) -> str:
        """Stable hash of everything that changes the produced rectangles."""
        payload = {f.name: getattr(self, f.name) for f in fields(self)}
        blob = json.dumps(payload, sort_keys=True).encode()
        return hashlib.blake2b(blob, digest_size=12).hexdigest()


@dataclass
class RenderConfig:
    """How the rectangles reach the screen. Never part of the fingerprint."""

    palette: str = "mono"
    monitor: int = -1               # -1 = span the whole virtual desktop
    region: tuple | None = None     # explicit (x, y, w, h) in virtual-screen px
    fit: FitMode = "contain"
    gap: int = 0                    # deflate each window by N px for a grid look
    min_window_px: int = 2
    click_through: bool = True      # WS_EX_TRANSPARENT so the desktop stays usable
    no_activate: bool = True        # WS_EX_NOACTIVATE so windows never steal focus
    topmost: bool = True
    # Screenshot-verified against the expected rectangle mask: "accurate"
    # renders 100.0% of pixels correctly, "fast" adds SWP_NOREDRAW for roughly
    # 1.4x the throughput but only 85% correct pixels, because nothing repaints
    # the area a window vacates. The original project recommends SWP_NOREDRAW
    # to avoid tearing -- it in fact causes the smearing. Hand-invalidating the
    # vacated region was tried and is slower than "fast" while no more
    # accurate, so it is not offered.
    redraw: Literal["accurate", "fast"] = "accurate"
    diff_frames: bool = True        # skip DeferWindowPos for unchanged slots
    stable_slots: bool = True       # temporal box->slot matching, kills jitter
    background_blackout: bool = False   # full-screen black window behind the show


@dataclass
class PlaybackConfig:
    speed: float = 1.0
    loop: LoopMode = "loop"
    reverse: bool = False
    mute: bool = False
    volume: int = 80                # 0..100
    audio_backend: Literal["waveout", "ffplay", "none"] = "waveout"
    av_sync: bool = True            # slave video to the audio clock when audible
    max_frame_skip: int = 4         # frames we may drop to catch up
    prefetch_frames: int = 96       # ring-buffer depth ahead of the playhead


@dataclass
class EffectConfig:
    """The 'time leap' half of the name: temporal distortions."""

    trails: int = 0                 # keep N previous frames faintly on screen
    echo_offset: int = 0            # frames of delay for the echo layer
    ghost: float = 0.0              # 0..1 blend of the echo layer
    slitscan: int = 0               # rows sampled from progressively older frames
    jitter: int = 0                 # random px offset per window
    strobe: int = 0                 # blank every Nth frame
    shuffle: int = 0                # randomly reorder box->slot assignment
    time_warp: float = 0.0          # sinusoidal speed modulation depth 0..1
    warp_period: float = 4.0        # seconds per warp cycle

    def active(self) -> bool:
        return any(
            (self.trails, self.echo_offset, self.ghost, self.slitscan,
             self.jitter, self.strobe, self.shuffle, self.time_warp)
        )


@dataclass
class AppConfig:
    video: VideoConfig = field(default_factory=VideoConfig)
    render: RenderConfig = field(default_factory=RenderConfig)
    playback: PlaybackConfig = field(default_factory=PlaybackConfig)
    effects: EffectConfig = field(default_factory=EffectConfig)
    last_dir: str = ""
    use_cache: bool = True
    show_stats: bool = True
    panic_hotkey: bool = True

    # ---- persistence -------------------------------------------------
    @classmethod
    def path(cls) -> Path:
        return config_dir() / "config.json"

    @classmethod
    def load(cls) -> AppConfig:
        try:
            raw = json.loads(cls.path().read_text("utf-8"))
        except Exception:
            return cls()
        return cls.from_dict(raw)

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> AppConfig:
        cfg = cls()
        for section, obj in (("video", cfg.video), ("render", cfg.render),
                             ("playback", cfg.playback), ("effects", cfg.effects)):
            sub = raw.get(section) or {}
            known = {f.name for f in fields(obj)}
            for k, v in sub.items():
                if k in known:
                    if k == "region" and v is not None:
                        v = tuple(v)
                    setattr(obj, k, v)
        for k in ("last_dir", "use_cache", "show_stats", "panic_hotkey"):
            if k in raw:
                setattr(cfg, k, raw[k])
        return cfg

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def save(self) -> None:
        try:
            tmp = self.path().with_suffix(".tmp")
            tmp.write_text(json.dumps(self.to_dict(), indent=2), "utf-8")
            tmp.replace(self.path())
        except Exception:
            pass                     # settings are a convenience, never fatal


# ---- presets ---------------------------------------------------------
PRESETS: dict[str, dict[str, Any]] = {
    "Bad Apple (classic)": {
        "video": {"grid_w": 64, "grid_h": 36, "max_windows": 90, "levels": 1,
                  "algo": "fast", "threshold_mode": "otsu"},
        "render": {"palette": "mono", "gap": 0},
    },
    "High detail": {
        "video": {"grid_w": 160, "grid_h": 90, "max_windows": 260, "levels": 1,
                  "algo": "balanced"},
        "render": {"palette": "mono"},
    },
    "Greyscale bands": {
        "video": {"grid_w": 112, "grid_h": 63, "max_windows": 220, "levels": 5},
        "render": {"palette": "mono"},
    },
    "Matrix": {
        "video": {"grid_w": 128, "grid_h": 72, "max_windows": 200, "levels": 4},
        "render": {"palette": "matrix", "gap": 1},
    },
    "Performance": {
        "video": {"grid_w": 48, "grid_h": 27, "max_windows": 60, "levels": 1,
                  "algo": "fast"},
        "render": {"palette": "mono", "diff_frames": True},
    },
    "Time leap": {
        "video": {"grid_w": 96, "grid_h": 54, "max_windows": 150, "levels": 3},
        "render": {"palette": "ice"},
        "effects": {"trails": 2, "ghost": 0.45, "echo_offset": 6, "time_warp": 0.3},
    },
}


def apply_preset(cfg: AppConfig, name: str) -> None:
    """Switch to a preset.

    Effects are cleared when the preset does not name them. Every other
    section merges, because presets deliberately specify only the few keys
    they care about and should not reset unrelated tuning. Effects are the
    exception: they default to off and are visually dramatic, so leaving the
    previous preset's trails running after picking "Bad Apple (classic)"
    silently gives you neither preset.
    """
    spec = PRESETS.get(name)
    if not spec:
        return
    if "effects" not in spec:
        cfg.effects = EffectConfig()
    for section, values in spec.items():
        obj = getattr(cfg, section)
        for k, v in values.items():
            setattr(obj, k, v)
