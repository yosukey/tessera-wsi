#!/usr/bin/env bash
# Unit tests for install.sh's apply_upload_body_cap() — what sizes nginx's
# slide upload / replace body cap from WSI_MAX_UPLOAD_BYTES — and
# apply_tour_asset_body_cap(), the same for the tour-asset upload location
# and TOUR_ASSET_MAX_BYTES.
# Run: bash infra/tests/test_install_upload_cap.sh
#
# WHY THIS EXISTS
# ---------------
# Raising the slide upload limit by editing client_max_body_size in the
# git-tracked infra/nginx/wsi-locations.conf would make update.sh's
# `git checkout <tag>` abort on the local change (and a rollback's
# `git reset --hard` silently drop it). Instead the operator raises
# WSI_MAX_UPLOAD_BYTES in /etc/wsi/wsi.env and install.sh rewrites the two
# `client_max_body_size 8193m;` lines of the INSTALLED snippet to fit. That only
# works while:
#
#   1. the real snippet still has exactly those two lines (the upload and the
#      replace location) — a changed default would make the rewrite a no-op;
#   2. a raised cap lands on both lines, as cap + 1 MiB of multipart framing
#      in whole MiB (rounded up), and nothing else in the file changes;
#   3. the default, a lower cap, an unset or a malformed value leave the
#      snippet alone (a malformed one with a warning).
#
# The function is extracted from the real install.sh by name rather than
# copied, so an edit to it is exercised here. Runs in a temp dir; needs no
# root and no nginx.
set -uo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
INSTALL_SH="${HERE}/../install.sh"
SNIPPET="${HERE}/../nginx/wsi-locations.conf"

fails=0
checks=0
expect() {
    # expect LABEL EXPECTED ACTUAL
    checks=$(( checks + 1 ))
    if [[ "$2" == "$3" ]]; then
        printf 'ok   - %s\n' "$1"
    else
        printf 'FAIL - %s: expected %q, got %q\n' "$1" "$2" "$3"
        fails=$(( fails + 1 ))
    fi
}

[[ -r "$INSTALL_SH" ]] || { echo "FAILED: cannot read $INSTALL_SH"; exit 1; }
[[ -r "$SNIPPET" ]] || { echo "FAILED: cannot read $SNIPPET"; exit 1; }

extract_fn() {
    # A plain string match, not a regex built from the name: mawk (Ubuntu's
    # awk) and gawk unescape a -v value differently.
    awk -v start="$1() {" '
        $0 == start      { grabbing = 1 }
        grabbing         { print }
        grabbing && /^\}$/ { exit }
    ' "$INSTALL_SH"
}
FN_SRC="$(extract_fn apply_upload_body_cap)"
if [[ -z "$FN_SRC" ]]; then
    echo "FAILED: could not extract apply_upload_body_cap() from $INSTALL_SH"
    exit 1
fi
TOUR_FN_SRC="$(extract_fn apply_tour_asset_body_cap)"
if [[ -z "$TOUR_FN_SRC" ]]; then
    echo "FAILED: could not extract apply_tour_asset_body_cap() from $INSTALL_SH"
    exit 1
fi
# ... and install.sh must call it on the snippet it installs, with the value
# from wsi.env.
# (install_nginx_site stages the snippet, caps the staged copy and installs
# that, so the cap is checked by nginx -t and rolled back with the rest.)
grep -qF 'UPLOAD_CAP_BYTES="$(wsi_env_value WSI_MAX_UPLOAD_BYTES "$ETC_DIR/wsi.env")"' "$INSTALL_SH" \
    && grep -qF 'apply_upload_body_cap "$staged_snippet" "$UPLOAD_CAP_BYTES"' "$INSTALL_SH" \
    && grep -qF 'install -m 0644 "$staged_snippet" "$snippet"' "$INSTALL_SH"
expect "install.sh applies it to the snippet it installs, from wsi.env" 0 $?
grep -qF 'TOUR_ASSET_CAP_BYTES="$(wsi_env_value TOUR_ASSET_MAX_BYTES "$ETC_DIR/wsi.env")"' "$INSTALL_SH" \
    && grep -qF 'apply_tour_asset_body_cap "$staged_snippet" "$TOUR_ASSET_CAP_BYTES"' "$INSTALL_SH"
expect "install.sh applies the tour-asset cap too, from wsi.env" 0 $?

TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
WARNED=0
log() { :; }
warn() { WARNED=$(( WARNED + 1 )); }
# shellcheck source=../lib/wsi-common.sh
. "${HERE}/../lib/wsi-common.sh"
eval "$FN_SRC"
eval "$TOUR_FN_SRC"

# 1. The real snippet has the two lines the rewrite targets.
expect "the snippet has exactly two 'client_max_body_size 8193m;' lines" 2 \
    "$(grep -cE '^[[:space:]]*client_max_body_size 8193m;' "$SNIPPET")"

# run VALUE -> the snippet after apply_upload_body_cap, as "<n changed lines> <caps>"
run() {
    cp "$SNIPPET" "$TMP/snippet.conf"
    apply_upload_body_cap "$TMP/snippet.conf" "$1"
    local changed caps
    changed="$(diff "$SNIPPET" "$TMP/snippet.conf" | grep -c '^>')"
    caps="$(grep -oE 'client_max_body_size ([0-9]{4,}m|[0-9]+g);' "$TMP/snippet.conf" | sort | uniq -c | awk '{printf "%s*%s ", $1, $3}')"
    printf '%s %s' "$changed" "${caps% }"
}

# 2. Raised caps.
expect "16 GiB -> 16385m on both lines"         "2 2*16385m;"  "$(run 17179869184)"
expect "16 GiB with stray spaces"               "2 2*16385m;"  "$(run ' 17179869184 ')"
expect "10 GB (decimal) -> 9538m"               "2 2*9538m;"   "$(run 10000000000)"
expect "8 GiB + 1 byte -> 8194m"                "2 2*8194m;"   "$(run 8589934593)"
expect "a terabyte -> 953676m"                  "2 2*953676m;" "$(run 1000000000000)"

# 3. Left alone.
expect "unset"                                  "0 2*8193m;"   "$(run '')"
expect "the 8 GiB default"                      "0 2*8193m;"   "$(run 8589934592)"
expect "a lower cap (4 GiB)"                    "0 2*8193m;"   "$(run 4294967296)"
WARNED=0
expect "malformed ('16g')"                      "0 2*8193m;"   "$(run 16g)"
run 16g >/dev/null; expect "... with a warning" 1 "$WARNED"
WARNED=0
run 99999999999999999999 >/dev/null
expect "too long to be a byte count: warned, left alone" "1 0" "$WARNED $(diff -q "$SNIPPET" "$TMP/snippet.conf" >/dev/null; echo $?)"

# Through install.sh's own read of wsi.env: the operator's appended line wins
# over the template's commented one.
printf '%s\n' '# --- Upload limits ---' '#WSI_MAX_UPLOAD_BYTES=1' 'WSI_MAX_UPLOAD_BYTES=8589934592' \
    'WSI_MAX_UPLOAD_BYTES=21474836480' > "$TMP/wsi.env"
expect "20 GiB read from wsi.env -> 20481m"     "2 2*20481m;" "$(run "$(wsi_env_value WSI_MAX_UPLOAD_BYTES "$TMP/wsi.env")")"

# ---- Tour-asset upload location ------------------------------------
TOUR_LOC='location ~ ^/api/admin/tours/[0-9]+/assets/upload$ {'
# The cap line of the tour-asset location only (a plain string match, as in
# extract_fn).
tour_cap() {
    awk -v loc="$TOUR_LOC" '
        $0 == loc           { inside = 1 }
        inside && /client_max_body_size/ { gsub(/^[[:space:]]+/, ""); print; exit }
    ' "$1"
}
expect "the tour-asset location exists with 'client_max_body_size 3m;'" \
    "client_max_body_size 3m;" "$(tour_cap "$SNIPPET")"

# trun VALUE -> "<n changed lines> <the tour-asset location's cap line>"
trun() {
    cp "$SNIPPET" "$TMP/snippet.conf"
    apply_tour_asset_body_cap "$TMP/snippet.conf" "$1"
    printf '%s %s' "$(diff "$SNIPPET" "$TMP/snippet.conf" | grep -c '^>')" "$(tour_cap "$TMP/snippet.conf")"
}
expect "10 MiB -> 11m on that line only"        "1 client_max_body_size 11m;" "$(trun 10485760)"
expect "2.5 MiB + 1 byte -> 4m"                 "1 client_max_body_size 4m;"  "$(trun 2621441)"
expect "unset: left at 3m"                      "0 client_max_body_size 3m;"  "$(trun '')"
expect "the 2.5 MiB default: left at 3m"        "0 client_max_body_size 3m;"  "$(trun 2621440)"
expect "a lower cap (1 MiB): left at 3m"        "0 client_max_body_size 3m;"  "$(trun 1048576)"
WARNED=0
expect "malformed ('10m'): left at 3m"          "0 client_max_body_size 3m;"  "$(trun 10m)"
trun 10m >/dev/null; expect "... with a warning" 1 "$WARNED"
# The other 3m location (viewer-settings images) keeps its cap.
cp "$SNIPPET" "$TMP/snippet.conf"
apply_tour_asset_body_cap "$TMP/snippet.conf" 10485760
expect "other 3m lines are untouched" \
    "$(( $(grep -c 'client_max_body_size 3m;' "$SNIPPET") - 1 ))" \
    "$(grep -c 'client_max_body_size 3m;' "$TMP/snippet.conf")"

echo "--------------------------------------------------------------"
if (( fails )); then
    echo "FAILED: ${fails}/${checks} checks"
    exit 1
fi
echo "PASS - ${checks} checks: the slide upload cap follows WSI_MAX_UPLOAD_BYTES, the tour-asset cap TOUR_ASSET_MAX_BYTES"
