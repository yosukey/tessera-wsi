"""Admin tour, step, and asset management."""
from __future__ import annotations

import errno
import json
import logging
import math
import os
import secrets
import shutil
import time
from typing import Callable, List, Literal, Optional, Tuple, Union

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse, Response
from fastapi.routing import APIRoute
from pydantic import BaseModel, Field, TypeAdapter, ValidationError
from sqlalchemy import case, delete, func, select, update
from sqlalchemy import inspect as sa_inspect
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, selectinload

from .. import backup as _backup
from .. import disk_space as _disk_space
from .._serializers import step_to_out, tour_attribute_map, tour_json
from ..admin_helpers import (
    CTRL_CHAR_RE,
    api_error,
    apply_tour_attribute_delta,
    audit,
    error_detail,
    mint_public_id,
    rebuild_tour_attributes,
    safe_filename,
    storable_asset_name,
    tour_asset_dir,
    tour_asset_lock,
    tour_asset_url,
    valid_public_id,
    validate_attribute_kv,
)
from ..auth import MANAGER_OR_ABOVE, require_staff
from ..config import settings
from ..db import get_db
from ..models import MetadataOption, Slide, SlideAttribute, Step, StepPin, StepShape, Tour, TourAttribute, User
from ..schemas import (
    DEFAULT_TRANSITION_MODE,
    PinItem,
    Shape,
    StepReorderIn,
    StepUpdateIn,
    TRANSITION_MODES,
    AdminTourOut,
    TourUpdateIn,
    _MAX_STEP_DESCRIPTION_LEN,
    _MAX_STEP_FREEHAND_POINTS,
    _MAX_STEP_PINS,
    _MAX_STEP_SHAPES,
    _MAX_TITLE_LEN,
    _MAX_TOUR_DESCRIPTION_LEN,
    _MAX_TOUR_FREEHAND_POINTS,
    _MAX_TOUR_STEPS,
    freehand_point_count,
)

logger = logging.getLogger(__name__)


def _bounded_text(raw: str, *, field: str, max_len: int) -> str:
    """Reject free-text bodies that exceed the documented length cap.

    Mirrors the Pydantic validators on the JSON-body endpoints so the
    multipart/form-data create endpoints can't be used as a way around the
    same limits. Empty / None coerces to "" for column-default parity.

    Also rejects NUL bytes: ``description`` / ``description_md`` are TEXT
    columns rendered as markdown, so legitimate input may contain \\t \\n
    \\r — but a NUL can truncate the value when surfaced to logging /
    auditing / C-extension paths and never occurs in well-formed markdown.
    """
    if raw is None:
        return ""
    if len(raw) > max_len:
        raise HTTPException(
            status_code=422,
            detail=f"{field} must be {max_len} characters or fewer",
        )
    if "\x00" in raw:
        raise HTTPException(
            status_code=422,
            detail=f"{field} contains NUL bytes",
        )
    return raw


def _validate_step_label(raw: str) -> str:
    """Step label is a single-line title-like string; apply the same control
    character rule as the tour title so labels can't smuggle in \\n / \\r
    that would break the editor's single-line rendering."""
    raw = _bounded_text(raw, field="label", max_len=_MAX_TITLE_LEN)
    if CTRL_CHAR_RE.search(raw):
        raise HTTPException(status_code=422, detail="label contains control characters")
    return raw

# Reused for pin/shape payload validation in admin_add_step so the same shape
# / finite-value checks that StepUpdateIn enforces also apply at create time.
_PIN_LIST_ADAPTER = TypeAdapter(List[PinItem])
_SHAPE_LIST_ADAPTER = TypeAdapter(List[Shape])

router = APIRouter()


# ---------------------------------------------------------------------------
# Optimistic concurrency (see "Tour editing concurrency" in
# docs/developer-guide.md for the client-facing contract)
# ---------------------------------------------------------------------------
# The step PATCH replaces whole fields — the full pins / shapes_l0 lists
# included — so an editor left open in another tab, or another admin's, would
# overwrite every change saved since it loaded, silently. Each mutation therefore
# bumps a revision counter in SQL, and a request that says which revision it
# was built from (``expected_revision``) only applies if that is still the
# current one; otherwise it gets 409 plus the server's current copy, so the
# client can offer reload-or-overwrite without another round trip.
#
# Step.revision covers a step's own content; Tour.revision covers the tour's
# fields and its step structure (add / delete / reorder) — see the models.
#
# Atomicity: the bump is the *first write* of the request's transaction. On
# SQLite that statement takes the database write lock (pysqlite opens the
# transaction right before it, and a transaction's first statement being a
# write retries cleanly under busy_timeout instead of failing on a stale WAL
# snapshot), so from there to commit no other writer can interleave: the
# revision compared is the revision overwritten, and anything the handler
# reads after the bump is current. Validation that needs no lock runs before
# it, so a 4xx never consumes a revision.
#
# A request without expected_revision stays unconditional (scripts, other
# non-SPA callers) but still bumps, so editors that do send one notice its
# write.

STEP_CONFLICT_CODE = "adminTour.apiErr.stepConflict"
TOUR_CONFLICT_CODE = "adminTour.apiErr.tourConflict"
ANNOTATION_LIMIT_CODE = "adminTour.apiErr.annotationLimit"
POINT_BUDGET_CODE = "adminTour.apiErr.pointBudget"
STEP_LIMIT_CODE = "adminTour.apiErr.stepLimit"
TOUR_POINT_BUDGET_CODE = "adminTour.apiErr.tourPointBudget"


def _bump_revision(
    db: Session, model, row_id: int, expected: Optional[int]
) -> Optional[int]:
    """``UPDATE <model> SET revision = revision + 1 WHERE id = ?`` — plus
    ``AND revision = ?`` when ``expected`` is given — and return the new
    revision, or None when nothing matched (stale ``expected``, or the row is
    gone). Takes the write lock; see the section comment.

    synchronize_session=False: the in-session object keeps its stale
    ``revision``, which is never dirty, so the ORM flush can't write it back.

    On success every object the session loaded is expired: the handlers read
    their row BEFORE the bump (to 404 early), outside the lock, and another
    commit may have changed it since. Without the expiry an unconditional
    write (no ``expected``) that assigns a value the stale copy already holds
    would look like no change to the ORM and be dropped while the response
    reported it applied. The bump is the transaction's first write, so there
    are no pending changes to lose; attributes reload, current, on next use.
    """
    stmt = update(model).where(model.id == row_id).values(revision=model.revision + 1)
    if expected is not None:
        stmt = stmt.where(model.revision == expected)
    result = db.execute(stmt, execution_options={"synchronize_session": False})
    if result.rowcount != 1:
        return None
    db.expire_all()
    # Read back under the same lock instead of UPDATE … RETURNING, which needs
    # SQLite 3.35+ in whatever library the host's Python links against.
    return db.execute(select(model.revision).where(model.id == row_id)).scalar_one()


def _revision_conflict(code: str, revision: int, current) -> HTTPException:
    """409 in the api_error shape, plus the server's current copy.

    ``params`` carries only i18n interpolation scalars (see error_detail), so
    the resource rides alongside it under ``current``.
    """
    detail = error_detail(code, revision=revision)
    detail["current"] = jsonable_encoder(current)
    return HTTPException(status_code=409, detail=detail)


def _step_conflict(db: Session, tour_id: int, step_id: int) -> HTTPException:
    """The step's 409 (or 404 if it was deleted meanwhile). Rolls back first:
    the failed conditional bump still opened the write transaction, and the
    reload must see the committed row, not the session's cached copy."""
    db.rollback()
    s = db.get(Step, step_id)
    if s is None or s.tour_id != tour_id:
        return HTTPException(status_code=404, detail="Step not found")
    out = step_to_out(s)
    return _revision_conflict(STEP_CONFLICT_CODE, out["revision"], out)


def _tour_conflict(db: Session, tour_id: int) -> HTTPException:
    """The tour's 409 carrying the full admin tour envelope (tour + steps), as
    structure operations need the current step list to resync."""
    db.rollback()
    t = db.get(Tour, tour_id)
    if t is None:
        return HTTPException(status_code=404, detail="Tour not found")
    return _revision_conflict(TOUR_CONFLICT_CODE, t.revision, _tour_detail(db, t))


def _tour_out(t: Tour, slide_public_id: str) -> AdminTourOut:
    return AdminTourOut(
        id=t.id, public_id=t.public_id or "", slide_public_id=slide_public_id or "",
        slide_id=t.slide_id, title=t.title, description=t.description,
        is_published=t.is_published, unlisted=t.unlisted,
        revision=t.revision,
        attributes=tour_attribute_map(t),
    )


def _tour_and_steps(db: Session, t: Tour) -> tuple[dict, list]:
    steps = db.execute(
        select(Step).where(Step.tour_id == t.id).order_by(Step.order.asc())
    ).scalars().all()
    parent = db.get(Slide, t.slide_id)
    return _tour_out(t, (parent.public_id or "") if parent else "").model_dump(), steps


def _tour_detail(db: Session, t: Tour) -> dict:
    """The ``{tour, steps}`` envelope of admin_get_tour (which renders it
    with tour_json) as a JSON-ready dict, for a tour 409 (see step_to_out)."""
    # Same shape as public.get_tour — the admin UI and the public viewer
    # consume the same JSON — plus the revisions.
    tour, steps = _tour_and_steps(db, t)
    return {"tour": tour, "steps": [step_to_out(s) for s in steps]}


def _validate_tour_title(raw: str) -> str:
    """Apply the same validation rules to tour titles regardless of which
    endpoint produced them, so create and update agree: a tour can never
    be created in a state (empty title, over-length, control chars) that
    the editor couldn't subsequently save back."""
    title = (raw or "").strip()
    if not title:
        raise HTTPException(status_code=422, detail="Title must not be empty")
    if len(title) > _MAX_TITLE_LEN:
        raise HTTPException(
            status_code=422, detail=f"Title is too long (max {_MAX_TITLE_LEN})"
        )
    if CTRL_CHAR_RE.search(title):
        raise HTTPException(status_code=422, detail="Title contains control characters")
    return title


ATTR_NOT_REGISTERED_CODE = "adminTour.apiErr.attrValueNotRegistered"


def _clean_tour_attributes(raw: dict) -> dict:
    """``raw`` (TourUpdateIn.attributes) held to the slide metadata editor's
    rules, values stripped and de-duplicated, empty keys dropped."""
    cleaned: dict = {}
    for key, values in raw.items():
        validate_attribute_kv(key, values)
        vals = sorted({v.strip() for v in values if v and v.strip()})
        if vals:
            cleaned[key] = vals
    return cleaned


def _check_tour_vocabulary(db: Session, cleaned: dict, user: User) -> None:
    """Instructors may edit tours but not the metadata vocabulary, so they can
    only pick the values the dashboard offers them — the option list's:
    registered MetadataOptions plus values already in use on a slide or a
    tour (orphans; see admin_list_metadata_options), which includes any value
    this tour carries, so they can keep or clear it. What they can't do is
    introduce a new value. Managers and admins may store any valid value, as
    on slides.

    Runs after the revision bump, under the write lock, so a rename or delete
    of a value can't commit between this check and the write and leave the
    old value behind as an orphan.
    """
    if user.role not in MANAGER_OR_ABOVE:
        _check_vocabulary(db, cleaned)


def _check_vocabulary(db: Session, cleaned: dict) -> None:
    """The check itself, whoever the user: every value in ``cleaned`` must be
    one the option list offers, or a 422 names the first that isn't."""
    # Only the values asked about, looked up by (key, value) — indexed — table
    # by table until every one is found: a handful of queries however large
    # the tables, as a bulk change repeats this per tour under the lock.
    for key, vals in cleaned.items():
        missing = set(vals)
        for model in (MetadataOption, SlideAttribute, TourAttribute):
            if not missing:
                break
            missing -= set(db.execute(
                select(model.value).where(model.key == key, model.value.in_(missing)).distinct()
            ).scalars())
        if missing:
            raise api_error(422, ATTR_NOT_REGISTERED_CODE, key=key, value=min(missing))


# ===== Bulk tour operations =====
#
# The dashboard's slide list lets several tours be selected (independently of
# the slides) and changed in one request, for the same reason as the slide
# bulk endpoints in admin_slides: looping per-tour requests from the client
# trips nginx's rate limit. Each tour is handled like its single endpoint —
# its own revision bump (so an open editor sees the change), its own commit —
# and reported in its own result entry, so one tour's refusal never rolls
# back or blocks the rest.
#
# Declared before the variable `/api/admin/tours/{tour_id}/...` routes so a
# future `/{tour_id}/<name>` route can never capture `bulk` as a tour id.

BULK_MAX_TOURS = 500
# Values per bulk-attribute request; see BULK_MAX_ATTR_VALUES in admin_slides.
BULK_MAX_TOUR_ATTR_VALUES = 200


class BulkTourRef(BaseModel):
    id: int
    # The revision the dashboard listed the tour at. A tour changed since
    # (in its editor, another window) is skipped as a conflict rather than
    # overwritten, as the single endpoints' 409 does. None writes
    # unconditionally.
    expected_revision: Optional[int] = Field(default=None, ge=0)


class BulkTourVisibilityIn(BaseModel):
    tours: List[BulkTourRef]
    visibility: Literal["draft", "unlisted", "public"]


class BulkTourAttrIn(BaseModel):
    tours: List[BulkTourRef]
    key: str
    values: List[str] = Field(max_length=BULK_MAX_TOUR_ATTR_VALUES)
    mode: str  # "add" | "remove"


class BulkTourDeleteIn(BaseModel):
    tour_ids: List[int]


def _check_bulk_tour_count(count: int) -> None:
    if count > BULK_MAX_TOURS:
        raise api_error(422, "adminDash.apiErr.bulkTooManyTours", max=BULK_MAX_TOURS, count=count)


def _run_bulk_tour_writes(
    db: Session,
    refs: List[BulkTourRef],
    apply: Callable[[Tour], Union[str, Tuple[bool, dict]]],
) -> Tuple[List[dict], List[int]]:
    """Write each tour as its single endpoint would, returning the per-tour
    results and the ids that changed.

    The revision bump comes first: it takes the write lock and is the
    conflict check (``conflict`` when the tour is at another revision,
    ``not_found`` when it is gone). ``apply(tour)`` then runs under the lock
    and returns an error code to refuse the tour, or ``(changed, fields)``
    for the result entry; a tour it found nothing to do for has its bump
    undone (it keeps its revision). Each tour commits on its own, and any
    other error (the lock not coming free in time, a refused write) is that
    tour's ``failed`` — the tours before it are committed, so the batch must
    still report them. A repeated id is handled once — its second ref would
    only meet its own bump as a conflict.

    Nothing is read from a tour after a rollback: that expires it, and
    reloading it outside the lock could meet a concurrent delete.
    """
    results: List[dict] = []
    changed_ids: List[int] = []
    seen: set = set()
    for ref in refs:
        if ref.id in seen:
            continue
        seen.add(ref.id)
        try:
            result = _bulk_tour_write(db, ref, apply)
        except Exception:  # noqa: BLE001 — must not abort the whole batch
            db.rollback()
            logger.exception("bulk tour write failed for tour %s", ref.id)
            result = {"tour_id": ref.id, "ok": False, "changed": False, "error": "failed"}
        results.append(result)
        if result["changed"]:
            changed_ids.append(ref.id)
    return results, changed_ids


def _bulk_tour_write(
    db: Session, ref: BulkTourRef, apply: Callable[[Tour], Union[str, Tuple[bool, dict]]],
) -> dict:
    """One tour of _run_bulk_tour_writes: its result entry."""
    def fail(error: str) -> dict:
        return {"tour_id": ref.id, "ok": False, "changed": False, "error": error}

    new_rev = _bump_revision(db, Tour, ref.id, ref.expected_revision)
    if new_rev is None:
        db.rollback()
        exists = db.execute(select(Tour.id).where(Tour.id == ref.id)).first() is not None
        return fail("conflict" if exists else "not_found")
    outcome = apply(db.get(Tour, ref.id))
    if isinstance(outcome, str):
        db.rollback()
        return fail(outcome)
    changed, fields = outcome
    if not changed:
        db.rollback()  # undo the bump: nothing to write
        return {"tour_id": ref.id, "ok": True, "changed": False, "error": None,
                **fields, "revision": new_rev - 1}
    db.commit()
    return {"tour_id": ref.id, "ok": True, "changed": True, "error": None,
            **fields, "revision": new_rev}


@router.post("/api/admin/tours/bulk/visibility")
def admin_bulk_tour_visibility(
    body: BulkTourVisibilityIn,
    admin: User = Depends(require_staff),
    db: Session = Depends(get_db),
):
    """Move several tours to one visibility level (draft / link-only /
    public), as the dashboard's per-tour control does in one or two calls.

    Per-tour errors: ``not_found``, ``conflict`` (changed since listed),
    ``parent_draft`` (link-only / public needs a published parent slide; the
    parent is never published as a side effect of a bulk change — the
    per-row control asks first, which a batch can't), ``failed``.
    A tour already at the level is reported ok with ``changed`` false and
    keeps its revision.
    """
    _check_bulk_tour_count(len(body.tours))
    target = body.visibility
    want_published = target != "draft"

    def apply(t: Tour) -> Union[str, Tuple[bool, dict]]:
        # A draft keeps its link-only flag, as applyVisibility leaves it:
        # only publishing decides whether the tour is listed.
        want_unlisted = bool(t.unlisted) if target == "draft" else target == "unlisted"
        flags = {"is_published": want_published, "unlisted": want_unlisted}
        if t.is_published == want_published and bool(t.unlisted) == want_unlisted:
            return False, flags
        # Under the write lock, like admin_publish_tour: a concurrent slide
        # unpublish can't land between this check and the commit.
        if want_published:
            parent = db.get(Slide, t.slide_id)
            if not parent or not parent.is_published:
                return "parent_draft"
        t.is_published = want_published
        t.unlisted = want_unlisted
        return True, flags

    results, changed_ids = _run_bulk_tour_writes(db, body.tours, apply)
    audit(admin, "tour.bulk_visibility", visibility=target,
          requested=len(body.tours), changed=changed_ids)
    return {"ok": True, "results": results}


@router.post("/api/admin/tours/bulk/attributes")
def admin_bulk_tour_attributes(
    body: BulkTourAttrIn,
    admin: User = Depends(require_staff),
    db: Session = Depends(get_db),
):
    """Add or remove one category's value(s) in several tours' OWN metadata
    (what they inherit from their slide is untouched), like
    admin_bulk_slide_attributes does for slides.

    Instructors may only add values the option list offers them, as in a
    single tour save (see _check_tour_vocabulary). Per-tour errors:
    ``not_found``, ``conflict``, ``not_registered``, ``failed``. A tour
    the change doesn't move is reported ok with ``changed`` false and keeps its
    revision.
    """
    _check_bulk_tour_count(len(body.tours))
    if body.mode not in ("add", "remove"):
        raise api_error(422, "adminDash.apiErr.invalidMode", mode=body.mode)
    key = (body.key or "").strip()
    if not key:
        raise api_error(422, "adminDash.apiErr.attrKeyRequired")
    validate_attribute_kv(key, body.values)
    values = sorted({v.strip() for v in body.values if v and v.strip()})
    if not values:
        # Nothing to add or remove: refuse rather than bump and roll back
        # every tour for a certain no-op.
        raise api_error(422, "adminDash.apiErr.metaValRequired")
    # Only an instructor's additions are held to the vocabulary (see
    # _check_tour_vocabulary); decided here once, as each tour's revision
    # bump expires `admin` and reading its role again would reload the user
    # under the write lock.
    vocabulary = (
        {key: values} if body.mode == "add" and admin.role not in MANAGER_OR_ABOVE else None
    )
    # Refuse an unregistered value up front, with the single save's message,
    # rather than as a per-tour error on every tour. Checked again per tour
    # under the write lock below (a rename / delete may land in between).
    if vocabulary:
        _check_vocabulary(db, vocabulary)

    def apply(t: Tour) -> Union[str, Tuple[bool, dict]]:
        if vocabulary:
            try:
                _check_vocabulary(db, vocabulary)
            except HTTPException:
                return "not_registered"
        return apply_tour_attribute_delta(t, key, values, body.mode), {}

    results, changed_ids = _run_bulk_tour_writes(db, body.tours, apply)
    audit(admin, "tour.bulk_attributes", key=key, mode=body.mode, values=values,
          requested=len(body.tours), changed=changed_ids)
    return {"ok": True, "results": results}


@router.post("/api/admin/tours/bulk/delete")
def admin_bulk_delete_tours(
    body: BulkTourDeleteIn,
    admin: User = Depends(require_staff),
    db: Session = Depends(get_db),
):
    """Delete several tours best-effort, each as admin_delete_tour does (row
    and on-disk assets, its own commit). Buckets as the slide bulk delete:
    ``deleted``, ``not_found`` (already gone), ``failed`` (retryable)."""
    _check_bulk_tour_count(len(body.tour_ids))
    deleted: List[int] = []
    not_found: List[int] = []
    failed: List[dict] = []
    for tour_id in dict.fromkeys(body.tour_ids):  # each id once, in order
        try:
            if _do_delete_tour(db, tour_id):
                deleted.append(tour_id)
            else:
                not_found.append(tour_id)
        except Exception:  # noqa: BLE001 — must not abort the whole batch
            db.rollback()
            # The detail stays in the log: a database error's text (SQL
            # included) is not for the dashboard's alert.
            logger.exception("bulk delete failed for tour %s", tour_id)
            failed.append({"tour_id": tour_id, "error": "failed"})
    audit(admin, "tour.bulk_delete", requested=len(body.tour_ids), deleted=deleted,
          not_found=not_found, failed=[f["tour_id"] for f in failed])
    return {"ok": True, "deleted": deleted, "not_found": not_found, "failed": failed}


@router.post("/api/admin/slides/{slide_id}/tours")
def admin_create_tour(slide_id: int, title: str = Form("Tour"), description: str = Form(""), admin: User = Depends(require_staff), db: Session = Depends(get_db)):
    if not db.get(Slide, slide_id):
        raise HTTPException(status_code=404, detail="Slide not found")
    title = _validate_tour_title(title)
    description = _bounded_text(description, field="description", max_len=_MAX_TOUR_DESCRIPTION_LEN)
    # Reserve-and-retry on the unique public_id: re-mint on the (astronomically
    # unlikely) collision instead of surfacing a 500. The slide's existence is
    # checked above, so within this handler an IntegrityError is the public_id
    # clash. Mirrors the slide-id reservation loop in admin_slides.
    t: Optional[Tour] = None
    for _attempt in range(5):
        t = Tour(
            slide_id=slide_id, title=title, description=description,
            is_published=False, public_id=mint_public_id(),
        )
        db.add(t)
        try:
            db.commit()
            break
        except IntegrityError:
            db.rollback()
            t = None
    if t is None:
        raise HTTPException(status_code=500, detail="Failed to allocate tour id")
    db.refresh(t)
    audit(admin, "tour.create", target=str(t.id), slide_id=slide_id)
    return {"id": t.id, "public_id": t.public_id}


# ===== Tour duplicate =====
#
# A copy is a new draft tour on the same slide: the source's description,
# own metadata, link-only flag and every step (pins and shapes included), plus
# its uploaded assets under the copy's own public_id, so editing or deleting
# either tour never touches the other's files. Step markdown that points at
# the source's assets is rewritten to the copy's.

# Columns a copied row does not take from its source: its own key, the owner
# it is re-pointed at, and the optimistic-concurrency counter (a new row
# starts at 0). Everything else is copied by mapper introspection, so a
# column added to these models later is carried without touching this.
_COPY_SKIP_COLUMNS = frozenset({"id", "revision", "tour_id", "step_id"})


def _copied_columns(row, **overrides) -> dict:
    values = {
        attr.key: getattr(row, attr.key)
        for attr in sa_inspect(type(row)).column_attrs
        if attr.key not in _COPY_SKIP_COLUMNS
    }
    values.update(overrides)
    return values


def _rewrite_tour_asset_refs(md: str, old_pid: str, new_pid: str) -> str:
    """Point ``/assets/tours/<old>/`` references at the copy's asset dir.
    Same rewrite as the slide import's (slide_transfer._rewrite_asset_paths)."""
    if not md or not old_pid:
        return md
    return md.replace(f"/assets/tours/{old_pid}/", f"/assets/tours/{new_pid}/")


def _copy_tour_assets(src_dir: str, dst_dir: str) -> bool:
    """Copy a tour's attachments — the flat, non-dot regular files an upload
    writes (backup._tour_asset_files) — into a new ``dst_dir``, and return
    whether it was created (a tour without attachments gets none). A failed
    copy removes what it wrote and answers 507 when the volume is full.

    The caller holds the source tour's ``tour_asset_lock``, so no upload
    changes the set meanwhile; a delete (which does not lock) may still
    remove a file, which is then left out as the delete intended. Each file
    is read through an O_NOFOLLOW fd, so an entry swapped for a symlink after
    the listing is never followed out of the tree."""
    files = _backup._tour_asset_files(src_dir)
    if not files:
        return False
    total = 0
    for e in files:
        try:
            total += e.stat(follow_symlinks=False).st_size
        except OSError:
            continue
    # Same instance-wide floor as an upload: a copy is that many uploads.
    _enforce_tour_asset_free_space(src_dir, total)
    # Not exist_ok: the caller picked an id no dir holds, and the cleanup
    # below must only ever remove a dir this call made.
    os.makedirs(dst_dir)
    try:
        for e in files:
            try:
                src_fd = os.open(e.path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
            except FileNotFoundError:
                continue
            with os.fdopen(src_fd, "rb") as src:
                dst_fd = os.open(
                    os.path.join(dst_dir, e.name), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644,
                )
                with os.fdopen(dst_fd, "wb") as dst:
                    shutil.copyfileobj(src, dst)
    except BaseException as exc:
        shutil.rmtree(dst_dir, ignore_errors=True)
        if isinstance(exc, OSError) and exc.errno == errno.ENOSPC:
            raise api_error(507, "adminDash.apiErr.uploadNoSpace") from exc
        raise
    return True


def _unused_tour_public_id(db: Session) -> str:
    """A fresh public_id that no tour row and no asset dir holds yet."""
    for _attempt in range(5):
        pid = mint_public_id()
        taken = db.execute(select(Tour.id).where(Tour.public_id == pid)).first() is not None
        if not taken and not os.path.exists(tour_asset_dir(pid)):
            return pid
    raise HTTPException(status_code=500, detail="Failed to allocate tour id")


@router.post("/api/admin/tours/{tour_id}/duplicate")
def admin_duplicate_tour(
    tour_id: int,
    # The copy's title; omitted, it keeps the source's.
    title: Optional[str] = Form(None),
    admin: User = Depends(require_staff),
    db: Session = Depends(get_db),
):
    src = db.get(Tour, tour_id)
    if not src:
        raise HTTPException(status_code=404, detail="Tour not found")
    # Validated before any work, so a 422 copies nothing.
    if title is not None:
        title = _validate_tour_title(title)
    # The source's lock keeps its asset set still while it is copied, and
    # holds off a delete of the source (or of its slide, which also takes its
    # tours' locks — and re-lists them, so it then sees the copy too) until
    # the copy has committed.
    with tour_asset_lock(tour_id):
        # A delete that finished before the lock was taken: fresh query, as
        # db.get would answer from the identity map.
        if db.execute(select(Tour.id).where(Tour.id == tour_id)).first() is None:
            raise HTTPException(status_code=404, detail="Tour not found")
        src_pid = src.public_id or ""
        slide_id = src.slide_id
        new_pid = _unused_tour_public_id(db)
        new_dir = tour_asset_dir(new_pid)
        # Copied before the first write below: that opens SQLite's write
        # transaction, and holding it across file copies would stall every
        # other writer (as the slide import avoids for the same reason). A
        # malformed source token (a restored / hand-edited DB) has no
        # resolvable asset dir; the copy then just goes without its assets.
        made_dir = valid_public_id(src_pid) and _copy_tour_assets(tour_asset_dir(src_pid), new_dir)
        try:
            copy = Tour(slide_id=slide_id, public_id=new_pid, title=title or src.title, is_published=False)
            db.add(copy)
            # The first write: from here to the commit no other writer can
            # interleave, so what is read below is the source as it stands,
            # not a mix of two of its saves.
            db.flush()
            new_id = copy.id
            db.refresh(src)
            if title is None:
                copy.title = src.title
            copy.description = _rewrite_tour_asset_refs(src.description, src_pid, new_pid)
            # A draft keeps its link-only flag (see admin_bulk_tour_visibility):
            # the copy is listed or not like its source once published.
            copy.unlisted = src.unlisted
            for a in db.execute(
                select(TourAttribute).where(TourAttribute.tour_id == tour_id)
            ).scalars():
                db.add(TourAttribute(tour_id=copy.id, **_copied_columns(a)))
            steps = db.execute(
                select(Step)
                .where(Step.tour_id == tour_id)
                .options(selectinload(Step.pins), selectinload(Step.shapes))
                .order_by(Step.order.asc())
            ).scalars().all()
            for s in steps:
                new_step = Step(**_copied_columns(
                    s, tour_id=copy.id,
                    description_md=_rewrite_tour_asset_refs(s.description_md, src_pid, new_pid),
                ))
                db.add(new_step)
                db.flush()
                # In id order (the relationships' order_by), so the copy's
                # on-canvas index badges read as the source's.
                for p in s.pins:
                    db.add(StepPin(step_id=new_step.id, **_copied_columns(p)))
                for sh in s.shapes:
                    db.add(StepShape(step_id=new_step.id, **_copied_columns(sh)))
            db.commit()
        except BaseException:
            db.rollback()
            if made_dir:
                shutil.rmtree(new_dir, ignore_errors=True)
            raise
    audit(admin, "tour.duplicate", target=str(new_id), source=str(tour_id),
          slide_id=slide_id, steps=len(steps))
    return {"id": new_id, "public_id": new_pid}


@router.get("/api/admin/tours", response_model=List[AdminTourOut])
def admin_list_all_tours(admin: User = Depends(require_staff), db: Session = Depends(get_db)):
    rows = db.execute(
        select(Tour, Slide.public_id)
        .join(Slide, Slide.id == Tour.slide_id)
        .options(selectinload(Tour.attributes))
        .order_by(Tour.slide_id.asc(), Tour.id.asc())
    ).all()
    return [_tour_out(t, slide_public_id) for t, slide_public_id in rows]


@router.get("/api/admin/slides/{slide_id}/tours", response_model=List[AdminTourOut])
def admin_list_slide_tours(slide_id: int, admin: User = Depends(require_staff), db: Session = Depends(get_db)):
    parent = db.get(Slide, slide_id)
    if not parent:
        raise HTTPException(status_code=404, detail="Slide not found")
    tours = db.execute(
        select(Tour)
        .where(Tour.slide_id == slide_id)
        .options(selectinload(Tour.attributes))
        .order_by(Tour.id.asc())
    ).scalars().all()
    return [_tour_out(t, parent.public_id or "") for t in tours]


@router.post("/api/admin/tours/{tour_id}/publish")
def admin_publish_tour(
    tour_id: int,
    publish: int = Form(1),
    # The tour's revision (Tour.revision covers is_published).
    expected_revision: Optional[int] = Form(None, ge=0),
    admin: User = Depends(require_staff),
    db: Session = Depends(get_db),
):
    t = db.get(Tour, tour_id)
    if not t:
        raise HTTPException(status_code=404, detail="Tour not found")
    will_publish = bool(publish)
    new_rev = _bump_revision(db, Tour, tour_id, expected_revision)
    if new_rev is None:
        raise _tour_conflict(db, tour_id)
    # Refuse to publish a tour whose parent slide is still a draft: the
    # public get_tour endpoint already 404s in that case via defence in
    # depth, so flipping the flag here would leave the DB in a confusing
    # "published but invisible" state. Surfacing 409 to the admin makes
    # the requirement explicit ("publish the slide first") instead of
    # silently masking the visibility cascade. Checked after the bump, under
    # the write lock, so a concurrent slide unpublish (whose cascade drafts
    # the slide's tours) can't land between this check and the commit.
    if will_publish:
        slide = db.get(Slide, t.slide_id)
        if not slide or not slide.is_published:
            db.rollback()
            raise HTTPException(
                status_code=409,
                detail="Cannot publish a tour while its parent slide is a draft",
            )
    t.is_published = will_publish
    db.commit()
    audit(admin, "tour.publish", target=str(tour_id), published=will_publish)
    # Echo the confirmed flag and the new revision: the client needs the
    # latter for its next tour write (applyVisibility may follow up with the
    # unlisted toggle straight away).
    return {"ok": True, "is_published": will_publish, "revision": new_rev}


@router.post("/api/admin/tours/{tour_id}/unlisted")
def admin_set_tour_unlisted(
    tour_id: int,
    unlisted: int = Form(1),
    expected_revision: Optional[int] = Form(None, ge=0),
    admin: User = Depends(require_staff),
    db: Session = Depends(get_db),
):
    """Toggle a tour's link-only ("unlisted") visibility.

    Mirrors admin_set_slide_unlisted: orthogonal to publish, settable in any
    publish state, and only affects the public tour listings (public.list_tours
    / list_all_tours). The direct /api/tours/{id} URL keeps working while the
    tour and its parent slide stay published, so the tour can be shared by link
    without appearing in browse.
    """
    t = db.get(Tour, tour_id)
    if not t:
        raise HTTPException(status_code=404, detail="Tour not found")
    new_state = bool(unlisted)
    new_rev = _bump_revision(db, Tour, tour_id, expected_revision)
    if new_rev is None:
        raise _tour_conflict(db, tour_id)
    t.unlisted = new_state
    db.commit()
    audit(admin, "tour.unlisted", target=str(tour_id), unlisted=new_state)
    return {"ok": True, "unlisted": new_state, "revision": new_rev}


@router.patch("/api/admin/tours/{tour_id}")
def admin_update_tour(
    tour_id: int,
    body: TourUpdateIn,
    admin: User = Depends(require_staff),
    db: Session = Depends(get_db),
):
    t = db.get(Tour, tour_id)
    if not t:
        raise HTTPException(status_code=404, detail="Tour not found")
    # Validate before the revision bump so a 422 never consumes a revision.
    title = _validate_tour_title(body.title) if body.title is not None else None
    attrs = _clean_tour_attributes(body.attributes) if body.attributes is not None else None
    new_rev = _bump_revision(db, Tour, tour_id, body.expected_revision)
    if new_rev is None:
        raise _tour_conflict(db, tour_id)
    if attrs is not None:
        try:
            _check_tour_vocabulary(db, attrs, admin)
        except HTTPException:
            # Undo the bump: a refused write must not consume a revision.
            db.rollback()
            raise
    changed: list[str] = []
    if title is not None:
        t.title = title
        changed.append("title")
    if body.description is not None:
        t.description = body.description
        changed.append("description")
    if attrs is not None:
        # The bump holds the write lock, so no other writer can touch this
        # tour's rows between the rebuild's DELETE and INSERT.
        rebuild_tour_attributes(t, db, attrs)
        changed.append("attributes")
    # Built before the commit, while the write lock still holds: re-reading
    # after it could meet a concurrent delete and turn a saved write into a
    # 500 (ObjectDeletedError).
    db.flush()
    parent = db.get(Slide, t.slide_id)
    out = _tour_out(t, (parent.public_id or "") if parent else "")
    db.commit()
    audit(admin, "tour.update", target=str(tour_id), fields=changed,
          **({"attributes": attrs} if attrs is not None else {}))
    return {"ok": True, "revision": new_rev, "tour": out}


@router.delete("/api/admin/tours/{tour_id}")
def admin_delete_tour(
    tour_id: int,
    admin: User = Depends(require_staff),
    db: Session = Depends(get_db),
):
    if not _do_delete_tour(db, tour_id):
        raise HTTPException(status_code=404, detail="Tour not found")
    audit(admin, "tour.delete", target=str(tour_id))
    return {"ok": True}


def _do_delete_tour(db: Session, tour_id: int) -> bool:
    """Delete the tour row and its asset directory; False when there is no
    such tour. Shared by the single and the bulk delete."""
    t = db.get(Tour, tour_id)
    if not t:
        return False
    # Capture the public_id before the row is gone — the asset dir is keyed by it.
    tour_public_id = t.public_id or ""
    # Row delete + directory removal are one critical section against asset
    # uploads (tour_asset_lock): an upload that passed its existence check
    # earlier would otherwise recreate the directory after its removal,
    # leaving publicly served files for a tour that no longer exists. The
    # upload re-checks the row inside the same lock.
    with tour_asset_lock(tour_id):
        db.delete(t)
        db.commit()
        # Mirror admin_delete_slide: drop the on-disk asset directory after the
        # row is gone so deleting a tour doesn't strand its uploads on disk
        # forever. Run after commit so a rollback (e.g. FK constraint failure)
        # doesn't take the files with it.
        # Skip (don't 500) when the stored token is malformed — the row is
        # already deleted, so the only thing left is best-effort file cleanup,
        # and tour_asset_dir refuses to resolve a token that could escape the
        # tree.
        if valid_public_id(tour_public_id):
            asset_dir = tour_asset_dir(tour_public_id)
            if os.path.isdir(asset_dir):
                shutil.rmtree(asset_dir, ignore_errors=True)
        elif tour_public_id:
            logger.warning(
                "tour %s has a malformed public_id; leaving its assets on disk", tour_id
            )
    return True


@router.get("/api/admin/tours/{tour_id}")
def admin_get_tour(tour_id: int, admin: User = Depends(require_staff), db: Session = Depends(get_db)):
    t = db.get(Tour, tour_id)
    if not t:
        raise HTTPException(status_code=404, detail="Tour not found")
    # _tour_detail's envelope, rendered straight to the body (see
    # _serializers.tour_json): no jsonable_encoder walk, and stored freehand
    # vertices are spliced in as text rather than decoded and re-encoded.
    return Response(tour_json(db, *_tour_and_steps(db, t)), media_type="application/json")


# Overwrite uploads stream to a hidden `.{name}.upload-{token}` sibling and
# os.replace() it onto the final name only after validation passes. A crash
# (or hard kill) between the write and the replace strands that hidden temp;
# because the asset listing skips dot-prefixed names it would otherwise
# accumulate invisibly. Sweep temps older than this threshold — comfortably
# longer than any real upload, so an in-flight concurrent overwrite is never
# collected out from under itself.
_UPLOAD_TEMP_MAX_AGE_SECONDS = 3600


def _sweep_stale_upload_temps(base: str) -> None:
    """Best-effort reaping of orphaned overwrite temp files under ``base``.

    Called opportunistically from the asset-list endpoint. Never raises: a
    permission error, or a file vanishing mid-sweep (a concurrent os.replace),
    is swallowed so the caller's own work is unaffected.
    """
    try:
        entries = os.listdir(base)
    except OSError:
        return
    cutoff = time.time() - _UPLOAD_TEMP_MAX_AGE_SECONDS
    for name in entries:
        if not (name.startswith('.') and '.upload-' in name):
            continue
        p = os.path.join(base, name)
        try:
            if os.path.getmtime(p) < cutoff:
                os.remove(p)
        except OSError:
            continue


def _enforce_tour_asset_quota(
    base: str, *, overwriting: bool, replacing_name: str = ""
) -> Optional[int]:
    """Refuse a new tour-asset upload once the per-tour quota is reached.

    Returns the number of bytes an *additive* upload may still occupy, or None
    when no byte quota is configured or the upload replaces an existing asset.
    The caller streams against that budget: checking only *current* usage would
    let every upload overshoot ``tour_asset_max_total_bytes`` by up to one full
    per-file cap. The budget is only valid while nothing else writes to the
    tour, so the caller must hold ``tour_asset_lock`` from this check until
    the upload is published. Replacements are exempt for the same reason they are
    exempt from the count and byte totals below — see the paragraph on
    replacement, and test_tour_asset_overwrite_allowed_at_byte_quota.

    The per-file size cap is not a bound on total usage: this endpoint is open
    to the lowest-privilege staff role and the "rename" policy mints a fresh
    filename on every upload, so a loop can accumulate unbounded distinct files
    until the data volume fills — which takes WSI uploads, backups and SQLite
    writes down with it, and is only recoverable by deleting assets one by one.

    Replacing a file that already exists neither adds a file nor (necessarily)
    grows the total, so it is measured against what usage will look like
    *after* the swap: the target is excluded from both the count and the byte
    total. Without that, an author who hit the cap could not even re-upload a
    corrected version of an existing image — they would have to delete it first,
    breaking every markdown reference in the meantime. An overwrite aimed at a
    name that does not exist yet is a plain addition and is capped normally.

    Orphaned ``.upload-`` temps are swept first so they can't consume quota.
    """
    max_files = settings.tour_asset_max_files
    max_total = settings.tour_asset_max_total_bytes
    if max_files <= 0 and max_total <= 0:
        return None
    _sweep_stale_upload_temps(base)
    try:
        entries = os.listdir(base)
    except OSError:
        return None
    replacing = replacing_name if overwriting else ""
    count = 0
    total = 0
    replaces_existing = False
    for name in entries:
        if name.startswith('.'):
            continue
        if replacing and name == replacing:
            # About to be swapped out; don't charge it to the new upload.
            replaces_existing = True
            continue
        count += 1
        try:
            total += os.path.getsize(os.path.join(base, name))
        except OSError:
            continue
    if max_files > 0 and not replaces_existing and count >= max_files:
        raise HTTPException(
            status_code=409,
            detail=(
                f"This tour already has the maximum of {max_files} assets. "
                f"Delete unused assets before uploading more."
            ),
        )
    if max_total > 0 and total >= max_total:
        raise HTTPException(
            status_code=409,
            detail=(
                f"This tour's assets already use the maximum of "
                f"{max_total // (1024 * 1024)} MiB. Delete unused assets "
                f"before uploading more."
            ),
        )
    if max_total <= 0 or replaces_existing:
        return None
    return max_total - total


def _enforce_tour_asset_free_space(base: str, size: int) -> None:
    """Refuse a tour-asset upload that would leave the volume under its reserve.

    The quotas in :func:`_enforce_tour_asset_quota` bound one tour. Tours cost
    nothing to create, though, and the lowest staff role that may upload assets
    may also add tours — each with a fresh 200-file / 200 MiB allowance — so the
    per-tour caps alone never bounded what an instructor's loop could put on
    the data volume. This is the instance-wide floor: one statvfs of the
    asset directory (measured where it really lives, should ``assets/`` be a
    symlink to another volume), via the shared disk_space check every large
    write uses, with ``tour_asset_min_free_bytes`` held back on top of the
    file itself.

    That operator setting is the one reserve that is not
    disk_space.reserve_for's 10 % / 256 MiB: a tour asset is a few MB at
    most, so the shared policy would always ask for its 256 MiB floor, while
    this documented knob (1 GiB by default, 0 = off) is tuned per install,
    and guards a different risk — many small writes by the lowest
    staff role adding up, not one large one. So it is passed as the reserve
    override; the mechanics (unknown capacity passes) are the shared ones.

    Checked under ``tour_asset_lock``, which serialises one tour only:
    concurrent uploads to *different* tours can each pass and dip below the
    reserve by at most one per-file cap apiece — noise against a reserve in
    GiB. Replacements are not exempt (unlike the per-tour quota): at the
    reserve it is the volume that is out of room, not the tour, and the swap
    writes a whole temp copy first anyway. Unknown capacity passes, as for
    slide uploads; a write that then hits ENOSPC still drops its partial file.
    """
    reserve = settings.tour_asset_min_free_bytes
    if reserve <= 0:
        return
    if _disk_space.check(base, size, reserve=reserve) is not None:
        raise api_error(507, "adminDash.apiErr.uploadNoSpace")


@router.get("/api/admin/tours/{tour_id}/assets")
def admin_list_tour_assets(
    tour_id: int,
    admin: User = Depends(require_staff),
    db: Session = Depends(get_db),
):
    t = db.get(Tour, tour_id)
    if not t:
        raise HTTPException(status_code=404, detail="Tour not found")
    items = []
    # The per-file upload cap (TOUR_ASSET_MAX_BYTES), so the editor checks and
    # names the limit the server enforces instead of a number of its own.
    out = {"items": items, "max_bytes": settings.tour_asset_max_bytes}
    if not valid_public_id(t.public_id):
        return out
    base = tour_asset_dir(t.public_id)
    if not os.path.isdir(base):
        return out
    # Opportunistically reap orphaned overwrite temp files before listing.
    _sweep_stale_upload_temps(base)
    for name in sorted(os.listdir(base)):
        if name.startswith('.'):
            continue
        items.append({
            "name": name,
            "url": tour_asset_url(t.public_id, name),
        })
    return out


# Two-phase validation:
#
# Phase 1 — `_is_allowed_asset_mime`: cheap, advisory pre-filter that runs
# before the upload starts, so blatantly wrong types (text/html, application/x-msdownload)
# get rejected without consuming the configured size budget on disk. Note
# that the Content-Type from a multipart upload is client-supplied, so this
# is NOT a security boundary — it is an optimisation. The actual content
# check lives in phase 2.
#
# Phase 2 — `_validate_asset_content`: runs after the upload finishes,
# inspects the first 64 bytes of the on-disk file, and rejects anything
# whose magic bytes do not match an allowed family or whose claimed
# Content-Type is contradicted by the actual content. This is the real
# security gate; an attacker who lies in the multipart header still has to
# produce a payload that begins with one of the magic prefixes below.
def _is_allowed_asset_mime(content_type: Optional[str]) -> bool:
    if not content_type:
        return False
    ct = content_type.split(";", 1)[0].strip().lower()
    return any(ct.startswith(p) for p in settings.tour_asset_allowed_mime_prefixes)


# Magic-byte signatures for the accepted asset families. Match against the
# start of the file (the validator reads its first 64 bytes; the longest
# signature here is 8). Anything not on this list (text, HTML, SVG,
# scripts, executables, ...) gets rejected as "unrecognised content".
_ASSET_MAGIC_PREFIXES: tuple[tuple[bytes, str], ...] = (
    (b"\x89PNG\r\n\x1a\n", "image"),
    (b"\xff\xd8\xff", "image"),                 # JPEG
    (b"GIF87a", "image"),
    (b"GIF89a", "image"),
    (b"RIFF", "image_or_audio"),                # WebP / WAV — disambiguated below
    (b"BM", "image"),                            # BMP
    (b"%PDF-", "pdf"),
    (b"ID3", "audio"),                           # MP3 with ID3 tag
    (b"\xff\xfb", "audio"),                      # MP3 frame
    (b"\xff\xf3", "audio"),
    (b"\xff\xf2", "audio"),
    (b"OggS", "audio"),                          # Ogg
    (b"fLaC", "audio"),                          # FLAC
)


# Lower-cased prefixes that, when seen at the very start of an upload, are
# always rejected even before family detection. SVG / XML / HTML can carry
# <script> and event handlers; the frontend renders asset URLs directly
# inside <img> and markdown contexts, so a server that serves an SVG with
# a forged Content-Type would yield active content.
_FORBIDDEN_TEXT_PREFIXES: tuple[bytes, ...] = (
    b"<?xml",
    b"<svg",
    b"<!doctype",
    b"<html",
    b"<script",
)


def _detect_asset_family(head: bytes) -> Optional[str]:
    """Return one of {"image", "pdf", "audio", "video"} based on head bytes.

    Returns None when the file does not match any allowed family.
    """
    if not head:
        return None

    # MP4 family: "....ftyp" at offset 4 is the canonical ISO Base Media box.
    # The container is shared by video and audio, so the major brand at offset
    # 8 decides which. Classifying every ftyp file as "video" would make the
    # cross-check in _validate_asset_content reject a legitimate .m4a
    # (Content-Type audio/mp4, brand "M4A ") with 415 — inert while the
    # default TOUR_ASSET_ALLOWED_MIME_PREFIXES is "image/", but wrong for the
    # audio-enabled deployments config.py explicitly invites.
    if len(head) >= 12 and head[4:8] == b"ftyp":
        brand = head[8:12]
        # M4A/M4B (Apple audio, audiobook) are the audio-only brands in
        # practice; M4V, mp4x, isom, qt and friends all carry video tracks.
        return "audio" if brand[:3] in (b"M4A", b"M4B") else "video"

    # WebP and WAV both start with RIFF; differentiate via the payload tag
    # at offset 8.
    if head.startswith(b"RIFF") and len(head) >= 12:
        tag = head[8:12]
        if tag == b"WEBP":
            return "image"
        if tag == b"WAVE":
            return "audio"
        return None

    for sig, family in _ASSET_MAGIC_PREFIXES:
        if family == "image_or_audio":
            continue  # handled above
        if head.startswith(sig):
            return family
    return None


# Extensions a stored asset may carry, per detected content family. The
# extension decides the Content-Type the file is later served with (StaticFiles
# maps it through ``mimetypes``), so it must name the same kind of thing the
# bytes are: a real PNG saved as ``notes.html`` would otherwise be served as
# text/html from the app's own origin — stored XSS through any link to it, whatever
# the magic bytes at the front say. Images may carry any image extension
# (browsers decode <img> by content), but never one outside the family.
_ASSET_FAMILY_EXTS: dict[str, tuple[str, ...]] = {
    "image": (".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp"),
    "pdf": (".pdf",),
    "audio": (".mp3", ".ogg", ".oga", ".flac", ".wav", ".m4a", ".m4b"),
    "video": (".mp4", ".m4v", ".mov"),
}

# Pillow decoders an image asset may be handed to — the formats the magic
# check admits. Pinning them keeps Pillow from sniffing the upload against
# every other plugin it ships.
_ASSET_PIL_FORMATS: tuple[str, ...] = ("PNG", "JPEG", "GIF", "WEBP", "BMP")

# Canonical extension per format Pillow reports. MPO is not requested above
# but comes back for multi-picture JPEGs (common from cameras and phones):
# the JPEG plugin hands those to the MPO class, so it is a JPEG as far as the
# served type is concerned.
_ASSET_PIL_FORMAT_EXT: dict[str, str] = {
    "PNG": ".png",
    "JPEG": ".jpg",
    "MPO": ".jpg",
    "GIF": ".gif",
    "WEBP": ".webp",
    "BMP": ".bmp",
}


def _canonical_non_image_ext(head: bytes, family: str) -> str:
    if family == "pdf":
        return ".pdf"
    if family == "video":
        return ".mp4"
    # audio
    if head.startswith(b"RIFF"):
        return ".wav"
    if head.startswith(b"OggS"):
        return ".ogg"
    if head.startswith(b"fLaC"):
        return ".flac"
    if len(head) >= 8 and head[4:8] == b"ftyp":
        return ".m4a"
    return ".mp3"


def asset_extension_matches(filename: str, family: str) -> bool:
    """True when ``filename``'s extension belongs to the detected ``family``."""
    ext = os.path.splitext(filename)[1].lower()
    return ext in _ASSET_FAMILY_EXTS.get(family, ())


def _validate_asset_content(
    path: str, claimed_content_type: Optional[str]
) -> tuple[str, str]:
    """Verify the on-disk content actually matches an allowed asset family.

    Returns ``(family, canonical_ext)`` for the detected content — the caller
    uses them to make sure the stored filename's extension agrees with the
    bytes (see _ASSET_FAMILY_EXTS). Raises HTTPException(415) on rejection.
    See the module-level comment above _is_allowed_asset_mime for the role of
    this function in the overall validation chain.

    Note: this runs *after* the upload finishes, so a file at the size cap
    that turns out to be a Word doc still consumed the full cap on disk
    before being rejected. The size cap (TOUR_ASSET_MAX_BYTES, defaulting to
    2.5 MiB per config.py) bounds the worst-case wasted I/O.
    """
    try:
        with open(path, "rb") as fh:
            head = fh.read(64)
    except OSError:
        raise HTTPException(status_code=415, detail="Could not read uploaded file")

    # Reject markup payloads regardless of claimed type (case-insensitive
    # because XML/HTML start tokens are case-tolerant). This catches a forged
    # Content-Type=image/png header that hides an <svg> body.
    head_lower = head.lstrip().lower()
    for forbidden in _FORBIDDEN_TEXT_PREFIXES:
        if head_lower.startswith(forbidden):
            raise HTTPException(status_code=415, detail="Markup or script payloads are not allowed")

    family = _detect_asset_family(head)
    if family is None:
        raise HTTPException(status_code=415, detail="Unrecognised file content")

    ct = (claimed_content_type or "").split(";", 1)[0].strip().lower()
    if ct.startswith("image/") and family != "image":
        raise HTTPException(status_code=415, detail="File content is not an image")
    if ct.startswith("video/") and family != "video":
        raise HTTPException(status_code=415, detail="File content is not a video")
    if ct.startswith("audio/") and family != "audio":
        raise HTTPException(status_code=415, detail="File content is not audio")
    if ct == "application/pdf" and family != "pdf":
        raise HTTPException(status_code=415, detail="File content is not a PDF")

    if family != "image":
        return family, _canonical_non_image_ext(head, family)

    # Pillow's verify() catches truncated or malformed image streams, and
    # the format it identifies decides the canonical extension.
    from PIL import Image, UnidentifiedImageError

    try:
        with Image.open(path, formats=list(_ASSET_PIL_FORMATS)) as im:
            fmt = im.format
            im.verify()
    except (UnidentifiedImageError, OSError, ValueError, SyntaxError, Image.DecompressionBombError):
        # DecompressionBombError derives from Exception directly: a tiny file
        # declaring a huge canvas must read as invalid content (415), not 500,
        # and must not abort a whole slide import.
        raise HTTPException(status_code=415, detail="Image content is invalid")
    ext = _ASSET_PIL_FORMAT_EXT.get(fmt or "")
    if ext is None:
        # Only reachable if Pillow grows another class behind these plugins;
        # refuse rather than guess an extension the content might not fit.
        raise HTTPException(status_code=415, detail="Image content is invalid")
    return family, ext


def _open_new_asset(path: str, flags: int) -> int:
    """``os.open`` for a new asset file. safe_filename caps the name well
    under the usual 255-byte limit, but a filesystem with a shorter one can
    still refuse it: that is the uploaded name's fault, so it answers 400
    rather than a 500."""
    try:
        return os.open(path, flags, 0o644)
    except OSError as exc:
        if exc.errno == errno.ENAMETOOLONG:
            raise HTTPException(status_code=400, detail="Asset filename is too long") from exc
        raise


@router.post("/api/admin/tours/{tour_id}/assets/upload")
def admin_upload_tour_asset(
    tour_id: int,
    duplicate_policy: str = Form("rename"),
    file: UploadFile = File(...),
    admin: User = Depends(require_staff),
    db: Session = Depends(get_db),
):
    t = db.get(Tour, tour_id)
    if not t:
        raise HTTPException(status_code=404, detail="Tour not found")
    if duplicate_policy not in {"rename", "overwrite"}:
        raise HTTPException(status_code=400, detail="duplicate_policy must be rename or overwrite")

    if not _is_allowed_asset_mime(file.content_type):
        raise HTTPException(
            status_code=415,
            detail=f"Unsupported media type: {file.content_type or 'unknown'}",
        )

    # A tour with a NULL/empty public_id (a restored or hand-edited DB) would
    # resolve ``tour_asset_dir("")`` to the shared ``assets/tours/`` root, so
    # the upload would scatter files into that root, return a broken
    # ``/assets/tours//name`` URL, and never be reaped by tour delete (which
    # skips empty public_ids). The listing endpoint already guards on
    # ``t.public_id``; reject here to match, rather than writing into the
    # shared root. A *malformed* (not just empty) token — only reachable from
    # a restored/hand-edited DB — is refused by tour_asset_dir itself; turn
    # that into the same 409 rather than a 500.
    if not valid_public_id(t.public_id):
        raise HTTPException(
            status_code=409,
            detail="Tour has no public id yet; save the tour before uploading assets.",
        )

    filename = safe_filename(file.filename or "asset")
    base = tour_asset_dir(t.public_id)
    # Quota check through publish is one critical section per tour (see
    # tour_asset_lock): checked separately, two concurrent uploads would each see
    # the same headroom and together exceed the file-count / byte caps.
    with tour_asset_lock(tour_id):
        # The existence check above ran before the lock: a delete that finished
        # in between has already removed the row and the asset directory, and
        # creating the directory again would leave orphaned files served
        # publicly for a tour with no row. Re-check inside the lock (a fresh
        # query: db.get would answer from the session's identity map).
        if db.query(Tour.id).filter(Tour.id == tour_id).first() is None:
            raise HTTPException(status_code=404, detail="Tour not found")
        os.makedirs(base, exist_ok=True)
        quota_headroom = _enforce_tour_asset_quota(
            base,
            overwriting=(duplicate_policy != "rename"),
            replacing_name=filename,
        )
        # The body is already spooled, so its size is known; what is left to
        # write is the one copy into the asset directory.
        _enforce_tour_asset_free_space(
            base, file.size if file.size is not None else settings.tour_asset_max_bytes,
        )

        # Atomically reserve the write target so concurrent uploads can't race past
        # os.path.exists checks.
        #
        # "rename": append an unguessable random suffix and stream straight to that
        # brand-new O_EXCL name (also defence-in-depth against URL guessing on draft
        # tours, since `/assets/tours/{id}/...` is served statically with no
        # per-asset authz). Removing it on failure can't destroy anyone's data.
        #
        # "overwrite": the caller wants to keep the exact filename so existing
        # markdown refs keep resolving — but the live file must NOT be opened with
        # O_TRUNC, or a failed upload (413, client disconnect, invalid content)
        # would have already destroyed the previous asset, and two concurrent
        # overwrites would interleave bytes into it. Instead stream to a temp
        # sibling and os.replace() it onto the final name only after validation
        # passes (the same pattern admin_slides uses for WSI replace).
        flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_EXCL
        publish_path: Optional[str] = None  # overwrite only: temp -> final swap
        if duplicate_policy == "rename":
            stem, ext = os.path.splitext(filename)
            # 8 random hex chars = 2^32 search space. O_EXCL retries handle the
            # vanishingly rare collision (same admin, same second, identical roll).
            MAX_COLLISION_RETRIES = 8
            for _ in range(MAX_COLLISION_RETRIES):
                suffix = secrets.token_hex(4)
                final_name = f"{stem}-{suffix}{ext}"
                write_path = os.path.join(base, final_name)
                try:
                    fd = _open_new_asset(write_path, flags)
                    break
                except FileExistsError:
                    continue
            else:
                raise HTTPException(
                    status_code=409,
                    detail="Could not allocate a unique asset filename; please retry.",
                )
        else:  # overwrite
            final_name = filename
            publish_path = os.path.join(base, final_name)
            write_path = os.path.join(base, f".{final_name}.upload-{secrets.token_hex(8)}")
            fd = _open_new_asset(write_path, flags)

        max_bytes = settings.tour_asset_max_bytes
        written = 0

        def _drop_partial() -> None:
            # Delete the partially-written *temp / new* file so disk usage doesn't
            # accumulate from cancelled / failed uploads. Never touches an existing
            # asset: for overwrite this is the temp sibling, for rename the
            # just-reserved new name.
            try:
                os.remove(write_path)
            except OSError:
                pass

        # os.fdopen takes ownership of fd and will close it on context exit; on
        # failure to wrap (e.g. MemoryError) the raw fd must be closed explicitly.
        try:
            f = os.fdopen(fd, "wb")
        except BaseException:
            os.close(fd)
            _drop_partial()
            raise

        try:
            with f:
                while True:
                    chunk = file.file.read(1024 * 1024)
                    if not chunk:
                        break
                    written += len(chunk)
                    if written > max_bytes:
                        raise HTTPException(
                            status_code=413,
                            detail=f"Asset exceeds {max_bytes} byte limit",
                        )
                    if quota_headroom is not None and written > quota_headroom:
                        # Stop at the tour's remaining byte budget rather than at
                        # the per-file cap, so the total quota is an actual bound.
                        # 409 (not 413) to match the pre-check above: the file is
                        # not individually too large, the tour has no room for it.
                        raise HTTPException(
                            status_code=409,
                            detail=(
                                f"This upload would exceed the tour's asset storage "
                                f"limit of "
                                f"{settings.tour_asset_max_total_bytes // (1024 * 1024)} "
                                f"MiB. Delete unused assets before uploading more."
                            ),
                        )
                    f.write(chunk)
        except BaseException:
            _drop_partial()
            raise

        try:
            family, canonical_ext = _validate_asset_content(write_path, file.content_type)
        except BaseException:
            _drop_partial()
            raise

        if not asset_extension_matches(final_name, family):
            # The extension decides the served Content-Type, so it must agree with
            # the content (see _ASSET_FAMILY_EXTS). "overwrite" exists to keep an
            # exact name that markdown already references, so there is nothing
            # sensible to rename to: refuse. "rename" already mints a fresh name,
            # so just give it the extension the content calls for.
            if publish_path is not None:
                _drop_partial()
                raise HTTPException(
                    status_code=415,
                    detail=(
                        f"File extension does not match its content; "
                        f"expected {canonical_ext}"
                    ),
                )
            fixed_name = os.path.splitext(final_name)[0] + canonical_ext
            fixed_path = os.path.join(base, fixed_name)
            try:
                # Reserve the new name exclusively, then move the upload onto it.
                os.close(_open_new_asset(fixed_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL))
            except FileExistsError:
                _drop_partial()
                raise HTTPException(
                    status_code=409,
                    detail="Could not allocate a unique asset filename; please retry.",
                )
            except HTTPException:
                _drop_partial()
                raise
            try:
                os.replace(write_path, fixed_path)
            except OSError:
                _drop_partial()
                try:
                    os.remove(fixed_path)
                except OSError:
                    pass
                raise HTTPException(status_code=500, detail="Failed to store asset")
            final_name, write_path = fixed_name, fixed_path

        # Overwrite: atomically swap the validated temp file onto the final name.
        # Only now is the previous asset replaced — and never on a failed upload.
        if publish_path is not None:
            try:
                os.replace(write_path, publish_path)
            except OSError:
                _drop_partial()
                raise HTTPException(status_code=500, detail="Failed to store asset")

    audit(
        admin,
        "tour.asset.upload",
        target=f"{tour_id}/{final_name}",
        bytes=written,
        duplicate_policy=duplicate_policy,
    )
    asset_url = tour_asset_url(t.public_id, final_name)
    return {
        "ok": True,
        "name": final_name,
        "url": asset_url,
        "md": f"![{final_name}]({asset_url})",
        "duplicate_policy": duplicate_policy,
    }


@router.delete("/api/admin/tours/{tour_id}/assets/{name}")
def admin_delete_tour_asset(
    tour_id: int,
    name: str,
    admin: User = Depends(require_staff),
    db: Session = Depends(get_db),
):
    t = db.get(Tour, tour_id)
    if not t:
        raise HTTPException(status_code=404, detail="Tour not found")
    # A tour with a NULL/empty public_id (a restored or hand-edited DB) would
    # resolve ``tour_asset_dir("")`` to the shared ``assets/tours/`` root,
    # making this endpoint able to delete files sitting directly in that root.
    # The upload and listing endpoints already refuse an empty public_id; match
    # them instead of operating on the shared root. Same for a malformed token,
    # which tour_asset_dir refuses outright.
    if not valid_public_id(t.public_id):
        raise HTTPException(
            status_code=409,
            detail="Tour has no public id yet; save the tour before managing assets.",
        )
    # The exact stored name, not safe_filename(name): rename mode stores names
    # past safe_filename's length cap, which it would rewrite into a different
    # name, leaving the asset impossible to delete. Held instead to what an
    # upload (or the slide import) can store — no separators, no "." / "..",
    # no dotfiles (in-flight upload temps) — so the join can't escape `base`.
    if not storable_asset_name(name):
        raise HTTPException(status_code=400, detail="Invalid asset name")
    base = tour_asset_dir(t.public_id)
    base_real = os.path.realpath(base)
    # Defence in depth: keep an explicit realpath comparison as a second line
    # of defence in case a future change weakens that guarantee, or a symlink
    # inside the assets tree points outside it.
    path = os.path.join(base, name)
    real_path = os.path.realpath(path)
    if not real_path.startswith(base_real + os.sep):
        raise HTTPException(status_code=400, detail="Invalid asset name")
    if not os.path.isfile(real_path):
        raise HTTPException(status_code=404, detail="Asset not found")
    try:
        os.remove(real_path)
    except FileNotFoundError:
        # Lost a race with a concurrent delete between the isfile() check and
        # here — the file is already gone, which is the outcome the caller
        # wanted, so treat it as success rather than a 500.
        pass
    except OSError as exc:
        # Permission / filesystem error (read-only mount, NFS hiccup): log and
        # return a clean 500 instead of leaking a traceback through the handler.
        logger.warning("Could not remove tour asset %s: %s", real_path, exc)
        raise HTTPException(status_code=500, detail="Could not delete asset")
    audit(admin, "tour.asset.delete", target=f"{tour_id}/{name}")
    return {"ok": True}


_STEP_COORD_MAX = 1.0e9


def _validate_step_coords(**values: float) -> None:
    for name, v in values.items():
        if v is None:
            continue
        if not math.isfinite(v):
            raise HTTPException(status_code=422, detail=f"{name} must be a finite number")
        if abs(v) > _STEP_COORD_MAX:
            raise HTTPException(status_code=422, detail=f"{name} out of range")


def _parse_json_list(raw: str, *, field: str) -> list:
    """Decode a Form-encoded JSON list, surfacing client bugs as 422.

    Both pins and shapes_l0 are carried as JSON strings in the multipart
    body for admin_add_step, since Form fields can't natively express
    nested object arrays. The same checks apply to both, so factor them
    out instead of repeating the parse/isinstance pair twice.
    """
    try:
        decoded = json.loads(raw or "[]")
    except json.JSONDecodeError as exc:
        raise HTTPException(
            status_code=422, detail=f"{field} must be valid JSON: {exc.msg}"
        )
    if not isinstance(decoded, list):
        raise HTTPException(status_code=422, detail=f"{field} must be a JSON array")
    return decoded


def _pin_row(step_id: int, p: PinItem, *, pin_id: Optional[str] = None) -> StepPin:
    """The step_pins row for a validated PinItem — the one serialisation
    shared by step create, PATCH and append, so the three can't drift."""
    return StepPin(
        pin_id=p.id if pin_id is None else pin_id, step_id=step_id,
        x=p.x, y=p.y,
        shape=p.shape or "circle",
        color=p.color or "red",
        rotation_deg=float(p.rotation_deg or 0.0),
        label=p.label or "",
        z=p.z,
    )


def _shape_row(step_id: int, sh: Shape, *, shape_id: Optional[str] = None) -> StepShape:
    """The step_shapes row for a validated Shape (see _pin_row). Shape's
    validators already pin ``kind`` / ``label_position`` to the accepted sets."""
    # Only freehand needs the points payload; for rect/circle the column is
    # left empty so the row stays compact.
    if sh.kind == "freehand" and sh.points:
        points_json = json.dumps([{"x": p.x, "y": p.y} for p in sh.points])
    else:
        points_json = ""
    return StepShape(
        shape_id=sh.id if shape_id is None else shape_id, step_id=step_id,
        kind=sh.kind,
        x=sh.x, y=sh.y, w=sh.w, h=sh.h,
        stroke_color=sh.stroke_color or "red",
        fill_enabled=bool(sh.fill_enabled),
        fill_opacity=float(sh.fill_opacity),
        points_json=points_json,
        smoothing=bool(sh.smoothing),
        label=sh.label or "",
        label_position=sh.label_position,
        z=sh.z,
    )


def _check_annotation_lists(pins, shapes) -> None:
    """Duplicate-id, per-step count and freehand-point budget checks shared
    by create and PATCH (the PATCH body's max_length enforces the count caps
    at parse time; this covers the form-encoded create, which can't use it).
    ``None`` = omitted."""
    if pins is not None:
        if len(pins) > _MAX_STEP_PINS:
            raise HTTPException(
                status_code=422, detail=f"pins must have at most {_MAX_STEP_PINS} entries"
            )
        pin_ids = [p.id for p in pins]
        if len(pin_ids) != len(set(pin_ids)):
            raise HTTPException(status_code=422, detail="pins contains duplicate id values")
    if shapes is not None:
        if len(shapes) > _MAX_STEP_SHAPES:
            raise HTTPException(
                status_code=422,
                detail=f"shapes_l0 must have at most {_MAX_STEP_SHAPES} entries",
            )
        shape_ids = [sh.id for sh in shapes]
        if len(shape_ids) != len(set(shape_ids)):
            raise HTTPException(status_code=422, detail="shapes_l0 contains duplicate id values")
        if freehand_point_count(shapes) > _MAX_STEP_FREEHAND_POINTS:
            raise api_error(422, POINT_BUDGET_CODE, max=_MAX_STEP_FREEHAND_POINTS)


# Freehand vertices in a stored points_json: one "{" per {"x", "y"} object
# (rect / circle rows store ""). Counted in SQL, so a write doesn't decode a
# whole tour's vertices while it holds the write lock; a malformed
# (hand-edited) row can only be over-counted.
_STORED_POINT_COUNT = func.length(StepShape.points_json) - func.length(
    func.replace(StepShape.points_json, "{", "")
)


def _stored_point_counts(
    db: Session, tour_id: int, step_id: Optional[int] = None
) -> tuple[int, int]:
    """``(tour total, step_id's share)`` of the freehand vertices stored in
    ``tour_id``'s steps. Read under the write lock (after the revision bump),
    so no other write can change them before the caller commits."""
    in_step = case((StepShape.step_id == step_id, _STORED_POINT_COUNT), else_=0)
    tour_points, step_points = db.execute(
        select(
            func.coalesce(func.sum(_STORED_POINT_COUNT), 0),
            func.coalesce(func.sum(in_step), 0),
        )
        .select_from(StepShape)
        .join(Step, Step.id == StepShape.step_id)
        .where(Step.tour_id == tour_id)
    ).one()
    return int(tour_points), int(step_points)


def _tour_point_budget_error(db: Session) -> HTTPException:
    db.rollback()
    return api_error(409, TOUR_POINT_BUDGET_CODE, max=_MAX_TOUR_FREEHAND_POINTS)


def _step_in_tour_or_404(db: Session, tour_id: int, step_id: int) -> Step:
    # Scoped to the tour the editor believes owns the step: a request carrying
    # only a step id can't tell "the step I was looking at" from another one
    # holding that id. (Ids are not recycled — Step uses AUTOINCREMENT —
    # so a stale editor gets a 404 either way; the tour check keeps a write
    # from ever reaching across tours should that guarantee lapse.)
    s = db.get(Step, step_id)
    if not s or s.tour_id != tour_id:
        raise HTTPException(status_code=404, detail="Step not found")
    return s


# admin_add_step's form limits. Its pins / shapes_l0 fields carry the step's
# whole annotation lists as JSON, and Starlette's default per-field cap (1 MiB)
# is below what a step within its budgets takes: _MAX_STEP_FREEHAND_POINTS
# vertices at full double precision alone run to ~2.5 MB. A step the editor
# saved (PATCH, a JSON body capped only by nginx) would then not be duplicable.
# The field cap is sized to the budgets instead, and the form may carry no
# more fields than the route takes and no file, which keeps the whole form
# far smaller than Starlette's default 1000 fields of 1 MiB each.
_STEP_FORM_MAX_PART = 4 * 1024 * 1024
_STEP_FORM_MAX_FIELDS = 16


class _StepFormRoute(APIRoute):
    """Parses the request's form with the _STEP_FORM_* limits before FastAPI
    reads it for the endpoint's Form() parameters. A request parses its form
    once and keeps it, so FastAPI's own read (with Starlette's defaults)
    returns this one."""

    def get_route_handler(self):
        handler = super().get_route_handler()

        async def parse_then_handle(request: Request) -> Response:
            await request.form(
                max_files=0,
                max_fields=_STEP_FORM_MAX_FIELDS,
                max_part_size=_STEP_FORM_MAX_PART,
            )
            return await handler(request)

        return parse_then_handle


def admin_add_step(
    tour_id: int,
    order: int = Form(...),
    label: str = Form(""),
    center_x: float = Form(...),
    center_y: float = Form(...),
    fov_um_w: float = Form(...),
    fov_um_h: float = Form(...),
    rotation_deg: float = Form(0.0),
    pins: str = Form("[]"),
    shapes_l0: str = Form("[]"),
    description_md: str = Form(""),
    transition_mode: str = Form(DEFAULT_TRANSITION_MODE),
    # The tour's revision: adding a step changes the tour's structure.
    expected_revision: Optional[int] = Form(None, ge=0),
    admin: User = Depends(require_staff),
    db: Session = Depends(get_db),
):
    if not db.get(Tour, tour_id):
        raise HTTPException(status_code=404, detail="Tour not found")
    label = _validate_step_label(label)
    description_md = _bounded_text(
        description_md, field="description_md", max_len=_MAX_STEP_DESCRIPTION_LEN
    )
    _validate_step_coords(
        center_x=center_x, center_y=center_y,
        fov_um_w=fov_um_w, fov_um_h=fov_um_h,
        rotation_deg=rotation_deg,
    )
    if fov_um_w <= 0 or fov_um_h <= 0:
        raise HTTPException(status_code=422, detail="fov_um_w and fov_um_h must be > 0")
    if transition_mode not in TRANSITION_MODES:
        raise HTTPException(
            status_code=422,
            detail=f"transition_mode must be one of {TRANSITION_MODES}",
        )
    # Validate `order` before the DB write so a malformed payload surfaces
    # as 422 instead of a UniqueConstraint IntegrityError mapped to 500.
    # The editor always passes order = max(existing.order) + 1, so anything
    # outside that contract (negative, zero, or colliding with an existing
    # step, checked below) is a client bug that should fail loudly.
    if order < 1:
        raise HTTPException(status_code=422, detail="order must be >= 1")
    # Parse and validate pins AND shapes BEFORE creating the Step row, so a
    # malformed payload doesn't leave a half-saved step behind and a
    # JSONDecodeError surfaces as an error instead of silently turning into
    # an empty pin list that hides client-side bugs from the user.
    raw_pins = _parse_json_list(pins, field="pins")
    # Run the same Pydantic validation that StepUpdateIn applies on update so
    # NaN / Inf coordinates and missing required fields are rejected here too,
    # rather than being coerced into nonsense via float(p.get("x") or 0.0).
    try:
        parsed_pins = _PIN_LIST_ADAPTER.validate_python(raw_pins)
    except ValidationError as exc:
        raise HTTPException(status_code=422, detail=f"pins payload is invalid: {exc.errors()}")
    raw_shapes = _parse_json_list(shapes_l0, field="shapes_l0")
    try:
        parsed_shapes = _SHAPE_LIST_ADAPTER.validate_python(raw_shapes)
    except ValidationError as exc:
        raise HTTPException(
            status_code=422, detail=f"shapes_l0 payload is invalid: {exc.errors()}"
        )
    _check_annotation_lists(parsed_pins, parsed_shapes)

    new_rev = _bump_revision(db, Tour, tour_id, expected_revision)
    if new_rev is None:
        raise _tour_conflict(db, tour_id)
    # Under the write lock from here (see _bump_revision), so these checks
    # can't be raced by another add. The IntegrityError handler below stays as
    # the backstop for a write that somehow bypasses this path.
    existing_orders = set(
        db.execute(select(Step.order).where(Step.tour_id == tour_id)).scalars().all()
    )
    if len(existing_orders) >= _MAX_TOUR_STEPS:
        db.rollback()
        raise api_error(409, STEP_LIMIT_CODE, max=_MAX_TOUR_STEPS)
    new_points = freehand_point_count(parsed_shapes)
    if new_points and (
        _stored_point_counts(db, tour_id)[0] + new_points > _MAX_TOUR_FREEHAND_POINTS
    ):
        raise _tour_point_budget_error(db)
    if order in existing_orders:
        db.rollback()
        raise HTTPException(
            status_code=409,
            detail="order conflicts with an existing step; refresh and retry",
        )

    s = Step(
        tour_id=tour_id, order=order, label=label,
        center_x=center_x, center_y=center_y,
        fov_um_w=fov_um_w, fov_um_h=fov_um_h,
        rotation_deg=rotation_deg,
        description_md=description_md,
        transition_mode=transition_mode,
    )
    db.add(s)
    try:
        db.flush()
        for p in parsed_pins:
            db.add(_pin_row(s.id, p))
        for sh in parsed_shapes:
            db.add(_shape_row(s.id, sh))
        db.commit()
    except IntegrityError:
        # A UNIQUE(tour_id, order) violation the check above didn't see.
        # Surface the same 409 the deterministic collision path returns
        # instead of a 500 (mirrors admin_create_tour's retry/409).
        db.rollback()
        raise HTTPException(
            status_code=409,
            detail="order conflicts with an existing step; refresh and retry",
        )
    audit(
        admin,
        "tour.step.add",
        target=f"{tour_id}/{s.id}",
        order=order,
        pins=len(parsed_pins),
        shapes=len(parsed_shapes),
    )
    # ``revision`` is the tour's (the one expected_revision referred to); the
    # new step itself starts at revision 0.
    return {"id": s.id, "revision": new_rev}


router.add_api_route(
    "/api/admin/tours/{tour_id}/steps",
    admin_add_step,
    methods=["POST"],
    route_class_override=_StepFormRoute,
)


@router.patch("/api/admin/tours/{tour_id}/steps/reorder")
def admin_reorder_steps(
    tour_id: int,
    body: StepReorderIn,
    admin: User = Depends(require_staff),
    db: Session = Depends(get_db),
):
    t = db.get(Tour, tour_id)
    if not t:
        raise HTTPException(status_code=404, detail="Tour not found")
    requested_ids = body.step_ids or []
    if len(set(requested_ids)) != len(requested_ids):
        raise HTTPException(status_code=400, detail="step_ids must include each step exactly once")

    new_rev = _bump_revision(db, Tour, tour_id, body.expected_revision)
    if new_rev is None:
        raise _tour_conflict(db, tour_id)
    # The step set is read under the write lock the bump took, so no add or
    # delete can land between this validation and the commit — so it needs no
    # post-flush re-read and no "steps changed during reorder" 409.
    steps = db.execute(select(Step).where(Step.tour_id == tour_id)).scalars().all()
    if not steps:
        # Nothing to reorder: not a structure change, so don't spend a revision.
        db.rollback()
        return {"ok": True, "count": 0, "revision": t.revision}

    current_ids = {s.id for s in steps}
    if len(requested_ids) != len(steps):
        db.rollback()
        raise HTTPException(status_code=400, detail="step_ids must include each step exactly once")
    if set(requested_ids) != current_ids:
        db.rollback()
        raise HTTPException(status_code=400, detail="step_ids do not match this tour")

    # Two-phase write avoids unique (tour_id, order) conflicts during updates.
    by_id = {s.id: s for s in steps}
    for idx, sid in enumerate(requested_ids, start=1):
        by_id[sid].order = -idx
    db.flush()
    for idx, sid in enumerate(requested_ids, start=1):
        by_id[sid].order = idx
    db.commit()
    audit(
        admin,
        "tour.step.reorder",
        target=str(tour_id),
        count=len(requested_ids),
    )
    return {"ok": True, "count": len(requested_ids), "revision": new_rev}


@router.delete("/api/admin/tours/{tour_id}/steps/{step_id}")
def admin_delete_step(
    tour_id: int,
    step_id: int,
    # The tour's revision (a query parameter: DELETE carries no body). The
    # delete changes the tour's structure and renumbers the remaining steps.
    expected_revision: Optional[int] = Query(None, ge=0),
    admin: User = Depends(require_staff),
    db: Session = Depends(get_db),
):
    s = _step_in_tour_or_404(db, tour_id, step_id)
    new_rev = _bump_revision(db, Tour, tour_id, expected_revision)
    if new_rev is None:
        raise _tour_conflict(db, tour_id)
    # Re-check under the lock: an unconditional concurrent delete may have
    # removed the step after the read above, and deleting it again would trip
    # the ORM's rowcount check (a 500) rather than the 404 it deserves.
    if db.execute(select(Step.id).where(Step.id == step_id)).first() is None:
        db.rollback()
        raise HTTPException(status_code=404, detail="Step not found")
    db.delete(s)
    db.flush()

    # Close the gap left by the deleted step so user-visible step numbers
    # stay contiguous. Uses the same two-phase write that admin_reorder_steps
    # relies on to avoid transient UniqueConstraint(tour_id, order) clashes.
    remaining = db.execute(
        select(Step).where(Step.tour_id == tour_id).order_by(Step.order.asc(), Step.id.asc())
    ).scalars().all()
    if remaining:
        for idx, st in enumerate(remaining, start=1):
            st.order = -idx
        db.flush()
        for idx, st in enumerate(remaining, start=1):
            st.order = idx
    db.commit()
    audit(admin, "tour.step.delete", target=f"{tour_id}/{step_id}")
    return {"ok": True, "revision": new_rev}


@router.patch("/api/admin/tours/{tour_id}/steps/{step_id}")
def admin_update_step(
    tour_id: int,
    step_id: int,
    body: StepUpdateIn,
    admin: User = Depends(require_staff),
    db: Session = Depends(get_db),
):
    t = db.get(Tour, tour_id)
    if not t:
        raise HTTPException(status_code=404, detail="Tour not found")
    s = _step_in_tour_or_404(db, tour_id, step_id)
    _check_annotation_lists(body.pins, body.shapes_l0)
    # The step editor requires a label. Checked here, not on StepUpdateIn
    # (which bundle restore reuses): create, duplicate and restore still allow
    # an empty label, and a step stored without one stays writable by anything
    # that omits it, but has to gain one the next time the editor saves it.
    if body.label is not None and not body.label.strip():
        raise HTTPException(status_code=422, detail="label must not be empty")

    # The conditional bump is what makes this PATCH safe to keep as a
    # whole-field replace: a stale editor (another tab, another admin) gets a
    # 409 carrying the step as it is now, instead of reverting everything
    # saved since it loaded.
    new_rev = _bump_revision(db, Step, step_id, body.expected_revision)
    if new_rev is None:
        raise _step_conflict(db, tour_id, step_id)
    new_points = freehand_point_count(body.shapes_l0) if body.shapes_l0 is not None else 0
    if new_points:
        tour_points, step_points = _stored_point_counts(db, tour_id, step_id)
        # Only a save that adds vertices to the step can be refused, so a
        # tour stored over the budget (e.g. after the budget is lowered)
        # stays editable.
        if new_points > step_points and (
            tour_points - step_points + new_points > _MAX_TOUR_FREEHAND_POINTS
        ):
            raise _tour_point_budget_error(db)

    # PATCH semantics: only touch columns the client included in the body.
    # Fields are Optional on StepUpdateIn, so a None value means "omitted"
    # and the existing DB value stays put. This avoids a full-replace
    # where omitting a key would silently snap it back to
    # the schema default (e.g. blanking the label or animate flag).
    changed: list[str] = []
    if body.label is not None:
        s.label = body.label
        changed.append("label")
    if body.description_md is not None:
        s.description_md = body.description_md
        changed.append("description_md")
    if body.rotation_deg is not None:
        s.rotation_deg = body.rotation_deg
        changed.append("rotation_deg")
    if body.fov_um_w is not None:
        s.fov_um_w = body.fov_um_w
        changed.append("fov_um_w")
    if body.fov_um_h is not None:
        s.fov_um_h = body.fov_um_h
        changed.append("fov_um_h")
    if body.center_x is not None:
        s.center_x = body.center_x
        changed.append("center_x")
    if body.center_y is not None:
        s.center_y = body.center_y
        changed.append("center_y")
    if body.transition_mode is not None:
        s.transition_mode = body.transition_mode
        changed.append("transition_mode")

    if body.shapes_l0 is not None:
        db.execute(delete(StepShape).where(StepShape.step_id == s.id))
        for sh in body.shapes_l0:
            db.add(_shape_row(s.id, sh))
        changed.append("shapes_l0")

    if body.pins is not None:
        db.execute(delete(StepPin).where(StepPin.step_id == s.id))
        for p in body.pins:
            db.add(_pin_row(s.id, p))
        changed.append("pins")

    # The saved step, so the client can adopt the new revision and content
    # without refetching the tour. Serialised before the commit, under the
    # write lock (flush, then expire so the pin / shape collections reload
    # from the rows just written): re-reading after the commit could meet a
    # concurrent delete and turn a saved write into a 500.
    db.flush()
    db.expire(s)
    out = step_to_out(s)
    db.commit()
    audit(
        admin,
        "tour.step.update",
        target=f"{tour_id}/{step_id}",
        fields=changed,
        pins=len(body.pins) if body.pins is not None else None,
        shapes=len(body.shapes_l0) if body.shapes_l0 is not None else None,
    )
    return JSONResponse({"ok": True, "revision": new_rev, "step": out})


def _mint_annotation_id(prefix: str, taken: set) -> str:
    # Same "<prefix>-<12 hex>" shape the editor mints (frontend lib/uid), well
    # inside the String(64) pin_id / shape_id columns.
    while True:
        candidate = f"{prefix}-{secrets.token_hex(6)}"
        if candidate not in taken:
            return candidate


def _append_annotation(
    db: Session, admin: User, tour_id: int, step_id: int,
    *, pin: Optional[PinItem] = None, shape: Optional[Shape] = None,
) -> dict:
    """Append one pin or shape to a step inside the write transaction.

    Reading the target's lists and PATCHing them back whole, two copies (or
    a copy and any other save, from any tab) racing on one target would drop
    all but the last. Appends commute, so this needs no
    expected revision: it inserts one row under the write lock and leaves
    everything else on the step alone. It still bumps Step.revision, so an
    editor open on the target sees a 409 instead of saving over the new item.
    """
    s = _step_in_tour_or_404(db, tour_id, step_id)
    if pin is not None:
        model, id_col, cap, prefix, item = StepPin, StepPin.pin_id, _MAX_STEP_PINS, "pin", pin
    else:
        model, id_col, cap, prefix, item = StepShape, StepShape.shape_id, _MAX_STEP_SHAPES, "shape", shape

    new_rev = _bump_revision(db, Step, step_id, None)
    if new_rev is None:
        db.rollback()  # deleted between the read above and the bump
        raise HTTPException(status_code=404, detail="Step not found")
    # Read under the lock, so the cap and the id check see every committed
    # append, including one that raced this request.
    taken = list(db.execute(select(id_col).where(model.step_id == step_id)).scalars())
    if len(taken) >= cap:
        db.rollback()
        raise api_error(409, ANNOTATION_LIMIT_CODE, max=cap)
    if shape is not None and shape.points:
        tour_points, step_points = _stored_point_counts(db, tour_id, step_id)
        if step_points + len(shape.points) > _MAX_STEP_FREEHAND_POINTS:
            db.rollback()
            raise api_error(409, POINT_BUDGET_CODE, max=_MAX_STEP_FREEHAND_POINTS)
        if tour_points + len(shape.points) > _MAX_TOUR_FREEHAND_POINTS:
            raise _tour_point_budget_error(db)
    # Keep the caller's id unless it is empty or already used on this step
    # (UNIQUE(step_id, pin_id / shape_id)) — e.g. the same pin copied twice.
    taken_set = set(taken)
    item_id = item.id if item.id and item.id not in taken_set else _mint_annotation_id(prefix, taken_set)
    if pin is not None:
        db.add(_pin_row(step_id, pin, pin_id=item_id))
    else:
        db.add(_shape_row(step_id, shape, shape_id=item_id))
    # Serialised under the write lock, before the commit (see
    # admin_update_step): exactly the state this append produced.
    db.flush()
    db.expire(s)
    out = step_to_out(s)
    db.commit()
    audit(
        admin, "tour.step.annotation.append",
        target=f"{tour_id}/{step_id}", kind=prefix, id=item_id,
    )
    return JSONResponse({"ok": True, "id": item_id, "revision": new_rev, "step": out})


@router.post("/api/admin/tours/{tour_id}/steps/{step_id}/pins")
def admin_append_step_pin(
    tour_id: int,
    step_id: int,
    body: PinItem,
    admin: User = Depends(require_staff),
    db: Session = Depends(get_db),
):
    """Atomically append one pin (validated as in the PATCH). An empty or
    already-used ``id`` is replaced by a fresh one; the response carries the
    id used, the step's new revision and the updated step."""
    return _append_annotation(db, admin, tour_id, step_id, pin=body)


@router.post("/api/admin/tours/{tour_id}/steps/{step_id}/shapes")
def admin_append_step_shape(
    tour_id: int,
    step_id: int,
    body: Shape,
    admin: User = Depends(require_staff),
    db: Session = Depends(get_db),
):
    """Atomically append one shape; see admin_append_step_pin."""
    return _append_annotation(db, admin, tour_id, step_id, shape=body)
