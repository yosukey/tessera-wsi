# Operations Guide

How to run and maintain a native Ubuntu (systemd) deployment: the
maintenance scripts, updates, backup / restore, server sync, sign-in,
caches, logs and analytics. All `infra/*.sh` scripts must be run as root
(`sudo bash infra/<script>.sh`).

Related documents:
- [`../QUICKSTART_FOR_UBUNTU.md`](../QUICKSTART_FOR_UBUNTU.md) — full step-by-step production install.
- [`architecture.md`](./architecture.md) — architecture overview and supported-format table.
- [`developer-guide.md`](./developer-guide.md) — implementation-level reference: repository map, endpoints, schema evolution, feature notes.

## Contents

**1. Updates and services**

- [Updating to the latest code — `infra/update.sh`](#updating-to-the-latest-code--infraupdatesh)
  - [Automatic rollback](#automatic-rollback)
  - [Basic usage](#basic-usage)
  - [Options (environment variables)](#options-environment-variables)
  - [Private forks and mirrors](#private-forks-and-mirrors)
- [Upstream update check](#upstream-update-check)
- [Live-sync coordinator — `wsi-sync.service`](#live-sync-coordinator--wsi-syncservice)
  - [Session lifetime and `WSI_SYNC_*` tunables](#session-lifetime-and-wsi_sync_-tunables)
  - [Capacity and per-IP limits](#capacity-and-per-ip-limits)
  - [Cross-site WebSocket check](#cross-site-websocket-check)
  - [Presenter on more than one device](#presenter-on-more-than-one-device)
  - [Unpublished slides during a session](#unpublished-slides-during-a-session)
  - [Refused connections — close codes](#refused-connections--close-codes)
  - [Participant list — screen sizes](#participant-list--screen-sizes)
  - [Share link (follower URL / QR)](#share-link-follower-url--qr)
- [Auxiliary systemd units](#auxiliary-systemd-units)

**2. Backup, migration, and server moves**

- [Backup & restore — admin dashboard](#backup--restore--admin-dashboard)
  - [Cloudflare Tunnel sessions](#cloudflare-tunnel-sessions)
  - [What's in an archive](#whats-in-an-archive)
  - [Backup version policy](#backup-version-policy)
  - [How schema changes are absorbed](#how-schema-changes-are-absorbed)
  - [Free-space checks](#free-space-checks)
  - [Creating a backup](#creating-a-backup)
  - [Downloading and deleting](#downloading-and-deleting)
  - [Restoring](#restoring)
  - [What happens during apply](#what-happens-during-apply)
  - [Recovering from a failed restore](#recovering-from-a-failed-restore)
  - [Recovering from an interrupted restore](#recovering-from-an-interrupted-restore)
  - [Restoring WSI files after a metadata-only restore](#restoring-wsi-files-after-a-metadata-only-restore)
  - [Resetting the install (Backup & Restore → Reset)](#resetting-the-install-backup--restore--reset)
  - [Off-the-record: making a backup without the dashboard](#off-the-record-making-a-backup-without-the-dashboard)
  - [Configuration](#configuration)
- [Slide export / import (cross-Tessera migration)](#slide-export--import-cross-tessera-migration)
  - [What's in a bundle](#whats-in-a-bundle)
  - [Version policy](#version-policy)
  - [Migration procedure](#migration-procedure)
  - [Slides refused for their pyramid layout](#slides-refused-for-their-pyramid-layout)
  - [Retrying a partial import](#retrying-a-partial-import)
- [Backup server and server moves — `infra/sync-from.sh`](#backup-server-and-server-moves--infrasync-fromsh)
  - [What a sync copies](#what-a-sync-copies)
  - [One-time setup](#one-time-setup)
  - [Running a sync](#running-a-sync)
  - [When the main server is down](#when-the-main-server-is-down)
  - [Rolling back the last sync](#rolling-back-the-last-sync)
  - [Moving to a new server — `--all`](#moving-to-a-new-server----all)
  - [Where things are kept](#where-things-are-kept)
  - [Troubleshooting](#troubleshooting)

**3. Users, authentication, and security**

- [User management](#user-management)
- [Two-factor sign-in (dashboard)](#two-factor-sign-in-dashboard)
  - [Turning it on and off](#turning-it-on-and-off)
  - [When someone loses their device](#when-someone-loses-their-device)
  - [`WSI_MFA_KEY`](#wsi_mfa_key)
- [App-access sign-in](#app-access-sign-in)
  - [Authentication modes](#authentication-modes)
  - [Customizing the login screen text](#customizing-the-login-screen-text)
  - [Signing every visitor out](#signing-every-visitor-out)
  - [LDAP over TLS: certificate verification](#ldap-over-tls-certificate-verification)
  - [IP access control](#ip-access-control)
- [Secrets and security posture](#secrets-and-security-posture)
  - [Rotating `JWT_SECRET`](#rotating-jwt_secret)
  - [Login throttles](#login-throttles)
  - [Initial admin password](#initial-admin-password)
  - [Password policy](#password-policy)
  - [Content-Security-Policy](#content-security-policy)
- [Cloudflare Access + Tunnel integration](#cloudflare-access--tunnel-integration)
  - [Ingress paths (A / B / C)](#ingress-paths-a--b--c)
  - [Configuring the app](#configuring-the-app)
  - [Configuring Cloudflare (Paths A and B)](#configuring-cloudflare-paths-a-and-b)
  - [Carrying the CF Access identity onto the LAN host](#carrying-the-cf-access-identity-onto-the-lan-host)
  - [Trust model](#trust-model)
  - [Verifying the deployment](#verifying-the-deployment)
  - [Using the app's own login instead of CF Access SSO (seat-limit workaround)](#using-the-apps-own-login-instead-of-cf-access-sso-seat-limit-workaround)
  - [Break-glass: unauthenticated raw-IP access](#break-glass-unauthenticated-raw-ip-access)
  - [Skip authentication for direct LAN access (dashboard setting)](#skip-authentication-for-direct-lan-access-dashboard-setting)

**4. Performance and caching**

- [WSI tile cache size — `infra/set-cache-size.sh`](#wsi-tile-cache-size--infraset-cache-sizesh)
  - [What is DZI_CACHE_SIZE?](#what-is-dzi_cache_size)
  - [Cache thrashing](#cache-thrashing)
  - [Soft byte cap — `DZI_CACHE_MAX_BYTES`](#soft-byte-cap--dzi_cache_max_bytes)
  - [Usage](#usage)
  - [Observability in the admin dashboard](#observability-in-the-admin-dashboard)
- [nginx tile cache — `infra/nginx-cache.sh`](#nginx-tile-cache--infranginx-cachesh)
  - [Disk usage statistics — `wsi-cache-stat.timer`](#disk-usage-statistics--wsi-cache-stattimer)
  - [Usage](#usage-1)
  - [What "Clear cache" does](#what-clear-cache-does)
  - [What "Change max_size" does](#what-change-max_size-does)
  - [Examples](#examples)
  - [Environment overrides](#environment-overrides)
  - [Publish state, deletion, replacement, and cache staleness](#publish-state-deletion-replacement-and-cache-staleness)
  - [When to clear the cache](#when-to-clear-the-cache)
  - [Browser caching of tiles](#browser-caching-of-tiles)
  - [When to change `max_size`](#when-to-change-max_size)
- [Uvicorn worker count — `infra/set-workers.sh`](#uvicorn-worker-count--infraset-workerssh)
  - [Background](#background)
  - [Interaction with DZI_CACHE_SIZE](#interaction-with-dzi_cache_size)
  - [Usage](#usage-2)
  - [Examples](#examples-1)
- [Database connection pool](#database-connection-pool)
  - [The failure mode this sizing prevents](#the-failure-mode-this-sizing-prevents)
  - [How it is sized](#how-it-is-sized)
- [Slide-metadata resolve cache](#slide-metadata-resolve-cache)
  - [The amplifier](#the-amplifier)
  - [Keeping it correct across workers](#keeping-it-correct-across-workers)
  - [Knobs](#knobs)
- [Performance measurement — admin Performance page](#performance-measurement--admin-performance-page)
  - [Server test](#server-test)
  - [Browser test](#browser-test)
  - [Reading the results](#reading-the-results)
  - [Exporting results with their conditions](#exporting-results-with-their-conditions)

**5. Logs, audit, and analytics**

- [Audit history — `/admin/history`](#audit-history--adminhistory)
- [Error logs — `/admin/logs`](#error-logs--adminlogs)
- [nginx access log](#nginx-access-log)
- [Access analytics](#access-analytics)
  - [What is recorded, and when](#what-is-recorded-and-when)
  - [Staff vs learner traffic](#staff-vs-learner-traffic)
  - [Privacy modes](#privacy-modes)
  - [`ANALYTICS_HASH_SALT`](#analytics_hash_salt)
  - [Consent toast](#consent-toast)
  - [Google Analytics (optional, external)](#google-analytics-optional-external)
  - [Retention and CSV export](#retention-and-csv-export)
  - [Bucket time zone — `WSI_ANALYTICS_TIMEZONE`](#bucket-time-zone--wsi_analytics_timezone)
  - [Dashboard query time](#dashboard-query-time)

**6. Removal and forks**

- [Uninstalling](#uninstalling)
  - [Standard uninstall steps](#standard-uninstall-steps)
  - [If you also ran `infra/install-ops-tools.sh`](#if-you-also-ran-infrainstall-ops-toolssh)
- [AGPL compliance — updating repository URLs in a fork](#agpl-compliance--updating-repository-urls-in-a-fork)
  - [Files to update](#files-to-update)
  - [Steps](#steps)

---

## Updating to the latest code — `infra/update.sh`

> **Keep tracked files in your clone unedited.** Settings belong outside
> the clone: the upload limit in `WSI_MAX_UPLOAD_BYTES` in `/etc/wsi/wsi.env`
> (QUICKSTART §6.5), the cache size in `infra/nginx-cache.sh`, the access log
> in `/etc/nginx/snippets/wsi-access-log.conf` ([nginx access
> log](#nginx-access-log)). git refuses to check out a release over a local
> edit, so `update.sh` stops in step 1 (nothing is stopped or deployed), names
> the files that block its checkout and prints the command that undoes them;
> `git -C ~/wsi status` lists them too (`~/wsi` = your clone).

`infra/update.sh` is a one-command updater that runs the following steps
in sequence, with progress output and elapsed-time tracking. It first refuses
to start (nothing fetched, stopped or deployed) while a restore / reset waits
for its restart (`/run/wsi/restore.request`) or an interrupted one is
recorded (`$DATA_DIR/.restore-apply.journal`): the update's restart would
carry it out under the new release, and a rolled-back update would put the
pre-update `wsi.db` back over files the restore already replaced. Restart
`wsi-app` first (it applies the request, or drops one over 10 minutes old),
check the Backup & Restore tab, then update. The check is repeated once the
services are stopped; a request that appeared meanwhile ends the update with
the old release restarted, which applies it. `WSI_ALLOW_PENDING_RESTORE=1`
skips the check. The start script also refuses a request made under another
release than the one starting (`"stage": "version_changed"`).

Run `update.sh` (and `sync-from.sh`) from **your own clone** (`cd ~/wsi && sudo
bash infra/update.sh`), never from `/opt/wsi`: both run as root and source
helpers from their own tree, so they refuse a tree the service user owns or
could rewrite (anything inside `/opt/wsi`; the clone, its `infra/` or
`infra/lib/` directory, the script or `infra/lib/wsi-common.sh` being
world-writable; or any of them owned by an account that is neither root nor
the user who ran `sudo`). An `origin` URL that is
`http://`, `git://` or `ftp://` gets a warning (from `update.sh`, and from
`install.sh` when it checks out a release itself): a release fetched over it can be
rewritten by anything on the path, and its `install.sh` runs as root. Use
`https://` or `ssh`, and consider `WSI_VERIFY_RELEASE_TAGS=1` (below).

1. `git fetch --tags --prune origin`, then check out the highest
   `vX.Y.Z` release tag on `origin` (or the ref pinned via `GIT_REF`,
   or stay on the current branch HEAD with `WSI_TRACK_BRANCH=1`). Only a
   final release counts: a pre-release tag such as `v2.0.0-rc1` is never
   picked, however new; deploy one with `GIT_REF`.
   Runs as the invoking non-root user so stored credentials work.
2. Stop `wsi-app.service` (and `wsi-sync.service` if still running), then snapshot the SQLite DB for the rollback (see [Automatic rollback](#automatic-rollback))
3. Re-run `infra/install.sh` (idempotent — rsync, pip, npm build, config files; it also runs `systemctl daemon-reload` for the units it installs, and `nginx -t` plus a reload for the nginx config. A config `nginx -t` rejects is not left installed: `install.sh` puts the previous nginx files back and stops, so the next nginx restart still has a valid config)
4. `systemctl enable --now wsi-app.service` → `systemctl enable --now wsi-sync.service`
5. Health-check `GET /api/health` on both the backend (port 8000, polls up to 30 s) and the sync coordinator (port 8001, polls up to 30 s). Either one not answering in time, or either unit entering the failed state, fails the update (and rolls it back)
6. Print a summary with useful `systemctl` / `journalctl` commands

**Preserved across updates:**

| Path | Contents |
|---|---|
| `/etc/wsi/wsi.env` | Environment variables and secrets — never regenerated. `update.sh` adds a `WSI_MFA_KEY` when the file has none (see [`WSI_MFA_KEY`](#wsi_mfa_key)), and rewrites a line in it only when you pass `WSI_LAN_HOST` / `WSI_LAN_PROBE_TIMEOUT_MS` / `WSI_PUBLIC_BASE_URL` to it, see [Setting the three build-time values](#setting-the-three-build-time-values). (`set-workers.sh` and `set-cache-size.sh` also edit their own keys when you run them.) |
| `/var/lib/wsi/` | WSI files, thumbnails, assets, SQLite database |

If `WSI_ALLOWED_EXTENSIONS` in `/etc/wsi/wsi.env` lists multi-file formats
such as `.mrxs`, `.vms` or `.vmu`, the backend ignores them (an upload carries
one file, so their companion data files can never be stored), logging a
WARNING that names them at startup. Delete them from the line (or copy the
line from `infra/wsi.env.example`) and restart `wsi-app` to silence it.

`install.sh` runs as root but does not follow symlinks inside
`/var/lib/wsi/` or `/opt/wsi/`, because the `wsi` user can write to both. If
an entry such as `/var/lib/wsi/thumbs` (or its parent) is a symlink,
`install.sh` does not create it, change its owner, or change its mode. It
prints a warning and continues. If you moved `wsi/` to another volume with a
symlink on purpose, that setup keeps working, but you must set the target's
owner (`wsi:wsi`) and mode yourself. Two more things to know for such a setup:

- The units run with `ProtectSystem=strict` and write only to `ReadWritePaths=`
  (`/var/lib/wsi`, plus `/etc/avahi/services` and `/run/wsi` for
  `wsi-app`). A symlink target outside `/var/lib/wsi` is read-only to the
  service, so uploads fail with a read-only-filesystem error until you add the
  target to `ReadWritePaths=` in **both** `wsi-app.service` and
  `wsi-sync.service` (a drop-in under `/etc/systemd/system/<unit>.d/` survives
  updates).
- A **restore or reset replaces `wsi/` and `thumbs/` as directories**. If either
  is a symlink, the link itself is moved into the `.pre-restore-*` rollback
  directory and a real directory takes its place — a full restore would then
  unpack the whole corpus onto the primary volume. Check the free space first,
  and afterwards point the link back (stop the services, move the data to the
  other volume, re-create the symlink, start the services).

**Apply or cancel a staged restore before updating.** A restore staged (the
**Backup & Restore** tab's Restore preview) but not yet applied does not carry over an
update: its schema-upgrade trial was made by the version that staged it, so
the new version refuses to apply it with HTTP 409 (*staged by another version
of the app … stage the archive again* / `STAGE_STALE`). Nothing is lost — cancel it, and
restore the same backup again after the update to stage it anew.

`install.sh` also runs `pip` and `npm` as the `wsi` user inside `/opt/wsi/`.
For that reason it installs the files that root uses (the systemd units,
`/usr/local/bin/wsi-cache-stat`, the nginx configs, and the `wsi.env`
template) from your source clone, not from `/opt/wsi/`. For the same reason
it refuses to run from a copy inside `/opt/wsi/`: always run it from the
clone. And it never writes into `/opt/wsi/` as root: the copy of your clone
into it is made by the `wsi` user (from a staging copy that root first makes
in a private directory under `/tmp`, because `wsi` usually cannot read your
home directory). If that copy fails with `Permission denied`, something under
`/opt/wsi/` is not owned by `wsi` — typically left by a command run there as
root; `sudo chown -R -h wsi:wsi /opt/wsi` and re-run.

### Automatic rollback

If any step fails after the services have been stopped (step 2 onwards) —
or once the git checkout/pull in step 1 has already moved the working tree —
the script automatically puts the repo directory back as it found it (HEAD
on the same branch, or detached, as before) with `git reset --hard <pre-pull
SHA>`, re-runs `install.sh`, and restarts `wsi-app` and `wsi-sync` to
restore the previous working state. Only the branch you started on is reset;
a branch step 1 switched to and fast-forwarded (`GIT_REF=<branch>`,
`WSI_TRACK_BRANCH=1`) keeps its new, origin-matching position. If the failure comes before step 3 has
run `install.sh`, nothing has been deployed yet. The script then only resets
the repo and starts the services again, without re-running `install.sh`.

Interrupting the update (Ctrl-C, a dropped SSH session, or `kill`) triggers
the same recovery. If you press Ctrl-C again during the recovery, it stops
the command that is running at that moment, but the script still starts the
services before it exits. A `kill` sent to the script alone takes effect
only after the command that is running (for example `install.sh`) finishes.
If the script's output was piped (`sudo bash infra/update.sh | tee update.log`)
and the reader goes away (the SSH session dropped and took `tee` with it),
the recovery still runs; its output goes to `/var/log/wsi-update-recovery.log`
(`WSI_UPDATE_RECOVERY_LOG` overrides the path). Once the new release has
passed the health check, nothing rolls it back: a Ctrl-C or a failed write
while the closing summary prints only cuts the summary short.

**The database is rolled back too.** Right after stopping the services in
step 2, the script snapshots the SQLite DB (as the `wsi` user, via SQLite's
backup API) into a `.wsi-update-snapshot.*` directory next to it. This is
needed because the new release records its version in the DB when it first
starts, and an older release refuses to start against a DB that a newer one
has opened. So if the failure comes after step 4 started the new release,
the rollback puts the snapshot back before starting the previous release.
Anything written while the new release was up is lost with it. The snapshot
is deleted once the update, or the rollback, is healthy. If taking the
snapshot fails, the update stops there, resets the repo, and starts the
services again. If there is no DB file at the path in `DATABASE_URL`, the
script prints a warning and continues without a snapshot. The warning is
expected only before the app has started for the first time.

After a rollback the script checks the backend's health again. It prints
`Rolled back to <sha>; the previous release is serving again.` only when
that check passes. Otherwise it prints `MANUAL RECOVERY REQUIRED`. Every
failure path starts `wsi-app` and `wsi-sync` again, including a failed
`git reset` and a failed `install.sh` re-run, so the site is never left
stopped. The script still exits non-zero. Some failures do not end in a
complete, healthy rollback, for example when no rollback is possible
(`SKIP_GIT_PULL=1`) or when the rollback fails. In those cases the script
keeps the snapshot and prints its path. Delete the snapshot yourself once
you no longer need it.

`install.sh` keeps the `wsi` user's npm and pip download caches
(`/opt/wsi/.npm`, `/opt/wsi/.cache`) across runs. A rollback can therefore
often reinstall the previous release's dependencies from cache even when the
update failed because the network did.

Set `WSI_SKIP_ROLLBACK=1` to suppress the rollback and leave the working
tree at the failed state for inspection (the DB snapshot is kept and its
path printed). The services are still started again on whatever is deployed,
like every other failure path; stop them yourself
(`sudo systemctl stop wsi-app wsi-sync`) if you want them down while you
look.

### Basic usage

```bash
cd ~/wsi          # the clone git clone created (the one install.sh ran from)
sudo bash infra/update.sh
```

### Options (environment variables)

| Variable | Effect |
|---|---|
| `SKIP_GIT_PULL=1` | Skip the git step entirely; deploy the current working tree as-is |
| `GIT_REF=<ref>` | Pin to a specific branch or tag (overrides latest-tag detection) |
| `WSI_TRACK_BRANCH=1` | Stay on the current branch HEAD (running `git pull --ff-only`) instead of checking out the latest `vX.Y.Z` tag. From the detached HEAD an earlier tag update leaves, it first switches to `origin`'s default branch (`main` / `master`) |
| `WSI_NGINX_DISCARD_SITE_TLS=1` | `install.sh` only. The nginx site file is replaced from the repository on every install; the TLS settings certbot had patched into it are carried over to `/etc/nginx/snippets/wsi-tls.conf` first. If the old file has an active `ssl_*` directive or TLS listener that is not certbot's (hand-written TLS, a client-certificate gate, a second server block, or settings outside any server block), the install stops, naming the lines, rather than drop them; this flag replaces the file anyway and HTTPS stays off until you restore it. The file is kept as `/etc/nginx/sites-available/wsi.previous` whenever it held TLS — replaced after carrying certbot's settings over, replaced on request with this flag, or left in place by a run that stopped: move its TLS lines into `wsi-tls.conf`. `update.sh` checks this before it stops anything and refuses in the same cases (a crashing check refuses even with the flag). A snippet that already has lines of its own is merged into, not replaced; a snippet that is a symlink is never rewritten through (see the TLS section of `QUICKSTART_FOR_UBUNTU.md`) |
| `WSI_VERIFY_RELEASE_TAGS=1` | Check out a tag only if `git verify-tag` accepts its signature against the keyring of the user git runs as (`$SUDO_USER`, or `GIT_AS_USER`); otherwise stop before anything is changed. `update.sh` and `install.sh` run the checked-out release's `install.sh` as root, so this moves the trust from "whoever can push a tag to `origin`" to "whoever holds the signing key". Off by default: it needs signed release tags and the maintainer's public key in that keyring (`gpg --import`), and with it on an unsigned tag stops every update. A branch (`GIT_REF=<branch>`, `WSI_TRACK_BRANCH=1`) cannot be verified and is refused while it is on, and so is a tag that a local branch of the same name would shadow at checkout |
| `WSI_RELEASE_SIGNING_KEY=<fingerprint>` | With `WSI_VERIFY_RELEASE_TAGS=1`: accept only a signature made by this key (its fingerprint as `gpg --fingerprint` prints it, or its primary key's; case and spaces do not matter). Without it any key in the keyring, at any trust level, satisfies the check — so keep that keyring to the maintainer's key, or set this |
| `WSI_SKIP_TAG_CHECKOUT=1`, `WSI_SKIP_RESTART_HEALTH=1` | Internal flags set by `update.sh` when invoking `install.sh`, so `install.sh` neither re-fetches and re-checks out nor restarts and health-checks the services itself (`update.sh` does both) |
| `SKIP_RESTART=1` | Do not start `wsi-app` / `wsi-sync` again at the end. The services are still stopped before the file swap, so they stay down until you run `systemctl start wsi-app wsi-sync` yourself. nginx is reloaded either way: `install.sh` installs the new nginx config and reloads it on every run. The pre-update DB snapshot is kept (its path is printed), since nothing has proven the new release boots; delete it once it runs |
| `WSI_HEALTH_TIMEOUT=<s>` | How long step 5 (and a rollback) waits for the backend's health check (default `30`). Raise it if a first boot after an upgrade legitimately takes longer (see [How schema changes are absorbed](#how-schema-changes-are-absorbed)) |
| `WSI_SYNC_HEALTH_TIMEOUT=<s>` | How long step 5 waits for the sync coordinator's health check (default `30`). Raise it alongside `WSI_HEALTH_TIMEOUT` on a host where boots are slow |
| `WSI_SKIP_ROLLBACK=1` | On failure, leave the repo at the failed state instead of rolling back (the services are still started again) |
| `WSI_SKIP_DB_SNAPSHOT=1` | Do not snapshot the DB before the update. Without the snapshot, a rollback after the new release has started cannot bring the previous release back up |
| `GIT_AS_USER=<u>` | User to run `git` as (default: `$SUDO_USER`) |
| `APT_LOCK_TIMEOUT=<s>`, `WSI_NPM_CI_ATTEMPTS=<n>`, `WSI_NPM_FETCH_*` | Passed through to `install.sh`: the wait for another apt/dpkg run's locks (default `300`) and the `npm ci` retry tuning. See the header of `infra/install.sh` |
| `WSI_UPDATE_RECOVERY_LOG=<path>` | Where a recovery whose output pipe broke (an SSH drop under `\| tee`) writes its output (default `/var/log/wsi-update-recovery.log`) |
| `WSI_LAN_HOST`, `WSI_LAN_PROBE_TIMEOUT_MS`, `WSI_PUBLIC_BASE_URL` | Passed through to `install.sh`, which bakes them into the frontend build (and persists them in `wsi.env`) when set |
| `APP_HOME=<path>` | Must equal `/opt/wsi` if set. The prefix is hardcoded in the systemd units / nginx config, and `install.sh` refuses to run with any other value — relocation is not supported |

**Examples:**

```bash
# Default: deploy the highest vX.Y.Z tag on origin
sudo bash infra/update.sh

# Deploy a specific tag
sudo GIT_REF=v1.2.3 bash infra/update.sh

# Track the development branch instead of the latest release tag
sudo WSI_TRACK_BRANCH=1 bash infra/update.sh

# Rebuild and redeploy without pulling (e.g. after a local edit)
sudo SKIP_GIT_PULL=1 bash infra/update.sh

# Let a failed update sit so you can inspect the broken tree
sudo WSI_SKIP_ROLLBACK=1 bash infra/update.sh
```

### Private forks and mirrors

`sudo` drops the invoking user's credentials, so `git pull` runs by
default as `$SUDO_USER` (not root) to keep `~/.config/gh` and SSH keys
accessible. If you have run `gh auth login && gh auth setup-git` as the
non-root user beforehand, `pull` will work without prompts.
To force running as root instead, set `GIT_AS_USER=root`.

---

## Upstream update check

The admin Server Info page shows an amber **● Update available: vX.Y.Z**
badge next to the **App version** row when a newer release tag is available
on the upstream GitHub repository.

Behavior:

- The backend polls
  `https://api.github.com/repos/<owner>/<repo>/releases/latest`
  asynchronously (in a background thread) at most once every 12 hours per
  `wsi-app` worker process: each worker keeps its own in-memory cache, so
  with several workers GitHub may be contacted once per worker per 12 h; the
  badge can differ between page loads until every worker has checked, and
  *Last checked* shows the answering worker's own time. The request handler always reads from that cache, so the
  check never blocks the dashboard. The poll is **scheduled by that same
  handler** — `GET /api/admin/server/info` is the only caller — so an
  install whose Server Info page is never opened never contacts GitHub at
  all, even with the check enabled.
- If that endpoint 404s — a repository that publishes annotated tags but no
  GitHub Releases — or answers 200 with a missing / empty `tag_name`, the
  check falls back to
  `https://api.github.com/repos/<owner>/<repo>/tags` and picks the highest
  parseable `vX.Y.Z` tag from the page — a pre-release tag such as
  `v2.0.0-rc1` counts here, although `update.sh` deploys one only when it
  is named with `GIT_REF`. The fallback sorts by semver rather
  than trusting the API's tag-creation order, so a hotfix cut later on an
  older minor (e.g. `v0.9.10` created after `v0.10.0`) does not raise a
  false update banner.
- The repository defaults to the canonical upstream and can be
  redirected with `WSI_UPDATE_CHECK_REPO=<owner>/<name>` in
  `/etc/wsi/wsi.env` (e.g. for a fork or after a repo rename).
- The dashboard prints the last successful check time under the App
  version row (`Last checked: <timestamp>`).
- The check fails open: if GitHub is unreachable or rate-limits the
  request, the previously-cached `latest` is preserved (the indicator
  does not flicker off on a transient outage) and the next retry
  happens after a 10-minute backoff instead of the 12-hour TTL.
- Disable entirely with `WSI_UPDATE_CHECK=0` in `/etc/wsi/wsi.env`
  (useful for air-gapped servers or when upstream contact is undesired).

---

## Live-sync coordinator — `wsi-sync.service`

A staff member (any role) starts a session from a **published** slide opened
through the dashboard (**Open Viewer**, which adds `?from=admin`) with **Start
live sync**; the panel then shows the follower link / QR, the participant list
and a **⇄ Slide** switcher that moves the whole audience to another slide.
Visitors join through that link (`/slides/<id>?sync=<sid>`) or, for a listed
slide, the join banner on the index page.

The live-sync feature (presenter → audience viewport mirroring) requires
all WebSocket connections for a session to land on the **same** process.
To avoid dragging in Redis or a sticky-load-balancer rule, the sync
coordinator runs as a **separate single-worker Uvicorn process**
(`wsi-sync.service`) on port 8001, independent of the multi-worker
`wsi-app.service` on port 8000.

nginx routes `/api/sync/` and `/api/admin/sync/` traffic exclusively to
port 8001; all other traffic goes to port 8000.

`wsi-sync.service` is **independent** of `wsi-app.service` at runtime: it
declares `After=wsi-app.service` (boot ordering) but deliberately **not**
`PartOf=`, so `systemctl restart wsi-app` does **not** restart the sync
coordinator. That is intentional — cascading a `wsi-app` restart into the
single-worker sync process would wipe every in-memory room, disconnecting an
entire in-progress class. Saving auth / network settings restarts neither
service: the worker that handled the save applies it at once, and every
other process — the `wsi-app` workers and the sync coordinator — picks it up
through the periodic snapshot refresh in the app lifespan
(`main._settings_refresh_loop`, tunable with `WSI_SETTINGS_REFRESH_SECONDS`,
default 5 s). An auth-mode switch, a new IP rule or a session revocation is
therefore in force everywhere within that interval, and in-flight uploads,
downloads and live-sync rooms are not interrupted. (Tiles, thumbnails and
tour assets already authorised at the edge can keep being served for up to
the 60 s `/api/edge-authz` verdict cache; see [nginx tile
cache](#nginx-tile-cache--infranginx-cachesh).) The one exception is a
restore / reset apply, which replaces the DB file itself: for that
`wsi-restart.service` stops the sync coordinator before restarting `wsi-app`
and, if it was running, starts it again after the swap (see [What happens during
apply](#what-happens-during-apply)). `infra/update.sh` also stops and starts it
explicitly, and `WantedBy=multi-user.target` keeps it in the normal boot set.

**Restart it yourself** when you change something the snapshot refresh does
not cover — a new `JWT_SECRET`, or any other `/etc/wsi/wsi.env` value, which
is only read at process start:

```bash
sudo systemctl restart wsi-app.service wsi-sync.service
```

You can also manage it independently:

```bash
sudo systemctl status  wsi-sync.service
sudo systemctl restart wsi-sync.service
sudo journalctl -u wsi-sync -f
```

The `UVICORN_WORKERS` / `UVICORN_WORKERS_PERCENT` env vars have **no
effect** on `wsi-sync.service` — it always runs with exactly one worker.

### Session lifetime and `WSI_SYNC_*` tunables

A live session ends on its own in three situations, each with its own end
reason (carried in the `end` frame). Viewers and the presenter get a specific
message for `max_age` and `presenter_idle`; `presenter_gone` shows viewers
"The live session ended because the presenter did not return." after the
"presenter left — ending in N s" countdown they were shown while the presenter
was away. Viewers are also told when the session was ended by hand — the
presenter's **End live sync** or a staff member's End on the
Server Info page (`ended_by_owner`) — or replaced with a new one
(`superseded`, below):

* **Maximum age** (`max_age`) — `WSI_SYNC_MAX_SESSION_AGE_SEC`, default 5400
  (1.5 h). A hard cap measured from session creation, **applied even to an
  active, attended session** (a presenter who needs longer starts a new one).
  The presenter gets a one-off warning `WSI_SYNC_MAX_SESSION_AGE_WARN_SEC`
  (default 300) before the end; interacting does not postpone it. `0` disables
  the cap.
* **Presenter idle** (`presenter_idle`) — `WSI_SYNC_PRESENTER_IDLE_SEC`,
  default 600 (10 min) without a view/cursor frame from a connected presenter
  (ping/pong does not count), warned `WSI_SYNC_PRESENTER_IDLE_WARN_SEC`
  (default 60) ahead of time; activity cancels the warning. `0` disables it.
  A **Start** or **Resume** counts as activity, but a dropped socket's
  automatic reconnect does not, nor do the view and pointer it replays, so a
  tab stuck reconnecting on a bad link is still ended for inactivity rather
  than only by the maximum age.
* **Presenter gone** (`presenter_gone`) — `WSI_SYNC_PRESENTER_GRACE_SEC`,
  default 45: how long a session survives with no presenter attached (a
  reload or network blip) before it is reaped. The presenter's browser retries
  at least every ~10 s, so a drop shorter than this window is recovered.

A staff account runs one live session at a time: starting a session (on any
slide) ends every other session that account owns, with the reason
`superseded`. The viewer checks first and asks for confirmation when the
account has another session running — on another slide, or presented from
another tab or device. The superseded session's viewers are offered **Join the
new session**, unless the new session is on a link-only slide (its link was
never given to them). Once `WSI_SYNC_MAX_ROOMS` sessions are running, starting another
is refused with HTTP 503 — unless it replaces one of the caller's own, which
does not add to the count.

All the variables below are read by `wsi-sync.service` at start (restart it
after editing `/etc/wsi/wsi.env`). The defaults suit classroom-scale use; change
one only against a measured problem. Times are seconds. A grace, notice delay,
GC tick or audience debounce that is negative (or a GC tick of 0) is replaced
by its default, and a notice delay not below the grace by half the grace, each
with a warning in the `wsi-sync` journal; a grace not well above the GC tick
is only warned about.

| Variable | Default | Meaning |
|---|---|---|
| `WSI_SYNC_MAX_SESSION_AGE_SEC` | `5400` | Hard cap on a session's total lifetime, even when active (see above); `<= 0` disables |
| `WSI_SYNC_MAX_SESSION_AGE_WARN_SEC` | `300` | Presenter warning this long before the age cap; `<= 0` = no warning. A value above half the cap is treated as half the cap, so a misconfiguration cannot warn a brand-new session |
| `WSI_SYNC_PRESENTER_IDLE_SEC` | `600` | Connected presenter silent this long → session ended; `<= 0` disables |
| `WSI_SYNC_PRESENTER_IDLE_WARN_SEC` | `60` | Idle warning lead time before the idle end; `<= 0` = no warning. A value above half the idle timeout is treated as half of it, so a misconfiguration cannot warn a presenter who is interacting |
| `WSI_SYNC_PRESENTER_GRACE_SEC` | `45` | Time a session survives without a presenter before it is reaped |
| `WSI_SYNC_PRESENTER_ABSENT_NOTIFY_DELAY_SEC` | `5` | Delay before viewers are told "presenter left" (keep well below the grace) |
| `WSI_SYNC_GC_INTERVAL_SEC` | `1` | Tick of the loop that drives the timers above |
| `WSI_SYNC_MAX_ROOMS` | `200` | Concurrent sessions per process; `<= 0` disables the cap |
| `WSI_SYNC_FOLLOWERS_PER_ROOM_MAX` | `200` | Viewers per session (refused with close code 4503) |
| `WSI_SYNC_FOLLOWERS_PER_IP_MAX` | `0` | Viewers per client IP per session; `0` = no limit |
| `WSI_SYNC_DISCOVERY_MAX_SUBS` | `500` | Index-page discovery sockets |
| `WSI_SYNC_DISCOVERY_MAX_SUBS_PER_IP` | `200` | Discovery sockets per client IP; `0` = no limit |
| `WSI_SYNC_REQUIRE_NAME` | `false` | Viewers must give a display name to join |
| `WSI_SYNC_REVERSE_DNS` | `false` | Resolve viewer IPs to hostnames for the presenter's audience list (skipped while the analytics privacy mode is **hashed**, which withholds host names anyway) |
| `WSI_SYNC_REVERSE_DNS_WORKERS` | `4` | Threads for those lookups |
| `WSI_SYNC_BASIC_AUTH_WORKERS` | `4` | Threads that verify HTTP Basic credentials on live-sync socket upgrades (each a full password hash), kept apart from the threads that serve live rooms |
| `WSI_SYNC_AUDIENCE_DEBOUNCE_SEC` | `0.2` | Coalescing window for audience updates sent to the presenter |
| `WSI_SYNC_HELLO_TIMEOUT_SEC` | `5` | Time a new socket has to send its hello frame. A presenter that misses it is asked to retry (1013); a viewer joins anonymously, and its hello is still applied when it arrives (under `WSI_SYNC_REQUIRE_NAME` it is asked to retry instead) |
| `WSI_SYNC_PING_INTERVAL_SEC` | `15` | Server keepalive ping cadence |
| `WSI_SYNC_PING_TIMEOUT_SEC` | `30` | Silence after which a socket is dropped. Checked on each ping tick, so a vanished peer is noticed after this plus up to one ping interval (30–45 s by default) |
| `WSI_SYNC_WS_RECV_IDLE_TIMEOUT_SEC` | `45` | Backstop for sockets that cannot be closed (keep above the ping timeout) |
| `WSI_SYNC_PRESENTER_REAUTH_SEC` | `60` | How often the presenter's staff account (still active, still a staff role, token not revoked) — and the IP filter, which applies to staff too — is re-checked |
| `WSI_SYNC_PRESENTER_REAUTH_MAX_FAILURES` | `5` | Consecutive re-checks that could not complete (database or Cloudflare Access keys unreachable) before a presenter, follower or discovery socket is dropped — with 1013, so the client reconnects once the backend is back; a revoked credential is dropped at once with 4401 (4403 when an IP rule now denies the client). `<= 0` disables this backstop, so an unreachable backend never drops a socket |
| `WSI_SYNC_VIEWER_REAUTH_SEC` | `60` | How often a viewer's app-access session (and the IP filter) is re-checked |
| `WSI_SYNC_WS_MAX_BYTES` | `65536` | Largest accepted inbound frame. The unit also passes a fixed `--ws-max-size 65536` to uvicorn, so lowering this works but raising it has no effect unless the unit is edited too |
| `WSI_SYNC_OUTBOUND_QUEUE_MAX` | `512` | Per-socket send backlog before a slow peer is dropped |
| `WSI_SYNC_WS_SEND_TIMEOUT_SEC` | `10` | A single send blocked this long drops the socket |
| `WSI_SYNC_INBOUND_FRAME_RATE` | `60` | Per-socket inbound frames/s (token-bucket refill) |
| `WSI_SYNC_INBOUND_FRAME_BURST` | `120` | Per-socket token-bucket size |
| `WSI_SYNC_INBOUND_OVERLIMIT_MAX` | `200` | Over-limit frames tolerated per window before the socket is closed with 1008 |
| `WSI_SYNC_INBOUND_OVERLIMIT_WINDOW_SEC` | `10` | That window |

### Capacity and per-IP limits

A room admits at most `WSI_SYNC_FOLLOWERS_PER_ROOM_MAX` followers (default
200) and the index-page discovery channel at most
`WSI_SYNC_DISCOVERY_MAX_SUBS` sockets (default 500). Each client IP may
additionally hold at most `WSI_SYNC_DISCOVERY_MAX_SUBS_PER_IP` discovery
sockets (default 200 — a full room's worth, yet well below 500, so one
machine cannot take the whole channel) and `WSI_SYNC_FOLLOWERS_PER_IP_MAX`
followers per room (default `0` = no per-IP limit; the per-room cap already
bounds a room). `0` disables either check. The IP is the resolved client
address — the `CF-Connecting-IP` / `X-Real-IP` that the local nginx or
cloudflared supplies — so participants behind the reverse proxy are still
counted separately, but **a whole class behind one NAT address** (a campus
network, every student reaching a Cloudflare tunnel through the same egress)
is a single IP. The defaults admit such a class; if you set
`WSI_SYNC_FOLLOWERS_PER_IP_MAX`, do so only where every participant has its
own address.

nginx meters the WebSocket handshakes for `/api/sync/ws/` and
`/api/sync/discovery` per client IP in a zone of their own, `wsi_sync_ws`
(600 per minute with a burst of 400, see `infra/nginx/wsi.conf`), separate
from the general `/api/` limit. That is sized for the same case: when
`wsi-sync` restarts or the network blips, every follower and index-page
discovery socket reconnects at once. 200 followers plus 200 discovery sockets
from one NAT address fit in the burst when their first attempt finds the
server up; after a longer outage, whose failed attempts use the burst up, they
come back at the zone's rate instead, the last within about a minute. A client
throttled there gets a 429 on the handshake and simply retries with its
backoff. Raise both numbers together (the zone in `wsi.conf`, the bursts in
`wsi-locations.conf`) if you raise `WSI_SYNC_FOLLOWERS_PER_ROOM_MAX` for
classes larger than that behind a single address.

The same goes for the slide detail read every follower makes the moment the
presenter switches slides (`GET /api/slides/<public_id>`): it has a zone of
its own, `wsi_slide_detail` (600 per minute with a burst of 200), so the whole
room fits in one switch instead of all but 40 students hitting the `/api/`
limit. The tour read (`GET /api/tours/<public_id>`) and the two small reads
every public page makes first (`GET /api/auth/state`, `GET /api/viewer/settings`)
have another, `wsi_page_reads` (600 per minute with a burst of 400), so a class
opening one tour link together gets its tour, and a room's followers sending the
settings read beside the slide read on a switch (or the sign-in state and
settings on a join: 400 requests for 200 followers) do not spend the slide
reads' burst; the page's analytics beacons stay under the `/api/` limit and a few
may be dropped when a very large class arrives at once. A follower answered 429 there retries on its own (1, 2, 4, 8, then
every 15 s, each delay jittered to 0.5–1.5×). Raise that burst along with the
room size too. The session check a follower makes as it opens the share link
(`GET /api/sync/sessions/<sid>`) has a zone of its own for the same reason,
`wsi_sync_peek` (600 per minute with a burst of 200), so a class scanning the
QR code together fits; a 429, a server error or no answer is retried the same
way for about a minute before the viewer shows "Could not check the live
session" with a **Retry** button.

A refused socket is closed with 4503. Neither the viewer nor the index page
treats that as final: both retry with backoff (0.5 s doubling to a 30 s cap,
each delay jittered to 10–100 % of the step),
and the viewer shows "This live session is at full capacity. Retrying
automatically…" until a retry is admitted. A refusal caused
by a **per-IP** limit (rather than a genuinely full room or channel) is
logged at WARNING by `wsi-sync.service` — at most once a minute per address,
naming the address and the variable to raise:

```text
live sync: refused a follower connection from 203.0.113.7: per-IP cap of 50 reached (WSI_SYNC_FOLLOWERS_PER_IP_MAX). ...
```

### Cross-site WebSocket check

Browsers let any web page open a WebSocket to any host, and attach whatever
credentials they hold for that host — cached HTTP Basic credentials included —
or none at all where the break-glass / LAN-direct exemptions admit a request
without one. So the sync coordinator refuses a browser's upgrade of
`/api/sync/ws/…` and `/api/sync/discovery` that does not come from the app's
own pages, closing it with 4403 (the presenter panel and the viewer report it
like an IP-filter refusal — access from this address is blocked — and stop
retrying):

* a browser that sends fetch metadata must report `Sec-Fetch-Site:
  same-origin` (a sibling subdomain, `same-site`, is refused too);
* otherwise its `Origin` hostname must match the `Host` hostname nginx
  forwards (ports are ignored, since nginx forwards `Host` without one, and so
  is a fully-qualified name's trailing dot, which nginx strips from `Host`
  but the browser keeps in `Origin`) or the hostname of `WSI_PUBLIC_BASE_URL`;
  an opaque `Origin: null` is refused.

A request with no `Origin` at all is not a browser and is judged by the usual
checks alone. Nothing needs configuring for a stock deployment, the
LAN-direct host or a raw-IP route — the app opens its sockets on the host the
page was loaded from. If you put a proxy in front of nginx that rewrites
`Host` to something the browser did not use, set `WSI_PUBLIC_BASE_URL` to the
address users browse to (older browsers without fetch metadata are judged by
`Host` and would otherwise be refused).

The app-access sign-in form's `POST /api/auth/login` and the **Sign out**
button's `POST /api/auth/logout` get the same check, answering 403 `cross-site
request rejected` (so another site cannot sign a visitor in as someone else,
or out), with these differences: `Sec-Fetch-Site: none` (a direct
navigation) is accepted as well as `same-origin`, an opaque `Origin: null` is
let through, and the `Origin` fallback compares with the `Host` hostname
only, not with `WSI_PUBLIC_BASE_URL`. Behind a proxy that
rewrites `Host`, a browser without fetch metadata therefore cannot sign in
through the form; send nginx the `Host` the browser used.

### Presenter on more than one device

Each presenter tab instance (a page load, **Start** or **Resume**) picks a
random id and sends it when it attaches. A presenter socket that drops (Wi-Fi
blip, laptop lid closed) reconnects on its own with that id, marked as an
automatic reconnect. If a *different* tab or device has attached since (the
**Resume** button on a tablet, say), the server closes the returning socket
with 4000 ("presenter replaced"). The old tab then shows "now being controlled
from another tab or device" instead of taking the session back and dragging
the audience to its old view. A tab's own reconnects are never refused while
no other device has attached, however often the connection drops. An explicit
**Resume** always takes over, so the device the presenter is holding can
reclaim the session.

The viewer offers **Resume live sync** (rather than **Start live sync**) whenever the presenter
owns a live session on that slide — also while the server still counts a
presenter socket as attached: another tab, or, after a network drop or a
crashed tab, the dead socket the keepalive has not reaped yet (up to
`WSI_SYNC_PING_TIMEOUT_SEC` plus one `WSI_SYNC_PING_INTERVAL_SEC`, 30–45 s by
default). **Start** would end that session for every participant and open a
new one; **Resume** takes it over and keeps them.

Leaving the viewer page within the app (a link or the browser's Back to
another app page) ends the session at once, unless another tab or device has
attached to it since — that one keeps driving it. Closing the tab, reloading
or typing another address does not end it explicitly (the browser asks for
confirmation first while a session is live): the socket just drops, followers
see the "presenter left" countdown, and the session ends as `presenter_gone`
after `WSI_SYNC_PRESENTER_GRACE_SEC` unless the presenter comes back and
**Resume**s it.

### Unpublished slides during a session

A session can only be started on a published slide (HTTP 403 otherwise), but
nothing ends it when that slide is unpublished or deleted later. From then on
the session is hidden from the index page's join banner, its share link
reports "Live session not found." and new viewers cannot join, while viewers
already in it stay (one that reconnects meanwhile is asked to retry, close
code 1013, and is let back in once the session is on a published slide). Within `WSI_SYNC_PRESENTER_REAUTH_SEC`
(checked with the presenter's re-authentication) the viewers already in it
are shown that the slide is no longer available, the presenter is told, and
index pages showing the banner take it down; the session follows the presenter
onto the next published slide as usual, and the viewers' notice clears when
the slide is published again. The same check puts the banner back, or takes it
down, when a slide being presented is made listed or link-only.

When the presenter moves to an unpublished slide, viewers stay on the previous
one, the presenter's pointer is hidden from them and the presenter is told
once; moving back to a published slide resumes normal following. A session on
a **link-only** slide works normally but is never shown in the index page's
banner. A session that *was* shown there — started on a listed slide — and
then moves onto a link-only one is taken off the banner, but every index page
that showed it already holds its session id, which is all a viewer needs to
join. So anyone who saw the banner can still follow it onto the link-only
slide; the presenter is told so when it happens, and can end the session and
start a new one (which is not announced) if that audience should not see the
slide.

### Refused connections — close codes

The coordinator accepts every live-sync socket first and then refuses it, if
it has to, with a close code; the presenter panel and the viewer turn each
into a message naming the cause. Only the last two rows are retried (with
back-off); every other refusal is final until the page is reloaded or the
session is started / resumed again.

| Code | Sent when | Presenter sees | Viewer sees |
|---|---|---|---|
| 4000 | Another tab or device took the session over (presenter only, see above) | "now being controlled from another tab or device" | — |
| 4400 | No usable hello: a presenter hello that is malformed; a viewer without a name under `WSI_SYNC_REQUIRE_NAME` (a hello that is merely late gets 1013, below) | "The live session could not be set up: the server refused the connection request. Start it again." | "Could not join the live session (a name may be required, or you need to sign in)…" |
| 4401 | The credential is missing, invalid, expired or was revoked (the presenter's staff token; a viewer's app access) | "Session expired — please sign in again" | as for 4400 |
| 4403 | The IP filter denies the client's address (at connect or a later re-check), or the [cross-site check](#cross-site-websocket-check) refused the upgrade | "Live sync connection refused: access from this network address is blocked by the server's IP filter…" | same |
| 4404 | The session does not exist: ended, reaped or lost to a restart — including one that ended while the presenter's socket was still attaching | "Live session ended." | "Live session ended." |
| 4409 | The session exists but belongs to another staff account (presenter only) | "This live session belongs to another staff account…" | — |
| 4503 | No viewer slot (see [Capacity and per-IP limits](#capacity-and-per-ip-limits)) | — | "at full capacity. Retrying automatically…" (retried) |
| 4429 / 1013 / 1008 | The per-IP login window is full; the auth check could not run (database or Cloudflare Access keys unreachable), the hello arrived after `WSI_SYNC_HELLO_TIMEOUT_SEC` (a presenter; a viewer only under `WSI_SYNC_REQUIRE_NAME`, otherwise it joins anonymously), the socket fell `WSI_SYNC_OUTBOUND_QUEUE_MAX` frames behind on its sends, or (viewers) the session's slide is unpublished for now; the inbound frame rate limit | "Disconnected — reconnecting…" (retried) | a reconnecting banner (retried) |

### Participant list — screen sizes

Each row in the presenter's participant panel shows the follower's viewer
size in CSS pixels (e.g. `1194×834`), reported once when they join. It is
also included in the panel's CSV export as `viewport_w` / `viewport_h`.

Use it to spot participants whose screen shape differs from the presenter's.
A follower fits the presenter's transmitted rectangle to its own canvas by
padding the **short** axis, so the presenter's field is always fully visible
— a mismatched aspect ratio never crops it, it only surrounds it with extra
margin, which makes it appear smaller.

To judge how much smaller, divide the **smaller** of the two aspect ratios by
the **larger**; the result is the fraction of the linear size the participant
would have seen on a matching screen. Compare against your own viewer area,
which is your browser window less the live-sync side panel (320 px open,
40 px collapsed) — the panel lists participants only, not you.

For example, a 1080p presenter with the panel open has a viewer of 1600×1080
(aspect 1.48); against a participant on 1194×834 (1.43) that gives
1.43 / 1.48 ≈ 0.97, so they see the presenter's field at about 97% of its
linear size. Landscape tablets against a widescreen presenter generally land
between 75% and 97% — highest with the side panel open, lowest with it
collapsed in a windowed browser — which is rarely worth acting on. A
participant in **portrait** is the case to watch: an aspect near 0.7 against
the same presenter gives roughly 0.4 to 0.5.

The figure is a join-time snapshot and is **not** updated afterwards, so a
participant who rotates or resizes mid-session keeps the size they joined
with (a reconnect re-reports it).

### Share link (follower URL / QR)

When a presenter starts a session, the panel shows a follower URL and QR
code for the audience to join. By default that link is built from the
presenter's own request host (the `Host` header nginx forwards; a
client-supplied `X-Forwarded-Host` is ignored so a caller cannot choose the
host baked into the link), which is exactly right when **every
participant reaches the server at the same address** (a single public
hostname, or a classroom server accessed by its intranet IP).

The viewport-sync frames and the slide tiles are always loaded from
whichever address each participant is on, so within a single-address
deployment nothing needs configuring.

If the server is reachable under **more than one** name/address (for
example a public hostname plus a LAN hostname or a raw IP) and a presenter
is on one that some participants cannot reach, a host-pinned share link
would be unusable for them. To pin the share link to one canonical entry
point that every participant can reach, set `WSI_PUBLIC_BASE_URL` (see
`wsi.env.example`); the follower URL / QR then uses it regardless of the
presenter's current host. The same value also drives the resume affordance,
so a reattached session hands out the canonical link too.

Whichever address you advertise must be reachable by the participants and,
for HTTPS, carry a valid certificate — the live-sync WebSocket and the
tiles are loaded from that origin.

#### Static share controls on a LAN-direct route

The same `WSI_PUBLIC_BASE_URL` also improves the **static** share controls —
the "copy URL + QR" affordances on the top-page URL, each slide, and each
tour. These are built from the operator's *current* origin, so when the
operator reaches the dashboard over a **direct/local route** (the LAN
hostname, a raw LAN IP, or a `.local` name) the issued URL only works for
same-VLAN clients and is useless to share off-LAN. When `WSI_PUBLIC_BASE_URL`
is set, `install.sh` bakes it into the frontend build (as
`VITE_PUBLIC_BASE_URL`), and on such a direct/local route each control then
shows **both** the current (direct) URL and the canonical public URL, each
with its own copy button and QR — so the operator can hand out a link that
also works for people off the LAN. On the public host itself (origin already
matches `WSI_PUBLIC_BASE_URL`), only the single URL is shown, unchanged.
Because the value is baked at build time (like `WSI_LAN_HOST` /
`VITE_LAN_HOST`), changing it needs a frontend rebuild to take effect here;
`install.sh` / `update.sh` re-apply it from `wsi.env` automatically.

#### Setting the three build-time values

`WSI_LAN_HOST`, `WSI_LAN_PROBE_TIMEOUT_MS` and `WSI_PUBLIC_BASE_URL` are the
only variables `install.sh` will write into an existing `/etc/wsi/wsi.env`,
and only when you hand them to it in the environment:

```bash
sudo WSI_LAN_HOST=lan.example.org bash infra/install.sh
```

Put the assignment **after** `sudo`, as with every other option in this
document. A bare `WSI_LAN_HOST=... sudo bash …` sets the variable in `sudo`'s
own environment, which Ubuntu's `Defaults env_reset` strips before the script
starts: the installer would see it unset, persist nothing, and still finish
successfully.

`WSI_LAN_HOST` is baked into the frontend build *and* the nginx CSP,
`WSI_LAN_PROBE_TIMEOUT_MS` into the build, and `WSI_PUBLIC_BASE_URL` into the
build as well as being read at runtime by the app — so all three have to
reach `wsi.env` or the next `update.sh` would rebuild without them.
Supplying one replaces any existing live assignment of that key — including a
line you wrote by hand, along with any trailing comment on it — and leaves
the rest of the file untouched. Run `install.sh` with the variable unset and
nothing is written. To clear one, delete its line from `wsi.env`.

Editing `wsi.env` by hand works too, with one caveat specific to these three:
**do not put a trailing `#` comment on their lines.** The installer's
read-back pattern cannot span a `#`, and because it is anchored to the end
of the line, a line carrying one does not match at all — the value is not
truncated, it is simply not seen, so `install.sh` and `update.sh` rebuild the
frontend without it:

```bash
WSI_LAN_HOST=lan.example.org   # VLAN gateway       <-- installer sees nothing
WSI_LAN_HOST=lan.example.org                        <-- correct
```

systemd does *not* rescue you here: its
`EnvironmentFile=` parser only treats `#` as a comment at the start of a
line, so the service receives the literal string
`lan.example.org   # VLAN gateway`, comment and all. For `WSI_LAN_HOST`
and `WSI_LAN_PROBE_TIMEOUT_MS` that is harmless (nothing reads them at
runtime), but `WSI_PUBLIC_BASE_URL` **is** read at runtime — with a trailing
comment there the value, comment included, is not a valid URL, so the app
ignores it (with a WARNING in the log) and live-sync follower URLs and the OAuth redirect lose
their canonical origin. Put any note on its own line above.

Quoting a value is fine: like systemd, the read-back strips one pair of
double or single quotes around it (`WSI_LAN_HOST='lan.example.org'` reads as
`lan.example.org`; the same read-back finds `DATABASE_URL` for `update.sh`'s
pre-update DB snapshot). Backslash escapes inside quotes are not supported.

`WSI_PUBLIC_BASE_URL` is an **origin** — `https://host`, optionally with a
`:port` (a bracketed IPv6 literal works too) — because Tessera is served at
the root path only. A value with a path, query or fragment
(`https://wsi.example.org/wsi`) is reduced to its origin by `install.sh`
(with a warning; it builds with the origin, and writes the origin to
`wsi.env` when the value came from the command line — a path already in
`wsi.env` stays there until you edit it), by the app at startup (a WARNING
in the log) and by the frontend. Anything that is not an
`http(s)` URL — or carries credentials, as in `https://user@host` — is
ignored with a warning, and `install.sh` falls back to the value already in
`wsi.env`. `make frontend-build` (local builds) refuses anything but a bare
origin. `install.sh`, the Makefile and the app use the same pattern
(`wsi_url_origin` in `infra/lib/wsi-common.sh`,
`config.public_base_url_from_env`).

---

## Auxiliary systemd units

Beyond `wsi-app.service` and `wsi-sync.service`, `infra/install.sh`
enables a few unattended helper units. Operators normally do not touch
these, but they are listed here so a `systemctl list-units 'wsi-*'` does
not surface unfamiliar names.

| Unit | Purpose |
|---|---|
| `wsi-cache-stat.timer` + `wsi-cache-stat.service` | Snapshot nginx tile cache disk usage to `/run/wsi-cache-stat/nginx_cache_stat.json` every 60 s, so the admin dashboard can display it. See [Disk usage statistics](#disk-usage-statistics--wsi-cache-stattimer) under `nginx-cache.sh`. |
| `wsi-avahi-reload.path` + `wsi-avahi-reload.service` | Watch `/etc/avahi/services` and run `systemctl try-reload-or-restart avahi-daemon` when the wsi mDNS service file changes (the app writes or removes it when **Network → mDNS (Bonjour / Avahi)** is switched on or off in the dashboard; the announced name is `Tessera (<hostname>)`, advertised as `_http._tcp` on port 80 — plain HTTP, whatever TLS front you use — and it makes the server discoverable by anyone on the local segment). |
| `wsi-restart.path` + `wsi-restart.service` | Watch `/run/wsi/restart.trigger` and run the privileged `systemctl restart wsi-app.service` when the API drops that file — the app runs under `NoNewPrivileges=true` and cannot restart itself. Used by the restore/reset apply flow (settings changes never restart the service); while a restore request is pending it also stops `wsi-sync.service` before the restart and, if it was running, starts it again once the swap is done (or after 15 minutes, or as soon as `wsi-app` fails). See "Backup & restore" below. |

All three pairs are installed and enabled (`systemctl enable --now` on the
`.timer` / `.path` units) by `install.sh`, so every `update.sh` run
reinstalls and re-enables them too.

---

## Backup & restore — admin dashboard

The dashboard's **Backup & Restore** sidebar tab (admin only) creates and
restores snapshots of the live install without leaving the browser.
Archives are stored on the server under `BACKUP_DIR`
(default `/var/lib/wsi/backups/`, mode `0750`, owned by `wsi:wsi`).

### Cloudflare Tunnel sessions

When the admin reaches the dashboard via a Cloudflare Tunnel, both the
**Backup & Restore** section and the **Upload WSI** section show a
notice ("You are connected through Cloudflare Tunnel."). The banner is
purely informational — it states the fact of the Tunnel path and makes
no claim about the account tier or transfer speed (the server cannot
tell a free plan from a paid one). The detection is server-side: the API
treats a request as "via Tunnel" iff the immediate peer is loopback
(cloudflared on the same host) **and** the request carries a non-empty
`Cf-Connecting-IP` header. nginx already scrubs `Cf-Connecting-IP` from
non-loopback peers, so a forged header from the LAN side never reaches
the check. Detection is independent of whether Cloudflare Access SSO is
configured — bare CF Tunnel + Bypass policy still triggers the banner.

### What's in an archive

| Path inside archive | Always included? |
|---|---|
| `manifest.json` | Yes — scope, `app_version`, the integer `schema_version` the restore policy keys on, archive format, slide/tour counts, note, created-at, and the `includes_analytics` / `includes_history` flags; for diagnostics only, the Python version and installed Python packages that wrote it |
| `wsi.db` | Yes — consistent SQLite snapshot via the online backup API (safe under live writes). A `metadata` snapshot's DB **still records the slides' `wsi_filename` values**; that's how the post-restore re-upload guidance knows what to ask for. It also holds each slide's macro / label images (downscaled JPEGs extracted at upload), so even a `metadata` archive carries the slide labels, which can show patient identifiers. |
| `assets/tours/...` | Yes — tour markdown attachments: each tour directory's regular files (files still being uploaded are left out; one deleted while the backup runs is skipped on its own, the tour's other attachments are still archived, and named in the finished job's warnings; so is a whole tour deleted after the DB snapshot, and a tour whose markdown references attachments of its own while its directory is missing). Anything copied into a tour directory by hand that uploads never create — a subdirectory, a dotfile, a symlink — is **not** archived; the backup logs a warning naming each one |
| `wsi/...` | **Only when scope = `full`** |
| `thumbs/...` | **Only when scope = `full`** — slide gallery thumbnails |

The `wsi.db` snapshot captures **every** table, but two ancillary groups can be
excluded at backup time (the snapshot copy is taken, then those tables are
emptied before it is archived; the schema survives, so a restore brings them
back as empty tables):

| Component checkbox | Tables | Default |
|---|---|---|
| Access statistics | `page_views`, `slide_dwell_events`, `tour_sessions`, `error_logs` | included |
| Activity history | `audit_logs` | included |

Excluding them keeps visitor PII (`page_views` carries client IPs / emails; the
`error_logs` warnings can carry visitor IPs too) and the admin action trail
out of an archive that may be shared off-site, and trims the archive size. The
**DB core** (slides, tours, settings, users, …) is always captured. The
`manifest.json` records which groups were included, which the **Saved
backups** list shows on every row (see [Creating a backup](#creating-a-backup)).

> **Every archive is credential-bearing.** The DB core cannot be excluded, and
> it holds the staff accounts' password hashes, the LDAP bind password and the
> Google OAuth client secret (`auth_settings`) in the clear. Excluding the
> analytics / history groups removes visitor PII, not these. Store archives,
> and any copy taken off the host, with the same care as those credentials
> (restricted permissions, encrypted transport and storage), delete copies you
> no longer need, and rotate the LDAP bind password / Google client secret if an
> archive leaks. The dashboard shows the same reminder next to the component
> toggles. On the host, archives (and slide export bundles) are written mode
> `0640` — the service user and its `wsi` group, never other accounts —
> whatever the umask.

Deep Zoom tiles are never stored under the data directory — they are rendered
on demand from the WSI files and cached only by nginx — and `cache/` holds
nothing but the resolve-cache sentinel, so neither is archived. `thumbs/` is **not**
regenerated on demand — thumbnails are produced only at upload / replace
time — so a `full` archive bundles them. A `metadata`-scope restore wipes
`thumbs/` along with `wsi/`; regenerate them afterwards by re-uploading the
WSI files or with the `rebuild-thumbnails` API (see "Restoring WSI
files after a metadata-only restore" below).

Archives are `tar.zst` when the optional `zstandard` Python package is
installed (the default after running `infra/install.sh`); they fall
back to `tar.gz` otherwise. The format is recorded in the filename
suffix and in `manifest.json`.

### Backup version policy

Each backup records **two** versions, both in `manifest.json` and in the
DB itself:

- `app_version` — the human `vX.Y.Z` release string, sourced from
  `$APP_HOME/VERSION`, which `infra/install.sh` (also when `infra/update.sh`
  runs it) stamps from `git describe --tags --always` at deploy time
  (`app_meta.app_version`).
- `schema_version` — a monotonic **integer** describing the data shape,
  bumped only when the schema changes (`app_meta.schema_version`; see
  *How schema changes are absorbed* below).

The first public release writes `schema_version` 6, so every database and
backup made by a released build records 6 or higher.

`backup.plan_restore` first requires that an `app_version` is recorded at
all, then weighs the integer `schema_version` **ahead of** the version
strings — the integer is the precise signal, since a release can move
`app_version` without touching the data shape. In evaluation order:

| Archive vs current binary | Behaviour |
|---|---|
| `app_version` missing from the archive | **Refused** (`NO_VERSION`). A recorded version is required before the rest of the policy can be evaluated. |
| `schema_version` **newer** than this binary supports | Always **refused** (`DOWNGRADE_NOT_SUPPORTED`), whatever the `app_version` strings say. Restoring it would leave the boot-time reconciler to `DROP COLUMN` the newer release's data. |
| `schema_version` **older** than the oldest this binary supports | **Refused** (`SCHEMA_TOO_OLD`): no migration path to the current shape exists. |
| `app_version` equal | Restored as-is. |
| `app_version` newer than the running binary (downgrade) | **Refused** (`DOWNGRADE_NOT_SUPPORTED`). Either upgrade the binary first, or restore an older archive. |
| `app_version` older than the running binary | Restore proceeds. The staged DB is stamped with the running `APP_VERSION` before the swap, and the next backend boot brings the schema forward in place. |

An `app_version` that is not a version at all — `unknown`, or the bare
commit hash `git describe --tags --always` falls back to when no tag is
reachable (e.g. a shallow clone) — is never ordered against another, so
it never counts as a downgrade here or in the boot-time guard; the
integer `schema_version` check still applies. The same goes when the running
version comes only from `frontend/package.json` (a tree with neither a
`VERSION` stamp nor a reachable git tag, e.g. a release tarball unpacked by
hand): that is the fixed placeholder `0.0.0-dev`, not bumped by releases, so the `app_version` comparison
is skipped with a WARNING in the log and only `schema_version` is checked.

`schema_version` is **required** in the manifest: an archive that omits
it (or whose `wsi.db` carries no `app_meta` stamps) is rejected at staging
as corrupt or foreign, rather than being read as an unknown shape that the
guard above would wave through.

The same rules apply to plain `wsi-app.service` startup: if the DB on
disk was last opened by a newer binary — by either version number — the
service refuses to start rather than silently running with mismatched
assumptions (`app_meta.assert_not_downgrade` /
`assert_schema_not_downgrade`).

### How schema changes are absorbed

`main.lifespan` runs the whole sequence under a cross-worker startup lock,
so exactly one worker performs it:

0. **Downgrade guards, before any schema work.** `assert_not_downgrade`
   and `assert_schema_not_downgrade` abort the boot while the data is still
   intact — the DROP phase below is destructive, so an older binary must
   never reach it against a newer DB.
1. **Missing tables, then the ADD phase.** `Base.metadata.create_all`
   creates any table the DB lacks. Then
   `main._apply_column_migrations(phase="add")` reflects each ORM-mapped
   table (`PRAGMA table_info`) and issues `ALTER TABLE ... ADD COLUMN` for
   every column the ORM declares but the table lacks. The reconciler only
   ever touches columns: a table that does not exist yet is skipped, because
   `create_all` has already handled it.
2. **Registered migrations.** `migrations.run_migrations` applies every
   migration in `wsi_app/migrations/` whose `version` is greater than the
   DB's recorded `schema_version`, in order, committing each **together
   with** its version bump — so a crash mid-chain resumes from the last
   good step. This is the escape hatch for changes the reconciler cannot
   express: data back-fills, column renames/splits, type changes, and
   table rebuilds (UNIQUE/FK reshapes SQLite can't `ALTER` in place).
3. **Automatic column reconciliation, DROP phase.**
   `_apply_column_migrations(phase="drop")` issues
   `ALTER TABLE ... DROP COLUMN` for any column the DB has but the ORM no
   longer declares. Running the migrations *between* the two reconciler
   phases is deliberate: a migration can populate a freshly-added column and
   still read one that is about to be dropped.
4. **Index reconciliation.** `main._reconcile_indexes` creates any index the
   ORM declares but the live schema lacks — which is every index on a column
   that arrived via `ADD COLUMN`, since SQLite cannot attach `UNIQUE` there
   and `create_all` builds indexes only for tables it creates itself. Run
   last so it sees the final column set. A `UNIQUE` index that cannot be
   built because the table already holds duplicates is logged at ERROR and
   skipped rather than blocking boot: de-duplicate and restart to restore
   the constraint.

   *One-time cost on the first boot after an upgrade that adds an index.*
   Building an index on a large table takes a while and holds the SQLite
   write lock throughout; on a `page_views` table at the default
   `WSI_ANALYTICS_LOG_MAX_ROWS` cap (5 million rows) expect several seconds,
   depending on disk speed. Later boots find the indexes and skip the work. Under
   `update.sh` this is harmless: both services are stopped, and the other
   `wsi-app` workers and `wsi-sync` (which runs the same startup code under
   the same lock) wait for the lock before they serve anything. The step
   also fits well inside the 30 s backend health check. Two cases to watch:
   - If you restart only `wsi-app` while an already-running `wsi-sync` keeps
     serving, its reads carry on (WAL), but a write that waits longer than the
     5 s `busy_timeout` fails once with `database is locked`. `wsi-sync`
     almost only reads, so at worst a live-sync connection attempt made
     during those seconds fails and has to be retried. Use `update.sh` (or
     restart both services) to avoid this.
   - If you raised `WSI_ANALYTICS_LOG_MAX_ROWS` well above the default, the
     build time grows with the row count and can exceed the health-check
     window, which makes `update.sh` roll back. Shrink `page_views` first
     with the scoped analytics wipe (see
     [Access analytics](#access-analytics)) so the build fits, or give the
     check more time (`sudo WSI_HEALTH_TIMEOUT=300 bash infra/update.sh`).
5. **Nullability drift check (read-only).**
   `_warn_nullability_divergence` logs any column the ORM declares `NOT NULL`
   that is nullable on disk. No step above can repair that — SQLite needs a
   table rebuild — so it is reported rather than fixed. It is normally
   silent; a line here means a column was added in place with a non-constant
   default and a numbered migration is needed to close the gap (to repair a
   database by hand, see `services/app/tools/normalize_live_db.py` and the
   developer guide link below).

Finally `app_meta.bootstrap_app_version` stamps the new `APP_VERSION` and
`app_version_seen_at` into the `app_meta` key/value table. The
staged-restore path does the same stamp on the staged DB before the
swap (`backup.apply_migrations_to_stage`), so the live binary always
opens a DB whose recorded version matches its own.

Between them these cover **every** kind of schema change: purely additive
ones need no bookkeeping at all (just declare the column on the ORM
model), and anything transformational gets a numbered migration. Authoring
rules for the latter — migrations must be defensive and idempotent, since
they run against the reconciled current-shape schema rather than a replayed
historical snapshot — are in
[`developer-guide.md` → Evolving the database schema](./developer-guide.md#evolving-the-database-schema).

### Free-space checks

Every operation that writes a lot of data checks up front that it fits, by
one rule: the bytes it is about to write must fit in the free space of the
volume they go to **with a reserve left over — 10 % of those bytes, and at
least 256 MiB** — so the live database, its WAL, the job records and other
uploads on that volume (`BACKUP_DIR`, `.staging/` and the upload spool all
default to the data volume) never find it full. What counts as "the bytes"
differs per operation:

| Operation | Bytes checked | Volume | Refused with |
|---|---|---|---|
| Backup | live DB ×2 (snapshot + archived copy) + WAL + `assets/` (+ `wsi/` and `thumbs/` for a backup with WSI files), uncompressed | `BACKUP_DIR` | job error "insufficient free space on backup volume" |
| Slide export | the selected slides' WSI bytes | `BACKUP_DIR` | `exportNoSpace` |
| Restore / slide-import upload (before the body is read) | the declared body | upload spool (`TMPDIR`) | HTTP 507 `uploadNoSpace` |
| Restore / slide-import staging | the archive's size before extracting, then the running total of extracted bytes, member by member | `BACKUP_DIR/.staging` | restore: job error "insufficient free space on staging volume"; import: `importNoSpace` |
| Restore staging, schema-upgrade trial | the staged `wsi.db` (the copy the startup schema upgrade is tried on) | `BACKUP_DIR/.staging` | job error "insufficient free space on staging volume … (for the copy of the staged database its schema upgrade is tried on)" |
| Reset | the live DB + WAL (the stage's snapshot) | `BACKUP_DIR/.staging` | job error "insufficient free space on staging volume" |
| Slide upload / replace | 2× the body (spool + staged copy) on one filesystem, or once on each when `wsi/` is elsewhere; once the body is in, the copy still to make. The bulk-upload screen runs the same rule before it starts, file by file in upload order with each earlier file's size taken off `wsi/`'s free space, and names the first file that would be refused | `wsi/` and the spool | HTTP 507 `uploadNoSpace` (the bulk-upload screen: a warning, and **Start upload** / **Retry failed** disabled) |
| Slide import, WSI copied onto the data volume | each slide's WSI (the plan reports the total) | `DATA_DIR/wsi` | `importNoSpace` |

Tour attachments are the one exception: they keep their own floor,
`TOUR_ASSET_MIN_FREE_BYTES` (1 GiB by default, `0` disables it), since each
is only a few MB. A slide upload's factor 2 is conservative on filesystems
that can share blocks (XFS / btrfs reflink). The checks are best-effort: when a volume's free space
cannot be read, or the filesystem reports no capacity at all (some FUSE
mounts), the check is skipped and the write's own "disk full" handling
decides. They also cannot see what other writers do at the same time, so
leave more room than the reserve on a busy install.

### Creating a backup

1. Sign in as an admin at `/admin`.
2. Open the **Backup & Restore** tab in the dashboard's sidebar (it is not
   on the separate **Server Info** page).
3. Choose which components to include (all checked by default — a `full`-scope
   backup). The DB core (slides, tours, settings) is always
   captured; each checkbox shows an approximate size next to it, and a running
   "selected total" estimate updates as you toggle:
   - **WSI image files** — the raw WSI files and their thumbnails. May be very
     large. Unchecking produces a metadata-only backup; the **Download** button
     still works for it in the browser, whereas a full archive in the GB–TB
     range is better copied off-host with `scp` / `rsync`. Restoring a backup
     taken **without** this component wipes the live `wsi/` and `thumbs/`
     directories and leaves them empty; you re-upload WSI files individually
     afterwards (see "Restoring WSI files after a metadata-only restore" below).
   - **Access statistics** — the analytics event tables and the error log.
     Uncheck to keep visitor IPs / emails out of an archive you intend to
     share off-site.
   - **Activity history** — the admin audit trail. Uncheck for the same
     privacy reason.
4. Optionally add a note (free-text, ≤ 500 chars) to remind your future
   self why you took the snapshot.
5. Click **Start backup**, then **Start backup** again in the **Confirm
   backup** dialog. The phase + bytes-written counter updates
   live. Closing the browser tab does **not** abort the job — it
   continues in the worker process.

When the job finishes, the new entry appears at the top of the
**Saved backups** list with its scope, size, app version, and the
first 12 hex chars of its SHA-256. The row lists the three components — WSI,
Stats, History — each marked ✓ (included) or ✕ (left out).

**Verify** on a row reads that archive end to end without restoring it: every
member through the compression checksums, and the raw bytes against the
SHA-256 recorded when it was made (`POST /api/admin/backup/{id}/verify`, a
job like the others, holding the slot while it runs). A truncated or
bit-rotted archive is reported as damaged, so it is found before a restore
needs it; a check that could not read the file (an I/O or permission error)
says so instead, without calling the backup damaged. A running check can be
stopped (**Stop check**), and is shown on every tab. A backup cannot be deleted while it is being verified or a restore
is being staged from it (409).

A backup that includes WSI files archives each image exactly as the database
snapshot describes it. Right after the snapshot, each slide's WSI file is
hard-linked into the snapshot directory (under the slide's lock, once its
generation is confirmed), so a slide **replaced or deleted while the archive
is being written** no longer matters: the archive carries the snapshot's
bytes. The links take no space (a deleted slide's blocks are freed when the
backup ends; those of a backup that was killed are freed at the next start,
backup or restore staging). Where links cannot be made — `wsi/` symlinked to another
volume than `BACKUP_DIR` — each file is checked when it is archived instead,
and a slide replaced or deleted after the snapshot, before its file is
archived, fails the job with a "replaced or deleted after the DB snapshot"
error rather than ship an archive that pairs the old width / height / MPP /
annotations with the new image — start the backup again once the slide is no
longer being changed. During the WSI phase the progress line also shows how
much of the slides' total has been read.
(A replace is recognised by the slide's replace counter, `wsi_generation`, as
well as by the file-derived columns, so even a replacement whose size,
dimensions and metadata all match the old file is caught.)
Replacing a slide whose file has already been archived is fine (the archive
keeps the bytes it captured), and nothing about a running backup blocks tile
serving or holds up replaces / uploads for longer than a file open. Thumbnails
caught mid-change are simply left out (rebuild them after a restore).

Only one backup / restore / reset / slide transfer runs at a time across all
workers. The claim on that slot is a record under `BACKUP_DIR/.jobs/`; if it
cannot be written (disk full, wrong ownership), the request fails with
**HTTP 503** and nothing is started — otherwise another worker could not see
the job and might start a second one beside it. If the worker running a job
exits before it finishes (a crash, the OOM killer, a restart of the service),
the record is left behind as "running"; the next poll of that job (or the
next job start) notices that its process is gone and frees the slot, and the
dashboard stops waiting: it shows the job as failed because its server worker
exited (or, if a job start reaped it first, as lost) — start it again.
The job holding the slot is visible from every tab and to every admin
(`GET /api/admin/backup/jobs/active`): a backup another tab or admin started
shows its progress and **Cancel backup** on any Backup & Restore tab, and any
other job is named there.

**Cancelling a running backup.** Because a full backup can hold that slot for
hours (blocking restore, reset and slide import / export meanwhile), the
progress line has a **Cancel backup** button (admin only, with a confirmation;
also `POST /api/admin/backup/jobs/{job_id}/cancel`). The request is recorded
as a `BACKUP_DIR/.jobs/<job_id>.cancel` marker, so it works whichever worker
receives it; the backup checks for it between phases and files and, about
twice a second, while streaming one large WSI file, so it usually stops within
a second or so. Two steps at the start cannot be interrupted — copying the
live database into the snapshot and, when statistics or history are left out,
compacting that copy (`VACUUM INTO` a file beside it, so it needs no more
space than the two copies the free-space check counts) — so a request made during one takes effect
when it finishes, which on a large database can take minutes; the dashboard
shows "Cancelling…" meanwhile. A cancelled backup removes its partial `.tar.*.tmp` archive and
its `.staging/wsi_backup_*` snapshot directory, publishes nothing, ends in
state `cancelled` (audit rows `backup.create.cancel_requested` and
`backup.create.cancelled`) and frees the slot immediately. A request that
arrives in the last instant, once the finished archive is being published, is
too late: the backup completes normally and the dashboard says so. Only
backup jobs can be cancelled; restore staging, applies, resets and slide
transfers cannot (the endpoint answers 409 for them, and for a job that has
already finished).

### Downloading and deleting

- **Download** uses a two-step flow: the dashboard requests a
  short-lived signed URL from `POST /api/admin/backup/{id}/download_url`,
  then the browser navigates to that URL via a synthetic `<a download>`
  click so the archive streams through the browser's native download
  pipeline. The signed token is scoped to a single backup id, expires
  after `WSI_DOWNLOAD_TOKEN_TTL_SECONDS` (default **3600 s / 1 hour** — long
  enough for a slow link to finish or resume a multi-GB archive), and is
  verified by the API on the GET request — no Authorization header is needed
  for the download itself, which is what lets the native download path work
  over plain HTTP. Nor is an app-access login: under `basic` / `ldap` /
  `google_oauth` the app-access gate admits a valid token on its own, so the
  download works for an admin who signed in only to the dashboard. The token
  also carries the minting user's id and
  revocation counter, both re-checked against the live row on every use, so
  suspending that account or changing its password invalidates any
  outstanding link immediately rather than at expiry. The `GET` endpoint
  also still accepts an admin Bearer token for CLI clients. The
  downloaded filename is `<id>.tar.zst` or `<id>.tar.gz`.

  nginx logs these downloads (and the slide and export ones, which use the
  same kind of link) with the token masked:
  `"GET /api/admin/backup/<id>/download?t=[redacted] HTTP/1.1"` in
  `/var/log/nginx/access.log`, written in the stock `combined` layout by the
  `wsi_combined` format in `infra/nginx/wsi.conf` (a token passed as `?%74=`,
  which the app reads as `t`, is masked too). If you changed the site's
  access log, see [nginx access log](#nginx-access-log). The app's own request log
  in the journal (`journalctl -u wsi-app`) masks it the same way. One place
  can still show a live token, so keep it as private as the backups
  themselves until the token expires: nginx's **error** log, whose layout
  cannot be changed, if an error happens while serving the download (an
  upstream timeout, a rate-limit refusal).
- **Delete** removes both the archive and its `<id>.tar.zst.meta.json`
  (or `.tar.gz.meta.json`) sidecar. There is no soft-delete or trash bin; double-check first.

### Restoring

There are two entry points:

1. **Restore button** on a row in the Saved backups list — restores
   from a server-side archive.
2. **Upload archive** in the **Restore** panel (below the Saved backups
   list) — accepts an
   archive previously downloaded from this or another install (subject
   to the version policy above). The browser sends the archive in one
   request; the ceiling is `BACKUP_MAX_UPLOAD_BYTES` (default 1 TiB).

Either path stages the archive into `BACKUP_DIR/.staging/<stage-id>/`,
validates the manifest and DB, then renders a **Restore preview**. Staging runs
as a background job (`POST /api/admin/restore` answers `202 {"job_id"}` as soon
as the server has received the upload; the dashboard polls
`/api/admin/backup/jobs/<job_id>`), because copying a large upload into
staging — a full second copy on a filesystem that cannot share blocks, such as
ext4 — and extracting and verifying a full-scope archive can take far longer
than nginx (600 s) or a Cloudflare Tunnel (100 s) waits for a response. The
button shows how far the copy, then the extraction, has got. If a proxy
answers the upload itself (an HTML 502 / 503 / 504, or Cloudflare's 520–524),
the dashboard says it is unknown whether the server received the archive:
wait a few minutes before uploading again, since a new upload is refused (409)
while a staging job still runs — only one backup / restore / reset / slide
transfer job runs at a time. The dashboard remembers the job for the browser
tab, so switching to another sidebar section or reloading the page resumes
it — and, once it is done, shows the same staged preview again — instead of
leaving the staged tree unreachable until the 24-hour sweep. **Cancel** on the
preview deletes the stage at once. Staging another archive while one is staged
releases the first only once the server has accepted the new one — the new
staging job removes it before measuring free space — so a refused or
cancelled attempt keeps it, and the new one is not refused for the space the
old one held. A staging job whose worker was killed leaves a
partial stage (and the copy of an uploaded archive); the next staging removes
them first, whatever their age, so their space does not count against it.
An uploaded archive is copied into staging by block sharing where the
filesystem supports it (XFS with reflink, btrfs: no extra space); otherwise
the staging volume must have room for the copy plus the shared reserve, or
the job fails up front with "insufficient free space".

Staging fails the job, with the reason as its error, for an archive that fails
any of these checks:

- The compressed stream is read to its end, so the gzip CRC32 / zstd frame
  checksum is verified (otherwise a damaged `.tar.gz` can extract "successfully").
- An archive restored from the Saved backups list is hashed in the same
  pass and must match the `sha256` its `.meta.json` sidecar recorded at
  creation. Uploaded archives carry no sidecar and rely on the codec
  checksum.
- The staged `wsi.db` must pass `PRAGMA quick_check`.
- No member name may contain a `..` segment or appear twice.
- At most 1,000,000 members (files and directories). A real archive holds two
  per slide plus each tour's attachments, far fewer; without the cap a few
  hundred KB of compressed empty entries could create millions of files on the
  data volume.
- The extended headers in front of a member (pax headers, GNU long names) may
  declare at most 1 MiB together, counting the keys that global pax headers
  earlier in the archive have set (a key a later global header sets again
  counts once), and GNU sparse members are refused. tar reads
  those headers into memory whole before it reaches the member, so without
  the cap a few KB of compressed zeros declaring a huge pax header would use
  gigabytes of RAM. Archives this app writes stay far below it (one pax header
  per member at most, holding a path). Slide-export bundles are read with the
  same checks.
- The extracted contents must fit on the staging volume (by default the data
  volume the live database writes to) with a reserve left over: 10 % of the
  extracted size, and at least 256 MiB (the rule every
  [free-space check](#free-space-checks) uses). The archive's own size is
  checked against it before anything is extracted, then the extracted total
  member by member, so an archive that does not fit is refused before the
  volume fills.

Once the archive has passed those checks, the staging job also tries the
startup schema upgrade the restored database will get at its first boot
(`main.reconcile_schema`: the column ADD / DROP phases, the numbered
migrations, the indexes, the version stamp) on a throwaway copy of the staged
`wsi.db` — checked against the free-space rule first, and removed afterwards.
An archive this version cannot bring up to date (say, a stale column a view
still references, which SQLite cannot drop) is not a staging failure: the
stage is kept and the preview shows a **Restore blocked** banner with the
reason (`SCHEMA_UPGRADE_FAILED`), which the preflight repeats and the apply
refuses with HTTP 409. Without the trial that failure would only appear after
the swap, leaving the service unable to start on the restored data. It runs
in the staging job rather than in the apply request because the copy takes
seconds per GB, longer than nginx waits for `/api/admin/` responses (60 s).

An upload that cannot even be copied into staging because the disk is full
fails the job with `uploadNoSpace` (*Not enough free disk space on the server
to accept this upload*).

The preview then shows:

- **Backup version** → **Current version** (the `app_version` strings; the
  integer `schema_version` is what the version policy keys on, but it is not
  displayed — a refusal because of it appears in the **Restore blocked**
  banner)
- Slide / tour counts, scope, note
- A red **Restore blocked** banner if the version policy refuses the
  archive
- A **Pre-apply summary** ("preflight") showing exactly what live data
  will be deleted and what the archive will install — including a
  count of slides whose WSI files will need to be re-uploaded after a
  metadata-scope restore
- An archive-integrity verdict: the apply endpoint will refuse the
  archive if its DB references WSI files that aren't in the archive's
  `wsi/` payload (or vice-versa for an unexpected `wsi/` in a
  metadata-scope archive). Surfaced as a `409 ARCHIVE_INCONSISTENT`
  response (shown as a translated message) so the operator sees the problem
  before the restart.
  A tour row whose `public_id` is not a well-formed token (it names the
  `assets/tours/<public_id>/` directory) is only a **warning** (listed
  under `integrity.warnings`, shown in the preview, and logged): backups archive such a row but
  skip its assets, also with a warning, and every code path validates the
  token before using it as a path, so the row restores harmlessly with no
  reachable assets. This keeps a backup that succeeded restorable.

The plan's migration list is always empty ("no migrations needed") and is not
displayed: the restore itself only stamps the staged DB, and any registered
migration runs on the next backend boot instead.

If the preview is acceptable, click **Restart service & apply**, then
in the confirmation modal **re-enter your admin password** and submit.
The exact confirmation phrase the API checks is `RESTORE` — the UI
fills this in for you, but it's there as a guard against scripted
double-clicks. Apply will refuse to proceed if the archive's integrity
check failed; cancel the stage and re-create a clean backup instead.

### What happens during apply

1. The API stamps the staged DB with the running `APP_VERSION` (so the
   next boot's downgrade guard sees a matching version). The live DB
   is **not yet touched.**
2. The API writes `/run/wsi/restore.request` (a JSON payload describing
   the swap). After the HTTP 202 has flushed (so the browser receives the
   response first), it drops `/run/wsi/restart.trigger`. The root-owned
   `wsi-restart.path` systemd unit watches that file and runs the privileged
   `systemctl restart wsi-app.service` for it — the app runs under
   `NoNewPrivileges=true` and cannot use `sudo` itself. (Same pattern as
   `wsi-avahi-reload.path`.) Because a restore request is pending, it also
   **stops `wsi-sync.service` first** and, if it was running, starts it
   again only once the swap has finished (or after 15 minutes, or as soon as
   `wsi-app` fails): the sync coordinator is a separate process with its own
   SQLite handles, which would otherwise stay on the parked pre-restore DB
   (answering live-sync auth / IP-filter / staff checks from the old data
   and writing its audit rows into the parked copy). As a backstop, every
   process also drops a pooled connection whose DB file was replaced under
   it (`db.install_replaced_file_guard`). Live-sync sessions in progress end
   with the restore.
3. systemd brings up `start-wsi-app.sh` again. With `wsi-app` a fresh
   process tree and `wsi-sync` stopped, no SQLite handle is held; the
   script's restore-apply phase runs **before** uvicorn starts. The swap
   itself is done by `/opt/wsi/services/app/wsi_app/restore_apply.py`, which
   the script runs with the venv's Python (`python -m wsi_app.restore_apply`
   from `/opt/wsi/services/app`); it uses only the Python standard library,
   so it never loads the app's settings or database layer. Before
   moving anything it writes the full list of planned moves to a journal
   on the data volume (`/var/lib/wsi/.restore-apply.journal`, fsync'd) and
   advances a cursor in it before each move, so the swap survives being cut
   short (see "Recovering from an interrupted restore" below). Every move is
   a single `rename`; a stage on a different filesystem fails the apply
   instead of degrading to a slow, non-atomic copy.
   1. Move the live `wsi.db` (plus any `-wal` / `-shm` / `-journal`
      siblings) aside into `/var/lib/wsi/.pre-restore-<UTC-timestamp>/`
      and promote the staged `wsi.db` straight away; then park
      `assets/`, **`wsi/`**, and **`thumbs/`** there too. (`wsi/` and
      `thumbs/` are parked regardless of scope — restore is a wholesale
      replace, so a metadata-scope restore deliberately wipes the live
      WSI files and their thumbnails along with the DB and assets.
      Thumbnails are keyed by slide id, so keeping a stale tree would
      mis-render any restored slide whose id collided with a previous
      deployment's.)
   2. Move the remaining staged contents into place. For a `full` archive the
      staged `wsi/` and `thumbs/` land at `/var/lib/wsi/`; for a
      `metadata` archive (or a reset) both stay empty until the
      operator re-uploads slides (or rebuilds thumbnails after a bulk
      copy).
   3. Sign everyone out: every user's `token_version` and the app-access
      `session_generation` are moved past both the restored value and the
      value it had before the swap. A restore otherwise brings back the
      archive's older counters, and with them every staff token and viewer
      cookie revoked since the backup was taken (a password change, a
      disabled account, "sign everyone out"). So after a restore (or reset)
      **all staff and viewers sign in again**. An archive without an
      `auth_settings` row — the archived install never saved its
      authentication settings — has nowhere to hold it: the value is left in
      `wsi.db.session-generation-floor` beside the database, and the next app
      start records it in that row and removes the file. When the parked
      pre-restore database cannot be read — the restore replaced a damaged
      database, or its `.pre-restore-*` directory was deleted before a
      retry — the sign-out does not need it: every counter is moved past the
      current time in milliseconds since 1970 instead, a value the old
      database's counters (which start below 2³¹ and move on by one per
      revocation) cannot have reached, and `journalctl` notes it. Should this
      step fail — the restored database itself cannot be updated — the
      swap still stands; the result file reports
      `session_revocation_error`, and `wsi.db.session-revocation-pending`
      is left beside the database. The app's next start retries the
      sign-out (against the parked pre-restore database the file names)
      before it serves anything, removes the file once it succeeds, and
      **refuses to start** while it keeps failing — the reason is in
      `journalctl -u wsi-app`. Deleting the file lets the app start without
      it, accepting that logins revoked before the restore may work again
      (then use **Sign everyone out** in the authentication settings and
      review the staff accounts).
   4. Write `/run/wsi/restore.result` with `{"ok": true, ...}`.
      The result file includes `rollback_root`, the `.pre-restore-*`
      directory holding the replaced data, and `wsi_uploads_pending` — for a
      `metadata` restore (or a reset) the number of slide rows with a
      non-empty `wsi_filename`, whose files were all wiped and must be
      re-uploaded; `0` for a `full` restore.
   5. On any failure, **all moved files are restored from the
      `.pre-restore-*` directory automatically** (which is then removed,
      being empty), and the result file
      records `{"ok": false, "error": ...}` so the UI can surface it.
      A failure to write the result file itself does not undo a completed
      swap; the outcome is then only in the systemd journal.
   6. The restore-apply journal is removed only once the tree is
      consistent again (fully restored, or fully rolled back). **uvicorn
      is never started while it exists** — if the rollback itself cannot
      finish, the unit fails instead of serving a half-swapped tree.
4. uvicorn starts. The dashboard polls `/api/admin/restore/result` until
   it describes this apply (matched by stage id), then reloads itself —
   everything it held in memory described the pre-restore data — and shows
   the outcome banner on the Backup & Restore tab. If the restarted service no longer
   accepts the browser's login (the restored database has its own accounts,
   or the session expired during the wait), the dashboard says so and asks
   you to log in again; the Backup & Restore tab then shows the result. That is not a
   failure — a successful restore always ends every session (step 3.3).

**A request no restart picks up expires after 10 minutes.** Dropping the
trigger file counts as success even if nothing acts on it (`wsi-restart.path`
disabled, `wsi-restart.service` failed on its start limit), and while the
request waits every backup, restore, reset and slide import/export is refused
(409). Left alone, the next unrelated restart (`set-workers.sh`,
`set-cache-size.sh`, `update.sh`, a manual `systemctl restart`) — possibly days
later — would apply that old stage and park everything changed since under
`.pre-restore-*`. So a request older than 10 minutes (a legitimate restart
reaches the start script within about a minute) is no
longer treated as pending, the start script refuses to apply it, and
`restore.result` reports `"ok": false, "stage": "expired"` — written by
whichever comes first: the API (when the Backup & Restore tab is opened or the next job
is started) or the next start. The live data is left as it is and the stage
stays for 24 hours: fix the restart path, then apply again from the Backup &
Restore tab (the new request clears the old report, so the tab waits for this
apply's own outcome), or remove the staged archive from the outcome banner. The age is measured on the kernel's boot clock, which the request records
(`requested_boot_id`, `requested_boot_seconds`), so the system clock being
set back or forward (NTP correcting a wrong hardware clock, a resumed VM)
between the request and the restart does not change it. Only a request
without that record — not written in the current boot, or on a host without
a boot clock — is aged by its `requested_at`, and is then also refused when
that stamp is more than 10 minutes in the future, since its real age is
unknown.

The dashboard remains unavailable for roughly 10–30 seconds while the
apply restarts the service. If it stays unavailable longer, check the service:

```bash
sudo systemctl status wsi-app.service
sudo journalctl -u wsi-app -n 200 --no-pager
sudo cat /run/wsi/restore.result   # {"ok": true, ...} or {"ok": false, "error": ...}
```

(`restore.result` and the `.pre-restore-*` directories are written by the
service with `UMask=0027`, so they are readable by `wsi` and root only — hence
the `sudo` here and below.)

**A `.pre-restore-*` directory does not mean the restore failed.** Every
successful apply leaves one behind holding the replaced (pre-restore) data —
your way back to the old state, so do not move it back unless you mean to undo
the restore. `rollback_root` in `restore.result` names it. After a *failed*
swap its contents are moved back into place and the then-empty directory is
removed (should that removal fail, the Backup & Restore tab's rollback list hides an
empty one). Judge the outcome by `restore.result` (and the journal), not by
the directory's presence. Reclaim old ones from the Backup & Restore tab's rollback list
once you are sure you won't need them.

### Recovering from a failed restore

The `.pre-restore-<timestamp>/` directory in `DATA_DIR` is the
authoritative rollback. The start script restores from it
automatically on a swap failure, but if the service still won't start
(e.g. the staged DB itself is incompatible in a way the version check
missed), you can restore manually. Only do this when the restore actually
failed — `restore.result` reports `"ok": false`, or the service will not
come up; after a successful restore these steps put the **old** data back
and undo it. (If `/var/lib/wsi/.restore-apply.journal` exists, the apply was
interrupted mid-swap — follow "Recovering from an interrupted restore" below
instead; the service will not start until that journal is resolved.)

A successful restore leaves the *new* data in place, so move that aside first
(otherwise the `mv` of a directory onto an existing non-empty one fails, and a
stale `wsi.db-wal` would be replayed onto the old database). Take the rollback
directory from the result of the restore you are undoing — not simply the
newest `.pre-restore-*`, which may belong to a later apply — and check that it
holds a `wsi.db` before moving anything:

```bash
sudo cat /run/wsi/restore.result   # "rollback_root": "/var/lib/wsi/.pre-restore-<UTC>"
# If the result is gone (a reboot clears /run): the newest directory that
# actually holds a database.
sudo sh -c 'ls -1dt /var/lib/wsi/.pre-restore-*/wsi.db' | head -n1
ROLLBACK=/var/lib/wsi/.pre-restore-<UTC>   # the directory found above (without /wsi.db)
sudo ls -l "$ROLLBACK/wsi.db"              # must exist: otherwise stop here
```

Then:

```bash
sudo systemctl stop wsi-app.service wsi-sync.service
ASIDE=/var/lib/wsi/.undo-aside-$(date +%s)
sudo -u wsi mkdir "$ASIDE"
for item in wsi.db wsi.db-wal wsi.db-shm wsi.db-journal assets wsi thumbs; do
  [ -e "/var/lib/wsi/$item" ] && sudo -u wsi mv "/var/lib/wsi/$item" "$ASIDE/"
done
sudo -u wsi mv "$ROLLBACK/wsi.db" /var/lib/wsi/wsi.db
# The tests run as wsi too: $ROLLBACK is 0750 wsi:wsi, so a plain [ -e ] as
# your login user is always false and would silently leave the data parked.
for item in wsi.db-wal wsi.db-shm wsi.db-journal assets wsi thumbs; do
  sudo -u wsi test -e "$ROLLBACK/$item" && sudo -u wsi mv "$ROLLBACK/$item" /var/lib/wsi/
done
sudo systemctl start wsi-app.service wsi-sync.service
```

Once the service is healthy, delete the now-empty rollback directory (and the `.undo-aside-*` directory, which holds the data of the restore you just undid, once you are sure you do not need it).
Old `.pre-restore-*` directories are **not** garbage-collected
automatically — keeping them is intentional in case a regression is
spotted only after the fact.

### Recovering from an interrupted restore

If the apply is cut short mid-swap — `systemctl stop` / a stop timeout
(SIGKILL), an OOM kill, a crash, a power loss or host reboot — the data
directory can be left half-swapped (for example the restored `wsi.db` in place
but the old `wsi/` still parked). The next start of `wsi-app` handles this
**automatically, before anything else**, from the journal at
`/var/lib/wsi/.restore-apply.journal` (it does not need
`/run/wsi/restore.request`, which a reboot wipes):

- If every move had already landed, the restore is finished and reported as
  successful (`"recovered"` in `restore.result`).
- Otherwise the moves are undone newest-first, the pre-restore data is put
  back, and `restore.result` reports `"ok": false` with
  `"stage": "interrupted"`. Re-stage the archive and apply again.

`wsi-sync` (and the app itself, if started by any other means) refuses to start
while the journal exists, so nothing opens the database mid-reconciliation;
systemd retries it every few seconds until the journal is gone. The start
script holds that line even if it cannot run the helper at all (e.g. a broken
deploy is missing `wsi_app/restore_apply.py`): it logs the helper's exit status
and refuses to start uvicorn while the journal exists. (A pending request with
no journal is then dropped instead, leaving the live data untouched.)

The reconciliation stops rather than guess if the tree no longer matches the
journal (e.g. someone moved or deleted part of a `.pre-restore-*` directory by
hand). The unit then fails with `RESTORE APPLY NOT RECONCILED: ...` in
`journalctl -u wsi-app`, and `restore.result` has `"stage": "recovery"`. To
resolve it by hand:

```bash
sudo systemctl stop wsi-app.service wsi-sync.service
sudo cat /var/lib/wsi/.restore-apply.journal
# "steps" lists every PLANNED move as {"op", "src", "dst"}, in order;
# "done" is how many of them may have run (the last of those possibly not).
# Undo only those first "done" steps, newest-first — move each one's "dst"
# back to its "src" where "dst" exists and "src" does not, as wsi
# (sudo -u wsi test -e <dst> && sudo -u wsi mv <dst> <src>). Leave every step
# after them alone: they never ran, and a later step's "dst" can be live data
# still in place (moving it would put it into the stage, which is deleted).
# Or otherwise put the data directory into the state you want
# (the parked pre-restore data is under the journal's "rollback_root").
# Then accept that state:
sudo rm /var/lib/wsi/.restore-apply.journal
sudo systemctl start wsi-app.service wsi-sync.service
```

The dev-only in-process apply (`BACKUP_DEV_INPROCESS_APPLY=1`) runs the same
`restore_apply` code, writes the same journal and reports its outcome in the
same result file (`RESTORE_RESULT_PATH`); with that flag set, the app
reconciles a leftover journal itself at startup. It signs everyone out as
well — the dashboard that applied it included, before the reset job even
reads as finished — so the dashboard sends you to sign in again and then shows
the result. Having no restart after the swap, it then brings the running
process up to the swapped-in data itself (schema reconcile, bootstrap, the
sign-out retry, this worker's slide caches); should one of those steps fail,
the swap still stands — the result keeps `"ok": true` and gains
`post_apply_error` (with `post_apply_stage`), the same message the apply
answered and its job failed with — and restarting the app finishes the job.
While a sign-out it could not finish is still pending
(`wsi.db.session-revocation-pending`), a further in-process restore or reset
is refused until a restart has run the retry: a second swap would otherwise
lose the first restore's record of the sessions it must sign out. New
database connections of the process wait (up to 30 s) while the swap moves
`wsi.db`, so a request arriving mid-swap opens the restored database instead
of creating an empty one where it must land.

### Restoring WSI files after a metadata-only restore

A `metadata` scope restore swaps in the database and tour assets and
**wipes the live `wsi/` and `thumbs/` directories** (parked under
`.pre-restore-*/` for rollback). The post-restore install therefore has
rows for every slide — with their original `wsi_filename` values — but no
files on disk; the app returns 404 for tile requests until the operator
re-uploads the WSI files individually.

> **Publishing is gated on the WSI body.** Until a slide's WSI file is
> back on disk, the publish endpoint refuses to flip it from draft to
> published (*Cannot publish: the WSI file itself is missing.*). A slide
> can carry complete metadata after a metadata restore and still not be
> publishable until its file is re-uploaded or re-copied — this prevents
> a published gallery card that 404s on every tile request.

This is the intended workflow for metadata-scope backups: WSI files
are assumed to be archived separately (object store, image archive,
SAN snapshot, …), and the dashboard's job is to restore the
*metadata layer* on top of whatever WSI inventory the operator brings
to the host.

To see which files the new DB expects, use the **WSI File Status**
panel in the admin dashboard (**Backup & Restore → WSI
File Status**) — it lists every slide whose file is missing from
`/var/lib/wsi/wsi/`. You can also query the database directly:

```bash
sudo -u wsi sqlite3 /var/lib/wsi/wsi.db \
    "SELECT id, wsi_filename FROM slides ORDER BY id;"
```

(The database is `0640 wsi:wsi`, so query it as `wsi`.)

Re-upload each missing file with **Replace WSI file** in that slide's edit
dialog (this is the normal path; it generates the thumbnail and respects the
size cap). Do not use **Upload WSI**, which creates a new slide. Replace
uses the per-slide endpoint (`POST /api/admin/slides/{id}/wsi`), which
targets the existing row and keeps its ID and metadata. Although uploads
reject a filename already used by another slide (case-insensitive, HTTP 409),
the replace path excludes the target slide itself — so restoring a
file under its original `wsi_filename` never trips the duplicate guard.
If you instead need to bulk-copy files in from another
host, drop them into `/var/lib/wsi/wsi/` preserving the exact
filenames recorded in `wsi_filename`:

```bash
# On the source host. The files (and the DB) are readable by wsi only, so
# rsync runs under sudo; -e runs its ssh as you, with your keys and (sudo
# drops SSH_AUTH_SOCK, so it is passed on explicitly) your ssh agent. They
# land in ~/wsi-incoming on the new server, which your ssh login can write.
sudo -u wsi sqlite3 /var/lib/wsi/wsi.db \
    "SELECT wsi_filename FROM slides WHERE wsi_filename <> '';" > /tmp/wsi-files.txt
sudo rsync -av --files-from=/tmp/wsi-files.txt \
    -e "sudo -u $USER env SSH_AUTH_SOCK=$SSH_AUTH_SOCK ssh" \
    /var/lib/wsi/wsi/ newserver:wsi-incoming/

# On the new server: move them into place and hand them to wsi.
sudo mv ~/wsi-incoming/* /var/lib/wsi/wsi/
sudo chown -R wsi:wsi /var/lib/wsi/wsi/
```

A bulk copy like the above lands the WSI bytes but does **not** create
thumbnails (those are normally generated by an upload or replace). After
the files are in place, rebuild the gallery thumbnails:

- **API:** `POST /api/admin/slides/rebuild-thumbnails` (admin/manager;
  there is no dashboard button for it).
  It regenerates a thumbnail for every slide whose WSI file is now present
  and whose thumbnail is missing (pass `force=1` as a form field, not a
  query parameter, to rebuild all:
  `curl -X POST -H "Authorization: Bearer <admin_token>" -F force=1 http://<host>/api/admin/slides/rebuild-thumbnails`;
  the token comes from [User management](#user-management)). Slides
  whose WSI file is still absent are skipped, so a thumbnail is never left
  pointing at a slide with no image. It never waits on a slide another
  operation is changing at that moment: such a slide is listed in
  `skipped_busy` (run the rebuild again for it), and a slide still being
  uploaded or imported is left out altogether, since that upload or import
  makes its own thumbnail. Deleting or publishing a slide that is still
  being uploaded or imported is likewise refused (409; in a bulk action that
  slide is reported as failed) rather than left waiting until it finishes.

Deep Zoom tiles are rendered from the WSI files on demand, so there is no tile
directory to copy. Once the files land in `wsi/` the app
serves tiles without a restart; rebuild thumbnails as above so the gallery
cards render.

> **Rollback knob:** the wiped pre-restore `wsi/` and `thumbs/`
> directories are still available under
> `/var/lib/wsi/.pre-restore-<UTC>/` if the operator decides to revert
> the restore. The "Recovering from a failed restore" steps above move
> them back in place.

### Resetting the install (Backup & Restore → Reset)

The Reset action wipes every piece of slide / tour / page-view /
slide-dwell / tour-session content — and with it the audit history, error log
and performance results — and the on-disk `assets/`, `wsi/`, and
`thumbs/` trees, but **keeps the staff accounts (admin, manager, instructor) and dashboard settings**. Use it to take an install
back to "freshly bootstrapped" state without re-running the installer
— e.g. after demo data has accumulated, or before handing the host
off to a different operator.

Preserved tables (the rest is emptied):

- `users` — admin / staff login rows
- `app_meta` — schema version stamp
- `viewer_settings`, `network_settings`, `auth_settings` — operator-
  tuned dashboard configuration
- `auth_users` — app-access login accounts (`basic` mode)
- `role_permissions` — per-role capability flags
- `seasonal_backgrounds` — dashboard background configuration
- `metadata_options` — curated metadata option lists (Metadata tab)

The flow mirrors a restore-apply:

1. The dashboard requests `/api/admin/reset/preflight` and shows what
   will be deleted (slide / tour counts, WSI file count; total bytes in
   `wsi/` and `assets/`) alongside what survives.
2. The operator confirms by typing `RESET` and re-entering their
   admin password, then clicks **Restart service & reset**.
3. The API starts a background job (`POST /api/admin/reset` answers
   `202 {"job_id", "stage_id", "scope"}`; building the stage of a large,
   analytics-heavy database takes longer than a proxy waits). The job builds
   a stage directory holding an empty DB (snapshot of the live DB with the
   non-preserved tables wiped, then VACUUMed — refused up front when the
   staging volume cannot take that snapshot with the
   [free-space reserve](#free-space-checks) left over) and uses the same
   `request_apply` + systemd-restart mechanism as a restore. `wsi/`,
   `thumbs/`, and `assets/` are parked under
   `/var/lib/wsi/.pre-restore-<UTC>/` along with the old DB. The dashboard
   shows "Preparing the reset…" while the job runs, then waits for the
   restore result of that stage id (the restart may take the job's worker,
   and the browser's login, with it). If the reset request itself got no
   answer, the dashboard says the outcome is unknown rather than inviting a
   second reset: reload and check the result first.
4. On any failure the swap rolls back automatically — same path as
   a restore. As after a restore, every staff and viewer session ends, so
   sign in again.

Manual rollback of a successful reset uses the same procedure as the
"Recovering from a failed restore" steps above; the parked `wsi/`,
`thumbs/`, `assets/`, and `wsi.db` are still on disk until the operator
removes them.

### Off-the-record: making a backup without the dashboard

For pre-deploy snapshots in a shell session, a similar archive (`wsi.db` and
`assets/` at the top level, as in a dashboard backup) can be produced
manually:

```bash
# umask 077: the snapshot holds password hashes, so keep it private (0600)
sudo -u wsi sh -c 'umask 077 && d=$(mktemp -d) &&
  sqlite3 /var/lib/wsi/wsi.db ".backup $d/wsi.db" &&
  tar -czf /tmp/wsi-snap.tar.gz -C "$d" wsi.db -C /var/lib/wsi assets
  rc=$?; rm -rf "$d"; exit $rc'
# It is 0600 wsi:wsi, and /tmp is emptied at boot: keep a copy with sudo,
# e.g. sudo cp /tmp/wsi-snap.tar.gz /root/
```

This produces a plain tarball without `manifest.json`, so the
dashboard's restore flow refuses it at staging (`archive missing
manifest.json`). It's still suitable for a manual re-extract via the
"recovering from a failed restore" steps above.

### Configuration

All in `/etc/wsi/wsi.env`:

| Variable | Default | Purpose |
|---|---|---|
| `BACKUP_DIR` | `/var/lib/wsi/backups` | Where archives + `.staging/` live. Also holds the background-job store (`.jobs/`). Pointing this **outside** `DATA_DIR` requires adding the path to `ReadWritePaths=` in **both** `wsi-app.service` and `wsi-sync.service` — `ProtectSystem=strict` makes every other path read-only, and the sync service runs the same app (so its boot janitor touches `.jobs/` too). **Restores** additionally need it on the **same mount** as `DATA_DIR`: the staged archive is swapped in with atomic renames, which cannot cross a mount point, and every `ReadWritePaths=` entry is its own bind mount even on one disk — so outside `DATA_DIR` both must sit under a single `ReadWritePaths=` entry, or every restore is refused up front. If the job store under it cannot be written, every backup / restore / reset / slide transfer request fails with HTTP 503 rather than run without the cross-worker "one job at a time" record. The app logs a startup warning when the directory is unwritable. |
| `BACKUP_MAX_UPLOAD_BYTES` | `1099511627776` (1 TiB) | Upper bound for uploaded archives (backup restores and slide-import bundles). nginx is configured with `client_max_body_size 0` (unlimited) on `/api/admin/restore` and `/api/admin/slides/import`, so this env is the **only** effective cap — change it here and restart the service; no nginx edit needed. A request whose `Content-Length` already exceeds the cap is refused with HTTP 413 before its body is read. The body is otherwise spooled to a temporary file on the data volume (`TMPDIR`) by the multipart parser, then checked against the cap again (HTTP 413) before the restore or import staging job copies it into staging — so a header-less (chunked) upload is only refused after it has been received. Slide uploads (`WSI_MAX_UPLOAD_BYTES`) are checked the same way. Free space is checked too, before the body is read and again once it is spooled (HTTP 507 `uploadNoSpace`; what must fit is in [Free-space checks](#free-space-checks)). A restore or slide-import upload sent while another backup / restore / transfer job is running (or a restore-apply awaits its restart) is likewise answered HTTP 409 before its body is read. |
| `BACKUP_MAX_EXTRACT_BYTES` | `4398046511104` (4× the upload cap) | Upper bound for the **decompressed** total of an archive extracted to staging, guarding against a high-ratio zstd bomb blowing past the upload cap during extraction. |
| `RESTORE_REQUEST_PATH` | `/run/wsi/restore.request` | IPC file written by the API, read by `start-wsi-app.sh`. **Do not change it:** `wsi-restart.service` hard-codes this path to decide whether to stop `wsi-sync` before the restart, so a different value is not noticed (edit the unit as well if you really must). |
| `RESTORE_RESULT_PATH` | `/run/wsi/restore.result` | IPC file written by the start script, read by the API |
| `RESTORE_JOURNAL_PATH` | `${DATA_DIR}/.restore-apply.journal` | Crash-recovery journal of an in-flight restore / reset swap. Must be on the persistent data volume (not `/run`). While it exists, `wsi-app` reconciles it before starting and `wsi-sync` refuses to start. |
| `RESTART_TRIGGER_PATH` | `/run/wsi/restart.trigger` | Trigger file the API drops to request a restart; watched by `wsi-restart.path`, which runs the privileged `systemctl restart`. **Do not change it** without also editing `PathExists=` in `wsi-restart.path`: with a different value no restart is ever triggered, so every restore / reset is held pending (backups, restores and imports return 409) until it expires 10 minutes later. |
| `RESTORE_RESTART_CMD` | _(empty)_ | Optional explicit restart command. Empty selects the `wsi-restart.path` trigger-file mechanism. A `sudo`-based value **cannot** work under the unit's `NoNewPrivileges=true`. |
| `BACKUP_DEV_INPROCESS_APPLY` | `0` | **Dev only.** When `1`, the apply path swaps files in-process instead of via systemd. Never set this in production — it races in-flight requests. |

`infra/install.sh` provisions the prerequisites:

- `/var/lib/wsi/backups/` and its `.staging/` subdir, mode `0750`,
  owned by `wsi:wsi`.
- `/etc/tmpfiles.d/wsi.conf` so `/run/wsi/` is recreated as `wsi:wsi`
  on every boot.
- `wsi-restart.path` + `wsi-restart.service` (enabled), which restart
  `wsi-app.service` when the API drops `/run/wsi/restart.trigger`.

If `wsi-restart.path` is not enabled, the restore-apply step writes the
trigger file but nothing acts on it, so the service never restarts and the
swap is not applied. After 10 minutes the request expires (see [What happens
during apply](#what-happens-during-apply)): the Backup & Restore tab then reports it
as `"stage": "expired"`, and a later restart does not apply it. Enable the
unit (`sudo systemctl enable --now wsi-restart.path`, or re-run
`install.sh`) and apply again while the stage still exists.

---

## Slide export / import (cross-Tessera migration)

The **Backup & Restore** feature above snapshots and *replaces* the whole
database. When you instead want to move **specific slides** — with their tours —
from one Tessera install to another, use **Slide export / import**. (To keep a
whole second server in step with this one, see [Backup server and server
moves](#backup-server-and-server-moves--infrasync-fromsh).) It is:

- **Additive** — an import adds the bundled slides as **new** entries alongside
  everything already in the target's database. Existing slides are never
  overwritten and the service is **not** restarted.
- **Admin-only** — the bundle contains the raw WSI file, with the vendor label
  images embedded in it (which can carry patient identifiers), and an import carries the slides'
  published/unlisted state across, so both actions require an `admin` login
  (same tier as Backup / Restore / DB reset). The usual publish rules still
  apply on import: a slide without a catalog ID and title arrives as a draft,
  and a tour is only published when its slide is.
- **WSI-inclusive by design** — a bundle always ships the WSI file(s). There is
  no metadata-only export: without the image file the migrated slide would be
  unopenable on the target.

### What's in a bundle

Bundles are `tar.zst` when the optional `zstandard` package is installed (the
default after `infra/install.sh`), falling back to `tar.gz`. They are written to
`BACKUP_DIR/.staging/` and reclaimed by the same 24 h sweep as restore staging
(as are uploaded bundles awaiting import, and bundles kept for a retry — see
[Retrying a partial import](#retrying-a-partial-import)). Size `BACKUP_DIR` for
an unpacked bundle as well: while a stage exists its WSI files stay there too
(hard-linked into `DATA_DIR` when both share a filesystem, so no extra space is
used; copied otherwise).

| Path inside the bundle | Contents |
|---|---|
| `manifest.json` | Bundle format marker, `app_version`, the integer `schema_version` the version guard keys on, archive format, created-at, slide/tour counts, the exported `slide_public_ids`, note |
| `slides.json` | A portable record of each slide (title, catalog id, MPP, visibility, image credit, metadata attributes) and its tours (with their own metadata) / steps / pins / shapes |
| `wsi/<id>/<file>` | The raw WSI file for each slide |
| `assets/tours/<id>/<file>` | Each tour's uploaded attachments (files still being uploaded are left out; a hand-copied subdirectory, dotfile or symlink is left out with a warning in the log, as in a backup) |

An uploaded archive holding anything else — another top-level file, a nested
directory, a non-file entry — is refused, naming the entry, as changed after
the export; so is a slide folder holding more than its one WSI (the `._*`
files macOS adds when a bundle is unpacked and re-packed there: re-pack with
`COPYFILE_DISABLE=1`, or use the original file). The other refusals each say
what is wrong: a truncated or damaged file, invalid slide data in
`slides.json`, an unsafe entry (a link, a `..` path), a file that is not a
slide-export bundle at all — and only a **whole-system backup** is pointed at
**Restore**.

A bundle may hold at most **101,505 files and folders** with the default
`TOUR_ASSET_MAX_FILES=200` — room for 500 slides (the most one export takes)
that each have a tour with a full 200 attachments, or the same total spread
over more tours. The limit is for the whole bundle: `manifest.json`,
`slides.json`, one WSI per slide and every attachment, plus the folders a copy
unpacked and re-packed with `tar` would add (`wsi/`, `assets/`,
`assets/tours/`, one per slide and one per tour with attachments). It is
5 + 500 × (3 + max(`TOUR_ASSET_MAX_FILES`, 200)), so a larger
`TOUR_ASSET_MAX_FILES` raises it, and `TOUR_ASSET_MAX_FILES=0` (no per-tour
cap) lifts it to the 1,000,000 members any archive is held to. It bounds what
unpacking an upload can create: a crafted file of many tiny entries is refused
once it passes the limit instead of filling staging with them.

- **Export** counts a selection against this limit before writing anything and
  refuses one over it (*"The selected slides would make a bundle of N files and
  folders … more than the M an import accepts"*), so whatever a system exports
  it can also import. Export such a selection in smaller batches.
- **Import** refuses a bundle over the target's limit while unpacking it, with
  a message naming the limit (*"This bundle holds more than M files and
  folders"*), and keeps nothing of
  it. A bundle from a source with a higher `TOUR_ASSET_MAX_FILES` can hit this;
  export its slides in smaller batches.

Thumbnails and the macro / label images are **not** stored in the bundle — they
are **regenerated from the WSI file on import**, so the target always gets
artefacts consistent with the transferred image.

### Version policy

Like Backup, each bundle records both the `app_version` string and the integer
`schema_version`. Import keys on the **`schema_version`**, which is the precise
compatibility signal: a bundle is accepted whenever its `schema_version` lies
between `MIN_BUNDLE_SCHEMA_VERSION` (currently 5) and the target's own schema,
**even if the two installs run different `app_version`s**, so a newer release
imports an older release's bundles. A bundle from a **newer** schema
than the target, or one older than that minimum, is refused with
`blocking_code=VERSION_MISMATCH`: upgrade the target first, or (for a bundle
below the minimum) the source, and export again.

`schema_version` is **required**. A bundle that omits it — or states a
non-numeric value — is refused with the same `VERSION_MISMATCH` code rather than
falling back to an `app_version` string comparison: every bundle this app writes
carries the key, so an absent one means the file was not produced by Tessera's
slide export, and a string match alone would let a bundle of unknown field
layout through.

Restore accepts an archive of any older supported schema (down to the
baseline) and migrates it forward on the next boot.

Bundle compatibility therefore **follows `schema_version`**: any release that
changes the schema — a numbered migration — bumps it, and from then on its bundles are
refused by older builds, even where the slide fields are unchanged (it still
imports theirs while their schema is within its range). Upgrade the target
first when moving slides from a newer release.
The flip side: a release that adds a field to the bundle must come with a
schema bump or a new `bundle_format`, or an
older build would import its bundles and silently drop the new field. As a
backstop, the importer names any `slides.json` key it does not recognise —
in the preflight and as an import warning — and reports an image-credit field
whose value it does not accept (an unknown license id, say), which is
imported empty, instead of dropping either silently.

The guard lives in `slide_transfer._import_version_block` and is applied by both
the preflight and the apply step, so the two can never disagree.

### Migration procedure

On the **source** system:

1. In `/admin/dashboard` → slide list, tick the slide(s) to move and use
   **Export selected** in the bulk action bar (each slide goes with its tours).
2. Wait for the bundle to build, then save the downloaded `.tar.zst` file. A
   selection whose bundle would pass the file limit (see
   [What's in a bundle](#whats-in-a-bundle)) is refused before anything is
   built; export it in smaller batches.

On the **target** system:

3. Open the **Import / Export** tab in the dashboard sidebar (admin only) and
   upload the bundle under **Import slides**. Once the upload completes the
   server copies it into staging, unpacks and checks it in the background
   (*Unpacking and checking the bundle…*) — a large bundle can take many
   minutes, well past the proxy's response timeout, so none of this runs
   inside the upload request. Unpacking stops at this system's file limit
   (see [What's in a bundle](#whats-in-a-bundle)): a bundle holding more is
   refused with a message naming the limit, and nothing of it is kept. You
   can switch tabs meanwhile — even before the
   server has answered the upload, which the tab then keeps showing — or
   reload once it has answered: the tab picks the job up again, and an
   unpacked bundle that has not been imported yet is offered under **Review
   bundle** (or **Discard bundle**) instead of being left on disk unseen until
   the 24 h sweep. Only while the bundle itself is still being sent does
   leaving the tab (another tab, a link, **Logout**, the browser's Back)
   cancel the upload, so the dashboard asks first; a reload or a closed tab
   is asked about until the server has answered the upload.
4. Review the preflight: a version-mismatch banner blocks the import; otherwise a
   per-slide table lists what will be added; for a slide whose original
   filename already exists on the target, the *On import* column offers a
   selector instead of **New**: choose **Import as copy** (default — the file is stored under a
   `-imported-…` name so both coexist) or **Skip**. A slide flagged **already
   on this system** (a slide here carries its public id — typically this
   bundle was imported before) defaults to **Skip**; **Import as copy** would
   add a second copy under a new public id. Existing slides are never
   overwritten. The table also flags a slide whose tour steps hold values the
   step editor would refuse (a non-finite coordinate, a field of view of zero or
   less, a multi-line label, more pins, shapes or freehand points than a step
   may hold, …), a tour with more steps than the editor allows (100) or more
   freehand points across its steps (300,000), or a
   title, catalog ID or tour title / description the editors would refuse
   (control characters, over the length limit — a tour description over 8000
   characters) — applying fails that slide, leaving the rest of the batch to
   import — and counts attachments that exceed **this** system's tour-asset
   limits (`TOUR_ASSET_MAX_BYTES` / `_MAX_FILES` / `_MAX_TOTAL_BYTES`, which
   may be tighter than the source's).
   It also names image-credit values this system will import empty, bundle
   fields it does not recognise (see [Version policy](#version-policy)), and —
   when `BACKUP_DIR` and `DATA_DIR` are on different filesystems, so the WSI
   files are copied rather than hard-linked — a data volume without room for
   them and the [free-space reserve](#free-space-checks) (the bytes needed,
   free and reserved are shown; each slide that does not fit then fails with
   "not enough free disk space" before anything of it is written).
5. Click **Import** and confirm the result summary (imported / skipped / failed).
   Below it, a warning list names everything an imported slide did not carry
   over: each metadata attribute or image-credit field that was refused, each
   bundle field this release does not know, each attachment that was refused
   (with the reason), and each step image reference that does not resolve on
   this system afterwards — a refused
   attachment, or an image of a tour that belongs to a skipped or failed slide.
   A reference into a skipped slide is left pointing at the tour id it had, so
   when the target already holds that tour and image (the usual reason for the
   skip) it keeps working.
6. If something is left to retry, see [Retrying a partial import](#retrying-a-partial-import)
   below — there is no need to upload the bundle again.
7. Open the imported slide and its tour in the viewer to confirm the image,
   steps, pins, shapes, and any embedded tour images render.

Uploading a slide-export bundle to the whole-DB **Restore** is refused with a
message pointing you to **Slide import** instead.

### Slides refused for their pyramid layout

Import holds every WSI to the same layout check as an upload (see *Supported
WSI Formats* in `docs/architecture.md`): a tiled TIFF with no pyramid larger
than about 8000 × 8000, a pyramid whose smallest level is still over 64 Mpx,
levels more than 16× apart, or an OME-TIFF the OME reader refuses. A slide
on the source that would fail that check (for example one copied into
`wsi/` by hand) still opens there — its
low-zoom tiles are read in bounded blocks, slowly — but it is not imported:

- The result summary lists it under **failed** with the same message the
  upload form shows (*"This slide has no image pyramid …"* and the like), and
  its tours are not imported with it. Step images of other tours that point
  into it are reported as unresolved.
- The bundle is kept, like any failed slide, but **Retry failed slides** fails
  it again: the file in the bundle is what is refused. Discard the bundle, or
  let the 24 h sweep reclaim it.

To move such a slide, convert its WSI on the source to a pyramidal file —
`vips tiffsave in.tif out.tif --tile --pyramid --compression jpeg` (a
pyramidal tiled TIFF), or a pyramidal OME-TIFF — and use **Replace WSI file** on the
slide there. The conversion keeps the base image's size, so the slide's tours
still line up, and the replaced slide now serves fast on the source too.
Then export it again.

This is deliberate rather than an oversight. The check caps the CPU one
low-zoom tile can cost: on a large file with no pyramid a single tile reads
the whole base image (tens of seconds of CPU), and accepting it on import
would let a bundle bring in what the upload form refuses, onto a target that
may be a smaller server than the source.

### Retrying a partial import

An uploaded bundle is unpacked into `BACKUP_DIR/.staging/import-<id>/` (the
"stage"). After an import the stage is **kept** whenever a retry could still do
something:

- **a slide failed** (whatever the reason — a transient disk or database
  problem, an unreadable file, a filename taken by a concurrent upload, …;
  the result names it: an unreadable WSI, a failed thumbnail, a full disk, or
  the underlying error), or
- **an attachment was refused by one of this system's own settings**: larger
  than `TOUR_ASSET_MAX_BYTES`, over the tour's `TOUR_ASSET_MAX_FILES` /
  `TOUR_ASSET_MAX_TOTAL_BYTES`, or a type outside
  `TOUR_ASSET_ALLOWED_MIME_PREFIXES` (image-only by default — a PDF from a
  source that allowed PDFs lands here).

Attachments refused for a defect of the file itself — an invalid name, content
that is not an allowed file type, or an extension that contradicts the content
(a PNG named `.html`) — are reported but do **not** keep the stage: a retry would
refuse them again. When nothing is left to retry the stage is deleted straight
away.

The result panel then says the bundle is kept and offers:

- **Retry failed slides** — runs the import again on the kept bundle. Slides
  imported by an earlier attempt are left alone (each one is recognised by the
  slide it created, so nothing is imported twice); slides you chose to **Skip**
  stay skipped; only the failed — and any never-attempted — slides are
  imported. Fix the cause first (free disk space, …).
- **Retry skipped attachments** — copies the refused attachments into the tours
  the import created, through the same checks and per-tour limits as an upload
  (the tour's existing files count). Raise the setting first — the
  `TOUR_ASSET_*` values are read at start-up, so restart the service after
  changing them — or delete unused attachments in that tour to free its quota.
  Only attachments recorded as refused are retried; one an author deleted after
  the import is not brought back. nginx does not cache a 404 for `/assets/`,
  so an image restored by the retry shows on the viewer's next request.
- **Discard bundle** — deletes the kept stage now to free the disk space.

A kept stage is deleted by the staging sweep (the one that reclaims abandoned
uploads) once **24 hours have passed since the last import attempt** on it.
The sweep is not a timer: it runs when the service starts, whenever an
uploaded slide-import bundle or a restore archive (uploaded or a saved
backup) is unpacked, and when a reset is staged, so on a quiet
server a stage (or an export bundle) can stay on disk longer. Within that
window the **Import / Export** tab offers it again after a reload or a tab switch
(**Review bundle**), showing the preflight with each slide's outcome so far:
**Already imported** (not re-importable from this bundle while that slide
exists), *Failed last time* with the reason, or *Skipped last time*. A slide whose
imported copy was deleted in the meantime counts as not imported and can be
imported again from the kept bundle.

The per-slide outcomes are recorded in `apply-state.json` inside the stage,
written before each slide is committed, so an import interrupted by a crash or a
restart can simply be retried: a slide whose import had already completed is
recognised as imported, and the leftovers of one that was cut short are cleaned
up before it is imported again. If that record cannot be written (e.g. the
backup volume is full) the import stops with an error instead of carrying on
unrecorded; free up space and retry. A bundle that itself contains a file named
`apply-state.json` is refused at upload.

---

## Backup server and server moves — `infra/sync-from.sh`

`infra/sync-from.sh` copies one Tessera server's content onto another. It
serves two purposes:

- **A backup server** students can be sent to when the main server is down.
  It runs all the time at its own address (say its intranet IP), and after you
  add or revise content on the main server you bring it up to date with one
  command.
- **Moving an install to a new machine** (`--all`): the new machine's
  database becomes an exact copy of the old one's, settings and statistics
  included.

It is run **by hand, on the server that receives the copy**, from that
server's clone of the repository. It pulls from the source over ssh; nothing
is installed or changed on the source. Only what changed since the last run
is transferred, and an interrupted transfer resumes where it stopped. The
receiving server's services are stopped only for the final swap, typically a
few seconds.

```bash
sudo bash infra/sync-from.sh --dry-run admin@main-server   # what would change
sudo bash infra/sync-from.sh admin@main-server             # bring the backup up to date
```

### What a sync copies

| Taken from the main server | Kept as the backup server has it |
|---|---|
| Slides, their metadata and attributes, tours, steps, pins, shapes, tour attachments | Network settings: IP filter, mDNS name, Google Analytics tag, analytics mode |
| WSI files and thumbnails | Sign-in settings (**Network → Authentication** in the dashboard) and the Basic-auth accounts |
| Public-page settings: site title, logo, favicon, header images, seasonal backgrounds, metadata choices | Access statistics, activity history, error log, performance history |
| Admin dashboard accounts (`admin` / `manager` / `instructor`, with their passwords) and role permissions | `/etc/wsi/wsi.env` (secrets, `WSI_PUBLIC_BASE_URL`, tuning) — except `WSI_MFA_KEY`, taken from the main server when admin accounts with two-factor sign-in come over |

So on the backup server:

- **Students sign in however the backup is configured**, not like on the main
  server. A fresh install has no sign-in at all; set one on the backup's own
  dashboard if you want one. **Google sign-in cannot be used at a bare IP
  address** (Google accepts only registered host names as redirect targets),
  so a backup reached by its IP uses no sign-in, Basic authentication or LDAP.
- **Admins sign in with their main-server passwords.** The admin account the
  backup's own install created is replaced by the main server's admin
  accounts on the first sync.
- **Each sync signs everyone out of the backup**, admins and (if the backup
  has a sign-in) students alike, as a restore does. The backup runs the app's
  own post-restore sign-out, so a sign-out, password change or disabled
  account on either server can never be undone by the copy.
- **The backup's own records name slides and accounts by number.** On the
  first sync onto a backup that already had content of its own, a number can
  come to mean another slide or admin. The statistics and activity history
  recorded under it would then be shown for that one. The summary says when
  this would happen; a fresh install has no such records.
- **Do not edit content on the backup**: the next sync replaces it with the
  main server's. The backup's own settings in the right-hand column are never
  touched, so set them once.
- Statistics recorded on the backup (say, during an outage of the main
  server) stay on the backup. They are never copied back.

The tile caches need no clearing after a sync: a slide whose WSI was replaced
on the main server arrives with its new tile generation, so viewers fetch
fresh tiles (see [Publish state, deletion, replacement, and cache
staleness](#publish-state-deletion-replacement-and-cache-staleness)).

### One-time setup

1. **Install Tessera on the backup server** ([QUICKSTART](../QUICKSTART_FOR_UBUNTU.md)),
   **at the main server's release**. A standby sync needs both on the same
   schema version, and refuses a main server that runs a newer release than
   the backup. From then on, **update the backup first**
   (`sudo bash infra/update.sh` on each, backup then main), or pin both to one
   tag with `GIT_REF=vX.Y.Z`.
2. **Let the backup's admin reach the main server over ssh.** The script runs
   ssh as the user who ran `sudo` (override with `SSH_AS_USER=`), with that
   user's keys and `~/.ssh/config`. Connect once by hand (`ssh admin@main-server`)
   to accept the host key. A password or key passphrase is asked for once per
   run (one connection carries everything). Put a non-standard port or an IPv6
   address in a `~/.ssh/config` `Host` entry and pass that alias.
3. **Allow that ssh user to act as the service user on the main server**,
   without a password and for nothing else. On the main server:

   ```bash
   sudo visudo -f /etc/sudoers.d/wsi-sync
   ```
   ```text
   admin ALL=(wsi) NOPASSWD: /usr/bin/rsync, /usr/bin/python3
   ```

   (`admin` = the ssh user.) Everything the script runs on the main server
   runs as `wsi`, apart from one optional read of `/etc/wsi/wsi.env` as root
   (with `--all`, or when admin accounts with two-factor sign-in come over;
   see below): it takes a consistent snapshot of the
   database (SQLite's online backup, safe while the main server is running),
   lists what the snapshot refers to, and serves the files to rsync. The
   helper that does this travels over ssh on stdin, so the main server needs
   nothing beyond Tessera itself. The script only reads, but the rule itself
   is broader: `python3` as `wsi` runs any code as the service user, which
   **owns** Tessera's data — the database with the staff password hashes and
   auth secrets, the WSI files — and can change or delete it as the app can.
   Grant the rule only to an ssh account you trust as much as an admin, and
   remove it (`/etc/sudoers.d/wsi-sync`) once you no longer sync.
4. **First sync.** Run `--dry-run` to see the size, then the real sync. The
   first run transfers every WSI file (tens of GB take a while even on a fast
   LAN); later runs only what was added or changed. `--bwlimit=20M` caps the
   rate if the transfer must share the network during the day.

### Running a sync

After adding or revising content on the main server:

```bash
cd ~/wsi                                   # the backup server's clone
sudo bash infra/sync-from.sh admin@main-server
```

It prints a summary (slides before and after, new, removed, replaced) and
asks before stopping the services to apply; `--yes` skips the question (and is
required when the script runs without a terminal, e.g. from cron). Run
it while nobody is editing on the main server. If a slide's files, a tour or
a tour's attachments change while they are being transferred, it transfers
again (up to three rounds); if they keep changing, it stops without changing
anything.

What happens, in order:

1. The mirror directory (below) and the ssh connection are set up, and the
   main server is checked for the `sudo` rule.
2. Both servers' versions are compared; a mismatch stops here with what to
   update.
3. The main server's database is snapshotted and copied here, and the files
   it refers to are rsynced into the mirror. Every WSI file, thumbnail and
   attachment the main server has must have arrived (a file it cannot read
   stops the sync here, naming it). Nothing on the backup has changed yet.
   `--dry-run` copies only the database snapshot: it reports how much the
   transfer would move and the summary, then stops without transferring
   files.
4. `wsi-app` / `wsi-sync` are stopped. The current database is kept as
   `wsi.db.pre-sync`, the files are put in place, and the content tables are
   swapped **in one transaction** (with `--all`, the whole database is copied
   in instead). Then everyone is signed out. If the swap fails, the database
   stays as it was. The files may already be in place, though, so every slide
   also moves to a fresh tile generation (the tile cache cannot pair the old
   database with a replaced image). The services are started again either
   way — unless the sign-out after a swap cannot be done: then they stay
   stopped (see *Troubleshooting*), since they would honour logins revoked
   since.
5. The services start and `wsi-app` is health-checked.

A file or attachment that the backup's previous database still refers to (a
slide, or an image a tour step linked to, just deleted on the main server)
stays until the next sync, so a rollback (or a failed swap) still finds it.
Interrupted transfers resume: rsync keeps what arrived and the next run goes
on from there.

Only one sync or rollback runs at a time (lock: `/run/wsi-sync.lock`). Neither runs while this server has
work of its own pending that wsi-app's next start would do over the swap: a
sign-out from an earlier swap, a restore staged in its dashboard, or an
interrupted restore (see *Troubleshooting*). A sync that fails keeps the
previous `wsi.db.pre-sync`: the copy becomes the rollback point only once the
swap has landed.

### When the main server is down

There is nothing to do on the backup: it is already running with the content
of the last sync. Give students its address.

### Rolling back the last sync

```bash
sudo bash infra/sync-from.sh --rollback
```

This command puts back what the last sync changed, signs everyone out, and
restarts. It takes no source host and no option but `--yes`, and undoes one
sync only: the copy it restores from is used up. After a standby sync that is
the content (from `wsi.db.pre-sync`): the backup's own settings, Basic-auth
accounts, statistics and logs recorded since stay. After a `--all` it is the whole database, and
`/etc/wsi/wsi.env` too; after a standby sync that carried the source's
`WSI_MFA_KEY`, the previous key line goes back as well. Where there is no record of which kind the last sync
was (`pre-sync.mode`, see *Where things are kept*), the whole database goes
back, and the script says so before asking. It needs no connection to the
main server, and it refuses while this server has work of its own pending,
like a sync (see *Running a sync*). The
database it replaced is kept as `wsi.db.pre-rollback`. A slide whose WSI was
replaced on the main server **under the same file name** keeps the new image
(the old file is gone). Every slide moves to a fresh tile generation, so the
tile cache never serves the two images mixed.

### Moving to a new server — `--all`

`--all` makes the receiving server's database an exact copy of the source's:
every table, settings, sign-in configuration, statistics and history
included. It also writes the source's `ANALYTICS_HASH_SALT` and
`WSI_MFA_KEY` into the new server's `/etc/wsi/wsi.env`, so hashed-mode
analytics keep recognising returning visitors and accounts with
[two-factor sign-in](#two-factor-sign-in-dashboard) keep working (the file as
it was is kept for `--rollback`). Everyone
signs in again afterwards, as after a restore, so `JWT_SECRET` stays the new
server's own. The new server may run a newer release than the old one; its
first start brings the database forward, as after a restore.

1. Install Tessera on the new machine and do the one-time setup above (steps
   2 and 3, with the old server as the source).
2. Copy the bulk while the old server keeps serving, as often as you like.
   Each run moves only what changed. When both run the same release, use the
   plain (standby) mode for these runs,
   `sudo bash infra/sync-from.sh admin@old-server`: the new machine keeps its
   own network settings meanwhile, so it does not announce the old server's
   mDNS name or send its Google Analytics tag while both are up. `--all`
   works too, and is required when the new machine runs a newer release, but
   then the new machine takes over those settings after every copy.
3. For the final copy, stop the old server's services
   (`sudo systemctl disable --now wsi-app wsi-sync` there — `disable`, so a
   reboot of the old server does not bring them back) and run the same command
   once more. Then point the old host name (DNS) at the new machine: Google
   sign-in only works under the host name registered with Google. The mDNS
   name comes along with the settings, and the new machine announces it from
   its first start, so keep the old server's services stopped. TLS
   certificates and a Cloudflare Tunnel's `cloudflared` live outside Tessera;
   set them up on the new machine as for any new host.

Reading the salt and key needs passwordless **root** sudo on the old server
(the `wsi` rule above is not enough). Without it the script says so and
carries on. Copy them yourself afterwards: on the old server
`sudo grep -E '^(ANALYTICS_HASH_SALT|WSI_MFA_KEY)=' /etc/wsi/wsi.env`, replace
the lines the new server's installer generated in its `/etc/wsi/wsi.env` with
them (`sudoedit /etc/wsi/wsi.env`; a second line below the old one also
works, since the last one wins), and run
`sudo systemctl restart wsi-app wsi-sync`.

The plain (standby) mode copies the admin accounts too, and keeps the
standby's own `wsi.env` — except that when any of those accounts uses
[two-factor sign-in](#two-factor-sign-in-dashboard), it also writes the
source's `WSI_MFA_KEY` there (only that key — `--dry-run` says so, and
`--rollback` puts this server's previous key back, leaving the rest of the
file as it is), since their secrets open only under it. That read needs the
same passwordless root sudo; without it the script says so, and you copy
the key by hand as above, or those accounts cannot sign in on the standby.

### Where things are kept

| Path (receiving server) | What |
|---|---|
| `/var/lib/wsi-replica/mirror/` | The mirror: the source's WSI files, thumbnails and tour attachments as of the last sync. rsync compares against it, which is what makes later runs incremental. |
| `/var/lib/wsi/wsi.db.pre-sync` | The database as it was before the last sync, for `--rollback`. |
| `/var/lib/wsi/wsi.db.pre-rollback` | The database a `--rollback` replaced. |
| `/var/lib/wsi-replica/wsi.env.pre-sync` | After a `--all`, or a standby sync that carried over `WSI_MFA_KEY`: `/etc/wsi/wsi.env` as it was, for `--rollback` (mode 0600). |
| `/var/lib/wsi-replica/pre-sync.mode` | Which kind of sync (`content` = standby, or `all`) made `wsi.db.pre-sync`, so `--rollback` knows whether to put back only the content or the whole database. |

The files in `DATA_DIR` are **hard links** to the mirror's, so the WSI files
take their space once, not twice. That needs the mirror on the same
filesystem (and mount) as `/var/lib/wsi`; the script tries a link to find
out. If `DATA_DIR` is a separate mount, set
`WSI_REPLICA_DIR=` to a directory on that mount (its parent must be owned by
root and writable by nobody else; the mirror must also be outside
`DATA_DIR`, see below — so when `/var/lib/wsi` is itself the mount point,
linking is impossible). Without a link the script warns and copies the
files, which needs their space twice (it checks that space before it stops
anything). The new or changed files are also copied while the services are
stopped, so the downtime lasts as long as that copy. The mirror must be outside `DATA_DIR`:
root writes it, and root never writes below `DATA_DIR`, where the service
user could plant symlinks. Every change below `DATA_DIR` is made as `wsi`.

The other options are environment variables: `WSI_SOURCE_DB`,
`WSI_SOURCE_DATA_DIR` and `WSI_SOURCE_ENV_FILE` for a source that is not at
the default paths, and `WSI_HEALTH_TIMEOUT` (seconds, default 30). See the
script's header (`sudo bash infra/sync-from.sh --help`).

### Troubleshooting

| Message | Fix |
|---|---|
| `this ssh user may not run commands as wsi without a password` | Add the `sudo` rule of setup step 3 on the source. |
| `the source's schema_version (N) is newer than this server's` / `the source runs vX, newer than this server's` | Update this server (`sudo bash infra/update.sh`, `GIT_REF=` the source's tag). |
| `...is older than this server's ... A standby sync needs both on the same schema` | Update the source to this server's release. This server cannot move back to the source's older release: the older release refuses to start on a database a newer one has opened, so `update.sh` rolls back. |
| `The source kept changing over 3 rounds` | Someone is uploading or replacing slides on the source. Run again when they are done. |
| `... files are COPIED into place` (warning) | See *Where things are kept*. |
| `wsi-app did not come up` | `sudo journalctl -u wsi-app -n 80`, then `sudo bash infra/sync-from.sh --rollback`. If the message says wsi-app refuses to start until the sign-out handed to it succeeds, fix that cause first (the database must be writable) and start it: `--rollback` refuses while that sign-out is pending. After a failed `--rollback`, the database it replaced is in `wsi.db.pre-rollback`; put it back from a shell. |
| `N file(s) the source has did not arrive intact` | Each file is compared by size and modification time with the main server's. The main server's `wsi` user cannot read them (check their owner and mode there), or the network dropped. Fix it and run again. |
| `a restore is staged on this server` / `an interrupted restore is recorded on this server` | The backup's own dashboard has a restore waiting to be applied, or one was cut short. The dashboard cannot cancel a restore at that point: let wsi-app finish or reconcile it (start or restart it — a waiting request over 10 minutes old is dropped rather than applied — then check the dashboard), then sync. |
| `Could not sign out the sessions from before the swap ... stay STOPPED` | The database could not be updated (disk full, read-only). Fix it, then `--rollback`, or start the services. |
| `...session-revocation-pending says an earlier swap's sign-out is still pending` | A sign-out was handed to the app's next start. Start the services so it finishes (`sudo systemctl start wsi-app wsi-sync`; if they do not come up, `sudo journalctl -u wsi-app` says why), then run the sync again. |

---

## User management

The bootstrap admin (seeded from `ADMIN_BOOTSTRAP_USERNAME` /
`ADMIN_BOOTSTRAP_PASSWORD` on first startup) is the only account that
exists out of the box. After the first login, additional users with
any of the three roles (`admin` / `manager` / `instructor`) are
created from the admin dashboard. (These are staff dashboard accounts; the
visitor logins of the `basic` app-access mode are a separate list, under
[Authentication modes](#authentication-modes).)

1. Open the **Account** page at `/admin/account` (reachable via the
   **Account** button in the admin dashboard header). The user
   management section is only visible to `admin` accounts — `manager`
   and `instructor` logins see the username/password panels and the
   **Two-factor authentication** panel.
2. Under **Create user**, enter a username, password, and role, then
   click **Create user**. The password is validated by the same
   [password policy](#password-policy); the role choice is fixed at
   creation and cannot be changed afterwards (delete and re-create the
   user to change it). A username that differs from an existing one only
   in letter case is refused.
3. To revoke access while keeping the account, click **Disable** on its
   row: the user is refused at login and every token they already hold
   is rejected immediately. **Enable** restores the account (they sign
   in again).
4. To remove a user, click **Delete** on the corresponding row.

The same section also exposes a **Role permissions** panel with four
toggles backed by the `RolePermissions` singleton:

- `manager_can_view_analytics`
- `manager_can_view_server`
- `instructor_can_view_analytics`
- `instructor_can_view_server`

All four default to OFF. Toggling one ON grants the matching role
read-only access to the Analytics or Server Info pages (both API and UI);
toggling OFF revokes it immediately (the next API call returns HTTP
403 and the dashboard link disappears).

Two safety rules are enforced server-side regardless of UI state:

- **A user cannot delete or disable themselves.** `DELETE
  /api/admin/users/{id}` and `PATCH /api/admin/users/{id}` with
  `{"is_active": false}` reject the request with HTTP 400 when the
  target equals the actor.
- **The last active admin cannot be deleted or disabled.** Deleting or
  disabling the only remaining `role == "admin" AND is_active` user
  fails with HTTP 400 ("Cannot delete the last admin" / "Cannot disable
  the last admin"). Create a second admin first if you intend to retire
  the original one.

Equivalent HTTP calls (for scripting). `<admin_token>` is the
`access_token` that `POST /api/admin/login` returns for an admin account
(valid for `JWT_TTL_SECONDS`, 24 h by default). With an
[app-access mode](#authentication-modes) on, the sign-in call must first
pass that gate like a browser: add `-u <user>:<password>` under `basic`, or
under `ldap` sign in with `curl -c jar -d username=… -d password=…
http://<host>/api/auth/login` and send `-b jar` along; under `google_oauth`
script from a host the [direct LAN](#skip-authentication-for-direct-lan-access-dashboard-setting)
exemption covers. Calls carrying `<admin_token>` need nothing more:

```bash
# Sign in: prints {"access_token":"<admin_token>"}, or, for an account with
# two-factor sign-in on, {"mfa_required":true,"mfa_token":"<mfa_token>"}
curl -X POST -H 'Content-Type: application/json' \
    -d '{"username":"admin","password":"<password>"}' \
    http://<host>/api/admin/login

# Two-factor accounts only: trade the mfa_token (valid 5 minutes) and a
# current authenticator code for {"access_token":"<admin_token>"}
curl -X POST -H 'Content-Type: application/json' \
    -d '{"mfa_token":"<mfa_token>","code":"123456"}' \
    http://<host>/api/admin/login/mfa

# List users (admin only)
curl -H "Authorization: Bearer <admin_token>" \
    http://<host>/api/admin/users

# Create a manager
curl -X POST -H "Authorization: Bearer <admin_token>" \
    -H 'Content-Type: application/json' \
    -d '{"username":"alice","password":"<strong-pw>","role":"manager"}' \
    http://<host>/api/admin/users

# Disable (suspend) a user; {"is_active": true} re-enables it
curl -X PATCH -H "Authorization: Bearer <admin_token>" \
    -H 'Content-Type: application/json' \
    -d '{"is_active": false}' \
    http://<host>/api/admin/users/<id>

# Toggle a role-permission flag
curl -X PATCH -H "Authorization: Bearer <admin_token>" \
    -H 'Content-Type: application/json' \
    -d '{"manager_can_view_analytics": true}' \
    http://<host>/api/admin/role-permissions
```

---

## Two-factor sign-in (dashboard)

Any staff account — admin, manager or instructor — can add a second factor
to its dashboard sign-in: a 6-digit code from an authenticator app (Google
Authenticator, Microsoft Authenticator, 1Password, …; the standard TOTP
scheme, 30-second codes). It is **off for every account by default**, and
only the account's owner can turn it on; nobody can switch it on for someone
else. Once it is on, `/admin` asks for the code after the password. Accounts
that have not turned it on sign in with the password alone.

This is the staff login only; it comes on top of the app-access gate
([App-access sign-in](#app-access-sign-in)), which — when a mode is on —
staff pass first, like every visitor. Cloudflare Access, when in front,
applies its own policy independently.

### Turning it on and off

On the **Account** page (`/admin/account`), the **Two-factor authentication**
panel:

1. **Turn on**: enter the current password and click **Turn on**. A QR code
   appears (with the key in text, for an app that cannot scan). Scan it, type
   the 6-digit code the app shows and click **Confirm and turn on**. Nothing
   changes on the account until that first code is accepted, and the QR code
   expires after `WSI_MFA_SETUP_TTL_SECONDS` (600 s).
2. Ten **recovery codes** (`XXXXX-XXXXX`) are then shown, **once**. Each can
   stand in for an authenticator code a single time — at sign-in, choose
   **Use a recovery code instead**. Copy or download them and keep them
   somewhere safe.
3. Turning it on signs the account out everywhere else (its other dashboard
   sessions were opened without the factor it now has).

While it is on, the same panel turns it **off**, or issues **new recovery
codes** (the old ones stop working, and the account's other sessions end —
one may have been opened with a leaked code), with the password plus a current code
or one recovery code; wrong codes here count towards the account's
[login lockout](#login-throttles) like at sign-in. A sign-in with a
recovery code warns when three or fewer are left. After signing in with the
*last* one, that session can issue new codes (or turn two-factor off) with
the password alone — leave the code empty — since there is no code left to
give.

The code step shares the account's [login lockout](#login-throttles) with
the password: a correct password does not reset the failure count for a
two-factor account (it is just not counted itself), only a correct code
does, so knowing the password buys no unlimited code guesses. A code is
accepted once only (a code seen over someone's shoulder cannot be replayed),
the password step's result is good for one sign-in, and the server's clock must be right
— codes from one step either side are accepted for drift, so keep NTP on.

### When someone loses their device

In order of preference:

1. **Their own recovery code** — sign in with one, then turn two-factor off
   or re-enrol the new phone from the Account page.
2. **Another admin resets it** — in the **Users** list (Account page, admin
   only) an account with two-factor on shows a **2FA** badge and a **Reset
   2FA** button. The acting admin confirms with their *own* password. The
   member's second factor and recovery codes are deleted and their open
   sessions end; they sign in with the password alone and may enrol again.
   An admin can reset another admin, but not themselves (their own goes
   through the Account panel, which wants a code). That is a guard against
   the shortcut, not a security boundary: whoever holds an admin's dashboard
   session controls the install, accounts included — sign out on shared
   machines.
3. **From the server**, when no admin can sign in (the only admin lost both
   the device and the recovery codes, or `WSI_MFA_KEY` was lost):

   ```bash
   cd ~/wsi    # your clone of the repository, the one update.sh runs from
   sudo python3 -I services/app/tools/reset_mfa.py <username>
   # another database: add --db /path/to/wsi.db
   ```

   Run it from your own clone, not from `/opt/wsi`: the service user owns
   the deployed tree, and root must not run code it could have rewritten —
   the tool refuses to (`-I` likewise keeps Python from importing anything
   placed beside the script). It does what **Reset 2FA** does, needs only
   the standard library, and may run while the services are up. It finds
   the database the service uses from `DATABASE_URL` in `/etc/wsi/wsi.env`
   (root-only, hence `sudo`), prints the path, refuses to guess when that
   value or file cannot be read, and switches to the database file's owner
   before opening it, so the SQLite `-wal` / `-shm` files keep their owner.

Every change is in the [audit history](#audit-history--adminhistory):
`admin.mfa.enable`, `admin.mfa.disable`, `admin.mfa.recovery_codes_regenerate`,
`admin.mfa.reset` (under **System**, with `via: cli`, from the server tool), a wrong code on the
account panel as `admin.mfa.code_failure`, a correct password at sign-in as
`admin.login.mfa_required`, and a wrong code after it as
`admin.login.mfa_failure`. Both of the last two mean the password was right:
when the account's owner was not signing in, treat it as a leaked password
and change it. A successful two-factor sign-in is
`admin.login.success` with `mfa: "totp"` or `"recovery"`.

### `WSI_MFA_KEY`

The authenticator secrets and recovery codes are stored sealed (AES-256-GCM)
under a key derived from `WSI_MFA_KEY` in `/etc/wsi/wsi.env`, so the database
alone — a backup, a copied file — does not give away anyone's second factor.
`install.sh` generates it (`openssl rand -hex 32`) on a fresh install and
appends one to an existing `wsi.env` that has none, so an update brings it
along; an existing value — however it is written, on any line, or set on the
`wsi-app` unit through a drop-in's `Environment=` — is never replaced.
(`sync-from.sh` reads the key from the source's `wsi.env` only: a key kept
in a drop-in has to be copied to the receiving server by hand.) A `wsi.env`
that is a symlink is not rewritten: `install.sh` warns instead, and you add
the key to the file it points to. It must be at least 32 bytes.

- **Without it** the Account panel says two-factor is unavailable, and an
  account that already has two-factor on cannot finish signing in (HTTP 503,
  "cannot be checked on this server") — restore the key, or reset that
  account's two-factor.
- **Back it up with the database.** A backup restored on another host, or a
  reinstall, needs the *same* `WSI_MFA_KEY` in that host's `wsi.env`;
  otherwise every enrolled account hits the 503 above until its two-factor is
  reset. `sync-from.sh` carries the key itself when it can read the source's
  `wsi.env` ([details](#moving-to-a-new-server----all)). Changing the key has the
  same effect, so there is no "rotation" short of resetting everyone.
- `WSI_MFA_ISSUER` (default `Tessera`) is the name the authenticator app
  shows for the account — set it per install when people have accounts on
  several. `WSI_MFA_LOGIN_TTL_SECONDS` (300) is how long the password step's
  result waits for the code.

---

## App-access sign-in

These apply to the app-access gate — the login visitors pass, configured
under **Network → Authentication** — whatever the ingress path. The
Cloudflare-specific pieces (Access SSO, the LAN-direct host, break-glass
routes) are in [Cloudflare Access + Tunnel
integration](#cloudflare-access--tunnel-integration).

### Authentication modes

**Network → Authentication → Method** selects one gate for the whole app —
viewer pages, API, tiles, thumbnails (`AuthSettings.mode`):

| Method | Visitors sign in with | Configure |
|---|---|---|
| None (`none`, default) | nothing — the app is open to anyone who can reach it (and passes the [IP access control](#ip-access-control)) | — |
| Basic auth (`basic`) | the browser's HTTP Basic dialog, which every page load raises — the SPA never sends a visitor to `/login` in this mode | the **Users** list on the same panel (`auth_users`; [password policy](#password-policy) applies) |
| LDAP (`ldap`) | the `/login` form; the app binds as the **Bind DN** (anonymous if blank), finds the user under **User search base DN** with the filter, then binds as that user | server URI and base DN are required (the filter defaults to `(uid={username})`); **Test connection** checks the *saved* settings; see [LDAP over TLS](#ldap-over-tls-certificate-verification) |
| Google OAuth (`google_oauth`) | **Sign in with Google** on `/login` | client ID / secret; register `https://<host>/api/auth/oauth/callback` (the panel shows it) for every host name users sign in on; **Allowed email domains** admit only Google Workspace accounts — see the domain / individual-address rules under [Using the app's own login](#using-the-apps-own-login-instead-of-cf-access-sso-seat-limit-workaround) |

- **The dashboard is inside the gate too.** With a mode other than `none`,
  the dashboard sign-in (`/api/admin/login`, and its
  [two-factor](#two-factor-sign-in-dashboard) code step) only answers a
  visitor the gate already admitted — an app session, Basic credentials, a
  Cloudflare Access identity, or the [direct LAN](#skip-authentication-for-direct-lan-access-dashboard-setting)
  / [break-glass](#break-glass-unauthenticated-raw-ip-access) exemptions.
  Staff therefore sign in to the app first (under `ldap` / `google_oauth`,
  opening `/admin` sends a signed-out visitor to `/login`, and back to
  `/admin` afterwards), and then to the dashboard with their staff account.
  Someone who cannot pass the app sign-in cannot even try a staff password;
  a refused attempt does not count towards the account's lockout.
  **Before turning on (or updating to) a gated mode**, make sure every staff
  member can pass it: under `ldap` they need an LDAP account, under
  `google_oauth` a Google address the allow-list admits (add individual
  addresses under **Individually allowed email addresses** for anyone outside the
  allowed domains, e.g. an external collaborator). Someone who cannot pass
  it reaches the dashboard only through the exemptions below.
- Requests carrying a valid staff login token (the dashboard's API calls)
  still pass the gate in every mode, and the app's own sign-in endpoints
  (`/api/auth/*`) and `/api/health` are never gated. So an admin who saves a
  setting that admits nobody keeps their open dashboard and can fix it from
  there; *do not sign out of the dashboard* until the new mode is confirmed
  to work (sign in from a private window to check). Once nobody holds a
  dashboard session, the way back in is
  [direct LAN access](#skip-authentication-for-direct-lan-access-dashboard-setting)
  if it was enabled, [break-glass](#break-glass-unauthenticated-raw-ip-access)
  (`WSI_BREAKGLASS_NOAUTH=1` in `/etc/wsi/wsi.env` and a restart, from the
  server), or Cloudflare Access when it is in front.
- Under `basic` every page load, `/admin` included, raises the Basic dialog
  and needs one of the **Users**. So that this cannot lock you out, a switch to `basic` is
  refused (HTTP 422) until at least one user exists — select Basic, add a
  user in the **Users** list that then appears, and save — and while the
  mode is `basic` the last user cannot be deleted (add another or switch the
  mode first).
- A switch to a mode that cannot admit anyone yet — or a save that leaves a
  working `ldap` / `google_oauth` setup unable to — is refused (HTTP 422):
  `basic` with no user, `ldap` without the server URI and user base DN,
  `google_oauth` without the client ID, secret and an allowed domain or
  address — fill them in and switch in the same save. A database already in
  such a state (e.g. a restored one) is only warned about after a save, so
  it can still be edited and fixed. A restore
  reloads the dashboard page, so with no Basic user whose password you know
  (none at all, or another install's), reach `/admin` through
  [direct LAN access](#skip-authentication-for-direct-lan-access-dashboard-setting)
  if it was enabled, or [break-glass](#break-glass-unauthenticated-raw-ip-access),
  and add one there.
- A successful login sets the `wsi_session` cookie for **Session TTL**
  (default 86400 s, range 60 s – 30 days). Lowering it also ends existing
  sessions older than the new value; an individual LDAP / Google session
  cannot otherwise be revoked (see [Signing every visitor
  out](#signing-every-visitor-out)).
- Basic and LDAP passwords travel as sent: over plain HTTP they cross the
  network readable. Use them behind TLS (or the Cloudflare Tunnel).
- A saved change takes effect on every worker and `wsi-sync` within
  `WSI_SETTINGS_REFRESH_SECONDS`; no restart.

### Customizing the login screen text

**Network → Authentication** exposes two optional, free-text overrides for
the app-access login screen, surfaced (unauthenticated) via `/api/auth/state`:

* **Message** — replaces the default per-mode hint. The Google OAuth default
  ("…sign in with your Google account.") is misleading for an organization that
  fronts a custom Google Workspace domain; set neutral wording here instead.
* **Button label** — replaces the default button text (e.g. "Sign in with
  Google"); a plain "Sign in" reads as more generic.

Both fall back to the built-in localized defaults when left empty. The button
exists only on the LDAP and Google OAuth screens, and under `basic` visitors
see the browser's own dialog rather than `/login`, so in practice both matter
for those two modes.

### Signing every visitor out

An individual ldap / Google / CF-carried app-access session cannot be revoked
on its own: it lives for **Session TTL** (see "Revocation is by TTL" under
[Carrying the CF Access identity onto the LAN
host](#carrying-the-cf-access-identity-onto-the-lan-host)).
To end **all** of them at once — a shared PC was left logged in, a login
cookie may have leaked, someone was off-boarded — use **Network →
Authentication → Sign everyone out** (shown while a mode other than `none` is
selected). After a confirmation it sends
`PATCH /api/admin/auth/settings` with only `revoke_app_sessions=1`, which bumps
`AuthSettings.session_generation`: every app-access cookie issued before is
rejected from then on (immediately on the worker that handled the request, and
on the others within `WSI_SETTINGS_REFRESH_SECONDS`, 5 s by default), so each
visitor has to log in again.

- No setting changes, and the dashboard's unsaved edits in the form above stay
  as they are (they are not saved). Like any auth-settings change it is
  audited (`auth.settings.update` with `sessions_revoked: true`); no service
  restart is involved.
- Staff dashboard logins (the admin JWT) are not affected; revoke those per
  account by suspending it or changing its password.
- Open live-sync viewer and index-page sockets admitted on such a cookie are
  closed (code 4401) at their next re-check, within
  `WSI_SYNC_VIEWER_REAUTH_SEC` (60 s) plus the settings refresh; a viewer then
  sees "Could not join the live session" and has to reload and sign in again.
- A switch of the auth mode, a change to the Google allowed domains or allowed
  email addresses and, under `ldap`, a change to the LDAP server / base DN /
  filter already sign everyone out the same way when saved.
- Limits: visitors admitted by Cloudflare Access (Path A) are let back in by
  CF on their next request, and under `basic` a browser that answered the
  HTTP Basic dialog resends its cached credentials and is logged straight back
  in; deleting or re-passwording a basic user is the way to end that user's
  access (the last one, while the mode is `basic`, can only be re-passworded).

### LDAP over TLS: certificate verification

With an `ldaps://` URI or **Use StartTLS** on, the app verifies the directory
server's certificate chain and that it names the host in the URI; a failed
check fails the login (and the dashboard's **Test connection** reports it at the
`connect` / `starttls` stage). The system trust store is used by default. For a
directory whose certificate comes from a private CA, set
`LDAP_CA_CERTS_FILE=/etc/wsi/ldap-ca.pem` in `/etc/wsi/wsi.env` (a PEM bundle
readable by the `wsi` user) and restart the app. A path that does not exist
makes every LDAP login fail rather than falling back to an unverified
connection. A plain `ldap://` URI without StartTLS still sends passwords in
cleartext; use it only on a trusted segment.

### IP access control

**Network → IP Access Control** is a network ACL in front of the sign-in
gate (`NetworkSettings.ip_filter_*`, `main.ip_filter_middleware`):

- When enabled, every request to the app and to `wsi-sync` (live-sync
  WebSockets included) is checked, **staff included**; a refused request gets
  HTTP 403 `Access denied`, a refused socket is closed with 4403 (an open one
  at its next re-check, within `WSI_SYNC_PRESENTER_REAUTH_SEC` /
  `WSI_SYNC_VIEWER_REAUTH_SEC`). Rules (an IPv4 / IPv6 address or CIDR) are tried
  top to bottom and the first match decides; an address no rule matches gets
  **Default for unmatched rules** — *Allow* makes the list a blocklist,
  *Deny* an allowlist.
- The address matched is the visitor's as the app sees it: for Cloudflare
  Tunnel traffic the public IP from `Cf-Connecting-IP`, otherwise the peer
  nginx reports in `X-Real-IP` (both honoured only from `TRUSTED_PROXIES`,
  loopback by default). A LAN client and the same person arriving through the
  Tunnel therefore need separate rules; **Fill current IP** says which one you
  are on.
- Loopback (`127.0.0.1`, `::1`) is always allowed. A save whose rules would
  block your own current address asks first, and goes through only if you
  confirm (the dashboard evaluates the rules exactly as the server does, IPv4
  and IPv6 addresses and CIDRs alike; should the server still find a lockout
  the dashboard missed — say your address changed since the page loaded — it
  asks again with the address the server saw). Declining saves nothing; put
  an allow rule for your address above the rule that blocks it instead.
  If you do get locked out, reach the dashboard from the server itself (e.g.
  an SSH tunnel to `127.0.0.1`) and fix the rules.
- Changes reach every worker and `wsi-sync` within
  `WSI_SETTINGS_REFRESH_SECONDS`; tiles an allowed client already had
  authorised at the edge can keep loading for up to the 60 s `/api/edge-authz`
  verdict cache.

---

## Secrets and security posture

### Rotating `JWT_SECRET`

`JWT_SECRET` signs the admin JWT, the app-access session cookie and the
signed `?t=` download links. Rotating it invalidates every active admin
session, every app-access session and every outstanding download link —
every browser will need to log in again. (Without a rotation, an admin JWT
expires after `JWT_TTL_SECONDS`, default 86400 = 24 h; an app-access session
after the lifetime set under **Network → Authentication**; a download link
after `WSI_DOWNLOAD_TOKEN_TTL_SECONDS`, default 3600.)

```bash
# 1. Generate a new secret
NEW=$(openssl rand -hex 48)

# 2. Replace the JWT_SECRET line in /etc/wsi/wsi.env (root-owned, mode 0600)
sudo sed -i "s|^JWT_SECRET=.*|JWT_SECRET=${NEW}|" /etc/wsi/wsi.env
unset NEW

# 3. Restart the services so the new secret takes effect
sudo systemctl restart wsi-app.service wsi-sync.service
```

Rotate when:

- The old secret may have leaked (e.g. an `/etc/wsi/wsi.env` backup ended
  up in a less-trusted location).
- An operator with shell access on the server has been off-boarded.
- Periodically, on the same cadence as other infrastructure secrets.

The app refuses to start if `JWT_SECRET` is unset or empty. Leaving it
blank will make `systemctl restart` fail; check `journalctl -u wsi-app`
for the exact message if the service won't come back up.

`JWT_SECRET` should be **at least 32 bytes** (the HS256 key-size floor;
`openssl rand -hex 48` gives 96). A shorter secret is still accepted, so an
install that has one keeps running, but every start logs a
`SECURITY: JWT_SECRET is shorter than 32 bytes` banner to the journal:
anyone holding a single token or session cookie could brute-force a short
secret offline and then forge both. Replace it with the procedure above.

### Login throttles

Failed sign-ins are throttled per client IP over a rolling window
(`ADMIN_LOGIN_IP_*` for the dashboard login — default 30 failures per
300 s; `APP_LOGIN_IP_*` for the app-access form login and the HTTP Basic
header — default 20 per 300 s; see `infra/wsi.env.example`; a
`*_MAX_FAILURES` of 0 disables that throttle). Once a window fills, the
endpoint answers `429` with a `Retry-After` until enough failures have aged
out. The counters live in each worker's memory, so the effective ceiling is
workers × the configured limit, and a restart forgives them.
An IPv6 client is counted per **/64** rather than per address — any host can
pick fresh addresses inside its /64 at will, so a per-address count would
give it a new budget with each one — and an IPv4-mapped IPv6 address
(`::ffff:192.0.2.7`) counts as the IPv4 address it carries. Everyone in one
/64 therefore shares a budget, as everyone behind one NAT'd IPv4 address
already does.

The dashboard login additionally locks a single **account** after
`ADMIN_LOGIN_MAX_FAILURES` consecutive failures (default 5) for
`ADMIN_LOGIN_LOCKOUT_SECONDS` (default 900): during the lockout even the
correct password is answered with `429` and a `Retry-After`. This counter is
stored in the `users` row, so it is exact across workers and survives a
restart; it simply expires, and a successful sign-in resets it. For an
account with [two-factor sign-in](#two-factor-sign-in-dashboard) on, the
code step counts against the same budget, and only a correct code — not the
password alone — resets it.

A lockout that anyone who knows a username can trigger would let them keep the
real owner out indefinitely, so two kinds of attempt stand **outside** it —
neither refused by it nor charged to it, though both still verify the password
and still count against the per-address throttle above:

- **A browser that has signed in as that account before.** A completed sign-in
  leaves a signed, `HttpOnly`, `SameSite=Strict` cookie (`wsi_kd_<account id>`,
  scoped to `/api/admin/login`, 30 days — `WSI_KNOWN_DEVICE_TTL_SECONDS`). It is
  tied to the account's `token_version`, so a password change, suspension,
  two-factor change or **Sign out everywhere** ends the trust, and a correct
  sign-in with it lifts the lockout for everyone. Someone guessing from
  elsewhere does not have the cookie, but the cookie is a bearer token: anyone
  who copies it from a browser (a shared computer the owner once signed in on)
  can present it from any address. So every attempt made under it (its
  password check, and its two-factor code check) takes one of a budget of
  slots of its own, 4 times `ADMIN_LOGIN_MAX_FAILURES` per lockout period per
  account and web worker, taken before the credential is checked so parallel
  guesses cannot all slip through; once the slots are used the cookie no longer
  exempts and the ordinary lockout applies again (to the owner as well) until
  the window passes or a sign-in under the cookie succeeds, which frees them
  all. **Sign out
  everywhere** (Account page) ends it outright. The audit row of such a sign-in
  carries `lockout_bypass`.
- **The server itself** (a client address of `127.0.0.1` / `::1`, e.g. an SSH
  tunnel to the box): the way back in for a sole admin who has no known browser
  at hand. It applies only when nothing says the request came from elsewhere:
  a proxy on the same host in front of nginx (Caddy, `tailscale serve`) makes
  every client look like `127.0.0.1`, but reports the real one in
  `X-Forwarded-For`, and a hop outside the loopback range there ends the
  exemption. A proxy that does not send that header cannot be told apart, so
  with one in front of nginx do not rely on the lockout alone: keep the
  per-address throttles on and consider `ADMIN_LOGIN_IP_MAX_FAILURES`.

What remains: while an account is locked, an attempt from any other browser
gets `429` for an existing, active account and `401` for an unknown or
disabled one, so staff usernames can be told apart by someone who deliberately
triggers a lockout. That is the price of showing the legitimate user a
`Retry-After`.

Passwords (dashboard accounts and viewer accounts in `basic` mode) are hashed
with PBKDF2-HMAC-SHA256 at `WSI_PASSWORD_HASH_ROUNDS` rounds (default
**600000**, the current OWASP guidance — a few hundred milliseconds of one core
per password sign-in, never per request). A hash made with fewer rounds is
upgraded in place the next time its owner signs in (and a hash made with more
rounds is brought down to a lowered setting the same way); for a `basic` viewer
account that changes the credential fingerprint of its session cookies, so the
same account's *other* signed-in browsers are asked to sign in once more. A
whole class signing in to one shared account at the same moment is fine: the
first sign-in upgrades the hash and the others, with the right password, are let
in on it. A failed sign-in costs about the same whether the username exists,
exists with an older hash, or does not exist (the shorter verify of an older
hash is padded up to the current setting), so response time does not list the
accounts; a hash made with MORE rounds than a setting you lowered is the one
exception, until its owner signs in. After changing the setting restart
`wsi-app` and `wsi-sync` together (`sudo systemctl restart wsi-app wsi-sync`):
both verify viewer passwords, and while they disagree about the count each
sign-in rewrites the shared account's hash for the other, which signs that
account's other browsers out. Lower the setting only if signing in at the same
moment would otherwise queue on your hardware.

In front of that, nginx limits the requests to both login endpoints per client
address (10 a minute, a burst of 5 — `wsi_admin_login` / `wsi_app_login` in
`infra/nginx/wsi.conf`) and, for IPv6, also per `/64` (`wsi_app_login_net`:
120 a minute, a burst of 100; `wsi_admin_login_net`: 30 a minute, a burst of
10), since an IPv6 client can pick a new address from its `/64` for every
attempt. The `/64` budget is sized for a class whose students each have their
own address on one campus network signing in at once; a larger class on one
`/64` sees some `429`s at first and gets in on a retry a few seconds later.

- **Basic mode and password rotation.** A browser tab that still caches a
  basic user's old password re-sends it with every request, including the
  edge-authz check behind every tile and thumbnail. A repeat of the *same*
  username + password counts as one failure per window (and is refused from
  memory without another password hash), so one stale tab cannot lock out
  everyone behind the same NAT address; each *different* password tried still
  counts. Ask users to reload (and re-enter the new password) after a rotation.
- **Current-password check on the account page** (`REAUTH_IP_*`, default 10
  failures per 300 s; `0` disables it). This budget
  is per dashboard token, not per IP: a leaked token cannot gain fresh
  guesses at the account's password by switching addresses, and only a
  correct password mints another token. Nor is it shared across the account,
  so whoever holds a leaked token cannot exhaust it to stop the real user —
  signed in afresh — from changing the password, which revokes the leaked
  token. It is separate from the login lockout, so exhausting it never blocks
  the real user's sign-in.

### Initial admin password

`infra/install.sh` writes the bootstrap admin password to
`/etc/wsi/admin-initial-password.txt` (mode 0600 root:root) on a fresh
install — it is **not** echoed to stdout / journald. Read it once with:

```bash
sudo cat /etc/wsi/admin-initial-password.txt
```

After logging in and changing the password from the **/admin/account**
page, delete the file:

```bash
sudo rm /etc/wsi/admin-initial-password.txt
```

If the file is missing on a fresh deploy, the install probably found an
existing `/etc/wsi/wsi.env` and skipped credential generation entirely.
In that case, the running secrets in `wsi.env` are the source of truth.

The same password also stays in `wsi.env` as `ADMIN_BOOTSTRAP_PASSWORD`. It is
read only at startup and only while no admin account exists (it seeds that
first admin), so changing the password in the dashboard does not update it and
it is not a second way in; blank it if you prefer — a startup with no admin and
no bootstrap password then fails loudly instead of seeding one.

Leave `ALLOW_ADMIN_REGISTER=0` (the default). Set to `1`, it lets any
unauthenticated caller create an admin through `POST /api/admin/register`
whenever no admin account exists, and every start logs a `SECURITY` warning
while it is on.

### Password policy

Both the staff dashboard accounts and the app-access `basic` users
(**Network → Authentication**) require **8 to 1024 characters**
(`MIN_PASSWORD_LEN` / `MAX_PASSWORD_LEN` in
`services/app/wsi_app/auth.py`). The same `validate_password_strength()`
helper is invoked by every create / change-password endpoint — and at
startup on `ADMIN_BOOTSTRAP_PASSWORD` when it seeds the first admin — so
tightening the policy in the future is a one-line change. LDAP and
Google passwords are the directory's / Google's business, not this
policy's.

There is no built-in complexity / dictionary check — operators are
expected to provide strong random passwords. The bootstrap admin
password generated by `install.sh` is 16 base64-derived characters
(~95 bits of entropy) and serves as the floor for anything weaker the
operator sets later.

The policy applies uniformly across `admin`, `manager`, and
`instructor` accounts.

### Content-Security-Policy

The site CSP is sent by nginx (see `infra/nginx/wsi.conf`):

```text
default-src 'self';
img-src 'self' data: blob: https://*.google-analytics.com https://*.googletagmanager.com;
style-src 'self' 'unsafe-inline';
script-src 'self' https://www.googletagmanager.com;
font-src 'self' data:;
connect-src 'self' https://*.google-analytics.com https://*.analytics.google.com https://*.googletagmanager.com;
worker-src 'self' blob:;
frame-ancestors 'none';
base-uri 'self';
form-action 'self';
```

The Google origins exist for one optional feature: the
[Google Analytics tag](#google-analytics-optional-external) an admin can enable
under **Network → Google Analytics**. `script-src` allows the `gtag.js` loader
by **origin only** (`www.googletagmanager.com`), not `'unsafe-inline'`: the GA
bootstrap in `App.tsx` initialises `dataLayer` / `gtag` in JavaScript
precisely so the inline-script allowance is never needed. `connect-src` /
`img-src` carry the hosts Google documents for GA4, whose beacons go to
regional endpoints such as `region1.google-analytics.com` /
`region1.analytics.google.com` — hence the wildcards. Nothing else in the app
contacts them, and with no Measurement ID configured (the default) no request
is ever made to any of them — so a deployment that will never use GA can
safely drop every Google origin from its site config.

`style-src 'unsafe-inline'` is present as a deliberate trade-off:

- OpenSeadragon (third-party) writes inline `style=` on its overlay
  container; that code path is not under this project's control.
- The viewer's pin / shape overlays set inline styles dynamically while
  positioning markers against the live OSD viewport.
- The React SPA uses `style={{...}}` prop liberally throughout the admin
  pages.

Migrating these callers to a nonce-based scheme (per-request nonce
injected by nginx + propagated through React) would remove the need for it.
Until such a migration, the residual risk is bounded by:

- `script-src` does **not** allow `'unsafe-inline'`. A style-only
  injection cannot run JavaScript.
- `frame-ancestors 'none'` blocks framing-based UI redress.
- `base-uri 'self'` and `form-action 'self'` block navigation hijacks.

Operators who need a stricter style policy can override the CSP header
in their nginx site config; nothing in the application reads it.

The same server block also sends `X-Frame-Options: DENY`,
`X-Content-Type-Options: nosniff` and `Referrer-Policy:
strict-origin-when-cross-origin` on every response, and hides the nginx
version (`server_tokens off`). `Strict-Transport-Security` ships commented
out — enable it, in `/etc/nginx/snippets/wsi-tls.conf` (yours; updates never
overwrite it), once TLS is in front
([QUICKSTART → TLS](../QUICKSTART_FOR_UBUNTU.md#tls-recommended-for-public-deployments)). nginx does not
inherit `add_header`s into a location that declares its own, so the cached
`/dzi/`, `/thumbs/` and `/assets/` locations (which add `X-Cache-Status`)
repeat the three static headers and send no CSP, and an HSTS line added only
at server level is missing from them too: they serve tiles, images and
attachments, not pages. For `/assets/` the app adds its own guard: a file that
is not one of the image / PDF / audio / video types a tour asset may be is
served as `Content-Disposition: attachment` under
`Content-Security-Policy: default-src 'none'; sandbox`, so it never renders as
a page on the site's origin.

When the optional LAN-direct probe is enabled (`WSI_LAN_HOST`), the LAN host
must additionally appear in `connect-src`. The installer injects it into the
bundled server block automatically — which also covers HTTPS set up through
`/etc/nginx/snippets/wsi-tls.conf`, since that snippet is included into the
same block — but **any other server block that serves the site — e.g. a
hand-made TLS vhost or a dedicated LAN-only block — must carry the same
`connect-src` entry by hand**; see the "Gotcha" note on the
LAN-direct probe under [Configuring Cloudflare (Paths A and
B)](#configuring-cloudflare-paths-a-and-b).

---

## Cloudflare Access + Tunnel integration

The app can run behind Cloudflare Tunnel (cloudflared) with Cloudflare
Access in front, and still record per-user access analytics — the
verified email from CF Access is pulled into `PageView.user_email`
alongside the rows produced by the app's own basic / LDAP / Google
OAuth flow. Page views are recorded only once an admin turns analytics
on (the mode defaults to `off`; see [Access analytics](#access-analytics)):
in `plain` mode the identities below are stored as-is, in `hashed` mode as
a keyed hash.

### Ingress paths (A / B / C)

Three distinct ingress topologies can coexist on the same deployment.
Distinguishing them matters because each one carries a different set of
`Cf-*` headers and is gated by a different layer of the auth pipeline.

| Path | Route | `Cf-Access-Jwt-Assertion` | `Cf-Connecting-IP` | nginx peer (`request.client.host`) | Auth gate that admits the request | Identity recorded in `PageView.user_email` |
|---|---|---|---|---|---|---|
| **A. External via CF Access SSO** | Browser → CF edge → **CF Access (SSO prompt)** → CF Tunnel → cloudflared → nginx → app | present, valid | present | loopback (cloudflared) | CF Access JWT verification | Verified `email` claim from the signed JWT (the `Cf-Access-Authenticated-User-Email` header is **not** consumed — it is not covered by the JWT signature; a service-token policy with no `email` claim is recorded as anonymous) |
| **B. Through Tunnel under a CF Access *Bypass* policy** | Browser → CF edge → **CF Access (Bypass match, no SSO)** → CF Tunnel → cloudflared → nginx → app | absent | present | loopback (cloudflared) | App's own mode (`basic` / `ldap` / `google_oauth`) configured in **Network → Authentication** | App-session subject, prefixed by mode (`basic:alice`, `ldap:jdoe`, `google:user@example.com`) |
| **C. LAN-direct, Tunnel *not* in path** | Browser → nginx (on the same LAN as the host) → app | absent | absent | the LAN client IP | App's own mode (same as B) | App-session subject (same as B) |

Notes on B vs C — these are easy to confuse:

- **B still goes through the Tunnel.** A CF Access *Bypass* policy is
  an action of CF Access ("admit without prompting for SSO"); it does
  not remove Cloudflare from the network path. cloudflared therefore
  still sets `Cf-Connecting-IP`, and nginx still sees loopback as the
  peer. What is absent is only the CF-issued JWT (no identity check
  ran).
- **C bypasses Cloudflare entirely** — typically a campus / facility
  client reaching `http://wsi.local` (or a private DNS name) over the
  LAN. cloudflared is not involved, no `Cf-*` header is genuine, and
  nginx sees the real LAN IP as the peer. Neither of the two headers
  the app acts on can be forged on this path: nginx blanks
  `Cf-Connecting-IP` and `Cf-Access-Jwt-Assertion` from any peer but
  loopback (see `geo $cf_hop_trusted` in `infra/nginx/wsi.conf`), and
  does not take the `X-Forwarded-Proto` scheme from a `Cf-Visitor` header
  such a peer sends. A forged JWT that did reach the app would still fail
  signature verification (`cf_access.verify_jwt`) and get a 401; the
  trusted-hop check cannot help here, because the app's TCP peer is always
  nginx on loopback. Other `Cf-*` headers (such as
  `Cf-Access-Authenticated-User-Email`) pass through but are never read.

In all three paths the request ultimately lands in the same
`app_auth_middleware` (see `services/app/wsi_app/main.py`), which
evaluates gates in this order (after the always-open bypass paths: everything under `/api/auth/`
(app-access sign-in, state, logout, OAuth), `/api/health`,
`/api/admin/register` and `/favicon.ico` — the dashboard sign-in
`/api/admin/login` and its two-factor step `/api/admin/login/mfa` are
deliberately not among them): **staff** JWT bearer (any of `admin` /
`manager` / `instructor` — the check is `app_auth.staff_token_valid`, not an
admin-only one) → signed download link (`GET …/download?t=…` for a slide,
backup or export bundle with a valid token — `app_auth.signed_download_valid`)
→ CF Access JWT (admits A, falls through for B / C with
`cf_status == "absent"`) → the opt-in
[break-glass](#break-glass-unauthenticated-raw-ip-access) and
[direct-LAN](#skip-authentication-for-direct-lan-access-dashboard-setting)
exemptions (no-ops unless enabled) → mode-based gate (admits B / C when
configured).

### Configuring the app

Set both env vars in `/etc/wsi/wsi.env`:

```text
CF_ACCESS_TEAM_DOMAIN=your-team.cloudflareaccess.com
CF_ACCESS_AUD=<Application AUD tag from the Zero Trust dashboard>
```

Leaving `CF_ACCESS_TEAM_DOMAIN` blank (the default) disables the CF
integration: no assertion is ever verified or trusted, and requests without
one (Paths B and C) are gated exactly as on a non-Cloudflare deployment. An
assertion is not silently ignored, though: a request that still carries a
`Cf-Access-Jwt-Assertion` (an Access application left in front of the Tunnel)
is refused with 401 like an invalid one. Path A therefore needs both
variables set; Path B (Tunnel, Bypass policy or no Access application) works
either way.

`CF_ACCESS_AUD` is **required** when the team domain is set: without it
the app refuses to verify tokens — every request carrying an assertion is
then refused with 401 — because a missing AUD pin would let
any Access application in the team mint a token the app would accept. Find the
AUD tag in the Cloudflare Zero Trust dashboard under
**Access → Applications → (your app) → Overview → Application Audience (AUD) Tag**.

After editing the env file, restart both services (the live-sync
coordinator in `wsi-sync.service` verifies the same assertion on its
WebSocket upgrades):

```bash
sudo systemctl restart wsi-app.service wsi-sync.service
```

### Configuring Cloudflare (Paths A and B)

1. Create an Access **Self-hosted application** for your hostname.
2. Under **Policies**, define an **Allow** policy for the identity
   provider you use (Google Workspace, Azure AD, etc.). This drives
   Path A.
3. (Optional, for Path B) Add a **Bypass** policy that matches your
   organisation's public IP range (e.g. campus gateway IPs). The
   Bypass action admits matching requests through the Tunnel without
   prompting for SSO. Because no identity check ran, the request
   carries `Cf-Connecting-IP` but no `Cf-Access-Jwt-Assertion`, and
   the app gates it with its own basic / LDAP / Google OAuth login
   (**Network → Authentication**).
4. Configure the Tunnel to forward to `http://127.0.0.1:80` so it
   reaches nginx on the same host. The bundled nginx config trusts CF
   headers only when the immediate peer is loopback — see the
   `geo $cf_hop_trusted` block in `infra/nginx/wsi.conf`. This is
   what defends Path C against forged `Cf-*` headers.

   Do not point the Tunnel (or any other reverse proxy or load balancer,
   or a container's published-port mapping) at nginx from another
   address. nginx takes the client address from its TCP peer, so behind
   such a hop every Internet client would look like that hop's private
   address, i.e. "on the LAN" — and the direct-LAN exemption and
   `WSI_BREAKGLASS_NOAUTH` would admit them. As a second line of defence
   the app also reads the `X-Forwarded-For` chain such a proxy fills in
   and withholds both exemptions when any hop in it lies outside the
   trusted ranges (a client can only add hops to its own request, so
   forging the header can only cost it the exemption) — but the Tunnel
   on the same host remains the supported topology.

Path C requires no Cloudflare configuration; it is just the plain
"point a browser at the host on the LAN" flow.

> **When split DNS is available, you do not need any of this.** The
> cleanest way to let on-LAN clients reach the host directly while keeping
> a single URL is **split-horizon DNS** (a.k.a. split DNS): configure the
> internal resolver so the *same* public hostname (`wsi.example.org`)
> resolves to the server's **LAN IP** for clients on the local network,
> while external resolvers keep returning the Tunnel/public path. On-LAN
> browsers then connect straight to the host over the identical URL — no
> probe, no separate LAN hostname, no DNS-01 certificate for a second name,
> and no CSP / cookie-domain juggling. If you control the LAN's DNS, prefer
> that and skip the probe entirely (leave `WSI_LAN_HOST` unset).
>
> The LAN-direct probe described below exists for environments where split
> DNS is **not** an option — e.g. you do not control the resolver clients
> use, DNS is managed/locked down, or hairpin NAT makes the public name
> unusable from inside. It achieves the same "one URL to share, direct LAN
> route" outcome from the **client side** instead, at the cost of the extra
> machinery (a dedicated LAN hostname, its DNS-01 certificate, CSP and
> session-cookie wiring) documented in the rest of this section.

Optionally, Path C can share the **same entry URL** as Paths A and B. On page
load the browser issues a no-cors `fetch()` (a bodiless `HEAD` request) for
`/favicon.ico` on the server's LAN hostname as a reachability probe and,
when the host answers at all, transparently switches to the LAN-direct
route. nginx serves that route
over HTTPS using a publicly-issued certificate obtained via DNS-01 for
the LAN hostname, so the switch raises no certificate warning. The
address bar then shows the LAN hostname (with the same path, query and
fragment); users should share and bookmark the public URL, which works on
and off the LAN, not the LAN-host URL shown after the switch. (`/favicon.ico` is exempted from the app auth
middleware so the probe works pre-login — see `BYPASS_EXACT` in
`services/app/wsi_app/app_auth.py`.) Off-LAN clients cannot reach the
LAN host; the probe aborts after a short timeout via `AbortController`
so an unreachable host never pins the browser's tab loading spinner.
Because the probe is a cross-origin `fetch()`, the LAN hostname must also
appear in the `connect-src` directive of the nginx CSP. A single value
drives both halves of the feature so they can never drift apart: the
frontend build bakes it in as `VITE_LAN_HOST`, and the installed nginx CSP
gets it added to `connect-src`.

> **Two properties of the hand-off worth knowing.**
>
> - **A raw-IP address bar is left alone.** When the page was loaded by IP
>   literal (IPv4 dotted-quad or any IPv6 form) the probe returns without
>   redirecting. That is deliberate: the raw-IP route is the documented
>   break-glass path used *precisely when* the LAN hostname or its certificate
>   is broken, so bouncing the operator back onto that hostname would defeat
>   the access they chose the IP to obtain. See
>   [Break-glass: unauthenticated raw-IP access](#break-glass-unauthenticated-raw-ip-access)
>   below.
> - **The redirect drops the referrer.** The hand-off sets a `no-referrer`
>   policy before navigating, so the LAN page loads with an empty
>   `document.referrer`. Without it, every LAN-switched visit would appear in
>   the analytics **Referrers** breakdown as referred by the app's own public
>   host; instead they record as direct traffic. The genuine inbound referrer
>   of the original public load (a search engine, an LMS link) lives on the
>   public host's page only and is not carried across the switch.

**Production (install.sh / update.sh).** Set the hostname once in
`/etc/wsi/wsi.env` and the installer wires up everything on every run:

```bash
# /etc/wsi/wsi.env  (preserved across updates)
WSI_LAN_HOST=lan.wsi.example.org
```

```bash
sudo bash infra/update.sh   # rebuilds the SPA with the probe AND installs
                            # an nginx conf whose CSP allows the host
```

`install.sh` reads `WSI_LAN_HOST` from `wsi.env`, passes it to the build as
`VITE_LAN_HOST`, and injects `https://<host>` into the CSP `connect-src` by
a single targeted edit of the canonical `infra/nginx/wsi.conf`. Because
`wsi.env` survives updates, there is **nothing to redo after `update.sh`** —
set it once. Leave it unset and the probe stays disabled with the stock CSP
installed verbatim.

#### First-load wait window (`WSI_LAN_PROBE_TIMEOUT_MS`)

A device's **very first** probe is given a longer timeout than every later
one. The reason is the browser's *"allow local network access"* permission
prompt: modern browsers gate access to local/private-network resources behind
such a prompt (Chrome's **Local Network Access** permission — the successor to
Private Network Access — and the local-network access prompts on
iOS/iPadOS/macOS), so this is **not specific to any one OS or vendor**. On the
first probe the LAN host resolves onto the local network, the `fetch()` is held
pending while that dialog is on screen, and a short timeout would abort long
before the user can tap *"Allow"* — so the first page loaded would stay on the
public URL while later pages (probed after permission was granted) redirect.
The long first window lets the first probe outlast the dialog. Because a
`fetch()` drives no tab loading spinner, an off-LAN client that simply waits
this window out pays nothing visible; every later session uses a fast fixed
timeout, so off-LAN clients give up promptly after the first load.

The first-load window defaults to 12000 ms (12 s; also used when the value is
`0`) and is tunable; later sessions use a fixed 500 ms per attempt (up to 3
attempts). Raise it if
first-load redirects keep losing the race against the permission dialog on
your devices:

```bash
# /etc/wsi/wsi.env  (preserved across updates)
WSI_LAN_HOST=lan.wsi.example.org
# first-probe wait in ms; unset -> built-in default. Keep the note on its own
# line: a trailing comment after the value hides it from install.sh.
WSI_LAN_PROBE_TIMEOUT_MS=12000
```

`install.sh` reads it with the same precedence as `WSI_LAN_HOST` and bakes it
into the build as `VITE_LAN_PROBE_TIMEOUT_MS`. It only affects the first probe
per device and has no effect unless `WSI_LAN_HOST` is set. For manual builds:
`make frontend-build WSI_LAN_HOST=lan.wsi.example.org WSI_LAN_PROBE_TIMEOUT_MS=12000`.

> **Reload vs. new tab after a missed first probe.** If the first probe loses
> the race above (or otherwise fails), the client stays on the public URL — and
> a **same-tab reload will not re-probe**: the probe runs at most once per
> browser session, guarded by a `sessionStorage` flag that a reload preserves.
> (The fast probe's up-to-3 attempts keep a single slow cold TLS/mDNS
> connection from stranding a session, but a genuine miss is retried only in
> a new tab.)
> Opening the same URL in a **new tab** starts a fresh session, re-runs the
> probe (now fast, since permission has been granted) and redirects to the LAN
> host. This asymmetry is intentional: `sessionStorage` (not `localStorage`)
> means a device that later leaves the LAN re-evaluates on its next session
> instead of staying pinned to a now-unreachable LAN host. The probe never
> stores a "use the LAN host" decision — it only redirects on a live, currently
> succeeding reachability check — so an off-LAN client is never sent to the LAN
> host in the first place.

> **Gotcha — additional server blocks need the `connect-src` entry too.**
> The installer's CSP edit only rewrites the **single** server block in the
> canonical `infra/nginx/wsi.conf` it installs to
> `/etc/nginx/sites-available/wsi`. HTTPS configured the standard way, in
> `/etc/nginx/snippets/wsi-tls.conf` (where `install.sh` also moves a
> certbot-patched site file's TLS lines), is included into that same block and
> so needs nothing extra. The edit does **not** touch any *other* server
> block that serves the site — most commonly a hand-made TLS vhost in a
> separate file, or the dedicated LAN-only block that terminates HTTPS for
> `WSI_LAN_HOST` itself. Those blocks carry
> their **own** `add_header Content-Security-Policy`, and if the document on
> which the probe runs (the public URL the browser actually loads) is served
> by one of them, its `connect-src` must list `https://<WSI_LAN_HOST>` or the
> browser blocks the probe `fetch()` with:
>
> ```text
> Refused to connect ... violates the following Content Security Policy
> directive: "connect-src 'self' ...".
> ```
>
> The client then silently stays on the public route (and, behind a shared
> NAT, can start collecting `429`s from the per-IP rate limiter). Add
> `https://<WSI_LAN_HOST>` to the `connect-src` of **every** server block
> that serves the page, then `sudo nginx -t && sudo systemctl reload nginx`.
> Confirm which CSP is live with:
>
> ```bash
> curl -sI https://<public-url>/ | grep -i content-security-policy
> ```
>
> **Custom CSP blocks:** the LAN probe is a no-cors `fetch()`, governed by
> **`connect-src`**. If you added the LAN host to `img-src` by hand in a
> custom block, move (or copy) that entry to `connect-src` — an `img-src`
> allowance does not satisfy the probe.

**Manual / dev builds.** Outside the installer, the same single value drives
the `Makefile`:

```bash
make frontend-build WSI_LAN_HOST=lan.wsi.example.org   # sets VITE_LAN_HOST
make nginx-config   WSI_LAN_HOST=lan.wsi.example.org   # -> wsi.conf.generated
sudo install -m 0644 infra/nginx/wsi.conf.generated /etc/nginx/sites-available/wsi
sudo nginx -t && sudo systemctl reload nginx
```

`make nginx-config` derives the file from the canonical `infra/nginx/wsi.conf`
by the same single `connect-src` edit, so there is no separate template to
drift. Other configuration details (DNS-01 cert, the LAN-only server block)
are out of scope for this document.

### Carrying the CF Access identity onto the LAN host

By itself, the LAN-direct probe creates an **identity gap** in the access
analytics. The public host (Path A) authenticates with Cloudflare Access,
so each pageview records the verified email from the CF JWT. But once the
probe redirects a client to the LAN host, the request is **Path C** —
Cloudflare is no longer in the network path, there is no `Cf-Access-Jwt-Assertion`,
and the LAN host has no way to know who the user is. Unless the app's own
`basic` / `ldap` / `google_oauth` gate is enabled, LAN pageviews record as
**anonymous**. Enabling that gate would close the gap but at the cost of a
**second interactive login** on the LAN host — the user already logged in at
the Cloudflare Access prompt on the public host, and would now be asked to log
in again. That double login is usually unacceptable.

`WSI_SESSION_COOKIE_DOMAIN` closes the gap **without** the second login. The
public host and the LAN host are served by the **same** app process behind the
same nginx, so they share the app-session signing key. When this variable is
set to a parent domain the two hostnames share, a request that Cloudflare
Access has already verified (`cf_status == "valid"` on the trusted hop) is
handed an app-access session cookie scoped to that domain, with the verified
email as its subject. Because the public and LAN hosts are *same-site*
(they share a registrable parent domain) and the probe's redirect is a
top-level navigation, the browser sends that `SameSite=Lax` cookie along to
the LAN host automatically. The LAN host's auth middleware then sees a valid
session cookie, admits the request silently, and the analytics beacon records
the **same** verified email it would have recorded on the public host. No
second prompt, no anonymous LAN rows.

```bash
# /etc/wsi/wsi.env  (preserved across updates)
CF_ACCESS_TEAM_DOMAIN=your-team.cloudflareaccess.com
CF_ACCESS_AUD=<Application AUD tag>
# Notes go on their own line, never after the value. A trailing "# ..." is
# read two different wrong ways: install.sh's read-back skips the line
# outright (WSI_LAN_HOST would silently lose its probe and CSP entry), and
# systemd keeps the comment as part of the value (WSI_SESSION_COOKIE_DOMAIN
# is read at runtime, so the text would land inside the cookie Domain).
# LAN-direct host (nested, see below)
WSI_LAN_HOST=lan.wsi.example.org
# shared parent of the public + LAN host
WSI_SESSION_COOKIE_DOMAIN=wsi.example.org
```

How it behaves:

- **Only ever asserts what CF already proved.** The cookie is minted solely
  on the `cf_status == "valid"` branch — i.e. for a cryptographically verified
  CF JWT arriving on a trusted hop. A Path B / Path C request (no JWT, or a
  forged header from an untrusted peer) never triggers it, so a LAN-side
  attacker cannot obtain a session this way.
- **One identifier across both hosts.** The session subject is the **bare
  email**, identical to what the CF JWT path records, so the same person
  buckets into a single row in the admin Top-Users table rather than splitting
  into `alice@…` (public) and a prefixed variant (LAN).
- **Self-healing on account switch.** The cookie is re-minted whenever the
  incoming one is absent or carries a different subject (e.g. the user picked a
  different Google account at the CF gate); a steady-state request adds no
  `Set-Cookie` header.
- **Revocation is by TTL.** Like every ldap / Google / CF app-access session,
  it lives for **Session TTL** (`AuthSettings.session_ttl_seconds`, default
  24 h) and is not individually revocable. Lowering the TTL also applies to
  sessions already signed in: a cookie older than the new value stops working
  on its next request (within `WSI_SETTINGS_REFRESH_SECONDS` on the other
  workers); raising it does not extend cookies already issued. To end every
  session at once, use [Sign everyone out](#signing-every-visitor-out). (Only
  `basic` sessions are bound to their local account: deleting a basic user or
  resetting their password ends that user's sessions within ~30 seconds on
  every worker.)

> **Scope the cookie domain narrowly.** A `Domain` cookie is sent to the
> domain **and all of its subdomains**. If your two hostnames only share a
> broad organisation-wide parent (e.g. `wsi.example.org` and
> `wsi-lan.example.org` share only `example.org`), setting
> `WSI_SESSION_COOKIE_DOMAIN=example.org` would leak the WSI session cookie to
> every other `*.example.org` service. **Nest the LAN host under the public
> host instead** — e.g. public `wsi.example.org`, LAN `lan.wsi.example.org`,
> `WSI_SESSION_COOKIE_DOMAIN=wsi.example.org` — so the cookie stays within
> `*.wsi.example.org`. A leading dot in the value is stripped. Leave the
> variable unset (the default) and cookies stay host-only.

> **Requires CF Access on the public host.** This mechanism carries the
> *Cloudflare-verified* identity; it does nothing on a deployment that doesn't
> run CF Access (Paths B / C only). For those, the LAN host's identity must
> come from the app's own `basic` / `ldap` / `google_oauth` gate.

### Trust model

The app accepts a CF Access identity (Path A) only when **all three**
of the following hold (plus `CF_ACCESS_AUD` being set — see above);
otherwise the assertion is classified `invalid` (see below for what that
does):

1. The immediate peer (`request.client.host`) is in `TRUSTED_PROXIES`
   (default: loopback only). Anything else means the request did not
   transit cloudflared — for example a Path C request that arrived
   straight from a LAN client and happened to set `Cf-Access-*`
   headers itself.
2. The JWT signature verifies against Cloudflare's JWKS
   (`https://<team>.cloudflareaccess.com/cdn-cgi/access/certs`).
3. `aud`, `iss`, and `exp` claims match the configured policy.

When a JWT *is* present but any of the above fails — or one arrives while
the integration is disabled — the request is rejected with 401
(`{"detail":"Cloudflare Access verification failed"}`) on every path outside
the always-open bypass list, unless an earlier gate already admitted it (a valid
staff bearer or signed download link). The analytics beacons
(`/api/analytics/pageview`, `/slide-dwell`, `/tour-event`) are exempted from
that 401: they log a WARNING (rate-limited) and record the row as anonymous,
so a misconfigured client cannot break analytics for legitimate users — but
they still have to pass the mode-based gate. Check
`journalctl -u wsi-app | grep "CF Access JWT verification failed"` to
detect either a stale-token storm or an attempt to forge assertions (that
line comes from the beacons; the rejected page and API requests themselves
show up as `401`s in the [nginx access log](#nginx-access-log)). A
`CF_ACCESS_AUD is unset; refusing to verify` warning (once per process)
means the AUD pin is missing, and `CF Access JWKS endpoint unreachable`
means the host cannot fetch Cloudflare's signing keys.

> **uvicorn must run with `--no-proxy-headers`.** Condition 1 depends on
> `request.client.host` being the *immediate TCP peer* (loopback, i.e.
> nginx). uvicorn enables `ProxyHeadersMiddleware` by default
> (`--forwarded-allow-ips=127.0.0.1`), which rewrites `request.client.host`
> to the `X-Forwarded-For` value nginx forwards — the real end-user IP.
> That makes the hop look untrusted, so **every CF Access request 401s**
> (`{"detail":"Cloudflare Access verification failed"}`) and every recorded
> analytics IP collapses to the forwarded value, with no verification log
> line (the hop check short-circuits before `verify_jwt`). The bundled
> `start-wsi-app.sh` and `wsi-sync.service` pass `--no-proxy-headers` so the
> app sees the true loopback peer and derives the real client IP itself from
> `Cf-Connecting-IP` / `X-Real-IP`. A symptom check: if
> `journalctl -u wsi-app | grep '"GET'` shows a public client IP as the
> uvicorn peer instead of `127.0.0.1`, proxy-header rewriting is still on.

When no JWT is present at all (Paths B and C), the CF gate is a no-op
and the request falls through to the mode-based gate.

### Verifying the deployment

Pick the curl that matches the path you want to exercise.

**Path A** — from an external browser (outside any Bypass IP range),
the dashboard URL must redirect through CF Access SSO:

```bash
curl -I https://your-host/api/admin/analytics/dashboard
# Expect: 302 to https://<team>.cloudflareaccess.com/...
```

After SSO, every page-view recorded carries the verified CF email (as-is in
`plain` analytics mode; a hex digest in `hashed` mode; no rows at all while
analytics is `off`):

```bash
sudo -u wsi sqlite3 /var/lib/wsi/wsi.db \
  "SELECT user_email, kind, occurred_at FROM page_views ORDER BY occurred_at DESC LIMIT 5;"
```

**Path B** — from an IP that matches the Bypass policy, the site
reaches the app without an SSO prompt. Opening `https://your-host/`
lands on the app's own sign-in (the `/login` page under LDAP / Google
OAuth, the browser's Basic dialog under basic); a plain `curl` (GET, no
`-I`) of the admin API URL above answers `401 {"detail":"Missing token"}`
instead, since it needs a staff
token. Post-login page views
record the session subject (e.g. `google:user@example.com`,
`ldap:jdoe`).

**Path C** — from a LAN client hitting the host directly (no
Cloudflare in path), the request bypasses CF entirely and is also
gated by the app's own login. Identity recording is the same as
Path B. To confirm forged CF headers get nowhere, send them from a
non-loopback peer: nginx blanks `Cf-Access-Jwt-Assertion` and
`Cf-Connecting-IP` from any peer but loopback (cloudflared), and the app never
reads the unsigned `Cf-Access-Authenticated-User-Email` header at all (a CF
identity is only ever the verified JWT's `email` claim), so nginx passes that
one through untouched and nothing acts on it. Use a path the app-access gate
covers (not `/api/health`, which is answered before the Cloudflare check): a
JWT header that reached the app would be refused there with
`Cloudflare Access verification failed`, whatever the mode:

```bash
# From a LAN client (NOT the host itself), against the host's LAN IP:
a=$(curl -s -w ' %{http_code}' http://<host-lan-ip>/api/slides)
b=$(curl -s -w ' %{http_code}' -H 'Cf-Access-Jwt-Assertion: forged' \
     -H 'Cf-Access-Authenticated-User-Email: attacker@example.com' \
     http://<host-lan-ip>/api/slides)
[ "$a" = "$b" ] && echo same
# Expect: "same" (nginx blanked the JWT, so the forged headers change nothing;
# $b never says "Cloudflare Access verification failed"), and NO row
# attributing the next page view to attacker@example.com.
```

### Using the app's own login instead of CF Access SSO (seat-limit workaround)

The Cloudflare Zero Trust **Free** plan caps identity seats (see Cloudflare's current plan limits): each user who completes an Access **SSO** login consumes one. An
organisation larger than that cap cannot put everyone behind CF Access SSO
(Path A). The supported way around this is to stop using CF Access for
identity and let the **app's own** `google_oauth` gate authenticate users:

- Either set the Access policy to **Bypass** for everyone, or remove the
  Access application entirely and keep just the Tunnel. A Bypass policy
  performs no login, so it consumes **no seats**. Every request then arrives
  as Path B (through the Tunnel) or Path C (LAN-direct), and the app gates it
  with the mode chosen in **Network → Authentication**.
- Enable **Google OAuth** there and set **Allowed email domains** to your
  organisation's domain (e.g. `example.org`). This reproduces, at the app
  layer, the "only my organisation" restriction that a CF Access *Email
  domain* policy gave you. A sign-in is accepted only when the account's
  email is verified, its domain is on the list **and** Google's ID token
  carries an `hd` (hosted-domain) claim that is itself one of the listed
  domains; only Google Workspace accounts have `hd`. A personal Google account
  registered under an address at your domain is therefore refused, and so is
  every account when the list names only a non-Workspace domain such as
  `gmail.com` — the organisation must use Workspace, and the list should name
  every domain the Workspace uses.
- To admit a named person outside those rules — an external collaborator, or
  a personal `gmail.com` account — add their address to **Individually
  allowed email addresses**. A listed address (matched exactly,
  case-insensitively, and still requiring a verified email) signs in without
  the domain / `hd` check, except one whose domain is also on the domain
  list: that keeps the `hd` requirement (skipping it would only admit a
  personal account registered under that address). Either list alone is
  enough; with both empty no Google account can sign in.
- With CF Access no longer verifying identity, `CF_ACCESS_TEAM_DOMAIN` /
  `CF_ACCESS_AUD` may be left set (harmless — no JWT ever arrives) or
  cleared.

#### Redirect URIs when the LAN-direct host is also in use

A concern with this setup is that the OAuth `redirect_uri` differs depending
on which hostname the user is on — the public host vs. the LAN-direct host the
probe redirects same-VLAN clients to. This is **not** a blocker: a Google
OAuth client accepts **many** Authorized redirect URIs. Register the callback
for **both** hostnames:

```text
https://wsi.example.org/api/auth/oauth/callback
https://lan.wsi.example.org/api/auth/oauth/callback
```

Registering both is all you need. The short-lived OAuth **state cookie**
(10 minutes, path `/api/auth`) is host-scoped, so the `/oauth/start` and
`/oauth/callback` legs must run on the **same** hostname, within those 10
minutes, or the callback rejects with *"OAuth state mismatch"*.
`_oauth_redirect_uri` (in `services/app/wsi_app/routers/app_auth.py`)
guarantees that: it builds the `redirect_uri` from the host the request
actually arrived on, so a login that starts on the LAN host also finishes
there. Setting `WSI_PUBLIC_BASE_URL` (for live-sync / share links) does **not**
break this, so it is safe even when a LAN-direct host is in use: it repins the
`redirect_uri` only for requests that arrive on the pinned host itself, and a
request on the LAN-direct host still derives its callback from that host.

A companion cookie with the state cookie's lifetime remembers the page the
visitor was trying to open (a live-sync share link, a tour), and a successful
sign-in returns them there instead of to the index page.

Because the LAN-direct probe redirects a same-VLAN client to the LAN host **on
page load, before any login**, each user authenticates once on whichever host
they end up on — there is no double login to design around here. (The separate
`WSI_SESSION_COOKIE_DOMAIN` carry mechanism described above applies only to a
*CF-Access-verified* identity on Path A; it has no effect on the app's own
OAuth login, whose session cookie is host-scoped by design.)

### Break-glass: unauthenticated raw-IP access

When the LAN-direct host depends on TLS + DNS (a Let's Encrypt certificate for
`lan.wsi.example.org` resolved over the LAN), a certificate or DNS failure can
make that host unreachable. The documented last resort is a **plaintext-HTTP,
raw-IP** route — `http://<server-LAN-IP>` — served by the plain-HTTP
port-80 listener of the bundled server block (`listen 80 default_server`),
which involves no TLS. Under `google_oauth` this route is
otherwise unusable: an interactive Google login cannot complete against a
private-IP / `http` origin (Google refuses such redirect URIs), so the user
would be stranded on a login screen.

Setting `WSI_BREAKGLASS_NOAUTH=1` in `/etc/wsi/wsi.env` lets that route skip
the app-access gate for clients in the trusted address ranges (by default the
LAN ranges) — and with it every other plaintext route those clients use, see
below (it is read at startup, so restart afterwards):

```text
WSI_BREAKGLASS_NOAUTH=1
```

```bash
sudo systemctl restart wsi-app.service wsi-sync.service
```

The exemption is keyed on three signals that
a client **cannot forge** (see `breakglass_exempt` in
`services/app/wsi_app/app_auth.py`):

1. **Not via the Tunnel.** A non-empty `Cf-Connecting-IP` survives only on the
   trusted loopback hop, so any Tunnel path (Path A or B) is excluded — the
   public hostname never becomes unauthenticated.
2. **Plaintext.** nginx overwrites `X-Forwarded-Proto` with the listener's
   scheme, so the value is `http` for every request nginx accepted on a
   plain-HTTP listener. The TLS LAN-direct vhost yields `https` and stays
   gated, and a forged `Host` header cannot move a TLS request onto the
   exemption because the scheme is fixed by which listener accepted the
   connection. (The one exception is the trusted loopback hop, where the edge
   scheme in cloudflared's `Cf-Visitor` header takes the listener's place: a
   process on the host itself can set it, a LAN or Internet client cannot.)
3. **The client is in a trusted range.** The real client address — taken
   from the peer, or from nginx's `X-Real-IP` only on the trusted loopback
   hop — must fall in `WSI_BREAKGLASS_TRUSTED_CIDRS`. By default that is the
   LAN: private IPv4 (`10.0.0.0/8`, `172.16.0.0/12`, `192.168.0.0/16`),
   loopback (`127.0.0.0/8`, `::1`), link-local (`169.254.0.0/16`, `fe80::/10`)
   and IPv6 unique-local (`fc00::/7`) — the ranges the dashboard's direct-LAN
   setting below uses. Any other address stays gated, so the public IP over
   `http://` (where port 80 is reachable from the Internet) does not open.

   Some campuses give their machines global addresses, which the default
   does not cover. Check what the server sees — the first field of
   `/var/log/nginx/access.log` for a request from a campus machine — and, if
   it is a global address, list the campus range in
   `WSI_BREAKGLASS_TRUSTED_CIDRS`. A set value **replaces** the default (so it
   can narrow it too), so keep the default ranges you still want beside it:

   ```text
   WSI_BREAKGLASS_TRUSTED_CIDRS=10.0.0.0/8,172.16.0.0/12,192.168.0.0/16,127.0.0.0/8,169.254.0.0/16,::1/128,fc00::/7,fe80::/10,198.51.100.0/24
   ```

   Entries are separated by commas or spaces; a bare address counts as a
   single host. An entry that is not an address or network is ignored with a
   WARNING in the log (if none is valid, no one is admitted), and a `/0`
   entry — every address — is honoured but logged as a WARNING. Restart both
   services after changing it.

The `Host` is not checked: the bundled server block's port-80 listener is a
plain-HTTP `listen 80 default_server` for every host name, so with the flag on, a client
in a trusted range reaches the app without a login by any name or address
over `http://`
— the raw IP, a LAN hostname over `http://`, a `.local` name. (So does a TLS
proxy you run on the host itself in front of nginx's port 80, which nginx sees
as a plaintext request from loopback.) This is deliberate: unlike the
dashboard setting, the flag lives in `wsi.env`, so it still works when nobody
can sign in to the dashboard to change that setting. Only the Tunnel, the TLS
listeners and clients outside the trusted ranges stay gated. Default off.

> **Understand what this opens.** On every plaintext route, traffic is
> **unencrypted** and **any client in the trusted ranges that can reach
> port 80 of the server, by any name or address, is admitted without
> authentication** (the app has no per-slide ACL — the gate is
> all-or-nothing). Enable this only when every network in those ranges is
> trusted; keep `WSI_BREAKGLASS_TRUSTED_CIDRS` as narrow as you can, and
> restricting port 80 to the same ranges at the firewall (e.g.
> `ufw allow from <trusted-range> to any port 80`) narrows it further. Page views on this
> route record as anonymous (no identity), which is acceptable for an
> emergency path. For day-to-day use, clients should always use the TLS
> LAN-direct host, which remains gated by the app's login.

Verify both sides:

```bash
# Raw-IP plaintext fallback: admitted without app login (break-glass).
curl -s -o /dev/null -w '%{http_code}\n' http://<server-LAN-IP>/api/slides
# Expect: 200

# TLS LAN-direct host: still gated by google_oauth (401 until logged in).
curl -s -o /dev/null -w '%{http_code}\n' \
  --resolve lan.wsi.example.org:443:<server-LAN-IP> \
  https://lan.wsi.example.org/api/slides
# Expect: 401

# Forged Host on the TLS endpoint does NOT reach the exemption (scheme=https).
curl -s -o /dev/null -w '%{http_code}\n' \
  -H 'Host: <server-LAN-IP>' https://lan.wsi.example.org/api/slides
# Expect: 401
```

From an address outside `WSI_BREAKGLASS_TRUSTED_CIDRS` the first check
returns `401` too. The two `401` checks assume the dashboard's "Skip
authentication for direct LAN access" (below) is **Off**: run from a LAN client, the first is admitted
under **Any LAN-direct access**, and the forged-`Host` one under either
level, since it names a bare private IP.

### Skip authentication for direct LAN access (dashboard setting)

For deployments where LAN clients are already trusted (authenticated by some
other means on a closed network), **Network → Authentication → "Skip
authentication for direct LAN access"** offers a softer alternative to the
`WSI_BREAKGLASS_NOAUTH` env var. It is a three-level selector (default **off**),
configurable from the dashboard without a restart, and intended both as an
emergency route and as a permanent trusted-LAN convenience. The level lets you
choose **how far** the exemption reaches:

- **Off** — no exemption; every request is gated by the configured auth mode.
- **Direct IP only** — skip the gate **only** when the client reaches the app
  by its **bare private-IP literal** `Host` (e.g. `http://192.168.1.50/`,
  typed straight into the address bar). A hostname / vhost `Host` (the normal
  public or LAN-direct vhost) does **not** qualify and stays gated, so the
  regular login UX is untouched. This is the narrowest level.
- **Any LAN-direct access** — skip the gate for **any** direct (non-Tunnel)
  request from a LAN client, over plaintext HTTP **or** TLS, whether the
  visitor used the server's bare private IP **or a LAN-direct hostname / vhost**
  (`https://lan.wsi.example.org/`). The broad trusted-segment level.

Whichever level you pick, two non-forgeable signals gate the exemption (see
`lan_direct_exempt` in `services/app/wsi_app/app_auth.py`); both are anchored on
the trusted-proxy boundary and cannot be forged from outside the LAN, so the
public path stays gated no matter what `Host` a client sends:

1. **Not via the Cloudflare Tunnel** — `cf_access.is_via_cloudflare_tunnel` is
   true only when a non-empty `Cf-Connecting-IP` arrives on the trusted
   loopback hop (cloudflared), so every Tunnel path (CF Access SSO *and*
   bypass) is excluded and the public hostname is never affected.
2. **The real client IP is on the LAN** — `network.get_client_ip` resolves a
   private / loopback / link-local address (RFC 1918, `127.0.0.0/8`,
   `169.254.0.0/16`, IPv6 ULA / link-local), honouring forwarded-IP headers
   only from the trusted loopback hop (nginx sets `X-Real-IP $remote_addr` on
   every proxied location). A public client setting `Host: lan.wsi.example.org`
   (or `Host: <private-IP>`) against the public endpoint resolves to a
   *public* client IP and stays gated.

The **Direct IP only** level adds a third, `Host`-based condition (the `Host`
must be a bare private-IP literal). That check is a UX gate, not a security
boundary — a spoofed `Host` still cannot pass signals 1–2 above — but it keeps
the normal hostname login flow intact while still allowing the bare-IP route.

> **What "Any LAN-direct access" opens.** Unlike "Direct IP only", this level
> also exempts the **normal LAN-direct hostname login flow** — a LAN visitor on
> `https://lan.wsi.example.org/` is admitted without logging in. That is the
> point of the level (you are declaring the whole LAN segment trusted), but
> select it only on a segment where every client is already trusted. The safety
> rests entirely on conditions 1–2 above; if you front the app with a
> non-standard reverse-proxy chain, make sure the real client IP still reaches
> the app (`X-Real-IP` from the trusted hop) before relying on this.

> **Difference from `WSI_BREAKGLASS_NOAUTH`.** Both admit only non-Tunnel
> requests from a trusted client address. The env-var break-glass covers
> *plaintext HTTP* only (any host), takes its address ranges from
> `WSI_BREAKGLASS_TRUSTED_CIDRS` (default: the LAN ranges) and needs a
> restart, but works without a dashboard sign-in; this setting covers HTTP
> and HTTPS, always uses the LAN ranges, is scoped to the chosen level, and
> is live from the dashboard.

---

## WSI tile cache size — `infra/set-cache-size.sh`

`infra/set-cache-size.sh` interactively updates `DZI_CACHE_SIZE` in
`/etc/wsi/wsi.env` and optionally restarts the service.

### What is DZI_CACHE_SIZE?

The backend keeps recently accessed WSI files open in an LRU cache
(one entry = one OpenSlide file handle + DeepZoom generator).
`DZI_CACHE_SIZE` controls the maximum number of entries.

- **Default: 8** — used whenever `/etc/wsi/wsi.env` has no value.
- The value is read from `/etc/wsi/wsi.env` (which this script writes) at
  service start, so a change takes effect only after a restart.

### Cache thrashing

If more distinct slides are being viewed concurrently than the cache can
hold, the LRU evicts one entry per cache miss and re-opens it the next
time it is needed (blocking I/O). This "thrashing" eliminates most of
the cache benefit.

**Rule of thumb:** set `DZI_CACHE_SIZE` ≥ the number of distinct slides
being viewed at the same time.

Memory impact scales with the number of entries × slide file size
(OS page-cache pressure), so do not set this arbitrarily large on
memory-constrained servers.

### Soft byte cap — `DZI_CACHE_MAX_BYTES`

`DZI_CACHE_SIZE` only caps the number of cached entries. A small entry
count can still drive substantial OS page-cache pressure if every cached
slide is multiple GiB. `DZI_CACHE_MAX_BYTES` adds a second eviction
trigger: once the sum of *file sizes* of cached entries exceeds this
value, the LRU evicts entries (newest just-inserted entry is preserved)
until the total is back under the cap.

- **Auto-derived from RAM when unset.** `start-wsi-app.sh` computes, per
  worker:

  ```text
  budget = RAM − max(RAM × (1 − fraction), reserve_min)
  per_worker = clamp(budget / workers, floor, ceil)
  ```

  with defaults `fraction = 75%` (assumes the host is dedicated to
  Tessera), `reserve_min = 2 GiB`, `floor = 2 GiB`, `ceil = 8 GiB`. The
  value is exported before the uvicorn master forks, so every worker
  agrees. On a RAM-rich host this lands at the **8 GiB**
  ceiling; on a small-RAM / many-core host it shrinks automatically, and
  the worker count is capped in step (`min(cores-formula, budget / floor)`)
  so each worker still gets at least `floor` of cache. Tunables:
  `WSI_CACHE_RAM_FRACTION`, `WSI_CACHE_RESERVE_MIN_BYTES`,
  `WSI_CACHE_FLOOR_BYTES`, `WSI_CACHE_CEIL_BYTES`.
- **Set `DZI_CACHE_MAX_BYTES` explicitly to override** the auto value;
  set `0` to disable the byte cap and rely solely on the entry count.
- This is **not a literal in-RAM tile cap** — the LRU holds OpenSlide
  handles + DeepZoomGenerator metadata (modest), not decoded tile bytes.
  The value is a proxy for the OS page-cache footprint OpenSlide pulls
  in when reading from those slide files. The OS page cache is shared
  across processes for the same file on disk, so this cap really bounds
  "how many distinct multi-GiB slides can be hot at once per worker"
  rather than per-worker process RAM (and because the page cache is
  reclaimable, an over-generous budget degrades to eviction, not OOM —
  which is why the 75% default is safe on a dedicated host).
- Preview the effective value (auto or pinned) with
  `sudo bash infra/set-workers.sh`, run from your clone (`~/wsi`).

### Usage

```bash
cd ~/wsi
sudo bash infra/set-cache-size.sh
```

The script shows the current value from both the env file and the
running process, asks for a new value, confirms before writing, and
offers to restart the service immediately.

Run it from your clone (`~/wsi`), as above. It runs as root and executes a
helper from its own tree (`infra/lib/envfile_set.py`), so it refuses to run
from the deployed copy under `/opt/wsi` — the service user owns that tree and
can rewrite it — or when the script, its directory or the helper it runs is
owned by an account other than root or you, or is writable by every user.

To target a non-default env file:

```bash
cd ~/wsi
sudo WSI_ENV=/path/to/other.env bash infra/set-cache-size.sh
```

### Observability in the admin dashboard

The admin **Server Info** page (`/admin/server` → **WSI Slide Handle Cache**)
displays the current entry count and the configured maxima (`max` entries
and `max_bytes`). When `DZI_CACHE_MAX_BYTES` is auto-derived from RAM (the
default), `max_bytes` reflects that computed value — it is read live from
the worker, not a literal env echo. The displayed values are a **snapshot
from the single uvicorn worker that served the HTTP request** — with
multiple workers, each carries its own LRU and the dashboard rotates
through them on successive refreshes. Use this view as a sanity check
("is the cache being exercised at all?") rather than a global accounting
of cached handles.

The dashboard does **not** provide controls to clear or resize the cache:
both operations would only affect one worker at a time and rarely produce
the global effect operators expect. Sizing is instead a start-time
concern applied uniformly to all workers — which is exactly how the
auto-derivation works. To resize permanently, edit `DZI_CACHE_SIZE` (or
pin `DZI_CACHE_MAX_BYTES`) in `/etc/wsi/wsi.env` — or run
`set-cache-size.sh` / `set-workers.sh` from your clone — and restart the
service. To force a full cache clear across all workers, restart
`wsi-app.service`.

---

## nginx tile cache — `infra/nginx-cache.sh`

`infra/nginx-cache.sh` is an interactive script for managing the nginx
HTTP cache that fronts the Deep Zoom (`/dzi/`) endpoints, and, for clearing,
the `/thumbs/` thumbnails and `/assets/` tour attachments, which are cached in
a zone of their own (`wsi_static_cache`, below). It can:

- show current cache statistics (disk usage, file count, configured `max_size`),
- delete the cache contents on disk and restart nginx,
- change the `max_size` value in the nginx site config and reload nginx.

These responses are cached **shared across all authenticated users** (the
cache key is the URL — for `/dzi/`, the path plus the slide's tile
generation `?v=` — never the session cookie; every request, HIT or MISS, is
first authorized by an `auth_request` subrequest to `/api/edge-authz`, which
checks app access and, for `/dzi/`, that the slide is still published, so a
HIT cannot bypass either). Clearing the cache empties both zones, so it affects
tiles, thumbnails and assets alike. Two kinds of request neither read nor fill
the shared caches:
one carrying a staff bearer (the dashboard's draft preview) and one carrying
a Cloudflare Access assertion (Path A in
[Ingress paths](#ingress-paths-a--b--c)) — so behind CF Access SSO every tile
is rendered by the app. The `/api/edge-authz` verdicts themselves are
kept in a separate, small `wsi_authz_cache` zone (`/var/cache/nginx/wsi_authz`,
60 s per allowed client and slide, entries dropped after 2 minutes unused,
denials never stored), so requests carrying made-up session cookies cannot
push tiles out of the tile zone. This script does not size that zone; it
needs no tuning.

Thumbnails and tour attachments are likewise cached in their own
`wsi_static_cache` zone (`/var/cache/nginx/wsi_static`, `keys_zone` 32m, entries
dropped after 10 minutes unused, no `max_size`). Their names are chosen by the
client, so storing the answers for made-up ones would let a flood push real
tiles out. Only existing files are therefore stored, in this zone or the tile
one: a `404` leaves nothing behind, and the keys carry no client-chosen query.
The static zone is also kept apart, so its
entries expire after 10 minutes unused and the most a flood of real names could
evict is other thumbnails and attachments, which one request re-fetches. A
thumbnail or attachment whose file is renamed or deleted stops being served as
soon as its cached copy expires (60 s / 1 h), to every request: the answer for
it is the app's `404`, which is not stored, and an expired copy is never handed
to a request that overlaps the refresh.

`/thumbs/` caches a thumbnail for 60 s and `/assets/` a tour asset for 1h,
both with `proxy_cache_revalidate` (once stale, nginx asks the app whether the
file changed and gets a bodiless `304` when it did not), so a replaced
thumbnail (WSI re-upload) appears within a minute and an overwritten tour
asset within an hour on its own; **clear the cache here to make such a change
appear immediately**. That is the edge: a browser that already showed the old
thumbnail can keep showing its own copy for longer, because a replace rewrites
the file under the same URL and `/thumbs/` responses carry no `Cache-Control`,
so the browser applies its own heuristic freshness until it revalidates.

`/dzi/` tile **images** use a 30-day TTL, the `.dzi` **descriptor** a short
5-minute TTL with `proxy_cache_revalidate`; what that means for publishing,
deletion and slide replacement — in short, nothing you have to act on — is
described in
[Publish state, deletion, replacement, and cache staleness](#publish-state-deletion-replacement-and-cache-staleness)
below.

This cache is **separate** from the in-process WSI tile cache controlled
by `DZI_CACHE_SIZE`:

| Cache | Where | Configured by |
|---|---|---|
| nginx HTTP cache | `/var/cache/nginx/wsi_dzi/` (disk) | `proxy_cache_path ... max_size=...` in `infra/nginx/wsi.conf` |
| App LRU cache    | per-worker Python process (memory) | `DZI_CACHE_SIZE` env var |

A request flow for a tile is: browser → nginx (auth_request verdict, then
HIT? serve from disk) → FastAPI (`/dzi/...`) → OpenSlide (HIT in app LRU?
serve from open file).

### Disk usage statistics — `wsi-cache-stat.timer`

The nginx cache directory (`/var/cache/nginx/wsi_dzi/`) is owned by
`www-data`, and nginx creates the cache subdirectories inside it with mode
`0700`, so the `wsi` service user cannot read them directly. To make disk
usage visible in the admin dashboard, `install.sh` provisions a small timer-driven snapshot service:

- `wsi-cache-stat.timer` — fires `wsi-cache-stat.service` 30 s after boot,
  then every 60 s (`OnBootSec=30s`, `OnUnitActiveSec=60s`, accuracy 5 s).
- `wsi-cache-stat.service` — runs `/usr/local/bin/wsi-cache-stat` as
  `www-data`, writing `{used_bytes, file_count}` to
  `/run/wsi-cache-stat/nginx_cache_stat.json`.
- The wsi app reads that file and surfaces the values under
  **Server Info → nginx Tile Disk Cache** in the admin dashboard, with the
  time the file was written. A file older than five minutes is flagged
  (⚠ "Not updated for …"): the timer has stopped and the figures are stale.
  Where the file is missing and the app can read the cache directory itself
  (a dev host), it counts the files directly — at most 200,000 of them, and
  then shows the figures as a lower bound. The tile cache is the
  `proxy_cache_path` with `keys_zone=wsi_dzi_cache`, read from
  `sites-enabled/wsi` first; commented-out lines are ignored.

The runtime directory (`/run/wsi-cache-stat/`) is intentionally separate
from `/run/wsi/` (used for backup/restore IPC, owned by `wsi:wsi`) — two
services with different `User=` cannot share a `RuntimeDirectory=`.

If dashboard cache stats appear stale or missing:

```bash
sudo systemctl status wsi-cache-stat.timer wsi-cache-stat.service
sudo journalctl -u wsi-cache-stat -n 50 --no-pager
```

### Usage

```bash
cd ~/wsi          # your clone
sudo bash infra/nginx-cache.sh
```

Run it from your clone, like `set-workers.sh` and `set-cache-size.sh`: it
refuses to run from the deployed copy under `/opt/wsi` (or a symlink into it),
or when the script, its directory or the code it runs is owned by an account
other than root or you, or is writable by every user, because it runs that
tree's code as root.

The script first prints a status block, then offers a menu:

```text
  1) Clear cache contents (rm files + restart nginx)
  2) Change max_size      (edit nginx config + reload nginx)
  3) Quit
```

Pick one and follow the prompts. The script confirms before any
destructive change.

### What "Clear cache" does

1. Counts files and disk usage under `/var/cache/nginx/wsi_dzi/`.
2. Asks you to type `yes` to confirm.
3. Deletes the cache contents (`find ... -mindepth 1 -delete`) — the
   directory itself is preserved (nginx needs it to exist).
4. Runs `systemctl restart nginx` — a restart, not a reload, so the
   in-memory cache keys-zone is rebuilt from the now-empty directory
   (deleting the files under a running nginx would otherwise leave its
   keys-zone accounting stale). Skipped with a warning if nginx is not
   running.

After clearing, the next requests for `/dzi/...` URLs will be cache
MISSes; nginx fetches from the FastAPI backend and re-fills the cache
on demand. You can verify with:

```bash
curl -I "http://127.0.0.1/dzi/<public_id>.dzi?v=<tile_version>"
# X-Cache-Status: MISS  (then HIT on the second request)
```

Include the `?v=` the viewer uses: `<tile_version>` is the slide's
`tile_version` field in `GET /api/slides/<public_id>` (or copy the `.dzi`
URL from the browser's network tab). A request without it is served but
deliberately never cached, so it always reports `BYPASS`, and a `v` that is
not the slide's current one gets a 404.

(With an auth mode other than `none` enabled, pass a valid session
cookie — an unauthenticated request is rejected by `auth_request`
with 401 before it ever reaches the cache, and carries no
`X-Cache-Status` header.)

### What "Change max_size" does

1. Reads the current `max_size=` value from the live nginx config.
2. Validates new input as a positive whole number, optionally followed by
   `k` / `m` / `g` in either case (e.g. `100g`, `500M`).
3. Shows the keys zone that goes with it (below) and asks for confirmation.
4. Rewrites `max_size=...` and the tile zone's `keys_zone=wsi_dzi_cache:...`
   in the live config (`/etc/nginx/sites-available/wsi`).
5. Runs `nginx -t`. If it fails, the previous config is put back and nothing
   is persisted or reloaded.
6. Persists the chosen value to `/etc/wsi/nginx-cache-max-size`. It
   deliberately does **not** edit the git-tracked `infra/nginx/wsi.conf` in
   the clone — doing so would make `update.sh`'s `git checkout` abort on
   local changes. Instead `install.sh` / `update.sh` read the persisted file
   and re-apply the value, and the matching keys zone, onto the freshly
   installed live config after each checkout.
7. Runs `systemctl reload nginx`. Files already cached keep serving as HITs
   across the reload, even when the keys zone was resized.

**The keys zone has to grow with `max_size`.** nginx keeps an in-memory index
entry for every cached file in the keys zone (about 8,000 per MB) and, once
that zone is full, evicts the least recently used file no matter how much
disk `max_size` still allows. A fixed `200m` zone fills at about 1.6 million
files — roughly 25 GB of 256-px tiles — so raising `max_size` beyond that
would add no capacity. The keys zone is therefore sized at 1 MB per 128 MB of
`max_size` (a 16 KB average file), never below `200m`: the stock `50g` gets
`400m`, `100g` gets `800m`, `500g` gets `4000m`. The zone costs RAM only as
it fills (an empty `400m` zone adds next to nothing to nginx's footprint); a
full one uses about its size.

**The keys zone is capped at a quarter of the host's RAM** (`MemTotal` in
`/proc/meminfo`), but never below `200m`. nginx maps the whole zone when it
reads its config — `nginx -t` included — and fails (`munmap(...) failed`,
`test failed`) when the zone is larger than the kernel will commit, roughly
RAM plus swap. Without the cap, a large persisted `max_size` (say `500g` on a
4 GB VM, which calls for `4000m`) would make every update's `install.sh` fail
`nginx -t`, put the old files back and stop, and `update.sh` roll back. With it, that host gets a zone of about `1000m` (a quarter of its
`MemTotal`, which a "4 GB" VM reports as a little under 4 GiB). Both
`nginx-cache.sh` (before you confirm, and in the status view) and
`install.sh` / `update.sh` (a warning naming `/etc/wsi/nginx-cache-max-size`,
repeated in the end-of-install summary) tell you when the cap applies. A capped
cache evicts once the zone is full (about 8,000 files per MB), before it
reaches `max_size`. To stop the warning, lower `max_size` with option 2 to
what the zone can index (about 128 MB of `max_size` per MB of zone).

`max_size` is a **soft** cap: the nginx cache_manager process prunes by
LRU in the background until on-disk usage falls under the limit. If you
shrink the cap below current usage, expect a delay before disk usage
drops; existing files keep serving HITs in the meantime. If you shrink
significantly and want immediate effect, pair the change with option
1 (Clear cache) afterwards.

### Examples

```bash
# Default interactive run (shows status, then menu)
sudo bash infra/nginx-cache.sh

# Targeting a non-default cache directory or config (rare)
sudo WSI_NGINX_CACHE_DIR=/srv/cache/wsi_dzi \
     WSI_NGINX_CONF=/etc/nginx/sites-available/wsi \
     bash infra/nginx-cache.sh
```

### Environment overrides

| Variable | Default | Meaning |
|---|---|---|
| `WSI_NGINX_CACHE_DIR` | `/var/cache/nginx/wsi_dzi` | Cache directory to clear / inspect |
| `WSI_NGINX_STATIC_CACHE_DIR` | `/var/cache/nginx/wsi_static` | Cache directory of the thumbnails / tour-attachment zone (`wsi_static_cache`), cleared together with the tile cache |
| `WSI_NGINX_CONF` | `/etc/nginx/sites-available/wsi` | Live nginx site config to edit |
| `WSI_NGINX_CACHE_SIZE_FILE` | `/etc/wsi/nginx-cache-max-size` | Persisted `max_size` override that `install.sh` / `update.sh` re-apply after a git checkout. They read only the default path, so a value written elsewhere is not re-applied |

### Publish state, deletion, replacement, and cache staleness

The shared edge cache keeps tile images for 30 days, and nginx serves a cache
HIT without asking the app's `/dzi/` routes. Two mechanisms keep that safe
when you **unpublish**, **delete**, or **replace** a slide, so none of these
actions needs a manual cache clear.

**Every `/dzi/` request — HIT or MISS — is authorized per slide at the edge.**
The `auth_request` subrequest to `/api/edge-authz` receives the original URI
and checks both the **app-access gate** (session cookie / admin bearer /
Cloudflare Access / an exemption / `mode=none`) and the **per-slide publish
gate**: the slide must exist and be published, or the request must carry a
staff bearer (the dashboard's draft preview). nginx answers a request the
publish gate refuses with `403` (one the app-access gate refuses with `401`),
cached tile or not. The check reads the app's in-memory slide cache (no
database query per tile), and an *allow* is cached by nginx for 60 s per
client **and slide** (denials are never cached). That verdict is keyed on the
resolved client IP and the session cookie as well as the slide, so an
IP-filter-denied client cannot ride an allowed client's cached verdict on
these shared surfaces.

**Tile URLs carry the slide's content generation.** Every WSI replace moves a
per-slide generation (`Slide.wsi_generation`) strictly upward — to the current
time in milliseconds, or the previous value plus one if that is larger — so
no number is ever handed to two different files, even after a backup restore
has put the old value back; the viewer requests
`/dzi/{public_id}.dzi?v=N` and OpenSeadragon adds the same `?v=N` to every
tile URL. nginx keys the tile cache on the path **plus `v`**, and the app
refuses (`404`, not cached) any `v` that is not the slide's current one, so a
cache entry can only ever hold the bytes of one generation.

What that means per action:

- **Unpublish / delete.** The catalog, the `/api/.../slides/{public_id}`
  metadata, and thus the normal viewer page **404 immediately**: those
  responses are never cached. (The one exception is the slide's macro
  (overview) image, `/api/slides/{public_id}/macro`, sent with `max-age=300`:
  a browser that already fetched it can keep showing it for up to 5
  minutes.) Tile and descriptor requests are refused at the
  edge **within 60 s** — the life of a cached allow verdict — for clients that
  had the slide open, and at once for everyone else, cached tiles included
  (plus up to `WSI_SLIDE_META_GEN_CHECK_SECONDS`, 1 s by default, for the
  app's other workers to see the change). Re-publishing restores access at
  once; the cached tiles are still there and serve again. A viewer that is
  **already open** can still redraw tiles its browser holds — a tile under the
  current `?v=` goes out as `Cache-Control: private, max-age=86400` (see
  "Browser caching of tiles" below) — for up to a day, but it fetches nothing
  new, and reopening the slide fails: the slide metadata and the descriptor
  are refused.

  The slide's **thumbnail** URL stops working **within 60 s**. `/thumbs/` has
  no per-slide check at the edge (only app access); instead, unpublishing
  renames the thumbnail file to a fresh random name and deleting the slide
  removes it, so every thumbnail URL handed out while the slide was public
  points at nothing. nginx keeps a fetched thumbnail for 60 s before asking
  the app again, so for up to that long the old URL is still answered from
  the edge cache; after that it is a `404`. (A browser that already showed the
  thumbnail may keep displaying its own copy.) Re-publishing serves the
  thumbnail under its new name.

  The slide's **tour attachments** (`/assets/`) are not revoked this way.
  `/assets/` also has only the app-access check at the edge (the tour editor
  previews a draft tour's images without a bearer), and unpublishing leaves
  the files where they are: the slide's tours turn into drafts, but an
  attachment URL keeps working for anyone with app access who has it.
  Deleting the slide removes its tours' attachment files, and nginx can
  answer an edge-cached copy for up to 1 h after that.

  A **live-sync session already running** on the slide is not torn down, but it
  stops being public: the slide disappears from the snapshot the discovery
  WebSocket sends (the index page's join banner) to any page that connects
  afterwards, a direct peek at its id 404s,
  and new followers are refused at the WebSocket. An index page that was
  already open keeps its banner until it reconnects, but joining from it is
  refused. If the presenter then switches the session to a published slide,
  the banner reappears on open index pages (the slide-change event adds a
  session the page did not list).
  Followers already attached keep receiving viewport frames — they just cannot
  fetch tiles, which the edge now refuses (within the 60 s above); tiles their
  browser already holds can still be redrawn, as for any open viewer.
  The presenter still sees the session in their own list, so **end it from the
  dashboard** if it should stop entirely.

- **Replace (`replace_wsi`).** Before the new file is moved into place, the
  old generation is retired on every worker (the replace waits
  `WSI_SLIDE_META_GEN_CHECK_SECONDS`, 1 s by default, for that — keep it
  small: every replace waits that long), and each tile handle records the
  generation current when its file was opened, so no tile of the new file is
  served — and edge-cached — under the old `v` even on a worker whose cache
  lags; a replace that fails and rolls back also leaves the slide on a fresh
  generation.
  The new file gets a new generation, so a viewer
  opened (or reloaded) after the replace requests new tile URLs and sees only
  the new scan — no mix of old and new tiles, even when the replacement has
  the same file name. A tab that was already open keeps requesting the old
  generation: tiles it had cached at the edge keep serving it the old scan, and
  anything else it asks for is a 404 until it reloads. The old generation's
  tiles stay on disk, reachable only by their exact old URLs (and only while
  the slide is published), until their cache entries expire: at most 30 days
  after they were stored (`proxy_cache_valid 200 30d`, `inactive=30d`), sooner
  if `max_size` evicts them.

- **Link-only (unlisted) toggle.** Changes nothing at the edge or in the app's
  caches: no gate reads that flag, so tiles, the descriptor and the thumbnail
  keep serving exactly as before; only the index, search and facet listings
  change, from their next request.

**Clearing the cache is only needed to reclaim disk space** — or, after a
replace, to make the *old* scan's tiles unreachable at the edge before they
age out (a wrong or sensitive image was uploaded and then replaced;
unpublishing would hide the new one too). It does not reach browsers: one
that viewed the old scan can keep its tiles for up to a day, reachable only
from a tab still open on the old generation. The app runs unprivileged and
the cache directory is owned by `www-data`, so the app cannot purge the cache
on its own; use option 1 above.

**Changing `DZI_TILE_SIZE` / `DZI_OVERLAP` needs no clear either.** It re-cuts
every pyramid under the same tile URLs, so on the first start with the new
values the app moves **every slide** to a new generation (the log says "Deep
Zoom tile geometry changed"): viewers opened afterwards request new URLs, and
neither the edge nor a browser can hand them a tile cut on the old grid. The
cost is the same as a clear — every tile is generated again on first view —
and a tab left open from before gets 404 tiles until it reloads. The old
tiles age out after 30 days (clear the cache if you need the disk space
sooner). The change is detected against the geometry recorded at the
previous start (`app_meta` key `dzi_tile_geometry`). `DZI_TILE_QUALITY` doesn't trigger this: tiles of another quality on
the same grid still draw correctly, and they are replaced as they age out.

A backup restore needs none. It puts every slide's generation back to the
backup's value, which is exactly the generation the restored file was served
under, and the next replace still jumps past every number used since (see
above). Likewise a slide imported from an export bundle that keeps its old
`public_id` starts at a fresh, never-used generation, so re-importing a
deleted slide needs no clear either.

A tile request without `?v=` (a script or other non-SPA caller) is still
served, but straight from the app and never stored. Because each one costs the
app a render, nginx meters them per client (zone `wsi_dzi_uncached` in
`wsi.conf`): after the first 100 in a burst they arrive at 10 per second, and
past 400 queued they are refused with `429`, so such a caller still gets its
tiles, slowly. Versioned requests are never counted.

A request that carries an `Authorization` header other than HTTP Basic — a staff
Bearer token (the dashboard's draft preview and the presenter's own tiles), or
anything that merely says it is one — is kept out of the shared cache both ways,
so the app renders its tiles every time. nginx cannot tell a valid token from
a junk one, so these are metered per client too (zone `wsi_dzi_authed`): 50 per
second with a burst of 600, far above what a person panning a slide sends, and
only the staff's own requests count (students carry no such header, so a class
behind one address is unaffected).

### When to clear the cache

- To **reclaim disk space**, or before lowering `max_size` if you want the new
  cap to be reached immediately rather than via background LRU.
- After a **replace**, only if the *old* scan must stop being reachable by its
  exact tile URLs before it ages out (see the section directly above).
- After changing `DZI_TILE_QUALITY`, if the new quality must show at once
  rather than as cached tiles age out. (`DZI_TILE_SIZE` / `DZI_OVERLAP` changes
  are handled by the app; see above.)
- When debugging tile rendering changes (so you compare fresh upstream
  responses, not cached bytes).

Unpublishing or deleting a slide does not call for a clear: the edge refuses
its tiles, and stops serving its old thumbnail URL, within 60 s.

### Browser caching of tiles

A tile under its slide's current `?v=` reaches the browser as
`Cache-Control: private, max-age=86400` (nginx sets it: the
`$wsi_dzi_tile_cache_control` map in `wsi.conf`); every other tile response
— one without `?v=`, or any error on a tile URL — says `no-cache`, and the
descriptor and `.szi` get the app's own `no-cache` on success (their error
responses, nginx's `401` / `403` included, carry no `Cache-Control`). Such a tile never changes under its URL (a replace or a tile
geometry change moves the generation), so for a day the viewer can redraw a
tile it evicted from memory without a round trip to the server. `private`
keeps a CDN or proxy in front from storing it for anyone else.

The trade-off: for up to a day a browser can redraw tiles it already holds
without the edge re-checking access — after an unpublish, a logout or an IP
filter change. It can't reopen the slide (the slide API and the descriptor
are refused), and it fetches nothing new. The app itself still sends
`no-cache` on every tile; the policy lives in nginx only, so rolling back the
nginx config also rolls it back for tiles already in the edge cache.

### When to change `max_size`

- Disk pressure on `/var` — shrink the cap.
- Cache hit rate is low and disk has room — grow the cap.
- The default is `50g`; size it to comfortably exceed the working set
  of tiles for the slides you serve frequently.

---

## Uvicorn worker count — `infra/set-workers.sh`

`infra/set-workers.sh` interactively updates `UVICORN_WORKERS` or
`UVICORN_WORKERS_PERCENT` in `/etc/wsi/wsi.env` and optionally restarts
the service.

### Background

The FastAPI backend runs as a multi-process Uvicorn server.
`start-wsi-app.sh` decides the worker count at startup using this logic:

1. If `UVICORN_WORKERS` is set, use that value directly (explicit count).
2. Else if `UVICORN_WORKERS_PERCENT` is set, compute
   `floor(CPU_cores × UVICORN_WORKERS_PERCENT / 100)` (no upper cap).
3. Otherwise apply the default formula `min(max(2, cores/2), 8)`, then cap
   it by the RAM budget so each worker can still hold a useful DZI byte
   cache: `min(formula, budget / WSI_CACHE_FLOOR_BYTES)`. This stops a
   many-core / low-RAM host from spawning more workers than RAM can back
   (e.g. 16 cores with a `MemTotal` of 16 GiB → 6 workers, not 8; a "16 GB"
   machine usually reports a little less, which gives 5). The RAM cap applies only
   to this default path; options 1 and 2 bypass it. Setting
   `WSI_CACHE_FLOOR_BYTES=0` means "no floor" and disables this cap
   (the formula result stands); a floor larger than the whole budget
   leaves one worker. A value too large for 64-bit arithmetic
   (≥ 2^63) is treated as malformed and the built-in default applies.
4. Result is clamped to `[1, nproc]`.

The maths is shared with `set-workers.sh` via `infra/systemd/wsi-calc.sh`,
so the interactive preview matches what the service does at startup.

The 8-worker default cap reflects that:

- Most tile traffic is absorbed by the nginx disk cache (`max_size=50g`),
  so upstream concurrency is rarely the limiting factor.
- OpenSlide reads and Pillow JPEG encoding both release the GIL, so a
  single worker can usefully drive multiple cores via threads.
- Uvicorn workers are async — one process serves many concurrent
  connections.
- Past ~8 workers the bottleneck shifts to disk I/O, while each extra
  worker still duplicates the per-process DZI cache.

Operators who genuinely need more workers (e.g. CPU-bound transcoding
profiles) can opt in via `UVICORN_WORKERS` or `UVICORN_WORKERS_PERCENT`,
which bypass the default cap.

The worker count **cannot be changed without restarting the service** —
Uvicorn forks worker processes at startup and there is no hot-reload
mechanism for process count.

### Interaction with DZI_CACHE_SIZE

Each worker is an independent Python process with its own WSI tile
cache. The total effective cache across the server is:

```text
total cache entries = UVICORN_WORKERS × DZI_CACHE_SIZE
```

The same slide may be cached in multiple workers simultaneously (no
shared cache). The duplication cost is mostly OpenSlide handle state
(tens of MB per handle), not the slide files themselves — the OS page
cache is shared across processes for the same file on disk, so reading
the same WSI from N workers does not multiply page-cache occupancy.

The per-worker **byte** cap (`DZI_CACHE_MAX_BYTES`) interacts with the
worker count in the opposite direction: when auto-derived, the RAM budget
is split across workers, so **fewer workers means a larger byte cache
each**. Changing `UVICORN_WORKERS` therefore also changes the auto byte
cap (unless you pin `DZI_CACHE_MAX_BYTES`); `set-workers.sh` shows both
together.

### Usage

```bash
cd ~/wsi
sudo bash infra/set-workers.sh
```

The script shows:
- current values of both `UVICORN_WORKERS` and `UVICORN_WORKERS_PERCENT` from the env file
- the effective worker count that would be used at next start
- the live worker count of the running process

It then asks whether to set an explicit count or a percentage, validates
the input, shows a preview, confirms before writing (the other variable is
commented out if present), and offers an immediate restart with a health
check. The script has no "back to default" choice: to return to the
RAM-aware default formula, comment out both lines in `/etc/wsi/wsi.env`
and restart `wsi-app.service`.

Run it from your clone (`~/wsi`), as above. Like `set-cache-size.sh` it runs
as root and executes code from its own tree (`infra/systemd/wsi-calc.sh`,
`infra/lib/envfile_set.py`), so it refuses to run from the deployed copy under
`/opt/wsi`, which the service user owns, or when the script, its directory
or the code it runs is owned by an account other than root or you, or is
writable by every user.

To target a non-default env file:

```bash
cd ~/wsi
sudo WSI_ENV=/path/to/other.env bash infra/set-workers.sh
```

### Examples

| Goal | Recommended setting |
|---|---|
| Accept the default cap (most installations) | leave both unset → `min(max(2, cores/2), 8)`, capped by RAM |
| Pin to a fixed count regardless of CPU | `UVICORN_WORKERS=4` |
| Exceed the 8-worker cap on a many-core host | `UVICORN_WORKERS_PERCENT=50` (16 cores → 8, 24 → 12, 32 → 16) |
| Single-core or low-memory server | `UVICORN_WORKERS=1` |

---

## Database connection pool

Each uvicorn worker holds its own SQLAlchemy connection pool to the SQLite
file. A sync request checks out one connection for the duration of its DB
work, so the pool's total capacity (`pool_size + max_overflow`) bounds how
many DB-touching requests a worker can serve at once before the rest start
waiting.

### The failure mode this sizing prevents

SQLAlchemy's built-in default pool is `pool_size=5 + max_overflow=10 = 15`
connections, with a 30-second checkout timeout. A single worker, however,
runs sync endpoints on the anyio threadpool, which admits up to
`WSI_REQUEST_THREADPOOL_TOKENS` (default 40) concurrent requests. When a
crowd arrives at once — e.g. a class all opening slides on a cold nginx
cache — the app is flooded with tile requests (each `/dzi/` request does a
`public_id → PK` lookup), analytics pageviews, and viewer-settings reads.
With more concurrent requests than pooled connections, the excess block on
connection checkout and, once the crowd sustains past the timeout, fail
with:

```text
sqlalchemy.exc.TimeoutError: QueuePool limit of size 5 overflow 10 reached,
connection timed out, timeout 30.00
```

which surfaces as `wsi_app.unhandled` 500s across `/api/viewer/settings`,
`/api/analytics/pageview`, `/api/slides`, and the tile endpoints.

### How it is sized

The defaults keep the pool capacity **above** the per-worker request
concurrency, so a connection is always available for an admitted request,
and the concurrency ceiling is pinned explicitly at startup rather than
left to the library default:

| Env | Default | Meaning |
|---|---|---|
| `WSI_DB_POOL_SIZE` | `20` | Persistent connections kept open per worker (values below 1 are raised to 1, with a warning) |
| `WSI_DB_MAX_OVERFLOW` | `60` | Extra connections opened on demand (capacity = 20 + 60 = 80). `-1` removes the cap (SQLAlchemy's "no limit"); any other negative value is raised to 0, with a warning |
| `WSI_DB_POOL_TIMEOUT_SECONDS` | `30` | How long a request waits for a connection before erroring (values below 1 are raised to 1, with a warning) |
| `WSI_REQUEST_THREADPOOL_TOKENS` | `40` | Max concurrent sync endpoints per worker (kept below the 80 capacity; values below 1 are raised to 1, with a warning, since 0 would hang every sync endpoint) |

SQLite connections in WAL mode are cheap — concurrent readers plus a single
serialised writer (writes still serialise on `busy_timeout` regardless of
pool size) — so the generous pool costs little memory. Keep
`WSI_REQUEST_THREADPOOL_TOKENS` below the pool capacity; a saturated worker
then queues excess requests cheaply at the scheduler instead of blocking on
connection checkout. If you raise the token count past the capacity the app
logs a startup warning and degrades to blocking on connection checkout rather
than refusing to boot. These are per-worker figures: total connections
across the server are `UVICORN_WORKERS × capacity`, which SQLite handles
comfortably.

---

## Slide-metadata resolve cache

The connection-pool sizing above widens the receiving end; this cache
removes most of the load that would otherwise hit it.

### The amplifier

Every `/dzi/` request (tile or descriptor) — and the nginx edge
authorization check in front of it — resolves the unguessable `public_id` to
the internal `(slide_id, is_published, wsi_generation)` with a DB query.
Viewing one slide at full zoom fans out into dozens–hundreds of tile
requests, so the DB read count is roughly:

```text
reads/sec ≈ concurrent viewers × tiles-per-second-per-viewer
```

Under a cold nginx cache (exactly the mass-access case), that lands on the
app as hundreds of near-identical reads per second — even though the data
is effectively constant: `public_id` is immutable, `is_published`
changes only when an admin toggles it, and `wsi_generation` (the tile URLs'
`?v=`) only on a WSI replace. Caching the resolution in-process
collapses those to about **one read per slide per worker per TTL**
(e.g. 200 tile requests for one slide within the TTL cost one DB read per
worker).

### Keeping it correct across workers

Reading the row live on every request would ensure a
publish/unpublish/delete on one worker is seen by the others (each worker
has its own cache; there is no shared memory). That correctness is
preserved without the per-request read via a filesystem **generation
sentinel**:

- On any publish/unpublish/delete/WSI replace, the handling worker flushes
  its own cache immediately and bumps `"<DATA_DIR>/cache/slide_meta.gen"`.
- Every other worker re-reads that sentinel at most once per
  `WSI_SLIDE_META_GEN_CHECK_SECONDS` (a cheap page-cached file read, never
  a DB round-trip) and flushes when the token changed.

So a just-hidden or deleted slide stops being served everywhere within the
check interval (≤ ~1s by default) — **faster than, and independent of, the
TTL**. The TTL is only a backstop for a missed sentinel bump.

### Knobs

| Env | Default | Meaning |
|---|---|---|
| `WSI_SLIDE_META_CACHE_TTL_SECONDS` | `300` | Per-entry cache lifetime; `0` disables the cache (restores read-live) |
| `WSI_SLIDE_META_GEN_CHECK_SECONDS` | `1` | Max propagation delay of a publish/delete to *other* workers |
| `WSI_SLIDE_META_CACHE_MAX` | `4096` | Cap on cached entries per worker (positive resolutions only) |

Notes:

- Unknown `public_id`s are never cached, so a flood of guessed tokens
  cannot evict live entries (and still can't outrun the widened pool).
- A single-worker deployment (`UVICORN_WORKERS=1`) has no other workers to
  propagate to, so publish/delete is always instant there regardless of the
  check interval.
- A WSI replace flushes this cache too, because it moves the slide's tile
  generation: the new `?v=` is current on the handling worker at once. A
  worker that has not seen the sentinel yet re-reads the row when a request
  carries a generation *newer* than the one it cached, so a viewer that
  reloads right after a replace is never refused. (Which bytes a worker
  serves is handled separately: the DZI handle cache re-`stat`s the file.)
- A `.szi` archive request does not use this cache: `/dzi/{public_id}.szi`
  reads the slide row live on every `Range` request (it is not edge-cached
  either), so an unpublish or delete refuses it at once. Its edge
  authorization check does use the cache, like every other `/dzi/` request.

---

## Performance measurement — admin Performance page

The admin dashboard's **Performance** page (`/admin/performance`, also linked
from Server Info) measures how fast this install serves slides and keeps every
result in a history, so a tuning change — worker count, cache sizes, storage,
network — can be checked with a before/after comparison instead of guessed.
Anyone who may see the Server Info page may read the results; only `admin` can run
(or cancel, or delete from the history) a measurement, because both tests load
the server.

### Server test

Runs inside the app worker that received the request, **below nginx** — so it
measures what a tile costs when nginx has *not* cached it yet:

| Phase | What it measures |
|-------|------------------|
| Slide open | Time to open the slide (OpenSlide / OME-TIFF reader / SZI archive) plus the Deep Zoom generator. Opened three times: the first open (from disk when **Cold cache** is ticked) and the median of the other two |
| Disk | Sequential read speed of the slide file (its first 256 MiB at most, in 4 MiB reads) and the latency of 200 random reads of up to 64 KiB |
| Tiles | Random tiles at three zoom levels (full resolution, 1/4, 1/16) — 30 per level by default (5–200) — split into generation (read + composite + resample) and JPEG encoding — the same work as a `/dzi/…/<col>_<row>.jpeg` cache miss. For SZI a tile is one archive read |
| Parallel requests | Random full-resolution tiles rendered by 1, 2, 4, … threads up to the chosen maximum (default: the CPUs the app may actually use — the affinity mask limited by a container's CPU quota — at most 8; a chosen value is capped at twice that, at least 4 and at most 32), for 3 s per step by default (1–10 s); tiles/s, latency, CPU busy / I/O wait and the share of tiles not rendered earlier in the run ("new tiles": a low share means the step mostly measured warm caches) per step. SZI slides are left out of this phase when other slides are measured |

Up to three slides can be chosen (default: the largest slides). **Cold cache**
evicts the slide file from the OS page cache first (`posix_fadvise DONTNEED`,
no root needed), so the disk rather than memory is measured. Those pages are
shared with the viewers, so the next student to open the same slide reads it
from disk again.

Safeguards: one run at a time across all workers (a partial unique index on
`perf_runs`); no run starts while a backup / restore / slide transfer is in
flight or a restore / reset is waiting for the restart that applies it (HTTP
409 `jobRunning` either way), and none of those starts while a run is in progress (HTTP 409
`perfRunActive` — cancel the run or wait, at most about 4 minutes); a limit of 240 s (a parallel step whose threads are stuck in a read on
failing or network storage gets 30 s more, is then abandoned and the run
recorded as cut short — its slide handles are closed only once those threads
return); a Cancel button that any worker can deliver (the run polls its row's
cancel flag). The benchmark opens its own slide handles and closes them
afterwards — it never touches the per-worker handle cache, so it cannot close
a slide a class is viewing. It does share the OME-TIFF reader's per-process
decoded-segment cache (`WSI_OME_TILE_CACHE_BYTES`): measuring an OME-TIFF
slide can push out segments of slides being viewed on that worker, which are
then decoded again on demand. Tiles that need a block read (a slide without
a pyramid level close enough) go through the benchmark's own render slot (a
new one per run), not the one the worker's viewers share, so a measurement
never waits in their queue nor fills it — their block-read tiles are not
refused because of it. It still competes with them for the CPU, the disk and
memory (a second such render in the worker while it runs), which is why it
belongs in a quiet period. A tile that fails to render is counted per zoom level; only a
reader that fails for good stops that slide, keeping the levels measured so
far. If the worker running a measurement dies, the
next request to the Performance page marks the stranded row failed
("interrupted"); so does one for a run still marked running after 540 s
(twice the limit plus a minute), whatever its process looks like. The history keeps
the newest 100 server tests and, separately, the newest 100 browser tests.

All server-test figures are **below HTTP**: they leave out the database
lookup, the file response and nginx that a real tile request also pays (the
result says so). CPU busy is measured for the CPUs the app may use: host-wide
when it may use them all, else from the container's cgroup when a cgroup CPU
quota is the limit, or from the process's own CPU time when only an affinity
mask (`taskset`, a cpuset) limits it — the cgroup's usage would count other
processes not held to those CPUs (shown as "CPU busy measured as").

### Browser test

Runs in the admin's browser and measures what a viewer on that device and
network experiences: API round-trip time (10 authenticated pings), download
speed (incompressible payload: a 1 MiB probe, then two downloads sized from it
to take about 1.5 s each, at most 32 MiB; the faster of the two is reported),
and Deep Zoom tile requests through nginx at the same three zoom levels (60
tiles by default, 10–500, split across the levels), with a chosen number of
parallel requests. Two tile paths:

- **Generated by the app** — requests carry the admin token, which nginx
  neither answers from nor writes to its tile cache
  (`proxy_cache_bypass` / `proxy_no_cache $wsi_auth_nocache`, set for a
  Bearer or any other non-Basic `Authorization` header), so every
  tile is a cache miss and the shared cache is left untouched. The token also
  bypasses nginx's cached access verdict, so each tile costs one extra
  `/api/edge-authz` round trip that a student's cache miss does not: the
  figures are slightly pessimistic. A fresh tile sample is drawn on every run
  — re-fetching the same tiles would be answered from the OS page cache and
  OpenSlide's own tile cache and look faster each time.
- **Normal path** — plain requests, as a student sends them (same URLs, with
  the slide's `?v=` tile generation, so they hit the entries viewers use). Each tile's
  `X-Cache-Status` (HIT / MISS / …) is recorded; the tiles it fetches are
  cached afterwards, so this doubles as a pre-warm. Published slides only. On
  an installation that requires login, the admin must also be signed in to
  the viewer in the same browser (the tile gate checks the viewer session, not
  the admin token). Behind Cloudflare Access nginx never caches tiles, so
  every tile reports BYPASS. The sample is fixed per slide and tile count, so
  a second run shows how much the cache now answers.

Parallel requests are capped at 6: a browser opens only six HTTP/1.1
connections to one server, so more would only queue inside the browser and
be counted as tile time. SZI slides (read by the browser straight from the
archive) are not offered. Per-tile times come from the browser's Resource
Timing (from the moment the request left the browser's own queue), and the
download speed counts only the body transfer, not the round trip before the
first byte. A zero-length body reading from Resource Timing is not believed:
that download keeps its `fetch()` time, round trip included, and a tile
without a Resource Timing entry keeps its `fetch()` time, which includes the
wait for a browser connection. Comparing two runs timed differently says so;
download speed and tile response time are the figures that differ most.
Failed tiles, a tab hidden during the test, and a server test or
background job that ran at the same time are recorded and shown as caveats in
the history and when comparing runs. The app path loads the server much like the server
test's parallel phase — run it when few people are using the viewer too.

### Reading the results

The server test ends with plain-language findings:

| Finding | Meaning / what to do |
|---------|----------------------|
| Throughput levels off at N parallel requests | One worker's capacity for cache-miss tiles (N is the first step that reaches 90 % of the peak rate). Several workers multiply it only while CPU cores are left. Only shown when more than one parallel step ran |
| CPU saturated at peak (85 % busy or more at the peak step) | More workers will not help on this hardware; heavily used slides can be converted to `.szi` (no generation) and pre-warmed |
| Parallel barely beats one, CPU mostly idle (peak under 1.5× the one-thread rate, CPU under 60 % busy) | One process cannot use the other cores for this format — raise `UVICORN_WORKERS` with `infra/set-workers.sh`. Not shown when the disk looks slow, I/O wait explains the stall (a fifth of the peak step's threads' worth of CPUs or more), or there are already as many workers as cores |
| Slow sequential reads (under 100 MB/s), slow random reads (p95 over 20 ms), high I/O wait (average over 20 %, or at least one CPU's worth) | Storage-bound (HDD or network storage): prefer SSD, and pre-warm slides before heavy use so nginx serves them |
| Slow full-resolution tiles (p95 over 500 ms) | Viewers panning into unseen areas will see blank tiles briefly; pre-warming and `.szi` help |
| N of M parallel tile renders failed | The throughput counts only the tiles that succeeded; the server log has the reader error. If *every* parallel render fails the run is recorded as failed. An OpenSlide reader that hits a read error fails every later call, so it is dropped from the phase after its first one; once no reader is left the phase stops and the run is marked cut short |

I/O wait is a share of the whole host, so it is also judged as "CPUs' worth
of waiting" — on a many-core host a few threads blocked on a disk are a small
percentage but still the bottleneck. A run that was cancelled, cut short,
failed, or lost tiles to errors — parallel renders or single-tile (per zoom
level) ones — never reports "no bottleneck", and comparing two runs warns
when their settings differ, one of them was cancelled, or one was cut short,
lost renders or overlapped a backup / restore / transfer — the history row
says which. Only finished or cancelled runs of the same kind (two server
tests or two browser tests) can be compared; a failed run cannot be picked.

Typical workflow: run the server test (warm, then cold), change one setting
(e.g. `set-workers.sh`, `set-cache-size.sh`, moving the data directory to an
SSD), restart, run again, tick both runs in the history, and read the
comparison. Measure when few people are using the viewer — the numbers include whatever else
the server is doing, and the test itself adds load.

### Exporting results with their conditions

A number is only comparable with another taken under the same conditions, so
every run records them and every export carries them:

- **Server side** (both tests): host, OS / kernel / virtualization, CPU model,
  cores and clock, memory, container CPU / memory limits, worker count, data
  disk (SSD / HDD, filesystem), Deep Zoom tile size / overlap / JPEG quality,
  the per-worker handle and OME caches, the nginx tile cache (size limit and
  current use), the viewer's rendering settings (immediate render, tile cache
  count, max zoom), and versions. A server test also records the load average
  when it started, so a run taken while the server was already busy shows it.
  For a browser test the server stamps this snapshot when the result is saved.
- **Browser side** (browser test): when it ran and for how long, the user
  agent and platform, its language and touch-point count (iPadOS Safari
  reports a Mac user agent, so this is what tells an iPad from a Mac),
  window / screen size and device pixel ratio, logical
  cores and device memory, the network as the browser reports it (type,
  effective type, downlink, RTT, save-data), the page origin, the HTTP
  protocol the tiles actually used (from Resource Timing: `http/1.1`, `h2`,
  `h3`; a tile the browser reported nothing for counts as `unknown`, so the
  counts always add up), and whether the tab went to the background during the run (browsers
  throttle background tabs; such a run is flagged on the page). The slide's
  Deep Zoom geometry (size, tile size, overlap, format, levels) and every
  tile request (start time, zoom band, level, column / row, first byte, total,
  bytes, `X-Cache-Status`, protocol) are kept as well.

The run's result view shows these under **Conditions** / **Environment**.
**Export (JSON)** on a result downloads that run; **Export history: CSV /
JSON** above the history downloads every stored run (oldest first):

- **JSON** (`GET /api/admin/perf/export.json[?ids=1,2,…]`) — every recorded
  value of each run, in an envelope (`format: tessera-perf-export`,
  `format_version`, `exported_at`, `exported_by`, the exporting server's
  version and host name) with a `units` table, so the file can be read without this
  installation.
- **CSV** (`GET /api/admin/perf/export.csv[?ids=…]`) — one row per run: its
  settings, headline numbers, the server's CPU / I/O-wait / memory during a
  server test, and the conditions above flattened into columns (`server.*`,
  `viewer.*`, `client.*`; a server test's failed single-tile and parallel
  renders in `server.band_errors` / `server.tile_errors`, empty for a run
  that has no such record). UTF-8 with a BOM for Excel;
  browser-reported text is defanged against spreadsheet formulas like the
  other CSV exports — each `;`-separated part on its own, as a spreadsheet
  that splits on `;` would see it.

Exports follow the read permission of the history (Server Info access). A
condition the browser did not report leaves that field empty.

---

## Audit history — `/admin/history`

Every administrative action (slide upload/delete/replace, tour edits,
settings changes, user management, backup/restore, logins, and app
version upgrades) is recorded twice:

- **journald** — a JSON line on the `wsi_app.audit` logger
  (`journalctl -u wsi-app -u wsi-sync | grep admin_audit`; live-sync
  session actions are logged by `wsi-sync`). This is the primary,
  tamper-evident record.
- **The `audit_logs` table** — a queryable copy shown on the
  **Audit History** page at `/admin/history` (admin role only), with
  filters for category, user, target, "System only" (events with no user),
  and date range. The user filter matches part of a name, case-insensitively,
  and also finds failed sign-ins *against* that account (their attempted name
  is the target, not the user).

The list is read from a snapshot taken when its first page loads, so events
recorded while you page through it don't shift the later pages; a banner
counts the events recorded since, and **Refresh** (or the banner's button)
starts over from a fresh snapshot.

The one exception is the app version upgrade (`system.version_upgrade`, written
at the first boot of a new version): it goes to the table only, and journald
has just the `app_meta: stamping app_version` INFO line for it.

The page also has an **Export CSV** button that downloads the audit log
(honouring the active category / user / target / System only / date-range
filters, not just the
on-screen page) for offline archiving or analysis. As with the Analytics
export, the file is UTF-8 with a BOM so Excel on Windows renders
multibyte text correctly, and cell values that begin with a formula
character (`=`, `+`, `-`, `@`, TAB, CR) are prefixed with `'` to neutralise
spreadsheet formula injection. The download follows the retention row
cap (`WSI_AUDIT_LOG_MAX_ROWS`) so it never streams more than the table is
allowed to keep, and is in turn bounded by an absolute safety ceiling of
1,000,000 rows so that disabling the row cap (`WSI_AUDIT_LOG_MAX_ROWS=0`,
unbounded table) can't stream an unbounded result set into memory. If you
retain more than a million audit rows and need the full set, query the
database directly rather than through the CSV export. The rows are read in
short batches, so a long download holds neither a database connection nor a
read transaction for its whole length, and each worker runs at most two
history / error-log exports at a time (a third is answered `409` with "another
export is already running" until one finishes). The slot is given back as soon
as the app has finished sending the file — through nginx, which buffers admin
responses, that is usually well before a slow browser has it all — and
straight away when the client goes away mid-download. An export in progress
shows the size received so far and can be cancelled.

Retention of the DB copy is capped by two environment variables in
`/etc/wsi/wsi.env` (the journald record is unaffected):

- `WSI_AUDIT_LOG_MAX_ROWS` (default `100000`) — keep at most the
  newest N rows; `0` disables the row cap.
- `WSI_AUDIT_LOG_MAX_AGE_DAYS` (default `730`) — drop rows older than
  N days; `0` disables the age cap.

Failed sign-in rows (`admin.login.failure`, `app.login.failure`, and the
two-factor sign-in's `admin.login.mfa_required` / `admin.login.mfa_failure`)
are capped separately as well: only the newest 10,000 of them are kept, on top of the
caps above. They are unauthenticated and attacker-generated, so without their
own cap a password spray from many addresses could fill the shared row cap and
push real admin actions out of the history. This cap is fixed (not an
environment variable).

Caveats:

- Restoring a backup replaces the whole SQLite DB, so the in-app
  history rolls back to the backup's snapshot. Use journald when you
  need the unabridged trail across restores.
- Failed login attempts record the *attempted* username, which may
  contain a mistyped password. On the admin sign-in, a name that is not an
  account is stored reduced to its first character and length (e.g.
  `T… (34 chars)`); a name that is an account is kept as typed. The viewer
  sign-in (`app.login.failure`) cannot tell — under LDAP the directory, not the
  app, knows the accounts — so it stores the name as typed. The history page
  is admin-only for this reason.

---

## Error logs — `/admin/logs`

The audit trail above records *intentional* administrative actions. Its
counterpart, the **Error Logs** page at `/admin/logs` (**admin role only**),
records what went *wrong*: every `WARNING`-or-worse record emitted by the
application's own loggers, so an operator can triage a fault from the browser
without shell access to journald.

As with the audit trail, journald remains the primary, unabridged record; the
`error_logs` table is a queryable convenience copy. Both services capture into
it, so the journald side of a given row is under `journalctl -u wsi-app` or
`-u wsi-sync` depending on which process logged it — while the table itself
holds both. Both surfaces are gated at `admin` because a message or traceback
can embed request paths, identifiers, and other operational detail.

**What is captured.** The handler is attached to the `wsi_app` parent logger,
not to the root logger. Every application logger is named `wsi_app.<area>` and
propagates, so one handler catches them all — while `sqlalchemy.*` and
`uvicorn.*` records are structurally excluded. That exclusion is deliberate:
the handler's own INSERT goes through SQLAlchemy, and capturing SQLAlchemy's
logs would risk a write → log → write loop.

**How it is written.** A `logger.exception(...)` typically fires from inside an
`except:` while the request transaction still holds the SQLite writer lock, so
the record is *not* inserted on the request thread. The `wsi_app` logger gets a
non-blocking `QueueHandler`, and a background `QueueListener` thread drains the
queue and performs the INSERT, serialised per worker. A capture failure is
reported on a logger outside the `wsi_app.*` tree so it cannot feed back into
the queue.

Startup is covered too. While a worker sets up the schema (downgrade guards,
migrations, index and drift checks, the admin bootstrap) the `error_logs`
table may not exist yet, so records from that phase are held in memory and
written as soon as the capture starts, right after it — ahead of the startup
banners and janitors, which log straight into the table. Every row carries the
time its record was logged (to the second), not the time of the INSERT, so it
lines up with its journald line and with the audit history even when the write
waited for the database. Only warnings raised while the modules are first
imported (e.g. a malformed `WSI_*` value) reach journald alone.

**Floods are bounded.** A message is rendered when it is logged (so the row
reads exactly like the journald line), and the queue holds at most 10,000
records per worker: past that, records are dropped, and a `WARNING` row of its
own ("N log record(s) were not stored: the error-log queue was full") says how
many once the backlog clears.
Each call site — logger, level, source line and message template, plus the
exception type and the innermost line *of the application* it came out of when
there is one (so two different bugs surfacing through the same
unhandled-exception handler stay apart, even when both errors are raised inside
a library such as the database driver) — may
store at most 5 rows per minute in each worker process; the rest are counted and noted on the next row that
gets through (`[N more record(s) from this call site were not stored …]`). A
fault repeated on every request (a broken slide viewed by a whole class) thus
cannot push every other error out of the row cap. journald still has every
line. Separately, a slide that will not open is logged in full once per minute
per slide, and as a one-line DEBUG record in between.

Oversized fields are truncated rather than dropped — messages at 8 KiB,
tracebacks at 16 KiB (journald keeps the full text either way). SQL bound
values are never stored: the engine runs with `hide_parameters=True`, and any
`[parameters: …]` line that still appears in a message or traceback is replaced
before the INSERT, so a failed statement cannot copy a secret (e.g. the LDAP
bind password) into the table, its CSV export or a backup.

**The journald side.** The app attaches its own stderr handler to the
`wsi_app` logger at import, so every `wsi_app.*` record at INFO and above —
including the `admin_audit` lines — reaches journald in the uvicorn-style form
`LEVEL:     logger.name: message`. If the root logger is configured (a custom
`uvicorn --log-config`, for example), that handler stays silent and the records
go to the root handlers instead, so nothing is printed twice. A request that
fails with an unhandled exception is printed once, by uvicorn's own
`Exception in ASGI application` traceback; the `wsi_app.unhandled` copy goes to
the table only.

**Filters and export.** The page filters by level, logger name (exact, or a
prefix ending in `.`), a search term (matched in the message, the traceback and
the request path), and an inclusive `since` / `until` date range interpreted in
`WSI_ANALYTICS_TIMEZONE` — the same convention as the audit history filter.
Above the table, the row count per level (under the other filters) doubles as a
level filter; rows logged since this browser last opened the page are marked
**NEW**. Tracebacks are loaded when a row is expanded, not with the list. As on
the audit history, the list is read from a snapshot, with a banner for the rows
logged since and a **Refresh** button.

**Export CSV** honours the active filters and, like the audit export, is UTF-8
with a BOM and neutralises leading formula characters (`=`, `+`, `-`, `@`, TAB,
CR). Untick **Include tracebacks** for a much smaller file (the column stays,
empty). Its row ceiling follows `WSI_ERROR_LOG_MAX_ROWS` and is in turn bounded
by the shared absolute ceiling of 1,000,000 rows, so disabling the retention cap
cannot stream an unbounded table into memory; it is read in batches and limited
to two concurrent exports per worker, as described for the audit export.

**Clear logs** deletes the stored rows up to the newest one in the current list
(whatever the filters), after a confirmation; a row logged after the list was
loaded is kept. Only the table copy goes — journald keeps every line — and the
clear is itself recorded in the audit history as `system.error_logs.clear`. It
also leaves one `INFO` row in the log (logger `wsi_app.error_log.clear`, "Error
log cleared by …"): besides saying why the list is short, it keeps row ids from
starting over at 1, which the list's snapshots and the NEW marks rely on (for
the same reason the age-based retention never deletes the newest row of either
table). Clearing again when nothing but such markers is left does nothing.

The NEW marks compare against the newest id this browser saw last time. A
restore or reset that empties the table restarts ids at 1; the marks notice
this only when the next visit sees ids below the remembered one, so right after
such a restore they can be off until the page has been opened once.

**Configuration** (all in `/etc/wsi/wsi.env`). These are read at process
start, and `wsi-sync.service` runs the same application — so it installs its
own capture handler and needs restarting too for a change to apply everywhere:
`sudo systemctl restart wsi-app.service wsi-sync.service`.

| Variable | Default | Purpose |
|---|---|---|
| `WSI_ERROR_LOG_ENABLED` | `1` | Kill switch. `0` installs no handler at all — the table simply stays empty and journald is unaffected. |
| `WSI_ERROR_LOG_LEVEL` | `WARNING` | Minimum stdlib level captured. Raise to `ERROR` on a noisy site. |
| `WSI_ERROR_LOG_MAX_ROWS` | `50000` | Keep at most the newest N rows; `0` disables the row cap. |
| `WSI_ERROR_LOG_MAX_AGE_DAYS` | `90` | Drop rows older than N days; `0` disables the age cap. |

Retention is enforced by a janitor that runs at boot and opportunistically
thereafter (at most hourly per worker), exactly like the audit-log janitor.
The defaults are deliberately shorter than the audit caps: error rows churn
faster and carry less compliance value.

Caveats:

- **`error_logs` travels in a backup archive unless "Access statistics" is
  unchecked.** Its rows can contain request paths, identifiers and visitor IPs,
  so they are emptied together with the analytics tables (see "What's in an
  archive"); the audit-history checkbox does not affect them. Take that into
  account before sharing an archive off-site.
- **A Reset or a restore clears it**, the same as the audit history: it is not
  in the reset preserve-list, and a restore swaps the whole DB.

---

## nginx access log

The WSI site logs its requests with its own `access_log`, in
`/etc/nginx/snippets/wsi-access-log.conf`, which the site config
(`/etc/nginx/sites-available/wsi`) includes in its server block. By default
it holds:

```nginx
access_log /var/log/nginx/access.log wsi_combined;
```

`wsi_combined` (defined in `infra/nginx/wsi.conf`) is nginx's stock
`combined` layout with one change: the one-hour download token of the admin
download routes (`?t=<jwt>`, also spelled `?%74=`) is written as
`t=[redacted]`. Tools that parse `combined` (goaccess, fail2ban, log
shippers) read it unchanged.

**Effect on the host's own logging.** An `access_log` at server level
*replaces* the one inherited from the `http` block of `/etc/nginx/nginx.conf`
rather than adding to it, so on a host whose `nginx.conf` sets
`access_log off;` (no request logging, say for privacy), a JSON format or a
syslog target, the WSI site still logs to `/var/log/nginx/access.log` in
`combined` layout. Other sites on the host are not affected.

**Keeping your own logging.** Edit `/etc/nginx/snippets/wsi-access-log.conf`
— not the site config, which every `install.sh` / `update.sh` run replaces.
`install.sh` creates the snippet only when it is missing and never
overwrites it (a symlink there is left alone too), so your version survives
updates. For example, replace its `access_log` line with **one** of these
(`my_json` stands for a `log_format` of your own; `log_format` is allowed
only in the `http` context, so define it in a file under `/etc/nginx/conf.d/`,
not in this snippet, which sits inside the server block):

```nginx
access_log off;                                       # no access log for this site
access_log syslog:server=unix:/dev/log wsi_combined;  # to syslog, token masked
access_log /var/log/nginx/wsi.json my_json;           # your own format
```

then `sudo nginx -t && sudo systemctl reload nginx`. A format of your own
should use `$wsi_log_request` where it would use `$request`, so the token
stays masked; `$request` (and `$request_uri`, `$args`, `$arg_t`) carry it in
clear. A line that refers to a format nginx does not know fails `nginx -t`,
and an update then stops and puts the previous nginx files back. To get the
default back, delete the file and re-run `install.sh` (or copy
`infra/nginx/wsi-access-log.conf` from the clone). If you add a second
server block (a TLS vhost) that includes `wsi-locations.conf`, include
`wsi-access-log.conf` in it too.

nginx's **error** log has a fixed layout and still shows the token for a
download that hits an error (upstream timeout, rate-limit refusal); see
[Downloading and deleting](#downloading-and-deleting).

---

## Access analytics

### What is recorded, and when

Nothing at all while the privacy mode (below) is **off**. Otherwise:

- **Page views** (`page_views`): one row per route the public app shows — the
  index page, a slide viewer, a tour — recorded when the path changes (a
  repeat of the same path within 5 s is ignored, and query-string changes such
  as a search or `?sync=` are not new page views). `/login` and `/admin*` are
  never recorded, nor is a view of a draft slide or tour unless a staff
  dashboard login sent it. Each row keeps the client IP and User-Agent (pseudonymised
  or dropped under **hashed**, see below), the device class (bots included, filtered out of reports by default), the referrer's
  host name only — the external site the page was loaded from, repeated on
  every page view until the next full page load (in-app navigation keeps the
  browser's `document.referrer`), none for a direct or same-site load — and
  the identity, if any.
- **Slide dwell** (`slide_dwell_events`): foreground time on a viewer page —
  time with the tab hidden is not counted — sent when the tab is hidden or
  closed and when the visitor leaves the page, and merged into one row per
  page visit. Visits under 1.5 s, bot User-Agents and visitors without an
  identity produce no row.
- **Tour sessions** (`tour_sessions`): a row when a tour opens, then the
  furthest step reached, foreground time and completion (reaching the last
  step), with the same identity and bot rules as slide dwell.

A dwell or tour time is capped at the visit's own wall-clock span and at
`WSI_ANALYTICS_MAX_DWELL_MS` (default 14400000, 4 h; set in
`/etc/wsi/wsi.env`, read at start), so a tab left open overnight cannot
dominate the averages. The first report of a slide-dwell visit counts at most
10 minutes, and from then on a visit (dwell or tour) grows only as fast as the
server's own clock runs, whatever the browser reports. The viewer reports at
least every 2 minutes, so this costs an ordinary visit nothing; a one-shot
scripted beacon, or any client that first reports only when hidden or
closed, loses whatever of its first uninterrupted stretch exceeds 10 minutes.

### Staff vs learner traffic

The analytics beacons tag every row (page views, slide dwell, tour sessions)
sent from a browser tab that holds a staff dashboard login with an
`actor_role` value of `admin`, `manager`, or `instructor`. The login lives in
that tab's `sessionStorage`: the dashboard's **Open Viewer** links carry it
into the tab they open, but a public page a staff member opens on their own,
in a tab without it, counts as learner traffic. A staff token that has
expired or been revoked still tags the row (from its signed role claim) while
no longer identifying the user. Anonymous and learner rows leave `actor_role`
`NULL`.

- **Default aggregation excludes staff.** The dashboard's totals, Top
  slides / tours / users, referrers, UA breakdown, and tour-completion
  metrics all filter on `actor_role IS NULL` so learner activity is
  reported in isolation by default.
- **"Include staff" toggle.** The admin Analytics page exposes an
  **Include staff** switch next to the **Include bots**
  switch. Turning it on sends `include_staff=1` with every analytics API
  call and folds staff rows back into the aggregates. (The API flags
  `include_staff` / `include_bots` are integers: `1` / `0`; `true` is
  rejected with 422.)
- **Recent-log view.** The recent page-view table on the Analytics
  page renders an `actor_role` column (admin / manager / instructor
  badge, blank for learner rows) and supports filtering by it through
  the table's role selector.
- **Draft previews.** Views of an unpublished slide or tour (page views,
  dwell and tour sessions) are recorded only for staff, tagged like any
  other staff row and so hidden unless **Include staff** is on. Nobody else
  can open a draft, so such a beacon from anyone else is dropped.

### Privacy modes

The in-app analytics beacon writes to three tables — `page_views`,
`slide_dwell_events`, and `tour_sessions` — and records a user
identifier alongside each row. The recording mode is controlled from
the admin dashboard under **Network → Access analytics**, where the three
modes are labelled **Off**, **Pseudonymous** (`hashed`) and **Identified**
(`plain`):

| Mode | What gets recorded | Default |
|---|---|---|
| **off** | No analytics rows are written at all (the beacon endpoints accept the request, drop the row, and return 204). | Yes — a fresh install opts out until an admin opts in. |
| **hashed** | User identity is stored as `hmac_sha256(ANALYTICS_HASH_SALT, lower(identifier))` (64-char hex), where the identifier is what **plain** would store (e.g. `alice@example.org`, `google:alice@example.org`, `ldap:jdoe`, a staff username), trimmed. Nothing that would re-identify it is kept beside it: page views store no `user_id` (no link to the `users` table), the client IP as a keyed hash with the same salt (so unique-IP counts still work), and no raw User-Agent (only its device class). A DB-only leak cannot be reversed into emails without the salt. | — |
| **plain** | User identity is stored verbatim — the admin username, app-access session subject, Google email, or CF Access email — the same verbatim identity that `PageView.user_email` stores. Page views also keep the raw client IP and User-Agent, and `user_id` for staff beacons. | — |

`slide_dwell_events` and `tour_sessions` are recorded **only for
authenticated viewers**: anonymous traffic deliberately produces no row
(a dwell row keyed by "" would add noise without analytic value).
`page_views` continue to record anonymous traffic in every mode that
isn't `off`, with `user_email` left empty.

The live-sync presenter's participant list follows the mode too, although it
records nothing: under **hashed** each participant's address is shown (and
exported in the panel's CSV, as `ip_pseudonym`) only as a 12-character prefix
of the same keyed hash page views store, and the browser and reverse-DNS host
name are left out, so an instructor running a session sees no more than the
analytics dashboard does. Under **off** and **plain** the list shows the
address, browser and (with `WSI_SYNC_REVERSE_DNS`) host name in full —
**off** only means "record no analytics", and it is every fresh install's
default. A mode change reaches the list on its next update.

The mode applies at write time. Switching from **plain** to **hashed** does not
rewrite rows already recorded — they keep their raw identity, IP and
User-Agent until retention prunes them or you clear them with the scoped wipe
below.

These three modes govern the app's **own, self-hosted** analytics only. A
separate, optional setting can additionally send page views to **Google
Analytics** — an external processor outside this policy. It is off by default;
see [Google Analytics (optional, external)](#google-analytics-optional-external)
below before enabling it.

### `ANALYTICS_HASH_SALT`

Lives in `/etc/wsi/wsi.env`. `infra/install.sh` generates one
automatically (`openssl rand -hex 32`) on a fresh install — manual
installs should do the same. The salt is **never** exposed via any API:
the admin UI only sees an availability boolean, and the dashboard
disables the **Pseudonymous** (`hashed`) radio when the salt is missing. A salt shorter than
32 characters is treated as missing (a warning is logged at startup).

To rotate:

```bash
# 1. Generate a new salt
NEW=$(openssl rand -hex 32)

# 2. Replace the ANALYTICS_HASH_SALT line in /etc/wsi/wsi.env
sudo sed -i "s|^ANALYTICS_HASH_SALT=.*|ANALYTICS_HASH_SALT=${NEW}|" /etc/wsi/wsi.env
unset NEW

# 3. Restart so the new salt takes effect (wsi-sync uses it for the
#    live-sync participant list under hashed mode)
sudo systemctl restart wsi-app.service wsi-sync.service
```

Existing hashed rows keep their previous-salt digests — rotation
deliberately breaks correlation across the boundary (a hash collision
across salts is cryptographically negligible). If you need a clean
break, follow the rotation with a scoped wipe:

```text
DELETE /api/admin/analytics/data?to_date=YYYY-MM-DD
```

The scope is always explicit: `from_date` and/or `to_date` for a range, or
`all=1` to empty all three event tables (one statement per table — seconds,
even for millions of rows). A request with neither, `all=1` together with a
date, or an empty date parameter is refused with HTTP 400
(`analyticsClearScopeRequired`) and deletes nothing, so a misspelt parameter
name can never turn into a full wipe. A scoped wipe
has to delete row by row, so each request works for at most about 20 seconds
and answers `"complete": false` when rows of the range may remain; repeat the
identical request until it answers `"complete": true` (the dashboard's
**Delete analytics data** dialog does this for you). Every request is recorded
in the audit trail with what it deleted.

A privacy-driven user purge (GDPR Article 17 / off-boarding) is also
exposed for both **plain** and **hashed** rows:

```text
DELETE /api/admin/analytics/user        (form field: identifier=<email-or-hash>)
POST   /api/admin/analytics/user/preview (same form field; deletes nothing)
```

The identifier goes in the form body, not the URL: nginx's and uvicorn's
access logs keep the full request line, so an email in the query string would
be written there in clear; a `?identifier=` query is not read (`422`). The
preview answers which stored identifiers
the purge would match (up to 50) and how many rows per table; the dashboard
shows it before the destructive confirm.

Pass either the digest shown in the dashboard or the raw email/username:
the server matches the value exactly as typed and, when a hash salt is
configured, also its digest under the current salt, so one call purges both
plaintext rows and rows recorded in **hashed** mode. Sign-ins through the
app-access gate (basic / LDAP / Google) are recorded as `<mode>:<username>`
(e.g. `google:alice@example.org`); a bare email or username is therefore also
tried with each of those prefixes. Plaintext rows additionally match without
regard to ASCII letter case; case is not folded for non-ASCII letters, so
type such a name exactly as it was stored. Rows hashed under an earlier salt
can only be purged by passing their stored digest. The purge deletes in
committed chunks, so a failure part-way (HTTP 500, audited with
`partial_failure`) can leave some rows already removed — repeating the
request finishes the job. Like a scoped wipe it stops after about 20 seconds
with `"complete": false` when rows may remain, to be repeated until
`"complete": true`. The identifier is recorded in the audit trail (as
its digest while the mode is **hashed**), not in the error log.

Switching the mode from **plain** to **hashed** only changes how new rows are
recorded. To convert the rows already recorded in plain mode, use the
dashboard's **Pseudonymize plaintext rows** action (`POST
/api/admin/analytics/pseudonymize`): it rewrites them into exactly the form
hashed mode stores (hashed identity and IP, no User-Agent string, no user
link) under the current salt, in committed chunks like a scoped wipe
(`"complete": false` until repeated), leaves already-hashed values alone, and
cannot be undone. It needs the hash salt (HTTP 409
`analyticsSaltUnavailable` without one) and the mode switched away from
**plain** — to **hashed**, or **off** if recording has stopped (HTTP 409
`analyticsPseudonymizeNeedsHashed` while it is still **plain**, changing
nothing): while the mode is **plain**, new rows — and the later reports of
visits already open, which would then no longer match their converted rows
and be dropped — keep arriving in plain form.

### Consent toast

When the mode is not `off`, the public viewer shows a one-time toast
informing the visitor that their activity is recorded. It closes by itself
after 8 seconds on screen, which counts as a dismissal like the close button
(the countdown starts over when the tab comes back to the front or the
visitor moves to another page, and does not run on a dashboard / sign-in
page). Dismissing it is remembered in the browser's `localStorage` per
recording mode, so it stays dismissed across sessions and tabs until the
operator switches to a different mode (e.g. **hashed** to **plain**), which
shows it again.

Operators who have already obtained explicit consent through
another channel (employee handbook, separate consent form, etc.) can
disable this from the same **Network → Access analytics** panel ("Show a
recording-notice toast to viewers") without disabling the underlying
recording (`analytics_notice_toast_enabled`).

The toast covers the app's own recording only. It says nothing about the
optional Google Analytics tag below, which is a separate disclosure the
operator is responsible for.

### Google Analytics (optional, external)

Independently of the in-app analytics above, the dashboard can inject a
**Google Analytics 4** tag into the public viewer. This is the one feature that
sends visitor activity to a third party, so it is **off by default** and
deserves a deliberate decision.

- **Where.** **Network → Google Analytics → Measurement ID** — a panel of its
  own, distinct from the **Access analytics** panel that carries the privacy
  modes above (`NetworkSettings.ga_measurement_id`, set via
  `PATCH /api/admin/network/settings`, `admin` only). An empty value — the
  default — disables the feature entirely: no third-party script is loaded and
  nothing is ever sent to Google.
- **Format.** GA4 measurement IDs only (`G-XXXXXXXXXX`). A `UA-` or `GTM-` id
  is rejected with HTTP 422. The page checks the same shape again before it
  builds the script URL, so a value that arrived some other way (a restored or
  hand-edited database row) loads nothing rather than a Tag Manager container.
- **No restart needed.** The id is served to the SPA by the public
  `GET /api/viewer/settings`, so the change takes effect on the next page load.

What happens when it is set: on the first public route of a page session the
frontend injects `https://www.googletagmanager.com/gtag/js` once, and from then
on **every** SPA navigation sends a `page_view` event carrying the **path, URL,
and document title**. `send_page_view` is disabled on the `config` call so
each navigation is reported exactly once by the explicit event rather than
being double-counted with gtag's own automatic pageview. The `config` call also
sets `allow_google_signals` and `allow_ad_personalization_signals` to `false`:
the tag is for the operator's own page counts, not for building advertising
audiences out of students.

What that URL carries, and what it leaves out:

- **The path, as is** — so `/slides/<id>` and `/slides/<id>/tours/<id>` carry
  the slide's and tour's public id, and the document title their title. That
  includes **link-only** slides and tours, whose id is all that keeps them
  off the index: whoever can read the GA property can find them, and open
  them as far as the app-access gate allows. The id is not masked for them:
  the point where the page is reported knows only the route, so telling a
  link-only item from a listed one would take a fetch of its own, and a
  masked id would leave GA unable to tell one slide from another. If the ids
  and titles of link-only content must not reach Google, do not enable GA.
- **Only the query parameters that say which view it was**: `step` (the tour
  step); `q`, `sort`, `order` and `type` (the index page's search, sort and
  view tab); `stain`, `organ`, `lab`, `tags` and `facet` (its filters — so
  the facet values a visitor filters by, e.g. `facet=grade:2`, are sent);
  plus GA's own `utm_*` campaign tags. Every other parameter is
  stripped, and so is the `#fragment` — above all `sync`, the live-session id
  in a follower link (`/slides/<id>?sync=<sid>`), which is all it takes to
  join that session. A navigation that only adds or drops a stripped
  parameter (joining a session, `?debug=1`) is not reported as a new page;
  changing a search, sort, tab or filter is.
  The stripped URL is also set (`gtag('set', …)`) as the page location of the
  hits gtag sends by itself, such as `user_engagement`.
- **The referrer, stripped the same way.** A full-page load inside the app
  (a reload, an admin-entry link) has the previous page's full URL as
  `document.referrer` (the nginx config's `strict-origin-when-cross-origin`
  Referrer-Policy keeps same-origin URLs whole), `?sync=` included, which
  gtag would report verbatim. The app sends `page_referrer` itself (on each
  `page_view` and as the `gtag('set', …)` default): a same-origin referrer
  reduced to its path and the parameters above, a cross-origin one as the
  browser gives it — under the browsers' default policy, just the other
  site's origin.

That is what the app sends. gtag can also report the address bar on its own,
through the web stream's *enhanced measurement* — notably **Page changes based
on browser history events**. Turn that one off in the GA property: the app
already reports every navigation, so it would only double-count, with the raw
URL. As a backstop, add `sync` to the stream's data-redaction list of query
parameters.

Scope and limits worth knowing before enabling it:

- **Public routes only.** The tag is never loaded on `/login` or on any
  `/admin*` route, so dashboard activity is not reported to Google. Admin entry
  points are full-page loads, so a session that started on a public page cannot
  carry the script into the dashboard. Nor is it loaded, or a page reported, in
  a tab that holds a staff dashboard login — a draft preview or a presenter's
  live-sync view opened from the dashboard — so staff browsing in such tabs is
  absent from GA, unlike the app's own analytics, which tag it instead.
- **The identity is Google's, not the app's.** The GA tag is independent of the
  in-app privacy modes — it is **not** suppressed by `analytics_mode=off`, it
  does not honour `ANALYTICS_HASH_SALT`, and the visitor identification
  (cookies, IP handling, retention) is governed by your Google Analytics
  property configuration, not by this application.
- **Consent and disclosure are the operator's responsibility.** The built-in
  recording-notice toast describes the app's own recording only. Where
  applicable (GDPR/ePrivacy, institutional policy), obtain consent and disclose
  the transfer through your own channel.
- **It is the only *visitor-facing* egress — not the only egress.** With it
  off, the page fetches nothing from a third party: web fonts and the viewer's
  UI sprites are served from this host. (The About dialog's source / licence
  links point off-site, but those are ordinary links a user chooses to follow,
  not requests the page issues.) Other destinations a deployment can reach —
  none of them carrying visitor activity — are worth knowing when auditing what
  the host talks to. Note which side initiates each: only the "server" rows
  need an egress rule on the host's firewall.

  | Destination | Reached by | When |
  |---|---|---|
  | `api.github.com` | server | The [upstream update check](#upstream-update-check). **On by default**, but scheduled only by a request to the admin Server Info page — so an install whose dashboard is never opened never contacts it. At most once per 12 h once a fetch has succeeded; once per 10 min while it keeps failing. `WSI_UPDATE_CHECK=0` disables the call outright. |
  | `oauth2.googleapis.com`, `www.googleapis.com` | server | The `google_oauth` code-for-token exchange and the JWKS fetch used to verify the returned ID token. Only in that auth mode. |
  | `<team>.cloudflareaccess.com` | server | Cloudflare Access JWKS fetch during token verification. Only when `CF_ACCESS_TEAM_DOMAIN` / `CF_ACCESS_AUD` are set. |
  | `accounts.google.com` | browser | The `google_oauth` sign-in redirect. The app only builds the authorize URL and sends the visitor there; the server itself never fetches it. Only in that auth mode. |
- **The CSP already allows it.** The Google origins in `script-src`,
  `connect-src` and `img-src` exist for exactly this feature (see
  [Content-Security-Policy](#content-security-policy)). If you never intend to
  use GA, you can tighten the CSP by removing them from your nginx site
  config; nothing else in the app needs them.

To turn it off again, blank the Measurement ID field. The already-collected
data lives in the Google Analytics property and has to be removed there — the
dashboard's **Delete analytics data** action (and
`DELETE /api/admin/analytics/data` behind it) only touches this app's own
tables.

### Retention and CSV export

Each of the three event tables is capped on its own, by the same janitor
scheme as the audit and error logs (at boot, then at most hourly per worker
from the beacon path). The beacon-path prune runs in the background after the
beacon has been answered, in rounds of at most 10 s; while a backlog remains
(a lowered cap, a year of rows ageing out at once) the next round is retried
by the first beacon after 60 s instead of an hour:

- `WSI_ANALYTICS_LOG_MAX_ROWS` (default `5000000`) — keep at most the newest
  N rows per table; `0` disables the row cap.
- `WSI_ANALYTICS_LOG_MAX_AGE_DAYS` (default `365`) — drop rows older than N
  days; `0` disables the age cap.

The Analytics page's CSV export (`GET /api/admin/analytics/export.csv`) offers
the datasets `summary`, `daily`, `top-slides`, `top-tours`, `top-users`,
`referrers`, `ua` and `recent`, under the page's current window (at most
365 days; 30 days when none is given, except `recent`, which then takes the
newest rows of the whole retained history) and its staff / bot toggles.
Ranked and `recent` exports stop at `limit` rows (default 1000, at most
10000). The file has the same UTF-8 BOM and formula-character defence as the
audit export.

### Bucket time zone — `WSI_ANALYTICS_TIMEZONE`

The admin analytics dashboard buckets daily aggregates by an explicit
time zone rather than UTC, so "yesterday's hits" matches the operator's
calendar day regardless of where the host runs. Set the IANA name in
`/etc/wsi/wsi.env`:

```text
WSI_ANALYTICS_TIMEZONE=Europe/Berlin
```

When unset (or set to a name that is not a valid IANA zone, which logs a
warning), the app falls back to the zone named by `TZ` / `/etc/localtime` /
`/etc/timezone`, then to the host's current UTC offset if none names one. The DB session
itself stays pinned to UTC; only the bucket math (and the per-window
"timezone offset shown in the UI") reads this value. The custom-range
date picker's "today" is also today in this zone, not in the browser's,
and the Audit History and Error Logs pages read their `since` / `until`
filters and show each row's time in it too.
A change takes effect after `systemctl restart wsi-app.service`.

### Dashboard query time

A full 365-day dashboard over a `page_views` table at the default
`WSI_ANALYTICS_LOG_MAX_ROWS` cap (5 million rows) takes on the order of
20 seconds. The dashboard, per-section and CSV report queries give up after
50 seconds — before nginx's 60 s proxy timeout — and answer HTTP 503 with a
message asking for a shorter period, rather than running on after the
browser has stopped waiting. If you raise the row cap well above the default
and hit this, narrow the window or lower the cap.

---

## Uninstalling

Completely remove Tessera — systemd units, nginx site, `/opt/wsi`,
`/etc/wsi`, `/var/lib/wsi`, the `wsi` system user, the source clone
at `~/wsi`, and the NodeSource apt repository installed by
`infra/install.sh` — with the steps below.

### Standard uninstall steps

```bash
sudo systemctl disable --now \
    wsi-app.service wsi-sync.service \
    wsi-cache-stat.timer wsi-avahi-reload.path wsi-restart.path

sudo rm /etc/systemd/system/wsi-app.service \
        /etc/systemd/system/wsi-sync.service \
        /etc/systemd/system/wsi-cache-stat.service \
        /etc/systemd/system/wsi-cache-stat.timer \
        /etc/systemd/system/wsi-avahi-reload.path \
        /etc/systemd/system/wsi-avahi-reload.service \
        /etc/systemd/system/wsi-restart.path \
        /etc/systemd/system/wsi-restart.service
sudo systemctl daemon-reload

sudo rm /etc/nginx/sites-enabled/wsi /etc/nginx/sites-available/wsi
# wsi-tls.conf holds your TLS settings (certificate paths, HSTS): keep a
# copy first if you will set the site up again elsewhere
sudo rm -f /etc/nginx/snippets/wsi-locations.conf \
           /etc/nginx/snippets/wsi-access-log.conf \
           /etc/nginx/snippets/wsi-tls.conf
# Optional: install.sh removed the stock "default" site link; restore it if
# you keep nginx and want its default site back
[ -e /etc/nginx/sites-available/default ] && \
    sudo ln -sf /etc/nginx/sites-available/default /etc/nginx/sites-enabled/default
sudo systemctl reload nginx

# tmpfiles.d entry and IPC dirs
sudo rm -f /etc/tmpfiles.d/wsi.conf
sudo rm -rf /run/wsi /run/wsi-cache-stat

# Avahi service file written by the app (mDNS toggle), and revert the
# /etc/avahi/services ownership change install.sh made. If you skip the
# chown, the directory stays owned by the (about-to-be-deleted) wsi UID,
# which becomes an orphan numeric owner after `userdel wsi` below.
sudo rm -f /etc/avahi/services/wsi.service
sudo chown root:root /etc/avahi/services

sudo rm -rf /opt/wsi /etc/wsi /var/cache/nginx/wsi_dzi \
            /var/cache/nginx/wsi_authz /var/cache/nginx/wsi_static \
            /usr/local/bin/wsi-cache-stat

# NodeSource apt repository installed by install.sh (only if you don't
# have other Node.js workloads on this host that rely on it).
sudo rm -f /etc/apt/sources.list.d/nodesource.list \
           /etc/apt/keyrings/nodesource.gpg
sudo apt-get update -qq

# Remove data (WSI files, thumbnails, SQLite DB) — only if you are sure.
# This also deletes the dashboard's saved backups (BACKUP_DIR, by default
# /var/lib/wsi/backups) and the .pre-restore-* rollback copies: copy off any
# archive you want to keep first. (A BACKUP_DIR set elsewhere is not removed.)
sudo rm -rf /var/lib/wsi
# Only on a server that received syncs (infra/sync-from.sh): its mirror; and,
# on a server that was a sync source, the sudo rule you added for it
sudo rm -rf /var/lib/wsi-replica
sudo rm -f /etc/sudoers.d/wsi-sync
# Written only if an update's output pipe broke mid-run (WSI_UPDATE_RECOVERY_LOG)
sudo rm -f /var/log/wsi-update-recovery.log

sudo userdel wsi || true
```

Finally, remove the source clone you used to run `install.sh` /
`update.sh`. This is the developer-side checkout in your home
directory (not a system path), so run it as the non-root user that
originally ran `git clone` — no `sudo` needed:

```bash
rm -rf ~/wsi
```

> **Note — apt packages are not removed.** `infra/install.sh` installs
> the following packages via `apt-get install`. They are commonly shared
> with other workloads on the host, so the steps above intentionally
> leave them in place. Remove them manually with
> `sudo apt-get purge <pkg>...` only if you are sure nothing else needs
> them:
>
> - System: `ca-certificates`, `curl`, `gnupg`, `lsb-release`, `git`,
>   `make`, `rsync`
> - Python: `python3`, `python3-venv`, `python3-pip`
> - WSI / image libs: `libopenslide0` **or** `libopenslide1` (the SONAME
>   bumped with OpenSlide 4.x — Ubuntu ≤ 24.04 ships the former, Ubuntu
>   26.04 / Debian trixie the latter; `install.sh` picks whichever apt
>   offers), `openslide-tools`, `libjpeg-dev`, `zlib1g-dev`
> - Database: `sqlite3`
> - Web / mDNS: `nginx`, `avahi-daemon`, `libnss-mdns`
> - Node.js: `nodejs` (from the NodeSource repo whose apt source was
>   removed above)

### If you also ran `infra/install-ops-tools.sh`

`install-ops-tools.sh` is independent of `install.sh` and the steps
above do **not** touch its side-effects. If you ran it on this host,
also do the following — otherwise the host will keep applying daily
security patches and **rebooting at 03:00 local time** long after
Tessera is gone.

```bash
# Drop the auto-reboot policy. Important: 52unattended-upgrades-reboot
# is system-wide — its filename does not advertise its Tessera origin.
sudo rm -f /etc/apt/apt.conf.d/52unattended-upgrades-reboot

# Drop the SSH brute-force jail. (Leave fail2ban itself running if you
# want sshd protected; removing only the Tessera drop-in disables just
# its overrides, not the package defaults.)
sudo rm -f /etc/fail2ban/jail.d/wsi-sshd.conf
sudo systemctl reload fail2ban 2>/dev/null || true

# Revert sysstat ENABLED back to the distro default.
sudo sed -i 's/^ENABLED="true"/ENABLED="false"/' /etc/default/sysstat
sudo systemctl disable --now sysstat.service 2>/dev/null || true

# Optional: disable the other daemons. Skip whichever you want to keep
# (they're general-purpose, not Tessera-specific).
sudo systemctl disable --now fail2ban.service 2>/dev/null || true
sudo systemctl disable --now smartd.service   2>/dev/null || true
```

`install-ops-tools.sh` may also have written
`/etc/apt/apt.conf.d/20auto-upgrades` if no copy existed on the host
when it ran. Leaving the file in place keeps daily security-update
fetches enabled (no automatic reboot once `52unattended-upgrades-reboot`
above is gone) — usually a good thing on a production host. Remove it
only if you want to switch off automatic security updates entirely:

```bash
sudo rm -f /etc/apt/apt.conf.d/20auto-upgrades
```

> **Note — ops-tools apt packages are not removed either.** Same
> rationale as above (commonly shared with other workloads). Purge
> manually only if you are sure:
>
> - `unattended-upgrades`, `fail2ban`, `smartmontools`, `sysstat`,
>   `goaccess`, `htop`, `ncdu`, `jq`, `tmux`

---

## AGPL compliance — updating repository URLs in a fork

This software is distributed under the **GNU Affero General Public License v3 (AGPL-3.0)**.
The AGPL requires that anyone who runs a modified version as a network service must make the
modified source code available to users of that service.
If you operate your own instance from a fork, update the following locations to point
to your fork's public repository URL.

### Files to update

| File | Location | Purpose |
|---|---|---|
| `frontend/src/lib/appInfo.ts` | `repoUrl` field | Source link displayed in the frontend UI (the About dialog) — the AGPL §13 network-source link users see |
| `QUICKSTART_FOR_UBUNTU.md` | `git clone` command | Clone URL shown in the install guide |
| `docs/developer-guide.md` | `git clone` command | Clone URL shown in the production-deployment quickstart |
| `infra/systemd/wsi-app.service` | `Documentation=` line | Systemd unit documentation URL |
| `infra/systemd/wsi-sync.service` | `Documentation=` line | Systemd unit documentation URL |
| `THIRD_PARTY_LICENSES.md` | AGPL §13 paragraph | Upstream repository URL cited as the corresponding-source location |

### Steps

1. Create your fork and make it publicly accessible.
2. Replace the repository URL in each of the files above with your fork's URL,
   commit, and push to the fork.
3. Deploy from that commit: point the server's clone at the fork
   (`git remote set-url origin <fork-url>`) and run `sudo bash infra/update.sh`
   (rebuilds the frontend and reinstalls the systemd unit files). By default
   `update.sh` checks out the newest `vX.Y.Z` tag among **all** tags in the
   clone — the upstream ones it already has included — so tag a release on the
   fork with a version above every upstream tag (otherwise the newest upstream
   release, without your URL changes, is what gets deployed), or deploy the branch with `sudo WSI_TRACK_BRANCH=1 bash infra/update.sh`
   (or `GIT_REF=<ref>`). Do not rely on uncommitted edits in the clone: git
   refuses to check out over them, and a rollback's `git reset --hard`
   discards them.

Also set `WSI_UPDATE_CHECK_REPO=<owner>/<repo>` in `/etc/wsi/wsi.env` (or
`WSI_UPDATE_CHECK=0`): otherwise the Server Info page's
[upstream update check](#upstream-update-check) keeps comparing your
version with `yosukey/tessera-wsi`'s releases.

> **Why `appInfo.ts` matters**
> The `repoUrl` value is rendered in the frontend UI so that users of the service can
> reach the source code. AGPL compliance requires that this link points to the actual
> running fork, not the upstream repository.
