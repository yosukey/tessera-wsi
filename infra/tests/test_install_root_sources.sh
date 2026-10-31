#!/usr/bin/env bash
# Tests that install.sh never feeds root a file from the service user's tree.
# Run: bash infra/tests/test_install_root_sources.sh   (checks 3-5 need root +
# sudo + a "nobody" account and are SKIPped without them:
#   sudo bash infra/tests/test_install_root_sources.sh)
#
# WHY THIS EXISTS
# ---------------
# $APP_HOME (/opt/wsi) is owned by the service user, and install.sh runs pip,
# `npm ci` and `npm run build` there AS that user before it installs the
# systemd units (including the root-run wsi-restart.service), the
# wsi-cache-stat binary, the nginx configs and the wsi.env template. Any
# package's install hook could therefore rewrite $APP_HOME/infra/* first, so
# root must install those from REPO_DIR (the operator's clone) — and must
# refuse to run with REPO_DIR inside $APP_HOME. Likewise root must never chmod
# / install onto a path under $APP_HOME: both follow a symlink the service
# user swaps in (install(1) sets the mode with a path-based fchmodat()).
#
# The same goes for the copy of the clone into $APP_HOME: rsync's receiver
# works by path, so as root it would write, chmod and --delete through a
# directory the service user swaps for a symlink while it runs. The service
# user does that copy, from a staging copy root makes in a private directory
# (the clone itself is normally unreadable to it: Ubuntu homes are 0750).
#
# Five checks:
#   1. static: no root command in install.sh reads from "$APP_HOME/infra/…",
#      chmods / install(1)s onto a path under $APP_HOME, rsyncs into it,
#      chown -Rs it, or reads a file in it;
#   2. the REPO_DIR-inside-APP_HOME guard, extracted from the real script;
#   3. the VERSION write, extracted from the real script and run against a
#      pre-planted symlink (needs root);
#   4. sync_repo_to_app_home, extracted and run from a clone the service user
#      cannot read, over a deployed tree with planted links — including with
#      rsync forced to act through them, the worst a mid-run swap can do
#      (needs root);
#   5. the read of the previous VERSION, through a planted link (needs root).
set -uo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
INSTALL_SH="${HERE}/../install.sh"

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

# ---- 1. static scan --------------------------------------------------------
# Code lines only (comments stripped), with backslash continuations joined so
# a multi-line `install ... \` is one logical line. Anything run through
# as_app_user / `sudo -u` executes as the service user and is fine.
CODE="$(sed -E 's/^[[:space:]]*#.*$//' "$INSTALL_SH" | sed -e ':a' -e '/\\$/N; s/\\\n//; ta')"
root_lines() { grep -E "$1" <<<"$CODE" | grep -Ev 'as_app_user|sudo -u' || true; }

expect "no root command reads \"\$APP_HOME/infra/...\"" "" \
       "$(root_lines '"\$APP_HOME/infra/')"
expect "no root chmod on a path under \$APP_HOME" "" \
       "$(root_lines '(^|[;&|[:space:]])chmod[[:space:]].*\$APP_HOME')"
expect "no root install(1) onto a path under \$APP_HOME" "" \
       "$(root_lines '(^|[;&|[:space:]])install[[:space:]]+-m[^|]*"\$APP_HOME/[^"]*"[[:space:]]*$')"
# The copy into $APP_HOME, and any read of a file in it, are the service
# user's too: a root rsync works by path through links swapped in mid-run,
# and a root read follows a planted VERSION -> /etc/shadow into the log.
expect "no root rsync into \$APP_HOME" "" \
       "$(root_lines '(^|[;&|[:space:]])rsync[[:space:]].*"\$APP_HOME/?"')"
# (A die/warn message may suggest the command to the operator; that is text.)
expect "no root chown -R over \$APP_HOME" "" \
       "$(root_lines '(^|[;&|[:space:]])chown[[:space:]]+-R.*\$APP_HOME' \
          | grep -Ev '(^|[;&|[:space:]])(die|warn|log)[[:space:]]+"' || true)"
expect "no root redirect-read of a file under \$APP_HOME" "" \
       "$(root_lines '<[[:space:]]*"\$APP_HOME/')"
# And the units / binary / nginx configs really come from REPO_DIR.
for f in systemd/wsi-restart.service systemd/wsi-avahi-reload.service \
         systemd/wsi-app.service systemd/wsi-cache-stat.sh \
         nginx/wsi.conf nginx/wsi-locations.conf nginx/wsi-access-log.conf wsi.env.example; do
    n="$(grep -cF "\"\$REPO_DIR/infra/$f\"" <<<"$CODE")"
    expect "infra/$f is installed from REPO_DIR" "yes" "$([[ "$n" -ge 1 ]] && echo yes)"
done
# The shell helpers root sources come from the same trusted tree.
expect "lib/wsi-common.sh is sourced from REPO_DIR" "yes" \
       "$(grep -qF 'WSI_COMMON_SH="$REPO_DIR/infra/lib/wsi-common.sh"' <<<"$CODE" \
          && grep -qE '^\. "\$WSI_COMMON_SH"$' <<<"$CODE" && echo yes)"
expect "nothing is sourced from \$APP_HOME" "" "$(grep -E '^(\.|source)[[:space:]].*APP_HOME' <<<"$CODE" || true)"

# ---- 2. REPO_DIR guard -----------------------------------------------------
GUARD_SRC="$(awk '
    /^_repo_real="/                    { grabbing = 1 }
    grabbing                           { print }
    grabbing && /^unset _repo_real /   { exit }
' "$INSTALL_SH")"
if [[ "$GUARD_SRC" != *'_app_real'* ]]; then
    echo "FAILED: could not extract the REPO_DIR guard from $INSTALL_SH"
    exit 1
fi
TMP_ROOT="$(mktemp -d)"
trap 'rm -rf "$TMP_ROOT"' EXIT
mkdir -p "$TMP_ROOT/opt/wsi/src" "$TMP_ROOT/opt/wsi-other" "$TMP_ROOT/home/clone"
ln -s "$TMP_ROOT/opt/wsi/src" "$TMP_ROOT/home/link-into-app"
guard_dies() {
    # guard_dies REPO_DIR -> "died" / "ok"
    # shellcheck disable=SC2034  # read by the eval'd guard
    local REPO_DIR="$1" APP_HOME="$TMP_ROOT/opt/wsi" died=0
    die() { died=1; }
    eval "$GUARD_SRC"
    [[ "$died" -eq 1 ]] && echo died || echo ok
}
expect "guard: separate clone is accepted"              ok   "$(guard_dies "$TMP_ROOT/home/clone")"
expect "guard: sibling with a common prefix is accepted" ok  "$(guard_dies "$TMP_ROOT/opt/wsi-other")"
expect "guard: REPO_DIR == APP_HOME is refused"         died "$(guard_dies "$TMP_ROOT/opt/wsi")"
expect "guard: REPO_DIR inside APP_HOME is refused"     died "$(guard_dies "$TMP_ROOT/opt/wsi/src")"
expect "guard: symlink into APP_HOME is refused"        died "$(guard_dies "$TMP_ROOT/home/link-into-app")"

# ---- 3. VERSION write as the service user ---------------------------------
if [[ ${EUID:-$(id -u)} -ne 0 ]] || ! command -v sudo >/dev/null 2>&1 \
   || ! id nobody >/dev/null 2>&1; then
    printf 'SKIP - VERSION write (needs root, sudo and a nobody account)\n'
else
    VER_SRC="$(awk '
        /^[[:space:]]*printf .*DEPLOYED_VER.* \| sudo -u / { grabbing = 1 }
        grabbing                                          { print }
        grabbing && /sh "\$APP_HOME"[[:space:]]*$/        { exit }
    ' "$INSTALL_SH")"
    if [[ "$VER_SRC" != *'mv -fT'* ]]; then
        echo "FAILED: could not extract the VERSION write from $INSTALL_SH"
        exit 1
    fi
    chmod 0755 "$TMP_ROOT"
    # shellcheck disable=SC2034  # APP_USER / DEPLOYED_VER are read by the eval'd snippet
    APP_USER=nobody
    APP_HOME="$TMP_ROOT/apphome"
    install -d -m 0755 -o nobody -g "$(id -gn nobody)" "$APP_HOME"
    VICTIM="$TMP_ROOT/victim"
    printf 'secret\n' > "$VICTIM"; chmod 0600 "$VICTIM"
    ln -s "$VICTIM" "$APP_HOME/VERSION"    # planted by the service user
    # shellcheck disable=SC2034
    DEPLOYED_VER="v9.9.9"
    eval "$VER_SRC"
    expect "VERSION: written with the deployed ref"  "v9.9.9"      "$(cat "$APP_HOME/VERSION" 2>/dev/null)"
    expect "VERSION: a regular file, not the link"   "yes"         "$([[ -f "$APP_HOME/VERSION" && ! -L "$APP_HOME/VERSION" ]] && echo yes)"
    expect "VERSION: owned by the service user, 0644" "nobody:644" "$(stat -c '%U:%a' "$APP_HOME/VERSION")"
    expect "VERSION: link target untouched"          "root:600:secret" \
           "$(stat -c '%U:%a' "$VICTIM"):$(cat "$VICTIM")"
    expect "VERSION: no temp file left behind"       "0" \
           "$(find "$APP_HOME" -name '.VERSION.*' | wc -l | tr -d ' ')"
fi

# ---- 4. the clone -> $APP_HOME copy, as the service user -------------------
if [[ ${EUID:-$(id -u)} -ne 0 ]] || ! command -v sudo >/dev/null 2>&1 \
   || ! id nobody >/dev/null 2>&1 || ! command -v rsync >/dev/null 2>&1; then
    printf 'SKIP - repo sync (needs root, sudo, rsync and a nobody account)\n'
else
    SYNC_SRC="$(awk '
        /^sync_repo_to_app_home\(\) \{$/ { grabbing = 1 }
        grabbing                         { print }
        grabbing && /^\}$/               { exit }
    ' "$INSTALL_SH")"
    if [[ "$SYNC_SRC" != *'sudo -u "$APP_USER" rsync'* ]]; then
        echo "FAILED: could not extract sync_repo_to_app_home() from $INSTALL_SH"
        exit 1
    fi
    chmod 0755 "$TMP_ROOT"
    S="$TMP_ROOT/sync"
    install -d -m 0755 "$S" "$S/bin"
    # shellcheck disable=SC2034  # read by the eval'd function
    APP_USER=nobody
    APP_GROUP="$(id -gn nobody)"
    warn() { :; }
    # The operator's clone, inside a home the service user cannot enter.
    install -d -m 0700 "$S/home"
    REPO_DIR="$S/home/clone"
    mkdir -p "$REPO_DIR/services/app" "$REPO_DIR/infra" "$REPO_DIR/.git" \
             "$REPO_DIR/frontend/node_modules/pkg" "$REPO_DIR/data"
    echo 'new code' > "$REPO_DIR/services/app/main.py"
    echo 'unit'     > "$REPO_DIR/infra/x.sh"
    echo 'private'  > "$REPO_DIR/infra/private.conf"; chmod 0600 "$REPO_DIR/infra/private.conf"
    touch "$REPO_DIR/.git/HEAD" "$REPO_DIR/frontend/node_modules/pkg/i.js" "$REPO_DIR/data/slide.svs"
    # The deployed tree a previous install left, as the service user owns it.
    APP_HOME="$S/apphome"
    install -d -m 0755 -o nobody -g "$APP_GROUP" "$APP_HOME"
    as_nobody() { command sudo -u nobody "$@"; }
    as_nobody mkdir -p "$APP_HOME/venv/bin" "$APP_HOME/.npm" "$APP_HOME/services/app/static.rel.1"
    as_nobody touch "$APP_HOME/venv/bin/python" "$APP_HOME/.npm/cached.tgz" \
                    "$APP_HOME/services/app/static.rel.1/index.html" "$APP_HOME/stale.py"
    as_nobody ln -s static.rel.1 "$APP_HOME/services/app/static"
    # ... with a link planted where the clone has a directory.
    VICTIM="$S/victim"; install -d -m 0700 "$VICTIM"
    echo keep > "$VICTIM/keep"; chmod 0600 "$VICTIM/keep"
    as_nobody ln -s "$VICTIM" "$APP_HOME/infra"

    # Every rsync goes through a logging stub: who ran it, into what. With
    # $S/follow present it adds -K (--keep-dirlinks) to the copy into
    # $APP_HOME, so rsync acts THROUGH a planted directory link — the worst a
    # link swapped in mid-transfer can make it do.
    REAL_RSYNC="$(command -v rsync)"
    : > "$S/rsync.log"; chmod 0666 "$S/rsync.log"
    cat > "$S/bin/rsync" <<SH
#!/usr/bin/env bash
dest="\${!#}"
printf '%s %s\n' "\$(id -un)" "\$dest" >> "$S/rsync.log"
if [[ -e "$S/follow" && "\$dest" == "$APP_HOME/" ]]; then set -- -K "\$@"; fi
exec "$REAL_RSYNC" "\$@"
SH
    chmod 0755 "$S/bin/rsync"
    # sudo resets PATH (secure_path), so route the service user's rsync to
    # the stub explicitly; root's plain `rsync` finds it through PATH.
    sudo() {
        if [[ "$1" == -u && "${3:-}" == rsync ]]; then
            local u="$2"; shift 3
            command sudo -u "$u" "$S/bin/rsync" "$@"
        else
            command sudo "$@"
        fi
    }
    eval "$SYNC_SRC"

    # From a cwd the service user cannot read, as install.sh may be run.
    rc="$(cd "$S/home" && PATH="$S/bin:$PATH" sync_repo_to_app_home >/dev/null 2>&1; echo $?)"
    expect "sync: succeeds"                               "0"     "$rc"
    expect "sync: exactly two rsyncs"                     "2"     "$(wc -l < "$S/rsync.log" | tr -d ' ')"
    expect "sync: root only writes its private staging dir" "root:yes" \
           "$(awk 'NR==1 {print $1 ":" ($2 ~ "^/tmp/wsi-sync\\.[^/]+/tree/$" ? "yes" : $2)}' "$S/rsync.log")"
    expect "sync: the copy into \$APP_HOME runs as the service user" "nobody $APP_HOME/" \
           "$(sed -n 2p "$S/rsync.log")"
    stage="$(awk 'NR==1 {sub("/tree/$", "", $2); print $2}' "$S/rsync.log")"
    expect "sync: the staging copy is removed"            "gone"  "$([[ -e "$stage" ]] && echo there || echo gone)"
    expect "sync: code updated"                           "new code" "$(cat "$APP_HOME/services/app/main.py" 2>/dev/null)"
    expect "sync: a 0600 file of the clone still arrives" "nobody:600:private" \
           "$(stat -c '%U:%a' "$APP_HOME/infra/private.conf" 2>/dev/null):$(cat "$APP_HOME/infra/private.conf" 2>/dev/null)"
    expect "sync: a planted link becomes the real directory" "dir" \
           "$([[ -d "$APP_HOME/infra" && ! -L "$APP_HOME/infra" ]] && echo dir || echo link)"
    expect "sync: the link's root-owned target is untouched" "root:700|keep" \
           "$(stat -c '%U:%a' "$VICTIM")|$(ls "$VICTIM" | tr '\n' ' ' | sed 's/ $//')"
    expect "sync: stale files are deleted"                "gone"  "$([[ -e "$APP_HOME/stale.py" ]] && echo there || echo gone)"
    expect "sync: venv, caches and static build survive"  "yes" \
           "$([[ -e "$APP_HOME/venv/bin/python" && -e "$APP_HOME/.npm/cached.tgz" \
                 && -e "$APP_HOME/services/app/static/index.html" ]] && echo yes)"
    expect "sync: excluded source dirs are not copied"    "none" \
           "$(ls -d "$APP_HOME/.git" "$APP_HOME/frontend/node_modules" "$APP_HOME/data" 2>/dev/null || echo none)"
    expect "sync: everything in \$APP_HOME is the service user's" "" \
           "$(find "$APP_HOME" ! -user nobody -printf '%p\n')"

    # Worst case: rsync made to act through a directory link (as a swap in
    # mid-transfer would) — as the service user it can neither write into nor
    # --delete from a root-owned target.
    : > "$S/rsync.log"
    touch "$S/follow"
    as_nobody rm -rf "$APP_HOME/services"
    as_nobody ln -s "$VICTIM" "$APP_HOME/services"
    rc="$(cd "$S/home" && PATH="$S/bin:$PATH" sync_repo_to_app_home >/dev/null 2>&1; echo $?)"
    expect "sync through a link: reported as a failure"   "yes"   "$([[ "$rc" != 0 ]] && echo yes)"
    expect "sync through a link: root-owned target untouched" "root:700|keep|root:600:keep" \
           "$(stat -c '%U:%a' "$VICTIM")|$(ls "$VICTIM" | tr '\n' ' ' | sed 's/ $//')|$(stat -c '%U:%a' "$VICTIM/keep"):$(cat "$VICTIM/keep")"
    rm -f "$S/follow"
    unset -f sudo

    # ---- 5. the previous VERSION, read as the service user -----------------
    PREV_SRC="$(awk '
        /^PREV_DEPLOYED_VER=/ { grabbing = 1 }
        grabbing              { print }
        grabbing && /\|\| true\)"[[:space:]]*$/ { exit }
    ' "$INSTALL_SH")"
    if [[ "$PREV_SRC" != *'sudo -u "$APP_USER"'* ]]; then
        echo "FAILED: could not extract the previous-VERSION read from $INSTALL_SH"
        exit 1
    fi
    printf 'v1.2.3\n' | as_nobody tee "$APP_HOME/VERSION" >/dev/null
    eval "$PREV_SRC"
    expect "previous VERSION: read"                       "v1.2.3" "$PREV_DEPLOYED_VER"
    as_nobody rm -f "$APP_HOME/VERSION"
    as_nobody ln -s "$VICTIM/keep" "$APP_HOME/VERSION"
    eval "$PREV_SRC"
    expect "previous VERSION: a planted link to a root-only file reads as nothing" "" \
           "$PREV_DEPLOYED_VER"
fi

printf -- '--------------------------------------------------------------\n'
if [[ "$fails" -eq 0 ]]; then
    printf 'PASS - install.sh installs root-consumed files from REPO_DIR, never through $APP_HOME\n'
else
    exit 1
fi
