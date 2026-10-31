"""Importing the same slide bundle more than once, and an import that dies
part-way through, must never leave duplicate slides.

* End to end through the endpoints: one bundle uploaded and applied twice,
  each time with the resolutions the dashboard sends by default
  (frontend/src/lib/admin/importRetry.ts: initialImportResolutions, then
  importResolutionsToSend) — the second run imports nothing.
* An apply killed after N of its slides, then run again: the retry imports
  only the rest, every slide once, each under its bundle public_id.
"""
from __future__ import annotations

import gzip
import io
import json
import os
import tarfile

import pytest

from wsi_app import slide_transfer as _st
from wsi_app.db import SessionLocal
from wsi_app.models import Slide

from .test_slide_transfer import (
    _Crash,
    _base_manifest,
    _patch_wsi_readers,
    _slide_doc,
    _stage,
    _stage_bundle,
    _tok,
)


def _dashboard_resolutions(plan: dict) -> dict:
    """initialImportResolutions + importResolutionsToSend, as the Import tab
    computes what it sends with an apply it was not told to change."""
    init = {}
    for s in plan["slides"]:
        if s.get("apply_status") == "imported":
            continue
        if s.get("apply_status") == "skipped" or s.get("public_id_conflict"):
            init[s["public_id"]] = "skip"
        elif s.get("filename_conflict"):
            init[s["public_id"]] = "copy"
    status = {s["public_id"]: s.get("apply_status") for s in plan["slides"]}
    return {
        pid: v for pid, v in init.items()
        if status.get(pid) != "imported" and not (status.get(pid) == "skipped" and v == "skip")
    }


def _bundle(slides: list, wsi: bytes = b"WSIDATA") -> bytes:
    """A tar.gz slide bundle as an export writes it: manifest, slides.json
    and one placeholder WSI per slide folder."""
    manifest = _base_manifest(
        schema_version=_st.CURRENT_SCHEMA_VERSION, slide_count=len(slides),
        slide_public_ids=[s["public_id"] for s in slides],
    )
    files = {
        "manifest.json": json.dumps(manifest).encode(),
        "slides.json": json.dumps({"slides": slides}).encode(),
    }
    for s in slides:
        files[f"wsi/{s['public_id']}/{s['wsi_original_basename']}"] = wsi
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tar:
        for name, data in files.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    return gzip.compress(buf.getvalue(), compresslevel=1, mtime=0)


def _apply_job(client, headers, stage_id, resolutions) -> dict:
    from .test_slide_transfer import _poll

    r = client.post("/api/admin/slides/import/apply",
                    json={"stage_id": stage_id, "resolutions": resolutions}, headers=headers)
    assert r.status_code == 200, r.text
    return _poll(client, r.json()["job_id"], headers)


def _rows_for(pids) -> dict:
    db = SessionLocal()
    try:
        out = {}
        for pid in pids:
            out[pid] = db.query(Slide).filter(Slide.catalog_id == f"C-{pid}").all()
        return {pid: [(r.id, r.public_id) for r in rows] for pid, rows in out.items()}
    finally:
        db.close()


def test_the_same_bundle_applied_twice_with_dashboard_defaults_imports_once(
    client, admin_headers, monkeypatch,
):
    _patch_wsi_readers(monkeypatch)
    t = _tok()
    slides = [_slide_doc(f"ra{t}", f"twice-a-{t}.svs", []),
              _slide_doc(f"rb{t}", f"twice-b-{t}.svs", [])]
    pids = [s["public_id"] for s in slides]
    content = _bundle(slides)

    first = _stage(client, admin_headers, content, "tar.gz")
    assert _dashboard_resolutions(first["plan"]) == {}  # nothing collides yet
    rj = _apply_job(client, admin_headers, first["stage_id"], _dashboard_resolutions(first["plan"]))
    assert rj["state"] == "succeeded", rj
    assert rj["result"]["imported_count"] == 2, rj["result"]
    after_first = _rows_for(pids)
    # Each slide once, under the public_id the bundle carries.
    assert {pid: [p for _i, p in rows] for pid, rows in after_first.items()} == {p: [p] for p in pids}

    # The applied stage is gone: applying its id again is refused.
    again = _apply_job(client, admin_headers, first["stage_id"], {})
    assert again["state"] == "failed", again
    assert again["result"]["code"] == "adminDash.apiErr.importStageMissing", again
    assert _rows_for(pids) == after_first

    # The same bytes uploaded again: the plan flags every slide as one this
    # system already has, the dashboard defaults them to skip, nothing is added.
    second = _stage(client, admin_headers, content, "tar.gz")
    rows = {s["public_id"]: s for s in second["plan"]["slides"]}
    assert all(rows[p]["public_id_conflict"] for p in pids), rows
    sent = _dashboard_resolutions(second["plan"])
    assert sent == {p: "skip" for p in pids}
    rj = _apply_job(client, admin_headers, second["stage_id"], sent)
    assert rj["state"] == "succeeded", rj
    assert rj["result"]["imported_count"] == 0 and rj["result"]["skipped_count"] == 2, rj["result"]
    assert _rows_for(pids) == after_first
    _st.cleanup_import_stage(second["stage_id"])


@pytest.mark.parametrize("killed_at", [1, 2])
def test_an_import_killed_after_n_slides_is_retried_without_duplicates(
    db_session, monkeypatch, killed_at,
):
    """The process dies as it starts slide ``killed_at + 1`` of three (the
    earlier ones fully imported and recorded). The retry leaves those alone,
    imports the rest, and every slide keeps its bundle public_id."""
    _patch_wsi_readers(monkeypatch)
    t = _tok()
    slides = [_slide_doc(f"k{i}{t}", f"kill-{i}-{t}.svs", []) for i in range(3)]
    pids = [s["public_id"] for s in slides]
    stage_id = _stage_bundle(slides)
    real_reserve = _st._reserve_slide_row
    calls = [0]

    def reserve_or_die(db, desired, before_commit=None):
        calls[0] += 1
        if calls[0] == killed_at + 1:
            raise _Crash()
        return real_reserve(db, desired, before_commit=before_commit)

    try:
        monkeypatch.setattr(_st, "_reserve_slide_row", reserve_or_die)
        with pytest.raises(_Crash):
            _st.apply_import(stage_id, {})
        monkeypatch.setattr(_st, "_reserve_slide_row", real_reserve)
        before = _rows_for(pids)
        assert [len(before[p]) for p in pids] == [1] * killed_at + [0] * (3 - killed_at)

        result = _st.apply_import(stage_id, {})
        assert result["imported_count"] == 3 - killed_at, result
        assert result["already_imported_count"] == killed_at, result
        assert result["failed_count"] == 0 and result["stage_kept"] is False, result
        after = _rows_for(pids)
        assert {p: [pub for _i, pub in after[p]] for p in pids} == {p: [p] for p in pids}
        # The slides imported before the kill are the very same rows.
        for p in pids[:killed_at]:
            assert after[p] == before[p]
        assert not os.path.exists(_st._stage_dir(stage_id))
    finally:
        _st.cleanup_import_stage(stage_id)


def test_a_bundle_with_an_undecodable_deflate_block_is_reported_as_corrupt(client, admin_headers):
    """GzipFile raises zlib.error (neither an OSError nor a TarError) for a
    deflate block it cannot decode; past the first member it would escape staging
    as an uncoded failure instead of importCorrupt."""
    from .test_restore_extraction import gzip_with_an_undecodable_block
    from .test_slide_transfer import _stage_job

    t = _tok()
    blob = _bundle([_slide_doc(f"z{t}", f"zlib-{t}.svs", [])], wsi=b"w" * 64 * 1024)
    raw = gzip.decompress(blob)
    job = _stage_job(client, admin_headers,
                     gzip_with_an_undecodable_block(raw, len(raw) - 32 * 1024), "tar.gz")
    assert job["state"] == "failed", job
    assert job["result"]["code"] == "adminDash.apiErr.importCorrupt", job
