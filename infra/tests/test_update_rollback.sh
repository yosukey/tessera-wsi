#!/usr/bin/env bash
# End-to-end test of infra/update.sh's automatic rollback, run against a
# scratch git repo with systemctl / curl / journalctl / sudo stubbed out.
# Run (as root): sudo bash infra/tests/test_update_rollback.sh
#
# WHY THIS EXISTS
# ---------------
# Once the new release has started, reinstalling the previous one is not
# enough to roll back: the new release stamps its version into app_meta on
# first boot and the previous release refuses to boot against a DB stamped by
# a newer one (app_meta.assert_not_downgrade). update.sh must also check that
# the rollback really came up before it prints "Rolled back ... and
# restarted", and must start the services even when the rollback's own
# install.sh fails.
#
# The fake "app" started by the systemctl stub reproduces exactly the part of
# the real boot that matters: refuse when app_meta.app_version is newer than
# the deployed version, otherwise stamp it and (unless the release is marked
# broken) become healthy. The real update.sh (and lib/wsi-common.sh, which it
# sources) is copied into the scratch repo unchanged, so what runs is the
# shipped script.
#
# Later cases cover the edges: Ctrl-C / SIGTERM mid-update (and a second
# Ctrl-C during the rollback) must still end with the services started; a
# CRLF / trailing-blank DATABASE_URL must not silently skip the snapshot; a
# failure before install.sh ran must not re-run the previous install.sh; a
# kept snapshot's path is always printed; and a local edit that blocks the
# checkout is named, with the command that undoes it.
#
# Needs root (update.sh refuses to run otherwise), git and python3; SKIPs
# (exit 0) without them.
set -uo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
UPDATE_SH="${HERE}/../update.sh"

skip() { printf 'SKIP - %s\n' "$1"; exit 0; }
[[ ${EUID:-$(id -u)} -eq 0 ]] || skip "must run as root (update.sh refuses otherwise)"
command -v git >/dev/null 2>&1 || skip "git not available"
command -v python3 >/dev/null 2>&1 || skip "python3 not available"

fails=0
pass() { printf 'ok   - %s\n' "$1"; }
fail() { printf 'FAIL - %s\n' "$1"; fails=$(( fails + 1 )); }

TMP_ROOT="$(mktemp -d)"
trap 'rm -rf "$TMP_ROOT"' EXIT

STUBS="$TMP_ROOT/stubs"
mkdir -p "$STUBS"

# sudo -u USER [--] [VAR=val ...] cmd ...  ->  run cmd (the test already runs as root).
cat > "$STUBS/sudo" <<'SH'
#!/usr/bin/env bash
while [[ $# -gt 0 ]]; do
    case "$1" in
        -u) shift 2 ;;
        --) shift; break ;;
        -*) shift ;;
        *) break ;;
    esac
done
# Test hook: make the step-2 snapshot fail at its first command.
[[ "${1:-}" == mktemp && -f "$FAKE_STATE/fail_snapshot" ]] && exit 1
exec env "$@"
SH

# The fake app: what a real boot does to app_meta, in miniature.
cat > "$STUBS/fake-app.py" <<'PY'
import os, sqlite3, sys
state_dir = os.environ["FAKE_STATE"]
deployed = open(os.path.join(state_dir, "deployed_version")).read().strip()
db = sqlite3.connect(os.path.join(state_dir, "wsi.db"))
db.execute("CREATE TABLE IF NOT EXISTS app_meta (key TEXT PRIMARY KEY, value TEXT)")
row = db.execute("SELECT value FROM app_meta WHERE key='app_version'").fetchone()
def vt(v): return tuple(int(x) for x in v.split("."))
if row and vt(row[0]) > vt(deployed):
    status = f"failed: DB was last opened by app version {row[0]!r}, newer than {deployed!r}"
else:
    db.execute("INSERT OR REPLACE INTO app_meta VALUES ('app_version', ?)", (deployed,))
    db.commit()
    broken = os.path.exists(os.path.join(state_dir, f"broken_{deployed}"))
    status = "failed: release is broken" if broken else "healthy"
db.close()
open(os.path.join(state_dir, "app_state"), "w").write(status)
PY

cat > "$STUBS/systemctl" <<'SH'
#!/usr/bin/env bash
S="$FAKE_STATE"
echo "systemctl $*" >> "$S/calls"
state() { cat "$S/app_state" 2>/dev/null; }
verb="$1"; shift
[[ "${1:-}" == "--quiet" || "${1:-}" == "--now" ]] && shift
case "$verb" in
    is-active) [[ "$1" == wsi-app.service ]] && [[ "$(state)" == healthy ]]; exit ;;
    is-failed) [[ "$1" == wsi-app.service ]] && [[ "$(state)" == failed* ]]; exit ;;
    stop)      for u in "$@"; do [[ "$u" == wsi-app.service ]] && echo stopped > "$S/app_state"; done; exit 0 ;;
    restart|enable|start)
        if [[ "$1" == wsi-app.service ]]; then python3 "$(dirname "$0")/fake-app.py"; fi
        exit 0 ;;
    *) exit 0 ;;
esac
SH

cat > "$STUBS/curl" <<'SH'
#!/usr/bin/env bash
for a in "$@"; do
    case "$a" in
        *127.0.0.1:8000/*) [[ "$(cat "$FAKE_STATE/app_state" 2>/dev/null)" == healthy ]]; exit ;;
        # A sync worker that hangs on boot: running (never is-failed) but
        # never answering, for the release named by sync_hung_<version>.
        *127.0.0.1:8001/*) [[ ! -f "$FAKE_STATE/sync_hung_$(cat "$FAKE_STATE/deployed_version")" ]]; exit ;;
        http*) exit 0 ;;
    esac
done
exit 0
SH

cat > "$STUBS/journalctl" <<'SH'
#!/usr/bin/env bash
cat "$FAKE_STATE/app_state" 2>/dev/null; echo
SH
chmod +x "$STUBS"/*

# One scratch repo per scenario: v1.0.0 deployed and running, v2.0.0 to update to.
setup() {
    local name="$1"
    CASE="$TMP_ROOT/$name"
    REPO="$CASE/repo"
    STATE="$CASE/state"
    mkdir -p "$REPO/infra/lib" "$STATE"
    cp "$UPDATE_SH" "$REPO/infra/update.sh"
    cp "${HERE}/../lib/wsi-common.sh" "$REPO/infra/lib/wsi-common.sh"
    cp "${HERE}/../lib/nginx_tls_carry.py" "$REPO/infra/lib/nginx_tls_carry.py"
    # Fake install.sh: "deploys" the tree's version; fails on request.
    cat > "$REPO/infra/install.sh" <<'SH'
#!/usr/bin/env bash
v="$(cat "$(dirname "$0")/../app_version.txt")"
echo "$v" >> "$FAKE_STATE/install_runs"
# Interrupt the updater mid-install (one-shot). INT is a terminal Ctrl-C: it
# reaches the whole foreground process group (update.sh, this install, the
# `sed` it is piped into), which run_update gives update.sh to itself via
# setsid. TERM is a `kill` aimed at update.sh alone; this install completes.
if [[ -f "$FAKE_STATE/signal_install_$v" ]]; then
    sig="$(cat "$FAKE_STATE/signal_install_$v")"
    rm -f "$FAKE_STATE/signal_install_$v"
    if [[ "$sig" == INT ]]; then kill -s INT 0; else kill -s "$sig" "$PPID"; fi
    sleep 1
fi
if [[ -f "$FAKE_STATE/fail_install_$v" ]]; then
    echo "install.sh: simulated failure (network?) for $v" >&2
    exit 1
fi
printf '%s\n' "$v" > "$FAKE_STATE/deployed_version"
SH
    local g=(git -C "$REPO" -c user.email=t@t -c user.name=t)
    "${g[@]}" init -q
    echo 1.0.0 > "$REPO/app_version.txt"
    "${g[@]}" add -A && "${g[@]}" commit -qm v1 && "${g[@]}" tag v1.0.0
    echo 2.0.0 > "$REPO/app_version.txt"
    "${g[@]}" commit -qam v2 && "${g[@]}" tag v2.0.0
    "${g[@]}" checkout -q v1.0.0
    printf 'DATABASE_URL=sqlite:///%s/wsi.db\n' "$STATE" > "$STATE/wsi.env"
    # v1 deployed and running: DB stamped 1.0.0, plus a row of "user data".
    echo 1.0.0 > "$STATE/deployed_version"
    FAKE_STATE="$STATE" python3 "$STUBS/fake-app.py"
    python3 - "$STATE/wsi.db" <<'PY'
import sqlite3, sys
db = sqlite3.connect(sys.argv[1])
db.execute("PRAGMA journal_mode=WAL")
db.execute("CREATE TABLE slides (name TEXT)")
db.execute("INSERT INTO slides VALUES ('pre-update slide')")
db.commit()
PY
}

# In its own session / process group (when setsid exists), so a simulated
# Ctrl-C (`kill -INT 0` from the fake install.sh) hits update.sh and its
# children exactly like a terminal one, without hitting this test.
SETSID=()
command -v setsid >/dev/null 2>&1 && SETSID=(setsid -w)
# A signal ignored when a shell starts can never be trapped by it, and an
# ignored SIGINT is inherited — from a non-interactive parent that started
# this test in the background, or from a CI runner. Start update.sh with
# SIGINT back at its default, so the Ctrl-C cases test update.sh rather than
# how this test happened to be launched (they would SKIP otherwise).
DEFAULT_INT=(python3 -I -c 'import os, signal, sys
signal.signal(signal.SIGINT, signal.SIG_DFL)
os.execvp(sys.argv[1], sys.argv[1:])')
run_update() {
    # Run the update against the case's repo; captures combined output + rc.
    (cd "$REPO" && "${DEFAULT_INT[@]}" "${SETSID[@]}" env -u SUDO_USER \
        PATH="$STUBS:$PATH" FAKE_STATE="$STATE" TMPDIR="$CASE" \
        APP_USER=root WSI_ENV_FILE="$STATE/wsi.env" \
        WSI_NGINX_SITE="$CASE/no-such-nginx-site" \
        GIT_REF=v2.0.0 WSI_HEALTH_TIMEOUT=5 WSI_SYNC_HEALTH_TIMEOUT=2 \
        "$@" bash "$REPO/infra/update.sh") > "$CASE/out.log" 2>&1
    RC=$?
}
db_version() {
    python3 -c 'import sqlite3,sys; print(sqlite3.connect(sys.argv[1]).execute("SELECT value FROM app_meta WHERE key=\"app_version\"").fetchone()[0])' "$STATE/wsi.db"
}
snapshots_left() { find "$STATE" -maxdepth 1 -name '.wsi-update-snapshot.*' | wc -l | tr -d ' '; }
show_log() { sed 's/^/    | /' "$CASE/out.log"; }

# --- A. healthy update: DB stamped by the new release, snapshot removed -----
setup healthy
run_update
if [[ "$RC" -eq 0 && "$(cat "$STATE/app_state")" == healthy && "$(db_version)" == 2.0.0 \
      && "$(snapshots_left)" == 0 ]]; then
    pass "healthy update: exit 0, v2 serving, snapshot cleaned up"
else
    fail "healthy update: rc=$RC state=$(cat "$STATE/app_state") db=$(db_version) snapshots=$(snapshots_left)"
    show_log
fi

# --- B. broken new release: rollback restores the DB and v1 boots again ------
setup rollback
touch "$STATE/broken_2.0.0"
run_update
if [[ "$RC" -ne 0 && "$(cat "$STATE/app_state")" == healthy \
      && "$(cat "$STATE/deployed_version")" == 1.0.0 && "$(db_version)" == 1.0.0 ]] \
   && grep -q "Rolled back to" "$CASE/out.log" \
   && [[ "$(git -C "$REPO" rev-parse HEAD)" == "$(git -C "$REPO" rev-parse v1.0.0)" ]]; then
    pass "broken release: DB snapshot restored, previous release healthy again"
else
    fail "broken release: rc=$RC state=$(cat "$STATE/app_state") deployed=$(cat "$STATE/deployed_version") db=$(db_version)"
    show_log
fi
[[ "$(snapshots_left)" == 0 ]] && pass "snapshot removed after a healthy rollback" \
                               || fail "snapshot left behind after a healthy rollback"
n="$(python3 -c 'import sqlite3,sys; print(sqlite3.connect(sys.argv[1]).execute("SELECT count(*) FROM slides").fetchone()[0])' "$STATE/wsi.db")"
[[ "$n" == 1 ]] && pass "pre-update data intact after the restore" \
                || fail "pre-update data lost: slides rows=$n"

# --- B2. new release's sync worker never answers: rolled back like a backend
# that never answers, not reported as a success with the snapshot discarded ---
setup synchung
touch "$STATE/sync_hung_2.0.0"
run_update
if [[ "$RC" -ne 0 && "$(cat "$STATE/app_state")" == healthy \
      && "$(cat "$STATE/deployed_version")" == 1.0.0 && "$(db_version)" == 1.0.0 ]] \
   && grep -q "wsi-sync health check failed" "$CASE/out.log" \
   && grep -q "Rolled back to" "$CASE/out.log" \
   && ! grep -q "Update complete" "$CASE/out.log"; then
    pass "hung sync worker: update fails and rolls back, DB snapshot restored"
else
    fail "hung sync worker: rc=$RC state=$(cat "$STATE/app_state") deployed=$(cat "$STATE/deployed_version") db=$(db_version)"
    show_log
fi

# --- C. no snapshot: the downgrade guard wins, and the script says so -------
setup nosnapshot
touch "$STATE/broken_2.0.0"
run_update WSI_SKIP_DB_SNAPSHOT=1
if [[ "$RC" -ne 0 && "$(cat "$STATE/app_state")" == failed:\ DB\ was\ last* ]] \
   && grep -q "MANUAL RECOVERY REQUIRED" "$CASE/out.log" \
   && ! grep -q "Rolled back to" "$CASE/out.log"; then
    pass "without a snapshot the failed rollback is reported, not claimed"
else
    fail "no-snapshot rollback: rc=$RC state=$(cat "$STATE/app_state")"
    show_log
fi

# --- D. rollback's own install.sh fails: services are still started --------
setup rollbackfail
touch "$STATE/broken_2.0.0" "$STATE/fail_install_1.0.0"
run_update
last_restart="$(grep -n 'restart wsi-app.service' "$STATE/calls" | tail -n1 | cut -d: -f1)"
last_stop="$(grep -n 'stop .*wsi-app.service' "$STATE/calls" | tail -n1 | cut -d: -f1)"
if [[ "$RC" -ne 0 && -n "$last_restart" && "$last_restart" -gt "${last_stop:-0}" ]] \
   && grep -q "install.sh failed during rollback" "$CASE/out.log" \
   && grep -q "MANUAL RECOVERY REQUIRED" "$CASE/out.log" \
   && [[ "$(snapshots_left)" == 1 ]]; then
    pass "failed rollback install: services restarted, failure reported, snapshot kept"
else
    fail "failed rollback install: rc=$RC restart@${last_restart:-none} stop@${last_stop:-none} snapshots=$(snapshots_left)"
    show_log
fi
grep -qF "snapshot is kept at $STATE/.wsi-update-snapshot." "$CASE/out.log" \
    && pass "failed rollback install: the kept snapshot's path is printed" \
    || { fail "failed rollback install: kept snapshot not reported"; show_log; }

# Helpers for the cases below.
restarted_after_stop() {
    # wsi-app was (re)started after the last time it was stopped.
    local r s
    r="$(grep -n -E '(restart|enable --now|start) wsi-app.service' "$STATE/calls" | tail -n1 | cut -d: -f1)"
    s="$(grep -n 'stop .*wsi-app.service' "$STATE/calls" | tail -n1 | cut -d: -f1)"
    [[ -n "$r" && "$r" -gt "${s:-0}" ]]
}
head_is_v1() { [[ "$(git -C "$REPO" rev-parse HEAD)" == "$(git -C "$REPO" rev-parse v1.0.0)" ]]; }

# --- E. SIGTERM mid-install: rolled back and serving, not left stopped ------
setup sigterm
echo TERM > "$STATE/signal_install_2.0.0"
run_update
if [[ "$RC" -eq 143 && "$(cat "$STATE/app_state")" == healthy \
      && "$(cat "$STATE/deployed_version")" == 1.0.0 && "$(snapshots_left)" == 0 ]] \
   && restarted_after_stop && head_is_v1 \
   && grep -q "interrupted by SIGTERM" "$CASE/out.log"; then
    pass "SIGTERM during install.sh: rolled back, services running, snapshot removed"
else
    fail "SIGTERM during install.sh: rc=$RC state=$(cat "$STATE/app_state") deployed=$(cat "$STATE/deployed_version") snapshots=$(snapshots_left)"
    show_log
fi

# --- F. Ctrl-C mid-install (whole process group): same recovery -------------
if [[ ${#SETSID[@]} -eq 0 ]]; then
    printf 'SKIP - Ctrl-C cases (no setsid to give update.sh its own process group)\n'
else
    setup sigint
    echo INT > "$STATE/signal_install_2.0.0"
    run_update
    if [[ "$RC" -eq 130 && "$(cat "$STATE/app_state")" == healthy \
          && "$(cat "$STATE/deployed_version")" == 1.0.0 ]] \
       && restarted_after_stop && head_is_v1 \
       && grep -q "interrupted by SIGINT" "$CASE/out.log"; then
        pass "Ctrl-C during install.sh: rolled back, services running"
    else
        fail "Ctrl-C during install.sh: rc=$RC state=$(cat "$STATE/app_state") deployed=$(cat "$STATE/deployed_version")"
        show_log
    fi

    # --- G. a second Ctrl-C during the rollback still ends with services up --
    # The first interrupt (TERM) lands during the new install; the second
    # (Ctrl-C) kills the rollback's own install.sh. The recovery must carry
    # on to starting the services rather than die with them stopped.
    setup sigtwice
    echo TERM > "$STATE/signal_install_2.0.0"
    echo INT  > "$STATE/signal_install_1.0.0"
    run_update
    if [[ "$RC" -eq 143 && "$(cat "$STATE/app_state")" == healthy ]] \
       && restarted_after_stop \
       && grep -q "SIGINT received during recovery" "$CASE/out.log" \
       && grep -q "install.sh failed during rollback" "$CASE/out.log" \
       && grep -qF "snapshot is kept at $STATE/.wsi-update-snapshot." "$CASE/out.log"; then
        pass "second Ctrl-C during the rollback: services still started, snapshot path printed"
    else
        fail "second Ctrl-C during the rollback: rc=$RC state=$(cat "$STATE/app_state")"
        show_log
    fi
fi

# --- H. CRLF / trailing blanks on DATABASE_URL: still snapshotted ------------
setup crlf
printf 'DATABASE_URL=sqlite:///%s/wsi.db  \r\n' "$STATE" > "$STATE/wsi.env"
touch "$STATE/broken_2.0.0"
run_update
if [[ "$RC" -ne 0 && "$(cat "$STATE/app_state")" == healthy && "$(db_version)" == 1.0.0 ]] \
   && grep -q "Snapshotted $STATE/wsi.db -> " "$CASE/out.log" \
   && grep -q "Rolled back to" "$CASE/out.log"; then
    pass "CRLF-terminated DATABASE_URL: DB snapshotted and restored"
else
    fail "CRLF DATABASE_URL: rc=$RC state=$(cat "$STATE/app_state") db=$(db_version)"
    show_log
fi

# --- H2. single-quoted DATABASE_URL (systemd strips the quotes): snapshotted -
setup squote
printf "DATABASE_URL='sqlite:///%s/wsi.db'\n" "$STATE" > "$STATE/wsi.env"
touch "$STATE/broken_2.0.0"
run_update
if [[ "$RC" -ne 0 && "$(cat "$STATE/app_state")" == healthy && "$(db_version)" == 1.0.0 ]] \
   && grep -q "Snapshotted $STATE/wsi.db -> " "$CASE/out.log" \
   && grep -q "Rolled back to" "$CASE/out.log"; then
    pass "single-quoted DATABASE_URL: DB snapshotted and restored"
else
    fail "single-quoted DATABASE_URL: rc=$RC state=$(cat "$STATE/app_state") db=$(db_version)"
    show_log
fi

# --- I. snapshot fails at step 2: nothing deployed, so nothing reinstalled ---
setup snapfail
touch "$STATE/fail_snapshot"
run_update
if [[ "$RC" -ne 0 && "$(cat "$STATE/app_state")" == healthy && ! -s "$STATE/install_runs" ]] \
   && restarted_after_stop && head_is_v1 \
   && grep -q "Could not snapshot the database" "$CASE/out.log" \
   && grep -q "Nothing had been deployed yet" "$CASE/out.log"; then
    pass "snapshot failure: tree reset, services restarted, install.sh not re-run"
else
    fail "snapshot failure: rc=$RC state=$(cat "$STATE/app_state") install_runs=$(tr '\n' ' ' < "$STATE/install_runs" 2>/dev/null)"
    show_log
fi

# --- J. no rollback possible (SKIP_GIT_PULL=1): kept snapshot is reported ---
setup inplace
touch "$STATE/fail_install_1.0.0"
run_update SKIP_GIT_PULL=1
if [[ "$RC" -ne 0 && "$(cat "$STATE/app_state")" == healthy && "$(snapshots_left)" == 1 ]] \
   && restarted_after_stop \
   && grep -qF "snapshot is kept at $STATE/.wsi-update-snapshot." "$CASE/out.log"; then
    pass "restart in place: services restarted, kept snapshot's path printed"
else
    fail "restart in place: rc=$RC state=$(cat "$STATE/app_state") snapshots=$(snapshots_left)"
    show_log
fi

# --- J2. WSI_SKIP_ROLLBACK=1: the tree stays for inspection, the site does not
# stay down. Skipping the restart along with the rollback would leave both
# services stopped despite "every failure path starts them again".
setup skiprollback
touch "$STATE/fail_install_2.0.0"
run_update WSI_SKIP_ROLLBACK=1
if [[ "$RC" -ne 0 && "$(cat "$STATE/app_state")" == healthy ]] \
   && restarted_after_stop \
   && [[ "$(git -C "$REPO" rev-parse HEAD)" == "$(git -C "$REPO" rev-parse v2.0.0)" ]] \
   && grep -q "WSI_SKIP_ROLLBACK=1: leaving the working tree" "$CASE/out.log" \
   && grep -qF "snapshot is kept at $STATE/.wsi-update-snapshot." "$CASE/out.log"; then
    pass "WSI_SKIP_ROLLBACK=1: tree left at the new ref, services started again, snapshot kept"
else
    fail "WSI_SKIP_ROLLBACK=1: rc=$RC state=$(cat "$STATE/app_state") head=$(git -C "$REPO" rev-parse --short HEAD)"
    show_log
fi

# --- K. DATABASE_URL pointing nowhere: a visible warning, not a progress line
setup nodb
printf 'DATABASE_URL=sqlite:///%s/missing.db\n' "$STATE" > "$STATE/wsi.env"
run_update
if [[ "$RC" -eq 0 ]] && grep -qE '^  \S* ?No database at .*/missing\.db' "$CASE/out.log" \
   && ! grep -q -- '-> .*No database at' "$CASE/out.log"; then
    pass "missing DB is reported as a warning"
else
    fail "missing DB warning: rc=$RC"
    show_log
fi

# --- L/M. branch updates: origin is ahead of the clone's local branch --------
# A clone that has only ever updated by tag keeps its clone-time local branch;
# GIT_REF=<branch> must still deploy origin's tip, and WSI_TRACK_BRANCH=1 must
# work from the detached HEAD a tag update leaves behind.
setup_origin_ahead() {
    setup "$1"
    local g=(git -C "$REPO" -c user.email=t@t -c user.name=t)
    BRANCH="$("${g[@]}" for-each-ref --format='%(refname:short)' refs/heads | head -n1)"
    git clone -q --bare "$REPO" "$CASE/origin.git"
    "${g[@]}" remote add origin "$CASE/origin.git"
    "${g[@]}" fetch -q origin
    "${g[@]}" branch -q --set-upstream-to="origin/$BRANCH" "$BRANCH"
    git clone -q -b "$BRANCH" "$CASE/origin.git" "$CASE/pusher"
    echo 3.0.0 > "$CASE/pusher/app_version.txt"
    git -C "$CASE/pusher" -c user.email=t@t -c user.name=t commit -qam v3
    git -C "$CASE/pusher" push -q origin "$BRANCH"
    # The local branch is behind origin/$BRANCH (still at v2), HEAD detached at v1.
}

setup_origin_ahead branchref
run_update GIT_REF="$BRANCH"
if [[ "$RC" -eq 0 && "$(cat "$STATE/deployed_version")" == 3.0.0 ]] \
   && [[ "$(git -C "$REPO" rev-parse HEAD)" == "$(git -C "$REPO" rev-parse "origin/$BRANCH")" ]]; then
    pass "GIT_REF=<branch> deploys origin's tip, not a stale local branch"
else
    fail "GIT_REF=<branch>: rc=$RC deployed=$(cat "$STATE/deployed_version") head=$(git -C "$REPO" rev-parse --short HEAD)"
    show_log
fi

setup_origin_ahead trackbranch
run_update GIT_REF= WSI_TRACK_BRANCH=1
if [[ "$RC" -eq 0 && "$(cat "$STATE/deployed_version")" == 3.0.0 ]] \
   && git -C "$REPO" symbolic-ref -q HEAD >/dev/null; then
    pass "WSI_TRACK_BRANCH=1 works from a detached HEAD and ends on a branch"
else
    fail "WSI_TRACK_BRANCH=1 from detached HEAD: rc=$RC deployed=$(cat "$STATE/deployed_version")"
    show_log
fi

# --- N/O/P. the rollback puts HEAD back where it was, not just its commit ---
# A `git reset --hard <old sha>` on whatever step 1 had checked out would move
# THAT branch: from branch feat with GIT_REF=<default branch>, a failed
# update would leave the default branch pointing at feat's commit.
head_ref() { git -C "$REPO" symbolic-ref -q HEAD || echo detached; }
sha_of()   { git -C "$REPO" rev-parse "$1"; }

setup branchback
BRANCH="$(git -C "$REPO" for-each-ref --format='%(refname:short)' refs/heads | head -n1)"
git -C "$REPO" checkout -q -b feat v1.0.0
touch "$STATE/broken_2.0.0"
run_update GIT_REF="$BRANCH"
if [[ "$RC" -ne 0 && "$(cat "$STATE/app_state")" == healthy \
      && "$(head_ref)" == refs/heads/feat && "$(sha_of HEAD)" == "$(sha_of v1.0.0)" \
      && "$(sha_of "$BRANCH")" == "$(sha_of v2.0.0)" \
      && -z "$(git -C "$REPO" status --porcelain --untracked-files=no)" ]]; then
    pass "rollback from GIT_REF=<branch>: back on feat, $BRANCH not moved"
else
    fail "rollback from GIT_REF=<branch>: rc=$RC head=$(head_ref)@$(git -C "$REPO" rev-parse --short HEAD) $BRANCH@$(git -C "$REPO" rev-parse --short "$BRANCH")"
    show_log
fi

setup tagfrombranch
git -C "$REPO" checkout -q -b stable v1.0.0
touch "$STATE/broken_2.0.0"
run_update      # GIT_REF=v2.0.0: a tag, so step 1 detaches HEAD
if [[ "$RC" -ne 0 && "$(head_ref)" == refs/heads/stable \
      && "$(sha_of HEAD)" == "$(sha_of v1.0.0)" ]]; then
    pass "rollback from a tag update: back on the branch it started on, not detached"
else
    fail "rollback from a tag update: rc=$RC head=$(head_ref)@$(git -C "$REPO" rev-parse --short HEAD)"
    show_log
fi

# WSI_TRACK_BRANCH=1 from the detached HEAD a tag update leaves: step 1
# switches to the branch and pulls; the rollback must detach again at the
# old commit, not drag the branch back to it.
setup_origin_ahead trackback
touch "$STATE/broken_3.0.0"
run_update GIT_REF= WSI_TRACK_BRANCH=1
if [[ "$RC" -ne 0 && "$(cat "$STATE/app_state")" == healthy && "$(head_ref)" == detached \
      && "$(sha_of HEAD)" == "$(sha_of v1.0.0)" \
      && "$(sha_of "$BRANCH")" == "$(sha_of "origin/$BRANCH")" ]]; then
    pass "rollback from WSI_TRACK_BRANCH=1: detached at the old commit again, $BRANCH not moved back"
else
    fail "rollback from WSI_TRACK_BRANCH=1: rc=$RC head=$(head_ref)@$(git -C "$REPO" rev-parse --short HEAD) $BRANCH@$(git -C "$REPO" rev-parse --short "$BRANCH")"
    show_log
fi

# --- Q. a local edit of a tracked file the update changes ------------------
# git refuses the checkout. An operator who raises client_max_body_size by
# hand in infra/nginx/wsi-locations.conf (QUICKSTART §6.5 says not to) hits
# exactly this, and git's own advice ("commit or stash") is no help to them: the
# updater names the files and the command that undoes them, and nothing is
# stopped, installed or reset (the edit is the operator's to drop).
setup dirty
echo 1.0.0-edited > "$REPO/app_version.txt"
run_update
if [[ "$RC" -ne 0 && "$(cat "$STATE/app_state")" == healthy && ! -s "$STATE/install_runs" ]] \
   && head_is_v1 && [[ "$(cat "$REPO/app_version.txt")" == 1.0.0-edited ]] \
   && ! grep -q 'stop .*wsi-app.service' "$STATE/calls" 2>/dev/null \
   && grep -q '^    app_version.txt$' "$CASE/out.log" \
   && grep -qF "git -C $REPO checkout -- app_version.txt" "$CASE/out.log" \
   && grep -q 'set WSI_MAX_UPLOAD_BYTES in' "$CASE/out.log"; then
    pass "local edit blocking the checkout: files and the undo command named, nothing touched"
else
    fail "local edit blocking the checkout: rc=$RC state=$(cat "$STATE/app_state") head=$(git -C "$REPO" rev-parse --short HEAD)"
    show_log
fi

# --- R. the summary cannot be written: a healthy update stays healthy -------
# Run with SIGPIPE ignored (as under systemd or a CI runner), and the reader
# of the output gone once step 6 begins: every later write fails with EPIPE.
# The snapshot is discarded at step 6, so a rollback set off by that failed
# write would start v1 on a database v2 had stamped, which it refuses.
setup summary_epipe
(cd "$REPO" && trap '' PIPE && env -u SUDO_USER \
    PATH="$STUBS:$PATH" FAKE_STATE="$STATE" TMPDIR="$CASE" \
    APP_USER=root WSI_ENV_FILE="$STATE/wsi.env" \
    GIT_REF=v2.0.0 WSI_HEALTH_TIMEOUT=5 WSI_SYNC_HEALTH_TIMEOUT=2 \
    WSI_UPDATE_RECOVERY_LOG="$CASE/recovery.log" \
    bash "$REPO/infra/update.sh" 2>"$CASE/err.log"; echo "$?" > "$CASE/rc") \
  | sed '/\[6\/6\]/q' > "$CASE/out.log"
if [[ "$(cat "$CASE/rc")" == 0 && "$(cat "$STATE/app_state")" == healthy \
      && "$(cat "$STATE/deployed_version")" == 2.0.0 && "$(db_version)" == 2.0.0 \
      && "$(snapshots_left)" == 0 ]] && ! grep -q ROLLBACK "$CASE/err.log"; then
    pass "a failed write of the summary does not roll a healthy update back"
else
    fail "summary write failure: rc=$(cat "$CASE/rc") state=$(cat "$STATE/app_state") deployed=$(cat "$STATE/deployed_version") db=$(db_version)"
    sed 's/^/    | /' "$CASE/err.log"
fi

# --- S. the output's reader goes away mid-install (`| tee`, SSH dropped) -----
# SIGPIPE at its default: the next write would kill update.sh outright,
# leaving the services stopped and no rollback, so the recovery runs with
# its output moved to WSI_UPDATE_RECOVERY_LOG. Started with SIGPIPE reset to
# its default, as the Ctrl-C cases do for SIGINT: a CI runner may start this
# test with it ignored, which a shell can never trap (writes then fail with
# EPIPE instead, the case R covers).
DEFAULT_PIPE=(python3 -I -c 'import os, signal, sys
signal.signal(signal.SIGPIPE, signal.SIG_DFL)
os.execvp(sys.argv[1], sys.argv[1:])')
setup reader_gone
(cd "$REPO" && "${DEFAULT_PIPE[@]}" env -u SUDO_USER \
    PATH="$STUBS:$PATH" FAKE_STATE="$STATE" TMPDIR="$CASE" \
    APP_USER=root WSI_ENV_FILE="$STATE/wsi.env" \
    GIT_REF=v2.0.0 WSI_HEALTH_TIMEOUT=5 WSI_SYNC_HEALTH_TIMEOUT=2 \
    WSI_UPDATE_RECOVERY_LOG="$CASE/recovery.log" \
    bash "$REPO/infra/update.sh" 2>&1; echo "$?" > "$CASE/rc") \
  | sed '/Streaming output/q' > "$CASE/out.log"
if [[ "$(cat "$STATE/app_state")" == healthy && "$(cat "$STATE/deployed_version")" == 1.0.0 ]] \
   && head_is_v1 && grep -q "SIGPIPE" "$CASE/recovery.log" \
   && grep -q "Rolled back to" "$CASE/recovery.log"; then
    pass "reader gone mid-update: rolled back and restarted, output kept in the recovery log"
else
    fail "reader gone mid-update: rc=$(cat "$CASE/rc") state=$(cat "$STATE/app_state") deployed=$(cat "$STATE/deployed_version")"
    sed 's/^/    | /' "$CASE/recovery.log" 2>/dev/null
fi

# --- T. nginx TLS install.sh would refuse stops the update BEFORE anything is
#        stopped: install.sh's own refusal comes after the services are down,
#        and the rollback it triggers runs the previous release's install.sh,
#        which replaces the site file without asking.
setup tlsrefuse
printf 'server {\n    listen 443 ssl;\n    ssl_certificate /etc/ssl/mine.pem;\n    ssl_certificate_key /etc/ssl/mine.key;\n}\n' > "$CASE/site"
run_update WSI_NGINX_SITE="$CASE/site"
if [[ "$RC" -ne 0 ]] && grep -q "Nothing was stopped or changed" "$CASE/out.log" \
   && grep -q "cannot carry over" "$CASE/out.log" \
   && grep -q "ssl_certificate /etc/ssl/mine.pem;" "$CASE/out.log" \
   && { [[ ! -e "$STATE/calls" ]] || ! grep -qE 'stop .*wsi-app.service' "$STATE/calls"; } \
   && [[ ! -s "$STATE/install_runs" ]] \
   && [[ "$(cat "$STATE/app_state")" == healthy ]]; then
    pass "unattributable site TLS: refused before the services are stopped or install.sh runs"
else
    fail "unattributable site TLS: rc=$RC calls=$(tr '\n' ';' < "$STATE/calls" 2>/dev/null | cut -c1-200) installs=$(cat "$STATE/install_runs" 2>/dev/null | tr '\n' ' ')"
    show_log
fi
# A check that cannot run (here: the extractor crashes) stops the update before
# anything is stopped too -- install.sh would stop on it after the services were down.
mkdir -p "$CASE/bin"
real_python="$(command -v python3)"
cat > "$CASE/bin/python3" <<SH
#!/bin/sh
case "\$*" in *nginx_tls_carry.py*) echo "Traceback: boom" >&2; exit 2 ;; esac
exec "$real_python" "\$@"
SH
chmod +x "$CASE/bin/python3"
run_update WSI_NGINX_SITE="$CASE/site" PATH="$CASE/bin:$STUBS:$PATH"
if [[ "$RC" -ne 0 ]] && grep -q "Could not check" "$CASE/out.log" && grep -q "Nothing was stopped or changed" "$CASE/out.log" \
   && grep -q "Traceback: boom" "$CASE/out.log" \
   && { [[ ! -e "$STATE/calls" ]] || ! grep -qE 'stop .*wsi-app.service' "$STATE/calls"; }; then
    pass "a TLS check that crashes stops the update before anything is stopped, and says why"
else
    fail "crashing TLS check: rc=$RC"
    show_log
fi
# install.sh dies on a crashed extractor whatever the discard flag says, so the
# pre-flight must too (the flag only overrides the refusal, exit 3).
run_update WSI_NGINX_SITE="$CASE/site" WSI_NGINX_DISCARD_SITE_TLS=1 PATH="$CASE/bin:$STUBS:$PATH"
if [[ "$RC" -ne 0 ]] && grep -q "Could not check" "$CASE/out.log" && grep -q "Nothing was stopped or changed" "$CASE/out.log" \
   && { [[ ! -e "$STATE/calls" ]] || ! grep -qE 'stop .*wsi-app.service' "$STATE/calls"; }; then
    pass "a TLS check that crashes stops the update even with WSI_NGINX_DISCARD_SITE_TLS=1"
else
    fail "crashing TLS check with the discard flag: rc=$RC"
    show_log
fi
run_update WSI_NGINX_SITE="$CASE/site" WSI_NGINX_DISCARD_SITE_TLS=1
if [[ "$RC" -eq 0 && "$(cat "$STATE/app_state")" == healthy && "$(db_version)" == 2.0.0 ]]; then
    pass "...and WSI_NGINX_DISCARD_SITE_TLS=1 lets the update through"
else
    fail "discard flag: rc=$RC state=$(cat "$STATE/app_state")"
    show_log
fi

printf -- '--------------------------------------------------------------\n'
if [[ "$fails" -eq 0 ]]; then
    printf 'PASS - update.sh rollback restores the DB, always restarts, reports truthfully\n'
else
    exit 1
fi
