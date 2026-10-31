#!/usr/bin/env bash
# Interactive script to manage the nginx DZI tile cache:
#   - show current cache statistics (disk usage, file count, configured max_size)
#   - clear cache contents on disk and restart nginx
#   - update max_size in the nginx site config and reload nginx
#
# Must be run as root (or with sudo) to delete cache files and to write
# /etc/nginx/sites-available/wsi, from your clone (~/wsi): it refuses to run
# from the deployed copy under /opt/wsi.
# ----------------------------------------------------------------
set -euo pipefail

# Root runs code from this script's own tree: it sources lib/wsi-common.sh
# found next to it. So, like install.sh, it is run from the operator's clone
# and refuses a tree that an account other than root or the operator could
# have rewritten — above all the deployed copy under /opt/wsi, which belongs
# to the service user and has had pip / npm run in it (QUICKSTART §3.4).
# Symlinks are resolved first, so a link into /opt/wsi is caught too, and
# nothing is read from the tree before the check passes. Same check as
# set-workers.sh / set-cache-size.sh.
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
for _p in "$SELF_DIR" "$SELF" "$SELF_DIR/lib" "$SELF_DIR/lib/wsi-common.sh"; do
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

# wsi_nginx_keys_zone_mb: the keys_zone size that goes with a max_size —
# the same function install.sh uses when it re-applies the persisted value.
# shellcheck source=lib/wsi-common.sh
. "${SELF_DIR}/lib/wsi-common.sh"

CACHE_DIR="${WSI_NGINX_CACHE_DIR:-/var/cache/nginx/wsi_dzi}"
# The thumbnails' and tour attachments' own zone (wsi_static_cache in wsi.conf).
# Not sized by this tool, but emptied together with the tile zone: clearing is
# how a replaced thumbnail or overwritten attachment is made to show at once.
STATIC_CACHE_DIR="${WSI_NGINX_STATIC_CACHE_DIR:-/var/cache/nginx/wsi_static}"
LIVE_CONF="${WSI_NGINX_CONF:-/etc/nginx/sites-available/wsi}"
# Where the operator-chosen max_size override is persisted. install.sh /
# update.sh re-apply this onto the freshly installed config after a git
# checkout, so the git-tracked infra/nginx/wsi.conf in the
# clone is never edited (doing so would make update.sh's `git checkout <tag>` abort on local
# changes, or get silently reverted by its `git reset --hard` rollback).
PERSIST_FILE="${WSI_NGINX_CACHE_SIZE_FILE:-/etc/wsi/nginx-cache-max-size}"
NGINX_SVC="nginx"

# ---- helpers -------------------------------------------------------

bold()  { printf '\033[1m%s\033[0m' "$*"; }
green() { printf '\033[32m%s\033[0m' "$*"; }
yellow(){ printf '\033[33m%s\033[0m' "$*"; }
red()   { printf '\033[31m%s\033[0m' "$*"; }
ask()   { printf '%s ' "$*" >&2; read -r REPLY; printf '%s' "$REPLY"; }
die()   { red "Error: $*"; echo; exit 1; }

# Extract a key=value setting from the proxy_cache_path directive in a
# config file. The directive may span multiple lines until ';'.
# Args: file key   (e.g. get_setting wsi.conf max_size  -> "200g")
get_setting() {
    local f="$1" key="$2"
    [[ -f "$f" ]] || return 0
    awk '
        /proxy_cache_path/ { capture=1 }
        capture            { buf = buf " " $0 }
        capture && /;/     { print buf; exit }
    ' "$f" \
        | grep -oE "${key}=[^ ;]+" \
        | head -1 \
        | sed "s|${key}=||"
}

# Validate nginx size format: digits + optional k/m/g suffix.
valid_size() {
    [[ "$1" =~ ^[1-9][0-9]*[kKmMgG]?$ ]]
}

# Replace the max_size= value, and the tile zone's keys_zone size that goes
# with it (wsi_nginx_keys_zone_mb, lib/wsi-common.sh), in a config file in
# place. Defers to
# Python so the regex literal isn't built by string-concatenating user
# input into a shell `sed` command — `valid_size` keeps $newval to
# digits + a single suffix today, but if validation ever loosens the
# Python form is still safe (the `re.sub` replacement is treated as
# literal text, no metacharacter expansion). Only keys_zone=wsi_dzi_cache:
# is resized — the authz verdict zone has its own, fixed size.
#
# -I (isolated mode) is essential: this runs as root, and a script read from
# stdin otherwise has the current directory first on sys.path, so a re.py or
# tempfile.py in whatever directory the operator ran sudo from would be
# imported — and run — as root. -I also ignores PYTHON* variables and the
# user site directory.
update_cache_size_in_file() {
    local f="$1" newval="$2" keys_mb="$3"
    [[ -f "$f" ]] || return 1
    python3 -I - "$f" "$newval" "$keys_mb" <<'PYEOF'
import os
import re
import sys
import tempfile

path, newval, keys_mb = sys.argv[1], sys.argv[2], sys.argv[3]
with open(path, 'r', encoding='utf-8') as fh:
    content = fh.read()
new_content, n = re.subn(
    r'max_size=[0-9]+[kKmMgG]?',
    lambda _m: f'max_size={newval}',
    content,
)
if n == 0:
    sys.exit(f'no max_size= directive found in {path}')
new_content, n = re.subn(
    r'(keys_zone=wsi_dzi_cache:)[0-9]+[kKmMgG]?',
    lambda m: f'{m.group(1)}{keys_mb}m',
    new_content,
)
if n != 1:
    sys.exit(f'expected one keys_zone=wsi_dzi_cache: in {path}, found {n}')
fd, tmp = tempfile.mkstemp(prefix='wsi.nginx.', dir=os.path.dirname(path) or '.')
try:
    with os.fdopen(fd, 'w', encoding='utf-8') as t:
        t.write(new_content)
    os.chmod(tmp, os.stat(path).st_mode & 0o777)
    os.replace(tmp, path)
except Exception:
    os.unlink(tmp)
    raise
PYEOF
}

# ---- actions -------------------------------------------------------

show_status() {
    echo
    bold "WSI nginx Cache Status"
    echo
    printf '  Cache directory   : %s\n' "$CACHE_DIR"
    printf '  Live nginx config : %s\n' "$LIVE_CONF"
    if [[ -f "$PERSIST_FILE" ]]; then
        printf '  Persisted max_size: %s (%s)\n' "$(bold "$(<"$PERSIST_FILE")")" "$PERSIST_FILE"
    fi
    echo

    if [[ -d "$CACHE_DIR" ]]; then
        local size files
        size=$(du -sh "$CACHE_DIR" 2>/dev/null | awk '{print $1}')
        files=$(find "$CACHE_DIR" -type f 2>/dev/null | wc -l)
        printf '  Disk usage        : %s (%s files)\n' \
            "$(bold "${size:-0}")" "$(bold "$files")"
    else
        yellow "  Cache directory not found."
        echo
        printf '  Disk usage        : %s\n' "(n/a)"
    fi

    local max inactive zone
    max=$(get_setting "$LIVE_CONF" max_size || true)
    inactive=$(get_setting "$LIVE_CONF" inactive || true)
    zone=$(get_setting "$LIVE_CONF" keys_zone || true)
    printf '  Configured max    : %s\n' "$(bold "${max:-unknown}")"
    printf '  Configured inactive: %s\n' "$(bold "${inactive:-unknown}")"
    printf '  Keys zone         : %s\n' "$(bold "${zone:-unknown}")"
    # The keys zone indexes every cached file (~8,000 per MB); once full,
    # nginx evicts by LRU whatever max_size says. Point out a pair that does
    # not match (a hand-edited config; install.sh / option 2 write both).
    local want
    if want="$(wsi_nginx_keys_zone_mb "${max:-}")" && [[ "${zone#*:}" != "${want}m" ]]; then
        yellow "  (max_size ${max} goes with a ${want}m keys zone, ~$(( want * 8 ))k cached files;"
        echo
        yellow "   option 2 with the same max_size, or the next install.sh / update.sh, sets it.)"
        echo
    elif [[ -n "$want" ]] && (( $(wsi_nginx_keys_zone_scaled_mb "$max") > want )); then
        yellow "  (capped at a quarter of RAM: the cache evicts at ~$(( want * 8 ))k files, before"
        echo
        yellow "   it reaches max_size ${max}; a smaller max_size would lose nothing.)"
        echo
    fi
    echo
}

clear_cache() {
    [[ -d "$CACHE_DIR" ]] || die "Cache directory $CACHE_DIR does not exist."
    # Safety: refuse to wipe a suspiciously short or root-like path.
    if [[ "${#CACHE_DIR}" -lt 10 || "$CACHE_DIR" == "/" ]]; then
        die "Refusing to clear suspicious path: $CACHE_DIR"
    fi

    local before files
    before=$(du -sh "$CACHE_DIR" 2>/dev/null | awk '{print $1}')
    files=$(find "$CACHE_DIR" -type f 2>/dev/null | wc -l)

    yellow "About to remove all files under $CACHE_DIR (and $STATIC_CACHE_DIR, thumbnails and tour attachments)"
    echo
    printf '  Current size : %s (%s files)\n' "$(bold "${before:-0}")" "$(bold "$files")"
    echo
    local confirm
    confirm=$(ask "Type 'yes' to confirm:")
    [[ "$confirm" == "yes" ]] || { echo "Cancelled."; return 0; }

    # -mindepth 1 keeps the cache directory itself.
    find "$CACHE_DIR" -mindepth 1 -delete
    # The thumbnails / attachments zone goes with it (see STATIC_CACHE_DIR),
    # when its directory exists.
    if [[ -d "$STATIC_CACHE_DIR" && "${#STATIC_CACHE_DIR}" -ge 10 && "$STATIC_CACHE_DIR" != "/" ]]; then
        find "$STATIC_CACHE_DIR" -mindepth 1 -delete
    fi
    green "✓ Cache files removed."
    echo

    if systemctl is-active --quiet "$NGINX_SVC" 2>/dev/null; then
        # restart, not reload: deleting the files out from under a running
        # nginx leaves its in-memory keys_zone still referencing them, so a
        # reload would keep the (now-wrong) size accounting and log "stale"
        # alerts until entries age out. A restart rebuilds the zone from the
        # now-empty directory. The operator already confirmed a full wipe, so
        # the brief restart is in scope.
        systemctl restart "$NGINX_SVC"
        green "✓ nginx restarted (cache keys_zone reset)."
        echo
        yellow "Note: subsequent /dzi/ requests will be MISSes until the cache refills."
        echo
    else
        yellow "nginx is not running; skipping restart."
        echo
        yellow "Start it with:  sudo systemctl start $NGINX_SVC"
        echo
    fi
}

change_max_size() {
    local cur
    cur=$(get_setting "$LIVE_CONF" max_size || true)
    [[ -z "$cur" ]] && cur="(unknown)"

    echo
    echo "  Current max_size in $LIVE_CONF : $(bold "$cur")"
    echo
    echo "Enter new max_size with an optional unit suffix:"
    echo "  k/K = KB    m/M = MB    g/G = GB    (no suffix = bytes)"
    echo "  Examples: 200g, 500m, 50g"
    echo

    local new keys_mb
    while true; do
        new=$(ask "New max_size (blank to cancel):")
        [[ -z "$new" ]] && { echo "Cancelled."; return 0; }
        valid_size "$new" && keys_mb="$(wsi_nginx_keys_zone_mb "$new")" && break
        red "Invalid format. Use digits + optional k/m/g (e.g. 200g)."
        echo
    done

    echo
    echo "  Current   : $(bold "$cur") (keys zone $(get_setting "$LIVE_CONF" keys_zone || true))"
    echo "  New       : $(bold "$new")"
    echo "  Keys zone : $(bold "wsi_dzi_cache:${keys_mb}m") — indexes ~$(( keys_mb * 8 ))k cached files"
    echo "              (1 MB per 128 MB of max_size, at least 200m, at most a quarter"
    echo "              of RAM; without it a larger max_size adds no room). It takes"
    echo "              RAM as the cache fills: up to ~${keys_mb} MB once full."
    # wsi_nginx_keys_zone_mb caps the zone at a quarter of RAM — nginx maps
    # all of it at startup and fails `nginx -t` when it cannot — so a
    # max_size past that point only looks bigger: say so before confirming.
    local scaled_mb
    scaled_mb="$(wsi_nginx_keys_zone_scaled_mb "$new")"
    if (( scaled_mb > keys_mb )); then
        yellow "  Capped: max_size $new calls for a ${scaled_mb}m keys zone, more than a quarter"
        echo
        yellow "  of this host's RAM. With ${keys_mb}m the cache evicts at ~$(( keys_mb * 8 ))k files,"
        echo
        yellow "  before it reaches $new; a smaller max_size would lose nothing."
        echo
    fi
    echo
    local confirm
    confirm=$(ask "Apply this change? [y/N]")
    [[ "$confirm" =~ ^[Yy]$ ]] || { echo "Cancelled."; return 0; }

    [[ -f "$LIVE_CONF" ]] || die "$LIVE_CONF not found."
    # Kept until nginx -t has accepted the change: a config nginx rejects must
    # not stay live (the next nginx restart or reboot would fail and take
    # every site down) nor be persisted (install.sh would re-apply it).
    local backup
    backup="$(mktemp)"
    cp -p -- "$LIVE_CONF" "$backup"
    update_cache_size_in_file "$LIVE_CONF" "$new" "$keys_mb" \
        || { rm -f "$backup"; die "Could not update $LIVE_CONF (unchanged)."; }
    green "✓ Updated $LIVE_CONF"
    echo

    echo "Validating nginx config..."
    if ! nginx -t; then
        cat -- "$backup" > "$LIVE_CONF"
        rm -f "$backup"
        die "nginx -t failed; $LIVE_CONF was put back as it was and nothing was persisted or reloaded."
    fi
    rm -f "$backup"
    echo

    # Persist the chosen size so install.sh / update.sh re-apply it after a
    # git checkout, instead of editing the git-tracked infra/nginx/wsi.conf in
    # the clone (which would break update.sh's checkout / get reverted by its
    # rollback). install.sh reads this file and re-sed's the live config —
    # max_size and, from the same function as here, the keys zone.
    if mkdir -p "$(dirname "$PERSIST_FILE")" 2>/dev/null \
       && printf '%s\n' "$new" > "$PERSIST_FILE" 2>/dev/null; then
        green "✓ Persisted $new to $PERSIST_FILE (install.sh / update.sh re-apply it)"
        echo
    else
        yellow "Could not write $PERSIST_FILE: the value is live now but would be"
        echo
        yellow "reverted by the next install.sh / update.sh run."
        echo
    fi
    echo

    echo "Reloading nginx..."
    systemctl reload "$NGINX_SVC"
    green "✓ nginx reloaded; new max_size=${new} (keys zone ${keys_mb}m) is in effect."
    echo
    yellow "Note: max_size is a soft cap enforced by the cache_manager process."
    echo
    yellow "      Existing files above the new cap are pruned in the background by LRU."
    echo
}

# ---- main ----------------------------------------------------------

if [[ $EUID -ne 0 ]]; then
    yellow "Warning: not running as root — write/reload operations will likely fail."
    echo
fi

show_status

echo "Choose an action:"
echo "  1) Clear cache contents (rm files + restart nginx)"
echo "  2) Change max_size      (edit nginx config + reload nginx)"
echo "  3) Quit"
echo
CHOICE=$(ask "Choice [1/2/3]:")
case "$CHOICE" in
    1) clear_cache ;;
    2) change_max_size ;;
    3|"") echo "Bye." ;;
    *) die "Invalid choice." ;;
esac
