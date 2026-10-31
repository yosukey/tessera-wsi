#!/usr/bin/env bash
# =====================================================================
# Tessera - native Ubuntu installer
#
# Tested on: Ubuntu 26.04 LTS
#
# Usage (from a freshly cloned repo):
#   sudo bash infra/install.sh
#
# This script is idempotent: re-running it should be safe and will only
# (re)write files that are out of date. It will NOT regenerate an existing
# /etc/wsi/wsi.env (so your secrets are preserved across re-runs); edit it
# manually if you need to change env vars.
#
# One narrow exception: WSI_LAN_HOST, WSI_LAN_PROBE_TIMEOUT_MS and
# WSI_PUBLIC_BASE_URL are written into an existing wsi.env when — and only
# when — you supply them in the environment. Put the assignment AFTER sudo:
#
#     sudo WSI_LAN_HOST=lan.example.org bash infra/install.sh
#
# A bare `WSI_LAN_HOST=... sudo bash ...` sets it in sudo's own environment,
# which Ubuntu's `Defaults env_reset` then strips — the script would see it
# unset, persist nothing, and still report success.
#
# WSI_LAN_HOST is baked into the frontend build and the nginx CSP,
# WSI_LAN_PROBE_TIMEOUT_MS into the build, and WSI_PUBLIC_BASE_URL into the
# build as well as being read at runtime by the app, so all three have to
# survive into wsi.env or the next update.sh would rebuild without
# them. Supplying one replaces any existing live assignment of that key
# (including a hand-written one, and any trailing comment on its line);
# everything else in wsi.env is left byte-for-byte alone. Run without the
# variable set and nothing is written — the value is simply read back. To
# clear one, delete its line from wsi.env by hand. WSI_PUBLIC_BASE_URL is
# written as its origin (https://host[:port]): a path is dropped, with a
# warning, as the app does at runtime.
#
# The other exception: a wsi.env with no (or an empty) WSI_MFA_KEY gets a
# freshly generated one appended. An existing value is never touched.
#
# Release tracking (default = latest final vX.Y.Z tag on origin; pre-release
# tags such as v2.0.0-rc1 are never picked by default):
#   GIT_REF=<ref>            Pin to a specific branch/tag (e.g. vX.Y.Z).
#   WSI_TRACK_BRANCH=1       Stay on current branch HEAD instead of
#                            checking out the latest release tag.
#   WSI_SKIP_TAG_CHECKOUT=1  Skip tag-checkout entirely (set by update.sh
#                            when it has already moved the tree).
#   GIT_AS_USER=<name>       Run git as this user (defaults to SUDO_USER
#                            so credentials in $HOME resolve).
#
# Other tuning (also after sudo):
#   APT_LOCK_TIMEOUT=<s>     Wait this long for another apt/dpkg run to
#                            release its locks (default 300).
#   NODE_MAJOR=<n>           Minimum Node.js major; an older system node is
#                            replaced from NodeSource (default 24).
#   WSI_NPM_CI_ATTEMPTS=<n>  `npm ci` attempts before giving up (default 4,
#                            2 s backoff doubling between them).
#   WSI_NPM_FETCH_RETRIES=<n> / WSI_NPM_FETCH_TIMEOUT=<ms> /
#   WSI_NPM_FETCH_RETRY_MAXTIMEOUT=<ms>
#                            npm's per-request retries (default 5), timeout
#                            (600000) and retry back-off cap (120000).
#   WSI_HEALTH_TIMEOUT=<s> / WSI_SYNC_HEALTH_TIMEOUT=<s>
#                            Health-check wait for wsi-app (default 60) /
#                            wsi-sync (default 30).
#   WSI_SKIP_RESTART_HEALTH=1  Do not restart or health-check wsi-app /
#                            wsi-sync (set by update.sh, which does both).
# =====================================================================
# -E (errtrace), as in update.sh: without it the ERR trap below is not
# inherited by shell functions, command substitutions or subshells, so a
# command failing inside one (ensure_data_subdir, persist_env_var, run_git,
# ...) still ended the run through `set -e` — but silently, with no
# "FAILED at line" message to say where. Calls in a tested context (`if`,
# `||`, `&&`, `!`) stay exempt from the trap, exactly as from `set -e`.
set -Eeuo pipefail

# ---- Configurable paths ---------------------------------------------
# NOTE: these are effectively fixed — the systemd units / nginx config / etc.
# hardcode the defaults, so a non-default override is rejected by a guard once
# die() is defined below. The ${VAR:-default} form is kept only so the values
# have a single definition the rest of the script reads.
APP_USER="${APP_USER:-wsi}"
APP_GROUP="${APP_GROUP:-wsi}"
APP_HOME="${APP_HOME:-/opt/wsi}"
DATA_DIR="${DATA_DIR:-/var/lib/wsi}"
ETC_DIR="${ETC_DIR:-/etc/wsi}"
CACHE_DIR="${CACHE_DIR:-/var/cache/nginx/wsi_dzi}"
VENV_DIR="${VENV_DIR:-$APP_HOME/venv}"
# Not fixed: the Node.js major installed from NodeSource when the system node
# is older (see the header).
NODE_MAJOR="${NODE_MAJOR:-24}"

# Source repo = directory containing this script's parent.
REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# Helpers shared with update.sh (wsi_env_value, wsi_define_run_git), read
# from the same tree as this script.
WSI_COMMON_SH="$REPO_DIR/infra/lib/wsi-common.sh"
# shellcheck source=lib/wsi-common.sh
. "$WSI_COMMON_SH"

# ---- Optional LAN-direct redirect probe -----------------------------
# A single value drives BOTH halves of the feature so the two can never
# drift apart across re-runs:
#   - the frontend build bakes it in as VITE_LAN_HOST (the SPA probes
#     https://<host>/favicon.ico to detect a same-VLAN client), and
#   - the installed nginx CSP gets the host added to connect-src, so that
#     cross-origin probe isn't blocked.
# Precedence: an explicit WSI_LAN_HOST in the environment wins; otherwise
# reuse whatever is already persisted in $ETC_DIR/wsi.env. Because wsi.env
# survives updates, this is what lets `update.sh` re-apply the probe with
# nothing to re-enter — set it once and every future update keeps it. Unset
# everywhere -> the probe stays disabled and the stock CSP is installed
# verbatim (the default build is byte-for-byte unchanged).
#
# The "set it once" promise above only holds if an env-supplied value
# actually reaches wsi.env. Capture, before the read-back below overwrites
# them, which of the three persisted tunables arrived from the environment;
# step 7 writes exactly those into wsi.env. Without this, `WSI_LAN_HOST=x
# sudo bash install.sh` would configure this run only, and the next
# update.sh (which re-runs install.sh with no such variable set) would
# silently revert to the stock build and CSP.
WSI_LAN_HOST_FROM_ENV="${WSI_LAN_HOST:-}"
WSI_LAN_PROBE_TIMEOUT_MS_FROM_ENV="${WSI_LAN_PROBE_TIMEOUT_MS:-}"
WSI_PUBLIC_BASE_URL_FROM_ENV="${WSI_PUBLIC_BASE_URL:-}"

WSI_LAN_HOST="${WSI_LAN_HOST:-}"
if [[ -z "$WSI_LAN_HOST" ]]; then
    WSI_LAN_HOST="$(wsi_env_value WSI_LAN_HOST "$ETC_DIR/wsi.env")"
fi
# Validate before it is spliced into a `#`-delimited sed expression and an
# nginx add_header CSP line below. A value containing '#', quotes, ';' or
# whitespace would break the sed or inject into the response header. Restrict
# to a plain hostname. (die() is not defined yet here, so fail inline.)
if [[ -n "$WSI_LAN_HOST" && ! "$WSI_LAN_HOST" =~ ^[A-Za-z0-9.-]+$ ]]; then
    printf '\033[1;31m[error]\033[0m %s\n' \
        "WSI_LAN_HOST='$WSI_LAN_HOST' is not a valid hostname (allowed: letters, digits, '.', '-')." >&2
    exit 1
fi

# Optional override (in ms) for the first-ever probe's wait window, which must
# outlast the browser's local-network permission prompt on the first load.
# Resolved with the same precedence as WSI_LAN_HOST (explicit env wins, else
# wsi.env). Only baked into the build when the probe itself is enabled; unset
# everywhere -> the frontend's built-in default applies.
WSI_LAN_PROBE_TIMEOUT_MS="${WSI_LAN_PROBE_TIMEOUT_MS:-}"
if [[ -z "$WSI_LAN_PROBE_TIMEOUT_MS" ]]; then
    WSI_LAN_PROBE_TIMEOUT_MS="$(wsi_env_value WSI_LAN_PROBE_TIMEOUT_MS "$ETC_DIR/wsi.env")"
fi
# Validate as a non-negative integer (milliseconds). Mirrors the WSI_LAN_HOST
# guard above, which already vets the same wsi.env source — without this a
# stale or mistyped value would ride straight into the Vite build env below.
# Unlike the hostname (which is spliced into sed / an HTTP header and so must
# be fatal), this is passed only as an isolated `env VAR=val`, so a bad value
# can't inject anywhere: treat it as non-fatal and fall back to unset, which
# the build path already maps to the frontend's built-in default. die()/warn()
# are not defined yet here, so emit inline like the WSI_LAN_HOST check.
if [[ -n "$WSI_LAN_PROBE_TIMEOUT_MS" && ! "$WSI_LAN_PROBE_TIMEOUT_MS" =~ ^[0-9]+$ ]]; then
    printf '\033[1;33m[warn]\033[0m %s\n' \
        "WSI_LAN_PROBE_TIMEOUT_MS='$WSI_LAN_PROBE_TIMEOUT_MS' is not a non-negative integer (ms); ignoring it." >&2
    WSI_LAN_PROBE_TIMEOUT_MS=""
    WSI_LAN_PROBE_TIMEOUT_MS_FROM_ENV=""
    # Fall back to whatever is already persisted rather than to "unset".
    # Blanking outright would rebuild the frontend WITHOUT a value this host
    # has been running with — a silent regression on one run, self-healing
    # only on the next — while wsi.env kept the good value. Re-read it here so
    # a typo on the command line costs a warning, not a behaviour change.
    WSI_LAN_PROBE_TIMEOUT_MS="$(wsi_env_value WSI_LAN_PROBE_TIMEOUT_MS "$ETC_DIR/wsi.env")"
    [[ "$WSI_LAN_PROBE_TIMEOUT_MS" =~ ^[0-9]+$ ]] || WSI_LAN_PROBE_TIMEOUT_MS=""
fi

# ---- Optional canonical public base URL -----------------------------
# WSI_PUBLIC_BASE_URL is already the app's runtime canonical origin (used
# server-side for the live-sync follower URL and the OAuth redirect). Bake it
# into the frontend build too, as VITE_PUBLIC_BASE_URL, so the static share
# controls can offer this public URL for copy/QR when the operator is on a
# direct/local route (LAN host, bare IP, .local) — a link that also works
# off-LAN. Same precedence as WSI_LAN_HOST (explicit env wins, else wsi.env),
# so `update.sh` re-applies it automatically. Unset everywhere -> the share
# controls only ever show the current-origin URL and the build is unchanged.
WSI_PUBLIC_BASE_URL="${WSI_PUBLIC_BASE_URL:-}"
if [[ -z "$WSI_PUBLIC_BASE_URL" ]]; then
    # Trimmed, as systemd reads it: a trailing blank or CR (CRLF-edited
    # wsi.env) would otherwise trip the "not a bare origin" warning below.
    WSI_PUBLIC_BASE_URL="$(wsi_env_value_trimmed WSI_PUBLIC_BASE_URL "$ETC_DIR/wsi.env")"
fi
# The value is an origin: Tessera is served at the root path only, so the app
# (config.public_base_url_from_env) and the frontend both use just the origin of
# whatever is set. Reduce it here the same way (wsi_url_origin), so the build,
# what step 7 persists and what the app runs with all agree; a path, query or
# fragment costs a warning. Something that is not an http(s) URL at all is
# ignored, non-fatally: the value is passed only as an isolated `env VAR=val`
# to the build (never spliced into sed / an HTTP header), so it can't inject
# anywhere, and unset maps to "no public-route affordance". An origin also
# round-trips through wsi.env, whose read-back cannot match a value holding
# '#' or '"' (see wsi_env_value). die()/warn() are not defined yet here, so
# emit inline like the checks above.
if [[ -n "$WSI_PUBLIC_BASE_URL" ]]; then
    if _origin="$(wsi_url_origin "$WSI_PUBLIC_BASE_URL")"; then
        if [[ "$_origin" != "${WSI_PUBLIC_BASE_URL%/}" ]]; then
            printf '\033[1;33m[warn]\033[0m %s\n' \
                "WSI_PUBLIC_BASE_URL='$WSI_PUBLIC_BASE_URL' is not a bare origin; using '$_origin' (Tessera is served at the root path only)." >&2
        fi
        WSI_PUBLIC_BASE_URL="$_origin"
    else
        printf '\033[1;33m[warn]\033[0m %s\n' \
            "WSI_PUBLIC_BASE_URL='$WSI_PUBLIC_BASE_URL' is not an http(s) URL (e.g. https://wsi.example.org); ignoring it." >&2
        WSI_PUBLIC_BASE_URL_FROM_ENV=""
        # Same reasoning as the probe-timeout fallback above: prefer the value
        # this host already runs with over rebuilding without one.
        WSI_PUBLIC_BASE_URL="$(wsi_url_origin "$(wsi_env_value WSI_PUBLIC_BASE_URL "$ETC_DIR/wsi.env")")" \
            || WSI_PUBLIC_BASE_URL=""
    fi
    unset _origin
fi

log()  { printf '\033[1;34m[install]\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m[warn]\033[0m %s\n'   "$*" >&2; }
die()  { printf '\033[1;31m[error]\033[0m %s\n'  "$*" >&2; exit 1; }

# These paths/users are hardcoded in the systemd units, the nginx config, the
# tmpfiles.d drop-in and start-wsi-app.sh, which this script installs verbatim
# (no templating). Overriding the env vars therefore produces a *silently
# broken* install (the app writes to one path while systemd sandboxes another).
# Refuse a non-default override loudly instead. (If real relocation support is
# ever needed, template these into the units/conf at install time and drop this
# guard.)
for _fixed in \
    "APP_USER=wsi" "APP_GROUP=wsi" "APP_HOME=/opt/wsi" \
    "DATA_DIR=/var/lib/wsi" "ETC_DIR=/etc/wsi" \
    "CACHE_DIR=/var/cache/nginx/wsi_dzi" "VENV_DIR=/opt/wsi/venv"; do
    _name="${_fixed%%=*}"
    _want="${_fixed#*=}"
    if [[ "${!_name}" != "$_want" ]]; then
        die "$_name is fixed at '$_want' (hardcoded in the systemd units / nginx config); '${!_name}' is not supported."
    fi
done
unset _fixed _name _want

# Non-fatal issues to re-surface in the final summary so the operator
# cannot miss them after a screenful of install output. Append entries
# with: POST_INSTALL_WARNINGS+=("short description").
POST_INSTALL_WARNINGS=()
# Services that did not answer their health check in time. Not fatal on the
# spot, so the summary (with a fresh install's credential file) still prints,
# but the run then exits non-zero: the install is not working.
HEALTH_TIMEOUTS=()

# Loud, line-numbered failure: `set -e` aborts on the first non-zero exit
# but bash itself prints nothing about *where*. When a downstream command
# emits a terse message like "install: No such file or directory" with no
# path attached, the operator has no idea which line went wrong. This trap
# fills that gap.
on_err() {
    local rc=$?
    # Under set -E the trap also runs inside subshells and $(...) / <(...)
    # substitutions; there, only exit. A failure that matters fails the main
    # shell too, which reports it once; one it does not (a substitution whose
    # status nobody reads) must not print a FAILED line for a run that is fine.
    [[ "$BASHPID" == "$$" ]] || exit "$rc"
    printf '\033[1;31m[install]\033[0m FAILED at line %d (exit %d): %s\n' \
        "$1" "$rc" "$2" >&2
    exit "$rc"
}
trap 'on_err "$LINENO" "$BASH_COMMAND"' ERR

if [[ $EUID -ne 0 ]]; then
    die "Please run as root (sudo bash infra/install.sh)"
fi

# Wait for any concurrent apt/dpkg process to release its locks before
# running the script's own apt-get update / install. Cloud Ubuntu images routinely
# run unattended-upgrades on first boot, which holds /var/lib/apt/lists/lock
# and /var/lib/dpkg/lock-frontend for several minutes; without this wait the
# very first `apt-get update` below dies with:
#   E: Could not get lock /var/lib/apt/lists/lock. It is held by process N
# and the whole install aborts on a transient condition that would resolve
# itself in <5 min. Override the wait budget with APT_LOCK_TIMEOUT=<seconds>.
apt_wait_lock() {
    local timeout="${APT_LOCK_TIMEOUT:-300}"
    local waited=0
    local interval=3
    local locks=(
        /var/lib/dpkg/lock-frontend
        /var/lib/dpkg/lock
        /var/lib/apt/lists/lock
        /var/cache/apt/archives/lock
    )
    local have_fuser=0
    command -v fuser >/dev/null 2>&1 && have_fuser=1
    while :; do
        local held=""
        if [[ "$have_fuser" == "1" ]]; then
            local lock
            for lock in "${locks[@]}"; do
                if [[ -e "$lock" ]] && fuser "$lock" >/dev/null 2>&1; then
                    held="$lock"
                    break
                fi
            done
        else
            # fuser missing (psmisc not installed): fall back to a process-name
            # check. Less precise — flags any apt/dpkg run anywhere on the host
            # — but still good enough to avoid the lock-collision abort.
            if pgrep -x apt-get >/dev/null 2>&1 \
               || pgrep -x apt    >/dev/null 2>&1 \
               || pgrep -x dpkg   >/dev/null 2>&1 \
               || pgrep -x unattended-upgr >/dev/null 2>&1; then
                held="apt/dpkg process"
            fi
        fi
        [[ -z "$held" ]] && return 0
        if (( waited >= timeout )); then
            warn "apt/dpkg lock still held after ${timeout}s ($held); proceeding anyway."
            warn "If apt fails immediately after this, the holder may be wedged. Diagnose with:"
            warn "    sudo lsof $held   ;   sudo systemctl status unattended-upgrades"
            warn "    sudo ps -fp \$(sudo fuser $held 2>/dev/null)"
            # Return 0 so the caller's set -e / ERR trap doesn't abort here.
            # apt-get below still has -o DPkg::Lock::Timeout=300 as a real
            # backstop, and if it ultimately fails it produces a much clearer
            # diagnostic than this helper can.
            return 0
        fi
        # Log on first detection and then every 15s so the operator knows
        # the install hasn't hung — without spamming a line every 3s.
        if (( waited == 0 || waited % 15 == 0 )); then
            local holders=""
            if [[ "$have_fuser" == "1" && -e "$held" ]]; then
                holders=" (pids:$(fuser "$held" 2>/dev/null | tr -s ' ' || true))"
            fi
            log "Waiting for apt/dpkg lock $held${holders}; ${waited}s/${timeout}s elapsed..."
        fi
        sleep "$interval"
        waited=$((waited + interval))
    done
}

# Sanity-check REPO_DIR before any destructive operation. The script
# later mirrors REPO_DIR into $APP_HOME with `rsync --delete` (step 4), which
# would happily wipe /opt/wsi if REPO_DIR somehow resolved to "/" or an
# empty/wrong directory. Three load-bearing files always expected in the repo:
for marker in Makefile services/app/requirements.txt frontend/package.json; do
    if [[ ! -f "$REPO_DIR/$marker" ]]; then
        die "REPO_DIR=$REPO_DIR does not look like a WSI checkout (missing $marker). Refusing to proceed."
    fi
done

# Every file root installs into a root-consumed location (the systemd units —
# including the root-run wsi-restart.service / wsi-avahi-reload.service —
# /usr/local/bin/wsi-cache-stat, the nginx configs, the wsi.env template) is
# read from REPO_DIR, never from $APP_HOME: $APP_HOME is owned by the service
# user, and by the time those files are installed that user has run pip and
# `npm ci` / `npm run build` there, so any package install hook could have
# rewritten $APP_HOME/infra/*. That only helps if REPO_DIR is not itself the
# service user's tree, so refuse to run from the deployed copy (e.g.
# `sudo bash /opt/wsi/infra/install.sh`). Compared physically so a symlinked
# path to /opt/wsi is caught too.
_repo_real="$(cd "$REPO_DIR" && pwd -P)"
_app_real="$(realpath -m -- "$APP_HOME")"
case "$_repo_real/" in
    "$_app_real"/*)
        die "REPO_DIR=$REPO_DIR is inside $APP_HOME (the service user's deployed copy). Run install.sh from your source clone (e.g. ~/wsi) instead." ;;
esac
unset _repo_real _app_real

log "Source repo: $REPO_DIR"

# ---- 0b. Pin the working tree to the latest vX.Y.Z release tag ------
# Default: deploy the highest vX.Y.Z tag on origin so end users always
# land on a released version. Developers iterating on the very repo
# that hosts this script can opt out with WSI_TRACK_BRANCH=1, and
# update.sh sets WSI_SKIP_TAG_CHECKOUT=1 when it has already moved the
# tree itself (avoids a redundant fetch + checkout).
if [[ -d "$REPO_DIR/.git" ]] && [[ "${WSI_SKIP_TAG_CHECKOUT:-0}" != "1" ]]; then
    # run_git: as the invoking user, with `-c safe.directory='*'` (see
    # lib/wsi-common.sh). The plain `git describe` call further down in this
    # script uses the same flag, so the version-stamp path does not die on the
    # "dubious ownership" guard either.
    wsi_define_run_git "$REPO_DIR"

    # Snapshot install.sh's content before the checkout. Bash reads scripts
    # incrementally from disk, so if the checkout below replaces this very
    # file with a longer/shorter version, the in-flight parse picks up at
    # whatever byte offset bash had reached — which lands mid-statement in
    # the new file and produces cryptic errors like a bare
    # "install: No such file or directory" several steps later. Detect this
    # with a hash and re-exec from the freshly checked-out file when needed.
    # The sourced helpers are hashed too: they were read before the checkout,
    # so a release that changes only them must also be re-exec'd, or this run
    # would carry on with the previous release's helper code.
    SELF_PATH="$REPO_DIR/infra/install.sh"
    self_hash() {
        { cat -- "$SELF_PATH" "$WSI_COMMON_SH" 2>/dev/null || true; } \
            | sha256sum | awk '{print $1}'
    }
    SELF_HASH_BEFORE=""
    if command -v sha256sum >/dev/null 2>&1 && [[ -f "$SELF_PATH" ]]; then
        SELF_HASH_BEFORE="$(self_hash)"
    fi

    # What gets deployed is run as root: warn when origin's transport lets
    # anyone on the network path substitute it (lib/wsi-common.sh; update.sh
    # makes the same check before its own fetch).
    ORIGIN_WARNING="$(wsi_origin_transport_warning)"
    [[ -z "$ORIGIN_WARNING" ]] || warn "$ORIGIN_WARNING"

    if [[ -n "${GIT_REF:-}" ]]; then
        log "Pinning to GIT_REF=$GIT_REF ..."
        run_git fetch --tags --prune origin || warn "git fetch failed; using local refs."
        wsi_verify_release_tag "$GIT_REF" || die "Refusing to deploy $GIT_REF (see above)."
        run_git checkout "$GIT_REF"
    elif [[ "${WSI_TRACK_BRANCH:-0}" == "1" ]]; then
        [[ "${WSI_VERIFY_RELEASE_TAGS:-0}" != "1" ]] \
            || die "WSI_VERIFY_RELEASE_TAGS=1 cannot be combined with WSI_TRACK_BRANCH=1: a branch has no signature to check."
        log "WSI_TRACK_BRANCH=1: keeping current branch HEAD ($(run_git rev-parse --abbrev-ref HEAD))."
    else
        run_git fetch --tags --prune origin || warn "git fetch failed; using local refs."
        # The highest FINAL vX.Y.Z tag (lib/wsi-common.sh): a pre-release
        # such as v2.0.0-rc1 is never picked, whatever it sorts above; deploy
        # one deliberately with GIT_REF=<tag>.
        LATEST_TAG="$(wsi_latest_release_tag)"
        if [[ -n "$LATEST_TAG" ]]; then
            log "Deploying latest release tag: $LATEST_TAG"
            wsi_verify_release_tag "$LATEST_TAG" || die "Refusing to deploy $LATEST_TAG (see above)."
            run_git checkout "$LATEST_TAG"
        else
            [[ "${WSI_VERIFY_RELEASE_TAGS:-0}" != "1" ]] \
                || die "WSI_VERIFY_RELEASE_TAGS=1: no vX.Y.Z release tag found on origin to verify, and a branch cannot be."
            warn "No vX.Y.Z release tags found on origin; deploying current branch HEAD."
            warn "Pass WSI_TRACK_BRANCH=1 to silence this warning during development."
        fi
    fi

    # If the checkout above swapped install.sh (or its helpers) on disk,
    # re-exec from the new file so bash parses it cleanly from line 1.
    # WSI_SKIP_TAG_CHECKOUT=1 prevents an infinite loop (the new instance
    # skips this whole block).
    if [[ -n "$SELF_HASH_BEFORE" ]] && [[ -f "$SELF_PATH" ]]; then
        SELF_HASH_AFTER="$(self_hash)"
        if [[ "$SELF_HASH_BEFORE" != "$SELF_HASH_AFTER" ]]; then
            log "install.sh changed during checkout; re-execing from $SELF_PATH ..."
            export WSI_SKIP_TAG_CHECKOUT=1
            exec bash "$SELF_PATH" "$@"
        fi
    fi
fi

# ---- 1. System packages ---------------------------------------------
log "Installing system packages..."
export DEBIAN_FRONTEND=noninteractive
# -o DPkg::Lock::Timeout=300 makes apt itself wait up to 5 min for the dpkg
# lock if another package operation slips in between apt_wait_lock and the
# call below (apt_wait_lock handles the apt lists lock, which DPkg::Lock
# doesn't cover).
apt_wait_lock
apt-get -o DPkg::Lock::Timeout=300 update -qq

# OpenSlide runtime library: the binary package name tracks the SONAME,
# which bumped from libopenslide0 (OpenSlide 3.x, Ubuntu <= 24.04) to
# libopenslide1 (OpenSlide 4.x, Ubuntu 26.04 / Debian trixie). Hard-coding
# libopenslide0 aborts the entire install with "Unable to locate package"
# on any distro that ships the 4.x line — including the 26.04 target this
# script documents. Pick whichever the apt cache actually offers; the
# post-install preflight (_openslide_installed) already tolerates both
# names for exactly this reason. `apt-cache show` exits non-zero when no
# record exists, which is the cheapest reliable existence probe post-update.
OPENSLIDE_PKG="libopenslide0"
if ! apt-cache show libopenslide0 >/dev/null 2>&1; then
    if apt-cache show libopenslide1 >/dev/null 2>&1; then
        OPENSLIDE_PKG="libopenslide1"
    else
        warn "Neither libopenslide0 nor libopenslide1 is available in apt; the OpenSlide dependency will fail to install."
    fi
fi
log "OpenSlide runtime package: $OPENSLIDE_PKG"

apt_wait_lock
apt-get -o DPkg::Lock::Timeout=300 install -y --no-install-recommends \
    ca-certificates curl gnupg lsb-release git make \
    python3 python3-venv python3-pip \
    "$OPENSLIDE_PKG" openslide-tools libjpeg-dev zlib1g-dev \
    sqlite3 \
    nginx \
    avahi-daemon \
    libnss-mdns \
    rsync
# libnss-mdns is a Recommends of avahi-daemon but --no-install-recommends
# drops it. Without the NSS plug-in this host cannot resolve any *.local
# name (gethostbyname / getaddrinfo bypass mDNS) — including its own
# wsi.local advertisement — so operator diagnostics like
# `ping wsi.local` from the server itself silently fail. List it
# explicitly here to match the manual Quickstart procedure, which picks
# it up automatically via Recommends.

# ---- 1b. Python version gate ----------------------------------------
# requirements.lock (installed in step 5) is resolved for one minimum Python,
# `make lock`'s --python-version, which uv records in the lock's header. An
# older python3 has no installable pin for several packages (numpy, tifffile,
# imagecodecs publish no wheels for it), so pip would stop half-way through
# step 5 with a resolver error that names a package rather than the cause.
# Read the floor from the lock itself so the check cannot drift from it.
PY_LOCK="$REPO_DIR/services/app/requirements.lock"
[[ -f "$PY_LOCK" ]] || die "services/app/requirements.lock missing — regenerate it with 'make lock' in the source tree and commit it before re-running install.sh."
PY_FLOOR="$(sed -n -E '/^#.*[[:space:]]--python-version[[:space:]]/{s/^.*--python-version[[:space:]]+([0-9]+\.[0-9]+).*$/\1/p;q;}' "$PY_LOCK")"
[[ -n "$PY_FLOOR" ]] || die "Could not read the minimum Python version (--python-version) from the header of $PY_LOCK."
# -I (isolated): run as root, so no PYTHON* variables, no user site, and no
# current directory on sys.path — the rule for every python3 these scripts run.
PY_HAVE="$(python3 -I -c 'import sys; print("%d.%d" % sys.version_info[:2])')" \
    || die "python3 does not run after the package install."
if [[ "$(printf '%s\n' "$PY_FLOOR" "$PY_HAVE" | sort -V | head -n1)" != "$PY_FLOOR" ]]; then
    die "Python $PY_FLOOR or newer is required, but python3 is $PY_HAVE. The pinned backend dependencies (services/app/requirements.lock) are resolved for Python >= $PY_FLOOR only. Install on Ubuntu 24.04 or newer (python3 3.12+; 26.04 is the tested target)."
fi
log "Python $PY_HAVE (requirements.lock needs >= $PY_FLOOR)."

# ---- 2. Node.js (for frontend build) --------------------------------
if ! command -v node >/dev/null 2>&1 || \
   ! command -v npm  >/dev/null 2>&1 || \
   [[ "$(node -v 2>/dev/null | sed 's/v\([0-9]*\).*/\1/')" -lt "$NODE_MAJOR" ]]; then
    log "Installing Node.js $NODE_MAJOR.x from NodeSource..."
    install -d -m 0755 /etc/apt/keyrings
    curl -fsSL https://deb.nodesource.com/gpgkey/nodesource-repo.gpg.key \
        | gpg --batch --yes --dearmor -o /etc/apt/keyrings/nodesource.gpg
    chmod a+r /etc/apt/keyrings/nodesource.gpg
    echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/nodesource.gpg] https://deb.nodesource.com/node_${NODE_MAJOR}.x nodistro main" \
        > /etc/apt/sources.list.d/nodesource.list
    apt_wait_lock
    apt-get -o DPkg::Lock::Timeout=300 update -qq
    apt_wait_lock
    apt-get -o DPkg::Lock::Timeout=300 install -y nodejs
else
    log "Node.js $(node -v) already installed."
fi

# ---- 3. App user & directories --------------------------------------
if ! id -u "$APP_USER" >/dev/null 2>&1; then
    log "Creating system user $APP_USER..."
    useradd --system --home-dir "$APP_HOME" --shell /usr/sbin/nologin \
            --user-group "$APP_USER"
fi

# Allow the app user to manage Avahi service files without root.
# ProtectSystem=strict in wsi-app.service makes /etc read-only except for
# paths listed in ReadWritePaths; filesystem ownership is still enforced.
# This chown is re-applied on every install.sh run (and update.sh re-runs
# install.sh) so an apt-get upgrade of avahi-daemon that resets the
# directory back to root:root won't permanently break mDNS toggles. It
# must run after the wsi user/group is created above; on a fresh install
# the user does not exist when apt finishes installing avahi-daemon.
#
# Guard the chown with an existence check: avahi-daemon's postinst
# normally creates this directory, but slimmed-down images / unusual
# package states can leave it missing. Without the guard, set -e aborts
# the entire install on the bare "chown: cannot access" — a confusing
# late-stage failure for what is really just a missing mDNS feature.
# wsi-app.service lists the directory as an optional ("-"-prefixed)
# ReadWritePaths= entry for the same reason, so the service still starts
# without it; only the dashboard's mDNS toggle fails (read-only /etc).
if [[ -d /etc/avahi/services ]]; then
    chown "$APP_USER:$APP_GROUP" /etc/avahi/services
else
    warn "/etc/avahi/services not present; the app runs, but turning mDNS (.local) advertising on from the admin UI will fail."
    warn "Reinstall avahi-daemon to recreate it, then re-run install.sh (or update.sh) so it is handed to $APP_USER."
    POST_INSTALL_WARNINGS+=("/etc/avahi/services missing — the admin mDNS toggle fails until avahi-daemon recreates it and install.sh is re-run")
fi

# $APP_HOME and $DATA_DIR themselves sit in root-owned parents (/opt, /var/lib),
# so only root can have put anything at those two paths; plain `install -d`
# (which chowns/chmods through a symlink) is fine for them.
install -d -m 0755 -o "$APP_USER" -g "$APP_GROUP" "$APP_HOME"
install -d -m 0755 -o "$APP_USER" -g "$APP_GROUP" "$DATA_DIR"

# Everything BELOW $DATA_DIR is different: the directory is owned by the
# service user, so that user — or a compromised app running as it — can
# replace any entry with a symlink (say thumbs -> /etc). `install -d -o wsi`
# on such an entry would chown the symlink's TARGET to wsi as root, handing
# the service user a root-owned directory. So for these paths root never
# follows a symlink: a symlinked entry (or one below a symlinked parent) is
# reported and left alone. An operator who deliberately symlinks e.g. wsi/
# onto a larger volume keeps that setup; they own the target's
# ownership/permissions themselves.
#
# The `-L` walk alone is only a check, though: the service user can swap ANY
# component (not just the last one) for a symlink between the check and the
# operation, and a path-based root mkdir / `chown -h` would then resolve the
# swapped parent and act on e.g. /etc/<something>/tours. So nothing below
# acts on a path as root:
#   - the directory is created by the service user (mkdir as that user can
#     only create where it could create anyway);
#   - the one step that needs root, the chown of a real directory some
#     earlier tool left root-owned, is done from INSIDE it: a subshell
#     `cd -P`s there, checks with `pwd -P` that the directory it actually
#     landed in is the expected one under the (root-owned-parent, so
#     trustworthy) canonical DATA_DIR — i.e. no component was a link at that
#     moment — and then chowns `.`, which names that directory inode no
#     matter what happens to the path afterwards;
#   - the mode is set by the service user, whose chmod gains nothing even
#     if the entry is swapped for a link in between.
# A mismatch (a component swapped for a link after the walk) is reported and
# the entry left alone, like a symlink found by the walk.
ensure_data_subdir() {
    local mode="$1" d cur part skip data_real want
    local -a parts
    shift
    data_real="$(cd -P -- "$DATA_DIR" && pwd -P)" || return 1
    for d in "$@"; do
        cur="$DATA_DIR"
        skip=0
        IFS='/' read -ra parts <<<"${d#"$DATA_DIR"/}"
        for part in "${parts[@]}"; do
            cur="$cur/$part"
            if [[ -L "$cur" ]]; then
                warn "$cur is a symlink; not creating, chown'ing or chmod'ing through it."
                POST_INSTALL_WARNINGS+=("$cur is a symlink — left untouched; check its target's owner/mode yourself")
                skip=1
                break
            fi
        done
        [[ "$skip" -eq 1 ]] && continue
        want="$data_real/${d#"$DATA_DIR"/}"
        if ! sudo -u "$APP_USER" mkdir -p -- "$d" \
           || ! ( cd -P -- "$d" && [[ "$(pwd -P)" == "$want" ]] \
                  && chown -h "$APP_USER:$APP_GROUP" . ); then
            warn "$d could not be created / resolved to $want (a component changed during setup?); left untouched."
            POST_INSTALL_WARNINGS+=("$d could not be set up safely — left untouched; check it and its parents yourself")
            continue
        fi
        sudo -u "$APP_USER" chmod "$mode" -- "$d"
    done
}
ensure_data_subdir 0755 "$DATA_DIR/wsi" \
                        "$DATA_DIR/thumbs" \
                        "$DATA_DIR/cache" \
                        "$DATA_DIR/assets" \
                        "$DATA_DIR/assets/tours"
# Backup archives and the staged-restore working directory live alongside
# the rest of the app's data so a single filesystem snapshot covers them.
# 0750 is enough for the dashboard's bearer-token-gated download flow;
# www-data does not need to traverse this path.
ensure_data_subdir 0750 "$DATA_DIR/backups" \
                        "$DATA_DIR/backups/.staging"
install -d -m 0750 -o root -g "$APP_GROUP" "$ETC_DIR"

# Retroactively tighten a pre-existing SQLite DB. On a FRESH install the DB
# does not exist yet — the service (UMask=0027) creates it 0640 on first
# boot. But a DB put in place by hand (e.g. restored from a copy, QUICKSTART
# §6.1) may have been created under the default umask 0022, leaving admin
# password hashes / session state / analytics identities world-readable at
# 0644; UMask can't fix an already-created file. Best-effort chmod the
# default-location DB and its WAL sidecars (custom DATABASE_URL paths are the
# operator's responsibility). Guarded so a missing file is a no-op. These live
# in the service user's directory, so (as with ensure_data_subdir above) a
# symlink is skipped rather than followed, and the chmod runs as the owning
# service user, not root — a root chmod through a planted link could strip read
# access from any file on the host.
for _db in "$DATA_DIR/wsi.db" "$DATA_DIR/wsi.db-wal" "$DATA_DIR/wsi.db-shm"; do
    if [[ -L "$_db" ]]; then
        warn "$_db is a symlink; not tightening its mode."
        continue
    fi
    if [[ -f "$_db" ]]; then sudo -u "$APP_USER" chmod 0640 -- "$_db" || true; fi
done

# Tighten the env directory itself: the wsi.env file inside is 0600 and
# read by systemd as root, so the wsi group does not need traversal.
chmod 0700 "$ETC_DIR"
chown root:root "$ETC_DIR"
install -d -m 0755 -o www-data -g www-data "$CACHE_DIR"
# The edge app-access verdict cache (wsi_authz_cache in wsi.conf) is a
# separate, short-lived zone so junk session cookies cannot crowd tiles out
# of the tile zone. Fixed path, like CACHE_DIR.
install -d -m 0755 -o www-data -g www-data /var/cache/nginx/wsi_authz
# Likewise for thumbnails and tour attachments (wsi_static_cache): client-chosen
# names must not be able to crowd tiles out of the tile zone either.
install -d -m 0755 -o www-data -g www-data /var/cache/nginx/wsi_static

# ---- Backup/restore: persistent /run/wsi for IPC ----
# The dashboard's restore-apply path drops a request file at
# /run/wsi/restore.request that start-wsi-app.sh reads on the next service
# start, and writes /run/wsi/restore.result back for the UI to display.
# tmpfiles.d ensures /run/wsi exists as wsi:wsi after every reboot without
# baking a RuntimeDirectory line into the unit (which would also wipe the
# directory between restarts and lose the request mid-restart).
#
# Some minimal Ubuntu images (and Ubuntu 26.04 as observed in the field)
# ship without /etc/tmpfiles.d pre-created, so the install below fails
# with a bare "install: No such file or directory". Create the parent
# unconditionally — install -d is a no-op when it already exists.
#
# The script deliberately avoids `install ... /dev/stdin` here: under Ubuntu 26.04
# / coreutils 9.x that combination dies with the same terse error in
# environments where /proc/self/fd/0 doesn't resolve to a regular-file-
# stat'able target (sandboxes, certain seccomp/apparmor setups, or
# heredoc-as-pipe). Writing the content to a real tempfile first and
# installing from there sidesteps the whole problem.
install -d -m 0755 /etc/tmpfiles.d
TMPFILES_STAGING="$(mktemp)"
cat > "$TMPFILES_STAGING" <<EOF
# Type Path        Mode UID GID Age Argument
d      /run/wsi    0755 wsi wsi -   -
EOF
install -m 0644 -o root -g root "$TMPFILES_STAGING" /etc/tmpfiles.d/wsi.conf
rm -f "$TMPFILES_STAGING"
systemd-tmpfiles --create /etc/tmpfiles.d/wsi.conf >/dev/null 2>&1 || true

# ---- Backup/restore: restart is handled by the wsi-restart.path unit ----
# The restore-apply path avoids `sudo systemctl`: the app
# runs under NoNewPrivileges=true, which refuses setuid sudo. Instead the app
# drops /run/wsi/restart.trigger and the root-owned wsi-restart.path unit
# (installed in the systemd section below) does the privileged restart — the
# same mechanism wsi-avahi-reload.path already uses.

# ---- 4. Sync repo into $APP_HOME ------------------------------------
# Capture the previously-deployed version BEFORE rsync clobbers
# $APP_HOME (the rsync's --delete wipes VERSION). Used below in the
# "Stamped deployed version: X -> Y" log line so it reports the real
# prior deploy. Read AS THE SERVICE USER, like every other access to its
# tree: VERSION may be a symlink it planted, and a root read would follow it
# to any file on the host and print it into the install log. head -c bounds
# what is read; a missing / unreadable file just leaves this empty.
PREV_DEPLOYED_VER="$(sudo -u "$APP_USER" head -c 256 -- "$APP_HOME/VERSION" 2>/dev/null \
                     | tr -d '[:space:]' || true)"

log "Syncing repo -> $APP_HOME ..."
# Excludes mirror .gitignore's "build artefacts / local-only" set so a
# developer's REPO_DIR (where __pycache__, .pytest_cache, etc. naturally
# accumulate from running tests locally) doesn't drag stale bytecode and
# tool caches into /opt/wsi. Python ignores out-of-date .pyc files
# silently, so leaving these is not a correctness bug — but they bloat
# $APP_HOME and slow every subsequent rsync on the same host.
#
# The copy INTO $APP_HOME runs as the service user, never as root. That
# user owns $APP_HOME and has run pip and npm there (so has every package's
# install hook), and rsync's receiver works by path: a directory swapped for
# a symlink while the transfer runs (say services/app -> /etc/cron.d) would
# have a root rsync write, chmod and, with --delete, DELETE files wherever
# the link points. Run as that user, the same swap gains nothing it could not
# do itself. The service user usually cannot read REPO_DIR, though (Ubuntu
# creates home directories 0750), so root first copies the clone into a
# fresh directory of its own under /tmp — created 0700 by mktemp, so nobody
# else can reach into it while root writes there — owned throughout by the
# service user (--chown), and only then hands that directory over. The
# second rsync, as the service user, applies it to $APP_HOME with --delete.
# Both use the same excludes: on the second they are also what protects the
# service user's venv, node_modules, caches and static build from --delete.
# Everything the second rsync creates is owned by the service user, so no
# `chown -R` over $APP_HOME follows.
sync_repo_to_app_home() {
    local stage rc=0
    local -a excludes=(
        --exclude='.git'
        --exclude='data/'
        --exclude='.venv'
        --exclude='venv'
        --exclude='.env*'
        --exclude='.vscode'
        --exclude='.idea'
        --exclude='__pycache__'
        --exclude='*.pyc'
        --exclude='*.pyo'
        --exclude='*.egg-info'
        --exclude='.pytest_cache'
        --exclude='.mypy_cache'
        --exclude='.ruff_cache'
        --exclude='frontend/node_modules'
        --exclude='frontend/dist'
        --exclude='/services/app/static*'
        --exclude='/.npm'
        --exclude='/.cache'
    )
    # Called as `sync_repo_to_app_home || die`, so neither `set -e` nor the
    # ERR trap act in here: every step checks its own status.
    stage="$(mktemp -d /tmp/wsi-sync.XXXXXX)" || return 1
    if ! rsync -a --chown="$APP_USER:$APP_GROUP" "${excludes[@]}" \
             "$REPO_DIR/" "$stage/tree/" \
       || ! chown "$APP_USER:$APP_GROUP" "$stage"; then
        rm -rf -- "$stage"
        return 1
    fi
    sudo -u "$APP_USER" rsync -a --delete "${excludes[@]}" \
        "$stage/tree/" "$APP_HOME/" || rc=$?
    # The staging copy now belongs to the service user, so it removes it.
    sudo -u "$APP_USER" rm -rf -- "$stage" \
        || warn "Could not remove the staging copy $stage; delete it by hand."
    return "$rc"
}
sync_repo_to_app_home \
    || die "Copying $REPO_DIR into $APP_HOME as $APP_USER failed (rsync's message is above). 'Permission denied' under $APP_HOME means an entry there is not owned by $APP_USER — e.g. left by a command run as root: sudo chown -R -h $APP_USER:$APP_GROUP $APP_HOME, then re-run."
# `/.npm` and `/.cache` are the service user's npm and pip download caches:
# its home directory IS $APP_HOME (see useradd above), so without these
# excludes `--delete` would wipe both on every run. Every update would then
# re-download the full npm + pip dependency set — and so would update.sh's
# automatic rollback, which is exactly the moment the network is most likely
# to be the thing that just failed. Kept, `npm ci` / `pip install` for an
# already-seen lockfile / requirements set can be satisfied from the cache.
# `/services/app/static*` is kept out of the rsync (anchored to source root)
# so an already-deployed static tree isn't wiped during the long npm-build
# window of a re-run. The trailing `*` is essential: the served `static` is a
# symlink into a sibling `static.rel.<pid>` real build dir (see the atomic
# publish below), and `--delete` protects only files matched by an exclude —
# so excluding just `static` would leave the symlink intact but let --delete
# reap its target directory, dangling the link and 404-ing every /assets/*
# for the whole pip+npm+vite window. The pattern also covers the transient
# `static.lnk.<pid>`/`static.legacy.<pid>` names used during publish. The new
# build below is staged into a sibling directory and swapped in with rename(2),
# keeping nginx-served assets continuously reachable for stand-alone install.sh
# re-runs against a live server. update.sh stops wsi-app first, so this only
# matters there for nginx's /assets/* path, not for the API.

# Stamp the deployed git ref so the admin Server Info page shows what
# was actually checked out. The rsync above strips .git, so the
# description is resolved from the source repo and the result is written
# into $APP_HOME/VERSION (read by version.py).
# `safe.directory='*'` defuses git's "dubious ownership" guard in case
# REPO_DIR is owned by a non-root developer.
if [[ -d "$REPO_DIR/.git" ]]; then
    DEPLOYED_VER=$(git -c safe.directory='*' -C "$REPO_DIR" \
        describe --tags --always 2>/dev/null || true)
    if [[ -n "$DEPLOYED_VER" ]]; then
        # Write VERSION entirely AS THE SERVICE USER. It owns $APP_HOME, so
        # anything at that path may be a symlink it planted (or swaps in
        # mid-operation), and every root-side way of writing it follows one
        # somewhere: `printf >` + chown opens and hands over the link's
        # target, and even install(1) — which unlinks the destination and
        # creates the new file O_EXCL, then fchownat(AT_SYMLINK_NOFOLLOW)s it
        # — sets the mode with a path-based fchmodat() that DOES follow a
        # link swapped in after the create (verified with strace, coreutils
        # 9.4), i.e. a root chmod 0644 of an arbitrary file. As the service
        # user the same race gains nothing. Staged in a temp file in the same
        # directory and renamed over VERSION, so readers never see it empty;
        # the chmod is on that private temp file, before the rename.
        printf '%s\n' "$DEPLOYED_VER" | sudo -u "$APP_USER" sh -c '
            tmp="$(mktemp "$1/.VERSION.XXXXXX")" || exit 1
            if cat > "$tmp" && chmod 0644 "$tmp" && mv -fT "$tmp" "$1/VERSION"; then
                exit 0
            fi
            rm -f "$tmp"
            exit 1' sh "$APP_HOME"
        # Both sides come from the same producer (`git describe --tags
        # --always` writes here, and PREV is just the previous run's
        # write read back), so a string compare is sufficient — no
        # leading-`v` normalisation needed.
        if [[ -n "$PREV_DEPLOYED_VER" && "$PREV_DEPLOYED_VER" != "$DEPLOYED_VER" ]]; then
            log "Stamped deployed version: $PREV_DEPLOYED_VER -> $DEPLOYED_VER ($APP_HOME/VERSION)"
        else
            log "Stamped deployed version: $DEPLOYED_VER (no change) -> $APP_HOME/VERSION"
        fi
    else
        warn "git describe failed; admin tab will fall back to package.json."
    fi
else
    warn "$REPO_DIR has no .git; admin tab will fall back to package.json."
fi

# ---- 5. Python venv + backend deps ----------------------------------
log "Creating Python venv at $VENV_DIR ..."
# `python3 -m venv` on an existing path is a no-op without --upgrade, so
# an apt-upgrade of the host's python3 (e.g. 3.10 -> 3.12) would leave
# the venv's bin/python symlink dangling and the next pip call dying
# with a bare "No such file or directory". Use --upgrade on re-runs to
# refresh interpreter symlinks and stdlib; bare invocation on the
# fresh-install path because --upgrade requires the venv to exist.
# -I: `-m` would otherwise put the current directory (wherever install.sh
# was started from) first on sys.path, ahead of the stdlib's venv.
if [[ -d "$VENV_DIR/bin" ]]; then
    sudo -u "$APP_USER" python3 -I -m venv --upgrade "$VENV_DIR"
else
    sudo -u "$APP_USER" python3 -I -m venv "$VENV_DIR"
fi
# Install the exact, hash-pinned dependency set from requirements.lock — the
# Python counterpart of `npm ci` below. requirements.txt only states the
# accepted ranges; resolving it live would give two hosts provisioned a week
# apart different trees from the same commit, and would let a new upstream
# release (or a tampered download) reach the server unannounced on the next
# update.sh run. --require-hashes makes pip refuse any file whose digest is
# not in the lock. The lock is universal (pins that differ by Python version
# carry environment markers, from the floor checked in step 1b up), so one
# file serves every supported Ubuntu python3.
# Like the npm lockfile, a missing lock is a source-tree mistake, not
# something to paper over with a live resolve.
#
# The venv's own pip does the install: no `pip install --upgrade pip` first,
# which would fetch whatever pip release is newest — the one unpinned,
# unhashed download of the whole step. Hash-checking mode needs nothing newer
# than the pip Ubuntu's python3-venv bundles (24.0 on 24.04), and every
# locked package ships a wheel, so nothing is built (no `wheel` needed).
LOCK_FILE="$APP_HOME/services/app/requirements.lock"
[[ -f "$LOCK_FILE" ]] || die "services/app/requirements.lock missing — regenerate it with 'make lock' in the source tree and commit it before re-running install.sh."
sudo -u "$APP_USER" "$VENV_DIR/bin/pip" install --require-hashes -r "$LOCK_FILE"

# ---- 6. Frontend build ----------------------------------------------
NPM_BIN="$(command -v npm)" || die "npm not found in PATH"
log "Using npm at $NPM_BIN"
log "Installing frontend npm deps..."
pushd "$APP_HOME/frontend" >/dev/null
# `npm ci` is mandatory: a committed package-lock.json pins transitive
# dependencies, so a fresh node_modules/ is built strictly from the
# lockfile and every host gets the same tree. The lockfile is checked
# into the repo (.gitignore intentionally does NOT ignore it); if it is
# missing, that's a developer-side mistake, not a deployment-time
# condition to silently paper over. Refuse to build rather than fall back
# to a non-deterministic `npm install` that would pin nothing.
if [[ ! -f package-lock.json ]]; then
    die "frontend/package-lock.json missing — regenerate with 'npm install' in the source tree and commit it before re-running install.sh."
fi
# `npm ci` fetches every package in the lockfile from the registry, so a single
# transient network blip — a proxy hiccup, a slow mirror, a dropped connection —
# aborts the whole install with `npm error network ... ETIMEDOUT`. That is
# exactly the class of failure a retry resolves, so make the fetch resilient on
# two levels:
#   1. Tell npm to retry individual requests more patiently than its stock
#      2 retries / 5-minute timeout (its built-in per-request backoff), and
#   2. wrap the whole `npm ci` in an outer retry loop with exponential backoff,
#      because some failures (a hung socket, a half-written cache entry) only
#      clear once the command is restarted from scratch. `npm ci` first wipes
#      node_modules/ and rebuilds strictly from the lockfile, so re-running it
#      is idempotent — no partial state survives between attempts.
# Every knob is overridable so air-gapped / private-mirror setups can retune or
# disable the extra patience without editing the script.
NPM_FETCH_RETRIES="${WSI_NPM_FETCH_RETRIES:-5}"
NPM_FETCH_TIMEOUT="${WSI_NPM_FETCH_TIMEOUT:-600000}"                   # 10 min / request
NPM_FETCH_RETRY_MAXTIMEOUT="${WSI_NPM_FETCH_RETRY_MAXTIMEOUT:-120000}" # cap between retries
NPM_CI_ATTEMPTS="${WSI_NPM_CI_ATTEMPTS:-4}"

npm_ci_once() {
    # Config passed as flags (not `npm config set`) so nothing is written to the
    # APP_USER's ~/.npmrc and the behaviour is self-contained to this install.
    sudo -u "$APP_USER" "$NPM_BIN" ci --no-audit --no-fund --loglevel=warn \
        --fetch-retries="$NPM_FETCH_RETRIES" \
        --fetch-timeout="$NPM_FETCH_TIMEOUT" \
        --fetch-retry-maxtimeout="$NPM_FETCH_RETRY_MAXTIMEOUT"
}

# Called in an `if` condition, so `set -e` / the ERR trap stay dormant on a
# failed attempt and the loop — not the trap — decides whether to retry or die.
npm_ci_attempt=1
npm_ci_delay=2
while :; do
    if npm_ci_once; then
        [[ "$npm_ci_attempt" -gt 1 ]] && log "npm ci succeeded on attempt $npm_ci_attempt."
        break
    fi
    if [[ "$npm_ci_attempt" -ge "$NPM_CI_ATTEMPTS" ]]; then
        die "npm ci failed after $NPM_CI_ATTEMPTS attempts (last error above). This is almost always a network/registry problem: check connectivity to the npm registry (or point npm at a reachable mirror) and re-run install.sh."
    fi
    warn "npm ci attempt $npm_ci_attempt/$NPM_CI_ATTEMPTS failed (see error above); retrying in ${npm_ci_delay}s ..."
    sleep "$npm_ci_delay"
    npm_ci_attempt=$((npm_ci_attempt + 1))
    npm_ci_delay=$((npm_ci_delay * 2))
done
log "Building frontend..."
# `env VITE_LAN_HOST=...` (not a bare `VAR=val sudo ...`) so the value
# survives sudo's env_reset and reaches Vite inside the APP_USER process.
# Only pass each var when set, keeping the stock build invocation unchanged.
# The optional first-probe timeout rides along only when the probe is enabled
# and a value was given; VITE_PUBLIC_BASE_URL rides along independently of the
# probe (the public-URL affordance and the LAN probe are separate features), so
# the all-unset case stays byte-identical to the stock build.
build_env=()
if [[ -n "$WSI_LAN_HOST" ]]; then
    log "  LAN-direct probe enabled: VITE_LAN_HOST=$WSI_LAN_HOST"
    build_env+=(VITE_LAN_HOST="$WSI_LAN_HOST")
    if [[ -n "$WSI_LAN_PROBE_TIMEOUT_MS" ]]; then
        log "  First-probe wait window: VITE_LAN_PROBE_TIMEOUT_MS=${WSI_LAN_PROBE_TIMEOUT_MS} ms"
        build_env+=(VITE_LAN_PROBE_TIMEOUT_MS="$WSI_LAN_PROBE_TIMEOUT_MS")
    fi
fi
if [[ -n "$WSI_PUBLIC_BASE_URL" ]]; then
    log "  Public-route share URL enabled: VITE_PUBLIC_BASE_URL=$WSI_PUBLIC_BASE_URL"
    build_env+=(VITE_PUBLIC_BASE_URL="$WSI_PUBLIC_BASE_URL")
fi
if [[ ${#build_env[@]} -gt 0 ]]; then
    sudo -u "$APP_USER" env "${build_env[@]}" "$NPM_BIN" run build
else
    sudo -u "$APP_USER" "$NPM_BIN" run build
fi
popd >/dev/null

log "Staging frontend dist -> $APP_HOME/services/app/static ..."
# Publish the new build by atomically flipping a SYMLINK, so the served path
# is never absent even for an instant. A two-step
# `mv static static.old; mv static.new static` would leave `static` non-existent
# between the two renames (a sub-second 404 window for /assets/* on a
# stand-alone install.sh re-run against a live server), because rename(2)
# cannot replace a *populated* directory in one call. A symlink flip can:
# `static` becomes a symlink to a timestamped real build dir, and pointing it
# at a new build is a single atomic rename(2) over the old symlink.
#
# Every step below runs AS THE SERVICE USER. These paths (and frontend/dist,
# which that user's `npm run build` just wrote) sit in the service user's own
# tree and are excluded from the rsync, so any of them may be a symlink that
# user planted. As root, `install -d -o wsi` through one would chown its
# target, `cp -a` would write root-owned files wherever it points, and a
# symlinked dist/ would copy arbitrary root-readable files into the public
# static tree. As the service user the same operations gain nothing.
STATIC_DIR="$APP_HOME/services/app/static"
STATIC_REL_NEW="${STATIC_DIR}.rel.$$"
STATIC_LNK_TMP="${STATIC_DIR}.lnk.$$"
as_app_user() { sudo -u "$APP_USER" "$@"; }
as_app_user rm -rf -- "$STATIC_REL_NEW" "$STATIC_LNK_TMP"
as_app_user install -d -m 0755 -- "$STATIC_REL_NEW"
as_app_user cp -a -- "$APP_HOME/frontend/dist/." "$STATIC_REL_NEW/"
# Relative target so the link keeps resolving if $APP_HOME is ever relocated.
as_app_user ln -s -- "$(basename "$STATIC_REL_NEW")" "$STATIC_LNK_TMP"
# If `static` is a real directory (one published by hand per QUICKSTART
# §3.6), move it aside first — `mv -T` of the new link onto a
# directory would fail. This is the only path with a brief window, and runs
# only until the symlink is in place.
if [[ -d "$STATIC_DIR" && ! -L "$STATIC_DIR" ]]; then
    STATIC_LEGACY="${STATIC_DIR}.legacy.$$"
    as_app_user mv -T -- "$STATIC_DIR" "$STATIC_LEGACY"
    as_app_user rm -rf -- "$STATIC_LEGACY" 2>/dev/null || true
fi
# Atomic publish: replace the (symlink) `static` with the new symlink.
as_app_user mv -T -- "$STATIC_LNK_TMP" "$STATIC_DIR"
# Reap superseded build dirs, keeping only the one the link now points at.
STATIC_CUR="$(readlink "$STATIC_DIR" 2>/dev/null || true)"
for _old_rel in "${STATIC_DIR}".rel.*; do
    [[ -e "$_old_rel" ]] || continue
    [[ "$(basename "$_old_rel")" == "$STATIC_CUR" ]] && continue
    as_app_user rm -rf -- "$_old_rel" 2>/dev/null || true
done

# ---- 7. Env file ----------------------------------------------------
ADMIN_USERNAME_DEFAULT="admin"
ADMIN_PW_FILE="$ETC_DIR/admin-initial-password.txt"

FRESH_INSTALL=0
if [[ ! -f "$ETC_DIR/wsi.env" ]]; then
    FRESH_INSTALL=1
    log "Installing $ETC_DIR/wsi.env from template..."

    # Generate JWT secret + bootstrap admin password BEFORE creating
    # wsi.env. If openssl fails (entropy starvation, broken binary, …)
    # the script aborts cleanly without ever materialising an env file. This
    # ordering matters: a wsi.env written before the secrets are ready could
    # persist with `JWT_SECRET=` empty, and a re-run would see wsi.env exists,
    # take the else branch, and leave the app refusing to boot with no
    # obvious cause.
    #
    # Over-generate the password: `openssl rand -base64 12` yields
    # exactly 16 base64 chars, but `tr -d '+/='` removes the (~3%
    # prevalence) '+' and '/' characters, so the post-head-c-16 result
    # can drop below 16 chars. 24 input bytes -> ~32 base64 chars; even
    # after removals there are comfortably >=16 left for `head -c 16`.
    JWT_SECRET="$(openssl rand -hex 48)"
    ADMIN_PW_DEFAULT="$(openssl rand -base64 24 | tr -d '+/=' | head -c 16)"
    # ANALYTICS_HASH_SALT pseudonymises user identities when the operator
    # selects analytics_mode="hashed" from the admin UI. Generated up-front
    # so the option is selectable from the first dashboard visit; the salt
    # never reaches the UI and is not rotated automatically (operator
    # responsibility — overwrite this line in wsi.env to rotate).
    ANALYTICS_HASH_SALT="$(openssl rand -hex 32)"
    # WSI_MFA_KEY seals the staff two-factor secrets (wsi_app/mfa.py).
    WSI_MFA_KEY="$(openssl rand -hex 32)"
    [[ -n "$JWT_SECRET" && ${#ADMIN_PW_DEFAULT} -ge 16 && -n "$ANALYTICS_HASH_SALT" && -n "$WSI_MFA_KEY" ]] \
        || die "failed to generate JWT_SECRET / ADMIN_BOOTSTRAP_PASSWORD / ANALYTICS_HASH_SALT / WSI_MFA_KEY via openssl"

    # Stage in the same directory as the final file so the closing
    # `mv` is rename(2) on the same filesystem — atomic on POSIX. Until
    # the mv, $ETC_DIR/wsi.env does not exist, so an interrupted run
    # never leaves a half-populated env file behind: the [[ ! -f ]]
    # guard above will fire again on the next invocation and the work
    # is redone from scratch. 0600 root:root: the file holds JWT_SECRET
    # and the bootstrap admin password. systemd reads EnvironmentFile=
    # as root before dropping to User=wsi, so the wsi user does not
    # need read access here.
    WSI_ENV_STAGING="${ETC_DIR}/wsi.env.new"
    # From REPO_DIR, not $APP_HOME: pip / npm have already run in the latter
    # as the service user (see the REPO_DIR guard near the top).
    install -m 0600 -o root -g root /dev/null "$WSI_ENV_STAGING"

    # The secrets reach awk through its environment (ENVIRON), never its
    # argv: a `sed -e "s|...|${JWT_SECRET}|"` puts them on the command line,
    # where any local user can read them from `ps` / /proc/<pid>/cmdline
    # while the process runs. /proc/<pid>/environ is readable only by the
    # process owner (root here). ENVIRON values are also taken literally
    # (no backslash-escape processing, unlike `awk -v`).
    WSI_ENV_ADMIN_USER="$ADMIN_USERNAME_DEFAULT" \
    WSI_ENV_ADMIN_PW="$ADMIN_PW_DEFAULT" \
    WSI_ENV_JWT_SECRET="$JWT_SECRET" \
    WSI_ENV_HASH_SALT="$ANALYTICS_HASH_SALT" \
    WSI_ENV_MFA_KEY="$WSI_MFA_KEY" \
    awk '
        /^ADMIN_BOOTSTRAP_USERNAME=/ { print "ADMIN_BOOTSTRAP_USERNAME=" ENVIRON["WSI_ENV_ADMIN_USER"]; next }
        /^ADMIN_BOOTSTRAP_PASSWORD=/ { print "ADMIN_BOOTSTRAP_PASSWORD=" ENVIRON["WSI_ENV_ADMIN_PW"]; next }
        /^JWT_SECRET=/               { print "JWT_SECRET=" ENVIRON["WSI_ENV_JWT_SECRET"]; next }
        /^ANALYTICS_HASH_SALT=/      { print "ANALYTICS_HASH_SALT=" ENVIRON["WSI_ENV_HASH_SALT"]; next }
        /^WSI_MFA_KEY=/              { print "WSI_MFA_KEY=" ENVIRON["WSI_ENV_MFA_KEY"]; next }
        { print }
    ' "$REPO_DIR/infra/wsi.env.example" > "$WSI_ENV_STAGING"

    mv "$WSI_ENV_STAGING" "$ETC_DIR/wsi.env"

    # Write the initial password to a 0600 root:root file rather than
    # echoing it into stdout / journald. systemd captures install.sh's
    # stdout when it runs from a unit, and operators frequently pipe the
    # script through tee/script(1) for an audit log; either way the
    # plaintext lands somewhere outside the script's control. A dedicated mode-0600
    # file under /etc/wsi keeps the secret on disk under the same access
    # boundary as wsi.env itself.
    install -m 0600 -o root -g root /dev/null "$ADMIN_PW_FILE"
    printf 'username=%s\npassword=%s\n' \
           "$ADMIN_USERNAME_DEFAULT" "$ADMIN_PW_DEFAULT" > "$ADMIN_PW_FILE"
    # Don't echo the password — point the operator at the file.
    log "Admin credentials written to $ADMIN_PW_FILE (mode 0600 root:root)."
    log "Read with: sudo cat $ADMIN_PW_FILE — change the password after first login."
    # Clear the in-shell variable so a later `set -x` or accidental `env`
    # dump doesn't reveal it. (set -u keeps a later expansion noisy.)
    unset ADMIN_PW_DEFAULT JWT_SECRET ANALYTICS_HASH_SALT WSI_MFA_KEY
else
    log "$ETC_DIR/wsi.env already exists; leaving secrets untouched."
fi

# Persist env-supplied tunables into wsi.env. The three values below are
# read back out of wsi.env by the precedence logic near the top of this
# script, and the comments there promise "set it once and every future
# update keeps it" — which only holds if the value actually lands in
# wsi.env. Only values that arrived via the environment AND survived
# validation are written; values merely read back from wsi.env are
# already there and are left alone.
persist_env_var() {
    local key="$1" value="$2" file="$ETC_DIR/wsi.env"
    [[ -f "$file" ]] || return 0
    # The staged copy is mv'd over the path, which would replace a symlink
    # (a wsi.env kept elsewhere, e.g. by configuration management) with a
    # plain file: later edits to the real one would stop taking effect, and
    # one re-linked would come back without what was written here — a
    # WSI_MFA_KEY regenerated then no longer opens the secrets sealed under
    # the first. Leave it to the operator.
    if [[ -L "$file" ]]; then
        local hint="${key}=<value>"
        [[ "$key" == WSI_MFA_KEY ]] && hint="WSI_MFA_KEY=<output of: openssl rand -hex 32>"
        warn "$file is a symlink; not rewriting it. Add ${hint} to $(readlink -f -- "$file") yourself."
        POST_INSTALL_WARNINGS+=("$file is a symlink — ${key} was not written; add it to the file it points to")
        return 0
    fi
    local current
    current="$(wsi_env_value "$key" "$file")"
    [[ "$current" == "$value" ]] && return 0
    # Drop every *live* assignment of the key, then append the new one last.
    #
    # Both consumers of wsi.env take the LAST uncommented assignment: the
    # read-back at the top of this script is `sed ... | tail -n1`, and
    # systemd's EnvironmentFile= lets a later line win. Editing an earlier
    # line in place (including the commented-out template line the example
    # file ships) would therefore be a silent no-op whenever the operator
    # has appended their own value further down — the exact "value silently
    # reverts" failure this function exists to prevent. Deleting the live
    # ones and appending is the only rule that provably agrees with both
    # readers, and it also guarantees a single live assignment per key.
    # Commented-out template lines are left alone: they are documentation.
    #
    # awk + ENVIRON keeps arbitrary characters in $value (URLs contain
    # '/', which would terminate a sed s/// expression) out of the program
    # text. Staged + mv for the same atomicity argument as the initial
    # wsi.env write above — including creating the staging file 0600 up
    # front rather than chmod'ing it afterwards, so the copy that carries
    # JWT_SECRET and the bootstrap password is never briefly umask-wide.
    local staged="${file}.new"
    install -m 0600 -o root -g root /dev/null "$staged"
    PERSIST_KEY="$key" PERSIST_VALUE="$value" awk '
        $0 ~ ("^[ \t]*" ENVIRON["PERSIST_KEY"] "[ \t]*=") { next }
        { print }
        END { print ENVIRON["PERSIST_KEY"] "=" ENVIRON["PERSIST_VALUE"] }
    ' "$file" > "$staged"
    mv "$staged" "$file"
    log "Persisted ${key} into $file (future update.sh runs will keep it)."
}
[[ -n "$WSI_LAN_HOST_FROM_ENV" && -n "$WSI_LAN_HOST" ]] \
    && persist_env_var WSI_LAN_HOST "$WSI_LAN_HOST"
[[ -n "$WSI_LAN_PROBE_TIMEOUT_MS_FROM_ENV" && -n "$WSI_LAN_PROBE_TIMEOUT_MS" ]] \
    && persist_env_var WSI_LAN_PROBE_TIMEOUT_MS "$WSI_LAN_PROBE_TIMEOUT_MS"
[[ -n "$WSI_PUBLIC_BASE_URL_FROM_ENV" && -n "$WSI_PUBLIC_BASE_URL" ]] \
    && persist_env_var WSI_PUBLIC_BASE_URL "$WSI_PUBLIC_BASE_URL"

# Add a WSI_MFA_KEY to a wsi.env that has none, so two-factor sign-in works
# after an update without a hand edit. Only when it is absent or empty:
# nothing can have been sealed under a key that never existed, and an existing
# key is never replaced (the two-factor secrets in the database open only
# under it).
#
# mfa_key_missing FILE: true when no live WSI_MFA_KEY assignment in FILE
# holds a value. Deliberately not wsi_env_value: that read-back returns ""
# for a value it cannot parse (a trailing comment, a '#' or a quote inside),
# and taking such a key for missing would replace it. Any non-empty value,
# however written and on whichever line, counts as a key — persist_env_var
# would delete every live line, so a stray empty one below a real key must
# not set this off either (the operator fixes that file by hand).
mfa_key_missing() {
    local file="$1" line value
    [[ -f "$file" ]] || return 1
    while IFS= read -r line; do
        value="${line#*=}"
        value="${value//$'\r'/}"
        value="${value#"${value%%[![:space:]]*}"}"
        value="${value%"${value##*[![:space:]]}"}"
        [[ -z "$value" || "$value" == '""' || "$value" == "''" ]] || return 1
    # `|| true`: no line at all is the usual case, and grep's exit 1 would
    # set off the ERR trap (inherited under -E) inside the substitution,
    # printing a FAILED line for a run that is fine.
    done < <(grep -E '^[[:space:]]*WSI_MFA_KEY[[:space:]]*=' "$file" || true)
    return 0
}
# A non-empty key given to the unit itself (a drop-in's Environment=) counts
# too: the EnvironmentFile line appended here would override it.
mfa_key_in_unit() {
    command -v systemctl >/dev/null 2>&1 || return 1
    systemctl show -p Environment wsi-app.service 2>/dev/null \
        | grep -Eq '(^Environment=|[[:space:]])WSI_MFA_KEY=[^[:space:]]'
}
if mfa_key_missing "$ETC_DIR/wsi.env"; then
    if mfa_key_in_unit; then
        log "WSI_MFA_KEY is set on the wsi-app unit (Environment=); not adding one to $ETC_DIR/wsi.env."
    else
        NEW_MFA_KEY="$(openssl rand -hex 32)"
        [[ -n "$NEW_MFA_KEY" ]] || die "failed to generate WSI_MFA_KEY via openssl"
        persist_env_var WSI_MFA_KEY "$NEW_MFA_KEY"
        unset NEW_MFA_KEY
    fi
fi

# ---- 8. systemd unit ------------------------------------------------
log "Installing systemd units..."
# Every root-consumed file below is installed from REPO_DIR, not from the
# rsync'd copy in $APP_HOME: the service user owns $APP_HOME and has just run
# pip and npm (and so every package's install hooks) in it, so a file under
# $APP_HOME/infra may no longer be what the checkout holds — a rewritten
# wsi-restart.service would be installed and run as root. REPO_DIR is the
# operator's own clone (the guard near the top refuses to run from inside
# $APP_HOME). The content is identical: $APP_HOME/infra is rsync'd from it.
#
# start-wsi-app.sh is the one file used in place under $APP_HOME (it is
# wsi-app.service's ExecStart, run as the service user). Mark it executable
# AS that user: a root chmod would follow a symlink planted at that path and
# could flip the mode of any file on the host, while the service user's own
# chmod gains it nothing it could not do already.
as_app_user chmod +x -- "$APP_HOME/infra/systemd/start-wsi-app.sh"
install -m 0644 "$REPO_DIR/infra/systemd/wsi-app.service" \
                /etc/systemd/system/wsi-app.service
# wsi-sync.service runs the single-worker uvicorn that owns in-memory
# live-sync rooms (port 8001). Multi-worker wsi-app cannot host them
# coherently, so they live in a sibling unit and nginx splits traffic.
install -m 0644 "$REPO_DIR/infra/systemd/wsi-sync.service" \
                /etc/systemd/system/wsi-sync.service

# wsi-cache-stat.timer runs wsi-cache-stat.service every minute as www-data.
# nginx cache subdirs are 0700 (www-data only); this timer writes a JSON
# snapshot of du/find output to /run/wsi-cache-stat/nginx_cache_stat.json
# (a www-data-owned RuntimeDirectory= dir distinct from the wsi:wsi
# /run/wsi IPC dir above) so the wsi app (which has NoNewPrivileges=true
# and cannot use sudo) can read the actual cache size.
install -m 0755 "$REPO_DIR/infra/systemd/wsi-cache-stat.sh" \
                /usr/local/bin/wsi-cache-stat
install -m 0644 "$REPO_DIR/infra/systemd/wsi-cache-stat.service" \
                /etc/systemd/system/wsi-cache-stat.service
install -m 0644 "$REPO_DIR/infra/systemd/wsi-cache-stat.timer" \
                /etc/systemd/system/wsi-cache-stat.timer

# wsi-avahi-reload.path watches /etc/avahi/services and triggers
# wsi-avahi-reload.service to reload avahi-daemon whenever the wsi app
# writes or removes its service file.  This avoids the need for the app
# process (NoNewPrivileges=true) to invoke systemctl directly.
install -m 0644 "$REPO_DIR/infra/systemd/wsi-avahi-reload.path" \
                /etc/systemd/system/wsi-avahi-reload.path
install -m 0644 "$REPO_DIR/infra/systemd/wsi-avahi-reload.service" \
                /etc/systemd/system/wsi-avahi-reload.service

# wsi-restart.path watches /run/wsi/restart.trigger and triggers
# wsi-restart.service to restart wsi-app whenever the app drops the trigger
# file (restore / reset apply). A
# sudoers-based `sudo systemctl restart` could not work under
# the app's NoNewPrivileges=true. Same pattern as wsi-avahi-reload.path above.
install -m 0644 "$REPO_DIR/infra/systemd/wsi-restart.path" \
                /etc/systemd/system/wsi-restart.path
install -m 0644 "$REPO_DIR/infra/systemd/wsi-restart.service" \
                /etc/systemd/system/wsi-restart.service

systemctl daemon-reload

# ---- 9. nginx site --------------------------------------------------
log "Installing nginx site config..."
# Ubuntu's nginx layout. A variable only so infra/tests/
# test_install_nginx_restore.sh can run install_nginx_site in a scratch tree.
NGINX_ETC=/etc/nginx
# Fit the edge body cap of slide upload / replace (the snippet's two
# `client_max_body_size 8193m;` — the 8 GiB default + 1 MiB — in the copy
# install_nginx_site stages) to WSI_MAX_UPLOAD_BYTES in wsi.env, so raising
# the app's cap is the whole job (QUICKSTART §6.5; the dashboard reads the cap
# from the app) — nobody edits the git-tracked snippet, which would make
# update.sh's checkout abort. Raised to that + 1 MiB of multipart framing, in
# whole MiB; never lowered (the app enforces a smaller cap under the default).
# Unset or malformed: left at 8193m.
apply_upload_body_cap() {
    local conf="$1" bytes="${2//[[:space:]]/}" mib
    [[ -n "$bytes" ]] || return 0
    if [[ ! "$bytes" =~ ^[0-9]{1,15}$ ]]; then
        warn "WSI_MAX_UPLOAD_BYTES='$bytes' is not a byte count; nginx slide upload cap left at 8193m"
        return 0
    fi
    (( bytes > 8 * 1024 * 1024 * 1024 )) || return 0
    mib=$(( (bytes + 1024 * 1024 + 1024 * 1024 - 1) / (1024 * 1024) ))
    sed -i -E "s/^([[:space:]]*client_max_body_size )8193m;/\\1${mib}m;/" "$conf"
    log "  nginx slide upload / replace cap set to ${mib}m (WSI_MAX_UPLOAD_BYTES=$bytes)"
}
# The same for the tour-asset upload location's `client_max_body_size 3m;`
# and TOUR_ASSET_MAX_BYTES (default 2.5 MiB): raised to that + 512 KiB of
# multipart framing (the headroom 3m gives the default), in whole MiB; never
# lowered. Only that location's line is touched — other 3m locations (the
# viewer-settings images) keep theirs. Unset or malformed: left at 3m.
apply_tour_asset_body_cap() {
    local conf="$1" bytes="${2//[[:space:]]/}" mib
    [[ -n "$bytes" ]] || return 0
    if [[ ! "$bytes" =~ ^[0-9]{1,15}$ ]]; then
        warn "TOUR_ASSET_MAX_BYTES='$bytes' is not a byte count; nginx tour-asset upload cap left at 3m"
        return 0
    fi
    (( bytes > 2621440 )) || return 0
    mib=$(( (bytes + 512 * 1024 + 1024 * 1024 - 1) / (1024 * 1024) ))
    sed -i -E '\#^location ~ \^/api/admin/tours/\[0-9\]\+/assets/upload\$ \{$#,/^\}/ s/^([[:space:]]*client_max_body_size )3m;/\1'"${mib}"'m;/' "$conf"
    log "  nginx tour-asset upload cap set to ${mib}m (TOUR_ASSET_MAX_BYTES=$bytes)"
}
# Read here, at the top level, where wsi_env_value and ETC_DIR are in scope;
# install_nginx_site applies it to the snippet it stages.
UPLOAD_CAP_BYTES="$(wsi_env_value WSI_MAX_UPLOAD_BYTES "$ETC_DIR/wsi.env")"
TOUR_ASSET_CAP_BYTES="$(wsi_env_value TOUR_ASSET_MAX_BYTES "$ETC_DIR/wsi.env")"
PERSIST_CACHE_SIZE_FILE="$ETC_DIR/nginx-cache-max-size"
# The operator's TLS snippet (/etc/nginx/snippets/wsi-tls.conf), which the stock
# server block includes. It is theirs: created here when missing and never
# overwritten -- the site file is replaced on every run, so TLS kept in it would
# be silently lost with each update. Only when the OLD site file carried
# certbot's TLS settings (carried, from lib/nginx_tls_carry.py) is the snippet
# written, with those settings (merged into the lines the operator has in it,
# if any), and a snippet that already had other content is first copied to
# wsi-tls.conf.bak-<time> beside it (its path in TLS_SNIPPET_BAK, so a failed
# install can remove it again) and the change announced at the end of the run.
#   install_wsi_tls_snippet SNIPPET_PATH CARRIED_TEXT_OR_EMPTY
# It runs inside the `if ! { ...; }` of install_nginx_site, where `set -e` does
# not apply: every step says so itself, or a full disk would leave an empty
# snippet in place and report success -- and HTTPS switched off.
TLS_SNIPPET_BAK=""
# Whether the config text (a file, or stdin with "-") has an active line that
# makes a server block serve TLS: an ssl_* directive or a listen on 443 / ssl.
tls_text_has_listener() {
    grep -Eq '^[[:space:]]*(ssl_[a-z0-9_]+|listen[^;#]*(443|ssl))' "$1"
}
# Whether the file has any active (not blank, not comment) line.
tls_text_has_content() {
    grep -Eqv '^[[:space:]]*(#|$)' "$1"
}
# Give the staged file the owner and mode of the snippet it replaces (an
# operator may have made it 0664 for a group, or their own); a new one is 0644.
tls_snippet_attrs() {
    local staged="$1" dest="$2"
    if [[ -f "$dest" && ! -L "$dest" ]]; then
        chown --reference="$dest" -- "$staged" && chmod --reference="$dest" -- "$staged"
    else
        chmod 0644 "$staged"
    fi
}
install_wsi_tls_snippet() {
    local dest="$1" carried="$2" staged
    TLS_SNIPPET_BAK=""
    if [[ -n "$carried" ]]; then
        if [[ -s "$dest" ]] && ! printf '%s' "$carried" | cmp -s - "$dest"; then
            TLS_SNIPPET_BAK="$dest.bak-$(date +%Y%m%d-%H%M%S)"
            # (never an earlier copy's name: two runs in one second would
            # otherwise overwrite the first copy, then delete it as "its own")
            while [[ -e "$TLS_SNIPPET_BAK" ]]; do TLS_SNIPPET_BAK="$TLS_SNIPPET_BAK+"; done
            cp -a -- "$dest" "$TLS_SNIPPET_BAK" || return 1
            # A snippet with active lines of its own -- an HSTS header the
            # operator uncommented, `ssl_verify_client`, a listener for another
            # port, or the TLS an earlier run carried -- is not replaced: the
            # carried lines are added to it, and only the lines they replace
            # (the same `listen` address, the same ssl_* directive) go, listed in
            # a comment at the end of the file. Replacing it outright would
            # switch off an access gate or HSTS without a word. A snippet of
            # comments only (the shipped one) is replaced, its copy kept.
            if tls_text_has_content "$dest"; then
                # Written to a file, not captured: the status of a pipeline in a
                # command substitution is its last command's, so a merge that
                # crashed would have been read as an empty result and written.
                staged="$(mktemp "$dest.XXXXXX")" || return 1
                if ! { printf '%s\n' "$carried" \
                       | python3 -I "$REPO_DIR/infra/lib/nginx_tls_carry.py" --merge "$dest" > "$staged" \
                       && [[ -s "$staged" ]] && tls_snippet_attrs "$staged" "$dest"; }; then
                    rm -f -- "$staged"
                    return 1
                fi
                if cmp -s -- "$staged" "$dest"; then
                    rm -f -- "$staged" "$TLS_SNIPPET_BAK"
                    TLS_SNIPPET_BAK=""
                    return 0
                fi
                log "  $dest has settings of its own: merging the TLS settings in (a copy of it as it is now: $TLS_SNIPPET_BAK)"
                mv -f -- "$staged" "$dest" || { rm -f -- "$staged"; return 1; }
                POST_INSTALL_WARNINGS+=("$dest had settings of its own: the TLS settings certbot added to the site file were merged into it (lines they replaced are listed in a comment at its end); the previous file is kept as $TLS_SNIPPET_BAK")
                return 0
            fi
            log "  $dest has no settings of its own: replacing it with the TLS settings carried over (a copy of it as it is now: $TLS_SNIPPET_BAK)"
            POST_INSTALL_WARNINGS+=("$dest was replaced by the TLS settings carried over from the site file; the previous file is kept as $TLS_SNIPPET_BAK")
        fi
        staged="$(mktemp "$dest.XXXXXX")" || return 1
        if ! { printf '%s' "$carried" > "$staged" && tls_snippet_attrs "$staged" "$dest" && mv -f -- "$staged" "$dest"; }; then
            rm -f -- "$staged"
            return 1
        fi
    elif [[ ! -e "$dest" && ! -L "$dest" ]]; then
        install -m 0644 "$REPO_DIR/infra/nginx/wsi-tls.conf" "$dest" || return 1
    fi
    return 0
}
# Install the snippet and the site config, and roll both back if nginx
# rejects them. A config `nginx -t` rejects must not stay installed: the
# running nginx keeps serving its old in-memory config, so nothing looks
# wrong — until the next restart or reboot, when nginx refuses to start and
# takes every site on the host down. Stand-alone install.sh has no other
# rollback (only update.sh re-runs the previous release's install.sh), so:
#   - the site config is generated completely (CSP edit, persisted cache
#     size) in a staging file BEFORE any live file changes, so a failure
#     there leaves nothing half-installed (a new snippet beside an old site
#     config can itself fail nginx -t);
#   - every file this changes — the snippet, the site config, its
#     sites-enabled link, the default site's link and the access-log
#     snippet — is copied aside first
#     (cp -a keeps links as links) and put back exactly, or removed if it did
#     not exist, when installing them or `nginx -t` fails.
# The staged files and the copies aside live in one private directory that
# an EXIT trap removes, however the function ends: a failing sed / cp / head
# here ends install.sh through the ERR trap (on_err exits), which would
# otherwise leave the 0600 copies in /tmp for good, as would a `die` path
# that forgot one.
# EXIT, not RETURN: neither on_err nor die returns. install.sh sets no EXIT
# trap of its own, so this one is simply cleared again on success.
install_nginx_site() {
    local snippet="$NGINX_ETC/snippets/wsi-locations.conf"
    local site="$NGINX_ETC/sites-available/wsi"
    local site_link="$NGINX_ETC/sites-enabled/wsi"
    local default_link="$NGINX_ETC/sites-enabled/default"
    local access_log="$NGINX_ETC/snippets/wsi-access-log.conf"
    local tls_snippet="$NGINX_ETC/snippets/wsi-tls.conf"
    local -a live=("$snippet" "$site" "$site_link" "$default_link" "$access_log" "$tls_snippet")
    local work staged staged_snippet backup persisted_size keys_mb scaled_mb i p
    local tls_carried="" tls_rc=0

    # The old site file's TLS settings, before anything is replaced: this run
    # puts the stock site in, which has none (they live in wsi-tls.conf).
    # certbot's are carried into that snippet; TLS that is not certbot's -- an
    # active ssl_certificate nobody can attribute -- stops the run, because
    # replacing the file would switch HTTPS off with no word, and a browser
    # that has seen HSTS then cannot reach the site at all.
    if [[ -f "$site" ]]; then
        tls_carried="$(python3 -I "$REPO_DIR/infra/lib/nginx_tls_carry.py" "$site")" || tls_rc=$?
        # Whenever the file holds TLS -- carried, discarded on request, or the
        # reason this run stops -- a copy is kept beside it before anything else:
        # if this run stops, update.sh from an OLDER release (the one that
        # started it) rolls back by re-running the previous install.sh, which
        # replaces the site file without asking, so the stop alone would not
        # have kept it. A run on an already migrated site has no TLS in the file
        # and leaves the copy of the original alone -- and so does one that
        # carries only certbot's redirect (`certbot enhance --redirect` on a
        # migrated host: no listener in the file) when there is a copy already,
        # which holds the listener and certificates the redirect-only file lacks.
        if [[ "$tls_rc" == 3 ]] \
           || { [[ -n "$tls_carried" ]] \
                && { printf '%s' "$tls_carried" | tls_text_has_listener /dev/stdin \
                     || [[ ! -e "$site.previous" ]]; }; }; then
            cp -p -- "$site" "$site.previous" \
                || die "Could not keep a copy of $site as $site.previous before touching it."
            log "  The previous $site is kept as $site.previous"
        fi
        case "$tls_rc" in
            0) ;;
            3)
                if [[ "${WSI_NGINX_DISCARD_SITE_TLS:-}" == 1 ]]; then
                    warn "$site has TLS settings that are not certbot's; WSI_NGINX_DISCARD_SITE_TLS=1, so they are replaced and HTTPS is OFF until you restore it."
                    tls_carried=""
                else
                    die "$site has TLS settings (the lines listed above) that this installer cannot carry over -- replacing the file would switch HTTPS off. Move the settings of THIS site into $tls_snippet (the stock server block includes it and updates never overwrite it; it takes effect when the new site file is installed, so run this again straight away -- until then the running site does not include it), and keep any other server block (another domain, another port) in a site file of its own. To replace the file anyway, run with WSI_NGINX_DISCARD_SITE_TLS=1."
                fi ;;
            *) die "Could not read $site to look for TLS settings (nginx_tls_carry.py exited $tls_rc)." ;;
        esac
        if [[ -n "$tls_carried" ]]; then
            # A snippet that is a link is the operator's arrangement (one file
            # for several hosts, a managed path): it is not rewritten through,
            # nor replaced by a regular file. Fine if it holds the carried lines
            # already; otherwise stop, before anything is changed.
            if [[ -L "$tls_snippet" ]] \
               && ! { printf '%s\n' "$tls_carried" \
                      | python3 -I "$REPO_DIR/infra/lib/nginx_tls_carry.py" --merge "$tls_snippet" 2>/dev/null \
                      | cmp -s - "$tls_snippet"; }; then
                die "$tls_snippet is a symlink, and $site holds TLS settings (certbot's) that are not in the file it points to. install.sh does not write through a link. Add the lines below to the file it points to (or replace the link by a regular file), then run this again; the site file is kept as $site.previous.
$tls_carried"
            fi
            log "  Carrying the TLS settings in $site over to $tls_snippet"
        fi
    fi

    work="$(mktemp -d)"
    # shellcheck disable=SC2064  # expanded now: $work is local to this function
    trap "rm -rf -- $(printf '%q' "$work")" EXIT
    staged="$work/site" staged_snippet="$work/snippet" backup="$work/backup"
    mkdir -- "$backup"
    # When the LAN-direct probe is enabled, add the LAN host to the CSP
    # connect-src so the SPA's cross-origin reachability fetch isn't blocked.
    # Derived from the canonical wsi.conf by a single targeted edit of the
    # connect-src token (same transform as `make nginx-config`), so there is
    # no separate template to drift; unset -> the stock conf verbatim.
    if [[ -n "$WSI_LAN_HOST" ]]; then
        log "  Adding https://$WSI_LAN_HOST to nginx CSP connect-src (LAN-direct probe)"
        sed "s#connect-src [']self[']#connect-src 'self' https://$WSI_LAN_HOST#" \
            "$REPO_DIR/infra/nginx/wsi.conf" > "$staged"
        if ! grep -q "connect-src 'self' https://$WSI_LAN_HOST" "$staged"; then
            die "Failed to inject WSI_LAN_HOST into the nginx CSP connect-src token."
        fi
    else
        cp -- "$REPO_DIR/infra/nginx/wsi.conf" "$staged"
    fi
    # Re-apply a persisted nginx cache max_size override (chosen via
    # infra/nginx-cache.sh). It is stored OUTSIDE the git tree precisely so
    # it survives update.sh's `git checkout`; re-sed it onto the fresh config
    # so the operator's choice isn't reverted on every install/update. The
    # tile zone's keys_zone goes with it, sized by the same function
    # nginx-cache.sh uses (lib/wsi-common.sh): without it a larger max_size
    # adds no capacity, as nginx evicts once the zone's index is full.
    # That function caps the zone at a quarter of RAM: nginx maps all of it
    # at `nginx -t`, so a large persisted size (500g -> 4000m) would
    # otherwise fail nginx -t on a small VM on every update. The cap
    # is named here and in the final summary, with the size that caused it.
    if [[ -f "$PERSIST_CACHE_SIZE_FILE" ]]; then
        persisted_size="$(head -n1 "$PERSIST_CACHE_SIZE_FILE" | tr -d '[:space:]')"
        if [[ "$persisted_size" =~ ^[1-9][0-9]*[kKmMgG]?$ ]] \
           && keys_mb="$(wsi_nginx_keys_zone_mb "$persisted_size")"; then
            sed -i -E -e "s/max_size=[0-9]+[kKmMgG]?/max_size=${persisted_size}/" \
                -e "s/(keys_zone=wsi_dzi_cache:)[0-9]+[kKmMgG]?/\\1${keys_mb}m/" "$staged"
            log "  Re-applied persisted nginx cache max_size=${persisted_size} (keys zone ${keys_mb}m)"
            scaled_mb="$(wsi_nginx_keys_zone_scaled_mb "$persisted_size")"
            if (( scaled_mb > keys_mb )); then
                warn "nginx cache max_size=${persisted_size} (from $PERSIST_CACHE_SIZE_FILE) calls for a ${scaled_mb}m keys zone, more than a quarter of this host's RAM; capped at ${keys_mb}m, so the cache evicts at ~$(( keys_mb * 8 ))k files before reaching max_size. Lower it with: sudo bash infra/nginx-cache.sh"
                POST_INSTALL_WARNINGS+=("nginx cache keys zone capped at ${keys_mb}m by RAM (persisted max_size=${persisted_size} in $PERSIST_CACHE_SIZE_FILE wants ${scaled_mb}m) — lower max_size with infra/nginx-cache.sh")
            fi
        else
            warn "Ignoring malformed $PERSIST_CACHE_SIZE_FILE (expected e.g. 200g)"
        fi
    fi
    # The snippet is staged too, so the upload cap from wsi.env lands in the
    # copy that `nginx -t` checks below and is rolled back with it.
    cp -- "$REPO_DIR/infra/nginx/wsi-locations.conf" "$staged_snippet"
    apply_upload_body_cap "$staged_snippet" "$UPLOAD_CAP_BYTES"
    apply_tour_asset_body_cap "$staged_snippet" "$TOUR_ASSET_CAP_BYTES"

    for i in "${!live[@]}"; do
        p="${live[$i]}"
        if [[ -e "$p" || -L "$p" ]]; then cp -a -- "$p" "$backup/$i"; fi
    done
    # The snippet goes in with the site config: wsi.conf pulls the location
    # blocks in via `include /etc/nginx/snippets/wsi-locations.conf`, which
    # `nginx -t` must be able to resolve. So does the server block's access
    # log, `include /etc/nginx/snippets/wsi-access-log.conf` — but that one
    # is the operator's (`access_log off;`, their own format or syslog
    # target in place of the masked combined log), so it is installed only
    # when nothing is there, never overwritten; a link counts as there.
    if ! { install -d -m 0755 "$NGINX_ETC/snippets" \
           && install -m 0644 "$staged_snippet" "$snippet" \
           && { [[ -e "$access_log" || -L "$access_log" ]] \
                || install -m 0644 "$REPO_DIR/infra/nginx/wsi-access-log.conf" "$access_log"; } \
           && install_wsi_tls_snippet "$tls_snippet" "$tls_carried" \
           && install -m 0644 "$staged" "$site" \
           && ln -sf "$site" "$site_link" \
           && rm -f "$default_link"; } \
       || ! nginx -t; then
        # A restore that itself fails keeps the copies aside (the trap would
        # delete the only copy of the previous files) and says where.
        for i in "${!live[@]}"; do
            p="${live[$i]}"
            if ! { rm -f -- "$p" \
                   && { [[ ! -e "$backup/$i" && ! -L "$backup/$i" ]] || cp -a -- "$backup/$i" "$p"; }; }; then
                trap - EXIT
                die "Could not put $p back after the new nginx config failed; the previous files are kept in $backup (named by their position in: ${live[*]}, from 0)."
            fi
        done
        # The copy the snippet's install made of what it replaced is the file
        # just put back: nothing left to keep.
        [[ -z "${TLS_SNIPPET_BAK:-}" ]] || rm -f -- "$TLS_SNIPPET_BAK"
        if nginx -t >/dev/null 2>&1; then
            die "The new nginx config was rejected (see above). The previous nginx files are back in place and pass nginx -t; nginx was not reloaded and keeps serving its current config."
        fi
        die "The new nginx config was rejected (see above). The previous nginx files are back in place, but nginx -t still fails, so the cause is elsewhere in the nginx config: fix it before nginx is next restarted."
    fi
    rm -rf -- "$work"
    trap - EXIT
}
install_nginx_site
# `reload` fails with exit 5 ("unit not active") if nginx was stopped
# manually before this re-run, and `set -e` would abort the whole
# install. Mirror update.sh's defensive branch: reload when active,
# start when not.
if systemctl is-active --quiet nginx; then
    systemctl reload nginx
else
    systemctl start nginx
fi

# ---- 9b. Post-install preflight ------------------------------------
# Fail loud (and early) if the apt step somehow left required tools out
# of the install path, rather than discovering it via a confusing
# uvicorn / OpenSlide ImportError 30 seconds later.
log "Verifying required tools are present ..."
command -v node >/dev/null 2>&1 \
    || die "node is missing from PATH after install"
command -v npm >/dev/null 2>&1 \
    || die "npm is missing from PATH after install"
# dpkg-query is the structured form that returns a single "<status>" line;
# parsing `dpkg -l` output is fragile across locales / dpkg versions and
# a regex on `^ii` could miss `iU`-state half-installs. Try the
# unversioned name first, then the most common SONAME-bumped successors.
_openslide_installed() {
    local pkg
    for pkg in libopenslide0 libopenslide1; do
        local status
        status=$(dpkg-query -W -f='${Status}' "$pkg" 2>/dev/null || true)
        if [[ "$status" == *"install ok installed"* ]]; then
            return 0
        fi
    done
    return 1
}
if ! _openslide_installed; then
    die "libopenslide is not installed (no libopenslide0 / libopenslide1 found by dpkg-query)"
fi
log "Preflight OK: node $(node -v), npm $(npm -v), libopenslide present."

# ---- 10. Enable + start the services, then health-check --------------
# Cache statistics timer: always enable/start unconditionally (idempotent).
log "Enabling wsi-cache-stat.timer ..."
systemctl enable --now wsi-cache-stat.timer

# mDNS path watcher: always enable/start unconditionally (idempotent).
log "Enabling wsi-avahi-reload.path ..."
systemctl enable --now wsi-avahi-reload.path

# Restart trigger watcher: drives restore / reset apply restarts
# without sudo. Always enable/start unconditionally (idempotent).
log "Enabling wsi-restart.path ..."
systemctl enable --now wsi-restart.path
# Run one snapshot immediately so the dashboard shows data right away.
systemctl start wsi-cache-stat.service || warn "wsi-cache-stat.service first run failed; stats will appear after next timer tick."

# update.sh wraps install.sh and runs its own restart + health-check in
# its Step 4-5. When invoked that way it sets WSI_SKIP_RESTART_HEALTH=1
# so this phase doesn't spend ~90s only for update.sh to spend another ~45s
# right after. Stand-alone runs keep the default (=0) and do the full
# bring-up here so a fresh install completes end-to-end with one script.
if [[ "${WSI_SKIP_RESTART_HEALTH:-0}" != "1" ]]; then
    if systemctl is-enabled --quiet wsi-app.service 2>/dev/null; then
        log "Restarting wsi-app.service to pick up new code ..."
    else
        log "Enabling and starting wsi-app.service ..."
        systemctl enable wsi-app.service
    fi
    # Always (re)start: covers both fresh installs and re-runs against an
    # already-enabled-but-stopped unit. A "skip when enabled" branch would
    # leave the service down whenever an install.sh re-run came
    # after a manual `systemctl stop`, causing nginx 502 with no obvious
    # cause.
    systemctl restart wsi-app.service

    # Health-check budget in wall-clock seconds. A
    # `for i in $(seq 1 N); ... sleep 1` loop only bounds the *number*
    # of iterations — each iteration is sleep 1 + curl --max-time 2, so
    # a hanging /api/health blows the wall-clock budget up to ~3x.
    HEALTH_TIMEOUT="${WSI_HEALTH_TIMEOUT:-60}"
    log "Waiting for backend to become healthy (timeout ${HEALTH_TIMEOUT}s) ..."
    HEALTH_OK=0
    HEALTH_DEADLINE=$(( $(date +%s) + HEALTH_TIMEOUT ))
    while [[ $(date +%s) -lt $HEALTH_DEADLINE ]]; do
        # Detect a unit that has already failed (e.g. import error in
        # main.py) to avoid sitting here for the full timeout only to give up.
        if systemctl is-failed --quiet wsi-app.service; then
            warn "wsi-app.service has entered failed state. Recent logs:"
            journalctl -u wsi-app.service -n 40 --no-pager || true
            die "Service failed during startup."
        fi
        if curl -fsS --max-time 2 http://127.0.0.1:8000/api/health >/dev/null 2>&1; then
            HEALTH_OK=1
            break
        fi
        sleep 1
    done
    if [[ $HEALTH_OK -eq 1 ]]; then
        log "Backend is healthy (http://127.0.0.1:8000/api/health)."
    else
        warn "Backend did not respond within ${HEALTH_TIMEOUT}s."
        warn "Check logs with: sudo journalctl -u wsi-app -n 80 --no-pager"
        HEALTH_TIMEOUTS+=("wsi-app did not answer http://127.0.0.1:8000/api/health within ${HEALTH_TIMEOUT}s (sudo journalctl -u wsi-app -n 80 --no-pager)")
    fi

    # Live-sync coordinator: a separate single-worker uvicorn (port 8001).
    if systemctl is-enabled --quiet wsi-sync.service 2>/dev/null; then
        log "Restarting wsi-sync.service to pick up new code ..."
    else
        log "Enabling and starting wsi-sync.service ..."
        systemctl enable wsi-sync.service
    fi
    systemctl restart wsi-sync.service

    SYNC_HEALTH_TIMEOUT="${WSI_SYNC_HEALTH_TIMEOUT:-30}"
    SYNC_OK=0
    SYNC_DEADLINE=$(( $(date +%s) + SYNC_HEALTH_TIMEOUT ))
    while [[ $(date +%s) -lt $SYNC_DEADLINE ]]; do
        if systemctl is-failed --quiet wsi-sync.service; then
            warn "wsi-sync.service has entered failed state. Recent logs:"
            journalctl -u wsi-sync.service -n 40 --no-pager || true
            die "Sync service failed during startup."
        fi
        if curl -fsS --max-time 2 http://127.0.0.1:8001/api/health >/dev/null 2>&1; then
            SYNC_OK=1
            break
        fi
        sleep 1
    done
    if [[ $SYNC_OK -eq 1 ]]; then
        log "Sync coordinator is healthy (http://127.0.0.1:8001/api/health)."
    else
        warn "wsi-sync did not respond within ${SYNC_HEALTH_TIMEOUT}s."
        warn "Check logs with: sudo journalctl -u wsi-sync -n 80 --no-pager"
        HEALTH_TIMEOUTS+=("wsi-sync did not answer http://127.0.0.1:8001/api/health within ${SYNC_HEALTH_TIMEOUT}s (sudo journalctl -u wsi-sync -n 80 --no-pager)")
    fi
else
    log "WSI_SKIP_RESTART_HEALTH=1: skipping service restart and health-check (caller will handle)."
    # Still enable the units so a subsequent boot brings them up; the
    # caller is responsible for the actual restart + health-probe.
    systemctl enable wsi-app.service >/dev/null 2>&1 || true
    systemctl enable wsi-sync.service >/dev/null 2>&1 || true
fi

# ---- 10b. UFW hint --------------------------------------------------
# If UFW is active but hasn't been told to allow nginx, remote hosts will
# not be able to reach http://<host>/ even though nginx is bound to all
# interfaces. Detect this and print an actionable hint; never touch the
# firewall automatically (could lock out SSH for the unwary).
if command -v ufw >/dev/null 2>&1 \
   && ufw status 2>/dev/null | grep -qi '^Status: active'; then
    if ! ufw status 2>/dev/null | grep -Eq '(^|[[:space:]])(80/tcp|Nginx Full|Nginx HTTP)([[:space:]]|$)'; then
        warn "UFW is active but port 80 (HTTP) is not allowed."
        warn "Remote hosts cannot reach this server until you run:"
        warn "    sudo ufw allow 'Nginx Full'"
    fi
fi

# ---- 11. Summary ----------------------------------------------------
INSTALL_HEADLINE='Installation complete.'
if (( ${#HEALTH_TIMEOUTS[@]} > 0 )); then
    INSTALL_HEADLINE='Installed, but not serving yet: see the errors at the end.'
fi
cat <<EOS

================================================================
 ${INSTALL_HEADLINE}
EOS

if [[ $FRESH_INSTALL -eq 1 ]]; then
cat <<EOS

 Admin credentials (initial):
   URL:       http://<host>/admin
   File:      ${ADMIN_PW_FILE} (0600 root:root)
   Read with: sudo cat ${ADMIN_PW_FILE}

 Change the password at /admin/account after the first login.
 The file can then be deleted: sudo rm ${ADMIN_PW_FILE}
EOS
fi

cat <<EOS

 Smoke test:
     curl -fsSL http://127.0.0.1:8000/api/health
     curl -fsSL http://127.0.0.1/api/health     # via nginx

 Logs:
     sudo journalctl -u wsi-app -f
     sudo journalctl -u wsi-sync -f
     sudo tail -f /var/log/nginx/error.log

 See QUICKSTART_FOR_UBUNTU.md for the full manual procedure.
================================================================
EOS

# Re-emit any non-fatal warnings collected during install. They were
# already printed in-context (as `warn`) but surface them again at the
# very end so they are not lost in the scrollback after a long run.
if (( ${#POST_INSTALL_WARNINGS[@]} > 0 )); then
    printf '\n\033[1;33m[warn]\033[0m install finished with %d warning(s):\n' \
        "${#POST_INSTALL_WARNINGS[@]}" >&2
    for w in "${POST_INSTALL_WARNINGS[@]}"; do
        printf '\033[1;33m[warn]\033[0m   - %s\n' "$w" >&2
    done
fi

if (( ${#HEALTH_TIMEOUTS[@]} > 0 )); then
    printf '\n\033[1;31m[error]\033[0m installed, but not serving:\n' >&2
    for w in "${HEALTH_TIMEOUTS[@]}"; do
        printf '\033[1;31m[error]\033[0m   - %s\n' "$w" >&2
    done
    printf '\033[1;31m[error]\033[0m Raise WSI_HEALTH_TIMEOUT / WSI_SYNC_HEALTH_TIMEOUT if it is only slow to start.\n' >&2
    exit 1
fi
