#!/usr/bin/env bash
# Tests for wsi_latest_release_tag (infra/lib/wsi-common.sh): which tag
# install.sh and update.sh deploy when no GIT_REF is given.
# Run: bash infra/tests/test_latest_release_tag.sh
#
# WHY THIS EXISTS
# ---------------
# The obvious one-liner
#     git -c versionsort.suffix=- tag -l 'v[0-9]*.[0-9]*.[0-9]*' \
#         --sort=-v:refname | head -n1
# does not keep pre-releases out: versionsort.suffix only sorts a
# pre-release below its OWN final, so with v1.10.0 and v2.0.0-rc1 on origin
# the rc comes first, and every host's next update would deploy it — and run
# its install.sh as root — as "the latest release". The glob also lets through
# v1.2.3.4 or v10.0.0x. The helper must return only an exact vX.Y.Z.
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

# latest_of NAME TAG... -> "<rc>:<what the helper printed>" for a fresh repo
# holding exactly those tags (all on one commit: only the names matter).
latest_of() {
    local repo="$TMP_ROOT/$1" t out rc sha
    shift
    git init -q "$repo"
    git -C "$repo" -c user.email=t@t -c user.name=t commit -q --allow-empty -m x
    sha="$(git -C "$repo" rev-parse HEAD)"
    # One update-ref for all of them: thousands of `git tag` runs are slow.
    for t in "$@"; do printf 'create refs/tags/%s %s\n' "$t" "$sha"; done \
        | git -C "$repo" update-ref --stdin
    wsi_define_run_git "$repo"
    out="$(wsi_latest_release_tag)"; rc=$?
    printf '%s:%s' "$rc" "$out"
}

expect "a newer pre-release is not 'the latest'" "0:v1.10.0" \
    "$(latest_of rc v1.9.0 v1.10.0 v2.0.0-rc1 v2.0.0-beta.1 v1.10.0-rc2)"
expect "four-part and suffixed names are not releases" "0:v1.10.0" \
    "$(latest_of junk v1.10.0 v1.2.3.4 v10.0.0x v11.0.0.1)"
expect "the final wins once it exists" "0:v2.0.0" \
    "$(latest_of final v1.10.0 v2.0.0-rc1 v2.0.0)"
expect "versions compare numerically, not as text" "0:v0.10.0" \
    "$(latest_of numeric v0.9.10 v0.10.0 v0.9.9)"
expect "only pre-releases: nothing, and still success" "0:" \
    "$(latest_of onlyrc v1.0.0-rc1 v1.0.0-beta)"
expect "no tags at all: nothing, and still success" "0:" \
    "$(latest_of none)"
# Enough tags (~200 KB of names) that the list overflows a pipe buffer: a
# reader that stops at the first line would leave git writing into a closed
# pipe (SIGPIPE, which pipefail reports as a failure).
many=()
for i in $(seq 1 12000); do many+=("v0.$i.0-rc1"); done
expect "a long tag list is read to the end (no SIGPIPE)" "0:v0.5.0" \
    "$(latest_of many v0.5.0 "${many[@]}")"

# Both scripts use the helper, and nothing else picks a tag its own way.
for s in install.sh update.sh; do
    expect "$s deploys wsi_latest_release_tag's pick" "1" \
        "$(grep -cE '^[[:space:]]*LATEST_TAG="\$\(wsi_latest_release_tag\)"$' "${INFRA}/$s")"
    expect "$s lists no tags itself" "0" "$(grep -cE 'tag -l|--sort=-?v:refname' "${INFRA}/$s")"
done

printf -- '--------------------------------------------------------------\n'
if [[ "$fails" -eq 0 ]]; then
    printf 'PASS - only a final vX.Y.Z tag is ever deployed as the latest release\n'
else
    exit 1
fi
