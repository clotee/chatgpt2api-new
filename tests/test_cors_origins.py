"""T-20 regression: CHATGPT2API_CORS_ORIGINS allowlist (default same-origin)."""

import pytest
from fastapi.testclient import TestClient
from starlette.middleware.cors import CORSMiddleware

from api.app import create_app


def _cors_allow_origins(app):
    middleware = [m for m in app.user_middleware if m.cls is CORSMiddleware]
    assert len(middleware) == 1
    return middleware[0].kwargs["allow_origins"]


def test_cors_defaults_to_empty_allowlist(monkeypatch):
    monkeypatch.delenv("CHATGPT2API_CORS_ORIGINS", raising=False)
    assert _cors_allow_origins(create_app()) == []


def test_cors_allowlist_parsed_from_env(monkeypatch):
    monkeypatch.setenv(
        "CHATGPT2API_CORS_ORIGINS",
        "https://a.example, https://b.example ,",
    )
    assert _cors_allow_origins(create_app()) == [
        "https://a.example",
        "https://b.example",
    ]


def test_cors_preflight_allows_listed_origin(monkeypatch):
    monkeypatch.setenv("CHATGPT2API_CORS_ORIGINS", "https://allowed.example")
    client = TestClient(create_app())
    response = client.options(
        "/api/accounts",
        headers={
            "Origin": "https://allowed.example",
            "Access-Control-Request-Method": "GET",
        },
    )
    assert response.status_code == 200
    assert response.headers["access-control-allow-origin"] == "https://allowed.example"


def test_cors_preflight_rejects_unlisted_origin(monkeypatch):
    monkeypatch.setenv("CHATGPT2API_CORS_ORIGINS", "https://allowed.example")
    client = TestClient(create_app())
    response = client.options(
        "/api/accounts",
        headers={
            "Origin": "https://evil.example",
            "Access-Control-Request-Method": "GET",
        },
    )
    assert response.status_code == 400
    assert "access-control-allow-origin" not in response.headers


def test_cors_default_blocks_cross_origin_preflight(monkeypatch):
    monkeypatch.delenv("CHATGPT2API_CORS_ORIGINS", raising=False)
    client = TestClient(create_app())
    response = client.options(
        "/api/accounts",
        headers={
            "Origin": "https://evil.example",
            "Access-Control-Request-Method": "GET",
        },
    )
    assert response.status_code == 400
    assert "access-control-allow-origin" not in response.headers
