from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import BaseModel, Field
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.core.errors import AppError, register_error_handlers


class _SamplePayload(BaseModel):
    count: int = Field(ge=1)
    name: str


def _make_error_test_app() -> FastAPI:
    app = FastAPI()
    register_error_handlers(app)

    @app.get("/app-error-no-details")
    def raise_app_error_no_details() -> None:
        raise AppError(status_code=409, code="DUPLICATE_TRACK", message="Track already exists.")

    @app.get("/app-error-with-details")
    def raise_app_error_with_details() -> None:
        raise AppError(
            status_code=400,
            code="INVALID_GPX",
            message="GPX file is invalid.",
            details={"reason": "missing_trkpt"},
        )

    @app.post("/validate")
    def validate_body(payload: _SamplePayload) -> dict[str, object]:
        return {"count": payload.count, "name": payload.name}

    @app.get("/http-403")
    def raise_http_403() -> None:
        raise StarletteHTTPException(status_code=403, detail="Admin role required.")

    @app.get("/http-500-secret")
    def raise_http_500() -> None:
        raise StarletteHTTPException(
            status_code=500,
            detail="postgresql+psycopg://secret_user:secret_pass@db/ontrack",
        )

    @app.get("/http-503")
    def raise_http_503() -> None:
        raise StarletteHTTPException(status_code=503, detail="internal downstream detail")

    @app.get("/unhandled")
    def raise_unhandled() -> None:
        raise RuntimeError("SECRET_DB_PASSWORD=supersecret traceback leak check")

    return app


def test_app_error_omits_details_when_none() -> None:
    client = TestClient(_make_error_test_app())
    response = client.get("/app-error-no-details")

    assert response.status_code == 409
    body = response.json()
    assert body == {"code": "DUPLICATE_TRACK", "message": "Track already exists."}
    assert "details" not in body


def test_app_error_includes_details_when_present() -> None:
    client = TestClient(_make_error_test_app())
    response = client.get("/app-error-with-details")

    assert response.status_code == 400
    assert response.json() == {
        "code": "INVALID_GPX",
        "message": "GPX file is invalid.",
        "details": {"reason": "missing_trkpt"},
    }


def test_request_validation_error_returns_422_envelope() -> None:
    client = TestClient(_make_error_test_app())
    response = client.post("/validate", json={"count": 0})

    assert response.status_code == 422
    body = response.json()
    assert body["code"] == "VALIDATION_ERROR"
    assert body["message"] == "Request validation failed."
    assert isinstance(body["details"], list)
    assert len(body["details"]) == 2
    for item in body["details"]:
        assert set(item.keys()) == {"loc", "msg", "type"}


def test_starlette_http_exception_404_and_405_and_403() -> None:
    client = TestClient(_make_error_test_app())

    not_found = client.get("/does-not-exist")
    assert not_found.status_code == 404
    assert not_found.json() == {"code": "NOT_FOUND", "message": "Not Found"}

    method_not_allowed = client.delete("/validate")
    assert method_not_allowed.status_code == 405
    assert "POST" in method_not_allowed.headers.get("allow", "")
    assert method_not_allowed.json() == {
        "code": "METHOD_NOT_ALLOWED",
        "message": "Method Not Allowed",
    }

    forbidden = client.get("/http-403")
    assert forbidden.status_code == 403
    assert forbidden.json() == {"code": "FORBIDDEN", "message": "Admin role required."}


def test_starlette_http_5xx_sanitizes_internal_detail() -> None:
    client = TestClient(_make_error_test_app())

    resp_500 = client.get("/http-500-secret")
    assert resp_500.status_code == 500
    assert resp_500.json() == {
        "code": "INTERNAL_ERROR",
        "message": "An unexpected internal error occurred.",
    }
    assert "secret_pass" not in resp_500.text
    assert "postgresql" not in resp_500.text

    resp_503 = client.get("/http-503")
    assert resp_503.status_code == 503
    assert resp_503.json() == {
        "code": "SERVICE_UNAVAILABLE",
        "message": "Service temporarily unavailable.",
    }
    assert "internal downstream detail" not in resp_503.text


def test_unhandled_exception_returns_500_without_traceback_or_secret_leakage() -> None:
    client = TestClient(_make_error_test_app(), raise_server_exceptions=False)
    response = client.get("/unhandled")

    assert response.status_code == 500
    assert response.json() == {
        "code": "INTERNAL_ERROR",
        "message": "An unexpected internal error occurred.",
    }
    assert "SECRET_DB_PASSWORD" not in response.text
    assert "RuntimeError" not in response.text
    assert "Traceback" not in response.text
