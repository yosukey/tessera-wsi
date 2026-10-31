# Tessera developer guide

Technical reference for the Tessera codebase: source-tree map, role gating,
HTTP/WebSocket endpoints, local development, schema evolution, i18n, release
process, deployment and tuning, and feature-level notes. The top-level [`README.md`](../README.md) is the
introduction; this document is the implementation-level companion.

Related documents:
- [`../QUICKSTART_FOR_UBUNTU.md`](../QUICKSTART_FOR_UBUNTU.md) — full step-by-step production install.
- [`architecture.md`](./architecture.md) — architecture overview and supported-format table.
- [`operations.md`](./operations.md) — operations reference: maintenance scripts, updates, backup / restore, server sync, sign-in, caches, analytics.
- [`tour-markdown.md`](./tour-markdown.md) — what a tour step description may contain, and what the sanitizer strips.

## Contents

**1. Codebase overview**

- [Repository map](#repository-map)
- [Roles & gating](#roles--gating)
- [Major endpoints](#major-endpoints)
  - [Tile and API URL layout](#tile-and-api-url-layout)

**2. Development workflow**

- [Local development (also native)](#local-development-also-native)
  - [Common dev commands](#common-dev-commands)
  - [Running the test suite](#running-the-test-suite)
  - [Dependency locking](#dependency-locking)
  - [Continuous integration](#continuous-integration)
- [Evolving the database schema](#evolving-the-database-schema)
  - [Two version numbers](#two-version-numbers)
  - [Adding a migration](#adding-a-migration)
- [Internationalisation](#internationalisation)
- [Admin forms: a request in flight](#admin-forms-a-request-in-flight)
- [Releases](#releases)
  - [Where the running version comes from](#where-the-running-version-comes-from)

**3. Deployment and tuning**

- [Production deployment (Ubuntu 26.04)](#production-deployment-ubuntu-2604)
- [Updating / maintenance](#updating--maintenance)
  - [RAM-aware worker / cache sizing](#ram-aware-worker--cache-sizing)
- [Viewer rendering tuning](#viewer-rendering-tuning)
  - [`immediate_render` (default `true`)](#immediate_render-default-true)
  - [`max_image_cache_count` (default `200`)](#max_image_cache_count-default-200)
  - [Per-frame load limit (`maxTilesPerFrame`, 1–4 by pixel ratio)](#per-frame-load-limit-maxtilesperframe-14-by-pixel-ratio)
  - [Tile browser caching (`Cache-Control`)](#tile-browser-caching-cache-control)

**4. Feature notes**

- [Bulk slide operations (admin)](#bulk-slide-operations-admin)
  - [API](#api)
  - [Verification steps](#verification-steps)
- [Image credit (admin)](#image-credit-admin)
  - [API](#api-1)
  - [Verification steps](#verification-steps-1)
- [Image-adjustment presets (admin)](#image-adjustment-presets-admin)
  - [API](#api-2)
  - [Verification steps](#verification-steps-2)
- [Bulk tour operations (admin)](#bulk-tour-operations-admin)
  - [API](#api-3)
  - [Verification steps](#verification-steps-3)
- [Step editing feature (admin)](#step-editing-feature-admin)
  - [API](#api-4)
  - [Tour metadata](#tour-metadata)
  - [Tour editing concurrency](#tour-editing-concurrency)
  - [Verification steps](#verification-steps-4)
- [Slide export / import (admin)](#slide-export--import-admin)
  - [Verification steps](#verification-steps-5)
- [Server-to-server sync (`infra/sync-from.sh`)](#server-to-server-sync-infrasync-fromsh)

## Repository map
- `services/app/wsi_app/main.py` — FastAPI entrypoint, registers API/DZI routers, mounts `/thumbs` / `/assets` / SPA static, bootstraps admin, exposes health (`GET /api/health`), and runs the boot-time schema reconciler (see [Evolving the database schema](#evolving-the-database-schema)).
- `services/app/wsi_app/config.py` (the env-driven `settings`), `db.py` (engine, SQLite pragmas, `get_db`), `models.py` (SQLAlchemy ORM), `schemas.py` (Pydantic request / response models and the step / annotation caps), `_serializers.py` (response-shape helpers shared by the routers and `slide_transfer`), `admin_helpers.py` (shared admin-router utilities: `api_error`, `audit`, slide-id reservation, …) — the core every router builds on.
- `services/app/wsi_app/auth.py` — staff JWTs (and the two short-lived two-factor tokens), password hashing, signed download tokens and the role dependencies (see [Roles & gating](#roles--gating)); `mfa.py` — the optional TOTP second factor: codes, secrets sealed under `WSI_MFA_KEY`, recovery codes; `app_auth.py` / `cf_access.py` — the app-access gate in front of the public viewer (Basic / LDAP / Google OAuth, Cloudflare Access).
- `services/app/wsi_app/migrations/` — numbered data migrations; `app_meta.py` — the `app_version` / `schema_version` stamps and downgrade guards; `version.py` — the running version; `update_check.py` — the dashboard's GitHub release check. See [Evolving the database schema](#evolving-the-database-schema) and [Releases](#releases).
- `services/app/wsi_app/slide_transfer.py` — per-slide export / import bundles (see [Slide export / import](#slide-export--import-admin)); `jobs.py` — the cross-worker background-job store (`BACKUP_DIR/.jobs`) behind backup / restore / reset / transfer jobs.
- `services/app/wsi_app/network.py` — the IP-filter cache and mDNS (avahi) helper; `login_throttle.py` — the per-IP failed-login throttle shared by the admin login and its two-factor step, the app-access logins and live-sync Basic auth; `analytics.py` — the analytics beacon helpers (identity probing, User-Agent classification, referrer-host extraction, retention pruning) behind `routers/analytics.py`; `admin_reports.py` — the analytics-timezone and listing plumbing shared by the analytics, history and log routers; `retention.py` — the boot-time and opportunistic pruning shared by the capped log tables; `service_restart.py` — the restart trigger behind restore / reset apply; `column_defaults.py` — the column-default rendering shared by the reconciler and `tools/normalize_live_db.py`; `slide_resolve_cache.py` — the per-worker `public_id` resolution cache behind `/dzi/` and `/api/edge-authz`; `slide_credit.py` — the per-slide image credit.
- `services/app/wsi_app/routers/public.py` — public read endpoints (`/api/slides`, `/api/tours`, `/api/index/tours`, `/api/facets`, `/api/viewer/settings`, ...).
- `services/app/wsi_app/routers/admin_*.py` — admin API split by domain: `admin_auth`, `admin_mfa` (two-factor sign-in: the login's code step, enrolment, removal, an admin's reset of another member), `admin_slides`, `admin_slide_transfer` (per-slide export / import bundles), `admin_metadata`, `admin_tours`, `admin_settings`, `admin_system`, `admin_perf` (the Performance page's benchmark runs), `admin_backup`, `admin_users`, `admin_history`, `admin_logs`. Analytics admin routes live in `routers/analytics.py`, and live-sync admin routes live in `routers/sync.py`. Each protected endpoint is gated by one of the six role dependencies listed under [Roles & gating](#roles--gating), except `GET`/`PATCH /api/admin/me`, `POST /api/admin/me/sign-out-everywhere` and `/api/admin/me/mfa/*` (bare `get_current_user`) and the three `…/download` GETs, which check the signed `?t=` token or an admin bearer inline.
- `services/app/wsi_app/routers/analytics.py` — page-view, slide-dwell, and tour-session ingest (`/api/analytics/{pageview,slide-dwell,tour-event}`) plus the admin analytics dashboard endpoints (`/api/admin/analytics/*`). Recording is gated by `NetworkSettings.analytics_mode` (`off` / `hashed` / `plain`); `hashed` mode HMACs the user identifier with `ANALYTICS_HASH_SALT` (see `analytics_hash.py`).
- `services/app/wsi_app/routers/app_auth.py` — app-access auth flows (`/api/auth/*`: state, login, logout, OAuth start/callback).
- `services/app/wsi_app/routers/sync.py` — live-sync REST + WebSocket endpoints (`/api/admin/sync/*`, `/api/sync/*`).
- `services/app/wsi_app/sync_room.py` — in-memory live-sync room manager backing `sync.py`.
- `services/app/wsi_app/dzi.py` — Deep Zoom endpoints (`/dzi/{public_id}.dzi`, `/dzi/{public_id}.szi`, `/dzi/{public_id}_files/...`).
- `services/app/wsi_app/openslide_utils.py`, `szi_utils.py` — WSI property + thumbnail helpers (OpenSlide and SZI archive paths). `open_wsi()` picks the reader for a file: the OME-TIFF reader for OME-TIFF, OpenSlide otherwise.
- `services/app/wsi_app/perf_export.py` — the Performance history's JSON / CSV exports: the self-describing JSON envelope and the column map that flattens each run's settings, headline numbers and conditions into one CSV row.
- `services/app/wsi_app/perf_bench.py` — server-side benchmark behind the admin Performance page (slide open, raw disk reads, per-zoom tile latency, throughput vs parallel requests, CPU / iowait sampling). Runs in a daemon thread of the worker that accepted the request, never touches the production handle cache, and uses its `perf_runs` row as the cross-worker job record (progress, cancel flag, result). See `operations.md` → "Performance measurement".
- `services/app/wsi_app/ome_tiff.py` — OME-TIFF reader (tifffile + imagecodecs) behind openslide-python's `AbstractSlide` interface; see [architecture.md](./architecture.md#ome-tiff) for its scope.
- `services/app/wsi_app/backup.py` — backup / restore archive logic used by `admin_backup`. `create_backup` always snapshots the whole DB, then (per the `include_analytics` / `include_history` flags) empties the ancillary table groups in the snapshot copy via `_strip_tables_in_place` before archiving; `estimate_backup_components` sizes each selectable component (WSI/thumbnails from disk, the DB groups via the `dbstat` virtual table with a row-count fallback).
- `services/app/wsi_app/disk_space.py` — the one free-space rule every large write is checked against (backup and export preflights, restore / import / reset staging and extraction, slide and archive upload spools, the copy of an imported WSI): `check(path, nbytes)` answers whether `nbytes` fit on `path`'s volume with `reserve_for(nbytes)` — 10 %, at least 256 MiB — left free, returning a `SpaceShortfall` (`needed`, `reserve`, `free`, `path`; `.message(what)` / `.as_params()`) or None; `shortfall_given(nbytes, free)` is the same against a figure measured earlier (the extraction's running total). Unknown free space (an `OSError`, or a filesystem reporting no capacity) is never "full": the check passes and the write's own ENOSPC handling decides. A new large write should call it with its own byte count rather than add another reserve rule; the only override is the operator-set `TOUR_ASSET_MIN_FREE_BYTES` for tour attachments. It imports nothing from `wsi_app`, so `backup`, `slide_transfer` and `admin_helpers` can all use it. Tests that fake the free space patch `disk_space.shutil.disk_usage` (or `disk_space.reserve_for`; see `tests/test_disk_space.py`). Operator-facing summary: [operations.md → Free-space checks](./operations.md#free-space-checks).
- `services/app/wsi_app/restore_apply.py` — the one implementation of the restore-apply swap (park the live DB / `assets/` / `wsi/` / `thumbs/` under `.pre-restore-*`, promote the staged ones) and its crash recovery from the on-disk journal. `infra/systemd/start-wsi-app.sh` runs it before uvicorn as `python -m wsi_app.restore_apply <request> <result> <journal>` from the app dir; `backup.inprocess_apply` (dev) and the lifespan's journal reconciliation (`backup.recover_interrupted_apply`) call its functions. **Standard library only** — it must never import `wsi_app` modules or third-party packages, so the boot-time run initialises none of the app; `tests/test_restore_apply_journal.py` enforces this and runs the kill-at-every-rename matrix against both entry points.
- `services/app/wsi_app/audit_log.py` — the **DB half** of the administrative action trail: `persist_audit_event` writes the `audit_logs` row behind `/admin/history`, and `prune_audit_logs` enforces `WSI_AUDIT_LOG_MAX_ROWS` / `_MAX_AGE_DAYS` at boot and opportunistically. The trail's entry point and its primary journald record are elsewhere — `admin_helpers.audit()` emits the `admin_audit` JSON line on the `wsi_app.audit` logger and then calls into this module.
- `services/app/wsi_app/error_log.py` — captures WARNING+ records from the `wsi_app` logger tree into `error_logs` (the table behind `/admin/logs`). Attached to the `wsi_app` parent logger — not root — so `sqlalchemy.*` / `uvicorn.*` records can't feed a write→log→write loop, and written off the request thread via `QueueHandler` → background `QueueListener` so a `logger.exception(...)` inside an open transaction never blocks on the SQLite writer lock. The queue handler renders a copy of each record when it is logged (message and traceback, so the row matches journald and no frames are kept alive in the queue), the queue is bounded, and each call site may store a few rows per minute (`_RepeatThrottle`); rows are stamped with the record's own time.
- `services/app/wsi_app/csv_export.py` — shared rendering for the analytics / audit / error-log CSV exports: UTF-8 BOM, formula-injection defang, and `resolve_export_limit` (retention row cap, bounded by `EXPORT_ABSOLUTE_MAX_ROWS = 1_000_000`).
- `services/app/requirements.txt` — backend Python dependency *ranges* (`requirements-dev.txt` adds test deps); `requirements.lock` / `requirements-dev.lock` — the hash-pinned sets generated from them, which is what install.sh, `make venv[-dev]` and CI install. See [Dependency locking](#dependency-locking).
- `services/app/tools/outdated.py` — developer report behind `make outdated`: direct backend dependencies with a newer release, flagging those past their `requirements*.txt` upper bound. See [Dependency locking](#dependency-locking).
- `services/app/tools/normalize_live_db.py` — one-off operator tool that repairs the schema drift `_warn_nullability_divergence` reports. See [Evolving the database schema](#evolving-the-database-schema).
- `services/app/tools/reset_mfa.py` — operator tool that turns an account's two-factor sign-in off from the server (standard library only), for when no admin can sign in to reset it. See `operations.md` → "When someone loses their device".
- `services/app/tests/` — the backend pytest suite; `infra/tests/` — the shell-side tests. See [Running the test suite](#running-the-test-suite).
- `frontend/src/pages/*` — React routes (index, viewer, tour viewer, app-access login, admin login / dashboard / account / analytics / server / performance / history / logs / tour editor); the route table is in `pages/App.tsx`.
- `frontend/src/components/`, `frontend/src/hooks/` — UI components (admin ones under `components/admin/`, the index page's under `components/index/`) and React hooks (tour viewer, live sync, analytics beacons, …).
- `frontend/src/lib/api/*` — typed clients for the backend API, one module per area; `frontend/src/lib/admin/*` — dashboard-side logic (upload queue, `apiError`, export / import progress, …); `frontend/src/lib/osd.ts`, `tileUrl.ts` — OpenSeadragon set-up and tile URLs.
- `frontend/bench/osd-render/` — the headless OpenSeadragon benchmark behind [Viewer rendering tuning](#viewer-rendering-tuning).
- `frontend/src/lib/i18n.tsx`, `frontend/src/lib/i18n/{en,ja}.ts` — UI localisation. See [Internationalisation](#internationalisation).
- `infra/install.sh` — automated native-Ubuntu installer.
- `infra/install-ops-tools.sh` — optional post-install installer for operator utilities (unattended-upgrades, fail2ban, smartmontools, sysstat, goaccess, htop, ncdu, jq, tmux). Run manually after `install.sh`; not invoked by `update.sh`.
- `infra/update.sh` — one-command updater (fetch → check out the latest `vX.Y.Z` tag → re-run `install.sh` → restart → health check, rolling back on failure).
- `infra/sync-from.sh` — copies another Tessera server's content onto this one: a standby backup server, or `--all` for a move to a new machine. See [Server-to-server sync](#server-to-server-sync-infrasync-fromsh).
- `infra/set-cache-size.sh` — interactive WSI tile cache size configurator.
- `infra/set-workers.sh` — interactive Uvicorn worker count configurator.
- `infra/lib/` — helpers the infra scripts share, read from the same tree as the script using them: `wsi-common.sh` (sourced by `install.sh`, `update.sh`, `nginx-cache.sh`, `sync-from.sh` and the Makefile's `frontend-build`: the `wsi.env` read-back, the `WSI_PUBLIC_BASE_URL` origin check, the `run_git` setup, the release-tag pick, the nginx `keys_zone` sizing), `envfile_set.py` (the `wsi.env` rewrite behind `set-workers.sh` / `set-cache-size.sh`), `replica_db.py` (the database side of `sync-from.sh`, stdlib-only so it also runs on the source fed over ssh), and `nginx_tls_carry.py` (`install.sh`'s move of a certbot-patched site file's TLS lines into `wsi-tls.conf`; see below).
- `infra/nginx-cache.sh` — interactive nginx tile cache manager (clear contents / change `max_size`).
- `infra/systemd/` — systemd units installed by `install.sh`: `wsi-app.service` (main backend), `wsi-sync.service` (single-worker live-sync on port 8001), `wsi-cache-stat.{service,timer}` (cache-size sampling), `wsi-avahi-reload.{service,path}` (mDNS reload), `wsi-restart.{service,path}` (watches `/run/wsi/restart.trigger` to restart `wsi-app` for a restore / reset apply). It also holds the helper scripts the units run: `start-wsi-app.sh` (`wsi-app.service`'s `ExecStart`), `wsi-calc.sh` (worker / cache sizing) and `wsi-cache-stat.sh` (installed as `/usr/local/bin/wsi-cache-stat`).
- `infra/nginx/wsi.conf` — nginx site config (http-context upstreams, rate-limit zones, the tile and edge-authz cache zones, CF-hop maps, the token-masking `wsi_combined` log format; in the server block the body-size / timeout defaults, security headers and CSP); `include`s the location snippet below.
- `infra/nginx/wsi-locations.conf` — reusable `location` snippet installed to `/etc/nginx/snippets/`; holds all the proxied route blocks so a second server block (e.g. a TLS vhost) can reuse them with one `include`. The `/dzi/`, `/thumbs/` and `/assets/` blocks are HTTP-cached and shared across authenticated users, gated at the edge by an `auth_request` to the internal `/_wsi_authz` → `/api/edge-authz` probe.
- `infra/nginx/wsi-tls.conf` — the operator's TLS settings (listen 443, certificate, HSTS, redirect), included by the site's server block and installed to `/etc/nginx/snippets/` by `install.sh` only when missing, so TLS survives the site file being replaced on every update; `install.sh` migrates a certbot-patched site file into it first (`infra/lib/nginx_tls_carry.py`, tested by `services/app/tests/test_nginx_tls_carry.py`, `infra/tests/test_install_nginx_restore.sh` and, against a real nginx, `infra/tests/test_tls_serving_nginx.sh`), and refuses to replace a site file with TLS it cannot attribute to certbot unless `WSI_NGINX_DISCARD_SITE_TLS=1`.
- `infra/nginx/wsi-access-log.conf` — the site's default `access_log` (the token-masking `wsi_combined` format), installed to `/etc/nginx/snippets/` by `install.sh` only when missing, so an operator's edit (`access_log off;`, their own format) survives updates.
- `infra/wsi.env.example` — environment variables template loaded by systemd.
- `docs/architecture.md` — architecture overview and supported-format table.
- `docs/operations.md` — operations reference (maintenance scripts, backup / restore, server sync, sign-in, caches, analytics).
- `docs/tour-markdown.md` — tour-step Markdown reference (the `SafeMarkdown` allow-list, tour assets, authoring limits).
- `docs/assets/generate_brand_assets.py` — regenerates `frontend/public/brand.png` (800×800) and the multi-resolution `frontend/public/favicon.ico` from the master artwork `docs/assets/brand-org.png`. Run from the repo root (`python3 docs/assets/generate_brand_assets.py`) after changing the master; both outputs are committed.

## Roles & gating

Tessera uses a three-role RBAC system (`admin` / `manager` / `instructor`).
The role is assigned at user creation and never changes — to "promote" a
user, delete and recreate the account.

The full capability matrix — which role may do what, including the
optional `⚙️ flag` permissions granted per role by an `admin` via the
`RolePermissions` singleton — lives in
[`architecture.md` → Permission matrix](./architecture.md#permission-matrix),
which is the single source of truth. This guide does not repeat it;
it only describes how those capabilities are enforced in the code below.

All admin routers gate access through one of these FastAPI dependencies
defined in `services/app/wsi_app/auth.py` (plus `require_storage_access`,
below). Each builds on `get_current_user`, which resolves the bearer token
to an active staff `User`; `GET` / `PATCH /api/admin/me`, `POST /api/admin/me/sign-out-everywhere` and the
`/api/admin/me/mfa/*` routes use it alone.

- `require_admin(user) -> User` — allows only `role == "admin"`; used by
  Users / Settings / Network / Auth / Backup / Restore / Reset routes,
  slide export / import, the WSI download, slide file-status, audit
  history, error logs, starting / cancelling / deleting performance runs,
  the performance probes and client-run results, and destructive
  analytics operations.
- `require_manager_or_above(user) -> User` — allows `admin` and
  `manager`; used by WSI upload / replace / delete / metadata-editing
  endpoints.
- `require_staff(user) -> User` — allows any of the three staff roles;
  used by slide browsing / publish toggle, tour CRUD, and synchronized
  viewing session create / list / delete.
- `require_analytics_access(user, db) -> User` — allows `admin`
  unconditionally, plus `manager` / `instructor` when their respective
  `RolePermissions.*_can_view_analytics` flag is set; used by the
  read-only Analytics dashboard endpoints.
- `require_server_access(user, db) -> User` — same shape as
  `require_analytics_access`, gated by the `*_can_view_server` flags;
  used by the Server Info read endpoints and the performance history.
- `require_storage_access(user, db) -> User` — defined in
  `routers/admin_system.py`: allows `admin` and `manager`, else falls back to
  `require_server_access`; used only by `GET /api/admin/storage`, so everyone
  who can upload can run the bulk-upload free-space preflight.

## Major endpoints
- Viewer routes — the frontend SPA, route table in `frontend/src/pages/App.tsx`. In production FastAPI's SPA fallback (`main._spa_handler`) answers every path it has no route or file for with `index.html`, except under `/api/`, `/dzi/`, `/thumbs/`, `/assets/` and `/static-assets/`, which 404; in dev Vite serves it. An unknown client-side path redirects to `/`.
  - `GET /` (slide index). Its search, view and sort state live in the URL, so a filtered view is shareable: `q`, `type` (`all` / `slides` / `tours`; absent → the **Default top-page filter** setting), `sort` (`title` / `catalog_id` / `wsi_filename`; absent → **Default sort order**), `order` (`asc` / `desc`), and the metadata filters `stain` / `organ` / `lab` / `tags` / `facet=key:value`, forwarded to `/api/slides` (`pages/IndexPage.tsx`, `components/index/results.ts`).
  - `GET /slides/:public_id` (WSI viewer). `?sync=<sid>` opens it as a live-sync follower (the presenter's share link; keyboard and pointer navigation are off while following).
  - `GET /slides/:public_id/tours/:tour_public_id` (guided tour viewer). `?step=N` (1-based, clamped to the tour's length) opens at that step, and the viewer rewrites it as the reader moves, so the address bar is always a deep link to the step on screen (step 1 drops the parameter).
  - Both viewers (and the tour editor) take `?debug=1` (`hooks/useDebugParam.ts`): OpenSeadragon's tile-boundary debug mode plus the mouse-coordinate overlay. In the viewers the toggle button is shown only when the page is opened from the dashboard (`?from=admin`, a URL flag, not a staff check); the parameter itself works for any visitor and changes nothing but the local rendering.
  - `?from=admin` (set by the dashboard's **Open Viewer** links) also makes the viewers send the browser's stored staff token, so a draft opens as a preview, and in the slide viewer shows the presenter chrome: **Start live sync** (staff token and a published slide only), the **⇄ Slide** switcher while a session is live, and **Save as preset** in **Image adjust** (manager or admin). Without it a staff member sees what a visitor sees. (Anyone's image adjustments otherwise last only until a reload or another slide; only **Save as preset** stores them for every viewer.)
  - Keyboard: the viewers keep OpenSeadragon's default keyboard navigation (pan, zoom, rotate, home) once the image has focus, except its `f` mirror key, which is disabled (`lib/osd.ts` `attachNoFlipKey`); `Esc` closes the image-adjust popover, the About dialog, the live-session join dialog and an enlarged Markdown image. There are no app-specific shortcuts.
  - Touch / page zoom: OpenSeadragon keeps its own pinch on the slide, but a pinch (or a trackpad pinch / Ctrl + wheel) that starts on a viewer's side panel or overlay is cancelled instead of zooming the whole page (`hooks/usePreventPinchZoom.ts`); the admin pages turn page zoom off on touch devices — pinch, double tap and the auto-zoom onto a focused input (`hooks/useNoPageZoom.ts`; Ctrl + wheel and a Mac trackpad pinch stay with the browser); and both viewers and the admin pages turn overscroll off — the rubber-band bounce, Android's stretch and pull-to-refresh, which the index page keeps (`hooks/useNoOverscroll.ts`). A live-sync follower has every OpenSeadragon gesture off for mouse, touch, pen and unknown pointers. The browser's own zoom commands and the OS accessibility zoom are untouched.
  - `GET /login` (`?next=<path>`: where to return after signing in; only a same-origin path is accepted, `lib/nextPath.ts`)
  - `GET /admin`, `/admin/dashboard`, `/admin/account`, `/admin/analytics`, `/admin/server`, `/admin/performance`, `/admin/history`, `/admin/logs`, `/admin/tours/:tourId`
- Public content is addressed by an unguessable `public_id` token (a random
  URL-safe string on every `Slide`/`Tour`), never the sequential integer PK.
  Every public read surface (viewer routes, the DZI/public-API routes below,
  and the tour-asset static path) accepts only the token, so link-only
  ("unlisted") content can't be found by walking ids. The integer PK stays
  internal (FKs, tile cache, analytics storage) and is still used by the
  JWT-gated `/admin/*` routes. See `models.Slide.public_id`.
  One deliberate exception: the live-sync surface (`GET /api/sync/sessions/{sid}`,
  the discovery WebSocket's `snapshot`/`started`/
  `slide_changed` events, and the `view` frames relayed to followers) also
  carries the numeric `slide_id` next to `slide_public_id`. Followers navigate
  and fetch tiles by `slide_public_id` only; `slide_id` is kept on the wire for
  internal correlation (the presenter's `view` frames are validated against it)
  and the public peek / discovery paths only surface sessions on
  published slides. Do not rely on it as a public identifier.
- DZI routes (keyed by the slide's `public_id`, versioned by `?v=`):
  - `GET /dzi/{public_id}.dzi?v=N` (OpenSlide-backed sources)
  - `GET /dzi/{public_id}.szi?v=N` (raw SZI archive passthrough for `szi-tile-source`)
  - `GET /dzi/{public_id}_files/{level}/{col}_{row}.jpeg?v=N`
  - `v` is the slide's WSI content generation (`Slide.wsi_generation`,
    reported as `tile_version` on the slide detail endpoints and the admin
    list; moved by every WSI replace to `max(previous + 1, now in ms)`, so a
    restore that rolls it back can never lead to a number being reissued). The viewer only puts it on the
    descriptor / archive URL — OpenSeadragon copies the `.dzi` URL's query onto
    every tile URL (`DziTileSource.queryParams`) — and builds both through
    `frontend/src/lib/tileUrl.ts`. The current `v` is served and edge-cached
    under a key that includes it; any other `v` (stale, non-canonical such as
    `03`, duplicated, or a percent-encoded name) is a 404 with
    `X-Accel-Expires: 0`, so nginx never stores it; no `v` at all is still
    served (a script or other non-SPA caller) but marked the same way and
    bypassed by nginx. Code that fetches tiles itself must add `v` to hit the
    cache (see `lib/admin/perfClientTest.ts`).
- Public API routes:
  - `GET /api/facets` — `{groups: {key: [{value, count, all_count,
    tour_count}]}}`: per value, the size of the "Slides" (`count`, which
    orders each group), "All" and "Tours" views' results for that value
    alone (see [Tour metadata](#tour-metadata)), plus registered options at
    0. Draft and link-only content counts nowhere. The registered options
    (`MetadataOption`, the shared vocabulary the dashboard offers) are
    listed whether or not any visible slide carries them, so a value that
    only a draft or link-only slide uses still *appears* — at 0, matching
    nothing; it is the registry that is public, not the content. Keep
    anything confidential out of the metadata options.
  - `GET /api/slides`, `GET /api/slides/{public_id}`, `GET /api/slides/{public_id}/macro`
    - The listing takes `q`, the metadata filters `stain` / `organ` / `lab` /
      `tags` (repeatable) and `facet=key:value` (repeatable; OR within a key,
      AND across keys), `sort` (`title` / `catalog_id` / `wsi_filename`,
      else id) with `order`, and `page` / `per_page` (≤ 5000; the index page
      sends `max_index_items`). A slide matches by itself when its own
      metadata passes the filters and `q` hits its numeric id, catalog id,
      title or any of its attribute values (case-insensitive `LIKE`). Unless
      `match_tours=false` (the index page's "Slides" view), it also matches
      through one of its published, listed tours: one whose effective
      metadata — its slide's plus its own — passes the filters (where
      `GET /api/index/tours` looks at the tour's own values only) and whose
      own fields (title, metadata values) or slide's fields `q` hits. Cap and
      `total` apply to that combined match set, so `total` is the size of the
      result itself. Each item carries `matched_tours: [{id, public_id,
      title}]`: in creation order, the matching tours that add something the
      slide lacks — a `q` hit on their own fields, or, when the slide's own
      metadata fails the filters, the metadata that passes them (empty
      without `q` or a filter, or with `match_tours=false`).
      Draft, unlisted and unlisted-parent content never matches.
  - `GET /api/slides/{public_id}/tours`
  - `GET /api/tours`, `GET /api/tours/{public_id}`
    - The listing takes no parameters: every published, listed tour on a
      published, listed slide, ordered by `(slide_id, id)`, without slide data.
  - `GET /api/index/tours` — the index page's "Tours" view. Same visibility
    as `GET /api/tours`, and the same `q` / filter / `sort` / `order` /
    `page` / `per_page` parameters as `GET /api/slides`: a tour matches `q`
    on its own title or metadata values, or when its parent slide matches on
    the slide's own fields (a sibling tour's doesn't count); the filters
    apply to the tour's own metadata only (a value its slide passes down
    doesn't count, so a filter lists the tours given that value
    themselves); results come in the slide list's order, then tour creation order
    — except `sort=title`, which orders by the tour's own title first (then
    the slide list's order, then creation order).
    Returns `{items, total, page, per_page}`, each item a tour summary with
    its own `attributes`, plus `slide: {id, public_id, catalog_id, title,
    thumb_url, image_adjust_brightness, image_adjust_contrast,
    image_adjust_saturation, attributes}` (the parent's card data). Kept outside `/api/tours/` so the path can't
    shadow a tour `public_id`.
  - `GET /api/viewer/settings`, `GET /api/viewer/logo`, `GET /api/viewer/header-default-background`, `GET /api/viewer/seasonal-background/{slot}`
  - `GET /favicon.ico` (also `HEAD`, which the LAN-direct probe sends)
  - `GET /api/health`
- App-access auth (gates the public viewer when enabled):
  - `GET /api/auth/state`
  - `POST /api/auth/login`, `POST /api/auth/logout`
  - `GET /api/auth/oauth/start`, `GET /api/auth/oauth/callback`
  - `GET /api/edge-authz` — internal authorization probe for nginx
    `auth_request`; returns 204 when the app-access middleware admitted the
    request, else 401. nginx gates the shared-cache read surfaces (`/dzi/`,
    `/thumbs/`, `/assets/`) against it so a cache HIT can't bypass auth. For a
    `/dzi/` URI (nginx sends the original URI as `X-Original-URI`) it also
    applies the per-slide publish gate — 403 unless the slide exists and is
    published or the request carries a valid staff bearer — read from the
    in-memory slide resolve cache; nginx caches the allow per client and slide
    for 60 s. Not called directly by the SPA. Deliberately outside the
    `/api/auth/` bypass prefix so it is gated like the surfaces it guards.
- Live sync:
  - `POST /api/admin/sync/sessions` (staff), `DELETE /api/admin/sync/sessions/{sid}` (staff; with `?client_id=` — the presenter page's end-on-leave — it is ignored once another presenter tab/device has attached)
  - `GET /api/admin/sync/sessions` (staff; the caller's own live sessions for the resume affordance, each flagged `presenter_connected`)
  - `GET /api/admin/sync/sessions/all` (staff; every active session with triage fields for the management UI — used to force-end an orphan)
  - `GET /api/sync/sessions/{sid}` (peek one session; there is no public list)
  - `WS /api/sync/ws/{sid}` (presenter / follower fan-out)
  - `WS /api/sync/discovery` (live-room presence)
- Admin routes (JWT-gated; representative selection — see the source for the full set). The required role is shown in parentheses using the convention from [Roles & gating](#roles--gating): `admin`, `manager+` (admin or manager), `staff` (any of the three), `analytics-access` / `server-access` (admin plus the matching `RolePermissions` flag):
  - `POST /api/admin/login`; `POST /api/admin/register` only when `ALLOW_ADMIN_REGISTER=1` **and** no `admin` row exists yet (the gate is re-checked under a process lock + file lock inside the transaction, so two concurrent first-launch callers can't both create one); it 403s once the first admin is in place
  - `POST /api/admin/login` answers `{access_token}`, or `{mfa_required: true, mfa_token}` for an account with two-factor sign-in on; `POST /api/admin/login/mfa` (body `mfa_token` + `code`, a 6-digit TOTP or a `XXXXX-XXXXX` recovery code; 401 `adminMfa.err.invalidCode` / `adminMfa.err.sessionExpired`, 503 `adminMfa.err.keyUnavailable` without a working `WSI_MFA_KEY`) — rate-limited, and behind the app-access gate like the login itself: under an app-access mode, a request the gate did not admit gets 401 `admin.err.appAuthRequired` before any credential is checked (the SPA then sends the visitor to `/login?next=/admin`). See `operations.md` → "Two-factor sign-in (dashboard)"
  - `GET /api/admin/me` (includes `mfa: {enabled, enabled_at, recovery_codes_remaining, available}`), `PATCH /api/admin/me` (change own username / password) — any authenticated staff
  - `POST /api/admin/me/sign-out-everywhere` — bumps the caller's `token_version`, ending every session of the account (this one included; no replacement token) and the trust of its known-device cookies; audited as `admin.account.sign_out_everywhere`. Any authenticated staff, own account only
  - `POST /api/admin/me/mfa/setup` (`current_password` → `secret`, `otpauth_uri`, `setup_token`), `POST /api/admin/me/mfa/confirm` (`setup_token` + first `code` → `recovery_codes`, fresh `access_token`), `POST /api/admin/me/mfa/disable` and `POST /api/admin/me/mfa/recovery-codes` (`current_password` + `code`; both bump `token_version` and return a fresh `access_token`; a wrong password counts in the request token's re-auth bucket, a code, once the password is right, is charged against the account's database-backed sign-in lockout (`ADMIN_LOGIN_MAX_FAILURES`) like the sign-in's code step, released when the code is spent — so wrong codes here count towards locking sign-in too, and neither a correct password elsewhere nor a freshly minted token buys fresh guesses. With no recovery code left, a session that signed in with the last one (its token carries `amr: "recovery"`, kept across a password change) may send an empty `code`; any other empty `code` is 400 `adminMfa.err.codeRequired`, before anything is charged) — any authenticated staff, own account only. Enabling, disabling and new recovery codes each bump `token_version` and return a replacement token
  - `GET /api/admin/slides` (staff), `GET /api/admin/slides/{slide_id}` (staff), `POST /api/admin/slides/upload` (manager+)
  - `GET /api/admin/slides/file-status` (admin), `POST /api/admin/slides/rebuild-thumbnails` (manager+)
  - `GET /api/admin/slides/{slide_id}/macro`, `GET /api/admin/slides/{slide_id}/label` (staff)
  - `POST /api/admin/slides/{slide_id}/download_url`, `GET /api/admin/slides/{slide_id}/download` (admin; the GET accepts admin Bearer auth or the short-lived signed `?t=<token>` URL minted by the POST)
  - `PATCH /api/admin/slides/{slide_id}` (manager+) — metadata, MPP, overlay overrides, image-adjustment preset, and image credit (`credit_json`, one object that replaces all three credit fields — see [Image credit](#image-credit-admin)). Omitted fields are left alone, and an empty value counts as omitted, so blanking the catalog id or title takes `clear_catalog_id=1` / `clear_title=1` (refused with 400 on a published slide); `DELETE /api/admin/slides/{slide_id}` (manager+)
  - `PUT /api/admin/slides/{slide_id}/image-adjust` (manager+) — save just the per-slide brightness/contrast/saturation preset (used by the in-viewer adjust panel). See [Image-adjustment presets](#image-adjustment-presets-admin).
  - `POST /api/admin/slides/{slide_id}/publish` (staff)
  - `POST /api/admin/slides/{slide_id}/unlisted` (staff) — toggle link-only visibility. Orthogonal to publish and settable in any publish state; see [Content visibility](./architecture.md#content-visibility).
  - `POST /api/admin/slides/{slide_id}/wsi` (manager+) — replace WSI binary
    - Both upload and replace reject a filename that is already used by another
      slide (case-insensitive match on the name as `safe_filename` sanitises
      it, before the `{id}_` storage prefix — so `a b.svs` and `A_B.svs`
      collide) with HTTP 409. Replace excludes the target slide itself,
      so re-uploading a slide's own file (e.g. after a metadata-only restore)
      is allowed.
    - Replace stages the new bytes as `wsi/{id}_{name}.upload-<tok>.<ext>`
      (thumbnail `thumbs/{id}.upload-<tok>`) and parks the old file as
      `<new destination>.rollback-<tok>` until the row commits. A worker
      killed in between leaves them; the startup sweep
      `admin_helpers.reap_interrupted_slide_swaps` (under each slide's lock;
      only names of exactly those shapes, 12-hex-digit token included, and
      never a name a row references) removes staged copies older than an
      hour and puts a park back over the row's path — moving the slide to a
      fresh tile generation, as the request's own undo does — or drops it,
      by which file the row's `file_size_bytes` describes (equal sizes: a
      sampled byte comparison), leaving an ambiguous one for the operator.
      A thumbnail park follows its WSI park's verdict.
  - `POST /api/admin/slides/bulk/delete` (manager+), `POST /api/admin/slides/bulk/attributes` (manager+), `POST /api/admin/slides/bulk/publish` (staff) — batch operations over a `slide_ids` list (capped at 500 per request); each returns a per-slide result so a single failure never aborts the batch. See [Bulk slide operations](#bulk-slide-operations-admin).
  - `GET /api/admin/slides/{slide_id}/tours`, `POST /api/admin/slides/{slide_id}/tours` (staff)
  - `GET /api/admin/tours`, `GET /api/admin/tours/{tour_id}`, `PATCH /api/admin/tours/{tour_id}`, `DELETE /api/admin/tours/{tour_id}` — all `staff`. The PATCH's `attributes` replaces the tour's own metadata; see [Tour metadata](#tour-metadata).
  - `POST /api/admin/tours/{tour_id}/publish` (staff)
  - `POST /api/admin/tours/{tour_id}/unlisted` (staff) — link-only visibility, mirroring the slide endpoint above
  - `POST /api/admin/tours/{tour_id}/duplicate` (staff) — form field `title` (omitted: the source's). Creates a draft copy on the same slide: description, own metadata, link-only flag, every step with its pins and shapes, and the uploaded assets, copied under the copy's own `public_id` with the step markdown's `/assets/tours/<source>/` references rewritten to it. Returns `{id, public_id}`; 507 `adminDash.apiErr.uploadNoSpace` when the assets would cross `TOUR_ASSET_MIN_FREE_BYTES`.
  - `POST /api/admin/tours/bulk/visibility`, `POST /api/admin/tours/bulk/attributes`, `POST /api/admin/tours/bulk/delete` (staff) — batch operations over the tours selected in the dashboard's slide list (capped at 500 per request), with a per-tour result. See [Bulk tour operations](#bulk-tour-operations-admin).
  - `POST /api/admin/tours/{tour_id}/steps` (staff)
  - `PATCH /api/admin/tours/{tour_id}/steps/{step_id}` (staff)
  - `PATCH /api/admin/tours/{tour_id}/steps/reorder` (staff)
  - `DELETE /api/admin/tours/{tour_id}/steps/{step_id}` (staff; 404 when the step belongs to another tour)
  - `POST /api/admin/tours/{tour_id}/steps/{step_id}/pins`, `POST /api/admin/tours/{tour_id}/steps/{step_id}/shapes` (staff) — append one pin / shape atomically ("Copy to next step")
  - Every tour / step mutation above except tour create, tour duplicate, tour delete (single and bulk) and the pin / shape appends takes an optional `expected_revision`; see [Tour editing concurrency](#tour-editing-concurrency).
  - `GET /api/admin/tours/{tour_id}/assets` (staff) — `{items, max_bytes}`; `max_bytes` is `TOUR_ASSET_MAX_BYTES`, which the tour editor checks and names before uploading
  - `POST /api/admin/tours/{tour_id}/assets/upload` (staff) — bounded per file by
    `TOUR_ASSET_MAX_BYTES` and per tour by `TOUR_ASSET_MAX_FILES` /
    `TOUR_ASSET_MAX_TOTAL_BYTES` (either set to `0` disables that check).
    nginx caps the location at `3m`, raised by `install.sh`
    (`apply_tour_asset_body_cap`) to fit a larger `TOUR_ASSET_MAX_BYTES`.
    The per-tour caps exist because the endpoint is open to the lowest
    staff role and the default `rename` policy mints a fresh filename per
    upload, so the per-file cap alone bounds nothing in aggregate.
    Replacing an existing name is measured against post-swap usage, so an
    author at the cap can still re-upload a corrected image. Because the
    same role can create tours, each with a fresh quota, there is also an
    instance-wide floor: 507 `adminDash.apiErr.uploadNoSpace` once the
    write would leave less than `TOUR_ASSET_MIN_FREE_BYTES` (1 GiB; `0`
    disables) free on the asset directory's volume, replacements included.
  - `DELETE /api/admin/tours/{tour_id}/assets/{name}` (staff). `name` is the exact stored name (as the asset list reports it), held to what an upload or import can store (`admin_helpers.storable_asset_name`: `[A-Za-z0-9._-]`, no leading dot, at most 255 characters — rename mode's suffix can take a name past `safe_filename`'s 200-byte cap); anything else answers 400.
  - `GET/POST/DELETE /api/admin/metadata/options`, `PATCH /api/admin/metadata/options` (rename a value in place — updates the `MetadataOption` master row **and** every `slide_attributes` / `tour_attributes` row carrying the old `(key, value)`, so slides and tours keep their assignment; they are string-linked with no FK, which is why a delete + re-add would orphan them), `GET /api/admin/metadata/options/usage`, `GET /api/admin/metadata/counts`, `GET /api/admin/metadata/keys/usage`, `DELETE /api/admin/metadata/keys` — all `manager+`, except `GET /api/admin/metadata/options` (the value list), which is `staff` because the tour editor picks from it. The usage, delete and rename endpoints count and rewrite tours' own metadata along with the slides', and the counts endpoint reports tours separately (`tour_keys` / `tour_values`).
  - `GET/PATCH /api/admin/viewer/settings`; `POST/DELETE /api/admin/viewer/settings/logo`; `POST/DELETE /api/admin/viewer/settings/favicon`; `POST/DELETE /api/admin/viewer/settings/header-default-background`; `POST /api/admin/viewer/header-default/reset` — `admin`
  - `GET /api/admin/viewer/seasonal-backgrounds`; `PATCH /api/admin/viewer/seasonal-backgrounds/{slot}`; `GET` (dashboard preview; serves a disabled slot too, unlike the public route) / `POST /api/admin/viewer/seasonal-backgrounds/{slot}/image`; `DELETE /api/admin/viewer/seasonal-backgrounds/{slot}/image`; `POST /api/admin/viewer/seasonal-backgrounds/{slot}/reset` — `admin`
  - `GET/PATCH /api/admin/network/settings`, `GET /api/admin/network/my-ip` — `admin`
  - `GET/PATCH /api/admin/auth/settings`, `POST /api/admin/auth/ldap/test`, `GET/POST /api/admin/auth/users`, `PATCH /api/admin/auth/users/{user_id}`, `DELETE /api/admin/auth/users/{user_id}` — `admin`. Under `basic` every page load, the dashboard's included, needs a Basic user, so the PATCH refuses a switch to `basic` while no Basic user exists (422 `adminDash.apiErr.basicNeedsUser`) and the DELETE refuses the last one while the mode is `basic` (422 `basicLastUser`).
  - `GET /api/admin/users`, `POST /api/admin/users`, `PATCH /api/admin/users/{user_id}` (suspend / re-enable via `{"is_active": bool}` — revokes access without deleting the account, bumping `token_version` so in-flight JWTs stop being accepted; same self / last-admin guards as delete), `DELETE /api/admin/users/{user_id}`, `POST /api/admin/users/{user_id}/mfa/reset` (body: the acting admin's `current_password`; turns another member's two-factor sign-in off and bumps their `token_version`; 400 `adminMfa.err.cannotResetSelf` for one's own), `GET/PATCH /api/admin/role-permissions` — `admin`. User rows carry `mfa_enabled`
  - `GET /api/admin/server/info`, `GET /api/admin/stats` — `server-access`
  - `GET /api/admin/storage` (data-dir free/total/used bytes, `max_upload_bytes` — `WSI_MAX_UPLOAD_BYTES`, which the Upload tab and **Replace WSI file** check and name — plus `upload_space`: the free bytes of the volumes a slide upload is checked against — `wsi/`'s and the spool's, from `admin_helpers.upload_space_volumes` — and the `disk_space` reserve policy, which `frontend/src/lib/admin/uploadSpace.ts` simulates per file; no paths; the bulk-upload free-space preflight) — `manager+` or `server-access`, since everyone who can upload must be able to see whether the files fit
  - `GET /api/admin/perf/runs` (newest-first history, summaries only, plus `running_id`), `GET /api/admin/perf/runs/{run_id}` (full result; progress while running), `GET /api/admin/perf/export.json` / `export.csv` (`?ids=1,2,…`, default all; every run with the conditions it was measured under — see `perf_export.py`) — `server-access`. `POST /api/admin/perf/runs` (body `slide_ids[]` ≤ 3 — empty = the largest slides — `cold`, `tiles_per_band`, `max_concurrency` (0 = auto), `step_seconds`; 202 + `{id}`; 409 while another run or a backup / restore / transfer job is in flight), `POST /api/admin/perf/runs/{run_id}/cancel`, `DELETE /api/admin/perf/runs/{run_id}`, `POST /api/admin/perf/client-runs` (stores a browser-test result, ≤ 128 KiB), `GET /api/admin/perf/ping`, `GET /api/admin/perf/payload?size=` (incompressible bytes for the download probe, 64 KiB–64 MiB) — `admin`. See `operations.md` → "Performance measurement".
  - `POST /api/admin/backup` (body: `include_wsi` / `include_analytics` / `include_history` booleans, default-true, plus `note`; `include_wsi` maps to the metadata/full scope, the other two strip their DB table groups from the snapshot), `GET /api/admin/backup`, `GET /api/admin/backup/estimate` (per-component byte estimate for the create-backup size hints; reused for 5 minutes per worker), `GET /api/admin/backup/jobs/active` (`{job}` — the job holding the single slot, whoever started it, or `null`), `GET /api/admin/backup/jobs/{job_id}`, `POST /api/admin/backup/{backup_id}/verify` (`202 {job_id}`, a `backup_verify` job reading the archive end to end against its codec checksums and recorded sha256; a damaged archive fails it with `result.damaged: true`, any other failure — an I/O error, its worker exiting — without), `POST /api/admin/backup/jobs/{job_id}/cancel` (backup and `backup_verify` jobs only; `202`, the job then ends `cancelled`), `POST /api/admin/backup/{backup_id}/download_url` (mints a signed URL valid for `WSI_DOWNLOAD_TOKEN_TTL_SECONDS`, default 3600 s), `GET /api/admin/backup/{backup_id}/download` (admin Bearer **or** `?t=<token>`), `DELETE /api/admin/backup/{backup_id}` (409 while a restore is staged from it or it is being verified) — `admin`
  - `POST /api/admin/restore` (multipart `file` **or** `snapshot_id`, optional `replaces_stage_id` — the stage this one replaces, removed by the job before it measures free space; `202 {job_id}` — staging runs as a job polled at `/api/admin/backup/jobs/{job_id}`, whose `result` carries `stage_id` / `manifest` / `plan`), `GET /api/admin/restore/preflight/{stage_id}`, `POST /api/admin/restore/apply`, `GET/DELETE /api/admin/restore/result`, `DELETE /api/admin/restore/stage/{stage_id}` — `admin`
  - `GET /api/admin/restore/rollbacks` (list parked `.pre-restore-*` rollback trees + their size), `DELETE /api/admin/restore/rollbacks/{name}` (reclaim one; refused while a restore-apply is pending restart) — `admin`. These trees are the automatic-rollback artefacts a restore/reset apply parks under `DATA_DIR`; they are never auto-deleted (a full-scope apply parks a complete copy of `wsi/`), so this surfaces them for manual reclamation.
  - `GET /api/admin/reset/preflight`, `POST /api/admin/reset` (`202 {job_id, stage_id, scope}`; the job builds the reset stage, then applies it like a restore) — `admin`. A restore or reset apply signs every staff token and app-access session out (`users.token_version` / `auth_settings.session_generation` are moved past both sides of the swap). Reset preserves admin/config rows: `users`, `app_meta`, `viewer_settings`, `seasonal_backgrounds`, `metadata_options`, `network_settings`, `auth_settings`, `auth_users`, `role_permissions`; everything else (slides, tours, analytics, audit, plus the `wsi/`/`assets/`/`thumbs/` trees) is wiped.
  - `POST /api/admin/slides/export` (body `slide_ids[]` + `note`), `GET /api/admin/slides/transfer/jobs/{job_id}`, `GET /api/admin/slides/export/history` (bundles built and still awaiting download — they sit in `BACKUP_DIR/.staging` until the staging sweep reclaims them — the first one to run once a bundle is over 24 h old, at a worker start or when a restore / slide import is staged or a reset prepared, not on a timer — so this is the window in which one can be re-downloaded), `DELETE /api/admin/slides/export/{export_id}` (reclaim one early), `POST /api/admin/slides/export/{export_id}/download_url` (mints a signed URL), `GET /api/admin/slides/export/{export_id}/download` (admin Bearer **or** `?t=<token>`), `POST /api/admin/slides/import` (multipart bundle → 202 `{job_id}` of a staging job, kind `slide_import_stage`, whose result is `{stage_id, plan}`), `POST /api/admin/slides/import/apply` (body `stage_id` + `resolutions`; re-running it on a kept stage is the "retry failed slides" action), `POST /api/admin/slides/import/retry-assets` (body `stage_id`; job that re-copies attachments an apply skipped because of this system's limits), `GET /api/admin/slides/import/stage/{stage_id}` (`{stage_id, plan}`: recomputed preflight of a stage still on disk, with each slide's earlier `apply_status`; 404 `importStageMissing` once it is gone), `DELETE /api/admin/slides/import/stage/{stage_id}` (drop a staged or kept bundle; 409 while an apply / attachment retry runs) — `admin`. Per-slide export/import for cross-Tessera migration; see [Slide export / import](#slide-export--import-admin). (The history lives one segment deeper than a bare `GET /api/admin/slides/export` would, because `GET /api/admin/slides/{slide_id}` in the slides router matches that path first and 422s on `"export"`.)
  - `GET /api/admin/analytics/dashboard`, `GET /api/admin/analytics/summary`, `GET /api/admin/analytics/top-slides`, `GET /api/admin/analytics/top-tours`, `GET /api/admin/analytics/referrers`, `GET /api/admin/analytics/ua-breakdown`, `GET /api/admin/analytics/top-users`, `GET /api/admin/analytics/recent`, `GET /api/admin/analytics/tour-completion`, `GET /api/admin/analytics/slide-dwell`, `GET /api/admin/analytics/export.csv` — `analytics-access`; `DELETE /api/admin/analytics/data` (`?from_date=…&to_date=YYYY-MM-DD`, or `?all=1` for everything — no scope is a 400), `DELETE /api/admin/analytics/user` and `POST /api/admin/analytics/user/preview` (form field `identifier`, kept out of the URL and so out of access logs), `POST /api/admin/analytics/pseudonymize` (409 `analyticsSaltUnavailable` without a salt, 409 `analyticsPseudonymizeNeedsHashed` while the mode is `plain`) — `admin`. The deletes and the pseudonymize answer `"complete": false` when they stopped at their per-request time budget; the caller repeats the identical request until `"complete": true` (an `all=1` wipe always completes in one call). The report endpoints answer 503 `adminDash.apiErr.analyticsQueryTimeout` when their queries exceed 50 s
  - `GET /api/admin/history`, `GET /api/admin/history/export.csv` — `admin`. The administrative audit trail behind `/admin/history`; filters: `action` (exact action name, or a category prefix ending in `.` such as `slide.`), `actor` (part of the username, case-insensitive; for a failed login, of the name tried), `target` (part of the target), `system` (only rows with no actor), and an inclusive `since` / `until` date range interpreted in `WSI_ANALYTICS_TIMEZONE`. The listing answers `snapshot_id` (pass it back as `max_id` on later pages so new rows don't shift them) and `newer` (matching rows above the snapshot). See `operations.md` → "Audit history".
  - `GET /api/admin/logs`, `GET /api/admin/logs/export.csv`, `GET /api/admin/logs/{id}`, `DELETE /api/admin/logs?max_id=` — `admin`. The captured WARNING+ rows behind `/admin/logs`; filters: `level`, `logger` (exact name or a prefix ending in `.`), `search` (substring of the message, traceback or request path), and `since` / `until`. The listing leaves the traceback out (`has_traceback` says whether there is one; `GET …/{id}` returns it), and answers `snapshot_id` / `newer` as the history does plus `level_counts` (per level, under the other filters). The export takes `include_traceback=0` to leave that column empty. The `DELETE` first writes a marker row (logger `wsi_app.error_log.clear`) and then removes the rows with `id <= max_id` below it, so the table never empties and ids keep growing; with nothing but earlier markers to remove it answers `{"deleted": 0}` and writes nothing (audited as `system.error_logs.clear` otherwise). Both CSV exports read in id-keyset batches (`admin_reports.iter_newest_first`) and share a per-worker limit of two concurrent downloads (`csv_export.claim_export_slot` + `slot_csv_response`, whose response releases the slot in a `finally` around its ASGI call, however the response ends, and which closes the request's auth session before the body streams; 409 `adminExport.busy`). See `operations.md` → "Error logs".
    - Both exports share `csv_export.resolve_export_limit`: the row ceiling follows that table's retention cap (`WSI_AUDIT_LOG_MAX_ROWS` / `WSI_ERROR_LOG_MAX_ROWS`) and is bounded by `EXPORT_ABSOLUTE_MAX_ROWS = 1_000_000`, so a disabled retention cap can't stream an unbounded table. An explicit `?limit` only narrows further.
- Public ingest:
  - `POST /api/analytics/pageview` (204)
  - `POST /api/analytics/slide-dwell` (204) — per-session viewer dwell, skipped for anonymous viewers
  - `POST /api/analytics/tour-event` (204) — tour `start` / `progress` / `complete` beacons keyed by a client-minted UUID. A reported `total_dwell_ms` is capped at the session's span — `started_at` → `completed_at`, or → the progress beacon's `sent_at` (a beacon without one is capped at the server's now) — and at `WSI_ANALYTICS_MAX_DWELL_MS`. Those stamps are taken on the client's own clock only while each lies within the 5-minute skew allowance of its beacon's receipt (`routers/analytics._CLIENT_CLOCK_SKEW`); one further off is clamped to the receipt time ± the allowance (`_receipt_offset`), so a span is receipt-anchored beyond it and can grow only with the server's clock

### Tile and API URL layout
- Tiles are exposed as **Deep Zoom (DZI)** under `/dzi/`, keyed by the slide's `public_id` token and versioned by its WSI generation (`?v={tile_version}`; the routes are listed under DZI routes above).
  - SZI archives (`.szi`) bypass OpenSlide and are streamed via `/dzi/{public_id}.szi?v=N` for the `szi-tile-source` library to read tiles directly out of the ZIP. Unlike DZI tiles, the `.szi` path is **not** nginx-cached — a nested `location ~ \.szi$` under `/dzi/` sets `proxy_cache off` so the client's HTTP `Range` reaches the app and it returns `206` partial content directly. Caching a `.szi` would make nginx strip the `Range` and pull the whole (multi-GB) archive on the first read; see `infra/tests/test_szi_range_nginx.sh` and the Cache Layers note in `architecture.md`.
- Public slide listing & tours are under `/api/*`.
- Admin endpoints are under `/api/admin/*` (JWT-gated by the FastAPI app).

## Local development (also native)

You can run the stack directly on your workstation without `systemd`.
The included `Makefile` expects `python3` and Node ≥24 on the host. The
Python must be 3.12 or newer, the oldest the pinned dependencies are resolved
for (the Ubuntu 24.04 and 26.04 defaults are fine).

```bash
# First time only
make install                # creates .venv and runs npm ci (make dev-install adds the test deps)

# Terminal 1: backend
export DATABASE_URL=sqlite:///$PWD/data/wsi.db
export DATA_DIR=$PWD/data
export BACKUP_DIR=$PWD/data/backups          # default: /var/lib/wsi/backups
export JWT_SECRET=$(openssl rand -hex 48)    # the app refuses to start without one
export ADMIN_BOOTSTRAP_PASSWORD='dev-password' # 8+ chars; seeds user "admin" on first boot
export WSI_MFA_KEY=$(openssl rand -hex 32)   # optional; two-factor sign-in cannot be turned on without it
mkdir -p data/wsi data/thumbs data/cache data/assets/tours data/backups
make run-app                # uvicorn --reload at 127.0.0.1:8000

# Terminal 2: frontend
make run-frontend           # Vite dev server at 127.0.0.1:5173
```

`WSI_ALLOW_INSECURE_DEFAULTS=1` lets the backend start without `JWT_SECRET` and `ADMIN_BOOTSTRAP_PASSWORD` —
with a per-process random JWT secret (every `--reload` signs you out) and an
`admin` / `changeme` bootstrap account — for development only. With no
`ANALYTICS_HASH_SALT` it also generates a per-process one, so the
pseudonymous analytics mode can be selected. `BACKUP_DIR`
also holds the background-job store, so without a writable one every backup,
restore and slide export / import is refused. Restore and reset normally
apply through systemd (a request file under `/run/wsi` and a service
restart); `BACKUP_DEV_INPROCESS_APPLY=1` swaps the data in the running
process instead, which is what a dev box without the units needs (never in
production). The outcome is still reported in a result file under
`/run/wsi` (which the dashboard reads after the apply signs everyone out),
and a boot without that directory logs `RESTORE: '/run/wsi' does not
exist`; on a dev box point both files at a writable directory (in Terminal 1,
before `make run-app`, from the repository root):

```bash
export BACKUP_DEV_INPROCESS_APPLY=1
export RESTORE_REQUEST_PATH=$PWD/data/run/restore.request
export RESTORE_RESULT_PATH=$PWD/data/run/restore.result
mkdir -p data/run
```

Open `http://127.0.0.1:5173/` for hot-reload dev UI. Vite proxies
`/api`, `/dzi`, `/thumbs`, `/assets` to `127.0.0.1:8000` (override
with `VITE_API_PROXY_TARGET=...`).

Health check:
```bash
curl http://127.0.0.1:8000/api/health
# -> {"ok":true}
```

The generated OpenAPI schema and API browsers (`/openapi.json`, `/docs`,
`/redoc`) are off by default because they are unauthenticated and list every
admin route. Export `WSI_API_DOCS=1` before `make run-app` to serve them.

For a "production-style" local build, `make frontend-build` copies
the built frontend into `services/app/static/` so the FastAPI app
serves the SPA directly on port 8000. `index.html` (and the SPA fallback for
client-side routes) goes out with `Cache-Control: no-cache`, the content-hashed
files under `/static-assets/` with `public, max-age=31536000, immutable`, and a
missing `/static-assets/` file is a 404 rather than the SPA shell, so a page
loaded before a deploy fails its chunk import (and reloads) instead of
rendering blank. The static root is resolved per request, so install.sh's
atomic `static` symlink flip takes effect without a restart.

### Common dev commands
```bash
make venv              # create .venv and install backend deps
make venv-dev          # venv + test deps (requirements-dev.lock)
make lock              # re-resolve the *.lock files after editing requirements*.txt (needs uv)
make lock-upgrade      # move every locked pin to the newest allowed release (needs uv)
make outdated          # direct deps with a newer release, incl. past a range cap
make install           # venv + frontend-install
make dev-install       # venv-dev + frontend-install
make frontend-install  # npm ci (falls back to npm install without a lockfile)
make frontend-build    # build frontend into services/app/static/
make nginx-config WSI_LAN_HOST=host  # emit infra/nginx/wsi.conf.generated
                       # with that host in the CSP (refuses to run without it)
make lint              # Python compileall + frontend build sanity check
make run-app           # uvicorn reload for backend
make run-frontend      # Vite dev server
make test              # curl /api/health against a running dev backend
make static            # alias for frontend-build
make clean             # remove .venv, frontend/node_modules, frontend/dist,
                       # services/app/static
```

### Running the test suite

`make test` is only a health-check curl. The backend's pytest suite
(`services/app/tests/`, config in `services/app/pytest.ini`) is run directly:

```bash
make dev-install                       # or: make venv-dev
cd services/app && ../../.venv/bin/pytest
```

The whole suite (some 2,900 tests) takes a few minutes — about four on a
small machine; while iterating, run one file or a `-k` match
(`../../.venv/bin/pytest tests/test_migrations.py`). CI runs
`python -m pytest -q -p no:cacheprovider` from the same directory.

`requirements-dev.txt` pulls in `pytest`, `pytest-asyncio` (`asyncio_mode =
auto`, so `async def` tests need no marker) and `openslide-bin`, which ships
the OpenSlide shared library as a wheel — that is what lets
`tests/test_openslide_utils.py` `import openslide` on a machine with no
system OpenSlide packages. The tests build their own SQLite DB per run and
need no running server.

The shell-side units are separate and self-contained:

```bash
bash infra/tests/test_wsi_calc.sh          # worker / cache-byte arithmetic
bash infra/tests/test_install_env_persist.sh # install.sh's persist_env_var():
                                           # the only code that rewrites an
                                           # existing /etc/wsi/wsi.env
bash infra/tests/test_install_upload_cap.sh # install.sh's apply_upload_body_cap() /
                                           # apply_tour_asset_body_cap(): nginx's
                                           # slide upload / replace cap follows
                                           # WSI_MAX_UPLOAD_BYTES, the tour-asset
                                           # cap TOUR_ASSET_MAX_BYTES
bash infra/tests/test_szi_range_nginx.sh   # boots nginx on 127.0.0.1:18080 with
                                           # the real wsi-locations.conf included
bash infra/tests/test_rate_limits_nginx.sh # same harness: asserts every proxied
                                           # /api/ location is metered and that
                                           # throttling answers 429, not 503;
                                           # /dzi/ without ?v= is metered per
                                           # client, versioned /dzi/ never is;
                                           # IPv6 logins are capped per /64
bash infra/tests/test_cache_key_nginx.sh   # same harness: "//", dot-segment and
                                           # %2F paths (any "%" under /dzi/) are
                                           # refused (400) before they can poison
                                           # the shared cache; /dzi/ keys carry
                                           # ?v=, unversioned tiles are never
                                           # stored, X-Accel-Expires: 0 honoured
bash infra/tests/test_upload_body_limits_nginx.sh # same harness: every large-
                                           # upload route has its own location
                                           # (the 2m server default would 413 it),
                                           # a file at each app cap gets through,
                                           # and every UploadFile route is listed
bash infra/tests/test_restart_unit_order.sh # wsi-restart.service's real ExecStart
                                           # with a stub systemctl: restore apply
                                           # = stop sync, restart app, start sync
bash infra/tests/test_authz_cache_nginx.sh # same harness: /_wsi_authz verdicts
                                           # use their own zone (from wsi.conf),
                                           # are keyed per slide for /dzi/, get
                                           # X-Original-URI, and 401/403 are
                                           # never cached
bash infra/tests/test_thumbs_ttl_nginx.sh # same harness (~1 min): /thumbs/
                                           # caches a 200 for <= 60 s, so an
                                           # unpublished (renamed) thumbnail
                                           # 404s after that; unchanged ones
                                           # revalidate with a 304
bash infra/tests/test_tile_cache_control_nginx.sh # same harness: a tile under
                                           # ?v= reaches the browser as
                                           # private, max-age=86400; unversioned
                                           # tiles, errors, .dzi and .szi stay
                                           # no-cache
bash infra/tests/test_access_log_redaction_nginx.sh # same harness: the
                                           # ?t= (or ?%74=) token of the
                                           # download routes is masked in the
                                           # access log, all else logged
                                           # exactly as "combined"
bash infra/tests/test_download_locations.sh # static: every GET /api/admin/
                                           # .../download route has its own
                                           # streaming nginx location
sudo bash infra/tests/test_install_root_sources.sh # install.sh installs root-run
                                           # units / nginx / env template from
                                           # the clone, never via /opt/wsi, and
                                           # copies the clone into /opt/wsi as
                                           # wsi (the VERSION and copy cases
                                           # need root: run it with sudo)
bash infra/tests/test_install_python_gate.sh # install.sh refuses a python3
                                           # below the lock's floor; lock,
                                           # Makefile and CI agree on it
bash infra/tests/test_latest_release_tag.sh # install.sh / update.sh deploy
                                           # the highest FINAL vX.Y.Z tag,
                                           # never a pre-release like -rc1
bash infra/tests/test_install_nginx_restore.sh # install.sh puts the previous
                                           # nginx files back when nginx -t
                                           # rejects the new ones, leaves no
                                           # temp files on any failure, caps
                                           # a persisted cache's keys zone by
                                           # RAM, and keeps an operator's
                                           # wsi-access-log.conf
bash infra/tests/test_errtrace.sh          # static: every script with an ERR
                                           # trap also sets -E, so the trap
                                           # fires inside functions too
bash infra/tests/test_systemd_optional_paths.sh # every sandbox path a unit
                                           # lists exists at start or carries
                                           # the optional "-" prefix
sudo bash infra/tests/test_set_workers_env.sh # the wsi.env rewrite behind
                                           # set-workers.sh / set-cache-size.sh
                                           # (infra/lib/envfile_set.py); the
                                           # foreign-owner case needs root
sudo bash infra/tests/test_nginx_cache_tool.sh # nginx-cache.sh refuses untrusted
                                           # trees, imports nothing from the
                                           # cwd, sizes keys_zone with
                                           # max_size, capped at a quarter
                                           # of RAM (nginx -t checked);
                                           # every python3 runs with -I; the
                                           # foreign-owner case needs root
sudo bash infra/tests/test_install_symlink_guard.sh # install.sh never chowns /
                                           # chmods through a symlink under
                                           # /var/lib/wsi (needs root)
sudo bash infra/tests/test_update_rollback.sh # update.sh end to end with stubbed
                                           # systemctl/curl: the rollback puts
                                           # the DB snapshot back, always
                                           # restarts, and reports honestly;
                                           # a local edit blocking the
                                           # checkout is named (needs root)
sudo bash infra/tests/test_sync_from.sh    # sync-from.sh between two scratch
                                           # installs (ssh / systemctl stubbed,
                                           # real rsync and sudo, "nobody" as
                                           # the service user): what is taken
                                           # and kept, hard links, sign-out,
                                           # --dry-run, refusals before
                                           # anything stops, fresh tile
                                           # generations after a failed swap,
                                           # pending sign-outs and the lock,
                                           # --rollback, --all (needs root)
sudo bash infra/tests/test_root_guard.sh   # update.sh and sync-from.sh refuse
                                           # a tree others can write (the
                                           # ownership case needs root)
bash infra/tests/test_origin_transport.sh # install.sh and update.sh warn
                                           # before deploying from an http://,
                                           # git:// or ftp:// origin (after
                                           # url.<base>.insteadOf)
bash infra/tests/test_verify_release_tag.sh # the opt-in WSI_VERIFY_RELEASE_TAGS
                                           # check: only a validly signed tag
                                           # passes (needs git and gpg)
bash infra/tests/test_tls_serving_nginx.sh # a certbot-migrated wsi-tls.conf
                                           # served by real nginx: HTTP is
                                           # redirected once, HTTPS is served,
                                           # the raw-IP route is not redirected
```

The ones shown with `sudo` need root for all or part of their checks and SKIP
those without it; the rest need no root: the nginx guards run under a
throwaway prefix on unprivileged ports, and SKIP themselves when `nginx` or
`python3` is absent. Whatever a test cannot run, it reports on a line starting
`SKIP - ` and still exits 0, so a local run without nginx or root stays usable
— keep to that form in new tests: CI fails on any such line (see
[Continuous integration](#continuous-integration)). That SKIP is also why the
zone stubs in each generated `nginx.conf` have to stay in sync with
`wsi.conf` by hand — a zone the snippet names but the stub omits makes nginx
reject the config, and on a machine without nginx the guard exits 0 without
ever noticing.

The infra scripts are linted with `shellcheck infra/*.sh infra/lib/*.sh
infra/systemd/*.sh` at its default severity; the settings (helpers followed
from the script's own directory, and the one false positive switched off,
with the reason) are in `.shellcheckrc`, so the bare command gives the same
result locally as in CI. CI pins ShellCheck to an exact release (the
version is in `.github/workflows/ci.yml`); use that version locally when a
newer or older one reports differently.

Frontend type-checking rides on the build: `npm run build` in `frontend/`
runs `tsc --noEmit` first (also reachable as `npm run typecheck`). Unit and
hook tests use Vitest:

```bash
cd frontend
npm test              # one run (vitest run)
npm run test:watch    # re-run on change
```

Tests live next to the code they cover (`src/**/*.test.ts(x)`). Pure-logic
tests run in Node; a test that needs the DOM (OpenSeadragon, React hooks via
`@testing-library/react`) opts in with a `// @vitest-environment jsdom`
docblock. `src/test/` holds shared fakes (e.g. a minimal OpenSeadragon viewer
model). Lifecycle bugs — an ID changing under a mounted component, a promise
resolving after navigation, two requests overlapping — are what static
typing cannot catch, so a fix for one should come with a hook-level test that
reproduces the ordering (see `src/hooks/useSyncFollower.test.tsx`).

### Dependency locking

`services/app/requirements.txt` (runtime) and `requirements-dev.txt` (adds
the test tools) state the accepted version *ranges* and the reasons behind
them. What actually gets installed — by `infra/install.sh` (and so
`update.sh`), `make venv` / `make venv-dev` and CI — is the pair of lock
files generated from them, `requirements.lock` and `requirements-dev.lock`:
every package, transitive ones included, at one exact version with its
SHA-256 hashes, installed with `pip install --require-hashes`. The locks are
universal: where the newest allowed release of a package needs a newer Python
(numpy, tifffile, imagecodecs, …) the lock carries one pin per Python range
with an environment marker, from Python 3.12 up (`LOCK_PYTHON_FLOOR` in the
Makefile: Ubuntu 24.04's `python3`, the oldest version `install.sh` accepts
and CI tests and audits — pins for anything older would be verified by
nothing), so one file serves every supported host. The dev lock is constrained to the runtime lock, so tests run
against the deployed versions.

- After editing a range file, run `make lock` and commit the changed lock
  files. It keeps every existing pin it can; CI's `lock` job
  fails if the committed locks differ from what `make lock` produces. It
  needs [uv](https://docs.astral.sh/uv/) at the release pinned as
  `UV_VERSION` in the Makefile
  (e.g. `pipx install "uv==$(make -s uv-version)"`, or install it anywhere
  and pass `make lock UV=/path/to/uv`): other releases order the `# via`
  annotations differently, and `make lock` warns when the `uv` it runs is
  not that release.
- `make lock-upgrade` moves every pin to the newest release the ranges allow.
  `.github/workflows/lock-refresh.yml` does this monthly (or on demand), runs
  the backend suite against the result, opens a pull request and starts CI
  and the dependency audit on its branch; it needs *Allow GitHub Actions to
  create and approve pull requests* enabled in the repository's Actions
  settings. The not-yet-reviewed releases only ever run in a job with a
  read-only token and no stored git credentials; a second job, which never
  installs or imports a dependency, commits the two lock files and opens the
  pull request.
- Protect the `v*` tags (a tag ruleset restricting who may create, move or
  delete them). `install.sh` / `update.sh` deploy the highest `vX.Y.Z` tag on
  `origin` and run that release's `install.sh` as root, so whoever can push a
  tag decides what every host installs on its next update.
- npm and the GitHub Actions versions are updated by Dependabot
  (`.github/dependabot.yml`); the frontend's own lock is `package-lock.json`.
  The workflows pin every action by full commit SHA with the release in a
  trailing comment (`uses: actions/checkout@<sha> # v7.0.1`), so a moved or
  re-pointed tag cannot change what runs; Dependabot updates the SHA and the
  comment together. Pin a new action the same way: resolve the tag with
  `git ls-remote https://github.com/<owner>/<repo> 'refs/tags/<tag>*'` (for an
  annotated tag, use the commit of its `^{}` line).
- `.github/workflows/audit.yml` checks `requirements.lock` (`pip-audit`) and
  the frontend's production dependencies (`npm audit --omit=dev`) against the
  published advisories every week, and on pull requests that change either
  lock. A finding is fixed out of cycle: raise the floor in
  `requirements.txt` with the advisory as the comment and `make lock`, or
  update the package in `frontend/`.
- No lock refresh crosses a range's upper bound, so `make outdated` (needs
  the dev venv, `make venv-dev` / `make dev-install`, for `packaging`) lists the direct dependencies with a newer release and
  marks the ones past their bound as `capped`, followed by `npm outdated` for
  the frontend. Check it every few months and move the bounds deliberately
  once the new release is tested.
- A backup archive's manifest records the Python package versions it was
  taken with (`python_packages`), so an incident can be reproduced against the
  same tree.

### Continuous integration

`.github/workflows/ci.yml` runs on every pull request and on demand (*Run
workflow*), not on pushes to `main`: a pull request's run already tests its
merge into `main`, so the merge push would only repeat it (release tags
never start it). To re-check `main` itself (say, after merging several pull requests
whose runs each predate the others' merges), run it by hand on `main`. It
covers the backend suite on Python 3.12 and 3.14,
the lock-file freshness check, the frontend typecheck, unit tests and
production build on Node 24, shell syntax (`bash -n`, and `dash -n` for the
`/bin/sh` script) and `shellcheck` for the infra scripts, and the infra shell
tests with nginx installed — so the nginx guards above really boot nginx
there instead of skipping — with the tests that need root for some of their
checks run under `sudo` (the list is in the workflow). There nothing may
skip: a test that prints a `SKIP - ` line fails the job just like a failing
one, since the runner has everything every test needs.

A first `changes` job decides which of those jobs the pull request can
affect, from the diff of its merge commit against the base (the whole pull
request, not only the latest push), and the others are skipped — a skipped
job reports success, so required checks still pass. A docs-only change runs
none of them; a frontend-only change runs only the frontend job, unless it
touches `frontend/package.json`, `frontend/src/lib/admin/validation.ts` or
`frontend/src/lib/slideCredit.sourceUrlCases.json`, which the backend suite
also reads. The backend
and infra jobs are skipped only when every changed file is one they are known
not to read (both suites also read files outside their own tree; the lists
are in the workflow), the frontend and lock jobs run when a file they are
built from changes, and a change to `ci.yml` itself or a manual run tests
everything. A new test that reads a file from another part of the tree must
be reflected in those lists.
The vulnerability audit of the locks is a separate workflow
(`.github/workflows/audit.yml`, see [Dependency locking](#dependency-locking)),
so a newly published advisory does not turn unrelated pull requests red.

## Evolving the database schema

The DB (SQLite, SQLAlchemy ORM in `services/app/wsi_app/models.py`) evolves
through two complementary mechanisms — pick by the *kind* of change:

| Change | What to do |
| --- | --- |
| **Add** a column or table | Just declare it on the ORM model. On the next boot `Base.metadata.create_all` creates any missing table, `main._apply_column_migrations` reflects the live schema and `ALTER TABLE ADD COLUMN`s the missing columns on every existing DB, then `main._reconcile_indexes` creates any index the model declares but the live schema lacks. That second step is what makes `index=True` / `unique=True` on a **new column of an existing table** actually take effect: SQLite's `ALTER TABLE ADD COLUMN` cannot carry `UNIQUE`, and `create_all` builds indexes only for tables it creates itself; without the reconciler, such a column would arrive bare and any `IntegrityError` backstop written against it would silently never fire. A unique index that cannot be built because the table already holds duplicates is logged at ERROR and skipped rather than blocking boot — de-duplicate and restart to get the constraint. Nothing else to write. |
| **Remove** a column | Delete it from the ORM model. The reconciler's DROP phase (`_apply_column_migrations(phase="drop")`, after the migrations run) drops any plain index on it and `ALTER TABLE DROP COLUMN`s it on the next boot. A column in a UNIQUE constraint, foreign key or primary key cannot be dropped in place: the boot refuses to start (`RuntimeError`), so remove such a column with a table-rebuild migration instead. |
| **Anything transformational** — data back-fill, column rename/split, type change, table rebuild (UNIQUE/FK reshape SQLite can't `ALTER` in place), data repair | Append a numbered migration to the **`migrations`** package. |

**What the reconciler cannot do: change an existing column's nullability.**
SQLite has no `ALTER COLUMN ... SET NOT NULL`; the only route is a full table
rebuild. This matters because `_column_add_clause` has to drop a
function-valued or Python-side default (`func.now()`, `_mint_public_id`) —
SQLite rejects it in `ALTER TABLE` — and therefore adds such a column
*nullable*, while `create_all` builds it `NOT NULL` on a fresh install. The
two shapes then diverge permanently and silently.
`main._warn_nullability_divergence` runs right after the schema reconcile
(still under the startup schema lock) and logs any column that has drifted
this way.

There are two ways to close the gap, and a "schema drift" line in the boot log
is the cue to pick one:

- **For a shipped schema change** — append a numbered migration that rebuilds
  the table, so every install converges on the strict shape.
- **For a one-off repair of an existing database** —
  `services/app/tools/normalize_live_db.py` does exactly what the warning
  reports: it reads the ORM as the target shape, back-fills NULLs in
  `NOT NULL` columns, mints missing `public_id` tokens, and rebuilds any table
  whose live nullability disagrees (recreating the ORM's indexes on it). It
  never writes in place — it checkpoints the WAL, copies the DB through
  SQLite's backup API, works on that copy, verifies it, and only then swaps it
  in beside a timestamped `.bak`. It also refuses to invent a value for a
  `NOT NULL` **foreign key** (a NULL parent id is an orphaned row — an
  operator decision), aborting with the offending row ids instead.

  ```bash
  # Stop BOTH services: the sync coordinator holds its own SQLite handles,
  # which would otherwise stay on the file the swap moves aside.
  sudo systemctl stop wsi-app wsi-sync
  # Inspect only (default): reports what would change, writes nothing.
  sudo -u wsi /opt/wsi/venv/bin/python normalize_live_db.py /var/lib/wsi/wsi.db
  # Apply, after reading the report.
  sudo -u wsi /opt/wsi/venv/bin/python normalize_live_db.py /var/lib/wsi/wsi.db --apply
  sudo systemctl start wsi-app wsi-sync
  ```

  Run it as the service user (`sudo -u wsi`), not as root: root would be running
  the venv and `wsi_app` that user can rewrite, and following links in its
  directory; the tool refuses to run as root on a database, or in a directory, a
  non-root user owns, and on a link to a database that such a user planted.

  The script may be copied anywhere on the server; it locates the `wsi_app`
  package via `--app-dir`, `$WSI_APP_DIR`, its own `../`, its own directory,
  then `/opt/wsi/services/app`. Running it with the deployed venv's interpreter
  satisfies its SQLAlchemy dependency.

### Two version numbers

`app_meta` records two things, stamped on every boot:

- `app_version` — the human `vX.Y.Z` string (release identity, About modal).
- `schema_version` — a monotonic **integer** describing the data shape,
  bumped only when the schema changes. Both have a downgrade guard that
  refuses to boot an older binary against a newer DB
  (`app_meta.assert_not_downgrade` / `assert_schema_not_downgrade`). The
  app_version guard is skipped (with a warning) when the running version is
  only the `frontend/package.json` fallback — see
  [Where the running version comes from](#where-the-running-version-comes-from).

`schema_version` is built from two constants in `migrations/__init__.py`:

- `BASELINE_SCHEMA_VERSION` — 5, the floor every database records. A
  database or backup stamped below it has no migration path: the boot
  refuses it (`migrations.assert_schema_floor`, from
  `main.reconcile_schema`) and so does a restore (`SCHEMA_TOO_OLD`). The boot
  also refuses a database that records an `app_version` but no
  `schema_version`.
  **Never lower it.** The schema downgrade
  guard refuses to boot a binary whose `CURRENT_SCHEMA_VERSION` is below what
  the DB records, so a baseline edited downward locks the app out of its own
  database. `tests/test_migrations.py::test_baseline_is_never_lowered`
  demonstrates the failure.
- `CURRENT_SCHEMA_VERSION` = `max(BASELINE_SCHEMA_VERSION, *migration
  versions)`, so appending a migration raises it automatically.

`MIGRATIONS` holds one step, 6, which transforms nothing: it raises
`schema_version` for the two-factor columns on `users` and
`perf_runs.summary_json` (see `_fence_staff_mfa_columns`). The first public
release therefore ships at `CURRENT_SCHEMA_VERSION` 6: every database, backup
and slide bundle a released build writes records 6 or higher, so 6 is the
oldest schema a public install can hold. The baseline constant stays 5 and is
not raised to match. An empty list is
also legitimate: the baseline alone then describes the schema. Removing a table takes a migration (the reconciler creates tables
but never drops one the ORM stopped declaring), as does any change it cannot
express.
Bumping `schema_version` also makes builds older than the bump refuse this
build's backups and slide bundles — worth it on its own when columns were
added without one, since an older build would otherwise accept such an archive
and drop the columns it does not know at its next boot. `test_migrations.py` drives
the runner's ordering, atomicity and resume with synthetic migrations rather
than with whatever happens to be registered.

### Adding a migration

1. Write an `upgrade(conn)` function (it receives a transactional SQLAlchemy
   connection) — in `migrations/__init__.py`, or in
   its own `migrations/mNNNN_<slug>.py` module once it is large.
2. Register it in `migrations/__init__.py`'s `MIGRATIONS` list with the next
   `version` integer — which must be **above `BASELINE_SCHEMA_VERSION`**, or
   the runner would skip it on every install (the package asserts this at
   import) — and a one-line `description`. `CURRENT_SCHEMA_VERSION` updates
   itself.

The runner (`run_migrations`) applies every migration with
`version > schema_version` in order, committing each **together with** its
version bump (a crash mid-chain resumes from the last good step). A DB with
neither a `schema_version` nor an `app_version` — a fresh install — starts
from `BASELINE_SCHEMA_VERSION`. It runs in
the lifespan between the reconciler's ADD phase (new columns already exist)
and its DROP phase (soon-to-be-removed columns are still readable) — so a
migration can populate a freshly-added column and read a doomed one.

**Authoring rules (important):** migrations run against the *reconciled,
current-shape* schema, not a reconstructed historical snapshot the way
Alembic replays revisions. So every migration must be **defensive** (guard on
the actual presence of the columns/tables it touches via `PRAGMA table_info`
/ `sqlite_master`) and **idempotent** (gate writes on the data conditions
that make them necessary). A fresh install builds the current shape directly
and runs every migration against it, so each must be a clean no-op there.

Each migration runs inside a transaction that the runner opens explicitly
with `BEGIN IMMEDIATE` (pysqlite would otherwise autocommit DDL, so a step
that failed after a `CREATE`/`ALTER` would leave it behind). Because
`PRAGMA foreign_keys` is a silent no-op inside a transaction, the runner follows
SQLite's [documented procedure](https://www.sqlite.org/lang_altertable.html#otheralter)
around every step: it sets `foreign_keys=OFF` on the connection *before*
`BEGIN IMMEDIATE`, runs `PRAGMA foreign_key_check` inside the transaction just
before COMMIT (any violation raises and rolls the whole step back), and then
restores the connection's previous setting. A migration can therefore rebuild a
table other tables reference — create `new_x`, copy, `DROP TABLE x`,
`ALTER TABLE new_x RENAME TO x` — without `ON DELETE CASCADE` wiping the child
rows (`db.py` turns enforcement on for every connection, so without this the
`DROP` would cascade and commit silently). The step must leave every reference
valid by the time it returns; don't issue `PRAGMA foreign_keys` yourself.

`schema_version` also rides in backup manifests (`backup.py`) and slide-export
bundles (`slide_transfer.py`): restore uses it for a schema-precise downgrade
decision and refuses one below the baseline, and slide import allows cross-`app_version` transfer as long as the
bundle's `schema_version` lies in `[MIN_BUNDLE_SCHEMA_VERSION, CURRENT]` (see
`slide_transfer.MIN_BUNDLE_SCHEMA_VERSION`: a migration that changes what a
bundled field means must raise it, or teach the import the older shape). Both treat the field as **required** — an archive or
bundle that omits it is rejected rather than read as an unknown/zero shape.
Tests: `tests/test_migrations.py`, `tests/test_downgrade_guard.py`,
`tests/test_app_meta.py`.

## Internationalisation

The whole UI — public viewer and admin dashboard alike — ships in **English
and Japanese**. There is no server-side locale: the choice is per-browser and
needs no configuration by the operator.

- **Where it lives.** `frontend/src/lib/i18n.tsx` provides `I18nProvider` and
  the `t(key, params)` accessor; the message catalogues are
  `frontend/src/lib/i18n/en.ts` and `ja.ts`. A message is either a plain string
  or a function of the interpolation params, so a plural or ordering difference
  between the two languages is expressed in the catalogue rather than at the
  call site.
- **Loading.** English is bundled **synchronously** into the main chunk and
  seeds the first render, so a cold load never flashes a bare key
  (`common.admin`) where a word ("Admin") belongs. Japanese is a lazy chunk
  fetched only when that locale is active; until it resolves the user sees
  English rather than raw keys.
- **Locale selection.** `detectLocale()` reads a saved choice from
  `localStorage` first, then falls back to a `ja`-prefixed entry in
  `navigator.languages` (or the singular `navigator.language` on engines that
  lack the plural form), then to `en`. The in-app `LanguageSwitcher` writes the
  saved choice, so an explicit pick always wins over browser negotiation.
- **User-facing server messages are keys, not prose.** Endpoints whose errors
  the UI shows raise `admin_helpers.api_error(status, code, **params)`, which
  puts a `{code, params}` payload in `detail` instead of a language-specific
  sentence; the client resolves it through the same catalogue via
  `apiErrorMessage()` (`frontend/src/lib/admin/apiError.ts`). Many validation
  and not-found paths still raise a plain `HTTPException` with an English
  `detail`, which `apiErrorMessage()` shows verbatim (`apiErrorMessageOr()`
  swaps in a localised fallback); new user-facing errors should use
  `api_error`. Field
  identifiers arrive as a `field` param and are mapped to the localized label
  the message template expects.
- **English is the mandatory catalogue; Japanese is checked against it.**
  `ja.ts` is typed as `Translation` (exported from `en.ts`), so `tsc` — and
  with it `npm run typecheck`, `npm run build` and CI — rejects a key missing
  from or extra to `ja.ts`, or a plain string where English has a
  parameterised message (or the reverse). Add every new string to both. At
  runtime `t()` looks the key up in the active locale, falls back to `en.ts`,
  and only returns the bare key when English lacks it too — the case for keys
  built at run time (`t()` accepts any string), which therefore must exist in
  `en.ts`.

## Admin forms: a request in flight

An admin control that sends a request (save, add, rename, reset, apply,
create, a password change, a test) takes what its fields hold at the click.
If those fields stay editable until the answer is applied, the answer
handler acts on a form that has moved on: a success that closes the dialog,
clears the fields or writes the server's values back drops whatever was
typed meanwhile, without a word — or reports as done a change that was
never sent. Admin controls follow one rule against that, and a new or
changed one must too:

**Lock what a request sends, and what its answer changes, from the click
until the answer has been applied.**

- **What to lock.** The fields whose values go into the request, and the
  ones its answer closes, clears or overwrites: in practice the form, the
  row or the dialog the request belongs to. Wrap them in `LockFieldset`
  (`frontend/src/components/ui/LockFieldset.tsx`) — a `<fieldset disabled>`
  without the browser's frame, which disables every control inside,
  buttons included. A single control outside such a group takes
  `disabled` itself.
- **Then the answer may do anything.** Close, clear, write back: nothing
  can have changed since the click, so the handler needs no comparison of
  "sent" against "now".
- **One request at a time.** Run the request through `useInFlight`
  (`frontend/src/hooks/useInFlight.ts`): `run(fn)` refuses to start while
  an earlier `run` is still out (a ref, so a double click in one tick is
  caught too) and `busy` drives the lock. `useInFlightKey` does the same
  for per-row actions and tells which row is busy.
- **Long operations lock less.** An upload or a backup that runs for
  minutes must not freeze the form: lock only the fields its answer
  overwrites (a WSI replace locks the MPP fields, which it resets to the
  new file's), and the controls that would start a second one.
- **Operations that undo each other don't overlap.** When one request's
  answer rewrites what another relies on (the viewer settings' Save and the
  header default's Reset; a slide's visibility and its tours'; the tour
  editor's writes that all send the tour revision), each is disabled while
  the other is out.
- **A load locks what it fills.** Fields filled by a load stay locked until
  it has landed (and stay so if it fails, with a way to load again): an edit
  made before it would be overwritten. A Save that sends what a load fills
  waits for it too (the slide dialog's preset).
- **Write state from the answer with updaters** (`setX(prev => …)`), not
  from a value read before the `await`: anything outside the lock may have
  moved on.

A disabled control drops its focus; `LockFieldset` gives it back, when the
lock ends, to the control inside it that had it when the lock began —
unless the user has clicked or moved focus elsewhere meanwhile — without
scrolling. After Enter in a field that is the field (a wrong password is
retyped at once); after a click on a button inside the group, the button.
`useDialogFocus` skips controls a locked fieldset disables. In jsdom
a disabled `<fieldset>` does not stop clicks on its controls (browsers do),
so tests assert the lock itself — `closest('fieldset').disabled` — rather
than click through it.

**Existing exceptions.** A few long-lived editors keep their fields
editable during a save and treat the edits made meanwhile as still unsaved:
the tour step editor (`resolveStepSave` in `frontend/src/lib/tourEditHelpers.ts`),
the slide edit dialog's Save, the viewer-settings and metadata-display
Saves, and the image-adjustment preset's Save (`ImageAdjustControls`). The
tour editor's "copy to next step" sends overlapping requests on purpose: the
server appends each one. They stay; new controls lock instead.

## Releases

A release is a `vX.Y.Z` (semver) tag on `main`, and the tag **is** the
version: nothing in the tree has to be bumped or committed first. Cut it on
GitHub with Releases → Draft a new release → a new `vX.Y.Z` tag on `main` →
Publish. Publish a Release even for a tag pushed with git: once the
repository has any Release, the admin dashboard's update check
(`wsi_app/update_check.py`) reads `/releases/latest` and does not see a bare
tag.

`install.sh` deploys the highest such tag and stamps it into `VERSION` (see
below). `frontend/package.json#version` is a fixed placeholder, `0.0.0-dev`,
not kept in step with the tags.
Only an exact `vX.Y.Z` counts (`wsi_latest_release_tag` in
`infra/lib/wsi-common.sh`): a pre-release tag such as `v2.0.0-rc1` can be
pushed for testing without any host picking it up on its next update; a test
host deploys it with `GIT_REF=v2.0.0-rc1`.

### Where the running version comes from

The version string (the **About** modal, the admin Server Info page, the
`app_version` stamped into the DB and into backups) is resolved in this
order, identically by the backend (`services/app/wsi_app/version.py`,
`resolve_version`) and the frontend build (`frontend/vite.config.ts`):

1. the `VERSION` file at the tree's root — `install.sh` writes it into
   `/opt/wsi` after syncing the code, from `git describe --tags --always` run
   against the clone, so this is what production reports;
2. `git describe --tags` — a development checkout with `.git` and a
   reachable tag (`1.2.0-3-gabc1234` = 3 commits past `v1.2.0`);
3. `frontend/package.json#version` — a tree with neither, e.g. a release
   archive unpacked by hand, or a clone that has no tag reachable (a shallow
   or `--no-tags` clone; `git fetch --tags`, plus `--unshallow` for a shallow
   one, moves it to step 2). Releases do not bump it: it holds the fixed
   placeholder `0.0.0-dev`, which sorts below every real release, so it is
   never mistaken for one;
4. `unknown` — backend only; the frontend build fails if it cannot read
   `package.json`.

The backend records which one it used at boot (`version.APP_VERSION_SOURCE`).
The admin server-info payload resolves it again on each request and carries it
as `app_process.app_version_source` and `app_update.current_source` (so a
deploy shows up there without a restart); the Server Info page shows it next
to the app version. A
version from the `package.json` fallback is **not trusted for ordering**: it
does not follow the release tags, so it can sit below the code actually
running. With it, the app_version downgrade checks — the boot guard
(`app_meta.assert_not_downgrade` / `bootstrap_app_version`) and the restore
preflight's `DOWNGRADE_NOT_SUPPORTED` comparison — are skipped with a
WARNING (the newer DB stamp is left in place, and so is a newer stamp on the
copies made from a database: a backup snapshot and its manifest, an applied
restore stage, a reset image), and the update check reports
the version as not comparable instead of offering an "update". The
`schema_version` guards apply regardless of where the version came from.

## Production deployment (Ubuntu 26.04)

Full step-by-step instructions are in [`../QUICKSTART_FOR_UBUNTU.md`](../QUICKSTART_FOR_UBUNTU.md).

The short version — the installer is fully automatic (no editor prompts):

```bash
git clone https://github.com/yosukey/tessera-wsi.git wsi
cd wsi
sudo bash infra/install.sh
```

By default the installer fetches tags from `origin` and checks out the
highest `vX.Y.Z` release tag before building, so you always deploy a
released version. Override with:

- `WSI_TRACK_BRANCH=1` — stay on the current branch HEAD (development).
- `GIT_REF=vX.Y.Z` — pin to a specific tag or branch.

If no `vX.Y.Z` tags exist yet on `origin`, the installer falls back to
the current branch HEAD with a warning.

`infra/install.sh` generates the JWT secret, the analytics HMAC salt
(`ANALYTICS_HASH_SALT`, which the pseudonymous analytics mode needs), the
two-factor sealing key (`WSI_MFA_KEY`, also added to an existing `wsi.env`
that lacks it) and a random admin password, writes them into `/etc/wsi/wsi.env`, enables and starts
both `wsi-app.service` and `wsi-sync.service`, and health-checks each backend
(ports 8000 and 8001).

The admin password is **not printed to the console**. Instead it is written
to a separate file readable only by root:

```text
/etc/wsi/admin-initial-password.txt   (mode 0600, root:root)
```

Retrieve it with:

```bash
sudo cat /etc/wsi/admin-initial-password.txt
```

Then open `http://<host>/` (viewer) or `http://<host>/admin` (CMS login).
After logging in, change the username and password from `/admin/account`.
Once you have changed the password you can delete the file:

```bash
sudo rm /etc/wsi/admin-initial-password.txt
```

After bootstrap, the admin can create additional users (`manager` /
`instructor` / further `admin`s) via the user management section on the
**Account** page (`/admin/account`), or directly via `POST /api/admin/users`.

What the installer provisions:

| Component          | Where                                      |
| ------------------ | ------------------------------------------ |
| App code           | `/opt/wsi/`                                |
| Python venv        | `/opt/wsi/venv/`                           |
| Data (WSI, thumbs) | `/var/lib/wsi/{wsi,thumbs,assets,cache}`   |
| Backup archives    | `/var/lib/wsi/backups/` (+ `.staging/`, mode `0750`) |
| Env file           | `/etc/wsi/wsi.env`                         |
| systemd units      | `/etc/systemd/system/wsi-*.{service,path,timer}` (+ `/usr/local/bin/wsi-cache-stat`, `/etc/tmpfiles.d/wsi.conf`) |
| nginx site         | `/etc/nginx/sites-{available,enabled}/wsi` (+ `/etc/nginx/snippets/wsi-{locations,access-log,tls}.conf`) |
| nginx caches       | `/var/cache/nginx/wsi_dzi` (tiles), `/var/cache/nginx/wsi_authz` (edge verdicts), `/var/cache/nginx/wsi_static` (thumbnails and tour attachments) |
| Runtime user       | system user `wsi`                          |
| Database           | SQLite file `/var/lib/wsi/wsi.db`          |

## Updating / maintenance

See **[`operations.md`](./operations.md)** for:

- `infra/update.sh` — one-command updater (fetch tags → checkout latest `vX.Y.Z` → rebuild → restart → health check)
- `infra/set-cache-size.sh` — interactive WSI tile cache size configurator
- `infra/set-workers.sh` — interactive Uvicorn worker count configurator
- `infra/nginx-cache.sh` — clear the nginx tile cache or change its `max_size`

### RAM-aware worker / cache sizing

`infra/systemd/start-wsi-app.sh` derives both the uvicorn worker count and
the per-worker `DZI_CACHE_MAX_BYTES` from system RAM at startup, exporting
the byte cap before the master forks so all workers agree. The pure
arithmetic lives in `infra/systemd/wsi-calc.sh` (sourced by both
`start-wsi-app.sh` and `set-workers.sh`, unit-tested by
`infra/tests/test_wsi_calc.sh`): a RAM fraction (default 75%, dedicated-host
assumption) becomes a page-cache budget, the default-path worker count is
capped so each worker keeps at least `WSI_CACHE_FLOOR_BYTES` (a floor of
`0` means "no floor" and lifts the cap), and the budget is split across
workers and clamped to `[floor, ceil]`. Explicit
`UVICORN_WORKERS` / `UVICORN_WORKERS_PERCENT` / `DZI_CACHE_MAX_BYTES`
override the respective auto value. See `operations.md` for the full
formula and tunables.

## Viewer rendering tuning

Four things decide how a viewer fills the screen: two OpenSeadragon knobs
exposed in the admin dashboard under **Settings → Advanced rendering**
(persisted on `ViewerSettings`, served by both `/api/viewer/settings` and
`/api/admin/viewer/settings`, consumed at OSD init in `ViewerPage`,
`TourViewerPage`, and `useTourViewer`), OSD's per-frame load limit, and the
tiles' browser caching. The two settings are **global**: they apply to every
client and can't be tuned per device.

**How the figures below were measured.** `frontend/bench/osd-render` (see its
README): OpenSeadragon 6.1.1 in headless Chromium with the canvas drawer (the
one OSD picks on iPad; the WebGL drawer elsewhere draws the same tiles), a
synthetic 100,000 × 80,000 px slide in 256-px tiles, 30 ms per request. "FHD"
is a 1920 × 1080 window at pixel ratio 1, "HiDPI" 1440 × 900 at 2, "iPad"
1180 × 820 at 2. Unless a table says otherwise it measures what ships — tiles
`private, max-age=86400`, the per-frame limit 1 on FHD and 4 on iPad — with
the cache at 200; FHD rows are the median of two runs, iPad rows one run.
"Tiles" counts the tiles OSD loaded, whether from the server or the browser's
cache. Headless Chromium rasterises in software (15–55 fps here, lowest at
pixel ratio 2), so read the times as specific to the measuring host and compare
rows, not devices. "Blank" is the share of the image area with nothing drawn at the
worst frame; "s·area" integrates such a share over time.

One screen at deep zoom takes about **54–160 tiles on FHD, 88–280 on iPad and
117–384 on HiDPI** — the count varies up to 4× with the zoom (OSD draws the
level whose pixel ratio lies between 0.5 and 1) and scales with the pixel
ratio squared.

### `immediate_render` (default `true`)

It decides **which tiles are fetched first**, not what is drawn. In OSD 6 the
option only feeds the load priority (`levelVisibility` in
`TiledImage._updateLevelsForViewport`): `true` ranks tiles at the display
resolution first, `false` (OSD's default) ranks the coarsest uncovered level
first. Drawing is the same in both modes — every coarser tile already in
memory is drawn, stretched, under the wanted level until its tiles arrive, and
each level loads centre-outwards (ties are broken by distance from the
centre). So a "ripple" of sharpening from the centre and a stretched preview
of the previous level appear either way.

| Action | `false` | `true` |
|---|---|---|
| FHD, four ×2 zooms in a row | 226 tiles, 7.5 s | 226 tiles, 7.3 s |
| FHD, zoom back home | 55 tiles, 1.7 s, never blank | 40 tiles, 1.2 s, up to 51 % blank |
| FHD, one ×16 jump | 63 tiles, 2.1 s | 50 tiles, 1.7 s |
| FHD, pan 13 screens at deep zoom | 979 tiles, 34 s, never blank | 671 tiles, 18 s, up to 57 % blank |
| iPad, four ×2 zooms in a row | 686 tiles, 12.3 s | 686 tiles, 12.2 s |
| iPad, zoom back home | 185 tiles, 2.9 s, never blank | 165 tiles, 2.8 s, the whole image blank for a moment |
| iPad, one ×16 jump | 229 tiles, 4.0 s | 197 tiles, 3.4 s |
| iPad, pan 13 screens at deep zoom | 2,837 tiles, 49 s, never blank | 2,443 tiles, 38 s, up to 58 % blank |

Step-by-step zooming is identical. On a large jump, a zoom-out or a pan into
unseen areas `true` loads 21–31 % fewer tiles on FHD and 11–14 % on iPad and
turns sharp sooner, but wherever no coarser tile is in memory — the overview
after the cache evicted it, or an area never seen before — it shows nothing
until the tile arrives, where `false` shows a blurry placeholder (FHD, the
13-screen pan: blurry for 18 s·area under `false` against blank for 3 s·area
under `true`). The gap narrows when more tiles are loaded per frame (at four
per frame on FHD the 13-screen pan took about as long either way).

### `max_image_cache_count` (default `200`)

The number of decoded tiles OSD keeps in its in-memory `TileCache` — a
**count of tiles, not bytes**. Eviction drops the least recently drawn tile
that is not on screen; only the levels that fit in a single tile are never
evicted (`TileSource.getClosestLevel()`), and tiles on screen are never
evicted either, so a cache smaller than one screen simply runs over its limit
and keeps nothing else. A dropped tile is loaded again when the view returns
to it: decoded from the browser's cache while its day-long caching lasts
(below), fetched from the server after that.

Panning six screens out at deep zoom and the same six back (`true`):

| Cache | FHD, way back | FHD, way out: worst blank | iPad, way back | iPad, way out: worst blank |
|---|---|---|---|---|
| 200 | 173 tiles reloaded, 8.5 s | 54 % | 1,176 tiles reloaded (every one), 16.7 s | 58 % |
| 1000 | nothing reloaded, 7.4 s | 0 % | 395 tiles reloaded, 11.5 s | 0 % |

None of these reloads reached the server (the tiles' browser caching); what a
larger cache saves is decoding and redrawing, and it keeps the coarse tiles of
the region resident as placeholders, which is why panning into unseen areas
stops showing blank. At the default 200 an iPad keeps less than one screen
(213 tiles were held). Zooming back home after 13 screens of panning, FHD with
1000 loaded 22 tiles and was hardly ever blank (1 % at worst; the whole
session fit in 750 tiles) against 40 tiles and a moment of the whole image blank with 200; on
iPad it made no difference at either size — the overview is the least
recently used tile by then. (With `no-cache` tiles every one of those
reloads would be a revalidation request to the server.)

Memory is about 0.27 MB per decoded 256-px tile (258 × 258 RGBA): 1000 tiles ≈
270 MB, four times that with 512-px SZI tiles, plus up to as much again in GPU
textures under the WebGL drawer that desktop browsers use. For audiences on
iPads or Retina laptops 1000 is the useful setting; desktop-only audiences
are well served by 500.

**Bounds and the escape hatch.** The GUI clamps `max_image_cache_count` to
`[100, 1000]`, and the server re-clamps on both read and write
(`normalize_max_image_cache_count` in `_serializers.py`), so a hand-edited DB
row or a direct API call can't ship an out-of-range value to the viewer. The
ceiling reaches about four iPad screens while bounding worst-case browser
RAM; mobile Safari discards and reloads a tab that exceeds its memory limit,
which 512-px tiles at 1000 could approach. An operator who genuinely needs a
higher ceiling must raise `MAX_IMAGE_CACHE_COUNT_MAX` in
`services/app/wsi_app/_serializers.py` (and the mirror in
`frontend/src/lib/api/viewerSettings.ts`) and rebuild — the bounds can't be
escaped from the dashboard.

### Per-frame load limit (`maxTilesPerFrame`, 1–4 by pixel ratio)

OpenSeadragon starts at most `maxTilesPerFrame` new tile loads per animation
frame. With OSD's default of **1**, loading is capped by the frame rate (60
tiles/s at 60 fps, less while the device is busy drawing) however fast the
server answers. The viewers set it by the screen's pixel ratio,
`round(ratio²)` clamped to 1–4 (`osdMaxTilesPerFrame` in
`frontend/src/lib/osd.ts`, used by `ViewerPage`, `TourViewerPage` and
`useTourViewer`; not an admin setting): 1 on a standard desktop screen, 2 at
1.25–1.5, 3 at 1.75, 4 at 2 and above.

Measured with tiles sent as `no-cache` and the cache at 200 (one run each):

| Action | `maxTilesPerFrame: 1` | `4` |
|---|---|---|
| HiDPI, four ×2 zooms (721 requests either way) | 43.8 s | 15.2 s |
| HiDPI, one ×16 jump | 9.5 s | 3.5 s |
| HiDPI, zoom back home | 3.6 s | 1.4 s |
| iPad, pan 13 screens (2,443 tiles either way) | 116 s; blank 6.0 s·area, blurry 48.7 s·area | 39 s; blank 2.9, blurry 16.0 |
| FHD, four ×2 zooms | 6.3 s, 226 requests | 5.1 s, 265 requests |
| FHD, zoom back home | 1.3 s, 40 requests | 1.3 s, 88 requests |

At 1 per frame these runs loaded exactly as fast as the headless browser drew
(about 16 fps at pixel ratio 2), so the 2.7–3× speed-ups overstate a 60 fps
device's gain; the limit still binds there whenever a screen needs more tiles
than 60 × the time it may take, which is a HiDPI screen. On FHD the extra
loads per frame bought little time and fetched the tiles of the levels a
zoom passes through — 17 % more requests on zooms in, up to 2.6× on zooms
out — hence 1 there. Each frame still starts the best tiles first. Over
HTTP/1.1 the browser's six connections per server bound how many requests
one viewer has in flight; over HTTP/2 or HTTP/3 (e.g. behind Cloudflare)
there is no such cap, and a viewer can have about tiles-per-frame × frames
per round trip in flight at once.

### Tile browser caching (`Cache-Control`)

A tile under its slide's current generation (`?v=N`) goes out as
`Cache-Control: private, max-age=86400`; everything else under `/dzi/` (the
descriptor, unversioned tiles, errors, `.szi`) stays `no-cache`. A tile never
changes under its generation — a replace mints a new N and any other `v` is a
404, and a `DZI_TILE_SIZE` / `DZI_OVERLAP` change moves every slide to a new
generation at the next start (`dzi.reconcile_tile_geometry`) — so for a day
the browser redraws an evicted tile from its own HTTP cache without asking.
The policy is nginx's alone (`$wsi_dzi_tile_cache_control` in `wsi.conf`,
which also rewrites tiles already cached): the app keeps sending
`no-cache`, because nginx stores the app's header with every cached tile and
a policy sent from the app would outlive a rollback of the nginx config
(`infra/tests/test_tile_cache_control_nginx.sh` pins it).

Measured with `maxTilesPerFrame` fixed at 4 (one run each):

| Action | `no-cache` | `private, max-age=86400` |
|---|---|---|
| FHD at 150 ms per request, zoom back home after 13 screens | 53 revalidations, 4.4 s, blank 1.8 s·area (the whole image at worst) | no revalidation, 1.3 s, blank 0.09 s·area |
| iPad at 30 ms, pan 6 screens back | 1,176 revalidations, 18.7 s | no request at all, 18.0 s |

The gain on screen follows the round-trip time: on a fast LAN the redraw is
bound by decoding and drawing, and what disappears is the request traffic;
over a slow link (remote viewers, a tunnel) it is the blank time. The price is
that for up to a day a browser can redraw tiles it already holds without the
edge re-checking access; an unpublished slide still can't be reopened, since
its descriptor stays `no-cache`. See `operations.md` ("Browser caching of
tiles").

**Why these defaults:** without them, on zoom-out (especially on iPads /
high-DPR screens) peripheral tiles repaint one by one for a moment. Three
causes add up: the overview tiles are evicted (the cache holds less than an
iPad screen at OpenSeadragon's default), each evicted tile costs a
revalidation round trip (`no-cache` tiles; at 150 ms per request the blank
time on FHD's zoom-out doubles against 30 ms), and OSD issues only one new
load per frame. The day-long tile caching removes the second, the per-frame limit by pixel ratio
the third, and a larger cache the first for recent screens;
`immediate_render: false` trades the blank for a blurry placeholder at the
cost of more loads.

## Bulk slide operations (admin)

The admin slide list supports multi-select (per-row checkboxes scoped to
the currently visible slides) plus a batch-action bar. Each action is a
single request carrying a `slide_ids` array — one request per action
rather than N per-slide calls, so the nginx `wsi_api` rate limit isn't
tripped by large selections. Every request is capped at
`BULK_MAX_SLIDES = 500` ids (`routers/admin_slides.py`), and a
bulk-attribute request at `BULK_MAX_ATTR_VALUES = 200` values; an over-cap
payload is rejected with HTTP 422. The bulk routes are declared **before**
the variable `/{slide_id}` routes so `bulk` isn't shadowed as a slide id.

### API
- `POST /api/admin/slides/bulk/delete` (manager+)
  - Body: `{ "slide_ids": [int, ...] }`.
  - Best-effort: each slide is deleted under its own per-slide lock and
    its own commit, so one failure never rolls back already-deleted
    slides. Results are bucketed into `deleted` / `not_found`
    (already gone, not retryable) / `failed` (pre-commit error, the row
    still exists — retryable). The admin UI keeps `failed` ids selected
    for a one-click retry.
- `POST /api/admin/slides/bulk/publish` (staff)
  - Body: `{ "slide_ids": [int, ...], "publish": bool }`.
  - Per-slide `results`: a slide that can't be published (missing
    `catalog_id` / `title`) is skipped with `error="missing_fields"`, one
    whose WSI file is not on disk with `error="missing_wsi"` (as the single
    endpoint refuses it); a missing slide reports `error="not_found"`, one
    an upload or import still holds `error="busy"`, and a failed write
    `error="commit_failed"`. Unpublishing cascades
    published tours to draft (same as the single publish endpoint), and
    the DZI cache is evicted for every slide whose flag actually flipped.
- `POST /api/admin/slides/bulk/attributes` (manager+)
  - Body: `{ "slide_ids": [int, ...], "key": str, "values": [str, ...], "mode": "add" | "remove" }`.
  - Surgically adds or removes one metadata key's value(s) across a
    heterogeneous selection without clobbering other attributes: `add`
    is idempotent (only touches slides missing the value); `remove` only
    touches holders. `changed` lists the ids whose attribute set actually
    moved. Key/values are validated by `validate_attribute_kv` (the same
    check single-slide metadata validation uses).

Permission gating mirrors the single-slide endpoints: `publish` is
`staff`, `delete` / `attributes` are `manager+`.

### Verification steps
1. In `/admin/dashboard` → slide list, tick several per-row checkboxes; the
   bulk action bar appears with the selected count.
2. **Delete:** select a mix of valid and already-removed slides (delete one
   in a second tab first), run bulk delete, and confirm the response of
   `POST /api/admin/slides/bulk/delete` buckets them as `deleted` /
   `not_found` (the dashboard shows no summary: both disappear from the
   list, only a `failed` one raises an alert) and that the selection clears
   (only `failed` ids would stay selected).
3. **Publish:** select a draft slide missing a title plus a complete one,
   bulk-publish, and confirm the incomplete one is skipped
   (`missing_fields`) while the other flips to published.
4. **Unpublish cascade:** bulk-unpublish a slide with a published tour and
   confirm the tour drops to draft.
5. **Attributes:** select slides where only some already carry
   `key=value`, run add (idempotent) then remove (touches only holders),
   and confirm via the category modal's per-value coverage (n/total) that
   only the intended slides changed.
6. **Cap:** attempt a selection >500 (or POST a >500 `slide_ids` array)
   and confirm the 422 cap response.

## Image credit (admin)

Each slide can name the rights holder of its image and the terms it is shown
under — distinct from the site-wide `copyright_msg` (the operator's own notice)
and from the filterable attributes. Three columns on `Slide`, validated by
`services/app/wsi_app/slide_credit.py`, `""` = not set:

- `credit_text` (≤ 512 chars, single line) — shown verbatim, e.g.
  "© 2024 Dept. of Pathology". Plain text, never Markdown.
- `license_id` — one of `slide_credit.LICENSE_IDS`: the Creative Commons 4.0
  licenses by SPDX id (`CC-BY-4.0` … `CC-BY-NC-ND-4.0`), `CC0-1.0`, `PDM`
  (Public Domain Mark), `ARR` (all rights reserved) or `custom` (terms given in
  the credit text / at the source). The frontend maps the id to the license's
  short English name (`CC BY 4.0`, `Public Domain Mark 1.0`, …; `custom`'s
  label is translated) and deed URL, if any — `ARR` and `custom` have none
  (`frontend/src/lib/slideCredit.ts`; keep the two lists in sync) — so no admin-entered license URL is needed.
- `source_url` (≤ 1024 chars) — optional link to the original; absolute
  `http(s)://` with a host only — no userinfo (`https://a@b/` opens `b`), no
  backslash, whitespace or invisible characters — so a stored value can never
  render as a `javascript:` link or open somewhere other than it reads. The
  server also refuses what a browser would not parse — an empty authority
  (`https:///x`), a bad port, a host containing a WHATWG forbidden code point
  or `%`, an invalid IPv4 address (`1.2.3.4.5`) or IPv6 literal — because the
  viewer only links a URL the browser parses (`safeSourceUrl` in
  `frontend/src/lib/slideCredit.ts` re-checks before linking, and the modal
  runs it before saving). Both are tested against one case table,
  `frontend/src/lib/slideCredit.sourceUrlCases.json`; the one known difference
  is the IDNA (UTS 46) validity of a host label (e.g. `xn--a.org`, `١٢.org`),
  which only the browser checks — and URL parsers disagree on it across
  engines and releases — so such a stored URL is linked where the browser
  parses it and shown as plain text where it does not.

The viewer and the tour viewer render it bottom-right (`components/SlideCredit.tsx`),
under the metadata panel, **whenever the slide has any credit — regardless of
`show_metadata_panel`**, since it is the image's attribution rather than
optional slide info. On narrow screens it collapses to a "©" button.

### API
- `PATCH /api/admin/slides/{slide_id}` (manager+) takes the credit as one form
  field `credit_json` = `{"credit_text", "license_id", "source_url"}`, which
  replaces all three fields (a missing key or `null` clears that field; omit
  `credit_json` to leave the credit alone). Any invalid field is a coded 422
  (`adminDash.apiErr.invalidLicenseId` / `invalidSourceUrl` / `invalidCredit`,
  or `validationMaxLen` / `validationCtrlChar`) and nothing in the request is
  written. Changes are recorded in the `slide.update_metadata` audit entry.
- The three fields are echoed on every public and admin `SlideOut` /
  `SlideDetail` (the public list included).
- Slide export carries them; import restores them (see [Slide export /
  import](#slide-export--import-admin)).

### Verification steps
1. In the dashboard edit modal, fill **Image credit / license** (credit,
   license, source URL), check the preview, and **Save**.
2. Open the slide's public URL and confirm the credit sits bottom-right with the
   license linked to its deed and a **Source ↗** link.
3. Hide the metadata panel for the slide (or site-wide) and confirm the credit
   is still shown; open a tour on the slide and confirm it appears there too.
4. At phone width, confirm it starts as a "©" button and expands on tap.
5. Enter `javascript:alert(1)` as the source URL and confirm the save is refused.

## Image-adjustment presets (admin)

The viewer applies a brightness/contrast/saturation CSS filter
(`frontend/src/lib/imageAdjust.ts`). Each slide can carry a saved **preset**
(`Slide.image_adjust_brightness` / `_contrast` / `_saturation`, all nullable —
`NULL` = identity 1.0 for that channel) that seeds the filter from first paint
for every viewer, public and tour alike. Useful for dark scans that otherwise
need the same manual correction on every visit.

### API
- `PATCH /api/admin/slides/{slide_id}` (manager+) accepts the preset as three
  form fields `image_adjust_brightness` / `image_adjust_contrast` /
  `image_adjust_saturation` (used by the dashboard edit modal, which sends the
  full metadata form).
- `PUT /api/admin/slides/{slide_id}/image-adjust` (manager+) saves only the
  preset (form fields `brightness` / `contrast` / `saturation`). The in-viewer
  adjust panel calls this so it can persist without round-tripping the whole
  metadata form.
- Both clamp each channel to its allowed range (brightness/contrast 0.5–1.5,
  saturation 0–2) and collapse the identity value (1.0) to `NULL` — a no-op
  channel is stored as "no preset". The values are echoed on every public and
  admin `SlideOut` / `SlideDetail`.

### Verification steps
1. As a manager, open a slide from the dashboard (`/slides/:public_id?from=admin`).
2. Open the **Image adjustments** panel (bottom-left), dial brightness /
   contrast / saturation with live preview, and click **Save as preset**.
3. Open the slide's public URL (`/slides/:public_id`) in a fresh session and confirm
   the corrected look is applied from first paint.
4. Confirm the **Reset** button returns to the saved preset (not to 1,1,1).
5. Open the slide in the dashboard edit modal and confirm the **Image
   adjustment preset** sliders show the saved values; use **Clear preset** and
   **Save** and confirm the slide reverts to unadjusted.
6. Open a tour on that slide and confirm the preset applies there too.
7. Permission check: an `instructor` token gets HTTP 403 from the
   `image-adjust` endpoint, and the save affordance is not shown.

## Bulk tour operations (admin)

The slide list's tour rows have their own checkboxes. Tours are selected
separately from slides — a slide action such as unpublishing also moves the
slide's tours, so a mixed selection would leave it unclear what an action
applies to — and get a bar of their own (`TourBulkActionBar`) beneath the
slides' one. As with slides, only the selected tours on currently visible
slides are acted on.

### API
All three are `staff`, like the single-tour endpoints, and declared before the
`/api/admin/tours/{tour_id}/...` routes. Every request is capped at
`BULK_MAX_TOURS = 500` tours (422 `adminDash.apiErr.bulkTooManyTours`), and a bulk-attribute
request at `BULK_MAX_TOUR_ATTR_VALUES = 200` values (422)
(`routers/admin_tours.py`). Each tour is handled as its single
endpoint would: its own revision bump (an open editor then sees the change as
a conflict) and its own commit, so one tour's refusal never rolls back or
blocks the rest. Any other error while writing a tour (the write lock not
coming free in time, a refused write) is that tour's `failed`, never a 500
that would hide the tours already committed. A repeated id is handled once.

- `POST /api/admin/tours/bulk/visibility`
  - Body: `{ "tours": [{ "id": int, "expected_revision": int }, ...], "visibility": "draft" | "unlisted" | "public" }`.
  - Sets both flags in one write (draft keeps the link-only flag, as
    `applyVisibility` does). Per-tour `results`: `conflict` (the tour changed
    since the dashboard listed it), `not_found`, `parent_draft` (link-only /
    public needs a published parent slide — the parent is never published as
    a side effect, unlike the per-row control, which asks first), `failed`.
    A tour already at the level is `ok` with `changed: false` and keeps its
    revision.
- `POST /api/admin/tours/bulk/attributes`
  - Body: `{ "tours": [...], "key": str, "values": [str, ...], "mode": "add" | "remove" }`.
  - Adds / removes one key's values in the tours' OWN metadata (what they
    inherit from their slide is untouched), surgically like the slide bulk
    endpoint (`apply_tour_attribute_delta`). When adding, instructors are held
    to the registered vocabulary as in a single save (a removal is not
    checked: it cannot introduce a new value): an unregistered
    value is a 422
    up front, and re-checked per tour under the write lock (`not_registered`).
    The check looks up only the values asked about, a query per table, so
    repeating it per tour stays cheap. An empty value list is a 422
    (`metaValRequired`).
- `POST /api/admin/tours/bulk/delete`
  - Body: `{ "tour_ids": [int, ...] }`. Deletes rows and asset directories
    (`_do_delete_tour`, shared with the single delete); buckets `deleted` /
    `not_found` / `failed` like the slide bulk delete. A failure's detail stays
    in the server log (the entry's `error` is just `"failed"`).

The dashboard reloads the tours after each action (applying the confirmed
change locally when the reload fails) — also after a request that failed as a
whole (network error, gateway timeout), which may still have committed some
tours, so a retry does not meet this admin's own changes as conflicts — and keeps the tours that failed for a
retryable reason (`conflict`, `parent_draft`, `failed`) selected; selected
tours the action didn't cover — on slides filtered out — stay selected.

### Verification steps
1. In `/admin/dashboard`, tick a tour row's checkbox: the tour bar appears
   (the slide bar only when a slide is selected too).
2. **Visibility:** select a tour of a published slide and one of a draft
   slide, choose Open: the first is published, the second is reported as
   skipped because its slide is a draft, and stays selected.
3. **Conflict:** open one selected tour in its editor and rename it, then
   change the visibility from the dashboard without reloading: it is reported
   as changed elsewhere.
4. **Metadata:** add then remove a value across tours where only some carry
   it; the slide's own metadata does not change.
5. **Delete:** bulk-delete tours with uploaded assets and confirm their
   asset directories are gone.

## Step editing feature (admin)
### API
- `POST /api/admin/tours/{tour_id}/steps`
  - Creates a new step. Accepts `multipart/form-data` (see `admin_add_step` in `routers/admin_tours.py`).
  - Required: `order`, `center_x`, `center_y`, `fov_um_w`, `fov_um_h`. Optional: `label`, `rotation_deg`, `description_md`, `transition_mode` (`none` / `smooth` / `smooth_zoom`), `pins` (JSON-encoded array), `shapes_l0` (JSON-encoded array of `rect` / `circle` / `freehand` annotations), `expected_revision` (the tour's).
  - `shapes_l0` is accepted and persisted as `StepShape` rows here, exactly as on the update endpoint (same `_SHAPE_LIST_ADAPTER` validation, duplicate-id rejection and point budget).
  - The form is parsed with its own limits (`_StepFormRoute`): up to 4 MiB per field, so a step at its point budget fits (the editor's Duplicate posts a saved step's shapes back through this endpoint; Starlette's default 1 MiB per field refuses one past ~20,000 full-precision vertices), at most 16 fields and no file part — anything else answers 400. nginx gives the bare step paths (`POST …/steps`, `PATCH …/steps/{step_id}`) their own `client_max_body_size 4m` location for the same reason: a step at its budgets saves as up to ~3.5 MiB, past the server-level 2m (`infra/tests/test_upload_body_limits_nginx.sh`).
  - A tour holds at most 100 steps (`schemas._MAX_TOUR_STEPS`); adding one more answers 409 `adminTour.apiErr.stepLimit`.
- `PATCH /api/admin/tours/{tour_id}/steps/{step_id}`
  - Accepts a JSON body matching `StepUpdateIn` (`services/app/wsi_app/schemas.py`).
  - Editable fields: `label`, `description_md`, `rotation_deg`, `fov_um_w`, `fov_um_h`, `center_x` (optional), `center_y` (optional), `transition_mode` (`none` / `smooth` / `smooth_zoom`), `pins` (array of `PinItem`), `shapes_l0` (array of `Shape`; `kind` ∈ `rect` / `circle` / `freehand`).
  - A `label` that is sent must not be blank (empty or whitespace only answers 422): the step editor requires one. Create, duplicate and backup restore still accept an empty label, and a PATCH that omits `label` leaves such a step as it is.
  - On each update the server replaces the step's pin and shape rows in full — clients send the complete intended set, not a diff. That is only safe because of the revision check below: send `expected_revision`.
  - Returns `{ok, revision, step}` — the step's new revision and the step as saved.
- `PATCH /api/admin/tours/{tour_id}/steps/reorder`
  - Payload: `{ "step_ids": [3,1,2,...], "expected_revision": <tour revision> }`
  - Updates `step.order` in that sequence.
- `DELETE /api/admin/tours/{tour_id}/steps/{step_id}[?expected_revision=<tour revision>]` — removes a step (and its pins / shapes). Scoped to the tour like the PATCH, and tour / step ids are not recycled (`AUTOINCREMENT`), so an editor holding a stale step id normally gets a 404 instead of deleting an unrelated step. (Not an absolute guarantee: restoring a backup rolls the id counter back with it, and a reset restarts tour and step ids at 1 — the tour check keeps such a reuse inside the right tour.)
- `POST /api/admin/tours/{tour_id}/steps/{step_id}/pins` (body: one `PinItem`) and `…/shapes` (body: one `Shape`)
  - Append one annotation inside the server's write transaction, validated exactly as in the PATCH (including the 4096-point freehand cap) plus a per-step cap of 1000 pins / 1000 shapes (`schemas._MAX_STEP_PINS` / `_MAX_STEP_SHAPES`). An append to a full step answers 409 `adminTour.apiErr.annotationLimit`; create and PATCH enforce the same cap on the list they send, answering 422 past it.
  - A step's freehand shapes hold at most 50,000 vertices in total (`schemas._MAX_STEP_FREEHAND_POINTS`): every read of the tour, the public one included, serialises all of them. Create and PATCH answer 422 and an append 409 `adminTour.apiErr.pointBudget` past it.
  - A whole tour holds at most 300,000 freehand vertices across its steps (`schemas._MAX_TOUR_FREEHAND_POINTS`) — the bound on what one read of it costs. Create (and so the editor's duplicate), PATCH and append answer 409 `adminTour.apiErr.tourPointBudget` when the write would take the tour past it, counted under the same write lock as the step cap. Only a write that adds vertices to its step is refused, so a tour already over the budget stays editable. The slide import's preflight refuses such a tour too.
  - Reads serialise the stored rows without re-running these validators. `GET /api/tours/{public_id}` and `GET /api/admin/tours/{tour_id}` render their body with `_serializers.tour_json`, which splices freehand vertices stored in the writers' own form (`json.dumps` of validated floats) into the response as text instead of decoding them into objects and encoding them again; for what the writers store the bytes are exactly what a `JSONResponse` of `step_to_out` produced (`tests/test_tour_read_cost.py`). A row in any other form (differently spaced, hand-edited, a BLOB) takes the `step_to_out` path, which skips a vertex that decodes to a non-finite number; a hand-edited row that only looks like writer output (`0.00001` for `1e-05`) is spliced as it is — the same numbers, and never one that parses as Infinity.
  - An empty or already-used `id` is replaced by a freshly minted one. No `expected_revision`: appends commute, so racing copies all land. The step's revision is still bumped.
  - Returns `{ok, id, revision, step}`: the id used, the step's new revision, the updated step.

### Tour metadata

Metadata is attached to slides (`slide_attributes`) and, separately, to tours
(`tour_attributes`, `models.TourAttribute`): the same `(key, value)` shape and
the same vocabulary (`metadata_options`), one table per owner so each keeps
its `ON DELETE CASCADE`. A tour's **effective** metadata is the union of its
slide's and its own; `tour_attributes` holds only the tour's own values, and
the admin tour responses' `attributes` (`schemas.AdminTourOut`) report only
those. Publicly, `GET /api/tours/{public_id}` and the `GET /api/index/tours`
items carry the tour's own `attributes` too (the latter with the slide's on
`slide.attributes`); `GET /api/tours` and `GET /api/slides/{public_id}/tours`
keep their shape.

- **Public display.** The tour viewer's info panel shows the categories
  picked for it (`extra_attrs`) from the slide's metadata plus the tour's
  own, and the Tours view's cards show `index_card_attrs` the same way
  (`lib/attributes.mergeAttributes`). A category that only tours use is
  picked under **Metadata → Display settings** like any other.

- **Index page.** The filters and `q` see it (see the public routes above):
  the "Tours" view filters each tour on its own metadata only (not what it
  inherits from its slide); the "All"
  view lists a slide that matches by itself or through a published, listed
  tour, and puts those tours first on the card (`matched_tours`); the
  "Slides" view keeps to the slides' own metadata. The filter chips show
  the active view's count from `/api/facets`, re-sorted per view
  (`components/index/results.facetsForView`).

- **Editing.** `PATCH /api/admin/tours/{tour_id}` with `attributes`
  (`{key: [values]}`) replaces the tour's own metadata as a whole (`{}`
  clears it; omitted leaves it). Keys and values follow the slide editor's
  rules (`validate_attribute_kv`); values are stripped and de-duplicated.
  The write is covered by `Tour.revision`. In the dashboard: the tour
  editor's **Metadata** button, which lists the slide's values read-only.
- **Instructors** may edit tours but not the vocabulary, so for them every
  value must be one the option list offers — a registered `MetadataOption`
  or a value already in use on a slide or a tour (which covers keeping or
  clearing one the tour carries) — else 422
  `adminTour.apiErr.attrValueNotRegistered`: they can't introduce a new
  value. Managers and admins may store any valid value, as on slides.
- **Vocabulary changes.** The option usage probes also return `tour_count` /
  `published_tour_count` (published: the tour and its slide), and `slides_fingerprint` covers the tours too (it
  is unchanged for a value no tour carries). Deleting a value or category
  strips it from tours as well (`tours_affected`, `tour_attrs_removed`), and
  a rename carries tours along (`tours_affected`, `tour_attrs_updated`);
  both bump the affected tours' `revision`, so an editor still holding the
  old list gets a 409 instead of writing it back. `GET
  /api/admin/metadata/counts` adds `tour_keys` / `tour_values`.
- **Export / import** carries each tour's `attributes`, read as leniently as
  the slide's (refused pairs are skipped and their keys reported with the
  slide's `importAttributeSkipped`).

### Tour editing concurrency

The step PATCH replaces whole fields, so without a check an editor left open
in another tab (or another admin's) would silently overwrite everything saved
since it loaded. Two counters guard this (`models.Tour.revision`,
`models.Step.revision`; `NOT NULL DEFAULT 0` — imported tours start at 0
and the export bundle omits them):

| Counter | Covers | Bumped by |
|---|---|---|
| `Step.revision` | the step's own content: label, description, view, transition mode, pins, shapes | step PATCH, annotation append |
| `Tour.revision` | tour fields (title, description, published, unlisted, its own metadata) and step structure | tour PATCH, publish, unlisted, step add / delete / reorder, a slide unpublish that cascades the tour to draft, and a metadata value rename / delete or category delete that rewrites the tour's metadata |

Admin responses carry both (`revision` on every `StepOut` and admin `TourOut`;
the public endpoints send `null` for the tour's). The contract:

- Every mutation bumps its counter in SQL (`revision = revision + 1`) and
  returns the new value as `revision`.
- A request may send `expected_revision` — the revision of **the resource the
  operation bumps**: the step's for a step PATCH, the tour's for everything
  else (JSON body field, form field, or query parameter on the DELETE). The
  write then becomes a conditional `UPDATE … WHERE id = ? AND revision = ?`
  as the first statement of its transaction, so on SQLite it holds the write
  lock from the check to the commit.
- On a mismatch nothing is written and the answer is **409** in the
  `api_error` shape, plus the server's current copy:
  `{"detail": {"code": "adminTour.apiErr.stepConflict", "params": {"revision": 7}, "current": <StepOut>}}`,
  or `adminTour.apiErr.tourConflict` with `current` = the `GET /api/admin/tours/{id}`
  envelope (`{tour, steps}`), so the client can resolve without another round trip.
- Without `expected_revision` the write is unconditional (scripts, other API
  clients) but still bumps, so editors that do send one notice it.

The SPA always sends it. The step editor sends the revision it was opened from
(or last saved); on a 409 it keeps the user's edits and offers **Reload
(discard my changes)** or **Overwrite with mine** (resend with the server's
revision). A title save conflict does the same; structure operations and
visibility toggles adopt the server's copy and ask for a retry.

### Verification steps
1. Open `/admin/tours/:tourId` and click **Edit** on any step.
2. Update `label`, `description_md`, `rotation_deg`, `fov_um_w/h`, `center_x/y`, add or remove entries in `pins`, and add a rectangle / circle / freehand annotation in `shapes_l0`.
3. Click **Save** and confirm the updated fields appear in the admin list immediately.
4. Open `/slides/:public_id/tours/:tour_public_id` and verify the right panel reflects updated label/description.
5. Confirm Markdown rendering supports (the full allow-list — and what it
   strips — is in [`tour-markdown.md`](./tour-markdown.md)):
   - links: `[text](https://example.com)`
   - images: `![alt](/assets/tours/{tour_public_id}/file.png)` (asset dirs are keyed by the tour's `public_id` token; the upload endpoint returns the ready-to-paste Markdown)
   - bold: `**bold**`
   - lists: `- item`
6. XSS check: include `<script>alert(1)</script>` in `description_md`; verify it is not executed in admin preview or tour viewer.
7. Open `/slides/:public_id` and confirm no step description panel is displayed (tour panel only appears on `/slides/:public_id/tours/:tour_public_id`).
8. Reorder check: in admin step list, use **↑/↓** to reorder, reload the page, and verify the order persists.
9. Tour navigation check: on `/slides/:public_id/tours/:tour_public_id`, use Next/Prev and confirm movement follows the reordered `order`.
10. Step jump/duplicate/insert check: click a step in admin list and verify viewer jumps to its FOV; use **Duplicate** to create a copy after that step; select a step then use **Insert Step After Selected** (labelled **Add Step from Current View** while no step is selected) to insert a new step from the current view after it. Reload and confirm all changes persist in DB.

## Slide export / import (admin)

Move *selected slides* — with their tours, steps, pins, shapes, attributes, and
tour assets — between separate Tessera installs. This is **additive**: an import
merges bundled slides as **new** rows into the live DB, never overwriting
existing data and never restarting the service. It is distinct from the
whole-DB **Backup & Restore** (a destructive swap-and-restart). Core logic:
`services/app/wsi_app/slide_transfer.py`; endpoints:
`routers/admin_slide_transfer.py`. All routes are `admin` (the bundle carries the
raw WSI, with any vendor label image still embedded in it, and import preserves
publish state).

**Bundle format** — a `.tar.zst` (`.tar.gz` fallback) built with the same
archive primitives as `backup.py`:

| Path inside bundle | Contents |
|---|---|
| `manifest.json` | `bundle_format` (`tessera-slide-export/1`), `app_version`, `schema_version` (the integer the import guard keys on), `created_at`, `archive_format`, slide/tour counts, `slide_public_ids`, `note` |
| `slides.json` | Portable JSON per slide: identity, file-truth fields, override-able fields (MPP override, image-adjust, viewer overlays), image credit (`credit_text` / `license_id` / `source_url`), `is_published`/`unlisted`, `attributes`, and nested `tours[] → steps[] → {pins, shapes_l0}` (each tour with its own `attributes`) |
| `wsi/<slide_public_id>/<basename>` | The raw WSI file (one per slide; `<basename>` is the slide's `wsi_original_basename`) |
| `assets/tours/<tour_public_id>/<file>` | Tour markdown attachments (regular, non-dot files only: an upload's `.<name>.upload-<token>` temp is skipped) |

`stage_import` refuses an archive holding anything outside this layout
(`_check_bundle_layout`), a `manifest.json` over 64 KiB, and JSON nested
deeper than the parser recurses. Bundle compatibility follows
`schema_version` (below): **adding a field to `slides.json` must come with a
schema bump or a new `bundle_format`**, or an older build at the same schema
would import the bundle and drop the field. As a backstop the importer lists
keys it does not know (`_KNOWN_SLIDE_KEYS` / `_KNOWN_TOUR_KEYS` /
`_KNOWN_STEP_KEYS`) in the
plan's `unknown_fields` and as `importUnknownField` warnings — so extend those
sets when you add a field. A numbered migration bumps `schema_version`, so
older releases refuse the bundles of a release that adds one. The
other direction is `MIN_BUNDLE_SCHEMA_VERSION` (5 today): the newer release
keeps importing older bundles down to it, and only a migration that changes
what a bundled field means raises it.

A bundle's member count (files and directories) is capped at
`_import_max_members()` — 5 + `IMPORT_MAX_SLIDES` ×
(3 + max(`TOUR_ASSET_MAX_FILES`, 200)), 101,505 by default, a total for the
bundle however its attachments are spread over tours; the generic 1,000,000
archive cap when `TOUR_ASSET_MAX_FILES=0`. Both sides enforce it: extraction stops at
the cap and `stage_import` refuses the bundle with `importTooManyFiles`
(`params.limit`), and `export_slides` counts the selection the same way
(`_bundle_entries`, which includes the directory members a re-packed copy
would have) before it opens the archive and again as it writes, refusing with
`exportTooManyFiles` (`params.count`, `params.limit`) — so a bundle always
imports on the system that exported it.

Thumbnails and the macro/label associated images are **not** shipped — they are
regenerated from the WSI file on import (reusing the upload-finalize helpers),
which keeps the bundle small and always consistent with the transferred bytes.

**Export flow:** `POST /api/admin/slides/export` claims the shared background-job
slot (mutually exclusive with backup/restore/reset and other transfers), builds
the bundle into `BACKUP_DIR/.staging/export-*`, and reports progress via
`GET …/transfer/jobs/{job_id}`; a job that fails with a `SlideTransferError`
has result `{code, params}` (the i18n key and its values, as for the import
jobs), any other failure leaves only `error`, and a job whose worker died
reports `jobWorkerExited`. A selection of more than `EXPORT_MAX_SLIDES = 500`
slides is refused with 422 up front. Before the archive is
opened the selection is checked for missing WSI files, the member cap (above)
and free space. Each slide's row is re-read and its WSI opened
(`O_NOFOLLOW`) under `slide_mutation_lock`; the lock is released before the
bytes are archived from that pinned fd, so a replace or delete of the slide
doesn't wait for a multi-GB copy. Download uses the signed-URL pattern
(`POST …/export/{id}/download_url` → `GET …/export/{id}/download?t=<token>`) so
the browser streams natively.

**Import flow:** `POST /api/admin/slides/import` claims the job slot, takes hold
of the received upload (`admin_backup.hold_upload`, shared with restore) and
answers 202 `{job_id}`; a background job (kind `slide_import_stage`) copies it
into staging (phase `copying`), then extracts it (path-traversal + decompression-bomb safe,
member count capped as above) and computes the **preflight** plan, its result
being `{stage_id, plan}` (the dashboard persists the job id, then the stage id, in sessionStorage as soon as
it has them, so a tab switch or reload returns to the bundle). The plan holds a
**version guard** (`blocking_code=VERSION_MISMATCH`) plus a per-slide report
flagging `filename_conflict` (the `wsi_filename_norm` UNIQUE index),
`public_id_conflict` (a live slide here already carries the bundle's public
id — typically the bundle was applied before; the dashboard defaults it to
"skip", the API's default for an unnamed slide stays "copy"),
`wsi_present_in_bundle`, `content_errors` (values the slide / tour / step
editors would refuse — title, catalog id, tour title and description, the
per-step pin / shape / freehand-point caps and the per-tour step cap included
— which make the apply refuse the slide), `assets_over_limit` and
`credit_skipped`; bundle-wide it carries `unknown_fields` and `data_space`
(set when the stage and `wsi/` are on different filesystems and the WSI files
still to import won't fit on the data volume). The guard (shared by preflight and apply
via `slide_transfer._import_version_block`) allows a bundle whose integer
`schema_version` lies between `MIN_BUNDLE_SCHEMA_VERSION` and this system's
`CURRENT_SCHEMA_VERSION` — even across differing `app_version` strings, since
the migrations in that range do not change what a bundle carries — and refuses
a newer or older one. The field is **required**: a bundle that omits it
(or states a non-numeric value) is refused outright rather than falling back to
an `app_version` string comparison, which would let a bundle of unknown
field layout through on a version match alone.
The bundle is treated as **untrusted input** (it originates on another install):
`slides.json` is validated for structure, a slide-count cap (`IMPORT_MAX_SLIDES`)
and a metadata-size cap, and — critically — every slide/tour `public_id` is
checked against the mint charset (`^[A-Za-z0-9_-]{1,32}$`) before it is used to
build any filesystem path (`wsi/<id>/`, `assets/tours/<id>/`) or stored on a row,
since those values would otherwise be a path-traversal vector at apply time
(this validation is re-run in `apply_import`, which re-reads `slides.json`).
`POST …/import/apply` then merges additively in a background job:

- Each slide gets a fresh monotonic id (`reserve_next_slide_id`); its
  `public_id` is preserved when free and reminted on collision (losing only
  the id race to a concurrent upload keeps it).
- Tour `public_id`s are remapped globally up front; step `description_md` has its
  `/assets/tours/<old>/…` references rewritten to the new ids.
- Filename conflicts resolve per-slide: **copy** (default — the imported file is
  stored as `<name>-imported-<hex6>.<ext>` so both coexist) or **skip**. Existing
  slides are never overwritten.
- Best-effort per slide: one slide's failure rolls back only that slide (files +
  reserved row cleaned up) and is reported in `failed[]` with a code
  (`wsiUnreadable`, `thumbnailFailed`, `importNoSpace`, …, or the catch-all
  `importSlideFailed`, whose `error` the dashboard shows); the batch continues.
  The per-slide skip lists are reported only through the capped `warnings`.
- Bundle field values are normalised, not trusted: image-adjust presets are
  clamped like the PATCH endpoints, a non-finite/out-of-range MPP override
  becomes unset, an invalid image-credit field (unknown license id, non-http(s)
  source URL, over-length text) imports as empty — reported as an
  `importCreditSkipped` warning — while the other credit fields survive, and
  the publish gates apply — a slide lacking `catalog_id` or
  `title` imports as a draft, and a tour is only published when its slide is.
- The staged WSI is hard-linked (same filesystem) or copied into `wsi/`, never
  moved (`_place_staged_wsi`), so the stage can still retry any slide.

**Stage lifetime and partial-success retry.** `apply_import` ends with
`_finish_stage`: the stage is **kept** while a retry could still do something —
any slide in `failed`, or any attachment skipped for a reason in
`_RETRYABLE_ASSET_REASONS` (`too_large`, `tour_file_limit`, `tour_size_limit`,
`type_not_allowed` — this system's own settings). `invalid_name`,
`invalid_content` and `extension_mismatch` are defects of the bundled file and
never keep a stage on their own. Otherwise it is removed at once. A
kept stage's mtime is refreshed by every apply / attachment retry (per slide /
per tour while running, and once at the end), so `sweep_stale_staging` reclaims
it on its first run once 24 h have passed since the last attempt (the sweep
runs at startup, when an import or restore archive is unpacked and when a
reset is staged — not on a timer). The result (job result) carries `stage_id`,
`stage_kept`, `retry: {failed_slides, skipped_assets}`, and
`already_imported[]` / `already_imported_count` for slides an earlier run
imported.

Per-slide outcomes live in `<stage>/apply-state.json` (`slide_transfer`'s
"Import: apply state" section has the record format), rewritten atomically on
every change. Re-running `apply_import` on the stage first reconciles the
records with the DB (`_reconcile_apply_state`), then skips slides recorded as
imported whose row still exists, leaves recorded skips skipped unless the
request's `resolutions` names them, and attempts failed / never-attempted slides.
Crash safety: a `pending` record naming the reserved slide id and public_id is
written *before* the slide row first exists (`_reserve_slide_row`'s
`before_commit` hook), `imported` after the final commit. On reconcile, a
`pending` record whose row is a finished import is promoted (never imported
twice); a placeholder row with the recorded public_id, or no row, means the
attempt died — its row, live WSI copy, thumbnail and newly created asset dirs
are removed and the slide is retried. Like an upload, each slide's import holds
its `slide_mutation_lock` from the reservation to the final commit (or its
cleanup), so the startup sweep every process runs
(`reap_orphan_slide_reservations`: placeholders older than 30 minutes whose lock
is free, re-read under that lock) never takes a slide still being imported. An `imported` record whose row has since
been deleted is dropped, making the slide importable again. If the state cannot
be written the run aborts (`importStateWriteFailed`) rather than commit a slide
unrecorded; an unreadable state is refused (`importStateUnreadable`) rather than
read as empty. `stage_import` refuses a bundle that carries an
`apply-state.json` of its own. The tour-id remap planned by each run is stored
too and preferred by the next (`_build_tour_remap(prefer=…)`), so markdown an
earlier run rewrote to the id planned for a then-failed slide's tour still
resolves once the retry imports it.

`retry_import_assets` (the `retry-assets` job, kind `slide_import_assets`)
re-runs `_import_tour_assets` — additive: never overwrites, counts the tour's
existing files against the per-tour limits — for the recorded retryable skips
only, into the tour id recorded for each bundled tour (it may have been
reminted), under that tour's `tour_asset_lock`, then re-runs the
`importAssetRefMissing` reference check for the affected slides. `plan_import`
(and `GET …/import/stage/{id}`) reports each slide's `apply_status`
(`imported` / `failed` / `skipped` / null) plus `applied`,
`already_imported_count` and `retry`.

### Verification steps
1. In `/admin/dashboard` → slide list, tick a slide with a tour and use
   **Export selected** in the bulk action bar; the bundle downloads once the
   job completes.
2. In the **Import / Export** sidebar tab (admin only), upload the bundle under **Import slides**. Confirm
   the preflight lists the slide, flags it as **already on this system** (same
   DB: its public id is taken) and, if the slide has a catalog id,
   **duplicate catalog id** (the file-name conflict has no badge of its own;
   it or the public-id conflict is what makes the row offer
   **Import as copy** / **Skip** instead of **New**), and defaults it to
   **Skip**; switch it to **Import as copy** for the next step.
3. Apply and confirm the summary reports `imported 1`; open the new slide and its
   tour and confirm the steps, pins, shapes, and any asset images render.
4. Edit the bundle's `manifest.json` `schema_version` (the field the guard keys
   on — changing only `app_version` is *expected* to still import, since the
   schema range is the compatibility contract) to one above this system's or
   below `MIN_BUNDLE_SCHEMA_VERSION`, and confirm the preflight blocks with
   `VERSION_MISMATCH` and apply is refused. Then delete
   `schema_version` outright and confirm it blocks too — the field is required,
   not optional, so an absent one is refused rather than falling back to an
   `app_version` comparison.
5. Feed a slide-export bundle to the whole-DB **Restore** and confirm it is
   rejected with a "use Slide Import" message.
6. Confirm `manager` / `instructor` receive 403 on export and import.
7. Partial retry: export two slides, then on the target make one fail (e.g.
   `chmod 000` its staged WSI under `BACKUP_DIR/.staging/import-*/wsi/` — the
   service runs unprivileged, so it can no longer read it) and apply. Confirm the result offers **Retry failed slides** and the stage
   directory is still there; restore the permission, retry, and confirm only
   that slide is added and the stage is gone. Likewise set
   `TOUR_ASSET_MAX_FILES=1` for a tour with two images, import, raise it,
   restart, and use **Retry skipped attachments** (or **Review bundle** after
   a reload).

## Server-to-server sync (`infra/sync-from.sh`)

Operator-facing guide: [operations.md → Backup server and server
moves](./operations.md#backup-server-and-server-moves--infrasync-fromsh). The
script runs on the **receiving** server and pulls from the source over ssh.
Everything that understands the database is in `infra/lib/replica_db.py`.
That file is stdlib-only because it runs with the system `python3`: on the
source it is fed over ssh on stdin (`sudo -n -u wsi python3 -I - <cmd>`), so
the source needs no new code; here it runs as `wsi` for anything that opens
the live DB or writes below `DATA_DIR`, and as root only on the mirror and
its staging files (plus `set-env` writing `wsi.env`, below). With `--all`
the source also runs the helper as root (`sudo -n python3 -I - env-secrets`)
to read `ANALYTICS_HASH_SALT` and `WSI_MFA_KEY`, which needs passwordless
root sudo there; a standby sync does the same, keeping only `WSI_MFA_KEY`,
when admin accounts with two-factor sign-in come over. Whatever was read is
written here with `set-env`, and `--rollback` uses `set-env` too to put the
previous `WSI_MFA_KEY` line back after a standby sync.

**Table classification.** The default (standby) scope swaps
`CONTENT_TABLES` in from the source and leaves `LOCAL_TABLES` alone;
`app_meta` is split by key (version stamps and unknown keys stay local,
`dzi_tile_geometry` follows the content, `slides_id_hwm` takes the larger
value). `tests/test_replica_db.py` fails when an ORM table is in neither
tuple. **A new table needs a decision there**: is it content every server
should show (taken from the source), or how this server is reached, or what
happened on it (kept per server)? A new `app_meta` key stays per server
unless it is added to the split in `merge_content`. The helper also keeps
copies of `app_meta.parse_version`, `PUBLIC_ID_RE` and the four `app_meta`
key names (`slides_id_hwm`, `dzi_tile_geometry`, `app_version`,
`schema_version`), and the same test ties each copy to the app's.

**Merge.** `merge_content` runs in one `BEGIN IMMEDIATE` transaction on the
live DB with `foreign_keys` off and the snapshot attached read-only:
`DELETE` + `INSERT … SELECT` per content table, columns matched **by name**
(two installs on one `schema_version` can order columns differently, and the
reconciler adds a column without a schema bump; a column only the source has
is reported and dropped). `sqlite_sequence` only moves up.
`PRAGMA foreign_key_check` must be empty, and `PRAGMA quick_check` (run inside
the transaction, so it sees the new pages) must pass, before `COMMIT`. Once
committed, the swap stands, and the caller signs everyone out. `--all` instead
copies the snapshot over the live file with the backup API (`replace`). The
snapshot is checked first. A copy that lands and then fails its check exits
4, which the script treats as swapped (sign-out first), not as "unchanged".

**Sign-out.** Every swap (merge, replace, `--rollback`) is followed by
`sign-out`, which loads the installed app's `wsi_app/restore_apply.py` (stdlib
only, enforced by its own test) and runs `invalidate_sessions` against the
database the swap replaced (`wsi.db.pre-sync`, or `wsi.db.pre-rollback`).
That is the restore's rule: every `users.token_version` and the
`auth_settings.session_generation` move past both databases' values. Without
it, a swapped-in counter would bring back a token revoked here, or let a
token issued to the account that held an id before the swap pass for the
account holding it after. If it fails, the helper leaves
`restore_apply.SESSION_REVOCATION_SUFFIX` beside the database and exits 3:
the app's boot retries the sign-out and refuses to serve until it succeeds,
and the script says so rather than "signed out". If even that marker cannot
be written, the script leaves the services stopped.

**Tile generations.** `install-files` runs before the swap. When the swap
then fails, or `--rollback` puts an older database back over files a sync
replaced under the same name, `bump-generations` moves every slide to
`max(generation + 1, now in ms)`, which is the tile-geometry change's rule.
Without it, a `(public_id, v)` pair would name tiles of two different files
in the edge cache.

**Version rule** (`check_versions`). Standby: equal `schema_version`, and the
source's `app_version` not newer (a mismatch on the same schema only warns).
`--all`: the restore policy, i.e. the destination at least as new in both.

**Files.** The source DB is snapshotted inside one read transaction
(`export`) and the WSI / thumbnail names it lists are rsynced with
`--files-from` into root's mirror (`/var/lib/wsi-replica/mirror`). The tours'
attachment directories go through a generated filter. `state` (slide file
fields, tours, and each tour's attachments with their signatures) is read
before the snapshot and again after the rsync. `check` compares the slides of
the second read with the snapshot on `(wsi_filename, thumb_filename,
wsi_generation)` (a replace always raises the generation, so equal tuples
mean the transferred files belong to the snapshot), and each slide's WSI /
thumbnail signatures and the attachment lists of the two reads with each
other. Any difference means another round (at most
3). Then every file the source has must be in the mirror with the source's
signature (size and whole-second mtime, which `rsync -t` keeps). An older
copy of a file replaced under its name is present but does not match, and
rsync's partial-transfer exits 23 / 24 are left to this check. The first
round's "before" read is the version check's, and each later round's is the
previous round's "after". Names are refused unless they are one non-hidden path component,
and tour ids unless they match the public-id alphabet, before they reach rsync
or a path. Root never writes below `DATA_DIR`: the mirror's files are chowned
to `wsi` (inside root-owned directories) and `install_files`, as `wsi`,
hard-links them into place (copies across filesystems) via a temp name and
`rename`. Before the swap it keeps everything that the database being
replaced (`wsi.db.pre-sync.new`) or the current rollback point
(`wsi.db.pre-sync`, which a failed sync keeps) refers to. Once the swap has
landed, a second pass keeps only what the new rollback point refers to.
"Unchanged" is rsync's quick check (same inode, or same size and mtime) on
any device, so a copy across two mounts of one filesystem is not redone on
every run. Attachments are not rows, so for them "refers to" means a link in
`tours.description` / `steps.description_md` (`ASSET_REF_RE`, the app's own
pattern, tied to `slide_transfer._ASSET_REF_RE` by the test).
Whether files are linked or copied is asked of `link()` itself (`can-link`,
from a probe file), because one filesystem mounted twice has one `st_dev`
but answers `EXDEV`. `_place` copies on `EXDEV` only, so any other refusal
(say `fs.protected_hardlinks`) is an error and not a silent copy.
`install-bytes` sizes what a copy would write, before the services stop. The source's snapshot is built next to its database
(`.wsi-sync-export.*`), not in a possibly small `/tmp`.

**Concurrency and pending work.** A sync and a rollback take the same `flock`
on `/run/wsi-sync.lock`, whatever `WSI_REPLICA_DIR` each uses. Both refuse (`check_nothing_pending`) while any of
these is pending:

- `restore_apply.SESSION_REVOCATION_SUFFIX` beside the live database
  (`check-sign-out`). Another swap would replace the database that the
  pending retry compares against; `backup.py` refuses a second restore for
  the same reason.
- A staged restore request (`RESTORE_REQUEST_PATH`).
- An interrupted restore's journal (`RESTORE_JOURNAL_PATH`).

For the last two, `start-wsi-app.sh` would apply or roll back the restore
over the sync at the next start. A sync and a rollback both run the check
again once the services are stopped, since a restore can be staged during
the transfer.

**Failure recovery.** `recover()` (every failure path once the services are
stopped) signs out first when a swap has landed and `sign_out` has not run.
If that fails, it leaves the services stopped. It then moves slides to fresh
generations when files may no longer match the database, and only then
starts the services. With `set -E` the ERR trap also fires in
`as_app_user`'s subshell, so the trap handlers exit there and leave the
handling to the main shell.

**Signals during a swap.** `SWAP_STARTED` is set before the swap helper runs.
bash runs a SIGHUP / SIGTERM trap only once that helper has returned, by which
time it may have committed. So `recover()` treats a started swap as landed:
it promotes `wsi.db.pre-sync.new` to the rollback point, signs everyone out
(harmless if the swap did not land) and moves slides to fresh generations.
`infra/tests/test_sync_from.sh` sends a SIGTERM during a 1.5M-row merge to
pin this.

**What `--rollback` undoes.** Each successful swap records its scope in
`WSI_REPLICA_DIR/pre-sync.mode`. After a standby sync, `--rollback` merges
`wsi.db.pre-sync`'s content tables back (`merge`, the same code as a sync), so
`LOCAL_TABLES` written since stay. After `--all` (or with no record), it
copies the whole file back (`replace`). A rollback
removes `wsi.db.pre-sync` once it is restored, so a second rollback cannot
overwrite `wsi.db.pre-rollback`. The pre-swap copy is written as
`wsi.db.pre-sync.new` and renamed over `wsi.db.pre-sync` only once the swap
has landed, so a failed sync keeps the last rollback point. The
`wsi.env.pre-sync` kept by an earlier sync that changed `wsi.env` (a `--all`,
or a standby sync that carried `WSI_MFA_KEY`) is dropped at the same moment.
`--rollback` restores that whole file after a `--all`, but only its
`WSI_MFA_KEY` line after a standby sync.

**Tests.** `tests/test_replica_db.py` covers the classification and the
copies of app definitions, plus every subcommand against databases built
from the ORM schema. `infra/tests/test_sync_from.sh` runs the whole script
between two scratch installs, with ssh / systemctl / curl stubbed and real
rsync and sudo, `nobody` acting as the service user so the permission model
is exercised.
