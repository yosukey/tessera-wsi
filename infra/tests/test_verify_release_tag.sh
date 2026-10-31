#!/usr/bin/env bash
# Tests for wsi_verify_release_tag (infra/lib/wsi-common.sh): the opt-in
# signature check install.sh and update.sh make before they check out a tag and
# run ITS install.sh as root.
# Run: bash infra/tests/test_verify_release_tag.sh
#
# WHY THIS EXISTS
# ---------------
# Both scripts deploy the highest vX.Y.Z tag of origin and run that release's
# install.sh as root; unchecked, nothing but who may push a tag stands
# between a pushed "v9.9.9" and root on every host that updates. With
# WSI_VERIFY_RELEASE_TAGS=1 the tag must carry a signature `git verify-tag`
# accepts from a key already in the operator's keyring. This pins: off by
# default (a no-op), and when on, accepted only for a validly signed tag --
# not for an unsigned annotated tag, a lightweight tag, a branch, a tag signed
# by a key the keyring lacks, or a tag whose message was edited after signing
# -- and that install.sh and update.sh call it before each checkout.
#
# Uses a throwaway GNUPGHOME and scratch git repos (git as the current user).
# Needs git and gpg; SKIPs (exit 0) without them.
set -uo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
INFRA="$(cd "${HERE}/.." && pwd)"

skip() { printf 'SKIP - %s\n' "$1"; exit 0; }
command -v git >/dev/null 2>&1 || skip "git not available"
command -v gpg >/dev/null 2>&1 || skip "gpg not available"

fails=0
expect() {  # expect LABEL EXPECTED ACTUAL
    if [[ "$2" == "$3" ]]; then printf 'ok   - %s\n' "$1"
    else printf 'FAIL - %s: expected %q, got %q\n' "$1" "$2" "$3"; fails=$(( fails + 1 )); fi
}

WORK="$(mktemp -d)"
trap 'gpgconf --homedir "${WORK}/gnupg" --kill all >/dev/null 2>&1 || true; rm -rf "${WORK}"' EXIT

unset SUDO_USER GIT_AS_USER WSI_VERIFY_RELEASE_TAGS
export HOME="${WORK}/home"; mkdir -p "$HOME"
export GIT_CONFIG_GLOBAL=/dev/null GIT_CONFIG_SYSTEM=/dev/null
export GNUPGHOME="${WORK}/gnupg"; mkdir -m 700 "$GNUPGHOME"

# Two keys: the maintainer's (in the keyring) and a stranger's (made, signed
# with, and then removed from the keyring).
gen_key() {  # gen_key NAME
    gpg --batch --quiet --pinentry-mode loopback --passphrase '' \
        --quick-generate-key "$1 <$1@example.test>" ed25519 sign never >/dev/null 2>&1
}
gen_key maintainer || skip "gpg could not generate a test key"
gen_key stranger   || skip "gpg could not generate a test key"
maint_fpr="$(gpg --list-keys --with-colons maintainer@example.test 2>/dev/null | awk -F: '/^fpr/ {print $10; exit}')"
stranger_fpr="$(gpg --list-keys --with-colons stranger@example.test 2>/dev/null | awk -F: '/^fpr/ {print $10; exit}')"

REPO="${WORK}/repo"
git init -q "$REPO"
git -C "$REPO" config user.name t; git -C "$REPO" config user.email t@example.test
git -C "$REPO" config gpg.program gpg
git -C "$REPO" commit -q --allow-empty -m one
git -C "$REPO" tag -s -u "$maint_fpr" -m "release" v1.0.0
git -C "$REPO" tag -a -m "unsigned" v1.1.0
git -C "$REPO" tag v1.2.0                                  # lightweight
git -C "$REPO" tag -s -u "$stranger_fpr" -m "other" v1.3.0 # signed by the stranger
git -C "$REPO" tag -s -u "$maint_fpr" -m "release" v1.4.0
# v1.4.0, message edited after signing: the signature no longer matches.
git -C "$REPO" cat-file tag v1.4.0 | sed 's/^release$/releasE/' > "${WORK}/forged"
forged="$(git -C "$REPO" hash-object -t tag -w "${WORK}/forged")"
git -C "$REPO" update-ref refs/tags/v1.4.0 "$forged"
git -C "$REPO" branch main-copy
# A signed tag whose name a local branch also carries: `git checkout <name>` takes
# the branch, so verifying the tag would vouch for something else.
git -C "$REPO" tag -s -u "$maint_fpr" -m "release" v1.5.0
git -C "$REPO" branch v1.5.0

# The stranger's public key leaves the keyring after signing (the attacker's
# key is not one the operator trusts).
gpg --batch --yes --delete-secret-and-public-key "$stranger_fpr" >/dev/null 2>&1

check() {  # check LABEL EXPECTED_RC REF [ENV=VAL ...]
    local label="$1" want="$2" ref="$3"; shift 3
    local out rc
    # shellcheck disable=SC2016  # the snippet is expanded by the inner bash
    out="$(env "$@" bash -c '. "$1"; wsi_define_run_git "$2"; wsi_verify_release_tag "$3"' _ \
        "${INFRA}/lib/wsi-common.sh" "$REPO" "$ref" 2>&1)"; rc=$?
    expect "$label: rc" "$want" "$rc"
    LAST_OUT="$out"
}

# Off by default, whatever the tag is.
check "off: unsigned tag passes" 0 v1.1.0
check "off: =0 passes too" 0 v1.1.0 WSI_VERIFY_RELEASE_TAGS=0
check "off: a branch passes" 0 main-copy

V=WSI_VERIFY_RELEASE_TAGS=1
check "on: a tag signed by the maintainer's key passes" 0 v1.0.0 "$V"
check "on: an unsigned annotated tag is refused" 1 v1.1.0 "$V"
expect "on: ...and says nothing was changed" "yes" \
    "$(grep -q "has no valid signature.*Nothing was changed" <<<"$LAST_OUT" && echo yes)"
check "on: a lightweight tag is refused" 1 v1.2.0 "$V"
check "on: a tag signed by a key not in the keyring is refused" 1 v1.3.0 "$V"
check "on: a tag edited after signing is refused" 1 v1.4.0 "$V"
check "on: a branch is refused" 1 main-copy "$V"
expect "on: ...saying it is not a tag" "yes" \
    "$(grep -q "is not a tag" <<<"$LAST_OUT" && echo yes)"
check "on: a ref that does not exist is refused" 1 v9.9.9 "$V"
check "on: a signed tag that a local branch of the same name shadows is refused" 1 v1.5.0 "$V"
expect "on: ...saying why" "yes" "$(grep -q "also the name of a local branch" <<<"$LAST_OUT" && echo yes)"
check "off: ... and it is not looked at when verification is off" 0 v1.5.0
# Pinning the signing key: the maintainer's tag passes only when the pin names
# the maintainer's key (fingerprint, either case, with or without spaces), and a
# good signature from another key in the keyring does not satisfy it.
spaced="$(sed -E 's/(.{4})/\1 /g' <<<"$maint_fpr")"
check "pin: the maintainer's own fingerprint passes" 0 v1.0.0 "$V" WSI_RELEASE_SIGNING_KEY="$maint_fpr"
check "pin: ... in lower case and spaced out" 0 v1.0.0 "$V" WSI_RELEASE_SIGNING_KEY="${spaced,,}"
check "pin: another key's fingerprint is refused for a validly signed tag" 1 v1.0.0 "$V" WSI_RELEASE_SIGNING_KEY="$stranger_fpr"
expect "pin: ...saying which key it wanted" "yes" "$(grep -q "not by the key WSI_RELEASE_SIGNING_KEY names" <<<"$LAST_OUT" && echo yes)"
# A pin is the whole fingerprint: its tail (the long key ID gpg prints in
# GOODSIG, and what any substring match would accept) or head is not enough.
check "pin: the last 16 hex digits (the long key ID) are refused" 1 v1.0.0 "$V" WSI_RELEASE_SIGNING_KEY="${maint_fpr: -16}"
check "pin: so is the fingerprint without its last digit" 1 v1.0.0 "$V" WSI_RELEASE_SIGNING_KEY="${maint_fpr%?}"
check "pin: an unsigned tag is still refused" 1 v1.1.0 "$V" WSI_RELEASE_SIGNING_KEY="$maint_fpr"
check "pin: ignored when verification is off" 0 v1.0.0 WSI_RELEASE_SIGNING_KEY="$stranger_fpr"
# Only "1" turns it on: a typo must not silently pass as enabled-but-lax, nor
# the other way round.
check "off: any other value leaves it off" 0 v1.1.0 WSI_VERIFY_RELEASE_TAGS=yes

# Both scripts call it before each checkout of a tag they deploy, and a refusal
# ends the run (`|| die`, not `|| true`, not ignored).
for pair in "install.sh:run_git checkout \"\$GIT_REF\"" "install.sh:run_git checkout \"\$LATEST_TAG\"" \
            "update.sh:move_tree checkout \"\$TARGET_REF\""; do
    script="${pair%%:*}" call="${pair#*:}"
    ok="$(python3 - "${INFRA}/${script}" "$call" <<'PY'
import re, sys
text, call = open(sys.argv[1]).read().splitlines(), sys.argv[2]
i = next(n for n, l in enumerate(text) if call in l)
prev = [l for l in text[max(0, i - 6):i] if "wsi_verify_release_tag" in l and not l.lstrip().startswith("#")]
print("yes" if prev and re.search(r'wsi_verify_release_tag "[^"]+" \|\| die ', prev[-1]) else "no")
PY
)"
    expect "${script}: wsi_verify_release_tag, with '|| die', runs just before '${call}'" "yes" "$ok"
done

# With the check on, the two paths that deploy a branch instead of a tag must
# refuse rather than run unverified code: WSI_TRACK_BRANCH=1, and a clone with
# no vX.Y.Z tag that would fall back to the branch HEAD.
for script in install.sh update.sh; do
    for what in "cannot be combined with WSI_TRACK_BRANCH=1" "no vX.Y.Z release tag found"; do
        ok="$(python3 - "${INFRA}/${script}" "$what" <<'PY'
import re, sys
text = open(sys.argv[1]).read()
# [[ "${WSI_VERIFY_RELEASE_TAGS:-0}" != "1" ]] \<newline> || die "...<what>..."
pat = r'\[\[ "\$\{WSI_VERIFY_RELEASE_TAGS:-0\}" != "1" \]\] \\\n\s*\|\| die "[^"\n]*' + re.escape(sys.argv[2])
print("yes" if re.search(pat, text) else "no")
PY
)"
        expect "${script}: '${what}' dies unless verification is off" "yes" "$ok"
    done
done

printf -- '--------------------------------------------------------------\n'
if [[ "$fails" -eq 0 ]]; then
    printf 'PASS - wsi_verify_release_tag accepts only a validly signed tag, and only when asked\n'
else
    exit 1
fi
