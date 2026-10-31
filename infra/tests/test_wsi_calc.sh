#!/usr/bin/env bash
# Unit tests for the pure sizing helpers in infra/systemd/wsi-calc.sh.
# Run: bash infra/tests/test_wsi_calc.sh
#
# The compute_* functions take every input as an argument, so these tests
# never touch /proc — they pass RAM sizes directly and assert the worker
# count and per-worker DZI byte cap match the documented model.
set -uo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
# shellcheck source=../systemd/wsi-calc.sh
. "${HERE}/../systemd/wsi-calc.sh"

GiB=1073741824
fails=0
checks=0

# Make the suite independent of any inherited tunables.
unset WSI_CACHE_RAM_FRACTION WSI_CACHE_RESERVE_MIN_BYTES \
      WSI_CACHE_FLOOR_BYTES WSI_CACHE_CEIL_BYTES 2>/dev/null || true

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

# workers LABEL EXPECTED_W  explicit percent cpu mem_gib
workers() {
    local label="$1" exp="$2" explicit="$3" percent="$4" cpu="$5" gib="$6"
    local mem=$(( gib * GiB ))
    expect "$label (workers)" "$exp" "$(wsi_compute_workers "$explicit" "$percent" "$cpu" "$mem")"
}

# cache LABEL EXPECTED_GIB  cpu mem_gib   (uses default-path worker count)
cache() {
    local label="$1" exp_gib="$2" cpu="$3" gib="$4"
    local mem=$(( gib * GiB ))
    local w; w=$(wsi_compute_workers "" "" "$cpu" "$mem")
    expect "$label (cache bytes)" "$(( exp_gib * GiB ))" "$(wsi_compute_cache_max_bytes "$mem" "$w")"
}

echo "# default-path worker count + auto byte cap (fraction=75%)"
# label                 W   expl pct cpu  RAM
workers "96GiB/8c"      4   ""   ""  8    96
workers "16GiB/16c"     6   ""   ""  16   16
workers "32GiB/32c"     8   ""   ""  32   32
workers "16GiB/64c"     6   ""   ""  64   16
workers "8GiB/4c"       2   ""   ""  4    8
workers "4GiB/2c"       1   ""   ""  2    4

# label                 GiB cpu  RAM
cache   "96GiB/8c"      8   8    96
cache   "16GiB/16c"     2   16   16
cache   "32GiB/32c"     3   32   32
cache   "8GiB/4c"       3   4    8
cache   "4GiB/2c"       2   2    4

echo "# explicit / percent paths bypass the RAM cap"
workers "explicit 16 clamped to 8 cores" 8  16 ""  8  16
workers "percent 50% of 16 cores"        8  ""  50 16 16
workers "percent 25% of 16 cores"        4  ""  25 16 16
workers "percent 100% uncapped, low RAM" 64 ""  100 64 16

echo "# RAM unreadable -> no auto cap / no derived cache"
expect "no-mem workers = bare formula" 8 "$(wsi_compute_workers "" "" 16 "")"
expect "no-mem cache empty"            "" "$(wsi_compute_cache_max_bytes "" 4)"
expect "zero-mem cache empty"          "" "$(wsi_compute_cache_max_bytes 0 4)"
expect "zero-workers cache empty"      "" "$(wsi_compute_cache_max_bytes "$(( 16 * GiB ))" 0)"

echo "# tunable override: fraction=50% shrinks budget (16GiB/16c -> 4 workers)"
WSI_CACHE_RAM_FRACTION=50 expect "fraction=50% workers" 4 \
    "$(WSI_CACHE_RAM_FRACTION=50 wsi_compute_workers "" "" 16 "$(( 16 * GiB ))")"

echo "# malformed worker overrides fall back to the default formula"
# A fat-fingered wsi.env edit must not reach wsi_compute_workers'
# arithmetic: under `set -eu` in start-wsi-app.sh a non-integer would
# abort the unit and crash-loop it with a journald error that never
# names the offending variable.
# stderr carries the warning; the captured stdout must stay a usable count.
expect "UVICORN_WORKERS=four -> default" 8 \
    "$(wsi_compute_workers "four" "" 16 "" 2>/dev/null)"
expect "UVICORN_WORKERS_PERCENT=5O -> default" 8 \
    "$(wsi_compute_workers "" "5O" 16 "" 2>/dev/null)"
expect "negative UVICORN_WORKERS -> default" 8 \
    "$(wsi_compute_workers "-2" "" 16 "" 2>/dev/null)"
expect "malformed override warns on stderr" "1" \
    "$(wsi_compute_workers "four" "" 16 "" 2>&1 >/dev/null | grep -c 'ignoring UVICORN_WORKERS')"
# Valid values are untouched by the guard — including the
# whitespace-padded forms a hand-edited wsi.env can produce, which must
# keep working.
expect "explicit 0 still clamps to 1"   1 "$(wsi_compute_workers "0" "" 16 "")"
expect "explicit ' 4' trimmed"          4 "$(wsi_compute_workers " 4" "" 16 "")"
expect "explicit '4 ' trimmed"          4 "$(wsi_compute_workers "4 " "" 16 "")"
expect "percent ' 25 ' trimmed"         4 "$(wsi_compute_workers "" " 25 " 16 "")"
# The journal summary in start-wsi-app.sh keys off the same helper, so it
# can never name an override that wsi_compute_workers rejected.
expect "wsi_valid_uint accepts padded"  "7"  "$(wsi_valid_uint "  7 ")"
expect "wsi_valid_uint rejects 'four'"  "1"  "$(wsi_valid_uint "four" >/dev/null; echo $?)"

echo "# leading zeros are normalised, not fed to bash as octal"
# bash reads a leading zero as octal inside (( )). An all-octal-digit value
# like 064 (=52) still clamps correctly, so the regression case is a
# leading-zero value containing an 8 or 9: "099" makes both clamps raise
# "value too great for base", errexit's &&-exemption swallows it, and the
# untouched string reaches uvicorn as decimal 99 — ninety-nine workers on a
# 16-core host. 099 is therefore the assertion that actually fails without
# the normalisation; 064 would pass either way.
expect "valid_uint 08 -> 8"             8    "$(wsi_valid_uint "08")"
expect "valid_uint 000 -> 0"            0    "$(wsi_valid_uint "000")"
expect "valid_uint 099 -> 99"           99   "$(wsi_valid_uint "099")"
expect "workers 08 -> 8, no arith error" 8   "$(wsi_compute_workers "08" "" 16 "" 2>/dev/null)"
expect "workers 099 clamped to cores"   16   "$(wsi_compute_workers "099" "" 16 "" 2>/dev/null)"
expect "workers 099 emits no stderr"    ""   "$(wsi_compute_workers "099" "" 16 "" 2>&1 >/dev/null)"
# 010 is valid octal, so nothing errors and nothing is clamped (10 < 16
# cores) — unnormalised it also reached uvicorn as decimal 10. It is here as
# a normalisation check only: the count this function REPORTS must be the
# same 10 uvicorn will parse, not the 8 the clamp arithmetic saw.
expect "workers 010 normalised to 10"   10   "$(wsi_compute_workers "010" "" 16 "" 2>/dev/null)"
expect "percent 025 -> 25% of 16"       4    "$(wsi_compute_workers "" "025" 16 "" 2>/dev/null)"
# The skipped arithmetic also took RAM-aware cache sizing down with it:
# unnormalised, wsi_compute_cache_max_bytes' `(( workers > 0 ))` guard errors
# out and it returns empty, leaving DZI_CACHE_MAX_BYTES unset. Normalised,
# 099 clamps to 16 workers and 24 GiB budget / 16 = 1.5 GiB lands under the
# 2 GiB floor, so the floor is what comes back.
expect "099 keeps cache auto-sizing on" "$(( 2 * GiB ))" \
    "$(wsi_compute_cache_max_bytes "$(( 32 * GiB ))" "$(wsi_compute_workers "099" "" 16 "$(( 32 * GiB ))" 2>/dev/null)")"
# _wsi_envint shares the normalisation, so a leading-zero WSI_CACHE_* value
# cannot break the byte arithmetic either.
expect "envint 075 -> 75"               75   "$(WSI_CACHE_RAM_FRACTION=075 wsi_cache_fraction)"
expect "envint garbage -> default"      75   "$(WSI_CACHE_RAM_FRACTION=abc wsi_cache_fraction)"
expect "envint unset -> default"        75   "$(wsi_cache_fraction)"
expect "envint 0 still honoured"        0    "$(WSI_CACHE_RAM_FRACTION=0 wsi_cache_fraction)"

echo "# a zero cache floor is 'no floor', not a divide-by-zero"
# WSI_CACHE_FLOOR_BYTES=0 is a plausible "remove the minimum" edit. A bare
# division would print "division by 0" and leave cap empty, which the
# caller would read as 0 and clamp to ONE worker on a box sized for several.
expect "floor=0 keeps the default count" 8 \
    "$(WSI_CACHE_FLOOR_BYTES=0 wsi_compute_workers "" "" 16 "$(( 32 * GiB ))" 2>/dev/null)"
expect "floor=0 emits no arithmetic error" "" \
    "$(WSI_CACHE_FLOOR_BYTES=0 wsi_compute_workers "" "" 16 "$(( 32 * GiB ))" 2>&1 >/dev/null)"
expect "floor=0 cap is the whole budget" "$(WSI_CACHE_FLOOR_BYTES=0 wsi_budget_bytes "$(( 32 * GiB ))")" \
    "$(WSI_CACHE_FLOOR_BYTES=0 wsi_ram_worker_cap "$(( 32 * GiB ))" 2>/dev/null)"
# A real floor still caps: 32 GiB * 75% = 24 GiB budget / 8 GiB floor = 3.
expect "floor=8GiB caps to 3 workers"    3 \
    "$(WSI_CACHE_FLOOR_BYTES=$(( 8 * GiB )) wsi_ram_worker_cap "$(( 32 * GiB ))")"
# Values too large for signed 64-bit arithmetic are rejected at the source,
# so wsi_cache_floor hands back the documented default and the cap behaves
# exactly as if the operator had never set the variable. Both wrap shapes
# matter: 2^63 wraps NEGATIVE (an `(( x <= 0 ))` guard would read it as
# "no floor") and any multiple of 2^64 wraps to exactly 0 (a `== 0` string
# guard misses it and the division blows up).
# Status 2 is "digits, but out of range"; status 1 is "not an integer".
# The split exists so the warning can name the real problem instead of
# telling an operator their 20-digit number is not a number.
expect "valid_uint rejects 2^63 with 2" "2" \
    "$(wsi_valid_uint 9223372036854775808 >/dev/null; echo $?)"
expect "valid_uint rejects 2^64 with 2" "2" \
    "$(wsi_valid_uint 18446744073709551616 >/dev/null; echo $?)"
expect "out-of-range warning says so"   "1" \
    "$(wsi_compute_workers 18446744073709551616 "" 16 "" 2>&1 >/dev/null | grep -c 'too large for 64-bit')"
expect "non-numeric warning says so"    "1" \
    "$(wsi_compute_workers four "" 16 "" 2>&1 >/dev/null | grep -c 'not a non-negative integer')"
expect "valid_uint keeps INT64_MAX"     9223372036854775807 \
    "$(wsi_valid_uint 9223372036854775807)"
expect "floor=2^63 falls back to default" \
    "$(wsi_ram_worker_cap "$(( 32 * GiB ))")" \
    "$(WSI_CACHE_FLOOR_BYTES=9223372036854775808 wsi_ram_worker_cap "$(( 32 * GiB ))")"
expect "floor=2^64 does not divide by zero" \
    "$(wsi_ram_worker_cap "$(( 32 * GiB ))")" \
    "$(WSI_CACHE_FLOOR_BYTES=18446744073709551616 wsi_ram_worker_cap "$(( 32 * GiB ))" 2>/dev/null)"
expect "floor=2^64 emits no arithmetic error" "" \
    "$(WSI_CACHE_FLOOR_BYTES=18446744073709551616 wsi_ram_worker_cap "$(( 32 * GiB ))" 2>&1 >/dev/null)"

echo
if (( fails == 0 )); then
    printf 'PASS: %d checks\n' "$checks"
    exit 0
else
    printf 'FAILED: %d/%d checks\n' "$fails" "$checks"
    exit 1
fi
