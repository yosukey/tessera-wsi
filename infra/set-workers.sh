#!/usr/bin/env bash
# Interactive script to update UVICORN_WORKERS / UVICORN_WORKERS_PERCENT
# in /etc/wsi/wsi.env and optionally restart the wsi-app service.
#
# Must be run as root (or with sudo) to write /etc/wsi/wsi.env, from your
# clone (~/wsi): it refuses to run from the deployed copy under /opt/wsi.
# ----------------------------------------------------------------
set -euo pipefail

ENV_FILE="${WSI_ENV:-/etc/wsi/wsi.env}"
SERVICE="wsi-app.service"
# Default formula in start-wsi-app.sh: min(max(2, cores/2), 8) when neither
# UVICORN_WORKERS nor UVICORN_WORKERS_PERCENT is set, then capped by the
# RAM budget (see wsi-calc.sh).
DEFAULT_FLOOR=2
DEFAULT_CAP=8

# Root runs code from this script's own tree: it sources systemd/wsi-calc.sh
# and runs lib/envfile_set.py found next to it. So, like install.sh, it is run
# from the operator's clone and refuses a tree that an account other than root
# or the operator could have rewritten — above all the deployed copy under
# /opt/wsi, which belongs to the service user and has had pip / npm run in it
# (QUICKSTART §3.4). Symlinks are resolved first, so a link into /opt/wsi is
# caught too, and nothing is read from the tree before the check passes.
# set-cache-size.sh carries the same check.
SELF="$(realpath -- "${BASH_SOURCE[0]}")"
SELF_DIR="$(dirname -- "$SELF")"
untrusted_tree() {
    printf '\033[31mError: refusing to run %s: %s.\033[0m\n' "$SELF" "$1" >&2
    printf 'It runs code from %s as root, so only root or you may be able\n' "$SELF_DIR" >&2
    printf 'to change that tree. Run it from your clone instead:\n' >&2
    printf '  cd ~/wsi && sudo bash infra/%s\n' "${SELF##*/}" >&2
    exit 1
}
for _home in /opt/wsi ${APP_HOME:+"$APP_HOME"}; do
    _home="$(realpath -m -- "$_home")"
    case "$SELF_DIR/" in
        "$_home"/*) untrusted_tree "it is inside $_home, the service user's deployed copy" ;;
    esac
done
for _p in "$SELF_DIR" "$SELF" "$SELF_DIR/systemd" "$SELF_DIR/systemd/wsi-calc.sh" \
          "$SELF_DIR/lib" "$SELF_DIR/lib/envfile_set.py"; do
    read -r _uid _user _mode < <(stat -L -c '%u %U %a' -- "$_p" 2>/dev/null) \
        || untrusted_tree "$_p is missing"
    if [[ "$_uid" != 0 && "$_uid" != "$EUID" && "$_uid" != "${SUDO_UID:-}" ]]; then
        untrusted_tree "$_p is owned by $_user, neither root nor you"
    fi
    if (( 8#$_mode & 8#002 )); then
        untrusted_tree "$_p is writable by every user"
    fi
done
unset _home _p _uid _user _mode

# Share the exact worker / cache maths used by start-wsi-app.sh so this
# preview can't drift from what the service will actually do at startup.
# shellcheck source=systemd/wsi-calc.sh
. "${SELF_DIR}/systemd/wsi-calc.sh"

# ---- helpers -------------------------------------------------------

bold()  { printf '\033[1m%s\033[0m' "$*"; }
green() { printf '\033[32m%s\033[0m' "$*"; }
yellow(){ printf '\033[33m%s\033[0m' "$*"; }
red()   { printf '\033[31m%s\033[0m' "$*"; }
ask()   { printf '%s ' "$*" >&2; read -r REPLY; printf '%s' "$REPLY"; }
die()   { red "Error: $*"; echo; exit 1; }

# ---- read a value from the env file --------------------------------

file_value() {
    # Whitespace around the '=' is tolerated on purpose. It is tempting to
    # assume `KEY = value` cannot work because the name would be "KEY " — it
    # is not so: systemd's env-file parser records where the key's trailing
    # whitespace starts and truncates there before storing, so
    # `UVICORN_WORKERS = 4` reaches the service as UVICORN_WORKERS=4. (See
    # the `last_key_whitespace` handling in systemd's src/basic/env-file.c.)
    # Leading indentation is skipped too.
    # Narrowing this pattern would make the preview miss a setting the
    # service honours, so it stays as loose as the wsi.env read-back of
    # install.sh / update.sh (wsi_env_value in lib/wsi-common.sh) and
    # set-cache-size.sh, which use the same key pattern.
    #
    # The agreement is key-side only. The value side below differs from
    # install.sh's (which is end-anchored and rejects '#'/'"' outright), and
    # `tr -d` squeezes out interior whitespace, so `KEY=4 5` previews as 45
    # while the service receives "4 5", rejects it, and falls back to the
    # default formula. Noted so the reader does not take "tolerant" for
    # "identical".
    local key="$1"
    if [[ -f "$ENV_FILE" ]]; then
        grep -E "^[[:space:]]*${key}[[:space:]]*=" "$ENV_FILE" \
            | tail -1 \
            | sed 's/.*=[[:space:]]*//' \
            | tr -d "[:space:]\"'" \
            || true
    fi
}

# ---- simulate start-wsi-app.sh: compute effective worker count -----
# Args: explicit_count  percent  cpu_cores
# Either explicit_count or percent may be empty (not "").
# Delegates to the shared helper so the RAM cap on the default path is
# applied identically to the live service.

compute_workers() {
    wsi_compute_workers "$1" "$2" "$3" "${MEM_BYTES:-}"
}

# ---- human-readable byte formatter (for the cache preview) ---------

fmt_bytes() {
    local b="$1"
    if [[ ! "$b" =~ ^[0-9]+$ ]]; then printf '%s' "$b"; return; fi
    awk -v b="$b" 'BEGIN{
        split("B KiB MiB GiB TiB", u, " "); i=1;
        while (b>=1024 && i<5){b/=1024; i++}
        printf (i==1 ? "%d %s" : "%.2f %s"), b, u[i]
    }'
}

# ---- count live worker processes (children of the systemd main PID) -

live_workers() {
    local main_pid n
    main_pid=$(systemctl show "$SERVICE" --property=MainPID --value 2>/dev/null || true)
    if [[ -n "$main_pid" && "$main_pid" != "0" ]]; then
        # With --workers N > 1 uvicorn starts each worker through
        # multiprocessing "spawn", next to a resource-tracker child that is
        # no worker; with one worker the master serves requests itself and
        # has no children at all.
        n=$(pgrep -P "$main_pid" -f 'multiprocessing\.spawn' 2>/dev/null | wc -l || true)
        if [[ "${n:-0}" -gt 0 ]]; then
            echo "$n"
        elif [[ -d "/proc/$main_pid" ]]; then
            echo 1
        fi
    fi
}

# ---- main ----------------------------------------------------------

# Adopt the WSI_CACHE_* tunables out of the env file before any preview
# maths runs. wsi-calc.sh reads them from the *process* environment, but at
# runtime they reach the service through EnvironmentFile= — and sudo's
# env_reset strips them from the operator's shell. Without this the preview
# would silently use the built-in defaults while the service uses the
# operator's values, making "Effective at next start" wrong and
# contradicting operations.md's promise that this preview matches startup.
#
# The file wins outright — a value exported in the operator's shell is
# deliberately overridden. The service's environment comes from the env file
# and nothing else, so honouring the shell here would predict a startup that
# cannot happen; it is the same divergence this loop exists to remove. This
# also matches how the script already reads UVICORN_WORKERS,
# UVICORN_WORKERS_PERCENT and DZI_CACHE_MAX_BYTES below: env file only.
for _wsi_cache_key in WSI_CACHE_RAM_FRACTION WSI_CACHE_RESERVE_MIN_BYTES \
                      WSI_CACHE_FLOOR_BYTES WSI_CACHE_CEIL_BYTES; do
    _wsi_cache_val="$(file_value "$_wsi_cache_key")"
    if [[ -n "$_wsi_cache_val" ]]; then
        export "$_wsi_cache_key=$_wsi_cache_val"
    else
        # Not set in the file -> the service would use the built-in default,
        # so drop any inherited value rather than letting it colour the preview.
        unset "$_wsi_cache_key"
    fi
done
unset _wsi_cache_key _wsi_cache_val

CPU_CORES=$(nproc)
MEM_BYTES=$(wsi_mem_total_bytes)

echo
bold "WSI Uvicorn Worker Configuration"
echo
printf '  Environment file : %s\n' "$ENV_FILE"
printf '  Service          : %s\n' "$SERVICE"
printf '  CPU cores        : %s\n' "$(bold "$CPU_CORES")"
printf '  System RAM       : %s\n' "$(bold "$(fmt_bytes "${MEM_BYTES:-unknown}")")"
echo

if [[ $EUID -ne 0 ]]; then
    yellow "Warning: not running as root — you may not be able to write $ENV_FILE."
    echo
fi

# Read current values from file
CUR_EXPLICIT=$(file_value UVICORN_WORKERS)
CUR_PERCENT=$(file_value UVICORN_WORKERS_PERCENT)
CUR_EFFECTIVE=$(compute_workers "$CUR_EXPLICIT" "$CUR_PERCENT" "$CPU_CORES")
LIVE_W=$(live_workers)

echo "Current settings in $ENV_FILE:"

# Report an override as "active" only if wsi_compute_workers would actually
# honour it. A value it rejects (non-numeric, or too large for 64-bit
# arithmetic) is replaced by the default formula and a warning on stderr, so
# calling it "active — takes priority" here would contradict both that
# warning and the effective count printed three lines below. Mirrors the
# same guard on start-wsi-app.sh's journal summary.
if [[ -n "$CUR_EXPLICIT" ]]; then
    # `$?` inside the else branch is the condition's status, so the reject
    # code is available without calling wsi_valid_uint a second time.
    # Reusing wsi-calc's own reason text keeps this line from saying
    # something different from the warning wsi_compute_workers prints for
    # the very same value a few lines above.
    if wsi_valid_uint "$CUR_EXPLICIT" >/dev/null 2>&1; then
        printf '  UVICORN_WORKERS         : %s\n' "$(bold "$CUR_EXPLICIT") (active — takes priority)"
    else
        EXPLICIT_RC=$?
        printf '  UVICORN_WORKERS         : %s\n' \
            "$(red "$CUR_EXPLICIT") $(yellow "(ignored — $(_wsi_reject_reason "$EXPLICIT_RC"); the default formula applies)")"
    fi
else
    printf '  UVICORN_WORKERS         : %s\n' "$(yellow "(not set)")"
fi

if [[ -n "$CUR_PERCENT" ]]; then
    if wsi_valid_uint "$CUR_PERCENT" >/dev/null 2>&1; then
        printf '  UVICORN_WORKERS_PERCENT : %s\n' "$(bold "${CUR_PERCENT}%")"
    else
        PERCENT_RC=$?
        printf '  UVICORN_WORKERS_PERCENT : %s\n' \
            "$(red "$CUR_PERCENT") $(yellow "(ignored — $(_wsi_reject_reason "$PERCENT_RC"))")"
    fi
else
    printf '  UVICORN_WORKERS_PERCENT : %s\n' "$(yellow "(not set — default formula min(max(${DEFAULT_FLOOR}, cores/2), ${DEFAULT_CAP}) applies)")"
fi

printf '  Effective at next start : %s\n' "$(bold "$CUR_EFFECTIVE") worker(s)"

if [[ -n "$LIVE_W" && "$LIVE_W" != "0" ]]; then
    printf '  Live worker count now   : %s\n' "$(bold "$LIVE_W")"
else
    printf '  Live worker count now   : %s\n' "$(yellow "(service not running or could not detect)")"
fi

# Preview the per-worker DZI byte cache that start-wsi-app.sh would set.
# Pinned DZI_CACHE_MAX_BYTES wins; otherwise it is auto-derived from RAM.
CUR_CACHE_BYTES=$(file_value DZI_CACHE_MAX_BYTES)
if [[ -n "$CUR_CACHE_BYTES" ]]; then
    printf '  DZI_CACHE_MAX_BYTES     : %s (explicit)\n' "$(bold "$(fmt_bytes "$CUR_CACHE_BYTES")")"
else
    AUTO_CACHE_BYTES=$(wsi_compute_cache_max_bytes "${MEM_BYTES:-}" "$CUR_EFFECTIVE")
    if [[ -n "$AUTO_CACHE_BYTES" ]]; then
        printf '  DZI_CACHE_MAX_BYTES     : %s / worker %s\n' \
            "$(bold "$(fmt_bytes "$AUTO_CACHE_BYTES")")" \
            "$(yellow "(auto from $(wsi_cache_fraction)% of RAM ÷ ${CUR_EFFECTIVE})")"
    else
        printf '  DZI_CACHE_MAX_BYTES     : %s\n' "$(yellow "(auto — RAM unreadable, app default applies)")"
    fi
fi

echo
echo "Notes:"
printf '  - UVICORN_WORKERS (explicit) takes priority over UVICORN_WORKERS_PERCENT.\n'
if [[ "$(wsi_cache_floor)" == 0 ]]; then
    printf '  - WSI_CACHE_FLOOR_BYTES=0 ("no floor"), so the default path is NOT capped\n'
    printf '    by RAM; the formula result stands.\n'
else
    printf '  - On the default path, the worker count is also capped so each worker can\n'
    printf '    hold a useful (>= %s) DZI byte cache; explicit settings bypass that cap.\n' "$(fmt_bytes "$(wsi_cache_floor)")"
fi
printf '  - Worker count is always clamped to [1, %s] (this machine'\''s core count).\n' "$CPU_CORES"
printf '  - Each worker is an independent Python process with its own WSI tile cache\n'
printf '    (DZI_CACHE_SIZE entries). Total effective cache = workers × DZI_CACHE_SIZE.\n'
printf '  - DZI_CACHE_MAX_BYTES is auto-derived from RAM per worker unless pinned;\n'
printf '    fewer workers therefore means a larger byte cache each.\n'
printf '  - More workers improve request concurrency but increase memory usage.\n'
printf '  - Changes require a service restart to take effect.\n'
echo

# Ask: mode
echo "How would you like to specify the worker count?"
echo "  1) Explicit count          (sets UVICORN_WORKERS directly)"
echo "  2) Percentage of CPU cores (sets UVICORN_WORKERS_PERCENT)"
echo "  3) Cancel"
echo
MODE=$(ask "Choice [1/2/3]:")

case "$MODE" in
    1)
        while true; do
            NEW_VAL=$(ask "Enter number of workers (1–${CPU_CORES}, blank to cancel):")
            [[ -z "$NEW_VAL" ]] && { echo "Cancelled."; exit 0; }
            if [[ "$NEW_VAL" =~ ^[1-9][0-9]*$ ]] && [[ "$NEW_VAL" -le "$CPU_CORES" ]]; then
                break
            fi
            red "Please enter an integer between 1 and ${CPU_CORES}."
            echo
        done
        WRITE_KEY="UVICORN_WORKERS"
        REMOVE_KEY="UVICORN_WORKERS_PERCENT"
        PREVIEW="${NEW_VAL} worker(s) (explicit)"
        ;;
    2)
        while true; do
            NEW_VAL=$(ask "Enter percentage of CPU cores (1–100, blank to cancel):")
            [[ -z "$NEW_VAL" ]] && { echo "Cancelled."; exit 0; }
            if [[ "$NEW_VAL" =~ ^[1-9][0-9]*$ ]] && [[ "$NEW_VAL" -le 100 ]]; then
                break
            fi
            red "Please enter an integer between 1 and 100."
            echo
        done
        WRITE_KEY="UVICORN_WORKERS_PERCENT"
        REMOVE_KEY="UVICORN_WORKERS"
        COMPUTED=$(compute_workers "" "$NEW_VAL" "$CPU_CORES")
        PREVIEW="${NEW_VAL}% → ${COMPUTED} worker(s) on this machine"
        ;;
    3|"")
        echo "Cancelled."; exit 0
        ;;
    *)
        die "Invalid choice."
        ;;
esac

echo
printf '  Setting   : %s=%s\n' "$WRITE_KEY" "$(bold "$NEW_VAL")"
printf '  Removing  : %s (commented out if present)\n' "$REMOVE_KEY"
printf '  Effect    : %s\n' "$PREVIEW"
echo

CONFIRM=$(ask "Apply this change to $ENV_FILE? [y/N]")
[[ ! "$CONFIRM" =~ ^[Yy]$ ]] && { echo "Cancelled."; exit 0; }

[[ -f "$ENV_FILE" ]] || die "$ENV_FILE not found. Create it first (see infra/wsi.env.example)."

# The rewrite is in Python (no sed metacharacter escaping to get right) and
# shared with set-cache-size.sh: infra/lib/envfile_set.py, found relative to
# this script. It sets WRITE_KEY, comments out a live REMOVE_KEY so the precedence
# rule (UVICORN_WORKERS over UVICORN_WORKERS_PERCENT) is honoured, and
# replaces the file atomically. -I: no PYTHON* variables, no user site.
python3 -I "${SELF_DIR}/lib/envfile_set.py" "$ENV_FILE" "$WRITE_KEY" "$NEW_VAL" \
    --comment-out "$REMOVE_KEY" \
    --tool set-workers.sh --heading "Uvicorn workers — set by set-workers.sh"

green "✓ ${WRITE_KEY}=${NEW_VAL} written to $ENV_FILE"
echo
echo "Note: this change takes effect after the service restarts."
echo

# Offer restart
if systemctl is-active --quiet "$SERVICE" 2>/dev/null; then
    RESTART=$(ask "Restart $SERVICE now to apply immediately? [y/N]")
    if [[ "$RESTART" =~ ^[Yy]$ ]]; then
        systemctl restart "$SERVICE"
        green "✓ $SERVICE restarted."
        echo
        printf '  Checking health'
        HEALTH_OK=0
        for _ in $(seq 1 20); do
            if curl -fsS --max-time 2 http://127.0.0.1:8000/api/health >/dev/null 2>&1; then
                HEALTH_OK=1; break
            fi
            printf '.'
            sleep 1
        done
        echo
        if [[ "$HEALTH_OK" -eq 1 ]]; then
            green "✓ Backend healthy."
        else
            yellow "Backend did not respond in 20s."
            yellow "Check: sudo journalctl -u $SERVICE -n 30 --no-pager"
        fi
        echo
    else
        yellow "Service not restarted. Use:  sudo systemctl restart $SERVICE"
        echo
    fi
else
    yellow "Service $SERVICE does not appear to be running."
    yellow "Start it with:  sudo systemctl start $SERVICE"
    echo
fi
