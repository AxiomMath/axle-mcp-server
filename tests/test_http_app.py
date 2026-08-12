from __future__ import annotations

import json
from typing import Any

import pytest

import axle_mcp_server.server as srv

pytest.importorskip("starlette", reason="HTTP mode needs the [http] extra")


async def _call(app: Any, method: str, path: str) -> tuple[int, dict[str, str], bytes]:
    """Drive the ASGI app directly — no socket, no lifespan."""
    sent: list[dict[str, Any]] = []

    async def receive() -> dict[str, Any]:
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message: dict[str, Any]) -> None:
        sent.append(message)

    await app({"type": "http", "method": method, "path": path, "headers": []}, receive, send)

    start = next(m for m in sent if m["type"] == "http.response.start")
    headers = {k.decode(): v.decode() for k, v in start.get("headers", [])}
    body = b"".join(m.get("body", b"") for m in sent if m["type"] == "http.response.body")
    return start["status"], headers, body


@pytest.mark.parametrize("method", ["GET", "HEAD"])
@pytest.mark.parametrize("path", ["/mcp", "/mcp/"])
async def test_stream_open_is_rejected_not_held(method: str, path: str) -> None:
    """Stateless mode has no session to push to, so a held-open SSE stream never
    emits and Cloud Run withholds its headers — the client hangs. 405 instead."""
    status, headers, _ = await _call(srv._build_http_app(), method, path)
    assert status == 405
    assert headers.get("allow") == "POST"


async def test_health_still_served() -> None:
    status, _, body = await _call(srv._build_http_app(), "GET", "/")
    assert status == 200
    assert json.loads(body)["mcp_endpoint"] == "/mcp"
