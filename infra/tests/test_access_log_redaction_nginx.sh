#!/usr/bin/env bash
# Guard: the download tokens (`?t=`) of the admin download routes never reach
# the nginx access log written by the REAL wsi.conf.
#
# Run: bash infra/tests/test_access_log_redaction_nginx.sh
#
# WHY THIS EXISTS
# ---------------
# GET /api/admin/slides/{id}/download, /api/admin/slides/export/{id}/download
# and /api/admin/backup/{id}/download accept a signed token in the query
# string, because the SPA starts a download as a plain link. A token is good
# for an hour and can hand out a whole-install backup, and the stock
# "combined" access log records the full request line in
# /var/log/nginx/access.log. wsi.conf therefore logs with its own format,
# wsi_combined, whose $wsi_log_request masks the token. This test takes that
# map and log_format from wsi.conf verbatim and requires that:
#
#   1. wsi.conf's server block logs with wsi_combined — through
#      /etc/nginx/snippets/wsi-access-log.conf, which it includes and whose
#      shipped default (infra/nginx/wsi-access-log.conf, installed only when
#      missing so an operator's own logging survives updates) is that line;
#   2. no token from any of the three routes lands in the log, and the line
#      says "t=[redacted]" where it was — also when the key is spelled %74,
#      which the app decodes to t (a mask keyed on a literal "t=" alone
#      would log that spelling in clear);
#   3. every other line — including a `t=` on a route that is not a download,
#      and a download without a query — is byte-for-byte the line "combined"
#      would have written (checked against a second log in that format).
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
UP_PORT="${LOG_REDACT_TEST_UP_PORT:-18251}"
NG_PORT="${LOG_REDACT_TEST_NG_PORT:-18252}"
UP_PID=""
cleanup() {
    [[ -n "${UP_PID}" ]] && kill "${UP_PID}" 2>/dev/null
    [[ -f "${WORK}/nginx.pid" ]] && "${NGINX_BIN}" -c "${WORK}/nginx.conf" -s quit 2>/dev/null
    rm -rf "${WORK}"
}
trap cleanup EXIT
mkdir -p "${WORK}"/{cache,authz,body,proxy,fastcgi,uwsgi,scgi}

# --- 1. what wsi.conf ships ---------------------------------------------------
real_map() { awk -v v="$1" '$1 == "map" && $3 == v { c = 1 } c { print } c && /^}/ { exit }' "${SITE_CONF}"; }
LOG_MAP="$(real_map '$wsi_log_request')"
LOG_FORMAT="$(awk '$1 == "log_format" && $2 == "wsi_combined" { c = 1 } c { print } c && /;[[:space:]]*$/ { exit }' "${SITE_CONF}")"
if [[ -z "${LOG_MAP}" || -z "${LOG_FORMAT}" ]]; then
    echo "FAIL - no \$wsi_log_request map / wsi_combined log_format in ${SITE_CONF}"
    exit 1
fi
# The server block includes the access-log snippet (and sets no access_log of
# its own that would sit beside it), and the shipped snippet's only live line
# uses the format.
if awk '/^server[[:space:]]*\{/ { s = 1 } s && ($1 == "include" || $1 == "access_log") { print }' "${SITE_CONF}" \
        | grep -vF 'wsi-locations.conf' \
        | grep -qxE '[[:space:]]*include[[:space:]]+/etc/nginx/snippets/wsi-access-log\.conf;'; then
    pass "wsi.conf's server block takes its access log from snippets/wsi-access-log.conf, only"
else
    fail "wsi.conf's server block does not log through exactly 'include /etc/nginx/snippets/wsi-access-log.conf;'"
fi
if [[ "$(grep -vE '^[[:space:]]*(#|$)' "${REPO}/infra/nginx/wsi-access-log.conf")" \
        == 'access_log /var/log/nginx/access.log wsi_combined;' ]]; then
    pass "the shipped wsi-access-log.conf logs to the access log with wsi_combined"
else
    fail "infra/nginx/wsi-access-log.conf is not just 'access_log /var/log/nginx/access.log wsi_combined;'"
fi

# --- upstream: 200 for anything ----------------------------------------------
cat > "${WORK}/upstream.py" <<PY
import http.server
class H(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a): pass
    def do_GET(self):
        self.send_response(200 if self.path != "/api/edge-authz" else 204)
        self.send_header("Content-Length", "0"); self.end_headers()
http.server.ThreadingHTTPServer(("127.0.0.1", ${UP_PORT}), H).serve_forever()
PY
setsid python3 "${WORK}/upstream.py" >/dev/null 2>&1 < /dev/null &
UP_PID=$!
disown "${UP_PID}" 2>/dev/null || true

DZI_MAPS="$(real_map '$wsi_authz_object')
$(real_map '$wsi_dzi_unversioned')
$(real_map '$wsi_dzi_tile_cache_control')"
ZONE_STUBS="$(grep -oE 'zone=[a-z0-9_]+:' "${SITE_CONF}" | grep -v '_cache:' | sort -u \
    | sed -E 's/^zone=([a-z0-9_]+):$/    limit_req_zone $binary_remote_addr zone=\1:1m rate=6000r\/m;/')"

cat > "${WORK}/nginx.conf" <<CONF
worker_processes 1;
user root;
pid ${WORK}/nginx.pid;
error_log ${WORK}/error.log warn;
events { worker_connections 128; }
http {
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

    # From wsi.conf, verbatim.
${LOG_MAP}
${LOG_FORMAT}

    server {
        listen 127.0.0.1:${NG_PORT};
        server_name _;
        # wsi.conf's line, pointed at the scratch dir; plus the stock format
        # alongside, for comparison.
        access_log ${WORK}/access.log wsi_combined;
        access_log ${WORK}/combined.log combined;
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

get() { curl -s -o /dev/null -m 5 -A "probe/1.0" -e "http://ref.example/admin" "http://127.0.0.1:${NG_PORT}$1"; }

# --- 2. the three download routes, token in every position -------------------
get "/api/admin/backup/b-2026_10_11/download?t=SECRETbackup.eyJ.sig"
get "/api/admin/slides/42/download?t=SECRETslide.eyJ.sig"
get "/api/admin/slides/export/exp-1/download?x=1&t=SECRETexport.eyJ.sig&y=2"
# The key percent-encoded: the app decodes %74 to t, so this authenticates.
get "/api/admin/backup/b-2/download?%74=SECRETpct.eyJ.sig"
get "/api/admin/slides/43/download?x=1&%74=SECRETpct2.eyJ.sig"
# --- 3. lines that must come out exactly as "combined" writes them -----------
get "/api/admin/slides/42/download"
get "/api/slides?t=not-a-download-token"
get "/api/admin/backup/b-1/download_url?t=abc"
sleep 0.5   # let nginx flush the log lines

n="$(wc -l < "${WORK}/access.log")"
if [[ "$n" -eq 8 ]]; then
    pass "eight requests, eight log lines"
else
    fail "expected 8 lines in the wsi_combined log, got ${n}"
fi
if grep -q 'SECRET' "${WORK}/access.log"; then
    fail "a download token reached the access log: $(grep -o '"GET [^"]*SECRET[^"]*"' "${WORK}/access.log" | head -1)"
else
    pass "no download token in the access log"
fi
for want in \
    '"GET /api/admin/backup/b-2026_10_11/download?t=[redacted] HTTP/1.1"' \
    '"GET /api/admin/slides/42/download?t=[redacted] HTTP/1.1"' \
    '"GET /api/admin/slides/export/exp-1/download?t=[redacted] HTTP/1.1"' \
    '"GET /api/admin/backup/b-2/download?t=[redacted] HTTP/1.1"' \
    '"GET /api/admin/slides/43/download?t=[redacted] HTTP/1.1"'; do
    if grep -qF -- "$want" "${WORK}/access.log"; then
        pass "logged as ${want}"
    else
        fail "no line with ${want}"
    fi
done
# Same line, line for line, as "combined" — once the masked request lines are
# put back. Anything else that differs is a layout change.
python3 - "${WORK}/combined.log" "${WORK}/access.log" <<'PY'
import re, sys
stock = open(sys.argv[1]).read().splitlines()
ours = open(sys.argv[2]).read().splitlines()
bad = 0
for a, b in zip(stock, ours):
    masked = re.sub(r'(download\?)(?:\S*&)?(?:t|%74)=\S*( HTTP/)', r'\1t=[redacted]\2', a) if "/download?" in a else a
    if masked != b:
        bad += 1
        print(f"FAIL - layout differs from combined:\n    combined:     {a}\n    wsi_combined: {b}")
if len(stock) != len(ours):
    bad += 1
    print(f"FAIL - {len(stock)} combined lines vs {len(ours)} wsi_combined lines")
if not bad:
    print("ok   - every line matches the stock combined format (unmasked ones byte for byte)")
sys.exit(1 if bad else 0)
PY
[[ $? -eq 0 ]] || FAILED=1

printf -- '--------------------------------------------------------------\n'
if [[ "$FAILED" -eq 0 ]]; then
    printf 'PASS - download tokens are masked in the access log; everything else is logged unchanged\n'
else
    exit 1
fi
