"""Pure response-shape helpers shared by the routers and slide_transfer.

Lives outside the ``routers`` package so the admin routers, the public
read-only handlers in ``routers/public.py`` and the slide export in
``slide_transfer`` can import the same helpers without one router module
importing another (or creating a circular dependency).
"""
from __future__ import annotations

import json
import math
import re
from datetime import datetime, timezone
from typing import Dict, List, Optional

from sqlalchemy.orm import Session

from .admin_helpers import ATTR_KEY_MAX_LEN, CTRL_CHAR_RE
from .models import SeasonalBackground, Slide, Step, Tour, ViewerSettings
from .schemas import DEFAULT_TRANSITION_MODE


def server_local_iso(dt: Optional[datetime]) -> str:
    """Render a stored DateTime as ISO 8601 in the server's local timezone.

    On SQLite (this build's only backend) ``func.now()`` records
    ``CURRENT_TIMESTAMP`` as a naive UTC string even though the column declares
    ``DateTime(timezone=True)``; naive values are presumed UTC before
    converting, so the resulting offset always reflects the host's TZ setting.
    The ``tzinfo is None`` guard below also leaves any already-aware value
    correct, just re-zoned to the server's local offset.
    """
    if not dt:
        return ""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone().isoformat()


def decode_points(raw: str) -> List[Dict[str, float]]:
    """Decode StepShape.points_json into a list of {x, y} dicts.

    Returns [] for empty input (rect/circle shapes don't carry points) or
    malformed JSON (best-effort tolerance — the renderer just skips the
    overlay rather than crashing the page)."""
    if not raw:
        return []
    try:
        decoded = json.loads(raw)
    except (TypeError, ValueError):
        return []
    if not isinstance(decoded, list):
        return []
    out: List[Dict[str, float]] = []
    for p in decoded:
        if not isinstance(p, dict):
            continue
        try:
            x, y = float(p.get("x", 0.0)), float(p.get("y", 0.0))
        except (TypeError, ValueError):
            continue
        # A hand-edited 1e999 decodes to inf, which no response can carry
        # (JSON has no Infinity): skip the vertex rather than fail the read.
        if math.isfinite(x) and math.isfinite(y):
            out.append({"x": x, "y": y})
    return out


_VALID_INDEX_TYPES = ("all", "slides", "tours")


def thumb_url(slide: Slide) -> str:
    if not slide.thumb_filename:
        return ""
    # Stable per-slide version tag on the thumbnail URL. NOTE: created_at is the
    # row's insertion time and does NOT change on WSI replace (the thumbnail
    # filename is reused too), so this query string is not a replace cache-buster
    # — a replaced thumbnail is picked up instead via nginx's mtime ETag
    # revalidation on /thumbs. int(timestamp()) keeps the query string ASCII /
    # URL-safe (the raw SQLite "YYYY-MM-DD HH:MM:SS" string trips up some
    # intermediate caches).
    return f"/thumbs/{slide.thumb_filename}?v={int(slide.created_at.timestamp())}"


def macro_url(slide: Slide) -> str:
    """Public macro URL: routes through ``/api/slides/{public_id}/macro``.

    Keyed by the slide's unguessable public_id (the public endpoint accepts
    only the token, never the numeric PK). The endpoint enforces
    ``is_published``, so this URL is appropriate for public-side responses.
    Admin-side responses use ``admin_macro_url`` below instead so an admin can
    preview a draft slide's overview.
    """
    # Presence is keyed off the etag, not the blob: `macro_etag` is a small
    # column set to a non-empty hash exactly when `macro_data` is stored (and
    # "" when absent — see admin_helpers.attach_associated_images). Checking it
    # here lets list endpoints `defer(Slide.macro_data)` without this URL
    # builder triggering a per-row lazy load of the LargeBinary blob.
    if not slide.macro_etag:
        return ""
    return f"/api/slides/{slide.public_id}/macro?v={slide.macro_etag}"


def admin_macro_url(slide: Slide) -> str:
    if not slide.macro_etag:
        return ""
    return f"/api/admin/slides/{slide.id}/macro?v={slide.macro_etag}"


def label_url(slide: Slide) -> str:
    """Admin-only label-image URL. Empty when the slide has no stored label.

    Labels often contain patient identifiers / barcodes / institution names;
    the corresponding endpoint requires admin auth, so the URL is only ever
    rendered into admin-side responses (never the public ones in routers/public.py).
    """
    if not slide.label_etag:
        return ""
    return f"/api/admin/slides/{slide.id}/label?v={slide.label_etag}"


def _attribute_map(rows) -> Dict[str, List[str]]:
    grouped: Dict[str, set] = {}
    for attr in rows:
        grouped.setdefault(attr.key, set()).add(attr.value)
    return {k: sorted(v) for k, v in grouped.items()}


def slide_attribute_map(slide: Slide) -> Dict[str, List[str]]:
    return _attribute_map(slide.attributes)


def tour_attribute_map(tour: Tour) -> Dict[str, List[str]]:
    """The tour's OWN attributes (not the ones it inherits from its slide)."""
    return _attribute_map(tour.attributes)


def slide_credit_fields(slide: Slide) -> Dict[str, str]:
    """The image-credit fields of a SlideOut (see slide_credit)."""
    return {
        "credit_text": slide.credit_text or "",
        "license_id": slide.license_id or "",
        "source_url": slide.source_url or "",
    }


def viewer_logo_url(vs: Optional[ViewerSettings]) -> str:
    if not vs or not vs.logo_data:
        return ""
    return f"/api/viewer/logo?v={vs.logo_etag}"


def viewer_favicon_url(vs: Optional[ViewerSettings]) -> str:
    if not vs or not vs.favicon_data:
        return ""
    return f"/favicon.ico?v={vs.favicon_etag}"


def header_default_image_url(vs: Optional[ViewerSettings]) -> str:
    if not vs or not vs.header_default_image_data:
        return ""
    return f"/api/viewer/header-default-background?v={vs.header_default_image_etag}"


def seasonal_background_url(sb: SeasonalBackground, *, admin: bool = False) -> str:
    """The slot's image URL. The public route serves enabled slots only; the
    dashboard previews every slot — an image is uploaded before its slot is
    enabled — through the admin route, which needs the admin bearer (fetch it,
    e.g. with adminFetchImageBlobUrl; a plain <img> cannot send it)."""
    if not sb or not sb.image_data:
        return ""
    if admin:
        return f"/api/admin/viewer/seasonal-backgrounds/{sb.id}/image?v={sb.image_etag}"
    return f"/api/viewer/seasonal-background/{sb.id}?v={sb.image_etag}"


def seasonal_background_to_dict(sb: SeasonalBackground, *, admin: bool = False) -> Dict:
    return {
        "slot": sb.id,
        "enabled": bool(sb.enabled),
        "label": sb.label or "",
        "start_month": int(sb.start_month or 1),
        "start_day": int(sb.start_day or 1),
        "end_month": int(sb.end_month or 1),
        "end_day": int(sb.end_day or 1),
        "image_url": seasonal_background_url(sb, admin=admin),
        "has_image": bool(sb.image_data),
        "title_color": sb.title_color or "",
    }


def normalize_index_type(raw: Optional[str]) -> str:
    v = (raw or "").strip().lower()
    return v if v in _VALID_INDEX_TYPES else "all"


# The other enum-valued viewer settings: (accepted values, default). An
# out-of-set value — stored by hand or sent to the PATCH — reads and saves as
# the default.
_VIEW_MODES = (("tile", "list"), "tile")
_SORT_FIELDS = (("title", "catalog_id", "wsi_filename"), "catalog_id")
_FILTER_SORT_ORDERS = (("count", "alpha"), "count")
_THUMBNAIL_SIZES = (("sm", "md", "lg"), "lg")


def _choice(raw: Optional[str], spec: tuple) -> str:
    allowed, default = spec
    return raw if raw in allowed else default


def normalize_view_mode(raw: Optional[str]) -> str:
    return _choice(raw, _VIEW_MODES)


def normalize_sort_field(raw: Optional[str]) -> str:
    return _choice(raw, _SORT_FIELDS)


def normalize_filter_sort_order(raw: Optional[str]) -> str:
    return _choice(raw, _FILTER_SORT_ORDERS)


def normalize_thumbnail_size(raw: Optional[str]) -> str:
    return _choice(raw, _THUMBNAIL_SIZES)


# Most keys one attribute-key list setting (ViewerSettings.extra_attrs /
# index_card_attrs / index_filter_attrs) may hold — far more attribute keys
# than a site defines. Each key is bounded like an attribute key itself
# (ATTR_KEY_MAX_LEN, no control characters).
ATTR_LIST_MAX_ITEMS = 64


def parse_attr_list(raw: Optional[str]) -> List[str]:
    """Decode a stored attribute-key list into a list[str], silently dropping
    malformed input.

    Non-string, empty, over-long or control-character items are dropped and
    duplicates collapsed (first-seen order kept, so the admin's intended
    order survives a save round-trip), and at most ATTR_LIST_MAX_ITEMS are
    returned: the lists ship to every public client, so a hand-edited row
    can't push garbage or bulk into that payload.
    """
    try:
        parsed = json.loads(raw or "[]")
    except Exception:
        return []
    if not isinstance(parsed, list):
        return []
    out: List[str] = []
    seen: set[str] = set()
    for item in parsed:
        if (
            not isinstance(item, str) or not item or item in seen
            or len(item) > ATTR_KEY_MAX_LEN or CTRL_CHAR_RE.search(item)
        ):
            continue
        seen.add(item)
        out.append(item)
        if len(out) >= ATTR_LIST_MAX_ITEMS:
            break
    return out


def _shape_out(sh, points) -> Dict:
    return {
        "id": sh.shape_id,
        "kind": sh.kind,
        "x": sh.x,
        "y": sh.y,
        "w": sh.w,
        "h": sh.h,
        "stroke_color": sh.stroke_color,
        "fill_enabled": bool(sh.fill_enabled),
        "fill_opacity": float(sh.fill_opacity),
        "points": points,
        "smoothing": bool(sh.smoothing),
        "label": sh.label,
        "label_position": sh.label_position,
        "z": sh.z,
    }


def step_to_out(s: Step) -> Dict:
    """A stored step as its JSON response object (the StepOut shape).

    Built straight from the rows, without re-running the input validators:
    the writers validated every value on the way in, and re-validating each
    freehand vertex on every read would make a tour read cost ~17 us per point —
    seconds for one heavily drawn step, on the unauthenticated public route
    too. Every value here is already a JSON scalar, so callers can hand the
    result to a JSONResponse as is. The tour reads use tour_json instead,
    which renders the same object without decoding the stored vertices.
    """
    # The Step.shapes / Step.pins relationships declare order_by in models.py,
    # so the lists arrive already sorted by row id (= insertion order) from
    # the DB.
    return _step_out(s, [_shape_out(sh, decode_points(sh.points_json)) for sh in s.shapes])


def _step_out(s: Step, shapes) -> Dict:
    pins = [
        {
            "id": p.pin_id,
            "x": p.x,
            "y": p.y,
            "shape": p.shape,
            "color": p.color,
            "rotation_deg": p.rotation_deg,
            "label": p.label,
            "z": p.z,
        }
        for p in s.pins
    ]
    return {
        "id": s.id,
        "tour_id": s.tour_id,
        "order": s.order,
        "label": s.label,
        "center_x": s.center_x,
        "center_y": s.center_y,
        "fov_um_w": s.fov_um_w,
        "fov_um_h": s.fov_um_h,
        "rotation_deg": s.rotation_deg,
        "pins": pins,
        "shapes_l0": shapes,
        "description_md": s.description_md,
        "transition_mode": s.transition_mode or DEFAULT_TRANSITION_MODE,
        "revision": s.revision,
    }


# Renders exactly as starlette's JSONResponse does (JSONResponse.render), so a
# body assembled from pieces of it is byte-identical to the one JSONResponse
# would send for the whole object.
_JSON_ENCODER = json.JSONEncoder(ensure_ascii=False, allow_nan=False, separators=(",", ":"))


def json_text(obj) -> str:
    return _JSON_ENCODER.encode(obj)


# A float literal shaped as float.__repr__ (and so json.dumps) writes one
# for a value the writers accept (schemas._COORD_MAX, |v| <= 1e9): fixed
# notation whose fraction is ".0" or ends in a non-zero digit, or — below
# 1e-4 — a one-digit mantissa with a negative two- or three-digit exponent.
# repr turns to a positive exponent only from 1e16, so none is admitted, and
# with at most 16 integer digits every admitted literal is finite: text a
# writer did not produce (a hand edit of 1e+400) cannot put a number into the
# response that a browser parses as Infinity; it is decoded instead.
# Possessive and factored so the match never backtracks over a literal: the
# stored text is whatever staff saved, and the unauthenticated read runs it,
# so its cost stays linear in the text whatever it holds.
_FLOAT_EXP = r"e-[0-9]{2,3}"
_FLOAT_REPR = rf"""
    -?(?:
        0\.(?:[0-9]*+(?<=[1-9])|0)                              # 0.5  0.0
      | [1-9](?:
            [0-9]{{1,15}}+\.(?:[0-9]*+(?<=[1-9])|0)              # 12.5  10.0
          | \.(?:[0-9]*+(?<=[1-9])(?:{_FLOAT_EXP})?|0)          # 1.5  1.5e-07  1.0
          | {_FLOAT_EXP}                                        # 1e-05
        )
    )
"""
_STORED_POINT = rf'\{{"x":[ ]{_FLOAT_REPR},[ ]"y":[ ]{_FLOAT_REPR}\}}'
# StepShape.points_json as the writers store it (admin_tours._shape_row and
# the slide import): json.dumps([{"x": float, "y": float}, ...]).
_STORED_POINTS_RE = re.compile(rf"\[{_STORED_POINT}(?:,[ ]{_STORED_POINT})*+\]", re.VERBOSE)


def _stored_points_json(raw) -> Optional[str]:
    """``json_text(decode_points(raw))`` made from the stored text itself, or
    None when ``raw`` is not in the writers' form (empty, differently spaced
    JSON, a BLOB a hand edit left in the column, ...) and has to be decoded.

    In that form the only spaces are the separators' (the only strings are
    the "x" / "y" keys), so dropping them gives the compact rendering. The
    pattern admits only finite literals shaped like a float's repr, and the
    writers' are exactly that — json.dumps wrote them — so decoding and
    re-rendering would write each one again unchanged; this path just skips
    building two dicts per vertex to get there. (A hand-edited literal of that
    shape that json.dumps would not have written, such as 0.00001, passes
    through as it is: the same number, spelled differently.)
    """
    if not isinstance(raw, str) or not raw or not _STORED_POINTS_RE.fullmatch(raw):
        return None
    return raw.replace(" ", "")


def _json_with(obj: Dict, key: str, raw: str) -> str:
    """``json_text(obj)`` with ``raw``, JSON text already, as ``key``'s value."""
    keys = list(obj)
    i = keys.index(key)
    head = json_text({k: obj[k] for k in keys[:i]})[:-1]
    tail = json_text({k: obj[k] for k in keys[i + 1:]})[1:]
    return f"{head}{',' if i else ''}{json_text(key)}:{raw}{',' if tail != '}' else ''}{tail}"


def _step_json(s: Step) -> str:
    """``json_text(step_to_out(s))``, without decoding the stored vertices.

    Every read of a tour, the unauthenticated public one included, renders
    every vertex of every step; splicing the stored text in (see
    _stored_points_json) makes that several times cheaper than decoding it
    into two dicts per vertex and encoding them again, and keeps those
    objects out of memory. A shape whose points are not in the writers' form
    takes the step_to_out path.
    """
    parts: List[str] = []
    plain: List[Dict] = []  # a run of shapes rendered by one json_text call
    for sh in s.shapes:
        points = _stored_points_json(sh.points_json)
        if points is None:
            plain.append(_shape_out(sh, decode_points(sh.points_json)))
            continue
        if plain:
            parts.append(json_text(plain)[1:-1])
            plain = []
        parts.append(_json_with(_shape_out(sh, None), "points", points))
    if plain:
        parts.append(json_text(plain)[1:-1])
    return _json_with(_step_out(s, None), "shapes_l0", f"[{','.join(parts)}]")


def tour_json(db: Session, tour: Dict, steps) -> bytes:
    """The body a JSONResponse would send for the tour reads' envelope,
    ``{"tour": tour, "steps": [step_to_out(s) for s in steps]}``.

    Each step is expired from ``db`` once rendered: that drops its loaded
    pins and shapes (the session's identity map holds them only weakly), so
    a read holds one step's stored rows at a time, not the whole tour's.
    """
    body = [b'{"tour":', json_text(tour).encode(), b',"steps":[']
    for i, s in enumerate(steps):
        if i:
            body.append(b",")
        body.append(_step_json(s).encode())
        db.expire(s)
    body.append(b"]}")
    return b"".join(body)


def normalize_max_zoom_pixel_ratio(raw: object) -> float:
    """Clamp to [1.0, 2.0] in 0.1 increments. Falls back to 1.0 on garbage."""
    try:
        v = float(raw)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 1.0
    # float("nan") parses, slips past both clamps and then crashes round();
    # treat it as garbage. (±inf is fine: the clamps below catch it.)
    if math.isnan(v):
        return 1.0
    if v < 1.0:
        v = 1.0
    elif v > 2.0:
        v = 2.0
    return round(v * 10) / 10


# Hard bounds on the top-page list cap. The lower bound stops an admin from
# typo-ing the field to 0 and emptying the catalogue; the upper bound mirrors
# the /api/slides per_page cap so the API can refuse to honour a request that
# exceeds it. The default (used when the row is missing or has garbage) is
# the same 2000 the model uses, kept here as a literal so callers don't have
# to import the ORM class just to recover from a corrupt value.
MAX_INDEX_ITEMS_MIN = 100
MAX_INDEX_ITEMS_MAX = 5000
MAX_INDEX_ITEMS_DEFAULT = 2000


def normalize_max_index_items(raw: object) -> int:
    """Clamp to [MAX_INDEX_ITEMS_MIN, MAX_INDEX_ITEMS_MAX]. Falls back to default on garbage."""
    try:
        v = int(raw)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return MAX_INDEX_ITEMS_DEFAULT
    if v < MAX_INDEX_ITEMS_MIN:
        return MAX_INDEX_ITEMS_MIN
    if v > MAX_INDEX_ITEMS_MAX:
        return MAX_INDEX_ITEMS_MAX
    return v


# Hard bounds on how many of a slide's tours the top page lists inline under
# that slide in the "All (slides & tours)" view. This is a *display* cap, not
# a fetch cap: the remainder stays one click away behind the card's / row's
# expander, so nothing is hidden — the cap only stops one heavily-toured slide
# from pushing every other slide off the screen.
#
# 0 is deliberately allowed and means "list none by default": the slide still
# shows its tour count and the expander, so the tours are reachable, they just
# don't cost vertical space until asked for. The upper bound is a soft sanity
# limit — past a few dozen rows per slide the expander is the better UI anyway,
# and an unbounded value would reintroduce exactly the layout problem this
# setting exists to solve. Default 3 keeps tile cards compact. Mirrored on the
# client in viewerSettings.ts.
INDEX_TOURS_PER_SLIDE_MIN = 0
INDEX_TOURS_PER_SLIDE_MAX = 50
INDEX_TOURS_PER_SLIDE_DEFAULT = 3


def normalize_index_tours_per_slide(raw: object) -> int:
    """Clamp to [INDEX_TOURS_PER_SLIDE_MIN, INDEX_TOURS_PER_SLIDE_MAX]. Falls back to default on garbage."""
    try:
        v = int(raw)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return INDEX_TOURS_PER_SLIDE_DEFAULT
    if v < INDEX_TOURS_PER_SLIDE_MIN:
        return INDEX_TOURS_PER_SLIDE_MIN
    if v > INDEX_TOURS_PER_SLIDE_MAX:
        return INDEX_TOURS_PER_SLIDE_MAX
    return v


# Hard bounds on OpenSeadragon's in-memory decoded-tile cache (a count of
# tiles, not bytes). One screen at deep zoom takes roughly 50-160 256-px tiles
# on a 1080p monitor but 90-280 on an iPad / Retina display (tiles scale with
# the pixel ratio squared), and only what fits beyond the screen survives a
# pan away and back — so the ceiling has to reach a few HiDPI screens (1000
# kept about 4 iPad screens in frontend/bench/osd-render). It still caps
# worst-case browser RAM: ~0.27 MB per decoded 256-px tile, so 1000 is
# ~270 MB (plus GPU textures under a desktop WebGL drawer), but ~1 GB with
# 512-px SZI tiles, where mobile Safari may terminate the tab. Operators
# who genuinely need more must raise this constant in source and rebuild.
# Default 200 matches OpenSeadragon's own default. Mirrored on the client in
# viewerSettings.ts so the admin form can validate before POST.
MAX_IMAGE_CACHE_COUNT_MIN = 100
MAX_IMAGE_CACHE_COUNT_MAX = 1000
MAX_IMAGE_CACHE_COUNT_DEFAULT = 200


def normalize_max_image_cache_count(raw: object) -> int:
    """Clamp to [MAX_IMAGE_CACHE_COUNT_MIN, MAX_IMAGE_CACHE_COUNT_MAX]. Falls back to default on garbage."""
    try:
        v = int(raw)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return MAX_IMAGE_CACHE_COUNT_DEFAULT
    if v < MAX_IMAGE_CACHE_COUNT_MIN:
        return MAX_IMAGE_CACHE_COUNT_MIN
    if v > MAX_IMAGE_CACHE_COUNT_MAX:
        return MAX_IMAGE_CACHE_COUNT_MAX
    return v


def viewer_settings_to_dict(vs: Optional[ViewerSettings]) -> Dict:
    """The viewer display settings as both the admin GET and the public
    /api/viewer/settings report them. ``None`` (no row yet) reports the
    model's column defaults.

    ``vs`` may be a stand-in whose image columns hold presence flags rather
    than the blobs (see public.get_viewer_settings): only their truthiness is
    read here.
    """
    if vs is None:
        vs = ViewerSettings(**{
            c.key: c.default.arg
            for c in ViewerSettings.__table__.columns
            if c.default is not None and not callable(c.default.arg)
        })
    return {
        "logo_url": viewer_logo_url(vs),
        "favicon_url": viewer_favicon_url(vs),
        "copyright_msg": vs.copyright_msg or "",
        "extra_attrs": parse_attr_list(vs.extra_attrs),
        "index_card_attrs": parse_attr_list(vs.index_card_attrs),
        "index_filter_attrs": parse_attr_list(vs.index_filter_attrs),
        "site_title": vs.site_title or "",
        "footer_text": vs.footer_text or "",
        "show_zoom_indicator": bool(vs.show_zoom_indicator),
        "show_scale_bar": bool(vs.show_scale_bar),
        "show_metadata_panel": bool(vs.show_metadata_panel),
        "default_index_type": normalize_index_type(vs.default_index_type),
        "show_filter_button": bool(vs.show_filter_button),
        "default_view_mode": normalize_view_mode(vs.default_view_mode),
        "default_sort_field": normalize_sort_field(vs.default_sort_field),
        "filter_sort_order": normalize_filter_sort_order(vs.filter_sort_order),
        "max_zoom_pixel_ratio": normalize_max_zoom_pixel_ratio(vs.max_zoom_pixel_ratio),
        "show_admin_link": bool(vs.show_admin_link),
        "default_thumbnail_size": normalize_thumbnail_size(vs.default_thumbnail_size),
        "max_index_items": normalize_max_index_items(vs.max_index_items),
        "index_tours_per_slide": normalize_index_tours_per_slide(vs.index_tours_per_slide),
        "immediate_render": bool(vs.immediate_render),
        "max_image_cache_count": normalize_max_image_cache_count(vs.max_image_cache_count),
        "header_default_image_url": header_default_image_url(vs),
        "header_default_title_color": vs.header_default_title_color or "",
    }
