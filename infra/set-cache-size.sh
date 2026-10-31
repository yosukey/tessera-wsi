#!/usr/bin/env bash
# Interactive script to update DZI_CACHE_SIZE in /etc/wsi/wsi.env
# and optionally restart the wsi-app service.
#
# Must be run as root (or with sudo) to write /etc/wsi/wsi.env, from your
# clone (~/wsi): it refuses to run from the deployed copy under /opt/wsi.
# ----------------------------------------------------------------
set -euo pipefail

ENV_FILE="${WSI_ENV:-/etc/wsi/wsi.env}"
SERVICE="wsi-app.service"
DEFAULT_CACHE_SIZE=8

# Root runs code from this script's own tree: lib/envfile_set.py, found next
# to it. So, like install.sh, it is run from the operator's clone and refuses
# a tree that an account other than root or the operator could have
# rewritten — above all the deployed copy under /opt/wsi, which belongs to the
# service user and has had pip / npm run in it (QUICKSTART §3.4). Symlinks are
# resolved first, so a link into /opt/wsi is caught too, and nothing is read
# from the tree before the check passes. Same check as set-workers.sh.
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
for _p in "$SELF_DIR" "$SELF" "$SELF_DIR/lib" "$SELF_DIR/lib/envfile_set.py"; do
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

# ---- helpers -------------------------------------------------------

bold()  { printf '\033[1m%s\033[0m' "$*"; }
green() { printf '\033[32m%s\033[0m' "$*"; }
yellow(){ printf '\033[33m%s\033[0m' "$*"; }
red()   { printf '\033[31m%s\033[0m' "$*"; }
ask()   { printf '%s ' "$*" >&2; read -r REPLY; printf '%s' "$REPLY"; }

die() { red "Error: $*"; echo; exit 1; }

# ---- read current value from env file ------------------------------

current_file_value() {
    # Strip single quotes as well as double: systemd's EnvironmentFile=
    # parser removes both kinds, so DZI_CACHE_SIZE='8' reaches the service
    # as 8 — this preview (and the live-vs-file drift warning below) must
    # read it the same way. Keeps the value handling in agreement with
    # set-workers.sh's file_value.
    if [[ -f "$ENV_FILE" ]]; then
        grep -E '^[[:space:]]*DZI_CACHE_SIZE[[:space:]]*=' "$ENV_FILE" \
            | tail -1 \
            | sed 's/.*=[[:space:]]*//' \
            | tr -d "[:space:]\"'" \
            || true
    fi
}

# ---- read live value from running process --------------------------

LIVE_UNSET=0
# wsi-app's main process (the uvicorn master, which start-wsi-app.sh exec's),
# or nothing when the service is not running.
main_pid() {
    local pid
    pid=$(systemctl show "$SERVICE" --property=MainPID --value 2>/dev/null || true)
    if [[ -n "$pid" && "$pid" != "0" ]]; then
        echo "$pid"
    fi
}

live_value() {
    # Read the environment of wsi-app's main process via /proc. Not a pgrep
    # for uvicorn: that also matches wsi-sync, which keeps its own, older
    # environment.
    local pid
    pid=$(main_pid)
    if [[ -n "$pid" ]] && [[ -r "/proc/$pid/environ" ]]; then
        tr '\0' '\n' < "/proc/$pid/environ" 2>/dev/null \
            | sed -n 's/^DZI_CACHE_SIZE=//p' \
            || true
    fi
}

# ---- main ----------------------------------------------------------

echo
bold "WSI Tile Cache Size Configuration"
echo
echo "  Environment file : $ENV_FILE"
echo "  Service          : $SERVICE"
echo "  Built-in default : $DEFAULT_CACHE_SIZE"
echo

# Warn if not root
if [[ $EUID -ne 0 ]]; then
    yellow "Warning: not running as root — you may not be able to write $ENV_FILE."
    echo
fi

# Show current state
FILE_VAL=$(current_file_value)
LIVE_VAL=$(live_value)
# Running without the variable (it is commented out in the template) means
# the built-in default is in effect.
if [[ -z "$LIVE_VAL" ]] && [[ -n "$(main_pid)" ]]; then
    LIVE_VAL=$DEFAULT_CACHE_SIZE
    LIVE_UNSET=1
fi

if [[ -n "$FILE_VAL" ]]; then
    echo "  Value in $ENV_FILE : $(bold "$FILE_VAL")"
else
    echo "  Value in $ENV_FILE : $(yellow "(not set — built-in default $DEFAULT_CACHE_SIZE will be used)")"
    FILE_VAL=$DEFAULT_CACHE_SIZE
fi

if [[ -n "$LIVE_VAL" ]]; then
    if [[ "$LIVE_UNSET" == 1 ]]; then
        echo "  Value in running process      : $(bold "$LIVE_VAL") (not set — built-in default)"
    else
        echo "  Value in running process      : $(bold "$LIVE_VAL")"
    fi
    if [[ "$LIVE_VAL" != "$FILE_VAL" ]]; then
        yellow "  ⚠  Live value differs from file — service may not have been restarted after the last edit."
        echo
    fi
else
    echo "  Value in running process      : $(yellow "(could not detect — service may not be running)")"
fi

echo
echo "Note: each entry holds one open OpenSlide file handle."
echo "      For N slides viewed concurrently, set max ≥ N to avoid cache thrashing."
echo "      Memory impact scales with slide file sizes × entry count."
echo "      DZI_CACHE_SIZE caps the entry COUNT. The per-worker byte budget is a"
echo "      separate cap (DZI_CACHE_MAX_BYTES), auto-derived from RAM at startup"
echo "      unless pinned in $ENV_FILE. Preview it with: sudo bash infra/set-workers.sh"
echo

# Ask for new value
while true; do
    NEW_VAL=$(ask "Enter new DZI_CACHE_SIZE (positive integer, blank to cancel):")
    [[ -z "$NEW_VAL" ]] && { echo "Cancelled."; exit 0; }
    if [[ "$NEW_VAL" =~ ^[1-9][0-9]*$ ]]; then
        break
    fi
    red "Please enter a positive integer (e.g. 4, 8, 16)."
    echo
done

echo
echo "  Current : $(bold "$FILE_VAL")"
echo "  New     : $(bold "$NEW_VAL")"
echo

CONFIRM=$(ask "Apply this change to $ENV_FILE? [y/N]")
if [[ ! "$CONFIRM" =~ ^[Yy]$ ]]; then
    echo "Cancelled."
    exit 0
fi

# Write to env file
if [[ ! -f "$ENV_FILE" ]]; then
    die "$ENV_FILE not found. Create it first (see infra/wsi.env.example)."
fi

# The rewrite is in Python, shared with set-workers.sh
# (infra/lib/envfile_set.py, found relative to this script): no sed metacharacter
# escaping to get right should the validation above ever be relaxed, and the
# file is replaced atomically, so an interrupted write never leaves it partial.
# -I: no PYTHON* variables, no user site.
python3 -I "${SELF_DIR}/lib/envfile_set.py" "$ENV_FILE" DZI_CACHE_SIZE "$NEW_VAL" \
    --tool set-cache-size.sh --heading "WSI tile cache — set by set-cache-size.sh"

green "✓ DZI_CACHE_SIZE=$NEW_VAL written to $ENV_FILE"
echo
echo "Note: this change takes effect after the service restarts."
echo "      The running process keeps its current value until then."
echo

# Offer restart
if systemctl is-active --quiet "$SERVICE" 2>/dev/null; then
    RESTART=$(ask "Restart $SERVICE now to apply immediately? [y/N]")
    if [[ "$RESTART" =~ ^[Yy]$ ]]; then
        systemctl restart "$SERVICE"
        green "✓ $SERVICE restarted."
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
