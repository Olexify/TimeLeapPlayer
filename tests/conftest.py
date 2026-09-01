"""Shared fixtures for the OS-independent core suite.

Nothing here may touch Win32, Tk or the screen: the whole point of this
suite is that it runs on any machine and in CI, so the only external
dependency is ffmpeg -- and that one is behind `require_ffmpeg`, which
skips instead of failing when the binaries are missing.
"""
from __future__ import annotations

import getpass
import os
import subprocess
import sys
import tempfile
from pathlib import Path

import numpy as np
import pytest

# Importable without the pytest.ini `pythonpath`, so a bare `python -m pytest
# tests/test_clock.py` from anywhere still works.
_SRC = Path(__file__).resolve().parents[1] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))


def _ensure_readable_temp_root() -> None:
    """Redirect pytest's temp root if the default one cannot be scanned.

    `tmp_path` starts by listing `%TEMP%/pytest-of-<user>` to find the last
    run's numbered directory. If an earlier run left that directory behind
    with an ACL this process cannot read -- which happens when the suite was
    once run from a more restricted context -- every `tmp_path` test errors
    out in fixture setup before its body runs, and the fix would otherwise be
    a manual elevated delete. Pointing the root one level deeper is enough
    and costs nothing when the default root is fine.
    """
    if os.environ.get("PYTEST_DEBUG_TEMPROOT"):
        return
    try:
        user = getpass.getuser()
    except Exception:
        return
    default = Path(tempfile.gettempdir()) / f"pytest-of-{user}"
    if not default.exists():
        return
    try:
        next(os.scandir(default), None)
    except OSError:
        fallback = Path(tempfile.gettempdir()) / "timeleap-pytest"
        try:
            fallback.mkdir(parents=True, exist_ok=True)
        except OSError:
            return
        os.environ["PYTEST_DEBUG_TEMPROOT"] = str(fallback)


_ensure_readable_temp_root()

# Hard rule 3: a console window must never flash, not even from a fixture.
CREATE_NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0
_ENCODE_TIMEOUT = 60.0


# ---- ffmpeg gating ---------------------------------------------------
@pytest.fixture(scope="session")
def ffmpeg_tools() -> tuple[bool, bool, bool]:
    from timeleap.media.probe import have_ffmpeg

    return have_ffmpeg()


@pytest.fixture(scope="session")
def require_ffmpeg(ffmpeg_tools: tuple[bool, bool, bool]) -> None:
    ffmpeg, ffprobe, _ = ffmpeg_tools
    if not (ffmpeg and ffprobe):
        pytest.skip("ffmpeg/ffprobe not on PATH")


def _encode(dest: Path, *args: str) -> Path:
    """Run one ffmpeg encode, or skip the test that asked for the clip."""
    cmd = ["ffmpeg", "-nostdin", "-loglevel", "error", "-y", *args, str(dest)]
    try:
        proc = subprocess.run(cmd, capture_output=True, timeout=_ENCODE_TIMEOUT,
                              creationflags=CREATE_NO_WINDOW)
    except (OSError, subprocess.SubprocessError) as exc:
        pytest.skip(f"ffmpeg could not produce {dest.name}: {exc}")
    if proc.returncode != 0 or not dest.is_file():
        pytest.skip(f"ffmpeg failed on {dest.name}: "
                    f"{proc.stderr.decode('utf-8', 'replace')[-200:]}")
    return dest


# Generated clips are session scoped: encoding costs more than every test
# that reads them put together.
@pytest.fixture(scope="session")
def tiny_mp4(require_ffmpeg, tmp_path_factory) -> Path:
    """64x48, 12 fps, exactly 1 s, with an audio track. mp4 carries nb_frames."""
    dest = tmp_path_factory.mktemp("media") / "tiny.mp4"
    return _encode(
        dest,
        "-f", "lavfi", "-i", "testsrc=size=64x48:rate=12:duration=1",
        "-f", "lavfi", "-i", "sine=frequency=440:duration=1",
        "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest",
    )


@pytest.fixture(scope="session")
def tiny_webm(require_ffmpeg, tmp_path_factory) -> Path:
    """Same clip in WebM, which omits nb_frames -- the estimation path."""
    dest = tmp_path_factory.mktemp("media") / "tiny.webm"
    return _encode(
        dest,
        "-f", "lavfi", "-i", "testsrc=size=64x48:rate=12:duration=1",
        "-c:v", "libvpx-vp9", "-b:v", "60k",
    )


@pytest.fixture(scope="session")
def audio_only(require_ffmpeg, tmp_path_factory) -> Path:
    dest = tmp_path_factory.mktemp("media") / "audio.m4a"
    return _encode(dest, "-f", "lavfi", "-i", "sine=frequency=440:duration=1",
                   "-c:a", "aac")


@pytest.fixture
def junk_file(tmp_path: Path) -> Path:
    """Bytes no demuxer can parse -- ffprobe exits non-zero on this."""
    rng = np.random.default_rng(1234)
    dest = tmp_path / "junk.bin"
    dest.write_bytes(rng.integers(0, 256, 50_000, dtype=np.uint8).tobytes())
    return dest


# ---- filesystem isolation --------------------------------------------
@pytest.fixture
def cache_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point `config.config_dir()`/`cache_dir()` at a throwaway directory.

    Both read LOCALAPPDATA on every call, so redirecting the variable is
    enough and no module state has to be reached into.
    """
    home = tmp_path / "localappdata"
    home.mkdir()
    monkeypatch.setenv("LOCALAPPDATA", str(home))

    from timeleap import config

    assert config.config_dir().parent == home
    return home


# ---- box helpers -----------------------------------------------------
@pytest.fixture
def make_boxes():
    """Factory for (N, 5) int32 box arrays from (x, y, w, h[, level]) tuples."""

    def _make(rows) -> np.ndarray:
        rows = list(rows)
        out = np.zeros((len(rows), 5), np.int32)
        for i, r in enumerate(rows):
            out[i, :len(r)] = r
        return out

    return _make


@pytest.fixture
def random_boxes():
    """Factory for pseudo-random boxes that all sit inside a given grid."""

    def _make(n: int, grid_w: int, grid_h: int, levels: int = 1,
              seed: int = 0) -> np.ndarray:
        rng = np.random.default_rng(seed)
        out = np.zeros((n, 5), np.int32)
        out[:, 0] = rng.integers(0, grid_w, n)
        out[:, 1] = rng.integers(0, grid_h, n)
        out[:, 2] = np.minimum(rng.integers(1, 5, n), grid_w - out[:, 0])
        out[:, 3] = np.minimum(rng.integers(1, 5, n), grid_h - out[:, 1])
        out[:, 4] = rng.integers(0, max(1, levels), n)
        return out

    return _make
