"""End-to-end OAuth tests against the real HTTP app on a real socket.

A mock AXLE upstream stands in for https://axle.axiommath.ai. The MCP SDK's own OAuth
client plays the role of Claude / ChatGPT: it discovers metadata, registers (DCR or
CIMD), runs authorization-code + PKCE, exchanges the code, and then calls a tool. The
"browser" part of the flow (our login page) is driven with httpx.
"""

from __future__ import annotations

import asyncio
import json
import socket
import urllib.parse
from collections.abc import AsyncIterator
from typing import Any

import pytest

pytest.importorskip("starlette", reason="HTTP mode needs the [http] extra")
pytest.importorskip("cryptography", reason="HTTP mode needs the [http] extra")
pytest.importorskip("uvicorn", reason="HTTP mode needs the [http] extra")

import httpx  # noqa: E402
import uvicorn  # noqa: E402
from mcp import ClientSession  # noqa: E402
from mcp.client.auth import OAuthClientProvider, TokenStorage  # noqa: E402
from mcp.client.streamable_http import streamable_http_client  # noqa: E402
from mcp.shared.auth import OAuthClientInformationFull, OAuthClientMetadata, OAuthToken  # noqa: E402
from pydantic import AnyUrl  # noqa: E402
from starlette.applications import Starlette  # noqa: E402
from starlette.requests import Request  # noqa: E402
from starlette.responses import JSONResponse  # noqa: E402
from starlette.routing import Route  # noqa: E402

import axle_mcp_server.auth as auth  # noqa: E402
import axle_mcp_server.server as srv  # noqa: E402

VALID_KEY = "sk-axle-valid-key"
CLIENT_REDIRECT = "http://localhost:3030/callback"


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


class _Server:
    def __init__(self, app: Any) -> None:
        self.port = _free_port()
        self.url = f"http://127.0.0.1:{self.port}"
        config = uvicorn.Config(app, host="127.0.0.1", port=self.port, log_level="warning")
        self.server = uvicorn.Server(config)
        self.task: asyncio.Task[None] | None = None

    async def __aenter__(self) -> _Server:
        self.task = asyncio.create_task(self.server.serve())
        for _ in range(200):
            if self.server.started:
                return self
            await asyncio.sleep(0.02)
        raise RuntimeError("server did not start")

    async def __aexit__(self, *exc: Any) -> None:
        self.server.should_exit = True
        assert self.task is not None
        await self.task


class MockAxle:
    """Just enough of the AXLE API for key validation and one tool call."""

    def __init__(self) -> None:
        self.seen_auth: list[str | None] = []

    def app(self) -> Starlette:
        async def environments(request: Request) -> JSONResponse:
            self.seen_auth.append(request.headers.get("authorization"))
            if request.headers.get("authorization") != f"Bearer {VALID_KEY}":
                return JSONResponse({"detail": "Invalid API key"}, status_code=401)
            return JSONResponse([{"name": "lean-4.28.0"}])

        async def check(request: Request) -> JSONResponse:
            self.seen_auth.append(request.headers.get("authorization"))
            body = await request.json()
            return JSONResponse({"echo": body, "auth": request.headers.get("authorization")})

        return Starlette(
            routes=[
                Route("/v1/environments", environments, methods=["GET"]),
                Route("/api/v1/check", check, methods=["POST"]),
            ]
        )


@pytest.fixture
async def stack(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[tuple[str, MockAxle]]:
    monkeypatch.setenv("AXLE_MCP_TOKEN_SECRET", "e2e-secret")
    monkeypatch.delenv("AXLE_MCP_PUBLIC_URL", raising=False)
    monkeypatch.delenv("AXLE_MCP_ALLOW_ANONYMOUS", raising=False)
    mock = MockAxle()
    async with _Server(mock.app()) as upstream:
        monkeypatch.setattr(srv, "AXLE_API_URL", upstream.url)
        async with _Server(srv._build_http_app()) as mcp:
            yield mcp.url, mock


class MemoryStorage(TokenStorage):
    def __init__(self) -> None:
        self.tokens: OAuthToken | None = None
        self.client_info: OAuthClientInformationFull | None = None

    async def get_tokens(self) -> OAuthToken | None:
        return self.tokens

    async def set_tokens(self, tokens: OAuthToken) -> None:
        self.tokens = tokens

    async def get_client_info(self) -> OAuthClientInformationFull | None:
        return self.client_info

    async def set_client_info(self, client_info: OAuthClientInformationFull) -> None:
        self.client_info = client_info


class Browser:
    """Drives /authorize -> /login -> redirect back to the client, like a person would."""

    def __init__(self, api_key: str = VALID_KEY, first_try_bad_key: bool = False) -> None:
        self.api_key = api_key
        self.first_try_bad_key = first_try_bad_key
        self.callback_query: dict[str, str] = {}
        self.login_pages: list[str] = []

    async def redirect_handler(self, authorization_url: str) -> None:
        async with httpx.AsyncClient(follow_redirects=False) as http:
            r = await http.get(authorization_url)
            assert r.status_code == 302, r.text
            login_url = r.headers["location"]
            assert "/login?req=" in login_url
            r = await http.get(login_url)
            assert r.status_code == 200
            self.login_pages.append(r.text)
            req = urllib.parse.parse_qs(urllib.parse.urlsplit(login_url).query)["req"][0]
            if self.first_try_bad_key:
                r = await http.post(
                    f"{login_url.split('/login')[0]}/login",
                    data={"req": req, "api_key": "definitely-wrong", "action": "allow"},
                )
                assert r.status_code == 400
                assert "AXLE rejected" in r.text
            r = await http.post(
                f"{login_url.split('/login')[0]}/login",
                data={"req": req, "api_key": self.api_key, "action": "allow"},
            )
            assert r.status_code == 302, r.text
            location = r.headers["location"]
            assert location.startswith(CLIENT_REDIRECT + "?")
            self.callback_query = {
                k: v[0]
                for k, v in urllib.parse.parse_qs(urllib.parse.urlsplit(location).query).items()
            }

    async def callback_handler(self) -> tuple[str, str | None]:
        return self.callback_query["code"], self.callback_query.get("state")


def _client_metadata() -> OAuthClientMetadata:
    return OAuthClientMetadata(
        redirect_uris=[AnyUrl(CLIENT_REDIRECT)],
        client_name="E2E test client",
        token_endpoint_auth_method="none",
        grant_types=["authorization_code", "refresh_token"],
        response_types=["code"],
    )


async def _run_mcp_session(mcp_url: str, oauth: OAuthClientProvider) -> dict[str, Any]:
    http = httpx.AsyncClient(
        auth=oauth, headers={"Accept": "application/json, text/event-stream"}, timeout=30
    )
    async with streamable_http_client(f"{mcp_url}/mcp", http_client=http) as (read, write, _):
        async with ClientSession(read, write) as session:
            init = await session.initialize()
            assert init.serverInfo.name == "axle"
            tools = await session.list_tools()
            names = {t.name for t in tools.tools}
            assert {"check", "verify_proof", "read_docs", "share_url"} <= names
            check = next(t for t in tools.tools if t.name == "check")
            assert check.annotations is not None and check.annotations.readOnlyHint is True
            result = await session.call_tool("check", {"content": "example : 1 = 1 := rfl"})
            assert not result.isError, result
            text = result.content[0].text  # type: ignore[union-attr]
            return json.loads(text)


async def test_dcr_flow_end_to_end(stack: tuple[str, MockAxle]) -> None:
    mcp_url, mock = stack
    storage = MemoryStorage()
    browser = Browser(first_try_bad_key=True)
    oauth = OAuthClientProvider(
        server_url=f"{mcp_url}/mcp",
        client_metadata=_client_metadata(),
        storage=storage,
        redirect_handler=browser.redirect_handler,
        callback_handler=browser.callback_handler,
    )
    payload = await _run_mcp_session(mcp_url, oauth)

    # The tool call reached AXLE with the user's API key, never with our OAuth token.
    assert payload["auth"] == f"Bearer {VALID_KEY}"
    assert payload["echo"]["content"] == "example : 1 = 1 := rfl"
    assert payload["echo"]["environment"] == srv.DEFAULT_ENVIRONMENT
    assert all(a is None or not a.startswith("Bearer axmcp_") for a in mock.seen_auth)

    # Dynamic registration produced a stateless client id; tokens are ours; iss echoed.
    assert storage.client_info is not None
    assert str(storage.client_info.client_id).startswith("axmcp_c_")
    assert storage.tokens is not None
    assert storage.tokens.access_token.startswith("axmcp_at_")
    assert storage.tokens.refresh_token and storage.tokens.refresh_token.startswith("axmcp_rt_")
    assert browser.callback_query["iss"] == mcp_url
    assert "E2E test client" in browser.login_pages[0]
    assert "<code>localhost</code>" in browser.login_pages[0]

    # Refresh: rotates both tokens and the new access token still works on /mcp.
    async with httpx.AsyncClient() as http:
        r = await http.post(
            f"{mcp_url}/token",
            data={
                "grant_type": "refresh_token",
                "refresh_token": storage.tokens.refresh_token,
                "client_id": storage.client_info.client_id,
            },
        )
        assert r.status_code == 200, r.text
        refreshed = r.json()
        assert refreshed["access_token"] != storage.tokens.access_token
        assert refreshed["refresh_token"] != storage.tokens.refresh_token
        assert refreshed["expires_in"] == auth.ACCESS_TOKEN_TTL

        r = await http.post(
            f"{mcp_url}/mcp",
            headers={
                "Authorization": f"Bearer {refreshed['access_token']}",
                "Accept": "application/json, text/event-stream",
                "Content-Type": "application/json",
            },
            json={"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}},
        )
        assert r.status_code == 200, r.text

        # A refresh token that isn't ours -> RFC 6749 invalid_grant (Claude relies on it).
        r = await http.post(
            f"{mcp_url}/token",
            data={
                "grant_type": "refresh_token",
                "refresh_token": "axmcp_rt_bogus",
                "client_id": storage.client_info.client_id,
            },
        )
        assert r.status_code == 400 and r.json()["error"] == "invalid_grant"


async def test_cimd_flow_end_to_end(
    stack: tuple[str, MockAxle], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Client ID Metadata Document: no /register call, client_id is an https URL."""
    mcp_url, _mock = stack
    cimd_url = "https://client.example/oauth/client.json"
    fetched: list[str] = []

    def fake_fetch(url: str) -> Any:
        fetched.append(url)
        return {
            "client_id": cimd_url,
            "client_name": "CIMD client",
            "redirect_uris": ["http://localhost/callback"],  # port-agnostic loopback
            "token_endpoint_auth_method": "none",
        }

    monkeypatch.setattr(auth, "fetch_client_metadata_document", fake_fetch)
    storage = MemoryStorage()
    browser = Browser()
    oauth = OAuthClientProvider(
        server_url=f"{mcp_url}/mcp",
        client_metadata=_client_metadata(),
        storage=storage,
        redirect_handler=browser.redirect_handler,
        callback_handler=browser.callback_handler,
        client_metadata_url=cimd_url,
    )
    payload = await _run_mcp_session(mcp_url, oauth)
    assert payload["auth"] == f"Bearer {VALID_KEY}"
    assert fetched and fetched[0] == cimd_url
    assert storage.client_info is not None and storage.client_info.client_id == cimd_url
    # Self-asserted document: the consent page names the host, not the client_name.
    assert "client.example" in browser.login_pages[0]


async def test_confidential_dcr_client_manual_flow(stack: tuple[str, MockAxle]) -> None:
    """ChatGPT-style: client_secret_post, hand-rolled PKCE, deny path, code single-use."""
    import base64
    import hashlib
    import secrets

    mcp_url, _mock = stack
    redirect = "https://chatgpt.com/connector_platform_oauth_redirect"
    async with httpx.AsyncClient(follow_redirects=False) as http:
        r = await http.post(
            f"{mcp_url}/register",
            json={
                "redirect_uris": [redirect],
                "client_name": "ChatGPT",
                "token_endpoint_auth_method": "client_secret_post",
                "grant_types": ["authorization_code", "refresh_token"],
                "response_types": ["code"],
            },
        )
        assert r.status_code == 201, r.text
        reg = r.json()
        assert reg["client_secret"] and reg["client_secret_expires_at"] == 0

        verifier = secrets.token_urlsafe(48)
        challenge = (
            base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest())
            .decode()
            .rstrip("=")
        )
        params = {
            "response_type": "code",
            "client_id": reg["client_id"],
            "redirect_uri": redirect,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
            "state": "xyz",
            "resource": f"{mcp_url}/mcp",
        }
        r = await http.get(f"{mcp_url}/authorize", params=params)
        assert r.status_code == 302, r.text
        login_url = r.headers["location"]
        req = urllib.parse.parse_qs(urllib.parse.urlsplit(login_url).query)["req"][0]

        # Cancel -> access_denied with state preserved.
        r = await http.post(f"{mcp_url}/login", data={"req": req, "action": "deny"})
        assert r.status_code == 302
        q = urllib.parse.parse_qs(urllib.parse.urlsplit(r.headers["location"]).query)
        assert q["error"] == ["access_denied"] and q["state"] == ["xyz"]

        # Approve.
        r = await http.post(
            f"{mcp_url}/login", data={"req": req, "api_key": VALID_KEY, "action": "allow"}
        )
        assert r.status_code == 302, r.text
        q = urllib.parse.parse_qs(urllib.parse.urlsplit(r.headers["location"]).query)
        assert q["state"] == ["xyz"] and q["iss"] == [mcp_url]
        code = q["code"][0]

        # Wrong secret -> 401; wrong verifier -> invalid_grant; right -> tokens.
        base_form = {
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": redirect,
            "client_id": reg["client_id"],
            "resource": f"{mcp_url}/mcp",
        }
        r = await http.post(
            f"{mcp_url}/token",
            data={**base_form, "client_secret": "nope", "code_verifier": verifier},
        )
        assert r.status_code == 401
        r = await http.post(
            f"{mcp_url}/token",
            data={**base_form, "client_secret": reg["client_secret"], "code_verifier": "wrong"},
        )
        assert r.status_code == 400 and r.json()["error"] == "invalid_grant"
        r = await http.post(
            f"{mcp_url}/token",
            data={**base_form, "client_secret": reg["client_secret"], "code_verifier": verifier},
        )
        assert r.status_code == 200, r.text
        tokens = r.json()
        assert tokens["token_type"] == "Bearer" and tokens["access_token"].startswith("axmcp_at_")

        # Same code again -> invalid_grant.
        r = await http.post(
            f"{mcp_url}/token",
            data={**base_form, "client_secret": reg["client_secret"], "code_verifier": verifier},
        )
        assert r.status_code == 400 and r.json()["error"] == "invalid_grant"

        # Unregistered redirect_uri is refused without redirecting anywhere.
        r = await http.get(
            f"{mcp_url}/authorize", params={**params, "redirect_uri": "https://evil.test/cb"}
        )
        assert r.status_code == 400


async def test_raw_api_key_header_end_to_end(stack: tuple[str, MockAxle]) -> None:
    """Claude Code `--header "Authorization: Bearer <key>"` path: no OAuth at all."""
    mcp_url, mock = stack
    headers = {
        "Accept": "application/json, text/event-stream",
        "Content-Type": "application/json",
    }
    init = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": "2025-06-18",
            "capabilities": {},
            "clientInfo": {"name": "curl", "version": "0"},
        },
    }
    async with httpx.AsyncClient() as http:
        r = await http.post(f"{mcp_url}/mcp", headers=headers, json=init)
        assert r.status_code == 401
        assert "resource_metadata=" in r.headers["www-authenticate"]

        r = await http.post(
            f"{mcp_url}/mcp",
            headers={**headers, "Authorization": "Bearer not-a-real-key"},
            json=init,
        )
        assert r.status_code == 401
        assert 'error="invalid_token"' in r.headers["www-authenticate"]

        r = await http.post(
            f"{mcp_url}/mcp", headers={**headers, "Authorization": f"Bearer {VALID_KEY}"}, json=init
        )
        assert r.status_code == 200, r.text
    assert f"Bearer {VALID_KEY}" in mock.seen_auth
