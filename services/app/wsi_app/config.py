import ipaddress
import logging
import os
import re
from dataclasses import dataclass

# Default data directory for native Ubuntu deployment. In systemd-managed
# production this is set via EnvironmentFile (/etc/wsi/wsi.env). For local
# development, an unset DATA_DIR falls through to the native path so the
# application can boot without extra environment wiring.
_DEFAULT_DATA_DIR = "/var/lib/wsi"
# Sentinel value meaning "operator has not set ADMIN_BOOTSTRAP_PASSWORD".
# bootstrap_admin() refuses to insert an admin row when this default is in
# effect unless WSI_ALLOW_INSECURE_DEFAULTS=1, so a fresh deploy that forgot
# to wire ADMIN_BOOTSTRAP_PASSWORD into wsi.env fails loudly instead of
# silently creating a known-credential admin.
ADMIN_BOOTSTRAP_PASSWORD_DEFAULT = "__unset__"


def env_int(name: str, default: int) -> int:
    """Read an integer setting, naming the variable when the value is malformed.

    A bare ``int(os.getenv(...))`` raises ``ValueError: invalid literal for
    int() with base 10: '2O'`` at *import* time, so a single typo in
    /etc/wsi/wsi.env crash-loops every worker under ``Restart=on-failure`` with
    a journald traceback that never says which variable is at fault. Fail with
    the name and the offending value instead — the operator can then fix it
    without bisecting the env file.
    """
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return int(raw.strip())
    except ValueError:
        raise ValueError(
            f"{name} must be an integer; got {raw!r}. "
            f"Fix it in the environment file (or unset it to use {default})."
        ) from None


def env_int_min(name: str, default: int, minimum: int) -> int:
    """``env_int`` raised to ``minimum`` (with a warning) when set below it.

    For settings where zero or a negative number is not "off" but a broken
    server: anyio with 0 thread tokens parks every sync endpoint forever, and
    a negative pool size is not a pool.
    """
    value = env_int(name, default)
    if value < minimum:
        logging.getLogger("wsi_app.config").warning(
            "%s=%d is below the minimum of %d; using %d.", name, value, minimum, minimum,
        )
        return minimum
    return value


def _env_db_max_overflow(name: str, default: int) -> int:
    """``WSI_DB_MAX_OVERFLOW``: connections the pool may open past its size.

    ``-1`` is SQLAlchemy's "no limit" and is kept as such (``db.py`` sizes
    around it). Any other negative number is not a limit SQLAlchemy can
    honour — it shrinks the pool below its size, and at or below
    ``-pool_size`` the first checkout recurses until ``RecursionError`` — so
    it is raised to 0 with a warning.
    """
    value = env_int(name, default)
    if value < -1:
        logging.getLogger("wsi_app.config").warning(
            "%s=%d is not a valid overflow (use 0 or more, or -1 for no "
            "limit); using 0.", name, value,
        )
        return 0
    return value


def env_float(name: str, default: float) -> float:
    """Read a float setting, naming the variable when the value is malformed.

    The :func:`env_int` rationale, for the timing/rate knobs that accept
    fractional values.
    """
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        # Coerced so an int literal at the call site still yields a float.
        return float(default)
    try:
        return float(raw.strip())
    except ValueError:
        raise ValueError(
            f"{name} must be a number; got {raw!r}. "
            f"Fix it in the environment file (or unset it to use {default})."
        ) from None


_TRUE_WORDS = frozenset({"1", "true", "yes", "on"})
_FALSE_WORDS = frozenset({"0", "false", "no", "off"})


def env_bool(name: str, default: bool) -> bool:
    """Read an on/off flag with one shared vocabulary.

    Accepts ``1/true/yes/on`` and ``0/false/no/off`` (case-insensitive);
    unset or blank means ``default``. With each flag compared against its
    own literal (``== "1"`` for some, ``!= "0"`` for others),
    ``WSI_UPDATE_CHECK=false`` would leave the check ON while
    ``WSI_ERROR_LOG_ENABLED=true`` turned capture OFF. Anything else logs a
    warning naming the variable and falls back to ``default``. Unlike
    :func:`env_int` this does not raise: a hand-written value such as
    ``enabled`` would otherwise crash-loop the service on boot.
    """
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    v = raw.strip().lower()
    if v in _TRUE_WORDS:
        return True
    if v in _FALSE_WORDS:
        return False
    logging.getLogger("wsi_app.config").warning(
        "%s must be one of 1/true/yes/on or 0/false/no/off; got %r. "
        "Using the default (%d); fix it in the environment file.",
        name, raw, int(default),
    )
    return default


# Single-file formats only: an upload carries exactly one file, so the
# multi-file formats (MIRAX .mrxs, Hamamatsu .vms / .vmu, whose index file
# names companion data files in a directory beside it) can never be stored
# whole. Keep in sync with ACCEPTED_WSI_EXTENSIONS in
# frontend/src/lib/admin/validation.ts and infra/wsi.env.example.
DEFAULT_WSI_ALLOWED_EXTENSIONS = ".svs,.ndpi,.tif,.tiff,.scn,.bif,.szi"
MULTI_FILE_WSI_EXTENSIONS = (".mrxs", ".vms", ".vmu")


def wsi_allowed_extensions_from_env() -> tuple[str, ...]:
    """WSI_ALLOWED_EXTENSIONS, lower-cased, minus the multi-file formats.

    A hand-edited /etc/wsi/wsi.env (install.sh never regenerates it) may
    name .mrxs / .vms / .vmu. Accepting them would take a multi-GB upload only
    to refuse the slide afterwards, so they are dropped with a WARNING (logged
    once: Settings reads this at import).
    """
    raw = os.getenv("WSI_ALLOWED_EXTENSIONS", DEFAULT_WSI_ALLOWED_EXTENSIONS)
    exts = tuple(ext.strip().lower() for ext in raw.split(",") if ext.strip())
    dropped = [ext for ext in exts if ext in MULTI_FILE_WSI_EXTENSIONS]
    if dropped:
        logging.getLogger("wsi_app.config").warning(
            "WSI_ALLOWED_EXTENSIONS lists %s: multi-file formats are not "
            "supported (an upload carries one file, so their companion data "
            "files can never be stored), so they are ignored. Remove them "
            "from the environment file.",
            ", ".join(dropped),
        )
    return tuple(ext for ext in exts if ext not in MULTI_FILE_WSI_EXTENSIONS)


# scheme://host[:port], then anything after a '/', '?' or '#'. Same pattern as
# wsi_url_origin in infra/lib/wsi-common.sh, which install.sh and the Makefile
# apply to the same value: keep the two in step.
_URL_ORIGIN_RE = re.compile(
    r"(https?://(?:\[[0-9A-Fa-f:.]+\]|[A-Za-z0-9.-]+)(?::([0-9]{1,5}))?)(?:[/?#].*)?",
    re.DOTALL,
)


def public_base_url_from_env() -> str:
    """WSI_PUBLIC_BASE_URL reduced to its origin, or ``""`` when unset.

    Tessera is served at the root path only, and the frontend uses
    just the origin of this value (it is baked into the build by install.sh,
    which reduces it the same way). A path kept here would put it into the
    live-sync share links and the OAuth redirect_uri only, which then lead
    nowhere — so a path, query or fragment is dropped with a WARNING, and a
    value that is not an http(s) URL (or carries credentials) is ignored with
    one: failing it would crash-loop an install that boots today.
    """
    raw = os.getenv("WSI_PUBLIC_BASE_URL", "").strip()
    if not raw:
        return ""
    m = _URL_ORIGIN_RE.fullmatch(raw)
    if m is None or (m.group(2) is not None and int(m.group(2)) > 65535):
        logging.getLogger("wsi_app.config").warning(
            "WSI_PUBLIC_BASE_URL=%r is not an http(s) URL such as "
            "https://wsi.example.org; it is ignored.",
            raw,
        )
        return ""
    origin = m.group(1)
    if origin != raw.removesuffix("/"):  # one slash, as install.sh's ${v%/}
        logging.getLogger("wsi_app.config").warning(
            "WSI_PUBLIC_BASE_URL=%r is not a bare origin; using %r (Tessera "
            "is served at the root path only). Set it to the origin in the "
            "environment file.",
            raw, origin,
        )
    return origin


# The "on the LAN" address ranges: RFC 1918 private IPv4, loopback,
# link-local, plus the IPv6 loopback / unique-local / link-local equivalents.
# Listed explicitly rather than via ``ipaddress.is_private``, which also flags
# documentation (TEST-NET), benchmarking and shared-CGNAT ranges as "private"
# — those are not a LAN and must not earn an auth bypass. The dashboard's
# direct-LAN exemption uses these as they are; they are also the default of
# WSI_BREAKGLASS_TRUSTED_CIDRS.
LAN_CIDRS = (
    "10.0.0.0/8",
    "172.16.0.0/12",
    "192.168.0.0/16",
    "127.0.0.0/8",
    "169.254.0.0/16",
    "::1/128",
    "fc00::/7",
    "fe80::/10",
)


def breakglass_trusted_networks_from_env() -> tuple:
    """WSI_BREAKGLASS_TRUSTED_CIDRS as ip_network objects; LAN_CIDRS if unset.

    The client address ranges the break-glass exemption admits. A set value
    REPLACES the default (so it can narrow it as well as widen it): to add a
    campus range that hands out global addresses, list it beside the default
    ranges. Entries are separated by commas and/or whitespace; a single
    address counts as a /32 (/128). An entry that is not an address or network
    is skipped with a WARNING (failing would crash-loop an install that boots
    today); if none is left, the exemption admits no one. A /0 entry, which
    trusts every address, is honoured but warned about.
    """
    log = logging.getLogger("wsi_app.config")
    raw = os.getenv("WSI_BREAKGLASS_TRUSTED_CIDRS")
    if raw is None or not raw.strip():
        return tuple(ipaddress.ip_network(c) for c in LAN_CIDRS)
    nets = []
    for entry in re.split(r"[\s,]+", raw.strip()):
        if not entry:
            continue
        try:
            net = ipaddress.ip_network(entry, strict=False)
        except ValueError:
            log.warning(
                "WSI_BREAKGLASS_TRUSTED_CIDRS: %r is not an IP address or "
                "network; it is ignored.", entry,
            )
            continue
        if net.prefixlen == 0:
            log.warning(
                "WSI_BREAKGLASS_TRUSTED_CIDRS: %s trusts every address, so "
                "with WSI_BREAKGLASS_NOAUTH=1 any client reaching port 80 "
                "skips the sign-in.", net,
            )
        nets.append(net)
    if not nets:
        log.warning(
            "WSI_BREAKGLASS_TRUSTED_CIDRS lists no valid range; the "
            "break-glass exemption admits no one."
        )
    return tuple(nets)


@dataclass(frozen=True)
class Settings:
    database_url: str = os.getenv(
        "DATABASE_URL",
        f"sqlite:///{_DEFAULT_DATA_DIR}/wsi.db",
    )
    data_dir: str = os.getenv("DATA_DIR", _DEFAULT_DATA_DIR)
    admin_bootstrap_username: str = os.getenv("ADMIN_BOOTSTRAP_USERNAME", "admin")
    admin_bootstrap_password: str = os.getenv(
        "ADMIN_BOOTSTRAP_PASSWORD", ADMIN_BOOTSTRAP_PASSWORD_DEFAULT
    )
    allow_admin_register: bool = env_bool("ALLOW_ADMIN_REGISTER", False)
    wsi_allowed_extensions: tuple[str, ...] = wsi_allowed_extensions_from_env()
    wsi_max_upload_bytes: int = env_int("WSI_MAX_UPLOAD_BYTES", 8 * 1024 * 1024 * 1024)
    # Failed admin logins per user before triggering a temporary lockout.
    admin_login_max_failures: int = env_int("ADMIN_LOGIN_MAX_FAILURES", 5)
    # Duration of the lockout window once the failure threshold is hit.
    admin_login_lockout_seconds: int = env_int("ADMIN_LOGIN_LOCKOUT_SECONDS", 900)
    # Per-IP failed-login throttle (complements the per-USER lockout above,
    # which an attacker spreading guesses across many usernames bypasses).
    # Counts failed admin-login attempts from one client IP in a rolling
    # window; once exceeded the endpoint 429s before doing any pbkdf2 work.
    # Generous by default so a NAT'd organisation behind one IP isn't locked
    # out by a few fat-fingered users. Set max_failures to 0 to disable.
    admin_login_ip_max_failures: int = env_int("ADMIN_LOGIN_IP_MAX_FAILURES", 30)
    admin_login_ip_window_seconds: int = env_int("ADMIN_LOGIN_IP_WINDOW_SECONDS", 300)
    # Per-IP failed-login throttle for the app-access login surface
    # (POST /api/auth/login and the middleware's HTTP Basic branch). Mirrors
    # the admin throttle above but on its own IP keyspace so the two limits are
    # independent. Without it the app login has no brute-force / credential-
    # stuffing bound at all, and each attempt still pays a full pbkdf2 verify
    # (basic) or an outbound LDAP bind (ldap). Set max_failures to 0 to disable.
    app_login_ip_max_failures: int = env_int("APP_LOGIN_IP_MAX_FAILURES", 20)
    app_login_ip_window_seconds: int = env_int("APP_LOGIN_IP_WINDOW_SECONDS", 300)
    # Throttle for the current-password re-auth on PATCH /api/admin/me (and
    # the two-factor setup / confirm / disable / recovery-code / reset
    # endpoints in routers/admin_mfa.py), counted
    # per presented token (despite the REAUTH_IP_* name) so rotating IPs gains
    # nothing, and a leaked token cannot block its owner's password change.
    # A stolen bearer token would otherwise let an attacker brute-force the
    # account's real password unbounded, bypassing both admin-login defenses.
    # Kept on its own keyspace (never the per-user lockout) so a token holder
    # can't lock the legitimate admin out of /api/admin/login. 0 disables.
    reauth_ip_max_failures: int = env_int("REAUTH_IP_MAX_FAILURES", 10)
    reauth_ip_window_seconds: int = env_int("REAUTH_IP_WINDOW_SECONDS", 300)
    # Two-factor sign-in (mfa.py). The issuer is the name authenticator apps
    # show next to the account; set it per install when one person holds
    # accounts on several. The two TTLs bound how long a password-verified
    # sign-in may wait for its code, and how long a QR code shown during
    # enrolment stays confirmable. Not "off" at 0: every token would be born
    # expired and no two-factor account could sign in, hence the floors.
    mfa_issuer: str = (os.getenv("WSI_MFA_ISSUER") or "Tessera").strip() or "Tessera"
    mfa_login_ttl_seconds: int = env_int_min("WSI_MFA_LOGIN_TTL_SECONDS", 300, 60)
    mfa_setup_ttl_seconds: int = env_int_min("WSI_MFA_SETUP_TTL_SECONDS", 600, 60)
    # Per-file size cap for tour markdown assets (default 2.5 MiB).
    tour_asset_max_bytes: int = env_int("TOUR_ASSET_MAX_BYTES", int(2.5 * 1024 * 1024))
    # Per-tour quotas. Without these the per-file cap above is the ONLY bound:
    # the upload endpoint is open to the lowest-privilege staff role and the
    # "rename" policy mints a fresh filename every time, so a loop could
    # accumulate 2.5 MiB files until the data volume fills — taking WSI uploads,
    # backups and SQLite writes down with it. Generous enough that no real tour
    # hits them (a step-by-step tour uses a handful of images). 0 disables.
    tour_asset_max_files: int = env_int("TOUR_ASSET_MAX_FILES", 200)
    tour_asset_max_total_bytes: int = env_int(
        "TOUR_ASSET_MAX_TOTAL_BYTES", 200 * 1024 * 1024
    )
    # Instance-wide floor under the per-tour quotas above: an asset upload is
    # refused (507 uploadNoSpace) once writing it would leave less than this
    # free on the volume holding the tour's asset directory. The same role
    # that uploads assets can also create tours, each with a fresh quota, so
    # the per-tour caps alone would not bound the total. 1 GiB keeps room for
    # SQLite / analytics writes and for an admin to act while still being
    # noise on any volume big enough to hold WSIs. 0 disables.
    tour_asset_min_free_bytes: int = env_int(
        "TOUR_ASSET_MIN_FREE_BYTES", 1024 * 1024 * 1024
    )
    # Comma-separated MIME prefixes accepted for tour assets. Use a wildcard
    # like "image/" to allow a whole family. The default is image-only to
    # match the admin UI: TourAssetsPanel exposes only `accept="image/*"`
    # and renders every entry through `<img>`, so a non-image asset would
    # appear as a broken icon. Operators who want to author with PDF /
    # video / audio attachments can extend this list (the magic-byte
    # checks in admin_tours._validate_asset_content cover those families).
    tour_asset_allowed_mime_prefixes: tuple[str, ...] = tuple(
        p.strip().lower()
        for p in os.getenv(
            "TOUR_ASSET_ALLOWED_MIME_PREFIXES",
            "image/",
        ).split(",")
        if p.strip()
    )
    # Where backup archives are written and listed from.
    backup_dir: str = os.getenv("BACKUP_DIR", f"{_DEFAULT_DATA_DIR}/backups")
    # Upper bound for restore archive uploads (default 1 TiB to accommodate
    # full snapshots that include WSI files).
    backup_max_upload_bytes: int = env_int(
        "BACKUP_MAX_UPLOAD_BYTES", 1024 * 1024 * 1024 * 1024
    )
    # Upper bound for the *decompressed* total of an archive being extracted
    # to staging. Caps the worst case where a high-ratio zstd bomb would
    # blow past the upload cap during extraction. Default is 4× the upload
    # cap so legitimate metadata-only backups (which compress 5–10×) stay
    # well within the limit while a 100×-bomb is rejected.
    backup_max_extract_bytes: int = env_int(
        "BACKUP_MAX_EXTRACT_BYTES",
        env_int("BACKUP_MAX_UPLOAD_BYTES", 1024 * 1024 * 1024 * 1024) * 4,
    )
    # IPC files used to hand off a staged restore to start-wsi-app.sh and
    # to surface the apply result back to the running app after restart.
    restore_request_path: str = os.getenv("RESTORE_REQUEST_PATH", "/run/wsi/restore.request")
    restore_result_path: str = os.getenv("RESTORE_RESULT_PATH", "/run/wsi/restore.result")
    # Crash-recovery journal of an in-flight restore/reset swap. Empty (the
    # default) means ``{data_dir}/.restore-apply.journal`` — resolved at use by
    # backup.restore_journal_path(), and spelled the same way in
    # start-wsi-app.sh. Must live on the persistent data volume, not /run.
    restore_journal_path: str = os.getenv("RESTORE_JOURNAL_PATH", "")
    # Trigger file the API touches to ask systemd to restart wsi-app. The
    # wsi-restart.path unit (installed by infra/install.sh) watches this path
    # and activates wsi-restart.service, which removes the file and runs a
    # privileged ``systemctl restart wsi-app.service``. A ``sudo systemctl``
    # approach could never work here: the unit runs with
    # ``NoNewPrivileges=true`` (implied by its seccomp hardening), under which
    # setuid ``sudo`` is refused. It mirrors the wsi-avahi-reload.path unit the
    # app already relies on for the same reason.
    restart_trigger_path: str = os.getenv("RESTART_TRIGGER_PATH", "/run/wsi/restart.trigger")
    # Optional explicit command to trigger a service restart. Empty by default,
    # which selects the wsi-restart.path trigger-file mechanism above. A
    # non-empty value (tests, or a custom deployment that prefers to shell out)
    # is run directly instead. NOTE: a ``sudo``-based command cannot work under
    # this unit's ``NoNewPrivileges=true`` — use the trigger-file default in
    # production.
    restore_restart_cmd: str = os.getenv("RESTORE_RESTART_CMD", "")
    # Explicit override for the Google OAuth `redirect_uri` parameter. When
    # unset, the callback URL is reconstructed from the inbound request's
    # ``Host`` / ``X-Forwarded-Proto`` headers, which a misbehaving (or
    # malicious) proxy can spoof — Google's redirect_uri allow-list bounds
    # the actual damage, but the constructed URI also leaks into audit
    # trails and authorize-page bookmarks. Setting this to e.g.
    # ``https://wsi.example.com`` pins the value to the operator's public
    # base URL so the callback path becomes
    # ``https://wsi.example.com/api/auth/oauth/callback`` regardless of
    # what headers the proxy passes through. Always an origin, with no
    # trailing slash (see public_base_url_from_env).
    public_base_url: str = public_base_url_from_env()
    # Serve FastAPI's generated schema and API browsers (/openapi.json,
    # /docs, /redoc). Off by default: they are unauthenticated and list every
    # admin route and parameter, which a public deployment has no reason to
    # advertise. Enable (WSI_API_DOCS=1) on a dev box to browse the API.
    api_docs_enabled: bool = env_bool("WSI_API_DOCS", False)
    # Dev-only escape hatch: if enabled, the restore-apply endpoint
    # performs the swap in-process (engine.dispose -> mv -> rebuild) instead
    # of writing /run/wsi/restore.request and asking systemd to restart.
    # NEVER enable in production: it races in-flight requests.
    backup_dev_inprocess_apply: bool = env_bool("BACKUP_DEV_INPROCESS_APPLY", False)
    # IANA timezone name (e.g. "Europe/Berlin") that drives the analytics
    # dashboard's daily bucket boundaries and recent-log timestamp display.
    # Decoupled from the host's TZ on purpose: an operator may well be
    # looking at a server in UTC, and they want "yesterday" to mean their
    # local yesterday, not UTC yesterday. When unset, the dashboard falls
    # back to the host process's local TZ for a sensible default.
    analytics_timezone: str = os.getenv("WSI_ANALYTICS_TIMEZONE", "")
    # Cap on the dwell time accepted from a single slide-view session
    # beacon. The frontend trims the clock for tab-hidden time, but a
    # misbehaving client could still report an enormous value; this is
    # the server-side defence. 4 hours is well above any legitimate
    # uninterrupted slide reading.
    analytics_max_dwell_ms: int = env_int("WSI_ANALYTICS_MAX_DWELL_MS", 4 * 60 * 60 * 1000)
    # Caps on the persistent audit-history table (audit_logs). The row cap
    # keeps the newest N rows; the age cap drops rows older than N days.
    # 0 disables that dimension. Both default conservative: 100k rows of
    # admin actions is years of history for a single-site install, and the
    # journald audit line remains the unabridged record either way.
    audit_log_max_rows: int = env_int("WSI_AUDIT_LOG_MAX_ROWS", 100000)
    audit_log_max_age_days: int = env_int("WSI_AUDIT_LOG_MAX_AGE_DAYS", 730)
    # Caps on the persistent error-log table (error_logs), mirroring the audit
    # caps above. Error/warning rows churn faster and carry less compliance
    # value than audit rows, so they default to a shorter window (90 days) and
    # a smaller row cap. 0 disables that dimension. journald keeps the
    # unabridged record regardless.
    error_log_max_rows: int = env_int("WSI_ERROR_LOG_MAX_ROWS", 50000)
    error_log_max_age_days: int = env_int("WSI_ERROR_LOG_MAX_AGE_DAYS", 90)
    # Caps on the high-frequency analytics event tables (page_views,
    # slide_dwell_events, tour_sessions), mirroring the audit/error caps above.
    # These are the busiest write path in the app (a public beacon on every SPA
    # route change, dwell flush, and tour event), so without a retention policy
    # they grow unbounded — degrading dashboard aggregation queries and
    # eventually risking disk exhaustion. The row cap keeps the newest N rows
    # per table; the age cap drops rows older than N days. 0 disables that
    # dimension. Pruning runs at boot and opportunistically, exactly like the
    # audit/error janitors. Defaults are generous: a year of retention and
    # 5M rows per table is ample for a single-site teaching install.
    analytics_log_max_rows: int = env_int("WSI_ANALYTICS_LOG_MAX_ROWS", 5000000)
    analytics_log_max_age_days: int = env_int("WSI_ANALYTICS_LOG_MAX_AGE_DAYS", 365)
    # Minimum level captured into error_logs from the wsi_app logger tree.
    # Default WARNING surfaces early trouble signs; raise to ERROR on a noisy
    # site. Accepts any stdlib level name (DEBUG/INFO/WARNING/ERROR/CRITICAL).
    error_log_level: str = os.getenv("WSI_ERROR_LOG_LEVEL", "WARNING").upper()
    # Kill switch for the DB error-log capture. When off ("0"), no handler is
    # installed and the feature's table simply stays empty (journald unaffected).
    error_log_enabled: bool = env_bool("WSI_ERROR_LOG_ENABLED", True)
    # Shared parent domain for the app-access session cookie. Set this only
    # when the app is reached under two sibling hostnames that share a parent
    # domain: a public Cloudflare-Access host (Path A) and a LAN-direct host
    # (Path C) that the LAN-direct probe redirects to. Example: public
    # `wsi.example.org`, LAN `lan.wsi.example.org`, set
    # `WSI_SESSION_COOKIE_DOMAIN=wsi.example.org`. When set, a request that
    # CF Access has already authenticated (valid Cf-Access-Jwt-Assertion on
    # the trusted hop) gets a session cookie scoped to this domain, so the
    # cookie rides the public -> LAN redirect and the LAN host records the
    # same verified email without a second interactive login. The Domain
    # attribute is sent to ALL subdomains of this value, so scope it as
    # narrowly as the two hostnames allow (nest the LAN host under the public
    # host rather than using a broad organisation-wide parent). A leading dot
    # is stripped; an unset value (default) keeps cookies host-only (the stock
    # behavior). See docs/operations.md -> "Carrying the CF Access identity
    # onto the LAN host".
    session_cookie_domain: str = os.getenv("WSI_SESSION_COOKIE_DOMAIN", "").strip().lstrip(".")
    # Break-glass exemption: when on, a plaintext-HTTP request that did NOT
    # arrive through the Cloudflare Tunnel skips the app-access gate. This
    # targets ONLY the documented raw-IP fallback (e.g. http://<server-LAN-IP>)
    # used when TLS / DNS for the LAN-direct host is down. The two qualifying
    # signals are nginx-derived and not client-forgeable: a non-empty
    # `Cf-Connecting-IP` (present only on the trusted loopback hop) marks
    # tunnel traffic and is excluded, and `X-Forwarded-Proto` is overwritten by
    # nginx with the listener scheme, so only the plaintext :80 raw-IP server
    # block yields `http`. The TLS LAN-direct host (`https`) and every tunnel
    # path stay gated, and a forged `Host` header cannot move a TLS request
    # onto this exemption. Default off; existing deployments are unaffected.
    # See docs/operations.md -> "Break-glass: unauthenticated raw-IP access".
    breakglass_noauth: bool = env_bool("WSI_BREAKGLASS_NOAUTH", False)
    # The client address ranges that exemption admits (the real client address,
    # as for the direct-LAN exemption). Default: LAN_CIDRS. Set it to add a
    # campus range that hands out global addresses, or to narrow the default.
    breakglass_trusted_networks: tuple = breakglass_trusted_networks_from_env()
    # PEM CA bundle used to verify the LDAP server certificate (ldaps:// and
    # StartTLS). Empty (default) = the system trust store. Verification is
    # always on; point this at the directory's private CA when its certificate
    # is not publicly trusted.
    ldap_ca_certs_file: str = os.getenv("LDAP_CA_CERTS_FILE", "").strip()
    # --- SQLAlchemy connection pool (per uvicorn worker process) --------
    # Each sync request runs in the anyio threadpool and checks out one
    # connection from this pool for the duration of its DB work. Under a
    # crowd hitting cold-cache tiles (each /dzi/ request does a public_id ->
    # PK lookup) plus analytics pageviews and settings reads, the number of
    # concurrent DB-touching requests per worker can reach the threadpool
    # ceiling (``db_request_threadpool_tokens`` below). If the pool's total
    # capacity (``pool_size + max_overflow``) is smaller than that, the
    # excess requests block on connection checkout for ``pool_timeout``
    # seconds and then fail with ``QueuePool limit ... reached, connection
    # timed out`` — the mass-access 500s this configuration is sized to
    # prevent. SQLite connections in WAL mode are cheap (concurrent readers,
    # a single serialised writer), so the pool is sized generously and kept
    # comfortably above the threadpool ceiling. All three are env-tunable;
    # ``WSI_DB_MAX_OVERFLOW=-1`` lifts the overflow cap entirely, and a
    # checkout timeout below 1 s would fail every checkout that has to wait.
    db_pool_size: int = env_int_min("WSI_DB_POOL_SIZE", 20, 1)
    db_max_overflow: int = _env_db_max_overflow("WSI_DB_MAX_OVERFLOW", 60)
    db_pool_timeout: int = env_int_min("WSI_DB_POOL_TIMEOUT_SECONDS", 30, 1)
    # Cap on concurrent sync-endpoint execution per worker (anyio's default
    # thread limiter). Set explicitly at startup so the relationship with the
    # pool capacity above is a documented, operator-tunable invariant rather
    # than an implicit library default (anyio's own default is 40) that could
    # drift out from under the pool on a dependency upgrade. Kept below the
    # pool capacity so a saturated worker queues excess requests cheaply at
    # the scheduler instead of dying after a 30s connection-checkout wait.
    db_request_threadpool_tokens: int = env_int_min("WSI_REQUEST_THREADPOOL_TOKENS", 40, 1)
    # --- Slide-metadata resolve cache (per worker) ----------------------
    # The /dzi/ tile path resolves public_id -> (slide_id, is_published,
    # wsi_generation) on every request. That mapping is near-constant (public_id is immutable;
    # is_published toggles rarely), yet a crowd viewing slides turns one user
    # action into hundreds of identical DB reads per second — the main
    # amplifier behind the connection-pool exhaustion. This cache holds the
    # resolution in-process so those reads collapse to roughly one per slide
    # per worker per TTL. Cross-worker correctness (a publish/delete/WSI
    # replace on another worker) is kept by a filesystem "generation" sentinel that is
    # re-checked every ``slide_meta_gen_check_interval`` seconds and flushes
    # the cache on change — so the TTL below can be long (it is only a
    # backstop for a missed sentinel bump) without letting a just-hidden or
    # deleted slide stay served. Set the TTL to 0 to disable the cache
    # entirely and restore the read-live-on-every-request behaviour.
    slide_meta_cache_ttl: int = env_int("WSI_SLIDE_META_CACHE_TTL_SECONDS", 300)
    # How often (seconds) a worker re-reads the generation sentinel. Bounds
    # how long a publish/delete on ANOTHER worker takes to propagate here;
    # the worker that handled the change flushes immediately. Cheap (a small
    # page-cached file read, at most once per interval), so keep it short.
    slide_meta_gen_check_interval: int = env_int("WSI_SLIDE_META_GEN_CHECK_SECONDS", 1)
    # Upper bound on cached entries per worker. Positive (found) resolutions
    # only — unknown public_ids are never cached — so this just caps memory
    # against a very large published-slide catalogue; the oldest entries are
    # dropped past the cap.
    slide_meta_cache_max: int = env_int("WSI_SLIDE_META_CACHE_MAX", 4096)

settings = Settings()

# This build targets SQLite exclusively: the inline column migrations
# (main._apply_column_migrations), the per-connection PRAGMA setup (db.py),
# the backup/restore file-swap (backup.py), and the analytics date arithmetic
# (routers/analytics.py) all assume a single SQLite file. Fail fast against any
# other backend instead of breaking later in confusing, backend-specific ways.
# The env override still lets operators relocate the SQLite file
# (e.g. sqlite:////mnt/data/wsi.db); it just can't select a different engine.
if not settings.database_url.startswith("sqlite"):
    raise RuntimeError(
        "DATABASE_URL must be a sqlite:/// URL — this build supports SQLite "
        f"only (got {settings.database_url!r})."
    )
