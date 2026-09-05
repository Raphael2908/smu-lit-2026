"""Liveness, readiness and CORS."""

from __future__ import annotations


def test_healthz(client):
    response = client.get("/healthz")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_readyz_reports_mock_mode_without_a_database(client):
    """The headline requirement: no database, no broker, no keys -- still answers."""
    response = client.get("/readyz")
    assert response.status_code == 200

    body = response.json()
    assert body["provider_mode"] == "mock"
    assert body["status"] == "ok"  # mock mode is self-contained by design
    assert body["database"] is False
    assert body["redis"] is False
    # Visible BEFORE a demo rather than during one.
    assert body["browser_session"] == "mock"


def test_openapi_is_servable(client):
    """A schema that cannot be generated means a broken response model somewhere."""
    response = client.get("/openapi.json")
    assert response.status_code == 200
    paths = response.json()["paths"]
    for path in ("/healthz", "/readyz", "/v1/verify", "/v1/runs/{run_id}", "/v1/lists"):
        assert path in paths


def test_request_id_is_echoed(client):
    response = client.get("/healthz", headers={"X-Request-ID": "abc123"})
    assert response.headers["X-Request-ID"] == "abc123"


def test_request_id_is_minted_when_absent(client):
    response = client.get("/healthz")
    assert response.headers.get("X-Request-ID")


def test_cors_allows_the_extension_and_claude_ai(client):
    """A content script's fetch carries the PAGE origin, not the extension's -- both
    must be allowed or every request dies in preflight with nothing obviously wrong."""
    for origin in (
        "chrome-extension://abcdefghijklmnopabcdefghijklmnop",
        "https://claude.ai",
        "http://localhost:5173",
    ):
        response = client.options(
            "/v1/verify",
            headers={
                "Origin": origin,
                "Access-Control-Request-Method": "POST",
                "Access-Control-Request-Headers": "content-type",
            },
        )
        assert response.status_code == 200, origin
        assert response.headers["access-control-allow-origin"] == origin
