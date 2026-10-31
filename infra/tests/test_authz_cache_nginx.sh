#!/usr/bin/env bash
# Guard the edge app-access verdict cache (/_wsi_authz) in the REAL nginx
# config: it must live in its own short-lived zone and never store a denial.
#
# Run: bash infra/tests/test_authz_cache_nginx.sh
#
# WHY THIS EXISTS
# ---------------
# /_wsi_authz caches the /api/edge-authz verdict under a key that folds in the
# client's session cookie. Sharing the tile zone (inactive=30d) and caching
# 401/403 would let every junk cookie sent by a client with no login at all
# leave a 30-day node in the tile zone; enough of them LRU-evict real tiles.
# The verdicts therefore go to wsi_authz_cache (declared in wsi.conf, taken
# verbatim below with only its path rewritten) and only 2xx is stored.
#
# The verdict is also PER SLIDE for /dzi/ (section 4): /api/edge-authz gets the
# original URI as X-Original-URI and refuses an unpublished / deleted slide
# with 403, so the cached allow for one slide must never be replayed for
# another — the key folds in the slide token ($wsi_authz_object, the real map
# from wsi.conf). Otherwise a tile cached while a slide was public stays
# servable for the tile cache's 30 days after it is unpublished.
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
UP_PORT="${AUTHZ_TEST_UP_PORT:-18211}"
NG_PORT="${AUTHZ_TEST_NG_PORT:-18212}"
UP_PID=""
cleanup() {
    [[ -n "${UP_PID}" ]] && kill "${UP_PID}" 2>/dev/null
    [[ -f "${WORK}/nginx.pid" ]] && "${NGINX_BIN}" -c "${WORK}/nginx.conf" -s quit 2>/dev/null
    rm -rf "${WORK}"
}
trap cleanup EXIT
mkdir -p "${WORK}"/{cache,body,proxy,fastcgi,uwsgi,scgi}

# --- the real authz zone declaration from wsi.conf ---------------------------
AUTHZ_ZONE="$(awk '
    /proxy_cache_path[ \t]+\/var\/cache\/nginx\/wsi_authz/ { c = 1 }
    c { print }
    c && /;/ { exit }
' "${SITE_CONF}")"
if [[ -z "${AUTHZ_ZONE}" ]]; then
    echo "FAIL - no proxy_cache_path for /var/cache/nginx/wsi_authz in ${SITE_CONF}"
    exit 1
fi
grep -q 'keys_zone=wsi_authz_cache:' <<<"${AUTHZ_ZONE}" \
    && pass "wsi.conf declares a separate wsi_authz_cache zone" \
    || fail "authz proxy_cache_path does not declare keys_zone=wsi_authz_cache"
# The cache-size tooling rewrites every max_size= and reads the first one; the
# authz zone must not carry one, and the tile zone must stay the first match.
if grep -q 'max_size=' <<<"${AUTHZ_ZONE}"; then
    fail "authz zone carries a max_size= that nginx-cache.sh would rewrite"
else
    pass "authz zone has no max_size= (cache-size tooling stays on the tile zone)"
fi
first_path="$(grep -m1 -oE 'proxy_cache_path[[:space:]]+[^[:space:]]+' "${SITE_CONF}" | awk '{print $2}')"
[[ "${first_path}" == "/var/cache/nginx/wsi_dzi" ]] \
    && pass "tile zone is still the first proxy_cache_path in wsi.conf" \
    || fail "first proxy_cache_path in wsi.conf is ${first_path}, expected the tile zone"
AUTHZ_ZONE="${AUTHZ_ZONE//\/var\/cache\/nginx\/wsi_authz/${WORK}/authz}"

# --- upstream: edge-authz admits only the "good" cookie; counts every probe ---
# Like the app (dzi.edge_slide_access) it also judges the slide named by
# X-Original-URI: "draft" (an unpublished slide) and any /dzi/ URI that does
# not parse are 403. Probes are counted per cookie and per (cookie, URI path).
cat > "${WORK}/upstream.py" <<PY
import http.server, re, threading
from http.cookies import SimpleCookie
from urllib.parse import urlsplit, parse_qs
HITS = {}
LAST_URI = {"v": ""}
LOCK = threading.Lock()
DZI = re.compile(r"/dzi/([A-Za-z0-9_-]{1,64})(?:_files/.*|\.dzi|\.szi)")
class H(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a): pass
    def reply(self, body):
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers(); self.wfile.write(body)
    def do_GET(self):
        u = urlsplit(self.path)
        if u.path == "/__count":
            c = parse_qs(u.query).get("c", [""])[0]
            return self.reply(str(HITS.get(c, 0)).encode())
        if u.path == "/__last_uri":
            return self.reply(LAST_URI["v"].encode())
        if u.path == "/api/edge-authz":
            jar = SimpleCookie(self.headers.get("Cookie", ""))
            c = jar["wsi_session"].value if "wsi_session" in jar else ""
            orig = self.headers.get("X-Original-URI", "")
            opath = orig.split("?", 1)[0]
            with LOCK:
                HITS[c] = HITS.get(c, 0) + 1
                HITS[c + " " + opath] = HITS.get(c + " " + opath, 0) + 1
                LAST_URI["v"] = orig
            # Like the app's LAN exemptions (breakglass_exempt / lan_direct_exempt):
            # no login needed when EVERY hop in X-Forwarded-For before nginx's
            # own (the last, added by \$proxy_add_x_forwarded_for) is a LAN address.
            hops = [h.strip() for h in self.headers.get("X-Forwarded-For", "").split(",") if h.strip()]
            lan = c == "" and len(hops) >= 2 and all(h.startswith("192.168.") for h in hops[:-1])
            # The other two things the LAN exemptions look at: a bare private-IP
            # Host (dashboard LAN-direct "ip" level) and a plaintext listener
            # (break-glass, LAN client). Neither depends on the URI, as in the app;
            # the test's other requests use neither a 192.168.* Host nor this client.
            host = self.headers.get("Host", "")
            plain = self.headers.get("X-Forwarded-Proto", "") == "http"
            by_host = c == "" and host.startswith("192.168.")
            by_proto = c == "" and plain and self.headers.get("Cf-Connecting-IP", "") == "198.51.100.77"
            status = 204 if (c == "good" or lan or by_host or by_proto) else 401
            if status == 204 and opath.startswith("/dzi/"):
                m = DZI.fullmatch(opath)
                if m is None or m.group(1) == "draft":
                    status = 403
            self.send_response(status)
            self.send_header("Content-Length", "0")
            self.end_headers(); return
        body = b"\xff\xd8\xff\xd9"
        self.send_response(200)
        self.send_header("Content-Type", "image/jpeg")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers(); self.wfile.write(body)
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
# The scheme the VISITOR used ($wsi_xfp), as wsi.conf derives it: the listener's,
# or the one Cloudflare's edge reported over a trusted loopback hop. The verdict
# of the break-glass exemption depends on it, so the key must carry it.
real_geo() { awk -v v="$1" '$1 == "geo" && $2 == v { c = 1 } c { print } c && /^}/ { exit }' "${SITE_CONF}"; }
# ... and who the client is: a trusted hop's Cf-Connecting-IP, else its address.
CLIENT_MAPS="$(real_map '$wsi_cf_connecting_ip')
$(real_map '$wsi_rl_key')"
XFP_MAPS="$(real_geo '$cf_hop_trusted')
$(real_map '$wsi_cf_edge_scheme')
$(real_map '$wsi_cf_edge_scheme_trusted')
$(real_map '$wsi_xfp')"
for v in cf_hop_trusted wsi_cf_edge_scheme wsi_cf_edge_scheme_trusted wsi_xfp; do
    grep -qE "[\$]${v} [{]" <<<"${XFP_MAPS}" || { echo "FAIL - could not extract \$${v} from ${SITE_CONF}"; exit 1; }
done

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
${AUTHZ_ZONE}
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
${XFP_MAPS}
${CLIENT_MAPS}
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
TILE="/dzi/abc123_files/12/3_4.jpeg"
status_with() { curl -s -o /dev/null -w '%{http_code}' -m 5 -b "wsi_session=$1" "${BASE}${TILE}"; }
probes()      { curl -s -m 5 "http://127.0.0.1:${UP_PORT}/__count?c=$1"; }

# 1. An admitted session: tiles serve, and the verdict is cached (one probe).
for i in 1 2 3; do
    c="$(status_with good)"
    [[ "$c" == "200" ]] || fail "good cookie request $i returned $c, expected 200"
done
n="$(probes good)"
[[ "$n" == "1" ]] && pass "allow verdict cached: 3 tile requests -> ${n} edge-authz probe" \
                  || fail "good cookie made ${n} edge-authz probes, expected 1 (204 not cached?)"

# 2. A junk cookie is denied every time and the denial is NOT cached.
for i in 1 2 3; do
    c="$(status_with junk1)"
    [[ "$c" == "401" ]] || fail "junk cookie request $i returned $c, expected 401"
done
n="$(probes junk1)"
[[ "$n" == "3" ]] && pass "401 not cached: 3 junk-cookie requests -> ${n} edge-authz probes" \
                  || fail "junk cookie made ${n} edge-authz probes, expected 3 (401 cached?)"

# 3. Verdict entries land in the authz zone's directory, never the tile zone's,
#    and no denial was written anywhere. (nginx stores "KEY: <key>" in each
#    cache file's header.)
sleep 0.5
if grep -rqa 'KEY: authz|' "${WORK}/cache" 2>/dev/null; then
    fail "an authz| verdict was written into the TILE cache zone"
else
    pass "no authz| entry in the tile cache zone"
fi
if grep -rqa 'KEY: authz|.*|wsi_session=good|' "${WORK}/authz" 2>/dev/null; then
    pass "allow verdict stored in the separate authz zone"
else
    fail "allow verdict not found in ${WORK}/authz"
fi
if grep -rqa 'junk1' "${WORK}/authz" "${WORK}/cache" 2>/dev/null; then
    fail "a denied (junk-cookie) verdict was written to disk"
else
    pass "no denied verdict written to either zone"
fi

# 4. Per-slide verdicts. The same admitted session asks for several objects;
#    the probe count tells whether a cached verdict was reused.
get_good() { curl -s -o /dev/null -w '%{http_code}' -m 5 -b "wsi_session=good" "${BASE}$1"; }
expect_probe_delta() {   # expect_probe_delta PATH WANT_STATUS WANT_NEW_PROBES LABEL
    local before after code
    before="$(probes good)"
    code="$(get_good "$1")"
    after="$(probes good)"
    if [[ "$code" == "$2" && $((after - before)) -eq "$3" ]]; then
        pass "$4"
    else
        fail "$4: $1 -> HTTP ${code} with $((after - before)) new probe(s), expected HTTP $2 with $3"
    fi
}

# 4a. The probe sees the client's original URI, query included.
get_good "/dzi/slideA_files/1/0_0.jpeg?v=7" >/dev/null
last="$(curl -s -m 5 "http://127.0.0.1:${UP_PORT}/__last_uri")"
[[ "${last}" == "/dzi/slideA_files/1/0_0.jpeg?v=7" ]] \
    && pass "edge-authz receives X-Original-URI (${last})" \
    || fail "edge-authz saw X-Original-URI '${last}', expected /dzi/slideA_files/1/0_0.jpeg?v=7"

# 4b. One verdict per slide: other tiles and the descriptor of slide A reuse
#     it, slide B does not (an allow for A says nothing about B).
expect_probe_delta "/dzi/slideA_files/1/1_0.jpeg?v=7" 200 0 "another tile of the same slide reuses its verdict"
expect_probe_delta "/dzi/slideA.dzi?v=7" 200 0 "the slide's descriptor reuses its verdict"
expect_probe_delta "/dzi/slideB_files/1/0_0.jpeg?v=7" 200 1 "a different slide gets a verdict of its own"
expect_probe_delta "/dzi/slideB_files/2/0_0.jpeg?v=7" 200 0 "...which is then cached too"

# 4c. A slide the app refuses (unpublished / deleted) is 403 to the client on
#     every request: the denial is never cached and no other slide's allow is
#     replayed for it.
for i in 1 2 3; do
    expect_probe_delta "/dzi/draft_files/1/0_0.jpeg?v=1" 403 1 "refused slide re-checked and denied (request $i)"
done

# 4d. /thumbs/ keeps one app-access verdict per client, separate from the
#     per-slide ones; a /dzi/ URI that does not parse gets a key of its own and
#     so can never ride that thumbnail verdict.
expect_probe_delta "/thumbs/1.jpg?v=1" 200 1 "/thumbs/ verdict is not a slide verdict"
expect_probe_delta "/thumbs/2.jpg?v=1" 200 0 "/thumbs/ verdict is shared across thumbnails"
expect_probe_delta "/dzi/weird.xml" 403 1 "unparseable /dzi/ URI does not reuse the /thumbs/ verdict"

# 4e. The stored key names the slide.
sleep 0.5
if grep -rqaE '^KEY: authz\|.*\|wsi_session=good\|dzi:slideB$' "${WORK}/authz" 2>/dev/null; then
    pass "authz cache key carries the slide token (…|wsi_session=good|dzi:slideB)"
else
    fail "no authz key with the slide token in ${WORK}/authz: $(grep -rhaE '^KEY: ' "${WORK}/authz" | tr '\n' ' ')"
fi
if grep -rqa 'dzi:draft' "${WORK}/authz" 2>/dev/null; then
    fail "a refused slide's verdict was written to the authz zone"
else
    pass "no verdict stored for the refused slide"
fi

# 4f. The verdict depends on the X-Forwarded-For chain too (the LAN exemptions
#     judge every hop in it), so the key carries the chain: behind a further
#     proxy, a LAN client's cached allow must not be replayed for an Internet
#     client that arrives through the same proxy, same address, same (empty)
#     cookie. Both requests reach nginx from 127.0.0.1; the second chain has a
#     public hop in it, appended by that proxy.
lan_code="$(curl -s -o /dev/null -w '%{http_code}' -m 5 -H 'X-Forwarded-For: 192.168.1.5' "${BASE}/thumbs/lan.jpg")"
[[ "$lan_code" == "200" ]] && pass "LAN client behind a proxy: allowed by the app's rule" \
                           || fail "LAN-chain request returned $lan_code, expected 200"
before="$(probes '')"
net_code="$(curl -s -o /dev/null -w '%{http_code}' -m 5 -H 'X-Forwarded-For: 192.168.1.5, 203.0.113.9' "${BASE}/thumbs/lan.jpg")"
after="$(probes '')"
if [[ "$net_code" == "401" && $((after - before)) -eq 1 ]]; then
    pass "Internet client through the same proxy: checked on its own, and refused"
else
    fail "chain with a public hop got HTTP ${net_code} with $((after - before)) new probe(s), expected 401 with 1 (the LAN client's cached allow replayed?)"
fi
before="$(probes '')"
lan_code="$(curl -s -o /dev/null -w '%{http_code}' -m 5 -H 'X-Forwarded-For: 192.168.1.5' "${BASE}/thumbs/lan.jpg")"
after="$(probes '')"
[[ "$lan_code" == "200" && $((after - before)) -eq 0 ]] \
    && pass "...while the LAN client's own verdict is still served from the cache" \
    || fail "repeat LAN request got HTTP ${lan_code} with $((after - before)) new probe(s), expected 200 with 0"

# 4g. The verdict also depends on the Host (the dashboard's LAN-direct "ip" level
#     admits a bare private-IP Host only) and on the visitor's scheme (the
#     break-glass exemption is plaintext-only). A verdict earned with one must
#     not be replayed for the other from the same client.
hostcode() { curl -s -o /dev/null -w '%{http_code}' -m 5 -H "Host: $1" "${BASE}/thumbs/h.jpg"; }
[[ "$(hostcode 192.168.1.50)" == "200" ]] && pass "bare-IP Host: allowed by the app's rule" \
    || fail "bare-IP Host request was not allowed"
before="$(probes '')"
code="$(hostcode wsi.example.org)"; after="$(probes '')"
if [[ "$code" == "401" && $((after - before)) -eq 1 ]]; then
    pass "the same client with a hostname Host is checked on its own, and refused"
else
    fail "hostname Host after a bare-IP allow: HTTP ${code} with $((after - before)) new probe(s), expected 401 with 1 (the verdict was replayed across Hosts)"
fi
protocode() { curl -s -o /dev/null -w '%{http_code}' -m 5 -H 'Cf-Connecting-IP: 198.51.100.77' "$@" "${BASE}/thumbs/p.jpg"; }
[[ "$(protocode)" == "200" ]] && pass "plaintext listener: allowed by the app's rule" \
    || fail "plaintext request was not allowed"
before="$(probes '')"
code="$(protocode -H 'Cf-Visitor: {"scheme":"https"}')"; after="$(probes '')"
if [[ "$code" == "401" && $((after - before)) -eq 1 ]]; then
    pass "the same client reported as HTTPS by Cloudflare's edge is checked on its own, and refused"
else
    fail "HTTPS visitor after a plaintext allow: HTTP ${code} with $((after - before)) new probe(s), expected 401 with 1 (the verdict was replayed across schemes)"
fi

# 5. A cookie name that appears twice. The upstream (like the app, Starlette)
#    takes the LAST value; nginx's $cookie_wsi_session would be the FIRST, so a
#    key built from it files the allow earned by the valid second cookie under
#    the key a cookie-less client at the same address uses. The key is the
#    whole Cookie header instead, so the allow stays with that exact header.
code="$(curl -s -o /dev/null -w '%{http_code}' -m 5 -H 'Cookie: wsi_session=; wsi_session=good' "${BASE}/thumbs/dup.jpg")"
[[ "$code" == "200" ]] && pass "duplicate wsi_session cookies: the app's verdict (last one wins) is honoured" \
                       || fail "duplicate-cookie request returned $code, expected 200"
code="$(curl -s -o /dev/null -w '%{http_code}' -m 5 "${BASE}/thumbs/dup.jpg")"
[[ "$code" == "401" ]] && pass "...and a cookie-less client at the same address does not inherit it" \
                       || fail "cookie-less request after a duplicate-cookie allow returned $code, expected 401"
code="$(curl -s -o /dev/null -w '%{http_code}' -m 5 -H 'Cookie: wsi_session=; wsi_session=good' "${BASE}/thumbs/dup.jpg")"
[[ "$code" == "200" ]] && pass "...while the same duplicate header is served from its own verdict" \
                       || fail "repeat duplicate-cookie request returned $code, expected 200"

printf -- '--------------------------------------------------------------\n'
if [[ "$FAILED" -eq 0 ]]; then
    printf 'PASS - authz verdicts use their own zone, are per slide, and denials are not cached\n'
else
    exit 1
fi
