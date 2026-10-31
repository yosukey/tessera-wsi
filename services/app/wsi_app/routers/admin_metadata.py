"""Admin metadata option management: master list of attribute keys and values."""
from __future__ import annotations

import hashlib
from typing import Dict, List, Optional

from fastapi import APIRouter, Depends, Form, Query
from fastapi.responses import JSONResponse
from sqlalchemy import and_, case, delete, func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ..admin_helpers import (
    ATTR_KEY_MAX_LEN,
    ATTR_VALUE_MAX_LEN,
    BASE_FILTER_KEYS,
    CTRL_CHAR_RE,
    api_error,
    audit,
)
from ..auth import require_manager_or_above, require_staff
from ..db import get_db
from ..models import MetadataOption, Slide, SlideAttribute, Tour, TourAttribute, User

router = APIRouter()


def _slide_set_fingerprint(slide_ids) -> str:
    """Order-independent digest of a set of slide ids.

    The usage probe hands it to the dashboard with the count it shows in the
    delete confirmation, and the delete refuses to run (409) when the slides
    it would strip no longer hash to it. A count alone can't do that: another
    admin swapping which slides carry the value leaves the count unchanged.
    """
    ids = sorted({int(i) for i in slide_ids})
    return hashlib.sha256(",".join(map(str, ids)).encode()).hexdigest()[:16]


def _usage_fingerprint(slide_ids, tour_ids=()) -> str:
    """_slide_set_fingerprint extended to the tours carrying the value too, so
    the delete's confirmation check also catches a tour gaining or losing it.
    With no tours it is exactly the slide fingerprint."""
    tours = sorted({int(i) for i in tour_ids})
    if not tours:
        return _slide_set_fingerprint(slide_ids)
    slides = sorted({int(i) for i in slide_ids})
    payload = ",".join(map(str, slides)) + "|t:" + ",".join(map(str, tours))
    return hashlib.sha256(payload.encode()).hexdigest()[:16]


def _bump_tour_revisions(db: Session, tour_ids) -> None:
    """A tour's attributes are covered by Tour.revision (see admin_tours), so
    a rename / delete that rewrites them bumps it: an editor still holding the
    old list then gets a 409 on save instead of writing the old value back."""
    ids = sorted({int(i) for i in tour_ids})
    if ids:
        db.execute(
            update(Tour).where(Tour.id.in_(ids)).values(revision=Tour.revision + 1),
            execution_options={"synchronize_session": False},
        )


def _tour_usage(db: Session, tour_ids) -> dict:
    """``tour_count`` / ``published_tour_count`` for the usage probes: the
    distinct tours among ``tour_ids`` (the tours whose own attributes carry
    the value), all and published."""
    ids = sorted({int(i) for i in tour_ids})
    published = 0
    if ids:
        # Published = what the public tour pages serve: the tour and its slide.
        published = db.scalar(
            select(func.count(Tour.id))
            .join(Slide, Slide.id == Tour.slide_id)
            .where(Tour.id.in_(ids), Tour.is_published.is_(True), Slide.is_published.is_(True))
        ) or 0
    return {"tour_count": len(ids), "published_tour_count": int(published)}


def _changed_since_confirmation(slides_now: int, tours_now: int = 0) -> JSONResponse:
    return JSONResponse(
        status_code=409,
        content={"ok": False, "changed": True, "slide_count": slides_now, "tour_count": tours_now},
    )


@router.get("/api/admin/metadata/options")
def admin_list_metadata_options(admin: User = Depends(require_staff), db: Session = Depends(get_db)):
    """Return the master list of metadata values, grouped by key.

    Open to every staff role: instructors edit tours, and the tour editor's
    metadata picker reads its choices from here (editing the list itself
    stays manager-or-above).

    Includes both:
      - Registered MetadataOption rows (the master list proper).
      - "Orphan" (key, value) pairs that exist only in slide_attributes or
        tour_attributes (e.g. surviving a delete that did not cascade, or imported via
        a backup).

    Merging the two surfaces orphans in the dashboard's Metadata tab so an
    admin can clean them up; without this they would be invisible there but
    still drive /api/facets and get silently re-saved by the slide editor.
    """
    options: Dict[str, List[str]] = {}
    seen: Dict[str, set] = {}

    for key, value in db.execute(
        select(MetadataOption.key, MetadataOption.value).order_by(
            MetadataOption.key.asc(), MetadataOption.value.asc()
        )
    ).all():
        options.setdefault(key, []).append(value)
        seen.setdefault(key, set()).add(value)

    for model in (SlideAttribute, TourAttribute):
        for key, value in db.execute(
            select(model.key, model.value).distinct()
        ).all():
            bucket = seen.setdefault(key, set())
            if value in bucket:
                continue
            bucket.add(value)
            options.setdefault(key, []).append(value)

    # Sort each value list deterministically; the union above can interleave
    # registered and orphan entries when both sources contribute.
    for key in options:
        options[key].sort()

    # Ensure base filter keys always appear (possibly empty) so the UI has a stable layout.
    for key in BASE_FILTER_KEYS:
        options.setdefault(key, [])
    return {"options": options}


def _validate_option_key(key: str) -> str:
    k = (key or "").strip()
    if not k:
        raise api_error(422, "adminDash.apiErr.metaKeyRequired")
    if len(k) > ATTR_KEY_MAX_LEN:
        raise api_error(422, "adminDash.apiErr.metaKeyMaxLen", max=ATTR_KEY_MAX_LEN)
    if CTRL_CHAR_RE.search(k):
        raise api_error(422, "adminDash.apiErr.metaKeyCtrlChar")
    if "," in k or ":" in k:
        raise api_error(422, "adminDash.apiErr.metaKeyInvalidChars")
    return k


def _validate_option_value(value: str) -> str:
    v = (value or "").strip()
    if not v:
        raise api_error(422, "adminDash.apiErr.metaValRequired")
    if len(v) > ATTR_VALUE_MAX_LEN:
        raise api_error(422, "adminDash.apiErr.metaValMaxLen", max=ATTR_VALUE_MAX_LEN)
    if CTRL_CHAR_RE.search(v):
        raise api_error(422, "adminDash.apiErr.metaValCtrlChar")
    return v


@router.post("/api/admin/metadata/options")
def admin_add_metadata_option(
    key: str = Form(...),
    value: str = Form(...),
    admin: User = Depends(require_manager_or_above),
    db: Session = Depends(get_db),
):
    k = _validate_option_key(key)
    v = _validate_option_value(value)
    existing = db.execute(
        select(MetadataOption).where(MetadataOption.key == k, MetadataOption.value == v)
    ).scalar_one_or_none()
    if existing:
        audit(admin, "metadata.option.add", target=f"{k}={v}", id=existing.id, duplicate=True)
        return JSONResponse(
            status_code=409,
            content={"ok": False, "id": existing.id, "duplicate": True},
        )
    opt = MetadataOption(key=k, value=v)
    db.add(opt)
    try:
        db.commit()
    except IntegrityError:
        # Another request slipped past the SELECT-then-INSERT window and
        # committed the same (key, value) first. The unique constraint
        # (uq_metadata_option) rejects the second insert; recover by
        # returning the same 409 shape as the deterministic duplicate path
        # so the dashboard surfaces "already exists" instead of a 500.
        db.rollback()
        existing = db.execute(
            select(MetadataOption).where(MetadataOption.key == k, MetadataOption.value == v)
        ).scalar_one_or_none()
        if existing is None:
            raise
        audit(admin, "metadata.option.add", target=f"{k}={v}", id=existing.id, duplicate=True, raced=True)
        return JSONResponse(
            status_code=409,
            content={"ok": False, "id": existing.id, "duplicate": True},
        )
    audit(admin, "metadata.option.add", target=f"{k}={v}", id=opt.id, duplicate=False)
    return {"ok": True, "id": opt.id, "duplicate": False}


@router.get("/api/admin/metadata/options/usage")
def admin_get_metadata_option_usage(
    key: str = Query(...),
    value: str = Query(...),
    admin: User = Depends(require_manager_or_above),
    db: Session = Depends(get_db),
):
    """Count slides — and tours, by their own attributes — that have the
    given metadata key+value set.

    Returns both the total (admin-visible) count and the published-only
    count so the dashboard's confirmation dialog can call out drafts
    explicitly — an admin who clears a value that looks unused on the
    public site might still pull it from a draft slide that's about to
    be published.
    """
    k = (key or "").strip()
    v = (value or "").strip()
    total = db.scalar(
        select(func.count(SlideAttribute.slide_id.distinct())).where(
            SlideAttribute.key == k, SlideAttribute.value == v
        )
    ) or 0
    published = db.scalar(
        select(func.count(SlideAttribute.slide_id.distinct()))
        .join(Slide, Slide.id == SlideAttribute.slide_id)
        .where(
            SlideAttribute.key == k,
            SlideAttribute.value == v,
            Slide.is_published.is_(True),
        )
    ) or 0
    tour_ids = db.execute(
        select(TourAttribute.tour_id).where(TourAttribute.key == k, TourAttribute.value == v)
    ).scalars().all()
    fingerprint = _usage_fingerprint(
        db.execute(
            select(SlideAttribute.slide_id).where(
                SlideAttribute.key == k, SlideAttribute.value == v
            )
        ).scalars(),
        tour_ids,
    )
    return {
        "key": k,
        "value": v,
        "slide_count": int(total),
        "published_slide_count": int(published),
        **_tour_usage(db, tour_ids),
        # Covers the tours as well, despite the name (part of the API).
        "slides_fingerprint": fingerprint,
    }


@router.delete("/api/admin/metadata/options")
def admin_delete_metadata_option(
    key: str = Query(...),
    value: str = Query(...),
    # The usage probe's slides_fingerprint for what the user confirmed;
    # omitted, the delete runs unconditionally (scripts, non-SPA callers).
    expected_fingerprint: Optional[str] = Query(None, max_length=64),
    admin: User = Depends(require_manager_or_above),
    db: Session = Depends(get_db),
):
    # Use query parameters rather than a form body: RFC 9110 leaves DELETE
    # request bodies "undefined", so reverse proxies (and some HTTP clients)
    # are free to strip them mid-flight, which would let the call reach the
    # backend with empty `key`/`value` and silently no-op.
    k = (key or "").strip()
    v = (value or "").strip()
    opt = db.execute(
        select(MetadataOption).where(MetadataOption.key == k, MetadataOption.value == v)
    ).scalar_one_or_none()
    # The dashboard's confirmation dialog warns "this value will be removed
    # from the N slide(s) that use it". Honour that contract by also deleting
    # matching slide_attributes rows — otherwise the slides keep the value as
    # an invisible orphan (still surfacing in /api/facets, still echoed back
    # on the next slide save) and the warning becomes a lie.
    # For an (option) delete the UNIQUE(slide_id, key, value) constraint makes
    # distinct-slide-count == rowcount, but the dashboard's TOCTOU guard
    # compares against a distinct-slide count from the usage probe — return
    # both fields so the comparison is unambiguous regardless of endpoint.
    #
    # The rows are removed with RETURNING, so the slides actually stripped are
    # known inside the same write transaction: if they are not the set the
    # user confirmed (expected_fingerprint), nothing is deleted.
    removed_ids = db.execute(
        delete(SlideAttribute)
        .where(SlideAttribute.key == k, SlideAttribute.value == v)
        .returning(SlideAttribute.slide_id)
    ).scalars().all()
    # Tours carry the same vocabulary (tour_attributes) and lose the value too.
    removed_tour_ids = db.execute(
        delete(TourAttribute)
        .where(TourAttribute.key == k, TourAttribute.value == v)
        .returning(TourAttribute.tour_id)
    ).scalars().all()
    slides_affected = len(set(removed_ids))
    attr_removed = len(removed_ids)
    tours_affected = len(set(removed_tour_ids))
    if (
        expected_fingerprint is not None
        and _usage_fingerprint(removed_ids, removed_tour_ids) != expected_fingerprint
    ):
        db.rollback()
        return _changed_since_confirmation(slides_affected, tours_affected)
    _bump_tour_revisions(db, removed_tour_ids)
    options_removed = 1 if opt is not None else 0
    if opt is not None:
        db.delete(opt)
    # Commit unconditionally: even a DELETE that matched nothing has opened a
    # write transaction, and audit() persists on another connection — left
    # open, that insert waits out busy_timeout and the audit row is lost.
    db.commit()
    audit(
        admin,
        "metadata.option.delete",
        target=f"{k}={v}",
        options_removed=options_removed,
        slide_attrs_removed=int(attr_removed),
        slides_affected=int(slides_affected),
        tour_attrs_removed=len(removed_tour_ids),
        tours_affected=tours_affected,
    )
    return {
        "ok": True,
        # Number of MetadataOption rows removed (0 or 1 here; matches the
        # category-delete endpoint's semantics so both DELETE responses use
        # the same shape).
        "options_removed": options_removed,
        "slide_attrs_removed": int(attr_removed),
        "slides_affected": int(slides_affected),
        "tour_attrs_removed": len(removed_tour_ids),
        "tours_affected": tours_affected,
    }


@router.patch("/api/admin/metadata/options")
def admin_rename_metadata_option(
    key: str = Form(...),
    old_value: str = Form(...),
    new_value: str = Form(...),
    admin: User = Depends(require_manager_or_above),
    db: Session = Depends(get_db),
):
    """Rename a metadata value in place, carrying existing slide and tour
    assignments.

    Renaming touches every table holding the value: the MetadataOption
    master row and every slide_attributes / tour_attributes row that carries
    the old (key, value). They are only
    string-linked (no FK), so a rename that updated one without the other
    would orphan the slides — the whole point of this endpoint over a
    delete+re-add is that slides keep their assignment.

    Reject policy (chosen over merge): if the target value already exists
    under this key — as a registered MetadataOption OR as an (orphan)
    attribute on any slide — refuse with the same 409 shape the add
    endpoint uses. The dashboard's value cloud unions both sources, so
    "already shown" == reject is what an admin sees; it also guarantees the
    UPDATE below cannot collide with uq_slide_attr_triplet.
    """
    # Validate the key with the same rules the add endpoint applies (length /
    # control chars / ',' ':'), so a malformed key that POST would 422 cannot
    # slip in here via the orphan-registration branch below.
    k = _validate_option_key(key)
    old_v = (old_value or "").strip()
    if not old_v:
        raise api_error(422, "adminDash.apiErr.metaValRequired")
    new_v = _validate_option_value(new_value)
    if old_v == new_v:
        return {
            "ok": True, "renamed": False, "slides_affected": 0, "slide_attrs_updated": 0,
            "tours_affected": 0, "tour_attrs_updated": 0,
        }

    new_opt = db.execute(
        select(MetadataOption).where(MetadataOption.key == k, MetadataOption.value == new_v)
    ).scalar_one_or_none()
    target_on_slides = any(
        (
            db.scalar(
                select(func.count()).select_from(model).where(
                    model.key == k, model.value == new_v
                )
            )
            or 0
        ) > 0
        for model in (SlideAttribute, TourAttribute)
    )
    if new_opt is not None or target_on_slides:
        return JSONResponse(
            status_code=409,
            content={"ok": False, "duplicate": True, "target_exists": True, "key": k, "value": new_v},
        )

    # Count distinct affected slides before mutating (mirrors the delete
    # endpoints' slides_affected contract used by the dashboard).
    slides_affected = db.scalar(
        select(func.count(SlideAttribute.slide_id.distinct())).where(
            SlideAttribute.key == k, SlideAttribute.value == old_v
        )
    ) or 0
    # Everything from the bulk UPDATE to the commit is one conflict window.
    # Another request can add (k, new_v) — to a slide that still carries
    # old_v, or as a master option — after the existence check above, and
    # SQLite raises the UNIQUE violation on the statement that collides (this
    # UPDATE, or the option rename's flush), not only at commit. Any of them
    # means the same thing as the deterministic reject above: the target now
    # exists, so roll back and answer 409.
    try:
        updated = db.execute(
            update(SlideAttribute)
            .where(SlideAttribute.key == k, SlideAttribute.value == old_v)
            .values(value=new_v)
        ).rowcount or 0
        renamed_tour_ids = db.execute(
            select(TourAttribute.tour_id).where(
                TourAttribute.key == k, TourAttribute.value == old_v
            )
        ).scalars().all()
        tour_updated = db.execute(
            update(TourAttribute)
            .where(TourAttribute.key == k, TourAttribute.value == old_v)
            .values(value=new_v)
        ).rowcount or 0
        _bump_tour_revisions(db, renamed_tour_ids)

        # Reconcile the master list: rename the registered row in place, or
        # (for an orphan-only value that was never registered) register the
        # target so the renamed value appears as a proper option rather than
        # an orphan.
        old_opt = db.execute(
            select(MetadataOption).where(MetadataOption.key == k, MetadataOption.value == old_v)
        ).scalar_one_or_none()
        if old_opt is None and updated == 0 and tour_updated == 0:
            # The old value matched neither a registered option nor any slide
            # or tour attribute: there is nothing to carry forward. Refuse with a 404
            # rather than silently registering (k, new_v) as a brand-new
            # option — a rename must move an *existing* value; use the add
            # endpoint to create one. (Rolling back here discards the no-op
            # 0-row UPDATE.)
            db.rollback()
            return JSONResponse(
                status_code=404,
                content={"ok": False, "renamed": False, "not_found": True, "key": k, "value": old_v},
            )
        if old_opt is not None:
            old_opt.value = new_v
        else:
            db.add(MetadataOption(key=k, value=new_v))
        db.commit()
    except IntegrityError:
        db.rollback()
        return JSONResponse(
            status_code=409,
            content={
                "ok": False,
                "duplicate": True,
                "target_exists": True,
                "key": k,
                "value": new_v,
                "raced": True,
            },
        )
    audit(
        admin,
        "metadata.option.rename",
        target=f"{k}:{old_v}->{new_v}",
        slide_attrs_updated=int(updated),
        slides_affected=int(slides_affected),
        tour_attrs_updated=int(tour_updated),
        tours_affected=len(set(renamed_tour_ids)),
    )
    return {
        "ok": True,
        "renamed": True,
        "slides_affected": int(slides_affected),
        "slide_attrs_updated": int(updated),
        "tours_affected": len(set(renamed_tour_ids)),
        "tour_attrs_updated": int(tour_updated),
    }


@router.get("/api/admin/metadata/counts")
def admin_get_metadata_counts(
    admin: User = Depends(require_manager_or_above),
    db: Session = Depends(get_db),
):
    """Return slide usage counts for all metadata keys and (key, value) pairs in one round-trip.

    Response shape::

        {
          "keys": {"stain": {"total": 5, "published": 3}, ...},
          "values": {"stain": {"H&E": {"total": 4, "published": 2}, ...}, ...},
          "tour_keys": {...}, "tour_values": {...}   # same shapes, for tours
        }

    Key-level totals count distinct slides that carry *any* value for that key;
    they are therefore independent of (and not simply the sum of) the
    per-value counts.
    """
    published_case = case((Slide.is_published.is_(True), SlideAttribute.slide_id))

    # Per-(key, value) counts.
    value_rows = db.execute(
        select(
            SlideAttribute.key,
            SlideAttribute.value,
            func.count(SlideAttribute.slide_id.distinct()).label("total"),
            func.count(func.distinct(published_case)).label("published"),
        )
        .join(Slide, Slide.id == SlideAttribute.slide_id)
        .group_by(SlideAttribute.key, SlideAttribute.value)
    ).all()

    # Per-key counts (distinct slides with *any* value for that key).
    key_rows = db.execute(
        select(
            SlideAttribute.key,
            func.count(SlideAttribute.slide_id.distinct()).label("total"),
            func.count(func.distinct(published_case)).label("published"),
        )
        .join(Slide, Slide.id == SlideAttribute.slide_id)
        .group_by(SlideAttribute.key)
    ).all()

    keys: dict = {row.key: {"total": int(row.total), "published": int(row.published)} for row in key_rows}
    values: dict = {}
    for row in value_rows:
        values.setdefault(row.key, {})[row.value] = {
            "total": int(row.total),
            "published": int(row.published),
        }

    # The same for tours, by their own attributes, under parallel maps so the
    # slide counts above keep their meaning.
    # Published as in _tour_usage: the tour and its slide.
    tour_published = case(
        (and_(Tour.is_published.is_(True), Slide.is_published.is_(True)), TourAttribute.tour_id)
    )
    tour_values: dict = {}
    for row in db.execute(
        select(
            TourAttribute.key,
            TourAttribute.value,
            func.count(TourAttribute.tour_id.distinct()).label("total"),
            func.count(func.distinct(tour_published)).label("published"),
        )
        .join(Tour, Tour.id == TourAttribute.tour_id)
        .join(Slide, Slide.id == Tour.slide_id)
        .group_by(TourAttribute.key, TourAttribute.value)
    ).all():
        tour_values.setdefault(row.key, {})[row.value] = {
            "total": int(row.total),
            "published": int(row.published),
        }
    tour_keys: dict = {
        row.key: {"total": int(row.total), "published": int(row.published)}
        for row in db.execute(
            select(
                TourAttribute.key,
                func.count(TourAttribute.tour_id.distinct()).label("total"),
                func.count(func.distinct(tour_published)).label("published"),
            )
            .join(Tour, Tour.id == TourAttribute.tour_id)
            .join(Slide, Slide.id == Tour.slide_id)
            .group_by(TourAttribute.key)
        ).all()
    }

    return {"keys": keys, "values": values, "tour_keys": tour_keys, "tour_values": tour_values}


@router.get("/api/admin/metadata/keys/usage")
def admin_get_metadata_key_usage(
    key: str = Query(...),
    admin: User = Depends(require_manager_or_above),
    db: Session = Depends(get_db),
):
    """Count slides — and tours, by their own attributes — that have the
    given metadata key set, with a draft/published split."""
    k = (key or "").strip()
    total = db.scalar(
        select(func.count(SlideAttribute.slide_id.distinct())).where(SlideAttribute.key == k)
    ) or 0
    published = db.scalar(
        select(func.count(SlideAttribute.slide_id.distinct()))
        .join(Slide, Slide.id == SlideAttribute.slide_id)
        .where(SlideAttribute.key == k, Slide.is_published.is_(True))
    ) or 0
    tour_ids = db.execute(
        select(TourAttribute.tour_id).where(TourAttribute.key == k)
    ).scalars().all()
    fingerprint = _usage_fingerprint(
        db.execute(select(SlideAttribute.slide_id).where(SlideAttribute.key == k)).scalars(),
        tour_ids,
    )
    return {
        "key": k,
        "slide_count": int(total),
        "published_slide_count": int(published),
        **_tour_usage(db, tour_ids),
        "slides_fingerprint": fingerprint,
    }


@router.delete("/api/admin/metadata/keys")
def admin_delete_metadata_key(
    key: str = Query(...),
    # See admin_delete_metadata_option.
    expected_fingerprint: Optional[str] = Query(None, max_length=64),
    admin: User = Depends(require_manager_or_above),
    db: Session = Depends(get_db),
):
    """Delete all values registered under a custom key. Base filter keys cannot be removed.

    Uses query parameters; see admin_delete_metadata_option for rationale.
    """
    k = (key or "").strip()
    if not k:
        raise api_error(422, "adminDash.apiErr.metaKeyRequired")
    if k in BASE_FILTER_KEYS:
        raise api_error(400, "adminDash.apiErr.baseCategoryImmutable")
    # Same contract as option deletion: the dashboard warns that the category
    # is removed from all slides using it, so cascade the SlideAttribute rows.
    # Refused unless the stripped slides are the confirmed set (see
    # admin_delete_metadata_option).
    removed_ids = db.execute(
        delete(SlideAttribute).where(SlideAttribute.key == k).returning(SlideAttribute.slide_id)
    ).scalars().all()
    removed_tour_ids = db.execute(
        delete(TourAttribute).where(TourAttribute.key == k).returning(TourAttribute.tour_id)
    ).scalars().all()
    slides_affected = len(set(removed_ids))
    attr_removed = len(removed_ids)
    tours_affected = len(set(removed_tour_ids))
    if (
        expected_fingerprint is not None
        and _usage_fingerprint(removed_ids, removed_tour_ids) != expected_fingerprint
    ):
        db.rollback()
        return _changed_since_confirmation(slides_affected, tours_affected)
    _bump_tour_revisions(db, removed_tour_ids)
    result = db.execute(delete(MetadataOption).where(MetadataOption.key == k))
    options_removed = int(result.rowcount or 0)
    db.commit()
    audit(
        admin,
        "metadata.key.delete",
        target=k,
        options_removed=options_removed,
        slide_attrs_removed=int(attr_removed),
        slides_affected=int(slides_affected),
        tour_attrs_removed=len(removed_tour_ids),
        tours_affected=tours_affected,
    )
    return {
        "ok": True,
        # Number of MetadataOption rows removed under this key.
        "options_removed": options_removed,
        "slide_attrs_removed": int(attr_removed),
        "slides_affected": int(slides_affected),
        "tour_attrs_removed": len(removed_tour_ids),
        "tours_affected": tours_affected,
    }
