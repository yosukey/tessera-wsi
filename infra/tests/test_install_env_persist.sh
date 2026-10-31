#!/usr/bin/env bash
# Unit tests for install.sh's persist_env_var() — the one function in the
# installer that REWRITES an existing /etc/wsi/wsi.env.
# Run: bash infra/tests/test_install_env_persist.sh
#
# WHY THIS EXISTS
# ---------------
# Everything else install.sh does to wsi.env is create-if-absent. This
# function edits a file that holds JWT_SECRET and the bootstrap admin
# password, on a host that is already serving. Two properties are easy to
# break and impossible to notice from a successful-looking install log:
#
#   1. It must agree with BOTH readers about which line wins. install.sh
#      reads back with `sed ... | tail -n1` (last uncommented assignment)
#      and systemd's EnvironmentFile= lets a later line win. Editing the
#      first matching line instead — including the commented-out template
#      line wsi.env.example ships — would be a silent no-op whenever the
#      operator had appended their own value further down, i.e. exactly the
#      "value silently reverts" failure the function exists to prevent.
#   2. It must not disturb anything else in the file, and must leave it
#      0600 root:root. A regression here leaks secrets or corrupts config.
#
# The function is extracted from the real install.sh by name rather than
# copied, so a rename, a moved brace, or an edit to the body is exercised
# here instead of drifting silently. Extraction failure is a test failure.
#
# Runs entirely in a temp dir; never touches /etc. chown is skipped when
# not root, so the suite is usable unprivileged (the mode check still runs).
set -uo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
INSTALL_SH="${HERE}/../install.sh"
EXAMPLE_ENV="${HERE}/../wsi.env.example"

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
[[ -r "$EXAMPLE_ENV" ]] || { echo "FAILED: cannot read $EXAMPLE_ENV"; exit 1; }

# ---- extract persist_env_var() from the real installer ---------------
# From the definition line to the first line that is exactly "}" — the
# function's body is indented, so a bare closing brace at column 0 is its
# terminator.
FN_SRC="$(awk '
    /^persist_env_var\(\) \{$/ { grabbing = 1 }
    grabbing                   { print }
    grabbing && /^\}$/         { exit }
' "$INSTALL_SH")"

if [[ -z "$FN_SRC" ]] || [[ "$FN_SRC" != *"persist_env_var() {"* ]]; then
    echo "FAILED: could not extract persist_env_var() from $INSTALL_SH"
    echo "        (renamed or reformatted? this test must be updated with it)"
    exit 1
fi

TMP_ROOT="$(mktemp -d)"
trap 'rm -rf "$TMP_ROOT"' EXIT

ETC_DIR="$TMP_ROOT/etc"
mkdir -p "$ETC_DIR"
LOG_LINES=0
log() { LOG_LINES=$(( LOG_LINES + 1 )); }
# install.sh runs as root and hands ownership to root; unprivileged runs
# would fail on -o/-g, so drop those two flags when not running as root. The
# mode is what the assertions below check either way.
if [[ ${EUID:-$(id -u)} -eq 0 ]]; then
    install() { command install "$@"; }
else
    install() {
        local args=() skip=0
        for a in "$@"; do
            if (( skip )); then skip=0; continue; fi
            case "$a" in
                -o|-g) skip=1 ;;
                *) args+=("$a") ;;
            esac
        done
        command install "${args[@]}"
    }
fi

# persist_env_var reads the current value back through the shared helper
# install.sh sources (wsi_env_value) — the same read-back install.sh uses for
# its precedence logic, so test the real one.
# shellcheck source=../lib/wsi-common.sh
. "${HERE}/../lib/wsi-common.sh"
eval "$FN_SRC"

env_file() { printf '%s' "$ETC_DIR/wsi.env"; }

# read_back KEY -> what install.sh's own top-of-script read-back would see.
# Deliberately a copy of that expression: if the two ever diverge, these
# assertions are what notice.
read_back() {
    sed -n -E "s/^[[:space:]]*$1[[:space:]]*=[[:space:]]*\"?([^\"#]*)\"?[[:space:]]*\$/\1/p" \
        "$(env_file)" | tail -n1
}

live_count() { grep -cE "^[[:space:]]*$1[[:space:]]*=" "$(env_file)"; }

reset_env() { cp "$EXAMPLE_ENV" "$(env_file)"; chmod 0600 "$(env_file)"; }

echo "# the file the operator actually has: wsi.env.example, untouched keys"
reset_env
BEFORE_LINES=$(wc -l < "$(env_file)")
persist_env_var WSI_LAN_HOST lan.example.org
expect "value is readable afterwards"   "lan.example.org" "$(read_back WSI_LAN_HOST)"
expect "exactly one live assignment"    "1"               "$(live_count WSI_LAN_HOST)"
expect "commented template is kept"     "1" \
    "$(grep -cE '^#WSI_LAN_HOST=' "$(env_file)")"
expect "file grew by exactly one line"  "$(( BEFORE_LINES + 1 ))" "$(wc -l < "$(env_file)")"
expect "mode is still 0600"             "600" "$(stat -c %a "$(env_file)")"

echo "# first-match editing would silently discard the operator's appended value"
# Operator appended their own value below the commented template. Editing
# the FIRST match would leave this line winning and silently discard the
# new value.
reset_env
printf 'WSI_LAN_HOST=operator.example.org\n' >> "$(env_file)"
persist_env_var WSI_LAN_HOST new.example.org
expect "operator's line is replaced, not shadowed" "new.example.org" "$(read_back WSI_LAN_HOST)"
expect "still exactly one live assignment"         "1" "$(live_count WSI_LAN_HOST)"

echo "# duplicates collapse; the survivor is the one both readers pick"
reset_env
printf 'WSI_LAN_HOST=a.example.org\nWSI_LAN_HOST=b.example.org\n  WSI_LAN_HOST = c.example.org\n' \
    >> "$(env_file)"
persist_env_var WSI_LAN_HOST final.example.org
expect "duplicates collapse to one"     "1" "$(live_count WSI_LAN_HOST)"
expect "surviving value is the new one" "final.example.org" "$(read_back WSI_LAN_HOST)"

echo "# idempotence: re-persisting the same value must not rewrite"
reset_env
persist_env_var WSI_PUBLIC_BASE_URL https://wsi.example.com
SNAPSHOT="$(cat "$(env_file)")"
LOG_LINES=0
persist_env_var WSI_PUBLIC_BASE_URL https://wsi.example.com
expect "second run writes nothing"      "0" "$LOG_LINES"
expect "file is byte-identical"         "$SNAPSHOT" "$(cat "$(env_file)")"

echo "# values with characters that would break a sed s/// expression"
reset_env
persist_env_var WSI_PUBLIC_BASE_URL 'https://ex.example.com/a&b\1$x'
expect "metacharacters survive verbatim" 'https://ex.example.com/a&b\1$x' \
    "$(read_back WSI_PUBLIC_BASE_URL)"

echo "# neighbouring keys and secrets are untouched"
reset_env
printf 'JWT_SECRET=deadbeef\nWSI_LAN_PROBE_TIMEOUT_MS=12000\n' >> "$(env_file)"
persist_env_var WSI_LAN_HOST only.example.org
expect "JWT_SECRET intact"              "deadbeef" "$(read_back JWT_SECRET)"
expect "sibling tunable intact"         "12000"    "$(read_back WSI_LAN_PROBE_TIMEOUT_MS)"

echo "# a key whose name prefixes another must not be over-matched"
reset_env
printf 'WSI_LAN_HOST_EXTRA=keepme\n' >> "$(env_file)"
persist_env_var WSI_LAN_HOST short.example.org
expect "prefix-sharing key survives"    "keepme" "$(read_back WSI_LAN_HOST_EXTRA)"

echo "# no trailing newline in the source file"
reset_env
printf 'WSI_LAN_HOST=nonewline.example.org' >> "$(env_file)"   # no \n
persist_env_var WSI_LAN_HOST fixed.example.org
expect "value still readable"           "fixed.example.org" "$(read_back WSI_LAN_HOST)"
expect "no line got glued together"     "1" "$(live_count WSI_LAN_HOST)"

echo "# the shared read-back (wsi_env_value) unquotes the way systemd does"
# systemd's EnvironmentFile= strips single quotes as well as double ones. A
# single-quoted DATABASE_URL read back with its quotes would make update.sh
# skip the DB snapshot, and a single-quoted WSI_LAN_HOST fail install.sh's
# hostname check on every run.
reset_env
cat >> "$(env_file)" <<'ENV'
DATABASE_URL='sqlite:////var/lib/wsi/wsi.db'
WSI_LAN_HOST = 'lan.example.org'
Q_DOUBLE="double"
Q_NESTED="'kept'"
Q_APOS=it's
Q_TWICE='Q_TWICE=inner'
ENV
expect "single-quoted value is unquoted"        "sqlite:////var/lib/wsi/wsi.db" \
    "$(wsi_env_value DATABASE_URL "$(env_file)")"
expect "... with blanks around '=' and after it" "lan.example.org" \
    "$(wsi_env_value WSI_LAN_HOST "$(env_file)")"
expect "double-quoted value is unquoted"        "double" "$(wsi_env_value Q_DOUBLE "$(env_file)")"
expect "quotes inside double quotes are kept"   "'kept'" "$(wsi_env_value Q_NESTED "$(env_file)")"
expect "an apostrophe mid-value is kept"        "it's"   "$(wsi_env_value Q_APOS "$(env_file)")"
expect "a value is unquoted once, not twice"    "Q_TWICE=inner" "$(wsi_env_value Q_TWICE "$(env_file)")"
SNAPSHOT="$(cat "$(env_file)")"
LOG_LINES=0
persist_env_var WSI_LAN_HOST lan.example.org
expect "re-persisting a single-quoted equal value writes nothing" "0|same" \
    "$LOG_LINES|$([[ "$(cat "$(env_file)")" == "$SNAPSHOT" ]] && echo same)"

echo "# WSI_PUBLIC_BASE_URL is resolved to an origin before it is built in or persisted"
# install.sh's own resolution block, extracted from its header comment to the
# log() definition that follows it, and run with the env value and wsi.env
# given. Prints "value|from-env|warned".
PBU_SRC="$(awk '
    /^# ---- Optional canonical public base URL/ { grabbing = 1 }
    grabbing && /^log\(\)/                       { exit }
    grabbing                                     { print }
' "$INSTALL_SH")"
if [[ "$PBU_SRC" != *"wsi_url_origin"* ]]; then
    echo "FAILED: could not extract the WSI_PUBLIC_BASE_URL block from $INSTALL_SH"
    exit 1
fi
resolve_pbu() {
    # resolve_pbu ENV_VALUE WSI_ENV_VALUE
    reset_env
    [[ -n "$2" ]] && printf 'WSI_PUBLIC_BASE_URL=%s\n' "$2" >> "$(env_file)"
    (
        WSI_PUBLIC_BASE_URL="$1"
        WSI_PUBLIC_BASE_URL_FROM_ENV="$1"
        eval "$PBU_SRC" 2>"$TMP_ROOT/pbu.err"
        printf '%s|%s|%s\n' "$WSI_PUBLIC_BASE_URL" "$WSI_PUBLIC_BASE_URL_FROM_ENV" \
            "$(grep -c '\[warn\]' "$TMP_ROOT/pbu.err")"
    )
}
expect "a bare origin is used as given"        "https://wsi.example.org|https://wsi.example.org|0" \
    "$(resolve_pbu https://wsi.example.org '')"
expect "a trailing slash is dropped silently"  "https://wsi.example.org|https://wsi.example.org/|0" \
    "$(resolve_pbu https://wsi.example.org/ '')"
expect "a path is dropped with a warning (persisted as the origin)" \
    "https://wsi.example.org|https://wsi.example.org/wsi|1" \
    "$(resolve_pbu https://wsi.example.org/wsi '')"
expect "an origin in wsi.env alone is read back" "http://10.0.0.5:8080||0" \
    "$(resolve_pbu '' http://10.0.0.5:8080)"
expect "a CRLF-edited origin in wsi.env is read back without a warning" "http://10.0.0.5:8080||0" \
    "$(resolve_pbu '' $'http://10.0.0.5:8080\r')"
expect "a path in wsi.env alone is reduced too" "https://wsi.example.org||1" \
    "$(resolve_pbu '' https://wsi.example.org/sub)"
expect "a non-URL is ignored, falling back to wsi.env's origin" "https://kept.example.org||1" \
    "$(resolve_pbu ftp://bad.example.org https://kept.example.org/x)"
expect "a non-URL with nothing to fall back to is unset" "||1" \
    "$(resolve_pbu 'user@bad' '')"

echo "# absent env file is a no-op, not an error"
rm -f "$(env_file)"
persist_env_var WSI_LAN_HOST anything.example.org
expect "returns success with no file"   "0" "$?"
expect "no file was created"            "0" "$([[ -e "$(env_file)" ]] && echo 1 || echo 0)"

# ---- mfa_key_missing(): when install.sh may add a WSI_MFA_KEY --------
# Replacing a key that exists would lock every two-factor account out, so the
# check must never take a written-but-unusual value for "missing".
MFA_FN_SRC="$(awk '
    /^mfa_key_missing\(\) \{$/ { grabbing = 1 }
    grabbing                   { print }
    grabbing && /^\}$/         { exit }
' "$INSTALL_SH")"
if [[ "$MFA_FN_SRC" != *"mfa_key_missing() {"* ]]; then
    echo "FAILED: could not extract mfa_key_missing() from $INSTALL_SH"
    exit 1
fi
eval "$MFA_FN_SRC"
mfa_case() {  # mfa_case LABEL EXPECTED(missing|present) CONTENT
    printf '%b' "$3" > "$TMP_ROOT/mfa.env"
    local got=present
    mfa_key_missing "$TMP_ROOT/mfa.env" && got=missing
    expect "mfa key: $1" "$2" "$got"
}
echo "# mfa_key_missing"
mfa_case "no line"                        missing 'JWT_SECRET=x\n'
mfa_case "template's empty line"          missing 'WSI_MFA_KEY=\n'
mfa_case "empty quotes"                   missing 'WSI_MFA_KEY=""\n'
mfa_case "commented out only"             missing '#WSI_MFA_KEY=abc\n'
mfa_case "a plain key"                    present 'WSI_MFA_KEY=0123abcd\n'
mfa_case "key with a trailing comment"    present 'WSI_MFA_KEY=0123abcd  # rotated 2026\n'
mfa_case "key with # and quotes inside"   present "WSI_MFA_KEY='pa#ss\"word'\n"
mfa_case "CRLF line"                      present 'WSI_MFA_KEY=0123abcd\r\n'
mfa_case "a key above a stray empty line" present 'WSI_MFA_KEY=0123abcd\nWSI_MFA_KEY=\n'
mfa_case "last live line set"             present 'WSI_MFA_KEY=\nWSI_MFA_KEY=0123abcd\n'
rm -f "$TMP_ROOT/mfa.env"
expect "mfa key: no file is not missing" "present" "$(mfa_key_missing "$TMP_ROOT/none.env" && echo missing || echo present)"
# Under install.sh's own options and ERR trap, a file with no WSI_MFA_KEY
# line (the usual one) must not print a FAILED line from inside the check.
printf 'JWT_SECRET=x\n' > "$TMP_ROOT/mfa.env"
trap_err="$(MFA_FN_SRC="$MFA_FN_SRC" bash -c '
    set -Eeuo pipefail
    trap '\''echo "FAILED: $BASH_COMMAND" >&2; exit 1'\'' ERR
    eval "$MFA_FN_SRC"
    mfa_key_missing "$1" && echo missing
' _ "$TMP_ROOT/mfa.env" 2>&1)"
expect "mfa key: no line, under install.sh's ERR trap" "missing" "$trap_err"
rm -f "$TMP_ROOT/mfa.env"

# ---- a symlinked wsi.env is left alone ----------------------------------
# mv'ing the staged copy over the path would replace the link with a plain
# file, detaching it from the file it points to.
echo "# symlinked wsi.env"
mkdir -p "$TMP_ROOT/managed"
printf 'JWT_SECRET=keep\n' > "$TMP_ROOT/managed/wsi.env"
rm -f "$(env_file)"
ln -s "$TMP_ROOT/managed/wsi.env" "$(env_file)"
POST_INSTALL_WARNINGS=()
warn() { :; }
persist_env_var WSI_MFA_KEY abc
expect "symlink: still a symlink"          "1" "$([[ -L "$(env_file)" ]] && echo 1 || echo 0)"
expect "symlink: target untouched"         "JWT_SECRET=keep" "$(cat "$TMP_ROOT/managed/wsi.env")"
expect "symlink: warning kept for the end" "1" "${#POST_INSTALL_WARNINGS[@]}"
rm -f "$(env_file)"

echo
if (( fails == 0 )); then
    printf 'PASS: %d checks\n' "$checks"
    exit 0
else
    printf 'FAILED: %d/%d checks\n' "$fails" "$checks"
    exit 1
fi
