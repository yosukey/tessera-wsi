#!/usr/bin/env bash
# Tests for how install.sh ends after its health checks: a service that did
# not answer /api/health in time is not fatal on the spot (the summary, with a
# fresh install's credential file, still prints), but the run then exits
# non-zero and its headline doesn't claim "Installation complete.".
# Run: bash infra/tests/test_install_health_summary.sh
#
# The summary tail (from "# ---- 11. Summary" to the end) is extracted from
# the real install.sh and run under install.sh's own shell options and an ERR
# trap, so an edit to it is exercised here. Needs no root and no services.
set -uo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
INSTALL_SH="${HERE}/../install.sh"

fails=0
checks=0
expect() {
    # expect LABEL EXPECTED ACTUAL
    checks=$(( checks + 1 ))
    if [[ "$2" == "$3" ]]; then
        printf 'ok   - %s\n' "$1"
    else
        printf 'FAIL - %s: expected %q, got %q\n' "$1" "$2" "$3"
        fails=$(( fails + 1 ))
    fi
}

[[ -r "$INSTALL_SH" ]] || { echo "FAILED: cannot read $INSTALL_SH"; exit 1; }

TAIL_SRC="$(sed -n '/^# ---- 11\. Summary/,$p' "$INSTALL_SH")"
if [[ -z "$TAIL_SRC" ]]; then
    echo "FAILED: could not extract the summary from $INSTALL_SH"
    exit 1
fi

# Both health-check timeouts are recorded, not only warned about.
grep -qF 'HEALTH_TIMEOUTS+=("wsi-app did not answer' "$INSTALL_SH"
expect "a wsi-app timeout is recorded" 0 $?
grep -qF 'HEALTH_TIMEOUTS+=("wsi-sync did not answer' "$INSTALL_SH"
expect "a wsi-sync timeout is recorded" 0 $?

TMP="$(mktemp -d)" || { echo "FAILED: mktemp"; exit 1; }
trap 'rm -rf "$TMP"' EXIT

# run_tail OUT FRESH [TIMEOUT...]: run the summary (FRESH = FRESH_INSTALL)
# with those timeouts recorded. The ERR trap is a stand-in for install.sh's
# on_err: it only reports that one fired.
run_tail() {
    local out="$1" fresh="$2"; shift 2
    {
        echo 'set -Eeuo pipefail'
        echo "trap 'echo ERR-TRAP-FIRED >&2' ERR"
        echo "FRESH_INSTALL=$fresh; ADMIN_PW_FILE=/etc/wsi/admin-initial-password.txt"
        echo 'POST_INSTALL_WARNINGS=(); HEALTH_TIMEOUTS=()'
        for t in "$@"; do printf 'HEALTH_TIMEOUTS+=(%q)\n' "$t"; done
        printf '%s\n' "$TAIL_SRC"
    } > "$TMP/tail.sh"
    bash "$TMP/tail.sh" > "$out" 2>&1
}

run_tail "$TMP/ok.out" 0
expect "all healthy: exit 0" 0 $?
grep -q '^ Installation complete\.$' "$TMP/ok.out"
expect "all healthy: says installation complete" 0 $?
grep -q 'not serving' "$TMP/ok.out"
expect "all healthy: no error" 1 $?

run_tail "$TMP/timeout.out" 0 "wsi-sync did not answer"
expect "a timeout: exit 1" 1 $?
grep -q 'Installation complete' "$TMP/timeout.out"
expect "a timeout: does not say installation complete" 1 $?
grep -q 'wsi-sync did not answer' "$TMP/timeout.out"
expect "a timeout: names the service" 0 $?

run_tail "$TMP/fresh.out" 1 "wsi-app did not answer"
expect "a fresh install that timed out: exit 1" 1 $?
grep -qF '/etc/wsi/admin-initial-password.txt' "$TMP/fresh.out"
expect "a fresh install that timed out: still names the credential file" 0 $?
grep -q 'ERR-TRAP-FIRED' "$TMP/ok.out" "$TMP/timeout.out" "$TMP/fresh.out"
expect "no path trips the ERR trap" 1 $?

printf -- '--------------------------------------------------------------\n'
if [[ "$fails" -eq 0 ]]; then
    printf 'PASS: %d checks\n' "$checks"
else
    printf 'FAILED: %d of %d checks\n' "$fails" "$checks"
    exit 1
fi
