#!/usr/bin/env bash
# Tests for wsi_origin_transport_warning (infra/lib/wsi-common.sh): the warning
# install.sh and update.sh print when origin is fetched over a transport that
# does not authenticate the server.
# Run: bash infra/tests/test_origin_transport.sh
#
# WHY THIS EXISTS
# ---------------
# Both scripts check out a tag fetched from origin and run that release's
# install.sh as root. Over http://, git:// or ftp:// anyone on the network path
# can rewrite what is fetched, and SECURITY.md promises that both scripts warn
# about it — a first install (or GIT_REF=<tag> run of install.sh) included,
# not only update.sh. This pins which URLs warn, that the
# URL git really fetches from (after url.<base>.insteadOf) is the one checked,
# and that both scripts call the helper before their tag checkout.
#
# Runs the real helper against scratch git repos (git as the current user:
# SUDO_USER / GIT_AS_USER are cleared, so run_git needs no sudo). Needs git;
# SKIPs (exit 0) without it.
set -uo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
INFRA="$(cd "${HERE}/.." && pwd)"

skip() { printf 'SKIP - %s\n' "$1"; exit 0; }
command -v git >/dev/null 2>&1 || skip "git not available"

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

TMP_ROOT="$(mktemp -d)"
trap 'rm -rf "$TMP_ROOT"' EXIT

unset SUDO_USER GIT_AS_USER
# shellcheck source=../lib/wsi-common.sh
. "${INFRA}/lib/wsi-common.sh"

REPO="$TMP_ROOT/clone"
git init -q "$REPO"
wsi_define_run_git "$REPO"

# warns URL -> "warn" when the helper prints a warning naming URL, "quiet" when
# it prints nothing, "other:<output>" otherwise.
warns() {
    local out
    git -C "$REPO" remote remove origin 2>/dev/null
    [[ -z "$1" ]] || git -C "$REPO" remote add origin "$1"
    out="$(wsi_origin_transport_warning)"
    if [[ -z "$out" ]]; then
        echo quiet
    elif [[ "$out" == *"unauthenticated transport ($1)"* ]]; then
        echo warn
    else
        echo "other:$out"
    fi
}

expect "http:// warns"                 warn  "$(warns http://example.org/tessera-wsi.git)"
expect "HTTP:// (any case) warns"      warn  "$(warns HTTP://example.org/tessera-wsi.git)"
expect "git:// warns"                  warn  "$(warns git://example.org/tessera-wsi.git)"
expect "ftp:// warns"                  warn  "$(warns ftp://example.org/tessera-wsi.git)"
expect "https:// is quiet"             quiet "$(warns https://github.com/yosukey/tessera-wsi.git)"
expect "ssh:// is quiet"               quiet "$(warns ssh://git@github.com/yosukey/tessera-wsi.git)"
expect "scp-style ssh is quiet"        quiet "$(warns git@github.com:yosukey/tessera-wsi.git)"
expect "a local path is quiet"         quiet "$(warns /srv/git/tessera-wsi.git)"
expect "no origin is quiet"            quiet "$(warns '')"

# url.<base>.insteadOf rewrites what git fetches from; the check must see that.
git -C "$REPO" remote add origin https://mirror.example/tessera-wsi.git
git -C "$REPO" config url.http://mirror.example/.insteadOf https://mirror.example/
out="$(wsi_origin_transport_warning)"
expect "insteadOf to http:// warns" yes \
    "$([[ "$out" == *"(http://mirror.example/tessera-wsi.git)"* ]] && echo yes || echo no)"
git -C "$REPO" config --unset url.http://mirror.example/.insteadOf

# Both deploying scripts call it (static: running them needs root and a host).
for s in install.sh update.sh; do
    # shellcheck disable=SC2016  # the literal source line, not an expansion
    expect "$s calls wsi_origin_transport_warning" 1 \
        "$(grep -c 'ORIGIN_WARNING="$(wsi_origin_transport_warning)"' "${INFRA}/$s")"
done
# ... and before the tag checkout it guards.
first_line() { grep -n -m1 -F -- "$2" "${INFRA}/$1" | cut -d: -f1; }
expect "install.sh warns before its checkout" yes \
    "$( (( $(first_line install.sh 'wsi_origin_transport_warning') < $(first_line install.sh 'run_git checkout') )) && echo yes || echo no)"
expect "update.sh warns before its checkout" yes \
    "$( (( $(first_line update.sh 'wsi_origin_transport_warning') < $(first_line update.sh 'move_tree checkout') )) && echo yes || echo no)"

if (( fails )); then
    printf '%d check(s) failed\n' "$fails"
    exit 1
fi
echo "all checks passed"
