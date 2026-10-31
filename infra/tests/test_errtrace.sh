#!/usr/bin/env bash
# Static check: every infra script that installs an ERR trap also turns on
# errtrace (`set -E`). Run: bash infra/tests/test_errtrace.sh
#
# WHY THIS EXISTS
# ---------------
# install.sh runs under `set -euo pipefail` with an ERR trap that prints
# "FAILED at line N (exit X): <command>". Without -E bash does not run an ERR
# trap inside shell functions, command substitutions or subshells, so a
# command failing in, say, ensure_data_subdir would still end the install
# through `set -e` — silently, with no hint of where. In update.sh the trap is
# also what triggers the rollback. This keeps every such script on -E.
# Needs nothing but bash and grep.
set -uo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
INFRA="$(cd "${HERE}/.." && pwd)"

fails=0
checked=0
for f in "${INFRA}"/*.sh "${INFRA}"/lib/*.sh "${INFRA}"/systemd/*.sh; do
    # A trap on ERR (alone or among other signals) outside a comment.
    grep -qE "^[^#]*trap[[:space:]].*[[:space:]]ERR([[:space:]]|$)" "$f" || continue
    checked=$(( checked + 1 ))
    rel="${f#"${INFRA}"/}"
    # The script's own `set -<flags>` line(s), before the trap: one of them
    # must carry E (as in `set -Eeuo pipefail`).
    if grep -qE '^set -[a-zA-Z]*E[a-zA-Z]*([[:space:]]|$)' "$f"; then
        printf 'ok   - %s: ERR trap with errtrace (-E)\n' "$rel"
    else
        printf 'FAIL - %s: sets an ERR trap but not -E (it would not fire inside functions)\n' "$rel"
        fails=$(( fails + 1 ))
    fi
done

printf -- '--------------------------------------------------------------\n'
if [[ "$checked" -lt 3 ]]; then
    # install.sh, update.sh and install-ops-tools.sh all trap ERR today.
    printf 'FAIL - found only %d script(s) with an ERR trap; the scan is broken\n' "$checked"
    exit 1
fi
if [[ "$fails" -eq 0 ]]; then
    printf 'PASS - every ERR trap also covers functions and subshells (%d scripts)\n' "$checked"
else
    exit 1
fi
