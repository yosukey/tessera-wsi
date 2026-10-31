#!/usr/bin/env bash
# What a certbot-migrated TLS setup really DOES when nginx serves it: the stock
# site file (infra/nginx/wsi.conf) with the snippet nginx_tls_carry.py writes
# from a certbot-patched site, on a real nginx with a self-signed certificate.
# Run: bash infra/tests/test_tls_serving_nginx.sh
#
# WHY THIS EXISTS
# ---------------
# The carry-over tests compare text and run `nginx -t`; neither notices a
# config that is valid and wrong. Certbot's `if ($host = X) { return 301
# https://... }` sits in a server block that listens on port 80 ALONE. Moved
# into the stock block, which also listens on 443 once the snippet adds it, the
# same `if` would answer every HTTPS request for X with a 301 to itself: valid
# config, and the site unreachable (curl -L: too many redirects). So this boots
# the assembled config and asks it:
#   - plain HTTP for the site's name is sent to HTTPS, once;
#   - HTTPS for the name is SERVED, and following redirects ends;
#   - another name on port 80 (the raw-IP break-glass route) is not redirected;
#   - a request that Cloudflare's edge says was HTTPS is not redirected either
#     (the tunnel talks plain HTTP to nginx, so a scheme-only test would loop).
#
# Self-contained: python3 stub upstream, openssl for the certificate. SKIPs
# (exit 0) without nginx, python3, openssl or curl.
set -uo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
REPO="$(cd "${HERE}/../.." && pwd)"
SITE_CONF="${REPO}/infra/nginx/wsi.conf"
LOCATIONS="${REPO}/infra/nginx/wsi-locations.conf"
CARRY="${REPO}/infra/lib/nginx_tls_carry.py"

skip() { printf 'SKIP - %s\n' "$1"; exit 0; }
fails=0
expect() {  # expect LABEL EXPECTED ACTUAL
    if [[ "$2" == "$3" ]]; then printf 'ok   - %s\n' "$1"
    else printf 'FAIL - %s: expected %q, got %q\n' "$1" "$2" "$3"; fails=$(( fails + 1 )); fi
}

for tool in python3 curl openssl; do command -v "$tool" >/dev/null 2>&1 || skip "$tool not available"; done
NGINX_BIN=""
for c in nginx /usr/sbin/nginx /usr/local/sbin/nginx; do
    command -v "$c" >/dev/null 2>&1 && { NGINX_BIN="$(command -v "$c")"; break; }
    [[ -x "$c" ]] && { NGINX_BIN="$c"; break; }
done
[[ -n "$NGINX_BIN" ]] || skip "nginx not available"

WORK="$(mktemp -d)"
UP_PORT="${TLS_TEST_UP_PORT:-18261}"
P80="${TLS_TEST_HTTP_PORT:-18262}"
P443="${TLS_TEST_HTTPS_PORT:-18263}"
UP_PID=""
cleanup() {
    [[ -n "$UP_PID" ]] && kill "$UP_PID" 2>/dev/null
    [[ -f "${WORK}/nginx.pid" ]] && "$NGINX_BIN" -c "${WORK}/nginx.conf" -s quit 2>/dev/null
    rm -rf "$WORK"
}
trap cleanup EXIT
mkdir -p "${WORK}"/{snippets,cache,body,proxy,fastcgi,uwsgi,scgi}

# A certificate for the site's name.
openssl req -x509 -newkey rsa:2048 -nodes -days 1 -subj "/CN=wsi.example.org" \
    -keyout "${WORK}/key.pem" -out "${WORK}/cert.pem" >/dev/null 2>&1 || skip "openssl could not make a certificate"

# The snippet, exactly as install.sh would write it from a certbot-patched site.
cat > "${WORK}/old-site" <<'SITE'
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
}
SITE
python3 -I "$CARRY" "${WORK}/old-site" > "${WORK}/carried.conf" || { echo "FAIL - nginx_tls_carry.py failed"; exit 1; }
# Only the places and ports are adapted to this scratch tree; the directives
# that decide the behaviour are the carried ones.
sed -E \
    -e "s#^listen \[::\]:443 .*##" \
    -e "s#^listen 443 ssl;.*#listen 127.0.0.1:${P443} ssl;#" \
    -e "s#^ssl_certificate .*#ssl_certificate ${WORK}/cert.pem;#" \
    -e "s#^ssl_certificate_key .*#ssl_certificate_key ${WORK}/key.pem;#" \
    -e "/^include \/etc\/letsencrypt\//d" \
    -e "/^ssl_dhparam /d" \
    "${WORK}/carried.conf" > "${WORK}/snippets/wsi-tls.conf"
grep -q 'wsi_tls_redirect' "${WORK}/snippets/wsi-tls.conf" || { echo "FAIL - the carried snippet has no redirect"; exit 1; }

echo 'access_log off;' > "${WORK}/snippets/wsi-access-log.conf"
cp "$LOCATIONS" "${WORK}/snippets/wsi-locations.conf"
# The stock site file, with its fixed places moved into the scratch tree.
sed -E \
    -e "s#listen 80 default_server;#listen 127.0.0.1:${P80} default_server;#" \
    -e "/listen \[::\]:80 default_server;/d" \
    -e "s#/etc/nginx/snippets/#${WORK}/snippets/#g" \
    -e "s#/var/cache/nginx/#${WORK}/cache/#g" \
    -e "s#server 127\.0\.0\.1:8000;#server 127.0.0.1:${UP_PORT};#" \
    -e "s#server 127\.0\.0\.1:8001;#server 127.0.0.1:${UP_PORT};#" \
    "$SITE_CONF" > "${WORK}/site.conf"

cat > "${WORK}/upstream.py" <<PY
import http.server
class H(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a): pass
    def do_GET(self):
        if self.path.startswith("/api/edge-authz"):
            self.send_response(204); self.send_header("Content-Length", "0"); self.end_headers(); return
        b = b"app"
        self.send_response(200); self.send_header("Content-Length", str(len(b))); self.end_headers(); self.wfile.write(b)
http.server.ThreadingHTTPServer(("127.0.0.1", ${UP_PORT}), H).serve_forever()
PY
setsid python3 "${WORK}/upstream.py" >/dev/null 2>&1 < /dev/null &
UP_PID=$!
disown "$UP_PID" 2>/dev/null || true

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
    include ${WORK}/site.conf;
}
CONF
if ! "$NGINX_BIN" -c "${WORK}/nginx.conf" -t 2>"${WORK}/t.log"; then
    echo "FAIL - nginx rejected the assembled config:"; sed 's/^/    /' "${WORK}/t.log"; exit 1
fi
"$NGINX_BIN" -c "${WORK}/nginx.conf" || { echo "FAIL - nginx failed to start"; exit 1; }
sleep 1

HTTP="http://127.0.0.1:${P80}"
NAME=wsi.example.org
status() { curl -s --noproxy '*' -o /dev/null -w '%{http_code}' -m 5 "$@"; }

expect "plain HTTP for the site's name is sent to HTTPS" \
    "301 https://${NAME}/x" \
    "$(curl -s --noproxy '*' -o /dev/null -w '%{http_code} %{redirect_url}' -m 5 -H "Host: ${NAME}" "${HTTP}/x")"
expect "HTTPS for the site's name is served, not redirected" "200" \
    "$(status -k --resolve "${NAME}:${P443}:127.0.0.1" "https://${NAME}:${P443}/")"
# (Following the redirect from the plain-HTTP URL goes round forever if the
# HTTPS leg answers it again, so that is what is asked next.)
expect "the HTTPS leg itself does not redirect again" "200" \
    "$(status -k --resolve "${NAME}:${P443}:127.0.0.1" -H "Host: ${NAME}" "https://${NAME}:${P443}/api/health")"
expect "another name on port 80 (the raw-IP break-glass route) is not redirected" "200" \
    "$(status -H 'Host: 192.168.1.10' "${HTTP}/")"
expect "port 80 with no Host header of the site's is not redirected" "200" "$(status "${HTTP}/")"
# Behind a Cloudflare tunnel nginx sees plain HTTP from cloudflared on loopback
# and the edge's scheme in Cf-Visitor: already HTTPS for the visitor, so a
# redirect would send it round again.
expect "a visitor Cloudflare's edge reports as HTTPS is not redirected" "200" \
    "$(status -H "Host: ${NAME}" -H 'Cf-Visitor: {"scheme":"https"}' -H 'Cf-Connecting-IP: 203.0.113.9' "${HTTP}/")"
expect "...one it reports as HTTP is" "301" \
    "$(status -H "Host: ${NAME}" -H 'Cf-Visitor: {"scheme":"http"}' -H 'Cf-Connecting-IP: 203.0.113.9' "${HTTP}/")"

# A snippet with the operator's own lines, merged with the carried ones, must be
# a config nginx accepts: the operator's `listen 0.0.0.0:443 ssl;` is the same
# listener as certbot's `listen 443 ssl;` ("a duplicate listen"), their ECDSA
# certificate pair is a second pair beside certbot's, and their HSTS header is
# the one that stays.
openssl req -x509 -newkey ec -pkeyopt ec_paramgen_curve:prime256v1 -nodes -days 1 -subj "/CN=wsi.example.org" \
    -keyout "${WORK}/ecdsa.key" -out "${WORK}/ecdsa.pem" >/dev/null 2>&1 || skip "openssl could not make an ECDSA certificate"
cat > "${WORK}/operator.conf" <<'OPS'
listen 0.0.0.0:443 ssl;
ssl_certificate /etc/ssl/ecdsa.pem;
ssl_certificate_key /etc/ssl/ecdsa.key;
add_header Strict-Transport-Security "max-age=63072000; includeSubDomains; preload" always;
OPS
python3 -I "$CARRY" --merge "${WORK}/operator.conf" < "${WORK}/carried.conf" > "${WORK}/merged.conf" \
    || { echo "FAIL - nginx_tls_carry.py --merge failed"; exit 1; }
mkdir -p "${WORK}/snippets2"
sed -E \
    -e "s#^listen \[::\]:443 .*##" \
    -e "s#^listen (0\.0\.0\.0:)?443 ssl;.*#listen 127.0.0.1:${P443} ssl;#" \
    -e "s#^ssl_certificate /etc/letsencrypt/.*#ssl_certificate ${WORK}/cert.pem;#" \
    -e "s#^ssl_certificate_key /etc/letsencrypt/.*#ssl_certificate_key ${WORK}/key.pem;#" \
    -e "s#^ssl_certificate /etc/ssl/ecdsa.pem;#ssl_certificate ${WORK}/ecdsa.pem;#" \
    -e "s#^ssl_certificate_key /etc/ssl/ecdsa.key;#ssl_certificate_key ${WORK}/ecdsa.key;#" \
    -e "/^include \/etc\/letsencrypt\//d" \
    "${WORK}/merged.conf" > "${WORK}/snippets2/wsi-tls.conf"
cp "${WORK}/snippets/wsi-access-log.conf" "${WORK}/snippets/wsi-locations.conf" "${WORK}/snippets2/"
sed "s#${WORK}/snippets/#${WORK}/snippets2/#g" "${WORK}/site.conf" > "${WORK}/site2.conf"
sed -e "s#${WORK}/site.conf#${WORK}/site2.conf#" -e "s#${WORK}/nginx.pid#${WORK}/nginx2.pid#" "${WORK}/nginx.conf" > "${WORK}/nginx2.conf"
if "$NGINX_BIN" -c "${WORK}/nginx2.conf" -t 2>"${WORK}/t2.log"; then merged_ok=ok; else merged_ok="$(tr '\n' ' ' < "${WORK}/t2.log")"; fi
expect "a snippet merged with the operator's own lines passes nginx -t (one listener on 443, no duplicate)" "ok" "$merged_ok"
expect "...with both certificate pairs, one listener, and the operator's HSTS only" "2|1|1" \
    "$(grep -c '^ssl_certificate ' "${WORK}/snippets2/wsi-tls.conf")|$(grep -c "^listen 127.0.0.1:${P443} ssl;" "${WORK}/snippets2/wsi-tls.conf")|$(grep -c '^add_header Strict-Transport-Security .*preload' "${WORK}/snippets2/wsi-tls.conf")"

printf -- '--------------------------------------------------------------\n'
if [[ "$fails" -ne 0 ]]; then
    echo "nginx error log:"; tail -n 15 "${WORK}/error.log" | sed 's/^/    /'
    echo "curl -v:"; curl -vk --noproxy '*' -m 5 --resolve "${NAME}:${P443}:127.0.0.1" "https://${NAME}:${P443}/" 2>&1 | tail -12 | sed 's/^/    /'
    echo "listening:"; (ss -ltn 2>/dev/null || netstat -ltn 2>/dev/null) | grep -E ":(${P80}|${P443}) " | sed 's/^/    /'
    echo "assembled TLS snippet:"; sed 's/^/    /' "${WORK}/snippets/wsi-tls.conf"
fi
if [[ "$fails" -eq 0 ]]; then
    printf 'PASS - a certbot-migrated TLS setup redirects HTTP to HTTPS and serves HTTPS without looping\n'
else
    exit 1
fi
