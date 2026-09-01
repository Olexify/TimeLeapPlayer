"""Content-addressed bake cache.

A cache entry is only useful if it was produced from *this* file with *these*
geometry settings, so an entry is keyed twice over: the filename carries the
source path and `VideoConfig.fingerprint()`, and the stored header repeats the
source hash and the fingerprint so a hit is verified rather than assumed.

`source_hash` deliberately does not read the whole file. A 2 GB video would
cost seconds to digest on every startup, which is longer than baking a short
clip; size + mtime_ns + the first and last 64 KB distinguishes re-encodes,
trims and edits in a few milliseconds. The failure mode is a file edited in
place, in the middle, without changing its length or mtime -- which no
transcoder or downloader does.
"""
from __future__ import annotations

import hashlib
import os
import time
from pathlib import Path

from ..config import cache_dir
from .format import SUFFIX, BakeError, BakeReader

SAMPLE_BYTES = 64 * 1024
TMP_GRACE = 3600.0              # seconds before an orphaned .tmp is collectable
_SAFE = frozenset("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._-")


def source_hash(path: str | os.PathLike[str]) -> str:
    """Cheap content identity: size, mtime_ns and the two 64 KB end samples."""
    st = os.stat(path)
    h = hashlib.blake2b(digest_size=16)
    h.update(f"{st.st_size}|{st.st_mtime_ns}|".encode())
    with open(path, "rb") as f:
        h.update(f.read(SAMPLE_BYTES))
        if st.st_size > SAMPLE_BYTES:
            f.seek(max(0, st.st_size - SAMPLE_BYTES))
            h.update(f.read(SAMPLE_BYTES))
    return h.hexdigest()


def _slug(name: str) -> str:
    out = "".join(c if c in _SAFE else "_" for c in name)[:32].strip("._-")
    return out or "video"


def cache_path(path: str | os.PathLike[str], fingerprint: str) -> Path:
    """Where the bake of `path` under `fingerprint` lives.

    The path is hashed rather than the file, so this is pure and cheap; the
    readable stem is only there to make the cache directory browsable.
    """
    key = os.path.normcase(os.path.abspath(os.fspath(path)))
    tag = hashlib.blake2b(key.encode("utf-8", "surrogatepass"), digest_size=8).hexdigest()
    return cache_dir() / f"{_slug(Path(path).stem)}-{tag}-{fingerprint}{SUFFIX}"


def _discard(path: Path) -> None:
    try:
        path.unlink()
    except OSError:
        pass                    # still mapped by another process; prune gets it later


def lookup(path: str | os.PathLike[str], fingerprint: str) -> BakeReader | None:
    """Open the bake for `path`/`fingerprint`, or None if there is no valid one.

    A stale or corrupt entry is deleted on the way out, so a miss never leaves
    dead weight behind for `prune` to charge against the budget.
    """
    entry = cache_path(path, fingerprint)
    if not entry.is_file():
        return None
    try:
        reader = BakeReader(entry)
    except (BakeError, OSError):
        _discard(entry)
        return None
    try:
        current = source_hash(path)
    except Exception:           # any failure to identify the source is a miss;
        reader.close()          # closing matters because an unclosed mmap pins
        return None             # the entry for the life of the process
    if reader.header.fingerprint != fingerprint or reader.header.source_hash != current:
        reader.close()
        _discard(entry)
        return None
    try:
        os.utime(entry)         # mtime doubles as the LRU stamp for prune()
    except OSError:
        pass
    return reader


def entries() -> list[tuple[Path, int, float]]:
    """Every cache file as (path, size_bytes, mtime), newest last."""
    out: list[tuple[Path, int, float]] = []
    for p in cache_dir().glob("*" + SUFFIX):
        try:
            st = p.stat()
        except OSError:
            continue
        out.append((p, st.st_size, st.st_mtime))
    out.sort(key=lambda e: e[2])
    return out


def prune(max_bytes: int = 2 << 30) -> int:
    """Delete least-recently-used bakes until the cache fits. Returns bytes freed."""
    freed = 0
    cutoff = time.time() - TMP_GRACE
    for p in cache_dir().glob("*" + SUFFIX + ".tmp*"):
        try:
            st = p.stat()
            if st.st_mtime > cutoff:
                continue        # a bake may still be writing this one
            p.unlink()
        except OSError:
            continue
        freed += st.st_size

    items = entries()
    total = sum(size for _, size, _ in items)
    for p, size, _ in items:
        if total <= max_bytes:
            break
        try:
            p.unlink()
        except OSError:
            continue            # open elsewhere; skip rather than fail the sweep
        total -= size
        freed += size
    return freed
