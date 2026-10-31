#!/usr/bin/env bash
# update.sh and sync-from.sh run as root and source / execute files from their
# own tree, so they must refuse a tree the service user (or anyone else) could
# have rewritten -- above all the deployed copy under /opt/wsi, as install.sh,
# set-workers.sh and set-cache-size.sh do -- and update.sh must make that
# check before it sources lib/wsi-common.sh.
#
# Run: bash infra/tests/test_root_guard.sh   (the ownership case needs root;
# CI runs this file under sudo)
set -uo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
REPO="$(cd "${HERE}/../.." && pwd)"
fails=0
expect() {  # expect LABEL EXPECTED ACTUAL
    if [[ "$2" == "$3" ]]; then printf 'ok   - %s\n' "$1"
    else printf 'FAIL - %s: expected %q, got %q\n' "$1" "$2" "$3"; fails=$(( fails + 1 )); fi
}

TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

# A scratch "checkout": just the scripts and the lib the guard looks at.
make_tree() {  # make_tree DIR
    mkdir -p "$1/infra/lib" "$1/infra/systemd" "$1/Makefile.d"
    cp "$REPO/infra/update.sh" "$REPO/infra/sync-from.sh" "$REPO/infra/install.sh" "$1/infra/"
    cp "$REPO/infra/lib/"*.sh "$REPO/infra/lib/"*.py "$1/infra/lib/"
    chmod 755 "$1" "$1/infra" "$1/infra/lib"
}

run() {  # run SCRIPT_PATH [ENV=VAL ...] : prints the combined output
    local script="$1"; shift
    ( cd / && env "$@" bash "$script" 2>&1 </dev/null ) | head -20
}

for name in update.sh sync-from.sh; do
    # 1. Inside the deployed copy: refused, before anything else is read.
    make_tree "$TMP/opt-wsi"
    out="$(run "$TMP/opt-wsi/infra/$name" APP_HOME="$TMP/opt-wsi")"
    expect "$name: refuses to run from inside APP_HOME" "yes" \
        "$(grep -q "refusing to run.*it is inside $TMP/opt-wsi, the service user's deployed copy" <<<"$out" && echo yes)"
    expect "$name: ...and tells the operator to use their clone" "yes" \
        "$(grep -q "cd ~/wsi && sudo bash infra/$name" <<<"$out" && echo yes)"
    # ... also through a symlink to it.
    ln -sfn "$TMP/opt-wsi" "$TMP/link-to-opt"
    out="$(run "$TMP/link-to-opt/infra/$name" APP_HOME="$TMP/opt-wsi")"
    expect "$name: a symlink into APP_HOME is caught too" "yes" \
        "$(grep -q "refusing to run" <<<"$out" && echo yes)"

    # 2. A world-writable helper: refused.
    make_tree "$TMP/clone-$name"
    chmod 666 "$TMP/clone-$name/infra/lib/wsi-common.sh"
    out="$(run "$TMP/clone-$name/infra/$name" APP_HOME="$TMP/not-this")"
    expect "$name: a world-writable lib/wsi-common.sh is refused" "yes" \
        "$(grep -q "wsi-common.sh is writable by every user" <<<"$out" && echo yes)"

    # 3. Owned by an account that is neither root nor the operator (root only).
    if [[ "$EUID" -eq 0 ]] && id nobody >/dev/null 2>&1; then
        make_tree "$TMP/owned-$name"
        chown nobody "$TMP/owned-$name/infra/lib/wsi-common.sh"
        out="$(run "$TMP/owned-$name/infra/$name" APP_HOME="$TMP/not-this" SUDO_UID=)"
        expect "$name: a helper owned by another account is refused" "yes" \
            "$(grep -q "wsi-common.sh is owned by nobody, neither root nor you" <<<"$out" && echo yes)"
    else
        printf 'note - %s: ownership case not exercised (needs root)\n' "$name"
    fi

    # 3b. The same ownership is fine for the operator who ran sudo (SUDO_UID) --
    #     CI and every real run have a clone that is not root's -- and for the
    #     account the login session belongs to when sudo left no SUDO_UID
    #     (`sudo su -`): the stub logname names `nobody`.
    if [[ "$EUID" -eq 0 ]] && id nobody >/dev/null 2>&1; then
        nobody_uid="$(id -u nobody)"
        mkdir -p "$TMP/stub-logname"
        printf '#!/bin/sh\necho nobody\n' > "$TMP/stub-logname/logname"; chmod +x "$TMP/stub-logname/logname"
        out="$(run "$TMP/owned-$name/infra/$name" APP_HOME="$TMP/not-this" SUDO_UID="$nobody_uid")"
        expect "$name: a helper owned by the account that ran sudo passes the guard" "no" \
            "$(grep -q "refusing to run" <<<"$out" && echo yes || echo no)"
        out="$(run "$TMP/owned-$name/infra/$name" APP_HOME="$TMP/not-this" SUDO_UID= PATH="$TMP/stub-logname:$PATH")"
        expect "$name: ... and by the login session's account when there is no SUDO_UID" "no" \
            "$(grep -q "refusing to run" <<<"$out" && echo yes || echo no)"
    fi

    # 3c. Every file root goes on to run, and the script itself, is checked: a
    #     world-writable one is refused even when the rest of the tree is fine.
    other=install.sh; [[ "$name" == sync-from.sh ]] && other=lib/replica_db.py
    for victim in "$name" "$other"; do
        make_tree "$TMP/w-$name-${victim//\//_}"
        chmod 666 "$TMP/w-$name-${victim//\//_}/infra/$victim"
        out="$(run "$TMP/w-$name-${victim//\//_}/infra/$name" APP_HOME="$TMP/not-this")"
        expect "$name: a world-writable $victim is refused" "yes" \
            "$(grep -q "$victim is writable by every user" <<<"$out" && echo yes)"
    done

    # 4. A proper clone passes the guard (it then fails later for its own
    #    reasons -- no git repo, not root-able here -- but not on the guard).
    make_tree "$TMP/ok-$name"
    out="$(run "$TMP/ok-$name/infra/$name" APP_HOME="$TMP/not-this")"
    expect "$name: a clone of the operator's own passes the guard" "no" \
        "$(grep -q "refusing to run" <<<"$out" && echo yes || echo no)"
done

# 5. The deployed copy is refused even when APP_HOME is not set: /opt/wsi is
#    built in. The guard block is lifted out of each script and run with that
#    path moved into the scratch tree -- the test fails if the literal is gone.
for name in update.sh sync-from.sh; do
    block="$(awk '/^_guard_refuse\(\) \{/ {on=1} on {print} /^unset _guard_repo_real/ {exit}' "$REPO/infra/$name")"
    expect "$name: the guard block has /opt/wsi built in" "1" "$(grep -c 'for _guard_home in /opt/wsi ' <<<"$block")"
    mkdir -p "$TMP/builtin-$name/opt-wsi/infra" "$TMP/builtin-$name/clone/infra"
    for tree in opt-wsi clone; do
        for f in lib/wsi-common.sh lib/replica_db.py install.sh; do mkdir -p "$TMP/builtin-$name/$tree/infra/lib"; cp "$REPO/infra/$f" "$TMP/builtin-$name/$tree/infra/$f"; done
        chmod 755 "$TMP/builtin-$name" "$TMP/builtin-$name/$tree" "$TMP/builtin-$name/$tree/infra" "$TMP/builtin-$name/$tree/infra/lib"
        {
            echo 'set -Eeuo pipefail'
            echo "REPO_DIR=\"$TMP/builtin-$name/$tree\""
            echo "${block//\/opt\/wsi/$TMP/builtin-$name/opt-wsi}"
            echo 'echo PASSED-THE-GUARD'
        } > "$TMP/builtin-$name/$tree/infra/$name"
        chmod 755 "$TMP/builtin-$name/$tree/infra/$name"
    done
    out="$(cd / && env -u APP_HOME SUDO_UID="${SUDO_UID:-}" bash "$TMP/builtin-$name/opt-wsi/infra/$name" 2>&1 </dev/null)"
    expect "$name: a tree inside the built-in /opt/wsi is refused with APP_HOME unset" "yes" \
        "$(grep -q "is inside $TMP/builtin-$name/opt-wsi" <<<"$out" && echo yes)"
    out="$(cd / && env -u APP_HOME SUDO_UID="${SUDO_UID:-}" bash "$TMP/builtin-$name/clone/infra/$name" 2>&1 </dev/null)"
    expect "$name: ... and a tree outside it is not" "PASSED-THE-GUARD" "$(grep -o PASSED-THE-GUARD <<<"$out")"
done

printf -- '--------------------------------------------------------------\n'
if [[ "$fails" -eq 0 ]]; then
    printf 'PASS - update.sh and sync-from.sh refuse a tree the service user could have rewritten\n'
else
    exit 1
fi
