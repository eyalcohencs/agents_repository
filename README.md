# OnTrack v2

OnTrack v2 is an off-road route planning, trail network, and track-sharing platform built as a FastAPI + PostgreSQL (PostGIS & pgRouting) modular monolith with an Angular SPA served behind unprivileged Nginx.

## Project Documentation

- [Product Requirements (PRD)](docs/PRD.md)
- [Functional & API Specification](docs/SPEC.md)
- [System Architecture](docs/ARCHITECTURE.md)
- [Execution Plan & Task Tracker](docs/TASKS.md)
- [QA Verification Report](docs/QA_REPORT.md)

## Shipped Foundation (Batch 1: `T-001` & `T-002`)

- **Monorepo & Runtime Stack:** Docker Compose with `postgres` (`pgrouting/pgrouting:16-3.5-3.7`), `api` (FastAPI + Alembic migrations on startup), `worker` (placeholder worker sharing `uploads_data`), and `nginx` (`nginxinc/nginx-unprivileged:1.27-alpine` serving the Angular SPA and proxying `/api/`).
- **Database Schema (`0001_poc_foundation`):** Enables `postgis` and `pgrouting` extensions and creates the initial POC tables (`nodes`, `edges`, `tracks`, `uploads`, `graph_metadata`) with spatial GiST indexes and integrity constraints.
- **Public APIs:**
  - `GET /api/health` — lightweight PostgreSQL ping (`200 {"status": "ok", "database": "ok"}`, or `503 SERVICE_UNAVAILABLE` if unreachable; exempt from Nginx rate limiting).
  - `GET /api/config/public` — non-secret client configuration (`support_email`, `network_layer_min_zoom`, `max_upload_mb`).
- **Frontend Skeleton & CI:** Angular 19 standalone app with Vitest unit tests, Playwright E2E profiles (Desktop Chrome, Android Chrome, iOS Safari), and GitHub Actions CI (`.github/workflows/ci.yml`).

## Prerequisites

- **Docker** and **Docker Compose** (v2)
- **Python 3.11** (backend requires `>=3.11,<3.12`)
- **Node.js 22** and **npm**

## Quickstart (Docker Compose)

1. Create your local environment file from the template:
   ```bash
   cp .env.example .env
   ```
   *(In production (`APP_ENV=production`), set a strong `DATABASE_URL` password, `COOKIE_SECURE=true`, and a valid non-`.local` `SUPPORT_EMAIL`.)*

2. Build and start all services (`postgres`, `api`, `worker`, `nginx`):
   ```bash
   docker compose up -d --build
   ```

3. Verify the running stack on `http://localhost:8080`:
   ```bash
   curl -i http://localhost:8080/
   curl -i http://localhost:8080/api/health
   curl -i http://localhost:8080/api/config/public
   ```
   Expected responses:
   - `GET /` -> `200 OK` serving the Angular SPA (`index.html`).
   - `GET /api/health` -> `200 OK` `{"status":"ok","database":"ok"}`.
   - `GET /api/config/public` -> `200 OK` `{"support_email":"support@ontrack.local","network_layer_min_zoom":12,"max_upload_mb":10}`.

4. Stop and clean up containers and volumes:
   ```bash
   docker compose down -v
   ```

## Backend Development & Testing

```bash
python3.11 -m venv backend/.venv
source backend/.venv/bin/activate
cd backend
pip install -e ".[dev]"
```

Run checks, migrations, and tests from `backend/`:

```bash
# Lint & strict typecheck
ruff check .
mypy app tests/poc/test_routing_poc.py tests/poc/test_snapping_poc.py

# Apply database migrations (requires a running PostgreSQL instance matching DATABASE_URL)
alembic upgrade head

# Run fast unit & non-integration tests (no Docker required)
pytest -m "not integration"

# Run Testcontainers PostGIS/pgRouting integration tests only (requires Docker)
pytest -m integration

# Run full suite (unit + integration, requires Docker)
pytest
```

### Batch 2 POC CLIs (`T-003` & `T-004`)

Run from `backend/` with the virtualenv activated:

#### 1. Node Snapping & Geometry Quality Benchmark (`T-003`)

Evaluates GPX/KML parsing, geodesic point reduction, and node snapping across threshold pairs (`reduction_m:snap_m`), either in-memory or against a PostGIS database (`DATABASE_URL` env var is preferred over `--database-url` to avoid exposing credentials in process lists):

```bash
# In-memory synthetic benchmark over built-in Israel fixtures
python -m app.domain.geo.cli \
  --input tests/fixtures/geo \
  --in-memory \
  --labels-json tests/fixtures/geo/labels.json \
  --pairs "5:5,10:10,20:20,20:10,30:20"

# Against a PostGIS database (resets graph tables between pairs; optional JSON report)
DATABASE_URL="postgresql+psycopg://ontrack:ontrack@localhost:5432/ontrack" \
  python -m app.domain.geo.cli \
  --input /path/to/tracks \
  --reset-db \
  --recursive \
  --output-json /tmp/snapping-report.json
```

#### 2. External Routing Provider Comparison (`T-004`)

Compares OpenRouteService (`ors`) and GraphHopper (`graphhopper`) — or an offline `fake` provider — over a JSON file of `{name, origin: {lat, lng}, destination: {lat, lng}, profiles?}` pairs, reporting latency percentiles, failure/rate-limit counts, and external snap gaps:

```bash
# Live ORS vs. GraphHopper comparison (API keys read from environment)
ORS_API_KEY="<your-ors-key>" GRAPHHOPPER_API_KEY="<your-gh-key>" \
  python -m app.integrations.routing.cli \
  --input /path/to/pairs.json \
  --providers ors,graphhopper \
  --profiles driving-car,cycling-mountain \
  --output-json /tmp/routing-comparison.json

# Offline smoke run using FakeRoutingProvider
python -m app.integrations.routing.cli \
  --input /path/to/pairs.json \
  --providers fake
```

## Frontend Development & Testing

Run from `frontend/`:

```bash
cd frontend
npm ci

# Lint, typecheck, unit tests, and production build
npm run lint
npm run typecheck
npm test
npm run build

# End-to-end tests (Playwright Desktop Chrome, Android Chrome, iOS Safari)
npx playwright install --with-deps chromium webkit
npm run e2e
```
