#!/usr/bin/env bash
# The browser-caching policy of Deep Zoom tiles in the REAL nginx snippet
# (infra/nginx/wsi-locations.conf + the $wsi_dzi_tile_cache_control map in
# infra/nginx/wsi.conf).
#
# Run: bash infra/tests/test_tile_cache_control_nginx.sh
#
# WHAT IT GUARDS
# --------------
# A tile whose URL carries the slide's current generation (?v=N) is immutable,
# so the browser may reuse it for a day ("private, max-age=86400") instead of
# revalidating every time OpenSeadragon redraws it. nginx applies that policy
# at the edge, replacing whatever Cache-Control the response carries — so a
# tile already in the 30-day cache (stored with the app's "no-cache") gets it
# too, without clearing the cache. Everything else must
# stay "no-cache": a tile without ?v=, an error (a stale generation's 404), the
# .dzi descriptor (what keeps an unpublished slide from being reopened) and the
# .szi archive. The upstream below answers like the app does without the
# edge policy: "Cache-Control: no-cache" + an ETag on every /dzi/ response.
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
UP_PORT="${TILE_CC_TEST_UP_PORT:-18231}"
NG_PORT="${TILE_CC_TEST_NG_PORT:-18232}"
UP_PID=""
cleanup() {
    [[ -n "${UP_PID}" ]] && kill "${UP_PID}" 2>/dev/null
    [[ -f "${WORK}/nginx.pid" ]] && "${NGINX_BIN}" -c "${WORK}/nginx.conf" -s quit 2>/dev/null
    rm -rf "${WORK}"
}
trap cleanup EXIT
mkdir -p "${WORK}"/{cache,authz,body,proxy,fastcgi,uwsgi,scgi}

# --- upstream: the app as it answers without the edge tile policy ----------
cat > "${WORK}/upstream.py" <<PY
import http.server, re
from urllib.parse import parse_qs
TILE = re.compile(r"^/dzi/[A-Za-z0-9_-]+_files/[0-9]+/[0-9]+_[0-9]+\.jpeg$")
DESC = re.compile(r"^/dzi/[A-Za-z0-9_-]+\.dzi$")
SZI = re.compile(r"^/dzi/[A-Za-z0-9_-]+\.szi$")
class H(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a): pass
    def send(self, status, ctype, body, extra=()):
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        for k, v in extra:
            self.send_header(k, v)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers(); self.wfile.write(body)
    def do_GET(self):
        p, _, q = self.path.partition("?")
        if p == "/api/edge-authz":
            # The access gate: refuse two slides the way the app refuses a
            # signed-out client (401) and a draft / filtered one (403).
            uri = self.headers.get("X-Original-URI", "")
            code = 401 if "/dzi/anon" in uri else 403 if "/dzi/denied" in uri else 204
            self.send_response(code); self.send_header("Content-Length", "0"); self.end_headers(); return
        if p.startswith("/dzi/boom"):
            return self.send(500, "text/plain", b"boom", (("Cache-Control", "no-cache"),))
        v = parse_qs(q, keep_blank_values=True).get("v", [""])[0]
        old = (("Cache-Control", "no-cache"), ("ETag", '"e1"'))
        if v == "9":
            # The app's answer to a stale generation.
            return self.send(404, "application/json", b'{"detail":"Slide not found"}',
                             old + (("X-Accel-Expires", "0"),))
        if self.headers.get("If-None-Match") == '"e1"':
            self.send_response(304)
            for k, val in old:
                self.send_header(k, val)
            self.end_headers(); return
        if TILE.match(p):
            return self.send(200, "image/jpeg", b"\xff\xd8\xff\xd9", old)
        if DESC.match(p):
            return self.send(200, "application/xml", b"<Image/>", old)
        if SZI.match(p):
            return self.send(206, "application/zip", b"PK", old + (
                ("Accept-Ranges", "bytes"), ("Content-Range", "bytes 0-1/100")))
        return self.send(404, "text/plain", b"nope")
http.server.ThreadingHTTPServer(("127.0.0.1", ${UP_PORT}), H).serve_forever()
PY
setsid python3 "${WORK}/upstream.py" >/dev/null 2>&1 < /dev/null &
UP_PID=$!
disown "${UP_PID}" 2>/dev/null || true

# The /dzi/ maps the snippet needs, verbatim from wsi.conf — the real
# expressions, so this exercises the policy that ships.
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
IMMUTABLE="private, max-age=86400"

# Every Cache-Control header of a response, joined by " | " (so a duplicated
# or missing header shows), plus its status and X-Cache-Status.
probe() {
    curl -s -D - -o /dev/null -m 5 "$@" | tr -d '\r' | awk -F': ' '
        NR == 1 { status = $0; sub(/^HTTP\/[0-9.]+ /, "", status); sub(/ .*/, "", status) }
        tolower($1) == "cache-control" { cc = cc (cc == "" ? "" : " | ") $2 }
        tolower($1) == "x-cache-status" { xc = $2 }
        END { printf "%s;%s;%s\n", status, xc, cc }'
}
expect() {
    local what="$1" want_status="$2" want_cc="$3"; shift 3
    local got status xc cc
    got="$(probe "$@")"
    IFS=';' read -r status xc cc <<<"$got"
    if [[ "$status" == "$want_status" && "$cc" == "$want_cc" ]]; then
        printf 'ok   - %-52s %s %-5s %s\n' "$what" "$status" "${xc:--}" "$cc"
    else
        fail "${what}: got status ${status}, Cache-Control '${cc}' (X-Cache-Status ${xc:--}); expected ${want_status}, '${want_cc}'"
    fi
}

T="/dzi/abc123_files/12/3_4.jpeg"

# 1. A versioned tile gets the day-long private policy, exactly once, both
#    from the app (MISS) and from the cache — whose stored copy carries the
#    app's old "no-cache" (HIT).
expect "versioned tile (MISS)"                       200 "${IMMUTABLE}" "${BASE}${T}?v=1"
expect "versioned tile (HIT, stored with no-cache)"  200 "${IMMUTABLE}" "${BASE}${T}?v=1"

# 2. A browser revalidating after the day is up gets a 304 that renews it.
expect "conditional request on a cached tile -> 304" 304 "${IMMUTABLE}" -H 'If-None-Match: "e1"' "${BASE}${T}?v=1"

# 3. Draft previews (staff Bearer, never shared) and Basic-auth viewers get the
#    same per-browser policy: private keeps them out of shared caches.
expect "versioned tile, staff Bearer (BYPASS)"        200 "${IMMUTABLE}" -H "Authorization: Bearer staff.jwt" "${BASE}${T}?v=1"

# 4. Everything else stays no-cache.
expect "tile without ?v= (script)"                   200 "no-cache" "${BASE}${T}"
expect "tile with an empty ?v="                       200 "no-cache" "${BASE}${T}?v="
expect "stale generation (404)"                       404 "no-cache" "${BASE}${T}?v=9"
expect "descriptor (MISS)"                            200 "no-cache" "${BASE}/dzi/abc123.dzi?v=1"
expect "descriptor (HIT)"                             200 "no-cache" "${BASE}/dzi/abc123.dzi?v=1"
expect "SZI archive Range read"                       206 "no-cache" -H "Range: bytes=0-1" "${BASE}/dzi/abc123.szi?v=1"
expect "non-canonical path (400)"                     400 "no-cache" --path-as-is "${BASE}/dzi//abc123_files/12/3_4.jpeg?v=1"

# 5. A refusal or a failure is never cached by the browser — a day-long 403
#    would keep a re-published slide or a signed-in user on broken tiles.
expect "access gate: signed out (401)"                401 "no-cache" "${BASE}/dzi/anon_files/12/3_4.jpeg?v=1"
expect "access gate: refused (403)"                   403 "no-cache" "${BASE}/dzi/denied_files/12/3_4.jpeg?v=1"
expect "app error (500)"                              500 "no-cache" "${BASE}/dzi/boom_files/12/3_4.jpeg?v=1"
kill "${UP_PID}" 2>/dev/null; UP_PID=""; sleep 0.5
got="$(probe "${BASE}/dzi/down1_files/12/3_4.jpeg?v=1")"
IFS=';' read -r status xc cc <<<"$got"
if [[ "$status" =~ ^5 && "$cc" == "no-cache" ]]; then
    printf 'ok   - %-52s %s %-5s %s\n' "app down" "$status" "${xc:--}" "$cc"
else
    fail "app down: got status ${status}, Cache-Control '${cc}'; expected a 5xx with 'no-cache'"
fi

printf -- '--------------------------------------------------------------\n'
if [[ "$FAILED" -eq 0 ]]; then
    printf 'PASS - tiles get their browser-caching policy; everything else stays no-cache\n'
else
    exit 1
fi
