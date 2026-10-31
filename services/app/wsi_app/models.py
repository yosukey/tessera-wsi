import secrets
import time
from datetime import datetime
from typing import Optional
from sqlalchemy import String, Integer, BigInteger, Float, Boolean, ForeignKey, Text, DateTime, LargeBinary, func, text, UniqueConstraint, Index
from sqlalchemy.orm import Mapped, mapped_column, relationship, validates
from .db import Base


def _mint_public_id() -> str:
    """Insert-time default for slide/tour ``public_id``: a 64-bit URL-safe token.

    ``secrets.token_urlsafe(8)`` yields an 11-char token. 64 bits keeps the
    public URLs short while remaining infeasible to guess: the enumeration
    surface (``/api/slides|tours/{public_id}``) is nginx rate-limited to
    120 req/min/IP, so brute-forcing the ~1.8e19 space is astronomically out
    of reach; the creation-site retry loop absorbs the negligible birthday
    collision risk at this scale.

    Applied by the ORM on any INSERT that doesn't set ``public_id`` explicitly,
    so every row minted through the ORM (production creation paths, fixtures,
    scripts) carries a token — the public URL invariant holds everywhere. This
    is a *Python-side* default (not a DDL ``server_default``): it never emits a
    SQL DEFAULT, so ``ALTER TABLE ADD COLUMN`` adds a bare nullable column on
    existing DBs while the ORM default still stamps a token on every insert. The
    concurrency-sensitive creation sites additionally pass an explicit value
    inside a retry loop so a unique collision re-mints instead of 500-ing.
    """
    return secrets.token_urlsafe(8)


def tile_version(wsi_generation: object) -> int:
    """``Slide.wsi_generation`` as the ``?v=`` value of the slide's tile URLs.

    The one normalisation shared by the serializers that hand the value to the
    viewer and the /dzi/ routes that check it, so the two can never disagree on
    what "current" is. The column is NOT NULL with a server default of 1; a
    hand-edited row holding anything but a non-negative integer reads as 1.
    (0 passes through unchanged, so a replace's ``+ 1`` still moves the URL.)
    """
    if isinstance(wsi_generation, int) and not isinstance(wsi_generation, bool) and wsi_generation >= 0:
        return wsi_generation
    return 1


def fresh_wsi_generation() -> int:
    """A ``wsi_generation`` no earlier content of this ``public_id`` can have
    been served under: milliseconds since the epoch.

    Tile URLs are ``/dzi/{public_id}...?v={generation}`` and nginx caches them
    for 30 days, so a generation number must never be handed to different
    bytes. Two callers need a floor beyond "previous + 1":

    * a slide import keeping the bundle's token. A deleted slide's tiles stay
      cached under its old token and generations, so restarting a re-imported
      slide at 1 could hand out the *previous* incarnation's v=1 tiles for
      different bytes (e.g. the bundle was exported after a replace);
    * every WSI replace (``max(wsi_generation + 1, fresh_wsi_generation())``).
      A backup restore puts generations back to the backup's values, which
      are consistent with the restored files — but a replace after it could
      otherwise re-issue a number a later, discarded replace already used.

    The clock is larger than any generation a counter starting at 1 can have
    reached, and larger than any earlier clock-derived one (barring a clock
    stepped back further than the time since). Still an ordinary integer (well
    inside SQLite's 64 bits and JS's 2**53).
    """
    return max(1, time.time_ns() // 1_000_000)

class AppMeta(Base):
    """Key-value metadata about the DB itself.

    Holds ``app_version`` (the APP_VERSION string of the wsi-app binary that
    last opened this DB), ``app_version_seen_at``, and ``schema_version`` —
    a monotonic integer describing the *data shape* (bumped only when the
    schema changes; see the ``migrations`` package), independent of the
    human-facing ``app_version``. Together they are the source of truth for
    "what version of the schema/data does this file represent", consulted by
    the startup version-check, the schema-migration runner, and backup/restore.
    """
    __tablename__ = "app_meta"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[str] = mapped_column(String(255), default="")


def _initial_token_version() -> int:
    """Random starting ``User.token_version`` (see the column comment)."""
    return secrets.randbelow(2**31)


class User(Base):
    __tablename__ = "users"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    # Login name for admin accounts. Used as the bare credential the admin
    # types into the login form; not required to be an email address.
    username: Mapped[str] = mapped_column(String(255), unique=True, index=True)
    password_hash: Mapped[str] = mapped_column(String(255))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    # Role-based access control. One of "admin" / "manager" / "instructor".
    # `admin` has full system control (incl. user management & settings);
    # `manager` runs the slide library (upload / replace / metadata / delete)
    # and inherits everything `instructor` can do; `instructor` is the day-to-day
    # educator (slide viewing + publish toggle, tour CRUD, live sync presenter).
    # The role is fixed at user creation — change by deleting and recreating
    # the user. See routers/admin_users.py for the management surface.
    role: Mapped[str] = mapped_column(
        String(16), default="instructor", server_default="instructor", index=True,
    )
    # Brute-force protection: counter resets on success, lockout_until is the
    # UTC timestamp before which login attempts are rejected outright.
    failed_login_count: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    lockout_until: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True, default=None)
    # Token-revocation generation. Every issued staff JWT embeds the value of
    # this column at mint time (``tv`` claim); ``auth.resolve_staff_user``
    # rejects any token whose ``tv`` does not match the current column value.
    # Bumping it (on a password change, or a forced logout) therefore
    # invalidates every JWT minted before the bump WITHOUT waiting for the
    # 24h TTL — closing the window where a token leaked before a password
    # reset stays usable. The claim is mandatory on decode: a token without
    # a well-formed ``tv`` is rejected rather than defaulted to 0.
    #
    # New rows start at a random generation rather than 0. ``users.id`` has no
    # AUTOINCREMENT, so SQLite hands a deleted max-id row's id to the next user
    # created; with a fixed starting value a token still held by the deleted
    # user (``sub`` + ``tv=0``) would authenticate as the new account — an
    # admin, if that is what was created next. The random start makes the old
    # ``tv`` miss. ``server_default`` stays "0" for rows added outside the ORM.
    token_version: Mapped[int] = mapped_column(
        Integer, default=_initial_token_version, server_default="0",
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    # Optional TOTP second factor (see mfa.py). ``totp_secret_enc`` is the
    # base32 secret sealed with WSI_MFA_KEY; NULL means MFA is off for this
    # account. ``totp_enabled_at`` is informational (shown to the user and in
    # the admin list). ``totp_last_step`` is the 30 s step of the last code
    # accepted, so one code cannot be replayed inside its validity window.
    # ``mfa_recovery_codes`` is a JSON list of keyed digests of the still-unused
    # one-time recovery codes (a used one is removed from the list).
    totp_secret_enc: Mapped[Optional[str]] = mapped_column(String(255), nullable=True, default=None)
    totp_enabled_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True, default=None)
    totp_last_step: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True, default=None)
    mfa_recovery_codes: Mapped[Optional[str]] = mapped_column(Text, nullable=True, default=None)

class Slide(Base):
    __tablename__ = "slides"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)  # numeric slide id
    # Unguessable, URL-safe public identifier. This — not the sequential
    # integer id — is what appears in every public URL (viewer route, tile
    # source, macro, tours) so link-only ("unlisted") slides can't be found by
    # walking 1,2,3… The integer id stays internal (FKs, tile cache, analytics
    # storage, sync frames). NOT NULL: the public-URL invariant is enforced by
    # the DB, not merely by the ORM's insert default, so a write that bypasses
    # the ORM cannot leave a slide unreachable.
    # Mirrors wsi_filename_norm below. See ``_mint_public_id`` for why the
    # default is Python-side rather than a DDL server_default — which is also
    # why ``_column_add_clause`` can only ADD this column nullable, and why
    # making it NOT NULL on an existing database takes a table rebuild
    # rather than the boot-time reconciler.
    public_id: Mapped[str] = mapped_column(
        String(32), nullable=False, unique=True, index=True, default=_mint_public_id
    )
    title: Mapped[str] = mapped_column(String(255), default="")
    catalog_id: Mapped[str] = mapped_column(String(128), default="", index=True)
    wsi_filename: Mapped[str] = mapped_column(String(512), default="")
    # Case-insensitive normalised form of the *original* basename (the
    # ``safe_name`` portion, without the ``{id}_`` prefix that wsi_filename
    # carries on disk). The UNIQUE index here is the race-free backstop for
    # the app-level duplicate-name check in admin_slides: two near-simultaneous
    # uploads of the same filename can both pass the in-Python check (the
    # collision window between the pre-commit re-check and commit), but only
    # one of them can commit this column — the loser hits an IntegrityError
    # that the upload/replace handlers translate into the same 409 as the
    # app-level check. NULL while a slide row is still a pre-upload placeholder
    # (no file yet); SQLite permits multiple NULLs under a UNIQUE index, so
    # concurrent placeholders never clash.
    wsi_filename_norm: Mapped[Optional[str]] = mapped_column(
        String(512), nullable=True, unique=True, index=True, default=None
    )
    thumb_filename: Mapped[str] = mapped_column(String(512), default="")
    width: Mapped[int] = mapped_column(Integer, default=0)
    height: Mapped[int] = mapped_column(Integer, default=0)
    mpp_x: Mapped[float] = mapped_column(Float, default=0.0)
    mpp_y: Mapped[float] = mapped_column(Float, default=0.0)
    # Original MPP as read from the WSI file at upload time. Preserved separately
    # so admins can refer back to the file's value after manually overriding mpp_x/y.
    mpp_x_file: Mapped[float] = mapped_column(Float, default=0.0)
    mpp_y_file: Mapped[float] = mapped_column(Float, default=0.0)
    # Objective magnification recorded in the WSI file metadata (e.g. Aperio
    # ``AppMag``, Hamamatsu ``SourceLens``), normalised by OpenSlide as
    # ``openslide.objective-power``. 0 when the file did not record one — the
    # dashboard then falls back to the MPP-derived estimate (10 / mpp_x).
    objective_power_file: Mapped[float] = mapped_column(Float, default=0.0, server_default="0")
    vendor: Mapped[str] = mapped_column(String(128), default="")
    # BigInteger: uploads run up to 8 GiB (config.wsi_max_upload_bytes). On
    # SQLite every INTEGER is already 64-bit, so this is equivalent to Integer;
    # BigInteger just states the 64-bit intent explicitly.
    file_size_bytes: Mapped[int] = mapped_column(BigInteger, default=0)
    # Content generation of the WSI bytes behind this row: 1 for a new upload.
    # Every WSI replace sets it to ``max(wsi_generation + 1,
    # fresh_wsi_generation())`` (in SQL, in the same commit as the swapped
    # file's metadata), and an imported slide starts at a fresh clock value —
    # see ``fresh_wsi_generation`` for why both need the clock floor. It is the
    # ``?v=`` of the slide's tile URLs (``/dzi/{public_id}.dzi?v=N`` —
    # OpenSeadragon carries the descriptor's query onto every tile URL), and
    # nginx keys the shared tile cache on it, so a replace moves viewers onto
    # URLs no old tile was ever stored under instead of mixing old and new
    # tiles under one URL for the cache's 30-day TTL. The /dzi/ routes 404 a
    # ``v`` that is not current (see dzi.py), so one cache key only ever holds
    # one generation's bytes. Also compared by the full-backup snapshot check
    # (backup._FILE_GENERATION_COLUMNS).
    wsi_generation: Mapped[int] = mapped_column(
        Integer, nullable=False, default=1, server_default="1",
    )
    # Newly uploaded slides start as drafts; an admin must explicitly publish
    # them. Public list/detail endpoints filter unpublished slides out, and
    # toggling a slide back to draft cascades the slide's tours to draft too.
    is_published: Mapped[bool] = mapped_column(Boolean, default=False, server_default="0")
    # Link-only ("unlisted") visibility. Independent of is_published and only
    # meaningful once the slide is published: a published+unlisted slide stays
    # reachable by its direct URL (viewer, tiles, macro) but is hidden from the
    # public index listing, search results, and metadata facets — so it can be
    # shared by link without appearing in browse. No effect while unpublished
    # (a draft is already hidden everywhere).
    unlisted: Mapped[bool] = mapped_column(Boolean, default=False, server_default="0")
    # Per-slide overrides for viewer overlays. NULL = follow global default.
    show_zoom_indicator: Mapped[Optional[bool]] = mapped_column(Boolean, nullable=True, default=None)
    show_scale_bar: Mapped[Optional[bool]] = mapped_column(Boolean, nullable=True, default=None)
    show_metadata_panel: Mapped[Optional[bool]] = mapped_column(Boolean, nullable=True, default=None)
    # Per-slide image-adjustment preset (brightness/contrast/saturation),
    # applied as the viewer's starting CSS filter so dark scans are corrected
    # from first paint for every viewer. NULL = no preset for that channel
    # (effective value 1.0 = identity). Set by manager+ from the viewer's
    # adjust panel (with live preview) or the dashboard edit modal.
    image_adjust_brightness: Mapped[Optional[float]] = mapped_column(Float, nullable=True, default=None)
    image_adjust_contrast: Mapped[Optional[float]] = mapped_column(Float, nullable=True, default=None)
    image_adjust_saturation: Mapped[Optional[float]] = mapped_column(Float, nullable=True, default=None)
    # Vendor associated images (Aperio macro, Hamamatsu/MRXS label, etc.) extracted
    # from the WSI file at upload time. NULL when the source file does not expose
    # the corresponding image. Stored as already-downscaled JPEG bytes; the
    # raw on-file size can run to tens of MB and would dwarf the rest of the row
    # if the original were kept. Etag derives from the bytes for browser caching;
    # mimetype is always image/jpeg today but is recorded for future formats.
    # deferred: even downscaled the pair runs to a few hundred KB a slide, and
    # almost every Slide load wants only the scalar columns — analytics and
    # sync title lookups (the analytics CSV export resolves up to 10 000 ids at
    # once), the tile handle cache's per-miss path lookup, every admin edit.
    # As plain columns each of those would pull both blobs into the worker (300
    # slides -> 82 MB for one top-slides pass). Deferred, they load by a second
    # SELECT only when the attribute is read, which the macro / label image
    # endpoints do inside the request's session; nothing reads them on a
    # detached instance (that would raise DetachedInstanceError), and a query
    # that wanted them for many rows would add ``undefer(...)`` rather than
    # pay one SELECT per row. "Has an image" is ``macro_etag`` / ``label_etag``
    # (non-empty exactly when the bytes are stored), so URLs never need them.
    macro_data: Mapped[Optional[bytes]] = mapped_column(
        LargeBinary, nullable=True, default=None, deferred=True,
    )
    macro_mimetype: Mapped[str] = mapped_column(String(64), default="", server_default=text("''"))
    macro_etag: Mapped[str] = mapped_column(String(64), default="", server_default=text("''"))
    label_data: Mapped[Optional[bytes]] = mapped_column(
        LargeBinary, nullable=True, default=None, deferred=True,
    )
    label_mimetype: Mapped[str] = mapped_column(String(64), default="", server_default=text("''"))
    label_etag: Mapped[str] = mapped_column(String(64), default="", server_default=text("''"))
    # Acquisition / scan timestamp extracted from the WSI file at upload time.
    # Stored as the literal vendor-normalised ISO 8601 string (e.g.
    # "2024-09-12T14:32:01") so the frontend can format it without a second
    # round-trip; empty when the file does not record one.
    acquired_at: Mapped[str] = mapped_column(String(32), default="", server_default=text("''"))
    # Image credit: the rights holder of this WSI and the terms it is shown
    # under. Shown by the viewer whenever the slide is on screen, regardless of
    # show_metadata_panel. Validated by slide_credit (license_id is one of
    # slide_credit.LICENSE_IDS; source_url is http(s) only). "" = not set.
    credit_text: Mapped[str] = mapped_column(String(512), default="", server_default=text("''"))
    license_id: Mapped[str] = mapped_column(String(32), default="", server_default=text("''"))
    source_url: Mapped[str] = mapped_column(String(1024), default="", server_default=text("''"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    tours: Mapped[list["Tour"]] = relationship(back_populates="slide", cascade="all, delete-orphan")
    attributes: Mapped[list["SlideAttribute"]] = relationship(back_populates="slide", cascade="all, delete-orphan")


class SlideAttribute(Base):
    __tablename__ = "slide_attributes"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    slide_id: Mapped[int] = mapped_column(ForeignKey("slides.id", ondelete="CASCADE"), index=True)
    key: Mapped[str] = mapped_column(String(128), index=True)
    value: Mapped[str] = mapped_column(String(255), index=True)

    slide: Mapped["Slide"] = relationship(back_populates="attributes")

    __table_args__ = (
        UniqueConstraint("slide_id", "key", "value", name="uq_slide_attr_triplet"),
    )


class MetadataOption(Base):
    """Predefined metadata values available for selection when editing a slide.

    The admin dashboard's Metadata tab maintains this master list; slide
    metadata editors then select values from it rather than typing free text.
    """
    __tablename__ = "metadata_options"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    key: Mapped[str] = mapped_column(String(128), index=True)
    value: Mapped[str] = mapped_column(String(255), index=True)

    __table_args__ = (
        UniqueConstraint("key", "value", name="uq_metadata_option"),
    )

class Tour(Base):
    __tablename__ = "tours"
    # AUTOINCREMENT: a plain INTEGER PRIMARY KEY hands the id of a deleted
    # max-id row to the next insert, but analytics history (page_views,
    # tour_sessions) outlives a tour and is keyed by this id, so a recycled id
    # would credit the old tour's views to an unrelated new one.
    __table_args__ = {"sqlite_autoincrement": True}
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    # Unguessable public identifier for tour URLs and tour asset paths. Same
    # rationale, and same NOT NULL guarantee, as Slide.public_id: the integer
    # id stays internal while every public surface is keyed by this token so
    # unlisted tours can't be walked.
    public_id: Mapped[str] = mapped_column(
        String(32), nullable=False, unique=True, index=True, default=_mint_public_id
    )
    slide_id: Mapped[int] = mapped_column(ForeignKey("slides.id", ondelete="CASCADE"), index=True)
    title: Mapped[str] = mapped_column(String(255), default="Tour")
    description: Mapped[str] = mapped_column(Text, default="")
    is_published: Mapped[bool] = mapped_column(Boolean, default=False)
    # Link-only ("unlisted") visibility, mirroring Slide.unlisted. A
    # published+unlisted tour stays reachable by its direct URL but is hidden
    # from the public tour listing (/api/tours) and from its parent slide's
    # tour list, so students can only discover it via a shared link.
    unlisted: Mapped[bool] = mapped_column(Boolean, default=False, server_default="0")
    # Optimistic-concurrency counter for the tour's own fields (title,
    # description, is_published, unlisted) and its step *structure* (add,
    # delete, reorder) — not for a step's content, which Step.revision covers,
    # so editing one step never conflicts with retitling the tour. Bumped in
    # SQL (``revision = revision + 1``) by every such write; a request carrying
    # ``expected_revision`` makes it a conditional UPDATE that answers 409 on a
    # mismatch (routers/admin_tours.py). A constant server_default lets the
    # boot-time reconciler add it NOT NULL to existing databases, every
    # existing row starting at 0 — as do imported tours.
    revision: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    slide: Mapped["Slide"] = relationship(back_populates="tours")
    steps: Mapped[list["Step"]] = relationship(back_populates="tour", order_by="Step.order", cascade="all, delete-orphan")
    # The tour's own metadata, on top of what it inherits from its slide (a
    # tour's effective attributes are the union of the two). Covered by
    # ``revision`` like the tour's other fields.
    attributes: Mapped[list["TourAttribute"]] = relationship(back_populates="tour", cascade="all, delete-orphan")


class TourAttribute(Base):
    """A tour's own (key, value) metadata pair. Same shape and vocabulary
    (MetadataOption) as SlideAttribute; the two are kept as separate tables
    so each keeps its ON DELETE CASCADE to its owner."""
    __tablename__ = "tour_attributes"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    tour_id: Mapped[int] = mapped_column(ForeignKey("tours.id", ondelete="CASCADE"), index=True)
    key: Mapped[str] = mapped_column(String(128), index=True)
    value: Mapped[str] = mapped_column(String(255), index=True)

    tour: Mapped["Tour"] = relationship(back_populates="attributes")

    __table_args__ = (
        UniqueConstraint("tour_id", "key", "value", name="uq_tour_attr_triplet"),
    )

class Step(Base):
    __tablename__ = "steps"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    tour_id: Mapped[int] = mapped_column(ForeignKey("tours.id", ondelete="CASCADE"), index=True)
    order: Mapped[int] = mapped_column(Integer, default=0)
    label: Mapped[str] = mapped_column(String(255), default="")
    # level-0 pixel center
    center_x: Mapped[float] = mapped_column(Float, default=0.0)
    center_y: Mapped[float] = mapped_column(Float, default=0.0)
    # field of view in micrometers
    fov_um_w: Mapped[float] = mapped_column(Float, default=600.0)
    fov_um_h: Mapped[float] = mapped_column(Float, default=450.0)
    rotation_deg: Mapped[float] = mapped_column(Float, default=0.0)
    description_md: Mapped[str] = mapped_column(Text, default="")
    # How navigating away from this step to the next one is animated:
    #   "none"        — jump immediately
    #   "smooth"      — interpolate pan/zoom/rotation in one glide
    #                   (the default for new steps)
    #   "smooth_zoom" — zoom out to frame both fields, then zoom into the next,
    #                   revealing the travel path
    # Allowed values mirror schemas.TRANSITION_MODES.
    transition_mode: Mapped[str] = mapped_column(String(20), default="smooth")
    # Optimistic-concurrency counter for this step's own content: label,
    # description, view (center / FOV / rotation), transition mode, pins and
    # shapes. Its ``order`` is tour structure and bumps Tour.revision instead.
    # Same SQL bump / conditional-UPDATE contract and reconciler-friendly
    # default as Tour.revision.
    revision: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")

    tour: Mapped["Tour"] = relationship(back_populates="steps")
    # order_by pushes the sort into SQL so _serializers.step_to_out doesn't
    # have to re-sort in Python on every read of a step. Sort by the integer
    # primary key so the listing reflects insertion order: admin_update_step
    # rewrites the rows on every save and inserts them in editor-card order,
    # which is what the user expects to see again on reload. (Sorting by the
    # public `pin_id` / `shape_id` would give an alphabetic order over random
    # crypto-hex ids, shuffling the on-canvas index badges across saves.)
    shapes: Mapped[list["StepShape"]] = relationship(
        back_populates="step",
        cascade="all, delete-orphan",
        order_by="StepShape.id",
    )
    pins: Mapped[list["StepPin"]] = relationship(
        back_populates="step",
        cascade="all, delete-orphan",
        order_by="StepPin.id",
    )

    __table_args__ = (
        UniqueConstraint("tour_id", "order", name="uq_steps_tour_order"),
        # Never recycle a step id (see Tour): an editor still showing a
        # deleted step would otherwise address whatever step reused its id,
        # in this tour or another one.
        {"sqlite_autoincrement": True},
    )


class StepPin(Base):
    __tablename__ = "step_pins"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    pin_id: Mapped[str] = mapped_column(String(64), default="")
    step_id: Mapped[int] = mapped_column(ForeignKey("steps.id", ondelete="CASCADE"), index=True)
    x: Mapped[float] = mapped_column(Float, default=0.0)
    y: Mapped[float] = mapped_column(Float, default=0.0)
    shape: Mapped[str] = mapped_column(String(16), default="circle")
    color: Mapped[str] = mapped_column(String(16), default="red")
    rotation_deg: Mapped[float] = mapped_column(Float, default=0.0)
    label: Mapped[str] = mapped_column(String(255), default="")
    # Stacking order (higher = on top), shared with StepShape.z. Pins default
    # to 1000000 and shapes to 0, so with no explicit reordering pins sit above
    # shapes (the desired "pins above shapes" default).
    z: Mapped[int] = mapped_column(Integer, default=1000000)

    step: Mapped["Step"] = relationship(back_populates="pins")

    __table_args__ = (
        UniqueConstraint("step_id", "pin_id", name="uq_step_pin"),
    )


class StepShape(Base):
    """Tour-step shape annotation. Stores rectangles, circles, and freehand
    strokes, discriminated by `kind`. For freehand, the bounding box
    (x, y, w, h) is computed from `points` and used for label positioning
    + overlay placement."""

    __tablename__ = "step_shapes"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    shape_id: Mapped[str] = mapped_column(String(64), default="")
    step_id: Mapped[int] = mapped_column(ForeignKey("steps.id", ondelete="CASCADE"), index=True)
    # 'rect' | 'circle' | 'freehand'
    kind: Mapped[str] = mapped_column(String(16), default="rect")
    x: Mapped[float] = mapped_column(Float, default=0.0)
    y: Mapped[float] = mapped_column(Float, default=0.0)
    w: Mapped[float] = mapped_column(Float, default=1.0)
    h: Mapped[float] = mapped_column(Float, default=1.0)
    # Stroke color: a preset name (red/orange/...) or a CSS hex like #aabbcc.
    stroke_color: Mapped[str] = mapped_column(String(32), default="red")
    fill_enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    # Fill opacity in [0, 1]; ignored when fill_enabled is false.
    fill_opacity: Mapped[float] = mapped_column(Float, default=0.5)
    # JSON-encoded list of {x, y} points for freehand strokes; empty for
    # rect/circle. Stored as text so SQLite has no native JSON dep.
    points_json: Mapped[str] = mapped_column(Text, default="")
    # Freehand-only: render the stroke as a smoothed curve (a client-side
    # low-pass of the vertices) instead of straight segments. Defaults True;
    # rect/circle ignore it. Purely a render hint — the stored points are
    # untouched. See lib/tourShapes.pointsToSvgPath.
    smoothing: Mapped[bool] = mapped_column(Boolean, default=True)
    label: Mapped[str] = mapped_column(String(255), default="")
    label_position: Mapped[str] = mapped_column(String(16), default="above")
    # Stacking order (higher = on top), shared with StepPin.z. Shapes default to
    # 0 and pins to 1000000, so pins sit above shapes unless reordered. See
    # StepPin.z.
    z: Mapped[int] = mapped_column(Integer, default=0)

    step: Mapped["Step"] = relationship(back_populates="shapes")

    __table_args__ = (
        UniqueConstraint("step_id", "shape_id", name="uq_step_shape"),
    )


class ViewerSettings(Base):
    """Singleton table (id=1) storing global viewer display settings."""
    __tablename__ = "viewer_settings"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, default=1)
    logo_data: Mapped[Optional[bytes]] = mapped_column(LargeBinary, nullable=True, default=None)
    logo_mimetype: Mapped[str] = mapped_column(String(64), default="")
    logo_etag: Mapped[str] = mapped_column(String(64), default="", server_default="")
    favicon_data: Mapped[Optional[bytes]] = mapped_column(LargeBinary, nullable=True, default=None)
    favicon_mimetype: Mapped[str] = mapped_column(String(64), default="")
    favicon_etag: Mapped[str] = mapped_column(String(64), default="", server_default="")
    copyright_msg: Mapped[str] = mapped_column(String(512), default="")
    # JSON list of slide attribute keys to display, e.g. '["stain","organ"]'
    extra_attrs: Mapped[str] = mapped_column(String(1024), default="[]")
    # JSON list of slide attribute keys (in order) shown on the top-page
    # slide cards / rows. Presence = show, position = display order.
    index_card_attrs: Mapped[str] = mapped_column(
        String(1024),
        default='["stain","organ","lab"]',
        server_default=text("""'["stain","organ","lab"]'"""),
    )
    # JSON list of slide attribute keys (in order) used for the top-page
    # filter facet panel. Categories whose values are absent from the
    # published catalogue are still hidden client-side.
    index_filter_attrs: Mapped[str] = mapped_column(
        String(1024),
        default='["stain","organ","lab","tags"]',
        server_default=text("""'["stain","organ","lab","tags"]'"""),
    )
    site_title: Mapped[str] = mapped_column(String(256), default="")
    footer_text: Mapped[str] = mapped_column(String(1024), default="")
    # Global defaults for viewer overlays; per-slide overrides live on Slide.
    show_zoom_indicator: Mapped[bool] = mapped_column(Boolean, default=False)
    show_scale_bar: Mapped[bool] = mapped_column(Boolean, default=True)
    show_metadata_panel: Mapped[bool] = mapped_column(Boolean, default=True)
    # Default top-page filter: "all" | "slides" | "tours"
    default_index_type: Mapped[str] = mapped_column(String(16), default="all")
    # Top-page display options
    show_filter_button: Mapped[bool] = mapped_column(Boolean, default=False)
    default_view_mode: Mapped[str] = mapped_column(String(8), default="tile")
    default_sort_field: Mapped[str] = mapped_column(String(16), default="catalog_id")
    # Order of the top-page filter chips within each category:
    #   "count" -> most-used values first (the default)
    #   "alpha" -> alphabetical by value
    filter_sort_order: Mapped[str] = mapped_column(String(8), default="count")
    # Maximum allowed digital-zoom past native (1:1). 1.0 disables digital zoom;
    # 2.0 lets the viewer zoom in to 2× the image's native pixel resolution.
    max_zoom_pixel_ratio: Mapped[float] = mapped_column(Float, default=1.0)
    # Show a link to the admin dashboard on the top (index) page. Default off.
    show_admin_link: Mapped[bool] = mapped_column(Boolean, default=False)
    # Default thumbnail size on the top page: "sm" | "md" | "lg"
    default_thumbnail_size: Mapped[str] = mapped_column(String(4), default="lg")
    # Top-page slide list cap. The page is single-fetch (no pagination), so
    # this bounds both the SQL LIMIT and the number of cards the browser
    # paints. Default 2000 fits typical educational catalogues with margin;
    # the hard upper bound (enforced at the API and admin form) is 5000 to
    # keep the single-shot page from drifting into territory that needs
    # virtualisation instead.
    max_index_items: Mapped[int] = mapped_column(Integer, default=2000, server_default="2000")
    # How many of a slide's tours the top page lists inline beneath that slide
    # in the "All (slides & tours)" view, before the rest collapse behind the
    # card's / row's expander. A display cap only — the tours are always
    # fetched and always one click away. 0 lists none by default. The GUI and
    # the API clamp this to [0, 50] (see _serializers.normalize_index_tours_
    # per_slide); default 3 keeps tile cards compact.
    index_tours_per_slide: Mapped[int] = mapped_column(Integer, default=3, server_default="3")
    # ----- Advanced viewer rendering tuning (OpenSeadragon) -----
    # immediate_render: after a zoom, fetch the display-resolution tiles first
    # and skip in-between levels (True, the default) vs. fetch coarse levels
    # first and refine step by step (False). It orders tile loading only; the
    # coarser tiles in memory are drawn as a placeholder either way. See
    # docs/developer-guide.md "Viewer rendering tuning".
    immediate_render: Mapped[bool] = mapped_column(Boolean, default=True, server_default="1")
    # max_image_cache_count: number of decoded tile images OpenSeadragon keeps
    # in the browser's in-memory TileCache (a count of tiles, NOT bytes). The
    # GUI clamps this to [100, 1000]; the server re-clamps on both read and
    # write so a stale/hand-edited row can't ship an out-of-range value to the
    # viewer. Default 200 matches OpenSeadragon's own default.
    max_image_cache_count: Mapped[int] = mapped_column(Integer, default=200, server_default="200")
    # Always-on header background (lowest priority below any active seasonal slot)
    header_default_image_data: Mapped[Optional[bytes]] = mapped_column(LargeBinary, nullable=True, default=None)
    header_default_image_mimetype: Mapped[str] = mapped_column(String(64), default="", server_default="")
    header_default_image_etag: Mapped[str] = mapped_column(String(64), default="", server_default="")
    # Always-on title text color (CSS hex, e.g. "#1a2b3c"); empty means use CSS default
    header_default_title_color: Mapped[str] = mapped_column(String(16), default="", server_default="")


class SeasonalBackground(Base):
    """One of up to 5 admin-configured date-range backgrounds for the top page.

    Each row (id=1..5) declares a calendar window (start_month/day to
    end_month/day) and an optional tiled image. The window must satisfy
    start <= end on the calendar (year-wrap is disallowed; split into
    two slots if needed). The frontend computes which slot, if any,
    matches the current local date and uses that image as the tiled
    background of the IndexHeader title bar; when multiple slots match,
    the higher-numbered slot wins.
    """
    __tablename__ = "seasonal_backgrounds"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)  # 1..5
    enabled: Mapped[bool] = mapped_column(Boolean, default=False, server_default="0")
    label: Mapped[str] = mapped_column(String(128), default="", server_default="")
    start_month: Mapped[int] = mapped_column(Integer, default=1, server_default="1")
    start_day: Mapped[int] = mapped_column(Integer, default=1, server_default="1")
    end_month: Mapped[int] = mapped_column(Integer, default=1, server_default="1")
    end_day: Mapped[int] = mapped_column(Integer, default=1, server_default="1")
    image_data: Mapped[Optional[bytes]] = mapped_column(LargeBinary, nullable=True, default=None)
    image_mimetype: Mapped[str] = mapped_column(String(64), default="", server_default="")
    image_etag: Mapped[str] = mapped_column(String(64), default="", server_default="")
    title_color: Mapped[str] = mapped_column(String(16), default="", server_default="")


class RolePermissions(Base):
    """Singleton (id=1) storing role-permission flags.

    The admin/manager/instructor role hierarchy fixes most permissions, but
    a small set of "may this non-admin role see this read-only view" toggles
    are operator-configurable so an admin can grant Analytics or Server
    visibility to managers or instructors without bumping them to admin.
    Default for every flag is False (least privilege); admin always sees
    everything regardless of these flags. The row is lazy-initialised by
    the admin endpoints — startup does not need to seed it.
    """
    __tablename__ = "role_permissions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, default=1)
    manager_can_view_analytics: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default="0",
    )
    manager_can_view_server: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default="0",
    )
    instructor_can_view_analytics: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default="0",
    )
    instructor_can_view_server: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default="0",
    )


class NetworkSettings(Base):
    """Singleton (id=1): IP-based access filter and mDNS advertisement settings."""
    __tablename__ = "network_settings"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, default=1)
    ip_filter_enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    ip_filter_default: Mapped[str] = mapped_column(String(8), default="allow")  # "allow" | "deny"
    ip_rules: Mapped[str] = mapped_column(Text, default="[]")  # JSON [{ip, action, note}]
    mdns_enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    ga_measurement_id: Mapped[str] = mapped_column(String(64), default="")
    # Access-analytics recording mode:
    #   "off"     — no analytics rows written at all
    #   "hashed"  — user identity recorded as HMAC-SHA256(salt, email),
    #               anonymising per-row identity at storage time
    #   "plain"   — user identity recorded as the verified email / username
    # Default is "off" so a fresh install records nothing until the operator
    # opts in. Switching to "hashed" requires ANALYTICS_HASH_SALT to be set
    # in the environment; the PATCH endpoint enforces this guard.
    analytics_mode: Mapped[str] = mapped_column(
        String(16), default="off", server_default="off",
    )
    # When True (and analytics_mode != "off"), the frontend shows a one-time
    # per-session toast informing the viewer that their activity is recorded.
    # Operators who have already obtained explicit consent through another
    # channel (employee handbook, separate consent form, etc.) can disable
    # this without disabling the underlying recording.
    analytics_notice_toast_enabled: Mapped[bool] = mapped_column(
        Boolean, default=True, server_default="1",
    )


class AuthSettings(Base):
    """Singleton (id=1): app-access authentication mode and provider config.

    The dashboard's Network tab → Authentication section writes to this row.
    Sensitive values (LDAP bind password, OAuth client secret) are stored
    here; the GET endpoint never returns them in plaintext.
    """
    __tablename__ = "auth_settings"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, default=1)
    # "none" | "basic" | "ldap" | "google_oauth"
    mode: Mapped[str] = mapped_column(String(16), default="none")

    # ---- LDAP fields ----
    ldap_server_uri: Mapped[str] = mapped_column(String(512), default="")
    ldap_bind_dn: Mapped[str] = mapped_column(String(512), default="")
    ldap_bind_password: Mapped[str] = mapped_column(String(512), default="")
    ldap_user_base_dn: Mapped[str] = mapped_column(String(512), default="")
    # Search filter; "{username}" is replaced with the entered username.
    ldap_user_filter: Mapped[str] = mapped_column(String(512), default="(uid={username})")
    ldap_use_starttls: Mapped[bool] = mapped_column(Boolean, default=False)

    # ---- Google OAuth fields ----
    google_client_id: Mapped[str] = mapped_column(String(512), default="")
    google_client_secret: Mapped[str] = mapped_column(String(512), default="")
    # Comma-separated allowed email domains, e.g. "example.com,example.org".
    # A domain admits only Google Workspace accounts whose ``hd`` claim is on
    # the list (see app_auth.google_email_allowed). Empty together with
    # google_allowed_emails = no Google account can sign in (fail closed).
    google_allowed_domains: Mapped[str] = mapped_column(String(512), default="")
    # Comma-separated individually allowed email addresses, e.g.
    # "alice@example.com,bob@example.net". Matched exactly (case-insensitive)
    # and without the Workspace ``hd`` requirement, so an operator can admit a
    # named outside collaborator or personal Google account on top of the
    # domain list.
    google_allowed_emails: Mapped[str] = mapped_column(
        String(4096), default="", server_default="",
    )

    session_ttl_seconds: Mapped[int] = mapped_column(Integer, default=86400)

    # Monotonic app-session generation. Embedded as the ``gen`` claim in every
    # app-access cookie at mint time (app_auth.create_session_token) and checked
    # on decode: a request whose cookie carries an older generation is rejected.
    # Bumped whenever the auth policy is tightened (mode switch, allowed-domain
    # change) or an operator explicitly revokes, so those actions invalidate all
    # already-issued sessions immediately instead of waiting out the TTL. The
    # claim is mandatory on decode: a cookie without a well-formed ``gen`` is
    # rejected rather than defaulted to generation 0.
    session_generation: Mapped[int] = mapped_column(
        Integer, default=0, server_default=text("0"),
    )

    # ---- Login screen customization ----
    # Optional operator overrides for the app-access /login page. Both are
    # surfaced (unauthenticated) via /api/auth/state and applied to every
    # mode; an empty value (the default) falls back to the built-in per-mode
    # i18n strings in the frontend. This lets an organisation using a custom
    # Google Workspace domain replace, e.g., "sign in with your Google
    # account" — which is misleading for a custom domain — with neutral
    # wording, and relabel the button to a generic "Sign in".
    login_message: Mapped[str] = mapped_column(Text, default="", server_default="")
    login_button_label: Mapped[str] = mapped_column(
        String(128), default="", server_default="",
    )

    # How far the "skip authentication for direct LAN access" exemption
    # reaches. Three levels, all of which leave the Cloudflare Tunnel path
    # fully gated (so the public hostname is never affected):
    #
    #   "off" — no exemption; every request is gated by `mode` (default).
    #   "ip"  — only a *bare private-IP literal* Host from a LAN client is
    #           admitted, i.e. the visitor typed the server's LAN IP straight
    #           into the address bar (e.g. http://192.168.1.50/). A
    #           hostname/vhost stays gated, so the regular login UX is intact.
    #   "lan" — any direct (non-tunnel) request from a LAN client is admitted,
    #           whether by bare IP *or* a LAN-direct hostname/vhost
    #           (e.g. https://lan.wsi.example.org/).
    #
    # "ip" is the narrower option; "lan" is the broader trusted-segment
    # convenience. Both assume LAN clients are already trusted/authenticated
    # by other means. See app_auth.lan_direct_exempt for the trust argument.
    lan_direct_noauth_mode: Mapped[str] = mapped_column(
        String(8), default="off", server_default="off",
    )


def auth_username_key(username: str) -> str:
    """Case-insensitive identity of a basic login name.

    Must stay exactly the normalisation ``analytics_hash.compute_user_hash``
    applies to the ``basic:<username>`` identifier (strip + Python
    ``str.lower``): two accounts the registration check lets coexist must
    never collapse into one pseudonymous analytics identity. SQLite's own
    ``lower()`` only folds ASCII, which is why the uniqueness check runs on
    this stored key rather than on ``func.lower(username)``."""
    return (username or "").strip().lower()


class AuthUser(Base):
    """Local username/password records used when AuthSettings.mode == 'basic'."""
    __tablename__ = "auth_users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    username: Mapped[str] = mapped_column(String(128), unique=True, index=True)
    # auth_username_key(username), kept in step by the validator below. The
    # UNIQUE index is the race-free backstop for "no two accounts differing
    # only by case", Unicode included. Every write sets it; the column is
    # nullable, as the boot-time reconciler adds columns.
    username_key: Mapped[Optional[str]] = mapped_column(
        String(128), unique=True, index=True, nullable=True,
    )
    password_hash: Mapped[str] = mapped_column(String(255))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    @validates("username")
    def _sync_username_key(self, _attr, value):
        self.username_key = auth_username_key(value)
        return value


class PageView(Base):
    """Raw page-view event recorded by the in-app analytics beacon.

    No FK to slides/tours/users: deleting a slide/tour/user must not
    cascade away historical access logs, and the snapshot `user_email`
    (identity captured at access time) must be retained even if the
    user later changes their identity.
    """
    __tablename__ = "page_views"
    # Every default dashboard aggregation filters `actor_role IS NULL` plus an
    # `occurred_at` window. With only single-column indexes SQLite picks the
    # actor_role one (nearly every row is NULL) and scans the whole history;
    # this composite serves both predicates. Created on existing DBs by
    # main._reconcile_indexes.
    # The kind-filtered rankings (top slides `kind IN (viewer, tour)`, top
    # tours `kind = tour`) have the same problem once "include staff" lifts the
    # actor_role clause: SQLite picks the single-column `kind` index and walks
    # every row of that kind in the whole history; (kind, occurred_at) turns
    # it into a window seek per kind. That composite also serves every lookup
    # a single-column `kind` index would (`kind` is its prefix), so `kind`
    # carries no index of its own — one fewer index for every beacon INSERT to
    # update. A DB that has an ix_page_views_kind index drops it at boot
    # (main._RETIRED_INDEXES). On a DB that lacks the (kind, occurred_at)
    # composite it is built once, at boot — a full pass over page_views under
    # the boot's write lock (seconds per million rows), not a recurring cost.
    __table_args__ = (
        Index("ix_page_views_actor_role_occurred_at", "actor_role", "occurred_at"),
        Index("ix_page_views_kind_occurred_at", "kind", "occurred_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    occurred_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), index=True
    )
    # 'top' | 'viewer' | 'tour'. Indexed through ix_page_views_kind_occurred_at
    # (see __table_args__).
    kind: Mapped[str] = mapped_column(String(8))
    slide_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True, index=True)
    tour_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True, index=True)
    client_ip: Mapped[str] = mapped_column(String(64), default="", index=True)
    user_agent: Mapped[str] = mapped_column(String(512), default="")
    # 'desktop' | 'mobile' | 'tablet' | 'bot' | 'other'
    ua_class: Mapped[str] = mapped_column(String(16), default="other", index=True)
    referrer_host: Mapped[str] = mapped_column(String(255), default="")
    # users.id when the beacon carried a live staff Bearer: the SPA forwards
    # the staff token from sessionStorage whenever one is present, so a
    # logged-in admin / manager / instructor browsing public pages lands
    # here (and gets `actor_role` below, which is what keeps their QA
    # traversals out of the default aggregations). NULL for everyone else,
    # including app-access (basic / ldap / Google) and Cloudflare Access
    # users, whose identity is only the `user_email` snapshot. In
    # analytics_mode="hashed" this stays NULL, client_ip holds a keyed hash
    # and user_agent is empty, so nothing beside the hashed user_email
    # re-identifies the viewer.
    user_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True, index=True)
    # Display-friendly identity snapshot. The column name is narrower than its
    # contents; depending on the auth source it can be a bare username or an
    # email:
    #   - admin: the User.username at the time of access
    #   - app-access basic/ldap: the session subject (username)
    #   - app-access google_oauth: the verified Google email
    #   - Cloudflare Access: the verified email from the JWT
    #   - unauthenticated: ""
    user_email: Mapped[str] = mapped_column(String(255), default="", index=True)
    # Role of the authenticated staff member at access time when the beacon
    # forwarded a valid staff JWT (admin/manager/instructor login). NULL for
    # learner / anonymous traffic, and for app-access-only sessions (those
    # never carry an admin Bearer). Used by the analytics endpoints to split
    # staff-vs-learner views: by default the aggregations filter to
    # `actor_role IS NULL` so staff QA traversals don't pollute educational
    # access metrics; the dashboard exposes an "Include staff" toggle to lift
    # that filter when needed.
    actor_role: Mapped[Optional[str]] = mapped_column(
        String(16), default=None, server_default=None, nullable=True, index=True,
    )


class SlideDwellEvent(Base):
    """Per-session slide dwell time recorded by the viewer-page beacon.

    One row per "user opens viewer → user leaves viewer" cycle. Only
    written when NetworkSettings.analytics_mode != "off" AND the visitor
    is authenticated (matching the per-user contract of PageView's
    `user_email` column). Anonymous traffic is intentionally excluded:
    the metric is "which named users spent how long on which slides",
    so a row without an identifier would add noise without analytic value.

    No FK to slides/users: deleting either must not cascade away
    historical access records, and the identity snapshot stored here is
    the value resolved at access time (plaintext or hash, depending on
    the analytics_mode that was active when the row was written).
    """
    __tablename__ = "slide_dwell_events"
    # `actor_role IS NULL` + `started_at` window — see PageView's index.
    __table_args__ = (
        Index("ix_slide_dwell_events_actor_role_started_at", "actor_role", "started_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    slide_id: Mapped[int] = mapped_column(Integer, index=True, nullable=False)
    # Client-minted session id (UUID v4 as 32 hex chars), mirroring
    # TourSession.session_id. Lets the viewer-page beacon send incremental
    # updates (visibilitychange-hidden / pagehide / unmount) that reconcile
    # into a single row via max(dwell_ms) rather than a "fire exactly
    # once on the first hidden event" scheme, which would lose every second of
    # dwell after the user returns from a backgrounded tab. Nullable so a
    # scripted caller that omits it still records a one-shot row (no
    # reconciliation, one row per beacon).
    session_id: Mapped[Optional[str]] = mapped_column(
        String(36), unique=True, index=True, nullable=True, default=None
    )
    # Same dual-purpose semantics as PageView.user_email: a username, an
    # email, or an HMAC-hex hash depending on which mode was active when
    # the row was written. NOT NULL because anonymous dwell isn't recorded.
    user_identifier: Mapped[str] = mapped_column(String(255), index=True, nullable=False)
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True
    )
    ended_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    # Pre-computed dwell so per-row queries don't need to compute (ended_at
    # - started_at) in SQL; clamped by the writer to a sensible upper bound
    # so an idle tab open for days doesn't skew aggregates.
    dwell_ms: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    # Staff role at access time when the beacon forwarded a valid staff JWT
    # (admin/manager/instructor). NULL for learner / anonymous traffic. The
    # dwell aggregations filter `actor_role IS NULL` by default so staff QA
    # traversals don't pollute the educational dwell averages — parity with
    # PageView.actor_role.
    actor_role: Mapped[Optional[str]] = mapped_column(
        String(16), default=None, server_default=None, nullable=True, index=True,
    )


class TourSession(Base):
    """A single user's pass through a tour, from arrival to last step.

    Written by the tour-page beacon. Each row represents one session
    (load → navigate steps → either reach the last step or leave).
    ``completed`` is true only when the user reached the final step;
    abandoned sessions land with ``completed=False``, ``completed_at=NULL``,
    and ``last_step_index`` at whichever step the user was on when the
    beacon last reported. The frontend reports both intermediate progress
    (so a tab-close mid-tour is captured) and a final completion event.

    No FK to tours/users for the same reason as SlideDwellEvent: history
    must survive deletion of either side, and identity is snapshotted.
    """
    __tablename__ = "tour_sessions"
    # `actor_role IS NULL` + `started_at` window — see PageView's index.
    __table_args__ = (
        Index("ix_tour_sessions_actor_role_started_at", "actor_role", "started_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    # Client-minted session identifier (UUID v4 as 32 hex chars). Lets the
    # frontend send incremental updates ("now on step 3", "completed at T")
    # without having to remember an autoincrement id from the server.
    session_id: Mapped[str] = mapped_column(String(36), unique=True, index=True, nullable=False)
    tour_id: Mapped[int] = mapped_column(Integer, index=True, nullable=False)
    user_identifier: Mapped[str] = mapped_column(String(255), index=True, nullable=False)
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True
    )
    # NULL until the user reaches the final step; the writer fills it on
    # the completion event, which doubles as the row's "did the user finish?"
    # signal (completed=true iff completed_at is not NULL).
    completed_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    last_step_index: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    total_steps: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    completed: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    # Total time spent in the tour (last update minus started_at, clamped).
    # Tracked separately from completed_at - started_at so an abandoned
    # session still yields a meaningful "user spent N seconds before quitting"
    # data point.
    total_dwell_ms: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    # Staff role at access time when the start beacon forwarded a valid staff
    # JWT (admin/manager/instructor). NULL for learner / anonymous traffic.
    # The tour-completion aggregation filters `actor_role IS NULL` by default
    # so staff QA traversals don't pollute completion-rate / dwell averages —
    # parity with PageView.actor_role.
    actor_role: Mapped[Optional[str]] = mapped_column(
        String(16), default=None, server_default=None, nullable=True, index=True,
    )


class AuditLog(Base):
    """Persisted admin audit event — the DB twin of the ``wsi_app.audit``
    JSON log line emitted by ``admin_helpers.audit()``.

    No FK to users: actor identity is a snapshot taken at event time and
    must survive user deletion (same rationale as PageView). The journald
    line remains the primary, tamper-evident record; this table exists so
    the history is queryable from the admin UI without log access. The
    table is capped by ``WSI_AUDIT_LOG_MAX_ROWS`` / ``_MAX_AGE_DAYS``
    (see ``audit_log.prune_audit_logs``).
    """
    __tablename__ = "audit_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    occurred_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), index=True
    )
    # NULL for system events (version upgrades) and unauthenticated events
    # (login failures, token-authenticated downloads).
    actor_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True, index=True)
    actor_username: Mapped[str] = mapped_column(String(255), default="", index=True)
    actor_role: Mapped[Optional[str]] = mapped_column(String(16), nullable=True, default=None)
    # Dotted event name, e.g. "slide.upload", "auth.settings.update".
    action: Mapped[str] = mapped_column(String(64), index=True)
    target: Mapped[Optional[str]] = mapped_column(String(512), nullable=True, default=None)
    # JSON-encoded ``**extra`` payload of the audit() call; "" when empty.
    details: Mapped[str] = mapped_column(Text, default="")


class ErrorLog(Base):
    """Persisted application error/warning log — the DB twin of the
    ``wsi_app.*`` records emitted through the stdlib ``logging`` module.

    A ``logging`` handler installed on the ``wsi_app`` parent logger (see
    ``error_log.DBLogHandler``) copies every WARNING+ record into this
    table so an admin can triage bugs from the dashboard without shell
    access to journald. journald remains the primary, unabridged record;
    this table is the queryable convenience copy, exactly like
    ``AuditLog`` is for the ``wsi_app.audit`` line.

    No FK to users: an error has no actor, and the table must survive user
    deletion (same rationale as PageView / AuditLog). The table is capped
    by ``WSI_ERROR_LOG_MAX_ROWS`` / ``_MAX_AGE_DAYS`` (see
    ``error_log.prune_error_logs``).
    """
    __tablename__ = "error_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    occurred_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), index=True
    )
    # Record level name, e.g. "WARNING" / "ERROR" / "CRITICAL".
    level: Mapped[str] = mapped_column(String(16), index=True)
    # Logger name, e.g. "wsi_app.routers.sync".
    logger: Mapped[str] = mapped_column(String(255), index=True)
    # Formatted log message (record.getMessage()).
    message: Mapped[str] = mapped_column(Text, default="")
    # Source location of the log call.
    module: Mapped[Optional[str]] = mapped_column(String(255), nullable=True, default=None)
    func: Mapped[Optional[str]] = mapped_column(String(255), nullable=True, default=None)
    line: Mapped[Optional[int]] = mapped_column(Integer, nullable=True, default=None)
    # Formatted exception traceback when the record carried exc_info; "" when
    # none. Length-capped by the handler (see error_log._TRACEBACK_MAX_BYTES).
    traceback: Mapped[str] = mapped_column(Text, default="")
    # Request context for unhandled-exception records (path + method only;
    # never the request body — see the exception handler in main.py).
    request_path: Mapped[Optional[str]] = mapped_column(String(512), nullable=True, default=None)
    request_method: Mapped[Optional[str]] = mapped_column(String(8), nullable=True, default=None)


class PerfRun(Base):
    """One performance measurement from the admin Performance page.

    ``kind`` is ``"server"`` for a benchmark the app ran on itself (see
    ``perf_bench``) or ``"client"`` for the browser-side tile / network test,
    whose results the admin's browser posts back. The row doubles as the
    cross-worker job record while a server run is in flight: the worker
    thread writes ``progress_json`` as it goes, any worker can answer the
    polling GETs from it, and a cancel request from any worker lands in
    ``cancel_requested``, which the running thread polls.

    At most one row may be ``running`` at a time — the partial unique index
    makes a second concurrent start fail at INSERT, so two admins (or two
    workers) can't both pass the "is one running?" check and load the server
    twice. ``pid`` lets a start request reap a row orphaned by a worker that
    died mid-run. No FK to users: the actor is a snapshot, like AuditLog.
    """
    __tablename__ = "perf_runs"
    __table_args__ = (
        Index(
            "uq_perf_runs_single_running", "state",
            unique=True, sqlite_where=text("state = 'running'"),
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    kind: Mapped[str] = mapped_column(String(16), index=True)
    # "running" | "succeeded" | "failed" | "cancelled"
    state: Mapped[str] = mapped_column(String(16))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), index=True
    )
    finished_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True, default=None)
    actor_username: Mapped[str] = mapped_column(String(255), default="")
    pid: Mapped[Optional[int]] = mapped_column(Integer, nullable=True, default=None)
    cancel_requested: Mapped[bool] = mapped_column(Boolean, default=False)
    # JSON documents; "" when not (yet) set.
    params_json: Mapped[str] = mapped_column(Text, default="")
    progress_json: Mapped[str] = mapped_column(Text, default="")
    result_json: Mapped[str] = mapped_column(Text, default="")
    # ``result_json``'s "summary", stored on its own so the history list
    # reads a few hundred bytes per run rather than parsing every full
    # result (a browser run's is up to 128 KB). NULL = not extracted yet (a
    # row with no summary stored: the history list derives it from the
    # result and writes it back, see admin_perf._backfill_summaries).
    summary_json: Mapped[Optional[str]] = mapped_column(Text, nullable=True, default=None)
    error: Mapped[str] = mapped_column(Text, default="")
