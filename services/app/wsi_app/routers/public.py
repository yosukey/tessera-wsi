"""Public, unauthenticated read endpoints.

Anything on /api/* that does not require an admin JWT lives here so the
admin router modules do not have to share a router with code anyone
can hit.
"""
from __future__ import annotations

import hashlib
import os
from types import SimpleNamespace
from typing import Annotated, Dict, List

from fastapi import APIRouter, Depends, HTTPException, Path, Query, Request
from fastapi.responses import FileResponse, Response
from sqlalchemy import and_, func, literal, or_, select
from sqlalchemy.orm import Session, defer, selectinload

from .._serializers import (
    macro_url as macro_image_url,
    seasonal_background_to_dict,
    server_local_iso,
    slide_attribute_map,
    slide_credit_fields,
    thumb_url,
    tour_attribute_map,
    tour_json,
    viewer_settings_to_dict,
)
from .. import analytics as _analytics
from .. import app_auth as _app_auth
from .. import cf_access as _cf_access
from ..admin_helpers import (
    resolve_slide_by_public_id,
    resolve_tour_by_public_id,
)
from ..db import get_db
from ..models import (
    MetadataOption,
    NetworkSettings,
    SeasonalBackground,
    Slide,
    SlideAttribute,
    Step,
    Tour,
    TourAttribute,
    ViewerSettings,
    tile_version,
)
from ..schemas import (
    FacetsOut,
    MatchedTourOut,
    SlideDetail,
    SlideListItemOut,
    SlideListOut,
    TourIndexItemOut,
    TourIndexOut,
    TourIndexSlideOut,
    TourOut,
)


router = APIRouter()

# Caps for the public /api/slides and /api/index/tours search inputs (see
# _IndexQuery). They exist to bound DB / query generation cost on an
# unauthenticated endpoint, not to constrain legitimate use (a 200-char search
# term and 100 facet selections are already far beyond any real UI
# interaction).
_MAX_Q_LEN = 200
_MAX_FACET_ITEMS = 100        # per facet key and for the raw ``facet`` list
_MAX_FACET_VALUE_LEN = 255    # mirrors SlideAttribute.value column width

# SQLite binds integers as signed 64-bit; a value at/above 2**63 raises
# OverflowError at bind time (a 500 on an otherwise-valid request). Constrain
# every id that reaches db.get / a column comparison to this range. Used both
# as a Path() bound (-> 422 instead of 500 for an out-of-range path id) and to
# validate the numeric search term.
_MAX_DB_ID = 2**63 - 1


def _parse_db_id(raw: str) -> int | None:
    """Parse a search token as a positive 63-bit slide id, or None.

    Guards two ways the raw input would otherwise blow up: ``int()`` raising on
    non-decimal digits that ``str.isdigit()`` still accepts (e.g. ``"²"``), and
    an in-range-looking but oversized value that overflows the SQLite bind.
    """
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return None
    return value if 0 < value <= _MAX_DB_ID else None


@router.get("/api/facets", response_model=FacetsOut)
def facets(db: Session = Depends(get_db)):
    # Pull SlideAttribute counts grouped by (key, value). The group_by counts
    # are the source of truth for slides-per-value; empty MetadataOption
    # entries are layered on top so registered-but-unused values still
    # show up in the filter UI. Only published slides contribute to counts
    # so a draft attribute doesn't leak its value into the public filters.
    distinct_count = func.count(SlideAttribute.slide_id.distinct())
    rows = db.execute(
        select(SlideAttribute.key, SlideAttribute.value, distinct_count)
        .join(Slide, Slide.id == SlideAttribute.slide_id)
        .where(Slide.is_published.is_(True))
        # Unlisted slides are link-only: keep their attribute values out of the
        # public filter facets so an unlisted slide can't be inferred (or
        # narrowed down to) from the browse filters.
        .where(Slide.unlisted.is_(False))
        .group_by(SlideAttribute.key, SlideAttribute.value)
        .order_by(SlideAttribute.key.asc(), distinct_count.desc(), SlideAttribute.value.asc())
    ).all()
    groups: Dict[str, List[Dict[str, int | str]]] = {}
    seen: Dict[str, set[str]] = {}
    for key, value, count in rows:
        groups.setdefault(key, []).append({"value": value, "count": count})
        seen.setdefault(key, set()).add(value)

    # The same value's reach in the two views that also match through tours
    # (see _IndexQuery): ``all_count`` — listed slides that carry it or have
    # a listed tour that does (the "All" view's result for that one value),
    # and ``tour_count`` — listed tours that carry it themselves, a value
    # their slide passes down not counting (the "Tours" view's). Same
    # visibility gates as the listings, so a draft or link-only tour or slide
    # never surfaces a value. A value only tours carry is added here.
    all_counts, tour_counts = _tour_facet_counts(
        db, {(key, value): count for key, value, count in rows},
    )
    for (key, value) in sorted(set(all_counts) | set(tour_counts)):
        bucket = seen.setdefault(key, set())
        if value in bucket:
            continue
        bucket.add(value)
        groups.setdefault(key, []).append({"value": value, "count": 0})
    for key, items in groups.items():
        for item in items:
            pair = (key, item["value"])
            item["all_count"] = all_counts.get(pair, 0)
            item["tour_count"] = tour_counts.get(pair, 0)

    # Layer in empty (count=0) entries for registered metadata options that
    # don't yet appear on any slide. Using a per-key set keeps this O(n)
    # instead of an O(n²) any()-over-list dedup.
    opt_rows = db.execute(
        select(MetadataOption.key, MetadataOption.value)
        .order_by(MetadataOption.key.asc(), MetadataOption.value.asc())
    ).all()
    for key, value in opt_rows:
        bucket = seen.setdefault(key, set())
        if value in bucket:
            continue
        bucket.add(value)
        groups.setdefault(key, []).append({"value": value, "count": 0, "all_count": 0, "tour_count": 0})
    return {"groups": groups}


def _tour_facet_counts(db: Session, slide_counts: dict) -> tuple[dict, dict]:
    """``(all_counts, tour_counts)``, each ``{(key, value): n}``; see facets.
    ``slide_counts`` is the "Slides" count per pair (listed slides carrying it).

    The UNIQUE (owner, key, value) constraints make the counts exact:

    * tour_count = the listed tours carrying the pair themselves (the
      "Tours" view filters on a tour's own metadata, so a value its slide
      passes down doesn't count);
    * all_count = slide_counts + the listed slides that don't carry it but
      have a listed tour that does.
    """
    tour_counts: dict = {
        (key, value): int(n)
        for key, value, n in db.execute(
            select(TourAttribute.key, TourAttribute.value, func.count(Tour.id.distinct()))
            .join(Tour, Tour.id == TourAttribute.tour_id)
            .join(Slide, Slide.id == Tour.slide_id)
            .where(*_LISTED_TOUR, *_LISTED_SLIDE)
            .group_by(TourAttribute.key, TourAttribute.value)
        ).all()
    }
    all_counts: dict = dict(slide_counts)
    slide_has_it = (
        select(SlideAttribute.id)
        .where(
            SlideAttribute.slide_id == Tour.slide_id,
            SlideAttribute.key == TourAttribute.key,
            SlideAttribute.value == TourAttribute.value,
        )
        .exists()
    )
    for key, value, slides in db.execute(
        select(TourAttribute.key, TourAttribute.value, func.count(Tour.slide_id.distinct()))
        .join(Tour, Tour.id == TourAttribute.tour_id)
        .join(Slide, Slide.id == Tour.slide_id)
        .where(*_LISTED_TOUR, *_LISTED_SLIDE, ~slide_has_it)
        .group_by(TourAttribute.key, TourAttribute.value)
    ).all():
        pair = (key, value)
        all_counts[pair] = all_counts.get(pair, 0) + int(slides)
    return all_counts, tour_counts


# Visibility of the public browse surfaces, shared by every listing below.
# Unlisted slides/tours are link-only: excluded from browse, search and the
# totals so they never surface there, while their direct /api/slides/{id} and
# /api/tours/{id} URLs keep working (those gate on is_published only).
_LISTED_SLIDE = (Slide.is_published.is_(True), Slide.unlisted.is_(False))
_LISTED_TOUR = (Tour.is_published.is_(True), Tour.unlisted.is_(False))

# Slides per IN (...) list when fetching a page's matched tours. Mirrors
# SQLAlchemy's own selectinload batch, and keeps each list under SQLite's
# bound-parameter limit (999 before 3.32) at the 5000-row per_page ceiling.
_MATCHED_TOURS_BATCH = 500


class _IndexQuery:
    """Search / filter / sort / page inputs shared by the two index listings.

    /api/slides and /api/index/tours take the same parameters so the index
    page's Tours view narrows, orders and caps its results exactly like the
    slide views do; one declaration (and one set of input caps) keeps the two
    from drifting. Used as ``Depends()``, so FastAPI still exposes each field
    as an ordinary query parameter.
    """

    def __init__(
        self,
        q: str = Query("", max_length=_MAX_Q_LEN),
        stain: List[str] = Query(default=[]),
        organ: List[str] = Query(default=[]),
        lab: List[str] = Query(default=[]),
        tags: List[str] = Query(default=[]),
        facet: List[str] = Query(default=[]),
        sort: str = "id",
        order: str = "asc",
        page: int = Query(1, ge=1),
        per_page: int = Query(60, ge=1, le=5000),
    ):
        self.q = q
        self.sort = sort
        self.order = order
        self.page = page
        self.per_page = per_page
        self.offset = (page - 1) * per_page

        # Public, unauthenticated endpoints: bound the cost an anonymous caller
        # can impose. ``q`` is already length-capped by Query(max_length); here
        # the number of facet values and each value's length are capped so a
        # giant query string can't expand into a pathological IN (...) clause.
        # Excess is dropped rather than rejected to keep the search box
        # forgiving.
        def _cap_values(values: List[str]) -> List[str]:
            return [v[:_MAX_FACET_VALUE_LEN] for v in values[:_MAX_FACET_ITEMS]]

        selected: Dict[str, List[str]] = {}
        for key, values in (("stain", stain), ("organ", organ), ("lab", lab), ("tags", tags)):
            if values:
                selected[key] = _cap_values(values)
        for pair in facet[:_MAX_FACET_ITEMS]:
            if ":" not in pair:
                continue
            key, value = pair.split(":", 1)
            key = key.strip()[:_MAX_FACET_VALUE_LEN]
            value = value.strip()[:_MAX_FACET_VALUE_LEN]
            if key and value:
                selected.setdefault(key, []).append(value)
        self.attribute_groups: Dict[str, List[str]] = {}
        for key, values in selected.items():
            normalized = list({v.strip() for v in values if v and v.strip()})
            if normalized:
                self.attribute_groups[key] = normalized

    @property
    def like(self) -> str:
        return f"%{self.q}%"

    def attribute_clauses(self) -> list:
        """One clause per selected metadata key: OR within a key, AND across.
        Matches on the slide's own metadata only."""
        return [
            Slide.id.in_(
                select(SlideAttribute.slide_id).where(
                    SlideAttribute.key == key,
                    SlideAttribute.value.in_(values),
                )
            )
            for key, values in self.attribute_groups.items()
        ]

    def tour_attribute_clauses(self) -> list:
        """attribute_clauses for a tour, on its effective metadata: per key,
        its slide's values or its own (tour_attributes) — the union a tour
        carries. Uncorrelated IN subqueries, like the slide's."""
        return [
            or_(
                Tour.slide_id.in_(
                    select(SlideAttribute.slide_id).where(
                        SlideAttribute.key == key, SlideAttribute.value.in_(values),
                    )
                ),
                Tour.id.in_(
                    select(TourAttribute.tour_id).where(
                        TourAttribute.key == key, TourAttribute.value.in_(values),
                    )
                ),
            )
            for key, values in self.attribute_groups.items()
        ]

    def tour_own_attribute_clauses(self) -> list:
        """attribute_clauses for a tour on its own metadata (tour_attributes)
        only, its slide's values not counting — the "Tours" view's filters."""
        return [
            Tour.id.in_(
                select(TourAttribute.tour_id).where(
                    TourAttribute.key == key, TourAttribute.value.in_(values),
                )
            )
            for key, values in self.attribute_groups.items()
        ]

    def slide_match(self):
        """OR of everything ``q`` matches on a slide's own fields — numeric
        id, catalog id, title, any attribute value — or None without ``q``."""
        if not self.q:
            return None
        like = self.like
        clauses = []
        slide_id_q = _parse_db_id(self.q)
        if slide_id_q is not None:
            clauses.append(Slide.id == slide_id_q)
        clauses.append(Slide.catalog_id.ilike(like))
        clauses.append(Slide.title.ilike(like))
        clauses.append(Slide.id.in_(select(SlideAttribute.slide_id).where(SlideAttribute.value.ilike(like))))
        return or_(*clauses)

    def tour_match(self):
        """OR of what ``q`` matches on a tour's own fields — its title or one
        of its own metadata values — or None without ``q``. (Its slide's
        fields are slide_match's; the callers combine the two.)"""
        if not self.q:
            return None
        like = self.like
        return or_(
            Tour.title.ilike(like),
            Tour.id.in_(select(TourAttribute.tour_id).where(TourAttribute.value.ilike(like))),
        )

    def tour_criteria(self) -> list:
        """What a tour must meet to match on its own account — its effective
        metadata passes the filters and ``q`` hits its own fields. With
        neither, empty (every tour)."""
        clauses = self.tour_attribute_clauses()
        tq = self.tour_match()
        if tq is not None:
            clauses.append(tq)
        return clauses

    def all_view_match(self):
        """The "All" view's match, or None with neither ``q`` nor a filter.

        A slide is listed when it matches by itself (its own metadata passes
        the filters and ``q`` hits its own fields), or when one of its
        published, listed tours matches: the tour's effective metadata (its
        slide's plus its own) passes the filters, and ``q`` hits the tour's
        own fields or its slide's. (The Tours view filters on the tour's own
        metadata alone; here a tour completes its slide, so the slide's
        values count toward it.) The
        tour legs are uncorrelated IN subqueries; the parent's visibility is
        applied by the caller.
        """
        sq = self.slide_match()
        if sq is None and not self.attribute_groups:
            return None
        own = and_(*self.attribute_clauses(), *([sq] if sq is not None else []))
        legs = [own]
        if self.q:
            # A tour hit on its own fields (title, own metadata).
            legs.append(Slide.id.in_(
                select(Tour.slide_id).where(*_LISTED_TOUR, *self.tour_criteria())
            ))
        if self.attribute_groups:
            # A tour passing the filters while q (if any) hits the slide.
            via_attrs = Slide.id.in_(
                select(Tour.slide_id).where(*_LISTED_TOUR, *self.tour_attribute_clauses())
            )
            legs.append(and_(sq, via_attrs) if sq is not None else via_attrs)
        return or_(*legs)

    def slide_order(self) -> tuple:
        """The slide list's ORDER BY: the chosen column, then id as tie-break."""
        if self.sort == "title":
            primary_col = Slide.title
        elif self.sort == "catalog_id":
            primary_col = Slide.catalog_id
        elif self.sort == "wsi_filename":
            primary_col = Slide.wsi_filename
        else:
            primary_col = Slide.id
        sort_col = primary_col.desc() if self.order.lower() == "desc" else primary_col.asc()
        return (sort_col, Slide.id.asc())

    def tour_order(self) -> tuple:
        """The "Tours" view's ORDER BY. A title sort orders by the tour's own
        title — the one each row leads with — not its slide's, which would
        list "Step 2" before "Step 1" because the slides' titles
        happen to sort that way. The other keys are slide fields (a tour has
        no catalog id or file name of its own), so those keep the slide list's
        order and group each slide's tours together. Creation order breaks
        any remaining tie."""
        if self.sort == "title":
            title = Tour.title.desc() if self.order.lower() == "desc" else Tour.title.asc()
            return (title, *self.slide_order(), Tour.id.asc())
        return (*self.slide_order(), Tour.id.asc())


def _matched_tours_by_slide(
    db: Session, slide_ids: List[int], params: "_IndexQuery",
) -> Dict[int, List[MatchedTourOut]]:
    """The listed tours that are a reason their slide is in an "All" result,
    grouped by slide, for one page.

    A tour is one when it matches as _IndexQuery.all_view_match takes it (its
    effective metadata passes the filters, ``q`` hits its own fields or its
    slide's) and adds something its slide doesn't: a ``q``
    hit on its own fields, or — when the slide's own metadata fails the
    filters — the metadata that passes them. So a search alone lists the
    tours it hit by title (or own metadata), and a filter alone lists the
    tours that let a slide through which would not pass by itself.

    A few queries per _MATCHED_TOURS_BATCH slides (one batch for any
    realistic page), never per slide: this runs on every search keystroke of
    the busiest public endpoint.
    """
    sq = params.slide_match()
    tq = params.tour_match()
    out: Dict[int, List[MatchedTourOut]] = {}
    for start in range(0, len(slide_ids), _MATCHED_TOURS_BATCH):
        batch = slide_ids[start:start + _MATCHED_TOURS_BATCH]
        # Slides passing the filters / q by themselves (all, without either).
        passes_attrs = (
            set(db.execute(
                select(Slide.id).where(Slide.id.in_(batch), *params.attribute_clauses())
            ).scalars())
            if params.attribute_groups else set(batch)
        )
        passes_q = (
            set(db.execute(select(Slide.id).where(Slide.id.in_(batch), sq)).scalars())
            if sq is not None else set(batch)
        )
        rows = db.execute(
            select(
                Tour.slide_id, Tour.id, Tour.public_id, Tour.title,
                (tq if tq is not None else literal(False)).label("own_hit"),
            )
            .where(Tour.slide_id.in_(batch))
            .where(*_LISTED_TOUR, *params.tour_attribute_clauses())
            .order_by(Tour.slide_id.asc(), Tour.id.asc())
        ).all()
        for slide_id, tour_id, tour_public_id, title, own_hit in rows:
            own_hit = bool(own_hit)
            if not (own_hit or slide_id in passes_q):
                continue  # the tour doesn't match q at all
            if not (own_hit or slide_id not in passes_attrs):
                continue  # adds nothing the slide doesn't match by itself
            out.setdefault(slide_id, []).append(
                MatchedTourOut(id=tour_id, public_id=tour_public_id or "", title=title)
            )
    return out


@router.get("/api/slides", response_model=SlideListOut)
def list_slides(
    params: _IndexQuery = Depends(),
    # Also match slides through their published, listed tours — by the
    # tours' own fields for ``q`` and by their effective metadata (the
    # slide's plus their own) for the filters (the index page's "All" view;
    # see _IndexQuery.all_view_match). The "Slides" view passes false to
    # match on the slides' own fields and metadata only, since it shows no
    # tours that could explain a match.
    match_tours: bool = True,
    db: Session = Depends(get_db),
):
    where = [*_LISTED_SLIDE]
    if match_tours:
        match = params.all_view_match()
        if match is not None:
            where.append(match)
    else:
        where.extend(params.attribute_clauses())
        match = params.slide_match()
        if match is not None:
            where.append(match)

    stmt = (
        select(Slide)
        # Never load the LargeBinary macro_data / label_data blobs for a browse
        # page: this is unauthenticated and per_page reaches 5000, so a single
        # request could otherwise pull tens of GB of associated-image bytes.
        # macro_url derives presence from the small macro_etag column.
        .options(
            selectinload(Slide.attributes),
            defer(Slide.macro_data),
            defer(Slide.label_data),
        )
        .where(*where)
        .order_by(*params.slide_order())
        .offset(params.offset)
        .limit(params.per_page)
    )
    slides = db.execute(stmt).scalars().all()
    # Counted over the same combined match set as the page, so ``total`` (and
    # the client's truncation banner) describe the search result itself,
    # matches through tours (their fields or metadata) included.
    total = db.execute(select(func.count(Slide.id)).where(*where)).scalar_one()
    matched = (
        _matched_tours_by_slide(db, [s.id for s in slides], params)
        if (params.q or params.attribute_groups) and match_tours and slides
        else {}
    )
    items = [
        SlideListItemOut(
            id=s.id, public_id=s.public_id or "", catalog_id=s.catalog_id, title=s.title,
            thumb_url=thumb_url(s), width=s.width, height=s.height, mpp_x=s.mpp_x, mpp_y=s.mpp_y,
            attributes=slide_attribute_map(s),
            show_zoom_indicator=s.show_zoom_indicator,
            show_scale_bar=s.show_scale_bar,
            show_metadata_panel=s.show_metadata_panel,
            image_adjust_brightness=s.image_adjust_brightness,
            image_adjust_contrast=s.image_adjust_contrast,
            image_adjust_saturation=s.image_adjust_saturation,
            macro_url=macro_image_url(s),
            acquired_at=s.acquired_at or "",
            **slide_credit_fields(s),
            created_at=server_local_iso(s.created_at),
            matched_tours=matched.get(s.id, []),
        )
        for s in slides
    ]
    return SlideListOut(items=items, total=total, page=params.page, per_page=params.per_page)


# Public read surfaces are keyed by the unguessable ``public_id`` token, never
# the numeric PK — a numeric string simply won't match any token, so id
# enumeration of unlisted content returns 404. Bound the length so an
# unbounded path segment can't reach the DB layer.
_PublicIdPath = Annotated[str, Path(min_length=1, max_length=64)]


def _is_staff_request(request: Request) -> bool:
    """True when the caller presents a valid staff (admin/manager/instructor) JWT.

    Lets the admin dashboard preview *draft* slides/tours through the same
    token-keyed public URLs the viewer uses (the viewer only knows the
    public_id token, not the numeric id, so it can't call the numeric admin
    detail endpoints). Any staff role can already view drafts via the
    ``require_staff`` admin endpoints, so the bypass matches that — an
    admin-only gate would 404 managers/instructors previewing their own
    unpublished tours. Mirrors the /dzi/ tile bypass.
    """
    from ..app_auth import staff_token_valid
    return staff_token_valid(request.headers.get("authorization", ""))


@router.get("/api/slides/{public_id}", response_model=SlideDetail)
def get_slide(public_id: _PublicIdPath, request: Request, db: Session = Depends(get_db)):
    s = resolve_slide_by_public_id(db, public_id)
    is_staff = _is_staff_request(request)
    if not s.is_published and not is_staff:
        # Return 404 (not 403) on draft slides so an unauthenticated probe
        # can't distinguish "slide doesn't exist" from "slide is unpublished".
        # An authenticated admin gets the draft so the dashboard can preview it.
        raise HTTPException(status_code=404, detail="Slide not found")
    # Original scan filenames routinely encode accession/case identifiers, which
    # is why the public list endpoint (list_slides) omits wsi_filename/vendor.
    # This token-keyed detail endpoint is reachable anonymously for published
    # slides, so mirror that visibility contract: expose the real filename and
    # scanner vendor only to staff. The viewer still needs the *extension* to
    # route SZI slides (isSziSlide checks for a trailing ``.szi``), so anonymous
    # callers get an extension-only sentinel — enough to branch on, nothing that
    # identifies the case.
    if is_staff:
        wsi_filename = s.wsi_filename
        vendor = s.vendor
    else:
        ext = os.path.splitext(s.wsi_filename or "")[1]
        wsi_filename = f"slide{ext}" if ext else ""
        vendor = ""
    return SlideDetail(
        id=s.id, public_id=s.public_id or "", catalog_id=s.catalog_id, title=s.title,
        thumb_url=thumb_url(s), width=s.width, height=s.height, mpp_x=s.mpp_x, mpp_y=s.mpp_y,
        wsi_filename=wsi_filename, vendor=vendor, attributes=slide_attribute_map(s),
        show_zoom_indicator=s.show_zoom_indicator,
        show_scale_bar=s.show_scale_bar,
        show_metadata_panel=s.show_metadata_panel,
        image_adjust_brightness=s.image_adjust_brightness,
        image_adjust_contrast=s.image_adjust_contrast,
        image_adjust_saturation=s.image_adjust_saturation,
        # Echo the publish state so the viewer can gate admin-only affordances
        # that are only valid on published slides — notably the live-sync
        # presenter launcher (the server refuses to start a session on a draft,
        # so surfacing the button on one would be a dead end). Without this the
        # field falls back to the schema default (False) for *every* slide,
        # including published ones, and the launcher never appears. Anonymous
        # callers only ever reach a published slide here (drafts 404 above), and
        # a staff draft-preview already knows the publish state, so this leaks
        # nothing.
        is_published=bool(s.is_published),
        macro_url=macro_image_url(s),
        acquired_at=s.acquired_at or "",
        **slide_credit_fields(s),
        created_at=server_local_iso(s.created_at),
        # The viewer opens /dzi/{public_id}.dzi?v={tile_version}; see
        # schemas.SlideOut.tile_version.
        tile_version=tile_version(s.wsi_generation),
    )


@router.get("/api/slides/{public_id}/macro")
def get_slide_macro(public_id: _PublicIdPath, request: Request, db: Session = Depends(get_db)):
    """Serve the slide's vendor "macro" associated image.

    Public, but only for published slides — keeps the visibility contract
    consistent with thumbnails (a draft slide's overview shouldn't be
    discoverable through a direct URL guess). 404 covers both "no such slide"
    and "slide is unpublished" so an unauthenticated probe can't tell them
    apart, mirroring ``get_slide`` above.
    """
    s = db.execute(select(Slide).where(Slide.public_id == public_id)).scalar_one_or_none()
    if not s or (not s.is_published and not _is_staff_request(request)) or not s.macro_data:
        raise HTTPException(status_code=404, detail="No macro image")
    etag = '"' + (s.macro_etag or "") + '"'
    # ``public`` explicitly licenses a shared cache to store the response and
    # hand it to anyone presenting the URL, even when the request carried
    # Authorization (RFC 9111 §3.5) — and a cache HIT never reaches the
    # visibility check above. So only a published slide on an ungated install
    # may say ``public``: a draft is served to staff alone, and with app
    # access or Cloudflare Access gating the site a published image must not
    # be replayed to an unauthenticated client either. 304s carry the same
    # policy, since they refresh the stored entry's freshness.
    shareable = (
        bool(s.is_published)
        and _app_auth.current_mode() == "none"
        and not _cf_access.is_enabled()
    )
    cache_control = "public, max-age=300" if shareable else "private, max-age=300"
    if request.headers.get("if-none-match") == etag:
        return Response(status_code=304, headers={"ETag": etag, "Cache-Control": cache_control})
    return Response(
        content=s.macro_data,
        media_type=s.macro_mimetype or "image/jpeg",
        headers={"ETag": etag, "Cache-Control": cache_control},
    )


@router.get("/api/slides/{public_id}/tours", response_model=List[TourOut])
def list_tours(public_id: _PublicIdPath, request: Request, db: Session = Depends(get_db)):
    # Public endpoint: only published tours on a published slide are visible.
    # Admins use the admin-only endpoints to view drafts. Unlisted tours are
    # link-only and dropped from this per-slide list too; the caller reaches
    # this slide via its own link, so only the tour's own unlisted flag gates
    # here (the parent slide's unlisted state is not re-checked).
    parent = resolve_slide_by_public_id(db, public_id)
    if not parent.is_published and not _is_staff_request(request):
        raise HTTPException(status_code=404, detail="Slide not found")
    stmt = (
        select(Tour)
        .where(Tour.slide_id == parent.id)
        .where(Tour.is_published.is_(True))
        .where(Tour.unlisted.is_(False))
    )
    tours = db.execute(stmt).scalars().all()
    return [
        TourOut(
            id=t.id, public_id=t.public_id or "", slide_public_id=parent.public_id or "",
            slide_id=t.slide_id, title=t.title, description=t.description,
            is_published=t.is_published, unlisted=t.unlisted,
        )
        for t in tours
    ]


@router.get("/api/tours", response_model=List[TourOut])
def list_all_tours(db: Session = Depends(get_db)):
    # Public endpoint: only published tours attached to published slides
    # are visible. The slide-level join avoids leaking a tour after the
    # slide is unpublished but the tour row hasn't yet been cascaded.
    #
    # Two unlisted gates, both link-only: a tour's own unlisted flag hides it
    # from this global browse, and an unlisted *parent slide* hides all of its
    # tours here too — otherwise a listed tour under a link-only slide would
    # re-expose that slide's existence through the tour index.
    stmt = (
        select(Tour, Slide.public_id)
        .join(Slide, Slide.id == Tour.slide_id)
        .where(Tour.is_published.is_(True))
        .where(Tour.unlisted.is_(False))
        .where(Slide.is_published.is_(True))
        .where(Slide.unlisted.is_(False))
        .order_by(Tour.slide_id.asc(), Tour.id.asc())
    )
    rows = db.execute(stmt).all()
    return [
        TourOut(
            id=t.id, public_id=t.public_id or "", slide_public_id=slide_public_id or "",
            slide_id=t.slide_id, title=t.title, description=t.description,
            is_published=t.is_published, unlisted=t.unlisted,
        )
        for t, slide_public_id in rows
    ]


# The index page's "Tours" view: tours searched, filtered, sorted and capped
# server-side, each carrying its parent slide's card data, so the view needs
# neither the slide lists (capped at max_index_items, so a tour whose parent
# sat past the cap couldn't be checked against an active filter) nor the full
# /api/tours dump. Lives outside /api/tours/ so the path can't shadow a tour
# whose public_id token — any [A-Za-z0-9_-]{1,32} on an imported bundle —
# happens to spell it.
@router.get("/api/index/tours", response_model=TourIndexOut)
def list_tour_index(params: _IndexQuery = Depends(), db: Session = Depends(get_db)):
    # Same visibility as /api/tours (published, listed tours on published,
    # listed slides — the unlisted-parent gate keeps a link-only slide from
    # resurfacing through its tours), narrowed by the filters /api/slides
    # takes, on the tour's own metadata alone: a value its slide passes down
    # doesn't make the tour match, so filtering lists the tours that were
    # given that value themselves.
    where = [*_LISTED_TOUR, *_LISTED_SLIDE, *params.tour_own_attribute_clauses()]
    if params.q:
        # A tour matches on its own fields (title, own metadata) or when its
        # parent slide matches q on the slide's own fields. A sibling tour's
        # deliberately doesn't count: searching "mitosis" lists the mitosis
        # tour, not every other tour that happens to share its slide.
        where.append(or_(params.tour_match(), params.slide_match()))

    stmt = (
        select(Tour, Slide)
        .join(Slide, Slide.id == Tour.slide_id)
        # Same blob guard as list_slides; the card needs none of them. The two
        # attribute sets feed the card's metadata line.
        .options(
            defer(Slide.macro_data), defer(Slide.label_data),
            selectinload(Slide.attributes), selectinload(Tour.attributes),
        )
        .where(*where)
        .order_by(*params.tour_order())
        .offset(params.offset)
        .limit(params.per_page)
    )
    rows = db.execute(stmt).all()
    total = db.execute(
        select(func.count(Tour.id))
        .select_from(Tour)
        .join(Slide, Slide.id == Tour.slide_id)
        .where(*where)
    ).scalar_one()
    items = [
        TourIndexItemOut(
            id=t.id, public_id=t.public_id or "", slide_public_id=s.public_id or "",
            slide_id=t.slide_id, title=t.title, description=t.description,
            is_published=t.is_published,
            attributes=tour_attribute_map(t),
            slide=TourIndexSlideOut(
                id=s.id, public_id=s.public_id or "", catalog_id=s.catalog_id,
                title=s.title, thumb_url=thumb_url(s),
                image_adjust_brightness=s.image_adjust_brightness,
                image_adjust_contrast=s.image_adjust_contrast,
                image_adjust_saturation=s.image_adjust_saturation,
                attributes=slide_attribute_map(s),
            ),
        )
        for t, s in rows
    ]
    return TourIndexOut(items=items, total=total, page=params.page, per_page=params.per_page)


@router.get("/api/tours/{public_id}")
def get_tour(public_id: _PublicIdPath, request: Request, db: Session = Depends(get_db)):
    t = resolve_tour_by_public_id(db, public_id)
    is_staff = _is_staff_request(request)
    if not t.is_published and not is_staff:
        raise HTTPException(status_code=404, detail="Tour not found")
    # Defence in depth: even if the tour was left published, hide it once the
    # parent slide is a draft. The cascade in admin_publish_slide normally
    # demotes the tour too, but this guards against any code path that might
    # leave the row out of sync. Admins previewing a draft bypass both gates.
    parent = db.get(Slide, t.slide_id)
    if (not parent or not parent.is_published) and not is_staff:
        raise HTTPException(status_code=404, detail="Tour not found")
    steps = db.execute(select(Step).where(Step.tour_id == t.id).order_by(Step.order.asc())).scalars().all()
    # Rendered straight to the body (tour_json): stored rows were validated on
    # write, so nothing is re-validated or walked by FastAPI's
    # jsonable_encoder, and stored freehand vertices are spliced in as text.
    # This route is unauthenticated and its cost grows with every vertex.
    # Plus the tour's own metadata (its slide's comes with the slide), which
    # the tour viewer's info panel shows alongside the slide's.
    return Response(tour_json(
        db,
        {**TourOut(
            # ``parent`` can legitimately be None here: the guard above only
            # 404s a missing parent for anonymous callers, so staff previewing
            # an orphaned tour (parent lost to a restore or a hand-edited DB)
            # reach this line. Dereferencing it unconditionally would turn that
            # into a 500 for staff while anonymous callers get a clean 404.
            # Matches admin_tours.admin_get_tour, which already guards the
            # same way.
            id=t.id, public_id=t.public_id or "",
            slide_public_id=(parent.public_id or "") if parent else "",
            slide_id=t.slide_id, title=t.title, description=t.description,
            is_published=t.is_published, unlisted=t.unlisted,
        ).model_dump(), "attributes": tour_attribute_map(t)},
        steps,
    ), media_type="application/json")


def _rows_without_blobs(db: Session, model, blobs: tuple, *where) -> list:
    """``model``'s rows as plain objects, each LargeBinary column in ``blobs``
    replaced by whether it holds any bytes (``length(blob) > 0``, which SQLite
    answers from the record header). The settings payload only needs to know
    an image exists — its URL carries the etag — and loading the blobs
    themselves would cost up to ~14.5 MB per /api/viewer/settings request."""
    cols = [
        (func.length(c) > 0).label(c.key) if c.key in blobs else c
        for c in model.__table__.columns
    ]
    rows = db.execute(select(*cols).where(*where).order_by(model.id.asc())).mappings()
    return [SimpleNamespace(**row) for row in rows]


def _list_seasonal_backgrounds(db: Session) -> list[dict]:
    """The slots the top page may show: enabled and with an image, in slot
    order (precedence between overlapping windows is the client's)."""
    rows = _rows_without_blobs(
        db, SeasonalBackground, ("image_data",),
        SeasonalBackground.id.between(1, 5),
        SeasonalBackground.enabled.is_(True),
        func.length(SeasonalBackground.image_data) > 0,
    )
    return [seasonal_background_to_dict(sb) for sb in rows]


@router.get("/api/viewer/settings")
def get_viewer_settings(db: Session = Depends(get_db)):
    vs_rows = _rows_without_blobs(
        db, ViewerSettings,
        ("logo_data", "favicon_data", "header_default_image_data"),
        ViewerSettings.id == 1,
    )
    ns = db.get(NetworkSettings, 1)
    ga_id = (ns.ga_measurement_id or "") if ns else ""
    # Analytics flags are sourced from NetworkSettings (same row that
    # already carries the GA4 ID). The frontend reads them to decide
    # whether to fire the dwell/tour beacons and whether to show the
    # one-time recording-notice toast. Defaults match a fresh install
    # where the NetworkSettings row hasn't been created yet.
    analytics_mode = _analytics.normalize_analytics_mode(ns.analytics_mode if ns else None)
    analytics_notice_toast_enabled = (
        bool(ns.analytics_notice_toast_enabled) if ns else True
    )
    return {
        **viewer_settings_to_dict(vs_rows[0] if vs_rows else None),
        "ga_measurement_id": ga_id,
        "seasonal_backgrounds": _list_seasonal_backgrounds(db),
        "analytics_mode": analytics_mode,
        "analytics_notice_toast_enabled": analytics_notice_toast_enabled,
    }


_DEFAULT_FAVICON_PATH = os.path.realpath(
    os.path.join(os.path.dirname(__file__), "..", "..", "static", "favicon.ico")
)


# HEAD too: the LAN-direct probe (frontend/src/lib/lanProbe.ts) sends a
# bodiless HEAD, and unlike a plain Starlette Route a FastAPI GET route does
# not answer HEAD on its own (405). The ASGI server drops the body of a HEAD
# response and FileResponse sends headers only, so both methods carry the
# same headers.
@router.api_route("/favicon.ico", methods=["GET", "HEAD"])
def get_favicon(request: Request, db: Session = Depends(get_db)):
    vs = db.get(ViewerSettings, 1)
    if vs and vs.favicon_data:
        etag = '"' + (vs.favicon_etag or hashlib.sha1(vs.favicon_data).hexdigest()[:16]) + '"'
        if request.headers.get("if-none-match") == etag:
            return Response(status_code=304, headers={"ETag": etag, "Cache-Control": "public, max-age=300"})
        return Response(
            content=vs.favicon_data,
            media_type=vs.favicon_mimetype or "image/x-icon",
            headers={"ETag": etag, "Cache-Control": "public, max-age=300"},
        )
    if os.path.isfile(_DEFAULT_FAVICON_PATH):
        return FileResponse(_DEFAULT_FAVICON_PATH, media_type="image/x-icon",
                            headers={"Cache-Control": "public, max-age=300"})
    raise HTTPException(status_code=404, detail="No favicon")


@router.get("/api/viewer/seasonal-background/{slot}")
def get_seasonal_background(slot: int, request: Request, db: Session = Depends(get_db)):
    if slot < 1 or slot > 5:
        raise HTTPException(status_code=404, detail="No background")
    sb = db.get(SeasonalBackground, slot)
    # Mirror the public listing's visibility contract (_list_seasonal_backgrounds
    # hides slots where ``enabled`` is false): a disabled slot must 404 here
    # too, otherwise an image the operator pulled by disabling its slot (e.g. an
    # accidental upload) stays fetchable by anyone enumerating slots 1-5. The
    # dashboard previews slots through GET
    # /api/admin/viewer/seasonal-backgrounds/{slot}/image instead, which serves
    # a disabled slot too.
    if not sb or not sb.enabled or not sb.image_data:
        raise HTTPException(status_code=404, detail="No background")
    etag = '"' + (sb.image_etag or hashlib.sha1(sb.image_data).hexdigest()[:16]) + '"'
    if request.headers.get("if-none-match") == etag:
        return Response(status_code=304, headers={"ETag": etag, "Cache-Control": "public, max-age=300"})
    return Response(
        content=sb.image_data,
        media_type=sb.image_mimetype or "image/png",
        headers={"ETag": etag, "Cache-Control": "public, max-age=300"},
    )


@router.get("/api/viewer/logo")
def get_viewer_logo(request: Request, db: Session = Depends(get_db)):
    vs = db.get(ViewerSettings, 1)
    if not vs or not vs.logo_data:
        raise HTTPException(status_code=404, detail="No logo")
    etag = '"' + (vs.logo_etag or hashlib.sha1(vs.logo_data).hexdigest()[:16]) + '"'
    if request.headers.get("if-none-match") == etag:
        return Response(status_code=304, headers={"ETag": etag, "Cache-Control": "public, max-age=300"})
    return Response(
        content=vs.logo_data,
        media_type=vs.logo_mimetype or "application/octet-stream",
        headers={"ETag": etag, "Cache-Control": "public, max-age=300"},
    )


@router.get("/api/viewer/header-default-background")
def get_header_default_background(request: Request, db: Session = Depends(get_db)):
    """Serve the always-on header background bytes (PNG/APNG/GIF).

    Mirrors get_viewer_logo: 404 when unset, ETag-based 304s otherwise.
    """
    vs = db.get(ViewerSettings, 1)
    if not vs or not vs.header_default_image_data:
        raise HTTPException(status_code=404, detail="No header background")
    etag = '"' + (vs.header_default_image_etag or hashlib.sha1(vs.header_default_image_data).hexdigest()[:16]) + '"'
    if request.headers.get("if-none-match") == etag:
        return Response(status_code=304, headers={"ETag": etag, "Cache-Control": "public, max-age=300"})
    return Response(
        content=vs.header_default_image_data,
        media_type=vs.header_default_image_mimetype or "image/png",
        headers={"ETag": etag, "Cache-Control": "public, max-age=300"},
    )
