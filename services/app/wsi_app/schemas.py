import math
import re
from pydantic import BaseModel, Field, field_validator
from typing import Optional, List, Dict


_COORD_MAX = 1.0e9  # generous upper bound for slide-level pixel coordinates
# Single-line title text (Tour.title, Step.label) shares the C0-control
# rejection used by admin_helpers.CTRL_CHAR_RE. Markdown free-text columns
# (description, description_md) are TEXT and may legitimately contain
# \t \n \r, so only NUL is blocked at the schema layer — anything else is
# either valid markdown or harmless when JSON-serialised for audit logs.
_TITLE_CTRL_CHAR_RE = re.compile(r"[\x00-\x1f\x7f]")


def _finite_or_raise(value: float, name: str) -> float:
    if not math.isfinite(value):
        raise ValueError(f"{name} must be a finite number")
    if abs(value) > _COORD_MAX:
        raise ValueError(f"{name} out of range")
    return value


_VALID_SHAPE_KINDS = {"rect", "circle", "freehand"}
_VALID_SHAPE_LABEL_POSITIONS = {"above", "below", "center"}
# Mirrors the frontend's PinShape union (lib/api/tours). "circle" is the
# StepPin.shape column default.
_VALID_PIN_SHAPES = {"pin", "circle", "arrow"}
DEFAULT_PIN_SHAPE = "circle"


# How a step animates the move OUT of itself into the next step. Mirrors the
# frontend's TransitionMode union (lib/api/tours). New steps default to
# "smooth" (a plain pan/zoom to the next framing, no zoom-out-then-in
# flourish); the order here is informational only.
TRANSITION_MODES = ("none", "smooth", "smooth_zoom")
DEFAULT_TRANSITION_MODE = "smooth"
_MAX_FREEHAND_POINTS = 4096  # generous upper bound; protects DB from runaway uploads
_MAX_PIN_COLOR_LEN = 16     # mirrors StepPin.color column width
_MAX_ANNOTATION_ID_LEN = 64  # mirrors StepPin.pin_id / StepShape.shape_id width
# Annotation stacking order ("z"): higher paints on top. Pins and shapes share
# one scale so any item can be moved above/below any other. New shapes default
# to 0 and pins to LAYER_PIN_BASE, so with no explicit ordering pins always sit
# above shapes (the desired default). Mirrors LAYER_PIN_BASE in the frontend
# lib/tourLayers. Bounded to keep repack/"bring-to-front" arithmetic
# (maxZ + 1) well inside int range.
LAYER_PIN_BASE = 1_000_000
_MAX_LAYER_Z = 1_000_000_000
# Tour / step free-text caps. The columns are TEXT (no DB-level limit), so
# without these the API would happily accept multi-MB payloads that bloat the
# DB and the audit-log JSON serialisation. The caps are deliberately loose
# (a generous tour description fits in ~5k; a step description_md is markdown
# so allow ~20 typical pages) — they exist to keep accidental or malicious
# payloads from running away, not to constrain authoring.
_MAX_TITLE_LEN = 255             # mirrors Tour.title / Step.label column width
_MAX_TOUR_DESCRIPTION_LEN = 8000
_MAX_STEP_DESCRIPTION_LEN = 65536
# Per-step annotation counts. A PATCH replaces the whole list, so a step is
# implicitly bounded by nginx's 2 MiB request-body cap; the one-item append
# endpoints (admin_tours.admin_append_step_pin / _shape) bypass that bound,
# and a loop of small appends could otherwise grow one step until every read
# of its tour (step_to_out serialises all of it) is unusable. Far above what
# an editor that renders one card per annotation can sensibly hold; enforced
# on every writer (create, PATCH, append) so they agree.
_MAX_STEP_PINS = 1000
_MAX_STEP_SHAPES = 1000
# Total freehand vertices across one step's shapes. The per-shape cap alone
# would still let a step hold 1000 x 4096 points, and every read of the tour —
# including the unauthenticated public one — serialises all of them. A drawn
# stroke is typically a few hundred to a couple of thousand vertices (the
# editor samples one per pointer event, at least 1 px apart), so this leaves
# room for dozens of careful outlines per step. A step at this budget saves
# as a ~2.5 MiB body, which nginx's step-write location (4m) and the create
# route's form limit (admin_tours._STEP_FORM_MAX_PART) are sized to carry.
# Enforced on create, PATCH and append.
_MAX_STEP_FREEHAND_POINTS = 50_000
# Steps per tour, enforced when a step is added. Well beyond a real tour (a
# long lecture runs to a few dozen).
_MAX_TOUR_STEPS = 100
# Total freehand vertices across all of a tour's steps — what bounds one tour
# read. The two caps above alone would allow 100 x 50,000: one unauthenticated
# read of such a tour would take 15-30 s of CPU, a 230-290 MB body and ~2 GB of
# worker memory. At this budget a read takes a fraction of a second and its
# body stays under ~18 MB (~14 MB with typical full-precision coordinates),
# and a tour still has room for a couple of hundred drawn outlines. Enforced,
# under the write lock, by step create, PATCH and append, and by the slide
# import.
_MAX_TOUR_FREEHAND_POINTS = 300_000


def freehand_point_count(shapes) -> int:
    """Total vertices across ``shapes`` (validated Shape items)."""
    return sum(len(sh.points) for sh in shapes)


class ShapePoint(BaseModel):
    x: float
    y: float

    @field_validator("x", "y")
    @classmethod
    def _finite(cls, v: float, info):
        return _finite_or_raise(v, info.field_name)


class Shape(BaseModel):
    """Tour-step shape annotation in level-0 pixel coordinates.

    The same type carries rectangles, circles, and freehand strokes —
    discriminated by ``kind``.
    """

    id: str
    kind: str = "rect"
    x: float
    y: float
    w: float
    h: float
    stroke_color: str = "red"
    fill_enabled: bool = False
    fill_opacity: float = 0.5
    points: List[ShapePoint] = Field(default_factory=list)
    # Freehand-only: render a smoothed curve (a low-pass of the vertices)
    # rather than straight segments. Defaults True (matches the DB column
    # default). Purely a client render hint; the stored points are unchanged.
    smoothing: bool = True
    label: str = ""
    label_position: str = "above"
    # Stacking order; see LAYER_PIN_BASE. Shapes default to 0 so pins (which
    # default to LAYER_PIN_BASE) sit above them unless explicitly reordered.
    z: int = 0

    @field_validator("z")
    @classmethod
    def _z_bounded(cls, v: int) -> int:
        if not -_MAX_LAYER_Z <= v <= _MAX_LAYER_Z:
            raise ValueError(f"z must be within +/-{_MAX_LAYER_Z}")
        return v

    @field_validator("id")
    @classmethod
    def _id_bounded(cls, v: str) -> str:
        # shape_id is a String(64) column. Without a cap SQLite would silently
        # persist a multi-MB id (DB bloat, defeating the per-field caps
        # elsewhere). Reject at the boundary instead.
        if len(v) > _MAX_ANNOTATION_ID_LEN:
            raise ValueError(f"id must be {_MAX_ANNOTATION_ID_LEN} characters or fewer")
        return v

    @field_validator("kind")
    @classmethod
    def _kind_choice(cls, v: str) -> str:
        if v not in _VALID_SHAPE_KINDS:
            raise ValueError(f"kind must be one of {sorted(_VALID_SHAPE_KINDS)}")
        return v

    @field_validator("x", "y")
    @classmethod
    def _coord_finite(cls, v: float, info):
        return _finite_or_raise(v, info.field_name)

    @field_validator("w", "h")
    @classmethod
    def validate_size(cls, value: float, info):
        _finite_or_raise(value, info.field_name)
        if value <= 0:
            raise ValueError("w and h must be > 0")
        return value

    @field_validator("fill_opacity")
    @classmethod
    def _opacity_range(cls, v: float, info):
        _finite_or_raise(v, info.field_name)
        # Clamp rather than reject: a slider rounding error shouldn't 422.
        if v < 0:
            return 0.0
        if v > 1:
            return 1.0
        return v

    @field_validator("stroke_color")
    @classmethod
    def _color_str(cls, v: str) -> str:
        # Accept arbitrary short strings: preset name or CSS color literal.
        # Bound the length to prevent payload abuse; the renderer treats
        # unrecognised values as opaque CSS.
        if len(v) > 32:
            raise ValueError("stroke_color is too long (max 32)")
        return v

    @field_validator("points")
    @classmethod
    def _points_bounded(cls, v):
        if len(v) > _MAX_FREEHAND_POINTS:
            raise ValueError(f"points must have at most {_MAX_FREEHAND_POINTS} entries")
        return v

    @field_validator("label_position")
    @classmethod
    def _label_position_choice(cls, v: str) -> str:
        # The renderer falls back to 'above' for unknown positions, but
        # rejecting here at the schema boundary keeps the DB row in a
        # known state and gives external API consumers a clear 422 instead
        # of silent normalisation. Mirrors the kind validator above.
        if v not in _VALID_SHAPE_LABEL_POSITIONS:
            raise ValueError(
                f"label_position must be one of {sorted(_VALID_SHAPE_LABEL_POSITIONS)}"
            )
        return v

    @field_validator("label")
    @classmethod
    def _label_len(cls, v: str) -> str:
        # Shape labels are single-line annotation captions stored in a
        # String(255) column. Reject overlong / control-char input here with a
        # 422 instead of letting the writer silently truncate to [:255]: that
        # truncation would make the API response and the persisted row
        # disagree on what was saved, which is a debugging trap. Mirrors
        # Step.label.
        if len(v) > _MAX_TITLE_LEN:
            raise ValueError(f"label must be {_MAX_TITLE_LEN} characters or fewer")
        if _TITLE_CTRL_CHAR_RE.search(v):
            raise ValueError("label contains control characters")
        return v


class PinItem(BaseModel):
    id: str
    x: float
    y: float
    shape: str = DEFAULT_PIN_SHAPE
    color: str = "red"
    rotation_deg: float = 0.0
    label: str = ""
    # Stacking order; see LAYER_PIN_BASE. Pins default to LAYER_PIN_BASE so they
    # sit above shapes (which default to 0) unless explicitly reordered.
    z: int = LAYER_PIN_BASE

    @field_validator("z")
    @classmethod
    def _z_bounded(cls, v: int) -> int:
        if not -_MAX_LAYER_Z <= v <= _MAX_LAYER_Z:
            raise ValueError(f"z must be within +/-{_MAX_LAYER_Z}")
        return v

    @field_validator("id")
    @classmethod
    def _id_bounded(cls, v: str) -> str:
        # pin_id is a String(64) column — same rationale as Shape.id: cap here
        # so an oversized id can't bloat SQLite.
        if len(v) > _MAX_ANNOTATION_ID_LEN:
            raise ValueError(f"id must be {_MAX_ANNOTATION_ID_LEN} characters or fewer")
        return v

    @field_validator("label")
    @classmethod
    def _label_len(cls, v: str) -> str:
        # Same rule as Shape.label / Step.label: bound the length to the
        # String(255) column and reject control chars at the schema boundary
        # rather than truncating in the writer.
        if len(v) > _MAX_TITLE_LEN:
            raise ValueError(f"label must be {_MAX_TITLE_LEN} characters or fewer")
        if _TITLE_CTRL_CHAR_RE.search(v):
            raise ValueError("label contains control characters")
        return v

    @field_validator("x", "y", "rotation_deg")
    @classmethod
    def _finite(cls, v: float, info):
        # Reject NaN / ±Inf and absurd magnitudes the same way the shape model does,
        # so a malformed admin payload can't slip non-finite coordinates
        # through to step_pins (where the renderer would propagate NaN
        # downstream into OSD's coordinate transforms).
        return _finite_or_raise(v, info.field_name)

    @field_validator("shape")
    @classmethod
    def _shape_choice(cls, v: str) -> str:
        # Same closed set as Shape.kind. An empty string keeps meaning "the
        # default" (the writers store `p.shape or "circle"`);
        # anything else outside the set is rejected rather than persisted into
        # the String(16) column for the renderer to guess at.
        if v == "":
            return DEFAULT_PIN_SHAPE
        if v not in _VALID_PIN_SHAPES:
            raise ValueError(f"shape must be one of {sorted(_VALID_PIN_SHAPES)}")
        return v

    @field_validator("color")
    @classmethod
    def _color_bounded(cls, v: str) -> str:
        # The DB column is String(16) and SQLite won't enforce that width,
        # so cap at the schema boundary: matches StepPin.color column width.
        # Free-form strings (preset name or hex) are allowed; only length
        # is bounded here.
        if len(v) > _MAX_PIN_COLOR_LEN:
            raise ValueError(f"color is too long (max {_MAX_PIN_COLOR_LEN})")
        return v

class SlideOut(BaseModel):
    id: int
    # Unguessable public identifier used in every public URL / tile source.
    # The numeric ``id`` is kept for internal client keying but no public
    # endpoint accepts it as input, so exposing it here is inert.
    public_id: str = ""
    catalog_id: str = ""
    title: str
    thumb_url: str
    width: int
    height: int
    mpp_x: float
    mpp_y: float
    mpp_x_file: float = 0.0
    mpp_y_file: float = 0.0
    # Objective magnification from the WSI file metadata
    # (``openslide.objective-power``). 0 when the file did not record one;
    # the dashboard then falls back to the MPP-derived estimate.
    objective_power_file: float = 0.0
    vendor: str = ""
    wsi_filename: str = ""
    file_size_bytes: int = 0
    attributes: Dict[str, List[str]] = Field(default_factory=dict)
    show_zoom_indicator: Optional[bool] = None
    show_scale_bar: Optional[bool] = None
    show_metadata_panel: Optional[bool] = None
    # Per-slide image-adjustment preset. NULL = no preset for that channel
    # (viewer falls back to identity 1.0). Echoed on both public and admin
    # responses so the preset applies to every viewer from first paint.
    image_adjust_brightness: Optional[float] = None
    image_adjust_contrast: Optional[float] = None
    image_adjust_saturation: Optional[float] = None
    is_published: bool = False
    # Link-only visibility. Only populated on admin responses (the public
    # listing already filters unlisted slides out); defaults False elsewhere.
    unlisted: bool = False
    # URLs for vendor-associated images extracted from the WSI file. Empty when
    # the source format does not expose the corresponding image. ``label_url``
    # is only populated on admin-side responses; the public router leaves it
    # blank because labels often carry identifying / sensitive content.
    macro_url: str = ""
    label_url: str = ""
    # Acquisition / scan timestamp from the WSI file, normalised to ISO 8601
    # (``YYYY-MM-DD`` or ``YYYY-MM-DDTHH:MM:SS``). Empty when the file does
    # not record one.
    acquired_at: str = ""
    # Image credit (see slide_credit): the rights holder's notice, a license
    # id from slide_credit.LICENSE_IDS, and an http(s) source link. Public:
    # the viewer must show them wherever the image is shown. "" = not set.
    credit_text: str = ""
    license_id: str = ""
    source_url: str = ""
    # Server-side timestamp of when this slide was uploaded via the admin
    # dashboard, as ISO 8601 with the server's local UTC offset
    # (_serializers.server_local_iso). Empty only when the underlying row
    # has no value (a hand-edited row; should not happen in practice).
    created_at: str = ""
    # The slide's WSI content generation (``Slide.wsi_generation``), bumped by
    # every WSI replace. The viewer opens ``/dzi/{public_id}.dzi?v=<this>``
    # (and ``.szi?v=``); OpenSeadragon carries the query onto every tile URL,
    # and nginx keys its shared tile cache on it, so a replace yields new tile
    # URLs instead of a mix of cached old and fresh new tiles. Set by the
    # detail endpoints and the admin list — the ones whose slides are opened in
    # a viewer; None elsewhere (the public index list has no tile use), and a
    # client then requests the unversioned URL, which is served but not
    # edge-cached.
    tile_version: Optional[int] = None

    @field_validator(
        "mpp_x", "mpp_y", "mpp_x_file", "mpp_y_file", "objective_power_file",
        mode="after",
    )
    @classmethod
    def _unknown_if_not_finite(cls, v: float) -> float:
        # Backstop for a hand-edited row holding "Infinity" / "NaN" (which
        # read_slide_properties rejects): one such value would otherwise
        # make every listing that includes the slide fail JSON encoding.
        # Reported as 0.0, the "not recorded" value these fields already use.
        return v if math.isfinite(v) else 0.0

class SlideDetail(SlideOut):
    pass


class MatchedTourOut(BaseModel):
    """A listed tour that is a reason its slide is in an /api/slides result
    (see routers/public._matched_tours_by_slide)."""
    id: int
    public_id: str = ""
    title: str


class SlideListItemOut(SlideOut):
    # The slide's published, listed tours that add to its match, in creation
    # order — the reason a slide that matches only through a tour is in the
    # result at all, so the index card can surface those tours first: a ``q``
    # hit on the tour's own title or metadata, or, when the slide's own
    # metadata fails the filters, the tour metadata that passes them. Empty
    # without ``q`` or a filter, with ``match_tours=false``, or when the slide
    # matched on its own.
    matched_tours: List[MatchedTourOut] = Field(default_factory=list)


class SlideListOut(BaseModel):
    items: List[SlideListItemOut]
    total: int
    page: int
    per_page: int


class TourIndexSlideOut(BaseModel):
    """The parent-slide fields a tour card on the index page renders.

    Deliberately not a full SlideOut: the Tours view draws a thumbnail, a
    "catalog id · title" caption and the card's metadata line (the slide's
    metadata plus the tour's own), so dimensions and the rest stay off this
    (up to 5000-row) response.
    """
    id: int
    public_id: str = ""
    catalog_id: str = ""
    title: str = ""
    thumb_url: str = ""
    image_adjust_brightness: Optional[float] = None
    image_adjust_contrast: Optional[float] = None
    image_adjust_saturation: Optional[float] = None
    # The slide's metadata, {key: [values]}.
    attributes: Dict[str, List[str]] = {}


class TourIndexItemOut(BaseModel):
    # Same fields as the public TourOut listing (minus ``unlisted``, always
    # false here), so the client can treat an item as a tour summary, plus the
    # parent slide's card data.
    id: int
    public_id: str = ""
    slide_public_id: str = ""
    slide_id: int
    title: str
    description: str
    is_published: bool
    # The tour's OWN metadata (its slide's is on ``slide``); the Tours view
    # filters on this alone (see public.list_tour_index).
    attributes: Dict[str, List[str]] = {}
    slide: TourIndexSlideOut


class TourIndexOut(BaseModel):
    items: List[TourIndexItemOut]
    total: int
    page: int
    per_page: int


class FacetItem(BaseModel):
    value: str
    # Listed slides carrying the value themselves: the "Slides" view's result
    # for this one value. The groups are ordered by it.
    count: int
    # The "All" view's (slides that carry it or have a listed tour that does)
    # and the "Tours" view's (listed tours carrying it themselves — a value
    # their slide passes down doesn't count). See routers/public.facets.
    all_count: int = 0
    tour_count: int = 0


class FacetsOut(BaseModel):
    groups: Dict[str, List[FacetItem]]

class TourOut(BaseModel):
    id: int
    # The tour's own public identifier, and the parent slide's — both are
    # needed to build the public tour URL ``/slides/{slide_public_id}/tours/
    # {public_id}`` and the tour's tile source, which the numeric ids alone
    # (from e.g. the global /api/tours listing) can't provide.
    public_id: str = ""
    slide_public_id: str = ""
    slide_id: int
    title: str
    description: str
    is_published: bool
    # Link-only visibility (see Tour.unlisted). Defaults False, so a caller
    # that doesn't pass it gets the listed behaviour.
    unlisted: bool = False
    # Optimistic-concurrency counter (see Tour.revision). Admin responses
    # only: the editor echoes it back as ``expected_revision``. The public
    # routers leave it None — an edit counter means nothing to a viewer.
    revision: Optional[int] = None


class AdminTourOut(TourOut):
    # The tour's OWN metadata, {key: [values]} (see models.TourAttribute); the
    # attributes it inherits from its slide are not repeated here. On every
    # admin tour response; publicly, GET /api/tours/{public_id} and the
    # /api/index/tours items carry the same field (routers/public.py), while
    # the public TourOut listings keep their shape.
    attributes: Dict[str, List[str]] = {}


# ``expected_revision`` on the admin tour/step mutations (see the concurrency
# contract in docs/developer-guide.md): when present the write is a
# conditional UPDATE that answers 409 on a mismatch; when omitted (scripts,
# other non-SPA callers) the write stays unconditional. Always the revision of
# the resource the operation bumps: the step's own for a step PATCH, the tour's
# for tour fields and for step add / delete / reorder.
def _expected_revision_field():
    return Field(default=None, ge=0)


class TourUpdateIn(BaseModel):
    title: Optional[str] = None
    description: Optional[str] = None
    # Replaces the tour's own attributes as a whole ({} clears them); omitted
    # leaves them alone. Checked by admin_tours (validate_attribute_kv, and for
    # instructors the vocabulary rule: only values the option list offers).
    attributes: Optional[Dict[str, List[str]]] = None
    expected_revision: Optional[int] = _expected_revision_field()

    @field_validator("description")
    @classmethod
    def _description_len(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return v
        if len(v) > _MAX_TOUR_DESCRIPTION_LEN:
            raise ValueError(
                f"description must be {_MAX_TOUR_DESCRIPTION_LEN} characters or fewer"
            )
        if "\x00" in v:
            raise ValueError("description contains NUL bytes")
        return v

class StepOut(BaseModel):
    # The response shape of a step. Reads build it as a plain dict
    # (_serializers.step_to_out) without re-validating stored rows;
    # tests/test_tour_read_cost.py pins the two together.
    id: int
    tour_id: int
    order: int
    label: str
    center_x: float
    center_y: float
    fov_um_w: float
    fov_um_h: float
    rotation_deg: float
    pins: List[PinItem] = Field(default_factory=list)
    shapes_l0: List[Shape] = Field(default_factory=list)
    description_md: str
    transition_mode: str = DEFAULT_TRANSITION_MODE
    # Optimistic-concurrency counter for this step's own content (see
    # Step.revision); the editor sends it back as ``expected_revision``.
    revision: int = 0

# Admin
# Length caps mirror auth.MAX_USERNAME_LEN / auth.MAX_PASSWORD_LEN. Hard-coded
# here to keep schemas.py free of a runtime dependency on auth.py (auth.py
# itself raises on JWT_SECRET misconfiguration at import time, which would
# pull that startup failure into schema validation).
class LoginIn(BaseModel):
    username: str = Field(max_length=255)
    password: str = Field(max_length=1024)

class LoginOut(BaseModel):
    """POST /api/admin/login. Exactly one of the two shapes: the staff token,
    or — for an account with two-factor sign-in on — ``mfa_required`` and the
    short-lived ``mfa_token`` that /api/admin/login/mfa takes with the code."""
    access_token: Optional[str] = None
    mfa_required: Optional[bool] = None
    mfa_token: Optional[str] = None


class MfaLoginIn(BaseModel):
    mfa_token: str = Field(max_length=4096)
    # A 6-digit code or a recovery code (XXXXX-XXXXX); spaces / hyphens allowed.
    code: str = Field(max_length=64)


class MfaSetupIn(BaseModel):
    current_password: str = Field(max_length=1024)


class MfaConfirmIn(BaseModel):
    setup_token: str = Field(max_length=4096)
    code: str = Field(max_length=64)


class MfaResetIn(BaseModel):
    # The acting admin's own password.
    current_password: str = Field(max_length=1024)


class MfaReauthIn(BaseModel):
    """Turning the second factor off, or replacing the recovery codes: the
    password and a current code (or a recovery code) together."""
    current_password: str = Field(max_length=1024)
    code: str = Field(max_length=64)


class AdminMeUpdateIn(BaseModel):
    current_password: str = Field(max_length=1024)
    username: Optional[str] = Field(default=None, max_length=255)
    new_password: Optional[str] = Field(default=None, max_length=1024)

class StepReorderIn(BaseModel):
    step_ids: List[int]
    # The tour's revision: a reorder rewrites the step structure.
    expected_revision: Optional[int] = _expected_revision_field()


class IpRuleIn(BaseModel):
    """One entry in NetworkSettings.ip_rules. Validated end-to-end so the
    admin endpoint can reject malformed payloads with 422 rather than silently
    dropping entries."""
    ip: str = Field(min_length=1)
    action: str = "allow"
    note: str = ""

    @field_validator("ip")
    @classmethod
    def _ip_or_cidr(cls, v: str) -> str:
        import ipaddress
        v = v.strip()
        try:
            if "/" in v:
                # Canonicalize: "192.168.1.5/24" -> "192.168.1.0/24" so
                # the displayed rule matches what actually matches in
                # ip_allowed(). strict=False accepts host bits set, then
                # str() returns the network form. A single-host network
                # (/32, /128) collapses to the bare address so "10.0.0.1" and
                # "10.0.0.1/32" dedupe as one rule instead of both surviving,
                # where a later deny would be shadowed by an earlier allow.
                net = ipaddress.ip_network(v, strict=False)
                if net.prefixlen == net.max_prefixlen:
                    return str(net.network_address)
                return str(net)
            # Canonicalize compressed/IPv6 forms (e.g. 2001:db8::0001) so
            # duplicate detection at the UI level can compare strings.
            return str(ipaddress.ip_address(v))
        except ValueError as exc:
            raise ValueError(f"Invalid IP/CIDR: {v}") from exc

    @field_validator("action")
    @classmethod
    def _action_choice(cls, v: str) -> str:
        if v not in ("allow", "deny"):
            raise ValueError("action must be 'allow' or 'deny'")
        return v

    @field_validator("note")
    @classmethod
    def _note_len(cls, v: str) -> str:
        v = v or ""
        if len(v) > 120:
            raise ValueError("note must be 120 characters or fewer")
        return v


class StepUpdateIn(BaseModel):
    """PATCH body for ``admin_update_step``.

    All fields are optional: any key omitted from the request body leaves
    the corresponding DB column untouched. ``pins`` / ``shapes_l0`` are
    explicit lists when sent (full replace of the annotation set) and
    preserved as-is when omitted. The validators below run only when a
    value is present, mirroring the per-field semantics of POST /steps
    while keeping the column unchanged on a true no-op patch.

    ``expected_revision`` is the step's revision the editor was opened from
    (or last saved); see the concurrency contract above
    ``_expected_revision_field``.
    """
    label: Optional[str] = None
    description_md: Optional[str] = None
    rotation_deg: Optional[float] = None
    fov_um_w: Optional[float] = None
    fov_um_h: Optional[float] = None
    center_x: Optional[float] = None
    center_y: Optional[float] = None
    pins: Optional[List[PinItem]] = Field(default=None, max_length=_MAX_STEP_PINS)
    shapes_l0: Optional[List[Shape]] = Field(default=None, max_length=_MAX_STEP_SHAPES)
    transition_mode: Optional[str] = None
    expected_revision: Optional[int] = _expected_revision_field()

    @field_validator("transition_mode")
    @classmethod
    def _transition_mode_choice(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return v
        if v not in TRANSITION_MODES:
            raise ValueError(f"transition_mode must be one of {TRANSITION_MODES}")
        return v

    @field_validator("label")
    @classmethod
    def _label_len(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return v
        if len(v) > _MAX_TITLE_LEN:
            raise ValueError(f"label must be {_MAX_TITLE_LEN} characters or fewer")
        # Step labels are single-line titles in the editor list view; reject
        # control characters (including \n / \r) so a copy-pasted multiline
        # payload can't break the layout or smuggle invisible content into
        # the step card. Mirrors the Tour title rule applied in
        # admin_helpers.CTRL_CHAR_RE.
        if _TITLE_CTRL_CHAR_RE.search(v):
            raise ValueError("label contains control characters")
        return v

    @field_validator("description_md")
    @classmethod
    def _description_md_len(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return v
        if len(v) > _MAX_STEP_DESCRIPTION_LEN:
            raise ValueError(
                f"description_md must be {_MAX_STEP_DESCRIPTION_LEN} characters or fewer"
            )
        # Markdown allows \t / \n / \r, but NUL bytes can truncate the
        # column when surfaced to logging or C-extension paths.
        if "\x00" in v:
            raise ValueError("description_md contains NUL bytes")
        return v

    @field_validator("fov_um_w", "fov_um_h")
    @classmethod
    def _fov_positive(cls, v: Optional[float], info):
        if v is None:
            return v
        _finite_or_raise(v, info.field_name)
        if v <= 0:
            raise ValueError(f"{info.field_name} must be > 0")
        return v

    @field_validator("center_x", "center_y", "rotation_deg")
    @classmethod
    def _finite(cls, v, info):
        if v is None:
            return v
        return _finite_or_raise(v, info.field_name)
