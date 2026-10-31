# Tessera Architecture

## Overview

A web application for viewing whole slide images (WSI) in the browser.
The backend converts WSI files into DZI (Deep Zoom Image) tiles in real time;
OpenSeadragon on the frontend fetches those tiles and renders them
interactively in the browser.

Related documents:
- [`developer-guide.md`](./developer-guide.md) — implementation-level reference: repository map, endpoints, schema evolution, feature notes.
- [`operations.md`](./operations.md) — operations reference: maintenance scripts, updates, backup / restore, server sync, sign-in, caches, analytics.
- [`../QUICKSTART_FOR_UBUNTU.md`](../QUICKSTART_FOR_UBUNTU.md) — full step-by-step production install.

---

## Stack

| Role | Technology |
|------|------------|
| Frontend viewer | OpenSeadragon ^6.1.1 / szi-tile-source v0.7.2 |
| Frontend framework | React (TypeScript) |
| Backend framework | FastAPI + Uvicorn |
| WSI reading library | OpenSlide (openslide-python); tifffile + imagecodecs for OME-TIFF |
| Image processing | Pillow (PIL) |
| Reverse proxy / cache | Nginx |
| Database | SQLite (SQLAlchemy, WAL mode) |

---

## Request Flow

Two upstreams sit behind nginx:

- `wsi_app` (port 8000, multi-worker) — REST API, DZI/SZI tiles, static assets.
- `wsi_sync` (port 8001, single worker) — live-sync REST + WebSocket only.
  Single-worker keeps the in-memory `RoomManager` coherent across peers
  without needing Redis. Nginx splits traffic by location (`/api/sync/*`
  and `/api/admin/sync/*` go to `wsi_sync`; everything else to `wsi_app`).

```text
Browser (OpenSeadragon)
  │
  │  GET /dzi/{public_id}_files/{level}/{col}_{row}.jpeg?v={tile_version}
  ▼
Nginx (auth_request gate → app-access + per-slide publish verdict; cache key = tile path + ?v=, shared across users)
  ├─ Cache hit  → return tile directly (1–5 ms)
  └─ Cache miss
       │
       ▼
  wsi_app : FastAPI / Uvicorn (port 8000, default min(max(2, cores/2), 8) workers, capped by available RAM)
       │
       ├─ LRU cache hit  → reuse DeepZoomGenerator
       └─ LRU cache miss → open WSI file via OpenSlide (OME-TIFF: the app's own reader)
             │
             ▼
        DeepZoomGenerator (OpenSlide)
             │  read tile region at requested level & coordinates
             ▼
        Pillow
             │  encode to JPEG (DZI_TILE_QUALITY, default 80, clamped 1–95)
             ▼
        tile bytes (with ETag: "<slide_id>-<file_mtime>-<file_size>-<tile/overlap/quality>")
             │
             ▼
  Nginx writes response to disk cache (the app's Cache-Control is ignored
  for that) and sends the browser its own: "private, max-age=86400" for a
  tile under the current ?v=, "no-cache" (ETag revalidation) for the rest
       │
       ▼
Browser (OpenSeadragon renders tile)
```

### Authentication Flow (with Cloudflare Access in front)

When deployed behind Cloudflare Tunnel + Access, the ingress paths (A, B
and C in `operations.md` → "Ingress paths (A / B / C)") converge on the same
nginx → app pipeline. Each of them populates `PageView.user_email` (while
analytics recording is on) whenever the request carries a verified identity —
a CF Access email on A, an app sign-in on B and C (so a visitor under mode
`none` or an exemption records anonymous unless a session cookie from an
earlier sign-in is still valid), and on any path the username of a staff
dashboard login the tab carries — so a view can be attributed to a user.

```text
                    ┌─ Path A: External user ──┐
                    │   Browser
                    │     │
                    │     ▼
                    │   Cloudflare Access  ── SSO (Google / Azure / …)
                    │     │  signs JWT, sets Cf-Access-* headers
                    │     ▼
                    │   Cloudflare Tunnel  (cloudflared)
                    │     │
                    │     ▼ loopback to
                    │   Nginx ──────────────┐
                    │                       │
                    └───────────────────────┤
                                            │
                    ┌─ Path C: LAN-direct ──┤  (Path B — a CF Access Bypass
                    │   Browser             │   policy — still runs through
                    │     │                 │   the Tunnel like A, but
                    │     ▼ direct          │   carries no JWT)
                    │   Nginx ──────────────┤
                    └───────────────────────┘
                                            │
                                            ▼
                    Nginx blanks Cf-Connecting-IP and
                    Cf-Access-Jwt-Assertion from non-loopback peers,
                    then forwards to wsi_app (127.0.0.1:8000) or
                    wsi_sync (127.0.0.1:8001) per location
                                            │
                                            ▼
                    ip_filter_middleware  (denies if NetworkSettings cache
                                           rejects Cf-Connecting-IP / peer)
                                            │
                                            ▼
                    app_auth_middleware
                      ├─ bypass paths (/api/health, /api/auth/*, …) → admit
                      ├─ staff JWT bearer (admin/manager/instructor) → admit
                      ├─ signed download link (…/download?t=) → admit
                      ├─ Cf-Access-Jwt valid → admit  (Path A)
                      ├─ Cf-Access-Jwt bad   → 401 (beacons: fall through
                      │                         below, recorded anonymous + WARN)
                      ├─ break-glass / direct-LAN exemption (opt-in) → admit
                      └─ no JWT → mode-based gate (basic / ldap / google)  (Paths B, C)
                                            │
                                            ▼
                    probe_identity (analytics beacon)
                      1. staff JWT  → users.username (+ actor_role tag;
                                      an expired/revoked staff token still
                                      tags actor_role from its signed claim)
                      2. CF Access  → JWT-claimed email (an invalid
                                      assertion: anonymous, stop here)
                      3. session    → cookie subject (Paths B, C)
                      4. HTTP Basic → basic:<username>  (only the username the
                                      middleware verified on this request;
                                      exempted / CF-admitted requests never
                                      count)
                                            │
                                            ▼
                                  PageView row written
```

All paths share the `user_email` column, so the admin dashboard's
"Top users" / recent-activity views show CF-Access users and
basic/LDAP/Google-OAuth users together.

Staff JWTs (admin, manager, instructor) all carry a `role` claim; there is
no separate `admin` claim. Each `/api/admin/*` router declares
the minimum role it requires through one of these FastAPI dependencies —
the first five from `services/app/wsi_app/auth.py`, the last one local to
`routers/admin_system.py`:

| Dependency | Allows |
|---|---|
| `require_admin` | `admin` only |
| `require_manager_or_above` | `admin`, `manager` |
| `require_staff` | `admin`, `manager`, `instructor` |
| `require_analytics_access` | `admin`, plus `manager` / `instructor` when the corresponding `RolePermissions.*_can_view_analytics` flag is set |
| `require_server_access` | `admin`, plus `manager` / `instructor` when the corresponding `RolePermissions.*_can_view_server` flag is set |
| `require_storage_access` | `admin`, `manager`, plus `instructor` when `require_server_access` admits them — gates only `GET /api/admin/storage`, the free-space figure the bulk-upload preflight needs, since every role that can upload must see it |

FastAPI parses a form / file body before it resolves these dependencies, so
`_AdminUploadPreauthMiddleware` (`main.py`) applies the same check to every
`/api/admin/*` form request before a byte of the body is read, with the
status and detail the endpoint's own dependency would give; the large upload
routes are also refused early for an over-cap declared size (413), a slide
that would not fit the data volume (507) or a busy restore / import job slot
(409).

Every staff token — `admin`, `manager` and `instructor` alike — bypasses
`app_auth_middleware` (and the sync coordinator's equivalent gate) via
`app_auth.staff_token_valid`, so a logged-in staff member's requests need
no second, app-access login on top of their dashboard session. Getting that
session is a different matter: the dashboard sign-in
(`app_auth.ADMIN_LOGIN_PATHS` — `/api/admin/login` and its two-factor code
step) is not a bypass path, so under an app-access mode it answers only a
request the gate admitted (otherwise 401 `admin.err.appAuthRequired`, before
any password check), and staff sign in to the app first. A page load carries
no token: under `ldap` / `google_oauth` the SPA's pages are served ungated
and the admin login page sends a signed-out visitor to `/login`, while under
`basic` every page load, `/admin` included, raises the Basic dialog — which
is why switching to `basic` with no Basic user, or deleting the last one
while in `basic`, is refused with 422 (see `operations.md` → "Authentication
modes"). The staff-token bypass is also what keeps an admin who saved a
setting that admits nobody inside the dashboard to fix it. The check re-reads the backing
row on each request (existence, `is_active`, `token_version`), so a suspended / password-reset user's stale JWT stops
bypassing immediately. Gating this at admin-only would 401 the very
managers/instructors the token-keyed draft-preview bypass in
`routers/public.py` / `dzi.py` exists for, since that bypass is only
reachable once the request has cleared this middleware.

On the live-sync WebSockets the token arrives differently: a browser cannot
set `Authorization` on a WebSocket upgrade, so the presenter sends its staff
token in the socket's hello frame. A `role=presenter` socket is therefore let
past the coordinator's app-access check (`_ws_admission` in
`routers/sync.py`) once the IP filter, the `Origin` check and the
Cloudflare Access check have passed — an invalid `Cf-Access-Jwt-Assertion` is still refused — and the
hello's token is then verified (`_resolve_active_presenter`: active staff,
current `token_version`) **before** the session id is looked up, so a client
without a valid staff token gets the same 4401 for a live session and an
unknown one. Follower and discovery sockets carry no staff token and keep
needing app access (a session cookie, Basic credentials, a CF Access JWT or
an exemption); a staff member with no app session therefore sees no
join banner on the index page under a gated mode (the dashboard's Server Info page
→ "Live sync sessions" lists the running sessions).

The SPA side follows the same rule. `GET /api/auth/state` (which the public
pages' `useAuthGate` asks before rendering) receives the tab's staff token
and reports `authenticated: true` for a valid one, so **Open Viewer (draft
preview)** and the presenter view work from the dashboard under ldap /
google_oauth without an app-access session. `useAuthGate` trusts that
answer alone: a token the server rejected (revoked, suspended, password
changed) gets the `/login` redirect like no token at all, whatever its own
expiry says. The same response's `app_session` is true only for a valid
app-access session cookie — the one thing `POST /api/auth/logout` ends — and
the public pages' "Sign out" button is shown only under `ldap` /
`google_oauth` and only when it is true, rather than on `authenticated`, so staff admitted by their token alone (or a visitor
admitted by Cloudflare Access) are not offered a sign-out that would only
bounce off `/login` back to the page. For the same reason the button goes to
`/login` only once the logout answered 2xx — the app answers it with 200
whether or not the session was still valid, and only that answer carries the
cookie deletion. When the request fails or is refused (unreachable, 429, 5xx,
or any other status) the cookie is still set, so the visitor stays on the
page and is told they are still signed in. The button, like the admin pages'
**Logout**, asks for confirmation before it signs out.

Conversely, an `/api/admin/*` request that nothing admitted is answered by
the gate with the 401 `get_current_user` gives under mode `none` — `"Invalid
token"` when its Bearer failed the check, `"Missing token"` when it carried
none (a bookmarked dashboard page loads before any staff login) — with no
`WWW-Authenticate`. The dashboard recognises both in every mode and returns
to its login screen instead of showing the app-access error (or, under
basic, the browser's credential dialog).

A signed-link download (`GET …/download?t=…` for a slide, backup or export
bundle) is admitted by its token alone (`app_auth.signed_download_valid`):
the dashboard starts it by navigating the browser to the URL `/download_url`
minted, so it carries no Authorization header, and a staff member who never
signed in to the app itself has no app-access session either. The token is
a staff credential in its own right — minted only for an admin, scoped to one
id, short-lived, and re-checked against the minting user's live row — and the
route validates it again. A forged, expired, revoked or misplaced token is
refused at the gate like any other unadmitted `/api/admin/*` request. Because
the token rides in the query string, nginx logs these routes with it masked:
`log_format wsi_combined` in `infra/nginx/wsi.conf` is the stock `combined`
layout with `$wsi_log_request` in place of `$request`, which matches the key
as `t=` or `%74=` since the app decodes both to `t`. nginx's error log cannot
be masked. The site's `access_log` line sits in an operator-owned snippet
that updates never overwrite; see `operations.md` → "nginx access log".

## Authorization & Roles

Tessera uses a three-role RBAC system. The role is assigned at user
creation time and never changes — to "promote" a user, delete and
recreate the account.

| Role | Description |
|---|---|
| `admin` | System administrator. Holds every permission and is the only role that can create or delete users. |
| `manager` | Slide library operator. WSI upload / replace / delete / metadata editing, plus everything `instructor` can do. |
| `instructor` | Educator. Browse slides, toggle publish, full tour CRUD, host synchronized viewing sessions. |

### Permission matrix

This matrix is the single source of truth for role capabilities; other
docs (e.g. the [developer guide](./developer-guide.md#roles--gating))
link here rather than copying it, so update this table only.

| Capability | `admin` | `manager` | `instructor` |
|---|:---:|:---:|:---:|
| Change own username / password | ✅ | ✅ | ✅ |
| Turn own two-factor sign-in on / off, new recovery codes | ✅ | ✅ | ✅ |
| **User management** (create / suspend / delete, reset another member's two-factor sign-in, role-permission flags) | ✅ | ❌ | ❌ |
| Viewer / Network / Auth Settings | ✅ | ❌ | ❌ |
| Backup / Restore / DB reset | ✅ | ❌ | ❌ |
| Slide export / import (cross-Tessera migration) | ✅ | ❌ | ❌ |
| **WSI original download** (`/api/admin/slides/{id}/download`) | ✅ | ❌ | ❌ |
| WSI file-status check (`/api/admin/slides/file-status`) | ✅ | ❌ | ❌ |
| **WSI upload** (new / replace) | ✅ | ✅ | ❌ |
| **Slide deletion** | ✅ | ✅ | ❌ |
| Slide metadata editing (title / tags / image-adjustment preset / etc.) | ✅ | ✅ | ❌ |
| Metadata options management (`/api/admin/metadata/*`) | ✅ | ✅ | ❌ (reads the value list) |
| Thumbnail rebuild (`/api/admin/slides/rebuild-thumbnails`) | ✅ | ✅ | ❌ |
| Slide browsing (list / detail / macro / label) | ✅ | ✅ | ✅ |
| Slide **publish / unpublish toggle** | ✅ | ✅ | ✅ |
| Slide / tour **link-only (unlisted) toggle** | ✅ | ✅ | ✅ |
| Tour create / edit / delete / step operations / publish | ✅ | ✅ | ✅ |
| Tour metadata (the tour's own values) | ✅ | ✅ | ✅ (existing values only) |
| Start synchronized viewing session | ✅ | ✅ | ✅ |
| End own synchronized viewing session (**End live sync** in the viewer) | ✅ | ✅ | ✅ |
| End another user's synchronized viewing session (Server Info page → "Live sync sessions") | ✅ | ⚙️ flag (server)¹ | ⚙️ flag (server)¹ |
| View Analytics | ✅ | ⚙️ flag | ⚙️ flag |
| Delete or pseudonymize analytics data (date-range wipe, per-user purge, pseudonymize plaintext rows) | ✅ | ❌ | ❌ |
| View Server Info (the stats' backup count/size is admin-only) | ✅ | ⚙️ flag | ⚙️ flag |
| Storage free-space check (`GET /api/admin/storage`; byte counts only, no path — the bulk-upload preflight) | ✅ | ✅ | ⚙️ flag (server) |
| View Performance results (`/admin/performance`) | ✅ | ⚙️ flag (server) | ⚙️ flag (server) |
| Run performance measurements (server benchmark, browser test) | ✅ | ❌ | ❌ |
| View Audit History (`/admin/history`) | ✅ | ❌ | ❌ |
| View Error Logs (`/admin/logs`) | ✅ | ❌ | ❌ |

⚙️ flag = optional permission granted per role by an `admin` via the
`RolePermissions` singleton (`manager_can_view_analytics`,
`manager_can_view_server`, `instructor_can_view_analytics`,
`instructor_can_view_server`). All four default to OFF.

¹ A limit of the UI, not of the API: `GET /api/admin/sync/sessions/all` and
`DELETE /api/admin/sync/sessions/{sid}` admit any staff role (so a manager can
close out a session an instructor abandoned), but the only screen that lists
other users' sessions is the Server Info page, which a manager or instructor reaches
only with the server flag.

### Constraints

- **Role is fixed at creation.** There is no UI or API to change a
  user's role after creation; delete and recreate instead.
- **The last `admin` cannot be deleted.** `DELETE /api/admin/users/{id}`
  rejects deletion of the only remaining active `admin`, and a user
  cannot delete themselves.
- **Accounts can be suspended without deletion.**
  `PATCH /api/admin/users/{id}` with `{"is_active": false}` revokes a
  user's access while preserving the username and audit trail; the same
  self / last-admin guards as deletion apply. A suspended account is
  refused at login and has every already-issued JWT rejected immediately
  (the suspend bumps `users.token_version`, which every token-acceptance
  path re-checks).
- **Tokens are revoked on a password change.** Changing an account's
  password bumps `users.token_version`, invalidating every JWT minted
  before the change for the rest of its TTL; the actor's current session
  is handed a freshly minted token so it is not forced to log in again.
- **A lockout cannot be held over the owner.** A browser that has signed in
  as the account before (a signed `wsi_kd_<id>` cookie tied to the account's
  `token_version`), and the server itself (loopback), are neither refused by
  the per-user lockout nor charged to it; see `operations.md` → "Login
  throttles". **Sign out everywhere** (Account page,
  `POST /api/admin/me/sign-out-everywhere`) bumps `token_version`, ending every
  session of the account, since "Log out" only forgets the token in one browser.
- **Login brute-force is bounded two ways.** A per-user lockout
  (`ADMIN_LOGIN_MAX_FAILURES` failures → `ADMIN_LOGIN_LOCKOUT_SECONDS`
  lockout) plus a per-client-IP throttle (`ADMIN_LOGIN_IP_MAX_FAILURES`
  failures per `ADMIN_LOGIN_IP_WINDOW_SECONDS`) that also catches guesses
  spread across many usernames. The per-user budget lives in the `users`
  row and each attempt reserves its slot with one conditional `UPDATE`
  before the password is verified, so parallel requests cannot get more
  than `ADMIN_LOGIN_MAX_FAILURES` guesses checked; a correct password
  releases the slot — on a two-factor account it only hands back its own
  slot, leaving earlier failures counted until a correct code clears them
  (see the next item). The throttles — `ADMIN_LOGIN_IP_*` and
  `APP_LOGIN_IP_*` per client IP (per /64 for IPv6,
  `login_throttle.ip_bucket`), `REAUTH_IP_*` per dashboard token — are
  kept in each uvicorn worker's memory, so the effective ceiling is workers ×
  the configured limit and a restart resets them. On the app-access HTTP
  Basic header a repeat of an already-counted username + password counts once per window
  (and is refused from a short-lived memory instead of another pbkdf2
  verify), so a tab replaying a rotated password cannot lock out its whole
  NAT address; every distinct guess still counts.
- **Two-factor sign-in is optional, per account, any role** (`mfa.py`,
  `routers/admin_mfa.py`). Off by default; only the owner turns it on
  (`/api/admin/me/mfa/setup` → `/confirm`, the secret travelling in a
  short-lived `aud="mfa-setup"` token until the first code proves the app has
  it). With it on, `POST /api/admin/login` answers `{mfa_required, mfa_token}`
  instead of a staff token; the `aud="mfa-pending"` token (5 min, bound to
  the account's `token_version`) plus a TOTP or one-time recovery code buys
  the staff token at `POST /api/admin/login/mfa`. Both intermediate tokens
  carry an `aud`, so no other decoder accepts them. The code step draws on
  the same per-account budget as the password step, whose earlier failures a
  correct password does not clear; a TOTP step is recorded
  (`users.totp_last_step`) so a code cannot be replayed, and the pending token
  carries a fingerprint of that state, so it is redeemed once only. Secrets are sealed with AES-GCM under a key
  derived from `WSI_MFA_KEY`, recovery codes kept as HMACs under another; no
  key → enrolment refused and enrolled sign-ins fail closed (503). Turning
  it on or off, and an admin's reset of another member
  (`POST /api/admin/users/{id}/mfa/reset`, never one's own), bumps
  `token_version`; `services/app/tools/reset_mfa.py` does the reset from the
  server.
- **Role-permission flags govern Analytics / Server Info view access** for
  non-admin roles. Toggling a flag OFF immediately removes both the API
  access (HTTP 403) and the UI affordance.
- **Staff page views are tagged.** Page-view beacons sent by an
  authenticated staff member carry an `actor_role` tag (`admin` /
  `manager` / `instructor`); the default analytics aggregation excludes
  these rows so learner traffic is reported in isolation.

## Content visibility

Roles decide *who may administer* content; visibility decides *what a viewer
can reach*. The two are independent. Every `Slide` and `Tour` carries two
orthogonal boolean flags — `is_published` and `unlisted` — that combine into
three effective levels, surfaced in the dashboard as a single three-way
control:

| Level | Flags | Listed in index / search / facets | Direct `public_id` URL |
|---|---|:---:|:---:|
| **Draft** | `is_published = false` | no | 404 |
| **Link-only** ("unlisted") | `is_published = true`, `unlisted = true` | no | served |
| **Public** (labelled "Open" in the dashboard) | `is_published = true`, `unlisted = false` | yes | served |

The two flags are enforced in different places, and the distinction matters:

- **`is_published` is the access gate.** A draft 404s on the public detail
  endpoint and on the app's `/dzi/` routes — 404 rather than 403, so an
  unauthenticated probe cannot distinguish "does not exist" from "not yet
  published". Through nginx a `/dzi/` request for a draft never reaches those
  routes: the edge check (`/api/edge-authz`) refuses it with 403, the same
  status it gives a slide that does not exist, so it is no oracle either. A
  valid staff JWT bypasses the gate, in the app routes (`_is_staff_request`
  in `routers/public.py`; `dzi.py` has the same check under the name `_is_admin_request`, which despite the name
  admits any staff role) and at the edge too (`/api/edge-authz` in
  `routers/app_auth.py` passes `request.state.staff_bearer` to
  `dzi.edge_slide_access`) so the dashboard can preview a draft through the same
  token-keyed URLs the viewer uses.
- **A tour additionally depends on its parent slide.** `get_tour` re-checks the
  parent and 404s when it is a draft, even if the tour row itself says
  published. Unpublishing a slide already cascades its tours to draft; this
  second gate is defence in depth against any path that leaves the two out of
  sync. So the table above describes a tour whose slide is published — which is
  the only state the cascade normally allows.
- **`unlisted` is only a listing filter.** It removes the item from
  `GET /api/slides`, `GET /api/tours`, `GET /api/index/tours` and
  `GET /api/facets` (an unlisted tour also never makes its slide match a
  `/api/slides` search), but gates nothing: a published+unlisted slide
  serves its metadata and tiles normally. What makes link-only sharing safe
  is therefore the unguessable `public_id` token, not this flag. It is also
  why the facets endpoint drops the attribute values of unlisted slides: a
  facet count that included them would let an unlisted slide be inferred —
  and narrowed down — from the browse filters alone. (The registered metadata
  options, the dashboard's shared vocabulary, are the one exception: they are
  listed at count 0 whether or not a visible slide carries them, so a value
  that only a hidden slide uses still shows up by name, matching nothing.)
- **An unlisted parent slide hides all of its tours** from the global tour
  browse; a tour's own flag hides just that tour. The per-slide tour listing
  re-checks only the tour's own flag, since a caller who reached that slide
  already holds its link.
- **A live-sync session on a link-only slide is not announced.** The
  discovery WebSocket (`WS /api/sync/discovery`, the index page's join banner)
  lists a session only while its current slide is published *and* listed
  (`_listed_slide_labels` in `routers/sync.py`): a
  session started on a link-only slide never appears, and one the presenter
  moves onto a link-only slide is withdrawn (index pages get `ended` for it)
  until it moves back onto a listed slide. The audience joins through the
  presenter's share link / QR (`?sync=<sid>`), which works as for a listed session — the
  session peek and the follower socket gate on `is_published` only. A
  session that was never listed never has its id sent to index pages, not
  even in an `ended` when it finishes (nor does one already withdrawn get a
  second `ended`). One that *was* listed has had its id sent, and the id is
  all a follower presents, so whoever saw the banner can still follow it onto
  the link-only slide: refusing them would refuse every follower already in
  the room on its next reconnect too. The presenter is told instead
  (`unlisted_announced`, `RoomManager._warn_unlisted_announced`).
- **`unlisted` is settable in any publish state**
  (`POST /api/admin/slides/{id}/unlisted`, `POST /api/admin/tours/{id}/unlisted`,
  both `staff`), so a draft can be pre-marked link-only *before* it is
  published — the item then never flashes onto the index in the window between
  two toggles.
- **No cache eviction on the unlisted toggle.** The tile gates (the `/dzi/`
  routes, the edge-authz verdict, the slide resolve cache) read the publish
  flag only, which unlisting does not change. Publishing / unpublishing evicts
  the slide's DZI handle and flushes the resolve cache (and unpublishing
  renames the thumbnail) — see `operations.md` → "Publish state, deletion,
  replacement, and cache staleness".

---

## Component Details

### Frontend (OpenSeadragon)

- OpenSeadragon instance initialised in `ViewerPage.tsx` / `TourViewerPage.tsx`
  (and, for the tour editor, `hooks/useTourViewer.ts`), with the shared setup
  in `lib/osd.ts`
- DZI XML descriptor at `/dzi/{public_id}.dzi?v={tile_version}` used as the
  tile source; OpenSeadragon copies the `?v=` onto every tile URL, so a WSI
  replace (which bumps `tile_version`) yields new tile URLs (see Cache Layers)
- SZI archives use `szi-tile-source` to read DZI tiles directly via HTTP Range
- OpenSeadragon automatically requests only the tiles needed for the current viewport
- Tile size: 256 px, overlap: 1 px (tunable via `DZI_TILE_SIZE` / `DZI_OVERLAP`)
- Image adjustment (brightness/contrast/saturation) is a CSS filter applied to
  the OSD canvas (`frontend/src/lib/imageAdjust.ts`), seeded from the slide's
  saved per-slide preset (`Slide.image_adjust_*`) so dark scans render corrected
  from first paint; viewers can further tweak for their session. The same preset
  filter is applied to the index/list thumbnails; the admin slide list adds a
  small icon overlay on thumbnails whose preset is active
- Info panel (`components/SlideInfoPanel.tsx`): one component for the viewer
  and the tour viewer, so both show the same bottom-right panel — the slide's
  catalog ID and title, the attributes picked under **Metadata → Display
  settings** (`extra_attrs`), and the logo and copyright message from
  **Settings → Slide & tour viewers (info panel)**. `show_metadata_panel` (site
  default, per-slide override) hides it in both
- Image credit: each slide's rights holder / license / source link
  (`Slide.credit_text` / `license_id` / `source_url`) is shown bottom-right in
  the viewer and tour viewer whenever set, independent of the metadata-panel
  toggle (see [developer-guide.md](./developer-guide.md#image-credit-admin))

### Backend (FastAPI)

Key endpoints:

| Endpoint | Purpose | Required Role |
|----------|---------|---------------|
| `GET /dzi/{public_id}.dzi?v=N` | Return DZI XML descriptor. `v` is the slide's `tile_version`: a non-current `v` is 404 (uncacheable), an absent one is served but never edge-cached | — (app-access; drafts staff-only) |
| `GET /dzi/{public_id}_files/{level}/{col}_{row}.jpeg?v=N` | Return tile image (`level` / `col` / `row` in canonical decimal only — `010`, `+1` etc. are 404, so one tile has one cache key; same `v` rule) | — (app-access; drafts staff-only) |
| `GET /dzi/{public_id}.szi?v=N` | Serve SZI archive with Range support (served **uncached** so `Range` reaches the app; see Cache Layers; same `v` rule) | — (app-access; drafts staff-only) |
| `POST /api/admin/slides/upload` | Upload a WSI file (rejects a filename already used by another slide, case-insensitive, with HTTP 409; names are stored as ASCII — a name that loses non-ASCII characters gets a short hash (8 hex chars of SHA-1 of its lower-cased NFC stem) appended, so `胃生検0a1b.svs` becomes `0a1b_<hash>.svs`) | manager+ |
| `GET /api/slides/{public_id}` | Return slide metadata | — (app-access; drafts staff-only) |
| `POST /api/analytics/pageview` | Record a page view | — |
| `POST /api/analytics/slide-dwell` | Record viewer-page dwell time (per-session, authenticated only) | — |
| `POST /api/analytics/tour-event` | Record tour start / progress / complete events (per-session, authenticated only) | — |
| `GET /api/admin/analytics/dashboard` | Aggregated access-analytics dashboard (page views, top slides/tours/users, slide dwell, tour completion) | analytics-access |
| `DELETE /api/admin/analytics/data` | Wipe analytics rows: scoped via `?from_date=YYYY-MM-DD` and/or `?to_date=YYYY-MM-DD`, or everything with `?all=1` (no scope → 400); a scoped wipe answers `"complete": false` until repeated to completion | admin |
| `DELETE /api/admin/analytics/user` | Purge a single user's rows across all three event tables (GDPR off-boarding); `identifier` as a form field | admin |
| `POST /api/admin/analytics/user/preview` | What that purge would match (stored identifiers, rows per table), deleting nothing | admin |
| `POST /api/admin/analytics/pseudonymize` | Rewrite rows recorded in plain mode into the hashed form; repeated until `"complete": true` | admin |
| `GET /api/admin/backup/estimate` | Approximate per-component backup size (DB core, access statistics, activity history, WSI, thumbnails) for the create-backup size hints | admin |
| `POST /api/admin/sync/sessions` | Create a live-sync session (served by `wsi_sync`) | staff |
| `WS /api/sync/ws/{sid}` | Live-sync WebSocket presenter / follower connection (served by `wsi_sync`) | — (follower: app-access; presenter: staff token in the hello frame) |
| `WS /api/sync/discovery` | WebSocket that monitors session presence (served by `wsi_sync`) | — (app-access) |

"Required Role" uses the convention from the **Authorization & Roles**
section: `admin`, `manager+` (admin or manager), `staff` (any of the
three roles), `analytics-access` / `server-access` (admin plus the
matching `RolePermissions` flag). A dash (`—`) marks endpoints that are
not gated by a staff role — they either ride on the public app-access
session or accept anonymous requests.

Worker configuration (`wsi-app.service` launches via `start-wsi-app.sh`):

```bash
# 1) UVICORN_WORKERS (explicit)  2) UVICORN_WORKERS_PERCENT (neither is capped at 8 nor by RAM)
# 3) Default formula: min(max(2, cores/2), 8), then capped by the RAM budget
#    (wsi-calc.sh). Always clamped to [1, nproc].
# DZI_CACHE_MAX_BYTES, unless set in wsi.env, is derived per worker from RAM
# and exported before uvicorn forks; TMPDIR is pointed at <DATA_DIR>/.upload-tmp.
exec /opt/wsi/venv/bin/uvicorn wsi_app.main:app --host 127.0.0.1 --port 8000 \
     --workers "${WORKERS}" --ws-max-size 65536 --no-proxy-headers
```

The arithmetic lives in `infra/systemd/wsi-calc.sh`, which
`infra/set-workers.sh` sources too so its preview matches what the service
starts with. The RAM budget is `WSI_CACHE_RAM_FRACTION` (default 75 %) of RAM,
keeping at least `WSI_CACHE_RESERVE_MIN_BYTES` (2 GiB) back; the default path
spawns no more workers than can each get `WSI_CACHE_FLOOR_BYTES` (2 GiB) of
it, and the derived per-worker `DZI_CACHE_MAX_BYTES` is the budget split
across the workers, clamped to 2–8 GiB (`WSI_CACHE_FLOOR_BYTES` /
`WSI_CACHE_CEIL_BYTES`). See `operations.md` → "Soft byte cap —
`DZI_CACHE_MAX_BYTES`".

Uvicorn deliberately runs with `--no-proxy-headers`. The bind is loopback-
only, so every TCP peer is `127.0.0.1` by construction; the trust gate in
`cf_access._is_trusted_hop` and the geo block in `infra/nginx/wsi.conf`
both rely on that invariant. Real-client IPs come from `Cf-Connecting-IP`
/ `X-Real-IP` (handled in `network.get_client_ip`, which uses a header value only if it is an IP address, without an IPv6 zone id (`fe80::1%eth0` is accepted by `ipaddress` with any text after the `%`): the result keys the login throttles beside strings the app makes up itself) and the original edge
scheme from `X-Forwarded-Proto`, which nginx sets to the `Cf-Visitor` scheme
when the hop is loopback and that header names one, else to `$scheme`;
`Cf-Connecting-IP` is blanked for non-loopback peers (`geo $cf_hop_trusted`
and its maps), and `X-Real-IP` is always set to `$remote_addr`.

`start-wsi-app.sh` also performs the restore-apply phase before launching
uvicorn: if `/run/wsi/restore.request` is present (written by the
Backup & Restore admin UI before triggering a service restart), it swaps the
staged DB / assets / WSI files into place and writes the outcome to
`/run/wsi/restore.result` — unless the request is more than 10 minutes old
(by the boot clock it records, so a system-clock step does not move it),
which means no restart picked it up when it was made, so it is reported
expired instead of applied. The swap and its journal-based crash recovery are
`wsi_app/restore_apply.py` (standard library only, run as
`python -m wsi_app.restore_apply`); the script itself only gates on it,
refusing to exec uvicorn while an interrupted apply's journal remains.

### Live-Sync Coordinator (`wsi-sync.service`)

A second uvicorn instance runs the same `wsi_app.main:app` with `--workers 1`
on port 8001. The live-sync feature relies on per-process in-memory rooms
(`RoomManager` in `sync_room.py`); pinning the sync workload to a single
worker keeps fan-out coherent without dragging in Redis, while letting the
main `wsi-app` stay multi-worker for DZI tile throughput. Nginx routes
every `/api/sync/*` and `/api/admin/sync/*` request — REST plus the
WebSocket upgrades — to this upstream.

The unit deliberately does **not** declare `PartOf=wsi-app.service`.
`PartOf` propagates restarts, and cascading a `wsi-app` restart (a worker-
or cache-size change, an operator's `systemctl restart`) into the
single-worker sync process would wipe every in-memory room, disconnecting
a whole in-progress class. Settings changes restart nothing: auth /
IP-filter settings live in a per-process snapshot that the handling worker
refreshes at once and every other process — the `wsi-app` workers and the
sync coordinator alike — re-reads through the periodic snapshot refresh in
the app lifespan (`main._settings_refresh_loop`,
`WSI_SETTINGS_REFRESH_SECONDS`, default 5 s). `After=wsi-app.service`
still orders boot, `WantedBy=multi-user.target` keeps it in the boot set,
and `update.sh` stops/starts it explicitly.

### Tile Generation (`dzi.py`)

```python
# Manual LRU via OrderedDict (default 8 slides, tunable with DZI_CACHE_SIZE)
# @lru_cache is not used: osr.close() must be called reliably on every eviction
_dz_cache: "OrderedDict[int, _DZEntry]" = OrderedDict()
# _DZEntry bundles the OpenSlide handle, DeepZoomGenerator, etag, file-size,
# path, tile generation, and a refcount used for safe deferred teardown on eviction.
```

- `DeepZoomGenerator` instances are cached (default `DZI_CACHE_SIZE=8` slides)
- `osr.close()` is called on every eviction to release file handles
- Two-stage eviction: by entry count first, then by total slide *file size*
  (`DZI_CACHE_MAX_BYTES` per worker — used as a proxy for the OS page-cache
  footprint, not a literal in-RAM tile cap). Under systemd `start-wsi-app.sh`
  derives it from RAM unless it is set (see Worker configuration above); the
  built-in 8 GiB default applies only to runs that bypass that script
- Each tile request has OpenSlide read the region and Pillow encode it to JPEG.
  A tile's source region is the next-finer stored level under it — and for
  the lowest zooms the smallest level whole — so a slide without a pyramid
  would read its full base image per low-zoom tile (gigabytes of RGBA on a
  large one). A region over 16 Mpx is therefore read in 16 Mpx blocks, each
  shrunk as it is read (`BoundedDeepZoomGenerator`): peak memory per tile
  stays bounded on any slide already stored, at a CPU cost proportional to
  the region — up to a minute or more for the lowest zooms of a large flat
  file. Such block-read tiles render one at a time per worker process, and
  requests that arrive while a tile is being rendered wait for it and get
  the same result (each slide handle keeps its last 8), since nginx's
  `proxy_cache_lock` holds concurrent misses for only 10 s before letting
  each through. At most 8 such requests wait per worker; the next gets a
  503 with `Retry-After` (not cached by nginx) instead of holding one more
  threadpool thread for minutes, and the viewer fetches that tile again on
  its next pan or zoom. Ordinary tiles never wait on either. The admin
  Performance benchmark renders through a slot and queue of its own (one
  per run, so a run left with a thread stuck in a read cannot stall the
  next), so while a measurement runs a worker can hold a second block-read
  render beside its viewers' one — plus one per earlier run still stuck —
  each within the same per-tile memory bound. Upload / replace /
  import refuse such layouts up front (see
  [Supported WSI Formats](#supported-wsi-formats))
- libopenslide aborts the whole process (rather than raising) on some
  malformed files, so a TIFF is walked with tifffile before OpenSlide opens
  it: a tile beyond 16384 px a side or 64 Mpx, or a file tifffile cannot walk for
  any reason other than plain damage, is refused (so is, at upload, a TIFF
  with more than 4096 directories; a stored slide is walked up to 65536).
  Upload / replace / import validation also opens the file and decodes the
  first tile of every level — the whole smallest level when it is at most
  64 Mpx — in a short-lived child process first; a file that crashes the
  child, or keeps it busy past 120 s, is refused (`wsiUnreadable`) without touching the worker
- The entry for one slide is evicted (`clear_dz_cache(slide_id)`) whenever its
  WSI binary is replaced, its publish flag is toggled, or the slide is deleted,
  so the next tile request re-opens against the current bytes/state. A brand-new
  upload needs no eviction — it has no cached entry yet
- An entry only pairs a handle with the generation its file belongs to:
  `_open_dz` re-`stat`s the path after reading the generation and opens again
  if a replace swapped the file meanwhile, and an entry another request cached
  while this one was opening is checked against its file before it is shared
- That eviction reaches only the worker that handled the change. Every other
  worker re-`stat`s a cached entry's file whenever that slide is requested,
  and — since a deleted slide is never requested again — also re-checks every
  cached entry (at most `DZI_CACHE_SIZE`) once each time it sees the
  slide-meta sentinel move, which every delete / replace bumps. Entries whose
  file is gone or different are closed (once no in-flight read holds them), so
  a deleted slide's unlinked file does not stay open — and its disk space
  unreclaimed — until LRU eviction

### Nginx Cache (`wsi.conf` + `wsi-locations.conf`)

The cache zone is declared in `infra/nginx/wsi.conf` (`proxy_cache_path`),
while the per-location cache directives (`proxy_cache_key`,
`proxy_cache_valid`, lock/stale tuning, `X-Cache-Status`) live in the
`infra/nginx/wsi-locations.conf` snippet that `wsi.conf` pulls in via
`include`. That zone (`wsi_dzi_cache`) backs `/dzi/`; `/thumbs/` and
`/assets/` share a separate `wsi_static_cache` zone (see the table below).

The cache key is the request URL — it does not fold in the session cookie —
so all authenticated users share one entry per object. For `/dzi/` tiles and
descriptors it is the path plus **only** the `v` query argument (the slide's
WSI generation, `?v=$arg_v`): a replace bumps the generation, so viewers move
onto keys no old tile was stored under, and every other query argument folds
out so junk queries cannot mint entries. The app 404s (uncacheably, via
`X-Accel-Expires: 0`) any `v` that is not current, non-canonical or
duplicated, so one key only ever holds one generation's bytes; a request with
no `v` (a script or other non-SPA caller) bypasses the cache
entirely (`$wsi_dzi_unversioned`).

The gates are enforced by an `auth_request` subrequest instead of the key (see
the Cache Layers section below and `/api/edge-authz`), so a cache HIT can't
bypass them: app access for every surface, and for `/dzi/` also the per-slide
publish gate (published, or a staff bearer for a draft preview) — nginx passes
the original URI as `X-Original-URI`, and the app answers 403 for a slide that
is unpublished or gone. The authz verdict is cached in a **separate** zone,
`wsi_authz_cache`: its key contains a cookie chosen by the client, so a client
that has not logged in could otherwise fill the tile zone with one 30-day
entry per made-up cookie and push real tiles out. That zone is small
(`keys_zone=wsi_authz_cache:4m`), drops entries after 2 minutes unused
(`inactive=2m`), keeps only allow verdicts (`200 204 60s` — which makes 60 s
the edge revocation latency of an unpublish / delete) and never stores a
401/403. The verdict's `authz|`-prefixed key folds in the session cookie, the
resolved client IP (`$wsi_rl_key`) and, for `/dzi/`, the slide token
(`$wsi_authz_object`, a `map` on `$request_uri` — inside the subrequest `$uri`
is `/_wsi_authz`). The IP is included so an IP-filter decision made for one
client is never reused for another (e.g. in `mode=none`, where every client is
cookie-less and would otherwise share one verdict); the slide so an allow for
one slide is never replayed for another. Neither is in the content key, so the
tile/thumb/asset cache is still shared across users; `auth_request` runs
before a HIT is served, so the verdict is enough to gate cached content. Because
that map reads the raw URI while the content key uses the normalised one, the
`/dzi/` locations refuse (400) any path containing `//`, a dot segment or a
`%`, which makes the two identical.

```nginx
# infra/nginx/wsi.conf
proxy_cache_path /var/cache/nginx/wsi_dzi
    levels=1:2
    keys_zone=wsi_dzi_cache:400m
    max_size=50g
    inactive=30d
    use_temp_path=off;
proxy_cache_path /var/cache/nginx/wsi_authz   # /_wsi_authz verdicts only
    levels=1:2
    keys_zone=wsi_authz_cache:4m
    inactive=2m
    use_temp_path=off;
map $request_uri $wsi_authz_object { ... }    # "dzi:<public_id>" for /dzi/ ("dzi:?" if unparsable), "" otherwise
map $arg_v $wsi_dzi_unversioned { "" 1; default ""; }

# infra/nginx/wsi-locations.conf — /_wsi_authz
proxy_set_header X-Original-URI $request_uri;
proxy_cache_key "authz|$wsi_rl_key|$wsi_xfp|$host|$http_x_forwarded_for|$http_cookie|$wsi_authz_object";   # everything the verdict depends on: client, scheme, forwarded chain, the whole Cookie header, the slide
proxy_cache_valid 200 204 60s;           # allow verdicts only; 401/403 never stored

# infra/nginx/wsi-locations.conf — /dzi/ (per-location)
auth_request /_wsi_authz;                # app-access + per-slide gate at the edge
proxy_cache_key "$scheme$proxy_host$uri?v=$arg_v";   # path + generation → shared across users
proxy_cache_valid 200 302 30d;                       # tile IMAGES: long-lived
# (no `proxy_cache_valid 404`: a 404 is never stored, see the comment in the config)
# The .dzi DESCRIPTOR is a nested location keyed the same way, with a short
# TTL + revalidate kept as a second line of defence:
#   location ~ "\.dzi$" { proxy_cache_valid 200 302 5m; proxy_cache_revalidate on; ... }
proxy_cache_lock on;
proxy_cache_lock_timeout 10s;
proxy_cache_lock_age 10s;
proxy_cache_use_stale error timeout updating;
proxy_ignore_headers Cache-Control;      # nginx's own TTL comes from proxy_cache_valid
                                         # (X-Accel-Expires is honoured: the app's "0" = don't store)
proxy_hide_header Cache-Control;         # the browser's policy is set here instead:
add_header Cache-Control $wsi_dzi_tile_cache_control always;
# "private, max-age=86400" for a 200/304 under ?v= (a tile never changes under
# its generation), "no-cache" otherwise, whatever Cache-Control a cached
# entry holds. The app sends no-cache on everything; the nested .dzi / .szi
# locations pass that through (proxy_pass_header Cache-Control).
proxy_no_cache    $wsi_auth_nocache $wsi_cf_access_jwt $wsi_dzi_unversioned;   # keep admin/CF/unversioned out
proxy_cache_bypass $wsi_auth_nocache $wsi_cf_access_jwt $wsi_dzi_unversioned;
# $wsi_auth_nocache (map in wsi.conf) is set for any Authorization header
# except HTTP Basic: a staff Bearer unlocks drafts and must never be shared,
# while app-access Basic only reaches published content. A successful Basic
# check also mints wsi_session, so later requests skip the pbkdf2 verify.
# The /_wsi_authz verdict itself is cached only for cookie-only requests
# ($wsi_authz_nocache): with an Authorization header the edge check is live.
add_header X-Cache-Status $upstream_cache_status;
```

| Parameter | Value | Description |
|-----------|-------|-------------|
| `max_size` | 50 GB | Maximum cache size on disk of the tile zone (`/dzi/`). `/thumbs/` and `/assets/` have a zone of their own (`wsi_static_cache`, 32 MB `keys_zone`, 10 min `inactive`, no `max_size`) so client-chosen names cannot evict tiles |
| `keys_zone` | 400 MB | Index of cached files (~8,000 per MB); once full, nginx evicts regardless of `max_size`. Sized with `max_size` (1 MB per 128 MB, at least 200 MB, at most a quarter of RAM — nginx maps the whole zone at `nginx -t` and fails past RAM + swap) by `infra/nginx-cache.sh` and `install.sh` (`wsi_nginx_keys_zone_mb`, `infra/lib/wsi-common.sh`); RAM is used only as it fills |
| TTL | 30 days (`/dzi/` tile images); 5 min + revalidate (`.dzi` descriptor); 60 s + revalidate (`/thumbs/`); 1h + revalidate (`/assets/`) | Tile images are immutable per URL — a WSI replace changes the URL's `?v=` — so they cache long; thumbnails and assets can change in place (WSI replace, asset overwrite), so they revalidate via the file's mtime ETag once stale. A thumbnail's URL is also revoked by renaming the file when its slide is unpublished (or removed with the slide), and `/thumbs/` has no per-slide edge check, so its TTL is that revocation's latency |
| `proxy_cache_lock` | on | Serialises duplicate requests for the same object. `/thumbs/` and `/assets/` do not use `use_stale updating`: a refresh that finds the file gone stores nothing, so handing the stale copy to overlapping requests would keep a deleted thumbnail servable for as long as it is asked for |
| Cache key | request path, `$uri` (no cookie); `/dzi/` appends `?v=$arg_v`, `/thumbs/` and `/assets/` no query at all | Shared across all authenticated users; the `auth_request` gate (`/api/edge-authz`) enforces app-access — and, for `/dzi/`, the per-slide publish gate — without fragmenting the cache |
| Authz verdict key | `authz\|<client IP>\|<scheme>\|<Host>\|<X-Forwarded-For>\|<Cookie header>\|<slide>` | One cached allow per client per slide for 60 s (the unpublish / delete revocation latency); denials never cached |

### Nginx Rate Limiting

```nginx
limit_req_zone $wsi_rl_key zone=wsi_admin_login:10m rate=10r/m;
limit_req_zone $wsi_rl_key zone=wsi_app_login:10m   rate=10r/m;
limit_req_zone $wsi_rl_net64 zone=wsi_admin_login_net:10m rate=30r/m;
limit_req_zone $wsi_rl_net64 zone=wsi_app_login_net:10m   rate=120r/m;
limit_req_zone $wsi_rl_key zone=wsi_api:10m         rate=120r/m;
limit_req_zone $wsi_rl_key zone=wsi_sync_ws:10m     rate=600r/m;
limit_req_zone $wsi_rl_key zone=wsi_slide_detail:10m rate=600r/m;
limit_req_zone $wsi_rl_key zone=wsi_page_reads:10m  rate=600r/m;
limit_req_zone $wsi_rl_key zone=wsi_sync_peek:10m   rate=600r/m;
limit_req_zone $wsi_rl_key zone=wsi_spa:10m         rate=600r/m;
limit_req_zone $wsi_dzi_uncached_key zone=wsi_dzi_uncached:10m rate=600r/m;
limit_req_zone $wsi_dzi_authed_key zone=wsi_dzi_authed:10m rate=3000r/m;
```

`$wsi_rl_key` is `$remote_addr` for non-loopback peers and the
`Cf-Connecting-IP` header (when forwarded over the trusted loopback hop)
for Cloudflare-tunneled requests, so rate limits attribute to the real
client rather than the tunnel. `$wsi_rl_net64` is the IPv6 `/64` that
address belongs to (empty, so not counted, for IPv4): an IPv6 client can take
a fresh source address from its `/64` for every request, so the two login
endpoints also cap each `/64`, on top of the per-address limit.

| Zone | Rate | Burst | Applies to |
|------|------|-------|-----------|
| `wsi_admin_login` | 10 req/min/IP | 5 | `POST /api/admin/login` (the two-factor code step, `/api/admin/login/mfa`, is under `wsi_api`: reachable only with the password step's token, its codes counted by the app's account lockout) |
| `wsi_app_login` | 10 req/min/IP | 5 | `POST /api/auth/login` (the app-access sign-in form: basic / LDAP; the Google OAuth start and callback are ordinary `wsi_api` routes) |
| `wsi_admin_login_net` | 30 req/min per IPv6 `/64` | 10 | `POST /api/admin/login`, in addition to `wsi_admin_login` |
| `wsi_app_login_net` | 120 req/min per IPv6 `/64` | 100 | `POST /api/auth/login`, in addition to `wsi_app_login` |
| `wsi_api` | 120 req/min/IP | 40 (interactive `/api/`, `/api/admin/` and the live-sync REST routes); 20 (the routes with their own body-size or streaming location: slide upload / replace, restore, slide import, tour asset upload, tour step writes, branding-image uploads, and the slide / export / backup downloads) | All other `/api/...` routes |
| `wsi_sync_ws` | 600 req/min/IP | 400 | The live-sync WebSocket handshakes, `/api/sync/ws/` and `/api/sync/discovery` |
| `wsi_slide_detail` | 600 req/min/IP | 200 | `GET /api/slides/<public_id>` (the slide detail read); the rest of `/api/slides/` stays in `wsi_api` |
| `wsi_page_reads` | 600 req/min/IP | 400 | `GET /api/tours/<public_id>` (the tour read), `GET /api/auth/state` and `GET /api/viewer/settings` (the two small reads every public page makes first); the rest of `/api/tours/` and the analytics beacons stay in `wsi_api` |
| `wsi_sync_peek` | 600 req/min/IP | 200 | `GET /api/sync/sessions/<sid>` only (the live-sync session peek); the rest of `/api/sync/` stays in `wsi_api` |
| `wsi_spa` | 600 req/min/IP | 100 | `location /` — the SPA fallback: index, JS/CSS bundles, fonts, and any non-`/api/`, non-`/dzi/`, non-`/thumbs/`, non-`/assets/` path |
| `wsi_dzi_uncached` | 600 req/min/IP | 400, of which the first 100 undelayed (`delay=100`) | `/dzi/` requests **without** `?v=` only (tiles, `.dzi`, `.szi`); the key is empty — not counted — for every versioned request |
| `wsi_dzi_authed` | 3000 req/min/IP | 600 (`nodelay`) | `/dzi/` requests carrying an `Authorization` header other than HTTP Basic (a staff Bearer, or a junk one): they bypass the shared cache both ways, so nginx cannot tell a staff preview from a request that makes the app render a tile every time; students carry no such header and are not counted, nor are requests with a Cloudflare Access JWT |

A public page load fires several `wsi_api` requests at once (slide list,
facets, tours, viewer settings, auth state, analytics beacon), so the zone is
sized for a human reloading and browsing rather than one request per page. The
two login zones stay at the tight 10 req/min, so relaxing general browsing does
not loosen brute-force protection.

The per-`/64` login zones are not simply the per-address ones re-keyed: a `/64`
is also a whole LAN segment, and on a campus Wi-Fi every student has an
address of their own in one `/64`, so a class signing in at the start of a
lesson must fit (100 app logins at once). A client rotating through its `/64`
is held to 120 (app) / 30 (dashboard) requests a minute in all, instead of 10
per address with 2^64 addresses. The other zones stay per address: they meter
browsing, not password guessing, and are not sized for a whole segment. The
app's own failed-login throttle (see `operations.md` → "Login throttles") is
what counts guesses.

`wsi_sync_ws` keeps the live-sync handshakes out of `wsi_api` and is sized for
a whole class behind one NAT address: when `wsi-sync` restarts or the network
blips, every follower and index-page discovery socket reconnects at once. 200
followers plus 200 discovery sockets from a single address fit in the burst
when their first attempt finds the server up. During a longer outage the failed
attempts count against the zone too and soon use up the burst, so the class
comes back at the rate, 10 req/s: that covers the steady retries of ~300
sockets once the client backoff reaches its 30 s cap, not all 400 (~13 req/s),
so a socket answered 429 retries at its next backoff step and the last ones are
back within about a minute. See `operations.md` → "Capacity and per-IP limits".

`wsi_slide_detail` is the same idea for the one REST read a live-sync follower
makes when the presenter switches slides: every follower fetches the new
slide's detail (`GET /api/slides/<public_id>`) at the same instant, so a class
behind one NAT address would exhaust the `wsi_api` burst and leave all but
the first 40 students on a "rate limited" banner. A room's worth (200) fits
in the burst; the viewer also retries a 429 on that read with a jittered
backoff, so a larger crowd still gets there at the zone's rate. The other reads
a class makes together on opening a tour link — the tour (`GET
/api/tours/<public_id>`, whose page has no retry), the sign-in state and the
site settings — are in a zone of their own, `wsi_page_reads` (burst 400), since
30 students x those three in one instant is more than the `wsi_api` burst even
before the analytics beacons (which stay there: they are unauthenticated
writes, and best-effort), and since a live-sync room sends the settings read
beside the slide read (and the sign-in state with it on a join): one zone for
all of them used up the burst on the small reads and left slide reads refused.
`wsi_sync_peek` does the same for the session peek each follower sends as it
opens the share link (a class scanning the QR code together); it is a zone of
its own so the two reads of one join do not share a burst.

`wsi_spa` is sized far above `wsi_api` on purpose: a cold SPA load legitimately
pulls dozens of bundles, fonts and header images through `location /` at once,
so a tight limit there would break normal browsing rather than abuse. Its point
is to keep a crawler or scanner spraying nonexistent paths from reaching uvicorn
unmetered ahead of the rate-limited `/api/` routes.

`/dzi/`, `/thumbs/`, and `/assets/` are otherwise not rate-limited. `limit_req`
runs before the cache lookup, so a zone over all their traffic would count every
cache HIT, and per IP it would throttle a class behind one NAT address (a
single iPad screen is 90-280 tiles). `wsi_dzi_uncached` meters only what is
known up front to reach the app every time: a `/dzi/` request with no `?v=`,
which the shared cache never stores (sent only by a script or other non-SPA
caller). On a slide
without a pyramid one low-zoom tile can mean reading the whole multi-GB image,
and nothing else bounded how often one client could ask for that. Past the
first 100, such requests are delayed to 10 per second (such a caller still
gets its tiles, slowly) and past 400 queued they get a 429. An edge-authz probe
that misses its cached verdict (a junk session cookie, a made-up slide id —
denials are never cached) also reaches the app, but cannot be told apart from
one that hits before the lookup; it costs a token check and an in-memory slide
lookup, not a render.

### Nginx Request Body Limits and Timeouts

The server block (`wsi.conf`) keeps every request small and short:
`client_max_body_size 2m`, 30 s client body / header timeouts and 60 s
`send_timeout` / `proxy_read_timeout` / `proxy_send_timeout`. The routes that
need more each have a location of their own in `wsi-locations.conf`:

| Location | Body cap | Timeouts | Buffering |
|---|---|---|---|
| `= /api/admin/slides/upload`, `~ ^/api/admin/slides/[0-9]+/wsi$` | `8193m` (8 GiB + 1 MiB of multipart framing) — rewritten by `install.sh` to fit `WSI_MAX_UPLOAD_BYTES` | 600 s (body, send, proxy) | `proxy_request_buffering off` |
| `= /api/admin/restore`, `= /api/admin/slides/import` | none (`0`); the app authenticates the request before reading the body and enforces `BACKUP_MAX_UPLOAD_BYTES` | 600 s (body, send, proxy) | `proxy_request_buffering off` |
| `~ ^/api/admin/tours/[0-9]+/steps(/[0-9]+)?$` (step create / save) | `4m` | 120 s body | — |
| Tour asset upload | `3m` — rewritten by `install.sh` to fit a larger `TOUR_ASSET_MAX_BYTES` | 120 s body | — |
| Logo, header-background and seasonal-background image uploads | `3m` | 120 s body | — |
| The slide, export-bundle and backup `…/download` routes | — | 600 s (send, proxy read) | `proxy_buffering off` (streamed, not spooled to disk) |
| `^~ /api/sync/ws/`, `= /api/sync/discovery` | — | 3600 s (proxy read / send), so an idle socket stays up | WebSocket `Upgrade` / `Connection` headers |

`infra/tests/test_upload_body_limits_nginx.sh` fails when an `UploadFile`
route is missing from its route table, when a large-upload route (slide
upload / replace, restore, import) has no location of its own, or when a
file at a route's app-side cap does not get through nginx.
`infra/tests/test_download_locations.sh` fails when a download route has no
location of its own. The security headers (and why the cached
`/dzi/`, `/thumbs/` and `/assets/` locations repeat them) are described in
`operations.md` → "Content-Security-Policy".

---

## Cache Layers

| Layer | Type | Scope | Details |
|-------|------|-------|---------|
| Nginx | Disk HTTP cache (tiles) | Server-wide | `wsi_dzi_cache`, 50 GB; `/dzi/` tiles (30-day TTL, keyed by tile generation `?v=`), **shared across all authenticated users** |
| Nginx | Disk HTTP cache (thumbnails / assets) | Server-wide | `wsi_static_cache`, no `max_size` (bounded by its 32 MB `keys_zone`); `/thumbs/` (60 s TTL) and `/assets/` (1h TTL), both revalidated, **shared across all authenticated users** |
| Nginx | authz verdict cache | Server-wide | `/api/edge-authz` allow verdicts, 60 s per client (IP + session cookie) and, for `/dzi/`, per slide |
| Application | Python LRU | Per worker process | Holds DeepZoomGenerator for default 8 slides (tunable via `DZI_CACHE_SIZE`) |
| Application | slide resolve cache | Per worker process | `public_id → (id, is_published, wsi_generation)` for the `/dzi/` routes and the edge authz check; flushed across workers by a sentinel on publish / delete / replace (see `operations.md` → "Slide-metadata resolve cache") |
| OS | Page cache | Server-wide | Kernel buffers WSI files in memory |

The nginx cache keys for `/dzi/`, `/thumbs/` and `/assets/` are the URL (for
`/dzi/`, the path plus the `v` version argument only; for `/thumbs/` and
`/assets/`, the path alone) — they do
**not** include the per-user session cookie — so N users viewing the same
slide/tour share ONE entry per object and cold work happens once, not once per
user.

**Tile content generations.** Tiles are immutable per URL because the URL
names the content: `Slide.wsi_generation` starts at 1 on upload, and every WSI
replace moves it to `max(generation + 1, now in ms)` twice — once before any
byte moves (retiring the old `v` on every worker, after which the replace
waits `WSI_SLIDE_META_GEN_CHECK_SECONDS`) and again in the replace's commit
(or, if the replace fails, once the old file is back) — so the clock floor
keeps it above every number used before, even after a restore put an older
value back. The API reports it as `tile_version`, the viewer opens
`/dzi/{public_id}.dzi?v={tile_version}`, and OpenSeadragon copies that query
onto every tile URL. A replace — even one that keeps the file
name, and so every tile path — therefore moves viewers onto new cache keys
instead of mixing the old scan's cached tiles with renders of the new one; a
`DZI_TILE_SIZE` / `DZI_OVERLAP` change does the same for every slide at the
next start (`dzi.reconcile_tile_geometry`). The app keeps each key
single-generation: a non-current, non-canonical or
duplicated `v` is a 404 marked `X-Accel-Expires: 0` (never stored), and an
absent `v` is served but also marked so (and bypassed by nginx). The
generation rides in the per-worker slide resolve cache, so checking it costs no
query; a worker that has not yet seen a replace's cache flush re-reads the row
when a request carries a generation newer than the one it cached. The `.szi`
archive URL carries `?v=` too, so szi-tile-source's Range reads of a replaced
archive fail cleanly instead of splicing two archives; the handler opens the
archive once, re-checks the generation after the open and serves that open
file, so a replace landing mid-request cannot put the new archive's bytes
under the old `v`. Under the interim generation a replace holds while it swaps
the file, the handler serves only an archive whose size matches the row's
recorded `file_size_bytes` (the old archive's until the final commit), so
range reads under that generation do not mix the two either — unless both
archives have exactly the same size (or no size was recorded, which skips the
check). A slide import that
keeps the bundle's `public_id` starts that token at a fresh, never-used
generation (`models.fresh_wsi_generation`), since tiles of a deleted earlier
incarnation may still be cached under its low generations.

**`.szi` archives are the one exception under `/dzi/`: they are served
uncached.** A `.szi` is a single (up to multi-GB) ZIP that `szi-tile-source`
reads piecewise via HTTP `Range`, starting with a suffix-range for the ZIP
central directory at the end of the file. nginx does **not** forward the
`Range` header to the upstream on a `proxy_cache` MISS — it fetches and stores
the *whole* object, then slices out the requested bytes. If the parent
`/dzi/` caching applied to `.szi`, every first open would pull the entire
archive through the app (measured: a 256 MB archive stalls the opening range
read ~2 s cold; an 8 GiB archive scales that to ~a minute) and write the whole
archive into the shared 50 GB tile cache, evicting real tiles — while also
masking the app's own `Range`/`206`/`304` handling in `dzi.get_szi`. A nested
`location ~ \.szi$ { proxy_cache off; proxy_buffering off; }` inside `/dzi/`
turns caching off for just `.szi`, restoring `Range` pass-through so the app
serves `206` partial content directly (cheap file I/O even for large archives)
and the archive never lands in the cache. DZI tiles
(`/dzi/{id}_files/.../*.jpeg`) are unaffected and stay cached/shared. The
behaviour is guarded by `infra/tests/test_szi_range_nginx.sh`, which boots
nginx with the real snippet and asserts Range pass-through, no-cache for
`.szi`, and continued caching of ordinary tiles. Trade-off: an uncached `.szi`
is re-read from disk (and re-resolved from the DB) on each range request rather
than served from the shared cache, which is acceptable at classroom scale on a
LAN; heavily-used `.szi` slides that need cross-user fan-out absorption are a
candidate for the nginx `slice` module (cache 206 chunks keyed by range) if
that ever becomes a bottleneck.

Without the cookie in the key, the gates are kept by the `auth_request` edge
check (`/api/edge-authz`, see [Nginx Cache](#nginx-cache-wsiconf--wsi-locationsconf)
above), so a cache HIT can never bypass auth. Because it applies the per-slide
publish gate to `/dzi/`, an unpublished or deleted slide's cached tiles are
refused at the edge within the verdict's 60 s rather than staying servable for
the tile cache's 30 days. `/thumbs/` (whose names rotate on unpublish) and
`/assets/` (which the tour editor's markdown preview of a draft tour fetches
without a bearer) keep the app-access-only verdict. Admin draft-preview
(Bearer) and Cloudflare-Access responses are kept out of the shared cache via
`proxy_no_cache`. `/thumbs/` and `/assets/` add `proxy_cache_revalidate on`
so a replaced thumbnail / overwritten asset is picked up (via its mtime ETag, a bodiless `304` while unchanged) once
the entry goes stale — after 60 s for `/thumbs/`, 1h for `/assets/`. The short
`/thumbs/` TTL is also how long an unpublished or deleted slide's old thumbnail
URL (renamed away / removed on disk) can still be answered from the cache. See
`infra/nginx/wsi-locations.conf` (the `/_wsi_authz`, `/dzi/`, `/thumbs/` and
`/assets/` blocks).

---

## Storage Layout

```text
/var/lib/wsi/
├── wsi/        # WSI files (SVS, NDPI, etc.; up to WSI_MAX_UPLOAD_BYTES, default 8 GiB each)
├── thumbs/     # Thumbnails (max 512×512 JPEG, generated on upload)
├── assets/
│   └── tours/  # Per-tour markdown attachments (images by default; audio / PDF / video
│              # only when TOUR_ASSET_ALLOWED_MIME_PREFIXES is widened from the image-only default)
├── backups/    # Backup archives created via the admin UI (configurable via BACKUP_DIR)
│   ├── .jobs/     # Cross-worker backup / restore / transfer job records (JSON, jobs.py)
│   └── .staging/  # Transient intermediates, reclaimed once over 24 h old by the staging
│                  # sweep (at a worker start, or when a restore / import is staged or a
│                  # reset prepared; not on a timer):
│                  #   stage-* / upload-* (whole-DB restore), wsi_backup_* (backup snapshot),
│                  #   export-* (generated slide-export bundles awaiting download),
│                  #   import-* (extracted slide-import bundles awaiting apply)
├── cache/      # Created on startup; holds slide_meta.gen, the sentinel whose bump
│              # tells every worker to flush its slide-metadata resolve cache
├── locks/      # Empty advisory-lock files (fcntl.flock), one per contended
│              # resource: schema-migration.lock serialises boot-time DDL across
│              # workers, slide-<id>.lock serialises mutations of one slide
│              # (publish / delete / replace / export), tour-assets-<id>.lock
│              # one tour's asset uploads. Never contains data;
│              # safe to delete while the services are stopped.
├── .admin_register.lock  # Empty advisory lock serialising the one-shot
│                         # ALLOW_ADMIN_REGISTER bootstrap across workers
├── .upload-tmp/  # TMPDIR of the app (set by start-wsi-app.sh): multipart upload
│                # spool files, on the data volume rather than in /tmp
├── .restore-apply.journal  # Present only while a restore / reset apply is in
│                           # progress; an interrupted apply is reconciled from it
│                           # on the next start
├── .pre-restore-<UTC>/  # The live DB / assets / wsi / thumbs a restore or reset
│                        # parked before its swap: the rollback artefact, kept
│                        # until removed from the Backup & Restore page
└── wsi.db      # SQLite DB (slide metadata, including each slide's macro
                # and label JPEGs / tours / users / settings /
                # page-views / slide-dwell events / tour sessions / audit and
                # error logs / performance runs). Live-sync sessions are held
                # only in memory (RoomManager), never persisted here.
                # WAL mode produces wsi.db-wal / wsi.db-shm siblings

/var/cache/nginx/wsi_dzi/   # DZI tile HTTP cache (up to 50 GB)
/var/cache/nginx/wsi_authz/ # Edge app-access verdict cache (/_wsi_authz, 60 s entries)
/var/cache/nginx/wsi_static/ # Thumbnail (/thumbs/) and tour attachment (/assets/) HTTP cache
/run/wsi/                   # Restore-apply IPC files (restore.request / restore.result,
                            # and restart.trigger, which wsi-restart.path watches)
```

---

## Supported WSI Formats

OpenSlide reads the vendor formats; OME-TIFF and SZI have their own
readers.

| Extension | Format |
|-----------|--------|
| `.svs` | Aperio (Leica) |
| `.ndpi` | Hamamatsu |
| `.tif` / `.tiff` | TIFF |
| `.ome.tif` / `.ome.tiff` | OME-TIFF (pyramidal brightfield RGB or grayscale; read by the app's tifffile-based reader, not OpenSlide) |
| `.scn` | Leica SCN |
| `.bif` | Ventana (Roche) slide format |
| `.szi` | SZI (pre-built DZI pyramid ZIP archive, OpenSlide-independent) |

Multi-file formats — MIRAX (`.mrxs`) and Hamamatsu VMS / VMU (`.vms` /
`.vmu`) — are **not supported**: each is a small index file plus companion
data files in a directory beside it, and an upload carries exactly one file,
so the slide could never be stored whole (OpenSlide would refuse it after the
upload). They are therefore absent from the default `WSI_ALLOWED_EXTENSIONS`
and the upload form's accept list, and dropped (with a startup WARNING) from a
`WSI_ALLOWED_EXTENSIONS` that still lists them. Convert such a slide to a single-file
format first — e.g. a tiled pyramidal TIFF with libvips (`vips tiffsave
slide.mrxs slide.tif --tile --pyramid --compression jpeg`, which reads the
source through OpenSlide), or a pyramidal OME-TIFF (QuPath's OME-TIFF export,
or Bio-Formats' `bioformats2raw` + `raw2ometiff`).

Upload, replace and import also refuse a file OpenSlide reads but whose
pyramid Deep Zoom cannot serve within bounds, with a specific message
(`slideLayout.<reason>`; the OME-TIFF reader applies its own, tighter version,
below):

| Case | Handling |
|------|----------|
| A single level over 64 Mpx — a tiled TIFF with no pyramid at all, larger than about 8000 × 8000 | Refused (`no_pyramid`) |
| A pyramid whose smallest level is still over 64 Mpx — e.g. a 130000 × 128000 scan whose 4× pyramid ends at 8125 × 8000 | Refused (`lowest_level_too_large`): the lowest zooms read the smallest level whole, so its size sets their cost whether or not levels sit above it. Real vendor pyramids stay under it — typically a few Mpx; a 4×-step SVS of a large scan whose pyramid stops 16× down can reach 30–50 Mpx, which is why the limit sits above the OME-TIFF reader's 16 Mpx |
| Consecutive levels more than 16× apart — beyond what rounding a level's size to whole pixels explains (a level up to one pixel smaller per side than an exact 16× step, e.g. 16385 → 1024, passes) | Refused (`level_gap`); real pyramids step 2× or 4× |

Convert such a file to a pyramidal tiled TIFF (`vips tiffsave in out.tif
--tile --pyramid --compression jpeg`). A stored slide that fails this check
(for example one copied into `wsi/` by hand) keeps opening; its low-zoom tiles are read in bounded blocks (see Tile Generation
above), so they cost CPU rather than memory.

A `.szi` upload (`szi_utils.py`) must be an **uncompressed (stored) ZIP**
with a root `<name>.dzi` descriptor (at most 1 MiB) and its
`<name>_files/<level>/` tiles; a deflated member, a missing descriptor or more
than 2,000,000 members is refused (`wsiUnreadable`), as is a descriptor whose
`Width` / `Height` exceed 2,000,000 px, whose `TileSize` is outside 64–4096 or
whose `Overlap` exceeds 1024. The DZI format carries no pixel size, so an SZI
slide starts with MPP 0 (no scale bar) until an admin sets it. Its thumbnail
is assembled from the smallest stored level that is at least 256 px on its
long side and at most 16 Mpx (else the largest level within 16 Mpx); an
archive with no level that small is refused (`thumbnailFailed`).
The app's `DZI_TILE_SIZE` / `DZI_OVERLAP` / `DZI_TILE_QUALITY` do not apply:
the browser reads the archive's own tiles.

**Macro / label images** are extracted at upload, replace and slide import and stored in
the database (downscaled to 1024 px on the long side, JPEG): OpenSlide's
associated `macro` / `label` images, or — for a TIFF-family file OpenSlide
does not recognise them in — any TIFF directory whose `ImageDescription`
starts with `macro` / `label` (the Aperio convention). One over 32 Mpx is
skipped, and a slide without them simply has none; SZI never has any.

### OME-TIFF

OpenSlide has no OME-TIFF driver: it opens one through its generic-TIFF
fallback, which walks only the main IFD chain. Modern OME-TIFF writers
(Bio-Formats 6+, QuPath, raw2ometiff, libvips) put the reduced resolutions
in SubIFDs, so OpenSlide would see a single full-resolution level with no
MPP, and every low-zoom tile would have to downsample the whole base image.
`wsi_app/ome_tiff.py` therefore reads OME-TIFF itself — tifffile for the
TIFF structure and OME-XML plane mapping, imagecodecs for the tile codecs —
and exposes it through openslide-python's `AbstractSlide` interface, so the
Deep Zoom endpoints, thumbnailer and macro/label extraction use it
unchanged. Detection is by content (OME-XML in the first IFD), not by the
`.ome.tif` suffix, which the upload staging name does not preserve.

What it serves, and what it refuses at upload (with a specific message):

| Case | Handling |
|------|----------|
| Brightfield RGB — interleaved, planar, or three 8-bit channel planes | Served; an alpha sample is kept, so transparent areas show the viewer background as with OpenSlide |
| Single-channel grayscale (8/16/32-bit, float) | Served; non-8-bit data gets one intensity window (0.5–99.5th percentile of the smallest level) used by every tile, so tiles don't show seams |
| 16-bit RGB | Served; scaled from 0 to the bit depth's maximum — OME `SignificantBits`, a TIFF `BitsPerSample` / `MaxSampleValue` below 16 bits, else the smallest common camera depth (8/10/12/14/16) covering the data (12-bit data maps from 4095) |
| Several OME Images | The first one in the file that is not a label / macro / thumbnail is the slide — the image OpenSlide would show for such a file. A name never demotes an image over 32 Mpx, which is too large to be a label or macro |
| Z-stack / time series | First plane (Z=0, T=0) |
| Label / macro images: OME Images whose name is just `label` / `macro` / `overview` / `barcode` (optionally with "image" / "slide", e.g. `label image`) | Extracted like vendor associated images; "CD68 macrophages" or "unlabeled" are not |
| Physical pixel size, objective, acquisition date | Read from OME-XML (units converted to µm) |
| Multi-channel fluorescence (C=2 or C≥4, more than 4 samples per pixel, or three non-8-bit channels) | Refused (`multichannel`) |
| Samples wider than 32 bits (float64, int64, complex) | Refused (`pixel_type`) |
| Unpyramided image over `WSI_OME_MAX_LOWEST_LEVEL_PIXELS` (16 Mpx) | Refused (`no_pyramid`); a smaller unpyramided image is served as is |
| A pyramid whose smallest level is still over `WSI_OME_MAX_LOWEST_LEVEL_PIXELS` | Refused (`lowest_level_too_large`) |
| Consecutive levels more than 16× apart, with the same one-pixel rounding allowance as above | Refused (`level_gap`) |
| A stored tile / strip over 16 Mpx (or `WSI_OME_MAX_LOWEST_LEVEL_PIXELS`, when that is set higher) or 128 MB decoded | Refused (`segment_size`) |
| A structurally broken file: OME-XML over 16 MiB, with DOCTYPE / ENTITY declarations or not parsing; levels that do not shrink or disagree on the pixel format; missing planes; volumetric pages; a segment table that is short or points past the end of the file (a truncated upload) | Refused (`structure`) |
| Compression: none, LZW, Deflate, zstd, PackBits, … (byte codecs), JPEG, JPEG 2000, WebP, PNG | Served. The image codecs' own headers are checked against the tile size before decoding, so a crafted header can't make a decoder allocate gigabytes; one such tile fails as a logged 500, and the first tile of every level is decoded at upload |
| JPEG XL, JPEG XR, old-style JPEG and other image codecs — on any level or channel plane, checked before any decode; a level that fails to decode | Refused (`codec`) |
| Multi-file datasets (companion / `BinaryOnly`, TiffData naming other files) | Refused (`multifile`) — following a FileName reference would read other files on the server |
| OME-XML that doesn't match the file's pages (tifffile can't map it) | Opened by OpenSlide as a generic TIFF — and at upload held to OpenSlide's layout limits above, so one whose reduced levels sit in SubIFDs (which OpenSlide doesn't see) is refused (`slideLayout.no_pyramid`) past 64 Mpx |

---

## Performance Characteristics

### Tile Response Times (rough estimates)

The admin **Performance** page measures these for a given install — cache-miss
tile cost, disk speed and throughput under parallel requests on the server, and
what a browser actually gets through nginx — and keeps the results for
before/after comparisons. See [operations.md → Performance measurement](./operations.md#performance-measurement--admin-performance-page).

| State | Tile latency |
|-------|-------------|
| Nginx cache hit | 1–5 ms |
| App LRU cache hit | 20–150 ms |
| Cold access (first request) | 115–620 ms |

### Concurrency Ceiling

```text
workers = min(max(2, CPU cores / 2), 8)   (default formula)
          then capped so each worker keeps a >= 2 GiB page-cache budget (RAM-aware)
          UVICORN_WORKERS or UVICORN_WORKERS_PERCENT to override (override path skips the 8-worker and RAM caps; still <= cores)
each worker runs blocking work (OpenSlide reads, DB queries) on a thread pool
          of WSI_REQUEST_THREADPOOL_TOKENS threads (default 40)
```

OpenSlide calls are blocking I/O, so latency grows as concurrent users increase.

### Expected Load Tolerance

| Scenario | Assessment |
|----------|-----------|
| Many users viewing the same slide / tour (e.g. an instructor-led class) | Absorbed by the shared Nginx cache — first view generates each tile once, the rest are HITs. Pre-warm the slide before heavy use for an all-HIT start. Holds regardless of app-access auth (the cache is shared across authenticated users; see Cache Layers) |
| Small number of users viewing different slides | LRU cache effective — generally fine |
| Many users simultaneously opening *distinct, unseen* slides | Worker saturation possible — this is CPU-bound on-demand tile generation, so raise `UVICORN_WORKERS` toward the core count, enlarge `DZI_CACHE_SIZE`, and prefer `.szi` (pre-built pyramid) for heavily-used slides |

---

## Improvement Opportunities

| Priority | Action | Benefit |
|----------|--------|---------|
| High | Pre-generate low zoom levels on upload | Eliminates first-view latency |
| Medium | Increase `DZI_CACHE_SIZE` (`infra/set-cache-size.sh`) to match server memory — `DZI_CACHE_MAX_BYTES` is already derived from RAM at start-up | Reduces overhead when switching slides |
| Low | Introduce Redis distributed cache | Useful in multi-server deployments |

---

## Related Files

| File | Purpose |
|------|---------|
| `services/app/wsi_app/dzi.py` | DZI tile generation, endpoints, SZI serving, per-slide edge-authz check |
| `services/app/wsi_app/slide_resolve_cache.py` | Per-worker `public_id → (id, is_published, wsi_generation)` cache + the `cache/slide_meta.gen` flush sentinel |
| `services/app/wsi_app/szi_utils.py` | SZI archive parsing + thumbnail extraction |
| `services/app/wsi_app/ome_tiff.py` | OME-TIFF reader (tifffile + imagecodecs behind openslide-python's `AbstractSlide`) |
| `infra/tests/test_szi_range_nginx.sh` | Boots nginx with the real snippet; asserts `.szi` Range pass-through + no-cache, and that ordinary tiles stay cached |
| `infra/tests/test_tile_cache_control_nginx.sh` | Boots nginx with the real snippet and maps; asserts a tile under `?v=` reaches the browser as `private, max-age=86400` (MISS, HIT of an entry stored with the app's `no-cache`, a 304, a staff Bearer) while an unversioned tile, a stale generation's 404, the `.dzi` descriptor, a `.szi` Range read and a refused path stay `no-cache` |
| `infra/tests/test_cache_key_nginx.sh` | Boots nginx with the real snippet; asserts non-canonical paths (`//`, dot segments, `%2F`; any `%` under `/dzi/`) under `/dzi/`, `/thumbs/`, `/assets/` are refused before they can poison the shared cache, that `/dzi/` cache keys carry the tile generation `?v=`, that an unversioned tile is never stored, and that the app's `X-Accel-Expires: 0` is honoured |
| `infra/tests/test_authz_cache_nginx.sh` | Boots nginx with the real snippet, the real `wsi_authz_cache` declaration and the real `/dzi/` maps; asserts app-access verdicts land in their own zone, are keyed per slide for `/dzi/` (with `X-Original-URI` forwarded), and 401/403 are not cached |
| `infra/tests/test_thumbs_ttl_nginx.sh` | Boots nginx with the real snippet; asserts `/thumbs/` caches a 200 for at most 60 s, that a thumbnail renamed away at the app (an unpublish) answers 404 once that is up, and that an unchanged one is revalidated with a `304` rather than re-fetched (takes about a minute) |
| `infra/tests/test_access_log_redaction_nginx.sh` | Boots nginx with the real snippet and the real `$wsi_log_request` map / `wsi_combined` format; asserts the `?t=` (or `?%74=`) token of every admin download route is logged as `t=[redacted]`, that every other line is exactly what `combined` writes, and that `wsi.conf`'s server block logs with that format through `wsi-access-log.conf` |
| `infra/tests/test_upload_body_limits_nginx.sh` | Boots nginx with the real snippet; asserts every large-upload route (slide upload/replace, restore, slide import) has its own location instead of inheriting the 2m server default, that a multipart file at the app's own cap reaches every capped upload route (tour assets, logo, favicon, header/seasonal backgrounds), and that every `UploadFile` route in the routers is covered |
| `infra/tests/test_restart_unit_order.sh` | Runs the real `wsi-restart.service` ExecStart against a stub `systemctl`; asserts a restore apply stops wsi-sync → restarts wsi-app → waits for the swap → starts wsi-sync, and a plain restart (no staged request) leaves wsi-sync alone |
| `services/app/wsi_app/routers/public.py` | Public REST API (slides / tours / facets / viewer settings) |
| `services/app/wsi_app/routers/admin_*.py` | Admin REST API (auth, mfa (two-factor sign-in), slides, slide transfer, metadata, tours, settings, system, backup, users, history, logs, perf) |
| `services/app/wsi_app/auth.py` | Staff JWTs, password hashing, signed download tokens, and the `require_*` role dependencies |
| `services/app/wsi_app/main.py` | App assembly: lifespan (schema reconciliation, migrations, settings refresh loop), the IP-filter / app-access / upload-preauth middleware, router and static mounts |
| `services/app/wsi_app/models.py` | SQLAlchemy ORM models (every table in `wsi.db`, including the `RolePermissions` singleton) |
| `services/app/wsi_app/audit_log.py` | DB half of the administrative action trail — `audit_logs` row + retention janitor (the journald line comes from `admin_helpers.audit()`) |
| `services/app/wsi_app/error_log.py` | WARNING+ capture into `error_logs` (queue handler + background writer) backing `/admin/logs` |
| `services/app/wsi_app/csv_export.py` | Shared CSV rendering for the analytics / audit / error-log exports (UTF-8 BOM, formula-injection defang, row ceiling) |
| `services/app/wsi_app/routers/app_auth.py` | App-access endpoints (`/api/auth/*`: state, basic / LDAP login, logout, Google OAuth start / callback) and the nginx `auth_request` probe `/api/edge-authz` |
| `services/app/wsi_app/routers/analytics.py` | Access-analytics REST API (page-view recording & dashboard) |
| `services/app/wsi_app/cf_access.py` | Cloudflare Access JWT verification + trust-hop checks |
| `services/app/wsi_app/app_auth.py` | App-access mode logic (session tokens, basic / LDAP / Google verification) |
| `services/app/wsi_app/network.py` | Trusted-proxy IP filter, `Cf-Connecting-IP` honoring, mDNS helper |
| `services/app/wsi_app/routers/sync.py` | Live-sync REST + WebSocket |
| `services/app/wsi_app/openslide_utils.py` | OpenSlide wrapper: slide open (OME-TIFF routing, pre-open TIFF walk), upload validation (`check_pyramid_layout`, child-process probe), thumbnails, macro / label extraction |
| `services/app/wsi_app/config.py` | Application configuration |
| `services/app/wsi_app/sync_room.py` | In-memory `RoomManager` for live-sync sessions |
| `services/app/wsi_app/backup.py` | Backup archive build / restore staging logic |
| `services/app/wsi_app/restore_apply.py` | Restore-apply swap + journal crash recovery (stdlib only; run by `start-wsi-app.sh`, called by the dev in-process apply) |
| `services/app/wsi_app/jobs.py` | Cross-worker backup / restore / slide-transfer job tracking (JSON-file store under `<BACKUP_DIR>/.jobs`) |
| `services/app/wsi_app/slide_transfer.py` | Slide export / import bundles (cross-Tessera migration) |
| `services/app/wsi_app/app_meta.py` | `app_meta` table I/O — stamps the running `APP_VERSION` and the integer `schema_version` on every boot and refuses to open a DB last touched by a newer binary / newer schema (downgrade guards). Additive schema reconciliation is handled at startup by `main._apply_column_migrations` (`ALTER TABLE ADD/DROP COLUMN` against the live ORM); transformational changes live in the `migrations` package. |
| `services/app/wsi_app/migrations/` | Ordered, numbered schema migrations for changes the reconciler can't express (back-fills, renames, table rebuilds). `run_migrations` applies pending steps and stamps `schema_version`, run in the lifespan between the reconciler's ADD and DROP phases. The only one registered so far is migration 6 (`_fence_staff_mfa_columns`), a no-op that only raises `schema_version` for the staff two-factor columns and `perf_runs.summary_json` (the reconciler adds the columns themselves); a database stamped below the baseline, 5, is refused. The runner itself is also exercised by synthetic migrations in the tests. See "Evolving the database schema" in the developer guide. |
| `infra/nginx/wsi.conf` | Nginx cache zones, maps, log format, rate-limit zones, both upstreams (`wsi_app` / `wsi_sync`) and the server block, which includes `wsi-tls.conf`, `wsi-access-log.conf` and `wsi-locations.conf` (where the per-location upstream split lives) |
| `infra/nginx/wsi-locations.conf` | `location`/`proxy_pass` blocks + per-location cache directives (installed to `/etc/nginx/snippets/`) |
| `infra/nginx/wsi-access-log.conf` | Default `access_log` of the site (masked `wsi_combined`), installed to `/etc/nginx/snippets/` only when missing — the operator's to edit |
| `infra/nginx/wsi-tls.conf` | The operator's TLS settings (`listen 443 ssl`, certificate, HSTS, redirect), included by the site's server block; installed to `/etc/nginx/snippets/` only when missing — the operator's to edit. A certbot-patched site file is migrated into it by the next `install.sh` / `update.sh` (`infra/lib/nginx_tls_carry.py`); a snippet with lines of the operator's own is merged into (their lines stay, only the `listen` address / `ssl_*` directive certbot has just written is replaced, the change is announced and the previous file kept as `.bak-<time>`) |
| `infra/systemd/wsi-app.service` | Main FastAPI uvicorn service (port 8000, multi-worker) |
| `infra/systemd/wsi-sync.service` | Live-sync coordinator (port 8001, single worker) |
| `infra/systemd/start-wsi-app.sh` | Restore-apply gate (runs `wsi_app.restore_apply`) + worker-count / `DZI_CACHE_MAX_BYTES` sizing + uvicorn launch |
| `infra/systemd/wsi-calc.sh` | Worker-count and per-worker DZI byte-cache arithmetic, shared by `start-wsi-app.sh` and `infra/set-workers.sh` (tested by `infra/tests/test_wsi_calc.sh`) |
| `infra/systemd/wsi-restart.{path,service}` | Restarts `wsi-app` when the app drops `/run/wsi/restart.trigger` (restore / reset apply), stopping and restarting `wsi-sync` around a restore |
| `infra/set-workers.sh` / `infra/set-cache-size.sh` / `infra/nginx-cache.sh` | Operator tools: worker count, `DZI_CACHE_SIZE`, nginx tile-cache stats / clear / `max_size` (with the matching `keys_zone`) |
| `infra/systemd/wsi-cache-stat.{service,timer,sh}` | Per-minute nginx tile-cache size sampler for the admin dashboard |
| `infra/systemd/wsi-avahi-reload.{path,service}` | Reloads avahi-daemon when the WSI mDNS service file changes |
| `infra/wsi.env.example` | Environment variable sample |
| `frontend/src/pages/ViewerPage.tsx` | OpenSeadragon viewer (DZI / SZI) |
| `frontend/src/pages/TourViewerPage.tsx` | Tour-mode viewer |
| `frontend/src/lib/osd.ts` | Shared OpenSeadragon setup (tile-source loading incl. SZI, zoom clamp, rotation / gesture handlers) |
| `frontend/src/lib/tileUrl.ts` | Builds the versioned `/dzi/…?v={tile_version}` descriptor / archive / tile URLs |
| `frontend/src/lib/admin/permissions.ts` | Dashboard gating for the Analytics / Server role-permission flags |
