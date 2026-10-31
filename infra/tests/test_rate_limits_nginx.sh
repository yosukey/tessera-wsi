#!/usr/bin/env bash
# Guard for the rate-limit contract in the REAL nginx snippet
# (infra/nginx/wsi-locations.conf).
#
# Run: bash infra/tests/test_rate_limits_nginx.sh
#
# WHY THIS EXISTS
# ---------------
# Two properties of the snippet are easy to break silently, because nothing
# about them shows up in `nginx -t`:
#
#   1. Every proxied /api/ location is metered. The live-sync REST locations
#      ("^~ /api/sync/" and "^~ /api/admin/sync/") are the easy ones to miss:
#      their "^~" prefix beats "^~ /api/", so omitting limit_req there means
#      they inherit *nothing* rather than the general API limit. They land on
#      the single-worker wsi_sync upstream that holds every live-sync room in
#      memory, so unmetered is the worst place for it.
#   2. A throttled request answers 429, not nginx's default 503. Uptime checks
#      and the SPA read 503 as a backend outage rather than back-pressure, so
#      a missing `limit_req_status` turns throttling into a false alarm.
#   3. The live-sync WebSocket upgrades have a zone of their own, sized for a
#      class behind ONE NAT address: 200 followers plus 200 index-page
#      discovery sockets reconnecting at once must get through, and must not
#      share a budget with the rest of /api/ (wsi_api, 120 r/min with a burst
#      of 10, is tripped by a single class reconnecting).
#      The slide detail read (GET /api/slides/<public_id>) likewise: every
#      follower fetches it at the same instant when the presenter switches
#      slides (wsi_slide_detail), while the rest of /api/slides/ stays in
#      wsi_api — and the session peek (GET /api/sync/sessions/<sid>), which
#      every follower sends as the class opens the shared link together
#      (wsi_sync_peek), while the rest of /api/sync/ stays in wsi_api.
#   4. /dzi/ requests without a ?v= reach the app every time (the shared cache
#      never stores them), so they are metered per client (wsi_dzi_uncached) —
#      delayed to 10 r/s past a burst, 429 past the queue — while versioned
#      requests, everything the current viewer sends, are not counted at all.
#   5. An IPv6 client can take a fresh source address from its /64 for every
#      request, so the two login endpoints also cap each /64 ($wsi_rl_net64,
#      the real map), generously enough for a class with an address each;
#      IPv4 is keyed per address only.
#
# The test boots nginx with the ACTUAL snippet `include`d (behind a tiny
# stdlib upstream) and the ACTUAL limit_req_zone lines from wsi.conf, asserts
# the proxying still works, then floods each location past its burst and
# checks the rejection code.
#
# Self-contained: a pure-stdlib Python upstream and a generated nginx.conf
# that stubs the http-context the snippet depends on. If nginx or python3 is
# unavailable the test SKIPs (exit 0) rather than failing, so it is safe in
# minimal CI images.
#
# NOTE: the generated nginx.conf below is an UNQUOTED heredoc so ${WORK} and
# friends expand. Do not put backticks or $(...) in it — bash would run them.

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
    command -v "$c" >/dev/null 2>&1 && { NGINX_BIN="$c"; break; }
done
[ -n "$NGINX_BIN" ] || skip "nginx not available"
[ -f "$SNIPPET" ] || skip "snippet not found at $SNIPPET"

FAILED=0
UP_PORT=18101
NG_PORT=18102
WORK="$(mktemp -d)"

cleanup() {
    "${NGINX_BIN}" -c "${WORK}/nginx.conf" -s quit >/dev/null 2>&1
    [ -n "${UP_PID:-}" ] && kill "${UP_PID}" >/dev/null 2>&1
    rm -rf "${WORK}"
}
trap cleanup EXIT

# --- upstream: answers every GET with a tiny JSON body ----------------------
cat > "${WORK}/upstream.py" <<PY
import http.server


class H(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        body = b'{}'
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass


http.server.ThreadingHTTPServer(("127.0.0.1", ${UP_PORT}), H).serve_forever()
PY

setsid python3 "${WORK}/upstream.py" >/dev/null 2>&1 < /dev/null &
UP_PID=$!
disown "${UP_PID}" 2>/dev/null || true
sleep 1

# The /dzi/ maps the snippet needs, verbatim from wsi.conf ($wsi_authz_object
# puts the slide into the per-slide authz verdict's key, $wsi_dzi_unversioned
# keeps tile requests without a ?v= out of the cache, $wsi_dzi_tile_cache_control
# sets the tiles' browser caching, $wsi_dzi_uncached_key keys the zone that
# meters those unversioned requests) — the real expressions,
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
# How the zones key a client, verbatim from wsi.conf: the loopback-trust geo,
# the trusted Cf-Connecting-IP (this test's client is 127.0.0.1, so it may set
# one, as cloudflared does), $wsi_rl_key and its IPv6 /64, $wsi_rl_net64.
real_geo() { awk -v v="$1" '$1 == "geo" && $2 == v { c = 1 } c { print } c && /^}/ { exit }' "${SITE_CONF}"; }
KEY_MAPS="$(real_geo '$cf_hop_trusted')
$(real_map '$wsi_cf_connecting_ip')
$(real_map '$wsi_cf_access_jwt')
$(real_map '$wsi_rl_key')
$(real_map '$wsi_rl_net64')"
for v in cf_hop_trusted wsi_cf_connecting_ip wsi_cf_access_jwt wsi_rl_key wsi_rl_net64; do
    if ! grep -qE "[\$]${v} [{]" <<<"${KEY_MAPS}"; then
        echo "FAIL - could not extract \$${v} from ${SITE_CONF}"
        exit 1
    fi
done
# The real rate-limit zones, so the rates under test are the shipped ones.
ZONES="$(grep -E '^limit_req_zone ' "${SITE_CONF}")"
if ! grep -q 'zone=wsi_sync_ws:' <<<"${ZONES}" || ! grep -q 'zone=wsi_api:' <<<"${ZONES}"; then
    echo "FAIL - could not extract the limit_req_zone lines from ${SITE_CONF}"
    exit 1
fi

# --- nginx.conf: stub the http-context the snippet needs, then include it ----
cat > "${WORK}/nginx.conf" <<CONF
worker_processes 1;
user root;
pid ${WORK}/nginx.pid;
error_log ${WORK}/error.log crit;
events { worker_connections 512; }
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

    # Every limit_req_zone of wsi.conf, verbatim.
${ZONES}

    # http-context vars the snippet references (stubbed to inert values).
    map \$http_upgrade \$connection_upgrade { default upgrade; "" close; }
    set_real_ip_from 127.0.0.1;
    map \$scheme                 \$wsi_xfp                { default \$scheme; }
${KEY_MAPS}
    map \$http_authorization \$wsi_auth_nocache { default 1; "" ""; "~*^Basic\s" ""; }
    map \$http_authorization \$wsi_authz_nocache { default 1; "" ""; }
${DZI_MAPS}

    server {
        listen 127.0.0.1:${NG_PORT};
        server_name _;
        include ${SNIPPET};
        # Shows how a request is keyed (test-only).
        location = /_rlkey { return 200 "\$wsi_rl_key|\$wsi_rl_net64"; }
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

code() { curl -s -o /dev/null -w '%{http_code}' -m 5 "http://127.0.0.1:${NG_PORT}$1"; }

# Flood a path and report the first status that is not in the allowed-OK set.
# Returns "none" when the path never rejected within the attempt budget.
first_rejection() {
    local path="$1" attempts="$2" ok_codes="$3" c
    for _ in $(seq 1 "$attempts"); do
        c="$(code "$path")"
        case " $ok_codes " in
            *" $c "*) ;;
            *) printf '%s' "$c"; return ;;
        esac
    done
    printf 'none'
}

# 0. The slide list, GET /api/slides (no trailing slash), is proxied as is.
#    A `^~ /api/slides/` prefix location with proxy_pass makes nginx answer
#    its slash-less twin with a 301 onto `http://<host>/api/slides/` — the
#    wrong scheme behind TLS termination, which the page's CSP then blocks,
#    so the index page shows its catalog-load error. Checked before the
#    floods below drain wsi_api.
c="$(code '/api/slides?page=1&per_page=2000&sort=catalog_id')"
if [ "$c" = "200" ]; then
    printf 'ok   - GET /api/slides is proxied, not redirected to /api/slides/\n'
else
    fail "GET /api/slides answered $c, expected 200 (a 301 means the slash redirect is back)"
fi

# 1. The added limit_req must not have broken proxying. The probes are the
#    live-sync peek (GET /api/sync/sessions/{sid}), the REST route this
#    location serves, and another path under it; the stub upstream answers
#    200 for any path.
for p in /api/sync/sessions/room1 /api/sync/other; do
    c="$(code "$p")"
    if [ "$c" = "200" ]; then
        printf 'ok   - %s still proxies to the sync upstream (200)\n' "$p"
    else
        fail "$p returned $c, expected 200"
    fi
done

# 2. Past the burst it must throttle, and with 429 rather than 503.
#    burst=40 on a 120r/min zone, so 80 attempts is comfortably past it.
c="$(first_rejection /api/sync/other 80 '200')"
if [ "$c" = "429" ]; then
    printf 'ok   - /api/sync/ is metered and throttles with 429\n'
elif [ "$c" = "none" ]; then
    fail "/api/sync/ never throttled in 80 requests — is limit_req missing?"
else
    fail "/api/sync/ threw $c when throttling, expected 429"
fi

# 2b. The session peek has a zone of its own: a class behind one address
#     opening the shared link together gets through although wsi_api (just
#     drained from this address) is exhausted ...
c="$(first_rejection /api/sync/sessions/AbC_-09xyz 200 '200')"
if [ "$c" = "none" ]; then
    printf 'ok   - 200 session peeks from one address in a row are all admitted\n'
else
    fail "session peeks: request rejected with $c within 200 (burst too small for a NAT'd class?)"
fi
# ... while a flood past the burst still meets a ceiling, answered with 429.
c="$(first_rejection /api/sync/sessions/AbC_-09xyz 300 '200')"
if [ "$c" = "429" ]; then
    printf 'ok   - the session-peek zone still throttles a flood, with 429\n'
elif [ "$c" = "none" ]; then
    fail "session peeks never throttled in 500 requests — is limit_req missing?"
else
    fail "session peeks threw $c when throttling, expected 429"
fi

# 3. The live-sync WebSocket upgrades are not in the (now exhausted) wsi_api
#    zone: test 2 just drained it from this same address.
for p in /api/sync/ws/room1 /api/sync/discovery; do
    c="$(code "$p")"
    if [ "$c" = "200" ]; then
        printf 'ok   - %s is not throttled by an exhausted wsi_api zone\n' "$p"
    else
        fail "$p returned $c right after wsi_api was exhausted (shares its zone?)"
    fi
done

# 4. A class behind one address reconnecting at once: 200 followers + 200
#    discovery sockets, back to back, all get through ...
c="$(first_rejection /api/sync/ws/room1 200 '200')"
[ "$c" = "none" ] || fail "follower upgrades: request rejected with $c within 200"
c="$(first_rejection /api/sync/discovery 198 '200')"
if [ "$c" = "none" ]; then
    printf 'ok   - 400 sync upgrades from one address in a row are all admitted\n'
else
    fail "discovery upgrades: request rejected with $c (burst too small for a NAT'd class)"
fi
# ... while a flood past that still meets a ceiling, answered with 429.
c="$(first_rejection /api/sync/ws/room1 300 '200')"
if [ "$c" = "429" ]; then
    printf 'ok   - the sync upgrade zone still throttles a flood, with 429\n'
elif [ "$c" = "none" ]; then
    fail "sync upgrades never throttled in 700 requests — is limit_req missing?"
else
    fail "sync upgrades threw $c when throttling, expected 429"
fi

# 4b. The slide detail read has a zone of its own too: when the presenter
#     switches slides every follower fetches it at once, so a class behind one
#     address must get through although wsi_api (drained by test 2 from this
#     same address) is exhausted ...
c="$(first_rejection /api/slides/AbC_-09xyz 200 '200')"
if [ "$c" = "none" ]; then
    printf 'ok   - 200 slide-detail reads from one address in a row are all admitted\n'
else
    fail "slide-detail reads: request rejected with $c within 200 (burst too small for a NAT'd class?)"
fi
# ... while the rest of /api/slides/ stays in wsi_api ...
c="$(first_rejection /api/slides/AbC_-09xyz/tours 80 '200')"
if [ "$c" = "429" ]; then
    printf 'ok   - other /api/slides/ routes stay metered by wsi_api (429)\n'
else
    fail "/api/slides/<id>/tours: first rejection '$c' in 80, expected 429 from the exhausted wsi_api"
fi
# ... and a flood past the burst still meets a ceiling, answered with 429.
c="$(first_rejection /api/slides/AbC_-09xyz 300 '200')"
if [ "$c" = "429" ]; then
    printf 'ok   - the slide-detail zone still throttles a flood, with 429\n'
elif [ "$c" = "none" ]; then
    fail "slide-detail reads never throttled in 500 requests — is limit_req missing?"
else
    fail "slide-detail reads threw $c when throttling, expected 429"
fi

# 4c. The same goes for what a visitor's browser reads to open a tour: the tour
#     itself (it has no retry), and the sign-in state and site settings every
#     public page fetches first. A class behind one NAT address opening one
#     tour link is 30 students x these three at once - more than wsi_api's
#     burst of 40 before the page's analytics beacons. They have the lenient
#     zone (shared with the slide detail, so this uses an address of its own),
#     and get through although wsi_api is exhausted for that address ...
code4() { curl -s -o /dev/null -w '%{http_code}' -m 5 --interface 127.0.0.4 "http://127.0.0.1:${NG_PORT}$1"; }
for _ in $(seq 1 80); do code4 /api/slides/AbC_-09xyz/tours >/dev/null; done
if [ "$(code4 /api/slides/AbC_-09xyz/tours)" = "429" ]; then
    printf 'ok   - (wsi_api is exhausted for 127.0.0.4)\n'
else
    fail "could not exhaust wsi_api for 127.0.0.4"
fi
for path in /api/tours/TourToken_-09 /api/auth/state /api/viewer/settings; do
    bad=""
    for _ in $(seq 1 60); do
        c="$(code4 "$path")"
        [ "$c" = "200" ] || { bad="$c"; break; }
    done
    if [ -z "$bad" ]; then
        printf 'ok   - 60 reads of %s from one address in a row are all admitted\n' "$path"
    else
        fail "$path: request rejected with $bad within 60 (a NAT'd class opening one tour?)"
    fi
done
# ... while the rest of /api/tours/ stays in wsi_api (drained again: it refills
#     at 2 r/s, and the reads above took a few seconds) ...
for _ in $(seq 1 80); do code4 /api/slides/AbC_-09xyz/tours >/dev/null; done
c="$(code4 /api/tours/TourToken_-09/steps)"
if [ "$c" = "429" ]; then
    printf 'ok   - other /api/tours/ routes stay metered by wsi_api (429)\n'
else
    fail "/api/tours/<id>/steps: answered $c, expected 429 from the exhausted wsi_api"
fi
# ... and a flood past the burst still meets a ceiling, answered with 429.
c="none"
for _ in $(seq 1 400); do
    r="$(code4 /api/tours/TourToken_-09)"
    [ "$r" = "200" ] || { c="$r"; break; }
done
if [ "$c" = "429" ]; then
    printf 'ok   - the tour read still throttles a flood, with 429\n'
else
    fail "tour reads: first rejection '$c' in 400, expected 429"
fi

# 4d. A live-sync room behind one address: on every presenter switch each
#     follower sends the slide read AND the settings read, and on a join the
#     sign-in state and the settings read. They must not spend each other's
#     burst: 200 followers = 200 + 200 requests of the two kinds, all admitted
#     (a shared budget leaves about half of the slide reads with a 429, the
#     follower without its slide until the retry).
code5() { curl -s -o /dev/null -w '%{http_code}' -m 5 --interface 127.0.0.5 "http://127.0.0.1:${NG_PORT}$1"; }
bad=""
for _ in $(seq 1 200); do
    c="$(code5 /api/viewer/settings)"; [ "$c" = "200" ] || { bad="settings:$c"; break; }
done
if [ -z "$bad" ]; then
    for _ in $(seq 1 200); do
        c="$(code5 /api/slides/AbC_-09xyz)"; [ "$c" = "200" ] || { bad="slide:$c"; break; }
    done
fi
if [ -z "$bad" ]; then
    printf 'ok   - 200 settings reads do not take the slide reads of the same room with them\n'
else
    fail "a presenter switch for 200 followers from one address: first refusal $bad (do the two reads share a zone?)"
fi
code6() { curl -s -o /dev/null -w '%{http_code}' -m 5 --interface 127.0.0.6 "http://127.0.0.1:${NG_PORT}$1"; }
bad=""
for _ in $(seq 1 200); do
    for path in /api/auth/state /api/viewer/settings; do
        c="$(code6 "$path")"; [ "$c" = "200" ] || { bad="$path:$c"; break 2; }
    done
done
if [ -z "$bad" ]; then
    printf 'ok   - a join by 200 followers (sign-in state + settings: 400 reads) is admitted\n'
else
    fail "a join by 200 followers from one address: first refusal $bad"
fi

# 5. Same for the SPA catch-all, which sets limit_req_status separately.
#    burst=100 on a 600r/min zone. 404 counts as served (no SPA files here).
c="$(first_rejection /rate-limit-probe 260 '200 404')"
if [ "$c" = "429" ]; then
    printf 'ok   - location / throttles with 429 rather than nginx default 503\n'
elif [ "$c" = "none" ]; then
    fail "location / never throttled in 260 requests"
else
    fail "location / threw $c when throttling, expected 429"
fi

# 6. /dzi/ is metered only for requests with no ?v= (wsi_dzi_uncached): the
#    shared cache never answers those, so each one costs the app a render. The
#    versioned requests every current viewer sends must never be counted —
#    cold MISSes and HITs alike, or a whole class behind one address would be
#    throttled. 500 distinct versioned tiles plus 100 repeats, back to back:
now_ms() { date +%s%3N; }
bad=""
for i in $(seq 1 500); do
    c="$(code "/dzi/s1_files/12/${i}_0.jpeg?v=3")"
    [ "$c" = "200" ] || { bad="$c (tile $i)"; break; }
done
if [ -z "$bad" ]; then
    for i in $(seq 1 100); do
        c="$(code "/dzi/s1_files/12/1_0.jpeg?v=3")"
        [ "$c" = "200" ] || { bad="$c (repeat $i)"; break; }
    done
fi
if [ -z "$bad" ]; then
    printf 'ok   - 600 versioned /dzi/ requests in a row are all served (not metered)\n'
else
    fail "a versioned /dzi/ request was refused: $bad"
fi

# ... while a client without ?v= gets its burst at once (101, sent in parallel
# so the bucket barely drains meanwhile — and the 600 versioned requests above
# did not eat into it) ...
flood() {  # flood <count> <parallel> <timeout-s> <path-with-{}>: "<n> <code>," per code
    seq 1 "$1" | xargs -P "$2" -I{} curl -s -o /dev/null -w '%{http_code}\n' -m "$3" \
        "http://127.0.0.1:${NG_PORT}$4" | sort | uniq -c | awk '{ printf "%s %s,", $1, $2 }'
}
t0="$(now_ms)"
codes="$(flood 101 32 5 '/dzi/s1_files/12/{}_0.jpeg')"
t1="$(now_ms)"
# (Under 2.5 s, or the bucket drains enough meanwhile — 10 r/s — to blur the
# next check.)
if [ "$codes" = "101 200," ] && [ $(( t1 - t0 )) -lt 2500 ]; then
    printf 'ok   - the first 101 unversioned /dzi/ requests are served at once (%d ms)\n' $(( t1 - t0 ))
else
    fail "unversioned /dzi/ burst: answers '${codes}' in $(( t1 - t0 )) ms (expected 101 200s, in < 2500 ms)"
fi
# ... then is slowed to the zone's 10 r/s rather than refused: of 40 more, at
# most ~25 can still go through undelayed, the rest take 100 ms each ...
t0="$(now_ms)"
c="$(first_rejection /dzi/s1.dzi 40 '200')"
t1="$(now_ms)"
if [ "$c" = "none" ] && [ $(( t1 - t0 )) -ge 1000 ]; then
    printf 'ok   - past that, unversioned requests (the .dzi too) are delayed to the rate (40 in %d ms)\n' $(( t1 - t0 ))
else
    fail "unversioned /dzi/ past the delay threshold: rejection '$c', 40 requests in $(( t1 - t0 )) ms (expected >= 1000 ms)"
fi
# ... a versioned request meanwhile is not held up ...
t0="$(now_ms)"
c="$(first_rejection "/dzi/s1_files/12/1_0.jpeg?v=3" 20 '200')"
t1="$(now_ms)"
if [ "$c" = "none" ] && [ $(( t1 - t0 )) -lt 1500 ]; then
    printf 'ok   - versioned requests from that client still go straight through (20 in %d ms)\n' $(( t1 - t0 ))
else
    fail "versioned /dzi/ while the client's unversioned bucket is full: rejection '$c', $(( t1 - t0 )) ms for 20"
fi
# ... a flood past the queue is answered 429 (the queued ones are still
# waiting their turn when curl gives up on them: 000) ...
codes="$(flood 600 200 1 '/dzi/s1_files/12/{}_1.jpeg')"
case ",${codes}" in
    *" 429,"*)
        if grep -qE ' 5[0-9][0-9],' <<<"${codes}"; then
            fail "unversioned /dzi/ flood: a 5xx among the answers (${codes})"
        else
            printf 'ok   - an unversioned /dzi/ flood past the queue is refused with 429 (%s)\n' "${codes%,}"
        fi ;;
    *) fail "unversioned /dzi/ flood of 600 concurrent requests never saw a 429 (${codes})" ;;
esac
# ... and the zone is per client: another address is not affected.
c="$(curl -s -o /dev/null -w '%{http_code}' -m 5 --interface 127.0.0.2 \
    "http://127.0.0.1:${NG_PORT}/dzi/s1_files/12/1_0.jpeg")"
if [ "$c" = "200" ]; then
    printf 'ok   - another client'"'"'s unversioned request is served (200)\n'
else
    fail "unversioned /dzi/ from 127.0.0.2 returned $c while 127.0.0.1 was throttled"
fi

# 6b. A request with an Authorization header other than Basic (a staff Bearer,
#     or a junk one: nginx cannot tell) bypasses the shared cache, so the app
#     renders its tile every time. The unversioned zone above never counts it
#     (the URL carries ?v=), so without a zone of its own `Authorization:
#     Bearer x` in a loop would be an unmetered way to make the app render.
#     wsi_dzi_authed meters it: 50 r/s
#     per client, a burst of 600 -- while a request WITHOUT the header (every
#     student) and an ordinary Basic one are not counted at all.
authed_flood() {  # authed_flood <count> <parallel> <address> <auth-header>: "<n> <code>," per code
    seq 1 "$1" | xargs -P "$2" -I{} curl -s -o /dev/null -w '%{http_code}\n' -m 8 \
        --interface "$3" -H "Authorization: $4" \
        "http://127.0.0.1:${NG_PORT}/dzi/s1_files/12/{}_2.jpeg?v=3" | sort | uniq -c \
        | awk '{ printf "%s %s,", $1, $2 }'
}
codes="$(authed_flood 60 8 127.0.0.3 'Bearer staff.jwt.token')"
if [ "$codes" = "60 200," ]; then
    printf 'ok   - a staff-sized run of Bearer tile requests (60) is served\n'
else
    fail "60 Bearer /dzi/ requests from one client: '${codes}' (expected 60 200s)"
fi
codes="$(authed_flood 900 32 127.0.0.3 'Bearer junk')"
case ",${codes}" in
    *" 429,"*) printf 'ok   - a flood of Bearer tile requests is refused with 429 (%s)\n' "${codes%,}" ;;
    *) fail "900 Bearer /dzi/ requests never saw a 429 (${codes}) - is wsi_dzi_authed missing?" ;;
esac
c="$(curl -s -o /dev/null -w '%{http_code}' -m 5 --interface 127.0.0.3 \
    "http://127.0.0.1:${NG_PORT}/dzi/s1_files/12/1_2.jpeg?v=3")"
if [ "$c" = "200" ]; then
    printf 'ok   - ...while the same address without the header (students) is not counted\n'
else
    fail "a versioned /dzi/ request without Authorization from the flooded address returned $c"
fi
codes="$(authed_flood 700 32 127.0.0.3 'Basic YWxpY2U6cHc=')"
if [ "$codes" = "700 200," ]; then
    printf 'ok   - HTTP Basic (cacheable) requests are not counted either\n'
else
    fail "700 Basic /dzi/ requests from the flooded address: '${codes}' (expected 700 200s)"
fi

# 7. IPv6 clients are also keyed on their /64 for the two login endpoints.
#    First the key itself ($wsi_rl_net64), for every way "::" can fall in the
#    first four groups, against Python's own reading of the address — the
#    expected key is the /64's network address, group by group.
rlkey() { curl -s -m 5 -H "Cf-Connecting-IP: $1" "http://127.0.0.1:${NG_PORT}/_rlkey"; }
ADDRS=(
    2001:db8:1:2:3:4:5:6 2001:db8:1:2::5 2001:db8:1:2:: 2001:db8:1::5 2001:db8:1::
    2001:db8::1 2001:db8::1:0:0:1 2001:db8::5:6:7:8:9 2001::1:2:3:4:5:6
    2001::2:3:4:5:6 2001::1 2001:: ::1 :: ::1:2:3:4:5:6:7 ::2:3:4:5:6:7
    ::3:4:5:6:7 fe80::1 fd00:ab:cd:ef:1:2:3:4 2001:db8:0:0:1:0:0:1 2001:db8::1:0:0:0
)
bad=0
for a in "${ADDRS[@]}"; do
    want="$(python3 -c 'import ipaddress, sys
n = ipaddress.ip_network(sys.argv[1] + "/64", strict=False).network_address.exploded.split(":")
print(sys.argv[1] + "|" + ":".join(format(int(g, 16), "x") for g in n[:4]) + "::/64")' "$a")"
    got="$(rlkey "$a")"
    [ "$got" = "$want" ] || { fail "IPv6 /64 key: Cf-Connecting-IP $a -> '$got', expected '$want'"; bad=1; }
done
for a in 203.0.113.7 ::ffff:198.51.100.9 64:ff9b::192.0.2.1; do
    got="$(rlkey "$a")"
    [ "$got" = "$a|" ] || { fail "IPv4 key: Cf-Connecting-IP $a -> '$got', expected '$a|' (no /64 key)"; bad=1; }
done
got="$(curl -s -m 5 "http://127.0.0.1:${NG_PORT}/_rlkey")"
[ "$got" = "127.0.0.1|" ] || { fail "no Cf-Connecting-IP: key '$got', expected '127.0.0.1|'"; bad=1; }
got="$(curl -s -m 5 --interface 127.0.0.2 -H "Cf-Connecting-IP: 2001:db8::1" "http://127.0.0.1:${NG_PORT}/_rlkey")"
[ "$got" = "127.0.0.2|" ] || { fail "untrusted peer's Cf-Connecting-IP was used: key '$got'"; bad=1; }
[ "$bad" -eq 0 ] && printf 'ok   - IPv6 clients key on their /64 (%d spellings), IPv4 and untrusted headers unchanged\n' "${#ADDRS[@]}"

# Count requests admitted before the first refusal, each from a fresh
# Cf-Connecting-IP made by printf-ing the counter into a template.
admitted_rotating() {
    local path="$1" tmpl="$2" max="$3" n=0 c i
    for i in $(seq 1 "$max"); do
        # shellcheck disable=SC2059
        c="$(curl -s -o /dev/null -w '%{http_code}' -m 5 -H "Cf-Connecting-IP: $(printf "$tmpl" "$i")" \
            "http://127.0.0.1:${NG_PORT}${path}")"
        [ "$c" = "200" ] || { printf '%s %s' "$n" "$c"; return; }
        n=$((n + 1))
    done
    printf '%s none' "$n"
}
check_rotation() {  # check_rotation <label> <path> <template> <max> <min admitted> <max admitted>
    local got n c
    got="$(admitted_rotating "$2" "$3" "$4")"; n="${got% *}"; c="${got#* }"
    if [ "$c" = "429" ] && [ "$n" -ge "$5" ] && [ "$n" -le "$6" ]; then
        printf 'ok   - %s: %s admitted, then 429\n' "$1" "$n"
    else
        fail "$1: ${n} admitted, then '${c}' (expected ${5}-${6}, then 429)"
    fi
}
# A client rotating through one /64 is capped (per-address keying alone would
# give each new address a fresh 10 r/min) — yet a class with an address each
# gets in.
check_rotation "app login, new address in one /64 each time" /api/auth/login '2001:db8:a1:1::%x' 160 100 125
check_rotation "dashboard login, new address in one /64 each time" /api/admin/login '2001:db8:a2:1::%x' 40 10 16
# Another /64 has a budget of its own.
c="$(curl -s -o /dev/null -w '%{http_code}' -m 5 -H "Cf-Connecting-IP: 2001:db8:a1:2::1" \
    "http://127.0.0.1:${NG_PORT}/api/auth/login")"
[ "$c" = "200" ] && printf 'ok   - another /64 is not affected (200)\n' \
    || fail "app login from another /64 returned $c"
# One address, IPv4 or IPv6, still meets the per-address limit (burst 5) ...
check_rotation "app login, one IPv6 address" /api/auth/login '2001:db8:a3:1::1%.0s' 20 6 8
check_rotation "app login, one IPv4 address" /api/auth/login '198.51.100.7%.0s' 20 6 8
# The dashboard's two-factor code step is not on the login zones (its own
# guesses are counted by the app): a sign-in costs one request of them, not two.
got="$(admitted_rotating /api/admin/login/mfa '198.51.100.9%.0s' 20)"
[ "$got" = "20 none" ] && printf 'ok   - dashboard two-factor step is under the general admin API limit\n' \
    || fail "two-factor step: ${got} (expected all 20 admitted under the /api/admin/ zone)"
# ... and IPv4 is not aggregated: 150 addresses of one /24, once each.
got="$(admitted_rotating /api/auth/login '203.0.113.%d' 150)"
[ "$got" = "150 none" ] && printf 'ok   - 150 IPv4 addresses, once each, are all admitted (no aggregation)\n' \
    || fail "rotating IPv4 addresses: ${got} (expected all 150 admitted)"

printf -- '--------------------------------------------------------------\n'
if [ "$FAILED" -eq 0 ]; then
    printf 'PASS - rate-limit metering + 429 status verified against the real snippet\n'
else
    exit 1
fi
