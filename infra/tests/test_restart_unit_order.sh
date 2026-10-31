#!/usr/bin/env bash
# Guard: wsi-restart.service keeps the stop-sync -> restart-app -> start-sync
# order around a restore / reset apply.
#
# Run: bash infra/tests/test_restart_unit_order.sh
#
# WHY THIS EXISTS
# ---------------
# A restore/reset apply swaps the SQLite file under every process. wsi-sync is
# a separate process with its own pooled handles; if it keeps running across
# the swap its connections follow the parked pre-restore inode, so live-sync
# auth / IP / staff checks answer from the old DB and its audit rows land in
# the parked copy. The unit therefore stops wsi-sync, restarts wsi-app (whose
# start script performs the swap), waits for the swap to finish, and only then
# starts wsi-sync again — while a plain restart (no staged request) must leave
# wsi-sync (and its in-memory live-sync rooms) alone. db.py's replaced-file
# guard is the backstop (pinned by services/app/tests/test_db_file_swap.py);
# this pins the primary mechanism.
#
# The test lifts the REAL ExecStart shell snippet out of
# infra/systemd/wsi-restart.service, points it at a stub `systemctl` that logs
# its calls and a temp dir standing in for /run/wsi, and runs it through each
# scenario. Needs only sh + python3; SKIPs (exit 0) without python3.

set -uo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
REPO="$(cd "${HERE}/../.." && pwd)"
UNIT="${REPO}/infra/systemd/wsi-restart.service"

skip() { printf 'SKIP - %s\n' "$1"; exit 0; }
fail() { printf 'FAIL - %s\n' "$1"; FAILED=1; }

command -v python3 >/dev/null 2>&1 || skip "python3 not available"
[[ -f "${UNIT}" ]] || { echo "FAIL - unit not found: ${UNIT}"; exit 1; }

FAILED=0
WORK="$(mktemp -d)"
trap 'rm -rf "${WORK}"' EXIT
mkdir -p "${WORK}/run"

# The trigger must be removed before the (blocking) restart, or the path unit
# can re-fire this oneshot once it returns.
if grep -Eq '^ExecStartPre=/usr/bin/rm -rf --one-file-system -- /run/wsi/restart\.trigger$' "${UNIT}"; then
    printf 'ok   - ExecStartPre clears the restart trigger first\n'
else
    fail "ExecStartPre no longer removes /run/wsi/restart.trigger (recursively: it may be a directory)"
fi

# The wsi user owns /run/wsi, so the trigger can be a non-empty directory. The
# real ExecStartPre command, aimed at a temp dir, must clear a file, a
# directory with content, and a symlink (the link, never its target).
pre="$(sed -n 's/^ExecStartPre=//p' "${UNIT}")"
pre="${pre//\/run\/wsi\//${WORK}/run/}"
trig="${WORK}/run/restart.trigger"
for kind in file dir symlink; do
    rm -rf "${trig}" "${WORK}/outside"
    mkdir -p "${WORK}/outside"; : > "${WORK}/outside/keep"
    case "$kind" in
        file)    : > "${trig}" ;;
        dir)     mkdir -p "${trig}/sub/deeper"; : > "${trig}/sub/deeper/x"; : > "${trig}/y" ;;
        symlink) ln -s "${WORK}/outside" "${trig}" ;;
    esac
    # shellcheck disable=SC2086  # the unit's command line: split on spaces, as systemd does
    if $pre && [[ ! -e "${trig}" && ! -L "${trig}" ]]; then
        if [[ "$kind" == symlink && ! -e "${WORK}/outside/keep" ]]; then
            fail "ExecStartPre followed a symlink trigger into its target"
        else
            printf 'ok   - ExecStartPre removes a trigger that is a %s\n' "$kind"
        fi
    else
        fail "ExecStartPre left a trigger that is a ${kind} in place"
    fi
done

# --- extract ExecStart's sh -c body, the way systemd would see it ------------
# systemd joins backslash-continued lines and unescapes $$ to $.
if ! python3 - "${UNIT}" "${WORK}" > "${WORK}/exec.sh" <<'PY'
import re, sys
unit, work = sys.argv[1], sys.argv[2]
joined = re.sub(r"\\\n", " ", open(unit).read())
m = re.search(r"^ExecStart=/bin/sh -c '(.*)'\s*$", joined, re.M)
if not m:
    sys.exit("ExecStart is no longer a /bin/sh -c '...' snippet")
body = m.group(1).replace("$$", "$")
body = body.replace("/usr/bin/systemctl", f"{work}/systemctl")
body = body.replace("/run/wsi/", f"{work}/run/")
print(body)
PY
then
    echo "FAIL - could not extract ExecStart from ${UNIT}"
    exit 1
fi

# Stub systemctl: logs every call. SYNC_ACTIVE=1 makes wsi-sync report active;
# APP_OK=1 makes `restart wsi-app` succeed and consume restore.request (what
# start-wsi-app.sh does), APP_OK=0 makes it fail and report is-failed.
cat > "${WORK}/systemctl" <<SH
#!/bin/sh
echo "\$*" >> "${WORK}/calls.log"
case "\$1 \$2" in
    "is-active --quiet") [ "\${SYNC_ACTIVE:-0}" = 1 ] ;;
    "is-failed --quiet") [ "\${APP_OK:-1}" = 0 ] ;;
    "restart wsi-app.service")
        if [ "\${APP_OK:-1}" = 1 ]; then rm -f "${WORK}/run/restore.request"; exit 0; fi
        exit 1 ;;
    *) exit 0 ;;
esac
SH
chmod +x "${WORK}/systemctl"

# run <name> <restore.request present 0|1> <SYNC_ACTIVE> <APP_OK> <want rc> <want calls...>
run() {
    local name="$1" req="$2" sync="$3" app="$4" want_rc="$5"; shift 5
    rm -f "${WORK}/calls.log" "${WORK}/run/restore.request"
    : > "${WORK}/calls.log"
    [[ "$req" == 1 ]] && : > "${WORK}/run/restore.request"
    SYNC_ACTIVE="$sync" APP_OK="$app" timeout 30 sh "${WORK}/exec.sh"
    local rc=$?
    local got want
    got="$(grep -v -- '--quiet' "${WORK}/calls.log" | paste -sd ';' -)"
    want="$(printf '%s\n' "$@" | paste -sd ';' -)"
    if [[ "$got" == "$want" && "$rc" == "$want_rc" ]]; then
        printf 'ok   - %s\n' "$name"
    else
        fail "${name}: got [${got}] rc=${rc}, want [${want}] rc=${want_rc}"
    fi
}

run "plain restart (no request) leaves wsi-sync alone" 0 1 1 0 \
    "restart wsi-app.service"
run "restore apply: stop sync -> restart app -> start sync" 1 1 1 0 \
    "stop wsi-sync.service" "restart wsi-app.service" "start wsi-sync.service"
run "restore apply with sync already stopped does not start it" 1 0 1 0 \
    "stop wsi-sync.service" "restart wsi-app.service"
run "restore apply where wsi-app fails still brings sync back, keeps rc" 1 1 0 1 \
    "stop wsi-sync.service" "restart wsi-app.service" "start wsi-sync.service"

# The wait loop must hold wsi-sync back until the swap is done, i.e. until
# start-wsi-app.sh removes restore.request — not merely until restart returns.
if grep -q 'while \[ -e /run/wsi/restore.request \]' "${UNIT}"; then
    printf 'ok   - sync start waits on restore.request being consumed\n'
else
    fail "the wait on /run/wsi/restore.request before starting wsi-sync is gone"
fi

printf -- '--------------------------------------------------------------\n'
if [[ "$FAILED" -eq 0 ]]; then
    printf 'PASS - wsi-restart.service keeps wsi-sync off the DB across a restore swap\n'
else
    exit 1
fi
