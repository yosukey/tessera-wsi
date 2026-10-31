# shellcheck shell=bash
# Helpers shared by infra/install.sh and infra/update.sh (wsi_env_value,
# wsi_sqlite_db_path, wsi_define_run_git, wsi_latest_release_tag), with the
# Makefile (wsi_url_origin, which install.sh uses too), with infra/nginx-cache.sh
# (wsi_nginx_keys_zone_mb and the two it builds on) and with
# infra/sync-from.sh (wsi_env_value_trimmed, wsi_sqlite_db_path). Sourced,
# never run.
#
# All of them run as root and source this file from their own tree — the
# operator's clone, the same place the sourcing script was read from — never
# from $APP_HOME, which the service user can rewrite (nginx-cache.sh refuses
# such a tree outright). install.sh includes this
# file in its "changed by the checkout?" check, so a tag checkout that changes
# it re-execs install.sh rather than running new code against old helpers.

# wsi_env_value KEY FILE
# Print the value of the last live (uncommented) `KEY=value` line of FILE —
# the one systemd's EnvironmentFile= honours — with surrounding double or
# single quotes removed; print nothing when the key is not set or FILE is
# missing. Whitespace around the '=' is tolerated, as systemd does.
#
# systemd strips either kind of quote: KEY='v' and KEY="v" both reach the
# service as v. Stripping only double quotes would read a single-quoted
# DATABASE_URL back as 'sqlite:////…' (quotes included) — update.sh could
# not resolve it and would update with no DB snapshot — and a single-quoted
# WSI_LAN_HOST would fail install.sh's hostname check, aborting every install.
# The single-quoted form is tried first and, when it matches, `t` ends the
# line's processing, so a value is never unquoted twice. Like systemd, an
# opening quote counts only right after the '=': KEY="'v'" is 'v', quotes
# included, and KEY=it's keeps its apostrophe.
#
# This is a subset of systemd's syntax, not all of it: no backslash escapes,
# and the value may not contain '#' or the quote character it is wrapped in
# ('"' when unquoted). The match is anchored to the end of the line, so a
# line outside that subset is not matched at all — an earlier assignment, or
# nothing, is read instead (install.sh persists only the origin of
# WSI_PUBLIC_BASE_URL, which never holds either character).
wsi_env_value() {
    local key="$1" file="$2"
    [[ -f "$file" ]] || return 0
    sed -n -E \
        -e "s/^[[:space:]]*${key}[[:space:]]*=[[:space:]]*'([^'#]*)'[[:space:]]*\$/\\1/p" \
        -e t \
        -e "s/^[[:space:]]*${key}[[:space:]]*=[[:space:]]*\"?([^\"#]*)\"?[[:space:]]*\$/\\1/p" \
        "$file" | tail -n1
}

# wsi_env_value_trimmed KEY FILE
# wsi_env_value, with every CR and any surrounding whitespace removed: the
# read-back's capture is greedy, so trailing blanks — and the CR of a
# CRLF-edited wsi.env — end up in the value. systemd drops them when it reads
# the file, but a path keeping them "does not exist". For paths read back by
# update.sh and sync-from.sh.
wsi_env_value_trimmed() {
    local v
    v="$(wsi_env_value "$1" "$2")"
    v="${v//$'\r'/}"
    v="${v#"${v%%[![:space:]]*}"}"
    printf '%s\n' "${v%"${v##*[![:space:]]}"}"
}

# wsi_sqlite_db_path ENV_FILE DEFAULT_PATH
# Print the SQLite file ENV_FILE's DATABASE_URL names (the last live
# assignment, trimmed as above), DEFAULT_PATH when it is unset; return 1,
# printing nothing, unless it is an absolute sqlite:////path (a query string
# is dropped). Used by update.sh (the pre-update snapshot) and sync-from.sh.
wsi_sqlite_db_path() {
    local url
    url="$(wsi_env_value_trimmed DATABASE_URL "$1")"
    url="${url:-sqlite:///$2}"
    url="${url%%\?*}"
    [[ "$url" == sqlite:////* ]] || return 1
    printf '%s\n' "${url#sqlite:///}"
}

# wsi_url_origin URL
# Print the origin (scheme://host[:port]) of an http(s) URL; return 1,
# printing nothing, for anything else. WSI_PUBLIC_BASE_URL means exactly this:
# Tessera is only ever served at the root path, so the app reduces the value
# to its origin as well (config.public_base_url_from_env, with the same
# pattern, which the two must keep in step), and the frontend has always used
# `new URL(...).origin` of it. A path, query or fragment is dropped; a value
# with credentials (user@host) is not an origin and is refused. The host is a
# DNS name or IPv4 address, or a bracketed IPv6 literal; a port above 65535
# is refused, as the browser's URL parser does. Surrounding whitespace (a CR
# from a CRLF-edited wsi.env) is ignored, as systemd does.
# Used by install.sh (warning when it drops something) and by `make
# frontend-build` (which refuses anything but a bare origin).
wsi_url_origin() {
    local v="${1//$'\r'/}"
    v="${v#"${v%%[![:space:]]*}"}"
    v="${v%"${v##*[![:space:]]}"}"
    [[ "$v" =~ ^(https?://(\[[0-9A-Fa-f:.]+\]|[A-Za-z0-9.-]+)(:([0-9]{1,5}))?)([/?#].*)?$ ]] \
        || return 1
    [[ -z "${BASH_REMATCH[4]}" ]] || (( 10#${BASH_REMATCH[4]} <= 65535 )) || return 1
    printf '%s\n' "${BASH_REMATCH[1]}"
}

# wsi_define_run_git REPO_DIR
# Define run_git: `git -C REPO_DIR ...` as the user who invoked sudo
# (GIT_AS_USER overrides; GIT_AS_USER=root, or no SUDO_USER, runs it as root),
# with that user's HOME, so the credentials in their ~/.gitconfig,
# ~/.config/gh and ssh keys resolve — root has none, and a private origin
# would prompt for a password. Sets GIT_RUNNER_USER ("root" when it runs as
# root) and GIT_RUNNER_HOME (empty then) for the caller's log line.
#
# `-c safe.directory='*'` defuses git's "dubious ownership" guard: the clone
# is routinely owned by the developer who cloned it, so a git run as root (or
# as another user) would otherwise die with `fatal: detected dubious
# ownership` on its first command.
wsi_define_run_git() {
    WSI_GIT_REPO_DIR="$1"
    GIT_RUNNER_USER="${GIT_AS_USER:-${SUDO_USER:-}}"
    if [[ "$GIT_RUNNER_USER" == "root" || -z "$GIT_RUNNER_USER" ]]; then
        GIT_RUNNER_USER="root"
        GIT_RUNNER_HOME=""
        run_git() { git -c safe.directory='*' -C "$WSI_GIT_REPO_DIR" "$@"; }
    else
        GIT_RUNNER_HOME="$(getent passwd "$GIT_RUNNER_USER" | cut -d: -f6)" \
            || GIT_RUNNER_HOME=""
        [[ -n "$GIT_RUNNER_HOME" ]] || GIT_RUNNER_HOME="/home/$GIT_RUNNER_USER"
        run_git() {
            sudo -u "$GIT_RUNNER_USER" \
                 HOME="$GIT_RUNNER_HOME" \
                 XDG_CONFIG_HOME="$GIT_RUNNER_HOME/.config" \
                 git -c safe.directory='*' -C "$WSI_GIT_REPO_DIR" "$@"
        }
    fi
}

# wsi_latest_release_tag
# Print the highest FINAL release tag of the clone run_git works on — exactly
# vX.Y.Z, nothing after the patch number — or nothing when there is none.
# Needs run_git, i.e. wsi_define_run_git first. This is what install.sh and
# update.sh deploy by default, and they run that release's install.sh as root.
#
# The tag glob alone does not do it: 'v[0-9]*.[0-9]*.[0-9]*' also matches
# v2.0.0-rc1, v2.0.0-beta.1 or v1.2.3.4, and versionsort.suffix only sorts a
# pre-release below its OWN final. With v1.10.0 and v2.0.0-rc1 the rc still
# sorts first, so every host would "update" to it. Only the anchored
# match below decides; a pre-release is deployed only when asked for with
# GIT_REF=<tag>.
#
# awk reads the whole list instead of stopping at the first match (as
# `head -n1` or `grep -m1` would), so git is never killed by SIGPIPE — which
# pipefail would turn into a failed update — and "no such tag" is a clean,
# empty success.
wsi_latest_release_tag() {
    run_git tag -l 'v[0-9]*.[0-9]*.[0-9]*' --sort=-v:refname 2>/dev/null \
        | awk '!found && /^v[0-9]+\.[0-9]+\.[0-9]+$/ { print; found = 1 }'
}

# wsi_origin_transport_warning
# Print a warning (one line, no prefix) when origin of the clone run_git works
# on is fetched over a transport that does not authenticate the server
# (http://, git://, ftp://), and nothing otherwise. Needs run_git, i.e.
# wsi_define_run_git first. install.sh and update.sh both deploy a tag fetched
# from origin and run that release's install.sh as root: over such a transport
# anyone on the network path can rewrite the tag list and the objects, so the
# "release" they supply would be run as root too. https and ssh authenticate
# the server. `git remote get-url` expands url.<base>.insteadOf, so this checks
# the URL git actually fetches from; the scheme is matched case-insensitively.
wsi_origin_transport_warning() {
    local url
    url="$(run_git remote get-url origin 2>/dev/null)" || return 0
    case "${url,,}" in
        http://*|git://*|ftp://*)
            printf '%s\n' "origin is fetched over an unauthenticated transport ($url): anyone on the network path can substitute the release this deploys and run it as root. Use an https:// or ssh URL (git remote set-url origin ...) or sign and verify tags (WSI_VERIFY_RELEASE_TAGS=1)." ;;
    esac
}

# wsi_verify_release_tag REF
# Opt-in: with WSI_VERIFY_RELEASE_TAGS=1, accept REF only when it is a tag whose
# signature `git verify-tag` accepts against the keyring of the user git runs as
# (run_git). Returns 0 when the check is off, or the tag verifies; otherwise
# prints why on stderr and returns 1. Needs run_git, i.e. wsi_define_run_git.
#
# install.sh and update.sh check out the highest vX.Y.Z tag of origin and run
# THAT release's install.sh as root. Nothing but who may push a tag to origin
# stands between a pushed "v9.9.9" and root on every host that updates -- a
# leaked token, a compromised mirror, or a clone made over a transport an
# on-path attacker can rewrite. A signature checked here, with the maintainer's
# public key already in the operator's keyring, moves that trust from the
# server to the key. Off by default because it needs both a signed tag and the
# key: turning it on for a release nobody signed would make every update fail.
# WSI_RELEASE_SIGNING_KEY=<fingerprint> narrows "a valid signature from a key in
# the keyring" to that one key.
# A branch has no signature to check: with the check on, GIT_REF=<branch> is
# refused here, and WSI_TRACK_BRANCH=1 (or a clone with no release tag) is
# refused by the scripts. Leave it off for a developer's tree.
wsi_verify_release_tag() {
    local ref="$1"
    [[ "${WSI_VERIFY_RELEASE_TAGS:-0}" == "1" ]] || return 0
    if ! run_git rev-parse -q --verify "refs/tags/$ref" >/dev/null 2>&1; then
        echo "WSI_VERIFY_RELEASE_TAGS=1: '$ref' is not a tag, so its signature cannot be checked." >&2
        return 1
    fi
    # `git checkout <name>` takes a local BRANCH of that name over the tag, so a
    # signed tag could be checked out as an unsigned branch of the same name.
    if run_git rev-parse -q --verify "refs/heads/$ref" >/dev/null 2>&1; then
        echo "WSI_VERIFY_RELEASE_TAGS=1: '$ref' is also the name of a local branch, which a checkout would take instead of the tag. Nothing was changed." >&2
        return 1
    fi
    if ! run_git verify-tag "$ref" >&2; then
        echo "WSI_VERIFY_RELEASE_TAGS=1: tag '$ref' has no valid signature from a key in the keyring of ${GIT_RUNNER_USER:-root}. Nothing was changed." >&2
        return 1
    fi
    # verify-tag takes a good signature from ANY key in the keyring, at any trust
    # level. WSI_RELEASE_SIGNING_KEY=<fingerprint> pins the one key whose
    # signature counts (the signing key's or its primary key's fingerprint, as
    # `gpg --fingerprint` prints it; spaces are ignored).
    if [[ -n "${WSI_RELEASE_SIGNING_KEY:-}" ]]; then
        local want got
        want="${WSI_RELEASE_SIGNING_KEY//[[:space:]]/}"
        want="${want^^}"
        got="$(run_git verify-tag --raw "$ref" 2>&1 \
               | awk '$1 == "[GNUPG:]" && $2 == "VALIDSIG" { print toupper($3); print toupper($NF) }')"
        if ! grep -qxF -- "$want" <<<"$got"; then
            echo "WSI_VERIFY_RELEASE_TAGS=1: tag '$ref' is signed, but not by the key WSI_RELEASE_SIGNING_KEY names ($want). Nothing was changed." >&2
            return 1
        fi
    fi
}

# wsi_nginx_keys_zone_mb MAX_SIZE
# Print the size, in MB, of the tile cache's keys_zone (wsi_dzi_cache in
# infra/nginx/wsi.conf) that goes with a max_size of MAX_SIZE (nginx syntax:
# digits plus an optional k/m/g; no suffix = bytes): the scaled size below,
# capped by this host's RAM (wsi_nginx_keys_zone_cap_mb). Returns 1, printing
# nothing, for a malformed or absurdly large value. Used by nginx-cache.sh
# when the operator changes max_size, and by install.sh when it re-applies
# the persisted value, so both always write the same pair. Callers compare
# it with wsi_nginx_keys_zone_scaled_mb to tell the operator about a cap.
wsi_nginx_keys_zone_mb() {
    local mb cap
    mb="$(wsi_nginx_keys_zone_scaled_mb "$1")" || return 1
    cap="$(wsi_nginx_keys_zone_cap_mb)"
    if [[ -n "$cap" ]] && (( mb > cap )); then mb=$cap; fi
    printf '%s\n' "$mb"
}

# wsi_nginx_keys_zone_scaled_mb MAX_SIZE
# The keys_zone size MAX_SIZE calls for, before the RAM cap; same input and
# failure rules as wsi_nginx_keys_zone_mb.
#
# max_size alone adds no capacity: nginx indexes every cached file in the
# keys_zone (about 8,000 keys per MB) and, once that is full, evicts the
# least recently used file whatever max_size says. 200m holds ~1.6M keys —
# ~25 GB of 256px tiles — so raising max_size alone past that would only
# look bigger.
# 1 MB per 128 MB of max_size (a 16 KB average file) keeps the zone ahead of
# the disk cap for tiles, thumbnails and assets; 200m stays the floor. The
# zone is a shared mapping whose pages are touched only as entries are
# added, so an empty or part-filled cache costs next to nothing; a full one
# costs about this much RAM — the price of indexing that many files.
wsi_nginx_keys_zone_scaled_mb() {
    local v="$1" n mb
    [[ "$v" =~ ^([1-9][0-9]{0,14})([kKmMgG]?)$ ]] || return 1
    n="${BASH_REMATCH[1]}"
    case "${BASH_REMATCH[2]}" in
        "")  mb=$(( (n + 1048575) / 1048576 )) ;;
        k|K) mb=$(( (n + 1023) / 1024 )) ;;
        m|M) mb=$n ;;
        g|G) mb=$(( n * 1024 )) ;;
    esac
    mb=$(( (mb + 127) / 128 ))
    (( mb < 200 )) && mb=200
    printf '%s\n' "$mb"
}

# wsi_nginx_keys_zone_cap_mb
# Print the largest keys_zone, in MB, this host gets: a quarter of MemTotal
# (from WSI_MEMINFO, default /proc/meminfo — overridable for the tests), but
# never below the 200m floor. Prints nothing (no cap) when MemTotal cannot
# be read.
#
# Without it a large persisted max_size — 500g, a documented example, calls
# for 4000m — would break every update on a small VM: nginx maps the whole
# zone when it parses the config, `nginx -t` included, and fails
# ("munmap(...) failed", "test failed") when the mapping is larger than the
# kernel will commit (about RAM + swap), so install.sh would restore the old
# files and die, and update.sh roll back, on every run, with an error that
# pointed at nginx rather than at the cache size. A quarter of RAM also
# bounds what a full cache's index can take from the app. The 200m floor is
# the smallest zone the scaling ever asks for, which any supported host
# maps, so a cap below it would only shrink a cache that works.
wsi_nginx_keys_zone_cap_mb() {
    local kb mb
    kb="$(awk '/^MemTotal:/ { print $2; exit }' "${WSI_MEMINFO:-/proc/meminfo}" 2>/dev/null || true)"
    [[ "$kb" =~ ^[0-9]{1,15}$ ]] || return 0
    mb=$(( kb / 1024 / 4 ))
    (( mb < 200 )) && mb=200
    printf '%s\n' "$mb"
}
