from __future__ import annotations

import pytest

from axle_mcp_server import server as srv


def test_headers_no_auth_configured(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(srv, "AXLE_API_KEY", None)
    h = srv._headers()
    assert "Authorization" not in h
    assert "X-Forwarded-For" not in h
    assert h["X-Request-Source"].startswith("axiom-axle-mcp/")


def test_headers_uses_env_key_in_stdio_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(srv, "AXLE_API_KEY", "env-key")
    assert srv._headers()["Authorization"] == "Bearer env-key"


def test_headers_request_context_overrides_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(srv, "AXLE_API_KEY", "env-key")
    auth_token = srv._request_authorization.set("Bearer user-key")
    ip_token = srv._request_client_ip.set("203.0.113.7")
    try:
        h = srv._headers()
    finally:
        srv._request_authorization.reset(auth_token)
        srv._request_client_ip.reset(ip_token)
    assert h["Authorization"] == "Bearer user-key"
    assert h["X-Forwarded-For"] == "203.0.113.7"


def test_extract_request_context_reads_headers_and_client() -> None:
    scope = {
        "headers": [
            (b"authorization", b"Bearer abc123"),
            (b"x-forwarded-for", b"198.51.100.4, 10.0.0.1"),
            (b"content-type", b"application/json"),
        ],
        "client": ("10.0.0.5", 54321),
    }
    auth, ip = srv._extract_request_context(scope)
    assert auth == "Bearer abc123"
    assert ip == "198.51.100.4"


def test_extract_request_context_falls_back_to_client() -> None:
    scope = {"headers": [], "client": ("192.0.2.9", 1234)}
    auth, ip = srv._extract_request_context(scope)
    assert auth is None
    assert ip == "192.0.2.9"
