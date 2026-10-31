#!/usr/bin/env bash
# Tests for install.sh's install_nginx_site(): a site config that `nginx -t`
# rejects must not stay installed.
# Run: bash infra/tests/test_install_nginx_restore.sh
#
# WHY THIS EXISTS
# ---------------
# install.sh overwrites /etc/nginx/snippets/wsi-locations.conf and
# /etc/nginx/sites-available/wsi, links the site and removes the default site.
# If it did so and only then ran `nginx -t`, a failure would stop the install
# (set -e) with the rejected files in place. The running nginx keeps its
# in-memory config, so nothing would look wrong — until the next nginx restart
# or reboot, which would fail and take every site on the host down.
# Stand-alone install.sh has no rollback of its own. The function therefore
# builds the new site config in a staging file, copies aside everything it
# changes, and puts it all back when installing or `nginx -t` fails.
#
# Also covered here: the staging and backup files are removed however the
# function ends — a failing cp / sed must not end install.sh through its ERR
# trap with 0600 copies left in /tmp; a persisted cache max_size that calls
# for a keys zone larger than a quarter of RAM is capped (otherwise a large
# max_size such as 500g fails nginx -t on small hosts, on every update); and
# the site's access log comes from
# /etc/nginx/snippets/wsi-access-log.conf, installed only when missing so an
# operator's own logging survives updates.
#
# The function is extracted from the real install.sh by name and run against
# a scratch /etc/nginx (NGINX_ETC) with the real wsi.conf / snippet as the new
# files and a stub `nginx` that rejects them on request. No root, no real
# nginx; never touches /etc.
set -uo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
INSTALL_SH="${HERE}/../install.sh"
REPO="$(cd "${HERE}/../.." && pwd)"

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

# install_nginx_site() and the apply_upload_body_cap() /
# apply_tour_asset_body_cap() it calls on the staged snippet.
FN_SRC="$(awk '
    /^(install_nginx_site|install_wsi_tls_snippet|tls_text_has_listener|tls_text_has_content|tls_snippet_attrs|apply_upload_body_cap|apply_tour_asset_body_cap)\(\) \{$/ { grabbing = 1 }
    grabbing                                             { print }
    grabbing && /^\}$/                                   { grabbing = 0 }
' "$INSTALL_SH")"
if [[ "$FN_SRC" != *"install_nginx_site() {"* || "$FN_SRC" != *"apply_upload_body_cap() {"* \
      || "$FN_SRC" != *"install_wsi_tls_snippet() {"* || "$FN_SRC" != *"apply_tour_asset_body_cap() {"* ]]; then
    echo "FAILED: could not extract install_nginx_site() / apply_upload_body_cap() / apply_tour_asset_body_cap() from $INSTALL_SH"
    exit 1
fi

TMP_ROOT="$(mktemp -d)"
trap 'rm -rf "$TMP_ROOT"' EXIT
mkdir -p "$TMP_ROOT/bin" "$TMP_ROOT/tmp" "$TMP_ROOT/etc-wsi"

# Stub nginx: `nginx -t` fails while $TMP_ROOT/reject exists AND the
# installed site config is the new one (it has a proxy_cache_path; the "old"
# configs below do not) — i.e. it rejects exactly the new files.
cat > "$TMP_ROOT/bin/nginx" <<SH
#!/usr/bin/env bash
echo "nginx \$*" >> "$TMP_ROOT/nginx.calls"
if [[ -e "$TMP_ROOT/reject" ]] && grep -q proxy_cache_path "\$NGINX_ETC/sites-available/wsi" 2>/dev/null; then
    echo "nginx: [emerg] simulated rejection" >&2
    exit 1
fi
exit 0
SH
chmod +x "$TMP_ROOT/bin/nginx"

# What install.sh provides around the function.
# shellcheck disable=SC2034  # read by the eval'd function
REPO_DIR="$REPO"
# shellcheck disable=SC2034
WSI_LAN_HOST=""
# shellcheck disable=SC2034
PERSIST_CACHE_SIZE_FILE="$TMP_ROOT/etc-wsi/nginx-cache-max-size"
# shellcheck disable=SC2034
UPLOAD_CAP_BYTES=""
# shellcheck disable=SC2034
TOUR_ASSET_CAP_BYTES=""
log()  { :; }
warn() { printf 'warn: %s\n' "$*"; }
die()  { printf 'died: %s\n' "$*"; exit 1; }
# wsi_nginx_keys_zone_mb, which install.sh sources from the same file.
# shellcheck source=../lib/wsi-common.sh
. "${HERE}/../lib/wsi-common.sh"
eval "$FN_SRC"
# A 64 GiB host unless a test says otherwise, so no keys zone below is capped
# by the RAM of the machine running the tests.
printf 'MemTotal: 67108864 kB\n' > "$TMP_ROOT/meminfo-64g"
printf 'MemTotal: 2097152 kB\n'  > "$TMP_ROOT/meminfo-2g"
export WSI_MEMINFO="$TMP_ROOT/meminfo-64g"

# scratch_etc NAME [fresh] : a scratch /etc/nginx with the default site
# enabled and, unless "fresh", a previous wsi install (old snippet + site).
scratch_etc() {
    NGINX_ETC="$TMP_ROOT/$1"
    export NGINX_ETC
    mkdir -p "$NGINX_ETC/snippets" "$NGINX_ETC/sites-available" "$NGINX_ETC/sites-enabled"
    echo 'server { listen 80 default_server; }' > "$NGINX_ETC/sites-available/default"
    ln -s ../sites-available/default "$NGINX_ETC/sites-enabled/default"
    if [[ "${2:-}" != fresh ]]; then
        echo '# OLD SNIPPET' > "$NGINX_ETC/snippets/wsi-locations.conf"
        echo '# OLD SITE'    > "$NGINX_ETC/sites-available/wsi"
        rm -f "$NGINX_ETC/sites-enabled/default"
        ln -s "$NGINX_ETC/sites-available/wsi" "$NGINX_ETC/sites-enabled/wsi"
    fi
}
# state : one line per managed path — content, or "->target" for a link, or "-".
state() {
    local p
    for p in snippets/wsi-locations.conf sites-available/wsi sites-enabled/wsi sites-enabled/default \
             snippets/wsi-access-log.conf snippets/wsi-tls.conf; do
        if [[ -L "$NGINX_ETC/$p" ]]; then printf '%s->%s|' "$p" "$(readlink "$NGINX_ETC/$p")"
        elif [[ -e "$NGINX_ETC/$p" ]]; then printf '%s:%s|' "$p" "$(cksum < "$NGINX_ETC/$p" | cut -d' ' -f1)"
        else printf '%s:-|' "$p"
        fi
    done
}
run_fn() { ( PATH="$TMP_ROOT/bin:$PATH" TMPDIR="$TMP_ROOT/tmp" install_nginx_site ) 2>&1; }
# As run_fn, and prints the warnings install.sh would list at the end of the run.
POST_INSTALL_WARNINGS=()
run_fn_w() {
    ( PATH="$TMP_ROOT/bin:$PATH" TMPDIR="$TMP_ROOT/tmp" install_nginx_site \
        && { (( ${#POST_INSTALL_WARNINGS[@]} == 0 )) || printf 'WARNING: %s\n' "${POST_INSTALL_WARNINGS[@]}"; } ) 2>&1
}
# As install.sh runs it: errexit + errtrace, and an ERR trap that exits.
run_fn_strict() {
    ( set -Eeuo pipefail
      trap 'echo "ERR trap: $BASH_COMMAND"; exit 1' ERR
      PATH="$TMP_ROOT/bin:$PATH" TMPDIR="$TMP_ROOT/tmp" install_nginx_site ) 2>&1
}

# --- accepted: the new files are live, the default site is off ------------
scratch_etc ok
out="$(run_fn)"; rc=$?
expect "accepted: succeeds" "0" "$rc"
expect "accepted: snippet is the repo's" "same" \
    "$(cmp -s "$REPO/infra/nginx/wsi-locations.conf" "$NGINX_ETC/snippets/wsi-locations.conf" && echo same)"
expect "accepted: site config is the repo's wsi.conf" "same" \
    "$(cmp -s "$REPO/infra/nginx/wsi.conf" "$NGINX_ETC/sites-available/wsi" && echo same)"
expect "accepted: site enabled, default site disabled" "$NGINX_ETC/sites-available/wsi|-" \
    "$(readlink "$NGINX_ETC/sites-enabled/wsi")|$([[ -e "$NGINX_ETC/sites-enabled/default" ]] && echo there || echo -)"
expect "accepted: no staging / backup left in TMPDIR" "" "$(ls -A "$TMP_ROOT/tmp")"
expect "accepted: the access-log snippet is installed (masked combined log)" "same" \
    "$(cmp -s "$REPO/infra/nginx/wsi-access-log.conf" "$NGINX_ETC/snippets/wsi-access-log.conf" && echo same)"
expect "accepted: the site config includes it" "1" \
    "$(grep -c '^[[:space:]]*include /etc/nginx/snippets/wsi-access-log.conf;' "$NGINX_ETC/sites-available/wsi")"

# --- an operator's own access log is kept, through updates ----------------
scratch_etc own-log
echo 'access_log off;' > "$NGINX_ETC/snippets/wsi-access-log.conf"
out="$(run_fn)"; rc=$?
expect "own access log: install succeeds, the file is left as the operator wrote it" "0|access_log off;" \
    "$rc|$(cat "$NGINX_ETC/snippets/wsi-access-log.conf")"
rm -f "$NGINX_ETC/snippets/wsi-access-log.conf"
ln -s /dev/null "$NGINX_ETC/snippets/wsi-access-log.conf"
out="$(run_fn)"; rc=$?
expect "own access log as a link: left alone" "0|/dev/null" "$rc|$(readlink "$NGINX_ETC/snippets/wsi-access-log.conf")"

# --- rejected: everything is put back exactly ------------------------------
scratch_etc rejected
before="$(state)"
touch "$TMP_ROOT/reject"
out="$(run_fn)"; rc=$?
expect "rejected: install stops" "1" "$rc"
expect "rejected: snippet, site, links left unchanged" "$before" "$(state)"
expect "rejected: says the previous files are back and valid" "yes" \
    "$(grep -q 'previous nginx files are back in place and pass nginx -t' <<<"$out" && echo yes)"
expect "rejected: no staging / backup left in TMPDIR" "" "$(ls -A "$TMP_ROOT/tmp")"
rm -f "$TMP_ROOT/reject"

# --- a step that fails before anything goes live: no temp files left ------
# Under install.sh's set -Ee and exiting ERR trap. A clone without the
# snippet makes its cp fail; a wsi.conf without the CSP token makes the
# LAN-host edit die.
BROKEN="$TMP_ROOT/broken-repo"
mkdir -p "$BROKEN/infra/nginx" "$BROKEN/infra/lib"
cp "$REPO/infra/nginx/wsi.conf" "$BROKEN/infra/nginx/"
cp "$REPO/infra/lib/nginx_tls_carry.py" "$BROKEN/infra/lib/"
scratch_etc cp-fails
before="$(state)"
out="$(REPO_DIR="$BROKEN" run_fn_strict)"; rc=$?
expect "failing cp: install stops in the ERR trap, files left unchanged" "1|yes|$before" \
    "$rc|$(grep -q 'ERR trap: cp' <<<"$out" && echo yes)|$(state)"
expect "failing cp: no staging / backup left in TMPDIR" "" "$(ls -A "$TMP_ROOT/tmp")"
echo 'server { }' > "$BROKEN/infra/nginx/wsi.conf"
scratch_etc csp-fails
before="$(state)"
out="$(REPO_DIR="$BROKEN" WSI_LAN_HOST=wsi.lan run_fn_strict)"; rc=$?
expect "failed CSP edit: install stops, files left unchanged" "1|yes|$before" \
    "$rc|$(grep -q 'Failed to inject WSI_LAN_HOST' <<<"$out" && echo yes)|$(state)"
expect "failed CSP edit: no staging / backup left in TMPDIR" "" "$(ls -A "$TMP_ROOT/tmp")"
scratch_etc strict-ok
out="$(run_fn_strict)"; rc=$?
expect "strict mode, all well: succeeds, nothing left in TMPDIR" "0|" "$rc|$(ls -A "$TMP_ROOT/tmp")"
touch "$TMP_ROOT/reject"

# --- rejected on a fresh host: new files removed, default site back -------
scratch_etc fresh fresh
before="$(state)"
out="$(run_fn)"; rc=$?
expect "fresh + rejected: install stops" "1" "$rc"
expect "fresh + rejected: no wsi files left, default site enabled again" "$before" "$(state)"

# --- still invalid after the restore: the message says so ------------------
scratch_etc elsewhere
# The "old" site now also trips the stub: the problem is not the new files.
echo 'proxy_cache_path /x;' >> "$NGINX_ETC/sites-available/wsi"
before="$(state)"
out="$(run_fn)"; rc=$?
expect "broken elsewhere: install stops, files left unchanged" "1|$before" "$rc|$(state)"
expect "broken elsewhere: says the cause is elsewhere" "yes" \
    "$(grep -q 'still fails, so the cause is elsewhere' <<<"$out" && echo yes)"
rm -f "$TMP_ROOT/reject"

# --- a persisted cache size is applied before anything goes live ----------
scratch_etc persisted
echo 123g > "$PERSIST_CACHE_SIZE_FILE"
out="$(run_fn)"; rc=$?
expect "persisted max_size: applied to the installed tile zone" "0|1" \
    "$rc|$(grep -c 'max_size=123g' "$NGINX_ETC/sites-available/wsi")"
# 123g = 125952 MB -> 984 MB of keys (1 MB per 128 MB); the authz zone's own
# fixed size is left alone.
expect "persisted max_size: the tile keys zone is scaled with it" "1|1" \
    "$(grep -c 'keys_zone=wsi_dzi_cache:984m' "$NGINX_ETC/sites-available/wsi")|$(grep -c 'keys_zone=wsi_authz_cache:4m' "$NGINX_ETC/sites-available/wsi")"
expect "persisted max_size under the cap: no warning" "" "$(grep 'capped' <<<"$out")"
# 500g wants 4000m; on a 2 GB host the zone is capped at a quarter of RAM,
# so nginx -t can map it, and the warning names the persisted size.
echo 500g > "$PERSIST_CACHE_SIZE_FILE"
out="$(WSI_MEMINFO="$TMP_ROOT/meminfo-2g" run_fn)"; rc=$?
expect "persisted 500g on a 2 GB host: installed, keys zone capped at 512m" "0|1|1" \
    "$rc|$(grep -c 'max_size=500g' "$NGINX_ETC/sites-available/wsi")|$(grep -c 'keys_zone=wsi_dzi_cache:512m' "$NGINX_ETC/sites-available/wsi")"
expect "persisted 500g on a 2 GB host: the warning names the size, file and cap" "yes" \
    "$(grep -F "max_size=500g (from $PERSIST_CACHE_SIZE_FILE) calls for a 4000m keys zone" <<<"$out" \
       | grep -qF 'capped at 512m' && echo yes)"
echo 10t > "$PERSIST_CACHE_SIZE_FILE"
out="$(run_fn)"; rc=$?
expect "malformed persisted size: ignored, stock config installed" "0|same" \
    "$rc|$(cmp -s "$REPO/infra/nginx/wsi.conf" "$NGINX_ETC/sites-available/wsi" && echo same)"
rm -f "$PERSIST_CACHE_SIZE_FILE"

# --- the upload cap from wsi.env lands in the installed snippet -----------
scratch_etc upload-cap
UPLOAD_CAP_BYTES=17179869184
out="$(run_fn)"; rc=$?
expect "upload cap: both slide upload lines raised to 16385m in the installed snippet" "0|2" \
    "$rc|$(grep -c 'client_max_body_size 16385m;' "$NGINX_ETC/snippets/wsi-locations.conf")"
expect "upload cap: the repo's snippet is left as it is" "0" \
    "$(grep -c 'client_max_body_size 16385m;' "$REPO/infra/nginx/wsi-locations.conf")"
UPLOAD_CAP_BYTES=""

scratch_etc tour-asset-cap
TOUR_ASSET_CAP_BYTES=10485760
out="$(run_fn)"; rc=$?
expect "tour-asset cap: its location raised to 11m in the installed snippet" "0|1" \
    "$rc|$(grep -c 'client_max_body_size 11m;' "$NGINX_ETC/snippets/wsi-locations.conf")"
TOUR_ASSET_CAP_BYTES=""

# --- TLS survives the site file being replaced -----------------------------
# The stock server block includes the operator's wsi-tls.conf, which is
# created when missing and never overwritten; a site file certbot had patched
# is migrated into it, and TLS nobody can attribute stops the run.
scratch_etc tls-fresh fresh
out="$(run_fn)"; rc=$?
expect "tls: a fresh install gets the shipped (all-commented) snippet" "0|same" \
    "$rc|$(cmp -s "$REPO/infra/nginx/wsi-tls.conf" "$NGINX_ETC/snippets/wsi-tls.conf" && echo same)"
expect "tls: the site config includes it" "1" \
    "$(grep -c '^[[:space:]]*include /etc/nginx/snippets/wsi-tls.conf;' "$NGINX_ETC/sites-available/wsi")"
expect "tls: the shipped snippet switches nothing on" "0" \
    "$(grep -cE '^[[:space:]]*[^#[:space:]]' "$REPO/infra/nginx/wsi-tls.conf")"

echo 'listen 443 ssl; # my own TLS' > "$NGINX_ETC/snippets/wsi-tls.conf"
out="$(run_fn)"; rc=$?
expect "tls: an operator's snippet is left exactly as written, update after update" "0|listen 443 ssl; # my own TLS" \
    "$rc|$(cat "$NGINX_ETC/snippets/wsi-tls.conf")"

CERTBOT_OLD_SITE='upstream wsi_app { server 127.0.0.1:8000; }
server {
    server_name wsi.example.org;
    add_header Strict-Transport-Security "max-age=31536000; includeSubDomains" always;
    listen [::]:443 ssl ipv6only=on; # managed by Certbot
    listen 443 ssl; # managed by Certbot
    ssl_certificate /etc/letsencrypt/live/wsi.example.org/fullchain.pem; # managed by Certbot
    ssl_certificate_key /etc/letsencrypt/live/wsi.example.org/privkey.pem; # managed by Certbot
    include /etc/letsencrypt/options-ssl-nginx.conf; # managed by Certbot
}
server {
    if ($host = wsi.example.org) {
        return 301 https://$host$request_uri;
    } # managed by Certbot
    listen 80 ;
    server_name wsi.example.org;
    return 404; # managed by Certbot
}'
scratch_etc tls-certbot
printf '%s\n' "$CERTBOT_OLD_SITE" > "$NGINX_ETC/sites-available/wsi"
out="$(run_fn)"; rc=$?
expect "tls: a certbot-patched site is replaced by the stock one" "0|same" \
    "$rc|$(cmp -s "$REPO/infra/nginx/wsi.conf" "$NGINX_ETC/sites-available/wsi" && echo same)"
for want in 'server_name wsi.example.org;' 'listen 443 ssl; # managed by Certbot' \
            'ssl_certificate /etc/letsencrypt/live/wsi.example.org/fullchain.pem; # managed by Certbot' \
            'add_header Strict-Transport-Security "max-age=31536000; includeSubDomains" always;' \
            'set $wsi_tls_redirect "$wsi_xfp:$host";' \
            'if ($wsi_tls_redirect = "http:wsi.example.org") { return 301 https://$host$request_uri; }'; do
    expect "tls: carried into the snippet: ${want:0:50}" "1" \
        "$(grep -cF -- "$want" "$NGINX_ETC/snippets/wsi-tls.conf")"
done
expect "tls: the replaced certbot site is kept as wsi.previous, byte for byte" "same" \
    "$(printf '%s\n' "$CERTBOT_OLD_SITE" | cmp -s - "$NGINX_ETC/sites-available/wsi.previous" && echo same)"
expect "tls: ...and nothing of the site file's own stock content" "0" \
    "$(grep -cE 'upstream|return 404|client_max_body_size' "$NGINX_ETC/snippets/wsi-tls.conf")"
# Run again: the old site is now the stock one, so the snippet stays as migrated.
cp "$NGINX_ETC/snippets/wsi-tls.conf" "$TMP_ROOT/carried.conf"
out="$(run_fn)"; rc=$?
expect "tls: the second run does not overwrite the copy of the original certbot file" "same" \
    "$(printf '%s\n' "$CERTBOT_OLD_SITE" | cmp -s - "$NGINX_ETC/sites-available/wsi.previous" && echo same)"
expect "tls: the next update leaves the migrated snippet alone" "0|same" \
    "$rc|$(cmp -s "$TMP_ROOT/carried.conf" "$NGINX_ETC/snippets/wsi-tls.conf" && echo same)"
expect "tls: ...and made no backup of it" "0" "$(ls "$NGINX_ETC/snippets" | grep -c 'wsi-tls.conf.bak-')"

# `certbot enhance --redirect` on an already migrated host: the site file now
# holds only certbot's redirect block (the listener and certificates are in the
# snippet), so what is carried has no listener of its own. The snippet must keep
# its listener and certificates -- replacing it would switch HTTPS off -- and gain
# only the redirect, once.
scratch_etc tls-enhanced
cat > "$NGINX_ETC/snippets/wsi-tls.conf" <<'SNIP'
# mine
server_name wsi.example.org;
listen 443 ssl; # managed by Certbot
ssl_certificate /etc/letsencrypt/live/wsi.example.org/fullchain.pem; # managed by Certbot
ssl_certificate_key /etc/letsencrypt/live/wsi.example.org/privkey.pem; # managed by Certbot
SNIP
cat > "$NGINX_ETC/sites-available/wsi" <<'SITE'
server {
    listen 443 ssl; # managed by Certbot
    ssl_certificate /x.pem; # managed by Certbot
}
server {
    if ($host = wsi.example.org) {
        return 301 https://$host$request_uri;
    } # managed by Certbot
    listen 80 ;
    server_name wsi.example.org;
    return 404; # managed by Certbot
}
SITE
# (...that is what a site that still has the listener would look like; the
# enhanced one has the redirect block alone:)
cat > "$NGINX_ETC/sites-available/wsi" <<'SITE'
server {
    listen 80 default_server;
    server_name _;
}
server {
    if ($host = wsi.example.org) {
        return 301 https://$host$request_uri;
    } # managed by Certbot
    listen 80 ;
    server_name wsi.example.org;
    return 404; # managed by Certbot
}
SITE
out="$(run_fn)"; rc=$?
expect "tls: a redirect-only carry keeps the snippet's listener and certificates" "0|1|1" \
    "$rc|$(grep -cF 'listen 443 ssl; # managed by Certbot' "$NGINX_ETC/snippets/wsi-tls.conf")|$(grep -cF 'ssl_certificate /etc/letsencrypt/live/wsi.example.org/fullchain.pem' "$NGINX_ETC/snippets/wsi-tls.conf")"
expect "tls: ...and gains the redirect, once" "1|1" \
    "$(grep -cF 'set $wsi_tls_redirect "$wsi_xfp:$host";' "$NGINX_ETC/snippets/wsi-tls.conf")|$(grep -cF 'if ($wsi_tls_redirect = "http:wsi.example.org")' "$NGINX_ETC/snippets/wsi-tls.conf")"
expect "tls: ...keeping the operator's own first line" "# mine" "$(head -1 "$NGINX_ETC/snippets/wsi-tls.conf")"
cp "$NGINX_ETC/snippets/wsi-tls.conf" "$TMP_ROOT/merged.conf"
# The next update finds the (re-)patched file again: nothing to add, nothing changes.
cp "$REPO/infra/nginx/wsi.conf" "$TMP_ROOT/stock.conf"
cat > "$NGINX_ETC/sites-available/wsi" <<'SITE'
server {
    listen 80 default_server;
    server_name _;
}
server {
    if ($host = wsi.example.org) {
        return 301 https://$host$request_uri;
    } # managed by Certbot
    listen 80 ;
    server_name wsi.example.org;
    return 404; # managed by Certbot
}
SITE
out="$(run_fn)"; rc=$?
expect "tls: running it again adds nothing" "0|same" \
    "$rc|$(cmp -s "$TMP_ROOT/merged.conf" "$NGINX_ETC/snippets/wsi-tls.conf" && echo same)"
expect "tls: ...and leaves no extra backup of an unchanged snippet" "1" \
    "$(find "$NGINX_ETC/snippets" -name 'wsi-tls.conf.bak-*' | wc -l | tr -d ' ')"

# A snippet that already had other content is kept as a .bak-<time> copy.
scratch_etc tls-certbot-over
printf '%s\n' "$CERTBOT_OLD_SITE" > "$NGINX_ETC/sites-available/wsi"
echo '# what I had' > "$NGINX_ETC/snippets/wsi-tls.conf"
out="$(run_fn)"; rc=$?
expect "tls: replacing a snippet that had content keeps a copy of it" "0|1|# what I had" \
    "$rc|$(ls "$NGINX_ETC/snippets" | grep -c 'wsi-tls.conf.bak-')|$(cat "$NGINX_ETC/snippets"/wsi-tls.conf.bak-* | head -1)"

# nginx rejecting the new files puts the snippet back as well.
scratch_etc tls-certbot-rejected
printf '%s\n' "$CERTBOT_OLD_SITE" > "$NGINX_ETC/sites-available/wsi"
echo '# what I had' > "$NGINX_ETC/snippets/wsi-tls.conf"
before="$(state)"
touch "$TMP_ROOT/reject"
out="$(run_fn)"; rc=$?
expect "tls + rejected: install stops, every file (the TLS snippet too) left unchanged" "1|$before" "$rc|$(state)"
expect "tls + rejected: ...and no wsi-tls.conf.bak-* copy is left behind" "0" \
    "$(find "$NGINX_ETC/snippets" -name 'wsi-tls.conf.bak-*' | wc -l | tr -d ' ')"
rm -f "$TMP_ROOT/reject"

# A snippet with active lines of the operator's own is merged into, never replaced
# outright: an HSTS header uncommented as QUICKSTART says, `ssl_verify_client`
# moved in as the remedy for the refusal below, a listener for another port.
scratch_etc tls-operator
printf '%s\n' "$CERTBOT_OLD_SITE" > "$NGINX_ETC/sites-available/wsi"
cat > "$NGINX_ETC/snippets/wsi-tls.conf" <<'SNIP'
# my notes
add_header Strict-Transport-Security "max-age=63072000; includeSubDomains; preload" always;
ssl_verify_client on;
ssl_client_certificate /etc/ssl/ca.pem;
listen 8443 ssl;
listen 443 ssl http2;
ssl_certificate /etc/ssl/old.pem;
ssl_certificate_key /etc/ssl/old.key;
SNIP
cp "$NGINX_ETC/snippets/wsi-tls.conf" "$TMP_ROOT/operator.orig"
out="$(run_fn_w)"; rc=$?
snip="$NGINX_ETC/snippets/wsi-tls.conf"
expect "tls merge: succeeds" "0" "$rc"
for want in 'ssl_verify_client on;' 'ssl_client_certificate /etc/ssl/ca.pem;' 'listen 8443 ssl;' '# my notes' \
            'add_header Strict-Transport-Security "max-age=63072000; includeSubDomains; preload" always;'; do
    expect "tls merge: the operator's line stays: ${want:0:45}" "1" "$(grep -cxF -- "$want" "$snip")"
done
expect "tls merge: their HSTS is the only one (not doubled by the site's)" "1" \
    "$(grep -c '^[[:space:]]*add_header Strict-Transport-Security' "$snip")"
expect "tls merge: certbot's listener and certificate are in" "1|1" \
    "$(grep -cxF 'listen 443 ssl; # managed by Certbot' "$snip")|$(grep -cxF 'ssl_certificate /etc/letsencrypt/live/wsi.example.org/fullchain.pem; # managed by Certbot' "$snip")"
expect "tls merge: what certbot replaced is gone from the active lines (one listen 443, certbot's certificate plus the operator's own)" "1|2" \
    "$(grep -cE '^listen 443 ' "$snip")|$(grep -cE '^ssl_certificate ' "$snip")"
expect "tls merge: ...and listed in a comment (the operator's own certificate pair is not certbot's to replace)" "1|0|1" \
    "$(grep -cxF '#   listen 443 ssl http2;' "$snip")|$(grep -cF '#   ssl_certificate /etc/ssl/old.pem;' "$snip")|$(grep -cxF 'ssl_certificate /etc/ssl/old.pem;' "$snip")"
expect "tls merge: a copy of the snippet as it was is kept" "same" \
    "$(cmp -s "$TMP_ROOT/operator.orig" "$(ls "$NGINX_ETC"/snippets/wsi-tls.conf.bak-* | head -1)" && echo same)"
expect "tls merge: the run says so, naming the snippet and the copy" "1|1" \
    "$(grep -c '^WARNING: .*wsi-tls.conf had settings of its own' <<<"$out")|$(grep -c '^WARNING: .*kept as .*wsi-tls.conf.bak-' <<<"$out")"
cp "$snip" "$TMP_ROOT/operator.merged"
# The next update (the site is the stock one now): nothing happens.
out="$(run_fn_w)"; rc=$?
expect "tls merge: the next update leaves it alone, with no new copy and no warning" "0|same|1|0" \
    "$rc|$(cmp -s "$TMP_ROOT/operator.merged" "$snip" && echo same)|$(ls "$NGINX_ETC/snippets" | grep -c 'wsi-tls.conf.bak-')|$(grep -c '^WARNING' <<<"$out")"
# certbot patches the site again (a renewal with a new certificate path): merged again, once.
sed 's#live/wsi.example.org#live/wsi-new.example.org#' <<<"$CERTBOT_OLD_SITE" > "$NGINX_ETC/sites-available/wsi"
out="$(run_fn_w)"; rc=$?
expect "tls merge: a re-patched site is merged again: certbot's new certificate replaces its old one, the operator's stays, the gate too" "0|2|1|0|1" \
    "$rc|$(grep -cE '^ssl_certificate ' "$snip")|$(grep -cF 'live/wsi-new.example.org/fullchain.pem' "$snip")|$(grep -cE '^ssl_certificate .*live/wsi.example.org/' "$snip")|$(grep -cxF 'ssl_verify_client on;' "$snip")"
# A merge that crashes must stop the install with the snippet as it was -- not
# write an empty result and report success (HTTPS and the gate gone).
scratch_etc tls-merge-crash
printf '%s\n' "$CERTBOT_OLD_SITE" > "$NGINX_ETC/sites-available/wsi"
printf 'ssl_verify_client on;\n' > "$NGINX_ETC/snippets/wsi-tls.conf"
mkdir -p "$TMP_ROOT/crashbin"
real_python="$(command -v python3)"
cat > "$TMP_ROOT/crashbin/python3" <<SH
#!/bin/sh
case "\$*" in *--merge*) exit 1 ;; esac
exec "$real_python" "\$@"
SH
chmod +x "$TMP_ROOT/crashbin/python3"
before="$(state)"
out="$(PATH="$TMP_ROOT/crashbin:$PATH" run_fn)"; rc=$?
expect "tls merge: a crashing merge stops the install, every file left unchanged" "1|$before" "$rc|$(state)"
expect "tls merge: ...and leaves no copy or temporary file beside the snippet" "0|" \
    "$(find "$NGINX_ETC/snippets" -name 'wsi-tls.conf.*' | wc -l | tr -d ' ')|$(ls -A "$TMP_ROOT/tmp")"
# The snippet keeps its owner and mode (an operator may have made it group-writable).
if [[ "$(id -u)" == 0 ]]; then
    scratch_etc tls-owner
    printf '%s\n' "$CERTBOT_OLD_SITE" > "$NGINX_ETC/sites-available/wsi"
    printf 'ssl_verify_client on;\n' > "$NGINX_ETC/snippets/wsi-tls.conf"
    chown nobody "$NGINX_ETC/snippets/wsi-tls.conf"; chmod 0664 "$NGINX_ETC/snippets/wsi-tls.conf"
    out="$(run_fn_w)"; rc=$?
    expect "tls merge: the merged snippet keeps its owner and mode" "0|nobody 664" \
        "$rc|$(stat -c '%U %a' "$NGINX_ETC/snippets/wsi-tls.conf")"
else
    printf 'note - tls: the owner check needs root (not exercised here)\n'
fi

# A snippet that is a symlink is never rewritten through or replaced by a file:
# stop before anything changes if it lacks the carried lines, go on if it has them.
scratch_etc tls-link
printf '%s\n' "$CERTBOT_OLD_SITE" > "$NGINX_ETC/sites-available/wsi"
mkdir -p "$TMP_ROOT/managed"
printf 'ssl_verify_client on;\n' > "$TMP_ROOT/managed/tls.conf"
ln -s "$TMP_ROOT/managed/tls.conf" "$NGINX_ETC/snippets/wsi-tls.conf"
before="$(state)"
out="$(run_fn)"; rc=$?
expect "tls link: a symlinked snippet without the carried lines stops the run, files left unchanged" "1|$before" "$rc|$(state)"
expect "tls link: ...saying so, with the lines to add; the target is untouched" "yes|yes|ssl_verify_client on;" \
    "$(grep -q 'is a symlink' <<<"$out" && echo yes)|$(grep -q 'listen 443 ssl; # managed by Certbot' <<<"$out" && echo yes)|$(cat "$TMP_ROOT/managed/tls.conf")"
expect "tls link: ...it is still a link" "yes" "$([[ -L "$NGINX_ETC/snippets/wsi-tls.conf" ]] && echo yes)"
# once the target holds the lines, the run goes through and leaves link and target alone
cp "$NGINX_ETC/sites-available/wsi" "$TMP_ROOT/site.keep"
python3 -I "$REPO/infra/lib/nginx_tls_carry.py" "$NGINX_ETC/sites-available/wsi" > "$TMP_ROOT/carried.txt"
python3 -I "$REPO/infra/lib/nginx_tls_carry.py" --merge "$TMP_ROOT/managed/tls.conf" < "$TMP_ROOT/carried.txt" > "$TMP_ROOT/managed/tls.new"
mv "$TMP_ROOT/managed/tls.new" "$TMP_ROOT/managed/tls.conf"
cp "$TMP_ROOT/managed/tls.conf" "$TMP_ROOT/managed/tls.conf.keep"
out="$(run_fn)"; rc=$?
expect "tls link: with the lines in its target the run goes through, link and target as they were" "0|yes|same" \
    "$rc|$([[ -L "$NGINX_ETC/snippets/wsi-tls.conf" ]] && echo yes)|$(cmp -s "$TMP_ROOT/managed/tls.conf" "$TMP_ROOT/managed/tls.conf.keep" && echo same)"

# A snippet of comments only (the shipped one) is replaced; its copy is kept and the run says so.
scratch_etc tls-shipped
printf '%s\n' "$CERTBOT_OLD_SITE" > "$NGINX_ETC/sites-available/wsi"
cp "$REPO/infra/nginx/wsi-tls.conf" "$NGINX_ETC/snippets/wsi-tls.conf"
out="$(run_fn_w)"; rc=$?
expect "tls shipped: replaced, copy kept, announced" "0|1|1" \
    "$rc|$(ls "$NGINX_ETC/snippets" | grep -c 'wsi-tls.conf.bak-')|$(grep -c '^WARNING: .*was replaced by the TLS settings carried over' <<<"$out")"

# The copy of the original certbot file (wsi.previous) survives a later run that
# carries only certbot's redirect (`certbot enhance --redirect` on a migrated host).
scratch_etc tls-previous
printf '%s\n' "$CERTBOT_OLD_SITE" > "$NGINX_ETC/sites-available/wsi"
out="$(run_fn)"; rc=$?
cat > "$NGINX_ETC/sites-available/wsi" <<'SITE'
server {
    listen 80 default_server;
    server_name _;
}
server {
    if ($host = wsi.example.org) {
        return 301 https://$host$request_uri;
    } # managed by Certbot
    listen 80 ;
    server_name wsi.example.org;
    return 404; # managed by Certbot
}
SITE
out="$(run_fn)"; rc=$?
expect "tls previous: a redirect-only run does not replace the copy of the original" "0|same" \
    "$rc|$(printf '%s\n' "$CERTBOT_OLD_SITE" | cmp -s - "$NGINX_ETC/sites-available/wsi.previous" && echo same)"

# Two runs in the same second must not share a backup name: the second would
# overwrite the first copy and then delete it as its own.
mkdir -p "$TMP_ROOT/datebin"
printf '#!/bin/sh\necho 20200101-000000\n' > "$TMP_ROOT/datebin/date"; chmod +x "$TMP_ROOT/datebin/date"
mkdir -p "$TMP_ROOT/samesec"
printf '# one\n' > "$TMP_ROOT/samesec/wsi-tls.conf"
( PATH="$TMP_ROOT/datebin:$PATH"; install_wsi_tls_snippet "$TMP_ROOT/samesec/wsi-tls.conf" "$(printf 'listen 443 ssl; # managed by Certbot')" ) >/dev/null 2>&1
printf '# two\n' > "$TMP_ROOT/samesec/wsi-tls.conf"
( PATH="$TMP_ROOT/datebin:$PATH"; install_wsi_tls_snippet "$TMP_ROOT/samesec/wsi-tls.conf" "$(printf 'listen 443 ssl; # managed by Certbot')" ) >/dev/null 2>&1
expect "tls backup: two runs in one second keep two copies, the first untouched" "# one|# two" \
    "$(cat "$TMP_ROOT/samesec/wsi-tls.conf.bak-20200101-000000")|$(cat "$TMP_ROOT/samesec/wsi-tls.conf.bak-20200101-000000+")"

# TLS this installer cannot attribute to certbot: the run stops, nothing changes.
scratch_etc tls-handwritten
cat > "$NGINX_ETC/sites-available/wsi" <<'SITE'
server {
    listen 443 ssl;
    ssl_certificate /etc/ssl/mine.pem;
    ssl_certificate_key /etc/ssl/mine.key;
}
SITE
before="$(state)"
out="$(run_fn)"; rc=$?
expect "tls: hand-written TLS stops the run, files left unchanged" "1|yes|$before" \
    "$rc|$(grep -q 'cannot carry over -- replacing the file would switch HTTPS off' <<<"$out" && echo yes)|$(state)"
expect "tls: ...and the message names the way out" "yes" \
    "$(grep -q 'WSI_NGINX_DISCARD_SITE_TLS=1' <<<"$out" && grep -q 'wsi-tls.conf' <<<"$out" && echo yes)"
expect "tls: nothing left in TMPDIR" "" "$(ls -A "$TMP_ROOT/tmp")"
cp "$NGINX_ETC/sites-available/wsi" "$TMP_ROOT/handwritten.site"
# The file stays in place -- but a copy is kept anyway: update.sh of an older
# release rolls a refused install back with the previous install.sh, which
# replaces the site file without asking.
expect "tls: a refused run keeps the file as wsi.previous all the same" "same" \
    "$(cmp -s "$TMP_ROOT/handwritten.site" "$NGINX_ETC/sites-available/wsi.previous" && echo same)"
out="$(WSI_NGINX_DISCARD_SITE_TLS=1 run_fn)"; rc=$?
expect "tls: WSI_NGINX_DISCARD_SITE_TLS=1 replaces it anyway, with a warning" "0|yes" \
    "$rc|$(grep -q 'HTTPS is OFF' <<<"$out" && echo yes)"
expect "tls: ...and the replaced hand-written file is kept as wsi.previous" "same" \
    "$(cmp -s "$TMP_ROOT/handwritten.site" "$NGINX_ETC/sites-available/wsi.previous" && echo same)"

# TLS in a second server block, or Ubuntu's snakeoil include: not carried, so
# the run stops rather than drop it.
for kind in second snakeoil clientcert oneline httplevel nextline; do
    scratch_etc "tls-$kind"
    if [[ "$kind" == clientcert ]]; then
        { printf '%s\n' "$CERTBOT_OLD_SITE" | sed 's|^    listen 443 ssl; # managed by Certbot|&\n    ssl_verify_client on;|'; } > "$NGINX_ETC/sites-available/wsi"
    elif [[ "$kind" == httplevel ]]; then
        { printf 'ssl_verify_client on;\nssl_client_certificate /etc/ssl/ca.pem;\n'; printf '%s\n' "$CERTBOT_OLD_SITE"; } \
            > "$NGINX_ETC/sites-available/wsi"
    elif [[ "$kind" == nextline ]]; then
        { printf '%s\n' "$CERTBOT_OLD_SITE"
          printf 'server\n{\n    listen 8443 ssl;\n    ssl_certificate /etc/ssl/b.pem;\n    ssl_certificate_key /etc/ssl/b.key;\n}\n'; } \
            > "$NGINX_ETC/sites-available/wsi"
    elif [[ "$kind" == oneline ]]; then
        { printf '%s\n' "$CERTBOT_OLD_SITE"
          printf 'server { listen 8443 ssl; ssl_certificate /etc/ssl/b.pem; ssl_certificate_key /etc/ssl/b.key; }\n'; } \
            > "$NGINX_ETC/sites-available/wsi"
    elif [[ "$kind" == second ]]; then
        { printf '%s\n' "$CERTBOT_OLD_SITE"
          printf 'server {\n    listen 8443 ssl;\n    ssl_certificate /etc/ssl/b.pem;\n    ssl_certificate_key /etc/ssl/b.key;\n}\n'; } \
            > "$NGINX_ETC/sites-available/wsi"
    else
        printf 'server {\n    listen 80;\n    listen 443 ssl;\n    include snippets/snakeoil.conf;\n}\n' > "$NGINX_ETC/sites-available/wsi"
    fi
    before="$(state)"
    out="$(run_fn)"; rc=$?
    expect "tls: $kind -- the run stops, files left unchanged" "1|$before" "$rc|$(state)"
    case "$kind" in
        httplevel) expect "tls: $kind -- the refusal names the line" "yes" \
                       "$(grep -q 'ssl_verify_client on;' <<<"$out" && echo yes)" ;;
        nextline)  expect "tls: $kind -- the refusal names the line" "yes" \
                       "$(grep -q 'listen 8443 ssl;' <<<"$out" && echo yes)" ;;
    esac
done

# A snippet that cannot be written completely (a full disk) is an error, not an
# empty snippet in place and a success.
if mkdir -p "$TMP_ROOT/tiny" && mount -t tmpfs -o size=8k tmpfs "$TMP_ROOT/tiny" 2>/dev/null; then
    head -c 8192 /dev/zero > "$TMP_ROOT/tiny/filler" 2>/dev/null || true
    carried_text="$(printf 'listen 443 ssl; # managed by Certbot\n%.0s' $(seq 1 40))"
    ( install_wsi_tls_snippet "$TMP_ROOT/tiny/wsi-tls.conf" "$carried_text" ) >/dev/null 2>&1; rc=$?
    expect "tls: a snippet that cannot be written in full fails the install step" "1|no|0" \
        "$rc|$([[ -e "$TMP_ROOT/tiny/wsi-tls.conf" ]] && echo yes || echo no)|$(find "$TMP_ROOT/tiny" -name 'wsi-tls*' | wc -l | tr -d ' ')"
    umount "$TMP_ROOT/tiny"
else
    printf 'note - tls: the full-disk case needs root and tmpfs (not exercised here)\n'
fi

printf -- '--------------------------------------------------------------\n'
if [[ "$fails" -eq 0 ]]; then
    printf 'PASS - a config nginx -t rejects is never left installed\n'
else
    exit 1
fi
