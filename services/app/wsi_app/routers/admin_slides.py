"""Admin slide management: upload, list, update metadata, delete."""
from __future__ import annotations

import contextlib
import json
import logging
import os
import random
import shutil
import time
import uuid
from typing import List, Optional, Type, TypeVar

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.responses import FileResponse, Response
from pydantic import BaseModel, Field
from sqlalchemy import func, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, defer, selectinload

from .._serializers import (
    admin_macro_url,
    label_url as label_image_url,
    server_local_iso,
    slide_attribute_map,
    slide_credit_fields,
    thumb_url,
)
from ..admin_helpers import (
    api_error,
    apply_slide_attribute_delta,
    attach_associated_images as _attach_associated_images,
    audit,
    error_detail,
    mint_public_id,
    tour_asset_lock,
    parse_mpp_value,
    rebuild_slide_attributes,
    reserve_next_slide_id,
    revert_thumb_rotation,
    rotate_thumb_filename,
    thumb_filename as _thumb_filename,
    tour_asset_dir,
    safe_filename,
    slide_mutation_lock,
    stream_upload_to_file,
    try_slide_mutation_lock,
    upload_space_short,
    upload_write_error,
    validate_attribute_kv,
    validate_slide_metadata,
    validated_extension,
    valid_public_id,
)
from ..auth import (
    bearer_token,
    create_slide_download_token,
    decode_slide_download_token,
    require_admin,
    require_manager_or_above,
    require_staff,
    resolve_staff_user,
)
from ..config import settings
from ..db import SessionLocal, get_db
from ..models import Slide, Tour, User, fresh_wsi_generation, tile_version
from ..ome_tiff import UnsupportedSlideError
from ..openslide_utils import make_thumbnail, read_slide_properties
from ..schemas import SlideDetail, SlideOut
from ..slide_credit import clean_credit

router = APIRouter()
logger = logging.getLogger("wsi_app.api")


def _layout_rejection_code(exc: UnsupportedSlideError, path: str) -> str:
    """Log a slide refused for its layout and return its localisable code
    (``adminDash.apiErr.omeTiff.<reason>`` from the OME-TIFF reader,
    ``adminDash.apiErr.slideLayout.<reason>`` for an OpenSlide-read slide).

    A refused layout (multi-channel, no pyramid, ...) is the admin's file, not
    a server fault, so it is logged at INFO — WARNING+ would land in the
    admin error-log screen, which exists to surface real failures.
    """
    logger.info("rejected slide layout %s (%s): %s", path, exc.code, exc)
    return f"adminDash.apiErr.{exc.code}"


# ===== Bulk slide operations =====
#
# Each bulk endpoint takes a list of slide ids and does the work in a single
# request. Looping per-slide requests from the client trips nginx's
# 120r/m burst=40 rate limit (documented in AdminDashboard.tsx / SlideList.tsx),
# so the batching happens server-side.
#
# These fixed-path routes are declared BEFORE the variable `/{slide_id}/...`
# routes on purpose: FastAPI matches in declaration order, and
# `/api/admin/slides/bulk/publish` would otherwise be captured by
# `/api/admin/slides/{slide_id}/publish` (slide_id="bulk") and rejected with a
# 422 int-parse error instead of reaching the handler below.

# Cap per request so a runaway / malicious payload can't pin a worker.
BULK_MAX_SLIDES = 500
# Values per bulk-attribute request. Rows written scale with slides x values
# (and every value lands in the audit entry), so bound this side too. Far above
# what the category modal sends (one key's picked options).
BULK_MAX_ATTR_VALUES = 200


class BulkDeleteRequest(BaseModel):
    slide_ids: List[int]


class BulkPublishRequest(BaseModel):
    slide_ids: List[int]
    publish: bool


class BulkAttrRequest(BaseModel):
    slide_ids: List[int]
    key: str
    values: List[str] = Field(max_length=BULK_MAX_ATTR_VALUES)
    mode: str  # "add" | "remove"


def _failure_reason(exc: BaseException) -> str:
    """What a per-slide failure tells the caller: the exception type, and for an
    OSError the system's reason (``Permission denied``) without the file name.

    These batch endpoints are open to managers, who are deliberately told byte
    counts and never paths (see /api/admin/storage); ``str(exc)`` of an OSError
    or a database error carries the absolute path or the statement. The full
    exception is in the log (``logger.exception`` at the call site)."""
    if isinstance(exc, OSError) and exc.errno:
        return f"{type(exc).__name__}: {os.strerror(exc.errno)}"
    return type(exc).__name__


def _check_bulk_size(slide_ids: List[int]) -> None:
    if len(slide_ids) > BULK_MAX_SLIDES:
        raise api_error(
            422, "adminDash.apiErr.bulkTooMany", max=BULK_MAX_SLIDES, count=len(slide_ids)
        )


def _wsi_file_present(slide: Slide) -> bool:
    """True iff the slide's WSI body actually exists on disk under wsi/.

    The publish gate uses this so a slide whose metadata is complete but
    whose WSI file is missing — the normal state right after a metadata-scope
    restore, before the operator re-uploads / re-copies the files — cannot be
    flipped to published. Publishing it would surface a gallery card that
    404s on every tile request. Reuses ``dzi._slide_path`` so the path-safety
    rules (no traversal, strict descent under wsi/) stay in one place.
    """
    if not (slide.wsi_filename or "").strip():
        return False
    try:
        from ..dzi import _slide_path
        path = _slide_path(slide)
    except FileNotFoundError:
        return False
    return os.path.isfile(path)


def _publish_if_identified(db: Session, slide_id: int) -> bool:
    """Set ``is_published`` only if catalog_id and title are still non-blank.

    The fields are checked in the UPDATE itself rather than on the row read
    earlier: a metadata PATCH blanking one of them could otherwise commit
    between that read and this write and leave a published slide with no
    identifier. The PATCH side guards its blanking the same way (see
    ``_blank_unless_published``), so whichever commits second is refused.
    Returns False when the guard refused the write.
    """
    res = db.execute(
        update(Slide)
        .where(
            Slide.id == slide_id,
            func.trim(func.coalesce(Slide.catalog_id, "")) != "",
            func.trim(func.coalesce(Slide.title, "")) != "",
        )
        .values(is_published=True)
        .execution_options(synchronize_session="fetch")
    )
    return res.rowcount == 1


def _blank_unless_published(db: Session, slide_id: int, column) -> bool:
    """Blank ``column`` unless the slide is published with a non-blank value.

    Counterpart of :func:`_publish_if_identified`: evaluated against the row
    as it stands at write time, not as this request first read it, so a
    publish committing in between can't be overwritten by a clear that was
    only permitted on the draft. A published slide whose field is already
    blank (a restored or hand-edited DB) still accepts the no-op.
    Returns False when the guard refused the write.
    """
    res = db.execute(
        update(Slide)
        .where(
            Slide.id == slide_id,
            or_(
                Slide.is_published.is_(False),
                func.trim(func.coalesce(column, "")) == "",
            ),
        )
        .values({column.key: ""})
        .execution_options(synchronize_session="fetch")
    )
    return res.rowcount == 1


def _guarded_data_path(subdir: str, filename: str) -> Optional[str]:
    """Resolve ``<data_dir>/<subdir>/<filename>`` under a realpath/strict-descent
    guard, or ``None`` when the value is empty or escapes the subdir.

    Mirrors ``dzi._slide_path`` for data directories other than ``wsi/`` (e.g.
    ``thumbs/``): the DB row is mutable, so a restored/hand-edited traversal
    sequence must not let an os.remove follow a path outside the data root.
    """
    name = (filename or "").strip()
    if not name:
        return None
    base = os.path.realpath(os.path.join(settings.data_dir, subdir))
    full = os.path.realpath(os.path.join(base, name))
    if not full.startswith(base + os.sep):
        return None
    return full


# A slide row whose wsi_filename is still empty is an id reservation: an
# upload (upload_slide) or a slide import (slide_transfer) committed it and
# holds the slide's mutation lock until it finalises or cleans up — for the
# whole multi-GB body stream or copy, minutes. The admin list shows that row
# meanwhile, so an operator can aim a delete or publish at it, and waiting on
# the lock would hold the request (and a worker thread) until the proxy timed
# it out. Such a request is refused with a 409 instead. Every other holder of
# the lock keeps it for a short critical section, so a real slide's request
# still waits its turn. A reservation whose lock is free (its upload died and
# the boot reaper has not run yet) is acted on normally, so a ghost row can
# still be deleted.
_RESERVATION_BUSY_DETAIL = (
    "This slide is still being uploaded or imported. Try again once that has finished."
)


class _SlideReservationBusy(HTTPException):
    """The slide is an id reservation whose upload / import still holds it.
    A 409 as it stands, for the single-slide endpoints; the bulk ones catch
    it and report that slide as busy."""

    def __init__(self) -> None:
        super().__init__(status_code=409, detail=_RESERVATION_BUSY_DETAIL)


def _is_unfinished_reservation(slide_id: int) -> bool:
    # Its own short-lived session, like _do_delete_slide's tour_ids_now: a
    # read on the request's session here would start its snapshot before the
    # lock is held, and the re-read under the lock would then miss what the
    # holder committed.
    with SessionLocal() as probe:
        name = probe.execute(
            select(Slide.wsi_filename).where(Slide.id == slide_id)
        ).scalar_one_or_none()
    return name is not None and not name.strip()


@contextlib.contextmanager
def _slide_lock_unless_reserving(slide_id: int):
    """:func:`slide_mutation_lock`, except that it raises
    :class:`_SlideReservationBusy` rather than wait while an upload or import
    still holds a reservation (see the comment above)."""
    with try_slide_mutation_lock(slide_id) as acquired:
        if acquired:
            yield
            return
    if _is_unfinished_reservation(slide_id):
        raise _SlideReservationBusy()
    with slide_mutation_lock(slide_id):
        yield


@router.post("/api/admin/slides/bulk/delete")
def admin_bulk_delete_slides(
    body: BulkDeleteRequest,
    admin: User = Depends(require_manager_or_above),
    db: Session = Depends(get_db),
):
    """Delete several slides best-effort.

    Each slide is deleted under its own per-slide lock and its own commit
    (via _do_delete_slide), so one failure never rolls back the slides that
    already succeeded. Failures are bucketed so the client can keep the
    failed ids selected and retry:

      * deleted   — row + files gone (a post-commit file-removal hiccup is
                    non-fatal and still counts as deleted).
      * not_found — already gone (e.g. removed in another tab); not retryable.
      * failed    — a pre-commit error (the row still exists); retryable.
    """
    _check_bulk_size(body.slide_ids)
    deleted: List[int] = []
    not_found: List[int] = []
    failed: List[dict] = []
    for slide_id in body.slide_ids:
        try:
            with _slide_lock_unless_reserving(slide_id):
                _do_delete_slide(slide_id, admin, db)
            deleted.append(slide_id)
        except _SlideReservationBusy:
            # Retryable once the upload / import finishes (it may yet fail
            # and clean the row up itself).
            failed.append({"slide_id": slide_id, "error": _RESERVATION_BUSY_DETAIL})
        except HTTPException as exc:
            # _do_delete_slide commits before touching files, so a 404 here
            # means the row was never deleted — recover the session and move on.
            db.rollback()
            if exc.status_code == 404:
                not_found.append(slide_id)
            else:
                failed.append({"slide_id": slide_id, "error": str(exc.detail)})
        except Exception as exc:  # noqa: BLE001 — must not abort the whole batch
            # Recover the session so the next id isn't poisoned by a pending
            # failed transaction (mirrors the rollback pattern used elsewhere
            # in this router). The row still exists, so this is retryable.
            db.rollback()
            logger.exception("bulk delete failed for slide %s", slide_id)
            failed.append({"slide_id": slide_id, "error": _failure_reason(exc)})
    audit(
        admin,
        "slide.bulk_delete",
        requested=len(body.slide_ids),
        deleted=deleted,
        not_found=not_found,
        failed=[f["slide_id"] for f in failed],
    )
    return {"ok": True, "deleted": deleted, "not_found": not_found, "failed": failed}


@router.post("/api/admin/slides/bulk/publish")
def admin_bulk_publish_slides(
    body: BulkPublishRequest,
    admin: User = Depends(require_staff),
    db: Session = Depends(get_db),
):
    """Publish / unpublish several slides, returning a per-slide result.

    Mirrors admin_publish_slide for each id but never raises on a per-slide
    problem — a slide that can't be published (missing catalog_id/title) or
    doesn't exist is reported via its result entry so the rest of the batch
    still goes through. Unpublishing cascades published tours to draft, same
    as the single endpoint.
    """
    _check_bulk_size(body.slide_ids)
    new_state = body.publish
    results: List[dict] = []
    changed_ids: List[int] = []
    # Process each slide under its own per-slide lock with an independent
    # commit, mirroring admin_bulk_delete_slides. Two reasons:
    #   * the lock spans the _wsi_file_present gate and the commit, so a
    #     concurrent delete / replace_wsi (same lock) cannot pull the WSI body
    #     out from under a slide between the check and the flip — otherwise a
    #     slide could end up published with no backing file (404 tiles).
    #   * per-slide commits make this best-effort: one slide's commit failure
    #     is reported in its own result entry instead of rolling back the
    #     whole batch.
    for slide_id in body.slide_ids:
        with contextlib.ExitStack() as held:
            try:
                held.enter_context(_slide_lock_unless_reserving(slide_id))
            except _SlideReservationBusy:
                # An upload / import still holds this reservation; there is
                # no image to publish yet, and waiting would stall the batch.
                results.append({"slide_id": slide_id, "ok": False, "is_published": False,
                                "cascaded_tours": 0, "error": "busy"})
                continue
            s = db.get(Slide, slide_id)
            if not s:
                results.append({"slide_id": slide_id, "ok": False, "is_published": False,
                                "cascaded_tours": 0, "error": "not_found"})
                continue
            if new_state and (not (s.catalog_id or "").strip() or not (s.title or "").strip()):
                results.append({"slide_id": slide_id, "ok": False, "is_published": s.is_published,
                                "cascaded_tours": 0, "error": "missing_fields"})
                continue
            if new_state and not _wsi_file_present(s):
                # WSI body absent on disk (e.g. after a metadata-scope restore,
                # before re-upload). Publishing would yield 404 tiles; refuse,
                # same as the single-slide endpoint.
                results.append({"slide_id": slide_id, "ok": False, "is_published": s.is_published,
                                "cascaded_tours": 0, "error": "missing_wsi"})
                continue
            cascaded = 0
            if s.is_published != new_state:
                if new_state:
                    if not _publish_if_identified(db, slide_id):
                        # A concurrent metadata edit blanked a field after
                        # the check above (see _publish_if_identified).
                        db.rollback()
                        results.append({"slide_id": slide_id, "ok": False, "is_published": False,
                                        "cascaded_tours": 0, "error": "missing_fields"})
                        continue
                else:
                    s.is_published = False
                rotated_from: Optional[str] = None
                rotated_to: Optional[str] = None
                if not new_state:
                    # Rotate the public thumbnail's capability token, exactly as
                    # the single-slide endpoint does: /thumbs is an ungated
                    # static mount, so the token in the filename is the only
                    # thing gating a URL handed out while the slide was live.
                    # Without this, bulk-unpublishing would leave every
                    # previously shared thumbnail URL resolving forever.
                    # Best-effort — a rename failure returns None and keeps the
                    # old name rather than failing the toggle.
                    old_thumb = (s.thumb_filename or "").strip()
                    new_thumb = rotate_thumb_filename(s)
                    if new_thumb:
                        rotated_from, rotated_to = old_thumb, new_thumb
                        s.thumb_filename = new_thumb
                    cascaded += _cascade_tours_to_draft(db, s.id)
                try:
                    db.commit()
                except Exception:
                    db.rollback()
                    # The rotation already renamed the file on disk; put it back
                    # so the rolled-back row's thumb_filename still resolves.
                    if rotated_from and rotated_to:
                        revert_thumb_rotation(rotated_from, rotated_to)
                    logger.exception("bulk publish commit failed for slide %s", slide_id)
                    results.append({"slide_id": slide_id, "ok": False,
                                    "is_published": not new_state, "cascaded_tours": 0,
                                    "error": "commit_failed"})
                    continue
                changed_ids.append(slide_id)
            results.append({"slide_id": slide_id, "ok": True, "is_published": new_state,
                            "cascaded_tours": cascaded, "error": None})
    # Evict the DZI cache for every slide whose publish flag actually flipped
    # so the new state takes effect on the next tile request (see the single
    # publish endpoint for why).
    for slide_id in changed_ids:
        try:
            from ..dzi import clear_dz_cache
            clear_dz_cache(slide_id)
        except Exception:
            logger.exception("Could not evict DZI cache after bulk publish toggle")
    if changed_ids:
        # Flush the public_id -> (id, is_published) resolve cache on this worker
        # and bump the cross-worker sentinel so the new publish state reaches
        # the /dzi/ tile path everywhere (see slide_resolve_cache).
        try:
            from .. import slide_resolve_cache
            slide_resolve_cache.invalidate_slide_meta()
        except Exception:
            logger.exception("Could not invalidate slide-meta cache after bulk publish toggle")
    audit(
        admin,
        "slide.bulk_publish",
        published=new_state,
        requested=len(body.slide_ids),
        changed=changed_ids,
    )
    return {"ok": True, "results": results}


@router.post("/api/admin/slides/bulk/attributes")
def admin_bulk_slide_attributes(
    body: BulkAttrRequest,
    admin: User = Depends(require_manager_or_above),
    db: Session = Depends(get_db),
):
    """Add or remove one category (key) + value(s) across several slides.

    The selection may be heterogeneous — some slides already carry the value,
    some don't. "add" only touches slides missing the value; "remove" only
    touches slides that have it. `changed` lists the ids whose attribute set
    actually moved (no-ops are excluded).
    """
    _check_bulk_size(body.slide_ids)
    if body.mode not in ("add", "remove"):
        raise api_error(422, "adminDash.apiErr.invalidMode", mode=body.mode)
    key = (body.key or "").strip()
    if not key:
        raise api_error(422, "adminDash.apiErr.attrKeyRequired")
    validate_attribute_kv(key, body.values)
    changed: List[int] = []
    not_found: List[int] = []
    for slide_id in body.slide_ids:
        s = db.get(Slide, slide_id)
        if not s:
            not_found.append(slide_id)
            continue
        if apply_slide_attribute_delta(s, key, body.values, body.mode):
            changed.append(slide_id)
    try:
        db.commit()
    except Exception:
        db.rollback()
        logger.exception("bulk attribute commit failed")
        raise api_error(500, "adminDash.apiErr.bulkCategoryFailed")
    audit(
        admin,
        "slide.bulk_attributes",
        key=key,
        mode=body.mode,
        values=body.values,
        changed=changed,
        not_found=not_found,
    )
    return {"ok": True, "changed": changed, "not_found": not_found}


def _is_duplicate_filename_violation(exc: IntegrityError) -> bool:
    """Whether ``exc`` is the UNIQUE(wsi_filename_norm) backstop firing.

    Matched on the driver's message ("UNIQUE constraint failed:
    slides.wsi_filename_norm"), not ``str(exc)``, which embeds the whole
    statement — whose column list names wsi_filename_norm for ANY constraint
    failure on the slide row (as in slide_transfer's import)."""
    return "wsi_filename_norm" in str(getattr(exc, "orig", "") or "")


def _assert_unique_wsi_filename(
    db: Session,
    safe_name: str,
    *,
    exclude_slide_id: Optional[int] = None,
    on_conflict=None,
) -> None:
    """Reject uploading a WSI whose original filename collides with another slide.

    Files are stored on disk as ``{slide_id}_{safe_name}`` so the on-disk paths
    never clash, but the *original* name (the ``safe_name`` portion) is what
    operators see in the dashboard. Allowing two slides to share it produces
    indistinguishable entries and accidental double-registrations, so it is
    forbidden. Matching is case-insensitive. ``exclude_slide_id`` skips the
    slide being replaced so a re-upload of the same file onto it (e.g. after a
    metadata-only restore) still works.

    Resolved with a single indexed lookup on ``wsi_filename_norm`` — the
    case-folded original basename that every write path (upload finalise,
    replace, bundle import) stores and that carries a UNIQUE index — instead of
    scanning every slide row in Python and re-deriving the basename. NULL norm
    rows (pre-upload placeholders with no file yet) never match ``== target``,
    the same rows a row-by-row scan would skip with ``if not fname: continue``.

    ``on_conflict`` is invoked before raising, so a caller that has already
    staged bytes on disk (the replace flow re-checks *after* streaming) can drop
    them instead of leaking a temp file on every 409.
    """
    target = safe_name.casefold()
    stmt = select(Slide.id).where(Slide.wsi_filename_norm == target)
    if exclude_slide_id is not None:
        stmt = stmt.where(Slide.id != exclude_slide_id)
    if db.execute(stmt.limit(1)).first() is not None:
        if on_conflict is not None:
            on_conflict()
        raise api_error(409, "adminDash.apiErr.duplicateFilename")


@router.post("/api/admin/slides/upload")
def upload_slide(
    file: UploadFile = File(...),
    admin: User = Depends(require_manager_or_above),
    db: Session = Depends(get_db),
):
    """Upload a new WSI file. The slide ID monotonically advances past every
    id ever assigned, so it is never reused after a delete.

    Metadata is entered separately after upload via the slide edit UI; values
    come from the master list managed in the Metadata tab.
    """
    filename = file.filename or "slide.wsi"
    safe_name = safe_filename(filename)
    validated_extension(safe_name)
    # The body is already spooled here (Starlette parses it before the
    # endpoint runs); what is left is the copy to the staged WSI.
    if upload_space_short(file.size or 0, factor=1):
        raise api_error(507, "adminDash.apiErr.uploadNoSpace")
    # Reject duplicate names before reserving an ID so a collision returns 409
    # immediately without creating (and then deleting) a placeholder row.
    _assert_unique_wsi_filename(db, safe_name)

    # Reserve the slide ID atomically by inserting (and committing) an empty
    # row first. SQLite's write lock serialises concurrent reservations, so
    # one of two simultaneous uploads will fail at commit with a UNIQUE
    # violation on slides.id and retry the reservation with a higher id.
    # The commit also happens immediately so the write lock isn't held for
    # the duration of the upload (potentially many GB).
    #
    # Explicit id assignment via ``reserve_next_slide_id`` sidesteps SQLite's
    # rowid recycling. Without it the public viewer keeps serving a deleted
    # slide's pyramid under the reused id, because nginx caches /dzi/{id}.dzi
    # for 30 days and each worker holds the old OpenSlide handle in its
    # _dz_cache.
    slide: Optional[Slide] = None
    last_err: Optional[Exception] = None
    # Many concurrent uploads all read the same max(id)+1 and race to insert it;
    # with only a handful of retries the unlucky ones would run out (500). Retry more
    # and back off by a few random ms so the racers spread out.
    for _attempt in range(20):
        candidate_id = reserve_next_slide_id(db)
        # Mint the public_id here (inside the retry loop) so a unique collision
        # on either the id or the public_id is absorbed by the same rollback +
        # retry — a fresh token is minted on the next iteration.
        slide = Slide(id=candidate_id, public_id=mint_public_id())
        db.add(slide)
        try:
            db.commit()
            break
        except IntegrityError as exc:
            # A racing worker committed the same id first. Roll back and
            # let the next iteration read a freshly-advanced watermark.
            db.rollback()
            last_err = exc
            slide = None
            time.sleep(random.uniform(0.0, 0.02))
        except Exception:
            db.rollback()
            logger.exception("Failed to reserve slide ID")
            raise HTTPException(status_code=500, detail="Failed to reserve slide ID")
    if slide is None:
        logger.error("Failed to reserve slide ID after retries", exc_info=last_err)
        raise HTTPException(status_code=500, detail="Failed to reserve slide ID")
    db.refresh(slide)
    slide_id = slide.id

    # From here on, treat any work on this slide_id as a serialised
    # mutation. A concurrent replace_wsi / delete on the same id can only
    # come from an admin who already knows the id, which doesn't happen on
    # a fresh upload — but a paranoid lock costs nothing and keeps the
    # behaviour symmetric with replace_wsi.
    with slide_mutation_lock(slide_id):
        return _do_upload_slide_finalise(slide, slide_id, file, admin, db)


def _do_upload_slide_finalise(
    slide: "Slide",
    slide_id: int,
    file: UploadFile,
    admin: User,
    db: Session,
):
    # data_dir/{wsi,thumbs} are created by ensure_dirs() on app startup
    # (see main.lifespan); no need to re-create them on every upload.
    filename = file.filename or "slide.wsi"
    safe_name = safe_filename(filename)
    # extension was already validated above; the helper is cheap to re-run
    # here so the finaliser stays self-contained.
    validated_extension(safe_name)
    wsi_dir = os.path.join(settings.data_dir, "wsi")
    # Prefix with slide_id so uploads of same-named files from different slides
    # don't collide on disk.
    stored_name = f"{slide_id}_{safe_name}"
    wsi_path = os.path.join(wsi_dir, stored_name)
    thumb_name = _thumb_filename(slide_id, slide.thumb_filename)
    thumb_path = os.path.join(settings.data_dir, "thumbs", thumb_name)

    # Stage the upload to a sibling ``.upload-<token>`` path and only
    # ``os.replace`` it onto the final ``{id}_{name}`` name once it has
    # streamed and validated cleanly (mirrors admin_replace_slide_wsi). Without
    # staging, a crash mid-stream leaves a real-looking partial WSI at the
    # final path that admin_slide_file_status reports as an orphaned,
    # re-importable file; a ``.upload-<token>`` remnant is instead classified
    # as a temp file. The token is inserted *before* the extension so the
    # staging file still ends in the real suffix — read_slide_properties /
    # make_thumbnail dispatch SZI vs OpenSlide by extension, so a bare
    # ``.upload-<token>`` tail (not ``.szi``) would misroute an SZI upload.
    tmp_token = uuid.uuid4().hex[:12]
    _root, _ext = os.path.splitext(wsi_path)
    wsi_tmp_path = f"{_root}.upload-{tmp_token}{_ext}"

    def _abort(status: int, detail) -> HTTPException:
        # Drop both the placeholder slide row and any side-effect files (the
        # staged upload, the final WSI once swapped in, and the thumbnail) so
        # a failed upload leaves no trace.
        for p in (wsi_tmp_path, wsi_path, thumb_path):
            try:
                os.remove(p)
            except OSError:
                pass
        # Roll back first: a failed commit / flush leaves the session unusable
        # until it is rolled back, and the delete below would then raise
        # PendingRollbackError and strand the placeholder row. Harmless when
        # nothing is pending (the row itself was committed at reservation).
        db.rollback()
        try:
            db.delete(slide)
            db.commit()
        except Exception:
            db.rollback()
        return HTTPException(status_code=status, detail=detail)

    try:
        stream_upload_to_file(file, wsi_tmp_path)
    except BaseException as exc:
        # ANY failure during the streaming upload — HTTPException (size cap
        # hit), ClientDisconnect (browser tab closed mid-upload), OSError
        # (disk full), and so on — must release the placeholder slide row.
        # Catching only HTTPException would leave the row stranded with
        # an empty wsi_filename whenever the client aborts the request,
        # producing ghost slides that the dashboard renders as broken
        # tiles and that backup counts toward slide_count.
        # stream_upload_to_file already removes any partial file on its
        # error path.
        try:
            db.delete(slide)
            db.commit()
        except Exception:
            db.rollback()
        # Same mapping as the replace flow: a full disk or any other storage
        # failure is a localisable error, not a bare 500.
        if isinstance(exc, Exception) and not isinstance(exc, HTTPException):
            logger.exception("Upload streaming failed for new slide %s", slide_id)
            raise upload_write_error(exc) from exc
        raise

    try:
        props = read_slide_properties(wsi_tmp_path)
    except UnsupportedSlideError as exc:
        raise _abort(422, error_detail(_layout_rejection_code(exc, wsi_tmp_path)))
    except Exception:
        logger.exception("OpenSlide failed to read uploaded slide %s", wsi_tmp_path)
        raise _abort(422, error_detail("adminDash.apiErr.wsiUnreadable"))

    try:
        make_thumbnail(wsi_tmp_path, thumb_path)
    except Exception:
        logger.exception("Thumbnail generation failed for %s", wsi_tmp_path)
        raise _abort(422, error_detail("adminDash.apiErr.thumbnailFailed"))

    # Validation passed on the staged file — move it onto the final
    # {id}_{name} path atomically (same directory keeps os.replace atomic).
    # Only after this does anything read wsi_path (associated images, size).
    try:
        os.replace(wsi_tmp_path, wsi_path)
    except OSError:
        logger.exception("Failed to move uploaded slide %s into place", slide_id)
        raise _abort(500, error_detail("adminDash.apiErr.saveFailed"))

    slide.wsi_filename = stored_name
    # Normalised basename feeding the UNIQUE index — the race-free backstop
    # to the _assert_unique_wsi_filename checks above/below.
    slide.wsi_filename_norm = safe_name.casefold()
    slide.thumb_filename = thumb_name
    slide.width = props["width"]
    slide.height = props["height"]
    slide.mpp_x = props["mpp_x"]
    slide.mpp_y = props["mpp_y"]
    slide.mpp_x_file = props["mpp_x"]
    slide.mpp_y_file = props["mpp_y"]
    slide.objective_power_file = props.get("objective_power", 0.0) or 0.0
    slide.vendor = props["vendor"]
    slide.acquired_at = props.get("acquired_at", "") or ""
    try:
        slide.file_size_bytes = os.path.getsize(wsi_path)
    except OSError:
        slide.file_size_bytes = 0
    _attach_associated_images(slide, wsi_path)

    # Re-check uniqueness immediately before committing. The check at the top
    # of upload_slide ran before the (potentially multi-GB, multi-minute)
    # stream completed, so a second upload of the same filename could have
    # slipped through and committed in the meantime. Excluding this slide_id
    # (whose wsi_filename is still NULL on disk until the commit below) keeps
    # the re-upload-onto-self case working. This shrinks the collision window
    # to the check→commit gap, which the UNIQUE index on wsi_filename_norm then
    # closes at commit (see the IntegrityError handler below). Route a
    # collision through _abort so the placeholder row + uploaded file are
    # cleaned up instead of stranded as an orphan.
    try:
        _assert_unique_wsi_filename(db, safe_name, exclude_slide_id=slide_id)
    except HTTPException as exc:
        raise _abort(exc.status_code, exc.detail or "duplicate filename")

    try:
        db.commit()
    except IntegrityError as exc:
        # The UNIQUE index on slides.wsi_filename_norm is the race-free
        # backstop: a concurrent upload of the same original name that
        # committed during this request's check->commit gap collides here.
        # Roll back the failed commit before _abort touches the session,
        # then clean up the placeholder row + uploaded file and report the
        # same 409 the app-level check raises. Any other constraint failure
        # is not a duplicate name and must not be reported as one.
        db.rollback()
        if _is_duplicate_filename_violation(exc):
            raise _abort(409, error_detail("adminDash.apiErr.duplicateFilename"))
        logger.exception("Failed to finalise slide %s", slide_id)
        raise _abort(500, error_detail("adminDash.apiErr.saveFailed"))
    except Exception:
        logger.exception("Failed to finalise slide %s", slide_id)
        raise _abort(500, error_detail("adminDash.apiErr.saveFailed"))

    audit(admin, "slide.upload", target=str(slide_id), filename=stored_name)
    return {"ok": True, "slide_id": slide_id}


_SlideOutT = TypeVar("_SlideOutT", bound=SlideOut)


def _admin_slide_out(s: Slide, model: Type[_SlideOutT]) -> _SlideOutT:
    """A slide as the admin list and detail endpoints return it: drafts
    included, with the admin-only macro / label URLs (which need the staff
    Bearer token) and the tile version the admin preview viewer opens."""
    return model(
        id=s.id, public_id=s.public_id or "", catalog_id=s.catalog_id, title=s.title,
        thumb_url=thumb_url(s), width=s.width, height=s.height, mpp_x=s.mpp_x, mpp_y=s.mpp_y,
        mpp_x_file=s.mpp_x_file, mpp_y_file=s.mpp_y_file,
        objective_power_file=s.objective_power_file or 0.0,
        vendor=s.vendor, wsi_filename=s.wsi_filename,
        file_size_bytes=s.file_size_bytes or 0,
        attributes=slide_attribute_map(s),
        show_zoom_indicator=s.show_zoom_indicator,
        show_scale_bar=s.show_scale_bar,
        show_metadata_panel=s.show_metadata_panel,
        image_adjust_brightness=s.image_adjust_brightness,
        image_adjust_contrast=s.image_adjust_contrast,
        image_adjust_saturation=s.image_adjust_saturation,
        is_published=bool(s.is_published),
        unlisted=bool(s.unlisted),
        macro_url=admin_macro_url(s),
        label_url=label_image_url(s),
        acquired_at=s.acquired_at or "",
        **slide_credit_fields(s),
        created_at=server_local_iso(s.created_at),
        tile_version=tile_version(s.wsi_generation),
    )


@router.get("/api/admin/slides", response_model=List[SlideOut])
def admin_list_slides(admin: User = Depends(require_staff), db: Session = Depends(get_db)):
    # Never load the LargeBinary macro_data / label_data blobs here: they can
    # run to tens of MB per slide and this list returns every row (see the same
    # guard on /api/admin/stats). Presence for macro_url/label_url is derived
    # from the small *_etag columns, so deferring the blobs is transparent.
    slides = db.execute(
        select(Slide)
        .options(
            selectinload(Slide.attributes),
            defer(Slide.macro_data),
            defer(Slide.label_data),
        )
        .order_by(Slide.id.asc())
    ).scalars().all()
    return [_admin_slide_out(s, SlideOut) for s in slides]


@router.get("/api/admin/slides/file-status")
def admin_slide_file_status(
    admin: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    """Return WSI file presence for every slide and a list of orphaned files.

    Useful after a metadata-only restore: the DB is swapped in but the wsi/
    directory is not touched, so files from a previous install may be missing
    (or there may be surplus files no longer referenced by any slide row).
    """
    wsi_dir = os.path.join(settings.data_dir, "wsi")
    # Defer the LargeBinary blobs: this scans every slide row and only reads
    # scalar metadata (id/title/wsi_filename) below.
    slides = db.execute(
        select(Slide)
        .options(defer(Slide.macro_data), defer(Slide.label_data))
        .order_by(Slide.id.asc())
    ).scalars().all()

    referenced: set[str] = set()
    slide_statuses = []
    for s in slides:
        filename = s.wsi_filename or ""
        exists = False
        if filename:
            referenced.add(filename)
            # Probe presence through the same realpath/strict-descent guard the
            # serving paths use, so a crafted wsi_filename (reachable via a
            # restored malicious DB) can't be used to probe arbitrary files
            # outside wsi/. A guard failure means "not a real slide file under
            # wsi/" -> report absent.
            try:
                from ..dzi import _slide_path
                path = _slide_path(s)
            except FileNotFoundError:
                path = None
            exists = bool(path and os.path.isfile(path))
        slide_statuses.append({
            "id": s.id,
            "catalog_id": s.catalog_id,
            "title": s.title,
            "wsi_filename": filename,
            "file_exists": exists,
        })

    orphaned: list[str] = []
    temp_files: list[str] = []
    try:
        if os.path.isdir(wsi_dir):
            for fname in sorted(os.listdir(wsi_dir)):
                if not os.path.isfile(os.path.join(wsi_dir, fname)):
                    continue
                if fname in referenced:
                    continue
                # Distinguish in-progress / rollback temp files from genuinely
                # orphaned WSI files. Temp names are suffixed by
                # stream_upload_to_file (.upload-<token>) and the WSI swap
                # (.rollback-<token>). They appear when a replace_wsi operation
                # is interrupted mid-flight and its own cleanup also fails.
                # Listing them separately avoids confusing operators who may
                # otherwise mistake them for data that should be re-imported.
                if ".upload-" in fname or ".rollback-" in fname:
                    temp_files.append(fname)
                else:
                    orphaned.append(fname)
    except OSError:
        pass

    present = sum(1 for s in slide_statuses if s["file_exists"])
    return {
        "total": len(slide_statuses),
        "present": present,
        "missing": len(slide_statuses) - present,
        "slides": slide_statuses,
        "orphaned_files": orphaned,
        "temp_files": temp_files,
        "wsi_dir": wsi_dir,
    }


@router.get("/api/admin/slides/{slide_id}", response_model=SlideDetail)
def admin_get_slide(
    slide_id: int,
    admin: User = Depends(require_staff),
    db: Session = Depends(get_db),
):
    """Return a single slide's full detail regardless of publish state.

    The public /api/slides/{id} endpoint hides drafts so unauthenticated
    probes can't enumerate them; the admin tour editor and the admin-side
    viewer preview need draft slides too, so they call this endpoint
    instead.
    """
    s = (
        db.execute(
            select(Slide).options(selectinload(Slide.attributes)).where(Slide.id == slide_id)
        )
        .scalars()
        .one_or_none()
    )
    if not s:
        raise HTTPException(status_code=404, detail="Slide not found")
    return _admin_slide_out(s, SlideDetail)


@router.post("/api/admin/slides/{slide_id}/publish")
def admin_publish_slide(
    slide_id: int,
    publish: int = Form(1),
    admin: User = Depends(require_staff),
    db: Session = Depends(get_db),
):
    """Toggle a slide's published state.

    Unpublishing a slide cascades to every tour attached to it: the public
    viewer is unreachable while the slide is a draft, so leaving the tours
    flagged as published would let direct tour URLs surface content the
    admin clearly intended to hide. Re-publishing the slide does NOT
    auto-republish its tours — the admin must republish each tour
    explicitly so a slide flip doesn't silently reveal in-progress drafts.
    """
    new_state = bool(publish)
    # Hold the per-slide mutation lock across the file-presence gate and the
    # commit so a concurrent delete / replace_wsi (which take the same lock)
    # cannot remove the WSI body between the _wsi_file_present check and the
    # commit, which would otherwise leave the slide published with no backing
    # file (404 tiles). Re-fetch inside the lock for the same reason. Mirrors
    # admin_delete_slide, which already serialises through this lock. A
    # reservation still being uploaded / imported is a 409, not a wait (see
    # _slide_lock_unless_reserving).
    with _slide_lock_unless_reserving(slide_id):
        s = db.get(Slide, slide_id)
        if not s:
            raise HTTPException(status_code=404, detail="Slide not found")
        # Require both catalog_id and title before going public: without them
        # the public listing renders as "Slide #N" with no identifier, which
        # is confusing to viewers and effectively unpublishable content.
        # Drafts may still be saved with these fields blank.
        if new_state and (not (s.catalog_id or "").strip() or not (s.title or "").strip()):
            raise api_error(400, "adminDash.apiErr.publishMissingFields")
        # Refuse to publish a slide whose WSI body is missing on disk. This is
        # the state a slide is left in after a metadata-scope restore (or any
        # restore onto a host that doesn't yet have the files): the row carries
        # a wsi_filename but no bytes back it, so a published slide would 404
        # on every tile request. The operator must re-upload (or re-copy +
        # rebuild thumbnails) before the slide can go public again.
        if new_state and not _wsi_file_present(s):
            raise api_error(400, "adminDash.apiErr.publishMissingFile")
        if new_state:
            if not _publish_if_identified(db, slide_id):
                db.rollback()
                raise api_error(400, "adminDash.apiErr.publishMissingFields")
        else:
            s.is_published = False
        cascaded = 0
        rotated_from: Optional[str] = None
        rotated_to: Optional[str] = None
        if not new_state:
            # Rotate the public thumbnail's capability token so any URL handed
            # out while the slide was live (public index / gallery cards) stops
            # resolving — the /thumbs mount is ungated, so the token is the only
            # gate. Best-effort: a rename failure returns None and leaves the
            # old name; the toggle must not fail over thumbnail housekeeping.
            old_thumb = (s.thumb_filename or "").strip()
            new_thumb = rotate_thumb_filename(s)
            if new_thumb:
                rotated_from, rotated_to = old_thumb, new_thumb
                s.thumb_filename = new_thumb
            cascaded += _cascade_tours_to_draft(db, s.id)
        try:
            db.commit()
        except Exception:
            db.rollback()
            # The rename above already landed on disk. Undo it, or the
            # rolled-back row would point at a filename that no longer exists
            # and the thumbnail would 404 until a manual rebuild.
            if rotated_from and rotated_to:
                revert_thumb_rotation(rotated_from, rotated_to)
            raise
    # The /dzi/ tile path caches the slide's publish flag in its LRU
    # entry to avoid a per-tile DB hit; drop that entry so the new state
    # takes effect on the next request instead of waiting for the
    # OpenSlide handle to age out of the cache.
    try:
        from ..dzi import clear_dz_cache
        clear_dz_cache(slide_id)
    except Exception:
        logger.exception("Could not evict DZI cache after publish toggle")
    # Flush the public_id -> (id, is_published) resolve cache and bump the
    # cross-worker sentinel so the /dzi/ tile path reflects the new publish
    # state on this worker immediately and on the others within the check
    # interval (see slide_resolve_cache).
    try:
        from .. import slide_resolve_cache
        slide_resolve_cache.invalidate_slide_meta()
    except Exception:
        logger.exception("Could not invalidate slide-meta cache after publish toggle")
    audit(
        admin,
        "slide.publish",
        target=str(slide_id),
        published=new_state,
        cascaded_tours=cascaded,
    )
    return {"ok": True, "is_published": new_state, "cascaded_tours": cascaded}


@router.post("/api/admin/slides/{slide_id}/unlisted")
def admin_set_slide_unlisted(
    slide_id: int,
    unlisted: int = Form(1),
    admin: User = Depends(require_staff),
    db: Session = Depends(get_db),
):
    """Toggle a slide's link-only ("unlisted") visibility.

    Orthogonal to publish: unlisting only removes the slide from the public
    index listing / search / facets (see public.list_slides & public.facets);
    the direct /api/slides/{id} URL and its tiles keep serving as long as the
    slide is published. It is settable in any publish state so an admin can
    pre-mark a draft as link-only *before* publishing — that way the slide
    never flashes onto the top page in the window between the two toggles.

    No DZI cache eviction (unlike publish): the tile path keys its cache on the
    publish flag only, which unlisting does not change.
    """
    s = db.get(Slide, slide_id)
    if not s:
        raise HTTPException(status_code=404, detail="Slide not found")
    new_state = bool(unlisted)
    s.unlisted = new_state
    db.commit()
    audit(admin, "slide.unlisted", target=str(slide_id), unlisted=new_state)
    return {"ok": True, "unlisted": new_state}


@router.post("/api/admin/slides/rebuild-thumbnails")
def admin_rebuild_thumbnails(
    force: int = Form(0),
    admin: User = Depends(require_manager_or_above),
    db: Session = Depends(get_db),
):
    """Regenerate slide thumbnails from the on-disk WSI files.

    Recovery tool for after a metadata-scope restore (or a bulk copy of WSI
    files straight into ``wsi/``): the slide rows exist and the WSI bytes are
    back on disk, but ``thumbs/`` was wiped by the restore and thumbnails are
    otherwise produced only at upload / replace time — so the gallery would
    show broken cards until they are rebuilt.

    For every slide whose WSI file is present on disk:
      * ``force=0`` (default): (re)generate only when the thumbnail is missing.
      * ``force=1``: regenerate unconditionally (e.g. corrupt thumbnails).

    Slides whose WSI body is absent are skipped — a thumbnail is never
    created without its backing WSI file, mirroring the publish gate.

    Each slide is processed under its own per-slide lock + commit so a single
    unreadable file never aborts the batch.

    The lock is only tried, never waited for: a slide something else is
    mutating right now is reported in ``skipped_busy`` (run the rebuild again
    for it). Id reservations — rows an upload or a slide import has not
    finalised yet, ``wsi_filename`` still empty — are left out altogether:
    they have no image to make a thumbnail of (the upload / import makes its
    own), and their lock is held for the whole multi-GB transfer, so waiting
    on it would hold this request, and a worker thread, until the proxy timed it
    out.
    """
    from ..dzi import _slide_path

    thumbs_dir = os.path.join(settings.data_dir, "thumbs")
    os.makedirs(thumbs_dir, exist_ok=True)
    rebuilt: List[int] = []
    skipped_missing_wsi: List[int] = []
    skipped_present: List[int] = []
    skipped_busy: List[int] = []
    failed: List[dict] = []

    slide_ids = db.execute(
        select(Slide.id)
        .where(func.trim(func.coalesce(Slide.wsi_filename, "")) != "")
        .order_by(Slide.id)
    ).scalars().all()
    for slide_id in slide_ids:
        try:
            with try_slide_mutation_lock(slide_id) as acquired:
                if not acquired:
                    skipped_busy.append(slide_id)
                    continue
                s = db.get(Slide, slide_id)
                if s is None:
                    continue
                if not _wsi_file_present(s):
                    skipped_missing_wsi.append(slide_id)
                    continue
                thumb_name = _thumb_filename(slide_id, s.thumb_filename)
                thumb_path = os.path.join(thumbs_dir, thumb_name)
                if not force and os.path.isfile(thumb_path):
                    # Already have a thumbnail; just make sure the row points
                    # at it (a restored DB normally does already).
                    if s.thumb_filename != thumb_name:
                        s.thumb_filename = thumb_name
                        db.commit()
                    skipped_present.append(slide_id)
                    continue
                make_thumbnail(_slide_path(s), thumb_path)
                s.thumb_filename = thumb_name
                db.commit()
                rebuilt.append(slide_id)
        except Exception as exc:  # noqa: BLE001 — one bad file must not abort the batch
            db.rollback()
            logger.exception("rebuild thumbnail failed for slide %s", slide_id)
            failed.append({"slide_id": slide_id, "error": _failure_reason(exc)})

    audit(
        admin,
        "slide.rebuild_thumbnails",
        force=bool(force),
        rebuilt=rebuilt,
        skipped_missing_wsi=skipped_missing_wsi,
        skipped_busy=skipped_busy,
        failed=[f["slide_id"] for f in failed],
    )
    return {
        "ok": True,
        "rebuilt": rebuilt,
        "skipped_missing_wsi": skipped_missing_wsi,
        "skipped_present": skipped_present,
        "skipped_busy": skipped_busy,
        "failed": failed,
    }


def _parse_tri_bool(raw: Optional[str]) -> Optional[bool]:
    """Parse a tri-state override: empty/None/"null" -> None, else truthy/falsy bool."""
    if raw is None:
        return None
    s = raw.strip().lower()
    if s in ("", "null", "none"):
        return None
    if s in ("1", "true", "yes", "on"):
        return True
    if s in ("0", "false", "no", "off"):
        return False
    return None


# Allowed ranges for the per-slide image-adjustment preset, mirroring
# IMAGE_ADJUST_RANGES on the frontend (frontend/src/lib/imageAdjust.ts). Values
# are clamped server-side so a crafted request can't store an out-of-range
# filter that the viewer would faithfully apply.
_ADJUST_RANGES = {
    "brightness": (0.5, 1.5),
    "contrast": (0.5, 1.5),
    "saturation": (0.0, 2.0),
}


def _parse_adjust_value(raw: Optional[str], channel: str) -> Optional[float]:
    """Parse one image-adjustment channel into a stored preset value.

    empty/None/"null" -> None (no preset). Otherwise parse as float, clamp to
    the channel's allowed range, and collapse the identity value (1.0) to None
    so a no-op adjustment is stored as "no preset" — matching the per-channel
    semantics of isImageAdjustDefault on the frontend.
    """
    if raw is None:
        return None
    s = raw.strip().lower()
    if s in ("", "null", "none"):
        return None
    try:
        v = float(s)
    except ValueError:
        raise HTTPException(status_code=400, detail=f"invalid {channel} value")
    lo, hi = _ADJUST_RANGES[channel]
    v = max(lo, min(hi, v))
    if v == 1.0:
        return None
    return v


@router.patch("/api/admin/slides/{slide_id}")
def admin_update_slide_metadata(
    slide_id: int,
    catalog_id: Optional[str] = Form(None),
    title: Optional[str] = Form(None),
    # An empty form value arrives as None (= "leave alone"), so blanking either
    # field needs an explicit flag — without one the dashboard's save of a
    # cleared field would answer 200 "saved" while keeping the old value. Same
    # clear_<field> convention as the settings PATCHes.
    clear_catalog_id: Optional[str] = Form(None),
    clear_title: Optional[str] = Form(None),
    attributes_json: Optional[str] = Form(None),
    mpp_x: Optional[str] = Form(None),
    mpp_y: Optional[str] = Form(None),
    show_zoom_indicator: Optional[str] = Form(None),
    show_scale_bar: Optional[str] = Form(None),
    show_metadata_panel: Optional[str] = Form(None),
    image_adjust_brightness: Optional[str] = Form(None),
    image_adjust_contrast: Optional[str] = Form(None),
    image_adjust_saturation: Optional[str] = Form(None),
    # Image credit as one JSON object {credit_text, license_id, source_url}
    # that replaces all three fields (a missing key clears that field).
    # Bundled so a blanked field needs no clear_* flag of its own; omitted
    # entirely = leave the credit alone.
    credit_json: Optional[str] = Form(None),
    admin: User = Depends(require_manager_or_above),
    db: Session = Depends(get_db),
):
    s = db.get(Slide, slide_id)
    if not s:
        raise HTTPException(status_code=404, detail="Slide not found")
    credit: Optional[dict] = None
    if credit_json is not None:
        try:
            raw_credit = json.loads(credit_json)
        except Exception:
            # ValueError for malformed JSON, RecursionError for absurd nesting
            # — either way the client's input, never a 500.
            raise api_error(422, "adminDash.apiErr.invalidCredit")
        credit = clean_credit(raw_credit)
    # True PATCH semantics: only fields actually present in the request are
    # written. catalog_id/title/attributes_json default to None and omitting a
    # field means "leave as-is". Note that an *empty* value counts as omitted
    # too, not as an explicit clear: Starlette/Pydantic deliver an empty form
    # field as None (pinned by test_patch_empty_form_value_is_treated_as_absent),
    # so catalog_id / title are blanked only through clear_catalog_id /
    # clear_title, which take precedence over a value sent alongside.
    # Writing them unconditionally would let a partial call carrying
    # only (say) ``mpp_x`` silently wipe the title, catalog_id and EVERY
    # attribute with a 200 OK — turning an already-published slide into an
    # unidentified "Slide #N" (the required-fields gate only runs at publish
    # time). The dashboard always sends all three fields.
    attrs: Optional[dict] = None
    if attributes_json is not None:
        # A corrupt or non-object attributes_json must NOT silently fall back to
        # an empty dict: rebuild_slide_attributes() below clears every existing
        # attribute first, so an empty dict would wipe the slide's attributes on
        # what is effectively a malformed request. Reject with 400 so the caller
        # sees the error instead of losing data with a 200 OK.
        try:
            attrs = json.loads(attributes_json or "{}")
        except Exception:
            raise HTTPException(
                status_code=400, detail="invalid attributes_json (not valid JSON)"
            )
        if not isinstance(attrs, dict):
            raise HTTPException(
                status_code=400,
                detail="invalid attributes_json (expected a JSON object)",
            )
    # Strip leading/trailing whitespace before length validation and persist
    # so users don't end up with invisible padding from a copy-paste mistake.
    if _parse_tri_bool(clear_catalog_id) is True:
        catalog_id = ""
    if _parse_tri_bool(clear_title) is True:
        title = ""
    if catalog_id is not None:
        catalog_id = catalog_id.strip()
    if title is not None:
        title = title.strip()
    # Validate the effective post-merge values (already-stored catalog_id/title
    # passed validation on the way in, so re-checking them is a no-op; only the
    # provided attrs need checking).
    validate_slide_metadata(
        catalog_id if catalog_id is not None else s.catalog_id,
        title if title is not None else s.title,
        attrs if attrs is not None else {},
    )
    # Never let a *published* slide end up without the fields the publish gate
    # requires, which would render it as an unidentified "Slide #N" in the
    # public gallery. Reached through the clear_* flags above (and through a
    # whitespace-only value, which strips to ""). Clearing stays permitted on a
    # draft. Only a request that *blanks a stored value* is refused: a published
    # slide whose field is already blank (a restored or hand-edited DB) must
    # still accept unrelated edits, which omit the field.
    #
    # The check on the row read above is only the fast path: a publish can
    # commit after that read, so the blanking itself is written by a guarded
    # UPDATE that re-evaluates "published with a stored value" at write time
    # (see _blank_unless_published / _publish_if_identified).
    if s.is_published:
        blanks_catalog = catalog_id is not None and not catalog_id and (s.catalog_id or "").strip()
        blanks_title = title is not None and not title and (s.title or "").strip()
        if blanks_catalog or blanks_title:
            raise api_error(400, "adminDash.apiErr.publishedRequiredFields")
    for value, column in ((catalog_id, Slide.catalog_id), (title, Slide.title)):
        if value == "" and not _blank_unless_published(db, slide_id, column):
            db.rollback()
            raise api_error(400, "adminDash.apiErr.publishedRequiredFields")
    if catalog_id:
        s.catalog_id = catalog_id
    if title:
        s.title = title
    if mpp_x is not None:
        s.mpp_x = parse_mpp_value(mpp_x, "mpp_x")
    if mpp_y is not None:
        s.mpp_y = parse_mpp_value(mpp_y, "mpp_y")
    if show_zoom_indicator is not None:
        s.show_zoom_indicator = _parse_tri_bool(show_zoom_indicator)
    if show_scale_bar is not None:
        s.show_scale_bar = _parse_tri_bool(show_scale_bar)
    if show_metadata_panel is not None:
        s.show_metadata_panel = _parse_tri_bool(show_metadata_panel)
    if image_adjust_brightness is not None:
        s.image_adjust_brightness = _parse_adjust_value(image_adjust_brightness, "brightness")
    if image_adjust_contrast is not None:
        s.image_adjust_contrast = _parse_adjust_value(image_adjust_contrast, "contrast")
    if image_adjust_saturation is not None:
        s.image_adjust_saturation = _parse_adjust_value(image_adjust_saturation, "saturation")
    if credit is not None:
        s.credit_text = credit["credit_text"]
        s.license_id = credit["license_id"]
        s.source_url = credit["source_url"]
    try:
        if attrs is not None:
            rebuild_slide_attributes(s, db, attrs)
        db.commit()
    except IntegrityError:
        # Another admin saved this slide's attributes between this read and this
        # delete/insert (uq_slide_attr_triplet): report a conflict to retry
        # rather than a 500.
        db.rollback()
        raise api_error(409, "adminDash.apiErr.slideConflict")
    # Metadata edits (catalog_id / title / attributes / MPP overrides) are the
    # most-edited slide surface; record them in the audit trail like every other
    # slide mutation (upload / replace / delete / publish / image-adjust).
    audit(admin, "slide.update_metadata", target=str(slide_id),
          catalog_id=s.catalog_id, title=s.title,
          **({"credit": credit} if credit is not None else {}))
    return {"ok": True}


@router.put("/api/admin/slides/{slide_id}/image-adjust")
def admin_update_slide_image_adjust(
    slide_id: int,
    brightness: Optional[str] = Form(None),
    contrast: Optional[str] = Form(None),
    saturation: Optional[str] = Form(None),
    admin: User = Depends(require_manager_or_above),
    db: Session = Depends(get_db),
):
    """Save the per-slide image-adjustment preset on its own.

    The in-viewer adjust panel calls this so it can persist the preset without
    round-tripping the whole metadata form. Unlike the PATCH above (where an
    omitted channel is left alone), all three channels are always written, so
    an omitted one resets to "no preset". Each channel is clamped and identity
    collapses to NULL via ``_parse_adjust_value``.
    """
    s = db.get(Slide, slide_id)
    if not s:
        raise HTTPException(status_code=404, detail="Slide not found")
    s.image_adjust_brightness = _parse_adjust_value(brightness, "brightness")
    s.image_adjust_contrast = _parse_adjust_value(contrast, "contrast")
    s.image_adjust_saturation = _parse_adjust_value(saturation, "saturation")
    db.commit()
    audit(admin, "slide.image_adjust", target=str(slide_id))
    return {
        "ok": True,
        "image_adjust_brightness": s.image_adjust_brightness,
        "image_adjust_contrast": s.image_adjust_contrast,
        "image_adjust_saturation": s.image_adjust_saturation,
    }


@router.post("/api/admin/slides/{slide_id}/wsi")
def admin_replace_slide_wsi(
    slide_id: int,
    file: UploadFile = File(...),
    admin: User = Depends(require_manager_or_above),
    db: Session = Depends(get_db),
):
    """Replace (or set) the WSI file for an existing slide.

    Useful after a metadata-only restore — the slide row is present but the
    WSI file is missing — or to swap out a defective scan without losing
    metadata, tours, or the slide's numeric ID.

    The old WSI file is removed from disk if its stored name differs from the
    new one. mpp_x/mpp_y are reset to the values read from the new file (same
    as a fresh upload); any previous manual override is cleared.
    """
    # Phase 1 — UNLOCKED. Everything here writes only to uniquely-tokenised
    # staging paths, so it is safe to run concurrently with other mutations of
    # this slide. Deliberately outside the lock: streaming is paced by the
    # client, so a slow or stalled connection uploading multi-GB bytes would
    # otherwise hold the cross-worker mutation lock for the whole
    # transfer, stalling every publish / delete / bulk operation that touches
    # this slide_id on any worker.
    slide = db.get(Slide, slide_id)
    if not slide:
        raise HTTPException(status_code=404, detail="Slide not found")

    filename = file.filename or "slide.wsi"
    safe_name = safe_filename(filename)
    validated_extension(safe_name)
    if upload_space_short(file.size or 0, factor=1):
        raise api_error(507, "adminDash.apiErr.uploadNoSpace")
    # Advisory pre-check so an obvious collision fails before the upload
    # consumes the size budget; re-checked under the lock below, which is what
    # actually makes it race-free.
    _assert_unique_wsi_filename(db, safe_name, exclude_slide_id=slide_id)

    wsi_dir = os.path.join(settings.data_dir, "wsi")
    stored_name = f"{slide_id}_{safe_name}"
    wsi_path = os.path.join(wsi_dir, stored_name)

    # Stage the new bytes alongside the destination so a failed validate or
    # commit cannot destroy the existing WSI. Same-filesystem placement
    # makes the final os.replace() atomic. The collision-window of an
    # in-place truncate-and-overwrite — which would let the cached
    # OpenSlide handle read torn data via the still-open fd when the new
    # filename matches the old — is closed by writing to a sibling and
    # invalidating the DZI cache before swapping into place.
    tmp_token = uuid.uuid4().hex[:12]
    # Insert the token *before* the extension so the staging file still ends in
    # the real suffix (e.g. ``12_foo.upload-abc.szi``). read_slide_properties /
    # make_thumbnail dispatch SZI vs OpenSlide by extension, so a bare
    # ``.szi.upload-<token>`` tail would misroute an SZI replacement into the
    # OpenSlide branch and 422. Mirrors the upload flow's staging path.
    _wsi_root, _wsi_ext = os.path.splitext(wsi_path)
    wsi_tmp_path = f"{_wsi_root}.upload-{tmp_token}{_wsi_ext}"
    # Keyed on the token alone rather than on the slide's current thumbnail
    # name: that name is re-read under the lock in phase 2 (a concurrent
    # unpublish rotates it), so the staging path must not depend on it.
    thumb_tmp_path = os.path.join(
        settings.data_dir, "thumbs", f"{slide_id}.upload-{tmp_token}"
    )

    def _drop_staged() -> None:
        for p in (wsi_tmp_path, thumb_tmp_path):
            try:
                os.remove(p)
            except OSError:
                pass

    try:
        stream_upload_to_file(file, wsi_tmp_path)
    except HTTPException:
        # stream_upload_to_file already removed wsi_tmp_path on its error path.
        raise
    except Exception as exc:
        # OSError (disk full), etc. The temp file is already cleaned up by
        # stream_upload_to_file's BaseException handler, but wrapping as
        # an HTTPException keeps the user-facing message consistent with the
        # upload flow (which catches BaseException and surfaces a 500 with a
        # localizable error code). Without this, a raw exception bubbles up as
        # FastAPI's generic "Internal Server Error".
        logger.exception("Upload streaming failed for slide %s replacement", slide_id)
        raise upload_write_error(exc) from exc

    try:
        props = read_slide_properties(wsi_tmp_path)
    except UnsupportedSlideError as exc:
        _drop_staged()
        raise api_error(422, _layout_rejection_code(exc, wsi_tmp_path))
    except Exception:
        logger.exception("OpenSlide failed to read replacement slide %s", wsi_tmp_path)
        _drop_staged()
        raise api_error(422, "adminDash.apiErr.wsiUnreadable")

    try:
        make_thumbnail(wsi_tmp_path, thumb_tmp_path)
    except Exception:
        logger.exception("Thumbnail generation failed for %s", wsi_tmp_path)
        _drop_staged()
        raise api_error(422, "adminDash.apiErr.thumbnailFailed")

    # Phase 2 — LOCKED. Only the swap-and-commit needs serialising: two
    # simultaneous replaces would otherwise both capture the same
    # ``old_wsi_filename`` snapshot, race their on-disk swaps, and orphan the
    # loser's bytes; a delete racing alongside takes the same lock.
    with slide_mutation_lock(slide_id):
        return _finalize_replace_slide_wsi(
            slide_id, admin, db,
            safe_name=safe_name, stored_name=stored_name, wsi_path=wsi_path,
            wsi_tmp_path=wsi_tmp_path, thumb_tmp_path=thumb_tmp_path,
            tmp_token=tmp_token, props=props, drop_staged=_drop_staged,
        )


def _cascade_tours_to_draft(db: Session, slide_id: int) -> int:
    """Turn every published tour of a slide being unpublished into a draft,
    in SQL, and return how many. Flushes first, so the slide's own write
    takes the database write lock before the tours are matched: reading
    ``slide.tours`` beforehand would run outside that lock, and a tour publish
    committing in between would be missed — leaving a published tour on a
    draft slide, which would then go public again on its own with the slide's
    next publish. Each cascaded tour's revision is bumped (see Tour.revision) so
    an editor open on it sees a 409 instead of acting on stale flags."""
    db.flush()
    result = db.execute(
        update(Tour)
        .where(Tour.slide_id == slide_id, Tour.is_published.is_(True))
        .values(is_published=False, revision=Tour.revision + 1),
        execution_options={"synchronize_session": False},
    )
    return int(result.rowcount or 0)


def _retire_generation(db: Session, slide_id: int) -> None:
    """Move the slide to a fresh tile generation and make it current on
    every worker (see the swap-window note in _finalize_replace_slide_wsi).
    Commits."""
    db.execute(
        update(Slide)
        .where(Slide.id == slide_id)
        .values(wsi_generation=func.max(Slide.wsi_generation + 1, fresh_wsi_generation())),
        execution_options={"synchronize_session": False},
    )
    db.commit()
    from .. import slide_resolve_cache
    slide_resolve_cache.invalidate_slide_meta()


def _retire_generation_quietly(db: Session, slide_id: int) -> None:
    """_retire_generation for a failure path that already rolled the files
    back: the interim generation may have served the new bytes during the
    swap, so it must not stay current over the restored old ones."""
    # Retried: the failure that led here (a busy or full database)
    # may well hit this write too.
    for attempt in range(3):
        try:
            db.rollback()
            _retire_generation(db, slide_id)
            return
        except Exception:
            db.rollback()
            if attempt == 2:
                logger.exception("Could not retire the tile generation of slide %s", slide_id)
            else:
                time.sleep(0.5 * (attempt + 1))


def _finalize_replace_slide_wsi(
    slide_id: int,
    admin: User,
    db: Session,
    *,
    safe_name: str,
    stored_name: str,
    wsi_path: str,
    wsi_tmp_path: str,
    thumb_tmp_path: str,
    tmp_token: str,
    props: dict,
    drop_staged,
):
    """Swap the staged WSI/thumbnail into place and commit the row.

    Runs under ``slide_mutation_lock``; every input was produced by the
    unlocked staging phase in :func:`admin_replace_slide_wsi`.
    """
    # Re-fetch and re-check under the lock: the slide may have been deleted, or
    # another slide may have committed this filename, while the upload streamed.
    #
    # The rollback is what makes that re-read real. Phase 1 already queried this
    # row, so it sits in the session's identity map and ``db.get`` would hand
    # back the cached object without touching the DB — and even an explicit
    # expire would re-read inside the read transaction phase 1 opened, which on
    # SQLite still sees the pre-stream snapshot. Phase 1 performs no writes, so
    # ending that transaction here is free and gives every read below a fresh
    # view of what other workers committed while the upload streamed.
    db.rollback()
    slide = db.get(Slide, slide_id)
    if not slide:
        drop_staged()
        raise HTTPException(status_code=404, detail="Slide not found")
    _assert_unique_wsi_filename(
        db, safe_name, exclude_slide_id=slide_id, on_conflict=drop_staged
    )

    # Read the thumbnail name under the lock — a concurrent unpublish rotates
    # it, and reusing a pre-lock snapshot would write the pre-rotation name
    # back onto the row, resurrecting the URL the rotation invalidated.
    thumb_name = _thumb_filename(slide_id, slide.thumb_filename)
    thumb_path = os.path.join(settings.data_dir, "thumbs", thumb_name)
    # Rollback parking paths. Same-filesystem location keeps the os.replace
    # back swap atomic if an undo is needed. The WSI park is a peer of the
    # destination filename so it survives until the DB commit has locked in
    # the new state.
    wsi_rollback_path = wsi_path + f".rollback-{tmp_token}"
    thumb_rollback_path = thumb_path + f".rollback-{tmp_token}"

    # Resolve the OLD file's on-disk path through the same realpath/strict-descent
    # guard used by delete/download/publish. The DB row is mutable (a restored or
    # hand-edited ``wsi_filename`` could carry a ``..`` traversal), and this path
    # is fed to os.replace()/os.remove() below; a bare os.path.join would let a
    # crafted row move or delete a file outside wsi/. A traversal value resolves
    # to "no parkable old file" — the swap then simply drops the new bytes in.
    old_wsi_filename = slide.wsi_filename or ""
    old_wsi_path_on_disk = ""
    if old_wsi_filename:
        try:
            from ..dzi import _slide_path
            old_wsi_path_on_disk = _slide_path(slide)
        except FileNotFoundError:
            old_wsi_path_on_disk = ""

    # Retire the current tile generation BEFORE any byte moves. Tile URLs are
    # ``?v=<generation>`` and nginx caches each for 30 days, so a generation
    # must only ever be served one file's bytes. Bumping it only in the final
    # commit would leave the swap window (the file already replaced, the row
    # not yet committed — _attach_associated_images alone reads the whole new
    # file) serving the NEW bytes under the OLD, still-current v; a failed
    # commit (or a later restore) would then make that v current again over
    # the old bytes with the new ones cached under it. So the old v is 404
    # (uncacheable) on every worker before the swap starts, and the generation is moved
    # again on success (see below) and on rollback (_retire_generation in
    # the failure paths), so the interim one — served whichever bytes were
    # on disk during the swap — never becomes current over settled bytes.
    try:
        _retire_generation(db, slide_id)
    except Exception:
        db.rollback()
        logger.exception("Could not retire the tile generation of slide %s", slide_id)
        drop_staged()
        raise api_error(500, "adminDash.apiErr.saveFailed")
    # Other workers drop their cached generation at their next sentinel check.
    time.sleep(max(0, settings.slide_meta_gen_check_interval))

    # Drop any cached OpenSlide handle on this slide BEFORE swapping the
    # file into place. The next tile request must re-open against the new
    # bytes; without this, the LRU keeps an fd that points at the same
    # inode os.replace() is about to repoint, which would race a concurrent
    # tile read against the writer.
    try:
        from ..dzi import clear_dz_cache
        clear_dz_cache(slide_id)
    except Exception:
        pass

    # Order the on-disk swaps so a mid-flight failure can never leave NEW
    # WSI bytes paired with OLD DB metadata (width / height / mpp). That
    # combination breaks the viewer silently — the OpenSlide handle against
    # the new bytes reports a level pyramid the DB doesn't know about.
    #
    # Thumbnail is swapped first (cheap and reversible), then WSI (also
    # reversible because the old bytes are parked alongside instead of
    # overwritten). After both physical swaps land, the slide row is
    # rewritten and committed; only on a successful commit are the parked
    # old WSI and old thumbnail deleted.
    thumb_parked = False
    wsi_parked = False
    try:
        if os.path.isfile(thumb_path):
            os.replace(thumb_path, thumb_rollback_path)
            thumb_parked = True
        os.replace(thumb_tmp_path, thumb_path)
    except OSError:
        logger.exception("Failed to swap thumbnail into place for slide %s", slide_id)
        if thumb_parked:
            try:
                os.replace(thumb_rollback_path, thumb_path)
            except OSError:
                logger.exception("thumb rollback failed for slide %s", slide_id)
        drop_staged()
        raise api_error(500, "adminDash.apiErr.saveFailed")

    try:
        # Park the OLD WSI before clobbering its path. Two distinct cases
        # must be guarded against:
        #   - same-name replace: the destination ``wsi_path`` IS the old
        #     file's path. Renaming it to ``wsi_rollback_path`` ensures a
        #     later ``os.replace(wsi_tmp_path, wsi_path)`` does not destroy
        #     the old bytes; on commit failure ``os.replace`` puts it back.
        #   - different-name replace: the old file lives at a different
        #     path; it is still parked (moved into the rollback name) so
        #     the "old vs new" coexistence question doesn't leak past commit.
        if old_wsi_path_on_disk and os.path.isfile(old_wsi_path_on_disk):
            os.replace(old_wsi_path_on_disk, wsi_rollback_path)
            wsi_parked = True
        os.replace(wsi_tmp_path, wsi_path)
    except OSError:
        logger.exception("Failed to swap WSI into place for slide %s", slide_id)
        # Best-effort unwind: put the parked WSI back where it came from
        # and roll the thumb back so the slide row's references stay
        # coherent with the on-disk state.
        if wsi_parked:
            try:
                os.replace(wsi_rollback_path, old_wsi_path_on_disk)
            except OSError:
                logger.exception("WSI rollback failed for slide %s", slide_id)
        if thumb_parked:
            try:
                os.replace(thumb_rollback_path, thumb_path)
            except OSError:
                logger.exception("thumb rollback failed for slide %s", slide_id)
        else:
            try:
                os.remove(thumb_path)
            except OSError:
                pass
        _retire_generation_quietly(db, slide_id)
        drop_staged()
        raise api_error(500, "adminDash.apiErr.saveFailed")

    def _restore_parked_files() -> None:
        """Reverse the on-disk swaps. Called when the DB commit fails so
        the slide row's old filename column lines up with on-disk bytes."""
        # WSI first: same-name needs to wipe the new bytes from wsi_path
        # before moving the parked old bytes back; different-name has the
        # new bytes at a different path that is explicitly removed.
        if wsi_parked:
            try:
                if (
                    old_wsi_path_on_disk
                    and os.path.abspath(old_wsi_path_on_disk) != os.path.abspath(wsi_path)
                ):
                    try:
                        os.remove(wsi_path)
                    except FileNotFoundError:
                        pass
                os.replace(wsi_rollback_path, old_wsi_path_on_disk)
            except OSError:
                logger.exception("WSI rollback failed for slide %s after commit failure", slide_id)
        else:
            # No old WSI was parked (this was a first-time set). Just drop
            # the new bytes so the slide row's empty wsi_filename stays
            # consistent with disk.
            try:
                os.remove(wsi_path)
            except OSError:
                pass
        if thumb_parked:
            try:
                os.replace(thumb_rollback_path, thumb_path)
            except OSError:
                logger.exception("thumb rollback failed for slide %s after commit failure", slide_id)
        else:
            try:
                os.remove(thumb_path)
            except OSError:
                pass

    # Second eviction, AFTER the on-disk swap. A concurrent tile request
    # arriving in the small window between the pre-swap clear and os.replace
    # could have re-opened the OLD file and re-populated the cache; that
    # entry now points at an inode that has just been unlinked (when names
    # match) or at a path the DB will no longer reference (when names
    # differ). Drop it again so the next read goes through _open_dz against
    # the current bytes.
    try:
        clear_dz_cache(slide_id)
    except Exception:
        pass

    slide.wsi_filename = stored_name
    slide.wsi_filename_norm = safe_name.casefold()
    slide.thumb_filename = thumb_name
    slide.width = props["width"]
    slide.height = props["height"]
    slide.mpp_x = props["mpp_x"]
    slide.mpp_y = props["mpp_y"]
    slide.mpp_x_file = props["mpp_x"]
    slide.mpp_y_file = props["mpp_y"]
    slide.objective_power_file = props.get("objective_power", 0.0) or 0.0
    slide.vendor = props["vendor"]
    slide.acquired_at = props.get("acquired_at", "") or ""
    try:
        slide.file_size_bytes = os.path.getsize(wsi_path)
    except OSError:
        slide.file_size_bytes = 0
    _attach_associated_images(slide, wsi_path)
    # New content generation -> new tile URLs (``?v=``), so viewers never mix
    # tiles nginx cached for the old bytes with renders of the new ones, even
    # when the file name — and so every tile path — is unchanged; and past
    # the interim generation retired-to before the swap (see above), whose
    # tiles may mix old and new bytes. Computed in
    # SQL (UPDATE ... SET wsi_generation = max(wsi_generation + 1, <now ms>))
    # rather than from the value loaded above, and committed with the
    # file-derived columns in the same transaction, so a failed commit leaves
    # the old generation paired with the restored old bytes. The clock floor
    # (see models.fresh_wsi_generation) keeps the new value above every
    # generation this slide has ever had, even after a backup restore rolled
    # the counter back below one a later, discarded replace already used —
    # whose tiles nginx may still hold under that number.
    slide.wsi_generation = func.max(Slide.wsi_generation + 1, fresh_wsi_generation())

    try:
        db.commit()
    except IntegrityError as exc:
        # UNIQUE(wsi_filename_norm) backstop: another slide committed the
        # same original name during this request's check->commit gap. Roll
        # back, restore the parked old bytes so the row still points at a
        # file that exists, drop the now-stale tile cache, and report 409 —
        # the same code the app-level _assert_unique_wsi_filename raises.
        # Any other constraint failure gets the same undo but the generic
        # update failure, not a duplicate-name message.
        db.rollback()
        duplicate = _is_duplicate_filename_violation(exc)
        if not duplicate:
            logger.exception("Failed to update slide %s after WSI replacement", slide_id)
        _restore_parked_files()
        try:
            clear_dz_cache(slide_id)
        except Exception:
            pass
        _retire_generation_quietly(db, slide_id)
        if duplicate:
            raise api_error(409, "adminDash.apiErr.duplicateFilename")
        raise api_error(500, "adminDash.apiErr.updateFailed")
    except Exception:
        # Commit failed. Now that the old WSI / thumb bytes are parked
        # alongside the destination paths, the filesystem can be rolled
        # back so the slide row's still-old wsi_filename remains pointing
        # at bytes that actually exist. Without this rollback the slide
        # row would reference filename X while the on-disk bytes at X are
        # the NEW file (same-name case), or filename X would be missing
        # entirely (different-name case after the move).
        db.rollback()
        logger.exception("Failed to update slide %s after WSI replacement", slide_id)
        _restore_parked_files()
        try:
            clear_dz_cache(slide_id)
        except Exception:
            pass
        _retire_generation_quietly(db, slide_id)
        raise api_error(500, "adminDash.apiErr.updateFailed")

    # Commit succeeded. Drop the parked old files; their byte-identical
    # twins are either the new wsi_path bytes (no-op cleanup) or in the
    # rollback parking slot which is no longer needed. A leftover .rollback-* file
    # is cosmetic and not catastrophic, so log on failure rather than
    # raising.
    if wsi_parked:
        try:
            os.remove(wsi_rollback_path)
        except FileNotFoundError:
            pass
        except OSError as exc:
            logger.warning("Could not remove parked WSI %s: %s", wsi_rollback_path, exc)
    if thumb_parked:
        try:
            os.remove(thumb_rollback_path)
        except FileNotFoundError:
            pass
        except OSError as exc:
            logger.warning("Could not remove parked thumb %s: %s", thumb_rollback_path, exc)

    # If the new stored_name differs from old_wsi_filename, the old path
    # was parked (and just deleted via wsi_rollback_path above) — there is
    # NO stray old file to remove here. Same-name replace also handled by
    # the park/swap above. Nothing further to clean up.

    # The resolve cache holds each slide's generation, which the /dzi/ routes
    # check every ``?v=`` against: flush it here and bump the cross-worker
    # sentinel, so the new generation is current on this worker at once and on
    # the others within the check interval (a viewer already on the new ``v``
    # before then is still served — dzi._resolve_for_request re-reads a
    # generation newer than the cached one).
    try:
        from .. import slide_resolve_cache
        slide_resolve_cache.invalidate_slide_meta()
    except Exception:
        logger.exception("Could not invalidate slide-meta cache after WSI replace")

    audit(admin, "slide.replace_wsi", target=str(slide_id), filename=stored_name)
    return {"ok": True, "slide_id": slide_id}


@router.post("/api/admin/slides/{slide_id}/download_url")
def admin_slide_download_url(
    slide_id: int,
    admin: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    """Mint a short-lived signed URL the browser can navigate to directly.

    Mirrors the backup-download flow: the SPA can't put an Authorization
    header on a plain <a download> click, so the endpoint issues a
    query-string token that the corresponding GET endpoint accepts.
    Letting the browser drive
    the download keeps multi-GB WSI files out of tab memory (which a
    fetch+Blob round-trip would otherwise pin).
    """
    slide = db.get(Slide, slide_id)
    if not slide:
        raise HTTPException(status_code=404, detail="Slide not found")
    if not slide.wsi_filename:
        raise HTTPException(status_code=404, detail="Slide has no WSI file")
    token = create_slide_download_token(slide_id, admin)
    audit(admin, "slide.download_url", target=str(slide_id), filename=slide.wsi_filename)
    return {"url": f"/api/admin/slides/{slide_id}/download?t={token}"}


@router.get("/api/admin/slides/{slide_id}/download")
def admin_download_slide(
    slide_id: int,
    request: Request,
    t: Optional[str] = Query(None),
    db: Session = Depends(get_db),
):
    """Serve a slide's WSI file as an attachment.

    Authorized either by a `?t=<token>` query string (issued by
    /download_url so the browser can navigate without an Authorization
    header) or by an admin Bearer token on the Authorization header. Falls
    back to a generic application/octet-stream because the endpoint serves
    a mix of proprietary formats whose canonical mime types are inconsistent across
    OS / browser combinations; the original filename in
    Content-Disposition is what the user actually cares about.
    """
    authorized = False
    actor: Optional[User] = None
    # The ?t= token is bound to the minting user's live row (uid + tv), so this
    # is a revocation check, not just a signature check: disabling the account
    # or resetting the password kills an already-issued download URL instead of
    # leaving it good for the rest of its TTL.
    if t and decode_slide_download_token(t, slide_id, db):
        authorized = True
    else:
        token = bearer_token(request.headers.get("authorization", ""))
        if token:
            # resolve_staff_user enforces signature/exp/aud + existence +
            # is_active + token_version, so a JWT revoked by a password change
            # (token_version bump) can't keep downloading raw slide files for
            # the rest of its 24h TTL. A signature-only check would miss the
            # tv claim.
            user = resolve_staff_user(token, db)
            if user and user.role == "admin":
                authorized = True
                actor = user
    if not authorized:
        raise HTTPException(status_code=401, detail="unauthorized")

    slide = db.get(Slide, slide_id)
    if not slide or not slide.wsi_filename:
        raise HTTPException(status_code=404, detail="Slide not found")

    # Through the same realpath / strict-descent guard as the tile path:
    # safe_filename() already strips traversal sequences at upload time, but
    # the DB row is mutable (a restored DB, a hand edit), and serving a file
    # from outside the data root must never follow from that.
    from ..dzi import _slide_path
    try:
        full = _slide_path(slide)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="Slide file missing")
    if not os.path.isfile(full):
        raise HTTPException(status_code=404, detail="Slide file missing")

    # Strip the `<id>_` prefix added at upload time so the user gets the
    # filename they originally chose, not the on-disk storage name.
    download_name = slide.wsi_filename
    prefix = f"{slide_id}_"
    if download_name.startswith(prefix):
        download_name = download_name[len(prefix):]

    # Always audit the download. When actor is None the download was
    # authorized via a signed query-string token (no user identity in the
    # request), so actor_id/actor_username are logged as null — the URL
    # issuance audit already records who minted the token.
    audit(actor, "slide.download", target=str(slide_id), filename=slide.wsi_filename,
          via_token=(actor is None))
    return FileResponse(
        full,
        media_type="application/octet-stream",
        filename=download_name,
    )


@router.delete("/api/admin/slides/{slide_id}")
def admin_delete_slide(
    slide_id: int,
    admin: User = Depends(require_manager_or_above),
    db: Session = Depends(get_db),
):
    # Serialise against in-flight replace_wsi on the same slide so a delete
    # racing the swap can't unlink the new bytes mid-replace or chase a
    # filename column that the other thread is about to rewrite. A reservation
    # still being uploaded / imported is a 409, not a wait (see
    # _slide_lock_unless_reserving).
    with _slide_lock_unless_reserving(slide_id):
        return _do_delete_slide(slide_id, admin, db)


def _do_delete_slide(slide_id: int, admin: User, db: Session):
    """Delete a slide, serialised against asset uploads to its tours.

    The cascade removes the slide's tours and the tail of this function removes
    their asset directories. An upload that passed its "tour exists" check just
    before would otherwise recreate a directory for a tour that no longer has a
    row, and nothing would ever collect it. Uploads and ``admin_delete_tour``
    already hold ``tour_asset_lock``; take the same per-tour locks here, in id
    order so two deletions can never wait on each other crosswise."""
    def tour_ids_now() -> list[int]:
        # A short-lived session of its own: ``db``'s snapshot must not start
        # before the locks are held, or the delete below would not see a tour
        # created while waiting for them.
        with SessionLocal() as probe:
            return sorted(probe.execute(select(Tour.id).where(Tour.slide_id == slide_id)).scalars())

    wanted = tour_ids_now()
    for attempt in range(5):
        with contextlib.ExitStack() as stack:
            for tour_id in wanted:
                stack.enter_context(tour_asset_lock(tour_id))
            # A tour created while waiting is not covered by the locks taken:
            # lock it too (release and re-take in id order, so the ordering rule
            # that keeps two deletions from deadlocking still holds). The last
            # attempt deletes regardless; the window is milliseconds wide.
            current = tour_ids_now()
            if set(current) <= set(wanted) or attempt == 4:
                return _do_delete_slide_locked(slide_id, admin, db)
            wanted = sorted(set(wanted) | set(current))
    raise AssertionError("unreachable")  # pragma: no cover


def _do_delete_slide_locked(slide_id: int, admin: User, db: Session):
    slide = db.get(Slide, slide_id)
    if not slide:
        raise HTTPException(status_code=404, detail="Slide not found")

    # Collect tour ids (for the audit trail) and public_id tokens (for the
    # on-disk asset cleanup) before the cascade delete removes the rows. Asset
    # directories are keyed by the tour's public_id token (not the numeric id),
    # so the cleanup below must use the token or it would orphan the dirs.
    tour_ids = [t.id for t in slide.tours]
    tour_public_ids = [t.public_id for t in slide.tours if t.public_id]

    # Resolve the on-disk paths BEFORE the delete (so the guard reads a live
    # row) and route both through a realpath/strict-descent guard rather than
    # os.remove'ing the raw column. A crafted wsi_filename / thumb_filename
    # ("../../wsi.db"), reachable via a restored malicious DB, would otherwise
    # unlink an arbitrary file. On guard failure: skip + warn (the row is still
    # deleted; only the file cleanup is suppressed).
    wsi_remove_path: Optional[str] = None
    if (slide.wsi_filename or "").strip():
        try:
            from ..dzi import _slide_path
            wsi_remove_path = _slide_path(slide)
        except FileNotFoundError:
            logger.warning(
                "Refusing to remove WSI for slide %s: filename %r escapes wsi/",
                slide_id, slide.wsi_filename,
            )
    thumb_remove_path = _guarded_data_path("thumbs", slide.thumb_filename)
    if (slide.thumb_filename or "").strip() and thumb_remove_path is None:
        logger.warning(
            "Refusing to remove thumbnail for slide %s: filename %r escapes thumbs/",
            slide_id, slide.thumb_filename,
        )

    db.delete(slide)
    db.commit()

    def _remove_orphan(path: str, kind: str) -> None:
        """Delete a file owned by the slide row that was just removed.

        FileNotFoundError is expected (e.g. file already cleaned up by a
        previous failed delete, or never existed because the upload aborted
        mid-flight) and is silent. Any other OSError — typically permission
        problems or filesystem errors — is logged at WARNING and surfaced
        in the audit trail so an operator can reconcile, but does NOT fail
        the request: the slide row is already gone, so re-trying the API
        call will not help, and leaving the UI in a confused state is not
        desirable.
        """
        try:
            os.remove(path)
        except FileNotFoundError:
            return
        except OSError as exc:
            logger.warning("Could not remove orphan %s file %s: %s", kind, path, exc)
            audit(admin, "slide.delete.orphan", target=str(slide_id), kind=kind, path=path, error=str(exc))

    # Remove WSI file (guarded path resolved above)
    if wsi_remove_path:
        _remove_orphan(wsi_remove_path, "wsi")

    # Remove thumbnail (guarded path resolved above)
    if thumb_remove_path:
        _remove_orphan(thumb_remove_path, "thumb")

    # Remove tour asset directories (keyed by the tour's public_id token).
    # A malformed stored token (restored/hand-edited DB) is skipped rather than
    # resolved — tour_asset_dir refuses it precisely so this rmtree can't be
    # aimed outside the assets tree; the rows are already gone either way.
    for tour_public_id in tour_public_ids:
        if not valid_public_id(tour_public_id):
            logger.warning(
                "slide %s has a tour with a malformed public_id; "
                "leaving its assets on disk", slide_id
            )
            continue
        asset_dir = tour_asset_dir(tour_public_id)
        if os.path.isdir(asset_dir):
            shutil.rmtree(asset_dir, ignore_errors=True)

    # Evict DZI cache so a re-uploaded slide with the same ID is served fresh
    try:
        from ..dzi import clear_dz_cache
        clear_dz_cache(slide_id)
    except Exception:
        pass
    # Flush + bump the slide-meta sentinel so the deleted slide's public_id
    # stops resolving to a live (id, is_published) on every worker's /dzi/
    # tile path (see slide_resolve_cache).
    try:
        from .. import slide_resolve_cache
        slide_resolve_cache.invalidate_slide_meta()
    except Exception:
        logger.exception("Could not invalidate slide-meta cache after slide delete")

    audit(admin, "slide.delete", target=str(slide_id), tour_ids=tour_ids)
    return {"ok": True}


@router.get("/api/admin/slides/{slide_id}/macro")
def admin_slide_macro(
    slide_id: int,
    request: Request,
    admin: User = Depends(require_staff),
    db: Session = Depends(get_db),
):
    """Admin counterpart to ``/api/slides/{id}/macro`` that ignores publish state.

    The admin dashboard previews draft slides, so the public endpoint
    (which 404s on unpublished slides) is not enough.
    """
    slide = db.get(Slide, slide_id)
    if not slide or not slide.macro_data:
        raise HTTPException(status_code=404, detail="No macro image")
    etag = '"' + (slide.macro_etag or "") + '"'
    if request.headers.get("if-none-match") == etag:
        return Response(status_code=304, headers={"ETag": etag, "Cache-Control": "private, max-age=300"})
    return Response(
        content=slide.macro_data,
        media_type=slide.macro_mimetype or "image/jpeg",
        headers={"ETag": etag, "Cache-Control": "private, max-age=300"},
    )


@router.get("/api/admin/slides/{slide_id}/label")
def admin_slide_label(
    slide_id: int,
    request: Request,
    admin: User = Depends(require_staff),
    db: Session = Depends(get_db),
):
    """Serve the slide's vendor "label" associated image (admin only).

    Labels often carry barcodes, patient identifiers, or institution names
    written on the physical glass slide. They are kept behind admin auth even
    when the slide itself is published, so they cannot be discovered through
    the public listing endpoints. Cached privately in the browser for 5
    minutes, then revalidated via the bytes-derived ETag; the etag changes
    whenever the WSI is replaced, which re-runs ``_attach_associated_images``
    and rewrites ``label_etag``.
    """
    slide = db.get(Slide, slide_id)
    if not slide or not slide.label_data:
        raise HTTPException(status_code=404, detail="No label image")
    etag = '"' + (slide.label_etag or "") + '"'
    if request.headers.get("if-none-match") == etag:
        return Response(status_code=304, headers={"ETag": etag, "Cache-Control": "private, max-age=300"})
    return Response(
        content=slide.label_data,
        media_type=slide.label_mimetype or "image/jpeg",
        headers={"ETag": etag, "Cache-Control": "private, max-age=300"},
    )
