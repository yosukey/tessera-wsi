#!/usr/bin/env bash
# =====================================================================
# Tessera - optional operational tools installer
#
# Tested on: Ubuntu 26.04 LTS
#
# Usage:
#   sudo bash infra/install-ops-tools.sh
#
# This script is OPTIONAL. The core Tessera stack (wsi-app, wsi-sync,
# nginx) installed by infra/install.sh runs fine without it. Run this
# script when you want the following operator utilities added to the
# host:
#
#   Security : unattended-upgrades, fail2ban
#   Storage  : smartmontools
#   Metrics  : sysstat (sar history)
#   Logs     : goaccess (nginx log analyzer)
#   CLI      : htop, ncdu, jq, tmux
#
# Side-effects (in addition to apt-get install):
#   * /etc/apt/apt.conf.d/20auto-upgrades         -> daily security updates
#   * /etc/apt/apt.conf.d/52unattended-upgrades-reboot
#                                                 -> reboot at 03:00 LOCAL
#                                                    when an update flags
#                                                    /var/run/reboot-required
#   * /etc/fail2ban/jail.d/wsi-sshd.conf          -> sshd jail enabled
#   * /etc/default/sysstat                        -> ENABLED="true"
#   * systemctl enable --now {fail2ban,smartd,sysstat}.service
#
# This script is idempotent: re-running it is safe and will not
# overwrite any of the config files above once they exist (the one
# exception: ENABLED="false" in /etc/default/sysstat is flipped back to
# "true" on every run). To force a fresh write, remove the relevant file
# and re-run.
#
# See QUICKSTART_FOR_UBUNTU.md §5 "Bundled operational tools" for the
# full operational policy and usage examples.
# =====================================================================
# -E (errtrace), as in install.sh / update.sh: without it the ERR trap below
# does not fire for a command failing inside a function, which then ends the
# run through `set -e` with no "FAILED at line" message.
set -Eeuo pipefail

log()  { printf '\033[1;34m[ops-tools]\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m[warn]\033[0m %s\n'      "$*" >&2; }
die()  { printf '\033[1;31m[error]\033[0m %s\n'     "$*" >&2; exit 1; }

POST_INSTALL_WARNINGS=()

on_err() {
    local rc=$?
    printf '\033[1;31m[ops-tools]\033[0m FAILED at line %d (exit %d): %s\n' \
        "$1" "$rc" "$2" >&2
    exit "$rc"
}
trap 'on_err "$LINENO" "$BASH_COMMAND"' ERR

if [[ $EUID -ne 0 ]]; then
    die "Please run as root (sudo bash infra/install-ops-tools.sh)"
fi

# Wait for any concurrent apt/dpkg process to release its locks before
# running the script's own apt-get update / install. See install.sh for
# the full rationale — this is the same helper duplicated to keep each
# script usable stand-alone. Override the budget with APT_LOCK_TIMEOUT=<seconds>.
apt_wait_lock() {
    local timeout="${APT_LOCK_TIMEOUT:-300}"
    local waited=0
    local interval=3
    local locks=(
        /var/lib/dpkg/lock-frontend
        /var/lib/dpkg/lock
        /var/lib/apt/lists/lock
        /var/cache/apt/archives/lock
    )
    local have_fuser=0
    command -v fuser >/dev/null 2>&1 && have_fuser=1
    while :; do
        local held=""
        if [[ "$have_fuser" == "1" ]]; then
            local lock
            for lock in "${locks[@]}"; do
                if [[ -e "$lock" ]] && fuser "$lock" >/dev/null 2>&1; then
                    held="$lock"
                    break
                fi
            done
        else
            if pgrep -x apt-get >/dev/null 2>&1 \
               || pgrep -x apt    >/dev/null 2>&1 \
               || pgrep -x dpkg   >/dev/null 2>&1 \
               || pgrep -x unattended-upgr >/dev/null 2>&1; then
                held="apt/dpkg process"
            fi
        fi
        [[ -z "$held" ]] && return 0
        if (( waited >= timeout )); then
            warn "apt/dpkg lock still held after ${timeout}s ($held); proceeding anyway."
            warn "If apt fails immediately after this, the holder may be wedged. Diagnose with:"
            warn "    sudo lsof $held   ;   sudo systemctl status unattended-upgrades"
            warn "    sudo ps -fp \$(sudo fuser $held 2>/dev/null)"
            # Return 0 so the caller's set -e / ERR trap doesn't abort here.
            # apt-get below still has -o DPkg::Lock::Timeout=300 as a real
            # backstop, and if it ultimately fails it produces a much clearer
            # diagnostic than this helper can.
            return 0
        fi
        if (( waited == 0 || waited % 15 == 0 )); then
            local holders=""
            if [[ "$have_fuser" == "1" && -e "$held" ]]; then
                holders=" (pids:$(fuser "$held" 2>/dev/null | tr -s ' ' || true))"
            fi
            log "Waiting for apt/dpkg lock $held${holders}; ${waited}s/${timeout}s elapsed..."
        fi
        sleep "$interval"
        waited=$((waited + interval))
    done
}

# ---- 1. apt-get install ---------------------------------------------
log "Installing operational tool packages..."
export DEBIAN_FRONTEND=noninteractive
apt_wait_lock
apt-get -o DPkg::Lock::Timeout=300 update -qq
apt_wait_lock
apt-get -o DPkg::Lock::Timeout=300 install -y --no-install-recommends \
    unattended-upgrades \
    fail2ban \
    smartmontools \
    sysstat \
    goaccess \
    htop ncdu jq tmux

# ---- 2. unattended-upgrades: daily security updates -----------------
# 20auto-upgrades turns on Update-Package-Lists / Unattended-Upgrade and
# a weekly autoclean. Only write when absent so any operator tweak
# survives re-runs. (The unattended-upgrades postinst may already have
# created a default version of this file; leaving it alone is safe —
# security auto-upgrades remain enabled.)
if [[ ! -f /etc/apt/apt.conf.d/20auto-upgrades ]]; then
    log "Writing /etc/apt/apt.conf.d/20auto-upgrades ..."
    cat >/etc/apt/apt.conf.d/20auto-upgrades <<'EOF'
APT::Periodic::Update-Package-Lists "1";
APT::Periodic::Unattended-Upgrade "1";
APT::Periodic::AutocleanInterval "7";
EOF
fi

# Auto-reboot policy lives in a SEPARATE drop-in (52...) so distro
# upgrades of the unattended-upgrades package — which manage 50... —
# never collide with this script's settings. APT merges all apt.conf.d
# files in lexical order, so 52 wins over 50's "Automatic-Reboot false"
# default.
# Reboot at 03:00 server-local time when a security update has flagged
# /var/run/reboot-required.
if [[ ! -f /etc/apt/apt.conf.d/52unattended-upgrades-reboot ]]; then
    log "Writing /etc/apt/apt.conf.d/52unattended-upgrades-reboot (auto-reboot at 03:00 local) ..."
    cat >/etc/apt/apt.conf.d/52unattended-upgrades-reboot <<'EOF'
// Managed by infra/install-ops-tools.sh — edit values, do not remove the header.
Unattended-Upgrade::Automatic-Reboot "true";
Unattended-Upgrade::Automatic-Reboot-WithUsers "true";
Unattended-Upgrade::Automatic-Reboot-Time "03:00";
EOF
fi

# Surface the current timezone so the operator can notice a UTC-only
# host before "03:00" silently lands at lunchtime local.
if command -v timedatectl >/dev/null 2>&1; then
    OPS_TZ="$(timedatectl show --property=Timezone --value 2>/dev/null || echo unknown)"
else
    OPS_TZ="unknown"
fi
log "Auto-reboot for security updates: 03:00 (timezone: ${OPS_TZ})."

# ---- 3. fail2ban: ban SSH brute-force sources -----------------------
# Drop-in jail keyed to sshd only. Deliberately no nginx jail is added
# for the admin login endpoint: the app already has its own
# rate-limit there, and a second layer with IP-level bans risks locking
# out a legitimate admin behind shared NAT.
if [[ ! -f /etc/fail2ban/jail.d/wsi-sshd.conf ]]; then
    log "Writing /etc/fail2ban/jail.d/wsi-sshd.conf ..."
    cat >/etc/fail2ban/jail.d/wsi-sshd.conf <<'EOF'
# Managed by infra/install-ops-tools.sh — edit values, do not remove the header.
[sshd]
enabled  = true
maxretry = 5
findtime = 10m
bantime  = 1h
EOF
fi
systemctl enable --now fail2ban.service

# ---- 4. smartmontools: SMART monitoring of physical disks -----------
# smartd may fail to start for several reasons: hypervisors that hide
# physical disks, hardware RAID controllers, USB-attached storage, or
# NVMe drives whose SMART attributes the default smartd.conf can't
# read. Tolerate that — SMART monitoring is best-effort and its
# absence must not break this script. Surface a warning in the final
# summary with a diagnostic hint so the operator can decide whether
# action is needed.
if systemctl enable --now smartd.service 2>/dev/null; then
    log "smartd.service enabled."
else
    warn "smartd.service did not start. Run 'journalctl -xeu smartd.service' to see why"
    warn "  (common causes: VM with no physical disks, hardware RAID, USB storage, or smartd.conf needs tuning)."
    POST_INSTALL_WARNINGS+=("smartd.service not started — SMART monitoring inactive (run 'journalctl -xeu smartd.service' for the cause)")
fi

# ---- 5. sysstat: historical CPU / IO / memory via sar ---------------
# The default install ships /etc/default/sysstat with ENABLED="false".
# Flip it on idempotently (sed is a no-op if already "true"), then start
# the service so the cron-driven collector runs from now.
if [[ -f /etc/default/sysstat ]]; then
    sed -i 's/^ENABLED="false"/ENABLED="true"/' /etc/default/sysstat
fi
systemctl enable --now sysstat.service

# ---- 6. Summary -----------------------------------------------------
cat <<EOS

================================================================
 Operational tools installed.

 Daily security updates : enabled (unless an existing 20auto-upgrades was kept)
 Auto-reboot            : 03:00 (timezone: ${OPS_TZ})
 fail2ban sshd jail     : enabled (maxretry=5, bantime=1h, unless an existing
                          jail.d/wsi-sshd.conf was kept)
 sysstat (sar)          : collecting
 smartd                 : $(systemctl is-active smartd.service 2>/dev/null || echo "inactive (see warnings)")

 See QUICKSTART_FOR_UBUNTU.md §5 "Bundled operational tools" for
 usage examples and how to tune each tool.
================================================================
EOS

if (( ${#POST_INSTALL_WARNINGS[@]} > 0 )); then
    printf '\n\033[1;33m[warn]\033[0m completed with %d warning(s):\n' \
        "${#POST_INSTALL_WARNINGS[@]}" >&2
    for w in "${POST_INSTALL_WARNINGS[@]}"; do
        printf '\033[1;33m[warn]\033[0m   - %s\n' "$w" >&2
    done
fi
