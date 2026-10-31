#!/usr/bin/env bash
# Guard against cache poisoning through non-canonical request paths in the
# REAL nginx snippet (infra/nginx/wsi-locations.conf).
#
# Run: bash infra/tests/test_cache_key_nginx.sh
#
# WHY THIS EXISTS
# ---------------
# The shared-cache locations (/dzi/, /thumbs/, /assets/) key the cache on
# nginx's NORMALISED path ($uri: "//" merged, dot segments resolved, %XX
# decoded), but `proxy_pass http://wsi_app;` (no URI part) forwards the
# client's RAW path. For "//dzi/<id>_files/0/0_0.jpeg" the app sees a path no
# route matches and answers with the SPA's index.html — which nginx would then
# store under the real tile's key for 30 days, breaking that tile for every
# viewer. The snippet answers 400 to such paths before the cache is consulted.
#
# The upstream below mimics the app's routing on the raw path: a canonical
# tile path gets a JPEG, anything else gets the SPA shell (text/html, 200).
#
# It also guards the tile-generation key (section 7): tile and descriptor URLs
# carry the slide's WSI generation as ?v=N, the cache key must include it (so a
# WSI replace yields new keys instead of a mix of old and new tiles), a request
# without it must never be stored, and the app's "X-Accel-Expires: 0" (sent on
# a stale-v 404 and on unversioned responses) must be honoured. For that the
# upstream answers ?v=9 like the app answers a stale generation (404 +
# X-Accel-Expires: 0) and ?v=8 with a 200 carrying X-Accel-Expires: 0, and
# counts what reaches it per raw request target.
#
# Self-contained, like the other nginx tests here: SKIPs (exit 0) when nginx,
# python3 or curl is unavailable.

set -uo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
REPO="$(cd "${HERE}/../.." && pwd)"
SNIPPET="${REPO}/infra/nginx/wsi-locations.conf"
SITE_CONF="${REPO}/infra/nginx/wsi.conf"

skip() { printf 'SKIP - %s\n' "$1"; exit 0; }
fail() { printf 'FAIL - %s\n' "$1"; FAILED=1; }

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
UP_PORT="${CACHE_KEY_TEST_UP_PORT:-18201}"
NG_PORT="${CACHE_KEY_TEST_NG_PORT:-18202}"
UP_PID=""
cleanup() {
    [[ -n "${UP_PID}" ]] && kill "${UP_PID}" 2>/dev/null
    [[ -f "${WORK}/nginx.pid" ]] && "${NGINX_BIN}" -c "${WORK}/nginx.conf" -s quit 2>/dev/null
    rm -rf "${WORK}"
}
trap cleanup EXIT
mkdir -p "${WORK}"/{cache,body,proxy,fastcgi,uwsgi,scgi}

# --- upstream: route on the RAW path like the app does ----------------------
cat > "${WORK}/upstream.py" <<PY
import http.server, re, threading
from urllib.parse import parse_qs
TILE = re.compile(r"^/dzi/[A-Za-z0-9_-]+_files/[0-9]+/[0-9]+_[0-9]+\.jpeg$")
DESC = re.compile(r"^/dzi/[A-Za-z0-9_-]+\.dzi$")
FILE = re.compile(r"^/(thumbs|assets)/[A-Za-z0-9._/-]+$")
HITS = {}
LOCK = threading.Lock()
class H(http.server.BaseHTTPRequestHandler):
    authz_calls = 0
    def log_message(self, *a): pass
    def send(self, ctype, body, extra=(), status=200):
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        for k, v in extra:
            self.send_header(k, v)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers(); self.wfile.write(body)
    def do_GET(self):
        # Route on the request line, not self.path: http.server collapses a
        # leading "//" in self.path, which uvicorn (the real app) does not.
        target = self.requestline.split(" ")[1]
        p, _, q = target.partition("?")
        if p == "/api/edge-authz":
            H.authz_calls += 1
            self.send_response(204); self.end_headers(); return
        if p == "/__authz_calls":
            return self.send("text/plain", str(H.authz_calls).encode())
        if p == "/__hits":
            t = parse_qs(q).get("t", [""])[0]
            return self.send("text/plain", str(HITS.get(t, 0)).encode())
        if TILE.match(p) or DESC.match(p):
            with LOCK:
                HITS[target] = HITS.get(target, 0) + 1
            v = parse_qs(q, keep_blank_values=True).get("v", [""])[0]
            if v == "7":
                # An out-of-range tile coordinate: the app 404s it WITHOUT
                # marking the answer uncacheable.
                return self.send("application/json", b'{"detail":"Tile not found"}', (), status=404)
            if v == "9":
                # The app's answer to a stale / malformed generation.
                return self.send("application/json", b'{"detail":"Slide not found"}',
                                 (("X-Accel-Expires", "0"),), status=404)
            extra = (("X-Accel-Expires", "0"),) if v == "8" else ()
            if TILE.match(p):
                return self.send("image/jpeg", b"\xff\xd8\xff\xd9", extra)
            return self.send("application/xml", b"<Image/>", extra)
        if p in ("/thumbs/missing.jpg", "/assets/missing.png"):
            with LOCK:
                HITS[target] = HITS.get(target, 0) + 1
            return self.send("application/json", b'{"detail":"Not Found"}', (), status=404)
        if FILE.match(p) and "//" not in p and "/." not in p:
            if p.endswith(".html"):
                # What main._TourAssetFiles sends for a non-media asset.
                return self.send("text/html", b"<script>alert(1)</script>", (
                    ("X-Content-Type-Options", "nosniff"),
                    ("Content-Disposition", "attachment"),
                    ("Content-Security-Policy", "default-src 'none'; sandbox"),
                ))
            return self.send("image/png", b"\x89PNG\r\n\x1a\n",
                             (("X-Content-Type-Options", "nosniff"),))
        # Everything else: the SPA shell, as serve_spa answers.
        return self.send("text/html", b"<!doctype html><title>spa</title>")
http.server.ThreadingHTTPServer(("127.0.0.1", ${UP_PORT}), H).serve_forever()
PY
setsid python3 "${WORK}/upstream.py" >/dev/null 2>&1 < /dev/null &
UP_PID=$!
disown "${UP_PID}" 2>/dev/null || true

# The /dzi/ maps the snippet needs, verbatim from wsi.conf ($wsi_authz_object
# puts the slide into the per-slide authz verdict's key, $wsi_dzi_unversioned
# keeps tile requests without a ?v= out of the cache, $wsi_dzi_tile_cache_control
# sets the tiles' browser caching) — the real expressions,
# not stubs, so a test that touches /dzi/ exercises what ships.
real_map() { awk -v v="$1" '$1 == "map" && $3 == v { c = 1 } c { print } c && /^}/ { exit }' "${SITE_CONF}"; }
DZI_MAPS="$(real_map '$wsi_authz_object')
$(real_map '$wsi_dzi_unversioned')
$(real_map '$wsi_dzi_tile_cache_control')
$(real_map '$wsi_dzi_uncached_key')
$(real_map '$wsi_dzi_authed_key')"
if ! grep -q 'wsi_authz_object' <<<"${DZI_MAPS}" || ! grep -q 'wsi_dzi_unversioned' <<<"${DZI_MAPS}" \
    || ! grep -q 'wsi_dzi_tile_cache_control' <<<"${DZI_MAPS}"; then
    echo "FAIL - could not extract the /dzi/ maps from ${SITE_CONF}"
    exit 1
fi

# --- nginx.conf: stub the http-context the snippet needs, then include it ----
# NOTE: unquoted heredoc so ${WORK} expands; escape nginx's own \$ variables.
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

    limit_req_zone \$binary_remote_addr zone=wsi_admin_login:1m rate=10r/m;
    limit_req_zone \$binary_remote_addr zone=wsi_app_login:1m   rate=10r/m;
    limit_req_zone \$binary_remote_addr zone=wsi_admin_login_net:1m rate=30r/m;
    limit_req_zone \$binary_remote_addr zone=wsi_app_login_net:1m rate=120r/m;
    limit_req_zone \$binary_remote_addr zone=wsi_api:1m         rate=120r/m;
    limit_req_zone \$binary_remote_addr zone=wsi_sync_ws:1m     rate=600r/m;
    limit_req_zone \$binary_remote_addr zone=wsi_slide_detail:1m rate=600r/m;
    limit_req_zone \$binary_remote_addr zone=wsi_page_reads:1m rate=600r/m;
    limit_req_zone \$binary_remote_addr zone=wsi_sync_peek:1m rate=600r/m;
    limit_req_zone \$binary_remote_addr zone=wsi_spa:1m         rate=600r/m;
    limit_req_zone \$wsi_dzi_uncached_key  zone=wsi_dzi_uncached:1m rate=600r/m;
    limit_req_zone \$wsi_dzi_authed_key    zone=wsi_dzi_authed:1m rate=3000r/m;

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
# --path-as-is: send "//" and dot segments verbatim, as an attacker would.
status() { curl -s --path-as-is -o /dev/null -w '%{http_code}' -m 5 "${BASE}$1"; }
ctype()  { curl -s --path-as-is -o /dev/null -w '%{content_type}' -m 5 "${BASE}$1"; }

expect_status() {
    local path="$1" want="$2" got
    got="$(status "$path")"
    if [[ "$got" == "$want" ]]; then
        printf 'ok   - %-48s -> %s\n' "$path" "$got"
    else
        fail "$path returned $got, expected $want"
    fi
}

TILE="/dzi/abc123_files/12/3_4.jpeg?v=1"

# 1. Poisoning attempts are refused before the cache is touched ...
for p in \
    "/${TILE}" \
    "/dzi//abc123_files/12/3_4.jpeg" \
    "/dzi/x/../abc123_files/12/3_4.jpeg" \
    "/dzi/./abc123_files/12/3_4.jpeg" \
    "/dzi/abc123_files%2F12/3_4.jpeg" \
    "/dzi/abc123_files/12/%2E%2E/12/3_4.jpeg" \
    "//dzi/abc123.dzi" \
    "//dzi/abc123.szi" \
    "//thumbs/1.jpg" \
    "//assets/tours/tp/pic.png" \
    "/assets/tours/tp/../tp/pic.png" \
    "/%64zi/abc123_files/12/3_4.jpeg?v=1" \
    "/dzi/abc%31_files/1/0_0.jpeg?v=1" \
    "/dzi/abc123.dz%69?v=1" \
    "/dzi/abc123.sz%69"
do
    # The last four: any "%" in a /dzi/ path is refused, because the per-slide
    # authz verdict is keyed on the raw URI (see $wsi_authz_object in wsi.conf)
    # while the tile cache keys on the decoded one.
    expect_status "$p" 400
done

# 2. ... so the canonical tile still serves (and caches) the real image.
for _ in 1 2; do
    ct="$(ctype "${TILE}")"
    if [[ "$ct" == image/jpeg* ]]; then
        printf 'ok   - %-48s -> %s\n' "${TILE}" "$ct"
    else
        fail "${TILE} served '${ct}' (cache poisoned?), expected image/jpeg"
    fi
done

# 2b. Absolute-form request targets ("GET http://host/path") must be judged on
#     the path alone: the canonical one still serves, a "//" one is refused.
abs_status() {
    curl -s -o /dev/null -w '%{http_code}' -m 5 \
        --request-target "http://127.0.0.1:${NG_PORT}$1" "${BASE}/"
}
c="$(abs_status "${TILE}")"
if [[ "$c" == "200" ]]; then
    printf 'ok   - absolute-form %-34s -> %s\n' "${TILE}" "$c"
else
    fail "absolute-form ${TILE} returned $c, expected 200"
fi
c="$(abs_status "/${TILE}")"
if [[ "$c" == "400" ]]; then
    printf 'ok   - absolute-form %-34s -> %s\n' "/${TILE}" "$c"
else
    fail "absolute-form /${TILE} returned $c, expected 400"
fi

# 3. Legitimate paths — including a query string on thumbs — are unaffected.
expect_status "/dzi/abc123.dzi" 200
expect_status "/thumbs/1.jpg?v=1700000000" 200
expect_status "/assets/tours/tp/pic.png" 200
expect_status "/assets/tours/tp/pic.v2.png" 200

# 4. The app's protective headers on a non-media tour asset survive nginx and
#    its cache (MISS, then HIT), and nosniff is sent exactly once.
for pass in miss hit; do
    h="$(curl -s -D - -o /dev/null -m 5 "${BASE}/assets/tours/tp/legacy.html" | tr -d '\r')"
    n="$(grep -ci '^x-content-type-options: nosniff' <<<"$h")"
    if grep -qi '^content-disposition: attachment' <<<"$h" \
        && grep -qi "^content-security-policy: .*sandbox" <<<"$h" \
        && [[ "$n" == "1" ]]; then
        printf 'ok   - /assets/ legacy.html (%s) keeps attachment + sandbox CSP, one nosniff\n' "$pass"
    else
        fail "/assets/ legacy.html (${pass}) headers wrong (nosniff x${n}):"$'\n'"$h"
    fi
done

# 5. Which Authorization headers bypass the shared cache ($wsi_auth_nocache):
#    HTTP Basic (app-access basic mode; published content only) shares it like
#    a cookie user, while a staff Bearer (draft previews) or any unknown scheme
#    stays out of it.
cache_status() {
    curl -s -o /dev/null -D - -m 5 -H "Authorization: $2" "${BASE}$1" \
        | tr -d '\r' | awk -F': ' 'tolower($1)=="x-cache-status"{print $2}'
}
expect_cache() {
    local path="$1" auth="$2" want="$3" got
    got="$(cache_status "$path" "$auth")"
    if [[ "$got" == "$want" ]]; then
        printf 'ok   - %-30s %-22s -> %s\n' "$path" "(${auth%% *})" "$got"
    else
        fail "$path with '${auth%% *}' auth: X-Cache-Status '$got', expected $want"
    fi
}
BT="/dzi/basic1_files/1/0_0.jpeg?v=1"
expect_cache "$BT" "Basic YWxpY2U6cHc=" MISS
expect_cache "$BT" "Basic YWxpY2U6cHc=" HIT
expect_cache "$BT" "Bearer staff.jwt.token" BYPASS
expect_cache "$BT" "bearer staff.jwt.token" BYPASS
expect_cache "$BT" "Negotiate abc" BYPASS

# 6. The authz verdict ($wsi_authz_nocache): a request carrying any
#    Authorization header is re-checked every time, with or without a session
#    cookie (a 204 earned by the header must not be cached under a cookie that
#    may be junk); a cookie-only request is cached per cookie.
authz_calls() { curl -s -m 5 "http://127.0.0.1:${UP_PORT}/__authz_calls"; }
AT="/dzi/basic2_files/1/0_0.jpeg"
a0="$(authz_calls)"
for _ in 1 2; do
    curl -s -o /dev/null -m 5 -H "Authorization: Basic YWxpY2U6cHc=" "${BASE}${AT}"
done
a1="$(authz_calls)"
for _ in 1 2; do
    curl -s -o /dev/null -m 5 -H "Authorization: Basic YWxpY2U6cHc=" \
        -H "Cookie: wsi_session=basic-session-1" "${BASE}${AT}"
done
a2="$(authz_calls)"
# The cookie the Basic requests above presented must not have left a cached
# allow behind: a cookie-only request with it is checked live.
curl -s -o /dev/null -m 5 -H "Cookie: wsi_session=basic-session-1" "${BASE}${AT}"
a3="$(authz_calls)"
for _ in 1 2; do
    curl -s -o /dev/null -m 5 -H "Cookie: wsi_session=cookie-only-1" "${BASE}${AT}"
done
a4="$(authz_calls)"
if [[ $((a1 - a0)) -eq 2 ]]; then
    printf 'ok   - Basic without session cookie: authz checked every time\n'
else
    fail "Basic without cookie hit the authz upstream $((a1 - a0))x for 2 requests, expected 2"
fi
if [[ $((a2 - a1)) -eq 2 ]]; then
    printf 'ok   - Basic with session cookie: authz checked every time\n'
else
    fail "Basic with cookie hit the authz upstream $((a2 - a1))x for 2 requests, expected 2"
fi
if [[ $((a3 - a2)) -eq 1 ]]; then
    printf 'ok   - cookie seen alongside Basic left no cached verdict\n'
else
    fail "cookie-only replay of a Basic request's cookie was served from cache"
fi
if [[ $((a4 - a3)) -eq 1 ]]; then
    printf 'ok   - cookie-only: authz verdict cached\n'
else
    fail "cookie-only hit the authz upstream $((a4 - a3))x for 2 requests, expected 1"
fi

# 7. Tile generation (?v=N) in the cache key. A WSI replace bumps the slide's
#    generation, so the viewer's tile URLs change; the key must follow, or the
#    old scan's cached tiles keep answering the new URLs for 30 days.
xcache() {
    curl -s -o /dev/null -D - -m 5 "${BASE}$1" \
        | tr -d '\r' | awk -F': ' 'tolower($1)=="x-cache-status"{print $2}'
}
hits() { curl -s -m 5 -G --data-urlencode "t=$1" "http://127.0.0.1:${UP_PORT}/__hits"; }
expect_xcache() {
    local path="$1" want="$2" got
    got="$(xcache "$path")"
    if [[ "$got" == "$want" ]]; then
        printf 'ok   - %-44s -> %s\n' "$path" "$got"
    else
        fail "$path: X-Cache-Status '$got', expected $want"
    fi
}
expect_hits() {
    local target="$1" want="$2" what="$3" got
    got="$(hits "$target")"
    if [[ "$got" == "$want" ]]; then
        printf 'ok   - %s\n' "$what"
    else
        fail "$what: $target reached the upstream ${got}x, expected ${want}"
    fi
}
# nginx writes each cache file's key as a "KEY: <key>" header line.
cache_keys() { grep -rhaE '^KEY: ' "${WORK}/cache" 2>/dev/null | sed 's/^KEY: //'; }
key_ends_with() {
    cache_keys | awk -v k="$1" 'substr($0, length($0) - length(k) + 1) == k { f = 1 } END { exit !f }'
}

# 7a. The generation is part of the key: v=1 is stored and re-served, v=2 is a
#     separate entry, and any other query arg still folds out.
G1="/dzi/gen1_files/1/0_0.jpeg"
expect_xcache "${G1}?v=1" MISS
expect_xcache "${G1}?v=1" HIT
expect_xcache "${G1}?v=1&r=junk" HIT
expect_xcache "${G1}?v=2" MISS
expect_hits "${G1}?v=1" 1 "v=1 tile fetched from the app once, then served from cache"
sleep 0.3
if key_ends_with "${G1}?v=1" && key_ends_with "${G1}?v=2"; then
    printf 'ok   - tile cache keys carry the generation (?v=1 / ?v=2 stored apart)\n'
else
    fail "tile cache key does not include ?v= (keys: $(cache_keys | grep gen1_files | tr '\n' ' '))"
fi

# 7b. A tile request without a generation (a script or crafted request) is
#     served but never stored or answered from the cache — the bare key
#     could otherwise hold any generation's bytes. An empty v= is the same
#     thing to nginx ($arg_v is empty either way).
G2="/dzi/gen2_files/1/0_0.jpeg"
expect_xcache "${G2}" BYPASS
expect_xcache "${G2}" BYPASS
expect_xcache "${G2}?v=" BYPASS
expect_hits "${G2}" 2 "unversioned tile reached the app every time"
sleep 0.3
if cache_keys | grep -qF "${G2}"; then
    fail "an unversioned tile response was written to the cache"
else
    printf 'ok   - unversioned tile response not stored\n'
fi

# 7c. The .dzi descriptor (nested location) is keyed the same way.
expect_xcache "/dzi/gen3.dzi?v=1" MISS
expect_xcache "/dzi/gen3.dzi?v=1" HIT
expect_xcache "/dzi/gen3.dzi" BYPASS

# 7d. The app's "X-Accel-Expires: 0" is honoured (only Cache-Control is
#     ignored in /dzi/): its stale-generation 404 is not stored despite
#     proxy_cache_valid 404, a 200 carrying it is not stored either, and the
#     header itself never reaches the client.
G4="/dzi/gen4_files/1/0_0.jpeg"
for _ in 1 2; do
    c="$(curl -s -o /dev/null -w '%{http_code}' -m 5 "${BASE}${G4}?v=9")"
    [[ "$c" == "404" ]] || fail "stale-generation tile returned $c, expected 404"
done
expect_hits "${G4}?v=9" 2 "stale-generation 404 (X-Accel-Expires: 0) not cached"
expect_xcache "${G4}?v=8" MISS
expect_xcache "${G4}?v=8" MISS
if curl -s -D - -o /dev/null -m 5 "${BASE}${G4}?v=9" | grep -qi '^x-accel-expires'; then
    fail "X-Accel-Expires leaked to the client"
else
    printf 'ok   - X-Accel-Expires is not forwarded to the client\n'
fi

# 8. A 404 is never stored. The app answers a tile coordinate outside the
#    pyramid with a plain 404 (no X-Accel-Expires), and each distinct made-up
#    coordinate, if stored, would write a cache file that the tile zone keeps
#    for its whole 30-day inactive window. Neither /dzi/ nor the thumbnails
#    stores one.
G5="/dzi/gen5_files/99/99_99.jpeg"
for _ in 1 2; do
    c="$(curl -s -o /dev/null -w '%{http_code}' -m 5 "${BASE}${G5}?v=7")"
    [[ "$c" == "404" ]] || fail "out-of-range tile returned $c, expected 404"
done
expect_hits "${G5}?v=7" 2 "an out-of-range tile's 404 reaches the app every time (not stored)"
for _ in 1 2; do
    c="$(curl -s -o /dev/null -w '%{http_code}' -m 5 "${BASE}/thumbs/missing.jpg")"
    [[ "$c" == "404" ]] || fail "missing thumbnail returned $c, expected 404"
done
expect_hits "/thumbs/missing.jpg" 2 "a missing thumbnail's 404 reaches the app every time (not stored)"
for _ in 1 2; do
    c="$(curl -s -o /dev/null -w '%{http_code}' -m 5 "${BASE}/assets/missing.png")"
    [[ "$c" == "404" ]] || fail "missing tour attachment returned $c, expected 404"
done
expect_hits "/assets/missing.png" 2 "a missing attachment's 404 reaches the app every time (not stored)"
for _ in 1 2; do
    c="$(curl -s -o /dev/null -w '%{http_code}' -m 5 "${BASE}/dzi/gen5.dzi?v=7")"
    [[ "$c" == "404" ]] || fail "descriptor of a missing slide returned $c, expected 404"
done
expect_hits "/dzi/gen5.dzi?v=7" 2 "a descriptor's 404 reaches the app every time (not stored)"
sleep 0.3
if grep -rqa "KEY: .*gen5" "${WORK}/cache" 2>/dev/null || grep -rqa "KEY: .*missing\." "${WORK}/static" 2>/dev/null; then
    fail "a 404 was written to a cache zone"
else
    printf 'ok   - no 404 body was written to either cache zone\n'
fi

# 9. Thumbnails and tour attachments have a zone of their own (wsi_static_cache),
#    keyed on the path alone. Their names are the client's to choose, so a flood
#    of junk ones must not be able to push tiles out of the tile zone; and the
#    `?v=` of a thumbnail URL is the client's to choose too, so it must not
#    multiply the entries of one real thumbnail.
T1="/thumbs/42-abcdef0123456789.jpg"
expect_xcache "${T1}?v=1700000000" MISS
expect_xcache "${T1}?v=1" HIT
expect_xcache "${T1}?v=2&r=junk" HIT
expect_xcache "${T1}" HIT
expect_xcache "/assets/tours/tp/spray.png" MISS
expect_xcache "/assets/tours/tp/spray.png?r=junk" HIT
sleep 0.3
n_thumb_entries="$(grep -rhaE "^KEY: .*/thumbs/42-abcdef0123456789.jpg" "${WORK}/static" 2>/dev/null | wc -l)"
if [[ "$n_thumb_entries" -eq 1 ]]; then
    printf 'ok   - one thumbnail, many ?v= spellings -> one cache entry\n'
else
    fail "thumbnail stored ${n_thumb_entries}x in the static zone, expected 1 (client-chosen ?v= in the key?)"
fi
if grep -rqaE "KEY: .*/(thumbs|assets)/" "${WORK}/cache" 2>/dev/null; then
    fail "a thumbnail or attachment was stored in the TILE zone"
else
    printf 'ok   - thumbnails and attachments never enter the tile zone\n'
fi
if grep -rqaE "KEY: .*/(thumbs|assets)/" "${WORK}/static" 2>/dev/null; then
    printf 'ok   - ...they are stored in the static zone\n'
else
    fail "no thumbnail or attachment entry in the static zone"
fi

printf -- '--------------------------------------------------------------\n'
if [[ "$FAILED" -eq 0 ]]; then
    printf 'PASS - non-canonical paths cannot poison the shared cache\n'
else
    exit 1
fi
