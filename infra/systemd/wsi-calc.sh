# shellcheck shell=bash
# Shared helpers for sizing uvicorn workers and the per-worker DZI byte
# cache from system RAM. Sourced by start-wsi-app.sh (which sets the
# runtime env before the uvicorn master forks) and by set-workers.sh
# (which previews the identical numbers). Keeping the maths in one place
# stops the two scripts from drifting apart.
#
# The compute_* functions take every input as an argument and have no
# side effects, so they are unit-testable (see infra/tests/test_wsi_calc.sh).
# Only wsi_mem_total_bytes touches the system (/proc, cgroup).
#
# Model: the per-worker DZI byte cap bounds reclaimable OS page-cache
# footprint, not process RSS. A budget is carved out of RAM, split
# across workers, and no more workers are spawned than can be given a
# useful (>= FLOOR) slice — so a many-core / low-RAM host stops scaling
# workers off core count alone.
#
# Tunables (env, read at call time so tests can override them):
#   WSI_CACHE_RAM_FRACTION       percent of RAM used as the cache budget   (default 75)
#   WSI_CACHE_RESERVE_MIN_BYTES  absolute floor on the non-cache reserve   (default 2 GiB)
#   WSI_CACHE_FLOOR_BYTES        minimum useful per-worker byte cap        (default 2 GiB)
#   WSI_CACHE_CEIL_BYTES         maximum per-worker byte cap               (default 8 GiB)

wsi_valid_uint() {
    # wsi_valid_uint VALUE -> echo the value NORMALISED for shell arithmetic
    # and return 0 when it is a non-negative integer that fits in signed
    # 64-bit (or empty); echo nothing and return non-zero otherwise:
    #
    #   1  not a non-negative integer at all ("four", "-2", "1e9")
    #   2  digits only, but too large for bash arithmetic (>= 2^63)
    #
    # The two are distinguished so a caller can name the actual problem
    # instead of telling an operator their 20-digit number "is not an
    # integer". Callers use the status to choose between "use it" and "warn
    # and fall back", so a malformed operator override never reaches shell
    # arithmetic — see wsi_compute_workers and start-wsi-app.sh's SOURCE
    # line, which must agree about which values were accepted.
    #
    # Normalising is not cosmetic. Surrounding whitespace survives a hand
    # edit of wsi.env, and a matching-digits check alone is not enough:
    # bash reads a leading zero as OCTAL inside (( )). A value whose digits
    # are all valid octal (064 = 52) still clamps correctly, so the damage
    # is confined to leading-zero values containing an 8 or a 9 — and there
    # it is severe. UVICORN_WORKERS=099 makes both `(( w < 1 ))` and
    # `(( w > cpu ))` raise "value too great for base"; because those clamps
    # are `(( ... )) && x=y` lists, errexit's &&-exemption swallows the
    # failure and the clamps are simply skipped. The untouched string "099"
    # is then handed to uvicorn, which reads it as decimal 99 — ninety-nine
    # workers on a 16-core host, with nothing in the journal but two cryptic
    # arithmetic errors that never name the variable. The same skipped
    # arithmetic makes wsi_compute_cache_max_bytes bail, silently disabling
    # RAM-aware DZI cache sizing. Stripping the zeros makes every accepted
    # value mean the same thing to bash and to uvicorn.
    local v="$1"
    v="${v#"${v%%[![:space:]]*}"}"
    v="${v%"${v##*[![:space:]]}"}"
    [[ -z "$v" ]] && { printf ''; return 0; }
    [[ "$v" =~ ^[0-9]+$ ]] || return 1
    # Single-pass leading-zero strip, written without extglob (it may be off
    # here). ${v%%[!0]*} is the run of leading zeros; removing it can empty
    # the string ("000"), which normalises back to "0".
    v="${v#"${v%%[!0]*}"}"
    [[ -z "$v" ]] && v=0
    # Reject anything that will not survive bash's signed 64-bit arithmetic.
    # Matching digits is not enough: 2^63 wraps to a NEGATIVE value and any
    # multiple of 2^64 wraps to exactly 0. Downstream that means a guard
    # like `(( x <= 0 ))` mistakes an unsatisfiable setting for "unlimited",
    # and `(( budget / x ))` divides by zero. Catching it at the single
    # entry point lets every consumer treat an accepted value as an ordinary
    # integer instead of re-guarding each arithmetic site. Round-tripping
    # through (( )) is the portable detector: a value that wrapped no longer
    # prints as what was written.
    #
    # This bounds each value individually, not the expressions built from
    # them: wsi_budget_bytes still computes `mem * (100 - frac)`, which an
    # in-range but absurd WSI_CACHE_RAM_FRACTION can overflow. That case is
    # contained downstream (`(( budget < 0 )) && budget=0`, then the
    # [1, nproc] clamp) rather than here.
    local roundtrip=0
    (( roundtrip = v )) || true
    [[ "$roundtrip" == "$v" ]] || return 2
    printf '%s' "$v"
    return 0
}

_wsi_reject_reason() {
    # _wsi_reject_reason STATUS -> the human half of a warning, matching the
    # status codes wsi_valid_uint returns. Kept next to it so the two cannot
    # drift into telling an operator to hunt for a typo in a value whose only
    # problem is its magnitude.
    case "$1" in
        2) printf 'too large for 64-bit arithmetic' ;;
        *) printf 'not a non-negative integer' ;;
    esac
}

_wsi_envint() {
    # _wsi_envint VARNAME DEFAULT -> the env value, normalised by
    # wsi_valid_uint, when it is a non-negative integer that fits in signed
    # 64-bit; otherwise DEFAULT. Guards against a malformed wsi.env entry
    # silently zeroing a budget.
    #
    # Normalising means the returned digits can differ from the raw env
    # value:
    #
    #   "075"  leading zeros are stripped, yielding 75. Left verbatim it
    #          would be read by (( )) as octal 61 — the hazard the
    #          normalisation exists to remove.
    #   " 75"  surrounding whitespace is stripped, yielding 75, so a stray
    #          space does not silently discard the operator's setting.
    #   >=2^63 rejected, falling back to DEFAULT. Such a value would wrap
    #          negative (or, for a multiple of 2^64, to exactly 0) inside
    #          (( )). This is the one class deliberately NOT honoured: no
    #          such value can be satisfied anyway.
    #
    # A rejected value falls back silently, unlike the UVICORN_WORKERS path
    # which warns on stderr. These knobs are read several times per run, so
    # warning here would repeat itself. The trade-off is uneven, and worse
    # than it first looks:
    #
    #   FLOOR_BYTES        the value in EFFECT is always printed
    #                      (set-workers.sh's Notes block) — but a rejected
    #                      floor prints the default, which looks exactly
    #                      like success. Visible, not diagnostic.
    #   RAM_FRACTION       printed only on the auto-sizing path, i.e. while
    #                      DZI_CACHE_MAX_BYTES is unset AND RAM is readable.
    #                      Pin the cache and it vanishes, though it still
    #                      moves the default-path worker cap.
    #   RESERVE_MIN_BYTES  never printed.
    #   CEIL_BYTES         never printed.
    #
    # So a typo in any of the four can be silently replaced by the default
    # with nothing that identifies it as a typo.
    local v
    v="$(wsi_valid_uint "${!1:-}")" || { printf '%s' "$2"; return 0; }
    if [[ -n "$v" ]]; then
        printf '%s' "$v"
    else
        printf '%s' "$2"
    fi
}

wsi_cache_fraction()    { _wsi_envint WSI_CACHE_RAM_FRACTION 75; }
wsi_cache_reserve_min() { _wsi_envint WSI_CACHE_RESERVE_MIN_BYTES 2147483648; }
wsi_cache_floor()       { _wsi_envint WSI_CACHE_FLOOR_BYTES 2147483648; }
wsi_cache_ceil()        { _wsi_envint WSI_CACHE_CEIL_BYTES 8589934592; }

wsi_mem_total_bytes() {
    # Total RAM in bytes, clamped to this process's cgroup v2 memory.max
    # when that is a finite value (so a systemd MemoryMax= is honoured).
    # Prints nothing on failure so callers fall back to the app default.
    local kb bytes cg path v
    kb=$(awk '/^MemTotal:/{print $2; exit}' /proc/meminfo 2>/dev/null || true)
    [[ "$kb" =~ ^[0-9]+$ ]] || return 0
    bytes=$(( kb * 1024 ))
    # Best-effort cgroup v2 clamp. /proc/self/cgroup is "0::<path>" under
    # the unified hierarchy; the controller file lives at
    # /sys/fs/cgroup/<path>/memory.max and reads "max" (unlimited) or an
    # integer byte count.
    cg=$(awk -F: '/^0::/{print $3; exit}' /proc/self/cgroup 2>/dev/null || true)
    if [[ -n "$cg" ]]; then
        path="/sys/fs/cgroup${cg}/memory.max"
        if [[ -r "$path" ]]; then
            v=$(cat "$path" 2>/dev/null || true)
            if [[ "$v" =~ ^[0-9]+$ ]] && (( v > 0 )) && (( v < bytes )); then
                bytes=$v
            fi
        fi
    fi
    printf '%s' "$bytes"
}

wsi_default_workers() {
    # wsi_default_workers CPU -> min(max(2, CPU/2), 8), at least 1.
    local cpu="$1" w
    w=$(( cpu / 2 ))
    (( w < 2 )) && w=2
    (( w > 8 )) && w=8
    (( w < 1 )) && w=1
    printf '%s' "$w"
}

wsi_budget_bytes() {
    # wsi_budget_bytes MEM_BYTES -> reclaimable page-cache budget.
    # reserve = max(RAM*(1-fraction), reserve_min); budget = RAM - reserve.
    local mem="$1" frac reserve_min reserve budget
    [[ "$mem" =~ ^[0-9]+$ ]] || { printf '0'; return 0; }
    frac=$(wsi_cache_fraction)
    reserve_min=$(wsi_cache_reserve_min)
    reserve=$(( mem * (100 - frac) / 100 ))
    (( reserve < reserve_min )) && reserve=$reserve_min
    budget=$(( mem - reserve ))
    (( budget < 0 )) && budget=0
    printf '%s' "$budget"
}

wsi_ram_worker_cap() {
    # wsi_ram_worker_cap MEM_BYTES -> most workers that can each get >= FLOOR.
    #
    # WSI_CACHE_FLOOR_BYTES=0 is a plausible way for an operator to say
    # "no minimum", but to the bare division it is a divide-by-zero: it
    # would print "division by 0" to the journal and — because this runs
    # inside `cap=$(...)` whose failure the caller's `(( w > cap ))` then
    # reads as an empty string — quietly settle on ONE worker on a box
    # sized for several. Read it as intended instead: every worker
    # qualifies, so the cap is the budget itself and the caller's own
    # [1, nproc] clamp does the rest.
    #
    # Only the literal 0 needs handling here. Values too large for signed
    # 64-bit arithmetic (which would wrap to a negative number or, for a
    # multiple of 2^64, to exactly 0 and reintroduce the division) cannot
    # reach this point: wsi_valid_uint rejects them, so wsi_cache_floor
    # returns the documented default instead.
    local mem="$1" budget floor cap
    budget=$(wsi_budget_bytes "$mem")
    floor=$(wsi_cache_floor)
    if [[ "$floor" == 0 ]]; then
        cap=$budget
    else
        cap=$(( budget / floor ))
    fi
    (( cap < 1 )) && cap=1
    printf '%s' "$cap"
}

wsi_compute_workers() {
    # wsi_compute_workers EXPLICIT PERCENT CPU [MEM_BYTES] -> worker count.
    # EXPLICIT / PERCENT may be empty. The RAM cap is applied ONLY on the
    # default path (neither var set) so an explicit operator choice wins;
    # the PERCENT path is intentionally uncapped. An empty/unreadable
    # MEM_BYTES skips the RAM cap.
    local explicit="$1" percent="$2" cpu="$3" mem="${4:-}" w cap
    # Validate-or-fallback, mirroring _wsi_envint for the WSI_CACHE_*
    # tunables: a non-integer value (fat-fingered wsi.env edit like
    # UVICORN_WORKERS=four or UVICORN_WORKERS_PERCENT=5O) would otherwise
    # blow up the arithmetic below under `set -eu` and crash-loop
    # wsi-app.service with a journald error that never names the variable.
    # wsi_valid_uint tolerates surrounding whitespace, so a stray space in
    # wsi.env still works.
    local _rc
    explicit="$(wsi_valid_uint "$explicit")" || {
        _rc=$?
        echo "WSI: ignoring UVICORN_WORKERS='$1' ($(_wsi_reject_reason "$_rc")); using the default formula" >&2
        explicit=""
    }
    percent="$(wsi_valid_uint "$percent")" || {
        _rc=$?
        echo "WSI: ignoring UVICORN_WORKERS_PERCENT='$2' ($(_wsi_reject_reason "$_rc")); using the default formula" >&2
        percent=""
    }
    if [[ -n "$explicit" ]]; then
        w="$explicit"
    elif [[ -n "$percent" ]]; then
        w=$(( cpu * percent / 100 ))
    else
        w=$(wsi_default_workers "$cpu")
        if [[ "$mem" =~ ^[0-9]+$ ]] && (( mem > 0 )); then
            cap=$(wsi_ram_worker_cap "$mem")
            (( w > cap )) && w=$cap
        fi
    fi
    (( w < 1 )) && w=1
    (( w > cpu )) && w=$cpu
    printf '%s' "$w"
}

wsi_compute_cache_max_bytes() {
    # wsi_compute_cache_max_bytes MEM_BYTES WORKERS -> per-worker byte cap.
    # Prints empty when inputs are unusable so the caller keeps the app's
    # built-in DZI_CACHE_MAX_BYTES default.
    local mem="$1" workers="$2" budget floor ceil per
    { [[ "$mem" =~ ^[0-9]+$ ]] && (( mem > 0 )); } || { printf ''; return 0; }
    { [[ "$workers" =~ ^[0-9]+$ ]] && (( workers > 0 )); } || { printf ''; return 0; }
    budget=$(wsi_budget_bytes "$mem")
    floor=$(wsi_cache_floor)
    ceil=$(wsi_cache_ceil)
    per=$(( budget / workers ))
    (( per < floor )) && per=$floor
    (( per > ceil )) && per=$ceil
    printf '%s' "$per"
}
