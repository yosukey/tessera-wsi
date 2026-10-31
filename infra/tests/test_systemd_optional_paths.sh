#!/usr/bin/env bash
# Guard: a sandbox path a unit lists must exist, or be marked optional.
#
# Run: bash infra/tests/test_systemd_optional_paths.sh
#
# WHY THIS EXISTS
# ---------------
# systemd builds a unit's mount namespace from ReadWritePaths= /
# ReadOnlyPaths= / InaccessiblePaths= / ExecPaths= / NoExecPaths= before it
# starts the process. A listed path that does not exist fails that step —
# the service dies with status=226/NAMESPACE and never runs — unless the entry
# carries a "-" prefix, which tells systemd to skip it when absent.
# If wsi-app.service listed /etc/avahi/services without the prefix, a host
# without avahi-daemon (a slim image, or avahi removed later) could not start
# the app at all, while install.sh reports the missing directory as a mere
# "mDNS toggle will not work" warning.
#
# The rule checked here: every such entry in infra/systemd/*.service is either
# "-"-prefixed or one of the paths install.sh guarantees before it starts the
# units — /var/lib/wsi (created with install -d) and /run/wsi (the tmpfiles.d
# entry, recreated at every boot). That list is itself checked against
# install.sh, so a guarantee dropped there fails here too.
#
# Static: needs only python3 (SKIPs without it), no systemd, no root.

set -uo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
INFRA="$(cd "${HERE}/.." && pwd)"

command -v python3 >/dev/null 2>&1 || { echo "SKIP - python3 not available"; exit 0; }

python3 - "${INFRA}" <<'PY'
import glob
import os
import re
import sys

infra = sys.argv[1]
install_sh = open(os.path.join(infra, "install.sh")).read()

PATH_KEYS = ("ReadWritePaths", "ReadOnlyPaths", "InaccessiblePaths",
             "ExecPaths", "NoExecPaths")
# Paths install.sh creates before it starts any unit, each with the evidence
# that it still does.
GUARANTEED = {
    "/var/lib/wsi": (
        'DATA_DIR="${DATA_DIR:-/var/lib/wsi}"' in install_sh
        and re.search(r'^install -d [^\n]*"\$DATA_DIR"$', install_sh, re.M) is not None
    ),
    "/run/wsi": re.search(r"^d\s+/run/wsi\s", install_sh, re.M) is not None,
}

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


for path, ok in GUARANTEED.items():
    check(f"install.sh still creates {path} before starting the units", ok)


def directives(unit_text):
    """(key, value) for every directive, backslash continuations joined."""
    text = re.sub(r"\\\n", " ", unit_text)
    for line in text.splitlines():
        line = line.strip()
        if not line or line[0] in "#;" or "=" not in line:
            continue
        key, _, value = line.partition("=")
        yield key.strip(), value.strip()


units = sorted(glob.glob(os.path.join(infra, "systemd", "*.service")))
check("found the systemd units", len(units) >= 5, str(units))

seen = {}
for unit in units:
    name = os.path.basename(unit)
    for key, value in directives(open(unit).read()):
        if key not in PATH_KEYS:
            continue
        for entry in value.split():
            # "-" and "+" (relative to RootDirectory=) may come in either order.
            optional = entry.lstrip("+").startswith("-")
            path = entry.lstrip("-+")
            seen[(name, path)] = optional
            check(f"{name}: {key} {entry} exists at start or is optional",
                  optional or GUARANTEED.get(path, False),
                  f"prefix it with '-' unless install.sh guarantees {path}")

# The avahi case: pin it explicitly, so it cannot pass by being dropped.
check("wsi-app.service keeps /etc/avahi/services writable, as optional",
      seen.get(("wsi-app.service", "/etc/avahi/services")) is True,
      "expected ReadWritePaths=... -/etc/avahi/services ...")

print(f"{checks - fails}/{checks} checks passed")
sys.exit(1 if fails else 0)
PY
