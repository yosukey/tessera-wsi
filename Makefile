# =====================================================================
# Tessera - Makefile (native Ubuntu workflow)
#
# Intended use is local development against a FastAPI backend running
# at 127.0.0.1:8000. See QUICKSTART_FOR_UBUNTU.md for production
# deployment (systemd + nginx + SQLite).
# =====================================================================

PYTHON ?= python3
VENV   ?= .venv
PIP    = $(VENV)/bin/pip
PY     = $(VENV)/bin/python
UVICORN = $(VENV)/bin/uvicorn

export PYTHONDONTWRITEBYTECODE = 1

.PHONY: help venv venv-dev install dev-install frontend-install frontend-build static lint test run-app run-frontend clean nginx-config lock lock-upgrade lock-uv-check uv-version outdated

# Optional LAN-direct redirect probe (off by default). Setting WSI_LAN_HOST to
# the hostname your LAN-only HTTPS route is served under wires BOTH halves of
# the feature from this single value:
#   - `make frontend-build` bakes it in as VITE_LAN_HOST so the SPA probes it
#   - `make nginx-config`    emits an nginx conf whose CSP connect-src allows it
# Leave it unset for the stock build and the feature stays disabled end-to-end.
WSI_LAN_HOST ?=

# Optional override (in ms) for the LAN-direct probe's first-ever wait window,
# which must outlast the browser's local-network permission prompt on the first
# load. Baked into the build as VITE_LAN_PROBE_TIMEOUT_MS. Unset -> the default
# in frontend/src/lib/lanProbe.ts. Only meaningful when WSI_LAN_HOST is set.
WSI_LAN_PROBE_TIMEOUT_MS ?=

# Optional canonical public origin (e.g. https://wsi.example.org). Baked into
# the build as VITE_PUBLIC_BASE_URL so the static share controls also offer
# this public URL when the page is open on a direct/local route. (Live-sync
# follower URLs and the OAuth redirect use the app's runtime value instead.)
# Declared here (rather than relied on from the ambient environment) so a typo
# is a visibly empty value instead of a silently different variable name;
# frontend-build validates the format before it is baked in.
WSI_PUBLIC_BASE_URL ?=

help:
	@echo "Targets:"
	@echo "  make venv             Create Python venv at $(VENV) and install backend deps (requirements.lock)"
	@echo "  make venv-dev         venv + dev deps (pytest etc.) from requirements-dev.lock"
	@echo "  make lock             Re-resolve services/app/*.lock after editing requirements*.txt (needs uv)"
	@echo "  make lock-upgrade     Move every locked pin to the newest allowed release (needs uv)"
	@echo "  make outdated         List direct deps with a newer release, incl. ones past a range cap"
	@echo "  make install          Alias for 'venv frontend-install'"
	@echo "  make dev-install      Alias for 'venv-dev frontend-install'"
	@echo "  make frontend-install npm ci in frontend/ (npm install if there is no lockfile)"
	@echo "  make frontend-build   Build frontend to services/app/static/"
	@echo "                        (set WSI_LAN_HOST=host to enable the LAN-direct probe;"
	@echo "                         WSI_LAN_PROBE_TIMEOUT_MS=ms tunes the first-probe wait;"
	@echo "                         WSI_PUBLIC_BASE_URL=https://host adds a public share URL)"
	@echo "  make nginx-config     Emit infra/nginx/wsi.conf.generated with the LAN host"
	@echo "                        added to the CSP connect-src (needs WSI_LAN_HOST)"
	@echo "  make lint             Compile Python bytecode + build frontend (sanity check)"
	@echo "  make test             Smoke test /api/health against a running dev backend"
	@echo "  make run-app          Run FastAPI backend with --reload at 127.0.0.1:8000"
	@echo "  make run-frontend     Run Vite dev server at 127.0.0.1:5173"
	@echo "  make clean            Remove .venv, frontend/node_modules, frontend/dist, services/app/static"

# The venv's own pip installs the locks as they are (no unpinned `pip install
# --upgrade pip` first): hash-checking mode needs nothing newer than the pip
# Ubuntu's python3-venv bundles, and every locked package ships a wheel.
venv:
	$(PYTHON) -m venv $(VENV)
	$(PIP) install --require-hashes -r services/app/requirements.lock

venv-dev:
	$(PYTHON) -m venv $(VENV)
	$(PIP) install --require-hashes -r services/app/requirements-dev.lock

# Hash-pinned lock files resolved from requirements.txt / requirements-dev.txt
# (the human-edited ranges). Universal: pins that differ by Python version carry
# environment markers, from LOCK_PYTHON_FLOOR up, so one file serves every
# supported host. The floor is the oldest Python install.sh accepts and CI
# tests (Ubuntu 24.04's 3.12): pins for anything older would be installed
# nowhere, tested by nothing and audited by nothing. `make lock` re-resolves only what the range files changed
# (existing pins are kept as preferences); `make lock-upgrade` moves every pin
# to the newest release the ranges allow. The dev lock is constrained to the
# runtime lock so tests always run against the deployed versions. Needs uv
# (https://docs.astral.sh/uv/), at UV_VERSION: releases differ in how they lay
# out the files' "# via" annotations, so CI's freshness check (and the
# lock-refresh workflow) install exactly this one — `pipx install
# uv==$(make -s uv-version)` locally. Bump it deliberately, with a `make lock`.
UV ?= uv
UV_VERSION := 0.12.23
LOCK_PYTHON_FLOOR ?= 3.12
LOCK_FLAGS = --universal --generate-hashes --python-version $(LOCK_PYTHON_FLOOR) --quiet

uv-version:
	@echo $(UV_VERSION)

lock-uv-check:
	@v=$$($(UV) --version 2>/dev/null | awk '{print $$2}'); \
	if [ "$$v" != "$(UV_VERSION)" ]; then \
		echo "WARNING: $(UV) is $${v:-missing}, the locks are generated with uv $(UV_VERSION);" \
		     "the annotations may come out differently and fail CI's lock check." >&2; \
	fi

lock: lock-uv-check
	cd services/app && $(UV) pip compile requirements.txt $(LOCK_FLAGS) -o requirements.lock
	cd services/app && $(UV) pip compile requirements-dev.txt $(LOCK_FLAGS) -c requirements.lock -o requirements-dev.lock

lock-upgrade: lock-uv-check
	cd services/app && $(UV) pip compile requirements.txt $(LOCK_FLAGS) --upgrade -o requirements.lock
	cd services/app && $(UV) pip compile requirements-dev.txt $(LOCK_FLAGS) --upgrade -c requirements.lock -o requirements-dev.lock

# Read-only report of what the locks are behind on: the backend's direct deps
# (services/app/tools/outdated.py marks a release past a requirements*.txt
# upper bound as "capped", which no lock refresh will ever pick up) and the
# frontend's (`npm outdated`: "Latest" beyond "Wanted" is a major past the
# package.json range; Dependabot opens those too). Needs the dev venv
# (`make venv-dev` / `make dev-install`): outdated.py parses the ranges with
# `packaging`, which only the dev lock installs.
outdated:
	@test -x $(PY) || { echo "No $(VENV); run 'make venv-dev' first."; exit 1; }
	@$(PY) -c 'import packaging' 2>/dev/null \
		|| { echo "$(VENV) has no 'packaging' (a dev dependency); run 'make venv-dev' first."; exit 1; }
	@echo "== Backend (services/app/requirements*.txt) =="
	@$(PY) services/app/tools/outdated.py
	@echo
	@echo "== Frontend (frontend/package.json) =="
	@cd frontend && npm outdated || true

install: venv frontend-install
dev-install: venv-dev frontend-install

frontend-install:
	cd frontend && if [ -f package-lock.json ]; then \
		npm ci --no-audit --no-fund; \
	else \
		echo "WARNING: frontend/package-lock.json missing; using non-deterministic 'npm install'."; \
		npm install --no-audit --no-fund; \
	fi

frontend-build: frontend-install
	@if [ -n "$(WSI_PUBLIC_BASE_URL)" ]; then \
		v='$(WSI_PUBLIC_BASE_URL)'; \
		o="$$(bash -c '. infra/lib/wsi-common.sh && wsi_url_origin "$$1"' _ "$$v")"; \
		[ -n "$$o" ] && [ "$$o" = "$${v%/}" ] \
			|| { echo "ERROR: WSI_PUBLIC_BASE_URL='$$v' must be an http(s) origin with no path, e.g. https://wsi.example.org"; exit 1; }; \
	fi
	cd frontend && VITE_LAN_HOST="$(WSI_LAN_HOST)" VITE_LAN_PROBE_TIMEOUT_MS="$(WSI_LAN_PROBE_TIMEOUT_MS)" VITE_PUBLIC_BASE_URL="$(WSI_PUBLIC_BASE_URL)" npm run build
	rm -rf services/app/static
	mkdir -p services/app/static
	cp -a frontend/dist/. services/app/static/

static: frontend-build

# Generate an nginx config whose CSP connect-src allows the LAN-direct host, so
# the SPA's reachability probe (fetch https://$WSI_LAN_HOST/favicon.ico) isn't
# blocked. Derived from the canonical infra/nginx/wsi.conf by a single targeted
# edit of the connect-src token — there is no separate template file to drift.
# Install the result the same way as the stock conf (see QUICKSTART), then
# `sudo nginx -t && sudo systemctl reload nginx`.
nginx-config:
	@if [ -z "$(WSI_LAN_HOST)" ]; then \
		echo "Usage: make nginx-config WSI_LAN_HOST=lan.wsi.example.org"; \
		echo "       (the hostname your LAN-direct HTTPS route is served under)"; \
		exit 1; \
	fi
	@echo "$(WSI_LAN_HOST)" | grep -qE '^[A-Za-z0-9.-]+$$' \
		|| { echo "ERROR: WSI_LAN_HOST='$(WSI_LAN_HOST)' is not a valid hostname (allowed: letters, digits, '.', '-')."; exit 1; }
	sed "s#connect-src [']self[']#connect-src 'self' https://$(WSI_LAN_HOST)#" \
		infra/nginx/wsi.conf > infra/nginx/wsi.conf.generated
	@grep -q "connect-src 'self' https://$(WSI_LAN_HOST)" infra/nginx/wsi.conf.generated \
		|| { echo "ERROR: connect-src token not found in infra/nginx/wsi.conf"; \
		     rm -f infra/nginx/wsi.conf.generated; exit 1; }
	@echo "Wrote infra/nginx/wsi.conf.generated (CSP connect-src now allows https://$(WSI_LAN_HOST))."
	@echo "Build the SPA from the SAME host so the probe targets it:"
	@echo "    make frontend-build WSI_LAN_HOST=$(WSI_LAN_HOST)"

lint:
	$(PY) -m compileall -q services/app/wsi_app
	cd frontend && npm run build

test:
	curl --fail --silent http://127.0.0.1:8000/api/health

run-app:
	cd services/app && ../../$(UVICORN) wsi_app.main:app --host 127.0.0.1 --port 8000 --reload

run-frontend:
	cd frontend && npm run dev

clean:
	rm -rf $(VENV) frontend/node_modules frontend/dist services/app/static
