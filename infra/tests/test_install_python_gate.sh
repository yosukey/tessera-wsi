#!/usr/bin/env bash
# Tests for install.sh's Python version gate, and for the one Python floor the
# lock, the Makefile and CI must agree on.
# Run: bash infra/tests/test_install_python_gate.sh
#
# WHY THIS EXISTS
# ---------------
# requirements.lock is resolved for Python >= `make lock`'s --python-version
# (LOCK_PYTHON_FLOOR in the Makefile). Pins for an interpreter below the
# Python versions CI tests and audits are pins nobody ever verified — e.g.
# 3.10 / 3.11 sets of numpy, tifffile, SQLAlchemy ... that no workflow
# installs — and on an interpreter below the floor pip fails half-way
# through the install with a resolver error. So:
#   1. install.sh refuses a python3 below the floor it reads from the lock's
#      header, with a message that says so, and accepts the floor and above
#      (the gate is extracted from the real script and run against a stub
#      python3 reporting each version);
#   2. the lock header, LOCK_PYTHON_FLOOR and the lowest Python of ci.yml's
#      backend matrix and audit.yml's matrix are all the same version.
#
# Needs nothing but bash and coreutils; never touches the system.
set -uo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
REPO="$(cd "${HERE}/../.." && pwd)"
INSTALL_SH="${REPO}/infra/install.sh"

fails=0
expect() {
    # expect LABEL EXPECTED ACTUAL
    if [[ "$2" == "$3" ]]; then
        printf 'ok   - %s\n' "$1"
    else
        printf 'FAIL - %s: expected %q, got %q\n' "$1" "$2" "$3"
        fails=$(( fails + 1 ))
    fi
}

GATE_SRC="$(awk '
    /^# ---- 1b\. Python version gate/ { grabbing = 1 }
    /^# ---- 2\. /                     { exit }
    grabbing                           { print }
' "$INSTALL_SH")"
if [[ "$GATE_SRC" != *'PY_FLOOR='* || "$GATE_SRC" != *'PY_HAVE='* ]]; then
    echo "FAILED: could not extract the Python version gate from $INSTALL_SH"
    exit 1
fi

TMP_ROOT="$(mktemp -d)"
trap 'rm -rf "$TMP_ROOT"' EXIT
mkdir -p "$TMP_ROOT/bin"
# A python3 that only answers the gate's version query.
cat > "$TMP_ROOT/bin/python3" <<'SH'
#!/usr/bin/env bash
printf '%s\n' "$FAKE_PY_VERSION"
SH
chmod +x "$TMP_ROOT/bin/python3"

# run_gate VERSION -> "ok" / "died: <message>"
run_gate() (
    REPO_DIR="$REPO"
    PATH="$TMP_ROOT/bin:$PATH"
    export FAKE_PY_VERSION="$1"
    die() { printf 'died: %s' "$*"; exit 1; }
    log() { :; }
    eval "$GATE_SRC"
    printf 'ok'
)

FLOOR="$(sed -n -E '/^#.*--python-version/{s/^.*--python-version[[:space:]]+([0-9.]+).*$/\1/p;q;}' \
    "$REPO/services/app/requirements.lock")"
expect "the lock header records a Python floor" "yes" "$([[ -n "$FLOOR" ]] && echo yes)"

for v in 3.9 3.10 3.11; do
    out="$(run_gate "$v")"
    expect "python3 $v is refused" "died" "${out%%:*}"
done
out="$(run_gate 3.11)"
expect "the refusal names the required and the found version" "yes" \
    "$([[ "$out" == *"$FLOOR or newer"* && "$out" == *"python3 is 3.11"* ]] && echo yes)"
for v in "$FLOOR" 3.13 3.14 3.20 4.0; do
    expect "python3 $v is accepted" "ok" "$(run_gate "$v")"
done

# --- the floor is the same everywhere --------------------------------------
MAKE_FLOOR="$(sed -n -E 's/^LOCK_PYTHON_FLOOR[[:space:]]*\?=[[:space:]]*([0-9.]+)[[:space:]]*$/\1/p' "$REPO/Makefile")"
expect "Makefile LOCK_PYTHON_FLOOR matches the lock header" "$FLOOR" "$MAKE_FLOOR"
DEV_FLOOR="$(sed -n -E '/^#.*--python-version/{s/^.*--python-version[[:space:]]+([0-9.]+).*$/\1/p;q;}' \
    "$REPO/services/app/requirements-dev.lock")"
expect "requirements-dev.lock has the same floor" "$FLOOR" "$DEV_FLOOR"
# The lowest version in a workflow's `python: [...]` matrix line.
lowest_matrix_python() {
    sed -n -E "s/^[[:space:]]*python:[[:space:]]*\[(.*)\][[:space:]]*$/\1/p" "$1" \
        | head -n1 | tr -d "'\" " | tr ',' '\n' | sort -V | head -n1
}
expect "ci.yml tests the floor (lowest backend matrix Python)" "$FLOOR" \
    "$(lowest_matrix_python "$REPO/.github/workflows/ci.yml")"
expect "audit.yml audits the floor (lowest matrix Python)" "$FLOOR" \
    "$(lowest_matrix_python "$REPO/.github/workflows/audit.yml")"

printf -- '--------------------------------------------------------------\n'
if [[ "$fails" -eq 0 ]]; then
    printf 'PASS - install.sh enforces the lock'"'"'s Python floor, and CI tests it\n'
else
    exit 1
fi
