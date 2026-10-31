#!/bin/bash
# Compute the number of Uvicorn worker processes and start the app.
#
# Priority:
#   1. UVICORN_WORKERS         - explicit count (overrides everything else)
#   2. UVICORN_WORKERS_PERCENT - percentage of CPU cores (no upper cap)
#   3. Default formula         - min(max(2, cores/2), 8)
#                                Most tile traffic is absorbed by the nginx
#                                disk cache, OpenSlide / Pillow release the
#                                GIL during reads and JPEG encode, and uvicorn
#                                workers are async (one process serves many
#                                concurrent connections). Past ~8 workers the
#                                bottleneck shifts to disk I/O, so the cap
#                                stops many-core hosts from spawning dozens
#                                of redundant processes that each duplicate
#                                the per-worker DZI cache. Operators who
#                                need more can set UVICORN_WORKERS or
#                                UVICORN_WORKERS_PERCENT explicitly.
#
# Result is always clamped to [1, nproc].
#
# Deployed in place by install.sh (rsync to /opt/wsi + chmod +x);
# wsi-app.service's ExecStart points here directly.

set -euo pipefail

# -----------------------------------------------------------------------------
# Restore-apply / reset-apply phase
#
# When the dashboard's Backup & Restore UI commits a restore (or a Reset),
# the API writes /run/wsi/restore.request (a JSON record describing what to
# swap in) and drops /run/wsi/restart.trigger, on which the root-owned
# wsi-restart.path / wsi-restart.service restart wsi-app (the app itself runs
# under NoNewPrivileges=true and cannot). systemd lands
# the process back here with a fresh tree, so this is the right moment to
# actually move files around: the SQLite engine inside the previous wsi-app
# is gone, no request handlers are running, and any failure can be caught
# before uvicorn exposes a half-restored DB.
#
# The request payload's ``scope`` is one of:
#   * ``full``     - swap DB, assets/, wsi/, and thumbs/ from the staged archive
#   * ``metadata`` - swap DB and assets/; wipe wsi/ + thumbs/ (operator re-uploads)
#   * ``reset``    - swap DB only (admin-/config-preserving wipe); wipe assets/, wsi/, thumbs/
# In every case the live DB, assets/, wsi/, and thumbs/ are first parked under
# /var/lib/wsi/.pre-restore-<UTC>/ so a swap failure rolls back automatically
# and a successful swap still leaves the operator a rollback artefact for
# manual recovery.
#
# A committed swap also moves every user's token_version and the app-access
# session generation past the values on both sides of it, so no staff token or
# viewer cookie issued before the restore (including any revoked since the
# backup was taken) is accepted afterwards: everyone signs in again.
#
# Result (success or rollback) is written to /run/wsi/restore.result so the
# UI can read it via /api/admin/restore/result after the service comes
# back. The request file is removed unconditionally; leaving it in place
# would re-apply the swap on every subsequent restart.
#
# Crash safety. The swap is a sequence of rename(2)s (park the live paths,
# then promote the staged ones), and a stop that bypasses Python's exception
# handling — SIGKILL after TimeoutStopSec, the default SIGTERM disposition,
# power loss, a host reboot — can land between any two of them, e.g. with the
# new wsi.db promoted but wsi/ not yet. An in-memory rollback list cannot
# survive that, and the request file cannot either (/run is a tmpfs). So
# before the first move the helper writes a journal of every planned move to
# the data volume (RESTORE_JOURNAL, fsync'd), advances a cursor in it before
# each move, and marks it ``committed`` once every move has landed. The next
# start reconciles a leftover journal FIRST, before looking at any request:
# an uncommitted apply is rolled back move by move (newest first) to the
# pre-restore state, a committed one is finished. uvicorn is never exec'd
# while a journal remains — if the reconciliation cannot complete, the unit
# fails (and systemd retries) rather than serve a half-applied tree.
#
# The helper is services/app/wsi_app/restore_apply.py — the one implementation
# of the swap and its recovery, which the dev in-process apply calls too.
# It imports only the standard library, so running it initialises none of the
# app (settings, DB engine) and works even if the venv's packages are broken.
# This script keeps only the gate around it.
# -----------------------------------------------------------------------------
RESTORE_REQUEST=${RESTORE_REQUEST_PATH:-/run/wsi/restore.request}
RESTORE_RESULT=${RESTORE_RESULT_PATH:-/run/wsi/restore.result}
RESTART_TRIGGER=${RESTART_TRIGGER_PATH:-/run/wsi/restart.trigger}
# Must match wsi_app.backup.restore_journal_path(): on the data volume (never
# /run, which a reboot wipes) so it outlives exactly the crashes it is for.
RESTORE_JOURNAL=${RESTORE_JOURNAL_PATH:-${DATA_DIR:-/var/lib/wsi}/.restore-apply.journal}
PYTHON_BIN=/opt/wsi/venv/bin/python3
# install.sh rsyncs the repo to the fixed APP_HOME=/opt/wsi (it refuses any
# other), so the package root is here — the unit's WorkingDirectory too, but
# the helper is pointed at it explicitly rather than trusting the inherited cwd.
APP_DIR=/opt/wsi/services/app

# Clear the restart trigger on every boot. wsi-restart.service already removes
# it before restarting, but doing it here too guarantees the wsi-restart.path
# PathExists condition is false once the app is back up, so a stale trigger can
# never re-fire the restart unit into a loop.
# -r: the wsi user can make it a directory, which `rm -f` leaves in place.
rm -rf --one-file-system -- "${RESTART_TRIGGER}" 2>/dev/null || true

# -L too: a dangling symlink still blocks the start (the helper and the app
# both test with lexists), rather than slipping past a bare -e.
journal_present() { [ -e "${RESTORE_JOURNAL}" ] || [ -L "${RESTORE_JOURNAL}" ]; }

if journal_present && [ ! -x "${PYTHON_BIN}" ]; then
    echo "WSI: interrupted restore apply recorded at ${RESTORE_JOURNAL}, but ${PYTHON_BIN} is missing so it cannot be reconciled; refusing to start uvicorn on a possibly half-applied data directory" >&2
    exit 1
fi

if { [ -e "${RESTORE_REQUEST}" ] || journal_present; } && [ -x "${PYTHON_BIN}" ]; then
    echo "WSI: restore request or interrupted-apply journal detected; reconciling before uvicorn starts" >&2
    mkdir -p "$(dirname "${RESTORE_RESULT}")"
    # The Python helper handles parse, mv, rollback and result-write so
    # atomicity isn't reinvented in shell. An ordinary failure (bad request,
    # failed swap that rolled back cleanly) must not abort the service:
    # uvicorn must come up so the operator can see the error in the UI. Only a
    # journal left behind (checked below) blocks the start — whatever the exit
    # status, so a helper that cannot run at all (module missing from a broken
    # deploy, APP_DIR gone) still fails closed on a journal, and on a bare
    # request just leaves the live tree untouched.
    #
    # Run from APP_DIR in a subshell (this script's own cwd is unchanged):
    # `-m` puts the cwd first on sys.path, and PYTHONPATH (replacing any
    # inherited value — the helper needs nothing else) covers PYTHONSAFEPATH.
    helper_rc=0
    ( cd "${APP_DIR}" && PYTHONPATH="${APP_DIR}" "${PYTHON_BIN}" -m wsi_app.restore_apply \
        "${RESTORE_REQUEST}" "${RESTORE_RESULT}" "${RESTORE_JOURNAL}" ) || helper_rc=$?
    if [ "${helper_rc}" -ne 0 ]; then
        echo "WSI: restore-apply helper (${APP_DIR}/wsi_app/restore_apply.py) exited with status ${helper_rc}" >&2
    fi
    # The helper removes the request itself, but an exception it does not
    # catch (e.g. write_result failing on a full disk) would skip that and
    # leave the request behind — to be re-applied on every later restart, and
    # to make wsi-restart.service treat every later plain restart as a
    # restore (stopping wsi-sync and waiting on this file). One attempt per
    # request, as documented above: remove it here regardless. (An interrupted
    # apply does not need it: its journal carries everything the next start's
    # reconciliation uses.)
    rm -rf --one-file-system -- "${RESTORE_REQUEST}"
    # Never start the app on a tree the helper could not bring back to a
    # consistent state. Exiting non-zero leaves the unit failed (systemd's
    # Restart=on-failure retries the reconciliation) with the reason in the
    # journal; the app's lifespan also refuses to start while the file exists,
    # which covers wsi-sync. See docs/operations.md, "Recovering from an
    # interrupted restore".
    if journal_present; then
        echo "WSI: restore apply could not be reconciled (journal ${RESTORE_JOURNAL} remains); refusing to start uvicorn" >&2
        exit 1
    fi
fi

# Worker count and the per-worker DZI byte cache are both derived here so
# that every uvicorn worker inherits identical values from the master
# process environment. The arithmetic lives in wsi-calc.sh; set-workers.sh
# sources the same file to preview the numbers an operator would get.
SELF_DIR="$(cd "$(dirname "$0")" && pwd)"
# shellcheck source=wsi-calc.sh
. "${SELF_DIR}/wsi-calc.sh"

CPU_CORES=$(nproc)
MEM_BYTES=$(wsi_mem_total_bytes)

# Default path applies a RAM cap (a many-core host won't spawn more
# workers than RAM can back with a useful cache); explicit
# UVICORN_WORKERS / UVICORN_WORKERS_PERCENT bypass that cap.
WORKERS=$(wsi_compute_workers "${UVICORN_WORKERS:-}" "${UVICORN_WORKERS_PERCENT:-}" "${CPU_CORES}" "${MEM_BYTES}")

# Ask wsi-calc which overrides it actually accepted. Reading the raw env
# vars here would name a value that wsi_compute_workers rejected (e.g.
# UVICORN_WORKERS=four), so the summary line below would contradict the
# warning it just printed and misattribute the worker count.
ACCEPTED_WORKERS="$(wsi_valid_uint "${UVICORN_WORKERS:-}" || true)"
ACCEPTED_PERCENT="$(wsi_valid_uint "${UVICORN_WORKERS_PERCENT:-}" || true)"

if [ -n "${ACCEPTED_WORKERS}" ]; then
    SOURCE="UVICORN_WORKERS=${ACCEPTED_WORKERS}"
elif [ -n "${ACCEPTED_PERCENT}" ]; then
    SOURCE="UVICORN_WORKERS_PERCENT=${ACCEPTED_PERCENT}% (uncapped)"
else
    SOURCE="RAM-aware default (min(max(2, cores/2), 8) capped by RAM budget)"
fi

echo "WSI: starting uvicorn with ${WORKERS} worker(s) (${CPU_CORES} CPU cores, RAM=${MEM_BYTES:-unknown} bytes, ${SOURCE})" >&2

# Auto-derive the per-worker DZI byte cache from RAM unless the operator
# pinned DZI_CACHE_MAX_BYTES explicitly. Exported before uvicorn forks so
# all workers agree. An empty result (RAM unreadable) leaves the env unset
# and the app falls back to its built-in default.
if [ -z "${DZI_CACHE_MAX_BYTES:-}" ]; then
    DERIVED_CACHE_BYTES=$(wsi_compute_cache_max_bytes "${MEM_BYTES}" "${WORKERS}")
    if [ -n "${DERIVED_CACHE_BYTES}" ]; then
        export DZI_CACHE_MAX_BYTES="${DERIVED_CACHE_BYTES}"
        echo "WSI: auto DZI_CACHE_MAX_BYTES=${DERIVED_CACHE_BYTES} bytes/worker (fraction=$(wsi_cache_fraction)% of RAM, split across ${WORKERS} worker(s))" >&2
    else
        echo "WSI: RAM unreadable; DZI_CACHE_MAX_BYTES left unset (app built-in default applies)" >&2
    fi
else
    echo "WSI: DZI_CACHE_MAX_BYTES=${DZI_CACHE_MAX_BYTES} (explicit; auto-sizing skipped)" >&2
fi

# Spool multipart uploads onto the data volume, not /tmp. Starlette writes
# every uploaded file part to a temp file (TMPDIR) before the endpoint copies
# it into place, so a multi-GB slide or restore upload briefly needs that much
# room there too. Under PrivateTmp=true, /tmp is on the root filesystem (or a
# RAM-backed tmpfs) that nothing sizes for this; the data volume is the disk
# the uploads are headed for anyway. Only this service uses the directory and
# no worker is running yet, so anything left in it is an aborted request's
# debris from a previous run: clear it.
UPLOAD_TMP="${DATA_DIR:-/var/lib/wsi}/.upload-tmp"
if mkdir -p "${UPLOAD_TMP}" 2>/dev/null && chmod 0700 "${UPLOAD_TMP}" 2>/dev/null; then
    find "${UPLOAD_TMP}" -mindepth 1 -delete 2>/dev/null || true
    export TMPDIR="${UPLOAD_TMP}"
else
    echo "WSI: cannot use ${UPLOAD_TMP} for upload spooling; leaving TMPDIR as is" >&2
fi

# --no-proxy-headers: uvicorn enables ProxyHeadersMiddleware by default
# (forwarded-allow-ips=127.0.0.1), which rewrites request.client.host to the
# X-Forwarded-For value handed in by nginx on the loopback hop. The app's
# trust model requires request.client.host to be the *immediate TCP peer*
# (loopback for the nginx/cloudflared hop) — cf_access.cf_jwt_status() and
# network.get_client_ip() gate on it, and the app derives the real client IP
# itself from Cf-Connecting-IP / X-Real-IP. Letting uvicorn pre-rewrite the
# peer makes the hop look untrusted, so CF Access requests 401 and every
# recorded IP collapses to the forwarded value. Turn it off and let the app
# do its own forwarded-IP handling.
# --ws-max-size: bound WebSocket frame size at the protocol layer, mirroring
# wsi-sync.service. nginx only routes the live-sync WS upgrades to the sync
# coordinator, so this app process should never see a WS frame in production —
# but the routes are registered on the same ASGI app, so cap them here too as
# defense-in-depth against a direct-to-127.0.0.1 client that bypasses nginx.
exec /opt/wsi/venv/bin/uvicorn wsi_app.main:app \
    --host 127.0.0.1 \
    --port 8000 \
    --workers "${WORKERS}" \
    --ws-max-size 65536 \
    --no-proxy-headers
