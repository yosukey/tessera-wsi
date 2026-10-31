#!/usr/bin/env bash
# Tests for infra/nginx-cache.sh, the root-run tile-cache tool.
# Run: bash infra/tests/test_nginx_cache_tool.sh
#
# WHY THIS EXISTS
# ---------------
# nginx-cache.sh runs as root, so like set-workers.sh / set-cache-size.sh it
# must not run from an untrusted tree, the service user's deployed copy under
# /opt/wsi included; and it must not rewrite max_size with `python3 - <<EOF`,
# which puts the current directory first on sys.path — a re.py or tempfile.py
# in whatever directory the operator ran sudo from would be imported, and
# run, as root. The script refuses an untrusted tree before reading anything
# and runs its Python with -I. Since the same cwd rule applies to every
# python3 the infra scripts run, the static check at the end covers all of them.
#
# Changing max_size must also scale the tile zone's keys_zone: at 200m nginx
# fills it at ~1.6M files (~25 GB of tiles) and then evicts from it — so a
# larger max_size alone adds nothing. The tool scales keys_zone with it
# (lib/wsi-common.sh, shared with install.sh's re-apply), and puts the config
# back, unpersisted, when nginx -t rejects the change. The rewritten
# directive is checked with the real nginx when one is installed.
#
# Unbounded, that scaling would break updates on small hosts: a persisted
# max_size of 500g (a documented example) calls for a 4000m zone, which
# nginx maps whole at `nginx -t` and cannot past RAM + swap, so every
# update's install.sh would fail nginx -t and update.sh roll back. The zone is
# capped at a quarter of MemTotal (never below 200m), read from a fake
# meminfo here (WSI_MEMINFO) and from the real one for the real nginx -t.
#
# The script is run with its prompts answered from a pipe, against a temp
# copy of infra/nginx/wsi.conf (WSI_NGINX_CONF), with stub nginx / systemctl.
# Needs python3; SKIPs (exit 0) without it.
set -uo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
INFRA="$(cd "${HERE}/.." && pwd)"

skip() { printf 'SKIP - %s\n' "$1"; exit 0; }
command -v python3 >/dev/null 2>&1 || skip "python3 not available"

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

WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT
mkdir -p "$WORK/bin"
# Stubs: nginx -t fails while $WORK/nginx-fails exists; systemctl succeeds.
cat > "$WORK/bin/nginx" <<SH
#!/bin/sh
echo "nginx \$*" >> "$WORK/calls"
[ -e "$WORK/nginx-fails" ] && exit 1
exit 0
SH
printf '#!/bin/sh\necho "systemctl $*" >> "%s/calls"\nexit 0\n' "$WORK" > "$WORK/bin/systemctl"
chmod +x "$WORK/bin/nginx" "$WORK/bin/systemctl"

# run_tool SCRIPT ANSWERS [ENV...] : runs it on a fresh copy of wsi.conf,
# from directory $RUN_CWD (default /); sets OUT, RC, CONF.
RUN_CWD=/
run_tool() {
    local script="$1" answers="$2"
    shift 2
    CONF="$WORK/wsi.conf"
    cp "$INFRA/nginx/wsi.conf" "$CONF"
    rm -f "$WORK/max-size"
    OUT="$(cd "$RUN_CWD" && printf '%s' "$answers" | env PATH="$WORK/bin:$PATH" \
        WSI_MEMINFO="$WORK/meminfo-64g" WSI_NGINX_CONF="$CONF" WSI_NGINX_CACHE_SIZE_FILE="$WORK/max-size" \
        WSI_NGINX_CACHE_DIR="$WORK/cache" "$@" bash "$script" 2>&1)"
    RC=$?
}
# A 64 GiB host unless a test says otherwise: big enough that no size below
# is capped, so the expectations do not depend on the machine running them.
meminfo() { printf 'MemTotal:       %s kB\nMemFree:          1000 kB\n' "$2" > "$WORK/$1"; }
meminfo meminfo-64g 67108864
meminfo meminfo-2g 2097152
meminfo meminfo-512m 524288
max_size_of() { grep -oE 'max_size=[^ ;]+' "$1" | sed 's/max_size=//' | tr '\n' ' ' | sed 's/ $//'; }

# --- the happy path --------------------------------------------------------
run_tool "$INFRA/nginx-cache.sh" $'2\n100g\ny\n'
expect "max_size change: succeeds"              "0"     "$RC"
expect "max_size change: tile zone rewritten"   "100g"  "$(max_size_of "$CONF")"
expect "max_size change: persisted"             "100g"  "$(cat "$WORK/max-size" 2>/dev/null)"

# --- no module from the current directory runs as root --------------------
mkdir -p "$WORK/cwd"
for m in re tempfile os; do
    printf 'open("%s/ran-%s", "w").close()\n' "$WORK" "$m" > "$WORK/cwd/$m.py"
done
RUN_CWD="$WORK/cwd"
run_tool "$INFRA/nginx-cache.sh" $'2\n100g\ny\n'
RUN_CWD=/
expect "planted re.py / tempfile.py in the cwd: not imported" "" \
    "$(find "$WORK" -maxdepth 1 -name 'ran-*' -printf '%f ')"
expect "... and the change still applies"         "0|100g" "$RC|$(max_size_of "$CONF")"

# --- refused from a tree root should not run code from --------------------
# refused LABEL WHY SCRIPT [ENV...]
refused() {
    local label="$1" why="$2" script="$3"
    shift 3
    run_tool "$script" $'2\n100g\ny\n' "$@"
    expect "$label: exits 1"                    "1"   "$RC"
    expect "$label: says why ($why)"            "yes" \
        "$(grep -F -- "$why" <<<"$OUT" | grep -qF 'refusing to run' && echo yes)"
    expect "$label: asks nothing, changes nothing" "no|50g" \
        "$(grep -qE 'Choose an action|New max_size' <<<"$OUT" && echo yes || echo no)|$(max_size_of "$CONF")"
}
copy_tree() {
    mkdir -p "$1/lib"
    cp "$INFRA/nginx-cache.sh" "$1/"
    cp "$INFRA/lib/wsi-common.sh" "$1/lib/"
}
DEPLOYED="$WORK/opt-wsi"
copy_tree "$DEPLOYED/infra"
mkdir -p "$WORK/links"
ln -s "$DEPLOYED/infra/nginx-cache.sh" "$WORK/links/nginx-cache.sh"
refused "in the app home"          "is inside $DEPLOYED" "$DEPLOYED/infra/nginx-cache.sh" APP_HOME="$DEPLOYED"
refused "linked into the app home" "is inside $DEPLOYED" "$WORK/links/nginx-cache.sh"   APP_HOME="$DEPLOYED"
CLONE="$WORK/clone/infra"
copy_tree "$CLONE"
chmod o+w "$CLONE"
refused "world-writable tree"      "$CLONE is writable by every user" "$CLONE/nginx-cache.sh"
chmod o-w "$CLONE"
if [[ ${EUID:-$(id -u)} -eq 0 ]] && id nobody >/dev/null 2>&1; then
    chown nobody "$CLONE/nginx-cache.sh"
    refused "script owned by another account" "$CLONE/nginx-cache.sh is owned by nobody" \
        "$CLONE/nginx-cache.sh"
    chown 0 "$CLONE/nginx-cache.sh"
else
    printf 'SKIP - foreign-owner refusal (needs root and a "nobody" user)\n'
fi
run_tool "$CLONE/nginx-cache.sh" $'2\n100g\ny\n'
expect "the same copy in a sound clone runs"  "0|100g" "$RC|$(max_size_of "$CONF")"

# --- the keys zone grows with max_size -------------------------------------
# nginx indexes every cached file in the keys_zone (~8,000 per MB) and evicts
# by LRU once it is full, whatever max_size says: 200m fills at ~25 GB of
# tiles, so raising max_size past that alone adds nothing. 1 MB per 128 MB of
# max_size, never below 200m (lib/wsi-common.sh).
# shellcheck source=../lib/wsi-common.sh
. "$INFRA/lib/wsi-common.sh"
export WSI_MEMINFO="$WORK/meminfo-64g"
for pair in 50g:400 25g:200 1g:200 100g:800 64g:512 500g:4000 300000m:2344 \
            1048576k:200 1099511627776:8192 26G:208 1:200; do
    expect "keys zone for max_size=${pair%%:*}" "${pair#*:}|${pair#*:}" \
        "$(wsi_nginx_keys_zone_scaled_mb "${pair%%:*}")|$(wsi_nginx_keys_zone_mb "${pair%%:*}")"
done
for bad in "" 0g 10t 1.5g -5g 1234567890123456g; do
    expect "max_size '$bad' is refused" "1:" "$(out="$(wsi_nginx_keys_zone_mb "$bad")"; echo "$?:$out")"
done
# ... but never more than a quarter of RAM, and never below the 200m floor
# (a 512 MB host keeps 200m); no MemTotal, no cap.
for c in meminfo-64g:16384 meminfo-2g:512 meminfo-512m:200 missing:; do
    expect "keys zone cap with ${c%%:*}" "${c#*:}" \
        "$(WSI_MEMINFO="$WORK/${c%%:*}" wsi_nginx_keys_zone_cap_mb)"
done
for c in meminfo-2g:500g:512 meminfo-2g:50g:400 meminfo-2g:64g:512 meminfo-2g:65g:512 \
         meminfo-512m:500g:200 meminfo-512m:1g:200 missing:500g:4000; do
    IFS=: read -r mi sz want <<<"$c"
    expect "keys zone for max_size=$sz with $mi" "$want" \
        "$(WSI_MEMINFO="$WORK/$mi" wsi_nginx_keys_zone_mb "$sz")"
done
stock_max="$(max_size_of "$INFRA/nginx/wsi.conf")"
expect "the stock wsi.conf pairs its max_size with that keys zone" \
    "keys_zone=wsi_dzi_cache:$(wsi_nginx_keys_zone_mb "$stock_max")m" \
    "$(grep -oE 'keys_zone=wsi_dzi_cache:[^ ;]+' "$INFRA/nginx/wsi.conf")"

run_tool "$INFRA/nginx-cache.sh" $'2\n100g\ny\n'
expect "max_size change: the tile keys zone is scaled with it" "0|wsi_dzi_cache:800m" \
    "$RC|$(grep -oE 'keys_zone=wsi_dzi_cache:[^ ;]+' "$CONF" | sed 's/keys_zone=//')"
expect "max_size change: the authz zone keeps its own size" \
    "$(grep -oE 'keys_zone=wsi_authz_cache:[^ ;]+' "$INFRA/nginx/wsi.conf")" \
    "$(grep -oE 'keys_zone=wsi_authz_cache:[^ ;]+' "$CONF")"
expect "max_size change: the keys zone is shown before confirming" "yes" \
    "$(grep -q 'Keys zone : .*wsi_dzi_cache:800m' <<<"$OUT" && echo yes)"
expect "max_size change: no cap on a large host" "no" \
    "$(grep -q 'Capped' <<<"$OUT" && echo yes || echo no)"

# On a 2 GB host 500g is written with a 512m zone, and the operator is told
# before confirming that the cap, not max_size, bounds the cache.
run_tool "$INFRA/nginx-cache.sh" $'2\n500g\ny\n' WSI_MEMINFO="$WORK/meminfo-2g"
expect "capped max_size change: succeeds, zone at a quarter of RAM" "0|500g|wsi_dzi_cache:512m|500g" \
    "$RC|$(max_size_of "$CONF")|$(grep -oE 'keys_zone=wsi_dzi_cache:[^ ;]+' "$CONF" | sed 's/keys_zone=//')|$(cat "$WORK/max-size" 2>/dev/null)"
expect "capped max_size change: the cap is named before confirming" "yes" \
    "$(grep -q 'Capped: max_size 500g calls for a 4000m keys zone' <<<"$OUT" && echo yes)"

# A change nginx -t rejects is put back, not persisted, not reloaded — the
# next nginx restart must not inherit it.
: > "$WORK/calls"
touch "$WORK/nginx-fails"
run_tool "$INFRA/nginx-cache.sh" $'2\n100g\ny\n'
rm -f "$WORK/nginx-fails"
expect "nginx -t rejects: tool fails"                    "1" "$RC"
expect "nginx -t rejects: config byte-identical to before" "same" \
    "$(cmp -s "$INFRA/nginx/wsi.conf" "$CONF" && echo same)"
expect "nginx -t rejects: nothing persisted, no reload"  "none|0" \
    "$(cat "$WORK/max-size" 2>/dev/null || echo none)|$(grep -c 'reload' "$WORK/calls")"

# And the generated directive is one a real nginx accepts.
# (The real nginx, not the stub: a scratch prefix, the directive verbatim but
# for its directory.)
NGINX_BIN="$(PATH="${PATH}:/usr/sbin:/sbin" command -v nginx || true)"
if [[ -z "$NGINX_BIN" ]]; then
    printf 'SKIP - nginx -t of the rewritten tile zone (nginx not available)\n'
else
    # This host's real MemTotal, and a max_size whose uncapped zone (80000m)
    # is more than any test machine can map: only the cap lets nginx -t pass.
    run_tool "$INFRA/nginx-cache.sh" $'2\n10000g\ny\n' WSI_MEMINFO=/proc/meminfo
    NG="$WORK/ngx"
    mkdir -p "$NG/tmp"
    DIRECTIVE="$(awk '/^proxy_cache_path .*wsi_dzi/ { grab = 1 } grab { print } grab && /;/ { exit }' "$CONF" \
                 | sed "s#/var/cache/nginx/wsi_dzi#$NG/cache#")"
    cat > "$NG/nginx.conf" <<CONF
pid $NG/nginx.pid;
error_log $NG/error.log;
events { worker_connections 16; }
http {
    client_body_temp_path $NG/tmp; proxy_temp_path $NG/tmp;
    fastcgi_temp_path $NG/tmp; uwsgi_temp_path $NG/tmp; scgi_temp_path $NG/tmp;
    $DIRECTIVE
}
CONF
    expect "nginx -t accepts the rewritten tile zone ($(grep -oE 'keys_zone=[^ ]+' <<<"$DIRECTIVE"))" "0" \
        "$("$NGINX_BIN" -p "$NG" -c "$NG/nginx.conf" -e "$NG/error.log" -t >/dev/null 2>&1; echo $?)"
fi

# --- every python3 the infra scripts run is isolated ----------------------
# Code lines only, continuations joined; apt package lists, `command -v` and
# messages mention python3 without running it.
unisolated=""
for f in "$INFRA"/*.sh "$INFRA"/lib/*.sh "$INFRA"/systemd/*.sh; do
    hits="$(sed -E 's/^[[:space:]]*#.*$//' "$f" | sed -e ':a' -e '/\\$/N; s/\\\n//; ta' \
        | grep -E '(^|[[:space:];&|(])python3[[:space:]]+([^-[:space:]]|-[^I])' \
        | grep -Ev 'apt-get|command -v python3|(die|warn|log|printf|echo)[[:space:]]+"' || true)"
    [[ -n "$hits" ]] && unisolated+="${f#"$INFRA"/}: $hits"$'\n'
done
expect "every python3 in infra/*.sh runs with -I" "" "$unisolated"

printf -- '--------------------------------------------------------------\n'
if [[ "$fails" -eq 0 ]]; then
    printf 'PASS - nginx-cache.sh: trusted tree only, isolated Python, keys zone sized with max_size\n'
else
    exit 1
fi
