"""Indexed, seekable container for baked box geometry (`.tlp`).

The old `boxes.bin` was a bare count-prefixed dump of float32 rectangles: no
fps, no index, no record of which settings produced it. Nothing could load it
back and not even the writer could seek. This replaces it with a real
container:

    MAGIC       4 bytes   b"TLPC"
    u32                   version
    u32                   json_len
    bytes                 header JSON, space padded to json_len
    u64 [n+1]             absolute file offset of every frame blob, plus the
                          end offset, so frame i spans index[i]..index[i+1]
    frame blobs           u16 count, then count * 5 u16 (x, y, w, h, level)

Coordinates are grid cells (see `core.boxgen`), so u16 is exact for any grid
up to 65535 and a box costs 10 bytes instead of the 16 the float dump used.

Both variable-length regions are written before their contents are known --
the frame count only exists once the stream is exhausted -- so the JSON is
space padded (`json.loads` ignores trailing whitespace) and the index table is
over-reserved, then both are patched by seeking back. Offsets are absolute, so
an over-reservation just leaves a few unused bytes ahead of the first blob
instead of forcing the whole payload to move. If the reservation missed badly
in either direction -- too small to hold the real count, or so large the unused
table would bloat the file -- it is rebuilt once with an exact table, which is
the only path that touches the payload twice.
"""
from __future__ import annotations

import json
import mmap
import operator
import os
import shutil
import struct
import threading
import time
from collections.abc import Callable, Iterable, Iterator
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import BinaryIO

import numpy as np

MAGIC = b"TLPC"
VERSION = 1
SUFFIX = ".tlp"

_PREAMBLE = 12                  # MAGIC + u32 version + u32 json_len
_SLOT = 8                       # one u64 index entry
_MAX_BOXES = 0xFFFF             # the u16 count field
_MAX_COORD = 0xFFFF
_JSON_ALIGN = 64
_JSON_SLACK = 64                # room for frame_count growing to its real value
_MIN_SLOTS = 64                 # index slack when the caller has no estimate
_MAX_SLOTS = 1 << 22            # ~19 h at 60 fps; caps a bogus duration estimate
_WASTE_FACTOR = 4               # rebuild rather than carry a table this oversized
_EMPTY_BLOB = struct.pack("<H", 0)
_COPY_CHUNK = 1 << 20
_REPLACE_TRIES = 6
_REPLACE_WAIT = 0.05


class BakeError(RuntimeError):
    """Malformed, truncated or unsupported `.tlp` file."""


_HEADER_TYPES: dict[str, type] = {
    "fps": float, "grid_w": int, "grid_h": int, "levels": int,
    "frame_count": int, "duration": float, "source": str, "source_hash": str,
    "fingerprint": str, "created": str, "algo": str,
}


def _coerce(name: str, value: object, cast: type) -> object:
    """One header field, or `BakeError` -- never a stray TypeError/ValueError."""
    bad = BakeError(f"header field {name!r} is not a {cast.__name__}: {value!r}")
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        raise bad
    if cast is int and isinstance(value, float) and not value.is_integer():
        raise bad
    try:
        return cast(value)
    except (ValueError, OverflowError):   # "abc", or a 400-digit JSON integer
        raise bad from None


@dataclass
class BakeHeader:
    """Everything needed to play a bake back without touching the source."""

    fps: float = 0.0
    grid_w: int = 0
    grid_h: int = 0
    levels: int = 1
    frame_count: int = 0
    duration: float = 0.0
    source: str = ""
    source_hash: str = ""
    fingerprint: str = ""
    created: str = ""
    algo: str = ""

    @classmethod
    def from_dict(cls, raw: dict) -> BakeHeader:
        """Build from parsed JSON, rejecting values of the wrong type.

        A file whose header says `frame_count: null` has to fail as a
        `BakeError` here, not as a `TypeError` from the render thread three
        frames into playback.
        """
        return cls(**{k: _coerce(k, raw[k], cast)
                      for k, cast in _HEADER_TYPES.items() if k in raw})

    def to_dict(self) -> dict:
        return asdict(self)


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _json_blob(header: BakeHeader, length: int) -> bytes:
    blob = json.dumps(header.to_dict(), sort_keys=True).encode("utf-8")
    if len(blob) > length:
        raise BakeError(f"header JSON grew past its reserved {length} bytes")
    return blob.ljust(length, b" ")


def _json_capacity(header: BakeHeader) -> int:
    raw = len(json.dumps(header.to_dict(), sort_keys=True).encode("utf-8"))
    need = raw + _JSON_SLACK
    return -(-need // _JSON_ALIGN) * _JSON_ALIGN


def _reserve_slots(estimate: object) -> int:
    """Index slots to reserve up front, from an untrusted frame-count estimate.

    `probe()` derives the count from `duration * fps` whenever the container
    omits `nb_frames`, so a broken duration can hand over 1e12. The cap keeps
    that from reserving gigabytes; `_rebuild` covers whatever the cap cut off.
    """
    try:
        est = max(0, int(estimate))
    except (TypeError, ValueError, OverflowError):   # None, "abc", nan, inf
        est = 0
    return min(_MAX_SLOTS, est + max(_MIN_SLOTS, est // 8))


def _needs_exact_table(count: int, slots: int) -> bool:
    """Whether the reservation missed badly enough to be worth a rebuild.

    Too small and the table cannot hold the real count. Grossly too large and
    the file would carry megabytes of unused index forever. A rebuild copies
    the payload once, so the margin is generous rather than exact: a merely
    optimistic estimate is not worth re-reading the whole bake for.
    """
    return count > slots or slots > _WASTE_FACTOR * count + _MIN_SLOTS


def _zero_fill(f: BinaryIO, total: int) -> None:
    """Reserve `total` bytes without materialising them -- the table can be MBs."""
    block = b"\0" * min(total, _COPY_CHUNK)
    while total > 0:
        n = min(total, len(block))
        f.write(block if n == len(block) else block[:n])
        total -= n


def _encode_frame(frame: np.ndarray) -> bytes:
    arr = np.asarray(frame)
    # Shape and dtype are checked before the empty shortcut: several boxgen
    # paths return (0, 4) or float arrays, and silently baking those as an
    # empty frame would hide the caller's bug until playback looked wrong.
    if arr.ndim != 2 or arr.shape[1] != 5:
        raise BakeError(f"frame must be (N, 5), got {arr.shape}")
    if arr.dtype.kind not in "iu":
        raise BakeError(f"frame must be an integer array, got {arr.dtype}")
    if arr.size == 0:
        return _EMPTY_BLOB
    count = arr.shape[0]
    if count > _MAX_BOXES:
        raise BakeError(f"{count} boxes exceeds the u16 limit of {_MAX_BOXES}")
    if int(arr.min()) < 0 or int(arr.max()) > _MAX_COORD:
        raise BakeError("box values must fit 0..65535")
    body = np.ascontiguousarray(arr, dtype="<u2")
    return struct.pack("<H", count) + body.tobytes()


def write_bake(path: str | os.PathLike[str], header: BakeHeader,
               frames: Iterable[np.ndarray],
               progress: Callable[[int], None] | None = None) -> None:
    """Stream `frames` into a `.tlp` at `path`, patching the index at the end.

    Frames are consumed lazily, so a two-hour bake never holds more than one
    frame in memory. The write lands on a sibling `.tmp` and is `os.replace`d
    into place, so an interrupted bake can never leave a half-written file
    that a later `lookup()` would treat as a hit.
    """
    dest = Path(path)
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_name(dest.name + ".tmp")

    hdr = replace(header, created=header.created or _now())
    json_len = _json_capacity(hdr)
    index_start = _PREAMBLE + json_len
    slots = _reserve_slots(hdr.frame_count)
    data_start = index_start + (slots + 1) * _SLOT

    offsets: list[int] = []
    done = False
    try:
        with open(tmp, "wb") as f:
            f.write(MAGIC)
            f.write(struct.pack("<II", VERSION, json_len))
            f.write(_json_blob(hdr, json_len))
            _zero_fill(f, (slots + 1) * _SLOT)
            pos = data_start
            for frame in frames:
                blob = _encode_frame(frame)
                offsets.append(pos)
                f.write(blob)
                pos += len(blob)
                if progress is not None:
                    progress(len(offsets))
            offsets.append(pos)                     # end sentinel
            count = len(offsets) - 1
            hdr = replace(hdr, frame_count=count)
            rebuild = _needs_exact_table(count, slots)
            if not rebuild:
                f.seek(_PREAMBLE)
                f.write(_json_blob(hdr, json_len))
                f.write(np.asarray(offsets, "<u8").tobytes())
        if rebuild:
            _rebuild(tmp, hdr, json_len, offsets, data_start)
        _replace_with_retry(tmp, dest)
        done = True
    finally:
        if not done:
            try:
                tmp.unlink()
            except OSError:
                pass


def _replace_with_retry(tmp: Path, dest: Path) -> None:
    """`os.replace`, tolerating a transient Windows sharing violation.

    Any process still holding `dest` open -- a `BakeReader` mmapping the
    previous bake, or the AV scanner that grabbed the new one the instant it
    closed -- makes the rename fail with EACCES. Those handles go away in
    milliseconds, and discarding a finished bake over one is far worse than
    waiting for it.
    """
    for attempt in range(_REPLACE_TRIES):
        try:
            os.replace(tmp, dest)
            return
        except PermissionError:
            if attempt == _REPLACE_TRIES - 1:
                raise
            time.sleep(_REPLACE_WAIT * (attempt + 1))


def _rebuild(tmp: Path, header: BakeHeader, json_len: int,
             offsets: list[int], old_data_start: int) -> None:
    """Re-emit `tmp` with an exactly sized index when the reservation missed.

    Handles both directions: the payload shifts up when the estimate was too
    small and down when it was so large the unused table would bloat the file.
    """
    count = len(offsets) - 1
    new_data_start = _PREAMBLE + json_len + (count + 1) * _SLOT
    shift = new_data_start - old_data_start
    staged = tmp.with_name(tmp.name + ".grow")
    try:
        with open(tmp, "rb") as src, open(staged, "wb") as dst:
            dst.write(MAGIC)
            dst.write(struct.pack("<II", VERSION, json_len))
            dst.write(_json_blob(header, json_len))
            dst.write((np.asarray(offsets, np.int64) + shift).astype("<u8").tobytes())
            src.seek(old_data_start)
            shutil.copyfileobj(src, dst, _COPY_CHUNK)
        os.replace(staged, tmp)
    except BaseException:
        try:
            staged.unlink()
        except OSError:
            pass
        raise


class BakeReader:
    """Random-access reader over a `.tlp`. O(1) per frame via the index table."""

    def __init__(self, path: str | os.PathLike[str], use_mmap: bool = True) -> None:
        self.path = Path(path)
        self._file = open(self.path, "rb")
        self._mm: mmap.mmap | None = None
        self._seek_lock = threading.Lock()
        try:
            size = os.fstat(self._file.fileno()).st_size
            if size < _PREAMBLE:
                raise BakeError(f"{self.path.name}: too small to be a bake")
            pre = self._file.read(_PREAMBLE)
            if pre[:4] != MAGIC:
                raise BakeError(f"{self.path.name}: bad magic {pre[:4]!r}")
            version, json_len = struct.unpack("<II", pre[4:])
            if version != VERSION:
                raise BakeError(f"{self.path.name}: version {version}, expected {VERSION}")
            index_start = _PREAMBLE + json_len
            if index_start > size:
                raise BakeError(f"{self.path.name}: truncated header")
            try:
                payload = json.loads(self._file.read(json_len).decode("utf-8"))
            except (ValueError, UnicodeDecodeError) as exc:
                raise BakeError(f"{self.path.name}: unreadable header ({exc})") from exc
            if not isinstance(payload, dict):
                raise BakeError(f"{self.path.name}: header is not an object")
            if "frame_count" not in payload:
                # It sizes the index, so defaulting it would silently present a
                # full bake as an empty one instead of reporting the damage.
                raise BakeError(f"{self.path.name}: header has no frame_count")
            self.header = BakeHeader.from_dict(payload)

            count = self.header.frame_count
            if count < 0:
                raise BakeError(f"{self.path.name}: negative frame_count")
            data_start = index_start + (count + 1) * _SLOT
            if data_start > size:
                raise BakeError(f"{self.path.name}: truncated index")
            index = np.frombuffer(self._file.read((count + 1) * _SLOT), "<u8").astype(np.int64)
            if index[0] < data_start or index[-1] > size or np.any(np.diff(index) < 0):
                raise BakeError(f"{self.path.name}: truncated or corrupt frame data")
            self._index = index
            if use_mmap and size:
                try:
                    self._mm = mmap.mmap(self._file.fileno(), 0, access=mmap.ACCESS_READ)
                except (OSError, ValueError):
                    self._mm = None     # buffered reads still work; only slower
        except BaseException:
            self._file.close()
            raise

    # ---- access ------------------------------------------------------
    def __len__(self) -> int:
        return self._index.size - 1

    def __getitem__(self, i: int) -> np.ndarray:
        """Frame `i` as an (N, 5) int32 array. Negative indices count back."""
        if self._file.closed:
            raise ValueError("BakeReader is closed")
        n = len(self)
        j = operator.index(i)
        if j < 0:
            j += n
        if not 0 <= j < n:
            raise IndexError(f"frame index {i} out of range (0..{n - 1})")
        start = int(self._index[j])
        stop = int(self._index[j + 1])
        raw = self._mm[start:stop] if self._mm is not None else self._read(start, stop - start)
        if len(raw) < 2:
            raise BakeError(f"{self.path.name}: frame {j} blob is truncated")
        count = int.from_bytes(raw[:2], "little")
        if len(raw) != 2 + count * 10:
            raise BakeError(f"{self.path.name}: frame {j} blob is {len(raw)} bytes, "
                            f"expected {2 + count * 10}")
        if count == 0:
            return np.empty((0, 5), np.int32)
        return np.frombuffer(raw, "<u2", count=count * 5, offset=2).reshape(count, 5).astype(np.int32)

    def __iter__(self) -> Iterator[np.ndarray]:
        for i in range(len(self)):
            yield self[i]

    def box_count(self, i: int) -> int:
        """Boxes in frame `i` without decoding it -- for stats and budgeting."""
        n = len(self)
        j = operator.index(i)
        if j < 0:
            j += n
        if not 0 <= j < n:
            raise IndexError(f"frame index {i} out of range (0..{n - 1})")
        start = int(self._index[j])
        raw = self._mm[start:start + 2] if self._mm is not None else self._read(start, 2)
        return int.from_bytes(raw, "little")

    def _read(self, offset: int, size: int) -> bytes:
        # seek+read is two calls on shared state and the engine reads frames
        # from worker threads, so without this two readers interleave and each
        # silently gets the other's frame -- corrupt output, never an error.
        with self._seek_lock:
            self._file.seek(offset)
            return self._file.read(size)

    # ---- lifetime ----------------------------------------------------
    def close(self) -> None:
        if self._mm is not None:
            self._mm.close()
            self._mm = None
        if not self._file.closed:
            self._file.close()

    def __enter__(self) -> BakeReader:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def __repr__(self) -> str:
        return (f"<BakeReader {self.path.name} {len(self)} frames "
                f"{self.header.grid_w}x{self.header.grid_h} @ {self.header.fps:g} fps>")
