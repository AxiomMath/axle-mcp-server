from __future__ import annotations

import json
import time
import urllib.parse
from typing import Any

import pytest

pytest.importorskip("starlette", reason="HTTP mode needs the [http] extra")
pytest.importorskip("cryptography", reason="HTTP mode needs the [http] extra")

from pydantic import AnyUrl  # noqa: E402

import axle_mcp_server.auth as auth  # noqa: E402
import axle_mcp_server.server as srv  # noqa: E402

BASE = "https://mcp.example.test"


@pytest.fixture
def codec() -> auth.TokenCodec:
    return auth.TokenCodec("unit-test-secret")


@pytest.fixture
def provider(codec: auth.TokenCodec) -> auth.AxleOAuthProvider:
    return auth.AxleOAuthProvider(codec, axle_api_url=lambda: "https://axle.invalid")


# --- sealed tokens -------------------------------------------------------------


def test_codec_roundtrip_and_prefix(codec: auth.TokenCodec) -> None:
    tok = codec.seal("at", {"api_key": "k"}, ttl=60)
    assert tok.startswith("axmcp_at_")
    assert codec.looks_issued(tok)
    payload = codec.open("at", tok)
    assert payload is not None and payload["api_key"] == "k" and "jti" in payload


def test_codec_rejects_wrong_kind_tamper_and_expiry(codec: auth.TokenCodec) -> None:
    tok = codec.seal("at", {"api_key": "k"}, ttl=60)
    assert codec.open("rt", tok) is None
    assert codec.open("at", tok[:-2] + "zz") is None
    assert codec.open("at", "axmcp_at_notatoken") is None
    expired = codec.seal("at", {"api_key": "k"}, ttl=-1)
    assert codec.open("at", expired) is None
    other = auth.TokenCodec("different-secret")
    assert other.open("at", tok) is None


def test_codec_ephemeral_secret_warns(caplog: pytest.LogCaptureFixture) -> None:
    c = auth.TokenCodec(None)
    assert c.ephemeral
    assert "AXLE_MCP_TOKEN_SECRET" in caplog.text


# --- URL helpers ---------------------------------------------------------------


def test_canonical_resource() -> None:
    assert (
        auth.canonical_resource("HTTPS://MCP.Example.com:443/mcp/") == "https://mcp.example.com/mcp"
    )
    assert auth.canonical_resource("http://localhost:8080/mcp") == "http://localhost:8080/mcp"
    assert auth.canonical_resource("https://x.test") == "https://x.test"


@pytest.mark.parametrize(
    ("registered", "requested", "ok"),
    [
        (
            "https://claude.ai/api/mcp/auth_callback",
            "https://claude.ai/api/mcp/auth_callback",
            True,
        ),
        (
            "https://claude.ai/api/mcp/auth_callback",
            "https://evil.test/api/mcp/auth_callback",
            False,
        ),
        ("http://localhost/callback", "http://localhost:3118/callback", True),
        ("http://127.0.0.1/callback", "http://127.0.0.1:52011/callback", True),
        ("http://localhost/callback", "http://localhost:3118/other", False),
        ("http://localhost/callback", "https://localhost:3118/callback", False),
        ("https://a.test/cb", "https://a.test:8443/cb", False),
    ],
)
def test_redirect_uri_matches(registered: str, requested: str, ok: bool) -> None:
    assert auth.redirect_uri_matches(registered, requested) is ok


def test_check_redirect_uri_allowed() -> None:
    auth.check_redirect_uri_allowed("https://chatgpt.com/connector_platform_oauth_redirect")
    auth.check_redirect_uri_allowed("http://localhost:6274/oauth/callback")
    auth.check_redirect_uri_allowed("cursor://anysphere.cursor-mcp/oauth/callback")
    with pytest.raises(ValueError):
        auth.check_redirect_uri_allowed("http://evil.test/cb")
    with pytest.raises(ValueError):
        auth.check_redirect_uri_allowed("https://a.test/cb#frag")
    with pytest.raises(ValueError):
        auth.check_redirect_uri_allowed("/relative")


def test_is_cimd_client_id() -> None:
    assert auth.is_cimd_client_id("https://chatgpt.com/oauth/client.json")
    assert auth.is_cimd_client_id("https://claude.ai/oauth/claude-code-client-metadata")
    assert not auth.is_cimd_client_id("https://claude.ai")
    assert not auth.is_cimd_client_id("https://claude.ai/")
    assert not auth.is_cimd_client_id("http://claude.ai/x")
    assert not auth.is_cimd_client_id("some-uuid")


def test_public_base_url_env_and_headers(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("AXLE_MCP_PUBLIC_URL", raising=False)
    scope = {
        "scheme": "http",
        "headers": [(b"host", b"mcp.example.test"), (b"x-forwarded-proto", b"https")],
    }
    assert auth.public_base_url(scope) == "https://mcp.example.test"
    assert auth.public_base_url({"scheme": "http", "headers": [(b"host", b"localhost:8080")]}) == (
        "http://localhost:8080"
    )
    monkeypatch.setenv("AXLE_MCP_PUBLIC_URL", "https://configured.test/")
    assert auth.public_base_url(scope) == "https://configured.test"


# --- clients -------------------------------------------------------------------


async def test_dcr_public_client_roundtrip(provider: auth.AxleOAuthProvider) -> None:
    from mcp.shared.auth import OAuthClientMetadata

    meta = OAuthClientMetadata.model_validate(
        {
            "redirect_uris": ["https://claude.ai/api/mcp/auth_callback"],
            "client_name": "Claude",
            "token_endpoint_auth_method": "none",
            "grant_types": ["authorization_code", "refresh_token"],
            "response_types": ["code"],
        }
    )
    info = provider.register(meta)
    assert info["client_id"].startswith("axmcp_c_")
    assert "client_secret" not in info
    assert info["token_endpoint_auth_method"] == "none"

    client = await provider.get_client(info["client_id"])
    assert isinstance(client, auth.Client)
    assert client.client_secret is None
    assert client.display_label == "Claude"
    assert str(client.validate_redirect_uri(None)) == "https://claude.ai/api/mcp/auth_callback"
    assert client.validate_scope("anything goes") == ["anything", "goes"]


async def test_dcr_confidential_client_gets_secret(provider: auth.AxleOAuthProvider) -> None:
    from mcp.shared.auth import OAuthClientMetadata

    meta = OAuthClientMetadata.model_validate(
        {
            "redirect_uris": ["https://chatgpt.com/connector/oauth/abc"],
            "token_endpoint_auth_method": "client_secret_post",
        }
    )
    info = provider.register(meta)
    assert info["client_secret"] and info["client_secret_expires_at"] == 0
    client = await provider.get_client(info["client_id"])
    assert client is not None and client.client_secret == info["client_secret"]
    assert "refresh_token" in client.grant_types


def test_dcr_rejects_bad_redirects_and_methods(provider: auth.AxleOAuthProvider) -> None:
    from mcp.shared.auth import OAuthClientMetadata

    with pytest.raises(ValueError, match="redirect"):
        provider.register(
            OAuthClientMetadata.model_validate({"redirect_uris": ["http://evil.test/cb"]})
        )
    with pytest.raises(ValueError, match="token_endpoint_auth_method"):
        provider.register(
            OAuthClientMetadata.model_validate(
                {
                    "redirect_uris": ["https://a.test/cb"],
                    "token_endpoint_auth_method": "private_key_jwt",
                }
            )
        )


async def test_unknown_client_ids(provider: auth.AxleOAuthProvider) -> None:
    assert await provider.get_client("not-a-client") is None
    assert await provider.get_client("axmcp_c_garbage") is None
    # Looks like CIMD but is not https-with-path: not even fetched.
    assert await provider.get_client("https://example.test") is None


async def test_cimd_client(
    provider: auth.AxleOAuthProvider, monkeypatch: pytest.MonkeyPatch
) -> None:
    url = "https://claude.ai/oauth/claude-code-client-metadata"
    doc = {
        "client_id": url,
        "client_name": "Claude Code",
        "redirect_uris": ["http://localhost/callback", "http://127.0.0.1/callback"],
        "token_endpoint_auth_method": "none",
    }
    calls: list[str] = []

    def fake_fetch(u: str) -> Any:
        calls.append(u)
        return doc

    monkeypatch.setattr(auth, "fetch_client_metadata_document", fake_fetch)
    client = await provider.get_client(url)
    assert isinstance(client, auth.Client)
    assert client.client_id == url
    assert client.token_endpoint_auth_method == "none"
    # Self-asserted document: shown by host, not by client_name.
    assert client.display_label == "claude.ai"
    assert str(client.validate_redirect_uri(AnyUrl("http://localhost:3118/callback"))) == (
        "http://localhost:3118/callback"
    )
    # Cached.
    await provider.get_client(url)
    assert calls == [url]


async def test_cimd_rejects_mismatched_document(
    provider: auth.AxleOAuthProvider, monkeypatch: pytest.MonkeyPatch
) -> None:
    url = "https://client.test/oauth/client.json"
    monkeypatch.setattr(
        auth,
        "fetch_client_metadata_document",
        lambda u: {
            "client_id": "https://other.test/x",
            "redirect_uris": ["https://client.test/cb"],
        },
    )
    assert await provider.get_client(url) is None
    monkeypatch.setattr(
        auth,
        "fetch_client_metadata_document",
        lambda u: {"client_id": url, "redirect_uris": ["http://not-loopback.test/cb"]},
    )
    assert await provider.get_client(url) is None


# --- tokens & resource-server gate ---------------------------------------------------


async def test_issued_token_resolves_to_api_key(provider: auth.AxleOAuthProvider) -> None:
    tokens = provider._issue_tokens(
        client_id="c", api_key="sk-user", scopes=[], resource=f"{BASE}/mcp"
    )
    assert tokens.expires_in == auth.ACCESS_TOKEN_TTL and tokens.refresh_token
    upstream = await provider.resolve_upstream_authorization(f"Bearer {tokens.access_token}", BASE)
    assert upstream == "Bearer sk-user"
    # Case-insensitive scheme, trailing-slash-insensitive resource.
    assert await provider.resolve_upstream_authorization(
        f"bearer {tokens.access_token}", BASE + "/"
    ) == ("Bearer sk-user")


async def test_issued_token_for_other_resource_rejected(provider: auth.AxleOAuthProvider) -> None:
    tokens = provider._issue_tokens(
        client_id="c", api_key="k", scopes=[], resource="https://other.test/mcp"
    )
    with pytest.raises(auth.AuthFailure) as ei:
        await provider.resolve_upstream_authorization(f"Bearer {tokens.access_token}", BASE)
    assert ei.value.error == "invalid_token"


async def test_missing_and_garbage_tokens(provider: auth.AxleOAuthProvider) -> None:
    with pytest.raises(auth.AuthFailure) as ei:
        await provider.resolve_upstream_authorization(None, BASE)
    assert ei.value.error is None
    with pytest.raises(auth.AuthFailure):
        await provider.resolve_upstream_authorization("Basic abc", BASE)
    with pytest.raises(auth.AuthFailure) as ei:
        await provider.resolve_upstream_authorization("Bearer axmcp_at_bogus", BASE)
    assert ei.value.error == "invalid_token"
    # Refresh tokens are not access tokens.
    tokens = provider._issue_tokens(client_id="c", api_key="k", scopes=[], resource=None)
    with pytest.raises(auth.AuthFailure):
        await provider.resolve_upstream_authorization(f"Bearer {tokens.refresh_token}", BASE)


async def test_raw_api_key_validated_and_cached(
    provider: auth.AxleOAuthProvider, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: list[str] = []

    def fake_verify(api_url: str, key: str) -> bool:
        seen.append(key)
        return key == "good"

    monkeypatch.setattr(auth, "verify_api_key", fake_verify)
    assert await provider.resolve_upstream_authorization("Bearer good", BASE) == "Bearer good"
    assert await provider.resolve_upstream_authorization("Bearer good", BASE) == "Bearer good"
    assert seen == ["good"]  # cached
    with pytest.raises(auth.AuthFailure) as ei:
        await provider.resolve_upstream_authorization("Bearer bad", BASE)
    assert ei.value.error == "invalid_token"
    with pytest.raises(auth.AuthFailure):
        await provider.resolve_upstream_authorization("Bearer has space", BASE)


async def test_raw_api_key_passes_when_axle_unavailable(
    provider: auth.AxleOAuthProvider, monkeypatch: pytest.MonkeyPatch
) -> None:
    def boom(api_url: str, key: str) -> bool:
        raise auth.AxleUnavailable("down")

    monkeypatch.setattr(auth, "verify_api_key", boom)
    assert (
        await provider.resolve_upstream_authorization("Bearer whatever", BASE) == "Bearer whatever"
    )


async def test_authorization_code_single_use(provider: auth.AxleOAuthProvider) -> None:
    from mcp.server.auth.provider import TokenError

    pending = {
        "client_id": "c",
        "redirect_uri": "https://claude.ai/api/mcp/auth_callback",
        "redirect_uri_provided_explicitly": True,
        "scopes": [],
        "code_challenge": "x",
        "resource": None,
    }
    code = provider.issue_authorization_code(pending, "sk")
    client = auth.Client(client_id="c", redirect_uris=[AnyUrl(pending["redirect_uri"])])
    loaded = await provider.load_authorization_code(client, code)
    assert loaded is not None and loaded.api_key == "sk" and loaded.expires_at > time.time()
    tokens = await provider.exchange_authorization_code(client, loaded)
    assert (await provider.load_access_token(tokens.access_token)).api_key == "sk"  # type: ignore[union-attr]
    with pytest.raises(TokenError):
        await provider.exchange_authorization_code(client, loaded)


# --- metadata & 401 shape -------------------------------------------------------------


def test_metadata_documents() -> None:
    m = auth.authorization_server_metadata(BASE)
    assert m["issuer"] == BASE
    assert m["authorization_endpoint"] == f"{BASE}/authorize"
    assert m["token_endpoint"] == f"{BASE}/token"
    assert m["registration_endpoint"] == f"{BASE}/register"
    assert m["code_challenge_methods_supported"] == ["S256"]
    # Claude only picks CIMD when both of these hold.
    assert m["client_id_metadata_document_supported"] is True
    assert "none" in m["token_endpoint_auth_methods_supported"]
    assert m["authorization_response_iss_parameter_supported"] is True
    prm = auth.protected_resource_metadata(BASE)
    assert prm["resource"] == f"{BASE}/mcp"
    assert prm["authorization_servers"] == [BASE]
    assert "scopes_supported" not in prm


def test_www_authenticate_header() -> None:
    assert auth.www_authenticate(BASE) == (
        f'Bearer resource_metadata="{BASE}/.well-known/oauth-protected-resource/mcp"'
    )
    h = auth.www_authenticate(BASE, auth.AuthFailure("invalid_token", "expired"))
    assert h.startswith(
        'Bearer error="invalid_token", error_description="expired", resource_metadata='
    )


async def test_login_page_renders_client_and_redirect_host(codec: auth.TokenCodec) -> None:
    pending = {
        "client_label": "Claude <script>",
        "redirect_uri": "https://claude.ai/api/mcp/auth_callback",
    }
    page = auth.render_login_page("blob", pending)
    assert "Claude &lt;script&gt;" in page
    assert "<code>claude.ai</code>" in page
    assert 'name="req" value="blob"' in page
    assert auth.CONSOLE_URL in page
    assert "AXLE rejected" in auth.render_login_page("blob", pending, "AXLE rejected this API key.")


# --- ASGI app: /mcp gate + discovery -------------------------------------------------


async def _call(
    app: Any,
    method: str,
    path: str,
    headers: list[tuple[bytes, bytes]] | None = None,
    body: bytes = b"",
) -> tuple[int, dict[str, str], bytes]:
    sent: list[dict[str, Any]] = []

    async def receive() -> dict[str, Any]:
        return {"type": "http.request", "body": body, "more_body": False}

    async def send(message: dict[str, Any]) -> None:
        sent.append(message)

    scope = {
        "type": "http",
        "method": method,
        "path": path,
        "raw_path": path.encode(),
        "query_string": b"",
        "scheme": "https",
        "headers": [(b"host", b"mcp.example.test"), *(headers or [])],
        "client": ("203.0.113.9", 1234),
        "server": ("mcp.example.test", 443),
    }
    await app(scope, receive, send)
    start = next(m for m in sent if m["type"] == "http.response.start")
    hdrs = {k.decode(): v.decode() for k, v in start.get("headers", [])}
    out = b"".join(m.get("body", b"") for m in sent if m["type"] == "http.response.body")
    return start["status"], hdrs, out


@pytest.fixture
def app(monkeypatch: pytest.MonkeyPatch) -> Any:
    monkeypatch.setenv("AXLE_MCP_TOKEN_SECRET", "unit-test-secret")
    monkeypatch.delenv("AXLE_MCP_PUBLIC_URL", raising=False)
    monkeypatch.delenv("AXLE_MCP_ALLOW_ANONYMOUS", raising=False)
    return srv._build_http_app()


async def test_mcp_post_without_token_is_401_with_discovery_pointer(app: Any) -> None:
    status, headers, body = await _call(app, "POST", "/mcp", body=b"{}")
    assert status == 401
    assert headers["www-authenticate"] == (
        'Bearer resource_metadata="https://mcp.example.test/.well-known/oauth-protected-resource/mcp"'
    )
    assert json.loads(body)["error"] == "unauthorized"


async def test_mcp_post_with_bad_token_is_401_invalid_token(app: Any) -> None:
    status, headers, _ = await _call(
        app, "POST", "/mcp", headers=[(b"authorization", b"Bearer axmcp_at_nope")], body=b"{}"
    )
    assert status == 401
    assert 'error="invalid_token"' in headers["www-authenticate"]


async def test_anonymous_opt_in(monkeypatch: pytest.MonkeyPatch) -> None:
    from mcp.server.streamable_http_manager import StreamableHTTPSessionManager

    monkeypatch.setenv("AXLE_MCP_ALLOW_ANONYMOUS", "1")
    monkeypatch.setenv("AXLE_MCP_TOKEN_SECRET", "s")
    reached: list[str | None] = []

    async def fake_handle(self: Any, scope: Any, receive: Any, send: Any) -> None:
        reached.append(srv._request_authorization.get())
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b""})

    monkeypatch.setattr(StreamableHTTPSessionManager, "handle_request", fake_handle)
    app = srv._build_http_app()
    status, _, _ = await _call(app, "POST", "/mcp", body=b"{}")
    assert status == 200
    assert reached == [None]  # forwarded upstream with no Authorization at all
    # Credentials, when present, are still checked.
    status, _, _ = await _call(
        app, "POST", "/mcp", headers=[(b"authorization", b"Bearer axmcp_at_nope")], body=b"{}"
    )
    assert status == 401


async def test_discovery_endpoints(app: Any) -> None:
    for path in (
        "/.well-known/oauth-protected-resource",
        "/.well-known/oauth-protected-resource/mcp",
    ):
        status, headers, body = await _call(app, "GET", path)
        assert status == 200, path
        doc = json.loads(body)
        assert doc["resource"] == "https://mcp.example.test/mcp"
        assert doc["authorization_servers"] == ["https://mcp.example.test"]
    status, _, body = await _call(app, "GET", "/.well-known/oauth-authorization-server")
    assert status == 200
    doc = json.loads(body)
    assert doc["issuer"] == "https://mcp.example.test"
    assert doc["token_endpoint"] == "https://mcp.example.test/token"


async def test_discovery_honours_public_url_env(app: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AXLE_MCP_PUBLIC_URL", "https://mcp.axiommath.ai")
    _, _, body = await _call(app, "GET", "/.well-known/oauth-protected-resource/mcp")
    assert json.loads(body)["resource"] == "https://mcp.axiommath.ai/mcp"
    status, headers, _ = await _call(app, "POST", "/mcp", body=b"{}")
    assert "https://mcp.axiommath.ai/.well-known" in headers["www-authenticate"]


async def test_cors_preflight_on_token_endpoint(app: Any) -> None:
    status, headers, _ = await _call(
        app,
        "OPTIONS",
        "/token",
        headers=[
            (b"origin", b"http://localhost:6274"),
            (b"access-control-request-method", b"POST"),
            (b"access-control-request-headers", b"content-type"),
        ],
    )
    assert status == 200
    assert headers["access-control-allow-origin"] == "*"


async def test_health_reports_auth(app: Any) -> None:
    status, _, body = await _call(app, "GET", "/")
    assert status == 200
    doc = json.loads(body)
    assert doc["auth"] == "oauth"
    assert doc["token_secret"] == "configured"


async def test_login_get_with_bad_blob(app: Any) -> None:
    status, _, body = await _call(app, "GET", "/login")
    assert status == 400
    assert b"not valid" in body


# --- hardening regressions ---------------------------------------------------------------


def test_canonical_resource_never_raises() -> None:
    assert auth.canonical_resource("https://host:notaport/mcp") == "https://host:notaport/mcp"
    assert auth.canonical_resource("https://[::1/mcp") == "https://[::1/mcp"
    assert isinstance(auth.canonical_resource(""), str)


def test_prune_keeps_fresh_entries_under_flood() -> None:
    cache: dict[str, tuple[float, Any]] = {"fresh": (10_000.0, True)}
    for i in range(20):
        cache[f"junk{i}"] = (10_000.0 + i, False)
    auth._prune(cache, now=0.0, max_size=10)
    assert "fresh" not in cache  # oldest dropped first ...
    assert len(cache) == 9  # ... but the cache is never wiped wholesale
    cache = {"expired": (0.0, True), "fresh": (10_000.0, True)}
    auth._prune(cache, now=5.0, max_size=2)
    assert list(cache) == ["fresh"]


async def test_cimd_negative_cache(
    provider: auth.AxleOAuthProvider, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[str] = []

    def bad_fetch(u: str) -> Any:
        calls.append(u)
        raise ValueError("nope")

    monkeypatch.setattr(auth, "fetch_client_metadata_document", bad_fetch)
    url = "https://attacker.test/oauth/client.json"
    assert await provider.get_client(url) is None
    assert await provider.get_client(url) is None
    assert calls == [url]


def test_cimd_fetch_refuses_redirects(monkeypatch: pytest.MonkeyPatch) -> None:
    import http.server
    import threading

    class Redirector(http.server.BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            self.send_response(302)
            self.send_header("Location", "http://127.0.0.1:1/internal")
            self.end_headers()

        def log_message(self, *a: Any) -> None:
            pass

    httpd = http.server.HTTPServer(("127.0.0.1", 0), Redirector)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        monkeypatch.setattr(auth, "_assert_public_host", lambda host: None)
        with pytest.raises(ValueError, match="redirect"):
            auth.fetch_client_metadata_document(f"http://127.0.0.1:{httpd.server_port}/client.json")
    finally:
        httpd.shutdown()


async def test_oversized_oauth_bodies_are_rejected(app: Any) -> None:
    big = b"{" + b" " * (200 * 1024) + b"}"
    status, _, _ = await _call(
        app,
        "POST",
        "/register",
        headers=[
            (b"content-type", b"application/json"),
            (b"content-length", str(len(big)).encode()),
        ],
        body=big,
    )
    assert status == 413


# --- second review pass ------------------------------------------------------------------


def test_append_query_keeps_blank_and_existing_params() -> None:
    out = auth.append_query(
        "https://app.test/cb?ctx=&flag&keep=1", code="C", state=None, iss="https://i"
    )
    q = urllib.parse.parse_qsl(urllib.parse.urlsplit(out).query, keep_blank_values=True)
    assert q == [("ctx", ""), ("flag", ""), ("keep", "1"), ("code", "C"), ("iss", "https://i")]


def test_register_requires_redirect_uris(provider: auth.AxleOAuthProvider) -> None:
    from mcp.shared.auth import OAuthClientMetadata

    with pytest.raises(ValueError, match="redirect_uris"):
        provider.register(OAuthClientMetadata.model_validate({"redirect_uris": None}))


async def test_register_null_redirect_uris_is_400_not_500(app: Any) -> None:
    body = json.dumps({"redirect_uris": None, "client_name": "x"}).encode()
    status, _, out = await _call(
        app, "POST", "/register", headers=[(b"content-type", b"application/json")], body=body
    )
    assert status == 400
    assert json.loads(out)["error"] == "invalid_redirect_uri"


def test_verify_api_key_maps_http_client_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    import http.client

    def boom(*a: Any, **k: Any) -> Any:
        raise http.client.RemoteDisconnected("gone")

    monkeypatch.setattr(auth.urllib.request, "urlopen", boom)
    with pytest.raises(auth.AxleUnavailable):
        auth.verify_api_key("https://axle.invalid", "k")


async def test_refresh_does_not_extend_session(provider: auth.AxleOAuthProvider) -> None:
    client = auth.Client(client_id="c", redirect_uris=[AnyUrl("https://a.test/cb")])
    first = provider._issue_tokens(client_id="c", api_key="k", scopes=[], resource=None)
    rt1 = await provider.load_refresh_token(client, first.refresh_token or "")
    assert rt1 is not None
    second = await provider.exchange_refresh_token(client, rt1, [])
    rt2 = await provider.load_refresh_token(client, second.refresh_token or "")
    assert rt2 is not None and rt2.expires_at is not None and rt1.expires_at is not None
    assert rt2.expires_at <= rt1.expires_at
    assert second.access_token != first.access_token


async def test_raw_key_fail_open_is_cached(
    provider: auth.AxleOAuthProvider, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[str] = []

    def boom(api_url: str, key: str) -> bool:
        calls.append(key)
        raise auth.AxleUnavailable("down")

    monkeypatch.setattr(auth, "verify_api_key", boom)
    await provider.resolve_upstream_authorization("Bearer k1", BASE)
    await provider.resolve_upstream_authorization("Bearer k1", BASE)
    assert calls == ["k1"]


async def test_login_page_cannot_be_framed(app: Any) -> None:
    status, headers, _ = await _call(app, "GET", "/login")
    assert status == 400
    assert headers["x-frame-options"] == "DENY"
    assert "frame-ancestors 'none'" in headers["content-security-policy"]
    assert headers["referrer-policy"] == "no-referrer"


async def test_mcp_cors_preflight_and_exposed_401(app: Any) -> None:
    status, headers, _ = await _call(
        app,
        "OPTIONS",
        "/mcp",
        headers=[
            (b"origin", b"https://inspector.test"),
            (b"access-control-request-method", b"POST"),
        ],
    )
    assert status == 204
    assert headers["access-control-allow-origin"] == "*"
    assert "Authorization" in headers["access-control-allow-headers"]
    status, headers, _ = await _call(app, "POST", "/mcp", body=b"{}")
    assert status == 401
    assert headers["access-control-allow-origin"] == "*"
    assert "WWW-Authenticate" in headers["access-control-expose-headers"]


def test_forwarded_host_is_not_trusted(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("AXLE_MCP_PUBLIC_URL", raising=False)
    scope = {
        "scheme": "https",
        "headers": [(b"host", b"real.test"), (b"x-forwarded-host", b"evil.test")],
    }
    assert auth.public_base_url(scope) == "https://real.test"
