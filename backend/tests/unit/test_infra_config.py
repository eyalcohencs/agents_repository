from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import yaml

REPO_ROOT = Path(__file__).resolve().parents[3]


def _extract_location_block(nginx_conf: str, location_header: str) -> str:
    pattern = re.compile(
        rf"{re.escape(location_header)}\s*\{{(?P<body>[^{{}}]*)\}}",
        re.MULTILINE | re.DOTALL,
    )
    match = pattern.search(nginx_conf)
    assert match is not None, f"Missing Nginx block: {location_header}"
    return match.group("body")


def test_nginx_conf_spa_proxy_limits_and_health_exemption() -> None:
    nginx_path = REPO_ROOT / "nginx" / "nginx.conf"
    assert nginx_path.is_file()
    content = nginx_path.read_text(encoding="utf-8")

    # Unprivileged runtime listener, pid, security & upload sizing
    assert "listen 8080;" in content
    assert "pid /tmp/nginx.pid;" in content
    assert "server_tokens off;" in content
    assert "client_max_body_size 12m;" in content
    assert "X-Content-Type-Options" in content
    assert "X-Frame-Options" in content
    assert "Content-Security-Policy" in content

    # Edge proxy must overwrite X-Forwarded-For with $remote_addr (no client spoofing)
    assert "proxy_set_header X-Forwarded-For $remote_addr;" in content
    assert "$proxy_add_x_forwarded_for" not in content

    # Per-IP rate limiting zones and JSON 429 handling
    assert "limit_req_zone $binary_remote_addr zone=api_general:" in content
    assert "limit_req_zone $binary_remote_addr zone=api_auth:" in content
    assert "limit_req_zone $binary_remote_addr zone=api_heavy:" in content
    assert "limit_req_status 429;" in content
    assert "error_page 429 = @rate_limited;" in content

    # Auth & heavy regexes include trailing slash support and edge rating endpoint
    assert "location ~ ^/api/auth/(login|register)/?$" in content
    assert (
        "location ~ ^/api/(routes|uploads/validate|uploads/[^/]+/confirm"
        "|network/edges|network/edges/[^/]+/rating)/?$" in content
    )

    # Upstream targets FastAPI api:8000
    assert "server api:8000;" in content

    # /api/health is proxied to api_backend and MUST be exempt from rate limiting
    health_block = _extract_location_block(content, "location = /api/health")
    assert "proxy_pass http://api_backend;" in health_block
    assert "limit_req" not in health_block

    # /api/ proxies to api_backend with rate limiting
    api_block = _extract_location_block(content, "location /api/")
    assert "proxy_pass http://api_backend;" in api_block
    assert "limit_req zone=api_general" in api_block

    # SPA fallback supports direct URL loads & hard reloads (e.g., /privacy-and-terms-of-use)
    spa_block = _extract_location_block(content, "location /")
    assert "try_files $uri $uri/ /index.html;" in spa_block


def test_nginx_dockerfile_uses_unprivileged_image() -> None:
    dockerfile = REPO_ROOT / "nginx" / "Dockerfile"
    assert dockerfile.is_file()
    content = dockerfile.read_text(encoding="utf-8")

    assert "FROM nginxinc/nginx-unprivileged:1.27-alpine" in content
    assert "USER nginx" in content
    assert "EXPOSE 8080" in content


def test_dockerignore_files_exclude_secrets_and_build_caches() -> None:
    root_ignore = REPO_ROOT / ".dockerignore"
    backend_ignore = REPO_ROOT / "backend" / ".dockerignore"
    assert root_ignore.is_file()
    assert backend_ignore.is_file()

    root_lines = {
        line.strip()
        for line in root_ignore.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.strip().startswith("#")
    }
    for required in (".git", ".env", ".env.*", "!.env.example", "backend/", "**/node_modules"):
        assert required in root_lines

    backend_lines = {
        line.strip()
        for line in backend_ignore.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.strip().startswith("#")
    }
    for required in (".venv", ".env", ".env.*", "!.env.example", "tests", "**/__pycache__"):
        assert required in backend_lines


def test_docker_compose_services_volumes_and_healthchecks() -> None:
    compose_path = REPO_ROOT / "docker-compose.yml"
    assert compose_path.is_file()
    data: dict[str, Any] = yaml.safe_load(compose_path.read_text(encoding="utf-8"))

    services = data["services"]
    assert {"postgres", "api", "worker", "nginx"}.issubset(services.keys())

    # Postgres with pgRouting image, healthcheck & persistent pg_data volume
    postgres = services["postgres"]
    assert postgres["image"] == "pgrouting/pgrouting:16-3.5-3.7"
    assert "pg_data:/var/lib/postgresql/data" in postgres["volumes"]
    assert "pg_isready" in " ".join(postgres["healthcheck"]["test"])

    # API & Worker mount shared uploads_data:/data/uploads and use psycopg v3
    for svc_name in ("api", "worker"):
        svc = services[svc_name]
        assert "uploads_data:/data/uploads" in svc["volumes"]
        assert svc["environment"]["UPLOAD_STORAGE_DIR"] == "/data/uploads"
        assert svc["environment"]["DATABASE_URL"].startswith("postgresql+psycopg://")
        assert svc["depends_on"]["postgres"]["condition"] == "service_healthy"

    # API runs migrations + uvicorn and has /api/health check
    api_cmd = " ".join(services["api"]["command"])
    assert "alembic upgrade head" in api_cmd
    assert "uvicorn app.main:app" in api_cmd
    assert "/api/health" in " ".join(services["api"]["healthcheck"]["test"])

    # Worker runs placeholder entrypoint cleanly
    assert services["worker"]["command"] == ["python", "-m", "app.workers.placeholder"]

    # Nginx builds from repo root and exposes ${NGINX_PORT:-8080}:8080 (unprivileged)
    nginx = services["nginx"]
    assert nginx["build"]["context"] == "."
    assert nginx["build"]["dockerfile"] == "nginx/Dockerfile"
    assert "${NGINX_PORT:-8080}:8080" in nginx["ports"]
    assert nginx["depends_on"]["api"]["condition"] == "service_healthy"

    # Named volumes
    assert "pg_data" in data["volumes"]
    assert "uploads_data" in data["volumes"]


def test_env_example_contains_all_required_keys() -> None:
    env_example = REPO_ROOT / ".env.example"
    assert env_example.is_file()

    parsed: dict[str, str] = {}
    for raw_line in env_example.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        key, _, value = line.partition("=")
        parsed[key.strip()] = value.strip()

    expected_keys = {
        "APP_ENV",
        "POSTGRES_USER",
        "POSTGRES_PASSWORD",
        "POSTGRES_DB",
        "POSTGRES_PORT",
        "DATABASE_URL",
        "DB_POOL_SIZE",
        "DB_MAX_OVERFLOW",
        "DB_STATEMENT_TIMEOUT_MS",
        "UPLOAD_STORAGE_DIR",
        "SUPPORT_EMAIL",
        "NETWORK_LAYER_MIN_ZOOM",
        "MAX_UPLOAD_MB",
        "COOKIE_SECURE",
        "LOG_LEVEL",
        "NGINX_PORT",
    }
    assert expected_keys.issubset(parsed.keys())
    assert parsed["DATABASE_URL"].startswith("postgresql+psycopg://")


def test_github_actions_ci_workflow_structure() -> None:
    ci_path = REPO_ROOT / ".github" / "workflows" / "ci.yml"
    assert ci_path.is_file()
    workflow: dict[str, Any] = yaml.safe_load(ci_path.read_text(encoding="utf-8"))

    # YAML 1.1 parses bare `on:` as boolean True
    triggers = workflow.get("on", workflow.get(True))
    assert "push" in triggers
    assert "pull_request" in triggers
    assert workflow.get("permissions", {}).get("contents") == "read"

    jobs = workflow["jobs"]
    expected_jobs = {
        "backend-lint-typecheck",
        "backend-tests",
        "frontend-checks",
        "frontend-e2e",
    }
    assert expected_jobs.issubset(jobs.keys())

    # Every checkout step must disable credential persistence
    for job_name in expected_jobs:
        checkout_steps = [
            step
            for step in jobs[job_name]["steps"]
            if str(step.get("uses", "")).startswith("actions/checkout@")
        ]
        assert checkout_steps, f"Missing checkout step in {job_name}"
        for step in checkout_steps:
            assert step.get("with", {}).get("persist-credentials") is False

    lint_runs = "\n".join(
        step.get("run", "") for step in jobs["backend-lint-typecheck"]["steps"]
    )
    assert "ruff check ." in lint_runs
    assert "mypy app" in lint_runs

    test_runs = "\n".join(step.get("run", "") for step in jobs["backend-tests"]["steps"])
    assert "pytest" in test_runs

    fe_runs = "\n".join(step.get("run", "") for step in jobs["frontend-checks"]["steps"])
    for expected in (
        "npm ci",
        "npm run lint",
        "npm run typecheck",
        "npm run test",
        "npm run build",
    ):
        assert expected in fe_runs

    e2e_runs = "\n".join(step.get("run", "") for step in jobs["frontend-e2e"]["steps"])
    assert "playwright install" in e2e_runs
    assert "npm run e2e" in e2e_runs


def test_frontend_global_styles_do_not_mask_horizontal_overflow() -> None:
    styles_path = REPO_ROOT / "frontend" / "src" / "styles.scss"
    assert styles_path.is_file()
    content = styles_path.read_text(encoding="utf-8")
    assert "overflow-x: clip" not in content
    assert "overflow-x: hidden" not in content
