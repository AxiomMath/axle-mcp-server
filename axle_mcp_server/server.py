"""MCP Server for AXLE (https://axle.axiommath.ai/).

Copyright (c) 2026 Axiom Math. MIT License.
"""
from __future__ import annotations

import argparse
import asyncio
import contextvars
import importlib.metadata
import json
import logging
import os
import pathlib
import re
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Final, TypedDict

import mcp.server.stdio
import mcp.types as types
from mcp.server import Server

logger = logging.getLogger(__name__)

VERSION: Final[str] = importlib.metadata.version("axiom-axle-mcp")
AXLE_API_URL: Final[str] = os.environ.get("AXLE_API_URL", "https://axle.axiommath.ai")
AXLE_API_KEY: Final[str | None] = os.environ.get("AXLE_API_KEY")

# Populated per HTTP request by the streamable-HTTP wrapper. Unused in stdio mode,
# where authentication comes from the AXLE_API_KEY env var instead.
_request_authorization: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "axle_request_authorization", default=None
)
_request_client_ip: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "axle_request_client_ip", default=None
)


# Process config, set once by main(). Alternative: a _build_server builder
# whose handlers close over the flag.
class _Config:
    http_mode: bool = False


_config = _Config()

TYPE_MAP: Final[dict[str, dict[str, Any]]] = {
    "text": {"type": "string"},
    "textarea": {"type": "string"},
    "textarea_list": {"type": "array", "items": {"type": "string"}},
    "number": {"type": "number"},
    "checkbox": {"type": "boolean"},
    "list": {"type": "array", "items": {"type": "string"}},
    "dict": {"type": "object"},
}


class InputField(TypedDict, total=False):
    name: str
    type: str
    description: str
    default: Any
    required: bool
    cli_list_type: str


def _headers() -> dict[str, str]:
    h: dict[str, str] = {"X-Request-Source": f"axiom-axle-mcp/{VERSION}"}
    # Per-request auth (HTTP mode) wins over the stdio env-var fallback.
    auth = _request_authorization.get()
    if auth is None and AXLE_API_KEY:
        auth = f"Bearer {AXLE_API_KEY}"
    if auth:
        h["Authorization"] = auth
    client_ip = _request_client_ip.get()
    if client_ip:
        # AXLE may use this to attribute anonymous requests to end-user IPs
        # when our egress is on its trusted-proxy list.
        h["X-Forwarded-For"] = client_ip
    return h


def _fetch_json(path: str) -> Any:
    """GET a JSON resource from the AXLE API (sync, used at startup)."""
    url = f"{AXLE_API_URL}{path}"
    req = urllib.request.Request(url, headers=_headers())
    with urllib.request.urlopen(req) as resp:
        if resp.status != 200:
            body = resp.read().decode()
            raise RuntimeError(f"AXLE API error: {resp.status} {body}")
        return json.loads(resp.read().decode())


async def _call_endpoint(name: str, request: dict[str, Any]) -> Any:
    """POST to an AXLE endpoint and return the parsed JSON response."""

    def _do() -> Any:
        url = f"{AXLE_API_URL}/api/v1/{name}"
        data = json.dumps(request).encode()
        headers = {**_headers(), "Content-Type": "application/json"}
        req = urllib.request.Request(url, data=data, headers=headers, method="POST")
        with urllib.request.urlopen(req) as resp:
            body = resp.read().decode()
            if resp.status != 200:
                raise RuntimeError(f"AXLE API error: {resp.status} {body}")
            result = json.loads(body)
        for key in ("internal_error", "user_error", "error"):
            if key in result:
                raise RuntimeError(f"AXLE error ({key}): {result[key]}")
        return result

    return await asyncio.to_thread(_do)


def _make_share_url(tool_name: str, request_id: str) -> str:
    """Webapp URL that rehydrates the form + result for a saved request."""
    return f"{AXLE_API_URL}/{tool_name}#r={request_id}"


async def _post_shared_link(request_id: str) -> dict[str, Any]:
    """POST /v1/shared-links to make a request shareable via a permanent URL."""

    def _do() -> dict[str, Any]:
        url = f"{AXLE_API_URL}/v1/shared-links"
        data = json.dumps({"request_id": request_id}).encode()
        headers = {**_headers(), "Content-Type": "application/json"}
        req = urllib.request.Request(url, data=data, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req) as resp:
                body = resp.read().decode()
                if resp.status != 201:
                    raise RuntimeError(f"AXLE shared-links error: {resp.status} {body}")
                parsed = json.loads(body)
                assert isinstance(parsed, dict)
                return parsed
        except urllib.error.HTTPError as e:
            err_body = e.read().decode(errors="replace")
            raise RuntimeError(f"AXLE shared-links error: {e.code} {err_body}") from None

    return await asyncio.to_thread(_do)


async def _get_shared_link(request_id: str) -> dict[str, Any]:
    """GET /v1/shared-links/{request_id} to look up the saved tool_name + payload."""

    def _do() -> dict[str, Any]:
        url = f"{AXLE_API_URL}/v1/shared-links/{request_id}"
        req = urllib.request.Request(url, headers=_headers())
        try:
            with urllib.request.urlopen(req) as resp:
                body = resp.read().decode()
                if resp.status != 200:
                    raise RuntimeError(f"AXLE shared-links error: {resp.status} {body}")
                parsed = json.loads(body)
                assert isinstance(parsed, dict)
                return parsed
        except urllib.error.HTTPError as e:
            err_body = e.read().decode(errors="replace")
            raise RuntimeError(f"AXLE shared-links error: {e.code} {err_body}") from None

    return await asyncio.to_thread(_do)


DOCS_PATH: Final[str] = "/v1/docs"
DOCS_ALL_PATH: Final[str] = f"{DOCS_PATH}/all.json"


class DocPage(TypedDict):
    slug: str
    title: str
    html_url: str
    markdown: str


_docs_cache: list[DocPage] | None = None
_docs_lock: Final[asyncio.Lock] = asyncio.Lock()


async def _load_docs() -> list[DocPage]:
    """Load the docs corpus once per process."""
    global _docs_cache
    if _docs_cache is not None:
        return _docs_cache
    async with _docs_lock:
        if _docs_cache is None:
            _docs_cache = await asyncio.to_thread(_fetch_doc_pages)
        return _docs_cache


def _fetch_doc_pages() -> list[DocPage]:
    try:
        payload = _fetch_json(DOCS_ALL_PATH)
    except urllib.error.HTTPError as e:
        hint = " (these docs require an API key)" if e.code in (401, 403) else ""
        raise RuntimeError(
            f"Could not fetch AXLE docs from {AXLE_API_URL}{DOCS_ALL_PATH}: "
            f"{e.code} {e.reason}{hint}"
        ) from None
    return _pages_from_bundle(payload)


def _pages_from_bundle(payload: Any) -> list[DocPage]:
    """Normalize {version, pages: [{slug, title, html_url, markdown}]}, nav order."""
    raw = payload.get("pages") if isinstance(payload, dict) else None
    if not isinstance(raw, list) or not raw:
        raise RuntimeError(f"{AXLE_API_URL}{DOCS_ALL_PATH} returned no pages")
    return [
        DocPage(
            slug=str(entry.get("slug", "")).strip("/"),
            title=str(entry.get("title") or entry.get("slug") or "index"),
            html_url=str(entry.get("html_url", "")),
            markdown=str(entry.get("markdown", "")).strip(),
        )
        for entry in raw
        if isinstance(entry, dict)
    ]


def _find_doc_page(ref: str, pages: list[DocPage]) -> DocPage:
    """Resolve a slug, a bare tool name, or a docs URL onto a page."""
    key = ref.strip().split(f"{DOCS_PATH}/", 1)[-1]
    key = key.split("#")[0].strip("/").removesuffix(".md").lower() or "index"
    for page in pages:
        if (page["slug"].lower() or "index") in (key, f"tools/{key}"):
            return page
    known = ", ".join(p["slug"] for p in pages)
    raise ValueError(f"Unknown docs page {ref!r}. Available pages: {known}")


def _render_doc_index(pages: list[DocPage]) -> str:
    lines = ["# AXLE documentation index", ""]
    lines += [f"- `{p['slug']}` — {p['title']}" for p in pages]
    return "\n".join(lines)


def _render_doc_page(page: DocPage) -> str:
    source = f"Source: {page['html_url']}\n\n" if page["html_url"] else ""
    return f"{source}{page['markdown']}"


def _has_textarea_content(inputs: list[InputField]) -> bool:
    return any(
        f.get("name") == "content" and f.get("type") == "textarea" for f in inputs
    )


def _inject_file_uri(schema: dict[str, Any]) -> dict[str, Any]:
    """Add `file_uri` as an optional alternative to `content`.

    The "exactly one of content/file_uri" rule is enforced server-side in
    `_resolve_file_uri`, not via a top-level oneOf: the Anthropic Messages API
    (and Vertex via OpenRouter) reject oneOf/anyOf/allOf in a tool's
    input_schema outright.
    """
    schema["properties"]["file_uri"] = {
        "type": "string",
        "format": "uri",
        "description": (
            "file:// URI or absolute path. The server reads the file locally "
            "and sends it as `content`. Provide exactly one of `content` or "
            "`file_uri`. Stdio mode only."
        ),
    }
    # content is no longer required; file_uri can satisfy the call instead.
    required = [r for r in schema.get("required", []) if r != "content"]
    if required:
        schema["required"] = required
    else:
        schema.pop("required", None)
    return schema


def _uri_to_path(uri: str) -> pathlib.Path:
    """Accept a file:// URI or a bare absolute path. Resolves symlinks."""
    if uri.startswith("file://"):
        parsed = urllib.parse.urlparse(uri)
        return pathlib.Path(urllib.request.url2pathname(parsed.path)).resolve()
    return pathlib.Path(uri).resolve()


async def _client_roots() -> list[pathlib.Path] | None:
    """Resolve the client's declared MCP roots.

    Returns None if the client has no roots capability (unconstrained),
    [] if the capability is declared but no roots are set (deny all),
    or the list of declared roots otherwise.
    """
    try:
        session = server.request_context.session
    except LookupError:
        return None
    if not session.check_client_capability(
        types.ClientCapabilities(roots=types.RootsCapability())
    ):
        return None
    result = await session.list_roots()
    return [_uri_to_path(str(r.uri)) for r in result.roots]


async def _resolve_file_uri(
    tool_schema: dict[str, Any], arguments: dict[str, Any]
) -> None:
    """Replace file_uri in arguments with content read from disk. In-place.

    Enforces "exactly one of content/file_uri" for content endpoints, since the
    schema no longer encodes it (see `_inject_file_uri`).
    """
    accepts_file_uri = "file_uri" in tool_schema.get("properties", {})
    uri = arguments.pop("file_uri", None)
    if uri is None:
        if accepts_file_uri and arguments.get("content") is None:
            raise ValueError("provide exactly one of content or file_uri")
        return
    if not accepts_file_uri:
        raise ValueError("file_uri is not accepted by this tool")
    if _config.http_mode:
        raise ValueError("file_uri is only supported in stdio mode")
    if not isinstance(uri, str) or not uri:
        raise ValueError("file_uri must be a non-empty string")
    if arguments.get("content") is not None:
        raise ValueError("provide exactly one of content or file_uri")

    path = _uri_to_path(uri)
    if not path.is_file():
        raise ValueError(f"file_uri does not point to a regular file: {uri}")

    roots = await _client_roots()
    if roots is not None and not any(
        path == r or r in path.parents for r in roots
    ):
        raise ValueError(
            f"file_uri is outside the client's declared MCP roots: {uri}"
        )

    arguments["content"] = await asyncio.to_thread(path.read_text)


def field_to_json_schema(field: InputField) -> dict[str, Any]:
    field_type = field.get("type", "text")
    schema = dict(TYPE_MAP.get(field_type, {"type": "string"}))

    if field_type == "list" and field.get("cli_list_type") == "int":
        schema["items"] = {"type": "integer"}

    if "description" in field:
        schema["description"] = field["description"]

    if "default" in field:
        schema["default"] = field["default"]

    return schema


def build_input_schema(
    inputs: list[InputField], default_environment: str | None = None
) -> dict[str, Any]:
    properties: dict[str, Any] = {}
    required: list[str] = []

    for field in inputs:
        name = field["name"]
        prop = field_to_json_schema(field)

        if name == "environment" and default_environment:
            prop["default"] = default_environment

        properties[name] = prop

        if field.get("required") and name != "environment":
            required.append(name)

    schema: dict[str, Any] = {
        "type": "object",
        "properties": properties,
    }
    if required:
        schema["required"] = required
    return schema


# ChatGPT treats tools without readOnlyHint as write actions and asks the user to
# confirm every call. AXLE tools are pure computations over the Lean code they're given.
_READ_ONLY: Final[types.ToolAnnotations] = types.ToolAnnotations(
    readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=False
)


def _build_tool_defs(
    endpoints: dict[str, Any], default_environment: str
) -> list[types.Tool]:
    tools: list[types.Tool] = []
    for name, meta in endpoints.items():
        inputs = meta.get("inputs", [])
        schema = build_input_schema(inputs, default_environment)
        if _has_textarea_content(inputs):
            schema = _inject_file_uri(schema)
        tools.append(
            types.Tool(
                name=name,
                description=meta.get("description", name),
                inputSchema=schema,
                annotations=_READ_ONLY,
            )
        )
    tools.append(
        types.Tool(
            name="read_docs",
            description=(
                "Read the AXLE documentation. Consult this before using an AXLE tool "
                "on anything non-trivial: each tool has a docs page covering its input "
                "semantics, output shape, Lean conventions and failure modes. Call with "
                "no arguments for the page index, then with page='verify_proof' (or any "
                "slug from that index) to read a page. Returns markdown."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "page": {
                        "type": "string",
                        "description": (
                            "Page slug from the index, e.g. 'quickstart', "
                            "'troubleshooting', 'tools/verify_proof' (bare tool names "
                            "like 'verify_proof' also work). Omit for the index."
                        ),
                    },
                },
            },
            annotations=_READ_ONLY,
        )
    )
    tools.append(
        types.Tool(
            name="list_environments",
            description="List available Lean environments on the AXLE server.",
            inputSchema={"type": "object", "properties": {}},
            annotations=_READ_ONLY,
        )
    )
    tools.append(
        types.Tool(
            name="share_url",
            description=(
                "Generate a permanent shareable webapp URL for an AXLE verification. "
                "Call this when the user asks for a link they can open or share to "
                "inspect a prior tool's inputs and result. Pass the request_id from "
                "that tool's response (info.request_id) and the tool_name. Returns "
                "{share_url, request_id, tool_name, saved_at}."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "request_id": {
                        "type": "string",
                        "description": "UUID returned in info.request_id from a prior tool call.",
                    },
                    "tool_name": {
                        "type": "string",
                        "description": (
                            "The tool that produced the request (e.g. 'verify_proof'). "
                            "Optional — omit and the server will look it up."
                        ),
                    },
                },
                "required": ["request_id"],
            },
            annotations=types.ToolAnnotations(
                readOnlyHint=False, destructiveHint=False, idempotentHint=True, openWorldHint=False
            ),
        )
    )
    tools.append(
        types.Tool(
            name="read_share_url",
            description=(
                "Fetch the inputs and result of a previously shared AXLE verification. "
                "Call this when the user gives you a share URL (or bare request_id) and "
                "asks what it contains. Accepts either a full webapp URL like "
                "'https://axle.axiommath.ai/verify_proof#r=<uuid>' or just the UUID. "
                "Returns {request_id, tool_name, inputs, result, state, created_at}."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "share_url": {
                        "type": "string",
                        "description": (
                            "A share URL produced by share_url, or a bare request_id UUID."
                        ),
                    },
                },
                "required": ["share_url"],
            },
            annotations=_READ_ONLY,
        )
    )
    return tools


def _default_environment(environments: list[dict[str, Any]]) -> str:
    """Select the latest lean-4.{minor}.{micro} environment by version."""
    pattern = re.compile(r"^lean-4\.(\d+)\.(\d+)$")
    best_name: str | None = None
    best_version: tuple[int, int] = (-1, -1)
    for env in environments:
        m = pattern.match(env["name"])
        if m:
            version = (int(m.group(1)), int(m.group(2)))
            if version > best_version:
                best_version = version
                best_name = env["name"]
    if best_name is None:
        return str(environments[-1]["name"])
    return best_name


def _resolve_default_environment(environments: list[dict[str, Any]]) -> str:
    """Pick the default environment, honoring `AXLE_DEFAULT_ENVIRONMENT`.

    If the env var names a registered environment (e.g. `pnt-4.26.0`), use
    it. Otherwise fall back to `_default_environment`'s latest stable
    `lean-4.X.Y`. An unknown override raises so typos fail loudly at
    startup rather than silently routing to the auto-picked env.
    """
    override = (os.environ.get("AXLE_DEFAULT_ENVIRONMENT") or "").strip()
    if not override:
        return _default_environment(environments)
    known = {e["name"] for e in environments}
    if override not in known:
        raise RuntimeError(
            f"AXLE_DEFAULT_ENVIRONMENT={override!r} is not a registered "
            f"environment. Known: {sorted(known)}"
        )
    return override


ENDPOINTS: Final[dict[str, Any]] = _fetch_json("/v1/endpoints")
ENVIRONMENTS: Final[list[dict[str, Any]]] = _fetch_json("/v1/environments")
DEFAULT_ENVIRONMENT: Final[str] = _resolve_default_environment(ENVIRONMENTS)
TOOL_DEFS: Final[list[types.Tool]] = _build_tool_defs(ENDPOINTS, DEFAULT_ENVIRONMENT)
BUILTIN_TOOLS: Final[set[str]] = {
    "read_docs",
    "list_environments",
    "share_url",
    "read_share_url",
}
ENDPOINT_NAMES: Final[set[str]] = {t.name for t in TOOL_DEFS} - BUILTIN_TOOLS

_UUID_RE: Final[re.Pattern[str]] = re.compile(
    r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", re.IGNORECASE
)


def _extract_request_id(share_url: str) -> str | None:
    """Pull the first UUID-shaped substring out of a URL or bare ID."""
    m = _UUID_RE.search(share_url)
    return m.group(0) if m else None

SERVER_INSTRUCTIONS: Final[str] = (
    "AXLE (Axiom Lean Engine) checks and manipulates Lean 4 code. Use verify_proof to "
    "check a candidate proof against a sorried theorem statement, check to compile any "
    "Lean source and collect messages, and read_docs before non-trivial use. Every tool "
    "is read-only apart from share_url, which creates a shareable link."
)

server = Server("axle", version=VERSION, instructions=SERVER_INSTRUCTIONS)


@server.list_tools()
async def handle_list_tools() -> list[types.Tool]:
    return list(TOOL_DEFS)


@server.call_tool()
async def handle_call_tool(
    name: str, arguments: dict[str, Any] | None
) -> list[types.TextContent]:
    if arguments is None:
        arguments = {}

    if name == "read_docs":
        pages = await _load_docs()
        page_arg = arguments.get("page")
        if isinstance(page_arg, str) and page_arg.strip():
            text = _render_doc_page(_find_doc_page(page_arg, pages))
        else:
            text = _render_doc_index(pages)
        return [types.TextContent(type="text", text=text)]

    if name == "list_environments":
        return [types.TextContent(type="text", text=json.dumps(ENVIRONMENTS, indent=2))]

    if name == "share_url":
        request_id = arguments.get("request_id")
        if not isinstance(request_id, str) or not request_id:
            raise ValueError("share_url requires a non-empty request_id")
        tool_name = arguments.get("tool_name")
        saved = await _post_shared_link(request_id)
        if not isinstance(tool_name, str) or not tool_name:
            tool_name = (await _get_shared_link(request_id)).get("tool_name")
        payload = {
            "share_url": _make_share_url(tool_name, request_id) if tool_name else None,
            "request_id": request_id,
            "tool_name": tool_name,
            "saved_at": saved.get("saved_at"),
        }
        return [types.TextContent(type="text", text=json.dumps(payload, indent=2))]

    if name == "read_share_url":
        raw = arguments.get("share_url")
        if not isinstance(raw, str) or not raw:
            raise ValueError("read_share_url requires a non-empty share_url")
        request_id = _extract_request_id(raw)
        if request_id is None:
            raise ValueError(
                "read_share_url could not find a request_id UUID in the input"
            )
        fetched = await _get_shared_link(request_id)
        payload = {
            k: fetched.get(k)
            for k in ("request_id", "tool_name", "inputs", "result", "state", "created_at")
        }
        return [types.TextContent(type="text", text=json.dumps(payload, indent=2))]

    if name not in ENDPOINT_NAMES:
        raise ValueError(f"Unknown tool: {name}")

    tool_schema = next(t.inputSchema for t in TOOL_DEFS if t.name == name)
    await _resolve_file_uri(tool_schema, arguments)

    if "environment" not in arguments:
        arguments["environment"] = DEFAULT_ENVIRONMENT

    request = {k: v for k, v in arguments.items() if v is not None}

    result = await _call_endpoint(name, request)

    return [types.TextContent(type="text", text=json.dumps(result, indent=2))]


async def _stdio_main() -> None:
    async with mcp.server.stdio.stdio_server() as (read_stream, write_stream):
        await server.run(
            read_stream,
            write_stream,
            server.create_initialization_options(),
        )


def _extract_request_context(scope: Any) -> tuple[str | None, str | None]:
    """Pull Authorization + first X-Forwarded-For hop from an ASGI scope."""
    authorization: str | None = None
    client_ip: str | None = None
    for name, value in scope.get("headers", []):
        lname = name.decode("latin-1").lower()
        if lname == "authorization":
            authorization = value.decode("latin-1")
        elif lname == "x-forwarded-for" and client_ip is None:
            client_ip = value.decode("latin-1").split(",", 1)[0].strip() or None
    if client_ip is None:
        client = scope.get("client")
        if client:
            client_ip = client[0]
    return authorization, client_ip


def _build_http_app() -> Any:
    """Construct the ASGI app that serves MCP over streamable HTTP.

    Routes:
      POST /mcp                  MCP endpoint. Requires `Authorization: Bearer <token>`
                                 where <token> is either an access token issued by this
                                 server's OAuth flow or a raw AXLE API key.
      GET  /.well-known/...      OAuth discovery (RFC 9728 + RFC 8414).
      /authorize /token /register /login   OAuth 2.1 authorization server (see auth.py).
      GET  /                     Health.
    """
    from contextlib import asynccontextmanager

    from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
    from mcp.server.transport_security import RequestBodyLimitMiddleware
    from starlette.applications import Starlette
    from starlette.middleware import Middleware
    from starlette.middleware.cors import CORSMiddleware
    from starlette.requests import Request
    from starlette.responses import JSONResponse
    from starlette.routing import Route

    from . import auth

    session_manager = StreamableHTTPSessionManager(
        app=server,
        event_store=None,
        stateless=True,
    )
    codec = auth.TokenCodec(os.environ.get("AXLE_MCP_TOKEN_SECRET"))
    # Read AXLE_API_URL lazily so tests (and the login page) follow the live value.
    provider = auth.AxleOAuthProvider(codec, axle_api_url=lambda: AXLE_API_URL)
    allow_anonymous = os.environ.get("AXLE_MCP_ALLOW_ANONYMOUS", "").lower() in ("1", "true", "yes")

    cors_headers = [
        (b"access-control-allow-origin", b"*"),
        (b"access-control-expose-headers", b"WWW-Authenticate, Mcp-Session-Id, Mcp-Protocol-Version"),
    ]

    def with_cors(send: Any) -> Any:
        """Add permissive CORS headers so browser-based MCP clients can call /mcp."""

        async def _send(message: dict[str, Any]) -> None:
            if message.get("type") == "http.response.start":
                message = {**message, "headers": [*message.get("headers", []), *cors_headers]}
            await send(message)

        return _send

    async def preflight(send: Any) -> None:
        await send({
            "type": "http.response.start",
            "status": 204,
            "headers": [
                *cors_headers,
                (b"access-control-allow-methods", b"POST, OPTIONS"),
                (b"access-control-allow-headers", b"Authorization, Content-Type, Accept, Mcp-Session-Id, Mcp-Protocol-Version"),
                (b"access-control-max-age", b"86400"),
            ],
        })
        await send({"type": "http.response.body", "body": b""})

    async def handle_mcp(scope: Any, receive: Any, send: Any) -> None:
        send = with_cors(send)
        authorization, client_ip = _extract_request_context(scope)
        base_url = auth.public_base_url(scope)
        if authorization is None and allow_anonymous:
            upstream_authorization: str | None = None
        else:
            try:
                upstream_authorization = await provider.resolve_upstream_authorization(
                    authorization, base_url
                )
            except auth.AuthFailure as failure:
                await auth.send_unauthorized(send, base_url, failure)
                return
        auth_token = _request_authorization.set(upstream_authorization)
        ip_token = _request_client_ip.set(client_ip)
        try:
            await session_manager.handle_request(scope, receive, send)
        finally:
            _request_authorization.reset(auth_token)
            _request_client_ip.reset(ip_token)

    async def health(request: Request) -> JSONResponse:
        base = auth.public_base_url(request.scope)
        return JSONResponse(
            {
                "service": "axle-mcp-server",
                "version": VERSION,
                "upstream": AXLE_API_URL,
                "mcp_endpoint": "/mcp",
                "auth": "oauth" if not allow_anonymous else "oauth-or-anonymous",
                "resource_metadata": f"{base}{auth.PRM_MCP_PATH}",
                "authorization_server_metadata": f"{base}{auth.AS_METADATA_PATH}",
                "token_secret": "ephemeral" if codec.ephemeral else "configured",
            }
        )

    @asynccontextmanager
    async def lifespan(_app: Starlette) -> Any:
        async with session_manager.run():
            yield

    starlette_app: Any = Starlette(
        routes=[Route("/", health, methods=["GET"]), *auth.build_routes(provider)],
        lifespan=lifespan,
        # Browser-based OAuth clients (e.g. MCP Inspector) fetch discovery metadata and
        # call /token and /register cross-origin.
        middleware=[
            Middleware(
                CORSMiddleware,
                allow_origins=["*"],
                allow_methods=["GET", "POST", "OPTIONS"],
                allow_headers=["Authorization", "Content-Type", "Mcp-Protocol-Version"],
            )
        ],
    )
    # OAuth requests (registration JSON, token/login forms) are tiny; refuse anything big.
    starlette_app = RequestBodyLimitMiddleware(starlette_app, 64 * 1024)

    async def reject_stream(send: Any) -> None:
        """405 the GET/SSE stream, which the spec allows in place of a stream.

        Stateless mode builds a fresh transport per request, so a GET stream has
        no session to receive server-initiated messages and stays open emitting
        nothing. Cloud Run withholds the response headers until the first body
        byte, so a client sees silence rather than an idle stream and waits until
        it times out. 405 tells it to use POST instead.
        """
        await send({
            "type": "http.response.start",
            "status": 405,
            "headers": [(b"content-type", b"text/plain; charset=utf-8"), (b"allow", b"POST")],
        })
        await send({"type": "http.response.body", "body": b"POST to /mcp; SSE stream unsupported"})

    # ASGI wrapper: route /mcp (with or without trailing slash) straight to the
    # MCP handler. Skipping Starlette's Mount avoids a 307 redirect on /mcp
    # that Claude's web connector doesn't follow on POST.
    async def app(scope: Any, receive: Any, send: Any) -> None:
        if scope.get("type") != "http":
            await starlette_app(scope, receive, send)
            return
        base_token = auth.bind_base_url(scope)
        try:
            if scope.get("path") in ("/mcp", "/mcp/"):
                if scope.get("method") == "OPTIONS":
                    await preflight(send)
                    return
                if scope.get("method") in ("GET", "HEAD"):
                    await reject_stream(send)
                    return
                await handle_mcp(scope, receive, send)
                return
            await starlette_app(scope, receive, send)
        finally:
            auth.unbind_base_url(base_token)

    return app


def _http_main(host: str, port: int) -> None:
    import uvicorn

    app = _build_http_app()
    # Behind Cloud Run / any TLS-terminating proxy the public scheme+host arrive in
    # X-Forwarded-* headers; trust them so OAuth metadata advertises https URLs.
    uvicorn.run(
        app, host=host, port=port, log_level="info", proxy_headers=True, forwarded_allow_ips="*"
    )


def main() -> None:
    parser = argparse.ArgumentParser(prog="axle-mcp-server")
    parser.add_argument(
        "--http",
        action="store_true",
        help="Serve MCP over streamable HTTP instead of stdio (for hosted deployments).",
    )
    parser.add_argument(
        "--host",
        default="0.0.0.0",
        help="HTTP bind address (default: 0.0.0.0). Ignored in stdio mode.",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=int(os.environ.get("PORT", "8080")),
        help="HTTP port (default: $PORT or 8080). Ignored in stdio mode.",
    )
    args = parser.parse_args()

    _config.http_mode = args.http

    if args.http:
        _http_main(args.host, args.port)
    else:
        asyncio.run(_stdio_main())


if __name__ == "__main__":
    main()
