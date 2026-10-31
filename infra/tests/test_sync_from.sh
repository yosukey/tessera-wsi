#!/usr/bin/env bash
# End-to-end test of infra/sync-from.sh, run against two scratch installs on
# this machine: a "source" (DB + DATA_DIR) and a "destination" (DB +
# DATA_DIR + wsi.env), with ssh, systemctl, curl and journalctl stubbed out.
# ssh runs the remote command locally, so the real rsync and the real
# infra/lib/replica_db.py do the work exactly as they would across two hosts,
# and the real sudo switches to "nobody" as the service user on both sides:
# the data directories are nobody's, the mirror root's, as in production, so
# the permission model (root never writes below DATA_DIR; the service user
# hard-links files it owns, fs.protected_hardlinks) is exercised for real.
# Run (as root): sudo bash infra/tests/test_sync_from.sh
#
# What it pins: a standby sync takes the content and admin accounts and keeps
# the destination's own settings and records; files are hard-linked into
# place and the previous sync's files and attachments survive for --rollback;
# every swap signs everyone out (the app's own restore_apply.py); --dry-run
# changes nothing; a version mismatch or a failed swap leaves the destination
# serving what it served (services started again, database unchanged, every
# slide on a fresh tile generation once files were put in place); --rollback
# puts the previous database (and the wsi.env a --all changed) back; --all
# replaces the database and carries ANALYTICS_HASH_SALT / WSI_MFA_KEY over; the mirror may
# not sit inside DATA_DIR.
#
# Needs root (sync-from.sh refuses otherwise), sudo, a "nobody" account,
# python3, rsync and flock; SKIPs (exit 0) without them.
set -uo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
SYNC_SH="${HERE}/../sync-from.sh"

skip() { printf 'SKIP - %s\n' "$1"; exit 0; }
[[ ${EUID:-$(id -u)} -eq 0 ]] || skip "must run as root (sync-from.sh refuses otherwise)"
for c in python3 rsync flock sudo; do
    command -v "$c" >/dev/null 2>&1 || skip "$c not available"
done
id nobody >/dev/null 2>&1 || skip "no 'nobody' account to act as the service user"
REAL_SUDO="$(command -v sudo)"
APP_USER=nobody
APP_GROUP="$(id -gn nobody)"

fails=0
pass() { printf 'ok   - %s\n' "$1"; }
fail() { printf 'FAIL - %s\n' "$1"; fails=$(( fails + 1 )); }

TMP_ROOT="$(mktemp -d)"
chmod 0755 "$TMP_ROOT"
trap 'rm -rf "$TMP_ROOT"' EXIT
STUBS="$TMP_ROOT/stubs"
mkdir -p "$STUBS"

# The real sudo, with the stubs kept on PATH (its secure_path drops them).
cat > "$STUBS/sudo" <<SH
#!/usr/bin/env bash
opts=()
while [[ \$# -gt 0 ]]; do
    case "\$1" in
        -u) opts+=("\$1" "\$2"); shift 2 ;;
        --) shift; break ;;
        -*) opts+=("\$1"); shift ;;
        *) break ;;
    esac
done
exec $REAL_SUDO "\${opts[@]}" -- env PATH="\$PATH" FAKE_STATE="\$FAKE_STATE" "\$@"
SH

# ssh [opts] host [command...]: run the command here, as a remote sshd would
# through the user's shell. -O (control) and -N (master only) do nothing.
cat > "$STUBS/ssh" <<'SH'
#!/usr/bin/env bash
master=0
while [[ $# -gt 0 ]]; do
    case "$1" in
        -o|-l|-p|-i|-F) shift 2 ;;
        -O) exit 0 ;;
        -N) master=1; shift ;;
        -*) shift ;;
        *) break ;;
    esac
done
host="$1"; shift
echo "ssh $host $*" >> "$FAKE_STATE/ssh_calls"
[[ "$master" == "1" ]] && exit 0
exec bash -c "$*"
SH

cat > "$STUBS/systemctl" <<'SH'
#!/usr/bin/env bash
echo "systemctl $*" >> "$FAKE_STATE/calls"
verb="$1"; shift
[[ "${1:-}" == "--quiet" ]] && shift
case "$verb" in
    is-active) [[ -f "$FAKE_STATE/active.$1" ]]; exit ;;
    is-failed) exit 1 ;;
    stop)      for u in "$@"; do rm -f "$FAKE_STATE/active.$u"; done ;;
    start)     for u in "$@"; do touch "$FAKE_STATE/active.$u"; done ;;
esac
exit 0
SH

cat > "$STUBS/curl" <<'SH'
#!/usr/bin/env bash
[[ -f "$FAKE_STATE/active.wsi-app.service" && ! -f "$FAKE_STATE/unhealthy" ]]
SH
printf '#!/bin/sh\nexit 0\n' > "$STUBS/journalctl"
chmod +x "$STUBS"/*

# The installed app's stdlib-only sign-out, where sync-from.sh looks for it
# (APP_HOME), readable by the service user.
APP_HOME_DIR="$TMP_ROOT/app"
install -D -m 0644 "$HERE/../../services/app/wsi_app/restore_apply.py" \
    "$APP_HOME_DIR/services/app/wsi_app/restore_apply.py"

# A schema with the shape of Tessera's for every table the helper touches.
SCHEMA_SQL="$TMP_ROOT/schema.sql"
cat > "$SCHEMA_SQL" <<'SQL'
CREATE TABLE app_meta (key VARCHAR(64) PRIMARY KEY, value VARCHAR(255));
CREATE TABLE slides (id INTEGER PRIMARY KEY, public_id VARCHAR(32) UNIQUE NOT NULL,
    title VARCHAR(255) DEFAULT '', wsi_filename VARCHAR(512) DEFAULT '',
    thumb_filename VARCHAR(512) DEFAULT '', wsi_generation INTEGER DEFAULT 0,
    is_published BOOLEAN DEFAULT 0);
CREATE TABLE slide_attributes (id INTEGER PRIMARY KEY, slide_id INTEGER REFERENCES slides(id) ON DELETE CASCADE, key TEXT, value TEXT);
CREATE TABLE tours (id INTEGER PRIMARY KEY AUTOINCREMENT, public_id VARCHAR(32) UNIQUE NOT NULL,
    slide_id INTEGER REFERENCES slides(id) ON DELETE CASCADE, title VARCHAR(255),
    description TEXT DEFAULT '');
CREATE TABLE tour_attributes (id INTEGER PRIMARY KEY, tour_id INTEGER REFERENCES tours(id) ON DELETE CASCADE, key TEXT, value TEXT);
CREATE TABLE steps (id INTEGER PRIMARY KEY AUTOINCREMENT, tour_id INTEGER REFERENCES tours(id) ON DELETE CASCADE, label TEXT,
    description_md TEXT DEFAULT '');
CREATE TABLE step_pins (id INTEGER PRIMARY KEY, step_id INTEGER REFERENCES steps(id) ON DELETE CASCADE, label TEXT);
CREATE TABLE step_shapes (id INTEGER PRIMARY KEY, step_id INTEGER REFERENCES steps(id) ON DELETE CASCADE);
CREATE TABLE viewer_settings (id INTEGER PRIMARY KEY, site_title TEXT);
CREATE TABLE seasonal_backgrounds (id INTEGER PRIMARY KEY, label TEXT);
CREATE TABLE metadata_options (id INTEGER PRIMARY KEY, key TEXT, value TEXT);
CREATE TABLE users (id INTEGER PRIMARY KEY, username TEXT UNIQUE, password_hash TEXT,
    token_version INTEGER DEFAULT 0);
CREATE TABLE role_permissions (id INTEGER PRIMARY KEY, manager_can_view_server BOOLEAN);
CREATE TABLE network_settings (id INTEGER PRIMARY KEY, mdns_enabled BOOLEAN, ga_measurement_id TEXT);
CREATE TABLE auth_settings (id INTEGER PRIMARY KEY, mode TEXT, session_generation INTEGER DEFAULT 0);
CREATE TABLE auth_users (id INTEGER PRIMARY KEY, username TEXT);
CREATE TABLE page_views (id INTEGER PRIMARY KEY, kind TEXT, client_ip TEXT);
CREATE TABLE slide_dwell_events (id INTEGER PRIMARY KEY);
CREATE TABLE tour_sessions (id INTEGER PRIMARY KEY);
CREATE TABLE audit_logs (id INTEGER PRIMARY KEY, action TEXT);
CREATE TABLE error_logs (id INTEGER PRIMARY KEY);
CREATE TABLE perf_runs (id INTEGER PRIMARY KEY);
SQL

# sql DB "SELECT ..." prints the rows (columns joined by |); anything else is
# run as a script. As the service user, who owns the databases (a root
# connection could leave root-owned -wal / -shm files behind).
sql() { "$REAL_SUDO" -u "$APP_USER" python3 -I - "$1" "$2" <<'PY'
import sqlite3, sys
conn = sqlite3.connect(sys.argv[1])
if sys.argv[2].lstrip().upper().startswith("SELECT"):
    rows = conn.execute(sys.argv[2]).fetchall()
    print("\n".join("|".join(str(c) for c in r) for r in rows))
else:
    conn.executescript(sys.argv[2])
conn.commit()
conn.close()
PY
}

new_db() {  # path app_version schema_version (as the service user, like sql)
    "$REAL_SUDO" -u "$APP_USER" python3 -I - "$1" "$SCHEMA_SQL" "$2" "$3" <<'PY'
import sqlite3, sys
conn = sqlite3.connect(sys.argv[1])
conn.executescript(open(sys.argv[2]).read())
conn.execute("INSERT INTO app_meta VALUES ('app_version', ?), ('schema_version', ?)",
             (sys.argv[3], sys.argv[4]))
conn.commit()
conn.execute("PRAGMA journal_mode = WAL")
conn.close()
PY
}

# One scenario: a source with two slides (one with a tour attachment) and a
# destination with an old slide of its own, its own settings and records.
setup() {
    CASE="$TMP_ROOT/$1"
    SRC="$CASE/src"; DST="$CASE/dst"
    export FAKE_STATE="$CASE/state"
    mkdir -p "$SRC/wsi" "$SRC/thumbs" "$SRC/assets/tours/tourA" \
             "$DST/wsi" "$DST/thumbs" "$DST/assets/tours" "$FAKE_STATE" "$CASE/etc"
    chown -R "$APP_USER:$APP_GROUP" "$SRC" "$DST"
    new_db "$SRC/wsi.db" "${2:-v1.4.0}" "${3:-7}"
    sql "$SRC/wsi.db" "
        INSERT INTO slides VALUES (1, 'slideA', 'Liver', '1_liver.svs', '1_t.jpg', 100, 1);
        INSERT INTO slides VALUES (2, 'slideB', 'Lung', '2_lung.svs', '2_t.jpg', 200, 1);
        INSERT INTO tours VALUES (1, 'tourA', 1, 'Lobules', '');
        INSERT INTO steps VALUES (1, 1, 'vein', '![vein](/assets/tours/tourA/fig.png)');
        INSERT INTO users VALUES (1, 'admin', 'source-hash', 40);
        INSERT INTO viewer_settings VALUES (1, 'Histology');
        INSERT INTO network_settings VALUES (1, 1, 'G-SOURCE');
        INSERT INTO auth_settings VALUES (1, 'google', 3);
        INSERT INTO page_views VALUES (1, 'viewer', '10.0.0.1');"
    printf 'liver-bytes' > "$SRC/wsi/1_liver.svs"
    printf 'lung-bytes'  > "$SRC/wsi/2_lung.svs"
    printf 't1' > "$SRC/thumbs/1_t.jpg"
    printf 't2' > "$SRC/thumbs/2_t.jpg"
    printf 'png' > "$SRC/assets/tours/tourA/fig.png"
    printf 'JWT_SECRET=source-jwt\nANALYTICS_HASH_SALT=source-salt\nWSI_MFA_KEY=source-mfa-key\n' > "$CASE/etc/src.env"

    new_db "$DST/wsi.db" "v1.4.0" "7"
    sql "$DST/wsi.db" "
        INSERT INTO slides VALUES (9, 'oldSlide', 'Old', '9_old.svs', '9_t.jpg', 50, 1);
        INSERT INTO users VALUES (1, 'backup-admin', 'backup-hash', 900);
        INSERT INTO viewer_settings VALUES (1, 'Backup');
        INSERT INTO network_settings VALUES (1, 0, '');
        INSERT INTO auth_settings VALUES (1, 'none', 5);
        INSERT INTO page_views VALUES (1, 'top', '10.9.9.9');"
    printf 'old-bytes' > "$DST/wsi/9_old.svs"
    printf 't9' > "$DST/thumbs/9_t.jpg"
    printf 'DATABASE_URL=sqlite:///%s\nDATA_DIR=%s\nJWT_SECRET=backup-jwt\nANALYTICS_HASH_SALT=backup-salt\nWSI_MFA_KEY=backup-mfa-key\n' \
        "$DST/wsi.db" "$DST" > "$CASE/etc/wsi.env"
    chown -R "$APP_USER:$APP_GROUP" "$SRC" "$DST"
    chmod 0600 "$CASE/etc/wsi.env"
    touch "$FAKE_STATE/active.wsi-app.service" "$FAKE_STATE/active.wsi-sync.service"
    : > "$FAKE_STATE/calls"
}

# DATA_DIR is passed in, as the tests' scratch paths are not the default,
# unless DATA_DIR_FROM_ENV_FILE is set (then wsi.env's own value is read).
run_sync() {
    OUT="$CASE/out.log"
    local -a data_dir=(DATA_DIR="$DST")
    [[ -z "${DATA_DIR_FROM_ENV_FILE:-}" ]] || data_dir=()
    env PATH="$STUBS:$PATH" APP_USER="$APP_USER" APP_GROUP="$APP_GROUP" SSH_AS_USER=root \
        APP_HOME="${APP_HOME_OVERRIDE:-$APP_HOME_DIR}" WSI_SYNC_LOCK="$CASE/sync.lock" \
        WSI_ENV_FILE="$CASE/etc/wsi.env" "${data_dir[@]}" \
        WSI_REPLICA_DIR="${REPLICA_DIR:-$CASE/replica}" \
        WSI_SOURCE_DB="$SRC/wsi.db" WSI_SOURCE_DATA_DIR="$SRC" \
        WSI_SOURCE_ENV_FILE="$CASE/etc/src.env" WSI_HEALTH_TIMEOUT=3 \
        bash "$SYNC_SH" "$@" </dev/null >"$OUT" 2>&1
    RC=$?
}

services_running() {
    [[ -f "$FAKE_STATE/active.wsi-app.service" && -f "$FAKE_STATE/active.wsi-sync.service" ]]
}

# ---- 1. standby sync ----------------------------------------------------------
setup standby
run_sync --yes backup-src
if [[ "$RC" -eq 0 ]]; then pass "standby sync succeeds"; else fail "standby sync exit $RC"; cat "$OUT"; fi
[[ "$(sql "$DST/wsi.db" "SELECT public_id FROM slides ORDER BY id")" == $'slideA\nslideB' ]] \
    && pass "slides come from the source" || fail "slides: $(sql "$DST/wsi.db" "SELECT public_id FROM slides")"
[[ "$(sql "$DST/wsi.db" "SELECT username, password_hash FROM users")" == "admin|source-hash" ]] \
    && pass "admin accounts come from the source" || fail "users not taken from the source"
[[ "$(sql "$DST/wsi.db" "SELECT site_title FROM viewer_settings")" == "Histology" ]] \
    && pass "public-page settings come from the source" || fail "viewer_settings not taken"
[[ "$(sql "$DST/wsi.db" "SELECT mdns_enabled, ga_measurement_id FROM network_settings")" == "0|" \
   && "$(sql "$DST/wsi.db" "SELECT mode FROM auth_settings")" == "none" ]] \
    && pass "network and sign-in settings stay this server's" || fail "local settings overwritten"
[[ "$(sql "$DST/wsi.db" "SELECT client_ip FROM page_views")" == "10.9.9.9" ]] \
    && pass "access statistics stay this server's" || fail "page_views overwritten"
[[ "$(cat "$DST/wsi/1_liver.svs" 2>/dev/null)" == "liver-bytes" \
   && "$(cat "$DST/assets/tours/tourA/fig.png" 2>/dev/null)" == "png" \
   && "$(cat "$DST/thumbs/2_t.jpg" 2>/dev/null)" == "t2" ]] \
    && pass "WSI, thumbnails and attachments are in place" || fail "files missing in DATA_DIR"
[[ "$(stat -c %i "$DST/wsi/1_liver.svs")" == "$(stat -c %i "$CASE/replica/mirror/wsi/1_liver.svs")" ]] \
    && pass "files are hard-linked from the mirror, not copied" || fail "WSI was copied"
[[ "$(stat -c %U "$DST/wsi/1_liver.svs") $(stat -c %U "$DST/assets/tours/tourA") $(stat -c %U "$DST/wsi.db")" \
   == "$APP_USER $APP_USER $APP_USER" && "$(stat -c %U "$CASE/replica/mirror/assets/tours/tourA")" == root ]] \
    && pass "DATA_DIR stays the service user's, the mirror's directories root's" \
    || fail "ownership: $(stat -c %U "$DST/assets/tours/tourA") / $(stat -c %U "$CASE/replica/mirror/assets/tours/tourA")"
[[ -f "$DST/wsi/9_old.svs" && -f "$DST/wsi.db.pre-sync" ]] \
    && pass "the previous database and its files are kept for --rollback" || fail "pre-sync state lost"
grep -q '^systemctl stop wsi-app.service wsi-sync.service' "$FAKE_STATE/calls" && services_running \
    && pass "services were stopped for the apply and started again" || fail "service handling"
[[ "$(stat -c '%U %a' "$CASE/replica")" == "root 750" ]] \
    && pass "the mirror directory is root's, mode 0750" || fail "mirror perms $(stat -c '%U %a' "$CASE/replica")"
# Past the source's 40 and the 900 this server's own admin (same id) had;
# this server's app-cookie generation (5) moves on too.
[[ "$(sql "$DST/wsi.db" "SELECT token_version FROM users")" == 901 \
   && "$(sql "$DST/wsi.db" "SELECT session_generation FROM auth_settings")" == 6 ]] \
    && pass "the swap signs everyone out of this server" \
    || fail "sign-out: tv $(sql "$DST/wsi.db" "SELECT token_version FROM users"), gen $(sql "$DST/wsi.db" "SELECT session_generation FROM auth_settings")"

# A second sync after the source deleted a slide and an attachment (and the
# link to it): they are dropped from the mirror, and from DATA_DIR one sync
# later (the pre-sync database still refers to them).
sql "$SRC/wsi.db" "DELETE FROM slides WHERE id = 2; UPDATE steps SET description_md = 'gone';"
rm -f "$SRC/wsi/2_lung.svs" "$SRC/thumbs/2_t.jpg" "$SRC/assets/tours/tourA/fig.png"
run_sync --yes backup-src
[[ "$RC" -eq 0 && ! -f "$DST/wsi/9_old.svs" && -f "$DST/wsi/2_lung.svs" \
   && -f "$DST/assets/tours/tourA/fig.png" && ! -f "$CASE/replica/mirror/wsi/2_lung.svs" ]] \
    && pass "files and attachments are swept one sync after the source drops them" \
    || { fail "second sync sweep (rc $RC)"; cat "$OUT"; }
run_sync --yes backup-src
[[ "$RC" -eq 0 && ! -f "$DST/wsi/2_lung.svs" && ! -f "$DST/assets/tours/tourA/fig.png" ]] \
    && pass "...and then removed" || fail "third sync did not sweep 2_lung.svs / fig.png"

# A sync that fails keeps the previous rollback point (and leaves no copy).
before_presync="$(sha256sum < "$DST/wsi.db.pre-sync")"
sql "$SRC/wsi.db" "INSERT INTO tours VALUES (9, 'orphan', 77, 'no slide', '');"
run_sync --yes backup-src
[[ "$RC" -ne 0 && "$(sha256sum < "$DST/wsi.db.pre-sync")" == "$before_presync" && ! -e "$DST/wsi.db.pre-sync.new" ]] \
    && pass "a failed sync keeps the previous rollback point" || { fail "rollback point after a failed sync (rc $RC)"; cat "$OUT"; }
sql "$SRC/wsi.db" "DELETE FROM tours WHERE id = 9;"

# ---- 2. --rollback --------------------------------------------------------------
sql "$SRC/wsi.db" "UPDATE slides SET title = 'Renamed' WHERE id = 1;"
run_sync --yes backup-src
[[ "$(sql "$DST/wsi.db" "SELECT title FROM slides WHERE id = 1")" == "Renamed" ]] || fail "rename not synced"
tv_before="$(sql "$DST/wsi.db" "SELECT token_version FROM users")"
sql "$DST/wsi.db" "INSERT INTO page_views VALUES (2, 'top', '10.7.7.7');"
run_sync --rollback --yes
[[ "$RC" -eq 0 && "$(sql "$DST/wsi.db" "SELECT title FROM slides WHERE id = 1")" == "Liver" ]] && services_running \
    && pass "--rollback puts the previous database back" || { fail "--rollback (rc $RC)"; cat "$OUT"; }
presync_gone=0; [[ ! -e "$DST/wsi.db.pre-sync" ]] && presync_gone=1
# A standby sync's rollback puts back the content only: what this server
# recorded since (statistics) stays.
[[ "$(sql "$DST/wsi.db" "SELECT client_ip FROM page_views ORDER BY id")" == $'10.9.9.9\n10.7.7.7' ]] \
    && pass "--rollback of a standby sync keeps this server's records since the sync" \
    || fail "rollback lost this server's records: $(sql "$DST/wsi.db" "SELECT client_ip FROM page_views")"
pre_rollback="$(sha256sum < "$DST/wsi.db.pre-rollback")"
run_sync --rollback --yes
[[ "$presync_gone" == 1 && "$RC" -ne 0 && "$(sha256sum < "$DST/wsi.db.pre-rollback")" == "$pre_rollback" ]] \
    && pass "a second --rollback is refused, keeping the copy of the synced database" \
    || { fail "second rollback (rc $RC)"; cat "$OUT"; }
[[ "$(sql "$DST/wsi.db" "SELECT wsi_generation FROM slides WHERE id = 1")" -gt 1600000000000 \
   && "$(sql "$DST/wsi.db" "SELECT token_version FROM users")" -gt "$tv_before" ]] \
    && pass "--rollback moves slides to fresh tile generations and signs everyone out" \
    || fail "--rollback generations / sign-out"
grep -q '^WSI_MFA_KEY=backup-mfa-key$' "$CASE/etc/wsi.env" \
    && pass "a standby sync without two-factor accounts leaves WSI_MFA_KEY alone" \
    || { fail "standby sync changed WSI_MFA_KEY"; cat "$CASE/etc/wsi.env"; }

# ---- 2b. standby sync bringing two-factor accounts: WSI_MFA_KEY comes along ----------
setup standby-mfa
for db in "$SRC/wsi.db" "$DST/wsi.db"; do sql "$db" "ALTER TABLE users ADD COLUMN totp_secret_enc TEXT"; done
sql "$SRC/wsi.db" "UPDATE users SET totp_secret_enc = 'v1.sealed'"
run_sync --dry-run backup-src
[[ "$RC" -eq 0 ]] && grep -q "two-factor sign-in: the source's WSI_MFA_KEY goes into" "$OUT" \
   && grep -q '^WSI_MFA_KEY=backup-mfa-key$' "$CASE/etc/wsi.env" \
    && pass "--dry-run says the source's WSI_MFA_KEY would come along, and writes nothing" \
    || { fail "dry run with two-factor accounts (rc $RC)"; cat "$OUT"; }
run_sync --yes backup-src
[[ "$RC" -eq 0 ]] && grep -q '^WSI_MFA_KEY=source-mfa-key$' "$CASE/etc/wsi.env" \
   && grep -q '^ANALYTICS_HASH_SALT=backup-salt$' "$CASE/etc/wsi.env" \
   && grep -q '^JWT_SECRET=backup-jwt$' "$CASE/etc/wsi.env" && [[ "$(stat -c %a "$CASE/etc/wsi.env")" == "600" ]] \
    && pass "a standby sync bringing two-factor accounts carries WSI_MFA_KEY only" \
    || { fail "standby two-factor key (rc $RC)"; cat "$OUT"; cat "$CASE/etc/wsi.env"; }
! grep -q 'source-mfa-key\|source-salt' "$OUT" && pass "...without printing it" || fail "the key reached the output"
printf 'LATER_EDIT=kept\n' >> "$CASE/etc/wsi.env"
run_sync --rollback --yes
[[ "$RC" -eq 0 ]] && grep -q '^WSI_MFA_KEY=backup-mfa-key$' "$CASE/etc/wsi.env" \
   && grep -q '^LATER_EDIT=kept$' "$CASE/etc/wsi.env" && [[ "$(grep -c '^WSI_MFA_KEY=' "$CASE/etc/wsi.env")" == 1 ]] \
   && [[ "$(stat -c %a "$CASE/etc/wsi.env")" == "600" ]] \
    && pass "--rollback of that standby sync puts back this server's WSI_MFA_KEY only, keeping later edits" \
    || { fail "standby rollback of WSI_MFA_KEY (rc $RC)"; cat "$OUT"; cat "$CASE/etc/wsi.env"; }
# A destination with no key of its own before the sync: --rollback has no
# line to put back, says so, and leaves the source's (it opens nothing here).
setup standby-mfa-nokey-dest
for db in "$SRC/wsi.db" "$DST/wsi.db"; do sql "$db" "ALTER TABLE users ADD COLUMN totp_secret_enc TEXT"; done
sql "$SRC/wsi.db" "UPDATE users SET totp_secret_enc = 'v1.sealed'"
sed -i '/^WSI_MFA_KEY=/d' "$CASE/etc/wsi.env"
run_sync --yes backup-src
run_sync --rollback --yes
[[ "$RC" -eq 0 ]] && grep -q '^WSI_MFA_KEY=source-mfa-key$' "$CASE/etc/wsi.env" \
   && grep -q "had no WSI_MFA_KEY before that sync" "$OUT" && ! grep -q "copy that line by hand" "$OUT" \
   && [[ ! -e "$CASE/replica/wsi.env.pre-sync" ]] \
    && pass "--rollback when this server had no WSI_MFA_KEY says so instead of a false manual step" \
    || { fail "rollback without a previous key (rc $RC)"; cat "$OUT"; }
setup standby-mfa-nokey
for db in "$SRC/wsi.db" "$DST/wsi.db"; do sql "$db" "ALTER TABLE users ADD COLUMN totp_secret_enc TEXT"; done
sql "$SRC/wsi.db" "UPDATE users SET totp_secret_enc = 'v1.sealed'"
rm -f "$CASE/etc/src.env"
run_sync --yes backup-src
[[ "$RC" -eq 0 ]] && grep -q '^WSI_MFA_KEY=backup-mfa-key$' "$CASE/etc/wsi.env" \
   && grep -q "sudo grep '^WSI_MFA_KEY=' " "$OUT" \
    && pass "...and says how to copy it by hand when it cannot be read" \
    || { fail "standby two-factor key, unreadable (rc $RC)"; cat "$OUT"; }

# ---- 3. --dry-run -----------------------------------------------------------------
setup dryrun
before="$(sql "$DST/wsi.db" "SELECT public_id FROM slides")"
run_sync --dry-run backup-src
[[ "$RC" -eq 0 && "$(sql "$DST/wsi.db" "SELECT public_id FROM slides")" == "$before" \
   && ! -e "$DST/wsi/1_liver.svs" && ! -e "$DST/wsi.db.pre-sync" ]] \
    && ! grep -q 'systemctl stop' "$FAKE_STATE/calls" \
    && pass "--dry-run changes nothing and stops nothing" || { fail "--dry-run (rc $RC)"; cat "$OUT"; }
grep -q 'Would transfer' "$OUT" && grep -q '0 here now\|1 here now -> 2' "$OUT" \
    && pass "--dry-run reports the transfer and the changes" || { fail "--dry-run report"; cat "$OUT"; }

# ---- 4. version mismatch -----------------------------------------------------------
setup versions v1.5.0 8
run_sync --yes backup-src
[[ "$RC" -ne 0 ]] && grep -q "Update this server" "$OUT" && ! grep -q 'systemctl stop' "$FAKE_STATE/calls" \
    && pass "a newer source is refused before anything stops" || { fail "version refusal (rc $RC)"; cat "$OUT"; }

# ---- 5. a failed swap leaves the destination as it was ------------------------------
setup badmerge
sql "$SRC/wsi.db" "INSERT INTO tours VALUES (2, 'orphan', 77, 'no slide', '');"
run_sync --yes backup-src
[[ "$RC" -ne 0 && "$(sql "$DST/wsi.db" "SELECT public_id, title FROM slides")" == "oldSlide|Old" ]] && services_running \
    && pass "a failed swap changes no data and restarts the services" || { fail "failed swap (rc $RC)"; cat "$OUT"; }
[[ "$(sql "$DST/wsi.db" "SELECT wsi_generation FROM slides")" -gt 1600000000000 ]] \
    && pass "...with every slide on a fresh tile generation, since files were already in place" \
    || fail "failed swap left the old tile generations"
[[ "$(grep -c '^systemctl start' "$FAKE_STATE/calls")" == 2 ]] \
    && pass "...starting each service once" || { fail "services started more than once"; cat "$FAKE_STATE/calls"; }

# A helper failing outside any `||` (this server's database is not one): the
# ERR trap also fires in as_app_user's subshell, which must leave the
# handling to the main shell instead of reporting (and recovering) twice.
setup corrupt
printf 'not a database' > "$DST/wsi.db"
run_sync --yes backup-src
[[ "$RC" -ne 0 && "$(grep -c 'Sync failed at step' "$OUT")" == 1 ]] \
    && pass "a failure is handled once, by the main shell" || { fail "failure handled $(grep -c 'Sync failed at step' "$OUT") times"; cat "$OUT"; }

# A failed sync keeps the files its kept rollback point refers to: the
# original slide (9_old.svs), which only wsi.db.pre-sync still names.
setup keepfiles
run_sync --yes backup-src
sql "$SRC/wsi.db" "INSERT INTO tours VALUES (9, 'orphan', 77, 'no slide', '');"
run_sync --yes backup-src
[[ "$RC" -ne 0 && -f "$DST/wsi/9_old.svs" ]] \
    && pass "a failed sync keeps the files of the rollback point it keeps" \
    || { fail "rollback point files after a failed sync (rc $RC)"; cat "$OUT"; }

# A sign-out the helper can only hand to the app's start is reported as such.
setup deferred
DEFER_APP="$CASE/app"
install -D -m 0644 "$APP_HOME_DIR/services/app/wsi_app/restore_apply.py" \
    "$DEFER_APP/services/app/wsi_app/restore_apply.py"
cat >> "$DEFER_APP/services/app/wsi_app/restore_apply.py" <<'PY'


def invalidate_sessions(db_path, previous_db):
    raise OSError("database is locked")
PY
chown -R "$APP_USER" "$DEFER_APP"
APP_HOME_OVERRIDE="$DEFER_APP" run_sync --yes backup-src
[[ "$RC" -eq 0 && -f "$DST/wsi.db.session-revocation-pending" ]] && grep -q "handed to wsi-app's next start" "$OUT" \
   && ! grep -q 'Signed everyone out' "$OUT" \
    && pass "a sign-out handed to the app's start is reported as such" || { fail "deferred sign-out (rc $RC)"; cat "$OUT"; }

# A sign-out that cannot be done (nor handed to the app's boot) after the
# swap: the services stay stopped rather than serve revoked logins.
setup nosignout
BROKEN_APP="$CASE/app"
install -D -m 0644 "$APP_HOME_DIR/services/app/wsi_app/restore_apply.py" \
    "$BROKEN_APP/services/app/wsi_app/restore_apply.py"
cat >> "$BROKEN_APP/services/app/wsi_app/restore_apply.py" <<'PY'


def invalidate_sessions(db_path, previous_db):
    raise OSError("database is read-only")


def write_json_durable(path, payload):
    raise OSError("disk full")
PY
APP_HOME_OVERRIDE="$BROKEN_APP" run_sync --yes backup-src
[[ "$RC" -ne 0 && "$(sql "$DST/wsi.db" "SELECT public_id FROM slides ORDER BY id")" == $'slideA\nslideB' ]] \
   && [[ ! -f "$FAKE_STATE/active.wsi-app.service" ]] && grep -q 'stay STOPPED' "$OUT" \
    && pass "a swap whose sign-out cannot be done leaves the services stopped" \
    || { fail "sign-out failure (rc $RC)"; cat "$OUT"; }

# A sign-out still pending from an earlier swap: neither a sync nor a rollback
# may replace what it compares against.
setup pending
run_sync --yes backup-src
printf '{}' > "$DST/wsi.db.session-revocation-pending"
: > "$FAKE_STATE/calls"
run_sync --yes backup-src
rc_sync=$RC
run_sync --rollback --yes
[[ "$rc_sync" -ne 0 && "$RC" -ne 0 ]] && grep -q 'still pending' "$OUT" && ! grep -q 'systemctl stop' "$FAKE_STATE/calls" \
    && pass "a pending sign-out stops a sync and a rollback before anything changes" \
    || { fail "pending sign-out (rc $rc_sync / $RC)"; cat "$OUT"; }
rm -f "$DST/wsi.db.session-revocation-pending"

# One sync or rollback at a time.
flock "$CASE/sync.lock" sleep 30 &
holder=$!
sleep 0.5
run_sync --rollback --yes
kill "$holder" 2>/dev/null; wait "$holder" 2>/dev/null
[[ "$RC" -ne 0 ]] && grep -q 'Another sync or rollback is running' "$OUT" \
    && pass "a rollback waits for no sync: it refuses while one holds the lock" \
    || { fail "rollback lock (rc $RC)"; cat "$OUT"; }
run_sync --rollback --dry-run --yes
[[ "$RC" -ne 0 ]] && grep -q 'takes no --dry-run' "$OUT" \
    && pass "--rollback refuses --dry-run instead of rolling back" || { fail "rollback --dry-run (rc $RC)"; cat "$OUT"; }
run_sync --rollback --all --yes backup-src
[[ "$RC" -eq 2 ]] && grep -q 'cannot be combined' "$OUT" \
    && pass "--rollback and --all are refused together" || { fail "--rollback --all (rc $RC)"; cat "$OUT"; }

# A restore staged on this server, or an interrupted one's journal: wsi-app's
# next start would apply it over the sync, so neither a sync nor a rollback
# goes ahead.
setup restorepending
printf 'RESTORE_REQUEST_PATH=%s\n' "$CASE/run/restore.request" >> "$CASE/etc/wsi.env"
mkdir -p "$CASE/run"
printf '{}' > "$CASE/run/restore.request"
run_sync --yes backup-src
rc_req=$RC
rm -f "$CASE/run/restore.request"
printf '{}' > "$DST/.restore-apply.journal"
run_sync --yes backup-src
[[ "$rc_req" -ne 0 && "$RC" -ne 0 ]] && grep -q 'interrupted restore' "$OUT" \
   && ! grep -q 'systemctl stop' "$FAKE_STATE/calls" && [[ ! -e "$DST/wsi.db.pre-sync" ]] \
    && pass "a staged or interrupted restore stops the sync before anything changes" \
    || { fail "pending restore (rc $rc_req / $RC)"; cat "$OUT"; }

# A CRLF-edited wsi.env with trailing blanks still names the right paths.
setup crlf
printf 'DATABASE_URL=sqlite:///%s \r\nDATA_DIR=%s  \r\nJWT_SECRET=x\r\n' "$DST/wsi.db" "$DST" > "$CASE/etc/wsi.env"
DATA_DIR_FROM_ENV_FILE=1 run_sync --yes backup-src
[[ "$RC" -eq 0 && "$(sql "$DST/wsi.db" "SELECT COUNT(*) FROM slides")" == 2 ]] \
    && pass "a CRLF wsi.env is read like systemd reads it" || { fail "CRLF wsi.env (rc $RC)"; cat "$OUT"; }

# ---- 6. --all (migration) -------------------------------------------------------------
setup migration
run_sync --all --yes backup-src
[[ "$RC" -eq 0 ]] || { fail "--all exit $RC"; cat "$OUT"; }
[[ "$(sql "$DST/wsi.db" "SELECT mode FROM auth_settings")" == "google" \
   && "$(sql "$DST/wsi.db" "SELECT client_ip FROM page_views")" == "10.0.0.1" ]] \
    && pass "--all copies every table" || fail "--all did not replace the database"
grep -q '^ANALYTICS_HASH_SALT=source-salt$' "$CASE/etc/wsi.env" && grep -q '^JWT_SECRET=backup-jwt$' "$CASE/etc/wsi.env" \
   && grep -q '^WSI_MFA_KEY=source-mfa-key$' "$CASE/etc/wsi.env" \
   && grep -q "^DATA_DIR=$DST\$" "$CASE/etc/wsi.env" && [[ "$(stat -c %a "$CASE/etc/wsi.env")" == "600" ]] \
    && pass "--all carries ANALYTICS_HASH_SALT and WSI_MFA_KEY into wsi.env (not JWT_SECRET), keeping its mode" \
    || { fail "--all secrets"; cat "$CASE/etc/wsi.env"; }
! grep -q 'source-salt\|source-jwt\|source-mfa-key' "$OUT" && pass "the secrets are never printed" || fail "a secret reached the output"
[[ "$(sql "$DST/wsi.db" "SELECT token_version FROM users")" == 901 \
   && "$(sql "$DST/wsi.db" "SELECT session_generation FROM auth_settings")" == 6 ]] \
    && pass "--all signs everyone out too" || fail "--all sign-out"
run_sync --rollback --yes
[[ "$RC" -eq 0 && "$(sql "$DST/wsi.db" "SELECT mode FROM auth_settings")" == "none" ]] \
   && grep -q '^ANALYTICS_HASH_SALT=backup-salt$' "$CASE/etc/wsi.env" && grep -q '^WSI_MFA_KEY=backup-mfa-key$' "$CASE/etc/wsi.env" \
   && [[ "$(stat -c %a "$CASE/etc/wsi.env")" == "600" ]] \
    && pass "--rollback after --all puts the database and wsi.env back" || { fail "--rollback after --all (rc $RC)"; cat "$OUT"; }

# Without access to the source's wsi.env the move still goes ahead, and says
# how to copy the salt by hand.
setup migration-nosecrets
rm -f "$CASE/etc/src.env"
run_sync --all --yes backup-src
[[ "$RC" -eq 0 && "$(sql "$DST/wsi.db" "SELECT mode FROM auth_settings")" == "google" ]] \
   && grep -q '^ANALYTICS_HASH_SALT=backup-salt$' "$CASE/etc/wsi.env" && grep -q "sudo grep '^ANALYTICS_HASH_SALT=' " "$OUT" \
    && pass "--all without the source's secrets copies the database and prints the manual step" \
    || { fail "--all without secrets (rc $RC)"; cat "$OUT"; }

# ---- 7. an attachment that cannot be read on the source -------------------------------
setup badasset
printf 'secret' > "$SRC/assets/tours/tourA/locked.png"
chown root:root "$SRC/assets/tours/tourA/locked.png"
chmod 0600 "$SRC/assets/tours/tourA/locked.png"
run_sync --yes backup-src
[[ "$RC" -ne 0 ]] && grep -q 'assets/tours/tourA/locked.png' "$OUT" && ! grep -q 'systemctl stop' "$FAKE_STATE/calls" \
    && pass "an attachment that did not arrive stops the sync before anything changes" \
    || { fail "unreadable attachment (rc $RC)"; cat "$OUT"; }

# ---- 8. a SIGTERM while the swap runs --------------------------------------------------
# bash runs the trap only once the swap helper returns, i.e. after it has
# committed: recovery must take the swap as landed (sign everyone out, keep
# the copy from just before it as the rollback point).
# The SIGTERM must land once the swap has begun: a try where it came before
# (nothing swapped, which recovery handles as an ordinary failure) proves
# nothing here, so it is tried again, up to three times.
sigterm_landed=0
for attempt in 1 2 3; do
    setup "sigterm-$attempt"
    sql "$SRC/wsi.db" "WITH RECURSIVE n(i) AS (SELECT 1 UNION ALL SELECT i + 1 FROM n WHERE i < 1500000)
        INSERT INTO step_pins SELECT i, 1, 'p' FROM n;"
    OUT="$CASE/out.log"
    # env execs bash, so $! is the script's own pid.
    env PATH="$STUBS:$PATH" APP_USER="$APP_USER" APP_GROUP="$APP_GROUP" SSH_AS_USER=root \
        APP_HOME="$APP_HOME_DIR" WSI_SYNC_LOCK="$CASE/sync.lock" WSI_ENV_FILE="$CASE/etc/wsi.env" DATA_DIR="$DST" \
        WSI_REPLICA_DIR="$CASE/replica" WSI_SOURCE_DB="$SRC/wsi.db" WSI_SOURCE_DATA_DIR="$SRC" \
        WSI_SOURCE_ENV_FILE="$CASE/etc/src.env" WSI_HEALTH_TIMEOUT=3 \
        bash "$SYNC_SH" --yes backup-src </dev/null >"$OUT" 2>&1 &
    sync_pid=$!
    for _ in $(seq 1 600); do grep -q 'Files:' "$OUT" 2>/dev/null && break; sleep 0.05; done
    kill -TERM "$sync_pid" 2>/dev/null
    wait "$sync_pid"; RC=$?
    [[ "$(sql "$DST/wsi.db" "SELECT COUNT(*) FROM step_pins")" == 1500000 ]] || continue
    sigterm_landed=1
    [[ "$(sql "$DST/wsi.db" "SELECT token_version FROM users")" == 901 \
       && -f "$DST/wsi.db.pre-sync" && ! -e "$DST/wsi.db.pre-sync.new" ]] \
        && pass "a SIGTERM during the swap still signs everyone out and keeps the rollback point" \
        || { fail "SIGTERM during the swap (rc $RC)"; cat "$OUT"; }
    break
done
[[ "$sigterm_landed" == 1 ]] || fail "the SIGTERM never landed after the swap began in 3 tries; the case proves nothing"

# ---- 9. the mirror must not sit inside DATA_DIR ----------------------------------------
setup badstage
REPLICA_DIR="$DST/replica" run_sync --yes backup-src
[[ "$RC" -ne 0 ]] && grep -q 'outside DATA_DIR' "$OUT" \
    && pass "a mirror inside DATA_DIR is refused" || { fail "mirror placement (rc $RC)"; cat "$OUT"; }
REPLICA_DIR="$CASE/replica/.." run_sync --yes backup-src
[[ "$RC" -ne 0 ]] && grep -q 'plain characters' "$OUT" \
    && pass "a mirror path with a .. component is refused" || { fail "mirror path with .. (rc $RC)"; cat "$OUT"; }

if [[ "$fails" -gt 0 ]]; then
    printf '\n%d check(s) failed\n' "$fails"
    exit 1
fi
printf '\nall checks passed\n'
