#!/usr/bin/env bash
# Guard: an unpublished / deleted slide's thumbnail stops being served from the
# nginx cache within a minute, in the REAL snippet (infra/nginx/wsi-locations.conf).
#
# Run: bash infra/tests/test_thumbs_ttl_nginx.sh   (takes about a minute: it
# waits out the real TTL)
#
# WHY THIS EXISTS
# ---------------
# /thumbs/ is cached and shared, and its edge check (/_wsi_authz) is app access
# only — there is no per-slide publish gate as for /dzi/. Unpublishing a slide
# instead renames its thumbnail to a fresh random token (deleting the slide
# removes the file), so that every URL handed out while it was public stops
# resolving. A cached 200 ignores that for as long as it is fresh: with
# `proxy_cache_valid 200 1h` the old URL would keep answering for up to an hour.
# This test:
#
#   1. reads the /thumbs/ 200 TTL from the snippet and requires it to be at
#      most 60 s (the window the /dzi/ verdict gives an unpublish);
#   2. caches two thumbnails, "renames" one away at the upstream (now 404) and
#      shows the cache still serves it while fresh;
#   3. waits the TTL out, then requires the renamed one to answer 404 and the
#      untouched one to come back REVALIDATED — a conditional request the
#      upstream answered with a bodiless 304, which is what keeps the short
#      TTL cheap.
#
# The upstream mimics Starlette's StaticFiles (ETag + Last-Modified, 304 on a
# matching If-None-Match). Self-contained like the other nginx tests here:
# SKIPs (exit 0) when nginx, python3 or curl is unavailable.

set -uo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
REPO="$(cd "${HERE}/../.." && pwd)"
SNIPPET="${REPO}/infra/nginx/wsi-locations.conf"
SITE_CONF="${REPO}/infra/nginx/wsi.conf"

skip() { printf 'SKIP - %s\n' "$1"; exit 0; }
fail() { printf 'FAIL - %s\n' "$1"; FAILED=1; }
pass() { printf 'ok   - %s\n' "$1"; }

command -v python3 >/dev/null 2>&1 || skip "python3 not available"
command -v curl >/dev/null 2>&1 || skip "curl not available"
NGINX_BIN=""
for c in nginx /usr/sbin/nginx /usr/local/sbin/nginx; do
    command -v "$c" >/dev/null 2>&1 && { NGINX_BIN="$(command -v "$c")"; break; }
    [[ -x "$c" ]] && { NGINX_BIN="$c"; break; }
done
[[ -n "${NGINX_BIN}" ]] || skip "nginx not available"
[[ -f "${SNIPPET}" ]] || { echo "FAIL - snippet not found: ${SNIPPET}"; exit 1; }

FAILED=0
WORK="$(mktemp -d)"
UP_PORT="${THUMBS_TTL_TEST_UP_PORT:-18241}"
NG_PORT="${THUMBS_TTL_TEST_NG_PORT:-18242}"
UP_PID=""
cleanup() {
    [[ -n "${UP_PID}" ]] && kill "${UP_PID}" 2>/dev/null
    [[ -f "${WORK}/nginx.pid" ]] && "${NGINX_BIN}" -c "${WORK}/nginx.conf" -s quit 2>/dev/null
    rm -rf "${WORK}"
}
trap cleanup EXIT
mkdir -p "${WORK}"/{cache,authz,body,proxy,fastcgi,uwsgi,scgi}

# --- 1. the TTL the snippet gives a cached thumbnail ------------------------
TTL_RAW="$(awk '
    /^location \^~ \/thumbs\/ / { c = 1 }
    c && $1 == "proxy_cache_valid" && $2 == "200" { sub(/;$/, "", $3); print $3; exit }
    c && /^}/ { exit }
' "${SNIPPET}")"
case "${TTL_RAW}" in
    *ms) TTL="" ;;
    *s) TTL="${TTL_RAW%s}" ;;
    *m) TTL="$(( ${TTL_RAW%m} * 60 ))" ;;
    *h) TTL="$(( ${TTL_RAW%h} * 3600 ))" ;;
    *d) TTL="$(( ${TTL_RAW%d} * 86400 ))" ;;
    [0-9]*) TTL="${TTL_RAW}" ;;
    *) TTL="" ;;
esac
if [[ ! "${TTL}" =~ ^[0-9]+$ ]]; then
    echo "FAIL - no 'proxy_cache_valid 200 <time>' in the /thumbs/ location of ${SNIPPET} (got '${TTL_RAW}')"
    exit 1
fi
if (( TTL <= 60 )); then
    pass "/thumbs/ caches a 200 for ${TTL_RAW} (<= 60 s)"
else
    fail "/thumbs/ caches a 200 for ${TTL_RAW}: an unpublished slide's thumbnail stays servable that long"
fi

# --- upstream: StaticFiles for /thumbs/, plus an allowing edge-authz --------
cat > "${WORK}/upstream.py" <<PY
import http.server, json
FILES = {"/thumbs/1-aaaa.jpg": '"etag-a"', "/thumbs/2-bbbb.jpg": '"etag-b"', "/thumbs/3-cccc.jpg": '"etag-c"'}
HITS = {}
class H(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a): pass
    def send(self, status, body=b"", extra=()):
        self.send_response(status)
        for k, v in extra:
            self.send_header(k, v)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers(); self.wfile.write(body)
    def do_GET(self):
        p = self.path.partition("?")[0]
        if p == "/api/edge-authz":
            return self.send(204)
        if p == "/_ctl/hits":
            return self.send(200, json.dumps(HITS).encode())
        if p.startswith("/_ctl/remove/"):
            FILES.pop("/thumbs/" + p[len("/_ctl/remove/"):], None)
            return self.send(200)
        inm = self.headers.get("If-None-Match")
        tag = FILES.get(p)
        HITS.setdefault(p, []).append(
            "404" if tag is None else "304" if inm == tag else "200")
        if tag is None:
            return self.send(404, b'{"detail":"Not Found"}', (("Content-Type", "application/json"),))
        meta = (("ETag", tag), ("Last-Modified", "Mon, 05 Oct 2026 00:00:00 GMT"))
        if inm == tag:
            return self.send(304, b"", meta)
        return self.send(200, b"\xff\xd8\xff\xd9", meta + (("Content-Type", "image/jpeg"),))
http.server.ThreadingHTTPServer(("127.0.0.1", ${UP_PORT}), H).serve_forever()
PY
setsid python3 "${WORK}/upstream.py" >/dev/null 2>&1 < /dev/null &
UP_PID=$!
disown "${UP_PID}" 2>/dev/null || true

# The maps the snippet needs from wsi.conf, verbatim, and every limit_req_zone
# it names (only the zone names matter here, so they are stubbed).
real_map() { awk -v v="$1" '$1 == "map" && $3 == v { c = 1 } c { print } c && /^}/ { exit }' "${SITE_CONF}"; }
DZI_MAPS="$(real_map '$wsi_authz_object')
$(real_map '$wsi_dzi_unversioned')
$(real_map '$wsi_dzi_tile_cache_control')"
if ! grep -q 'wsi_authz_object' <<<"${DZI_MAPS}" || ! grep -q 'wsi_dzi_unversioned' <<<"${DZI_MAPS}" \
    || ! grep -q 'wsi_dzi_tile_cache_control' <<<"${DZI_MAPS}"; then
    echo "FAIL - could not extract the /dzi/ maps from ${SITE_CONF}"
    exit 1
fi
ZONE_STUBS="$(grep -oE 'zone=[a-z0-9_]+:' "${SITE_CONF}" | grep -v '_cache:' | sort -u \
    | sed -E 's/^zone=([a-z0-9_]+):$/    limit_req_zone $binary_remote_addr zone=\1:1m rate=600r\/m;/')"

cat > "${WORK}/nginx.conf" <<CONF
worker_processes 1;
user root;
pid ${WORK}/nginx.pid;
error_log ${WORK}/error.log warn;
events { worker_connections 128; }
http {
    access_log off;
    client_body_temp_path ${WORK}/body;
    proxy_temp_path       ${WORK}/proxy;
    fastcgi_temp_path     ${WORK}/fastcgi;
    uwsgi_temp_path       ${WORK}/uwsgi;
    scgi_temp_path        ${WORK}/scgi;

    proxy_cache_path ${WORK}/cache levels=1:2 keys_zone=wsi_dzi_cache:16m
                     max_size=1g inactive=30d use_temp_path=off;
    proxy_cache_path ${WORK}/authz levels=1:2 keys_zone=wsi_authz_cache:1m
                     inactive=2m use_temp_path=off;
    proxy_cache_path ${WORK}/static levels=1:2 keys_zone=wsi_static_cache:1m
                     inactive=10m use_temp_path=off;

    upstream wsi_app  { server 127.0.0.1:${UP_PORT}; keepalive 8; }
    upstream wsi_sync { server 127.0.0.1:${UP_PORT}; keepalive 8; }

${ZONE_STUBS}

    map \$http_upgrade \$connection_upgrade { default upgrade; "" close; }
    map \$scheme                 \$wsi_xfp                { default \$scheme; }
    map \$remote_addr            \$wsi_rl_key             { default \$remote_addr; }
    map \$remote_addr            \$wsi_cf_connecting_ip   { default ""; }
    map \$remote_addr            \$wsi_cf_access_jwt      { default ""; }
    map \$http_authorization \$wsi_auth_nocache { default 1; "" ""; "~*^Basic\s" ""; }
    map \$http_authorization \$wsi_authz_nocache { default 1; "" ""; }
${DZI_MAPS}

    server {
        listen 127.0.0.1:${NG_PORT};
        server_name _;
        include ${SNIPPET};
        location /_ctl/ { proxy_pass http://wsi_app; }
    }
}
CONF

if ! "${NGINX_BIN}" -c "${WORK}/nginx.conf" -t 2>"${WORK}/nginx-test.log"; then
    printf 'FAIL - nginx rejected the config (snippet + stubs):\n'
    sed 's/^/    /' "${WORK}/nginx-test.log"
    exit 1
fi
"${NGINX_BIN}" -c "${WORK}/nginx.conf" || { printf 'FAIL - nginx failed to start\n'; exit 1; }
sleep 1

BASE="http://127.0.0.1:${NG_PORT}"
# "<status> <X-Cache-Status>" of one GET.
probe() {
    curl -s -D - -o /dev/null -m 5 "${BASE}$1" | tr -d '\r' | awk -F': ' '
        NR == 1 { split($0, a, " "); status = a[2] }
        tolower($1) == "x-cache-status" { xc = $2 }
        END { printf "%s %s\n", status, (xc == "" ? "-" : xc) }'
}
expect() {
    local what="$1" path="$2" want="$3" got
    got="$(probe "$path")"
    if [[ "$got" == $want ]]; then
        printf 'ok   - %-58s %s\n' "$what" "$got"
    else
        fail "${what}: got '${got}', expected '${want}'"
    fi
}

A="/thumbs/1-aaaa.jpg?v=1700000000"   # the slide that gets unpublished
B="/thumbs/2-bbbb.jpg?v=1700000000"   # a slide that stays published
C="/thumbs/3-cccc.jpg?v=1700000000"   # unpublished too; asked for by several clients at once

# --- 2. both cached; then A is renamed away, but the cache still has it -----
expect "thumbnail A fetched and stored"                 "$A" "200 MISS"
expect "thumbnail B fetched and stored"                 "$B" "200 MISS"
expect "thumbnail C fetched and stored"                 "$C" "200 MISS"
curl -s -o /dev/null -m 5 "${BASE}/_ctl/remove/1-aaaa.jpg"
curl -s -o /dev/null -m 5 "${BASE}/_ctl/remove/3-cccc.jpg"
expect "A unpublished (renamed): still served while fresh" "$A" "200 HIT"

if (( TTL > 60 )); then
    # Not worth waiting an hour to show what section 1 already failed on.
    printf -- '--------------------------------------------------------------\n'
    exit 1
fi

# --- 3. once the TTL is out --------------------------------------------------
sleep "$(( TTL + 2 ))"
# (A 404 carries no X-Cache-Status: that add_header has no `always`.)
expect "A after the TTL: the rename has taken hold"     "$A" "404 -"
expect "A again: still 404 (asked of the upstream, not stored)" "$A" "404 -"
expect "B after the TTL: revalidated, not re-fetched"   "$B" "200 REVALIDATED"
expect "B is fresh again after the revalidation"        "$B" "200 HIT"
# What the upstream answered per thumbnail, in order.
hits="$(curl -s -m 5 "${BASE}/_ctl/hits")"
answers() { python3 -c 'import json,sys; print(",".join(json.loads(sys.argv[1]).get(sys.argv[2], [])))' "$hits" "$1"; }
b_hits="$(answers /thumbs/2-bbbb.jpg)"
if [[ "$b_hits" == "200,304" ]]; then
    pass "the upstream sent B's bytes once, then a bodiless 304 (${b_hits})"
else
    fail "upstream answers for B were '${b_hits}', expected '200,304' (a full fetch, then a conditional one)"
fi
a_hits="$(answers /thumbs/1-aaaa.jpg)"
# A 404 is never stored (no `proxy_cache_valid 404`: made-up names would
# otherwise each leave an entry behind), so the renamed thumbnail's second
# request is asked of the upstream again rather than answered from the cache.
if [[ "$a_hits" == "200,404,404" ]]; then
    pass "A's 404 replaced the cached 200 and is not stored: every request asks the upstream (${a_hits})"
else
    fail "upstream answers for A were '${a_hits}', expected '200,404,404' (a 404 must not be served from the cache)"
fi

# Several clients asking for an unpublished thumbnail at the same moment, round
# after round. With `use_stale updating` the ones that overlap a refresh would
# be handed the stale 200, and since a 404 is not stored the entry would never
# be replaced, so the thumbnail would stay servable for as long as it was
# asked for.
leaked=""
for _ in 1 2 3 4 5; do
    for n in 1 2 3 4; do probe "$C" > "${WORK}/probe-$n" & done
    wait
    leaked+="$(cat "${WORK}"/probe-[1-4] | grep -v '^404 ' | tr '\n' ' ')"
    sleep 0.3
done
if [[ -z "$leaked" ]]; then
    pass "C asked for by 4 clients at once, 5 rounds: every answer is the 404"
else
    fail "an unpublished thumbnail was still served to overlapping requests: ${leaked}"
fi

printf -- '--------------------------------------------------------------\n'
if [[ "$FAILED" -eq 0 ]]; then
    printf 'PASS - an unpublished thumbnail leaves the edge cache within %s; unchanged ones revalidate with a 304\n' "${TTL_RAW}"
else
    exit 1
fi
