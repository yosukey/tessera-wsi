"""Admin settings routes: viewer display, network, app-access auth."""
from __future__ import annotations

import hashlib
import json
import os
import re
from typing import List, Optional

from fastapi import (
    APIRouter,
    Depends,
    File,
    Form,
    HTTPException,
    Request,
    UploadFile,
)
from fastapi.responses import Response
from pydantic import TypeAdapter, ValidationError
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from .. import analytics as _analytics
from .. import analytics_hash as _analytics_hash
from .. import app_auth as _app_auth
from .. import cf_access as _cf_access
from .. import network as _net
from .._serializers import (
    ATTR_LIST_MAX_ITEMS,
    header_default_image_url,
    normalize_filter_sort_order,
    normalize_index_tours_per_slide,
    normalize_index_type,
    normalize_max_image_cache_count,
    normalize_max_index_items,
    normalize_max_zoom_pixel_ratio,
    normalize_sort_field,
    normalize_thumbnail_size,
    normalize_view_mode,
    seasonal_background_to_dict,
    viewer_favicon_url,
    viewer_logo_url,
    viewer_settings_to_dict,
)
from ..admin_helpers import (
    ATTR_KEY_MAX_LEN,
    CTRL_CHAR_RE,
    FAVICON_EXT_TO_MIMETYPE,
    HEADER_IMAGE_EXTS,
    LOGO_EXT_TO_MIMETYPE,
    api_error,
    audit,
    detect_animated_image_mimetype,
    detect_favicon_mimetype,
    detect_logo_mimetype,
    get_or_create_singleton,
)
from ..auth import hash_password, require_admin, validate_password_strength
from ..db import get_db
from ..models import (
    AuthSettings,
    AuthUser,
    NetworkSettings,
    SeasonalBackground,
    User,
    ViewerSettings,
    auth_username_key,
)
from ..schemas import IpRuleIn

router = APIRouter()


# ===== Admin: viewer settings =====


# Strict 6-digit hex color used for the always-on / per-slot title text color.
# Empty string is also accepted on input and means "no override (use CSS default)".
_HEX_COLOR_RE = re.compile(r"^#[0-9A-Fa-f]{6}\Z")


def _normalize_title_color(raw: str) -> str:
    """Return ``"#rrggbb"`` (lowercased) for a valid hex value, ``""`` for empty.

    Raises ``HTTPException(422)`` for anything else. Used by every endpoint
    that accepts a title color so the DB never holds malformed CSS that the
    frontend would silently render as black.
    """
    cleaned = (raw or "").strip()
    if not cleaned:
        return ""
    if not _HEX_COLOR_RE.match(cleaned):
        raise HTTPException(
            status_code=422,
            detail="title_color must be a 6-digit hex like #1a2b3c, or empty",
        )
    return cleaned.lower()


# C0/DEL control characters. Single-line free-text fields drop them all;
# the multiline footer keeps newlines/tabs (\x09-\x0d) but strips the rest.
_SINGLE_LINE_CTRL_RE = re.compile(r"[\x00-\x1f\x7f]")
_MULTILINE_CTRL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


def _clean_free_text(raw: Optional[str], *, max_len: int, multiline: bool = False) -> str:
    """Strip control characters and cap length for an admin free-text setting.

    SQLite (the default backend) does not enforce ``String(N)`` column widths,
    so an unbounded value would persist verbatim and ship to every public
    client via /api/viewer/settings. Cap to the column width and drop control
    characters (mirroring the username / metadata hygiene elsewhere) so the
    stored value can't corrupt the rendered header / footer / tab title.
    """
    cleaned = raw or ""
    cleaned = (_MULTILINE_CTRL_RE if multiline else _SINGLE_LINE_CTRL_RE).sub("", cleaned)
    return cleaned[:max_len]


def _clean_attr_list(raw: str, *, field: str) -> str:
    """The JSON to store for an attribute-key list sent to the viewer PATCH
    (extra_attrs / index_card_attrs / index_filter_attrs).

    Lenient about shape, like the read side (_serializers.parse_attr_list):
    malformed JSON stores an empty list and non-string items are dropped.
    Each key gets the free-text hygiene (control characters stripped, then
    surrounding whitespace) and empties and duplicates go; a key longer than
    an attribute key can be, or more keys than ATTR_LIST_MAX_ITEMS, is refused
    rather than cut, since a truncated key names a different attribute.
    """
    try:
        parsed = json.loads(raw or "[]")
    except ValueError:
        parsed = []
    out: List[str] = []
    for item in parsed if isinstance(parsed, list) else []:
        if not isinstance(item, str):
            continue
        key = _SINGLE_LINE_CTRL_RE.sub("", item).strip()
        if not key or key in out:
            continue
        if len(key) > ATTR_KEY_MAX_LEN:
            raise HTTPException(
                status_code=422,
                detail=f"{field}: each key must be at most {ATTR_KEY_MAX_LEN} characters",
            )
        out.append(key)
    if len(out) > ATTR_LIST_MAX_ITEMS:
        raise HTTPException(
            status_code=422,
            detail=f"{field}: at most {ATTR_LIST_MAX_ITEMS} keys",
        )
    return json.dumps(out)


@router.get("/api/admin/viewer/settings")
def admin_get_viewer_settings(admin: User = Depends(require_admin), db: Session = Depends(get_db)):
    return viewer_settings_to_dict(get_or_create_singleton(db, ViewerSettings))


def _parse_form_bool(raw: Optional[str]) -> bool:
    """Truthiness of a form flag / boolean field ("1", "true", "yes", "on")."""
    return (raw or "").strip().lower() in ("1", "true", "yes", "on")


@router.patch("/api/admin/viewer/settings")
def admin_update_viewer_settings(
    copyright_msg: Optional[str] = Form(None),
    extra_attrs: Optional[str] = Form(None),
    index_card_attrs: Optional[str] = Form(None),
    index_filter_attrs: Optional[str] = Form(None),
    site_title: Optional[str] = Form(None),
    footer_text: Optional[str] = Form(None),
    show_zoom_indicator: Optional[str] = Form(None),
    show_scale_bar: Optional[str] = Form(None),
    show_metadata_panel: Optional[str] = Form(None),
    default_index_type: Optional[str] = Form(None),
    show_filter_button: Optional[str] = Form(None),
    default_view_mode: Optional[str] = Form(None),
    default_sort_field: Optional[str] = Form(None),
    filter_sort_order: Optional[str] = Form(None),
    max_zoom_pixel_ratio: Optional[str] = Form(None),
    show_admin_link: Optional[str] = Form(None),
    default_thumbnail_size: Optional[str] = Form(None),
    max_index_items: Optional[str] = Form(None),
    index_tours_per_slide: Optional[str] = Form(None),
    immediate_render: Optional[str] = Form(None),
    max_image_cache_count: Optional[str] = Form(None),
    header_default_title_color: Optional[str] = Form(None),
    # FastAPI/httpx round-trip empty form values to the Form default, so an
    # empty "header_default_title_color=" string is indistinguishable from
    # "field not sent". To clear the color, the caller passes
    # clear_header_default_title_color=1; this mirrors the
    # auth_settings.clear_ldap_bind_password / clear_google_client_secret
    # pattern.
    clear_header_default_title_color: Optional[str] = Form(None),
    # Same FastAPI quirk applies to the free-text fields below: an empty
    # "copyright_msg=" coerces to the Form(None) default, so the is-not-None
    # guard skips it and the stored value can never be cleared by emptying the
    # field. A companion clear_<field>=1 flag makes the clear explicit.
    clear_copyright_msg: Optional[str] = Form(None),
    clear_site_title: Optional[str] = Form(None),
    clear_footer_text: Optional[str] = Form(None),
    admin: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    # True PATCH semantics: only fields that the caller actually sent are
    # written. Each `is not None` guard skips columns the client didn't
    # include, so a partial update can't silently reset other settings to
    # their Form() defaults.
    vs = get_or_create_singleton(db, ViewerSettings)
    if _parse_form_bool(clear_copyright_msg or ""):
        vs.copyright_msg = ""
    elif copyright_msg is not None:
        # Column widths mirrored from models.ViewerSettings.
        vs.copyright_msg = _clean_free_text(copyright_msg, max_len=512)
    if extra_attrs is not None:
        vs.extra_attrs = _clean_attr_list(extra_attrs, field="extra_attrs")
    if index_card_attrs is not None:
        vs.index_card_attrs = _clean_attr_list(index_card_attrs, field="index_card_attrs")
    if index_filter_attrs is not None:
        vs.index_filter_attrs = _clean_attr_list(index_filter_attrs, field="index_filter_attrs")
    if _parse_form_bool(clear_site_title or ""):
        vs.site_title = ""
    elif site_title is not None:
        vs.site_title = _clean_free_text(site_title, max_len=256)
    if _parse_form_bool(clear_footer_text or ""):
        vs.footer_text = ""
    elif footer_text is not None:
        vs.footer_text = _clean_free_text(footer_text, max_len=1024, multiline=True)
    if show_zoom_indicator is not None:
        vs.show_zoom_indicator = _parse_form_bool(show_zoom_indicator)
    if show_scale_bar is not None:
        vs.show_scale_bar = _parse_form_bool(show_scale_bar)
    if show_metadata_panel is not None:
        vs.show_metadata_panel = _parse_form_bool(show_metadata_panel)
    if default_index_type is not None:
        vs.default_index_type = normalize_index_type(default_index_type)
    if show_filter_button is not None:
        vs.show_filter_button = _parse_form_bool(show_filter_button)
    if default_view_mode is not None:
        vs.default_view_mode = normalize_view_mode(default_view_mode)
    if default_sort_field is not None:
        vs.default_sort_field = normalize_sort_field(default_sort_field)
    if filter_sort_order is not None:
        vs.filter_sort_order = normalize_filter_sort_order(filter_sort_order)
    if max_zoom_pixel_ratio is not None:
        vs.max_zoom_pixel_ratio = normalize_max_zoom_pixel_ratio(max_zoom_pixel_ratio)
    if show_admin_link is not None:
        vs.show_admin_link = _parse_form_bool(show_admin_link)
    if default_thumbnail_size is not None:
        vs.default_thumbnail_size = normalize_thumbnail_size(default_thumbnail_size)
    if max_index_items is not None:
        vs.max_index_items = normalize_max_index_items(max_index_items)
    if index_tours_per_slide is not None:
        vs.index_tours_per_slide = normalize_index_tours_per_slide(index_tours_per_slide)
    if immediate_render is not None:
        vs.immediate_render = _parse_form_bool(immediate_render)
    if max_image_cache_count is not None:
        vs.max_image_cache_count = normalize_max_image_cache_count(max_image_cache_count)
    if _parse_form_bool(clear_header_default_title_color or ""):
        vs.header_default_title_color = ""
    elif (header_default_title_color or "").strip():
        # Test the *stripped* value: a whitespace-only field is truthy, and
        # _normalize_title_color maps it to "" — so without the strip it
        # would clear the stored color while bypassing the clear_ flag that owns
        # that transition. Blank means "no change", as it does everywhere
        # else in this handler.
        vs.header_default_title_color = _normalize_title_color(header_default_title_color)
    db.commit()
    # Audit the field *names* the caller sent (values are display settings,
    # not secrets, but names keep the record compact) — same `fields=`
    # convention as tour.update.
    sent = [
        name for name, value in (
            ("copyright_msg", copyright_msg),
            ("extra_attrs", extra_attrs),
            ("index_card_attrs", index_card_attrs),
            ("index_filter_attrs", index_filter_attrs),
            ("site_title", site_title),
            ("footer_text", footer_text),
            ("show_zoom_indicator", show_zoom_indicator),
            ("show_scale_bar", show_scale_bar),
            ("show_metadata_panel", show_metadata_panel),
            ("default_index_type", default_index_type),
            ("show_filter_button", show_filter_button),
            ("default_view_mode", default_view_mode),
            ("default_sort_field", default_sort_field),
            ("filter_sort_order", filter_sort_order),
            ("max_zoom_pixel_ratio", max_zoom_pixel_ratio),
            ("show_admin_link", show_admin_link),
            ("default_thumbnail_size", default_thumbnail_size),
            ("max_index_items", max_index_items),
            ("index_tours_per_slide", index_tours_per_slide),
            ("immediate_render", immediate_render),
            ("max_image_cache_count", max_image_cache_count),
            ("header_default_title_color", header_default_title_color),
            ("clear_header_default_title_color", clear_header_default_title_color),
            ("clear_copyright_msg", clear_copyright_msg),
            ("clear_site_title", clear_site_title),
            ("clear_footer_text", clear_footer_text),
        ) if value is not None
    ]
    audit(admin, "viewer.settings.update", fields=sent)
    return {"ok": True}


_LOGO_MAX_BYTES = 2 * 1024 * 1024  # 2 MB; logos are typically ~tens of KB.


@router.post("/api/admin/viewer/settings/logo")
def admin_upload_viewer_logo(
    file: UploadFile = File(...),
    admin: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    ext = os.path.splitext(file.filename or "")[1].lower()
    if ext not in LOGO_EXT_TO_MIMETYPE:
        raise HTTPException(status_code=400, detail="Unsupported image format. Allowed: png, jpg, jpeg, webp, gif")
    data = file.file.read(_LOGO_MAX_BYTES + 1)
    if len(data) > _LOGO_MAX_BYTES:
        raise HTTPException(status_code=413, detail=f"Logo too large (max {_LOGO_MAX_BYTES // 1024} KB)")
    # Magic-byte check, matching the favicon upload. The logo is served
    # same-origin from /api/viewer/logo with the stored mimetype, so trusting
    # the extension alone would let a renamed payload through as a content-
    # injection foothold. SVG is intentionally unsupported (absent from
    # LOGO_EXT_TO_MIMETYPE): it can carry <script> / on*-handlers and there is
    # no HTML/XML sanitizer wired up — same rationale as detect_favicon_mimetype.
    detected = detect_logo_mimetype(data)
    expected = LOGO_EXT_TO_MIMETYPE[ext]
    if detected is None or detected != expected:
        raise HTTPException(
            status_code=400,
            detail="File contents do not match the claimed image format",
        )
    vs = get_or_create_singleton(db, ViewerSettings)
    vs.logo_data = data
    vs.logo_mimetype = expected
    vs.logo_etag = hashlib.sha1(data).hexdigest()[:16]
    db.commit()
    audit(admin, "viewer.logo.upload", size=len(data), mimetype=expected)
    return {"ok": True, "logo_url": viewer_logo_url(vs)}


@router.delete("/api/admin/viewer/settings/logo")
def admin_delete_viewer_logo(
    admin: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    vs = db.get(ViewerSettings, 1)
    if vs and vs.logo_data is not None:
        vs.logo_data = None
        vs.logo_mimetype = ""
        vs.logo_etag = ""
        db.commit()
        audit(admin, "viewer.logo.delete")
    return {"ok": True}


_FAVICON_MAX_BYTES = 512 * 1024  # 512 KB


@router.post("/api/admin/viewer/settings/favicon")
def admin_upload_viewer_favicon(
    file: UploadFile = File(...),
    admin: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    ext = os.path.splitext(file.filename or "")[1].lower()
    if ext not in FAVICON_EXT_TO_MIMETYPE:
        raise HTTPException(status_code=400, detail="Unsupported format. Allowed: ico, png")
    data = file.file.read(_FAVICON_MAX_BYTES + 1)
    if len(data) > _FAVICON_MAX_BYTES:
        raise HTTPException(status_code=413, detail=f"Favicon too large (max {_FAVICON_MAX_BYTES // 1024} KB)")
    # Magic-byte check: a .ico / .png suffix only proves the operator's
    # intent. Without sniffing the actual bytes, a renamed HTML/JS/SVG
    # payload would reach the unauthenticated /favicon.ico endpoint with
    # whatever Content-Type the extension map dictated — a weak but real
    # foothold for content-injection. Enforce that the bytes match the
    # claimed format and that the resolved mimetype agrees with the
    # extension (so foo.ico carrying PNG magic is also rejected).
    detected = detect_favicon_mimetype(data)
    expected = FAVICON_EXT_TO_MIMETYPE[ext]
    if detected is None or detected != expected:
        raise HTTPException(status_code=400, detail="File contents do not match the .ico/.png format")
    vs = get_or_create_singleton(db, ViewerSettings)
    vs.favicon_data = data
    vs.favicon_mimetype = expected
    vs.favicon_etag = hashlib.sha1(data).hexdigest()[:16]
    db.commit()
    audit(admin, "viewer.favicon.upload", size=len(data), mimetype=expected)
    return {"ok": True, "favicon_url": viewer_favicon_url(vs)}


@router.delete("/api/admin/viewer/settings/favicon")
def admin_delete_viewer_favicon(
    admin: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    vs = db.get(ViewerSettings, 1)
    if vs and vs.favicon_data is not None:
        vs.favicon_data = None
        vs.favicon_mimetype = ""
        vs.favicon_etag = ""
        db.commit()
        audit(admin, "viewer.favicon.delete")
    return {"ok": True}


# ===== Admin: always-on (default) header background =====


_HEADER_DEFAULT_MAX_BYTES = 2 * 1024 * 1024  # 2 MB; matches seasonal cap.


def _upload_header_image_bytes(file: UploadFile, max_bytes: int) -> tuple[bytes, str]:
    """Read+validate an uploaded PNG/APNG/GIF, returning (data, mimetype).

    Shared by the always-on header background and per-slot seasonal upload
    handlers below so format/size rules are identical.
    """
    ext = os.path.splitext(file.filename or "")[1].lower()
    if ext and ext not in HEADER_IMAGE_EXTS:
        raise HTTPException(
            status_code=400,
            detail="Unsupported format. Allowed: png, apng, gif",
        )
    data = file.file.read(max_bytes + 1)
    if len(data) > max_bytes:
        raise HTTPException(
            status_code=413,
            detail=f"Image too large (max {max_bytes // 1024} KB)",
        )
    detected = detect_animated_image_mimetype(data)
    if detected is None:
        raise HTTPException(
            status_code=400,
            detail="File is not a valid PNG/APNG or GIF image",
        )
    return data, detected


@router.post("/api/admin/viewer/settings/header-default-background")
def admin_upload_header_default_background(
    file: UploadFile = File(...),
    admin: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    data, mimetype = _upload_header_image_bytes(file, _HEADER_DEFAULT_MAX_BYTES)
    vs = get_or_create_singleton(db, ViewerSettings)
    vs.header_default_image_data = data
    vs.header_default_image_mimetype = mimetype
    vs.header_default_image_etag = hashlib.sha1(data).hexdigest()[:16]
    db.commit()
    audit(admin, "viewer.header_background.upload", size=len(data), mimetype=mimetype)
    return {"ok": True, "header_default_image_url": header_default_image_url(vs)}


@router.delete("/api/admin/viewer/settings/header-default-background")
def admin_delete_header_default_background(
    admin: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    vs = db.get(ViewerSettings, 1)
    if vs and vs.header_default_image_data is not None:
        vs.header_default_image_data = None
        vs.header_default_image_mimetype = ""
        vs.header_default_image_etag = ""
        db.commit()
        audit(admin, "viewer.header_background.delete")
    return {"ok": True}


@router.post("/api/admin/viewer/header-default/reset")
def admin_reset_header_default(
    admin: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    """Clear the always-on header image AND title color in one transaction.

    Saves the frontend from chaining DELETE-image + PATCH-color="" calls
    (and from inconsistent half-applied state if the second call fails).
    """
    vs = get_or_create_singleton(db, ViewerSettings)
    vs.header_default_image_data = None
    vs.header_default_image_mimetype = ""
    vs.header_default_image_etag = ""
    vs.header_default_title_color = ""
    db.commit()
    audit(admin, "viewer.header_background.reset")
    return {"ok": True}


# ===== Admin: seasonal backgrounds (up to 5 date-range images) =====


_SEASONAL_BG_MAX_BYTES = 2 * 1024 * 1024  # 2 MB; tiled images should be small.
_SEASONAL_BG_SLOTS = (1, 2, 3, 4, 5)


def _check_slot(slot: int) -> None:
    if slot not in _SEASONAL_BG_SLOTS:
        raise HTTPException(status_code=404, detail="Unknown background slot")


def _get_or_create_slot(db: Session, slot: int) -> SeasonalBackground:
    # Race-safe under multiple workers (SAVEPOINT + re-read); a bare
    # get/add/flush here would 500 the loser on the PK when two requests
    # first-create the same slot concurrently.
    return get_or_create_singleton(db, SeasonalBackground, slot)


def _validate_month_day(month: int, day: int) -> tuple[int, int]:
    if not (1 <= month <= 12):
        raise HTTPException(status_code=422, detail="month must be between 1 and 12")
    # Cap to days-in-month using a non-leap-year baseline so Feb gets 28,
    # which is the strictest a recurring annual range can rely on.
    days_in_month = [31, 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31][month - 1]
    if not (1 <= day <= days_in_month):
        raise HTTPException(status_code=422, detail=f"day must be between 1 and {days_in_month} for month {month}")
    return month, day


@router.get("/api/admin/viewer/seasonal-backgrounds")
def admin_list_seasonal_backgrounds(
    admin: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    rows = db.execute(
        select(SeasonalBackground).order_by(SeasonalBackground.id.asc())
    ).scalars().all()
    by_id = {r.id: r for r in rows}
    out = []
    for slot in _SEASONAL_BG_SLOTS:
        sb = by_id.get(slot) or SeasonalBackground(id=slot)
        out.append(seasonal_background_to_dict(sb, admin=True))
    return out


def _date_ordinal(month: int, day: int) -> int:
    """Pack (month, day) into a comparable integer (Jan 1 = 101, Dec 31 = 1231)."""
    return month * 100 + day


@router.patch("/api/admin/viewer/seasonal-backgrounds/{slot}")
def admin_update_seasonal_background(
    slot: int,
    enabled: Optional[str] = Form(None),
    label: Optional[str] = Form(None),
    start_month: Optional[int] = Form(None),
    start_day: Optional[int] = Form(None),
    end_month: Optional[int] = Form(None),
    end_day: Optional[int] = Form(None),
    title_color: Optional[str] = Form(None),
    # See PATCH /viewer/settings: FastAPI maps an empty form value to the
    # parameter's Form default, so an explicit "clear" flag is required to
    # tell "leave alone" apart from "set to empty".
    clear_title_color: Optional[str] = Form(None),
    # Same quirk applies to the label: an empty "label=" coerces to None and
    # the is-not-None guard skips it, so the stored label can never be cleared
    # by emptying the field without this companion flag.
    clear_label: Optional[str] = Form(None),
    admin: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    _check_slot(slot)
    sb = _get_or_create_slot(db, slot)
    if enabled is not None:
        sb.enabled = _parse_form_bool(enabled)
    if _parse_form_bool(clear_label or ""):
        sb.label = ""
    elif label is not None:
        # Same hygiene as every other admin free-text setting: the label ships
        # to every public client through seasonal_background_to_dict (and into
        # the audit record), so control characters must not survive the round
        # trip. A bare [:128] slice would cap the length but let them through.
        sb.label = _clean_free_text(label, max_len=128)
    new_start_m = start_month if start_month is not None else sb.start_month
    new_start_d = start_day if start_day is not None else sb.start_day
    new_end_m = end_month if end_month is not None else sb.end_month
    new_end_d = end_day if end_day is not None else sb.end_day
    new_start_m, new_start_d = _validate_month_day(new_start_m, new_start_d)
    new_end_m, new_end_d = _validate_month_day(new_end_m, new_end_d)
    # Reject year-wrap: callers split such windows into two slots
    # (e.g. 12/20-12/31 + 1/1-1/5). Simplifies precedence + range
    # rendering by making every active window a contiguous interval.
    if _date_ordinal(new_start_m, new_start_d) > _date_ordinal(new_end_m, new_end_d):
        raise HTTPException(
            status_code=422,
            detail=(
                "start date must be on or before end date in the same calendar year; "
                "split year-wrapping windows into two slots"
            ),
        )
    sb.start_month, sb.start_day = new_start_m, new_start_d
    sb.end_month, sb.end_day = new_end_m, new_end_d
    if _parse_form_bool(clear_title_color or ""):
        sb.title_color = ""
    elif (title_color or "").strip():
        # Stripped for the same reason as header_default_title_color above:
        # a whitespace-only value is truthy but normalizes to "", which would
        # clear the stored color without the clear_ flag that owns that
        # transition.
        sb.title_color = _normalize_title_color(title_color)
    db.commit()
    audit(admin, "viewer.seasonal_background.update", target=str(slot))
    return seasonal_background_to_dict(sb, admin=True)


@router.post("/api/admin/viewer/seasonal-backgrounds/{slot}/image")
def admin_upload_seasonal_background_image(
    slot: int,
    file: UploadFile = File(...),
    admin: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    _check_slot(slot)
    data, mimetype = _upload_header_image_bytes(file, _SEASONAL_BG_MAX_BYTES)
    sb = _get_or_create_slot(db, slot)
    sb.image_data = data
    sb.image_mimetype = mimetype
    sb.image_etag = hashlib.sha1(data).hexdigest()[:16]
    db.commit()
    audit(admin, "viewer.seasonal_background.image_upload", target=str(slot),
          size=len(data), mimetype=mimetype)
    return seasonal_background_to_dict(sb, admin=True)


@router.get("/api/admin/viewer/seasonal-backgrounds/{slot}/image")
def admin_get_seasonal_background_image(
    slot: int,
    request: Request,
    admin: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    """Admin counterpart to ``/api/viewer/seasonal-background/{slot}`` that
    ignores ``enabled``: the dashboard previews a slot's image before the slot
    is enabled, and the public route 404s a disabled slot so a pulled image is
    not fetchable by anyone enumerating slots."""
    _check_slot(slot)
    sb = db.get(SeasonalBackground, slot)
    if not sb or not sb.image_data:
        raise HTTPException(status_code=404, detail="No background")
    etag = '"' + (sb.image_etag or hashlib.sha1(sb.image_data).hexdigest()[:16]) + '"'
    if request.headers.get("if-none-match") == etag:
        return Response(status_code=304, headers={"ETag": etag, "Cache-Control": "private, max-age=300"})
    return Response(
        content=sb.image_data,
        media_type=sb.image_mimetype or "image/png",
        headers={"ETag": etag, "Cache-Control": "private, max-age=300"},
    )


@router.delete("/api/admin/viewer/seasonal-backgrounds/{slot}/image")
def admin_delete_seasonal_background_image(
    slot: int,
    admin: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    _check_slot(slot)
    sb = db.get(SeasonalBackground, slot)
    if sb and sb.image_data is not None:
        sb.image_data = None
        sb.image_mimetype = ""
        sb.image_etag = ""
        db.commit()
        audit(admin, "viewer.seasonal_background.image_delete", target=str(slot))
    return {"ok": True}


@router.post("/api/admin/viewer/seasonal-backgrounds/{slot}/reset")
def admin_reset_seasonal_background(
    slot: int,
    admin: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    """Return a slot to factory state: disabled, no image, no color, dates=1/1.

    The row itself is preserved (slot rows are fixed id=1..5); only the
    mutable columns are cleared. One round-trip vs. PATCH + DELETE-image.
    """
    _check_slot(slot)
    sb = _get_or_create_slot(db, slot)
    sb.enabled = False
    sb.label = ""
    sb.start_month = 1
    sb.start_day = 1
    sb.end_month = 1
    sb.end_day = 1
    sb.image_data = None
    sb.image_mimetype = ""
    sb.image_etag = ""
    sb.title_color = ""
    db.commit()
    audit(admin, "viewer.seasonal_background.reset", target=str(slot))
    return seasonal_background_to_dict(sb, admin=True)


# ===== Admin: network settings =====


# A measurement ID looks like "G-XXXXXXXXXX" (GA4). UA-/GTM- IDs are
# explicitly *not* accepted because the viewer's gtag wiring in
# pages/App.tsx only supports the GA4 tag URL.
_GA_MEASUREMENT_ID_RE = re.compile(r"^G-[A-Z0-9]{4,32}\Z")


_VALID_ANALYTICS_MODES = _analytics.VALID_ANALYTICS_MODES


def _network_settings_to_dict(ns: NetworkSettings) -> dict:
    try:
        rules = json.loads(ns.ip_rules or "[]")
        if not isinstance(rules, list):
            rules = []
    except Exception:
        rules = []
    return {
        "ip_filter_enabled": ns.ip_filter_enabled,
        "ip_filter_default": ns.ip_filter_default or "allow",
        "ip_rules": rules,
        "mdns_enabled": ns.mdns_enabled,
        "mdns_active": _net.mdns_active(),
        "ga_measurement_id": ns.ga_measurement_id or "",
        "analytics_mode": _analytics.normalize_analytics_mode(ns.analytics_mode),
        "analytics_notice_toast_enabled": bool(ns.analytics_notice_toast_enabled),
        # The salt itself is never returned; the UI only needs to know
        # whether selecting "hashed" is safe, so a single boolean is
        # surfaced. The PATCH handler enforces the same check server-side.
        "analytics_hash_salt_available": _analytics_hash.salt_available(),
    }


@router.get("/api/admin/network/settings")
def admin_get_network_settings(
    admin: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    ns = get_or_create_singleton(db, NetworkSettings)
    return _network_settings_to_dict(ns)


@router.patch("/api/admin/network/settings")
def admin_update_network_settings(
    request: Request,
    ip_filter_enabled: Optional[str] = Form(None),
    ip_filter_default: Optional[str] = Form(None),
    ip_rules: Optional[str] = Form(None),
    mdns_enabled: Optional[str] = Form(None),
    ga_measurement_id: Optional[str] = Form(None),
    # FastAPI coerces an empty form value to the Form(None) default, so
    # "ga_measurement_id=" is indistinguishable from not sending the field and
    # a stored ID could never be removed (GA kept loading on every public
    # page). clear_ga_measurement_id=1 makes the clear explicit, like the
    # clear_<field> flags on the viewer / auth settings PATCHes.
    clear_ga_measurement_id: Optional[str] = Form(None),
    analytics_mode: Optional[str] = Form(None),
    analytics_notice_toast_enabled: Optional[str] = Form(None),
    # Override for the self-lockout guard below. The guard refuses (422) an
    # IP-filter change that would block the requesting admin's own resolved
    # client IP at the outer middleware (recovery would be loopback-only);
    # setting this flag saves the change anyway for the operator who knows
    # they'll reconnect from an allowed IP.
    override_ip_lockout: Optional[str] = Form(None),
    admin: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    # PATCH semantics: every field is optional and only those actually present
    # in the body are written. These fields default to None and every write is
    # gated on ``is not None`` — so a partial update (e.g. submitting only
    # analytics_mode) leaves the IP allowlist, the filter toggle, and the GA id
    # untouched rather than wiping them. This matches the other settings PATCHes
    # (admin_update_viewer_settings / admin_update_auth_settings), which
    # likewise never reset unrelated fields to their Form() defaults on a
    # partial update.

    # Validate ip_rules up front (may raise 422) only when it is provided.
    validated_rules: Optional[list[dict]] = None
    if ip_rules is not None:
        try:
            raw_rules = json.loads(ip_rules)
        except json.JSONDecodeError as exc:
            raise HTTPException(status_code=422, detail=f"ip_rules must be valid JSON: {exc.msg}")
        if not isinstance(raw_rules, list):
            raise HTTPException(status_code=422, detail="ip_rules must be a JSON array")
        try:
            parsed = TypeAdapter(List[IpRuleIn]).validate_python(raw_rules)
        except ValidationError as exc:
            # ``ctx`` carries the raw ValueError from IpRuleIn's validator,
            # which the JSON response can't serialise (the 422 would become a
            # 500); drop it and the doc URL, keeping loc/msg/type/input.
            raise HTTPException(
                status_code=422,
                detail=exc.errors(include_url=False, include_context=False),
            )
        # Dedupe by canonical IP. IpRuleIn._ip_or_cidr already canonicalizes via
        # ipaddress.ip_network(strict=False), so two host-bit-equivalent CIDRs
        # like "192.168.1.0/24" and "192.168.1.5/24" both collapse to the same
        # parsed.ip here. Without this guard, the client's best-effort string
        # dedupe lets duplicates through and they end up stored verbatim,
        # producing dead rules after the first match.
        seen: set[str] = set()
        validated_rules = []
        for r in parsed:
            if r.ip in seen:
                continue
            seen.add(r.ip)
            validated_rules.append(r.model_dump())

    # Reject UA-XXXX / GTM-XXXX or other obvious typos here; the viewer's
    # gtag wiring only handles G- (GA4) IDs.
    ga_clean: Optional[str] = None
    if _parse_form_bool(clear_ga_measurement_id or ""):
        ga_clean = ""
    elif ga_measurement_id is not None:
        ga_clean = ga_measurement_id.strip()[:64]
        if ga_clean and not _GA_MEASUREMENT_ID_RE.match(ga_clean):
            raise HTTPException(
                status_code=422,
                detail=(
                    "ga_measurement_id must look like G-XXXXXXXXXX "
                    "(GA4 measurement ID); UA-/GTM- IDs are not supported."
                ),
            )

    ns = get_or_create_singleton(db, NetworkSettings)
    _ga_before = ns.ga_measurement_id or ""
    if ip_filter_enabled is not None:
        ns.ip_filter_enabled = _parse_form_bool(ip_filter_enabled)
    if ip_filter_default is not None:
        # Reject out-of-range values with a 422 rather than silently coercing
        # to "allow": a typo like "deby" would otherwise fail *open* (drop the
        # deny default) instead of failing loudly, and this matches how
        # analytics_mode / lan_direct_noauth_mode validate their enums below.
        if ip_filter_default not in ("allow", "deny"):
            raise HTTPException(
                status_code=422,
                detail='ip_filter_default must be "allow" or "deny"',
            )
        ns.ip_filter_default = ip_filter_default
    if validated_rules is not None:
        ns.ip_rules = json.dumps(validated_rules)

    if ga_clean is not None:
        ns.ga_measurement_id = ga_clean

    if analytics_mode is not None:
        mode_clean = (analytics_mode or "").strip().lower()
        if mode_clean not in _VALID_ANALYTICS_MODES:
            raise HTTPException(
                status_code=422,
                detail=f"analytics_mode must be one of {list(_VALID_ANALYTICS_MODES)}",
            )
        # Reject only a *transition* into "hashed" without a salt. Defence in
        # depth against a hand-crafted PATCH flipping the row into a state the
        # writer cannot honour (the UI already disables the radio). Keeping an
        # already-"hashed" value must stay saveable so an unrelated change
        # (e.g. the GA measurement ID) isn't blocked when the salt was removed
        # out-of-band; the beacon writer fails that case safe by recording
        # rows anonymously (see analytics.resolve_identity).
        current_mode = (ns.analytics_mode or "off").strip().lower()
        if mode_clean == "hashed" and current_mode != "hashed" and not _analytics_hash.salt_available():
            raise HTTPException(
                status_code=422,
                detail=(
                    "Cannot enable pseudonymous (hashed) analytics: "
                    "ANALYTICS_HASH_SALT is not set (or is shorter than 32 "
                    "characters, which is treated as unset). Add a value to "
                    "/etc/wsi/wsi.env (generate one with "
                    "`openssl rand -hex 32`) and restart."
                ),
            )
        ns.analytics_mode = mode_clean
    if analytics_notice_toast_enabled is not None:
        ns.analytics_notice_toast_enabled = _parse_form_bool(analytics_notice_toast_enabled)

    # Self-lockout guard. Only when this request actually changed an IP-filter
    # field (enable/default/rules) AND the resulting state has the filter on:
    # evaluate the *proposed* rule set against the requesting admin's own
    # resolved client IP the same way the outer ip_filter middleware will. If
    # it would be blocked, refuse with 422 (recovery would otherwise be
    # loopback-only) unless the operator passed override_ip_lockout. Runs
    # before the mdns side effect below so a rejected save leaves no on-disk
    # avahi change. A partial PATCH that doesn't touch the IP filter is never
    # second-guessed, so pre-existing config can't start 422-ing unrelated
    # updates.
    ip_filter_touched = (
        ip_filter_enabled is not None
        or ip_filter_default is not None
        or ip_rules is not None
    )
    if ip_filter_touched and bool(ns.ip_filter_enabled):
        try:
            final_rules = json.loads(ns.ip_rules or "[]")
            if not isinstance(final_rules, list):
                final_rules = []
        except (json.JSONDecodeError, TypeError):
            final_rules = []
        client_host = request.client.host if request.client else "127.0.0.1"
        my_ip = _net.get_client_ip(request.headers, client_host)
        if not _net.rules_allow_ip(
            my_ip, True, ns.ip_filter_default or "allow", final_rules
        ) and not _parse_form_bool(override_ip_lockout or ""):
            # A translatable {code, params} detail; the dashboard recognises
            # the code, asks, and resends with override_ip_lockout=1.
            raise api_error(422, "adminDash.apiErr.ipFilterSelfLockout", ip=my_ip)

    # mDNS reconciliation is a *filesystem* side effect (writes / removes the
    # avahi service file), so it runs only AFTER every validation above that
    # can raise 422 — otherwise a request that flips mDNS on but carries an
    # invalid analytics_mode / ip_filter_default would leave avahi advertising
    # while the rejected PATCH never commits (DB says disabled, host keeps
    # broadcasting). Reconcile every time mDNS is requested on, not only on
    # toggle change, so a transient apply_mdns failure (e.g. /etc/avahi/services
    # not yet writable) is retried on the next save instead of leaving the DB
    # stuck at enabled=True with no service file. apply_mdns is idempotent
    # (open(..., "w") + os.remove if missing).
    mdns_ok = True
    mdns_msg = ""
    if mdns_enabled is not None:
        desired_state = _parse_form_bool(mdns_enabled)
        actual_state = _net.mdns_active()
        if desired_state != actual_state or bool(ns.mdns_enabled) != desired_state:
            mdns_ok, mdns_msg = _net.apply_mdns(desired_state)
        # Only persist mdns_enabled when the on-disk state matches what the
        # operator asked for, so the dashboard's toggle reflects reality.
        if mdns_ok:
            ns.mdns_enabled = desired_state

    db.commit()
    # This worker gates on the new rules at once; the others (and wsi-sync)
    # pick them up on their next periodic refresh (main._settings_refresh_loop).
    _net.refresh(db)

    # Audit the persisted state (post-update) rather than the request fields,
    # since a partial PATCH may have left some settings untouched.
    try:
        stored_rule_count = len(json.loads(ns.ip_rules or "[]"))
    except (json.JSONDecodeError, TypeError):
        stored_rule_count = 0
    audit(
        admin,
        "network.settings.update",
        ip_filter_enabled=bool(ns.ip_filter_enabled),
        ip_filter_default=ns.ip_filter_default,
        ip_rule_count=stored_rule_count,
        mdns_enabled=bool(ns.mdns_enabled),
        analytics_mode=ns.analytics_mode,
        analytics_notice_toast_enabled=bool(ns.analytics_notice_toast_enabled),
        # Starting or stopping a third-party (Google) data feed is worth a trail.
        ga_measurement_id_changed=[_ga_before, ns.ga_measurement_id or ""]
        if (ns.ga_measurement_id or "") != _ga_before else None,
    )
    # Return the full settings so the dashboard can avoid the extra GET
    # round-trip after save. mdns_ok / mdns_msg surface transient avahi
    # write failures separately.
    return {
        **_network_settings_to_dict(ns),
        "ok": True,
        "mdns_ok": mdns_ok,
        "mdns_msg": mdns_msg,
    }


@router.get("/api/admin/network/my-ip")
def admin_get_my_ip(request: Request, admin: User = Depends(require_admin)):
    headers = request.headers
    client_host = request.client.host if request.client else "unknown"
    return {
        "ip": _net.get_client_ip(headers, client_host),
        "via_cloudflare_tunnel": _cf_access.is_via_cloudflare_tunnel(headers, client_host),
    }


# ===== Admin: app-access auth settings =====


_VALID_AUTH_MODES = ("none", "basic", "ldap", "google_oauth")

# The filter an empty ``ldap_user_filter`` stands for (the column default, and
# what app_auth binds with). Clearing the field stores "", while GET reports
# this — and the dashboard sends back what GET reported — so every comparison
# and audit uses the effective value: otherwise the next unrelated save would
# turn "" into this literal, read as an LDAP scope change, and log every app
# user out.
_DEFAULT_LDAP_USER_FILTER = "(uid={username})"


def _effective_ldap_user_filter(raw: Optional[str]) -> str:
    return raw or _DEFAULT_LDAP_USER_FILTER

# Leave-alone sentinel for the login-screen strings. FastAPI coerces an empty
# form value to the Form() default, so an omitted field and an explicitly-empty
# field are indistinguishable at the handler. A NUL-bearing default that no
# legitimate caller ever sends lets the handler treat "omitted / empty" as
# leave-alone (preserving the stored value across a partial PATCH) and require
# an explicit clear_login_* flag to actively reset the override — matching the
# clear_ldap_bind_password / clear_google_client_secret secrets.
_LOGIN_STR_UNSET = "\x00__wsi_login_str_unset__"


def _auth_settings_to_dict(s: AuthSettings) -> dict:
    return {
        "mode": s.mode or "none",
        "ldap_server_uri": s.ldap_server_uri or "",
        "ldap_bind_dn": s.ldap_bind_dn or "",
        "ldap_bind_password_configured": bool(s.ldap_bind_password),
        "ldap_user_base_dn": s.ldap_user_base_dn or "",
        "ldap_user_filter": _effective_ldap_user_filter(s.ldap_user_filter),
        "ldap_use_starttls": bool(s.ldap_use_starttls),
        "google_client_id": s.google_client_id or "",
        "google_client_secret_configured": bool(s.google_client_secret),
        "google_allowed_domains": s.google_allowed_domains or "",
        "google_allowed_emails": s.google_allowed_emails or "",
        "session_ttl_seconds": int(s.session_ttl_seconds or 86400),
        "login_message": s.login_message or "",
        "login_button_label": s.login_button_label or "",
        "lan_direct_noauth_mode": _app_auth.normalize_lan_direct_mode(s.lan_direct_noauth_mode),
    }


def _has_auth_user(db: Session) -> bool:
    return db.execute(select(AuthUser.id).limit(1)).first() is not None


def _auth_mode_readiness_warning(db: Session, s: AuthSettings) -> str:
    """Return an operator warning if the selected auth mode can't admit logins.

    A mode whose prerequisites are missing admits nobody — staff included,
    since the dashboard sign-in sits behind the gate too (only a dashboard
    session already open keeps working). So a *switch* to such a mode is
    refused outright (admin_update_auth_settings: basicNeedsUser,
    ldapNeedsConfig, googleNeedsConfig); this warning fires for a database
    already in that state (e.g. restored from a backup, or a partial edit
    while in the mode), which must stay editable to be fixed. Returns "" when
    the mode's prerequisites are satisfied.
    """
    mode = s.mode or "none"
    if mode == "basic":
        if not _has_auth_user(db):
            return (
                "Auth mode is 'basic' but no login users exist; no one can "
                "sign in, and every page load, the dashboard's included, asks "
                "for one. Add a user under Users from a dashboard tab that is "
                "already open, or switch the mode."
            )
    elif mode == "ldap":
        if not (s.ldap_server_uri or "").strip() or not (s.ldap_user_base_dn or "").strip():
            return (
                "Auth mode set to 'ldap' but the LDAP server URI and/or user "
                "base DN is not configured; nobody, staff included, can sign in "
                "until it is. Fix it from this dashboard tab before closing it."
            )
    elif mode == "google_oauth":
        # Empty allow-lists (no domain and no individual address) are
        # deliberately fail-closed at sign-in (app_auth.google_email_allowed
        # rejects everyone), so they strand visitors exactly like a missing
        # client ID does.
        if (
            not (s.google_client_id or "").strip()
            or not s.google_client_secret
            or not (
                (s.google_allowed_domains or "").strip()
                or (s.google_allowed_emails or "").strip()
            )
        ):
            return (
                "Auth mode set to 'google_oauth' but the Google client ID, "
                "client secret and/or allowed domains or email addresses are "
                "not configured; "
                "nobody, staff included, can sign in until they are. Fix it "
                "from this dashboard tab before closing it."
            )
    return ""


# Auth-settings fields recorded in the audit trail with before/after values, and
# the secrets for which only "changed" is recorded (never the value itself).
_AUTH_AUDIT_FIELDS = (
    "mode", "ldap_server_uri", "ldap_bind_dn", "ldap_user_base_dn",
    "ldap_user_filter", "ldap_use_starttls", "google_client_id",
    "google_allowed_domains", "google_allowed_emails", "session_ttl_seconds",
    "lan_direct_noauth_mode",
)
_AUTH_AUDIT_SECRETS = ("ldap_bind_password", "google_client_secret")


def _auth_audit_snapshot(s) -> dict:
    snap = {f: getattr(s, f, None) for f in _AUTH_AUDIT_FIELDS}
    snap["ldap_user_filter"] = _effective_ldap_user_filter(s.ldap_user_filter)
    snap.update({f: getattr(s, f, None) or "" for f in _AUTH_AUDIT_SECRETS})
    return snap


def _auth_audit_changes(before: dict, after: dict) -> dict:
    """Fields that actually changed: {name: [old, new]} for ordinary settings and
    {name: "set" | "cleared"} for secrets, so the trail shows a
    security-relevant edit (LAN no-auth level, LDAP target, allowed domains, a
    rotated secret) without ever holding a credential."""
    out: dict = {}
    for f in _AUTH_AUDIT_FIELDS:
        if before[f] != after[f]:
            out[f] = [before[f], after[f]]
    for f in _AUTH_AUDIT_SECRETS:
        if before[f] != after[f]:
            out[f] = "set" if after[f] else "cleared"
    return out


@router.get("/api/admin/auth/settings")
def admin_get_auth_settings(
    admin: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    s = get_or_create_singleton(db, AuthSettings)
    return _auth_settings_to_dict(s)


@router.patch("/api/admin/auth/settings")
def admin_update_auth_settings(
    mode: str = Form(""),
    # These plain-config fields default to None (not "") so an omitted field
    # is left untouched: this is a PATCH and the API client sends a partial
    # payload (auth.ts adminUpdateAuthSettings types it Partial<...>). With a
    # "" default an update that only toggled `mode` would silently wipe the
    # stored LDAP/Google config. An explicit "" still clears, matching the
    # sibling viewer/network PATCH handlers' is-not-None semantics.
    ldap_server_uri: Optional[str] = Form(None),
    ldap_bind_dn: Optional[str] = Form(None),
    # Bounded to the String(512) column width like the sibling fields, but via
    # Form(max_length=...) rather than _clean_free_text: a secret must be stored
    # byte-for-byte, so an over-long value is rejected with a 422 instead of
    # being silently truncated or stripped into a different secret. Without a
    # bound these two would be the only auth fields to persist a multi-MB
    # value verbatim (SQLite does not enforce String(N)).
    ldap_bind_password: str = Form("", max_length=512),
    clear_ldap_bind_password: str = Form(""),
    ldap_user_base_dn: Optional[str] = Form(None),
    ldap_user_filter: Optional[str] = Form(None),
    ldap_use_starttls: str = Form(""),
    google_client_id: Optional[str] = Form(None),
    # Same bound/rationale as ldap_bind_password above.
    google_client_secret: str = Form("", max_length=512),
    clear_google_client_secret: str = Form(""),
    google_allowed_domains: Optional[str] = Form(None),
    google_allowed_emails: Optional[str] = Form(None),
    # FastAPI coerces an empty form value to the Form(None) default, so an
    # empty "ldap_server_uri=" reads as None and the is-not-None guard skips
    # it — these plain-config fields could never be cleared back to "" by
    # emptying the textarea. Companion clear_<field>=1 flags make the clear
    # explicit, mirroring clear_ldap_bind_password / clear_google_client_secret.
    clear_ldap_server_uri: str = Form(""),
    clear_ldap_bind_dn: str = Form(""),
    clear_ldap_user_base_dn: str = Form(""),
    clear_ldap_user_filter: str = Form(""),
    clear_google_client_id: str = Form(""),
    clear_google_allowed_domains: str = Form(""),
    clear_google_allowed_emails: str = Form(""),
    session_ttl_seconds: str = Form(""),
    # Login-screen overrides. A leave-alone sentinel default (see
    # _LOGIN_STR_UNSET) makes omission preserve the stored value: the API type
    # is Partial<...> and a partial PATCH that omits these (e.g. one that only
    # changes session_ttl_seconds) must not wipe the public login screen back
    # to its per-mode default. A present non-empty value is written; an empty
    # value is indistinguishable from omitted under FastAPI, so clearing the
    # override requires the explicit clear_login_* flag below.
    login_message: str = Form(_LOGIN_STR_UNSET),
    login_button_label: str = Form(_LOGIN_STR_UNSET),
    clear_login_message: str = Form(""),
    clear_login_button_label: str = Form(""),
    lan_direct_noauth_mode: Optional[str] = Form(None),
    # Explicit "log every app-access session out now" flag. Auth-mode and
    # allowed-domain changes bump the session generation automatically; this
    # covers the manual/emergency case (e.g. a leaked cookie) without also
    # changing the mode.
    revoke_app_sessions: str = Form(""),
    admin: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    s = get_or_create_singleton(db, AuthSettings)
    _audit_before = _auth_audit_snapshot(s)
    # Snapshot policy-relevant fields before mutation so a tightening can be
    # detected and session_generation bumped to invalidate outstanding cookies.
    _prev_mode = s.mode or "none"
    _prev_allowed_domains = s.google_allowed_domains or ""
    _prev_allowed_emails = s.google_allowed_emails or ""
    # Whether the settings as they stand admit anyone (see the refusal below).
    _prev_ready = not _auth_mode_readiness_warning(db, s)
    _prev_ldap_scope = (
        s.ldap_server_uri or "", s.ldap_user_base_dn or "",
        _effective_ldap_user_filter(s.ldap_user_filter),
    )

    if mode:
        if mode not in _VALID_AUTH_MODES:
            raise HTTPException(status_code=422, detail=f"Invalid mode: {mode}")
        # Under basic the gate challenges every page load, /admin included, and
        # only an AuthUser can answer it: switching with none would lock the
        # operator out of any dashboard tab not already open. Refuse before
        # anything is changed; the Users list can be filled first.
        if mode == "basic" and _prev_mode != "basic" and not _has_auth_user(db):
            raise api_error(422, "adminDash.apiErr.basicNeedsUser")
        s.mode = mode

    # Strip control characters and cap to the declared column widths, matching
    # the viewer-settings free-text hygiene: SQLite does not enforce String(N),
    # so an ESC-laced or multi-MB value would otherwise persist verbatim (and,
    # for login_message/label below, ship to the public login screen).
    if _parse_form_bool(clear_ldap_server_uri):
        s.ldap_server_uri = ""
    elif ldap_server_uri is not None:
        s.ldap_server_uri = _clean_free_text(ldap_server_uri, max_len=512)
    if _parse_form_bool(clear_ldap_bind_dn):
        s.ldap_bind_dn = ""
    elif ldap_bind_dn is not None:
        s.ldap_bind_dn = _clean_free_text(ldap_bind_dn, max_len=512)
    if ldap_bind_password:
        s.ldap_bind_password = ldap_bind_password
    elif _parse_form_bool(clear_ldap_bind_password):
        s.ldap_bind_password = ""
    if _parse_form_bool(clear_ldap_user_base_dn):
        s.ldap_user_base_dn = ""
    elif ldap_user_base_dn is not None:
        s.ldap_user_base_dn = _clean_free_text(ldap_user_base_dn, max_len=512)
    if _parse_form_bool(clear_ldap_user_filter):
        s.ldap_user_filter = ""
    elif ldap_user_filter is not None:
        s.ldap_user_filter = _clean_free_text(ldap_user_filter, max_len=512)
    if _parse_form_bool(clear_google_client_id):
        s.google_client_id = ""
    elif google_client_id is not None:
        s.google_client_id = _clean_free_text(google_client_id, max_len=512)
    if google_client_secret:
        s.google_client_secret = google_client_secret
    elif _parse_form_bool(clear_google_client_secret):
        s.google_client_secret = ""
    # Normalize on save: strip whitespace, lowercase, drop empties.
    # Otherwise the dashboard would round-trip "Example.com, Example.org"
    # back to the user verbatim while the comparison snapshot uses the
    # normalized form (see app_auth._snapshot.google.allowed_domains).
    # An explicit clear flag (or a present, non-empty value that normalizes to
    # empty) clears the restriction.
    if _parse_form_bool(clear_google_allowed_domains):
        s.google_allowed_domains = ""
    elif google_allowed_domains is not None:
        # Same hygiene as the sibling free-text fields above: strip control
        # chars from each domain. Filter on the *cleaned* value so a
        # control-char-only token doesn't survive as an empty domain / stray
        # comma. Over-long input is refused rather than cut to fit: this is an
        # exact-match allow-list, and a cut landing mid-domain would store the
        # head of a name ("example.edu" out of "example.edu.example.org") as a
        # domain of its own — admitting one the operator never named.
        _domains = []
        for _d in google_allowed_domains.split(","):
            _cleaned = _clean_free_text(_d, max_len=len(_d)).strip().lower()
            if not _cleaned:
                continue
            if len(_cleaned) > 253:  # a DNS name maxes out at 253 chars
                raise HTTPException(
                    status_code=422,
                    detail="google_allowed_domains: each domain must be at most 253 characters",
                )
            _domains.append(_cleaned)
        _joined = ",".join(_domains)
        if len(_joined) > 512:  # the String(512) column; SQLite won't enforce it
            raise HTTPException(
                status_code=422,
                detail="google_allowed_domains: the list must be at most 512 characters",
            )
        s.google_allowed_domains = _joined
    # Individual addresses: same normalization and refuse-don't-truncate rule
    # as the domain list (a cut could leave a different, shorter address).
    if _parse_form_bool(clear_google_allowed_emails):
        s.google_allowed_emails = ""
    elif google_allowed_emails is not None:
        _emails = []
        for _e in google_allowed_emails.split(","):
            _cleaned = _clean_free_text(_e, max_len=len(_e)).strip().lower()
            if not _cleaned:
                continue
            _local, _at, _host = _cleaned.partition("@")
            if not _at or not _local or not _host or "@" in _host or any(
                ch.isspace() for ch in _cleaned
            ):
                raise HTTPException(
                    status_code=422,
                    detail=f"google_allowed_emails: not an email address: {_cleaned[:64]}",
                )
            if len(_cleaned) > 254:  # RFC 5321 path limit
                raise HTTPException(
                    status_code=422,
                    detail="google_allowed_emails: each address must be at most 254 characters",
                )
            if _cleaned not in _emails:
                _emails.append(_cleaned)
        _joined = ",".join(_emails)
        if len(_joined) > 4096:  # the String(4096) column; SQLite won't enforce it
            raise HTTPException(
                status_code=422,
                detail="google_allowed_emails: the list must be at most 4096 characters",
            )
        s.google_allowed_emails = _joined

    # Empty/missing means "leave alone"; any sent value is parsed like every
    # other boolean form field (so "0" / "false" turn STARTTLS off).
    if ldap_use_starttls.strip():
        s.ldap_use_starttls = _parse_form_bool(ldap_use_starttls)

    if session_ttl_seconds:
        try:
            ttl = int(session_ttl_seconds)
        except ValueError:
            raise HTTPException(status_code=422, detail="session_ttl_seconds must be an integer")
        if ttl < 60 or ttl > 30 * 24 * 3600:
            raise HTTPException(status_code=422, detail="session_ttl_seconds out of range")
        # No generation bump needed for a cut: decode_session_token measures
        # every cookie's age against the TTL in force, so a lower value ends
        # the sessions it no longer covers and leaves the rest signed in.
        s.session_ttl_seconds = ttl

    # Login-screen overrides (partial-update; see the Form() note above). A
    # present non-empty value is written; omission / empty leaves the stored
    # override untouched so a partial PATCH can't wipe the public login screen;
    # the explicit clear_login_* flag resets it back to the per-mode default.
    if _parse_form_bool(clear_login_message):
        s.login_message = ""
    elif login_message != _LOGIN_STR_UNSET:
        # Public login screen; multiline (keeps newlines/tabs), control chars
        # stripped, length capped even though the column is TEXT.
        s.login_message = _clean_free_text(login_message, max_len=2048, multiline=True)
    if _parse_form_bool(clear_login_button_label):
        s.login_button_label = ""
    elif login_button_label != _LOGIN_STR_UNSET:
        s.login_button_label = _clean_free_text(login_button_label, max_len=128)

    # Partial-update semantics (like the LDAP/Google config): an omitted field
    # is left untouched; a present value must be one of the three levels.
    if lan_direct_noauth_mode is not None:
        level = lan_direct_noauth_mode.strip().lower()
        if level not in _app_auth.LAN_DIRECT_MODES:
            raise HTTPException(
                status_code=422,
                detail=f"lan_direct_noauth_mode must be one of {list(_app_auth.LAN_DIRECT_MODES)}",
            )
        s.lan_direct_noauth_mode = level

    # Invalidate all outstanding app-access cookies when the policy tightens or
    # the operator asks for it. A mode switch or a change to the Google
    # allowed-domain or allowed-email list can mean previously-admitted holders should no longer
    # have access; so can repointing LDAP (server, user base DN or filter) while
    # LDAP is the active mode. Bumping the generation makes decode_session_token
    # reject every already-issued cookie immediately instead of waiting out
    # each TTL.
    _revoke = _parse_form_bool(revoke_app_sessions)
    _policy_changed = (
        (s.mode or "none") != _prev_mode
        or (s.google_allowed_domains or "") != _prev_allowed_domains
        or (s.google_allowed_emails or "") != _prev_allowed_emails
        or (
            (s.mode or "none") == "ldap"
            and (
                s.ldap_server_uri or "", s.ldap_user_base_dn or "",
                _effective_ldap_user_filter(s.ldap_user_filter),
            ) != _prev_ldap_scope
        )
    )
    if _revoke or _policy_changed:
        # Incremented in SQL, not from the value loaded above: two overlapping
        # saves that both read generation n would otherwise both store n+1,
        # and a cookie minted at n+1 between their commits — under the policy
        # the first save left — would survive the second save's tightening.
        s.session_generation = func.coalesce(AuthSettings.session_generation, 0) + 1

    # ldap / google_oauth settings that cannot admit anyone would strand
    # staff too (the dashboard sign-in is behind the gate): refuse a save
    # that switches to them, or that breaks them while in the mode, as basic
    # is refused without a user. Checked on the settings as this request
    # leaves them, so one save can switch the mode and fill in the provider
    # together; a configuration that was already broken (e.g. restored) stays
    # editable, so it can be fixed.
    _new_mode = s.mode or "none"
    if _new_mode in ("ldap", "google_oauth") and (_prev_mode != _new_mode or _prev_ready) \
            and _auth_mode_readiness_warning(db, s):
        db.rollback()
        raise api_error(
            422,
            "adminDash.apiErr.ldapNeedsConfig" if _new_mode == "ldap" else "adminDash.apiErr.googleNeedsConfig",
        )

    if (s.mode or "none") == "basic" and _prev_mode != "basic":
        # Close the TOCTOU of the basicNeedsUser check above: a concurrent
        # DELETE of the last AuthUser (on another worker) can pass its own
        # check, which saw the old mode, between that check and this commit.
        # Flush so this transaction holds SQLite's write lock, then re-count
        # inside it (as admin_users does for the last admin).
        db.flush()
        if not _has_auth_user(db):
            db.rollback()
            raise api_error(422, "adminDash.apiErr.basicNeedsUser")

    db.commit()
    # As for the network settings: this worker now, the others within
    # WSI_SETTINGS_REFRESH_SECONDS (main._settings_refresh_loop).
    _app_auth.refresh(db)
    audit(
        admin,
        "auth.settings.update",
        mode=s.mode,
        changes=_auth_audit_changes(_audit_before, _auth_audit_snapshot(s)),
        sessions_revoked=bool(_revoke or _policy_changed),
    )
    # Surface a non-blocking warning when the saved mode cannot admit anyone
    # (a database already in that state; a switch into it was refused above).
    # Only evaluated when the request actually selected a mode, so a partial
    # PATCH of unrelated fields doesn't nag about pre-existing config.
    warning = _auth_mode_readiness_warning(db, s) if mode else ""
    return {**_auth_settings_to_dict(s), "warning": warning}


@router.post("/api/admin/auth/ldap/test")
def admin_test_ldap(
    # Capped like every other credential Form field in this router: without a
    # max_length FastAPI buffers the whole value in memory before the handler
    # runs, so a multi-MB field would be materialised only to be handed to a
    # bind that can never succeed.
    username: str = Form("", max_length=512),
    password: str = Form("", max_length=1024),
    admin: User = Depends(require_admin),
):
    """Probe the saved LDAP config; optionally try a sample user bind.

    Uses the in-memory snapshot (same source as verify_ldap), so the
    operator must save before testing. Audit logs the attempt without
    the password — the username is recorded only when supplied so an
    accidentally-pasted password into the wrong field never reaches the
    audit log.
    """
    result = _app_auth.test_ldap_settings(username=username, password=password)
    audit(
        admin,
        "auth.ldap.test",
        ok=bool(result.get("ok")),
        stage=str(result.get("stage", "")),
        with_user=bool(username),
    )
    return result


@router.get("/api/admin/auth/users")
def admin_list_auth_users(
    admin: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    rows = db.execute(select(AuthUser).order_by(AuthUser.username.asc())).scalars().all()
    return [{"id": r.id, "username": r.username} for r in rows]


@router.post("/api/admin/auth/users")
def admin_add_auth_user(
    # Username gets a tighter 128-char ceiling further down to mirror the
    # AuthUser.username column width; the 255 here is a defensive boundary
    # cap before that handler-level rule runs. Password cap matches
    # auth.MAX_PASSWORD_LEN.
    username: str = Form(..., max_length=255),
    password: str = Form(..., max_length=1024),
    admin: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    username = (username or "").strip()
    if not username or len(username) > 128:
        raise HTTPException(status_code=422, detail="username: 1-128 characters")
    if CTRL_CHAR_RE.search(username) or ":" in username:
        raise HTTPException(status_code=422, detail="username: invalid characters")
    validate_password_strength(password)
    # Case-insensitive, matching the staff-user create path: two app-auth
    # accounts differing only by case are indistinguishable to an operator
    # managing the viewer allow-list — and would share one pseudonymous
    # analytics identity, which folds case with Python's lower(). Compared on
    # the stored auth_username_key, not SQLite's ASCII-only lower(), so
    # "Älice" and "älice" collide too; its UNIQUE index catches a concurrent
    # create.
    key = auth_username_key(username)
    if db.execute(
        select(AuthUser.id).where(AuthUser.username_key == key).limit(1)
    ).first() is not None:
        raise HTTPException(status_code=409, detail="username already exists")
    row = AuthUser(username=username, password_hash=hash_password(password))
    db.add(row)
    try:
        db.commit()
    except IntegrityError:
        # Lost the SELECT-then-INSERT race against another concurrent admin
        # creating the same username; surface the same 409 the deterministic
        # check above produces instead of a 500 with an aborted session.
        db.rollback()
        raise HTTPException(status_code=409, detail="username already exists")
    audit(admin, "auth.user.create", target=str(row.id), username=row.username)
    return {"id": row.id, "username": row.username}


@router.patch("/api/admin/auth/users/{user_id}")
def admin_update_auth_user_password(
    user_id: int,
    password: str = Form(..., max_length=1024),
    admin: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    row = db.get(AuthUser, user_id)
    if not row:
        raise HTTPException(status_code=404, detail="user not found")
    validate_password_strength(password)
    row.password_hash = hash_password(password)
    db.commit()
    # Existing cookies carry the old hash's fingerprint and now fail decode
    # (immediately on this worker, within the cache TTL on the others).
    _app_auth.forget_basic_credential(row.username)
    audit(admin, "auth.user.password_reset", target=str(user_id), username=row.username)
    return {"ok": True}


@router.delete("/api/admin/auth/users/{user_id}")
def admin_delete_auth_user(
    user_id: int,
    admin: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    row = db.get(AuthUser, user_id)
    if not row:
        raise HTTPException(status_code=404, detail="user not found")
    # The same lockout as a switch to basic with no users (see
    # admin_update_auth_settings): refuse to remove the last one while the
    # mode is basic.
    s = db.get(AuthSettings, 1)
    if s is not None and s.mode == "basic":
        others = db.execute(
            select(AuthUser.id).where(AuthUser.id != user_id).limit(1)
        ).first()
        if others is None:
            raise api_error(422, "adminDash.apiErr.basicLastUser")
    username = row.username
    db.delete(row)
    # Close the TOCTOU of the check above: two workers deleting the last two
    # users, or a delete racing a switch to basic, can each pass it. Flush so
    # this transaction holds SQLite's write lock, then re-read the mode and
    # re-count inside it (the PATCH re-checks the same way).
    db.flush()
    mode_now = db.execute(
        select(AuthSettings.mode).where(AuthSettings.id == 1)
    ).scalar_one_or_none()
    if mode_now == "basic" and not _has_auth_user(db):
        db.rollback()
        raise api_error(422, "adminDash.apiErr.basicLastUser")
    db.commit()
    _app_auth.forget_basic_credential(username)
    audit(admin, "auth.user.delete", target=str(user_id), username=username)
    return {"ok": True}
