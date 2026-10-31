# Third-Party Licenses

Tessera itself is distributed under the **GNU Affero General Public
License version 3.0 (AGPL-3.0)** — see [`LICENSE`](./LICENSE) for the
full text.

This file enumerates the third-party open-source software that Tessera
bundles, links against, or depends on. Every component listed below has
been checked for AGPL-3.0 compatibility; see the "Compatibility with
AGPL-3.0" section at the end. The list is provided to satisfy
attribution requirements and to give credit to the upstream projects.
Each entry references the upstream project home page where the
authoritative copyright notice and full license text can be obtained.

If you redistribute binaries that include any of these components, make
sure the corresponding license text and copyright notice travel with the
distribution.

Test-only development dependencies (pytest, pytest-asyncio and
openslide-bin from `services/app/requirements-dev.txt`, and vitest, jsdom
and Testing Library in `frontend/package.json`) only run
the test suites; they are not part of the running service or the built
frontend, and are not listed.

This file is best-effort and is not a legal opinion. When in doubt,
consult the upstream project's `LICENSE` / `COPYING` file directly.

---

## Backend — Python (`services/app/requirements.txt`)

| Component           | License             | Project URL                                                    |
| ------------------- | ------------------- | -------------------------------------------------------------- |
| FastAPI             | MIT                 | https://github.com/fastapi/fastapi                             |
| Uvicorn             | BSD-3-Clause        | https://github.com/encode/uvicorn                              |
| uvicorn `[standard]` extras (transitive): websockets, python-dotenv (BSD-3); httptools, watchfiles, PyYAML (MIT); uvloop (MIT / Apache-2.0) | BSD-3 / MIT / Apache-2.0 | https://github.com/encode/uvicorn |
| SQLAlchemy          | MIT                 | https://github.com/sqlalchemy/sqlalchemy                       |
| python-multipart    | Apache-2.0          | https://github.com/Kludex/python-multipart                     |
| passlib             | BSD-3-Clause        | https://passlib.readthedocs.io/                                |
| PyJWT               | MIT                 | https://github.com/jpadilla/pyjwt                              |
| cryptography (via `pyjwt[crypto]`; also used directly for two-factor secrets) | Apache-2.0 / BSD-3 | https://github.com/pyca/cryptography            |
| ldap3               | LGPL-3.0            | https://github.com/cannatag/ldap3                              |
| httpx               | BSD-3-Clause        | https://github.com/encode/httpx                                |
| certifi (transitive, via `httpx`) | MPL-2.0 | https://github.com/certifi/python-certifi                    |
| openslide-python    | LGPL-2.1            | https://github.com/openslide/openslide-python                  |
| tifffile            | BSD-3-Clause        | https://github.com/cgohlke/tifffile                            |
| imagecodecs         | BSD-3-Clause (bundled codec libraries: see note below) | https://github.com/cgohlke/imagecodecs |
| NumPy               | BSD-3-Clause AND 0BSD AND MIT AND Zlib AND CC0-1.0 (bundled runtime libraries: see note below) | https://github.com/numpy/numpy       |
| Pillow              | MIT-CMU (HPND) (bundled image libraries: see note below) | https://github.com/python-pillow/Pillow |
| psutil              | BSD-3-Clause        | https://github.com/giampaolo/psutil                            |
| zstandard           | BSD-3-Clause        | https://github.com/indygreg/python-zstandard                   |
| Other transitive dependencies in `requirements.lock`: Starlette, httpcore, idna, click, pycparser (BSD-3-Clause); pyasn1 (BSD-2-Clause, via `ldap3`); Pydantic, pydantic-core, AnyIO, h11, annotated-types, annotated-doc, typing-inspection, cffi (MIT); typing-extensions (PSF-2.0); opentelemetry-api (Apache-2.0) | BSD / MIT / PSF / Apache-2.0 | (see each package's metadata) |

## Frontend — npm (`frontend/package.json`)

| Component                  | License            | Project URL                                                  |
| -------------------------- | ------------------ | ------------------------------------------------------------ |
| react, react-dom           | MIT                | https://github.com/facebook/react                            |
| react-router               | MIT                | https://github.com/remix-run/react-router                    |
| openseadragon              | BSD-3-Clause       | https://github.com/openseadragon/openseadragon               |
| szi-tile-source            | MIT                | https://github.com/sundogbio/szi-tile-source                 |
| react-markdown             | MIT                | https://github.com/remarkjs/react-markdown                   |
| remark-gfm                 | MIT                | https://github.com/remarkjs/remark-gfm                       |
| rehype-sanitize            | MIT                | https://github.com/rehypejs/rehype-sanitize                  |
| qrcode                     | MIT                | https://github.com/soldair/node-qrcode                       |
| @fontsource/inter          | OFL-1.1 (font files and CSS) | https://github.com/fontsource/fontsource          |
| @fontsource/noto-sans-jp   | OFL-1.1 (font files and CSS) | https://github.com/fontsource/fontsource          |
| Inter (font family)        | OFL-1.1            | https://rsms.me/inter/                                       |
| Noto Sans JP (font family) | OFL-1.1            | https://fonts.google.com/noto/specimen/Noto+Sans+JP          |
| vite                       | MIT                | https://github.com/vitejs/vite                               |
| @vitejs/plugin-react       | MIT                | https://github.com/vitejs/vite-plugin-react                  |
| typescript                 | Apache-2.0         | https://github.com/microsoft/TypeScript                      |
| @types/* (DefinitelyTyped) | MIT                | https://github.com/DefinitelyTyped/DefinitelyTyped           |

The table above lists the direct dependencies (and the build tools). All
146 production packages in `frontend/package-lock.json`, direct and
transitive, are under permissive licenses: 134 MIT, 8 ISC (e.g.
`@ungap/structured-clone`, which ends up in the built SPA), 2 OFL-1.1 (the
fonts) and 1 BSD-3-Clause (openseadragon); `szi-tile-source` declares no
`license` field but ships an MIT `LICENSE.txt`. `npm ls --omit=dev --all`
lists the tree; each package's `license` field is in the lockfile.

## Vendored assets (checked into this repository)

Unlike the entries above, these files are copied into this repository and
ship with it. They are not fetched from a package manager.

| Component                  | License            | Project URL                                                  |
| -------------------------- | ------------------ | ------------------------------------------------------------ |
| OpenSeadragon UI sprites (`frontend/public/osd-images/`) | Unlicense (public domain) | https://gitlab.com/tombrossman/openseadragon-svg-icons |

The 24 PNGs under `frontend/public/osd-images/` are the viewer's
navigation-control sprites for the six controls it shows (zoom in, zoom out,
home, full page, rotate left, rotate right; rest / hover / pressed /
grouphover for each). They are **not** the sprites bundled inside the
`openseadragon` npm package — see the note below for their provenance.

## System / OS packages (installed by `infra/install.sh`)

These components are not vendored by this repository; they are pulled in
from the operating system's package manager (Ubuntu / Debian) at install
time. They are listed here for transparency.

| Component             | License            | Project URL                                                  |
| --------------------- | ------------------ | ------------------------------------------------------------ |
| OpenSlide (libopenslide1 on Ubuntu 26.04, libopenslide0 on 24.04; openslide-tools) | LGPL-2.1 | https://openslide.org/ |
| libjpeg-turbo (libjpeg-dev)                | IJG / BSD-3 / zlib | https://libjpeg-turbo.org/                          |
| zlib (zlib1g-dev)                          | zlib               | https://zlib.net/                                   |
| SQLite                                     | Public Domain      | https://www.sqlite.org/copyright.html               |
| nginx                                      | BSD-2-Clause       | https://nginx.org/LICENSE                           |
| Node.js (from the NodeSource apt repository) | MIT (and others) | https://github.com/nodejs/node                      |
| Python 3 (python3, python3-venv)           | PSF License        | https://docs.python.org/3/license.html              |
| pip (python3-pip)                          | MIT (and others: vendored libraries) | https://github.com/pypa/pip                         |
| systemd                                    | LGPL-2.1+          | https://systemd.io/                                 |
| avahi-daemon                               | LGPL-2.1+          | https://www.avahi.org/                              |
| libnss-mdns                                | LGPL-2.1+          | https://github.com/lathiat/nss-mdns                 |
| Git, GNU Make, rsync, curl, GnuPG, ca-certificates, lsb-release | Various (GPL/MIT/Apache-2.0/MPL) | (see Debian package metadata) |

---

## Notes on key license obligations

- **OpenSlide** and **openslide-python** (LGPL-2.1) are linked
  dynamically. Source for OpenSlide is available from the upstream
  project; this repository does not modify it.
- **OpenSeadragon** (BSD-3-Clause) requires the copyright notice and
  disclaimer to be preserved in distributions that include the library.
  The upstream notice is available in the project's repository.
- **OpenSeadragon UI sprites** (`frontend/public/osd-images/`) are
  vendored rather than taken from the `openseadragon` package, because
  the viewer's visual identity is the flat icon set, not the beveled
  sprites the library ships with. Provenance chain:
  1. [Feather](https://github.com/feathericons/feather) (MIT) — the
     underlying glyph shapes.
  2. [openseadragon-svg-icons](https://gitlab.com/tombrossman/openseadragon-svg-icons)
     (**Unlicense**, i.e. released into the public domain) — Feather
     redrawn as an OpenSeadragon control set.
  3. [Benomrans/openseadragon-icons](https://github.com/Benomrans/openseadragon-icons)
     — a mechanical PNG rasterization of (2), pinned here at commit
     `6f0d2d03d12f5e4c1ae376f0d2917321981cafcf`. This repository carries
     no `LICENSE` file of its own; the files are treated as retaining the
     public-domain dedication of (2), from which they are a
     format conversion carrying no independent creative content. The
     Feather MIT attribution is preserved above regardless, which
     satisfies the most restrictive reading of the chain.
- **Inter** and **Noto Sans JP** are licensed under the SIL Open Font
  License 1.1. When redistributing the font files, the OFL text must be
  included with the distribution. Fontsource ships the OFL alongside the
  font payloads.
- **Pillow** (MIT-CMU) requires that the copyright notice and permission
  notice are retained in copies of the software. Its binary wheels bundle
  image libraries — among them libjpeg-turbo, libpng, libtiff, libwebp,
  OpenJPEG, Little CMS, libavif, HarfBuzz, Brotli and zstd — under
  permissive licenses, plus FreeType (dual-licensed: the FreeType License or
  GPL-2.0-or-later). Every bundled library's license text ships in the
  wheel's `LICENSE` file.
- **imagecodecs** (BSD-3-Clause, used by the OME-TIFF reader) ships binary
  wheels that bundle third-party codec libraries — among them
  libjpeg-turbo, OpenJPEG, libtiff, zlib-ng, zstd, LZ4, libwebp, libjxl,
  LERC, Brotli and CharLS — under permissive licenses (BSD, MIT,
  Apache-2.0, zlib, IJG, libpng), plus the GCC OpenMP runtime (`libgomp`,
  GPL-3.0 with the GCC Runtime Library Exception, which permits linking
  from code under any license). Every bundled library's license text is
  installed with the package under `site-packages/imagecodecs/licenses/`.
- **certifi** (MPL-2.0) ships Mozilla's CA certificate bundle as a data
  file, unmodified. MPL-2.0 obligations attach to the covered files
  themselves: keep the license notice, and make the source of any modified
  MPL file available. The package's source and license ship in its wheel.
- **NumPy** (BSD-3-Clause AND 0BSD AND MIT AND Zlib AND CC0-1.0) wheels bundle OpenBLAS (BSD-3-Clause), the GCC
  Fortran runtime (`libgfortran`, GPL-3.0 with the GCC Runtime Library
  Exception) and `libquadmath` (LGPL-2.1+); the texts ship in the wheel's
  `LICENSE.txt`.
- All other components listed under permissive licenses (MIT, BSD,
  Apache-2.0) carry similar requirements: preserve copyright notices and
  the license text in redistribution. These are satisfied by retaining
  each package's `LICENSE` file inside its installed location (Python
  site-packages, `node_modules/`, or system package metadata). The built
  frontend (`services/app/static/`) compiles the npm runtime packages and
  their dependencies into its JavaScript, so a redistribution of the build
  without `node_modules/` should carry their license texts alongside it.

For automated regeneration of this list, the following tools may be
helpful:

- Python: `pip-licenses --format=markdown`
- Node.js: `npx license-checker --production --summary`
- Debian packages: `dpkg-licenses`

---

## Compatibility with AGPL-3.0

Tessera is distributed under the GNU Affero General Public License,
version 3.0 (AGPL-3.0). Every third-party component listed in this file
has been checked against AGPL-3.0:

- **Permissive licenses** (MIT, BSD-2-Clause, BSD-3-Clause, 0BSD, ISC, zlib,
  Apache-2.0, CC0-1.0, MIT-CMU/HPND, PSF, OFL-1.1, IJG, Unlicense, Public Domain) are
  one-way compatible with AGPL-3.0: their code may be combined into an
  AGPL-3.0 work as long as the original copyright notice, license text,
  and disclaimers are preserved in the redistribution. (Apache-2.0 is
  compatible with GPLv3/AGPLv3 specifically; it is *not* compatible
  with GPLv2-only, but that does not apply here.)
- **MPL-2.0** (`certifi`) is compatible with AGPL-3.0: MPL-2.0 §3.3 lets
  a "Larger Work" combine MPL-covered files with code under a "Secondary
  License", which explicitly includes the GNU AGPL 3.0 or later, unless
  the files carry an "Incompatible With Secondary Licenses" notice
  (certifi's do not). The MPL-covered files themselves stay under MPL-2.0.
- **LGPL-3.0** (`ldap3`) is compatible with AGPL-3.0: per LGPL-3.0
  §3, an LGPL-3.0 work may be conveyed under GPL-3.0, which is in turn
  combinable with AGPL-3.0 §13.
- **LGPL-2.1 only** (`OpenSlide`, `openslide-python`) is compatible
  with AGPL-3.0 via two independent paths:
  1. LGPL-2.1 §3 permits relicensing a copy of the library to "any
     later version" of the ordinary GPL — which includes GPL-3.0, and
     GPL-3.0 is combinable with AGPL-3.0 §13.
  2. LGPL-2.1 §6 permits "works that use the Library" to be released
     under terms of the user's choice, provided the user can replace
     the library with a modified version. Tessera links against
     the OpenSlide library (`libopenslide1` on Ubuntu 26.04,
     `libopenslide0` on older releases) *dynamically* (system shared
     library installed by `apt`) and calls `openslide-python` via
     `ctypes`, which satisfies the relink requirement.
- **LGPL-2.1+** (`systemd`, `avahi-daemon`) runs as separate processes
  outside Tessera's address space; even setting that aside, the "or
  later" clause means GPL-3.0/AGPL-3.0 combination is unconditionally
  permitted. The same "or later" reasoning covers the in-process cases:
  `libquadmath`, which the NumPy wheel loads, and `libnss-mdns`, which
  glibc loads into any process that resolves a `.local` name.
- **FreeType** (bundled by the Pillow wheel) may be taken under either of
  its licenses: the FreeType License is GPLv3-compatible, and
  GPL-2.0-or-later can be used as GPL-3.0, which is combinable with
  AGPL-3.0 §13.
- **Command-line tools** (Git, GPL-2.0-only; GNU Make, rsync and GnuPG,
  GPL-3.0-or-later; curl, MIT-style), installed by `install.sh`, only ever
  run as separate programs (by the install / update / sync scripts, or by
  hand: no script runs `make`) and are never linked into Tessera, so
  their licenses place no conditions on Tessera's own code.
- **GPL-3.0 with the GCC Runtime Library Exception** (`libgomp`,
  `libgfortran`, bundled by the imagecodecs and NumPy wheels): the
  exception explicitly allows these runtime libraries to be combined with
  independent code under any license, AGPL-3.0 included.

### AGPL §13 (network use) obligations

Because Tessera is a network-facing service, AGPL-3.0 §13 requires that
operators of a public deployment **offer the corresponding source code
to all users interacting with it over the network**. The *About* dialog
in the frontend exposes the upstream repository URL
(`https://github.com/yosukey/tessera-wsi`) for that purpose; if you fork
Tessera and run a modified version, you must update that link to point
at the source corresponding to your running build.

### LGPL relink obligations (OpenSlide)

To preserve the LGPL-2.1 relink permission for OpenSlide:

- Keep the OpenSlide library (`libopenslide1` / `libopenslide0`) as a
  system-installed *shared* library; do not statically link it into the
  Python extension.
- Do not remove the upstream OpenSlide license text or copyright notice
  from the deployed system.
- If you fork OpenSlide, publish your modified source so downstream
  users can replace the library.

### Attribution checklist for redistribution

When shipping a binary or container image of Tessera, include:

- This file (`THIRD_PARTY_LICENSES.md`).
- The full LGPL-2.1 text alongside any OpenSlide / openslide-python
  artifacts (the upstream packages already ship this).
- The full LGPL-3.0 text alongside `ldap3`.
- The MPL-2.0 text alongside `certifi` (shipped in its package metadata).
- The OFL-1.1 text alongside the Inter and Noto Sans JP font files
  (Fontsource ships this in `node_modules/@fontsource/*/LICENSE`).
- Each permissively-licensed package's `LICENSE` / `COPYING` file as
  shipped in `site-packages/` or `node_modules/`.
