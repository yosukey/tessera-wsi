#!/usr/bin/env bash
# =====================================================================
# Tessera - copy another Tessera server's content onto this one
#
# Run on the server that RECEIVES the copy (a backup server, or the new
# machine of a move), from its clone of the repository:
#   sudo bash infra/sync-from.sh [options] [user@]source-host
#
# Modes:
#   (default)    Standby sync. Takes the source's slides, tours, attachments,
#                WSI files, thumbnails, public-page settings and admin
#                accounts; keeps this server's own network settings (IP
#                filter, mDNS, Google Analytics), sign-in settings and app
#                accounts, access statistics, audit trail and error log.
#                When admin accounts with two-factor sign-in come over, the
#                source's WSI_MFA_KEY is copied into /etc/wsi/wsi.env too
#                (their secrets open only under it).
#                Both servers must be on the same schema version.
#   --all        Migration. This server's database becomes a copy of the
#                source's, every table included, and ANALYTICS_HASH_SALT and
#                WSI_MFA_KEY are copied into /etc/wsi/wsi.env (when the source lets this read
#                it; otherwise the command to copy it by hand is printed).
#                This server may run a newer release than the source. Stop
#                the source's services first for the final run (see
#                docs/operations.md).
#   --rollback   Put back the database this server had before the last sync
#                (kept as <db>.pre-sync), and the wsi.env that sync changed.
#                No ssh, no source needed.
#
# Every swap (and a rollback) signs everyone out of this server, as a restore
# does, with the app's own sign-out (wsi_app/restore_apply.py).
#
# Options:
#   --dry-run          Show what a sync would change and transfer; change
#                      nothing here (the mirror directory aside).
#   --yes, -y          Do not ask before stopping the services to apply
#                      (needed when there is no terminal, e.g. from cron).
#   --bwlimit=RATE     Cap the transfer (rsync syntax, e.g. 20M = 20 MB/s).
#
# Environment overrides:
#   SSH_AS_USER=<name>     The local user ssh runs as (default: SUDO_USER, so
#                          your keys and ~/.ssh/config apply; an ssh-agent
#                          does not reach through sudo, so a key passphrase
#                          is asked for, once per run).
#   WSI_SOURCE_DB=<path>   The source's SQLite file (default
#                          /var/lib/wsi/wsi.db).
#   WSI_SOURCE_DATA_DIR=<path>
#                          The source's DATA_DIR (default /var/lib/wsi).
#   WSI_SOURCE_ENV_FILE=<path>
#                          The source's wsi.env, read by --all, and by a
#                          standby sync for WSI_MFA_KEY when accounts with
#                          two-factor sign-in come over (default
#                          /etc/wsi/wsi.env).
#   WSI_REPLICA_DIR=<path> Where this server keeps its mirror of the
#                          source's files (default /var/lib/wsi-replica).
#                          Keep it on the same filesystem as DATA_DIR: files
#                          are then hard-linked into place instead of copied.
#   WSI_HEALTH_TIMEOUT=<s> Seconds to wait for wsi-app after the apply (30).
#
# What the source needs: sshd reachable from here, and for the ssh user
#   <user> ALL=(wsi) NOPASSWD: /usr/bin/rsync, /usr/bin/python3
# in sudoers (visudo -f /etc/sudoers.d/wsi-sync): every command this runs
# there runs as the service user, never as root. --all (and a standby sync
# that takes over accounts with two-factor sign-in, for WSI_MFA_KEY)
# additionally reads /etc/wsi/wsi.env with `sudo -n python3` as root when
# allowed to.
#
# How it works. The source's database is snapshotted (SQLite's online
# backup, safe while it runs) and streamed here; the files that snapshot
# refers to are rsynced into the mirror under WSI_REPLICA_DIR (only what
# changed travels; an interrupted transfer resumes). If a slide's files
# (or a tour or an attachment) changed on the source meanwhile, the round is
# repeated. Only then are the services stopped (for seconds): the database is
# kept as <db>.pre-sync, the files are put in place, the content tables are
# swapped in one transaction (or, with --all, the whole database is copied
# in), everyone is signed out, and the services are started again and
# health-checked. Files and attachments only the previous database refers to
# are kept until the next sync, so --rollback finds them. Should the swap fail after
# files were put in place, every slide moves to a fresh tile generation, so
# the edge cache never mixes tiles of an old and a new file.
#
# Root never writes below DATA_DIR (the service user can plant symlinks
# there): the mirror is root's own directory, and everything under DATA_DIR
# is done as the service user.
# =====================================================================
set -Eeuo pipefail
umask 022

# ---- Paths -----------------------------------------------------------
REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# Root runs this script, and sources and executes more files from this tree
# (lib/wsi-common.sh below, install.sh, replica_db.py). So, like install.sh and
# set-workers.sh, it is run from the operator's clone and refuses a tree that
# an account other than root or the operator could have rewritten -- above all
# the deployed copy under /opt/wsi, which belongs to the service user and has
# had pip / npm run in it. Checked before anything is read from the tree.
# Symlinks are resolved first, so a link into /opt/wsi is caught too.
_guard_refuse() {
    printf '\033[31mError: refusing to run %s: %s.\033[0m\n' "${BASH_SOURCE[0]}" "$1" >&2
    printf 'It runs code from %s as root, so only root or you may be able\n' "$REPO_DIR" >&2
    printf 'to change that tree. Run it from your clone instead:\n' >&2
    printf '  cd ~/wsi && sudo bash infra/%s\n' "$(basename -- "${BASH_SOURCE[0]}")" >&2
    exit 1
}
_guard_repo_real="$(realpath -- "$REPO_DIR")"
for _guard_home in /opt/wsi ${APP_HOME:+"$APP_HOME"}; do
    _guard_home="$(realpath -m -- "$_guard_home")"
    case "$_guard_repo_real/" in
        "$_guard_home"/*) _guard_refuse "it is inside $_guard_home, the service user's deployed copy" ;;
    esac
done
# The operator: whoever ran sudo, else (a root shell from `sudo su -`, which
# drops SUDO_UID) the account the login session belongs to.
_guard_op="${SUDO_UID:-}"
if [[ -z "$_guard_op" ]]; then _guard_op="$(id -u -- "$(logname 2>/dev/null)" 2>/dev/null || true)"; fi
for _guard_p in "$REPO_DIR" "$REPO_DIR/infra" "$REPO_DIR/infra/lib" "$REPO_DIR/infra/lib/wsi-common.sh" \
                "$REPO_DIR/infra/lib/replica_db.py" "$(realpath -- "${BASH_SOURCE[0]}")"; do
    read -r _guard_uid _guard_user _guard_mode < <(stat -L -c '%u %U %a' -- "$_guard_p" 2>/dev/null) \
        || _guard_refuse "$_guard_p is missing"
    if [[ "$_guard_uid" != 0 && "$_guard_uid" != "$EUID" && "$_guard_uid" != "$_guard_op" ]]; then
        _guard_refuse "$_guard_p is owned by $_guard_user, neither root nor you"
    fi
    if (( 8#$_guard_mode & 8#002 )); then
        _guard_refuse "$_guard_p is writable by every user"
    fi
done
unset _guard_repo_real _guard_home _guard_op _guard_p _guard_uid _guard_user _guard_mode
HELPER="$REPO_DIR/infra/lib/replica_db.py"
# shellcheck source=lib/wsi-common.sh
. "$REPO_DIR/infra/lib/wsi-common.sh"
# Fixed by install.sh, like in update.sh; overridable (with WSI_ENV_FILE and
# DATA_DIR) only so infra/tests/test_sync_from.sh can run against a scratch
# tree.
APP_USER="${APP_USER:-wsi}"
APP_GROUP="${APP_GROUP:-$APP_USER}"
# The installed app, for its stdlib-only sign-out (restore_apply.py).
APP_HOME="${APP_HOME:-/opt/wsi}"
RESTORE_APPLY_PY="$APP_HOME/services/app/wsi_app/restore_apply.py"
WSI_ENV_FILE="${WSI_ENV_FILE:-/etc/wsi/wsi.env}"
SERVICES=(wsi-app.service wsi-sync.service)
HEALTH_URL_LOCAL="http://127.0.0.1:8000/api/health"
DEFAULT_DB_PATH="/var/lib/wsi/wsi.db"
SRC_DB="${WSI_SOURCE_DB:-/var/lib/wsi/wsi.db}"
SRC_DATA_DIR="${WSI_SOURCE_DATA_DIR:-/var/lib/wsi}"
SRC_ENV_FILE="${WSI_SOURCE_ENV_FILE:-/etc/wsi/wsi.env}"
STAGE="${WSI_REPLICA_DIR:-/var/lib/wsi-replica}"
# One sync or rollback at a time on this server, whatever WSI_REPLICA_DIR each
# is given: in /run, which only root can write (overridable for the tests).
LOCK_FILE="${WSI_SYNC_LOCK:-/run/wsi-sync.lock}"
MIRROR="$STAGE/mirror"
MAX_ROUNDS=3

# ---- Pretty progress output -----------------------------------------
if [[ -t 1 ]]; then
    C_BLUE=$'\033[1;34m'; C_GREEN=$'\033[1;32m'; C_YELLOW=$'\033[1;33m'
    C_RED=$'\033[1;31m'; C_DIM=$'\033[2m'; C_BOLD=$'\033[1m'; C_OFF=$'\033[0m'
else
    C_BLUE=""; C_GREEN=""; C_YELLOW=""; C_RED=""; C_DIM=""; C_BOLD=""; C_OFF=""
fi
TOTAL_STEPS=5
CURRENT_STEP=0
START_TS=$(date +%s)
step() {
    CURRENT_STEP=$((CURRENT_STEP + 1))
    printf '\n%s[%d/%d]%s %s%s%s  %s(+%ss)%s\n' \
        "$C_BLUE" "$CURRENT_STEP" "$TOTAL_STEPS" "$C_OFF" "$C_BOLD" "$*" "$C_OFF" \
        "$C_DIM" "$(( $(date +%s) - START_TS ))" "$C_OFF"
}
sub()  { printf '  %s->%s %s\n' "$C_DIM" "$C_OFF" "$*"; }
ok()   { printf '  %s✓%s %s\n'  "$C_GREEN" "$C_OFF" "$*"; }
warn() { printf '  %s!%s %s\n'  "$C_YELLOW" "$C_OFF" "$*"; }
die()  {
    printf '\n%s[error]%s %s\n' "$C_RED" "$C_OFF" "$*" >&2 || true
    [[ "$BASHPID" == "$$" ]] || exit 1
    recover || true
    exit 1
}

usage() {
    sed -n '/^# Run on/,/^# =====/p' "${BASH_SOURCE[0]}" | sed -e '$d' -e 's/^# \{0,1\}//'
}

# ---- Arguments ---------------------------------------------------------
MODE="content"
# Admin accounts with two-factor sign-in in the snapshot (standby sync only).
MFA_ACCOUNTS=0
DRY_RUN=0
ASSUME_YES=0
BWLIMIT=""
SOURCE=""
while [[ $# -gt 0 ]]; do
    case "$1" in
        --all|--rollback)
                     want="${1#--}"
                     [[ "$MODE" == "content" || "$MODE" == "$want" ]] \
                         || { printf -- '--all and --rollback cannot be combined.\n' >&2; exit 2; }
                     MODE="$want" ;;
        --dry-run)   DRY_RUN=1 ;;
        --yes|-y)    ASSUME_YES=1 ;;
        --bwlimit=*) BWLIMIT="${1#--bwlimit=}" ;;
        -h|--help)   usage; exit 0 ;;
        -*)          printf 'Unknown option: %s (see --help)\n' "$1" >&2; exit 2 ;;
        *)           [[ -z "$SOURCE" ]] || { printf 'Only one source host.\n' >&2; exit 2; }
                     SOURCE="$1" ;;
    esac
    shift
done

# ---- Recovery ------------------------------------------------------------
# Once the apply has stopped the services, every exit must start them again:
# a failed sync leaves this server serving what it served before (the swap is
# one transaction, so a failure leaves the database as it was), never down.
# Two exceptions, both handled by recover() before it starts anything:
#  - a database swapped in (sync or rollback) is served only once everyone is
#    signed out (SWAP_STARTED / SIGNED_OUT); if that cannot be done, the services
#    stay stopped, since they would honour logins revoked since;
#  - files put in place under a database that does not describe them (a swap
#    that failed after install-files, a rollback) need every slide on a fresh
#    tile generation first (BUMP_NEEDED).
SERVICES_STOPPED=0
WAS_ACTIVE=()
RECOVERING=0
SSH_CTL_DIR=""
# SWAP_STARTED is set before the swap helper runs: bash runs a trap for a
# SIGHUP / SIGTERM only once that helper has returned, so a swap that
# committed can look like one that never happened. recover() treats a started
# swap as landed (signing everyone out when it did not is harmless).
SWAP_STARTED=0
SIGNED_OUT=0
SIGN_OUT_PREV=""
SIGN_OUT_DEFERRED=0
BUMP_NEEDED=0

start_services() {
    local u rc=0
    for u in "${WAS_ACTIVE[@]}"; do
        systemctl start "$u" || { warn "Could not start $u; check journalctl -u $u."; rc=1; }
    done
    SERVICES_STOPPED=0
    return "$rc"
}

recover() {
    [[ "$RECOVERING" == "0" ]] || return 0
    RECOVERING=1
    # A sync's swap may have landed: the copy taken just before it is then the
    # database to go back to, not the older rollback point.
    if [[ "$MODE" != "rollback" && "$SWAP_STARTED" == "1" && -e "$PRE_SYNC_DB.new" ]]; then
        if promote_pre_sync; then
            warn "The database from before this sync is kept as $PRE_SYNC_DB (for --rollback)."
        fi
    fi
    if [[ "$SWAP_STARTED" == "1" && "$SIGNED_OUT" == "0" ]]; then
        if try_sign_out; then
            warn "Signed everyone out of this server (the swapped-in database may be in place)."
        elif [[ "$SERVICES_STOPPED" == "1" ]]; then
            SERVICES_STOPPED=0
            warn "Could not sign out the sessions from before the swap, so wsi-app / wsi-sync"
            warn "stay STOPPED: they would honour logins revoked since. Fix the cause (above),"
            warn "then $(go_back_hint) (or start them yourself)."
        fi
    fi
    # Some files may hold bytes the database does not describe (a WSI
    # replaced under the same name): give every slide a generation no cached
    # tile was served under.
    if [[ "$BUMP_NEEDED" == "1" ]]; then
        if app_helper bump-generations "$DB_PATH"; then
            BUMP_NEEDED=0
            warn "Files were already put in place, so every slide moved to a fresh tile generation."
        else
            warn "Could not move the slides to fresh tile generations; clear the tile cache"
            warn "(sudo bash infra/nginx-cache.sh) before relying on this server."
        fi
    fi
    # A copy that never became the rollback point (no swap was attempted).
    if [[ "$SWAP_STARTED" == "0" && -n "${PRE_SYNC_DB:-}" && -e "$PRE_SYNC_DB.new" ]]; then
        as_app_user rm -f -- "$PRE_SYNC_DB.new" || true
    fi
    if [[ "$SERVICES_STOPPED" == "1" ]]; then
        warn "Starting the services again ..."
        start_services || true
    fi
}

close_ssh() {
    [[ -n "$SSH_CTL_DIR" ]] || return 0
    "${SSH_BASE[@]}" -O exit "$SOURCE" >/dev/null 2>&1 || true
    rm -rf -- "$SSH_CTL_DIR" 2>/dev/null || true
    SSH_CTL_DIR=""
}

# With set -E the ERR trap also fires inside subshells (as_app_user's), and a
# signal reaches them too: there, only exit; the main shell handles it, once.
on_error() {
    local rc=$? lineno=$1
    [[ "$BASHPID" == "$$" ]] || exit "$rc"
    printf '\n%s[error]%s Sync failed at step %d/%d (line %d, exit %d).\n' \
        "$C_RED" "$C_OFF" "$CURRENT_STEP" "$TOTAL_STEPS" "$lineno" "$rc" >&2 || true
    recover || true
    exit "$rc"
}
on_signal() {
    [[ "$BASHPID" == "$$" ]] || exit "$2"
    printf '\n%s[error]%s Interrupted by SIG%s.\n' "$C_RED" "$C_OFF" "$1" >&2 || true
    recover || true
    exit "$2"
}
trap 'on_error ${LINENO}' ERR
trap 'on_signal INT 130' INT
trap 'on_signal HUP 129' HUP
trap 'on_signal TERM 143' TERM
trap 'close_ssh' EXIT

# ---- Helpers ---------------------------------------------------------------
# Run as the service user, from / (see update.sh): everything that opens the
# live database or writes under DATA_DIR goes through this.
as_app_user() { (cd / && sudo -u "$APP_USER" -- "$@"); }
# The helper runs from stdin, so the service user need not be able to read
# this clone (typically under the operator's home).
app_helper() { as_app_user python3 -I - "$@" < "$HELPER"; }
root_helper() { python3 -I "$HELPER" "$@"; }


wait_backend_healthy() {
    local deadline=$(( $(date +%s) + $1 ))
    while [[ $(date +%s) -lt $deadline ]]; do
        systemctl is-failed --quiet wsi-app.service && return 1
        curl -fsS --max-time 2 "$HEALTH_URL_LOCAL" >/dev/null 2>&1 && return 0
        printf '    %s.%s' "$C_DIM" "$C_OFF"
        sleep 1
    done
    return 2
}

stop_services() {
    local u
    WAS_ACTIVE=()
    for u in "${SERVICES[@]}"; do
        systemctl is-active --quiet "$u" && WAS_ACTIVE+=("$u")
    done
    SERVICES_STOPPED=1
    if [[ ${#WAS_ACTIVE[@]} -gt 0 ]]; then
        systemctl stop "${WAS_ACTIVE[@]}"
        ok "Stopped ${WAS_ACTIVE[*]}."
    else
        sub "wsi-app / wsi-sync are not running; they stay stopped."
    fi
}

# Start what stop_services stopped and health-check wsi-app. Returns 1 when it
# did not come up.
bring_back() {
    [[ ${#WAS_ACTIVE[@]} -gt 0 ]] || { SERVICES_STOPPED=0; return 0; }
    start_services || return 1
    [[ " ${WAS_ACTIVE[*]} " == *" wsi-app.service "* ]] || return 0
    local timeout="${WSI_HEALTH_TIMEOUT:-30}" rc=0
    sub "Waiting for wsi-app.service to come up (timeout ${timeout}s) ..."
    wait_backend_healthy "$timeout" || rc=$?
    printf '\n'
    if [[ "$rc" -eq 0 ]]; then
        ok "wsi-app.service is healthy."
        return 0
    fi
    warn "wsi-app.service did NOT come up. Recent logs:"
    journalctl -u wsi-app.service -n 30 --no-pager 2>/dev/null | sed 's/^/    /' >&2 || true
    return 1
}

# sign_out: revoke every session issued before the swap, against
# SIGN_OUT_PREV (see the header). On failure recover() tries once more and
# otherwise leaves the services stopped.
# Exit 3 from the helper: it could not, but left the marker that makes
# wsi-app finish the sign-out at its start (and refuse to serve until then).
try_sign_out() {
    local rc=0
    app_helper sign-out "$DB_PATH" "$SIGN_OUT_PREV" "$RESTORE_APPLY_PY" || rc=$?
    case "$rc" in
        0) SIGNED_OUT=1 ;;
        3) SIGNED_OUT=1; SIGN_OUT_DEFERRED=1 ;;
        *) return 1 ;;
    esac
}
sign_out() {
    try_sign_out || die "Could not sign out the sessions from before the swap (above)."
    if [[ "$SIGN_OUT_DEFERRED" == "1" ]]; then
        warn "The sign-out is handed to wsi-app's next start, which does it before serving (above)."
    else
        ok "Signed everyone out of this server (every login from before the swap is refused)."
    fi
}

# check_nothing_pending: refuse to swap while this server has work of its own
# pending that wsi-app's next start would do over the swap: a sign-out an
# earlier swap handed to the app (another swap would destroy what it compares
# against), a restore staged in the dashboard, or an interrupted restore's
# journal (start-wsi-app.sh applies or rolls those back before uvicorn, over
# whatever this put in place). A dry run only reports.
check_nothing_pending() {
    local req journal pending=0
    req="$(wsi_env_value_trimmed RESTORE_REQUEST_PATH "$WSI_ENV_FILE")"
    req="${req:-/run/wsi/restore.request}"
    journal="$(wsi_env_value_trimmed RESTORE_JOURNAL_PATH "$WSI_ENV_FILE")"
    journal="${journal:-$DATA_DIR/.restore-apply.journal}"
    app_helper check-sign-out "$DB_PATH" "$RESTORE_APPLY_PY" || pending=1
    if [[ -e "$req" || -L "$req" ]]; then
        printf 'error: a restore is staged on this server (%s): it is applied when wsi-app next starts.\n' "$req" >&2
        printf '       The dashboard cannot cancel it. Restart wsi-app (sudo systemctl restart wsi-app),\n' >&2
        printf '       which applies it (or drops it, if over 10 minutes old), check the dashboard, then sync.\n' >&2
        pending=1
    fi
    if [[ -e "$journal" || -L "$journal" ]]; then
        printf 'error: an interrupted restore is recorded on this server (%s): wsi-app reconciles it\n' "$journal" >&2
        printf '       when it next starts. Start it (sudo systemctl start wsi-app) and check the dashboard first.\n' >&2
        pending=1
    fi
    [[ "$pending" == "0" ]] && return 0
    [[ "$DRY_RUN" == "1" ]] || die "Not going ahead (above)."
    warn "A real run would stop here (above)."
}

# promote_pre_sync: the copy taken before this sync's swap becomes the
# rollback point, recording what --rollback must undo (a standby sync: the
# content tables; --all: the whole database). The wsi.env an earlier --all
# kept went with the database before THAT sync, so it goes.
promote_pre_sync() {
    as_app_user mv -f -- "$PRE_SYNC_DB.new" "$PRE_SYNC_DB" || return 1
    SIGN_OUT_PREV="$PRE_SYNC_DB"
    printf '%s\n' "$MODE" > "$STAGE/pre-sync.mode"
    rm -f -- "$STAGE/wsi.env.pre-sync"
}

# How to go back, for the failure messages: a rollback has used up
# wsi.db.pre-sync, and keeps the database it replaced instead.
go_back_hint() {
    if [[ "$MODE" == "rollback" ]]; then
        printf '%s' "put back the database from before this rollback, kept as $DB_PATH.pre-rollback, from a shell"
    else
        printf '%s' "to go back run: sudo bash infra/sync-from.sh --rollback"
    fi
}

# The advice when wsi-app does not come up after a swap. With a sign-out still
# pending, it is the app's retry that keeps failing, and --rollback refuses
# until that is solved: point at the cause instead.
not_up_hint() {
    if [[ -e "$DB_PATH.session-revocation-pending" ]]; then
        printf '%s' "wsi-app refuses to start until the sign-out handed to it succeeds; its log says why
(sudo journalctl -u wsi-app -n 80). Fix that (the database must be writable), then start it"
    else
        go_back_hint
    fi
}

# replace_db SNAPSHOT: copy SNAPSHOT over the live database. Exit 4 from the
# helper means the copy landed but failed its check: it was swapped all the
# same, so recover() must sign everyone out before anything starts.
replace_db() {
    local rc=0
    SWAP_STARTED=1
    app_helper replace "$DB_PATH" "$1" || rc=$?
    case "$rc" in
        0) ;;
        4) die "$DB_PATH was replaced but failed its check (above). Restore it from a
backup, or $(go_back_hint)." ;;
        *) SWAP_STARTED=0
           die "Copying $1 in failed (above); the database is unchanged." ;;
    esac
}

# plain_path P: an absolute path of plain characters, with no "." or ".."
# component (which the outside-DATA_DIR checks below would not see through).
plain_path() {
    [[ "$1" =~ ^/[A-Za-z0-9._/-]+$ && ! "$1/" =~ /\.\.?/ ]]
}

# check_stage: WSI_REPLICA_DIR must be a directory only root controls: outside
# DATA_DIR (which the service user owns), in a root-owned parent the service
# user cannot rename it out of, and never a symlink. It need not exist yet.
check_stage() {
    local data_real stage_parent parent_real stage_real
    plain_path "$STAGE" || die "WSI_REPLICA_DIR '$STAGE' must be an absolute path of plain characters."
    STAGE="${STAGE%/}"
    MIRROR="$STAGE/mirror"
    data_real="$(cd -P -- "$DATA_DIR" && pwd -P)"
    stage_parent="$(dirname "$STAGE")"
    [[ -d "$stage_parent" ]] || die "$stage_parent (parent of WSI_REPLICA_DIR) does not exist."
    parent_real="$(cd -P -- "$stage_parent" && pwd -P)"
    stage_real="$parent_real/$(basename "$STAGE")"
    case "$stage_real/" in "$data_real"/*) die "WSI_REPLICA_DIR must be outside DATA_DIR ($DATA_DIR)." ;; esac
    case "$data_real/" in "$stage_real"/*) die "WSI_REPLICA_DIR must not contain DATA_DIR ($DATA_DIR)." ;; esac
    [[ "$(stat -c '%u' "$parent_real")" == "0" && "$(( 8#$(stat -c '%a' "$parent_real") & 8#022 ))" -eq 0 ]] \
        || die "$parent_real must be owned by root and writable by nobody else (it holds WSI_REPLICA_DIR)."
    if [[ -L "$STAGE" ]] || { [[ -e "$STAGE" ]] && [[ ! -d "$STAGE" || "$(stat -c '%u' "$STAGE")" != "0" ]]; }; then
        die "$STAGE exists but is not a directory owned by root."
    fi
}

# take_lock: one sync or rollback at a time.
take_lock() {
    exec 9>"$LOCK_FILE"
    flock -n 9 || die "Another sync or rollback is running (lock: $LOCK_FILE)."
}

confirm() {
    [[ "$ASSUME_YES" == "1" ]] && return 0
    [[ -t 0 ]] || die "Not a terminal, so cannot ask before applying; pass --yes."
    local answer
    read -r -p "  $1 [y/N] " answer
    [[ "$answer" == [yY] || "$answer" == [yY][eE][sS] ]]
}

# ---- Preconditions ---------------------------------------------------------
[[ $EUID -eq 0 ]] || die "Please run as root (sudo bash infra/sync-from.sh ...)."
[[ -f "$HELPER" ]] || die "$HELPER not found."
for c in sudo systemctl flock python3; do
    command -v "$c" >/dev/null 2>&1 || die "$c is not installed."
done
DB_PATH="$(wsi_sqlite_db_path "$WSI_ENV_FILE" "$DEFAULT_DB_PATH")" \
    || die "DATABASE_URL in $WSI_ENV_FILE is not an absolute sqlite:////path."
[[ -f "$DB_PATH" ]] || die "No database at $DB_PATH. Install Tessera here (infra/install.sh) first."
PRE_SYNC_DB="$DB_PATH.pre-sync"
DATA_DIR="${DATA_DIR:-$(wsi_env_value_trimmed DATA_DIR "$WSI_ENV_FILE")}"
DATA_DIR="${DATA_DIR:-/var/lib/wsi}"
DATA_DIR="${DATA_DIR%/}"
[[ -d "$DATA_DIR" ]] || die "DATA_DIR $DATA_DIR does not exist."

# ---- --rollback ------------------------------------------------------------
if [[ "$MODE" == "rollback" ]]; then
    TOTAL_STEPS=2
    [[ -z "$SOURCE" ]] || die "--rollback takes no source host."
    [[ "$DRY_RUN" == "0" && -z "$BWLIMIT" ]] || die "--rollback takes no --dry-run or --bwlimit."
    printf '%s==> Tessera: roll back the last sync%s\n' "$C_BOLD" "$C_OFF"
    step "Checking $PRE_SYNC_DB"
    take_lock
    check_nothing_pending
    check_stage
    [[ -f "$PRE_SYNC_DB" && ! -L "$PRE_SYNC_DB" ]] || die "No $PRE_SYNC_DB to roll back to."
    app_helper quick-check "$PRE_SYNC_DB" || die "$PRE_SYNC_DB is damaged; not rolling back."
    ok "$PRE_SYNC_DB is intact ($(date -r "$PRE_SYNC_DB" '+%Y-%m-%d %H:%M'))."
    # What the last sync was: a standby sync swapped only the content tables,
    # so only they go back (this server's own settings, statistics and audit
    # trail since then stay); a --all replaced everything, wsi.env included.
    LAST_MODE="$(cat "$STAGE/pre-sync.mode" 2>/dev/null || true)"
    PRE_SYNC_ENV=""
    if [[ "$LAST_MODE" == "all" ]]; then
        sub "The last sync was a --all: the whole database goes back."
    elif [[ "$LAST_MODE" == "content" ]]; then
        sub "The last sync was a standby sync: its content goes back; this server's own"
        sub "settings, accounts, statistics and logs since then stay."
    else
        LAST_MODE="all"
        warn "No record of what the last sync was: the whole database goes back."
    fi
    # The wsi.env the last sync changed (a --all's secrets, or a standby
    # sync's WSI_MFA_KEY), kept beside the database it goes with.
    if [[ -f "$STAGE/wsi.env.pre-sync" && ! -L "$STAGE/wsi.env.pre-sync" ]]; then
        PRE_SYNC_ENV="$STAGE/wsi.env.pre-sync"
        if [[ "$LAST_MODE" == "all" ]]; then
            sub "$WSI_ENV_FILE goes back too."
        else
            sub "The WSI_MFA_KEY it wrote into $WSI_ENV_FILE goes back too."
        fi
    fi
    confirm "Stop wsi-app / wsi-sync and put this database back?" || die "Cancelled; nothing changed."
    step "Restoring the database"
    stop_services
    check_nothing_pending
    app_helper backup-db "$DB_PATH" "$DB_PATH.pre-rollback" \
        || die "Could not copy the current database aside; nothing was changed."
    SIGN_OUT_PREV="$DB_PATH.pre-rollback"
    # The files the sync put in place stay (a WSI it replaced under the same
    # name keeps the new image), so no generation the old database names may
    # be served from the edge cache again.
    BUMP_NEEDED=1
    if [[ "$LAST_MODE" == "all" ]]; then
        replace_db "$PRE_SYNC_DB"
    else
        SWAP_STARTED=1
        app_helper merge "$DB_PATH" "$PRE_SYNC_DB" \
            || { SWAP_STARTED=0; die "Putting the content back failed (above) and was undone; the database is unchanged."; }
    fi
    # Used up: the live database is that copy now. A second --rollback would
    # otherwise overwrite wsi.db.pre-rollback, the only copy of the synced one.
    as_app_user rm -f -- "$PRE_SYNC_DB"
    rm -f -- "$STAGE/pre-sync.mode"
    ok "Restored $DB_PATH from $PRE_SYNC_DB (the replaced one is kept as $DB_PATH.pre-rollback)."
    app_helper bump-generations "$DB_PATH" || die "Could not move the slides to fresh tile generations (above)."
    BUMP_NEEDED=0
    ok "Moved every slide to a fresh tile generation."
    sign_out
    if [[ -n "$PRE_SYNC_ENV" && "$LAST_MODE" == "all" ]]; then
        install -m "$(stat -c '%a' "$WSI_ENV_FILE")" -o "$(stat -c '%u' "$WSI_ENV_FILE")" \
            -g "$(stat -c '%g' "$WSI_ENV_FILE")" "$PRE_SYNC_ENV" "$WSI_ENV_FILE.sync-tmp"
        mv -f -- "$WSI_ENV_FILE.sync-tmp" "$WSI_ENV_FILE"
        rm -f -- "$PRE_SYNC_ENV"
        ok "Restored $WSI_ENV_FILE."
    elif [[ -n "$PRE_SYNC_ENV" ]]; then
        # A standby sync changed one line, WSI_MFA_KEY: put back that line
        # only, so edits made to wsi.env since then stay.
        prev_key=""
        if ! prev_env="$(root_helper env-secrets "$PRE_SYNC_ENV")"; then
            # Unread is not "none": keep the copy and say where it is.
            warn "Could not read the previous WSI_MFA_KEY from $PRE_SYNC_ENV (above); it stays there —"
            warn "copy its WSI_MFA_KEY line into $WSI_ENV_FILE by hand."
        elif ! prev_key="$(printf '%s\n' "$prev_env" | grep '^WSI_MFA_KEY=')"; then
            # Nothing to put back: this server had no key, so nothing in the
            # database restored here was sealed under one, and the source's
            # key it keeps opens nothing — it does no harm.
            rm -f -- "$PRE_SYNC_ENV"
            sub "This server had no WSI_MFA_KEY before that sync; the source's stays in $WSI_ENV_FILE."
        elif printf '%s\n' "$prev_key" | root_helper set-env "$WSI_ENV_FILE" >/dev/null; then
            rm -f -- "$PRE_SYNC_ENV"
            ok "Put this server's previous WSI_MFA_KEY back into $WSI_ENV_FILE."
        else
            warn "Could not put the previous WSI_MFA_KEY back into $WSI_ENV_FILE (above);"
            warn "it is in $PRE_SYNC_ENV — copy that line by hand."
        fi
        prev_key="" prev_env=""
    fi
    bring_back || die "The rolled-back database is in place but wsi-app did not come up. $(not_up_hint)"
    exit 0
fi

# ---- Sync / migration --------------------------------------------------------
[[ -n "$SOURCE" ]] || die "Name the source server: sudo bash infra/sync-from.sh [user@]host (see --help)."
# Plain host names / addresses only: the value goes into ssh and rsync's
# host:path syntax. Ports, IPv6 addresses and keys go in ~/.ssh/config.
[[ "$SOURCE" =~ ^([A-Za-z0-9._-]+@)?[A-Za-z0-9][A-Za-z0-9._-]*$ ]] \
    || die "'$SOURCE' is not a host name or [user@]host (use a ~/.ssh/config alias for ports or IPv6)."
for p in "$SRC_DB" "$SRC_DATA_DIR" "$SRC_ENV_FILE"; do
    plain_path "$p" || die "'$p' must be an absolute path of plain characters."
done
SRC_DATA_DIR="${SRC_DATA_DIR%/}"
check_stage
[[ -z "$BWLIMIT" || "$BWLIMIT" =~ ^[0-9]+[KkMmGg]?$ ]] || die "--bwlimit=$BWLIMIT is not a rate like 20M."
for c in ssh rsync curl; do
    command -v "$c" >/dev/null 2>&1 || die "$c is not installed (apt install $c)."
done

printf '%s==> Tessera sync%s\n' "$C_BOLD" "$C_OFF"
printf '    source:     %s (%s, %s)\n' "$SOURCE" "$SRC_DB" "$SRC_DATA_DIR"
printf '    this host:  %s (%s)\n' "$DB_PATH" "$DATA_DIR"
printf '    mirror:     %s\n' "$MIRROR"
if [[ "$MODE" == "all" ]]; then
    printf '    mode:       %s--all%s (migration: the whole database is replaced)\n' "$C_BOLD" "$C_OFF"
else
    printf '    mode:       standby (this server keeps its own access settings and records)\n'
fi
[[ "$DRY_RUN" == "1" ]] && printf '    %s--dry-run: nothing on this server is changed%s\n' "$C_BOLD" "$C_OFF"

step "Preparing the mirror and connecting to $SOURCE"
# The mirror is written by root (check_stage above says where it may be).
install -d -m 0750 -o root -g "$APP_GROUP" "$STAGE" "$MIRROR" "$MIRROR/wsi" "$MIRROR/thumbs" \
    "$MIRROR/assets" "$MIRROR/assets/tours"
take_lock
check_nothing_pending
# Linked or copied into place? Asked of link() itself: one filesystem mounted
# twice (a bind mount) has one st_dev but refuses links across (EXDEV).
probe="$MIRROR/wsi/.link-probe.sync-$(od -An -N6 -tx1 /dev/urandom | tr -d ' \n')"
: > "$probe"
chown "$APP_USER:$APP_GROUP" "$probe"
SAME_FS="$(app_helper can-link "$probe" "$DATA_DIR/wsi")" \
    || { rm -f -- "$probe"; die "Could not test hard-linking from $MIRROR into $DATA_DIR/wsi (above)."; }
rm -f -- "$probe"
if [[ "$SAME_FS" != "1" ]]; then
    SAME_FS=0
    warn "$MIRROR and $DATA_DIR/wsi are on different filesystems: files are COPIED into place,"
    warn "so the WSI files need their space twice, and new or changed ones are copied while the"
    warn "services are stopped. Set WSI_REPLICA_DIR on DATA_DIR's filesystem to avoid both."
fi

SSH_AS_USER="${SSH_AS_USER:-${SUDO_USER:-root}}"
id "$SSH_AS_USER" >/dev/null 2>&1 || die "SSH_AS_USER=$SSH_AS_USER is not a user here."
# One authenticated connection (ControlMaster) carries every ssh / rsync call
# below, so a password or key passphrase is asked for once.
if [[ "$SSH_AS_USER" == "root" ]]; then
    SSH_CTL_DIR="$(mktemp -d /tmp/wsi-sync-ssh.XXXXXX)"
    SSH_BASE=(ssh)
else
    SSH_CTL_DIR="$(sudo -u "$SSH_AS_USER" mktemp -d /tmp/wsi-sync-ssh.XXXXXX)"
    SSH_BASE=(sudo -u "$SSH_AS_USER" -H ssh)
fi
SSH_BASE+=(-o "ControlPath=$SSH_CTL_DIR/cm" -o ServerAliveInterval=30 -o ServerAliveCountMax=10)
RSYNC_RSH="${SSH_BASE[*]}"
sub "ssh as $SSH_AS_USER to $SOURCE ..."
# The backgrounded master must not hold the lock (fd 9) after this script is
# gone, and ends on its own 15 minutes after its last use should the EXIT
# trap not run (a SIGKILL).
"${SSH_BASE[@]}" -o ControlMaster=yes -o ControlPersist=900 -f -N "$SOURCE" 9>&- \
    || die "Could not connect to $SOURCE as $SSH_AS_USER (try: ssh $SOURCE)."
run_ssh() { "${SSH_BASE[@]}" "$SOURCE" "$@"; }
# What the source runs, always as its service user (see the header's sudoers
# line). The helper travels on stdin: the source needs nothing beyond Tessera.
# The arguments are paths checked above and fixed words, so expanding them
# into the remote command line here is intended.
# shellcheck disable=SC2029
src_helper() { run_ssh "cd / && sudo -n -u $APP_USER python3 -I - $*" < "$HELPER"; }
# shellcheck disable=SC2029
run_ssh "sudo -n -u $APP_USER python3 -I -c '' && sudo -n -u $APP_USER rsync --version" </dev/null >/dev/null 2>&1 || die "On $SOURCE, this ssh user may not run commands as $APP_USER without a password.
Add this with 'sudo visudo -f /etc/sudoers.d/wsi-sync' there (<user> = the ssh user):
    <user> ALL=($APP_USER) NOPASSWD: /usr/bin/rsync, /usr/bin/python3"
run_ssh "command -v rsync >/dev/null && command -v python3 >/dev/null" </dev/null \
    || die "$SOURCE lacks rsync or python3."
ok "Connected; the source lets this run commands as $APP_USER."

step "Checking versions"
src_helper state "$SRC_DB" "$SRC_DATA_DIR" > "$STAGE/state.json" \
    || die "Could not read the source's database ($SRC_DB on $SOURCE)."
app_helper info "$DB_PATH" > "$STAGE/info.json"
root_helper check-versions --scope "$MODE" "$STAGE/state.json" "$STAGE/info.json" \
    || die "The two servers cannot be synced as they are (reason above)."
ok "Versions are compatible."

RSYNC_COMMON=(-s -t -p "--chmod=D0750,F0640" --no-links --no-specials --no-devices
              --partial-dir=.rsync-partial -e "$RSYNC_RSH"
              --rsync-path="cd / && sudo -n -u $APP_USER rsync")
[[ -n "$BWLIMIT" ]] && RSYNC_COMMON+=("--bwlimit=$BWLIMIT")

# rsync_files [extra rsync args...]: the WSI / thumbnail files the snapshot
# names, then the attachments of its tours. 23 / 24 (some files could not be
# read / vanished) are not failures here: the check after the round decides.
rsync_files() {
    local rc=0
    LC_ALL=C rsync "${RSYNC_COMMON[@]}" "$@" --from0 --files-from="$STAGE/files-from" \
        "$SOURCE:$SRC_DATA_DIR/" "$MIRROR/" || rc=$?
    [[ "$rc" -eq 0 || "$rc" -eq 23 || "$rc" -eq 24 ]] || return "$rc"
    rc=0
    LC_ALL=C rsync "${RSYNC_COMMON[@]}" "$@" -r --delete --delete-excluded \
        --filter="merge $STAGE/tour-filter" \
        "$SOURCE:$SRC_DATA_DIR/assets/tours/" "$MIRROR/assets/tours/" || rc=$?
    [[ "$rc" -eq 0 || "$rc" -eq 23 || "$rc" -eq 24 ]] || return "$rc"
}

# Bytes rsync would transfer, from a --dry-run --stats pass.
transfer_bytes() {
    { rsync_files --dry-run --stats 2>/dev/null || true; } \
        | awk -F': ' '/^Total transferred file size/ { gsub(/[^0-9]/, "", $2); n += $2 } END { print n + 0 }'
}

human() { numfmt --to=iec --suffix=B "$1" 2>/dev/null || printf '%s bytes' "$1"; }

fetch_snapshot() {
    src_helper export "$SRC_DB" --scope "$MODE" > "$STAGE/snapshot.db.part" \
        || die "Could not snapshot the source's database."
    mv -f -- "$STAGE/snapshot.db.part" "$STAGE/snapshot.db"
    # Built by root from root's own copy: these name what root's rsync writes.
    root_helper files-from "$STAGE/snapshot.db" > "$STAGE/files-from"
    root_helper tour-filter "$STAGE/snapshot.db" > "$STAGE/tour-filter"
}

step "Fetching the source's database and files"
round=0
while :; do
    round=$((round + 1))
    sub "Snapshotting the source's database (round $round) ..."
    # The source's state as read before this snapshot (by the version check,
    # or after the previous round's transfer), so an attachment changed while
    # the snapshot is taken shows up as a difference from the read after this
    # transfer.
    cp -f -- "$STAGE/state.json" "$STAGE/state-pre.json"
    fetch_snapshot
    # Sized (a file-list-only rsync pass) for the space check in the first
    # round; later rounds carry only what changed during the one before.
    need=0
    [[ "$round" -gt 1 ]] || need="$(transfer_bytes)"
    if [[ "$DRY_RUN" == "1" ]]; then
        ok "Would transfer $(human "$need") of new or changed files."
        break
    fi
    # The mirror's filesystem must take the transfer.
    avail="$(df -P -B1 "$MIRROR" | awk 'NR == 2 { print $4 }')"
    (( need < avail )) || die "$(human "$need") to transfer, but only $(human "$avail") free for $MIRROR."
    if [[ "$round" -gt 1 ]]; then sub "Transferring what changed ..."; else sub "Transferring $(human "$need") ..."; fi
    if [[ -t 1 ]]; then
        rsync_files --info=progress2 || die "rsync from $SOURCE failed."
    else
        rsync_files || die "rsync from $SOURCE failed."
    fi
    # rsync wrote the mirror as root. The service user must own the files to
    # hard-link them into DATA_DIR (fs.protected_hardlinks), and reach the
    # directories rsync created (a tour's) through their group. The
    # directories stay root's, so it still cannot add, rename or remove
    # anything in the mirror.
    find "$MIRROR" -xdev -type f ! -user "$APP_USER" -exec chown "$APP_USER:$APP_GROUP" {} +
    find "$MIRROR" -xdev -type d ! -group "$APP_GROUP" -exec chgrp "$APP_GROUP" {} +
    src_helper state "$SRC_DB" "$SRC_DATA_DIR" > "$STAGE/state.json" \
        || die "Could not re-read the source's database."
    rc=0
    root_helper check "$STAGE/state-pre.json" "$STAGE/state.json" "$STAGE/snapshot.db" "$MIRROR" || rc=$?
    if [[ "$rc" -eq 0 ]]; then
        ok "The mirror holds every file the snapshot refers to."
        break
    fi
    [[ "$rc" -eq 3 ]] || die "The transfer is incomplete (above); nothing on this server was changed."
    (( round < MAX_ROUNDS )) || die "The source kept changing over $MAX_ROUNDS rounds; run again when nobody is editing there."
    warn "Going round again for the changes."
done

# Files are copied into place rather than linked when the mirror is on
# another filesystem: DATA_DIR's must then take every file it lacks, whether
# it arrived now or in an earlier run that was never applied.
if [[ "$SAME_FS" == "0" && "$DRY_RUN" == "0" ]]; then
    copy="$(app_helper install-bytes "$STAGE/snapshot.db" "$MIRROR" "$DATA_DIR")"
    avail="$(df -P -B1 "$DATA_DIR/wsi" | awk 'NR == 2 { print $4 }')"
    (( copy < avail )) || die "$(human "$copy") to copy into place, but only $(human "$avail") free for $DATA_DIR."
fi

summary="$(app_helper summary "$STAGE/snapshot.db" "$DB_PATH" --scope "$MODE")" \
    || die "Could not compare the snapshot with this server's database."
printf '%s\n' "$summary" | sed 's/^/    /'
if [[ "$MODE" == "content" ]]; then
    sub "Kept as this server has them: network and sign-in settings, app accounts, statistics and logs."
    # Admin accounts with two-factor sign-in come over sealed under the
    # source's WSI_MFA_KEY, so that key has to come too (below). A failed
    # check counts as "some do": skipping the key silently would leave those
    # accounts unable to sign in here.
    MFA_ACCOUNTS="$(app_helper mfa-accounts "$STAGE/snapshot.db")" || MFA_ACCOUNTS="?"
    if [[ "$MFA_ACCOUNTS" == "?" ]]; then
        warn "Could not check whether the admin accounts use two-factor sign-in (above); the source's WSI_MFA_KEY will be carried over in case."
    elif [[ "$MFA_ACCOUNTS" != "0" ]]; then
        sub "$MFA_ACCOUNTS admin account(s) with two-factor sign-in: the source's WSI_MFA_KEY goes into $WSI_ENV_FILE too."
    fi
fi

if [[ "$DRY_RUN" == "1" ]]; then
    step "Dry run: nothing applied"
    ok "Run again without --dry-run to apply."
    exit 0
fi

step "Applying on this server"
SECRETS=""
if [[ "$MODE" == "all" ]]; then
    # shellcheck disable=SC2029
    SECRETS="$(run_ssh "sudo -n python3 -I - env-secrets $SRC_ENV_FILE" < "$HELPER" 2>/dev/null)" || SECRETS=""
    if [[ -n "$SECRETS" ]]; then
        sub "Read ANALYTICS_HASH_SALT / WSI_MFA_KEY from the source; they are written to $WSI_ENV_FILE."
    else
        warn "Could not read ANALYTICS_HASH_SALT / WSI_MFA_KEY from the source's $SRC_ENV_FILE"
        warn "(needs passwordless root sudo there, and a value set). Copy them by hand afterwards,"
        warn "or hashed-mode statistics treat every returning visitor as new and accounts with"
        warn "two-factor sign-in cannot sign in. On $SOURCE:"
        warn "    sudo grep '^ANALYTICS_HASH_SALT=' $SRC_ENV_FILE"
        warn "    sudo grep '^WSI_MFA_KEY=' $SRC_ENV_FILE"
        warn "then put those lines into $WSI_ENV_FILE here and run: sudo systemctl restart wsi-app wsi-sync"
    fi
elif [[ "${MFA_ACCOUNTS:-0}" != "0" ]]; then
    # A standby sync keeps this server's own wsi.env, but the admin accounts
    # it takes over include two-factor ones, whose secrets open only under
    # the source's WSI_MFA_KEY: carry that one key (not the analytics salt,
    # which goes with the statistics this server keeps).
    # shellcheck disable=SC2029
    SECRETS="$(run_ssh "sudo -n python3 -I - env-secrets $SRC_ENV_FILE" < "$HELPER" 2>/dev/null \
                 | grep '^WSI_MFA_KEY=')" || SECRETS=""
    if [[ -n "$SECRETS" ]]; then
        sub "Read WSI_MFA_KEY from the source (accounts with two-factor sign-in come over); it is written to $WSI_ENV_FILE."
    else
        warn "Accounts with two-factor sign-in come over, but WSI_MFA_KEY could not be read from"
        warn "the source's $SRC_ENV_FILE (needs passwordless root sudo there). Unless this server"
        warn "already has the source's key, those accounts cannot sign in here. On $SOURCE:"
        warn "    sudo grep '^WSI_MFA_KEY=' $SRC_ENV_FILE"
        warn "then put that line into $WSI_ENV_FILE here and run: sudo systemctl restart wsi-app wsi-sync"
    fi
fi
confirm "Stop wsi-app / wsi-sync here for a few seconds and apply?" || die "Cancelled; nothing on this server was changed."
stop_services
# Again, now that nothing can stage a restore any more: one staged during the
# transfer would be applied over the sync at the next start.
check_nothing_pending
# The copy becomes the rollback point only once the swap has landed: a sync
# that fails before keeps the previous one (and the wsi.env that goes with it).
app_helper backup-db "$DB_PATH" "$PRE_SYNC_DB.new" \
    || die "Could not copy the current database aside; nothing was changed."
SIGN_OUT_PREV="$PRE_SYNC_DB.new"
BUMP_NEEDED=1
# Kept: what the live database and the current rollback point refer to.
placed="$(app_helper install-files "$STAGE/snapshot.db" "$MIRROR" "$DATA_DIR" \
            --keep-db "$PRE_SYNC_DB.new" --keep-db "$PRE_SYNC_DB")" \
    || die "Putting the files in place failed; the database is unchanged. Run the sync again."
ok "Files: $placed."
if [[ "$MODE" == "all" ]]; then
    replace_db "$STAGE/snapshot.db"
    BUMP_NEEDED=0
    ok "Replaced the database with the source's."
else
    SWAP_STARTED=1
    app_helper merge "$DB_PATH" "$STAGE/snapshot.db" \
        || { SWAP_STARTED=0; die "The content swap failed (above) and was undone; the database is unchanged. Run the sync again."; }
    BUMP_NEEDED=0
    ok "Swapped in the source's content."
fi
promote_pre_sync || die "Could not keep the previous database as $PRE_SYNC_DB (above)."
ok "Kept the previous database as $PRE_SYNC_DB (sudo bash infra/sync-from.sh --rollback)."
# A sync that is about to change wsi.env (a --all, or a standby sync carrying
# WSI_MFA_KEY) keeps it for --rollback.
[[ -z "$SECRETS" ]] || install -m 0600 -o root -g root "$WSI_ENV_FILE" "$STAGE/wsi.env.pre-sync"
sign_out
# The old rollback point is gone now, and with it the reason to keep the files
# only it referred to: sweep again, keeping what the new one refers to.
if swept="$(app_helper install-files "$STAGE/snapshot.db" "$MIRROR" "$DATA_DIR" --keep-db "$PRE_SYNC_DB")"; then
    sub "Files no longer referenced by this or the previous database: ${swept#*, }."
else
    warn "Could not remove the files no longer referenced (above); the next sync does."
fi
if [[ -n "$SECRETS" ]]; then
    if written="$(printf '%s\n' "$SECRETS" | root_helper set-env "$WSI_ENV_FILE")"; then
        ok "Wrote $written to $WSI_ENV_FILE (the previous file is kept for --rollback)."
    else
        warn "Could not write the source's keys into $WSI_ENV_FILE (above); copy them by hand:"
        warn "    on $SOURCE: sudo grep -E '^(ANALYTICS_HASH_SALT|WSI_MFA_KEY)=' $SRC_ENV_FILE"
    fi
fi
SECRETS=""
bring_back || die "The sync is applied but wsi-app did not come up. $(not_up_hint)"

step "Done"
trap - INT HUP TERM
root_helper prune-mirror "$STAGE/files-from" "$MIRROR" || warn "Could not prune $MIRROR."
rm -f -- "$STAGE/snapshot.db" "$STAGE/state.json" "$STAGE/state-pre.json" "$STAGE/info.json"
printf '\n%s Sync complete in %ss.%s\n' "$C_GREEN" "$(( $(date +%s) - START_TS ))" "$C_OFF"
printf '   Everyone signed in to this server signs in again.\n'
[[ "$SIGN_OUT_DEFERRED" == "0" ]] || printf '   (The sign-out itself was handed to wsi-app'"'"'s start; see the warning above.)\n'
if [[ "$MODE" == "all" ]]; then
    printf '   Every setting came along, Google sign-in and mDNS included: point the old\n'
    printf '   server'"'"'s host name at this one before people use it.\n'
fi
