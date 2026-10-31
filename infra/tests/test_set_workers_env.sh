#!/usr/bin/env bash
# Tests for the env-file rewrite behind infra/set-workers.sh and
# infra/set-cache-size.sh: infra/lib/envfile_set.py, which both call.
# Run: bash infra/tests/test_set_workers_env.sh
#
# Both scripts rewrite /etc/wsi/wsi.env through that helper. Its
# `matches_key()` must not treat ANY line beginning (after '#') with `KEY=` as
# the setting, or the documentation line
#     # UVICORN_WORKERS=16 on a 4-core host still starts 4 workers.
# in wsi.env.example is deleted and the example line above it rewritten. The
# scripts are interactive, so the tests run the helper with the arguments each
# script passes on a temp copy of infra/wsi.env.example (the file a fresh
# install starts from), then diff to prove only the intended line changed —
# after checking that each script really calls the helper that way — and end
# with one run of each script, its prompts answered from a pipe — and with
# the runs each script must refuse: from a tree root should not execute code
# from (the deployed copy under the app home, a tree another account can write).
set -uo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
INFRA="$(cd "${HERE}/.." && pwd)"
EXAMPLE="${INFRA}/wsi.env.example"

command -v python3 >/dev/null 2>&1 || { echo "SKIP - python3 not available"; exit 0; }

WORK="$(mktemp -d)"
trap 'rm -rf "${WORK}"' EXIT

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

HELPER="${INFRA}/lib/envfile_set.py"
[[ -f "${HELPER}" ]] || { echo "FAIL - ${HELPER} not found"; exit 1; }

# The scripts call the shared helper (with these --tool / --heading values),
# and carry no rewriter of their own that could drift from it.
for pair in "set-workers.sh|Uvicorn workers — set by set-workers.sh" \
            "set-cache-size.sh|WSI tile cache — set by set-cache-size.sh"; do
    script="${pair%%|*}" heading="${pair#*|}"
    body="$(sed -e ':a' -e '/\\$/N; s/\\\n//; ta' "${INFRA}/${script}")"
    expect "${script}: calls lib/envfile_set.py as itself" "yes" \
        "$(grep -F 'python3 -I "${SELF_DIR}/lib/envfile_set.py" "$ENV_FILE"' <<<"$body" \
           | grep -F -- "--tool ${script} --heading \"${heading}\"" >/dev/null && echo yes)"
    expect "${script}: has no embedded rewriter" "0" "$(grep -c 'def matches_key' "${INFRA}/${script}")"
done

# The invocations the two scripts make (see above).
workers() {  # workers FILE KEY VALUE OTHER_KEY
    python3 -I "${HELPER}" "$1" "$2" "$3" --comment-out "$4" \
        --tool set-workers.sh --heading "Uvicorn workers — set by set-workers.sh"
}
cache() {    # cache FILE VALUE
    python3 -I "${HELPER}" "$1" DZI_CACHE_SIZE "$2" \
        --tool set-cache-size.sh --heading "WSI tile cache — set by set-cache-size.sh"
}

# fresh_copy NAME : a temp copy of wsi.env.example, path echoed.
fresh_copy() { cp "${EXAMPLE}" "${WORK}/$1"; printf '%s' "${WORK}/$1"; }

# changed ORIG NEW : the diff body, one `< old` / `> new` line each.
changed() { diff "$1" "$2" | grep -E '^[<>]' || true; }

# --- set-workers.sh against wsi.env.example --------------------------------
f="$(fresh_copy w1.env)"
workers "$f" UVICORN_WORKERS 6 UVICORN_WORKERS_PERCENT
expect "workers: exactly one line replaced in wsi.env.example" 2 "$(changed "${EXAMPLE}" "$f" | wc -l)"
expect "workers: the example assignment becomes the live setting" \
    "> UVICORN_WORKERS=6" "$(changed "${EXAMPLE}" "$f" | grep '^>')"
expect "workers: the documentation line survives" 1 \
    "$(grep -c '^# UVICORN_WORKERS=16 on a 4-core host still starts 4 workers\.$' "$f")"
expect "workers: only one live UVICORN_WORKERS line" 1 "$(grep -cE '^UVICORN_WORKERS=' "$f")"

f="$(fresh_copy w2.env)"
workers "$f" UVICORN_WORKERS_PERCENT 75 UVICORN_WORKERS
expect "percent: exactly one line replaced in wsi.env.example" 2 "$(changed "${EXAMPLE}" "$f" | wc -l)"
expect "percent: the commented example becomes the live setting" \
    "> UVICORN_WORKERS_PERCENT=75" "$(changed "${EXAMPLE}" "$f" | grep '^>')"

# --- set-workers.sh against a hand-written file ----------------------------
f="${WORK}/w3.env"
cat > "$f" <<'ENV'
FOO=bar
UVICORN_WORKERS = 2
# UVICORN_WORKERS=16 on a 4-core host still starts 4 workers.
UVICORN_WORKERS_PERCENT=50
#UVICORN_WORKERS=9
ENV
workers "$f" UVICORN_WORKERS 4 UVICORN_WORKERS_PERCENT
expect "workers: active line replaced, doc line kept, other key commented out, \
commented duplicate dropped" \
"FOO=bar|UVICORN_WORKERS=4|# UVICORN_WORKERS=16 on a 4-core host still starts 4 workers.|#UVICORN_WORKERS_PERCENT=(commented out by set-workers.sh)" \
    "$(paste -sd'|' "$f")"

f="${WORK}/w4.env"
printf 'FOO=bar\n# UVICORN_WORKERS=16 on a 4-core host still starts 4 workers.\n' > "$f"
workers "$f" UVICORN_WORKERS 4 UVICORN_WORKERS_PERCENT
expect "workers: a doc-only file gets the setting appended instead" 1 \
    "$(grep -c '^UVICORN_WORKERS=4$' "$f")"
expect "workers: ... and keeps its doc line" 1 \
    "$(grep -c '^# UVICORN_WORKERS=16 on a 4-core host' "$f")"

# Switching UVICORN_WORKERS <-> UVICORN_WORKERS_PERCENT back and forth must not
# stack a new marker line and heading on every round trip.
f="${WORK}/w4.env"
printf 'UVICORN_WORKERS=4\n' > "$f"
for _ in 1 2 3; do
    workers "$f" UVICORN_WORKERS_PERCENT 50 UVICORN_WORKERS
    workers "$f" UVICORN_WORKERS 4 UVICORN_WORKERS_PERCENT
done
expect "workers: round trips leave exactly one live UVICORN_WORKERS line" 1 \
    "$(grep -c '^UVICORN_WORKERS=4$' "$f")"
expect "workers: ... no live PERCENT line" 0 "$(grep -c '^UVICORN_WORKERS_PERCENT=' "$f")"
expect "workers: ... the 'commented out' marker does not accumulate" 1 \
    "$(grep -c 'commented out by set-workers.sh' "$f")"
expect "workers: ... nor the 'set by set-workers.sh' heading" 1 \
    "$(grep -c 'set by set-workers.sh' "$f")"

# --- set-cache-size.sh ------------------------------------------------------
f="$(fresh_copy c1.env)"
cache "$f" 12
expect "cache: exactly one line replaced in wsi.env.example" 2 "$(changed "${EXAMPLE}" "$f" | wc -l)"
expect "cache: the commented example becomes the live setting" \
    "> DZI_CACHE_SIZE=12" "$(changed "${EXAMPLE}" "$f" | grep '^>')"

f="${WORK}/c2.env"
cat > "$f" <<'ENV'
# Note: each worker has its own WSI tile cache (DZI_CACHE_SIZE entries).
# DZI_CACHE_SIZE=8 is the default and fine for most hosts.
#DZI_CACHE_SIZE=8
ENV
cache "$f" 12
expect "cache: prose and doc lines are untouched" \
    "# Note: each worker has its own WSI tile cache (DZI_CACHE_SIZE entries).|# DZI_CACHE_SIZE=8 is the default and fine for most hosts.|DZI_CACHE_SIZE=12" \
    "$(paste -sd'|' "$f")"

# --- the scripts themselves, answering their prompts ------------------------
# Proves each one finds the helper relative to itself and passes its answers
# through. A stub systemctl reports the service as stopped, so neither offers
# a restart.
mkdir -p "${WORK}/bin"
printf '#!/bin/sh\nexit 3\n' > "${WORK}/bin/systemctl"
chmod +x "${WORK}/bin/systemctl"
f="$(fresh_copy e1.env)"
printf '9\ny\n' | PATH="${WORK}/bin:$PATH" WSI_ENV="$f" \
    bash "${INFRA}/set-cache-size.sh" >"${WORK}/e1.log" 2>&1
expect "set-cache-size.sh run: writes the answer" "> DZI_CACHE_SIZE=9" \
    "$(changed "${EXAMPLE}" "$f" | grep '^>')"
f="$(fresh_copy e2.env)"
printf '1\n1\ny\n' | PATH="${WORK}/bin:$PATH" WSI_ENV="$f" \
    bash "${INFRA}/set-workers.sh" >"${WORK}/e2.log" 2>&1
expect "set-workers.sh run: writes the answer" "> UVICORN_WORKERS=1" \
    "$(changed "${EXAMPLE}" "$f" | grep '^>')"

# --- an env file that is a symlink stays one ------------------------------
# wsi.env may live in a config-managed directory and be linked into /etc/wsi:
# the rename must land on the file it points to (mode kept), not replace the link.
mkdir -p "${WORK}/managed"
cp "${EXAMPLE}" "${WORK}/managed/real.env"
chmod 640 "${WORK}/managed/real.env"
ln -s "${WORK}/managed/real.env" "${WORK}/linked.env"
python3 -I "${INFRA}/lib/envfile_set.py" "${WORK}/linked.env" DZI_CACHE_SIZE 7 \
    --tool set-cache-size.sh --heading "DZI tile cache size (GiB)" >/dev/null 2>&1
expect "symlinked env file: still a symlink" "yes" \
    "$([[ -L "${WORK}/linked.env" ]] && echo yes || echo no)"
expect "symlinked env file: the target holds the change" "> DZI_CACHE_SIZE=7" \
    "$(changed "${EXAMPLE}" "${WORK}/managed/real.env" | grep '^>')"
expect "symlinked env file: the target keeps its mode" "640" \
    "$(stat -c '%a' "${WORK}/managed/real.env")"
expect "symlinked env file: no temp file left beside the target" "real.env" \
    "$(ls "${WORK}/managed")"

# --- ... but not from a tree root should not trust ------------------------
# Run as root, both scripts execute files from their own directory
# (systemd/wsi-calc.sh, lib/envfile_set.py), so they refuse a directory the
# service user — or anyone but root and the operator — could have rewritten:
# the deployed copy under the app home (directly or through a symlink), a
# world-writable tree, a tree owned by another account. The refusal comes
# before anything is read from the tree and before any prompt, so the env
# file is untouched. APP_HOME stands in for /opt/wsi here.
# copy_tree DIR : the files the two scripts use, laid out as in infra/.
copy_tree() {
    mkdir -p "$1/systemd" "$1/lib"
    cp "${INFRA}/set-workers.sh" "${INFRA}/set-cache-size.sh" "$1/"
    cp "${INFRA}/systemd/wsi-calc.sh" "$1/systemd/"
    cp "${INFRA}/lib/envfile_set.py" "$1/lib/"
}
# In the deployed copy the helpers are booby-trapped: they leave a marker if
# root ever reads them.
DEPLOYED="${WORK}/opt-wsi"
copy_tree "${DEPLOYED}/infra"
printf 'touch %q\n' "${WORK}/ran-deployed-code" >> "${DEPLOYED}/infra/systemd/wsi-calc.sh"
printf 'open("%s", "w").close()\n' "${WORK}/ran-deployed-code" >> "${DEPLOYED}/infra/lib/envfile_set.py"
mkdir -p "${WORK}/links"
ln -s "${DEPLOYED}/infra/set-workers.sh" "${WORK}/links/set-workers.sh"
ln -s "${DEPLOYED}/infra/set-cache-size.sh" "${WORK}/links/set-cache-size.sh"
CLONE="${WORK}/clone/infra"
copy_tree "${CLONE}"

# refused LABEL WHY SCRIPT_PATH [ENV...] : runs the script with answers that
# would write, and checks it stopped with the refusal (naming WHY) instead.
refused() {
    local label="$1" why="$2" script="$3" f out rc answers=$'1\n1\ny\n'
    shift 3
    [[ "${script##*/}" == set-cache-size.sh ]] && answers=$'9\ny\n'
    f="$(fresh_copy refused.env)"
    out="$(printf '%s' "${answers}" | env PATH="${WORK}/bin:$PATH" WSI_ENV="$f" "$@" \
        bash "${script}" 2>&1)"; rc=$?
    expect "${label}: exits 1" 1 "$rc"
    expect "${label}: says why (${why})" yes \
        "$(grep -F -- "${why}" <<<"$out" | grep -qF 'refusing to run' && echo yes)"
    expect "${label}: tells the operator to use the clone" yes \
        "$(grep -qF 'Run it from your clone instead' <<<"$out" && echo yes)"
    expect "${label}: asks nothing" no \
        "$(grep -qE 'Choice|Enter new' <<<"$out" && echo yes || echo no)"
    expect "${label}: env file untouched" "" "$(changed "${EXAMPLE}" "$f")"
}

for s in set-workers.sh set-cache-size.sh; do
    refused "${s} in the app home" "is inside ${DEPLOYED}" \
        "${DEPLOYED}/infra/${s}" APP_HOME="${DEPLOYED}"
    refused "${s} linked into the app home" "is inside ${DEPLOYED}" \
        "${WORK}/links/${s}" APP_HOME="${DEPLOYED}"
done
expect "no code from the app-home copy ran" no \
    "$([[ -e "${WORK}/ran-deployed-code" ]] && echo yes || echo no)"

chmod o+w "${CLONE}/lib"
refused "set-workers.sh, world-writable lib/" "${CLONE}/lib is writable by every user" \
    "${CLONE}/set-workers.sh"
refused "set-cache-size.sh, world-writable lib/" "${CLONE}/lib is writable by every user" \
    "${CLONE}/set-cache-size.sh"
chmod o-w "${CLONE}/lib"

if [[ $EUID -eq 0 ]] && id nobody >/dev/null 2>&1; then
    chown nobody "${CLONE}/systemd/wsi-calc.sh" "${CLONE}/lib/envfile_set.py"
    refused "set-workers.sh, wsi-calc.sh owned by another account" \
        "${CLONE}/systemd/wsi-calc.sh is owned by nobody" "${CLONE}/set-workers.sh"
    refused "set-cache-size.sh, envfile_set.py owned by another account" \
        "${CLONE}/lib/envfile_set.py is owned by nobody" "${CLONE}/set-cache-size.sh"
    chown 0 "${CLONE}/systemd/wsi-calc.sh" "${CLONE}/lib/envfile_set.py"
else
    printf 'SKIP - foreign-owner refusal (needs root and a "nobody" user)\n'
fi

# The same copy, once nothing is wrong with it, runs normally: the refusals
# above are about where it lives, not about the copy.
f="$(fresh_copy e3.env)"
printf '1\n1\ny\n' | PATH="${WORK}/bin:$PATH" WSI_ENV="$f" \
    bash "${CLONE}/set-workers.sh" >"${WORK}/e3.log" 2>&1
expect "set-workers.sh from a clone copy: writes the answer" "> UVICORN_WORKERS=1" \
    "$(changed "${EXAMPLE}" "$f" | grep '^>')"
f="$(fresh_copy e4.env)"
printf '9\ny\n' | PATH="${WORK}/bin:$PATH" WSI_ENV="$f" \
    bash "${CLONE}/set-cache-size.sh" >"${WORK}/e4.log" 2>&1
expect "set-cache-size.sh from a clone copy: writes the answer" "> DZI_CACHE_SIZE=9" \
    "$(changed "${EXAMPLE}" "$f" | grep '^>')"

printf '%d/%d checks passed\n' "$(( checks - fails ))" "$checks"
exit $(( fails > 0 ? 1 : 0 ))
