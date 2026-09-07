"""OAuth 2.1 authorization server for HTTP mode.

Claude.ai and ChatGPT can only attach an OAuth token to a remote MCP server, so /login
asks for the user's AXLE API key and seals it into the tokens we issue; each MCP request
unseals it and forwards it upstream. Every artifact (client id, code, tokens, pending
login) is a Fernet blob keyed by AXLE_MCP_TOKEN_SECRET, so no state is stored anywhere.
"""

from __future__ import annotations

import base64
import contextvars
import hashlib
import html
import http.client
import ipaddress
import json
import logging
import os
import secrets
import socket
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Final

import anyio
from cryptography.fernet import Fernet, InvalidToken
from mcp.server.auth.handlers.authorize import AuthorizationHandler
from mcp.server.auth.handlers.token import TokenHandler
from mcp.server.auth.middleware.client_auth import ClientAuthenticator
from mcp.server.auth.provider import (
    AccessToken,
    AuthorizationCode,
    AuthorizationParams,
    RefreshToken,
    TokenError,
)
from mcp.shared.auth import (
    InvalidRedirectUriError,
    OAuthClientInformationFull,
    OAuthClientMetadata,
    OAuthToken,
)
from pydantic import AnyUrl, ValidationError
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from starlette.routing import Route

from axle_mcp_server.server import VERSION

logger = logging.getLogger(__name__)

MCP_PATH: Final[str] = "/mcp"
LOGIN_PATH: Final[str] = "/login"
AUTHORIZATION_PATH: Final[str] = "/authorize"
TOKEN_PATH: Final[str] = "/token"
REGISTRATION_PATH: Final[str] = "/register"
AS_METADATA_PATH: Final[str] = "/.well-known/oauth-authorization-server"
PRM_PATH: Final[str] = "/.well-known/oauth-protected-resource"
PRM_MCP_PATH: Final[str] = PRM_PATH + MCP_PATH

ACCESS_TOKEN_TTL: Final[int] = 60 * 60
REFRESH_TOKEN_TTL: Final[int] = 90 * 24 * 60 * 60
AUTH_CODE_TTL: Final[int] = 10 * 60
LOGIN_REQUEST_TTL: Final[int] = 30 * 60
CIMD_CACHE_TTL: Final[int] = 5 * 60
CIMD_NEGATIVE_CACHE_TTL: Final[int] = 60
RAW_KEY_OK_TTL: Final[int] = 5 * 60
RAW_KEY_BAD_TTL: Final[int] = 60
RAW_KEY_UNAVAILABLE_TTL: Final[int] = 30

DOCS_URL: Final[str] = "https://github.com/AxiomMath/axle-mcp-server"
CONSOLE_URL: Final[str] = "https://axle.axiommath.ai/app/console"
_USER_AGENT: Final[str] = f"axiom-axle-mcp/{VERSION}"

# Bound per request by the ASGI wrapper; the SDK's authorize handler gives the provider
# no request object, so this is how authorize() learns the public origin.
current_base_url: contextvars.ContextVar[str] = contextvars.ContextVar("axle_mcp_base_url")


def public_base_url(scope: Any) -> str:
    configured = os.environ.get("AXLE_MCP_PUBLIC_URL", "").strip()
    if configured:
        return configured.rstrip("/")
    headers = {
        k.decode("latin-1").lower(): v.decode("latin-1") for k, v in scope.get("headers", [])
    }
    scheme = headers.get("x-forwarded-proto", scope.get("scheme") or "http")
    scheme = scheme.split(",", 1)[0].strip() or "http"
    # Host is set by the proxy; X-Forwarded-Host is client-controlled and not trusted.
    host = (headers.get("host") or "").split(",", 1)[0].strip()
    if not host:
        server = scope.get("server")
        host = f"{server[0]}:{server[1]}" if server else "localhost"
    return f"{scheme}://{host}"


def canonical_resource(url: str) -> str:
    """RFC 8707 form: lowercase scheme/host, no default port, no trailing slash. Never raises."""
    url = url.strip()
    try:
        p = urllib.parse.urlsplit(url)
        port = p.port
    except ValueError:
        return url.lower().rstrip("/")
    scheme = p.scheme.lower()
    host = (p.hostname or "").lower()
    if ":" in host:
        host = f"[{host}]"
    if port and not ((scheme == "https" and port == 443) or (scheme == "http" and port == 80)):
        host = f"{host}:{port}"
    return f"{scheme}://{host}{p.path.rstrip('/')}"


def _prune(cache: dict[str, tuple[float, Any]], now: float, max_size: int) -> None:
    # Expired first, then oldest: a flood of junk keys must not evict everything at once.
    if len(cache) < max_size:
        return
    for key in [k for k, (exp, _) in cache.items() if exp <= now]:
        del cache[key]
    while len(cache) >= max_size:
        del cache[next(iter(cache))]


_LOOPBACK_HOSTS: Final[frozenset[str]] = frozenset({"localhost", "127.0.0.1", "::1"})


def is_loopback_redirect(url: str) -> bool:
    p = urllib.parse.urlsplit(url)
    return p.scheme == "http" and (p.hostname or "").lower() in _LOOPBACK_HOSTS


def redirect_uri_matches(registered: str, requested: str) -> bool:
    if registered == requested:
        return True
    # RFC 8252 §7.3: native clients (Claude Code) bind an ephemeral loopback port.
    if not (is_loopback_redirect(registered) and is_loopback_redirect(requested)):
        return False
    a, b = urllib.parse.urlsplit(registered), urllib.parse.urlsplit(requested)
    return (
        (a.hostname or "").lower() == (b.hostname or "").lower()
        and a.path == b.path
        and a.query == b.query
    )


def check_redirect_uri_allowed(url: str) -> None:
    p = urllib.parse.urlsplit(url)
    if not p.scheme:
        raise ValueError(f"redirect URI has no scheme: {url}")
    if p.fragment:
        raise ValueError(f"redirect URI must not have a fragment: {url}")
    if p.scheme == "http" and not is_loopback_redirect(url):
        raise ValueError(f"http redirect URIs are only allowed for loopback hosts: {url}")


def append_query(url: str, **params: str | None) -> str:
    parts = urllib.parse.urlsplit(url)
    query = urllib.parse.parse_qsl(parts.query, keep_blank_values=True)
    query += [(k, v) for k, v in params.items() if v is not None]
    return urllib.parse.urlunsplit(parts._replace(query=urllib.parse.urlencode(query)))


def is_cimd_client_id(client_id: str) -> bool:
    p = urllib.parse.urlsplit(client_id)
    return p.scheme == "https" and bool(p.netloc) and p.path not in ("", "/")


class TokenCodec:
    """Sealed JSON payloads as `axmcp_<kind>_<fernet>`; the prefix tells our tokens from raw keys."""

    PREFIX: Final[str] = "axmcp"

    def __init__(self, secret: str | None) -> None:
        self.ephemeral = not secret
        if not secret:
            secret = secrets.token_urlsafe(32)
            logger.warning(
                "AXLE_MCP_TOKEN_SECRET is not set: tokens will not survive a restart "
                "or be accepted by other instances."
            )
        self._fernet = Fernet(base64.urlsafe_b64encode(hashlib.sha256(secret.encode()).digest()))

    def seal(self, kind: str, payload: dict[str, Any], ttl: int | None) -> str:
        body = dict(payload)
        body["jti"] = secrets.token_urlsafe(16)
        if ttl is not None:
            body["exp"] = int(time.time()) + ttl
        raw = json.dumps(body, separators=(",", ":")).encode()
        return f"{self.PREFIX}_{kind}_{self._fernet.encrypt(raw).decode()}"

    def open(self, kind: str, token: str) -> dict[str, Any] | None:
        prefix = f"{self.PREFIX}_{kind}_"
        if not token.startswith(prefix):
            return None
        try:
            payload = json.loads(self._fernet.decrypt(token[len(prefix) :].encode()))
        except (InvalidToken, ValueError):
            return None
        if not isinstance(payload, dict):
            return None
        exp = payload.get("exp")
        if exp is not None and exp < time.time():
            return None
        return payload

    def looks_issued(self, token: str) -> bool:
        return token.startswith(f"{self.PREFIX}_")


class Client(OAuthClientInformationFull):
    display_label: str = "an MCP client"

    def validate_redirect_uri(self, redirect_uri: AnyUrl | None) -> AnyUrl:
        registered = [str(u) for u in self.redirect_uris or []]
        if redirect_uri is None:
            if len(registered) == 1:
                return AnyUrl(registered[0])
            raise InvalidRedirectUriError("redirect_uri is required")
        requested = str(redirect_uri)
        if any(redirect_uri_matches(r, requested) for r in registered):
            return redirect_uri
        raise InvalidRedirectUriError(f"Redirect URI '{requested}' not registered for client")

    def validate_scope(self, requested_scope: str | None) -> list[str] | None:
        # No scopes are defined; grant whatever was asked for.
        return requested_scope.split() if requested_scope else None


class AxleAuthorizationCode(AuthorizationCode):
    api_key: str
    jti: str


class AxleRefreshToken(RefreshToken):
    api_key: str


class AxleAccessToken(AccessToken):
    api_key: str


class AxleUnavailable(Exception):
    pass


def verify_api_key(api_url: str, api_key: str) -> bool:
    """Blocking. Raises AxleUnavailable when AXLE could not say yes or no."""
    req = urllib.request.Request(
        f"{api_url}/v1/environments",
        headers={"Authorization": f"Bearer {api_key}", "X-Request-Source": _USER_AGENT},
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return bool(200 <= resp.status < 300)
    except urllib.error.HTTPError as e:
        if e.code in (401, 403):
            return False
        raise AxleUnavailable(f"AXLE returned HTTP {e.code}") from None
    except (urllib.error.URLError, TimeoutError, OSError, http.client.HTTPException) as e:
        raise AxleUnavailable(str(e)) from None


def _api_key_is_wellformed(api_key: str) -> bool:
    # Must be safe to put in an HTTP header.
    return (
        0 < len(api_key) <= 512
        and api_key.isascii()
        and api_key.isprintable()
        and " " not in api_key
    )


def _assert_public_host(hostname: str) -> None:
    try:
        infos = socket.getaddrinfo(hostname, 443, proto=socket.IPPROTO_TCP)
    except socket.gaierror as e:
        raise ValueError(f"cannot resolve {hostname}: {e}") from None
    for info in infos:
        if not ipaddress.ip_address(info[4][0]).is_global:
            raise ValueError(f"{hostname} resolves to a non-public address")


class _NoRedirects(urllib.request.HTTPRedirectHandler):
    # A redirect could point the fetch at an internal address after the host check.
    def redirect_request(
        self, req: Any, fp: Any, code: int, msg: str, headers: Any, newurl: str
    ) -> Any:
        raise ValueError(f"client metadata document redirected ({code})")


_cimd_opener = urllib.request.build_opener(_NoRedirects)


def fetch_client_metadata_document(url: str) -> Any:
    """Blocking GET of a CIMD document: public host only, no redirects, 64 KiB cap."""
    _assert_public_host(urllib.parse.urlsplit(url).hostname or "")
    req = urllib.request.Request(
        url, headers={"Accept": "application/json", "User-Agent": _USER_AGENT}
    )
    try:
        with _cimd_opener.open(req, timeout=5) as resp:
            raw = resp.read(64 * 1024 + 1)
    except urllib.error.HTTPError as e:
        raise ValueError(f"client metadata document returned HTTP {e.code}") from None
    if len(raw) > 64 * 1024:
        raise ValueError("client metadata document is too large")
    return json.loads(raw)


def client_from_cimd(url: str, doc: Any) -> Client:
    if not isinstance(doc, dict):
        raise ValueError("client metadata document is not a JSON object")
    if doc.get("client_id") != url:
        raise ValueError("client metadata document client_id does not match its URL")
    redirect_uris = doc.get("redirect_uris")
    if not isinstance(redirect_uris, list) or not redirect_uris:
        raise ValueError("client metadata document has no redirect_uris")
    for uri in redirect_uris:
        if not isinstance(uri, str):
            raise ValueError("redirect_uris must be strings")
        check_redirect_uri_allowed(uri)
    client_name = doc.get("client_name")
    return Client(
        client_id=url,
        redirect_uris=[AnyUrl(u) for u in redirect_uris],
        token_endpoint_auth_method="none",
        client_name=client_name if isinstance(client_name, str) else None,
        # The document is self-asserted, so show where it is hosted rather than its name.
        display_label=urllib.parse.urlsplit(url).hostname or url,
    )


@dataclass
class AuthFailure(Exception):
    error: str | None = None
    description: str | None = None


class AxleOAuthProvider:
    def __init__(self, codec: TokenCodec, axle_api_url: Callable[[], str]) -> None:
        self.codec = codec
        self.axle_api_url = axle_api_url
        self._used_codes: dict[str, tuple[float, None]] = {}
        self._cimd_cache: dict[str, tuple[float, Client | None]] = {}
        self._raw_key_cache: dict[str, tuple[float, bool]] = {}
        self._lock = threading.Lock()
        # /authorize is unauthenticated; cap the outbound fetches it can trigger.
        self._fetch_limit = anyio.Semaphore(4)

    def register(self, metadata: OAuthClientMetadata) -> dict[str, Any]:
        redirect_uris = [str(u) for u in metadata.redirect_uris or []]
        if not redirect_uris:
            raise ValueError("redirect_uris is required")
        for uri in redirect_uris:
            check_redirect_uri_allowed(uri)
        method = metadata.token_endpoint_auth_method or "none"
        if method not in ("none", "client_secret_post", "client_secret_basic"):
            raise ValueError(f"unsupported token_endpoint_auth_method: {method}")
        if "authorization_code" not in metadata.grant_types:
            raise ValueError("grant_types must include authorization_code")
        if "code" not in metadata.response_types:
            raise ValueError("response_types must include code")
        secret = secrets.token_hex(32) if method != "none" else None
        record: dict[str, Any] = {
            "redirect_uris": redirect_uris,
            "token_endpoint_auth_method": method,
            # We always issue refresh tokens, and the SDK checks the grant against this list.
            "grant_types": sorted({*metadata.grant_types, "refresh_token"}),
            "response_types": metadata.response_types,
            "client_name": metadata.client_name,
            "scope": metadata.scope,
            "client_secret": secret,
            "client_id_issued_at": int(time.time()),
        }
        response = {k: v for k, v in record.items() if k != "client_secret" and v is not None}
        response["client_id"] = self.codec.seal("c", record, ttl=None)
        if secret is not None:
            response["client_secret"] = secret
            response["client_secret_expires_at"] = 0
        return response

    async def get_client(self, client_id: str) -> OAuthClientInformationFull | None:
        record = self.codec.open("c", client_id)
        if record is not None:
            try:
                return Client(
                    client_id=client_id,
                    client_secret=record.get("client_secret"),
                    redirect_uris=[AnyUrl(u) for u in record["redirect_uris"]],
                    token_endpoint_auth_method=record["token_endpoint_auth_method"],
                    grant_types=record["grant_types"],
                    response_types=record["response_types"],
                    client_name=record.get("client_name"),
                    scope=record.get("scope"),
                    display_label=record.get("client_name") or "an MCP client",
                )
            except (ValidationError, KeyError, ValueError):
                return None
        if is_cimd_client_id(client_id):
            return await self._get_cimd_client(client_id)
        return None

    async def _get_cimd_client(self, url: str) -> Client | None:
        now = time.time()
        with self._lock:
            cached = self._cimd_cache.get(url)
        if cached and cached[0] > now:
            return cached[1]
        client: Client | None
        try:
            async with self._fetch_limit:
                doc = await anyio.to_thread.run_sync(fetch_client_metadata_document, url)
            client = client_from_cimd(url, doc)
        except (ValueError, OSError, http.client.HTTPException) as e:
            logger.warning("Rejected CIMD client %s: %s", url, e)
            client = None
        with self._lock:
            _prune(self._cimd_cache, now, 1000)
            ttl = CIMD_CACHE_TTL if client is not None else CIMD_NEGATIVE_CACHE_TTL
            self._cimd_cache[url] = (now + ttl, client)
        return client

    # Required by the SDK's provider protocol; nothing is stored, so nothing to do.
    async def register_client(self, client_info: OAuthClientInformationFull) -> None:
        pass

    async def revoke_token(self, token: AxleAccessToken | AxleRefreshToken) -> None:
        pass

    async def authorize(
        self, client: OAuthClientInformationFull, params: AuthorizationParams
    ) -> str:
        pending = {
            "client_id": client.client_id,
            "client_label": getattr(client, "display_label", "an MCP client"),
            "redirect_uri": str(params.redirect_uri),
            "redirect_uri_provided_explicitly": params.redirect_uri_provided_explicitly,
            "state": params.state,
            "scopes": params.scopes or [],
            "code_challenge": params.code_challenge,
            "resource": params.resource,
        }
        blob = self.codec.seal("lr", pending, LOGIN_REQUEST_TTL)
        return f"{current_base_url.get()}{LOGIN_PATH}?{urllib.parse.urlencode({'req': blob})}"

    def issue_authorization_code(self, pending: dict[str, Any], api_key: str) -> str:
        keys = (
            "client_id",
            "redirect_uri",
            "redirect_uri_provided_explicitly",
            "scopes",
            "code_challenge",
            "resource",
        )
        payload = {**{k: pending[k] for k in keys}, "api_key": api_key}
        return self.codec.seal("ac", payload, AUTH_CODE_TTL)

    async def load_authorization_code(
        self, client: OAuthClientInformationFull, authorization_code: str
    ) -> AxleAuthorizationCode | None:
        payload = self.codec.open("ac", authorization_code)
        if payload is None:
            return None
        return AxleAuthorizationCode(
            code=authorization_code,
            jti=payload["jti"],
            scopes=payload["scopes"],
            expires_at=payload["exp"],
            client_id=payload["client_id"],
            code_challenge=payload["code_challenge"],
            redirect_uri=AnyUrl(payload["redirect_uri"]),
            redirect_uri_provided_explicitly=payload["redirect_uri_provided_explicitly"],
            resource=payload.get("resource"),
            api_key=payload["api_key"],
        )

    def _consume_code(self, jti: str, expires_at: float) -> bool:
        # Single use is per process; PKCE covers replay across instances.
        now = time.time()
        with self._lock:
            _prune(self._used_codes, now, 10000)
            if jti in self._used_codes:
                return False
            self._used_codes[jti] = (expires_at, None)
            return True

    async def exchange_authorization_code(
        self, client: OAuthClientInformationFull, authorization_code: AxleAuthorizationCode
    ) -> OAuthToken:
        if not self._consume_code(authorization_code.jti, authorization_code.expires_at):
            raise TokenError("invalid_grant", "authorization code has already been used")
        return self._issue_tokens(
            client_id=str(client.client_id),
            api_key=authorization_code.api_key,
            scopes=authorization_code.scopes,
            resource=authorization_code.resource,
        )

    async def load_refresh_token(
        self, client: OAuthClientInformationFull, refresh_token: str
    ) -> AxleRefreshToken | None:
        payload = self.codec.open("rt", refresh_token)
        if payload is None:
            return None
        return AxleRefreshToken(
            token=refresh_token,
            client_id=payload["client_id"],
            scopes=payload["scopes"],
            expires_at=payload["exp"],
            resource=payload.get("resource"),
            api_key=payload["api_key"],
        )

    async def exchange_refresh_token(
        self,
        client: OAuthClientInformationFull,
        refresh_token: AxleRefreshToken,
        scopes: list[str],
    ) -> OAuthToken:
        # Old refresh tokens cannot be revoked, so the new one keeps the original expiry.
        return self._issue_tokens(
            client_id=str(client.client_id),
            api_key=refresh_token.api_key,
            scopes=scopes,
            resource=refresh_token.resource,
            refresh_expires_at=refresh_token.expires_at,
        )

    async def load_access_token(self, token: str) -> AxleAccessToken | None:
        payload = self.codec.open("at", token)
        if payload is None:
            return None
        return AxleAccessToken(
            token=token,
            client_id=payload["client_id"],
            scopes=payload["scopes"],
            expires_at=payload["exp"],
            resource=payload.get("resource"),
            api_key=payload["api_key"],
        )

    def _issue_tokens(
        self,
        *,
        client_id: str,
        api_key: str,
        scopes: list[str],
        resource: str | None,
        refresh_expires_at: int | None = None,
    ) -> OAuthToken:
        common = {
            "client_id": client_id,
            "api_key": api_key,
            "scopes": scopes,
            "resource": resource,
        }
        refresh_ttl = REFRESH_TOKEN_TTL
        if refresh_expires_at is not None:
            refresh_ttl = max(0, int(refresh_expires_at - time.time()))
        return OAuthToken(
            access_token=self.codec.seal("at", common, ACCESS_TOKEN_TTL),
            token_type="Bearer",
            expires_in=ACCESS_TOKEN_TTL,
            scope=" ".join(scopes) if scopes else None,
            refresh_token=self.codec.seal("rt", common, refresh_ttl),
        )

    async def resolve_upstream_authorization(self, authorization: str | None, base_url: str) -> str:
        """Map the inbound Authorization header to the one sent to AXLE, or raise AuthFailure."""
        if not authorization or not authorization.lower().startswith("bearer "):
            raise AuthFailure()
        token = authorization[7:].strip()
        if not token:
            raise AuthFailure()
        if self.codec.looks_issued(token):
            access = await self.load_access_token(token)
            if access is None:
                raise AuthFailure("invalid_token", "access token is invalid or expired")
            expected = canonical_resource(f"{base_url.rstrip('/')}{MCP_PATH}")
            if access.resource and canonical_resource(access.resource) != expected:
                raise AuthFailure("invalid_token", "token was issued for a different resource")
            return f"Bearer {access.api_key}"
        if not _api_key_is_wellformed(token):
            raise AuthFailure("invalid_token", "malformed bearer token")
        if not await self._raw_key_accepted(token):
            raise AuthFailure("invalid_token", "AXLE rejected this API key")
        return f"Bearer {token}"

    async def _raw_key_accepted(self, api_key: str) -> bool:
        digest = hashlib.sha256(api_key.encode()).hexdigest()
        now = time.time()
        with self._lock:
            cached = self._raw_key_cache.get(digest)
        if cached and cached[0] > now:
            return cached[1]
        try:
            ok = await anyio.to_thread.run_sync(verify_api_key, self.axle_api_url(), api_key)
            ttl = RAW_KEY_OK_TTL if ok else RAW_KEY_BAD_TTL
        except AxleUnavailable as e:
            # Fail open: the forwarded key still has to satisfy AXLE on the tool call.
            logger.warning("Could not validate API key against AXLE: %s", e)
            ok, ttl = True, RAW_KEY_UNAVAILABLE_TTL
        with self._lock:
            _prune(self._raw_key_cache, now, 10000)
            self._raw_key_cache[digest] = (now + ttl, ok)
        return ok


def authorization_server_metadata(base: str) -> dict[str, Any]:
    return {
        "issuer": base,
        "authorization_endpoint": f"{base}{AUTHORIZATION_PATH}",
        "token_endpoint": f"{base}{TOKEN_PATH}",
        "registration_endpoint": f"{base}{REGISTRATION_PATH}",
        "response_types_supported": ["code"],
        "response_modes_supported": ["query"],
        "grant_types_supported": ["authorization_code", "refresh_token"],
        # Claude picks CIMD only if "none" is listed next to client_id_metadata_document_supported.
        "token_endpoint_auth_methods_supported": [
            "none",
            "client_secret_post",
            "client_secret_basic",
        ],
        "code_challenge_methods_supported": ["S256"],
        "client_id_metadata_document_supported": True,
        "authorization_response_iss_parameter_supported": True,
        "service_documentation": DOCS_URL,
    }


def protected_resource_metadata(base: str) -> dict[str, Any]:
    return {
        "resource": f"{base}{MCP_PATH}",
        "authorization_servers": [base],
        "bearer_methods_supported": ["header"],
        "resource_name": "AXLE MCP server",
        "resource_documentation": DOCS_URL,
    }


def www_authenticate(base: str, failure: AuthFailure | None = None) -> str:
    parts: list[str] = []
    if failure and failure.error:
        parts.append(f'error="{failure.error}"')
        if failure.description:
            parts.append(f'error_description="{failure.description}"')
    parts.append(f'resource_metadata="{base}{PRM_MCP_PATH}"')
    return "Bearer " + ", ".join(parts)


async def send_unauthorized(send: Any, base: str, failure: AuthFailure) -> None:
    body = json.dumps(
        {
            "error": failure.error or "unauthorized",
            "error_description": failure.description or "Authentication required",
        }
    ).encode()
    await send(
        {
            "type": "http.response.start",
            "status": 401,
            "headers": [
                (b"content-type", b"application/json"),
                (b"www-authenticate", www_authenticate(base, failure).encode("latin-1")),
                (b"cache-control", b"no-store"),
            ],
        }
    )
    await send({"type": "http.response.body", "body": body})


_PAGE_CSS = """
:root{color-scheme:light dark;--bg:#f6f6f4;--fg:#1a1a1a;--muted:#5c5c5c;--card:#fff;--line:#ddd;--accent:#3b5bdb;--err:#b42318}
@media(prefers-color-scheme:dark){:root{--bg:#141414;--fg:#ececec;--muted:#9a9a9a;--card:#1f1f1f;--line:#333;--accent:#7b93ff;--err:#ff7a70}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);font:15px/1.5 system-ui,-apple-system,Segoe UI,Roboto,sans-serif;display:flex;min-height:100vh;align-items:center;justify-content:center;padding:24px}
.card{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:28px;max-width:460px;width:100%}
h1{font-size:20px;margin:0 0 8px}p{margin:0 0 14px;color:var(--muted)}p.err{color:var(--err)}
label{display:block;font-weight:600;margin-bottom:6px}
input[type=password]{width:100%;padding:10px 12px;border:1px solid var(--line);border-radius:8px;background:transparent;color:var(--fg);font:inherit}
.row{display:flex;gap:10px;margin-top:16px;align-items:center}
button{font:inherit;font-weight:600;padding:10px 16px;border-radius:8px;border:1px solid var(--accent);background:var(--accent);color:#fff;cursor:pointer}
button.secondary{background:transparent;color:var(--fg);border-color:var(--line)}
a{color:var(--accent)}code{font-size:13px}
"""


def _page(title: str, body: str) -> str:
    return (
        '<!doctype html><html lang="en"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        '<meta name="robots" content="noindex">'
        f"<title>{html.escape(title)}</title><style>{_PAGE_CSS}</style></head>"
        f'<body><main class="card">{body}</main></body></html>'
    )


def render_login_page(req_blob: str, pending: dict[str, Any], error: str | None = None) -> str:
    label = html.escape(str(pending.get("client_label") or "an MCP client"))
    redirect_host = html.escape(
        str(urllib.parse.urlsplit(pending["redirect_uri"]).hostname or pending["redirect_uri"])
    )
    error_html = f'<p class="err">{html.escape(error)}</p>' if error else ""
    body = (
        "<h1>Connect AXLE</h1>"
        f"<p><strong>{label}</strong> wants to use AXLE (Axiom Lean Engine) on your behalf. "
        f"After you continue you will be sent back to <code>{redirect_host}</code>.</p>"
        f'<p>Paste your AXLE API key (create one in the <a href="{CONSOLE_URL}" target="_blank" '
        'rel="noopener">AXLE console</a>). It is verified with AXLE and stored only inside the '
        "encrypted token handed to the client; the AI model never sees it.</p>"
        f"{error_html}"
        f'<form method="post" action="{LOGIN_PATH}" autocomplete="off">'
        f'<input type="hidden" name="req" value="{html.escape(req_blob)}">'
        '<label for="api_key">AXLE API key</label>'
        '<input id="api_key" name="api_key" type="password" required autofocus '
        'autocomplete="off" spellcheck="false">'
        '<div class="row">'
        '<button type="submit" name="action" value="allow">Connect</button>'
        '<button type="submit" name="action" value="deny" class="secondary" '
        "formnovalidate>Cancel</button>"
        "</div></form>"
    )
    return _page("Connect AXLE", body)


def render_error_page(message: str) -> str:
    return _page("AXLE sign-in", f"<h1>Sign-in link is not valid</h1><p>{html.escape(message)}</p>")


def build_routes(provider: AxleOAuthProvider) -> list[Route]:
    authorization_handler = AuthorizationHandler(provider)
    token_handler = TokenHandler(provider, ClientAuthenticator(provider))
    no_store = {"Cache-Control": "no-store"}
    # The login page takes a secret: never framed, never leaks its URL onwards.
    page_headers = {
        **no_store,
        "X-Frame-Options": "DENY",
        "Content-Security-Policy": "frame-ancestors 'none'; default-src 'none'; style-src 'unsafe-inline'",
        "Referrer-Policy": "no-referrer",
        "X-Content-Type-Options": "nosniff",
    }

    def base_of(request: Request) -> str:
        return public_base_url(request.scope)

    def expired_link() -> Response:
        return HTMLResponse(
            render_error_page(
                "This sign-in link is missing, expired or was already used. Go back to "
                "your AI client and start the connection again."
            ),
            status_code=400,
            headers=page_headers,
        )

    def login_page(
        blob: str, pending: dict[str, Any], error: str | None = None, status: int = 200
    ) -> Response:
        return HTMLResponse(
            render_login_page(blob, pending, error), status_code=status, headers=page_headers
        )

    async def as_metadata(request: Request) -> Response:
        return JSONResponse(
            authorization_server_metadata(base_of(request)),
            headers={"Cache-Control": "public, max-age=300"},
        )

    async def prm(request: Request) -> Response:
        return JSONResponse(
            protected_resource_metadata(base_of(request)),
            headers={"Cache-Control": "public, max-age=300"},
        )

    async def register(request: Request) -> Response:
        try:
            metadata = OAuthClientMetadata.model_validate(await request.json())
        except (ValidationError, ValueError) as e:
            return JSONResponse(
                {"error": "invalid_client_metadata", "error_description": str(e)[:500]},
                status_code=400,
            )
        try:
            info = provider.register(metadata)
        except ValueError as e:
            error = "invalid_redirect_uri" if "redirect" in str(e) else "invalid_client_metadata"
            return JSONResponse({"error": error, "error_description": str(e)}, status_code=400)
        return JSONResponse(info, status_code=201, headers=no_store)

    async def login_get(request: Request) -> Response:
        blob = request.query_params.get("req") or ""
        pending = provider.codec.open("lr", blob)
        return expired_link() if pending is None else login_page(blob, pending)

    async def login_post(request: Request) -> Response:
        form = await request.form()
        blob = str(form.get("req") or "")
        pending = provider.codec.open("lr", blob)
        if pending is None:
            return expired_link()
        redirect_uri = pending["redirect_uri"]
        state = pending.get("state")
        if form.get("action") == "deny":
            return RedirectResponse(
                append_query(
                    redirect_uri,
                    error="access_denied",
                    error_description="User cancelled",
                    state=state,
                ),
                status_code=302,
                headers=no_store,
            )
        api_key = str(form.get("api_key") or "").strip()
        if not _api_key_is_wellformed(api_key):
            return login_page(blob, pending, "Please paste a valid AXLE API key.", 400)
        try:
            ok = await anyio.to_thread.run_sync(verify_api_key, provider.axle_api_url(), api_key)
        except AxleUnavailable as e:
            logger.warning("Login: AXLE unavailable while verifying key: %s", e)
            return login_page(
                blob,
                pending,
                "AXLE could not be reached to verify the key. Try again in a moment.",
                502,
            )
        if not ok:
            return login_page(
                blob,
                pending,
                "AXLE rejected this API key. Check it in the console and try again.",
                400,
            )
        code = provider.issue_authorization_code(pending, api_key)
        return RedirectResponse(
            append_query(redirect_uri, code=code, state=state, iss=base_of(request)),
            status_code=302,
            headers=no_store,
        )

    return [
        Route(AS_METADATA_PATH, as_metadata, methods=["GET"]),
        Route(PRM_PATH, prm, methods=["GET"]),
        Route(PRM_MCP_PATH, prm, methods=["GET"]),
        Route(REGISTRATION_PATH, register, methods=["POST"]),
        Route(AUTHORIZATION_PATH, authorization_handler.handle, methods=["GET", "POST"]),
        Route(TOKEN_PATH, token_handler.handle, methods=["POST"]),
        Route(LOGIN_PATH, login_get, methods=["GET"]),
        Route(LOGIN_PATH, login_post, methods=["POST"]),
    ]
