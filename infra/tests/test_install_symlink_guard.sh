#!/usr/bin/env bash
# Tests for install.sh's ensure_data_subdir() — how the root-run installer
# creates and normalises the directories under /var/lib/wsi.
# Run (as root): sudo bash infra/tests/test_install_symlink_guard.sh
#
# WHY THIS EXISTS
# ---------------
# /var/lib/wsi is owned by the service user, so that user (or a compromised
# app running as it) can replace e.g. thumbs/ with a symlink to a root-owned
# directory. Running `install -d -o wsi` over those paths would chown a
# symlink's TARGET — so the next update would hand the service user that
# root-owned directory. ensure_data_subdir() must never create, chown or
# chmod through a symlink, while still fixing up genuine directories — and
# that includes a PARENT component swapped for a link after the function's
# symlink check (a path-based root mkdir / `chown -h` resolves the swapped
# parent), which the "race" cases below simulate.
#
# The function is extracted from the real install.sh by name (as in
# test_install_env_persist.sh), so an edit to it is exercised here. It needs
# root (it chowns) and an unprivileged account to play the service user
# ("nobody"); SKIPs (exit 0) otherwise.
set -uo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
INSTALL_SH="${HERE}/../install.sh"

skip() { printf 'SKIP - %s\n' "$1"; exit 0; }
[[ ${EUID:-$(id -u)} -eq 0 ]] || skip "must run as root (the function chowns)"
command -v sudo >/dev/null 2>&1 || skip "sudo not available"
id nobody >/dev/null 2>&1 || skip "no 'nobody' account to act as the service user"

fails=0
expect() {
    # expect LABEL EXPECTED ACTUAL
    if [[ "$2" == "$3" ]]; then
        printf 'ok   - %s\n' "$1"
    else
        printf 'FAIL - %s: expected %q, got %q\n' "$1" "$2" "$3"
        fails=$(( fails + 1 ))
    fi
}

FN_SRC="$(awk '
    /^ensure_data_subdir\(\) \{$/ { grabbing = 1 }
    grabbing                      { print }
    grabbing && /^\}$/            { exit }
' "$INSTALL_SH")"
if [[ "$FN_SRC" != *"ensure_data_subdir() {"* ]]; then
    echo "FAILED: could not extract ensure_data_subdir() from $INSTALL_SH"
    exit 1
fi

TMP_ROOT="$(mktemp -d)"
trap 'rm -rf "$TMP_ROOT"' EXIT
chmod 0755 "$TMP_ROOT"

APP_USER=nobody
APP_GROUP="$(id -gn nobody)"
DATA_DIR="$TMP_ROOT/data"
install -d -m 0755 -o "$APP_USER" -g "$APP_GROUP" "$DATA_DIR"
POST_INSTALL_WARNINGS=()
warn() { :; }
eval "$FN_SRC"

owner_mode() { stat -c '%U:%a' "$1"; }

# Root-owned "victims" the service user would like to take over.
VICTIM="$TMP_ROOT/victim";   install -d -m 0700 -o root -g root "$VICTIM"
VICTIM2="$TMP_ROOT/victim2"; install -d -m 0700 -o root -g root "$VICTIM2"
# The service user plants links in its own directory.
# (The real sudo: the `sudo` wrapper function further down is defined later.)
# shellcheck disable=SC2218
sudo -u "$APP_USER" ln -s "$VICTIM"  "$DATA_DIR/thumbs"
# shellcheck disable=SC2218
sudo -u "$APP_USER" ln -s "$VICTIM2" "$DATA_DIR/assets"
# A genuine directory left root-owned by some earlier tool must still be fixed.
install -d -m 0700 -o root -g root "$DATA_DIR/cache"

ensure_data_subdir 0755 "$DATA_DIR/wsi" "$DATA_DIR/thumbs" "$DATA_DIR/cache" \
                        "$DATA_DIR/assets" "$DATA_DIR/assets/tours"
rc_a=$?
ensure_data_subdir 0750 "$DATA_DIR/backups" "$DATA_DIR/backups/.staging"
rc_b=$?

expect "returns success"                           "0 0"            "$rc_a $rc_b"
expect "fresh dir created for the service user"    "nobody:755"     "$(owner_mode "$DATA_DIR/wsi")"
expect "0750 dirs created with their mode"         "nobody:750"     "$(owner_mode "$DATA_DIR/backups/.staging")"
expect "root-owned real dir is chowned + chmod'd"  "nobody:755"     "$(owner_mode "$DATA_DIR/cache")"
expect "symlinked thumbs/ is left a symlink"       "yes"            "$([[ -L "$DATA_DIR/thumbs" ]] && echo yes)"
expect "its root-owned target keeps owner + mode"  "root:700"       "$(owner_mode "$VICTIM")"
expect "target of symlinked parent keeps owner"    "root:700"       "$(owner_mode "$VICTIM2")"
expect "nothing is created below a symlinked parent" "0"            "$(find "$VICTIM2" -mindepth 1 | wc -l | tr -d ' ')"
# thumbs, assets, and assets/tours (skipped via its parent).
expect "each skipped path is reported"             "3"              "${#POST_INSTALL_WARNINGS[@]}"

# --- Race: a PARENT swapped for a symlink after the -L walk ---------------
# The walk only checks; the service user can swap race/ for a link between
# that check and the operation. Simulate exactly that window: wrappers
# perform the swap right before the function's first mkdir, whether it runs
# it via `sudo -u <user> mkdir` or as a plain (root) `mkdir`. A path-based
# root mkdir / `chown -h` of race/tours would then resolve the link and hand
# the victim's tours/ (or a freshly root-created newleaf/) to the service user.
SWAP_BEFORE_MKDIR=""
swap_now() {
    if [[ -n "$SWAP_BEFORE_MKDIR" ]]; then
        eval "$SWAP_BEFORE_MKDIR"
        SWAP_BEFORE_MKDIR=""
    fi
}
sudo()  { [[ "${3:-}" == mkdir ]] && swap_now; command sudo "$@"; }
mkdir() { swap_now; command mkdir "$@"; }
race_case() {
    # race_case NAME LEAF: $DATA_DIR/NAME is a genuine service-user dir at
    # walk time and becomes a link to a root-owned victim before the mkdir.
    local name="$1" leaf="$2" victim="$TMP_ROOT/victim_$1"
    install -d -m 0755 -o root -g root "$victim" "$victim/tours"
    chmod 0700 "$victim/tours"
    install -d -m 0755 -o "$APP_USER" -g "$APP_GROUP" "$DATA_DIR/$name"
    SWAP_BEFORE_MKDIR="mv -T '$DATA_DIR/$name' '$DATA_DIR/$name.real' && ln -s '$victim' '$DATA_DIR/$name'"
    ensure_data_subdir 0755 "$DATA_DIR/$name/$leaf"
}
n_before=${#POST_INSTALL_WARNINGS[@]}
race_case race1 tours
rc_r1=$?
race_case race2 newleaf
rc_r2=$?
expect "race: returns success (reported, not fatal)"  "0 0"      "$rc_r1 $rc_r2"
expect "race: existing target dir keeps owner + mode" "root:700" "$(owner_mode "$TMP_ROOT/victim_race1/tours")"
expect "race: nothing created in the swapped-in target" "tours" \
       "$(find "$TMP_ROOT/victim_race2" -mindepth 1 -printf '%f\n' | sort | tr '\n' ' ' | sed 's/ $//')"
expect "race: each raced path is reported"            "2"        "$(( ${#POST_INSTALL_WARNINGS[@]} - n_before ))"
unset -f sudo mkdir

printf -- '--------------------------------------------------------------\n'
if [[ "$fails" -eq 0 ]]; then
    printf 'PASS - install.sh never chowns/chmods through a service-user symlink\n'
else
    exit 1
fi
