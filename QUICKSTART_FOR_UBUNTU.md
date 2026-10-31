# Quickstart: Native Ubuntu 26.04 Build & Deployment

How to install Tessera directly on **Ubuntu 26.04 LTS** using
systemd + nginx + SQLite.

---

## 0. Architecture overview

```text
┌────────────────────────────────────────────────────────┐
│ nginx (systemd)  :80                                   │
│   ├─ /api/admin/sync/* → 127.0.0.1:8001  (live-sync)  │
│   ├─ /api/sync/*       → 127.0.0.1:8001  (REST + WS)  │
│   ├─ /api/admin/login  → 127.0.0.1:8000  (rate-ltd)   │
│   ├─ /api/auth/login   → 127.0.0.1:8000  (rate-ltd)   │
│   ├─ /api/admin/*      → 127.0.0.1:8000  (JWT gated)  │
│   ├─ /api/*            → 127.0.0.1:8000               │
│   ├─ /dzi/*            → 127.0.0.1:8000  (tile cache) │
│   ├─ /thumbs/*         → 127.0.0.1:8000  (cached)     │
│   ├─ /assets/*         → 127.0.0.1:8000  (cached)     │
│   └─ /                 → 127.0.0.1:8000  (SPA)        │
│                                                        │
│ wsi-app.service   → uvicorn 127.0.0.1:8000  (multi)   │
│ wsi-sync.service  → uvicorn 127.0.0.1:8001  (1 wkr)   │
└────────────────────────────────────────────────────────┘
```

| Path | Purpose |
| --- | --- |
| App code | `/opt/wsi/` |
| Python venv | `/opt/wsi/venv/` |
| Data (WSI, thumbs, cache, assets) | `/var/lib/wsi/` |
| SQLite database | `/var/lib/wsi/wsi.db` (WAL mode) |
| Config / secrets | `/etc/wsi/wsi.env` (systemd EnvironmentFile) |
| Run user | `wsi` (system account) |
| Logs | `journalctl -u wsi-app` / `journalctl -u wsi-sync` |

`wsi-sync.service` is a sidecar that exclusively owns the in-memory
`RoomManager` for live-sync (presenter / follower / discovery). It always
runs with `--workers 1`; nginx routes all `/api/sync/*` and
`/api/admin/sync/*` traffic there, leaving `wsi-app` free to use multiple
workers for DZI tile throughput.

---

## 1. Prerequisites

- Ubuntu 26.04 LTS — logged in as a user with `sudo` access. (The installer
  also accepts Ubuntu 24.04; it refuses a `python3` older than 3.12, the
  oldest Python the pinned backend dependencies are resolved for.)
- Internet connection
- Hardware requirements depend less on the number of users than on how
  many *different* slides are viewed at the same time. In a typical class,
  everyone views the same slide or a handful of slides on the lecture's
  theme: after the first viewer, nearly every tile is served from the
  nginx disk cache without touching the backend, so a modest server
  handles a large class. Independent browsing of many large slides (e.g.
  self-study or a research archive) is the heavier pattern. The table
  below lists **rough sizing guidelines** for class-style use — not strict
  requirements. Treat these as starting points and adjust after a few
  days of operation by observing CPU load, process RSS, OS page-cache hit
  rate, disk I/O wait, and disk usage trends.

  | Profile                       | Cores    | RAM       | Disk            | Default workers |
  |-------------------------------|----------|-----------|-----------------|-----------------|
  | Smoke test / single user †    | 2        | 4 GB      | 50 GB           | 1               |
  | Small lab (≤ 10 users)        | 4        | 8 GB      | 100 GB SSD      | 2               |
  | Department (10–50 users)      | 8        | 16–32 GB  | 500 GB SSD      | 4               |
  | Large classes (50–150+ users) | 12–16 ‡  | 32–64 GB  | 1 TB+ NVMe SSD  | 6–8             |

  † Smoke test is for trying the software with 1–2 small slides;
  not intended for any production-shaped load.

  ‡ More cores are not used by default: the default worker count stops at
  8 (reached at 16 cores), so a 24- or 32-core host runs the same 8
  workers as a 16-core one. Add cores only together with an explicit
  `UVICORN_WORKERS` setting (§3.8), and only after monitoring shows the
  CPU — not disk I/O — is the limit.

  If many users browse many different large slides independently, size
  RAM and disk toward the upper end of each range or beyond (see the RAM
  and Disk notes below); cores still need not exceed 16.

  Sizing notes:
  - **CPU** — the default worker count is `min(max(2, cores/2), 8)`,
    then reduced by a RAM cap on low-memory hosts (see §3.8). Each worker
    can use more than one core while rendering uncached tiles (OpenSlide
    decoding and JPEG encoding run outside Python's GIL in the worker's
    thread pool), and nginx, `wsi-sync` and the OS need the rest — which
    is why the formula leaves about half the cores free. Past 16 cores
    the default configuration gains nothing. CPU load peaks when a new
    slide is first opened (its tiles are not yet cached), not with the
    number of viewers.
  - **RAM** — two parts:
    - *Process memory* ≈ 1 GB RSS per uvicorn worker. This stops growing
      at 8 workers (≈ 8–9 GB including `wsi-sync`).
    - *OS page cache* for the slide regions being viewed. This is the real
      performance lever and does **not** stop growing with the worker
      count: it follows the total size of the slides viewed at the same
      time. A class viewing a few slides needs little; many users browsing
      many large slides benefit from every extra GB, since the kernel uses
      spare memory as page cache automatically.
  - **Disk** ≈ 50 GB nginx tile cache (default `max_size=50g` in
    `infra/nginx/wsi.conf`; change it with `sudo bash infra/nginx-cache.sh`
    from your clone, see [nginx tile cache](./docs/operations.md#nginx-tile-cache--infranginx-cachesh)) +
    total WSI library size × ~1.5 (headroom
    for kernel writeback and occasional growth) + backup retention
    copies. Per-file caps are 8 GiB for slide uploads
    (`WSI_MAX_UPLOAD_BYTES`) and 1 TiB for backup archives
    (`BACKUP_MAX_UPLOAD_BYTES`). Speed matters as much as capacity: past
    ~8 workers the bottleneck shifts to disk I/O (slide reads plus the
    nginx tile cache), so prefer NVMe SSD over adding cores, and keep the
    nginx cache large enough to hold the tiles of all slides used in a
    term's classes.
  - **Network** — every tile a viewer sees crosses the wire, so with a
    large class panning at once the server's uplink (or the Cloudflare
    Tunnel host's) can become the limit before the CPU does.

Verify your release:

```bash
lsb_release -a
```

---

## 2. Automatic install (recommended)

Clone the repository and run the bundled `infra/install.sh`. It is
**idempotent** — re-running it is safe and will not regenerate your existing
`/etc/wsi/wsi.env`, so your secrets survive. (The one exception: if you pass
`WSI_LAN_HOST`, `WSI_LAN_PROBE_TIMEOUT_MS`, or `WSI_PUBLIC_BASE_URL` in the
environment (`sudo WSI_LAN_HOST=… bash infra/install.sh`), that value is
written into `wsi.env` so the next update keeps it. See
[Setting the three build-time values](./docs/operations.md#setting-the-three-build-time-values).)

```bash
cd "$HOME"
git clone https://github.com/yosukey/tessera-wsi.git wsi
cd wsi
sudo bash infra/install.sh
```

Install from a `git clone` as above, not from GitHub's **Download ZIP** or a
release's source archive: without `.git` the installer cannot check out a
release or record which one is running, so the dashboard shows the
placeholder version `0.0.0-dev` and never offers an update.

By default the installer fetches tags from `origin` and checks out the
highest `vX.Y.Z` release tag before building. Only a final release counts: a
pre-release tag such as `v2.0.0-rc1` is never picked, however new; deploy one
with `GIT_REF`. Override with:

- `sudo WSI_TRACK_BRANCH=1 bash infra/install.sh` — stay on the current branch HEAD.
- `sudo GIT_REF=vX.Y.Z bash infra/install.sh` — pin to a specific tag.

Keep the assignment **after** `sudo`. Written the other way round it lands in
`sudo`'s own environment, which Ubuntu's `Defaults env_reset` discards, and
the script runs with the option silently unset.

If no `vX.Y.Z` tags exist on `origin`, it falls back to the current
branch HEAD with a warning.

What the script does:

1. Installs apt packages (`python3-venv`, the OpenSlide runtime —
   `libopenslide0` or `libopenslide1` depending on the release — `nginx`,
   `sqlite3`, `avahi-daemon`, `rsync`, etc.), plus Node.js 24.x from
   NodeSource when the host's `node` or `npm` is missing or `node` is
   older than v24; stops if `python3`
   is older than 3.12
2. Creates the `wsi` system user (and hands it `/etc/avahi/services` for
   the dashboard's mDNS toggle)
3. Creates the data tree under `/var/lib/wsi/` (`wsi/` for the slide files,
   `thumbs/`, `cache/`, `assets/tours/`, **and `backups/` for the dashboard's Backup & Restore feature**),
   `/etc/wsi/`, and the nginx cache directories `/var/cache/nginx/wsi_dzi`,
   `wsi_authz` and `wsi_static`
4. Installs `/etc/tmpfiles.d/wsi.conf` (creates `/run/wsi` as `wsi:wsi`
   on every boot — used as IPC for the restore-apply flow). The
   restore-apply restart is driven by the `wsi-restart.path` unit (below),
   not by `sudo`.
5. Syncs the repository to `/opt/wsi/` (copied in as the `wsi` user); stamps
   the deployed git ref into `/opt/wsi/VERSION` so the admin Server Info
   tab shows the actual checkout
6. Creates a Python venv at `/opt/wsi/venv/` and installs backend deps
7. Runs `npm ci && npm run build` in `frontend/` and publishes the output
   as `services/app/static` — a symlink to the current build directory
   (`static.rel.<pid>`), swapped atomically so the SPA never goes missing
   during a re-run
8. Creates `/etc/wsi/wsi.env` from the template (mode `0600 root:root`)
   and auto-generates `JWT_SECRET`, `ANALYTICS_HASH_SALT`, `WSI_MFA_KEY`
   and an initial admin password (on a re-run, a `wsi.env` without a
   `WSI_MFA_KEY` gets one appended; nothing else in it changes).
   The password is also written to `/etc/wsi/admin-initial-password.txt`
   (mode `0600 root:root`) so it is never echoed to stdout / journald
9. Installs all systemd units: `wsi-app`, `wsi-sync`, `wsi-cache-stat`
   (timer + service + helper at `/usr/local/bin/wsi-cache-stat`),
   `wsi-avahi-reload` (path + service), and `wsi-restart` (path + service —
   restarts `wsi-app` when the API drops `/run/wsi/restart.trigger`, since the
   app runs with `NoNewPrivileges=true` and cannot use `sudo`)
10. Installs and enables the nginx site and its snippets, removing the
    default site; checks it with `nginx -t` (putting the previous nginx
    files back if that fails) and reloads nginx
11. Enables and starts `wsi-cache-stat.timer`, `wsi-avahi-reload.path` and
    `wsi-restart.path`

The SQLite database (`/var/lib/wsi/wsi.db`) is created automatically on
the first backend start (WAL mode, foreign keys ON). No manual DB setup
is needed.

The installer enables and (re)starts both services itself, then
health-checks `http://127.0.0.1:8000/api/health` and
`http://127.0.0.1:8001/api/health` before returning (waiting up to 60 s and
30 s; set `WSI_HEALTH_TIMEOUT` / `WSI_SYNC_HEALTH_TIMEOUT` to change that). A
unit that enters the failed state stops the installer with its recent log; one
that does not answer in time gets a warning, and the installer still prints its
summary but then exits non-zero. The other tuning variables (the
apt-lock wait, npm retries) are listed in the header of `infra/install.sh`.
If you need to enable and start them manually:

```bash
sudo systemctl enable --now wsi-app.service wsi-sync.service
```

Smoke test:

```bash
curl -fsSL http://127.0.0.1:8000/api/health   # direct to app
curl -fsSL http://127.0.0.1:8001/api/health   # direct to sync
curl -fsSL http://127.0.0.1/api/health        # via nginx
```

All three should return `{"ok":true}`. Open `http://<server-ip>/` in a
browser to see the viewer. The initial admin credentials are in
`/etc/wsi/admin-initial-password.txt`:

```bash
sudo cat /etc/wsi/admin-initial-password.txt
```

Log in at `http://<server-ip>/admin`, change the password on the
**Account** page (`/admin/account`), then delete the file:

```bash
sudo rm /etc/wsi/admin-initial-password.txt
```

This bootstraps a single `admin` user. Additional `manager` / `instructor`
accounts can be created later from the **Account** page (`/admin/account`).

### Optional: operational tools

A separate script, `infra/install-ops-tools.sh`, adds a set of
operator utilities (`unattended-upgrades`, `fail2ban`, `smartmontools`,
`sysstat`, `goaccess`, `htop`, `ncdu`, `jq`, `tmux`) on top of the core
stack. It is **not** invoked by `install.sh`; run it by hand when you
want them:

```bash
sudo bash infra/install-ops-tools.sh
```

Running this script also enables automatic security patches and
configures the host to reboot at **03:00 local time** when a security
update requires it. See [§5 Bundled operational tools](#bundled-operational-tools)
for what each tool does and how to operate it.

The script is idempotent, so re-running it is safe.

> **If the automatic install succeeded, you are done.**
> The rest of this document covers a manual step-by-step setup.

> **Keep the `~/wsi` clone.** `infra/update.sh` is run from this
> directory (see [§5 Updating](#updating)), and its automatic rollback
> needs it as well: recovery is a `git reset --hard` back to the
> pre-update commit in this clone, followed by a re-run of
> `infra/install.sh` against the reverted tree (the database is put back
> from a snapshot taken once the services are stopped, before
> `install.sh` runs). If you remove it, just
> re-clone before updating. The clone holds no user data — all data
> lives under `/var/lib/wsi/`.

---

## 3. Manual install

Use this section if you want to understand each step or need to customise
the installation.

All commands assume the repository root is `~/wsi`.

### 3.1 System packages

```bash
sudo apt update
sudo apt -y upgrade
sudo apt -y install \
    ca-certificates curl gnupg lsb-release git make rsync \
    python3 python3-venv python3-pip \
    openslide-tools libjpeg-dev zlib1g-dev \
    sqlite3 \
    nginx \
    avahi-daemon libnss-mdns
```

For the OpenSlide runtime library, install whichever package your release
ships — the name tracks the SONAME, which bumped from `libopenslide0`
(OpenSlide 3.x, Ubuntu ≤ 24.04) to `libopenslide1` (OpenSlide 4.x,
Ubuntu 26.04 / Debian trixie):

```bash
# Ubuntu ≤ 24.04
sudo apt -y install libopenslide0
# Ubuntu 26.04 / Debian trixie (OpenSlide 4.x)
sudo apt -y install libopenslide1
```

(`infra/install.sh` picks the right one automatically.)

Optional operational tools (installed by `infra/install-ops-tools.sh`):

```bash
sudo apt -y install \
    unattended-upgrades fail2ban smartmontools sysstat goaccess \
    htop ncdu jq tmux
```

These are not required for the core stack; see
[§5 Bundled operational tools](#bundled-operational-tools) for the
matching configuration (which `install-ops-tools.sh` applies
automatically).

### 3.2 Node.js (frontend build)

`install.sh` installs Node.js 24.x via NodeSource when `node` or `npm` is
missing or `node` is older than v24. Ubuntu 26.04 may ship a newer `nodejs`,
but the distro package does not include `npm`: you can skip this block only
if `node -v` reports v24+ **and** `npm -v` works.

```bash
sudo install -d -m 0755 /etc/apt/keyrings
curl -fsSL https://deb.nodesource.com/gpgkey/nodesource-repo.gpg.key \
    | sudo gpg --batch --yes --dearmor -o /etc/apt/keyrings/nodesource.gpg
echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/nodesource.gpg] \
https://deb.nodesource.com/node_24.x nodistro main" \
    | sudo tee /etc/apt/sources.list.d/nodesource.list
sudo apt update
sudo apt -y install nodejs
node -v   # should report v24.x or later
```

### 3.3 Dedicated user and directories

```bash
sudo useradd --system --home-dir /opt/wsi --shell /usr/sbin/nologin \
             --user-group wsi

sudo install -d -m 0755 -o wsi -g wsi /opt/wsi
sudo install -d -m 0755 -o wsi -g wsi \
    /var/lib/wsi \
    /var/lib/wsi/wsi \
    /var/lib/wsi/thumbs \
    /var/lib/wsi/cache \
    /var/lib/wsi/assets \
    /var/lib/wsi/assets/tours

# Backup/restore: archive store + staging dir for the dashboard's
# Backup & Restore feature. 0750 keeps these out of www-data's reach;
# downloads go through a bearer-token-gated endpoint instead.
sudo install -d -m 0750 -o wsi -g wsi \
    /var/lib/wsi/backups \
    /var/lib/wsi/backups/.staging

# Config directory: root-only read (wsi.env holds secrets)
sudo install -d -m 0700 -o root -g root /etc/wsi

# nginx tile cache, edge authorization-verdict cache and thumbnail / attachment
# cache directories
sudo install -d -m 0755 -o www-data -g www-data \
    /var/cache/nginx/wsi_dzi /var/cache/nginx/wsi_authz /var/cache/nginx/wsi_static

# Allow the wsi process to write Avahi mDNS service files (only for the
# dashboard's mDNS toggle; the app starts without this directory)
sudo chown wsi:wsi /etc/avahi/services
```

### 3.4 Place the repository at `/opt/wsi`

```bash
cd ~/wsi
sudo rsync -a --delete \
  --exclude='.git' --exclude='data/' \
  --exclude='.venv' --exclude='venv' \
  --exclude='.env*' \
  --exclude='frontend/node_modules' --exclude='frontend/dist' \
  ./ /opt/wsi/
sudo chown -R wsi:wsi /opt/wsi
```

This root `rsync` is safe only because `/opt/wsi` is brand new: nothing has
run in it as `wsi` yet. Do not repeat it over a deployed `/opt/wsi` — later
copies are made by `install.sh` / `update.sh` as the `wsi` user (see below).

From here on `/opt/wsi` belongs to `wsi`, and the next two steps run `pip` and
`npm` in it as that user — any package's install script can rewrite files
there. So everything root installs into a system location later (the systemd
units, the nginx config, the `wsi.env` template) is taken from your clone
(`~/wsi`), never from `/opt/wsi`, and the steps that do touch `/opt/wsi` run as
`wsi` (`sudo -u wsi`). `infra/install.sh` follows the same rule.

### 3.5 Python venv and backend dependencies

```bash
sudo -u wsi python3 -m venv /opt/wsi/venv
sudo -u wsi /opt/wsi/venv/bin/pip install --require-hashes \
    -r /opt/wsi/services/app/requirements.lock
```

`requirements.lock` pins every package (including transitive ones) to an
exact version with its SHA-256 hashes, so every host gets the same, verified
dependency tree; `requirements.txt` only lists the accepted version ranges
it is generated from. It needs Python 3.12 or newer. The venv's own `pip` is
enough — there is no need to upgrade it first (that would be the one
download not checked against a hash).

### 3.6 Build the frontend and copy to static

The backend serves `services/app/static/` itself — real files, and
`index.html` for every other path outside `/api/`, `/dzi/`, `/thumbs/`,
`/assets/` and `/static-assets/` — so the Vite build output must land there.
It checks that the directory exists once, at startup: build and copy the
frontend before `wsi-app` first starts (or restart it afterwards).

```bash
cd /opt/wsi/frontend
# Use `npm ci` when package-lock.json is present (deterministic)
sudo -u wsi npm ci --no-audit --no-fund
sudo -u wsi npm run build

# As wsi, not root: dist/ was just written by the build, and a root `cp`
# would follow anything planted there (e.g. a symlink to a root-only file)
# into the public static tree.
sudo -u wsi rm -rf /opt/wsi/services/app/static
sudo -u wsi install -d -m 0755 /opt/wsi/services/app/static
sudo -u wsi cp -a /opt/wsi/frontend/dist/. /opt/wsi/services/app/static/
cd -
```

### 3.7 SQLite database

The database file (`/var/lib/wsi/wsi.db`) is created automatically on the
first backend start with the following PRAGMAs:

| PRAGMA | Value | Why |
| --- | --- | --- |
| `journal_mode` | `WAL` | Safe concurrent access from multiple uvicorn workers |
| `foreign_keys` | `ON` | Enforces `ON DELETE CASCADE` |
| `busy_timeout` | `5000` ms | Waits up to 5 s on a locked write |
| `synchronous` | `NORMAL` | Safe and fast under WAL |

No extra DB setup is required as long as `/var/lib/wsi/` is owned by `wsi:wsi`
(created in §3.3).

### 3.8 Environment file `/etc/wsi/wsi.env`

```bash
sudo install -m 0600 -o root -g root \
    ~/wsi/infra/wsi.env.example /etc/wsi/wsi.env
sudoedit /etc/wsi/wsi.env
```

The file is stored as `0600 root:root`; systemd reads it as root via
`EnvironmentFile=` before dropping privileges to the `wsi` user.

Core settings:

| Variable | Notes |
| --- | --- |
| `JWT_SECRET` | **Required** — the app refuses to start without it. Generate with `openssl rand -hex 48` |
| `ADMIN_BOOTSTRAP_PASSWORD` | **Required on the first start** (while no admin account exists): a strong password for the first admin row |
| `ADMIN_BOOTSTRAP_USERNAME` | Initial admin login name (default `admin`) |
| `DATABASE_URL` | Default `sqlite:////var/lib/wsi/wsi.db` (as in the template) |

Also set `ANALYTICS_HASH_SALT` (generate with `openssl rand -hex 32`, as
`install.sh` does). It is not required to boot, but while it is blank the
dashboard's pseudonymous ("hashed") analytics mode cannot be selected.

Also set `WSI_MFA_KEY` (generate with `openssl rand -hex 32`, as
`install.sh` does). It seals each staff account's two-factor secret; while
it is blank, two-factor sign-in cannot be turned on.

**Tuning uvicorn worker count (optional)**

The default is `min(max(2, cores/2), 8)` — half the cores, floored at 2,
capped at 8 — and is then further reduced by a RAM cap so each worker keeps
a ≥2 GiB OS-page-cache budget, and finally clamped to the core count as on
every path (so a 1-core host gets 1 worker); on a low-RAM host the
effective count can be lower (e.g. 2 cores / 4 GB → 1 worker). The 8-worker cap reflects that
uvicorn workers are async and most
tile traffic is absorbed by the nginx disk cache; past ~8 workers the
bottleneck shifts to disk I/O while each extra worker still duplicates
the per-process DZI cache. To override, add to `/etc/wsi/wsi.env`:

```bash
# Option A: absolute count (takes precedence over PERCENT; bypasses the
# 8-worker and RAM caps; still clamped to [1, core count])
UVICORN_WORKERS=4

# Option B: percentage of CPU cores (bypasses the 8-worker and RAM
# caps; still clamped to [1, core count])
UVICORN_WORKERS_PERCENT=50
```

Apply with `sudo systemctl restart wsi-app.service`.
Interactive helper, run from your clone (`~/wsi`, not `/opt/wsi` — see §3.4):
`sudo bash infra/set-workers.sh`.

### 3.9 Runtime directory (Backup & Restore IPC)

The dashboard's Backup & Restore feature drops a request file at
`/run/wsi/restore.request` that `start-wsi-app.sh` reads on the next
service start, then writes `/run/wsi/restore.result` back for the UI.
The `tmpfiles.d` entry below recreates `/run/wsi` as `wsi:wsi` on every
boot. This configuration deliberately avoids `RuntimeDirectory=` in the unit
because it wipes the directory on every restart, which would lose the restore
request mid-restart. The restart itself is handled by the `wsi-restart.path`
unit installed in §3.10 (not by `sudo` — the app runs with
`NoNewPrivileges=true` and cannot use setuid `sudo`).

```bash
sudo install -d -m 0755 /etc/tmpfiles.d
sudo tee /etc/tmpfiles.d/wsi.conf >/dev/null <<'EOF'
# Type Path        Mode UID GID Age Argument
d      /run/wsi    0755 wsi wsi -   -
EOF
sudo systemd-tmpfiles --create /etc/tmpfiles.d/wsi.conf
```

### 3.10 systemd units

Root-run units and scripts come from your clone (see §3.4); the start
script stays in `/opt/wsi` (it runs as `wsi`), so `wsi` marks it executable.

```bash
cd ~/wsi

# wsi-app (multi-worker FastAPI backend)
sudo -u wsi chmod +x /opt/wsi/infra/systemd/start-wsi-app.sh
sudo install -m 0644 infra/systemd/wsi-app.service \
                     /etc/systemd/system/wsi-app.service

# wsi-sync (single-worker live-sync coordinator, port 8001)
sudo install -m 0644 infra/systemd/wsi-sync.service \
                     /etc/systemd/system/wsi-sync.service

# wsi-cache-stat (periodic nginx cache size snapshot for the admin dashboard)
sudo install -m 0755 infra/systemd/wsi-cache-stat.sh \
                     /usr/local/bin/wsi-cache-stat
sudo install -m 0644 infra/systemd/wsi-cache-stat.service \
                     /etc/systemd/system/wsi-cache-stat.service
sudo install -m 0644 infra/systemd/wsi-cache-stat.timer \
                     /etc/systemd/system/wsi-cache-stat.timer

# wsi-avahi-reload (triggers avahi-daemon reload on mDNS config changes)
sudo install -m 0644 infra/systemd/wsi-avahi-reload.path \
                     /etc/systemd/system/wsi-avahi-reload.path
sudo install -m 0644 infra/systemd/wsi-avahi-reload.service \
                     /etc/systemd/system/wsi-avahi-reload.service

# wsi-restart (restarts wsi-app when the API drops /run/wsi/restart.trigger —
# the privilege-safe alternative to `sudo systemctl restart`)
sudo install -m 0644 infra/systemd/wsi-restart.path \
                     /etc/systemd/system/wsi-restart.path
sudo install -m 0644 infra/systemd/wsi-restart.service \
                     /etc/systemd/system/wsi-restart.service

sudo systemctl daemon-reload

# Enable everything
sudo systemctl enable --now wsi-app.service wsi-sync.service
sudo systemctl enable --now wsi-cache-stat.timer
sudo systemctl enable --now wsi-avahi-reload.path
sudo systemctl enable --now wsi-restart.path
```

Verify:

```bash
systemctl status wsi-app.service wsi-sync.service --no-pager
journalctl -u wsi-app.service -n 50 --no-pager
curl -fsSL http://127.0.0.1:8000/api/health
curl -fsSL http://127.0.0.1:8001/api/health
```

### 3.11 nginx site

`wsi.conf` pulls in all the `location`/`proxy_pass` blocks from
`wsi-locations.conf` via `include /etc/nginx/snippets/wsi-locations.conf;`,
its access log from `include /etc/nginx/snippets/wsi-access-log.conf;` and
your TLS settings from `include /etc/nginx/snippets/wsi-tls.conf;`, so all
three snippets must be installed first — otherwise `nginx -t` fails on the
missing include. The access-log and TLS snippets are yours to edit
(`access_log off;`, your own format — see "nginx access log" in
`docs/operations.md`; the certificate, see "TLS" below), so they are copied
only when they are not there yet; `install.sh` does the same.

```bash
cd ~/wsi
sudo install -d -m 0755 /etc/nginx/snippets
sudo install -m 0644 infra/nginx/wsi-locations.conf \
                     /etc/nginx/snippets/wsi-locations.conf
[ -e /etc/nginx/snippets/wsi-access-log.conf ] \
    || [ -L /etc/nginx/snippets/wsi-access-log.conf ] \
    || sudo install -m 0644 infra/nginx/wsi-access-log.conf \
                            /etc/nginx/snippets/wsi-access-log.conf
[ -e /etc/nginx/snippets/wsi-tls.conf ] \
    || [ -L /etc/nginx/snippets/wsi-tls.conf ] \
    || sudo install -m 0644 infra/nginx/wsi-tls.conf \
                            /etc/nginx/snippets/wsi-tls.conf
sudo install -m 0644 infra/nginx/wsi.conf \
                     /etc/nginx/sites-available/wsi
sudo ln -sf /etc/nginx/sites-available/wsi /etc/nginx/sites-enabled/wsi
sudo rm -f /etc/nginx/sites-enabled/default
sudo nginx -t
sudo systemctl reload nginx
```

### 3.12 Smoke test

```bash
curl -fsSL http://127.0.0.1:8000/api/health   # app direct
curl -fsSL http://127.0.0.1:8001/api/health   # sync direct
curl -fsSL http://127.0.0.1/api/health        # via nginx
```

All three must return `{"ok":true}`. Open `http://<server-ip>/` in a
browser to confirm the viewer loads.

---

## 4. Adding slides

Slides — single-file OpenSlide-readable formats (`.svs`, `.ndpi`, `.scn`,
`.bif`, `.tif` / `.tiff`), pyramidal OME-TIFF (`.ome.tif`, read by the app's own
reader), or pre-built `.szi` archives (OpenSlide-independent) — are added
through the admin dashboard's **Upload** tab, which registers each file,
stores it under `/var/lib/wsi/wsi/` and generates its thumbnail. Multi-file
formats (MIRAX `.mrxs`, Hamamatsu `.vms` / `.vmu`) are not supported; convert
them to a single-file format first (see [Supported WSI Formats](./docs/architecture.md#supported-wsi-formats)).
A large image with no pyramid (reduced-resolution levels) — e.g. a plain tiled
TIFF over about 8000 × 8000 px — is refused with a message saying so; convert
it to a pyramidal TIFF (`vips tiffsave in.tif out.tif --tile --pyramid
--compression jpeg`).
Copying a file into `/var/lib/wsi/wsi/` by hand does **not** register it: the
app only serves files its database knows about. The one case for a manual copy
is putting back the files a metadata-only restore expects (same filenames as
before); see [Restoring WSI files after a metadata-only restore](./docs/operations.md#restoring-wsi-files-after-a-metadata-only-restore).

Slide upload requires the `admin` or `manager` role.

Use the admin dashboard (`http://<host>/admin`) to:

1. Log in with the bootstrap admin credentials. After an automatic
   install they are in `/etc/wsi/admin-initial-password.txt`
   (`sudo cat /etc/wsi/admin-initial-password.txt`); for a manual
   install they are whatever you set as `ADMIN_BOOTSTRAP_USERNAME` /
   `ADMIN_BOOTSTRAP_PASSWORD` in `/etc/wsi/wsi.env`
2. Register slides from the **Upload** tab. You can select multiple WSI
   files at once (up to 50 per batch); they upload one after another and a
   summary of which files succeeded or failed is shown at the end. Before
   starting, the tab shows the batch size against the server's free space
   and blocks the upload if the volume would not fit. Each upload creates an
   unpublished draft slide. Filenames must be unique across the library
   (matched case-insensitively, e.g. `slide.svs` and `Slide.SVS` collide);
   a duplicate is rejected with HTTP 409 and the file is left unregistered.
3. Edit metadata and create tours

Change the password on the **Account** page (the dashboard header's
**Account** button, `/admin/account`) immediately after the first
login, then remove `/etc/wsi/admin-initial-password.txt` if it exists.

---

## 5. Operations

### Bundled operational tools

`infra/install-ops-tools.sh` is an opt-in companion to `install.sh`
(see [§2 Optional: operational tools](#optional-operational-tools)).
When run, it installs the following utilities and applies the
configuration described in each subsection.

| Tool | Purpose | First command to try |
| --- | --- | --- |
| `unattended-upgrades` | Auto-apply security patches; reboot at 03:00 if needed | `sudo unattended-upgrades --dry-run --debug` |
| `fail2ban` | Ban SSH brute-force sources | `sudo fail2ban-client status sshd` |
| `smartmontools` | SMART health for storage disks | `sudo smartctl -H /dev/sdX` |
| `sysstat` (`sar`) | Historical CPU / IO / memory | `sar -u 1 5` |
| `goaccess` | nginx access-log analyzer | `sudo goaccess /var/log/nginx/access.log -c` |
| `htop` | Interactive process viewer | `htop` |
| `ncdu` | Disk usage explorer | `sudo ncdu /var/lib/wsi` |
| `jq` | JSON formatter | `journalctl -u wsi-app -o json \| jq .` |
| `tmux` | Persistent SSH sessions | `tmux new -s wsi` |

#### Automatic security updates

`install-ops-tools.sh` writes two drop-ins under `/etc/apt/apt.conf.d/`
(each only when the file does not exist yet, so a re-run keeps your edits
and an existing `20auto-upgrades` is left as it is):

- `20auto-upgrades` — runs `apt update` daily and applies updates from
  the distro `-security` pocket, then `apt autoclean` weekly.
- `52unattended-upgrades-reboot` — if a security update flags
  `/var/run/reboot-required`, the host **reboots at 03:00 server-local
  time** (`Automatic-Reboot-Time "03:00"`,
  `Automatic-Reboot-WithUsers "true"`).

The distro-managed `50unattended-upgrades` (the whitelist itself) is not
touched, so only `${distro_id}:${distro_codename}-security` updates are
ever applied automatically.

Operate it:

```bash
sudo unattended-upgrades --dry-run --debug   # show what would happen today
cat /var/run/reboot-required                 # exists -> reboot scheduled at 03:00
timedatectl status                           # confirm the server timezone
```

To change the reboot time, edit `Automatic-Reboot-Time` in
`/etc/apt/apt.conf.d/52unattended-upgrades-reboot`. To disable the
whole mechanism:

```bash
sudo systemctl disable --now unattended-upgrades.service \
                              apt-daily.timer apt-daily-upgrade.timer
```

> The reboot time is interpreted in the server's local timezone. On a
> host left at UTC, "03:00" is not your 3 AM. `install-ops-tools.sh`
> logs the resolved timezone after applying the policy; fix it up-front
> with `sudo timedatectl set-timezone <Zone>` if needed.

#### SSH brute-force protection (fail2ban)

`install-ops-tools.sh` writes `/etc/fail2ban/jail.d/wsi-sshd.conf` with
sshd enabled (`maxretry=5`, `findtime=10m`, `bantime=1h`). The
`wsi-app`-side admin login already has its own rate-limit, so no nginx
jail is added by default.

```bash
sudo fail2ban-client status sshd                    # current bans + counters
sudo fail2ban-client set sshd unbanip <IP>          # release a mistaken ban
sudo systemctl reload fail2ban                      # after editing jail.d
```

#### Disk health (smartmontools)

`smartd` is enabled with the distro-default `/etc/smartd.conf`
(`DEVICESCAN ... -m root ...`). It may fail to start in several environments —
hypervisors with no SMART-capable disks, hardware RAID controllers that
hide the underlying devices, USB-attached storage, or NVMe drives whose
attributes the default config can't read. `install-ops-tools.sh` records
a warning rather than aborting; run `journalctl -xeu smartd.service` to
see the specific cause and decide whether to tune `/etc/smartd.conf`.
Manual checks on bare metal:

```bash
sudo smartctl -H /dev/sdX             # quick health summary
sudo smartctl -a /dev/nvme0n1         # full attributes (NVMe)
```

The distro default addresses warnings to `root` by mail, which goes nowhere
without an MTA. To receive them, change the `-m` address in
`/etc/smartd.conf` and install an MTA — both outside this script's scope.

#### Historical performance (sysstat)

`install-ops-tools.sh` flips `ENABLED="true"` in `/etc/default/sysstat`
and starts `sysstat.service`. Its `sysstat-collect.timer` then records
system counters every 10 minutes under `/var/log/sysstat/`, kept for the
distro-default 7 days (`HISTORY` in `/etc/sysstat/sysstat`).

```bash
sar -u 1 5                                  # CPU, 5 samples 1s apart
sar -r                                      # memory pressure today
sar -d -p                                   # per-device I/O today
sar -u -f /var/log/sysstat/sa15             # CPU history for the 15th (if retained)
```

#### nginx access analysis (goaccess)

```bash
sudo goaccess /var/log/nginx/access.log -c                              # interactive TUI
sudo goaccess /var/log/nginx/access.log --log-format=COMBINED \
              -o /tmp/wsi-report.html                                   # HTML report
```

Useful for spotting hot DZI tile paths, sudden 4xx/5xx spikes, or
unfamiliar user agents probing `/admin`.

### Logs

```bash
sudo journalctl -u wsi-app -f
sudo journalctl -u wsi-sync -f
sudo tail -f /var/log/nginx/{access,error}.log
```

### Restart / stop

```bash
sudo systemctl restart wsi-app wsi-sync
sudo systemctl stop    wsi-app wsi-sync
sudo systemctl reload  nginx
```

### Updating

The recommended path is the bundled update script, which fetches the
latest tags, checks out the highest `vX.Y.Z` release tag, stops the
services, snapshots the SQLite database, re-runs `install.sh` (idempotent),
starts and health-checks the services, and rolls back automatically
(code and database) if a step fails. It will **not**
regenerate `/etc/wsi/wsi.env`. (It re-runs `install.sh` with its own
environment, so the same three-variable exception applies: passing
`sudo WSI_LAN_HOST=… bash infra/update.sh` writes that value into
`wsi.env`. Run it without those variables — the normal case — and the file
is untouched.)

> **Keep tracked files in your clone unedited.** Settings belong in
> `/etc/wsi/wsi.env` (the upload limit, for one, is `WSI_MAX_UPLOAD_BYTES`,
> §6.5). When a release changes a file you edited, the update's
> `git checkout` refuses to overwrite your edit and stops (nothing is stopped
> or deployed); `update.sh` names the files that block it and prints the
> command that undoes them, and `git -C ~/wsi status` lists them too.

```bash
cd ~/wsi
sudo bash infra/update.sh
```

Options:
- `SKIP_GIT_PULL=1` — skip the git step entirely; use the current working tree
- `SKIP_RESTART=1` — run `install.sh` but do not start the services again at the end (they are still stopped before the file swap, so start them yourself afterwards: `sudo systemctl start wsi-app wsi-sync`; nginx is reloaded either way, by `install.sh`)
- `GIT_REF=<ref>` — pin to a specific branch or tag (overrides latest-tag detection)
- `WSI_TRACK_BRANCH=1` — stay on the current branch (fast-forwarded with `git pull --ff-only`) instead of checking out the latest release tag; from the detached release-tag checkout a default install or update leaves, it first switches to the remote's default branch (usually `main`)
- `WSI_VERIFY_RELEASE_TAGS=1` — deploy a release tag only if its signature verifies (`git verify-tag`) against the keyring of the user running `sudo`; stops before changing anything otherwise. Needs signed release tags and the maintainer's public key imported with `gpg --import`; leave it off until both exist. `WSI_RELEASE_SIGNING_KEY=<fingerprint>` pins the one key whose signature counts (otherwise any key in that keyring does)
- `WSI_SKIP_ROLLBACK=1` — on failure, leave the clone and `/opt/wsi` at the failed state for inspection instead of rolling back (the services are still started again)
- `WSI_SKIP_DB_SNAPSHOT=1` — do not snapshot the database first; a rollback after the new release has started then cannot bring the previous release back up
- `WSI_HEALTH_TIMEOUT=<s>` / `WSI_SYNC_HEALTH_TIMEOUT=<s>` — how long the final health check waits for `wsi-app` / `wsi-sync` (default 30 s / 30 s here; `install.sh` run on its own waits 60 s / 30 s). Either one not answering in time fails the update and rolls it back

Full list: [`docs/operations.md` → Options](./docs/operations.md#options-environment-variables).

To control the git step yourself (a local branch, a commit not yet tagged),
move the clone to the code you want and let the script do everything else —
stop the services, snapshot the database, re-run `install.sh` (which also
reinstalls the systemd units, the nginx config and snippet, and reloads
both), then start and health-check the services:

```bash
cd ~/wsi
git checkout <ref>              # or: git pull
sudo SKIP_GIT_PULL=1 bash infra/update.sh
```

With `SKIP_GIT_PULL=1` the script records no pre-update commit, so on a
failure it cannot reset the clone for you (it still starts the services
again); move the clone back yourself and re-run the same command. Doing the
steps by hand instead is not recommended: the update is more than rsync, pip
and npm — it also has to reinstall the units and nginx files from the clone,
run `systemctl daemon-reload`, `nginx -t` and reload nginx.

### Backup

| Item | Path | Notes |
| --- | --- | --- |
| SQLite DB | `/var/lib/wsi/wsi.db` | Use `sqlite3 .backup` for a consistent online snapshot |
| WSI files | `/var/lib/wsi/wsi/` | |
| Thumbnails | `/var/lib/wsi/thumbs/` | Regeneratable, but slow |
| Tour assets | `/var/lib/wsi/assets/` | |
| Secrets | `/etc/wsi/wsi.env` | Contains `JWT_SECRET`, `ANALYTICS_HASH_SALT`, `WSI_MFA_KEY` and bootstrap password. Keep it with the DB backup: a restored DB needs the same `WSI_MFA_KEY`, or its two-factor sign-ins have to be reset |
| nginx cache size | `/etc/wsi/nginx-cache-max-size` | Only present if `infra/nginx-cache.sh` was used; without it a rebuilt server goes back to the default `max_size` |

```bash
# Online backup (safe while the service is running — WAL-aware).
# umask 077: the copy holds password hashes, so keep it private (0600).
sudo -u wsi sh -c 'umask 077; sqlite3 /var/lib/wsi/wsi.db ".backup /tmp/wsi-$(date +%F).db"'
# The copy is 0600 wsi:wsi and /tmp is emptied at boot: move it somewhere
# lasting with sudo (e.g. sudo mv /tmp/wsi-*.db /root/).
```

The admin dashboard also has a built-in **Backup & Restore** tab (archives
under `/var/lib/wsi/backups/`); see
[Backup & restore — admin dashboard](./docs/operations.md#backup--restore--admin-dashboard).

`/var/lib/wsi/cache/` and `/var/cache/nginx/wsi_dzi/` are regeneratable;
skip them.

> **WAL note**: SQLite creates `wsi.db-wal` / `wsi.db-shm` sidecars. The
> `.backup` command above merges them into a single consistent file. If you
> copy files directly (e.g. `cp`), stop both services first
> (`sudo systemctl stop wsi-app wsi-sync` — the live-sync coordinator keeps
> the database open and writes to it too) and copy all three files together.

### TLS (recommended for public deployments)

```bash
sudo apt -y install certbot python3-certbot-nginx
sudo certbot --nginx -d example.com
```

certbot patches the installed site file (`/etc/nginx/sites-available/wsi`)
and registers an auto-renewal timer.

**TLS survives updates.** `install.sh` (and therefore `update.sh`) replaces
`/etc/nginx/sites-available/wsi` from `infra/nginx/wsi.conf` on every run, but
the stock server block includes **`/etc/nginx/snippets/wsi-tls.conf`**, a file
that is yours: it is created when missing and never overwritten. When the next
`install.sh` / `update.sh` finds the site file certbot patched, it moves
what certbot put there into that snippet first — the `listen 443 ssl`, the
certificate and key paths and `include /etc/letsencrypt/...` lines
(everything certbot marked `# managed by Certbot`), your domain, an
uncommented HSTS header, and certbot's `http -> https` redirect for that host
— so there is nothing to redo after an update. The redirect applies to plain
HTTP only (the same server block now answers on 443 too). A snippet that
already had other content is kept beside it as `wsi-tls.conf.bak-<time>`, and
the run ends by saying so; the site file it was migrated from is kept as
`/etc/nginx/sites-available/wsi.previous` (also when the run stops because it
found TLS it cannot carry). A snippet with lines of your own — the HSTS header
above, an `ssl_verify_client` gate, a listener for another port — is not
replaced: certbot's lines are **added** to it, and only a line they replace is
dropped, then listed in a comment at the end of the file — the same `listen`
address (`443`, `*:443` and `0.0.0.0:443` are one; a `quic` listener is another),
the same single-valued `ssl_*` directive, or an *earlier certbot* certificate
pair. Your own certificate pair (say an ECDSA one beside certbot's RSA one) and
your own HTTP/3 listener stay. If the snippet is a **symlink**, it is not
rewritten through: when the file it points to lacks certbot's lines the run
stops, before changing anything, and prints the lines to add. Owner and mode of
the snippet are kept. A site file that holds only certbot's redirect block — what
`certbot enhance --redirect` leaves on a host that is already migrated — never
replaces the snippet's listener and certificates: the snippet keeps what it has
and gains the redirect, and `wsi.previous` keeps the earlier, complete copy.

An update checks the site file for TLS it cannot carry over before it stops
anything, and does not start in that case.

Once HTTPS works for good, enable HSTS **in the snippet**, not in the site
file:

```nginx
add_header Strict-Transport-Security "max-age=31536000; includeSubDomains" always;
```

You can also write the snippet by hand, with a certificate you already have
(for example from `certbot certonly`): `infra/nginx/wsi-tls.conf` is the
template, with every line commented out. TLS settings that are in the site file
but that are *not* certbot's — an `ssl_certificate` of your own, a second
server block with TLS, a `listen 443 ssl` with Ubuntu's snakeoil include, an
`ssl_verify_client` client-certificate gate or any other `ssl_*` line certbot
did not write, **anywhere in the file** (also outside a `server` block), or an
`include` of an ssl / tls file such as `snippets/ssl-params.conf` — are
not guessed at: `install.sh` (and `update.sh`, before it stops anything) stops
and names those lines rather than switch HTTPS off. Move
the settings of this site into `wsi-tls.conf` — they take effect when the new
site file is installed, so run it again straight away — and keep any other
server block (another domain, another port) in a site file of its own; `WSI_NGINX_DISCARD_SITE_TLS=1` replaces the file anyway.

### Firewall

If UFW is active:

```bash
sudo ufw allow OpenSSH
sudo ufw allow 'Nginx Full'
sudo ufw enable
```

The FastAPI backends (ports 8000/8001) are bound to loopback only; they do
not need to be exposed externally.

---

## 6. Troubleshooting

### 6.1 `wsi-app.service` stuck in `activating (auto-restart)`

Read the logs:

```bash
sudo journalctl -u wsi-app -n 200 --no-pager
```

Common causes:

- Wrong `DATABASE_URL` or missing parent directory
  → Fix `/etc/wsi/wsi.env`, then `sudo systemctl restart wsi-app wsi-sync`
- A numeric setting in `/etc/wsi/wsi.env` that is not a plain number (e.g.
  `WSI_MAX_UPLOAD_BYTES=8G`): the log says `<NAME> must be an integer; got …`
  (or `must be a number`)
  → Fix that line, then `sudo systemctl restart wsi-app wsi-sync`
- `/var/lib/wsi` not owned by `wsi:wsi`
  → `sudo chown -R wsi:wsi /var/lib/wsi`
- Corrupted SQLite DB
  → Stop both services (`sudo systemctl stop wsi-app wsi-sync`), restore from
  backup, remove `wsi.db-wal` / `wsi.db-shm`, make sure the restored file is
  owned by `wsi:wsi` (§6.3), then start both again

### 6.2 `nginx: [emerg] mkdir() ".../wsi_dzi" failed (13: Permission denied)`

The tile-cache directory (or the authorization-verdict cache `wsi_authz`
or the thumbnail / attachment cache `wsi_static` next to it) is missing or
has wrong ownership:

```bash
sudo install -d -m 0755 -o www-data -g www-data \
    /var/cache/nginx/wsi_dzi /var/cache/nginx/wsi_authz /var/cache/nginx/wsi_static
sudo nginx -t && sudo systemctl reload nginx
```

### 6.3 SQLite write errors (`database is locked` / `readonly database`)

- **`database is locked`**: a process is holding a write lock longer than
  `busy_timeout=5000 ms`. Identify it with `sudo lsof /var/lib/wsi/wsi.db`.
- **`attempt to write a readonly database`**: check ownership:
  ```bash
  sudo chown wsi:wsi /var/lib/wsi/wsi.db*
  ```

Manual DB inspection:

```bash
sudo -u wsi sqlite3 /var/lib/wsi/wsi.db '.tables'
```

### 6.4 `502 Bad Gateway`

`wsi-app.service` is not listening on port 8000:

```bash
systemctl status wsi-app --no-pager
ss -ltnp | grep 8000
curl -v http://127.0.0.1:8000/api/health
```

For live-sync 502s, check port 8001 and `wsi-sync.service` in the same way.

### 6.5 Large WSI upload returns `413 Request Entity Too Large`

The limit is one setting, `WSI_MAX_UPLOAD_BYTES` in `/etc/wsi/wsi.env`
(default 8 GiB). Raise it there, then re-run the installer from your clone:

```bash
sudoedit /etc/wsi/wsi.env       # e.g. WSI_MAX_UPLOAD_BYTES=17179869184  (16 GiB)
cd ~/wsi                        # your clone
sudo WSI_SKIP_TAG_CHECKOUT=1 bash infra/install.sh
```

`install.sh` — which `update.sh` also runs on every update — installs the
nginx config with the body limit of the upload and slide-replace locations
raised to fit: the new value plus 1 MiB of multipart framing, in whole MiB
(`16385m` here). It then reloads nginx and restarts the app, so the
two limits change together. `WSI_SKIP_TAG_CHECKOUT=1` keeps the release you
are on; without it the installer also moves to the latest release, like
`update.sh`. It re-runs every install step, so it takes a few minutes.
Restarting `wsi-app` alone is not enough for a raise: nginx would still
refuse anything over 8 GiB with this same `413`. (A *lower* limit is enforced
by the app; `sudo systemctl restart wsi-app` is all that needs.)

The dashboard's **Upload** tab and **Replace WSI file** ask the app for the
limit (`GET /api/admin/storage`), so once the app has restarted they check
and name the new value too; no frontend rebuild is involved.

Tour attachments work the same way with `TOUR_ASSET_MAX_BYTES` (default
2.5 MiB): `install.sh` raises the nginx body limit of the asset-upload
location (`3m`) to fit a larger value, and the tour editor reads the limit
from the app.

Do **not** edit `client_max_body_size` in the clone's
`infra/nginx/wsi-locations.conf`: it is a tracked file, so the next update's
`git checkout` aborts on the local change, and a rollback's `git reset
--hard` silently drops it. Nor in the installed
`/etc/nginx/snippets/wsi-locations.conf`, which every install / update
overwrites. If you have edited it, restore it with:

```bash
git -C <your clone> checkout -- infra/nginx/wsi-locations.conf
```

### 6.6 Frontend changes not reflected after update

Check that `services/app/static/` was actually updated. After
`install.sh` / `update.sh` it is a symlink to the current build directory
(`static.rel.<pid>`), so list it with a trailing slash:

```bash
ls -la /opt/wsi/services/app/static/
```

The SPA is served directly from disk so it takes effect immediately without
restarting the service. Force a hard reload in the browser (`Ctrl+Shift+R`).

### 6.7 Server is reachable locally but not from other hosts

```bash
# 1. Confirm nginx listens on all interfaces
sudo ss -tnlp | grep ':80'
#    → 0.0.0.0:80 and [::]:80 expected; 127.0.0.1:80 only means wsi.conf is broken

# 2. Check the server's outbound IP
ip -brief addr

# 3. Allow HTTP through UFW
sudo ufw status
sudo ufw allow 'Nginx Full'
sudo ufw reload
```

If the above checks pass but external access still fails, the block is
**outside the OS**:

- **Cloud VMs (AWS / GCP / Azure / Oracle …)**: open inbound
  80/tcp (and 443/tcp) in your security group / network ACL.
- **Home / office LAN**: check router port forwarding, corporate proxies,
  or client-side DNS / `hosts`.
- **Other host-based firewalls** (`nftables`, `firewalld`, `csf`): add
  port 80 there too.

Distinguish `Connection refused` (OS-level reject on the server) from
`timed out` (blocked in transit) by running
`curl -v http://<server-ip>/api/health` from a remote client.

---

## 7. Uninstall

See [`docs/operations.md` → Uninstalling](./docs/operations.md#uninstalling)
for the full removal procedure.

---

## Reference

- Repository overview: [`README.md`](./README.md)
- Developer guide (source map, HTTP/WebSocket endpoint reference): [`docs/developer-guide.md`](./docs/developer-guide.md)
- Operations reference (updates, backup / restore, server sync, sign-in, caches, analytics): [`docs/operations.md`](./docs/operations.md)
- Architecture overview and supported-format table: [`docs/architecture.md`](./docs/architecture.md)
- systemd units: [`infra/systemd/`](./infra/systemd/)
- nginx site config: [`infra/nginx/wsi.conf`](./infra/nginx/wsi.conf)
- Environment template: [`infra/wsi.env.example`](./infra/wsi.env.example)
- Automated installer: [`infra/install.sh`](./infra/install.sh)
- Optional operational tools: [`infra/install-ops-tools.sh`](./infra/install-ops-tools.sh)
- Update helper: [`infra/update.sh`](./infra/update.sh)
- Backup server / migration sync: [`infra/sync-from.sh`](./infra/sync-from.sh) ([guide](./docs/operations.md#backup-server-and-server-moves--infrasync-fromsh))
