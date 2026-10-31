#!/usr/bin/env bash
# Guard: every large admin download route streams through nginx.
#
# Run: bash infra/tests/test_download_locations.sh
#
# WHY THIS EXISTS
# ---------------
# A route that falls through to the generic `location /api/admin/` is proxied
# with proxy_buffering ON (nginx spools the whole response to
# /var/lib/nginx/proxy before sending a byte) and the server-level 60 s
# timeouts. For a multi-GB download that means a long stall before the first
# byte and a cut-off on a slow link. Each download route — the backup and
# slide-export downloads, and GET /api/admin/slides/{id}/download (the
# original WSI, up to the 8 GiB upload cap) — needs a streaming location.
#
# The check is static (it reads infra/nginx/wsi-locations.conf and the FastAPI
# routers), so it needs no nginx binary and never SKIPs: for every
# `GET /api/admin/.../download` route in the app it finds the location nginx
# would pick (regex locations, first match in file order — no `^~`/exact
# location shadows them) and requires the streaming settings the other
# download locations carry.

set -uo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
REPO="$(cd "${HERE}/../.." && pwd)"
SNIPPET="${REPO}/infra/nginx/wsi-locations.conf"
ROUTERS="${REPO}/services/app/wsi_app/routers"

command -v python3 >/dev/null 2>&1 || { echo "SKIP - python3 not available"; exit 0; }
[[ -f "${SNIPPET}" ]] || { echo "FAIL - snippet not found: ${SNIPPET}"; exit 1; }
[[ -d "${ROUTERS}" ]] || { echo "FAIL - routers not found: ${ROUTERS}"; exit 1; }

python3 - "${SNIPPET}" "${ROUTERS}" <<'PY'
import glob
import re
import sys

snippet, routers = sys.argv[1], sys.argv[2]
text = open(snippet).read()
# Drop comments so a commented-out directive can't satisfy a check.
text = "\n".join(re.sub(r"(^|\s)#.*$", "", ln) for ln in text.splitlines())

# `location [modifier] uri { ... }` — the blocks here don't nest.
locations = []  # (modifier, uri, body) in file order
for m in re.finditer(r"location\s+(~\*|~|\^~|=)?\s*(\S+)\s*\{([^{}]*)\}", text):
    locations.append((m.group(1) or "", m.group(2), m.group(3)))

fails = 0
checks = 0


def check(label, ok, detail=""):
    global fails, checks
    checks += 1
    if ok:
        print(f"ok   - {label}")
    else:
        fails += 1
        print(f"FAIL - {label}" + (f": {detail}" if detail else ""))


def pick(path):
    """The location nginx selects for ``path``: an exact match, else the
    longest matching prefix (remembered), unless that prefix is `^~`, else the
    first matching regex in file order, else the remembered prefix."""
    best = None
    for mod, uri, body in locations:
        if mod == "=" and uri == path:
            return mod, uri, body
    for mod, uri, body in locations:
        if mod in ("", "^~") and path.startswith(uri):
            if best is None or len(uri) > len(best[1]):
                best = (mod, uri, body)
    if best is not None and best[0] == "^~":
        return best
    for mod, uri, body in locations:
        if mod in ("~", "~*") and re.search(uri, path, re.I if mod == "~*" else 0):
            return mod, uri, body
    return best


# Every `@router.get("/api/admin/.../download")` in the app.
routes = []
for f in sorted(glob.glob(routers + "/*.py")):
    for m in re.finditer(r'@router\.get\(\s*"(/api/admin/[^"]*/download)"', open(f).read()):
        routes.append(m.group(1))
check("the app has admin download routes to guard", len(routes) >= 3, str(routes))

REQUIRED = (
    ("proxy_buffering off;", r"proxy_buffering\s+off\s*;"),
    ("proxy_read_timeout 600s", r"proxy_read_timeout\s+600s\s*;"),
    ("send_timeout 600s", r"send_timeout\s+600s\s*;"),
    ("limit_req zone=wsi_api", r"limit_req\s+zone=wsi_api\b"),
    ("proxy_pass http://wsi_app", r"proxy_pass\s+http://wsi_app\s*;"),
    ("Cf-Connecting-IP from $wsi_cf_connecting_ip",
     r"proxy_set_header\s+Cf-Connecting-IP\s+\$wsi_cf_connecting_ip\s*;"),
    ("Cf-Access-Jwt-Assertion from $wsi_cf_access_jwt",
     r"proxy_set_header\s+Cf-Access-Jwt-Assertion\s+\$wsi_cf_access_jwt\s*;"),
)

for template in routes:
    concrete = re.sub(r"\{[^}]+\}", "42", template)
    loc = pick(concrete)
    check(f"{template}: has a dedicated regex location",
          loc is not None and loc[0] in ("~", "~*"),
          f"falls through to {loc[0] + ' ' + loc[1] if loc else 'nothing'}")
    if loc is None or loc[0] not in ("~", "~*"):
        continue
    for label, pattern in REQUIRED:
        check(f"{template}: {label}", re.search(pattern, loc[2]) is not None)

print(f"{checks - fails}/{checks} checks passed")
sys.exit(1 if fails else 0)
PY
