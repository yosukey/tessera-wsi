# Tessera: An open-source WSI platform for learning histology and pathology

> [!NOTE]
> **Beta (0.9.x).** Tessera is in public beta. It is already in use for
> teaching, but before 1.0 the data formats, API and configuration may still
> change, and an upgrade may need steps beyond `infra/update.sh` (they are
> called out in the release notes). Back up (admin dashboard → Backup) before
> every update, and read the release notes first. Bug reports and feedback are
> welcome via [Issues](https://github.com/yosukey/tessera-wsi/issues).

![Tessera](frontend/public/brand.png)

Educational WSI (Whole Slide Image) viewer system with:
- **Single-file WSI formats**: SVS / NDPI / TIFF / SCN / BIF via OpenSlide, pyramidal **OME-TIFF** (brightfield RGB or grayscale) via a built-in tifffile reader, and `.szi` pre-built DZI archives. Multi-file formats (MRXS, VMS/VMU) are not supported — convert them to a single-file format such as OME-TIFF first
- **On-demand Deep Zoom tiles** served from WSI (no pre-generated tile forests)
- **Step-by-step tours** (fixed field-of-view, annotated with pins and rectangle / circle / freehand shapes) with rich **Markdown** descriptions and uploaded images ([syntax](./docs/tour-markdown.md))
- **Live sync** sessions (presenter / follower fan-out over WebSockets): a staff member presents a slide and every follower's viewer mirrors the view; followers join by link / QR code or from a banner on the index page
- **Index page** with search, metadata filters, All / Slides / Tours views, and tile or list layout
- **Per-slide image credit** — rights holder, license (Creative Commons and others, linked to the license text) and source link, shown in the viewer and tour viewer whenever set
- **Image adjustment** (brightness / contrast / saturation) in the viewer, with optional **per-slide presets** a manager or admin can save so dark scans are corrected for every viewer from first paint
- **Admin CMS** with role-based access (`admin` / `manager` / `instructor`): upload WSI, manage metadata, build tours by clicking in the viewer, run synchronized live sessions. See [docs/architecture.md](./docs/architecture.md#authorization--roles) for the permission matrix. Each staff account can add an optional authenticator-app second factor to its sign-in ([details](./docs/operations.md#two-factor-sign-in-dashboard)).
- **Site customization** from the dashboard's **Settings** tab: site title, logo, favicon, footer text, the index page's header background (with up to five date-ranged seasonal ones) and defaults, and what the viewer's info panel shows
- **Optional sign-in for viewers** — off by default (a fully public viewer), or sign-in via accounts kept in Tessera (HTTP Basic), LDAP, or Google, chosen in the admin dashboard; Tessera can also sit behind Cloudflare Access ([details](./docs/operations.md#app-access-sign-in))
- **Three-level visibility** per slide and per tour — **draft**, **link-only** (published but hidden from the index, shareable by its unguessable URL), and **public** (labelled **Open** in the dashboard)
- **Built-in analytics**, entirely self-hosted (page views, slide dwell time, tour completion, top slides/tours/users, CSV export) with three privacy modes — **Off** (`off`, default), **Pseudonymous** (`hashed`, HMAC-pseudonymised user identity), **Identified** (`plain`). An optional **Google Analytics 4** tag can be enabled separately for the public viewer; it is off by default and is the only feature that sends visitor activity to a third party ([details](./docs/operations.md#google-analytics-optional-external))
- **Backup / restore / reset** of the database, WSI files, and assets from the admin dashboard ([details](./docs/operations.md#backup--restore--admin-dashboard))
- **Slide export / import** to migrate specific slides (with their tours, metadata, and WSI files) from one Tessera system to another — an additive import that adds them alongside the target's existing slides
- **Backup server sync** (`infra/sync-from.sh`): brings a second Tessera server up to date with the main one over ssh, transferring only what changed. The backup server keeps its own address, viewer sign-in and network settings, and statistics (dashboard staff accounts are taken from the main server), so students can be pointed at it while the main server is down. `--all` copies everything, for moving an install to a new machine ([details](./docs/operations.md#backup-server-and-server-moves--infrasync-fromsh))
- **Performance measurement** in the admin dashboard: a server benchmark (slide open, disk read, tile generation and throughput under parallel requests, with plain-language findings) and a browser-side network test, kept in a history you can compare before and after a tuning change
- **Audit history** of every administrative action (uploads, edits, settings, user management, logins) with category / user / date filters and **CSV export**, plus a filterable **error log** view (also CSV-exportable) so an admin can triage faults without shell access to journald
- **Bilingual UI** (English / Japanese) across the viewer and the admin dashboard, following the browser's language, with an in-app switcher remembered per browser

## Documentation

Technical reference (repository map, HTTP/WebSocket endpoints, deployment,
maintenance, release process, local development, feature notes) lives in:

- **[`docs/developer-guide.md`](./docs/developer-guide.md)** — implementation-level reference for this codebase.
- [`QUICKSTART_FOR_UBUNTU.md`](./QUICKSTART_FOR_UBUNTU.md) — full step-by-step production install.
- [`docs/architecture.md`](./docs/architecture.md) — architecture overview and supported-format table.
- [`docs/operations.md`](./docs/operations.md) — maintenance scripts and operations reference (updates, backup / restore, server sync, sign-in, caches, analytics).
- [`docs/tour-markdown.md`](./docs/tour-markdown.md) — Markdown reference for tour step descriptions.

## Built with

This project stands on the shoulders of many open-source projects. The core
imaging stack is built around **OpenSlide** (whole-slide image I/O) and
**OpenSeadragon** (Deep Zoom viewer); the rest is a fairly standard
FastAPI + React deployment.

Backend (Python):
- [FastAPI](https://fastapi.tiangolo.com/) — web framework
- [Uvicorn](https://uvicorn.dev/) — ASGI server
- [SQLAlchemy](https://www.sqlalchemy.org/) — ORM
- [OpenSlide](https://openslide.org/) and
  [openslide-python](https://github.com/openslide/openslide-python) — WSI I/O
- [tifffile](https://github.com/cgohlke/tifffile),
  [imagecodecs](https://github.com/cgohlke/imagecodecs) and
  [NumPy](https://numpy.org/) — OME-TIFF reading
- [Pillow](https://python-pillow.github.io/) — image processing
- [PyJWT](https://github.com/jpadilla/pyjwt) — JWT auth
- [cryptography](https://github.com/pyca/cryptography) — encryption of two-factor secrets (also PyJWT's crypto backend)
- [passlib](https://passlib.readthedocs.io/) — password hashing (PBKDF2-SHA256)
- [ldap3](https://github.com/cannatag/ldap3) — LDAP client
- [httpx](https://www.python-httpx.org/) — HTTP client
- [python-multipart](https://github.com/Kludex/python-multipart) — multipart parser
- [psutil](https://github.com/giampaolo/psutil) — system / cache stats for the admin Server Info page and the performance benchmark
- [zstandard](https://github.com/indygreg/python-zstandard) — optional zstd compression for backup archives (falls back to `.tar.gz` when missing)

Frontend (TypeScript/React):
- [React](https://react.dev/) and [React Router](https://reactrouter.com/) — UI / routing
- [OpenSeadragon](https://openseadragon.github.io/) — Deep Zoom viewer
- [szi-tile-source](https://github.com/sundogbio/szi-tile-source) — OpenSeadragon tile source for `.szi` archives
- [react-markdown](https://github.com/remarkjs/react-markdown),
  [remark-gfm](https://github.com/remarkjs/remark-gfm),
  [rehype-sanitize](https://github.com/rehypejs/rehype-sanitize) — Markdown rendering
- [qrcode](https://github.com/soldair/node-qrcode) — QR-code generation
- [Inter](https://rsms.me/inter/) and
  [Noto Sans JP](https://fonts.google.com/noto/specimen/Noto+Sans+JP)
  via [Fontsource](https://fontsource.org/) — web fonts (SIL OFL-1.1)
- [Vite](https://vite.dev/) and [TypeScript](https://www.typescriptlang.org/) — build tooling

Infrastructure:
- [nginx](https://nginx.org/) — reverse proxy and tile cache
- [SQLite](https://www.sqlite.org/) — embedded database
- [systemd](https://systemd.io/) — service management
- [Avahi](https://www.avahi.org/) — mDNS advertisement of the server's `.local` name
- [Node.js](https://nodejs.org/) — frontend build runtime

Full attribution and license texts for third-party components are listed in
[`THIRD_PARTY_LICENSES.md`](./THIRD_PARTY_LICENSES.md).

## Disclaimer

**Educational use only.** Tessera is intended for teaching and
self-learning of histology and pathology. It is **not** a medical
device, **has not been certified by
any regulatory authority**, and must not be used for clinical
diagnosis, treatment, or any other patient-care decision.

**No availability or data-integrity guarantees.** Tessera runs on a
single host with SQLite; data loss is possible, and recovery is the
operator's responsibility.

**Small-scale classroom use only.** Tessera is designed for small-to-medium
classroom deployments. It is not designed, tested, or intended for
large-scale or high-concurrency user access.

**Intranet deployment first.** Tessera is intended primarily for operation
within a closed intranet, not for direct exposure to the public internet.

Operators are responsible for:

- holding the **rights to show each uploaded WSI** and crediting it as its
  license requires — each slide's credit / license / source fields (shown in
  the viewer whenever set) exist for this, but filling them in correctly is the
  operator's job;
- ensuring uploaded WSI and their metadata contain **no patient-identifying
  information** (and complying with any applicable privacy / data-protection
  regulations) — including any consent or disclosure obligation that follows
  from enabling the optional Google Analytics tag, which transfers visitor
  data to a third party;
- the **security, maintenance, and configuration** of the host on which
  Tessera is installed — including OS updates, TLS, network exposure,
  account management, and backups.

The software is provided "as is", without warranty of any kind, as
stated in the [LICENSE](./LICENSE).

## License

© 2026 Yosuke Yamazaki

This project is licensed under the
[GNU Affero General Public License v3.0](./LICENSE).

## Acknowledgement

This work was conducted with the support of JSPS KAKENHI Grant Number JP25K15395.
