"""ffprobe wrapper.

Containers lie about fps, duration and frame count, so the parsing rules get
tested against synthetic ffprobe output (fast, deterministic, covers the
containers this machine cannot produce) and the whole thing gets tested
against real files where ffmpeg is available.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from timeleap.media import probe as P
from timeleap.media.probe import DEFAULT_FPS, MediaError, MediaInfo, probe


def fake_probe(monkeypatch: pytest.MonkeyPatch, payload: dict) -> None:
    monkeypatch.setattr(P, "_ffprobe_json", lambda path: payload)


def video_stream(**over) -> dict:
    base = {
        "codec_type": "video", "codec_name": "h264", "width": 640,
        "height": 360, "r_frame_rate": "30/1", "avg_frame_rate": "30/1",
        "duration": "10.0", "nb_frames": "300",
    }
    base.update(over)
    return base


def payload(streams, fmt=None) -> dict:
    """`fmt={}` means a container that reports nothing, not "use the default"."""
    default = {"duration": "10.0", "size": "1234"}
    return {"streams": streams, "format": default if fmt is None else fmt}


# ---- rational parsing ------------------------------------------------
@pytest.mark.parametrize("raw,expected", [
    ("24000/1001", 24000 / 1001),
    ("30000/1001", 30000 / 1001),
    ("25/1", 25.0),
    ("30", 30.0),
    (25.0, 25.0),
    (25, 25.0),
    ("0/0", 0.0),        # ffprobe's "unknown"
    ("1/0", 0.0),
    ("abc/1", 0.0),
    ("1/abc", 0.0),
    (None, 0.0),
    ("", 0.0),
    ([1, 2], 0.0),
])
def test_rational_parsing(raw, expected: float) -> None:
    assert P._rational(raw) == pytest.approx(expected)


@pytest.mark.parametrize("raw,expected", [
    ("1.5", 1.5), (2, 2.0), (None, 0.0), ("nope", 0.0),
    (float("nan"), 0.0), (float("inf"), 0.0), (float("-inf"), 0.0),
])
def test_number_parsing_never_returns_something_undividable(raw, expected) -> None:
    assert P._number(raw) == expected


def test_ntsc_rate_reaches_probe_intact(tmp_path: Path, monkeypatch) -> None:
    src = tmp_path / "ntsc.mov"
    src.write_bytes(b"x")
    fake_probe(monkeypatch, payload([video_stream(r_frame_rate="24000/1001",
                                                  nb_frames="240")]))
    assert probe(str(src)).fps == pytest.approx(24000 / 1001)


def test_avg_frame_rate_rescues_a_stream_declaring_zero(tmp_path, monkeypatch) -> None:
    src = tmp_path / "a.mkv"
    src.write_bytes(b"x")
    fake_probe(monkeypatch, payload([video_stream(r_frame_rate="0/0",
                                                  avg_frame_rate="25/1")]))
    info = probe(str(src))
    assert info.fps == 25.0 and not info.fps_estimated


def test_an_implausible_rate_is_rejected(tmp_path, monkeypatch) -> None:
    """`r_frame_rate: 1000/1` is a container bug, not a 1000 fps stream."""
    src = tmp_path / "b.mkv"
    src.write_bytes(b"x")
    fake_probe(monkeypatch, payload([video_stream(r_frame_rate="90000/1",
                                                  avg_frame_rate="30000/1001")]))
    assert probe(str(src)).fps == pytest.approx(30000 / 1001)


def test_no_usable_rate_falls_back_and_says_so(tmp_path, monkeypatch) -> None:
    src = tmp_path / "c.mkv"
    src.write_bytes(b"x")
    fake_probe(monkeypatch, payload([video_stream(r_frame_rate="0/0",
                                                  avg_frame_rate="0/0")]))
    info = probe(str(src))
    assert info.fps == DEFAULT_FPS
    assert info.fps_estimated is True


# ---- frame count -----------------------------------------------------
def test_nb_frames_is_used_verbatim(tmp_path, monkeypatch) -> None:
    src = tmp_path / "d.mp4"
    src.write_bytes(b"x")
    fake_probe(monkeypatch, payload([video_stream(nb_frames="297")]))
    info = probe(str(src))
    assert info.frame_count == 297 and info.frames_estimated is False


def test_missing_nb_frames_estimates_from_duration(tmp_path, monkeypatch) -> None:
    """mkv/webm omit nb_frames; reporting 0 would break the seek bar."""
    src = tmp_path / "e.mkv"
    src.write_bytes(b"x")
    stream = video_stream(duration="10.0")
    stream.pop("nb_frames")
    fake_probe(monkeypatch, payload([stream]))
    info = probe(str(src))
    assert info.frame_count == 300
    assert info.frames_estimated is True


def test_the_matroska_frame_count_tag_is_preferred_over_estimating(
        tmp_path, monkeypatch) -> None:
    src = tmp_path / "f.mkv"
    src.write_bytes(b"x")
    stream = video_stream()
    stream.pop("nb_frames")
    stream["tags"] = {"NUMBER_OF_FRAMES": "289"}
    fake_probe(monkeypatch, payload([stream]))
    info = probe(str(src))
    assert info.frame_count == 289 and info.frames_estimated is False


def test_the_language_tagged_frame_count_is_also_read(tmp_path, monkeypatch) -> None:
    src = tmp_path / "g.mkv"
    src.write_bytes(b"x")
    stream = video_stream()
    stream.pop("nb_frames")
    stream["tags"] = {"NUMBER_OF_FRAMES-eng": "111"}
    fake_probe(monkeypatch, payload([stream]))
    assert probe(str(src)).frame_count == 111


def test_no_duration_and_no_count_is_zero_not_a_crash(tmp_path, monkeypatch) -> None:
    src = tmp_path / "h.mkv"
    src.write_bytes(b"x")
    stream = video_stream()
    stream.pop("nb_frames")
    stream.pop("duration")
    fake_probe(monkeypatch, payload([stream], fmt={}))
    info = probe(str(src))
    assert info.frame_count == 0 and info.duration == 0.0
    assert info.frames_estimated is True


def test_duration_is_derived_from_the_frame_count_when_absent(
        tmp_path, monkeypatch) -> None:
    src = tmp_path / "i.mp4"
    src.write_bytes(b"x")
    stream = video_stream(nb_frames="150")
    stream.pop("duration")
    fake_probe(monkeypatch, payload([stream], fmt={}))
    info = probe(str(src))
    assert info.duration == pytest.approx(5.0)


def test_a_negative_frame_count_is_clamped(tmp_path, monkeypatch) -> None:
    src = tmp_path / "j.mp4"
    src.write_bytes(b"x")
    stream = video_stream(nb_frames="-5")
    stream.pop("duration")
    fake_probe(monkeypatch, payload([stream], fmt={}))
    assert probe(str(src)).frame_count >= 0


# ---- streams ---------------------------------------------------------
def test_cover_art_is_not_mistaken_for_the_video(tmp_path, monkeypatch) -> None:
    src = tmp_path / "k.mp4"
    src.write_bytes(b"x")
    art = video_stream(codec_name="mjpeg", width=600, height=600, nb_frames="1")
    art["disposition"] = {"attached_pic": 1}
    fake_probe(monkeypatch, payload([art, video_stream()]))
    info = probe(str(src))
    assert info.codec == "h264" and (info.width, info.height) == (640, 360)


def test_audio_presence_is_reported(tmp_path, monkeypatch) -> None:
    src = tmp_path / "l.mp4"
    src.write_bytes(b"x")
    fake_probe(monkeypatch, payload([video_stream()]))
    assert probe(str(src)).has_audio is False
    fake_probe(monkeypatch, payload([video_stream(), {"codec_type": "audio"}]))
    assert probe(str(src)).has_audio is True


def test_a_file_without_a_video_stream_is_an_error(tmp_path, monkeypatch) -> None:
    src = tmp_path / "m.m4a"
    src.write_bytes(b"x")
    fake_probe(monkeypatch, payload([{"codec_type": "audio"}]))
    with pytest.raises(MediaError, match="No video stream"):
        probe(str(src))


def test_a_missing_file_is_an_error_before_ffprobe_is_touched(tmp_path) -> None:
    with pytest.raises(MediaError, match="No such file"):
        probe(str(tmp_path / "gone.mp4"))


def test_size_falls_back_to_the_filesystem(tmp_path, monkeypatch) -> None:
    src = tmp_path / "n.mp4"
    src.write_bytes(b"y" * 4096)
    fake_probe(monkeypatch, payload([video_stream()], fmt={"duration": "10.0"}))
    assert probe(str(src)).size_bytes == 4096


# ---- rotation and aspect ---------------------------------------------
@pytest.mark.parametrize("stream,expected", [
    ({"side_data_list": [{"rotation": -90}]}, 90),
    ({"side_data_list": [{"rotation": 90}]}, 270),
    ({"side_data_list": [{"rotation": -180}]}, 180),
    ({"tags": {"rotate": "90"}}, 90),
    ({"tags": {"ROTATE": "270"}}, 270),
    ({"tags": {"rotate": "-90"}}, 270),
    ({}, 0),
    ({"side_data_list": [{"nothing": 1}], "tags": {"rotate": "180"}}, 180),
])
def test_rotation_is_normalised_clockwise(stream: dict, expected: int) -> None:
    assert P._rotation(stream) == expected


def test_aspect_uses_the_decoded_orientation(tmp_path, monkeypatch) -> None:
    """ffmpeg auto-rotates, so auto-grid must size against the rotated frame."""
    src = tmp_path / "o.mp4"
    src.write_bytes(b"x")
    stream = video_stream(width=1920, height=1080)
    stream["side_data_list"] = [{"rotation": -90}]
    fake_probe(monkeypatch, payload([stream]))
    info = probe(str(src))
    assert info.rotation == 90
    assert info.aspect == pytest.approx(1080 / 1920)


def test_aspect_of_an_unrotated_clip() -> None:
    info = MediaInfo(path="", width=640, height=360, fps=30.0, duration=1.0,
                     frame_count=30, has_audio=False, codec="h264", rotation=0,
                     size_bytes=1)
    assert info.aspect == pytest.approx(16 / 9)


def test_aspect_of_a_zero_height_stream_is_zero_not_a_crash() -> None:
    info = MediaInfo(path="", width=640, height=0, fps=30.0, duration=1.0,
                     frame_count=30, has_audio=False, codec="h264", rotation=0,
                     size_bytes=1)
    assert info.aspect == 0.0


# ---- ffprobe process failures ----------------------------------------
def test_a_non_zero_exit_becomes_a_media_error(tmp_path, monkeypatch) -> None:
    src = tmp_path / "p.mp4"
    src.write_bytes(b"x")

    class Proc:
        returncode = 1
        stdout = b""
        stderr = b"junk.bin: Invalid data found when processing input\n"

    monkeypatch.setattr(P.subprocess, "run", lambda *a, **k: Proc())
    with pytest.raises(MediaError, match="Invalid data"):
        probe(str(src))


def test_unreadable_json_becomes_a_media_error(tmp_path, monkeypatch) -> None:
    src = tmp_path / "q.mp4"
    src.write_bytes(b"x")

    class Proc:
        returncode = 0
        stdout = b"{not json"
        stderr = b""

    monkeypatch.setattr(P.subprocess, "run", lambda *a, **k: Proc())
    with pytest.raises(MediaError, match="unreadable JSON"):
        probe(str(src))


def test_a_missing_binary_becomes_a_media_error(tmp_path, monkeypatch) -> None:
    src = tmp_path / "r.mp4"
    src.write_bytes(b"x")

    def missing(*a, **k):
        raise FileNotFoundError("ffprobe")

    monkeypatch.setattr(P.subprocess, "run", missing)
    with pytest.raises(MediaError, match="not found on PATH"):
        probe(str(src))


def test_the_probe_never_pops_a_console_window(tmp_path, monkeypatch) -> None:
    """Hard rule 3: every subprocess passes CREATE_NO_WINDOW on Windows."""
    src = tmp_path / "s.mp4"
    src.write_bytes(b"x")
    seen: dict = {}

    class Proc:
        returncode = 0
        stdout = b'{"streams": [], "format": {}}'
        stderr = b""

    def spy(cmd, **kwargs):
        seen.update(kwargs)
        return Proc()

    monkeypatch.setattr(P.subprocess, "run", spy)
    with pytest.raises(MediaError):
        probe(str(src))
    assert seen.get("creationflags") == P.CREATE_NO_WINDOW
    assert seen.get("timeout") == P.PROBE_TIMEOUT


# ---- against real files ----------------------------------------------
@pytest.mark.ffmpeg
def test_have_ffmpeg_reports_three_booleans(ffmpeg_tools) -> None:
    assert len(ffmpeg_tools) == 3
    assert all(isinstance(v, bool) for v in ffmpeg_tools)


@pytest.mark.ffmpeg
def test_a_real_mp4_probes_exactly(tiny_mp4: Path) -> None:
    info = probe(str(tiny_mp4))
    assert (info.width, info.height) == (64, 48)
    assert info.fps == pytest.approx(12.0)
    assert info.duration == pytest.approx(1.0, abs=0.1)
    assert info.frame_count == 12
    assert info.frames_estimated is False
    assert info.fps_estimated is False
    assert info.has_audio is True
    assert info.codec == "h264"
    assert info.rotation == 0
    assert info.size_bytes == tiny_mp4.stat().st_size
    assert info.aspect == pytest.approx(4 / 3)


@pytest.mark.ffmpeg
def test_a_real_webm_falls_back_to_estimating_the_count(tiny_webm: Path) -> None:
    """This is the container the fallback exists for."""
    info = probe(str(tiny_webm))
    assert info.frames_estimated is True
    assert info.frame_count == pytest.approx(12, abs=1)
    assert info.fps == pytest.approx(12.0)
    assert info.duration == pytest.approx(1.0, abs=0.1)
    assert info.has_audio is False


@pytest.mark.ffmpeg
def test_a_real_audio_file_has_no_video_stream(audio_only: Path) -> None:
    with pytest.raises(MediaError, match="No video stream"):
        probe(str(audio_only))


@pytest.mark.ffmpeg
def test_random_bytes_are_rejected(require_ffmpeg, junk_file: Path) -> None:
    with pytest.raises(MediaError):
        probe(str(junk_file))


@pytest.mark.ffmpeg
def test_an_empty_file_is_rejected(require_ffmpeg, tmp_path: Path) -> None:
    empty = tmp_path / "empty.mp4"
    empty.write_bytes(b"")
    with pytest.raises(MediaError):
        probe(str(empty))
