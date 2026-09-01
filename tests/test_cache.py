"""`.tlp` container and the content-addressed bake store.

A bake is only worth having if it comes back byte-for-byte and if a stale
one is never served: the old `boxes.bin` had no index and no record of the
settings that produced it, so it could neither be seeked nor validated.
Both properties are asserted here, including the ugly boundaries -- a frame
with no boxes at all, one with the u16 maximum of them, and coordinates at
65535.
"""
from __future__ import annotations

import json
import struct
from pathlib import Path

import numpy as np
import pytest

from timeleap.cache import store
from timeleap.cache.format import (
    MAGIC,
    VERSION,
    BakeError,
    BakeHeader,
    BakeReader,
    write_bake,
)

HEADER = BakeHeader(fps=30.0, grid_w=96, grid_h=54, levels=3, frame_count=0,
                    duration=1.0, source="clip.mp4", source_hash="deadbeef",
                    fingerprint="fingerprint0", algo="fast")


def frames_like(counts, seed: int = 0) -> list[np.ndarray]:
    rng = np.random.default_rng(seed)
    out = []
    for n in counts:
        arr = np.zeros((n, 5), np.int32)
        if n:
            arr[:, :4] = rng.integers(0, 300, (n, 4))
            arr[:, 4] = rng.integers(0, 4, n)
        out.append(arr)
    return out


def write(path: Path, frames, **header) -> Path:
    hdr = BakeHeader(**{**HEADER.to_dict(), "frame_count": len(frames), **header})
    write_bake(path, hdr, frames)
    return path


# ---- round trip ------------------------------------------------------
def test_round_trip_is_exact(tmp_path: Path) -> None:
    frames = frames_like([0, 1, 5, 0, 200, 3], seed=1)
    path = write(tmp_path / "a.tlp", frames)
    with BakeReader(path) as r:
        assert len(r) == len(frames)
        for i, expected in enumerate(frames):
            got = r[i]
            assert got.dtype == np.int32
            assert got.shape == expected.shape
            assert np.array_equal(got, expected), i


def test_zero_box_frames_round_trip_as_empty_not_missing(tmp_path: Path) -> None:
    path = write(tmp_path / "blank.tlp", [np.empty((0, 5), np.int32)] * 4)
    with BakeReader(path) as r:
        assert len(r) == 4
        for i in range(4):
            assert r[i].shape == (0, 5)
            assert r[i].dtype == np.int32
            assert r.box_count(i) == 0


def test_maximum_coordinates_survive_the_u16_encoding(tmp_path: Path) -> None:
    frame = np.array([[65535, 65535, 65535, 65535, 65535],
                      [0, 0, 0, 0, 0],
                      [65535, 0, 1, 65534, 7]], np.int32)
    path = write(tmp_path / "max.tlp", [frame])
    with BakeReader(path) as r:
        assert np.array_equal(r[0], frame)


def test_a_frame_with_the_maximum_box_count_round_trips(tmp_path: Path) -> None:
    n = 0xFFFF
    rng = np.random.default_rng(7)
    frame = rng.integers(0, 65536, (n, 5)).astype(np.int32)
    path = write(tmp_path / "full.tlp", [frame, np.empty((0, 5), np.int32)])
    with BakeReader(path) as r:
        assert r.box_count(0) == n
        assert np.array_equal(r[0], frame)
        assert r[1].shape == (0, 5)


def test_header_round_trips_including_created(tmp_path: Path) -> None:
    path = write(tmp_path / "h.tlp", frames_like([2, 2]))
    with BakeReader(path) as r:
        h = r.header
        assert (h.fps, h.grid_w, h.grid_h, h.levels) == (30.0, 96, 54, 3)
        assert h.source == "clip.mp4" and h.algo == "fast"
        assert h.fingerprint == "fingerprint0" and h.source_hash == "deadbeef"
        assert h.frame_count == 2
        assert h.created, "the writer must stamp a creation time"


def test_frame_count_in_the_header_matches_what_was_written(tmp_path: Path) -> None:
    """The estimate handed in is a reservation hint, never the truth."""
    for estimate in (0, 3, 100_000):
        path = write(tmp_path / f"est{estimate}.tlp", frames_like([1] * 40),
                     frame_count=estimate)
        with BakeReader(path) as r:
            assert r.header.frame_count == 40
            assert len(r) == 40


def test_a_wildly_wrong_estimate_does_not_bloat_the_file(tmp_path: Path) -> None:
    """`probe()` can hand over a nonsense frame count from a broken duration.

    Reserving 500k index slots for 10 frames would leave 4 MB of zeroes in
    the bake forever, so an over-reservation that large is rebuilt away.
    """
    frames = frames_like([1] * 10, seed=6)
    naive_table = (500_000 + 1) * 8
    huge = write(tmp_path / "b.tlp", frames, frame_count=500_000)
    close = write(tmp_path / "s.tlp", frames, frame_count=10)

    assert huge.stat().st_size < naive_table / 100
    assert huge.stat().st_size <= close.stat().st_size
    with BakeReader(huge) as r:
        assert len(r) == 10
        assert all(np.array_equal(r[i], f) for i, f in enumerate(frames))


def test_an_underestimate_grows_the_index_without_losing_frames(tmp_path: Path) -> None:
    frames = frames_like([2] * 500, seed=8)
    path = write(tmp_path / "grow.tlp", frames, frame_count=1)
    with BakeReader(path) as r:
        assert len(r) == 500
        assert np.array_equal(r[499], frames[499])
        assert np.array_equal(r[0], frames[0])


def test_progress_is_reported_once_per_frame(tmp_path: Path) -> None:
    seen: list[int] = []
    write_bake(tmp_path / "p.tlp", HEADER, frames_like([0, 1, 2, 3]), seen.append)
    assert seen == [1, 2, 3, 4]


def test_iteration_yields_every_frame_in_order(tmp_path: Path) -> None:
    frames = frames_like([3, 0, 7], seed=5)
    path = write(tmp_path / "i.tlp", frames)
    with BakeReader(path) as r:
        assert all(np.array_equal(a, b) for a, b in zip(r, frames))
        assert len(list(r)) == 3


def test_negative_indexing_counts_back(tmp_path: Path) -> None:
    frames = frames_like([1, 2, 3])
    path = write(tmp_path / "n.tlp", frames)
    with BakeReader(path) as r:
        assert np.array_equal(r[-1], frames[-1])
        assert np.array_equal(r[-3], frames[0])
        with pytest.raises(IndexError):
            r[3]
        with pytest.raises(IndexError):
            r[-4]


def test_reading_after_close_is_refused(tmp_path: Path) -> None:
    r = BakeReader(write(tmp_path / "c.tlp", frames_like([1])))
    r.close()
    r.close()                       # idempotent
    with pytest.raises(ValueError):
        r[0]


# ---- random access ---------------------------------------------------
def test_random_access_is_one_read_regardless_of_index(tmp_path: Path) -> None:
    """O(1) seek, asserted structurally rather than with a stopwatch: with the
    index table each frame costs exactly one positioned read, no scanning."""
    frames = frames_like(list(range(0, 400)) , seed=3)
    path = write(tmp_path / "idx.tlp", frames)
    with BakeReader(path, use_mmap=False) as r:
        calls: list[tuple[int, int]] = []
        real = r._read

        def counting(offset: int, size: int) -> bytes:
            calls.append((offset, size))
            return real(offset, size)

        r._read = counting                       # type: ignore[method-assign]
        for i in (399, 0, 250, 1, 398, 17):
            calls.clear()
            assert np.array_equal(r[i], frames[i]), i
            assert len(calls) == 1, f"frame {i} took {len(calls)} reads"


def test_out_of_order_access_returns_the_right_frames(tmp_path: Path) -> None:
    frames = frames_like([i % 13 for i in range(200)], seed=4)
    path = write(tmp_path / "shuffled.tlp", frames)
    order = np.random.default_rng(9).permutation(len(frames))
    with BakeReader(path) as r:
        for i in order:
            assert np.array_equal(r[int(i)], frames[int(i)]), i


def test_box_count_agrees_with_the_decoded_frame(tmp_path: Path) -> None:
    frames = frames_like([0, 1, 40, 7])
    path = write(tmp_path / "bc.tlp", frames)
    with BakeReader(path) as r:
        for i, f in enumerate(frames):
            assert r.box_count(i) == f.shape[0]
        with pytest.raises(IndexError):
            r.box_count(99)


# ---- rejection -------------------------------------------------------
def corrupt(path: Path, dest: Path, mutate) -> Path:
    data = bytearray(path.read_bytes())
    mutate(data)
    dest.write_bytes(bytes(data))
    return dest


def test_bad_magic_is_rejected(tmp_path: Path) -> None:
    good = write(tmp_path / "g.tlp", frames_like([1, 2]))
    bad = corrupt(good, tmp_path / "bad.tlp",
                  lambda d: d.__setitem__(slice(0, 4), b"XXXX"))
    with pytest.raises(BakeError, match="bad magic"):
        BakeReader(bad)


def test_wrong_version_is_rejected(tmp_path: Path) -> None:
    good = write(tmp_path / "g.tlp", frames_like([1, 2]))
    bad = corrupt(good, tmp_path / "v.tlp",
                  lambda d: d.__setitem__(slice(4, 8), struct.pack("<I", VERSION + 1)))
    with pytest.raises(BakeError, match="version"):
        BakeReader(bad)


@pytest.mark.parametrize("keep", [0, 4, 11, 20, 0.5, 0.9])
def test_truncation_is_rejected(tmp_path: Path, keep) -> None:
    good = write(tmp_path / "g.tlp", frames_like([3, 4, 5, 6]))
    data = good.read_bytes()
    n = int(len(data) * keep) if isinstance(keep, float) else keep
    bad = tmp_path / "t.tlp"
    bad.write_bytes(data[:n])
    with pytest.raises(BakeError):
        BakeReader(bad)


def test_a_truncated_final_blob_is_caught_on_read(tmp_path: Path) -> None:
    """The index still parses; the damage only shows when the frame is read."""
    good = write(tmp_path / "g.tlp", frames_like([2, 2, 40]))
    data = bytearray(good.read_bytes())
    del data[-100:]
    bad = tmp_path / "blob.tlp"
    bad.write_bytes(bytes(data))
    with pytest.raises(BakeError):
        with BakeReader(bad) as r:
            r[2]


def test_a_header_without_frame_count_is_rejected(tmp_path: Path) -> None:
    good = write(tmp_path / "g.tlp", frames_like([1]))
    data = bytearray(good.read_bytes())
    json_len = struct.unpack("<I", data[8:12])[0]
    payload = json.loads(data[12:12 + json_len].decode())
    payload.pop("frame_count")
    data[12:12 + json_len] = json.dumps(payload).encode().ljust(json_len, b" ")
    bad = tmp_path / "nofc.tlp"
    bad.write_bytes(bytes(data))
    with pytest.raises(BakeError, match="frame_count"):
        BakeReader(bad)


def test_a_header_that_is_not_json_is_rejected(tmp_path: Path) -> None:
    good = write(tmp_path / "g.tlp", frames_like([1]))
    data = bytearray(good.read_bytes())
    json_len = struct.unpack("<I", data[8:12])[0]
    data[12:12 + json_len] = b"[not an object]".ljust(json_len, b" ")
    bad = tmp_path / "nj.tlp"
    bad.write_bytes(bytes(data))
    with pytest.raises(BakeError):
        BakeReader(bad)


def test_missing_file_is_an_os_error_not_a_bake_error(tmp_path: Path) -> None:
    with pytest.raises(OSError):
        BakeReader(tmp_path / "nope.tlp")


@pytest.mark.parametrize("field,value", [
    ("frame_count", None), ("fps", "fast"), ("grid_w", 1.5), ("levels", True),
    ("duration", [1]),
])
def test_header_fields_of_the_wrong_type_are_rejected(field: str, value) -> None:
    raw = HEADER.to_dict() | {field: value}
    with pytest.raises(BakeError, match=field):
        BakeHeader.from_dict(raw)


def test_header_accepts_an_integral_float_frame_count() -> None:
    """JSON has one number type, so 300.0 must survive as 300."""
    h = BakeHeader.from_dict(HEADER.to_dict() | {"frame_count": 300.0})
    assert h.frame_count == 300 and isinstance(h.frame_count, int)


@pytest.mark.parametrize("frame,match", [
    (np.zeros((3, 4), np.int32), r"\(N, 5\)"),
    (np.zeros((3,), np.int32), r"\(N, 5\)"),
    (np.zeros((2, 5), np.float32), "integer"),
    (np.array([[0, 0, 1, 1, 70000]], np.int32), "0..65535"),
    (np.array([[-1, 0, 1, 1, 0]], np.int32), "0..65535"),
])
def test_unwritable_frames_are_refused(tmp_path: Path, frame, match: str) -> None:
    with pytest.raises(BakeError, match=match):
        write_bake(tmp_path / "x.tlp", HEADER, [frame])


def test_too_many_boxes_is_refused(tmp_path: Path) -> None:
    frame = np.zeros((0x10000, 5), np.int32)
    with pytest.raises(BakeError, match="u16 limit"):
        write_bake(tmp_path / "x.tlp", HEADER, [frame])


def test_a_failed_write_leaves_no_file_behind(tmp_path: Path) -> None:
    dest = tmp_path / "partial.tlp"

    def exploding():
        yield np.zeros((1, 5), np.int32)
        raise RuntimeError("decoder died")

    with pytest.raises(RuntimeError):
        write_bake(dest, HEADER, exploding())
    assert not dest.exists(), "a half-written bake must never be left in place"
    assert list(tmp_path.glob("*.tmp*")) == []


def test_an_interrupted_write_does_not_clobber_a_good_bake(tmp_path: Path) -> None:
    dest = write(tmp_path / "keep.tlp", frames_like([4, 4]))
    before = dest.read_bytes()

    def exploding():
        yield np.zeros((1, 5), np.int32)
        raise RuntimeError("nope")

    with pytest.raises(RuntimeError):
        write_bake(dest, HEADER, exploding())
    assert dest.read_bytes() == before


# ---- store -----------------------------------------------------------
@pytest.fixture
def video(tmp_path: Path) -> Path:
    src = tmp_path / "movie.mp4"
    src.write_bytes(b"not really a video, but it has bytes " * 5000)
    return src


def test_cache_path_depends_on_both_source_and_fingerprint(cache_home, video) -> None:
    a = store.cache_path(video, "fp-a")
    b = store.cache_path(video, "fp-b")
    other = store.cache_path(video.with_name("other.mp4"), "fp-a")
    assert a != b and a != other
    assert a == store.cache_path(video, "fp-a"), "must be pure"
    assert a.suffix == ".tlp"
    assert a.parent == store.cache_dir()
    assert "movie" in a.name and "fp-a" in a.name


def test_cache_path_is_case_and_form_insensitive_on_windows(cache_home, video) -> None:
    same = store.cache_path(str(video).upper(), "fp") if video.drive else None
    if same is not None:
        assert same == store.cache_path(video, "fp")


def test_source_hash_tracks_content_and_mtime(cache_home, video) -> None:
    first = store.source_hash(video)
    assert first == store.source_hash(video)
    video.write_bytes(video.read_bytes() + b"one more byte block" * 100)
    assert store.source_hash(video) != first


def test_lookup_misses_when_nothing_is_cached(cache_home, video) -> None:
    assert store.lookup(video, "fp") is None


def test_lookup_hits_when_everything_matches(cache_home, video) -> None:
    fp = "fingerprint-ok"
    frames = frames_like([2, 0, 5], seed=2)
    write(store.cache_path(video, fp), frames, fingerprint=fp,
          source_hash=store.source_hash(video), source=str(video))

    reader = store.lookup(video, fp)
    assert reader is not None
    try:
        assert len(reader) == 3
        assert np.array_equal(reader[2], frames[2])
    finally:
        reader.close()


def test_lookup_rejects_a_mismatched_fingerprint(cache_home, video) -> None:
    """The filename says one thing and the header another: never trust it."""
    entry = store.cache_path(video, "asked-for")
    write(entry, frames_like([1]), fingerprint="baked-with-something-else",
          source_hash=store.source_hash(video))
    assert entry.is_file()

    assert store.lookup(video, "asked-for") is None
    assert not entry.exists(), "a stale entry must be discarded, not kept"


def test_lookup_rejects_a_mismatched_source_hash(cache_home, video) -> None:
    fp = "same-settings"
    entry = store.cache_path(video, fp)
    write(entry, frames_like([1]), fingerprint=fp, source_hash="hash-of-another-file")
    assert entry.is_file()

    assert store.lookup(video, fp) is None
    assert not entry.exists()


def test_lookup_rejects_a_bake_after_the_source_is_re_encoded(cache_home, video) -> None:
    fp = "fp"
    write(store.cache_path(video, fp), frames_like([1]), fingerprint=fp,
          source_hash=store.source_hash(video))
    reader = store.lookup(video, fp)
    assert reader is not None
    reader.close()

    video.write_bytes(b"a completely different encode " * 4000)
    assert store.lookup(video, fp) is None


def test_lookup_discards_a_corrupt_entry(cache_home, video) -> None:
    entry = store.cache_path(video, "fp")
    entry.parent.mkdir(parents=True, exist_ok=True)
    entry.write_bytes(b"TLPC" + b"\0" * 40)
    assert store.lookup(video, "fp") is None
    assert not entry.exists()


def test_lookup_misses_when_the_source_disappeared(cache_home, video) -> None:
    fp = "fp"
    write(store.cache_path(video, fp), frames_like([1]), fingerprint=fp,
          source_hash=store.source_hash(video))
    video.unlink()
    assert store.lookup(video, fp) is None


def test_entries_lists_the_cache_oldest_first(cache_home, video) -> None:
    import os
    import time

    paths = []
    for i in range(3):
        p = store.cache_path(video, f"fp{i}")
        write(p, frames_like([1]), fingerprint=f"fp{i}")
        os.utime(p, (time.time() - 100 * (3 - i),) * 2)
        paths.append(p)
    listed = store.entries()
    assert [p for p, _, _ in listed] == paths
    assert all(size > 0 for _, size, _ in listed)


def test_prune_deletes_least_recently_used_until_it_fits(cache_home, video) -> None:
    import os
    import time

    sizes = []
    for i in range(4):
        p = store.cache_path(video, f"fp{i}")
        write(p, frames_like([20] * 10), fingerprint=f"fp{i}")
        os.utime(p, (time.time() - 100 * (4 - i),) * 2)
        sizes.append(p.stat().st_size)

    keep_bytes = sum(sizes[2:])
    freed = store.prune(max_bytes=keep_bytes)
    assert freed == sum(sizes[:2])
    remaining = {p.name for p, _, _ in store.entries()}
    assert store.cache_path(video, "fp0").name not in remaining
    assert store.cache_path(video, "fp3").name in remaining


def test_prune_to_zero_empties_the_cache(cache_home, video) -> None:
    for i in range(3):
        write(store.cache_path(video, f"fp{i}"), frames_like([1]), fingerprint=f"fp{i}")
    assert store.prune(max_bytes=0) > 0
    assert store.entries() == []


def test_prune_keeps_a_bake_that_still_fits(cache_home, video) -> None:
    p = write(store.cache_path(video, "fp"), frames_like([1]), fingerprint="fp")
    assert store.prune(max_bytes=1 << 30) == 0
    assert p.is_file()
