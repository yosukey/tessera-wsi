#!/usr/bin/env bash
# =====================================================================
# Tessera - update helper
#
# Moves the cloned repo to the latest release tag (or the ref / branch the
# options below name), re-runs infra/install.sh
# (idempotent) to refresh /opt/wsi, rebuild the frontend, refresh backend
# deps, and install the systemd units and nginx config (reloading both), then
# starts wsi-app / wsi-sync again and health-checks them.
#
# Usage (from the originally cloned repo):
#   sudo bash infra/update.sh
#
# Options (environment variables):
#   SKIP_GIT_PULL=1     Skip "git pull" step (use current working tree).
#   GIT_REF=<ref>       Check out this ref (branch or tag; a branch is then
#                       fast-forwarded to origin's tip).
#                       Overrides the default "latest vX.Y.Z tag" behavior
#                       (a final release; a pre-release tag such as
#                       v2.0.0-rc1 is deployed only when named here).
#   WSI_TRACK_BRANCH=1  Stay on the current branch HEAD (running git pull)
#                       instead of checking out the latest release tag.
#                       From a detached HEAD (a previous tag update) it
#                       first switches to origin's default branch.
#                       Use this when developing against this repo.
#   SKIP_RESTART=1      Do not start wsi-app / wsi-sync again at the end.
#                       NOTE: step 2 still stops wsi-app / wsi-sync before
#                       the file swap, so the site stays DOWN until you run
#                       `systemctl start wsi-app wsi-sync` yourself. nginx is
#                       reloaded regardless: install.sh (step 3) installs the
#                       new nginx config and reloads it every time.
#   WSI_SKIP_ROLLBACK=1 If a step fails partway through, leave the working
#                       tree at the failed state instead of running
#                       `git reset --hard` back to the captured SHA.
#                       Useful when you want to inspect the breakage. The
#                       services are still started again on whatever is
#                       deployed, and the DB snapshot is kept.
#   WSI_SKIP_DB_SNAPSHOT=1
#                       Do not snapshot the SQLite DB before the update. A
#                       rollback after the new release has started then
#                       cannot restore it, and the previous release refuses
#                       to boot against the DB the new one stamped.
#   GIT_AS_USER=<name>  Override the user that `git` runs as (defaults to
#                       SUDO_USER so credentials in $HOME resolve).
#   WSI_HEALTH_TIMEOUT=<s>
#                       How long step 5 (and a rollback) waits for wsi-app's
#                       health check (default 30).
#   WSI_SYNC_HEALTH_TIMEOUT=<s>
#                       How long step 5 waits for wsi-sync's health check
#                       (default 30).
#
# This script preserves:
#   - /etc/wsi/wsi.env              (env / secrets; never regenerated. The
#                                    exceptions are install.sh's
#                                    WSI_LAN_HOST / WSI_LAN_PROBE_TIMEOUT_MS
#                                    / WSI_PUBLIC_BASE_URL rule, which fires
#                                    only if you pass one of those to this
#                                    script — see infra/install.sh's header —
#                                    and a WSI_MFA_KEY added when the file
#                                    has none)
#   - /var/lib/wsi/**               (WSI files, thumbs, assets, cache,
#                                    SQLite DB at /var/lib/wsi/wsi.db)
#
# Before install.sh runs, the SQLite DB is snapshotted (as the service user,
# with the services stopped) into a .wsi-update-snapshot.* directory next to
# it. If the update then fails after the new release has been started, the
# rollback restores that snapshot before starting the previous release: the
# new release stamps its version into the DB on first boot, and the previous
# release refuses to start against a DB stamped by a newer one. Anything
# written while the new release was up is discarded with it. The snapshot is
# deleted once the update (or the rollback) is healthy; whenever it is kept
# instead, its path is printed.
#
# Ctrl-C / SIGHUP / SIGTERM mid-update run the same recovery as a failed
# step (rollback, or at least restarting the services step 2 stopped). A
# failure before install.sh has run deploys nothing, so it only resets the
# source tree and restarts the services.
# =====================================================================
# -E (errtrace): without it the ERR trap below is NOT inherited by shell
# functions, so a failing `run_git ...` (every git call goes through that
# helper) would exit the script silently with no on_error diagnostic and no
# rollback. With -E the trap fires inside functions too. Tested-context calls
# (`run_git fetch ... || warn`, `if ...; then`) are still exempt from the trap.
set -Eeuo pipefail

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
                "$REPO_DIR/infra/install.sh" "$(realpath -- "${BASH_SOURCE[0]}")"; do
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
INSTALL_SCRIPT="$REPO_DIR/infra/install.sh"
# Helpers shared with install.sh (wsi_env_value, wsi_define_run_git), read
# from the same clone as this script. Loaded now, before step 1 can move the
# tree, so a rollback keeps working whatever the checkout did to the file.
# shellcheck source=lib/wsi-common.sh
. "$REPO_DIR/infra/lib/wsi-common.sh"
APP_HOME="${APP_HOME:-/opt/wsi}"
SERVICE_NAME="wsi-app.service"
# Fixed by install.sh (it refuses any other value), like APP_HOME.
APP_USER="${APP_USER:-wsi}"
# Read only to locate the SQLite DB for the pre-update snapshot. Overridable
# (with APP_USER) solely so infra/tests/test_update_rollback.sh can run this
# script against a scratch tree; install.sh still refuses non-default values.
WSI_ENV_FILE="${WSI_ENV_FILE:-/etc/wsi/wsi.env}"
HEALTH_URL_LOCAL="http://127.0.0.1:8000/api/health"
HEALTH_URL_NGINX="http://127.0.0.1/api/health"

# ---- Pretty progress output -----------------------------------------
if [[ -t 1 ]]; then
    C_BLUE=$'\033[1;34m'; C_GREEN=$'\033[1;32m'; C_YELLOW=$'\033[1;33m'
    C_RED=$'\033[1;31m'; C_DIM=$'\033[2m'; C_BOLD=$'\033[1m'; C_OFF=$'\033[0m'
else
    C_BLUE=""; C_GREEN=""; C_YELLOW=""; C_RED=""; C_DIM=""; C_BOLD=""; C_OFF=""
fi

TOTAL_STEPS=6
CURRENT_STEP=0
START_TS=$(date +%s)

step() {
    CURRENT_STEP=$((CURRENT_STEP + 1))
    local elapsed=$(( $(date +%s) - START_TS ))
    printf '\n%s[%d/%d]%s %s%s%s  %s(+%ss)%s\n' \
        "$C_BLUE" "$CURRENT_STEP" "$TOTAL_STEPS" "$C_OFF" \
        "$C_BOLD" "$*" "$C_OFF" \
        "$C_DIM" "$elapsed" "$C_OFF"
}
sub()  { printf '  %s->%s %s\n' "$C_DIM" "$C_OFF" "$*"; }
ok()   { printf '  %s✓%s %s\n'  "$C_GREEN" "$C_OFF" "$*"; }
warn() { printf '  %s!%s %s\n'  "$C_YELLOW" "$C_OFF" "$*"; }
# die() rolls back before exiting. A bare `exit 1` does NOT trigger the ERR
# trap, so the explicit die calls on the post-install failure paths (health
# check / wsi-sync startup) would otherwise leave the freshly-installed-but-
# broken tree in place with no rollback. _rollback_if_warranted is defined
# below (before the first die call) and is idempotent, so this is safe even
# for the precondition die's that run before the tree is touched.
die()  {
    printf '\n%s[error]%s %s\n' "$C_RED" "$C_OFF" "$*" >&2 || true
    if [[ "$FAILURE_HANDLING" != "1" ]]; then
        FAILURE_HANDLING=1
        _rollback_if_warranted || true
    fi
    exit 1
}

# Captured during step 1 to record the HEAD to roll back to. Stays empty
# when SKIP_GIT_PULL=1 or the source dir is not a git checkout, in which
# case rollback is a no-op (nothing to revert).
ROLLBACK_SHA=""
# ...and what HEAD itself was at that moment: the branch it was on
# (refs/heads/<name>), or empty for a detached HEAD. The rollback puts HEAD
# back on exactly that before resetting, so it never moves the branch step 1
# switched to (see reset_source_tree).
ROLLBACK_HEAD_REF=""
ROLLBACK_DONE=0
# Set to 1 the moment the working tree is moved (checkout / pull). Even if
# the failure happens later inside step 1 — before step 2's `step` call
# increments CURRENT_STEP — rollback is still required, because the tree
# is no longer at ROLLBACK_SHA. Without this flag, on_error's
# `CURRENT_STEP -ge 2` check would leave the operator with a moved tree
# and no automatic recovery.
TREE_MOVED=0
# Set to 1 immediately before step 3 runs install.sh — the first moment
# anything under $APP_HOME / /etc can have changed. A failure before that
# (e.g. the step-2 DB snapshot, or an interrupt while stopping the services)
# has deployed nothing, so the rollback only has to put the source tree back
# and start the services again; re-running the previous install.sh there
# would be a slow, network-dependent no-op that can itself fail.
DEPLOY_STARTED=0
# Set to 1 by whichever failure handler (ERR trap, die, or a signal) runs
# first. A second failure or signal arriving while it is still rolling back /
# restarting must not start a second, nested recovery on top of it.
FAILURE_HANDLING=0

# Run a command as the service user, from / so a cwd the service user cannot
# read (typically the invoking developer's home) doesn't trip anything up.
# Everything that touches the DB goes through this: the DB and its directory
# belong to that user, so doing the file work as root would risk leaving
# root-owned -wal/-shm sidecars the app then cannot open, and would follow any
# symlink the service user had planted there with root's privileges.
as_app_user() { (cd / && sudo -u "$APP_USER" -- "$@"); }

# ---- Pre-update DB snapshot -------------------------------------------
# The new release stamps its version into app_meta on first boot, and
# app_meta.assert_not_downgrade makes the previous release refuse to boot
# against a DB stamped by a newer one (it would otherwise DROP the newer
# columns). So once step 4 has started the new release, rolling back the CODE
# alone can never bring the previous release back up: the DB has to go back
# too. Step 2 snapshots it (services stopped, so it is quiescent) and
# attempt_rollback restores it when the new release was started.
DB_PATH=""
DB_SNAPSHOT_DIR=""
# Set immediately before step 4 starts the new release — the first moment it
# can have touched the DB. Before that the DB is untouched and a rollback
# leaves it alone.
NEW_SERVICES_STARTED=0

# Resolve the SQLite file from wsi.env's DATABASE_URL (last assignment wins,
# as for systemd's EnvironmentFile=), defaulting like the app does. Only an
# absolute sqlite:////path is supported; anything else returns 1. A CR or
# trailing blank in the file would otherwise mean a silently skipped
# snapshot; lib/wsi-common.sh strips them.
DEFAULT_DB_PATH="/var/lib/wsi/wsi.db"
resolve_db_path() { wsi_sqlite_db_path "$WSI_ENV_FILE" "$DEFAULT_DB_PATH"; }

# Snapshot the DB into a fresh private directory next to it. Uses SQLite's
# online-backup API (via the system python3, not the venv install.sh is about
# to rebuild), so a WAL left behind by an unclean stop is folded into the
# copy, then quick_check's the copy. Returns non-zero on any failure.
snapshot_db() {
    if [[ "${WSI_SKIP_DB_SNAPSHOT:-0}" == "1" ]]; then
        warn "WSI_SKIP_DB_SNAPSHOT=1: no DB snapshot; a rollback cannot restore the DB."
        return 0
    fi
    if ! DB_PATH="$(resolve_db_path)"; then
        DB_PATH=""
        warn "DATABASE_URL in $WSI_ENV_FILE is not an absolute sqlite:////path;"
        warn "no DB snapshot, so a rollback cannot restore the DB."
        return 0
    fi
    if [[ ! -f "$DB_PATH" ]]; then
        # A warning, not a progress line: on anything but a host that has
        # never started the app this means the DB was not found where
        # DATABASE_URL says, and the update is about to proceed with no way
        # to roll the DB back.
        warn "No database at $DB_PATH; nothing to snapshot (expected only before the app's first start)."
        if [[ "$DB_PATH" != "$DEFAULT_DB_PATH" && -f "$DEFAULT_DB_PATH" ]]; then
            warn "But $DEFAULT_DB_PATH exists: check DATABASE_URL in $WSI_ENV_FILE."
            warn "Without a snapshot, a rollback cannot restore the DB."
        fi
        DB_PATH=""
        return 0
    fi
    DB_SNAPSHOT_DIR="$(as_app_user mktemp -d "$(dirname "$DB_PATH")/.wsi-update-snapshot.XXXXXX")" \
        || { DB_SNAPSHOT_DIR=""; return 1; }
    # -I (isolated): no PYTHON* variables, user site or cwd on sys.path, as
    # for every python3 the infra scripts run.
    as_app_user python3 -I - "$DB_PATH" "$DB_SNAPSHOT_DIR/wsi.db" <<'PY' || { discard_db_snapshot; return 1; }
import sqlite3, sys
src_path, dst_path = sys.argv[1], sys.argv[2]
src = sqlite3.connect(src_path)
dst = sqlite3.connect(dst_path)
try:
    src.backup(dst)
    result = dst.execute("PRAGMA quick_check").fetchone()[0]
finally:
    dst.close()
    src.close()
if result != "ok":
    sys.exit(f"snapshot failed quick_check: {result}")
PY
    ok "Snapshotted $DB_PATH -> $DB_SNAPSHOT_DIR/wsi.db"
}

# Put the snapshot back, again through the backup API: copying INTO the live
# file goes through SQLite's own journalling, so a -wal the new release left
# behind can never be replayed on top of the restored pages (as it could if
# the file were simply copied over). Only when the new release was started —
# otherwise the DB was never touched. Returns non-zero on failure.
restore_db_snapshot() {
    [[ -n "$DB_SNAPSHOT_DIR" && "$NEW_SERVICES_STARTED" == "1" ]] || return 0
    sub "Restoring the pre-update database snapshot ..."
    as_app_user python3 -I - "$DB_SNAPSHOT_DIR/wsi.db" "$DB_PATH" <<'PY' || return 1
import sqlite3, sys
snap_path, db_path = sys.argv[1], sys.argv[2]
snap = sqlite3.connect(snap_path)
db = sqlite3.connect(db_path)
try:
    snap.backup(db)
    result = db.execute("PRAGMA quick_check").fetchone()[0]
finally:
    db.close()
    snap.close()
if result != "ok":
    sys.exit(f"restored DB failed quick_check: {result}")
PY
    ok "Restored $DB_PATH from the pre-update snapshot."
}

discard_db_snapshot() {
    [[ -n "$DB_SNAPSHOT_DIR" ]] || return 0
    as_app_user rm -rf -- "$DB_SNAPSHOT_DIR" \
        || warn "Could not remove $DB_SNAPSHOT_DIR; delete it by hand."
    DB_SNAPSHOT_DIR=""
}

# Poll the backend's health endpoint. Returns 0 when healthy, 1 as soon as
# the unit enters the failed state, 2 on timeout. Prints a progress dot per
# second; the caller ends the line.
wait_backend_healthy() {
    local deadline=$(( $(date +%s) + $1 ))
    while [[ $(date +%s) -lt $deadline ]]; do
        systemctl is-failed --quiet "$SERVICE_NAME" && return 1
        curl -fsS --max-time 2 "$HEALTH_URL_LOCAL" >/dev/null 2>&1 && return 0
        printf '    %s.%s' "$C_DIM" "$C_OFF"
        sleep 1
    done
    return 2
}

# (Re)start both services after a failed update and report — truthfully —
# whether the backend actually came back. Every rollback / no-rollback exit
# path ends here, so no failure branch can leave the site stopped, and none
# claims a recovery that a health probe has not confirmed.
#   $1: what is now deployed, for the messages.
# Returns 0 only when the backend answered its health check.
bring_services_back() {
    local what="$1" rc=0
    sub "Starting the services on $what ..."
    if ! { systemctl daemon-reload && systemctl restart "$SERVICE_NAME"; }; then
        warn "Could not restart $SERVICE_NAME; check journalctl -u $SERVICE_NAME."
        rc=1
    fi
    # wsi-sync is not PartOf=wsi-app (see step 2), so it needs its own start.
    systemctl restart wsi-sync.service \
        || warn "Could not restart wsi-sync.service; check journalctl -u wsi-sync.service."
    [[ "$rc" -eq 0 ]] || return 1

    local timeout="${WSI_HEALTH_TIMEOUT:-30}"
    sub "Waiting for $SERVICE_NAME to come up (timeout ${timeout}s) ..."
    wait_backend_healthy "$timeout" || rc=$?
    printf '\n'
    if [[ "$rc" -eq 0 ]]; then
        ok "$SERVICE_NAME is healthy on $what."
        return 0
    fi
    warn "$SERVICE_NAME did NOT come up on $what. Recent logs:"
    journalctl -u "$SERVICE_NAME" -n 30 --no-pager 2>/dev/null | sed 's/^/    /' >&2 || true
    return 1
}

# Last-resort restart for the paths where no rollback is possible.
#
# Step 2 stops both services before the file swap, so every exit path after it
# must either restart them or hand the operator a hard-down service. Without
# this, the "cannot roll back" branches below would return before reaching
# any restart: a step-3 failure (e.g. install.sh dying on an unreachable npm
# registry) with SKIP_GIT_PULL=1, or a non-git source dir, would leave a
# working deployment stopped until someone noticed and ran systemctl by hand.
#
# Note what this does NOT claim: install.sh rsyncs REPO_DIR over $APP_HOME
# fairly early, so by the time it fails the deployed tree may already be the
# new code (with, say, a venv that never got the new deps). There is no way to
# know which side of that line the tree is on, and with no ROLLBACK_SHA no way
# to put it back either. Starting is still the right move: a stopped service is
# unambiguously down, whereas a started one is at worst equally broken and at
# best a full recovery (the common case — npm/pip failing after a successful
# rsync leaves a perfectly bootable backend). The failure is reported either
# way, so this never hides a bad update; it only avoids adding an outage to it.
# The DB is deliberately NOT restored here: whatever is deployed is at least as
# new as the release that last opened it.
#   $1 (optional): why there is no rollback, for the message.
restart_services_in_place() {
    local why="${1:-no rollback is possible}"
    if [[ "$CURRENT_STEP" -lt 2 ]]; then return 0; fi
    sub "Update failed and $why; starting the services back up ..."
    bring_services_back "the tree as currently deployed" && return 0
    warn "MANUAL RECOVERY REQUIRED: check sudo journalctl -u $SERVICE_NAME -n 80 --no-pager"
    return 1
}

# Printed whenever a rollback did not end with a healthy backend. Where the
# pre-update DB snapshot is kept (if one is) is printed by
# _rollback_if_warranted, right after this.
rollback_failed_hint() {
    warn "MANUAL RECOVERY REQUIRED: the update failed and the rollback did not"
    warn "bring $SERVICE_NAME back. Check: sudo journalctl -u $SERVICE_NAME -n 80 --no-pager"
    warn "If the log says the DB was last opened by a newer app version, the"
    warn "database must go back to its pre-update state (the snapshot named below,"
    warn "or a backup taken before the update) before the previous release can start."
}

# Put the source checkout back as step 1 found it: HEAD on the same branch
# (or detached), at ROLLBACK_SHA. Returns non-zero, after saying why, when it
# could not. Callers have checked ROLLBACK_SHA and $REPO_DIR/.git.
#
# HEAD is restored FIRST, then reset. Step 1 may have switched branches (to
# GIT_REF=<branch>, or to the default branch for WSI_TRACK_BRANCH=1) or
# detached HEAD (a tag), and a bare `git reset --hard ROLLBACK_SHA` there
# would move whatever is checked out: from branch feat with GIT_REF=main, a
# failed update would leave local main pointing at feat's commit (and HEAD on
# main). `symbolic-ref` / `update-ref --no-deref` only repoint HEAD, so the
# reset then moves the branch the operator started on — a no-op unless step
# 1 itself fast-forwarded that branch, which it undoes — or the detached
# HEAD. A different branch step 1 fast-forwarded to origin is left there: a
# fast-forward loses nothing, and the next update would do it again.
reset_source_tree() {
    local head_desc="detached HEAD"
    [[ -n "$ROLLBACK_HEAD_REF" ]] && head_desc="branch ${ROLLBACK_HEAD_REF#refs/heads/}"
    # Loud stderr warning so the operator sees this even if their terminal
    # has scrolled past earlier output. `git reset --hard` is destructive
    # and will discard any uncommitted local changes in REPO_DIR — set
    # WSI_SKIP_ROLLBACK=1 to opt out and inspect the failed state by hand.
    printf '\n%s==> ROLLBACK%s %sgit reset --hard%s will run on %s.\n' \
        "$C_YELLOW" "$C_OFF" "$C_BOLD" "$C_OFF" "$REPO_DIR" >&2
    printf '    Target SHA: %s (%s)\n' "$ROLLBACK_SHA" "$head_desc" >&2
    printf '    Any uncommitted local changes in %s WILL BE DISCARDED.\n' "$REPO_DIR" >&2
    printf '    To skip rollback (leave the working tree at the failed state):\n' >&2
    printf '        sudo WSI_SKIP_ROLLBACK=1 bash infra/update.sh\n\n' >&2
    pushd "$REPO_DIR" >/dev/null || return 1
    # Use run_git (defined in step 1 by wsi_define_run_git), not a bare
    # `git`: update.sh runs as root while REPO_DIR is normally owned by the
    # developer who cloned it, so a plain `git reset` here dies with
    # "fatal: detected dubious ownership" — the very failure every other git
    # call in this script defuses with `-c safe.directory='*'`. run_git also runs as the
    # invoking user, so the reset doesn't litter .git with root-owned
    # objects. ROLLBACK_SHA is only ever non-empty when step 1's else-branch
    # ran (which defines run_git), so the helper is always in scope here.
    # Capture stderr so a failed reset reports *why* instead of an
    # opaque ">/dev/null"-swallowed "manual recovery required".
    local reset_err
    local -a head_cmd=(update-ref --no-deref HEAD "$ROLLBACK_SHA")
    [[ -n "$ROLLBACK_HEAD_REF" ]] && head_cmd=(symbolic-ref HEAD "$ROLLBACK_HEAD_REF")
    if ! reset_err="$(run_git "${head_cmd[@]}" 2>&1)"; then
        warn "Could not put HEAD back on the $head_desc; the tree was NOT rolled back."
        [[ -n "$reset_err" ]] && warn "  git: $reset_err"
        popd >/dev/null
        return 1
    fi
    if ! reset_err="$(run_git reset --hard "$ROLLBACK_SHA" 2>&1)"; then
        warn "git reset --hard $ROLLBACK_SHA failed; the tree was NOT rolled back."
        [[ -n "$reset_err" ]] && warn "  git: $reset_err"
        popd >/dev/null
        return 1
    fi
    popd >/dev/null
}

attempt_rollback() {
    if [[ "$ROLLBACK_DONE" == "1" ]]; then return 0; fi
    ROLLBACK_DONE=1
    # Past step 4 the new release may be running — or crash-looping under the
    # unit's Restart= policy — and holding the DB. Stop both services before
    # touching the DB or the tree; every branch below starts them again.
    if [[ "$CURRENT_STEP" -ge 2 ]]; then
        systemctl stop "$SERVICE_NAME" wsi-sync.service 2>/dev/null || true
    fi
    # Nothing deployed yet (failure / interrupt before step 3 ran install.sh):
    # $APP_HOME, the units and the DB are exactly as the previous release left
    # them, so there is nothing to reinstall. Put the source tree back if
    # step 1 moved it, and start the services step 2 stopped.
    if [[ "$DEPLOY_STARTED" != "1" ]]; then
        if [[ "$TREE_MOVED" == "1" && -n "$ROLLBACK_SHA" && -d "$REPO_DIR/.git" ]]; then
            reset_source_tree \
                || warn "The source tree stays at the new ref; the deployment itself was not touched."
        fi
        [[ "$CURRENT_STEP" -ge 2 ]] || return 0
        if bring_services_back "the unchanged previous deployment"; then
            ok "Nothing had been deployed yet; the previous release is serving again."
            discard_db_snapshot
            return 0
        fi
        warn "MANUAL RECOVERY REQUIRED: check sudo journalctl -u $SERVICE_NAME -n 80 --no-pager"
        return 1
    fi
    if [[ -z "$ROLLBACK_SHA" ]]; then
        warn "No rollback SHA captured; cannot restore a previous tree."
        restart_services_in_place
        return
    fi
    if [[ ! -d "$REPO_DIR/.git" ]]; then
        warn "Source dir is not a git checkout; cannot roll back."
        restart_services_in_place
        return
    fi
    if ! reset_source_tree; then
        # The deployed code is still the new release, which matches the DB it
        # stamped, so leave the DB alone and bring that back up.
        bring_services_back "the updated (NOT rolled back) tree" || rollback_failed_hint
        return 1
    fi
    # Restore the DB before the previous release can try to open it. On
    # failure, carry on: the services must still be started, and the health
    # check below reports the outcome.
    if ! restore_db_snapshot; then
        warn "Restoring the DB snapshot failed; the previous release will likely refuse to start."
    fi
    sub "Re-running install.sh against the previous tree ..."
    # The rollback above (git reset --hard $ROLLBACK_SHA) already pinned
    # the tree, so don't let install.sh re-checkout to the latest tag.
    # WSI_SKIP_RESTART_HEALTH=1 mirrors the main update path: install.sh
    # does the file work, this function owns the restart. Without it, a
    # rollback would spend ~90s in install.sh's restart+health phase and
    # then run another systemctl restart right after — the same doubled
    # restart the main path avoids.
    #
    # Capture stdout+stderr to a tmpfile rather than dropping them.
    # A `>/dev/null 2>&1` here would make a rollback-install failure
    # invisible: the operator would see "install.sh failed during rollback;
    # service may be in an inconsistent state" with no diagnostics
    # and have to re-run install.sh by hand to see what broke.
    local rollback_log
    rollback_log="$(mktemp -t wsi-rollback-install.XXXXXX.log)"
    if ! WSI_SKIP_TAG_CHECKOUT=1 WSI_SKIP_RESTART_HEALTH=1 \
            bash "$INSTALL_SCRIPT" >"$rollback_log" 2>&1; then
        warn "install.sh failed during rollback; the deployed tree may be a mix of both releases."
        warn "Last 40 lines of rollback install.sh output (full log: $rollback_log):"
        tail -n 40 "$rollback_log" 2>/dev/null | sed 's/^/    /' >&2 || true
        # Still start the services: a stopped site is certainly down, and the
        # common failure (npm/pip after a successful rsync) leaves a bootable
        # backend. The health check says which it is.
        bring_services_back "the partially rolled-back tree" || rollback_failed_hint
        return 1
    fi
    rm -f "$rollback_log"
    if bring_services_back "the previous release ($ROLLBACK_SHA)"; then
        ok "Rolled back to $ROLLBACK_SHA; the previous release is serving again."
        discard_db_snapshot
        return 0
    fi
    rollback_failed_hint
    return 1
}

# Shared rollback decision used by on_error (ERR trap), on_signal and die.
# Roll back whenever step 2 or later started OR the working tree has already
# been moved by step 1. The latter covers failures between `git checkout` /
# `git pull` and the start of step 2 (e.g. the subsequent `rev-parse`,
# `git diff --stat`, or `popd`), where CURRENT_STEP is still 1 but the tree no
# longer matches ROLLBACK_SHA.
#
# WSI_SKIP_ROLLBACK=1 skips the rollback — the source tree and the deployed
# tree stay as the failure left them, for inspection — but not the restart:
# step 2 stopped the services, and inspecting a failed update needs the
# files and the journal, not a site that stays down. So the services are
# started again on whatever is deployed, exactly as when no rollback is
# possible (restart_services_in_place), and the DB snapshot is kept. Stop
# them by hand if you want them down while you look.
_rollback_if_warranted() {
    if [[ ( "$CURRENT_STEP" -ge 2 || "$TREE_MOVED" == "1" ) \
          && "${WSI_SKIP_ROLLBACK:-0}" != "1" ]]; then
        attempt_rollback || true
    elif [[ "${WSI_SKIP_ROLLBACK:-0}" == "1" ]]; then
        warn "WSI_SKIP_ROLLBACK=1: leaving the working tree at the failed state."
        restart_services_in_place "the rollback is skipped (WSI_SKIP_ROLLBACK=1)" || true
    fi
    # Every path that ends healthy with the snapshot no longer needed (a
    # completed rollback, or nothing deployed yet) has discarded it already.
    # One still set here is being KEPT — after a restart in place (no rollback
    # possible), a failed git reset, a failed rollback install.sh, an
    # unhealthy rollback, or WSI_SKIP_ROLLBACK=1 — because it may be the only
    # way back to the pre-update DB. Never keep one silently: say where it is
    # and how to drop it.
    if [[ -n "$DB_SNAPSHOT_DIR" ]]; then
        warn "The pre-update DB snapshot is kept at $DB_SNAPSHOT_DIR/wsi.db."
        warn "Delete it once it is no longer needed: sudo rm -rf $DB_SNAPSHOT_DIR"
    fi
}

on_error() {
    local rc=$?
    local lineno=$1
    # With set -E the trap also runs inside $(...) subshells; there, only
    # exit: the main shell fails the assignment and handles it, once.
    [[ "$BASHPID" == "$$" ]] || exit "$rc"
    printf '\n%s[error]%s Update failed at step %d/%d (line %d, exit %d).\n' \
        "$C_RED" "$C_OFF" "$CURRENT_STEP" "$TOTAL_STEPS" "$lineno" "$rc" >&2 || true
    if [[ "$FAILURE_HANDLING" != "1" ]]; then
        FAILURE_HANDLING=1
        _rollback_if_warranted || true
    fi
    exit "$rc"
}

# Ctrl-C, a closed SSH session (SIGHUP) or a `kill` must not leave the site
# down either: step 2 stopped the services, and without these traps bash's
# default action ends the script right there — no rollback, no restart. Route
# them into the same recovery as a failed step.
#
# The recovery itself must survive a second Ctrl-C. While it runs, a further
# signal only prints a note and returns (FAILURE_HANDLING guard), so the
# handler carries on to bring_services_back. The traps are real handlers, not
# `trap ''` (ignore): an ignored signal would be inherited by every child, so a
# hung rollback install.sh could not be interrupted at all. With a handler,
# children start with the default disposition, so a second Ctrl-C still stops
# whatever command the recovery is waiting on (typically the rollback's
# install.sh) — which the recovery treats as that step failing, and moves on to
# starting the services.
#
# Note that bash runs a trap only once the foreground command it is waiting
# on has exited: a SIGTERM sent to this script alone (not its process group)
# takes effect after, e.g., the running install.sh finishes. The `|| true`s
# keep `set -e` from aborting the recovery half-way, e.g. on a write to a
# terminal that has already hung up.
on_signal() {
    local sig="$1" rc="$2"
    if [[ "$FAILURE_HANDLING" == "1" ]]; then
        printf '\n%s[warn]%s SIG%s received during recovery; continuing — the services are still started before exiting.\n' \
            "$C_YELLOW" "$C_OFF" "$sig" >&2 || true
        return 0
    fi
    FAILURE_HANDLING=1
    printf '\n%s[error]%s Update interrupted by SIG%s at step %d/%d.\n' \
        "$C_RED" "$C_OFF" "$sig" "$CURRENT_STEP" "$TOTAL_STEPS" >&2 || true
    _rollback_if_warranted || true
    exit "$rc"
}

trap 'on_error ${LINENO}' ERR
trap 'on_signal INT 130' INT
trap 'on_signal HUP 129' HUP
trap 'on_signal TERM 143' TERM
# The reader of this script's output went away (`update.sh | tee log` and the SSH
# session dropped, taking tee with it): the next write would kill this script
# by SIGPIPE half-way through, the services left stopped. Move the output to a
# file first — every write in the recovery would raise SIGPIPE again — then
# recover as for any other signal. A handler, not `trap ''`, for the reason
# above: children start with the default disposition.
WSI_UPDATE_RECOVERY_LOG="${WSI_UPDATE_RECOVERY_LOG:-/var/log/wsi-update-recovery.log}"
trap 'exec >>"$WSI_UPDATE_RECOVERY_LOG" 2>&1 || exec >/dev/null 2>&1; on_signal PIPE 141' PIPE

# A restore staged in the dashboard (its request file) or an interrupted one
# (its journal) is carried out by wsi-app's next start, before uvicorn — which
# this update performs, with the NEW release: the stage was checked and its
# schema trial run by the old one. And should the update then fail its health
# check, the rollback puts the pre-update snapshot of wsi.db back over a
# wsi/ / assets/ / thumbs/ the restore already replaced, leaving a mix no
# journal records. So settle it first (as sync-from.sh does). Before anything
# is fetched, stopped or deployed. WSI_ALLOW_PENDING_RESTORE=1 skips the check.
check_no_restore_pending() {
    local req journal data_dir pending=0
    [[ "${WSI_ALLOW_PENDING_RESTORE:-0}" == "1" ]] && return 0
    req="$(wsi_env_value_trimmed RESTORE_REQUEST_PATH "$WSI_ENV_FILE" 2>/dev/null || true)"
    req="${req:-/run/wsi/restore.request}"
    data_dir="$(wsi_env_value_trimmed DATA_DIR "$WSI_ENV_FILE" 2>/dev/null || true)"
    data_dir="${data_dir:-/var/lib/wsi}"
    journal="$(wsi_env_value_trimmed RESTORE_JOURNAL_PATH "$WSI_ENV_FILE" 2>/dev/null || true)"
    journal="${journal:-$data_dir/.restore-apply.journal}"
    if [[ -e "$req" || -L "$req" ]]; then
        printf 'error: a restore / reset is waiting for wsi-app to restart (%s).\n' "$req" >&2
        printf '       Restart wsi-app first (sudo systemctl restart wsi-app), which applies it\n' >&2
        printf '       (or drops it, if over 10 minutes old); check the dashboard, then update.\n' >&2
        pending=1
    fi
    if [[ -e "$journal" || -L "$journal" ]]; then
        printf 'error: an interrupted restore is recorded on this server (%s): wsi-app\n' "$journal" >&2
        printf '       reconciles it when it next starts. Start it (sudo systemctl start wsi-app),\n' >&2
        printf '       check the dashboard, then update.\n' >&2
        pending=1
    fi
    [[ "$pending" == "0" ]] || die "Not updating (above). Nothing was changed."
}

# ---- Preconditions --------------------------------------------------
if [[ $EUID -ne 0 ]]; then
    die "Please run as root (sudo bash infra/update.sh)"
fi
[[ -x "$INSTALL_SCRIPT" || -f "$INSTALL_SCRIPT" ]] \
    || die "install.sh not found at $INSTALL_SCRIPT"
check_no_restore_pending

printf '%s==> Tessera updater%s\n' "$C_BOLD" "$C_OFF"
printf '    repo:      %s\n' "$REPO_DIR"
printf '    app home:  %s\n' "$APP_HOME"
printf '    service:   %s\n' "$SERVICE_NAME"

# ---- 1. git pull ----------------------------------------------------
step "Fetching latest code"
if [[ "${SKIP_GIT_PULL:-0}" == "1" ]]; then
    warn "SKIP_GIT_PULL=1: leaving working tree as-is."
elif [[ ! -d "$REPO_DIR/.git" ]]; then
    warn "$REPO_DIR is not a git checkout; skipping git pull."
else
    # Run git as the invoking (non-root) user so their git credentials
    # (credential helper, `gh auth`, ssh keys) are used. Root usually has
    # none, so running git as root against a private fork or mirror would
    # prompt for a password.
    #
    # GIT_AS_USER=<name>  overrides the detected user.
    # GIT_AS_USER=root    forces running git as root (skip drop).
    # `-c safe.directory='*'` (see lib/wsi-common.sh) defuses git's
    # "dubious ownership" guard: update.sh runs as root, but REPO_DIR is
    # normally owned by the developer who cloned it.
    wsi_define_run_git "$REPO_DIR"
    if [[ "$GIT_RUNNER_USER" == "root" ]]; then
        sub "Running git as: root"
    else
        sub "Running git as: $GIT_RUNNER_USER (HOME=$GIT_RUNNER_HOME)"
    fi

    # move_tree GIT_ARGS... : run_git GIT_ARGS (the checkout / pull that moves
    # the tree). git refuses to move over local changes to a file the target
    # changes, and its advice — "commit your changes or stash them" — is a
    # developer's. On a server clone such a change is an operator's edit of a
    # tracked file, above all infra/nginx/wsi-locations.conf's
    # client_max_body_size (which QUICKSTART §6.5 says not to edit); it has
    # to be undone and the supported setting used instead, so say exactly
    # that, naming the files. Nothing has been stopped or deployed at this
    # point. Any other failure (network, unknown ref) goes to the ERR trap,
    # and so does one after an earlier step already moved the tree: the
    # rollback's `git reset --hard` then drops the changes itself.
    move_tree() {
        run_git "$@" && return 0
        local dirty
        [[ "$TREE_MOVED" != "1" ]] || return 1
        dirty="$(run_git diff --name-only HEAD 2>/dev/null || true)"
        [[ -n "$dirty" ]] || return 1
        die "git $* failed in $REPO_DIR (git's message is above). The clone has local changes to tracked files,
which git refuses to overwrite when the update changes them:
    ${dirty//$'\n'/$'\n'    }
Nothing was stopped or deployed. Undo the changes, then re-run update.sh:
    git -C $REPO_DIR checkout -- ${dirty//$'\n'/ }
Settings that belong outside the clone, and survive every update:
  - nginx upload limit (client_max_body_size in infra/nginx/wsi-locations.conf):
    set WSI_MAX_UPLOAD_BYTES in $WSI_ENV_FILE (QUICKSTART §6.5);
  - nginx cache size (max_size in infra/nginx/wsi.conf): sudo bash infra/nginx-cache.sh;
  - access log (infra/nginx/wsi.conf): edit /etc/nginx/snippets/wsi-access-log.conf;
  - TLS (listen 443, certificate, HSTS): /etc/nginx/snippets/wsi-tls.conf."
    }

    pushd "$REPO_DIR" >/dev/null
    BEFORE_SHA=$(run_git rev-parse --short HEAD)
    # Full SHA is what `git reset --hard` wants if rollback is needed.
    # Captured BEFORE any checkout so rollback works even from detached HEAD.
    ROLLBACK_SHA=$(run_git rev-parse HEAD)
    ROLLBACK_HEAD_REF="$(run_git symbolic-ref -q HEAD || true)"
    sub "Current HEAD: $BEFORE_SHA ($(run_git rev-parse --abbrev-ref HEAD))"

    # Always refresh tags so latest-tag detection sees the current remote.
    # Tolerate a transient fetch failure (e.g. brief network blip): if the
    # ref the operator wants is already local — typical for re-runs and
    # for GIT_REF=<existing-tag> — the subsequent checkout / pull still
    # succeeds. A genuinely missing ref will surface as a clear checkout
    # error a few lines down rather than as an opaque "fetch failed" exit
    # here. install.sh's tag-checkout block uses the same fallback, so both
    # tolerate a single network blip.
    run_git fetch --tags --prune origin \
        || warn "git fetch failed; using local refs."

    # What gets deployed is run as root. Over http:// or git:// the tag list and
    # the objects can be rewritten by anyone on the path, so a "release" they
    # supply would be run as root too (lib/wsi-common.sh; install.sh makes the
    # same check before its own tag checkout).
    ORIGIN_WARNING="$(wsi_origin_transport_warning)"
    [[ -z "$ORIGIN_WARNING" ]] || warn "$ORIGIN_WARNING"

    TARGET_REF=""
    if [[ -n "${GIT_REF:-}" ]]; then
        TARGET_REF="$GIT_REF"
        sub "Using GIT_REF override: $TARGET_REF"
    elif [[ "${WSI_TRACK_BRANCH:-0}" == "1" ]]; then
        [[ "${WSI_VERIFY_RELEASE_TAGS:-0}" != "1" ]] \
            || die "WSI_VERIFY_RELEASE_TAGS=1 cannot be combined with WSI_TRACK_BRANCH=1: a branch has no signature to check. Nothing was stopped or changed."
        sub "WSI_TRACK_BRANCH=1: staying on current branch HEAD."
        # A previous tag update left a detached HEAD, which `git pull` below
        # cannot fast-forward ("You are not currently on a branch"). Return
        # to the remote's default branch first.
        if ! run_git symbolic-ref -q HEAD >/dev/null 2>&1; then
            TRACK_BRANCH_NAME=$(run_git symbolic-ref --short refs/remotes/origin/HEAD 2>/dev/null \
                                 | sed 's#^origin/##' || true)
            if [[ -z "$TRACK_BRANCH_NAME" ]]; then
                # origin/HEAD is unset on clones made without it; fall back to
                # the conventional default branch names.
                for _cand in main master; do
                    if run_git show-ref --verify --quiet "refs/remotes/origin/$_cand"; then
                        TRACK_BRANCH_NAME="$_cand"
                        break
                    fi
                done
            fi
            TRACK_BRANCH_NAME="${TRACK_BRANCH_NAME:-main}"
            sub "Detached HEAD; switching to branch $TRACK_BRANCH_NAME before pulling."
            move_tree checkout "$TRACK_BRANCH_NAME"
            TREE_MOVED=1
        fi
    else
        # The highest FINAL vX.Y.Z tag (lib/wsi-common.sh): a pre-release
        # such as v2.0.0-rc1 is never picked, whatever it sorts above; deploy
        # one deliberately with GIT_REF=<tag>.
        LATEST_TAG="$(wsi_latest_release_tag)"
        if [[ -n "$LATEST_TAG" ]]; then
            TARGET_REF="$LATEST_TAG"
            sub "Latest release tag detected: $TARGET_REF"
        else
            [[ "${WSI_VERIFY_RELEASE_TAGS:-0}" != "1" ]] \
                || die "WSI_VERIFY_RELEASE_TAGS=1: no vX.Y.Z release tag found to verify, and a branch cannot be. Nothing was stopped or changed."
            warn "No vX.Y.Z release tags found; falling back to branch HEAD."
        fi
    fi

    if [[ -n "$TARGET_REF" ]]; then
        # WSI_VERIFY_RELEASE_TAGS=1: only a tag with a valid signature (see
        # lib/wsi-common.sh) is checked out, because the release it names is run
        # as root. Before the tree moves, so a refusal changes nothing.
        wsi_verify_release_tag "$TARGET_REF" || die "Refusing to deploy $TARGET_REF (see above). Nothing was stopped or changed."
        sub "Checking out: $TARGET_REF"
        move_tree checkout "$TARGET_REF"
        TREE_MOVED=1
        # A tag (detached HEAD) already pins the tree. A *branch* name only
        # checks out the local branch, which can be far behind origin (a clone
        # that has only ever updated by tag keeps its clone-time main), so
        # fast-forward it to what the remote has.
        if run_git show-ref --verify --quiet "refs/heads/$TARGET_REF" \
           && run_git show-ref --verify --quiet "refs/remotes/origin/$TARGET_REF"; then
            sub "Fast-forwarding branch $TARGET_REF to origin/$TARGET_REF"
            run_git merge --ff-only "origin/$TARGET_REF"
        fi
    else
        sub "Running: git pull --ff-only"
        move_tree pull --ff-only
        TREE_MOVED=1
    fi

    AFTER_SHA=$(run_git rev-parse --short HEAD)
    if [[ "$BEFORE_SHA" == "$AFTER_SHA" ]]; then
        ok "Already up to date at $AFTER_SHA."
    else
        ok "Updated $BEFORE_SHA -> $AFTER_SHA"
        sub "Changed files:"
        run_git --no-pager diff --stat "$BEFORE_SHA..$AFTER_SHA" | sed 's/^/    /'
    fi
    popd >/dev/null
fi

# nginx TLS that install.sh cannot move into /etc/nginx/snippets/wsi-tls.conf
# (it is not certbot's, or sits in a second server block) stops its nginx step.
# By then the services are down, and the failure is rolled back by re-running the
# PREVIOUS release's install.sh, which replaces the site file without asking:
# the refusal would protect nothing. Ask now, with the new release's own
# extractor, before anything is stopped (a failure here only resets the tree).
# WSI_NGINX_SITE is overridable solely so infra/tests can aim it at a scratch
# file; install.sh keeps the real path.
WSI_NGINX_SITE="${WSI_NGINX_SITE:-/etc/nginx/sites-available/wsi}"
if [[ -f "$WSI_NGINX_SITE" && -f "$REPO_DIR/infra/lib/nginx_tls_carry.py" ]]; then
    tls_rc=0
    # (stderr, which names the lines it cannot attribute, is kept; stdout is not wanted)
    tls_why="$(python3 -I "$REPO_DIR/infra/lib/nginx_tls_carry.py" "$WSI_NGINX_SITE" 2>&1 >/dev/null)" || tls_rc=$?
    # A crash stops install.sh whatever WSI_NGINX_DISCARD_SITE_TLS says: only
    # the refusal (exit 3) is what that variable overrides.
    if [[ "$tls_rc" != 0 && "$tls_rc" != 3 ]]; then
        die "Could not check $WSI_NGINX_SITE for TLS settings (nginx_tls_carry.py exited $tls_rc${tls_why:+: ${tls_why//$'\n'/ }}); install.sh would stop on it after the services were down. Nothing was stopped or changed."
    fi
    if [[ "$tls_rc" == 3 && "${WSI_NGINX_DISCARD_SITE_TLS:-}" != 1 ]]; then
        die "$WSI_NGINX_SITE has TLS settings that install.sh cannot carry over to /etc/nginx/snippets/wsi-tls.conf (they are not certbot's, or are in a second server block or outside any); replacing the file would switch HTTPS off. ${tls_why//$'\n'/ } Move the settings of this site into that snippet (the stock server block includes it and updates never overwrite it; it takes effect when the new site file is installed, so update straight away), and keep any other server block (another domain, another port) in a site file of its own; or run with WSI_NGINX_DISCARD_SITE_TLS=1 to replace the file anyway. Nothing was stopped or changed."
    fi
fi

# ---- 2. Stop the app services before swapping files ----------------
step "Stopping $SERVICE_NAME (if running)"
if systemctl is-active --quiet "$SERVICE_NAME"; then
    systemctl stop "$SERVICE_NAME"
    ok "Stopped $SERVICE_NAME."
else
    sub "$SERVICE_NAME is not active; nothing to stop."
fi
# wsi-sync.service is deliberately NOT PartOf=wsi-app.service (see the unit
# file: PartOf would cascade every wsi-app restart into the single-worker
# sync process and wipe every live-sync room). So stopping wsi-app above does
# NOT take it down — stop it explicitly here, and the rollback/restart paths
# must likewise bring it back up on their own.
if systemctl is-active --quiet wsi-sync.service; then
    systemctl stop wsi-sync.service
    ok "Stopped wsi-sync.service."
fi
# Again now that the app is down, as sync-from.sh does: a restore requested
# while step 1 ran would otherwise be carried out by step 4's start, under the
# new release (a same-version redeploy passes the boot's version check), and
# a failed health check would then put the pre-update wsi.db over it. The
# failure path resets the source tree and restarts the services, which
# applies (or drops) that request under the release it was made with.
check_no_restore_pending
# Now that nothing holds the DB, snapshot it for the rollback (see
# snapshot_db above). A failure aborts the update: without a snapshot, a
# failure after step 4 could not be rolled back at all. Nothing has been
# deployed yet at this point, so that abort only resets the source tree and
# starts the services again (see DEPLOY_STARTED).
snapshot_db \
    || die "Could not snapshot the database (set WSI_SKIP_DB_SNAPSHOT=1 to update without one)."

# ---- 3. Re-run install.sh (idempotent) ------------------------------
step "Running install.sh (rsync, pip, npm build, configs)"
sub "Streaming output from $INSTALL_SCRIPT ..."
# Prefix each install.sh line so the user can see it's nested output.
# `stdbuf -oL` keeps the stream line-buffered for live progress -- but only
# when it actually works. Ubuntu 26.04 (and other rust-coreutils / uutils
# systems) ship a `stdbuf` whose companion libstdbuf.so is missing or cannot
# be preloaded; it then exports a broken LD_PRELOAD that every child process
# inherits, so each exec during the install spews:
#   ERROR: ld.so: object '/usr/libexec/rust-coreutils/libstdbuf.so' from
#   LD_PRELOAD cannot be preloaded (cannot open shared object file): ignored.
# The commands still run ("ignored"), but the install log is drowned in one
# such line per process and looks like a hard failure. `command -v stdbuf`
# cannot tell the good build from the broken one -- the binary is present
# either way -- so probe that a trivial `stdbuf` run stays silent on stderr,
# and fall back to an unbuffered (but clean) stream when it does not.
# WSI_SKIP_TAG_CHECKOUT=1 tells install.sh that the tree was already moved
# (step 1 above), so it must not re-run fetch + checkout.
# WSI_SKIP_RESTART_HEALTH=1 tells install.sh to skip its own service
# restart + health-check phase; step 4/5 below own that responsibility.
# Without it, install.sh restarts wsi-app and waits up to ~90s for it to
# come up, only for update.sh to restart again and wait another ~45s
# right after. Everything else stays install.sh's: it installs the units and
# runs `systemctl daemon-reload`, and installs the nginx config, checks it
# with `nginx -t` and reloads (or starts) nginx.
stdbuf_usable() {
    command -v stdbuf >/dev/null 2>&1 || return 1
    # A broken build prints the ld.so preload error here; a working stdbuf is
    # silent. Capture stderr (discard stdout) and require it to be empty.
    local probe_err
    probe_err="$(stdbuf -oL true 2>&1 >/dev/null)" && [[ -z "$probe_err" ]]
}

# From here on install.sh may have changed $APP_HOME / the installed configs,
# so a failure needs the full rollback (reset + previous install.sh).
DEPLOY_STARTED=1
# A failing install.sh fails the pipeline (pipefail), and the ERR trap turns
# that into "Update failed at step 3/6 (line N, exit <install.sh's status>)"
# plus the rollback, like any other failed step. (An ERR trap fires even
# under `set +e`, so a `set +e` / PIPESTATUS / die wrapper here would never
# get as far as its die.)
if stdbuf_usable; then
    WSI_SKIP_TAG_CHECKOUT=1 WSI_SKIP_RESTART_HEALTH=1 \
        stdbuf -oL bash "$INSTALL_SCRIPT" 2>&1 | sed "s/^/    ${C_DIM}|${C_OFF} /"
else
    WSI_SKIP_TAG_CHECKOUT=1 WSI_SKIP_RESTART_HEALTH=1 \
        bash "$INSTALL_SCRIPT" 2>&1 | sed "s/^/    ${C_DIM}|${C_OFF} /"
fi
ok "install.sh finished."

# Version stamping ($APP_HOME/VERSION) is owned entirely by install.sh.

# ---- 4. Start the services --------------------------------------------
# install.sh (step 3) already ran `systemctl daemon-reload` for the units it
# installed and reloaded nginx after `nginx -t`; only the two app services,
# stopped in step 2, are left to start.
step "Starting services"
if [[ "${SKIP_RESTART:-0}" == "1" ]]; then
    warn "SKIP_RESTART=1: not starting wsi-app / wsi-sync (nginx was reloaded by install.sh)."
    warn "wsi-app and wsi-sync were stopped in step 2 and remain STOPPED —"
    warn "the site is down until you run: systemctl start wsi-app wsi-sync"
else
    # From here on the new release may stamp / migrate the DB, so a rollback
    # must restore the snapshot.
    NEW_SERVICES_STARTED=1
    sub "systemctl enable --now $SERVICE_NAME"
    systemctl enable --now "$SERVICE_NAME"

    sub "systemctl enable --now wsi-sync.service"
    systemctl enable --now wsi-sync.service
    ok "$SERVICE_NAME and wsi-sync.service started."
fi

# ---- 5. Health check ------------------------------------------------
step "Health check"
if [[ "${SKIP_RESTART:-0}" == "1" ]]; then
    warn "Skipped (SKIP_RESTART=1)."
else
    # Wall-clock based: a `for i in $(seq 1 30); ... sleep 1` loop
    # only bounds the iteration count. Each iteration is sleep 1 +
    # curl --max-time 2, so a hanging /api/health could blow the bound
    # up to ~3x the documented value.
    APP_HEALTH_TIMEOUT="${WSI_HEALTH_TIMEOUT:-30}"
    sub "Waiting for $SERVICE_NAME to come up (timeout ${APP_HEALTH_TIMEOUT}s) ..."
    # wait_backend_healthy returns early (1) when the unit has already
    # failed, instead of running out the whole wait.
    HEALTH_RC=0
    wait_backend_healthy "$APP_HEALTH_TIMEOUT" || HEALTH_RC=$?
    printf '\n'
    if [[ "$HEALTH_RC" -eq 1 ]]; then
        warn "$SERVICE_NAME is in failed state. Recent logs:"
        journalctl -u "$SERVICE_NAME" -n 40 --no-pager | sed 's/^/    /' || true
        die "Service failed during startup."
    fi

    if [[ "$HEALTH_RC" -eq 0 ]]; then
        ok "Backend healthy at $HEALTH_URL_LOCAL"
        if curl -fsS --max-time 3 "$HEALTH_URL_NGINX" >/dev/null 2>&1; then
            ok "nginx -> backend healthy at $HEALTH_URL_NGINX"
        else
            warn "nginx health probe failed ($HEALTH_URL_NGINX)."
        fi
    else
        warn "Backend did not respond in ${APP_HEALTH_TIMEOUT}s. Recent logs:"
        journalctl -u "$SERVICE_NAME" -n 30 --no-pager | sed 's/^/    /' || true
        die "Health check failed."
    fi

    # Live-sync coordinator (port 8001). A timeout fails the update like the
    # backend's does: past this step the snapshot is discarded, so a release
    # whose sync worker never answers could no longer be rolled back. Same
    # default budget as the backend's (it boots the same app).
    SYNC_HEALTH_TIMEOUT="${WSI_SYNC_HEALTH_TIMEOUT:-30}"
    sub "Probing wsi-sync coordinator (timeout ${SYNC_HEALTH_TIMEOUT}s) ..."
    SYNC_HEALTH_OK=0
    SYNC_DEADLINE=$(( $(date +%s) + SYNC_HEALTH_TIMEOUT ))
    while [[ $(date +%s) -lt $SYNC_DEADLINE ]]; do
        if systemctl is-failed --quiet wsi-sync.service; then
            warn "wsi-sync.service is in failed state. Recent logs:"
            journalctl -u wsi-sync.service -n 40 --no-pager | sed 's/^/    /' || true
            die "wsi-sync failed during startup."
        fi
        if curl -fsS --max-time 2 http://127.0.0.1:8001/api/health >/dev/null 2>&1; then
            SYNC_HEALTH_OK=1
            break
        fi
        sleep 1
    done
    if [[ "$SYNC_HEALTH_OK" -eq 1 ]]; then
        ok "wsi-sync healthy at http://127.0.0.1:8001/api/health"
    else
        warn "wsi-sync did not respond in ${SYNC_HEALTH_TIMEOUT}s. Recent logs:"
        journalctl -u wsi-sync.service -n 30 --no-pager | sed 's/^/    /' || true
        die "wsi-sync health check failed."
    fi
fi

# ---- 6. Summary -----------------------------------------------------
# The update has succeeded; nothing from here on may roll a healthy release
# back — not a Ctrl-C while the summary prints, and not a failed write of it
# (a full disk under the log, a reader that went away): the snapshot is
# discarded below, so such a rollback would start the previous release on a
# database the new one stamped, which it refuses. Drop the ERR trap and
# set -e with the signal handlers, and ignore SIGPIPE: a write that fails
# now only loses the summary.
trap - ERR INT HUP TERM
trap '' PIPE
set +e
step "Done"
# The update is healthy, so the pre-update DB snapshot has served its purpose.
# With SKIP_RESTART=1 nothing has proven the new release boots yet, so keep it.
if [[ "${SKIP_RESTART:-0}" == "1" && -n "$DB_SNAPSHOT_DIR" ]]; then
    warn "Pre-update DB snapshot kept at $DB_SNAPSHOT_DIR/wsi.db (SKIP_RESTART=1);"
    warn "delete it once the new release is confirmed running."
else
    discard_db_snapshot
fi
TOTAL_ELAPSED=$(( $(date +%s) - START_TS ))
printf '\n%s================================================================%s\n' "$C_BOLD" "$C_OFF"
printf '%s Update complete in %ss.%s\n' "$C_GREEN" "$TOTAL_ELAPSED" "$C_OFF"
printf '\n'
printf '   Service status:  sudo systemctl status %s\n' "$SERVICE_NAME"
printf '   Service logs:    sudo journalctl -u %s -f\n'   "$SERVICE_NAME"
printf '   Sync service:    sudo systemctl status wsi-sync.service\n'
printf '   Sync logs:       sudo journalctl -u wsi-sync -f\n'
printf '   nginx logs:      sudo tail -f /var/log/nginx/error.log\n'
printf '%s================================================================%s\n' "$C_BOLD" "$C_OFF"
exit 0
