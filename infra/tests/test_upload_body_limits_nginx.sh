#!/usr/bin/env bash
# Guard: every endpoint that takes a large request body has its own location
# in the REAL nginx snippet (infra/nginx/wsi-locations.conf).
#
# Run: bash infra/tests/test_upload_body_limits_nginx.sh
#
# WHY THIS EXISTS
# ---------------
# wsi.conf sets a tight server-level `client_max_body_size 2m` (and 60s
# timeouts). An upload endpoint without a dedicated location falls through to
# the generic `location /api/admin/` and inherits that cap, so every realistic
# upload 413s at the edge — invisibly to the app's own tests, which never go
# through nginx. Likewise an upload whose app cap is 2 MiB (logo /
# header-background) 413s on a file just under the limit once multipart
# framing pushes the body past 2m, unless its location allows more. This test:
#
#   1. posts a body over the 2m cap to each large-upload route and requires it
#      to reach the upstream, with a generic admin route as the control that
#      must 413;
#   2. posts a real multipart upload of a file exactly at the APP's cap to each
#      capped upload route (tour assets, logo, favicon, header/seasonal
#      backgrounds) and requires it to reach the upstream, then checks the
#      edge cap on those routes is still bounded;
#   3. cross-checks the app source: every FastAPI route taking an UploadFile
#      must appear in the ROUTES table below, and each capped route's size is
#      read from the app constant that enforces it — so a new upload route, or
#      a raised app cap, can't silently drift past nginx.
#
# Self-contained like the other nginx tests here: SKIPs (exit 0) when nginx,
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
ROUTERS="${REPO}/services/app/wsi_app/routers"
APP_SRC="${REPO}/services/app/wsi_app"
[[ -d "${ROUTERS}" ]] || { echo "FAIL - routers not found: ${ROUTERS}"; exit 1; }

# Every app route that takes an UploadFile, as
#   "<METHOD> <route template>|<concrete path to probe>|<size>"
# where <size> is either
#   large            — app-capped / streamed; must accept a 4 MiB raw body, or
#   <file>:<CONST>   — module-level byte-cap constant in wsi_app/<file>, or
#   <file>:env_int:<ENV> — the default of `env_int("<ENV>", ...)` there.
# A capped route must accept a multipart upload of a file exactly that size.
# Add a row here (and a matching nginx location if the cap is near/over 2m)
# whenever a new upload route lands; section 3 fails until you do.
ROUTES=(
    "POST /api/admin/slides/upload|/api/admin/slides/upload|large"
    "POST /api/admin/slides/{slide_id}/wsi|/api/admin/slides/42/wsi|large"
    "POST /api/admin/restore|/api/admin/restore|large"
    "POST /api/admin/slides/import|/api/admin/slides/import|large"
    "POST /api/admin/tours/{tour_id}/assets/upload|/api/admin/tours/7/assets/upload|config.py:env_int:TOUR_ASSET_MAX_BYTES"
    "POST /api/admin/viewer/settings/logo|/api/admin/viewer/settings/logo|routers/admin_settings.py:_LOGO_MAX_BYTES"
    "POST /api/admin/viewer/settings/favicon|/api/admin/viewer/settings/favicon|routers/admin_settings.py:_FAVICON_MAX_BYTES"
    "POST /api/admin/viewer/settings/header-default-background|/api/admin/viewer/settings/header-default-background|routers/admin_settings.py:_HEADER_DEFAULT_MAX_BYTES"
    "POST /api/admin/viewer/seasonal-backgrounds/{slot}/image|/api/admin/viewer/seasonal-backgrounds/3/image|routers/admin_settings.py:_SEASONAL_BG_MAX_BYTES"
)

FAILED=0
WORK="$(mktemp -d)"
UP_PORT="${UPLOAD_TEST_UP_PORT:-18301}"
NG_PORT="${UPLOAD_TEST_NG_PORT:-18302}"
UP_PID=""
cleanup() {
    [[ -n "${UP_PID}" ]] && kill "${UP_PID}" 2>/dev/null
    [[ -f "${WORK}/nginx.pid" ]] && "${NGINX_BIN}" -c "${WORK}/nginx.conf" -s quit 2>/dev/null
    rm -rf "${WORK}"
}
trap cleanup EXIT
mkdir -p "${WORK}"/{body,proxy,fastcgi,uwsgi,scgi,cache}

# --- upstream: drain the body, report how much arrived -----------------------
cat > "${WORK}/upstream.py" <<PY
import http.server
class H(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    def log_message(self, *a): pass
    def do_POST(self):
        n = 0
        if self.headers.get("Transfer-Encoding", "").lower() == "chunked":
            while True:
                size = int(self.rfile.readline().strip() or b"0", 16)
                if size == 0:
                    self.rfile.readline(); break
                n += len(self.rfile.read(size)); self.rfile.readline()
        else:
            left = int(self.headers.get("Content-Length") or 0)
            while left:
                chunk = self.rfile.read(min(left, 1 << 16))
                if not chunk: break
                n += len(chunk); left -= len(chunk)
        body = str(n).encode()
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers(); self.wfile.write(body)
    do_PATCH = do_POST
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
# The server block mirrors wsi.conf's tight defaults (2m body cap), which is
# exactly what an upload route without its own location would inherit.
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
    limit_req_zone \$binary_remote_addr zone=wsi_api:1m         rate=6000r/m;
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
        client_max_body_size 2m;
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

# --- 1. large-upload routes: a 4 MiB raw body must reach the upstream -------
# 4 MiB: over the 2m server default, under every dedicated location's cap.
head -c $((4 * 1024 * 1024)) /dev/zero > "${WORK}/big.bin"
post() {
    curl -s -o /dev/null -w '%{http_code}' -m 30 \
        -H 'Content-Type: application/octet-stream' \
        --data-binary "@${WORK}/big.bin" "http://127.0.0.1:${NG_PORT}$1"
}
# Multipart upload shaped like the admin UI's FormData (file part + a small
# form field), so the multipart framing overhead is part of what is measured.
post_file() {
    curl -s -o /dev/null -w '%{http_code}' -m 30 \
        -F "duplicate_policy=rename" -F "file=@$2;type=application/octet-stream" \
        "http://127.0.0.1:${NG_PORT}$1"
}

# Resolve a "<file>:<CONST>" / "<file>:env_int:<ENV>" size spec to bytes by
# evaluating the constant's (pure arithmetic) expression from the app source.
resolve_cap() {
    python3 - "${APP_SRC}" "$1" <<'PY'
import ast, sys
src, spec = sys.argv[1], sys.argv[2]
parts = spec.split(":")
tree = ast.parse(open(f"{src}/{parts[0]}").read())
def ev(node):
    return int(eval(compile(ast.Expression(node), "<cap>", "eval"), {"__builtins__": {}, "int": int}))
if len(parts) == 3 and parts[1] == "env_int":
    for n in ast.walk(tree):
        if (isinstance(n, ast.Call) and getattr(n.func, "id", None) == "env_int"
                and n.args and isinstance(n.args[0], ast.Constant) and n.args[0].value == parts[2]):
            print(ev(n.args[1])); sys.exit(0)
else:
    for n in tree.body:
        if (isinstance(n, ast.Assign) and len(n.targets) == 1
                and getattr(n.targets[0], "id", None) == parts[1]):
            print(ev(n.value)); sys.exit(0)
sys.exit(f"cap {spec} not found")
PY
}

for row in "${ROUTES[@]}"; do
    IFS='|' read -r _route p size <<<"$row"
    [[ "$size" == "large" ]] || continue
    c="$(post "$p")"
    if [[ "$c" == "200" ]]; then
        printf 'ok   - %-52s accepts a 4 MiB body\n' "$p"
    else
        fail "$p answered $c to a 4 MiB body — missing a dedicated location?"
    fi
done

# Control: a generic admin route keeps the tight default.
c="$(post /api/admin/viewer/settings)"
if [[ "$c" == "413" ]]; then
    printf 'ok   - %-52s keeps the 2m default (413)\n' /api/admin/viewer/settings
else
    fail "/api/admin/viewer/settings answered $c, expected 413 (control)"
fi

# --- 1b. step writes: a step at its annotation budget must reach the app -----
# A step holding schemas._MAX_STEP_FREEHAND_POINTS vertices at full float
# precision (plus its pins, shapes and description) saves as ~3.5 MiB at most;
# the step-write location must pass that and still bound the body.
head -c $((3 * 1024 * 1024 + 512 * 1024)) /dev/zero > "${WORK}/step.bin"
head -c $((5 * 1024 * 1024)) /dev/zero > "${WORK}/step-over.bin"
send() {
    curl -s -o /dev/null -w '%{http_code}' -m 30 -X "$1" \
        -H 'Content-Type: application/json' \
        --data-binary "@$3" "http://127.0.0.1:${NG_PORT}$2"
}
for probe in "POST /api/admin/tours/7/steps" "PATCH /api/admin/tours/7/steps/9"; do
    read -r m p <<<"$probe"
    c="$(send "$m" "$p" "${WORK}/step.bin")"
    if [[ "$c" == "200" ]]; then
        printf 'ok   - %-52s accepts a 3.5 MiB step body\n' "$m $p"
    else
        fail "$m $p answered $c to a 3.5 MiB step body — step-write location missing or too tight?"
    fi
    c="$(send "$m" "$p" "${WORK}/step-over.bin")"
    if [[ "$c" == "413" ]]; then
        printf 'ok   - %-52s still 413s a 5 MiB body\n' "$m $p"
    else
        fail "$m $p answered $c to a 5 MiB body, expected 413 (edge cap should stay bounded)"
    fi
done
# The one-item routes beside them keep the 2m default.
for probe in "PATCH /api/admin/tours/7/steps/reorder" "POST /api/admin/tours/7/steps/9/shapes"; do
    read -r m p <<<"$probe"
    c="$(send "$m" "$p" "${WORK}/step.bin")"
    if [[ "$c" == "413" ]]; then
        printf 'ok   - %-52s keeps the 2m default (413)\n' "$m $p"
    else
        fail "$m $p answered $c, expected 413 (only bare step paths get the larger cap)"
    fi
done

# --- 2. capped upload routes: a file at the app's cap must reach the app -----
for row in "${ROUTES[@]}"; do
    IFS='|' read -r _route p size <<<"$row"
    [[ "$size" == "large" ]] && continue
    if ! cap="$(resolve_cap "$size" 2>"${WORK}/cap.err")"; then
        fail "$p: could not resolve app cap ${size}: $(cat "${WORK}/cap.err")"
        continue
    fi
    head -c "$cap" /dev/zero > "${WORK}/at-cap.bin"
    c="$(post_file "$p" "${WORK}/at-cap.bin")"
    if [[ "$c" == "200" ]]; then
        printf 'ok   - %-52s accepts a %s-byte file (app cap)\n' "$p" "$cap"
    else
        fail "$p answered $c to a multipart file at the app cap (${cap} bytes) — nginx location too tight?"
    fi
    # The headroom must stay headroom: 4 MiB is over every capped route's
    # edge limit, so an accidental `client_max_body_size 0` shows up here.
    c="$(post "$p")"
    if [[ "$c" == "413" ]]; then
        printf 'ok   - %-52s still 413s a 4 MiB body\n' "$p"
    else
        fail "$p answered $c to a 4 MiB body, expected 413 (edge cap should stay bounded)"
    fi
done

# --- 3. source cross-check: every UploadFile route is in ROUTES --------------
python3 - "${ROUTERS}" > "${WORK}/upload-routes.txt" <<'PY'
import ast, pathlib, sys
for f in sorted(pathlib.Path(sys.argv[1]).glob("*.py")):
    for fn in ast.walk(ast.parse(f.read_text())):
        if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        a = fn.args
        params = a.posonlyargs + a.args + a.kwonlyargs
        if not any(p.annotation is not None and "UploadFile" in ast.unparse(p.annotation)
                   for p in params):
            continue
        for d in fn.decorator_list:
            if (isinstance(d, ast.Call) and isinstance(d.func, ast.Attribute)
                    and d.func.attr in {"post", "put", "patch"}
                    and d.args and isinstance(d.args[0], ast.Constant)):
                print(f"{d.func.attr.upper()} {d.args[0].value}")
PY
if [[ ! -s "${WORK}/upload-routes.txt" ]]; then
    fail "found no UploadFile routes under ${ROUTERS} — extractor broken?"
fi
declare -A KNOWN=()
for row in "${ROUTES[@]}"; do KNOWN["${row%%|*}"]=1; done
while IFS= read -r r; do
    if [[ -n "${KNOWN[$r]:-}" ]]; then
        printf 'ok   - %-52s has a body-limit row\n' "$r"
    else
        fail "$r takes an UploadFile but is not in ROUTES — add it (and an nginx location if its cap nears 2m)"
    fi
    unset "KNOWN[$r]"
done < "${WORK}/upload-routes.txt"
for r in "${!KNOWN[@]}"; do
    fail "ROUTES lists '$r' but no router defines it with an UploadFile — stale row?"
done

printf -- '--------------------------------------------------------------\n'
if [[ "$FAILED" -eq 0 ]]; then
    printf 'PASS - every upload route clears the edge body limit\n'
else
    exit 1
fi
