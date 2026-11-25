# OnTrack v2.4 — QA Report

## Batch 1: Foundation (`T-001` & `T-002`)

**QA Engineer:** `qa-engineer`  
**Scope (Phase 1 — Backend & Infra):** `T-001` (Repo Bootstrap, Docker Compose, Nginx, CI, Backend Packaging) & `T-002` (PostGIS/pgRouting Extensions, Alembic `0001_poc_foundation`, `GET /api/health`, `GET /api/config/public`)  
**Scope (Phase 2 — Frontend & Full Stack):** `frontend/` contract/lint/typecheck/Vitest/build + `docker compose up -d --build` (4-service boot verified; external `localhost:8080` probe & `docker compose down -v` blocked on 60s user approval timeout).

### Summary

| Check | Command | Result | Details |
|---|---|---|---|
| Backend Lint | `backend/.venv/bin/ruff check .` | **PASS** | `All checks passed!` (post-fix; covers `app/`, `alembic/`, and `tests/`) |
| Backend Typecheck | `backend/.venv/bin/mypy app` | **PASS** | `Success: no issues found in 11 source files` (post-fix) |
| Alembic CLI Check | `python -W error::DeprecationWarning -m alembic heads` | **PASS** | `0001_poc_foundation (head)` with **zero** Alembic `DeprecationWarning` |
| Backend Unit Tests | `backend/.venv/bin/pytest tests/unit -v` | **PASS** | `62 passed in 1.09s` (post-fix security & review regression suite) |
| Backend Integration Tests (Testcontainers) | `backend/.venv/bin/pytest -v` | **PASS** | `4/4 integration passed` against `pgrouting/pgrouting:16-3.5-3.7` (`0001_poc_foundation.py` & `db.py` unchanged since `#11`) |
| Frontend Lint | `npm run lint` (`eslint .`) | **PASS** | 0 errors (post-fix) |
| Frontend Typecheck | `npm run typecheck` (`tsc --noEmit`) | **PASS** | 0 errors (post-fix) |
| Frontend Unit Tests | `npm test` (`vitest run`) | **PASS** | `4 passed (4)` in `src/app/app.component.spec.ts` (post-fix) |
| Frontend Production Build | `CI=true NG_CLI_ANALYTICS=false npm run build` | **PASS** | Produced `dist/ontrack/browser/index.html` (Initial total `226.00 kB`, est. transfer `63.49 kB`, post-fix) |
| Docker Compose Build & Boot | `docker compose up -d --build && docker compose ps` | **PASS** | All 4 services built & booted cleanly: `postgres` (`healthy`), `api` (`healthy`, ran `alembic upgrade head` + passed `/api/health` check), `worker` (`Up`), `nginx` (`Up`, `:8080->80`) |
| Live Nginx HTTP Probe & Teardown | `localhost:8080` HTTP check + `docker compose down -v` | **BLOCKED (Approval Timeout)** | Unsandboxed command timed out waiting 60s for user approval; not retried per permission policy. Containers remain running on `:8080` until `docker compose down -v` is run. |

---

### Test Suites Created & Verified

#### 1. Unit Tests (`backend/tests/unit/`)

- **`test_config.py`** (25 cases — PASS):
  - `Settings` defaults (`app_env`, `database_url`, pool/timeout params, `upload_storage_dir`, `support_email`, `network_layer_min_zoom`, `max_upload_mb`, `cookie_secure`, `log_level`).
  - Environment variable overrides and `extra="ignore"` behavior.
  - Range bounds for `network_layer_min_zoom` (`1..22`) and `max_upload_mb` (`1..100`), plus `app_env` literal validation.
  - Production guardrails: rejects `cookie_secure=False`, empty/whitespace/malformed/`*.local` `support_email` in `production`; accepts valid production config.
  - `get_settings()` `@lru_cache` caching and `cache_clear()` reset behavior.
- **`test_errors.py`** (6 cases — PASS):
  - `AppError` JSON envelope `{code, message, details?}` omitting `details` when `None` and including it when present.
  - `RequestValidationError` mapped to HTTP `422` with `code="VALIDATION_ERROR"` and sanitized `{loc, msg, type}` error list.
  - `StarletteHTTPException` (`403`, `404`, `405`) mapped to canonical error codes; `500`/`503` strip internal details.
  - Unhandled `Exception` mapped to HTTP `500` `INTERNAL_ERROR` with zero traceback or credential/secret leakage.
- **`test_health_unit.py`** (5 cases — PASS):
  - `GET /api/health` returns `200 {"status": "ok", "database": "ok"}` when `ping_database` succeeds, and `503 {"code": "SERVICE_UNAVAILABLE", "message": "Service temporarily unavailable.", "details": {"database": "unreachable"}}` when `ping_database` returns `False`.
  - `GET /api/config/public` returns exact non-secret keys (`support_email`, `network_layer_min_zoom`, `max_upload_mb`) and never exposes `database_url`, credentials, `cookie_secure`, or internal paths.
  - `ping_database` executes `SET LOCAL statement_timeout = 2000` followed by `SELECT 1`, and catches `SQLAlchemyError` returning `False`.
- **`test_infra_config.py`** (4 cases — PASS):
  - `nginx/nginx.conf`: validates `try_files $uri $uri/ /index.html;` (SPA history fallback for `AC-50`), `/api/` proxy to `api:8000`, `client_max_body_size 12m;`, per-IP `limit_req_zone` (`api_general`, `api_auth`, `api_heavy`) + JSON `429`, security headers, and `/api/health` explicitly exempt from `limit_req`.
  - `docker-compose.yml`: validates `postgres` (`pgrouting/pgrouting:16-3.5-3.7`, `pg_isready`, `pg_data`), `api` (`alembic upgrade head` + `uvicorn`, `/api/health` check, `uploads_data:/data/uploads`), `worker` (`python -m app.workers.placeholder`, shared `uploads_data:/data/uploads`), and `nginx` (`${NGINX_PORT:-8080}:80`, depends on `api` healthy).
  - `.env.example`: verifies all 16 documented keys and `postgresql+psycopg://` driver prefix.
  - `.github/workflows/ci.yml`: verifies triggers, `permissions: contents: read`, and jobs `backend-lint-typecheck`, `backend-tests`, `frontend-checks`, `frontend-e2e`.

#### 2. Integration Tests (`backend/tests/integration/test_foundation.py`)

Executed via `testcontainers[postgres]` against `pgrouting/pgrouting:16-3.5-3.7` (native `linux/amd64`):

- **`test_alembic_0001_upgrade_schema_constraints_and_indexes`** — PASS:
  - Runs `alembic upgrade head`.
  - Confirms `postgis` and `pgrouting` extensions are installed and callable (`PostGIS_Version()`, `pgr_version()`).
  - Confirms `graph_metadata` seeded with `(1, 0)` and enforces singleton `CHECK (id = 1)`.
  - Confirms `nodes` (`geom`, nullable `ele`) and GiST indexes `ix_nodes_geom_geog` (geography) and `ix_nodes_geom`, plus live metric `ST_DWithin(geom::geography, ...)` query.
  - Confirms `edges` valid insert, indexes (`ix_edges_geom`, `uq_edges_unordered_endpoints`, `ix_edges_source`, `ix_edges_target`), and `IntegrityError` rejections for: self-loop (`source = target`), unordered duplicate (`(target, source)`), `length_m <= 0`, and `initial_difficulty` outside `1..3`.
  - Confirms `tracks` `MultiLineString` insert and `created_at` default.
  - Confirms `uploads` valid insert with `track_id` FK, `ix_uploads_status_lease` index, and `IntegrityError` rejections for invalid `status` and `overall_difficulty` outside `1..3`.
- **`test_alembic_0001_downgrade_base_and_clean_reupgrade`** — PASS:
  - Runs `alembic downgrade base`, confirms all 5 foundation tables and both `pgrouting` & `postgis` extensions are removed, then re-runs `alembic upgrade head` and confirms clean restoration of extensions, tables, and `graph_metadata (1, 0)`.
- **`test_live_fastapi_health_and_public_config_against_testcontainers`** — PASS:
  - Live `TestClient(create_app(...))` against migrated Testcontainers DB: `GET /api/health` -> `200 {"status": "ok", "database": "ok"}`, `GET /api/config/public` -> `200` with configured public fields and no DB URL leakage.
- **`test_live_fastapi_health_returns_503_against_unreachable_database`** — PASS:
  - Live `TestClient` against unreachable port returns `503 SERVICE_UNAVAILABLE` with `{"database": "unreachable"}`.

---

### Phase 2 Verification Details (Frontend & Docker Compose)

#### 1. Frontend Contract & Unit Tests (`frontend/`)
- **Contract checks — PASS:**
  - `frontend/package-lock.json` committed (`616,646` bytes).
  - `frontend/angular.json` uses `"outputPath": "dist/ontrack"` (emits `dist/ontrack/browser/index.html`).
  - `frontend/playwright.config.ts` defines all 3 required device profiles: `desktop-chrome` (`1280x800`), `mobile-android-chrome` (`Pixel 7` touch, `360x740`), and `mobile-ios-safari` (`iPhone 13` WebKit touch).
  - Verified none of the `T-013` directories (`app/core/`, `app/shared/map/`, `app/shared/ui/`, `assets/i18n/`) exist.
- **Expanded Unit Tests (`frontend/src/app/app.component.spec.ts`) — PASS (`4/4`):**
  - Creates root `AppComponent` shell.
  - Redirects `/` to `/login` and renders `OnTrack` heading + `/privacy-and-terms-of-use` link.
  - Renders `/privacy-and-terms-of-use` heading + `/login` back link.
  - Renders `Page not found!` on unknown wildcard route (`**`) + `/` return link.
- **Commands executed:**
  - `npm run lint` -> PASS (exit 0)
  - `npm run typecheck` -> PASS (exit 0)
  - `npm test` -> PASS (`Test Files 1 passed (1)`, `Tests 4 passed (4)`)
  - `CI=true NG_CLI_ANALYTICS=false npm run build` -> PASS (`dist/ontrack/browser/index.html` verified)

#### 2. Docker Compose Stack (`docker-compose.yml`)
- **`docker compose up -d --build && docker compose ps` — PASS (exit 0):**
  - `ontrackv2-postgres-1` (`pgrouting/pgrouting:16-3.5-3.7`): `Up (healthy)`
  - `ontrackv2-api-1`: `Up (healthy)` — executed `alembic upgrade head && exec uvicorn app.main:app` and passed built-in `/api/health` container healthcheck.
  - `ontrackv2-worker-1` (`python -m app.workers.placeholder`): `Up` (no crash loop).
  - `ontrackv2-nginx-1` (`nginx:1.27-alpine` with built Angular bundle): `Up`, listening on `0.0.0.0:8080->80/tcp`.
- **Live Nginx HTTP Probe (`http://localhost:8080`) & `docker compose down -v` — BLOCKED (Approval Timeout):**
  - Unsandboxed command timed out waiting 60s for user approval and was not retried per permission policy.
  - Exact manual/retry verification commands once approved:
    1. `curl -i http://localhost:8080/` (`200` Angular `index.html`)
    2. `curl -i http://localhost:8080/privacy-and-terms-of-use` (`200` Angular `index.html`, `AC-50`)
    3. `curl -i http://localhost:8080/api/health` (`200 {"status":"ok","database":"ok"}`, `AC-48`)
    4. `curl -i http://localhost:8080/api/config/public` (`200` non-secret public config JSON)
    5. `docker compose down -v`

---

### Acceptance Criteria Coverage (Batch 1)

| AC | Scope Verified | Status |
|---|---|---|
| `AC-48` | Backend `GET /api/health` (`200` healthy / `503` DB unreachable), Nginx `/api/health` rate-limit exemption, `GET /api/config/public`, and Docker Compose `api` container `/api/health` healthcheck | Backend & Container **PASS** (external Nginx HTTP probe pending user approval; frontend banner pending `T-013`+) |
| `AC-50` | Nginx `try_files $uri $uri/ /index.html;` verified in `nginx/nginx.conf`, Angular `/privacy-and-terms-of-use` route verified in Vitest, Playwright E2E smoke test authored | Config & Unit **PASS** (live Nginx reload check pending user approval) |
| `AC-52` | Playwright Desktop Chrome (`1280x800`), emulated Android Chrome (`360x740` touch), and emulated iOS Safari (`iPhone 13` WebKit touch) configured in `frontend/playwright.config.ts` and `.github/workflows/ci.yml` | Config & CI **PASS** |

### Post-Fix Security & Review Regression Verification (`#25`, `#26`, `#28`)

Added 22 new unit test assertions across `backend/tests/unit/` (bringing unit suite from 40 to **62 passed**):
- **`test_config.py`:** Verifies `app_env="production"` rejects default dev `DATABASE_URL`, invalid SQLAlchemy URLs, missing/empty passwords, known dev passwords (`ontrack`, `ONTRACK`, `postgres`, `password`, `changeme`), and malformed `support_email` values (`@`, `a@b`, `@ontrack.example`, `support@`, leading/trailing dot domains, embedded whitespace), and normalizes/strips whitespace on valid `support_email`.
- **`test_errors.py`:** Verifies HTTP `405 Method Not Allowed` preserves the `Allow` response header (`POST`).
- **`test_health_unit.py`:** Verifies `create_app` disables `/docs`, `/redoc`, and `/openapi.json` (`404 NOT_FOUND`) when `app_env == "production"`, while keeping them enabled in non-production environments.
- **`test_infra_config.py`:** Verifies unprivileged Nginx (`nginxinc/nginx-unprivileged:1.27-alpine`, `USER nginx`, `EXPOSE 8080`, `listen 8080;`, `pid /tmp/nginx.pid;`, Compose port `${NGINX_PORT:-8080}:8080`), edge `X-Forwarded-For $remote_addr` (no `$proxy_add_x_forwarded_for`), trailing-slash + `network/edges/[^/]+/rating` rate-limit regexes, root `.dockerignore` & `backend/.dockerignore`, CI `persist-credentials: false` on all 4 `actions/checkout@v4` steps, and removal of `overflow-x: clip` from `frontend/src/styles.scss`.
- **Alembic CLI:** `python -W error::DeprecationWarning -m alembic heads` exited 0 (`0001_poc_foundation (head)`) with zero deprecation warnings.

### Remaining Non-Blocking Notes

- `testcontainers.postgres` logs a deprecation warning recommending `testcontainers.community.postgres` in newer 4.x releases.
- Zero blocking bugs found; escalation strikes used: `0/2`.

---

## Batch 2: Core Geospatial & Routing POCs (`T-003` & `T-004`)

**QA Engineer:** `qa-engineer`  
**Final Verified Commit:** `11f1c1a` (`11f1c1a0ae6336f3a739b87edb2910bc2c6c8ac7`)  
**Sign-Offs:**
- **Security Audit (`security-auditor`, `#26`):** **SECURITY_APPROVED**
- **Code Review (`code-reviewer`, `#27`, `#33`):** **APPROVED**

### Summary

| Check | Command | Result | Details |
|---|---|---|---|
| Backend Lint (`T-003` + `T-004`) | `PYTHONPATH=. backend/.venv/bin/ruff check app tests/poc/test_routing_poc.py tests/poc/test_snapping_poc.py` | **PASS** | `All checks passed!` |
| Backend Typecheck (`strict=true`) | `PYTHONPATH=. backend/.venv/bin/mypy app tests/poc/test_routing_poc.py tests/poc/test_snapping_poc.py` | **PASS** | `Success: no issues found in 29 source files` |
| Full Unit & Regression Suite | `PYTHONPATH=. backend/.venv/bin/pytest -v -m "not integration"` | **PASS** | **`117 passed, 16 deselected in 18.28s`** (`17` T-004 unit + `38` T-003 unit + `62` Batch 1 regression) |
| PostGIS/pgRouting Testcontainers Suite (`T-003` + `T-004`) | `PYTHONPATH=. backend/.venv/bin/pytest -v -m integration tests/poc/test_routing_poc.py tests/poc/test_snapping_poc.py` | **PASS** | **`12 passed, 55 deselected in 35.70s`** against live `pgrouting/pgrouting:16-3.5-3.7` (all `7` T-004 + all `5` T-003 integration tests passed) |
| T-003 Synthetic Benchmark CLI | `PYTHONPATH=. backend/.venv/bin/python -m app.domain.geo.cli --input tests/fixtures/geo --in-memory --labels-json tests/fixtures/geo/labels.json --pairs "5:5,10:10,20:20,20:10,30:20"` | **PASS** | All 15 Israel fixtures (incl. 5,000-pt stress track, 5,409 raw pts) across all 5 threshold pairs; p95 latency `9.62–34.66 ms` (`<< 30,000 ms` target) |
| Security Audit & Code Review | `security-auditor` (`#26`) & `code-reviewer` (`#27`, `#33`) | **PASS** | **SECURITY_APPROVED** & **APPROVED** after remediating all security findings (`#16`) and both `dev-routing` strikes (`#27`, `#29`) |

---

### Batch 2 — T-003 Verification (POC 1: Node Snapping, Point Reduction & Geometry Quality)

#### 1. Independent Audit, Security Hardening & QA Tests (`backend/tests/poc/test_snapping_poc.py`)
- Verified zero file overlap with `T-004` (`backend/app/domain/geo/`, `backend/tests/poc/test_snapping_poc.py`, `backend/tests/fixtures/geo/`, no `backend/tests/poc/__init__.py`) and zero edits to shared files (`pyproject.toml`, `0001_poc_foundation.py`, `conftest.py`).
- Audited `snapping.py` lock order (`pg_advisory_xact_lock(0x4F4E545241434B)` → `nodes` (`ST_DWithin(geom::geography, ...)` + `ORDER BY ST_Distance(...), id ASC`, seeing same-tx inserts) → `edges` (`ST_MakeLine` + `ST_Length(geom::geography)`, unordered reuse via `ON CONFLICT (LEAST(source,target), GREATEST(source,target)) DO NOTHING` preserving `geom`/`length_m`/`initial_difficulty`, no self-loops, no cross-segment bridging) → `tracks` (`ON CONFLICT (id) DO NOTHING`) → `graph_metadata(revision)` strictly last), savepoint rollback on `DEGENERATE_GEOMETRY`, and `defusedxml` XXE/billion-laughs protection.
- Hardened file ingestion and CLI error handling (`#26`): `parse_track_path()` rejects symlinks, enforces `is_relative_to(root_dir)`, validates file extensions, and checks `stat().st_size <= max_upload_mb` prior to `read_bytes()` (Medium #4); `app/domain/geo/cli.py` prefers the `DATABASE_URL` env var over CLI args and redacts embedded DSN credentials via `sanitize_error_message()` on `stderr` (Low #5).
- **Verified suite totals: `38` unit tests PASS + `5` live PostGIS integration tests PASS**, including QA additions:
  - `test_qa_ac64_exact_radius_boundary_and_nearest_beats_lower_id`: verifies exact radius boundary (`dist == radius` snaps, `dist == radius + 0.5m` does not) and that a closer higher-`id` node (5 m) beats a farther lower-`id` node (15 m) inside radius.
  - `test_qa_ac94_fingerprint_order_and_6dp_precision_boundaries`: verifies reversed point order changes `normalized_fingerprint` and 6-decimal rounding boundaries (`32.6000001` vs `32.6000004` equal; `32.6010010` distinct).
  - `test_qa_ac53_gpx_only_mode_kml_limits_and_mixed_degenerate_segment`: verifies `allowed_formats=frozenset({"gpx"})` rejects KML (`UNSUPPORTED_FORMAT`), KML point limit raises `TOO_MANY_POINTS`, and GPX with one 1-point degenerate `<trkseg>` alongside a valid `<trkseg>` retains the valid segment.
  - `test_integration_qa_ac64_nearest_vs_id_boundary_kml_and_duplicate_track` (`@pytest.mark.integration`): verified live against PostGIS — nearest-beats-lower-id, 19.5 m inside vs 21.0 m outside 20 m radius, KML Israel lat/lon bounds in PostGIS, and duplicate track `ON CONFLICT DO NOTHING` + 100% edge reuse.

#### 2. Synthetic Snapping Benchmark Metrics (`AC-65`, `AC-96`, 15 Israel Fixtures, 5,409 Raw Points)

Executed via `python -m app.domain.geo.cli --input tests/fixtures/geo --in-memory --labels-json tests/fixtures/geo/labels.json --pairs "5:5,10:10,20:20,20:10,30:20"` (and verified end-to-end against live PostGIS in `test_snapping_poc.py`):

| Pair (`red/snap` m) | Raw Pts | Reduced Pts | Final Nodes | Final Edges | Max Spacing (m) | P95 Spacing (m) | Max Dev (m) | P95 Dev (m) | False Joins | Missed Joins | Redundant Edges | Route Quality | P95 Latency (ms) |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| `5/5` | 5,409 | 5,277 | 5,253 | 5,247 | 100.1 | 8.1 | 5.0 | 0.0 | 1 | 0 | 0 | 1.000 | 34.66 |
| `10/10` | 5,409 | 2,732 | 2,705 | 2,699 | 100.1 | 16.1 | 8.0 | 0.1 | 1 | 0 | 0 | 0.999 | 13.51 |
| `20/20` | 5,409 | 1,850 | 1,806 | 1,799 | 100.1 | 25.1 | 18.7 | 0.1 | 14 | 0 | 0 | 1.000 | 10.37 |
| `20/10` | 5,409 | 1,850 | 1,825 | 1,817 | 100.1 | 25.1 | 14.2 | 0.1 | 1 | 0 | 0 | 0.999 | 12.56 |
| `30/20` | 5,409 | 1,393 | 1,362 | 1,353 | 100.1 | 39.9 | 18.8 | 0.2 | 8 | 0 | 0 | 0.998 | 9.62 |

*Key Trade-off Finding (`AC-65` / `AC-96`):* At `20/20 m`, parallel trails spaced 15 m apart merge (`FJ=14`), whereas decoupling reduction and snap radius (`20/10 m`) cuts node/edge count by ~66% while reducing false joins back down to `FJ=1` (overpass/underpass crossing only, which is expected without 3D/z-level snapping).

#### 3. Acceptance Criteria Coverage (`T-003`)

| AC | Scope Verified | Status |
|---|---|---|
| `AC-44` | GPX + KML (`LineString` & `gx:Track`) Israel axis order (`lat ~32.6, lon ~35.0`), endpoint preservation across `5/10/20/30 m`, `.twl` and corrupt files skipped in batch scan without aborting | **PASS** (Unit + Live PostGIS) |
| `AC-53` | Empty, malformed XML, XXE, billion-laughs, `.txt`, `.twl`, 1-pt, identical 2-pt, out-of-range lat/lon, pre-read symlink/size & point limits, `allowed_formats={'gpx'}`, stateless recovery | **PASS** (Unit) |
| `AC-54` | 2 km multi-segment `<trkseg>` gap & multi-`<trk>` separation preserved with 0 bridging edges | **PASS** (Unit + Live PostGIS) |
| `AC-64` | Inside/on/outside radius, nearest beats lower-id, equidistant `id ASC` tie-break, same-tx visibility, reverse edge reuse preserving `initial_difficulty`, self-loop skip, atomic rollback on degenerate geometry | **PASS** (Unit + Live PostGIS) |
| `AC-65` | `(5,5), (10,10), (20,20), (20,10), (30,20)` m across 15 labeled fixtures (sparse, dense, switchbacks, parallel, overpass, reverse, no-ele, multi-segment, 5,000-pt); metrics & trade-offs recorded | **PASS** (Synthetic In-Memory + Live PostGIS) |
| `AC-94` | Raw `file_sha256` vs canonical 6-decimal `normalized_fingerprint` (`-0.0 -> 0.0`, ignores metadata/ele/namespaces/whitespace, sensitive to order, 6dp boundary, and segment splits) | **PASS** (Unit) |
| `AC-96` | Full synthetic benchmark table + reusable `python -m app.domain.geo.cli` ready for `T-028` real legacy archive | **PASS** (Synthetic Phase 1 + Live PostGIS; real archive in `T-028`) |

---

### Batch 2 — T-004 Verification (POC 2: Exact Full-Graph pgRouting & RoutingProvider Benchmark)

#### 1. Independent Audit, Security Hardening & QA Tests (`backend/tests/poc/test_routing_poc.py`)
- Verified zero file overlap with `T-003` (`backend/app/integrations/routing/`, `backend/tests/poc/test_routing_poc.py`, no `backend/tests/poc/__init__.py`) and zero edits to shared files.
- Audited `graph_router.py`:
  - `NEAREST_NODE_SQL` uses explicit `CAST(... AS double precision)` on `:lng`, `:lat`, `:radius_m` with `ST_DWithin(n.geom::geography, ...)` and KNN `ORDER BY n.geom::geography <-> ... ASC, n.id ASC`. Live Testcontainers `EXPLAIN` verified `ix_nodes_geom_geog` index eligibility across `1x`/`5x`/`10x` scales plus unconstrained natural index selection on a spatially realistic `10x` grid (`40x40`, `step_deg=0.005`).
  - `build_shortest_path_sql` generates exact full-graph `pgr_dijkstra` / `pgr_bdDijkstra` with `directed := false`, `length_m::double precision` (`distance`) or `1.0::double precision` (`hops`), zero bounding-box predicates (`AC-78`), zero difficulty in inner cost/`WHERE` (`AC-30`), and `ST_Reverse(e.geom)` when `p.node = e.target`.
  - `calculate_leg` runs all DB reads in a single short `REPEATABLE READ READ ONLY` transaction and releases the DB connection **before** any external HTTP call (`AC-62`), handles all 5 hybrid branches (`connected`, `entry_equals_exit`, `disconnected`, `no_entry_fallback`, `entry_no_exit_fallback`), verifies section continuity, and returns zero partial route on provider or continuity error (`AC-29`).
- Audited `providers.py`, `models.py` & `cli.py`:
  - Bounded timeouts, exponential-backoff retries ONLY on transient `5xx` / `TimeoutException` / `TransportError`, non-retryable `429`/`401`/`403`/`400`, zero API key exposure in `__repr__`, logs, exception messages, or `__context__`.
  - Security remediations verified (`#26`): `_HttpxQuerySecretRedactingFilter` on `httpx`/`httpcore` loggers redacts query secrets (`key=`, `api_key=`, `token=`) at `INFO` and all log levels, and `_send_with_retries()` wraps `(httpx.HTTPError, httpx.InvalidURL)` into sanitized `RoutingProviderError` with `__context__ = None` / `from None` (High #1); strict `validate_routing_profile()` (`^[a-z0-9][a-z0-9_-]{0,63}$`) enforced across providers, config, and CLI (High #2); `validate_provider_base_url()` enforces `https` (localhost opt-in via `allow_insecure_http=True`), rejects embedded credentials/query/fragment/empty host, and explicitly sets `follow_redirects=False` (Medium #3).
  - GraphHopper profile mapping (`driving-car -> car`, `cycling-mountain -> mtb`) and CLI external snap-gap telemetry verified.
- **Verified suite totals: `17` unit tests PASS + `7` live PostGIS/pgRouting integration tests PASS**, including QA & cross-POC additions:
  - `test_qa_ors_and_graphhopper_error_classification_retry_exhaustion_and_redaction` & `test_security_httpx_info_logging_and_decoding_error_redaction`: verify error classification, retry exhaustion + backoff, `INFO`-level query-secret redaction, and `DecodingError`/`TooManyRedirects` wrapping with `exc.__context__ is None`.
  - `test_qa_calculate_leg_continuity_and_mid_faults_across_branches`: verifies `RouteContinuityError` rejection and `AC-29` mid-calculation provider failures with zero partial route.
  - `test_integration_qa_ac25_exit_far_hops_vs_distance_and_ac30_all_modes` (`@pytest.mark.integration`): verified live — asymmetric `CLOSE` entry + `FAR` exit (`AC-25`), `distance` vs `hops` divergence, and `AC-30` difficulty neutrality across all 4 `(algorithm, cost_mode)` combinations.
  - `test_integration_qa_cross_poc_snapping_to_routing_end_to_end` (`@pytest.mark.integration`): verified live — ingests forward + reverse Israel GPX tracks via `T-003` (`ingest_track_transaction`) and immediately routes in reverse via `T-004` (`calculate_leg`), verifying schema interoperability, `ST_Reverse` orientation, preserved `display_level=2`, and `0.0 m` continuity gaps.

#### 2. Key POC 2 Architecture Finding (`#29`, `ARCH §4.7` / `§9` — Exact Algorithm Selection)

During live Testcontainers execution against `pgrouting/pgrouting:16-3.5-3.7`, cross-checking `pgr_dijkstra` and `pgr_bdDijkstra` against an independent exact Python Dijkstra oracle (`compute_reference_shortest_path_costs()` / `compare_algorithm_parity()` over identical `edges` weights) uncovered a concrete upstream pgRouting 3.7 behavior:

- **`pgr_dijkstra` is strictly bit-exact:** **`suboptimal_pairs == 0`** (`rel=1e-9, abs=1e-9`) across all `30` benchmark pairs (`1x`/`5x`/`10x` × `distance`/`hops`) relative to the independent Python Dijkstra oracle.
- **`pgr_bdDijkstra` (pgRouting 3.7) early-termination suboptimality quirk:** On near-symmetric undirected grids (`1x` `10x10` Pair 1, `node 9 -> node 90`), `pgr_bdDijkstra` terminated early with a `+0.024 m` (`+0.023987 m`) suboptimal path (`3272.9829 m` vs. exact optimum `3272.9589 m` produced by both `pgr_dijkstra` and the independent reference solver).
- **Production Baseline Recommendation (`T-007` / `T-016`):** Retain **`pgr_dijkstra`** as the exact production baseline for full-graph routing; do **not** replace it with pgRouting 3.7 `pgr_bdDijkstra` where strict global optimality is required.

#### 3. Acceptance Criteria Coverage (`T-004`)

| AC | Scope Verified | Status |
|---|---|---|
| `AC-24` | Connected hybrid leg (access + network + exit), reversed edge orientation, segment difficulty & elevation propagation, continuity `<= tolerance` | **PASS** (Unit + Live PostGIS/pgRouting) |
| `AC-25` | `CLOSE` → `FAR` expansion on both entry and exit sides + deterministic `id ASC` tie-breaking + `ix_nodes_geom_geog` planner verification | **PASS** (Unit + Live PostGIS) |
| `AC-26` | No entry within `FAR` → full external `cycling-mountain` fallback Start→Finish | **PASS** (Unit + Live PostGIS) |
| `AC-27` | Entry found, no exit within `FAR` → continuous Start→Entry driving access + Entry→Finish fallback | **PASS** (Unit + Live PostGIS) |
| `AC-28` | Disconnected components → continuous Start→Entry + `driving-car` connector + Exit→Finish | **PASS** (Unit + Live PostGIS) |
| `AC-29` | Provider `429`/`503`/timeout/empty-geometry/continuity-gap raises structured error with zero partial route; `429` never retried; secrets never leaked (including `httpx` `INFO` logs) | **PASS** (Unit + Live PostGIS) |
| `AC-30` | Difficulty never in inner pgRouting SQL cost or `WHERE`; changing difficulty `1 -> 3` keeps identical path across `distance`/`hops` and `pgr_dijkstra`/`pgr_bdDijkstra` | **PASS** (Unit SQL Audit + Live PostGIS/pgRouting) |
| `AC-62` | Single `REPEATABLE READ READ ONLY` tx closed before external HTTP calls; live concurrent write/rollback snapshot-isolation + `EXPLAIN (ANALYZE, BUFFERS)` / p50 / p95 scaling benchmark (`1x`/`5x`/`10x`) | **PASS** (Unit + Live PostGIS/pgRouting) |
| `AC-78` | Zero bbox pruning in SQL; deterministic optimal-detour-outside-bbox fixture (`seed_outside_bbox_fixture`) tested under `distance` & `hops` and both algorithms | **PASS** (Unit SQL Audit + Live PostGIS/pgRouting) |

#### 4. Live PostGIS/pgRouting Integration Reproduction Command

```bash
cd backend && PYTHONPATH=. .venv/bin/pytest -v -m integration tests/poc/test_routing_poc.py tests/poc/test_snapping_poc.py
```

Verified result at commit `11f1c1a`: **`12 passed, 55 deselected in 35.70s`** against `pgrouting/pgrouting:16-3.5-3.7`.

---

### Batch 2 Security Review, Defect & Escalation Summary

- **Security Re-Audit (`security-auditor`, `#26`):** **SECURITY_APPROVED**. All 5 findings from `#16` remediated and regression-tested:
  - High #1: `httpx`/`httpcore` `INFO` query-secret redaction filter + uncaught `(httpx.HTTPError, httpx.InvalidURL)` wrapping with `__context__ = None`.
  - High #2: Strict `validate_routing_profile()` (`^[a-z0-9][a-z0-9_-]{0,63}$`) across providers, config, and CLI.
  - Medium #3: `validate_provider_base_url()` enforcing `https` (localhost opt-in only) + `follow_redirects=False`.
  - Medium #4: Pre-read symlink, root-containment, extension, and size checks in `parse_track_path()`.
  - Low #5: `DATABASE_URL` env fallback & DSN credential redaction in `app/domain/geo/cli.py`.
  - *Non-blocking future hardening notes (`#26`):* Commit a hash-pinned Python lockfile before production release, and keep `httpcore` logging at `>= WARNING` in production.
- **Code Review (`code-reviewer`, `#27`, `#33`):** **APPROVED** with zero blocking findings.
- **Escalation / Strikes Log:**
  - `dev-snapping`: **`0/2`** strikes.
  - `dev-routing`: **`2/2`** strikes resolved (Strike 1: `ix_nodes_geom_geog` planner verification via explicit `CAST` & realistic `10x` grid; Strike 2: mathematical proof of pgRouting 3.7 `pgr_bdDijkstra` early-termination quirk & independent Python Dijkstra reference oracle verifying `pgr_dijkstra` bit-exactness across all 30 pairs).
  - Debugger escalations required: **`0`**.
