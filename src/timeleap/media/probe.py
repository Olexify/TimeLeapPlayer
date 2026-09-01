"""ffprobe wrapper: a media file -> `MediaInfo`.

Grid sizing, the clock, the seek bar and the bake header all need fps,
duration and a frame count *before* a single frame is decoded -- and
containers lie about every one of them. mkv/webm carry no `nb_frames`,
phone footage hides its orientation in a display matrix, VFR streams have
no single frame rate, and `r_frame_rate` is occasionally nonsense like
`1000/1`. This module always returns numbers a caller can divide by, and
flags the ones it had to guess instead of reporting a confident zero.
"""
from __future__ import annotations

import json
import shutil
import subprocess
import sys
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

DEFAULT_FPS = 24.0
MAX_PLAUSIBLE_FPS = 1000.0
PROBE_TIMEOUT = 30.0

# Console windows would otherwise flash on every probe.
CREATE_NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0


class MediaError(RuntimeError):
    """ffprobe/ffmpeg could not make sense of the file."""


@dataclass
class MediaInfo:
    path: str
    width: int
    height: int
    fps: float
    duration: float
    frame_count: int
    has_audio: bool
    codec: str
    rotation: int
    size_bytes: int
    fps_estimated: bool = False        # container gave no usable rate
    frames_estimated: bool = False     # frame_count is duration * fps

    @property
    def aspect(self) -> float:
        """Display aspect ratio, i.e. after rotation.

        ffmpeg auto-rotates on decode, so a 1920x1080 portrait clip with a
        90 degree display matrix reaches the decoder as 1080x1920. Auto-grid
        must size against that, not against the stored dimensions.
        """
        w, h = (self.height, self.width) if self.rotation in (90, 270) else (self.width, self.height)
        return (w / h) if h else 0.0


def _binary_ok(name: str) -> bool:
    if shutil.which(name) is None:
        return False
    try:
        proc = subprocess.run([name, "-version"], capture_output=True, timeout=10,
                              stdin=subprocess.DEVNULL,
                              creationflags=CREATE_NO_WINDOW)
    except (OSError, subprocess.SubprocessError):
        return False
    return proc.returncode == 0


@lru_cache(maxsize=1)
def have_ffmpeg() -> tuple[bool, bool, bool]:
    """(ffmpeg, ffprobe, ffplay) availability.

    Cached because three process spawns cost ~100 ms and the UI wants to
    know on every file open, every backend switch and every stat refresh.
    """
    return (_binary_ok("ffmpeg"), _binary_ok("ffprobe"), _binary_ok("ffplay"))


def _rational(value: Any) -> float:
    """ffprobe rates are rationals: "24000/1001". "0/0" means unknown."""
    if isinstance(value, str) and "/" in value:
        num, _, den = value.partition("/")
        try:
            n, d = float(num), float(den)
        except ValueError:
            return 0.0
        return n / d if d else 0.0
    return _number(value)


def _number(value: Any) -> float:
    try:
        out = float(value)
    except (TypeError, ValueError):
        return 0.0
    return out if out == out and abs(out) != float("inf") else 0.0


def _integer(value: Any) -> int:
    return int(_number(value))


def _rotation(stream: dict[str, Any]) -> int:
    """Clockwise display rotation, normalised to 0..359.

    Two spellings that disagree by a sign: the Display Matrix side datum
    reports counter-clockwise degrees (a portrait phone clip reads -90),
    while the legacy mov/mp4 `tags:rotate` is already clockwise. Negating
    the first is what makes both containers report the same orientation.
    """
    for side in stream.get("side_data_list") or ():
        if isinstance(side, dict) and side.get("rotation") is not None:
            return int(round(-_number(side["rotation"]))) % 360
    tags = stream.get("tags") or {}
    for key in ("rotate", "ROTATE"):
        if tags.get(key) is not None:
            return int(round(_number(tags[key]))) % 360
    return 0


def _ffprobe_json(path: str) -> dict[str, Any]:
    cmd = ["ffprobe", "-v", "error", "-print_format", "json",
           "-show_streams", "-show_format", path]
    try:
        # ffprobe rejects ffmpeg's -nostdin, so detach stdin the other way:
        # inheriting a console handle lets it swallow keystrokes meant for us.
        proc = subprocess.run(cmd, capture_output=True, timeout=PROBE_TIMEOUT,
                              stdin=subprocess.DEVNULL,
                              creationflags=CREATE_NO_WINDOW)
    except FileNotFoundError as exc:
        raise MediaError("ffprobe not found on PATH") from exc
    except subprocess.TimeoutExpired as exc:
        raise MediaError(f"ffprobe timed out on {path}") from exc
    except OSError as exc:
        raise MediaError(f"ffprobe failed to start: {exc}") from exc
    if proc.returncode != 0:
        detail = proc.stderr.decode("utf-8", "replace").strip().splitlines()
        raise MediaError(f"ffprobe failed on {path}: {detail[-1] if detail else 'unknown error'}")
    try:
        raw = json.loads(proc.stdout.decode("utf-8", "replace") or "{}")
    except json.JSONDecodeError as exc:
        raise MediaError(f"ffprobe returned unreadable JSON for {path}") from exc
    if not isinstance(raw, dict):
        raise MediaError(f"ffprobe returned unexpected JSON for {path}")
    return raw


def _pick_video(streams: list[dict[str, Any]]) -> dict[str, Any] | None:
    """First real video stream; cover art is a video stream too."""
    video = [s for s in streams if s.get("codec_type") == "video"]
    real = [s for s in video if not (s.get("disposition") or {}).get("attached_pic")]
    return (real or video or [None])[0]


def probe(path: str) -> MediaInfo:
    """Inspect `path` with ffprobe. Raises `MediaError` if it is not playable."""
    src = Path(path)
    if not src.is_file():
        raise MediaError(f"No such file: {path}")

    raw = _ffprobe_json(str(src))
    streams = [s for s in (raw.get("streams") or []) if isinstance(s, dict)]
    video = _pick_video(streams)
    if video is None:
        raise MediaError(f"No video stream in {path}")
    fmt = raw.get("format") or {}

    # r_frame_rate is the container's declared rate and is what ffmpeg's own
    # CFR output follows, so it matches what the decoder will actually emit;
    # avg_frame_rate only rescues the odd stream that declares 0/0 or 1000/1.
    fps = _rational(video.get("r_frame_rate"))
    if not 0.0 < fps <= MAX_PLAUSIBLE_FPS:
        fps = _rational(video.get("avg_frame_rate"))
    fps_estimated = not 0.0 < fps <= MAX_PLAUSIBLE_FPS
    if fps_estimated:
        fps = DEFAULT_FPS

    duration = _number(video.get("duration")) or _number(fmt.get("duration"))

    tags = video.get("tags") or {}
    frame_count = _integer(video.get("nb_frames"))
    if frame_count <= 0:
        # Matroska/WebM omit nb_frames; ffmpeg's own mkv writer leaves this tag.
        frame_count = _integer(tags.get("NUMBER_OF_FRAMES") or tags.get("NUMBER_OF_FRAMES-eng"))
    frames_estimated = frame_count <= 0
    if frames_estimated and duration > 0:
        frame_count = int(round(duration * fps))
    frame_count = max(0, frame_count)

    if duration <= 0 and frame_count > 0:
        duration = frame_count / fps

    size_bytes = _integer(fmt.get("size"))
    if size_bytes <= 0:
        size_bytes = src.stat().st_size

    return MediaInfo(
        path=str(src),
        width=_integer(video.get("width")),
        height=_integer(video.get("height")),
        fps=float(fps),
        duration=float(duration),
        frame_count=frame_count,
        has_audio=any(s.get("codec_type") == "audio" for s in streams),
        codec=str(video.get("codec_name") or "unknown"),
        rotation=_rotation(video),
        size_bytes=size_bytes,
        fps_estimated=fps_estimated,
        frames_estimated=frames_estimated,
    )
