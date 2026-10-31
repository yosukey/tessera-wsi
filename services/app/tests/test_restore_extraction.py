"""Restore extraction: every member shape ``backup._safe_extract`` /
``backup.stage_restore`` must refuse, and the limits they enforce, exercised
end to end on small synthetic archives.

test_backup.py already covers ``..`` traversal, symlinks, duplicate names,
the member-count cap and the extended-header / sparse caps at the reader
level; this file covers the remaining shapes (hard links, device nodes and
FIFOs, absolute names, unknown member types), the decompressed-size cap, the
free-space reserve and damaged archives through ``stage_restore`` itself, and
SQLite sidecars (``wsi.db-wal`` / ``-shm`` / ``-journal``) smuggled in next to
the archived database.
"""
from __future__ import annotations

import gzip
import io
import os
import shutil
import sqlite3
import tarfile
from collections import namedtuple

import pytest

from wsi_app import backup as _backup
from wsi_app.config import settings

from .test_backup import _make_backup_via_api


def _staging_entries() -> set[str]:
    root = os.path.join(settings.backup_dir, ".staging")
    try:
        return set(os.listdir(root))
    except FileNotFoundError:
        return set()


def _tar_bytes(members) -> bytes:
    """An uncompressed tar of ``(TarInfo, data-or-None)`` pairs."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w", format=tarfile.PAX_FORMAT) as tar:
        for info, data in members:
            if data is None:
                tar.addfile(info)
            else:
                info.size = len(data)
                tar.addfile(info, io.BytesIO(data))
    return buf.getvalue()


def _member(name: str, type_: bytes = tarfile.REGTYPE, **attrs) -> tarfile.TarInfo:
    info = tarfile.TarInfo(name)
    info.type = type_
    info.mode = 0o644
    for k, v in attrs.items():
        setattr(info, k, v)
    return info


def _extract(tmp_path, members, **kw):
    """Write the members as a tar and run _safe_extract on it into ``out``."""
    path = tmp_path / "a.tar"
    path.write_bytes(_tar_bytes(members))
    with tarfile.open(str(path), "r") as tar:
        _backup._safe_extract(tar, str(tmp_path / "out"), **kw)


def _written(tmp_path) -> list[str]:
    out = tmp_path / "out"
    if not out.exists():
        return []
    return sorted(str(p.relative_to(out)) for p in out.rglob("*"))


# ---------------------------------------------------------------------------
# Member shapes
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("linkname", [
    "manifest.json",          # resolves inside the destination
    "../../../etc/passwd",    # escapes it
    "/etc/passwd",            # absolute target
])
def test_hard_link_members_are_refused_wherever_they_point(tmp_path, linkname):
    """A hard link to a file inside the stage would alias two staged paths
    (a later member written through one rewrites the other); one to a file
    outside would make the stage (and so the promoted tree) share an inode
    with an arbitrary host file. The app's writer archives hard-linked files as
    regular files, so a link member is never legitimate."""
    with pytest.raises(RuntimeError, match="links not allowed"):
        _extract(tmp_path, [
            (_member("manifest.json"), b"{}"),
            (_member("assets/evil", tarfile.LNKTYPE, linkname=linkname), None),
        ])
    assert _written(tmp_path) == ["manifest.json"]


@pytest.mark.parametrize("type_", [tarfile.FIFOTYPE, tarfile.CHRTYPE, tarfile.BLKTYPE],
                         ids=["fifo", "char", "block"])
def test_fifo_and_device_members_are_refused(tmp_path, type_):
    """A FIFO in the stage blocks whatever opens it next (the integrity walk,
    the swap); a device node would be promoted into DATA_DIR."""
    with pytest.raises(RuntimeError, match="device files not allowed"):
        _extract(tmp_path, [(_member("wsi/node", type_, devmajor=1, devminor=3), None)])
    assert _written(tmp_path) == []


@pytest.mark.parametrize("name", ["/tmp/escapee.txt", "//etc/cron.d/x"])
def test_absolute_member_names_are_refused(tmp_path, name):
    with pytest.raises(RuntimeError, match="unsafe path"):
        _extract(tmp_path, [(_member(name), b"pwned")])
    assert _written(tmp_path) == []
    assert not os.path.exists(name)


def test_an_unknown_member_type_is_refused_without_writing_it(tmp_path):
    """tarfile extracts a member of an unknown type as a regular file
    (``makeunknown``), and ``_safe_extract`` counts only ``isreg()`` members
    against the size caps — so it must never get that far. The ``data``
    extraction filter refuses it as a special file, before any byte lands."""
    payload = b"x" * 4096
    with pytest.raises(tarfile.TarError):
        _extract(tmp_path, [(_member("assets/odd", b"Z"), payload)], max_total_bytes=10)
    assert _written(tmp_path) == []


def test_an_unknown_member_type_fails_staging_as_a_bad_archive(tmp_path):
    archive = tmp_path / "odd.tar"
    archive.write_bytes(_tar_bytes([(_member("manifest.json", b"Z"), b"{}")]))
    before = _staging_entries()
    with pytest.raises(RuntimeError, match="corrupt or not a supported format"):
        _backup.stage_restore(str(archive))
    assert _staging_entries() == before


# ---------------------------------------------------------------------------
# Limits
# ---------------------------------------------------------------------------

def test_decompressed_size_cap_refuses_the_member_that_crosses_it(tmp_path):
    with pytest.raises(RuntimeError, match="configured cap"):
        _extract(tmp_path, [(_member("a.bin"), b"a" * 100), (_member("b.bin"), b"b" * 100)],
                 max_total_bytes=150)
    # Refused before the overflowing member was written.
    assert _written(tmp_path) == ["a.bin"]
    # Exactly at the cap is fine.
    shutil.rmtree(tmp_path / "out")
    _extract(tmp_path, [(_member("a.bin"), b"a" * 100), (_member("b.bin"), b"b" * 100)],
             max_total_bytes=200)
    assert _written(tmp_path) == ["a.bin", "b.bin"]


def _bomb_gz(tmp_path, size: int):
    """A tar.gz far smaller on disk than what it expands to."""
    path = tmp_path / "bomb.tar.gz"
    with tarfile.open(str(path), "w:gz") as tar:
        info = tarfile.TarInfo("manifest.json")
        info.size = size
        tar.addfile(info, io.BytesIO(b"\0" * size))
    return path


def test_stage_restore_enforces_the_configured_decompressed_size_cap(tmp_path):
    path = _bomb_gz(tmp_path, 1 << 20)
    assert path.stat().st_size < 64 * 1024
    saved = settings.backup_max_extract_bytes
    object.__setattr__(settings, "backup_max_extract_bytes", 512 * 1024)  # frozen
    try:
        before = _staging_entries()
        with pytest.raises(RuntimeError, match="configured cap"):
            _backup.stage_restore(str(path))
        assert _staging_entries() == before
    finally:
        object.__setattr__(settings, "backup_max_extract_bytes", saved)


def test_stage_restore_refuses_an_archive_whose_contents_would_eat_the_reserve(tmp_path, monkeypatch):
    """The archive itself fits the volume (the cheap pre-flight passes), but
    what it decompresses to plus the reserve for the running install does
    not: refused mid-extraction, and the partial stage removed. Measured
    through shutil.disk_usage, which every free-space check reads."""
    path = _bomb_gz(tmp_path, 1 << 20)
    usage = namedtuple("usage", "total used free")
    free = 10 * path.stat().st_size  # far more than the archive's own size
    monkeypatch.setattr(shutil, "disk_usage", lambda p: usage(10**12, 10**12 - free, free))
    before = _staging_entries()
    with pytest.raises(RuntimeError, match="free space"):
        _backup.stage_restore(str(path))
    assert _staging_entries() == before


def test_stage_restore_refuses_an_oversized_extended_header_as_a_bad_archive(tmp_path):
    """The reader's extended-header cap (_BoundedTarInfo) surfaces from
    staging as a bad archive (a 400), not a server error."""
    declared = _backup._MAX_EXTENDED_HEADER_BYTES + 4096
    info = tarfile.TarInfo("x")
    info.type = tarfile.XHDTYPE
    info.size = declared
    blob = info.tobuf(format=tarfile.USTAR_FORMAT) + b"\0" * (declared + 2048)
    path = tmp_path / "pax.tar.gz"
    path.write_bytes(gzip.compress(blob, compresslevel=1))
    before = _staging_entries()
    with pytest.raises(RuntimeError, match="corrupt or not a supported format"):
        _backup.stage_restore(str(path))
    assert _staging_entries() == before


# ---------------------------------------------------------------------------
# Damaged archives
# ---------------------------------------------------------------------------

def _compressed_archive(fmt: str) -> bytes:
    # Mostly incompressible, then text: the deflate stream holds stored blocks
    # (whose length fields a flipped byte breaks, a zlib.error) as well as
    # Huffman-coded ones (garbage only the CRC catches), past the first
    # member. Seeded, so every run damages the same bytes.
    import random

    payload = random.Random(1234).randbytes(96 * 1024) + b"tessera wsi " * 1400
    raw = _tar_bytes([(_member("manifest.json"), b"{}"), (_member("wsi.db"), payload)])
    if fmt == "gz":
        return gzip.compress(raw, compresslevel=1, mtime=0)
    if not _backup._HAS_ZSTD:
        pytest.skip("zstandard not installed")
    cctx = _backup._zstd.ZstdCompressor(level=3, write_checksum=True)
    if fmt == "zst":
        return cctx.compress(raw)  # one-shot: single segment, content size recorded
    # As _open_archive_writer streams it: no content size, a window descriptor.
    out = io.BytesIO()
    with cctx.stream_writer(out, closefd=False) as w:
        for at in range(0, len(raw), 4096):
            w.write(raw[at:at + 4096])
    return out.getvalue()


def _raw_error(path):
    """What reading the archive raises before staging translates it."""
    try:
        with _backup._open_archive_reader(str(path)) as tar:
            _backup._safe_extract(tar, str(path) + ".out")
    except BaseException as exc:  # noqa: BLE001 - classified by the caller
        return exc
    return None


def _assert_refused_as_corrupt(path) -> None:
    before = _staging_entries()
    with pytest.raises(RuntimeError, match="corrupt or not a supported format"):
        _backup.stage_restore(str(path))
    assert _staging_entries() == before


@pytest.mark.parametrize("fmt", ["gz", "zst", "zst-stream"])
def test_a_truncated_archive_is_refused_as_corrupt(tmp_path, fmt):
    """python-zstandard ends a truncated frame's output silently and tarfile
    ends an archive quietly at a member boundary, so a .tar.zst cut short —
    by as little as part of its checksum — could stage without its
    trailing members. Every cut is refused, as GzipFile refuses a .tar.gz."""
    blob = _compressed_archive(fmt)
    for keep in (len(blob) // 3, len(blob) * 2 // 3, len(blob) - 5, len(blob) - 1):
        path = tmp_path / f"cut{keep}.tar.{fmt[:3]}"
        path.write_bytes(blob[:keep])
        _assert_refused_as_corrupt(path)


@pytest.mark.parametrize("fmt", ["gz", "zst", "zst-stream"])
def test_a_flipped_byte_anywhere_past_the_header_is_refused_as_corrupt(tmp_path, fmt):
    """Wherever the damage lands — stored data (only the codec checksum can
    tell), a deflate block header, the trailer — staging says the archive is
    corrupt. A damaged deflate block raises zlib.error, which is neither an
    OSError nor a TarError, and must not escape staging as a server error."""
    blob = _compressed_archive(fmt)
    for at in range(16, len(blob), max(1, len(blob) // 48)):
        damaged = bytearray(blob)
        damaged[at] ^= 0xFF
        path = tmp_path / f"flip{at}.tar.{fmt[:3]}"
        path.write_bytes(bytes(damaged))
        _assert_refused_as_corrupt(path)


def gzip_with_an_undecodable_block(raw: bytes, cut: int) -> bytes:
    """A gzip stream whose deflate data holds ``raw[:cut]`` and then a block
    of the reserved type 3, which zlib refuses with zlib.error ("invalid
    block type") once the reader gets that far."""
    import zlib

    comp = zlib.compressobj(6, zlib.DEFLATED, -15)
    body = comp.compress(raw[:cut]) + comp.flush(zlib.Z_FULL_FLUSH)
    return (b"\x1f\x8b\x08\x00\x00\x00\x00\x00\x00\xff" + body + b"\x07" + b"\0" * 64
            + b"\0" * 8)


def test_a_deflate_block_that_does_not_decode_is_refused_as_corrupt(tmp_path):
    """GzipFile raises zlib.error — neither an OSError nor a TarError — for a
    block it cannot decode; up to Python 3.13 tarfile passes it through past
    the first member, where staging must not let it escape as a server error
    instead of a bad archive. Python 3.14's tarfile wraps it as
    ReadError("zlib error: …") itself. Either way it must come out as a
    corrupt archive."""
    import tarfile
    import zlib

    raw = _tar_bytes([(_member("manifest.json"), b"{}"), (_member("wsi.db"), b"w" * 64 * 1024)])
    path = tmp_path / "badblock.tar.gz"
    path.write_bytes(gzip_with_an_undecodable_block(raw, 40 * 1024))
    # The raw shape depends on the interpreter (see above); what matters is
    # that it is the undecodable block that is reported, not something else.
    err = _raw_error(path)
    assert isinstance(err, (zlib.error, tarfile.ReadError)), repr(err)
    assert isinstance(err, zlib.error) or "zlib error" in str(err), repr(err)
    _assert_refused_as_corrupt(path)


def test_an_intact_streamed_zstd_archive_still_reads_to_the_end(tmp_path):
    """The truncation check follows the frame's blocks; a whole archive, as
    the backup writer streams it, still reads to the end."""
    path = tmp_path / "ok.tar.zst"
    path.write_bytes(_compressed_archive("zst-stream"))
    with _backup._open_archive_reader(str(path)) as tar:
        _backup._safe_extract(tar, str(tmp_path / "out"))
    assert _written(tmp_path) == ["manifest.json", "wsi.db"]


# ---------------------------------------------------------------------------
# SQLite sidecars beside the archived wsi.db
# ---------------------------------------------------------------------------

def _archive_members(path: str) -> list[tuple[tarfile.TarInfo, bytes | None]]:
    out = []
    with _backup._open_archive_reader(path) as tar:
        for m in tar:
            data = tar.extractfile(m).read() if m.isreg() else None
            out.append((m, data))
    return out


def _wal_for(db_bytes: bytes, tmp_path) -> bytes:
    """A WAL whose frames, replayed onto ``db_bytes``, add an app_meta row."""
    work = tmp_path / "walsrc"
    work.mkdir()
    db = work / "wsi.db"
    db.write_bytes(db_bytes)
    con = sqlite3.connect(str(db))
    try:
        con.execute("PRAGMA journal_mode=WAL")
        con.execute("PRAGMA wal_autocheckpoint=0")
        con.execute("INSERT INTO app_meta(key, value) VALUES ('smuggled', 'from-the-wal')")
        con.commit()
        wal = (work / "wsi.db-wal").read_bytes()  # before close checkpoints it away
    finally:
        con.close()
    assert wal
    return wal


@pytest.mark.parametrize("suffix", ["-wal", "-shm", "-journal"])
def test_a_sqlite_sidecar_beside_the_archived_db_is_refused(client, admin_headers, tmp_path, suffix):
    """create_backup archives the snapshot alone, in rollback-journal mode,
    so an archive never legitimately carries wsi.db-wal / -shm / -journal.
    One that does would be acted on by the first sqlite3.connect of the
    staging checks: a -wal would be replayed into the staged DB and
    checkpointed into wsi.db (a hot -journal would be rolled back), so bytes no
    check or manifest describes would become the database the restore promotes."""
    backup_id = _make_backup_via_api(client, admin_headers)
    try:
        members = _archive_members(_backup.find_backup(backup_id).path)
        db_bytes = next(d for m, d in members if m.name == _backup.DB_ENTRY)
        stray = _wal_for(db_bytes, tmp_path) if suffix == "-wal" else b"\0" * 4096
        info = tarfile.TarInfo(_backup.DB_ENTRY + suffix)
        info.mode = 0o644
        repacked = tmp_path / "stray.tar.gz"
        with tarfile.open(str(repacked), "w:gz", format=tarfile.PAX_FORMAT) as tar:
            for m, data in members + [(info, stray)]:
                if data is None:
                    tar.addfile(m)
                else:
                    m.size = len(data)
                    tar.addfile(m, io.BytesIO(data))

        before = _staging_entries()
        with pytest.raises(RuntimeError, match=r"wsi\.db" + suffix):
            _backup.stage_restore(str(repacked))
        assert _staging_entries() == before

        # The same archive without the stray member stages normally.
        clean = tmp_path / "clean.tar.gz"
        with tarfile.open(str(clean), "w:gz", format=tarfile.PAX_FORMAT) as tar:
            for m, data in members:
                if data is None:
                    tar.addfile(m)
                else:
                    tar.addfile(m, io.BytesIO(data))
        stage_id, _manifest = _backup.stage_restore(str(clean))
        _backup.cleanup_stage(stage_id)
    finally:
        _backup.delete_backup(backup_id)


def test_extraction_does_not_keep_every_member_in_memory(tmp_path):
    """tarfile keeps each TarInfo it reads in ``members``; for a hostile
    archive of a million long-named entries that alone would be GBs."""
    members = [(_member(f"assets/f{i:04d}"), b"x") for i in range(300)]
    path = tmp_path / "a.tar"
    path.write_bytes(_tar_bytes(members))
    with tarfile.open(str(path), "r|") as tar:
        _backup._safe_extract(tar, str(tmp_path / "out"))
        assert len(tar.members) <= 1
    assert len(_written(tmp_path)) == 301  # the files and assets/


def test_an_over_long_entry_name_is_refused(tmp_path):
    name = "assets/" + "n" * 5000
    with pytest.raises(RuntimeError, match="longer than"):
        _extract(tmp_path, [(_member(name), b"x")])
    assert _written(tmp_path) == []


def test_duplicates_are_still_caught_by_digest(tmp_path):
    with pytest.raises(RuntimeError, match="duplicate entry"):
        _extract(tmp_path, [(_member("assets/a"), b"1"), (_member("assets/./a"), b"2")])


@pytest.mark.parametrize("split", ["mid_member", "member_boundary"])
def test_a_multi_frame_zstd_archive_is_read_whole(tmp_path, split):
    """pzstd and other parallel compressors write several frames; every
    member must come through, not only the first frame's."""
    zstandard = pytest.importorskip("zstandard")
    data = [os.urandom(50_000) for _ in range(3)]
    raw = _tar_bytes([(_member(f"assets/f{i}"), d) for i, d in enumerate(data)])
    cut = len(raw) // 2 + 7 if split == "mid_member" else 512 * 2 + ((50_000 + 511) // 512) * 512
    c = zstandard.ZstdCompressor(write_checksum=True)
    path = tmp_path / "multi.tar.zst"
    path.write_bytes(c.compress(raw[:cut]) + c.compress(raw[cut:]))
    with _backup._open_archive_reader(str(path)) as tar:
        _backup._safe_extract(tar, str(tmp_path / "out"))
    for i, d in enumerate(data):
        assert (tmp_path / "out" / "assets" / f"f{i}").read_bytes() == d
