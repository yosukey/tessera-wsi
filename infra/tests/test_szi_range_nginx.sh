#!/usr/bin/env bash
# End-to-end guard for the SZI Range/caching contract in the REAL nginx
# snippet (infra/nginx/wsi-locations.conf).
#
# Run: bash infra/tests/test_szi_range_nginx.sh
#
# WHY THIS EXISTS
# ---------------
# A .szi archive is a single (up to multi-GB) ZIP the szi-tile-source frontend
# reads piecewise over HTTP Range. nginx does NOT forward the Range header to
# the upstream on a proxy_cache MISS — it fetches and stores the WHOLE object,
# then slices out the requested bytes. Under the parent `location ^~ /dzi/`
# (proxy_cache ON) that means every first open of a .szi pulls the entire
# archive through the app and writes it into the shared 50 GB tile cache. The
# snippet avoids this with a nested `location ~ \.szi$ { proxy_cache off; }`.
#
# This test boots nginx with the ACTUAL snippet `include`d (behind a tiny
# stdlib upstream) and asserts:
#   1. a .szi suffix-range reaches the upstream WITH the Range header and comes
#      back 206 partial (Range passed through);
#   2. the .szi does NOT get written into the nginx cache dir;
#   3. a normal (versioned, ?v=N) DZI tile still caches (HIT on the second
#      request) — no regression to the cached/shared tile path — and a
#      versioned .szi?v=N keeps the Range pass-through.
#
# It is self-contained: a pure-stdlib Python upstream (no FastAPI), a generated
# nginx.conf that stubs the http-context the snippet depends on and includes the
# real snippet unchanged. If nginx or python3 is unavailable the test SKIPs
# (exit 0) rather than failing, so it is safe in minimal CI images.

set -uo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
REPO="$(cd "${HERE}/../.." && pwd)"
SNIPPET="${REPO}/infra/nginx/wsi-locations.conf"
SITE_CONF="${REPO}/infra/nginx/wsi.conf"

skip() { printf 'SKIP - %s\n' "$1"; exit 0; }

command -v python3 >/dev/null 2>&1 || skip "python3 not available"
NGINX_BIN=""
for c in nginx /usr/sbin/nginx /usr/local/sbin/nginx; do
    command -v "$c" >/dev/null 2>&1 && { NGINX_BIN="$(command -v "$c")"; break; }
    [[ -x "$c" ]] && { NGINX_BIN="$c"; break; }
done
[[ -n "${NGINX_BIN}" ]] || skip "nginx not available"
[[ -f "${SNIPPET}" ]] || { echo "FAIL - snippet not found: ${SNIPPET}"; exit 1; }

# Work dir: override with SZI_TEST_WORK to keep artifacts for inspection;
# otherwise a fresh mktemp dir removed on exit. Ports overridable for CI.
WORK="${SZI_TEST_WORK:-$(mktemp -d)}"
UP_PORT="${SZI_TEST_UP_PORT:-18000}"
NG_PORT="${SZI_TEST_NG_PORT:-18080}"
KEEP_WORK="${SZI_TEST_KEEP_WORK:-0}"
UP_PID=""
cleanup() {
    [[ -n "${UP_PID}" ]] && kill "${UP_PID}" 2>/dev/null
    [[ -f "${WORK}/nginx.pid" ]] && "${NGINX_BIN}" -c "${WORK}/nginx.conf" -s quit 2>/dev/null
    [[ "${KEEP_WORK}" == "1" ]] || rm -rf "${WORK}"
}
trap cleanup EXIT
rm -rf "${WORK}"; mkdir -p "${WORK}"

mkdir -p "${WORK}"/{cache,body,proxy,fastcgi,uwsgi,scgi,szi}

# --- test fixture: a ZIP_STORED .szi (.dzi descriptor + one tile) ----------
python3 - "${WORK}/szi/demo.szi" <<'PY'
import os, sys, zipfile
out = sys.argv[1]
with zipfile.ZipFile(out, "w", compression=zipfile.ZIP_STORED) as zf:
    zf.writestr("image.dzi",
        '<?xml version="1.0"?><Image TileSize="256" Overlap="1" Format="jpeg">'
        '<Size Width="1024" Height="1024"/></Image>')
    # Pad so a suffix-range is meaningfully smaller than the whole archive.
    zf.writestr("image_files/10/0_0.jpeg", os.urandom(512 * 1024))
PY
SZI_SIZE=$(stat -c%s "${WORK}/szi/demo.szi")

# --- stdlib upstream: honours Range -> 206, records whether Range arrived ----
cat > "${WORK}/upstream.py" <<PY
import os, http.server
SZI_DIR = "${WORK}/szi"
LOG = "${WORK}/upstream.recv.log"
class H(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a): pass
    def do_GET(self):
        p = self.path.split("?", 1)[0]
        if p == "/api/edge-authz":
            self.send_response(204); self.end_headers(); return
        if p.endswith(".szi"):
            name = os.path.basename(p)
            path = os.path.join(SZI_DIR, name)
            if not os.path.exists(path):
                self.send_response(404); self.end_headers(); return
            data = open(path, "rb").read()
            rng = self.headers.get("Range")
            with open(LOG, "a") as f:
                f.write("szi range=%r\n" % rng)
            if rng and rng.startswith("bytes="):
                spec = rng.split("=", 1)[1]
                start_s, _, end_s = spec.partition("-")
                if start_s == "":              # suffix range: last N bytes
                    n = int(end_s); start = len(data) - n; end = len(data) - 1
                else:
                    start = int(start_s); end = int(end_s) if end_s else len(data) - 1
                body = data[start:end + 1]
                self.send_response(206)
                self.send_header("Content-Range", "bytes %d-%d/%d" % (start, end, len(data)))
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Accept-Ranges", "bytes")
                self.end_headers(); self.wfile.write(body); return
            self.send_response(200)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Accept-Ranges", "bytes")
            self.end_headers(); self.wfile.write(data); return
        if "_files/" in p and p.endswith(".jpeg"):
            body = b"\xff\xd8\xff\xd9"            # minimal JPEG marker bytes
            self.send_response(200)
            self.send_header("Content-Type", "image/jpeg")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers(); self.wfile.write(body); return
        self.send_response(404); self.end_headers()
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

    # One stub per limit_req_zone declared in wsi.conf. Every zone the snippet
    # names must be declared here or nginx rejects the whole config with
    # "zero size shared memory zone" and this test fails before it can assert
    # anything — and such a failure can go unnoticed because the test SKIPs
    # wherever nginx is absent, so keep this list in sync when adding a zone.
    # (No backticks anywhere in this heredoc -- it is unquoted, so bash would
    # try to run their contents as a command.)
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

    # http-context vars the snippet references (stubbed to inert values).
    map \$http_upgrade \$connection_upgrade { default upgrade; "" close; }
    set_real_ip_from 127.0.0.1;
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
    echo "FAIL - nginx rejected the config (snippet + stubs):"
    sed 's/^/    /' "${WORK}/nginx-test.log"
    exit 1
fi
"${NGINX_BIN}" -c "${WORK}/nginx.conf"

BASE="http://127.0.0.1:${NG_PORT}"
for _ in $(seq 1 50); do
    curl -fsS -o /dev/null "${BASE}/dzi/demo_files/10/0_0.jpeg" 2>/dev/null && break
    sleep 0.1
done

fails=0
check() {   # check LABEL CONDITION-DESCRIPTION  (0 arg => pass)
    if [[ "$2" == "ok" ]]; then printf 'ok   - %s\n' "$1"
    else printf 'FAIL - %s (%s)\n' "$1" "$2"; fails=$((fails + 1)); fi
}

# 1) suffix-range on .szi must come back 206 partial (not 200 full).
hdr="$(curl -s -D - -o "${WORK}/body.out" -r -65536 "${BASE}/dzi/demo.szi")"
code="$(printf '%s' "$hdr" | awk 'NR==1{print $2}')"
dl=$(stat -c%s "${WORK}/body.out")
[[ "${code}" == "206" ]] && check "szi suffix-range returns 206" ok \
    || check "szi suffix-range returns 206" "got HTTP ${code}"
[[ "${dl}" -lt "${SZI_SIZE}" ]] && check "client got partial body (< full archive)" ok \
    || check "client got partial body (< full archive)" "downloaded ${dl} of ${SZI_SIZE}"

# 2) the upstream must have SEEN the Range header (proof it was passed through).
if grep -q "range='bytes=-65536'" "${WORK}/upstream.recv.log" 2>/dev/null \
   || grep -q 'range="bytes=-65536"' "${WORK}/upstream.recv.log" 2>/dev/null; then
    check "upstream received the Range header" ok
else
    check "upstream received the Range header" "upstream saw: $(cat "${WORK}/upstream.recv.log" 2>/dev/null | tr '\n' ' ')"
fi

# 3) the .szi must NOT have been written into the shared tile cache.
#    (Only tiny tile responses may be present.) Assert the cache stays well
#    under the archive size.
cache_bytes=$(du -sb "${WORK}/cache" 2>/dev/null | awk '{print $1}')
[[ "${cache_bytes:-0}" -lt "${SZI_SIZE}" ]] && check "szi not written to nginx cache" ok \
    || check "szi not written to nginx cache" "cache holds ${cache_bytes} bytes (archive ${SZI_SIZE})"

# 3b) the viewer requests the archive with its generation, ".szi?v=N": the
#     query must not drag it back under caching or strip Range.
: > "${WORK}/upstream.recv.log"
code="$(curl -s -o /dev/null -w '%{http_code}' -r -1024 "${BASE}/dzi/demo.szi?v=3")"
if [[ "${code}" == "206" ]] && grep -q "bytes=-1024" "${WORK}/upstream.recv.log" 2>/dev/null; then
    check "versioned .szi?v= keeps Range pass-through (206)" ok
else
    check "versioned .szi?v= keeps Range pass-through (206)" "HTTP ${code}; upstream saw: $(tr '\n' ' ' < "${WORK}/upstream.recv.log" 2>/dev/null)"
fi

# 4) regression: a normal DZI tile still caches (2nd request is a HIT). Tile
#    URLs carry the slide's generation (?v=N, see the /dzi/ block); an
#    unversioned one is deliberately never cached.
curl -s -o /dev/null "${BASE}/dzi/demo_files/10/1_1.jpeg?v=1"
xc="$(curl -s -D - -o /dev/null "${BASE}/dzi/demo_files/10/1_1.jpeg?v=1" | awk 'tolower($1)=="x-cache-status:"{print $2}' | tr -d '\r')"
[[ "${xc}" == "HIT" ]] && check "normal tile still cached (HIT on 2nd request)" ok \
    || check "normal tile still cached (HIT on 2nd request)" "X-Cache-Status=${xc:-none}"

echo "--------------------------------------------------------------"
if [[ "${fails}" -eq 0 ]]; then
    echo "PASS - SZI Range pass-through + no-cache verified against the real snippet"
    exit 0
else
    echo "FAIL - ${fails} check(s) failed"
    exit 1
fi
