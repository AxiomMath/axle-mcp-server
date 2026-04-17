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
import re
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
        # when our Cloud Run egress is on its trusted-proxy list.
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


def _build_tool_defs(endpoints: dict[str, Any], default_environment: str) -> list[types.Tool]:
    tools = [
        types.Tool(
            name=name,
            description=meta.get("description", name),
            inputSchema=build_input_schema(meta.get("inputs", []), default_environment),
        )
        for name, meta in endpoints.items()
    ]
    tools.append(
        types.Tool(
            name="list_environments",
            description="List available Lean environments on the AXLE server.",
            inputSchema={"type": "object", "properties": {}},
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
        return environments[-1]["name"]
    return best_name


ENDPOINTS: Final[dict[str, Any]] = _fetch_json("/v1/endpoints")
ENVIRONMENTS: Final[list[dict[str, Any]]] = _fetch_json("/v1/environments")
DEFAULT_ENVIRONMENT: Final[str] = _default_environment(ENVIRONMENTS)
TOOL_DEFS: Final[list[types.Tool]] = _build_tool_defs(ENDPOINTS, DEFAULT_ENVIRONMENT)
ENDPOINT_NAMES: Final[set[str]] = {t.name for t in TOOL_DEFS} - {"list_environments"}

server = Server("axle")


@server.list_tools()
async def handle_list_tools() -> list[types.Tool]:
    return list(TOOL_DEFS)


@server.call_tool()
async def handle_call_tool(
    name: str, arguments: dict[str, Any] | None
) -> list[types.TextContent]:
    if arguments is None:
        arguments = {}

    if name == "list_environments":
        return [types.TextContent(type="text", text=json.dumps(ENVIRONMENTS, indent=2))]

    if name not in ENDPOINT_NAMES:
        raise ValueError(f"Unknown tool: {name}")

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
    """Construct the Starlette ASGI app that serves MCP over streamable HTTP."""
    from contextlib import asynccontextmanager

    from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
    from starlette.applications import Starlette
    from starlette.requests import Request
    from starlette.responses import JSONResponse
    from starlette.routing import Mount, Route

    session_manager = StreamableHTTPSessionManager(
        app=server,
        event_store=None,
        stateless=True,
    )

    async def handle_mcp(scope: Any, receive: Any, send: Any) -> None:
        authorization, client_ip = _extract_request_context(scope)
        auth_token = _request_authorization.set(authorization)
        ip_token = _request_client_ip.set(client_ip)
        try:
            await session_manager.handle_request(scope, receive, send)
        finally:
            _request_authorization.reset(auth_token)
            _request_client_ip.reset(ip_token)

    async def health(_request: Request) -> JSONResponse:
        return JSONResponse(
            {
                "service": "axle-mcp-server",
                "version": VERSION,
                "upstream": AXLE_API_URL,
                "mcp_endpoint": "/mcp",
            }
        )

    @asynccontextmanager
    async def lifespan(_app: Starlette) -> Any:
        async with session_manager.run():
            yield

    return Starlette(
        routes=[
            Route("/", health, methods=["GET"]),
            Mount("/mcp", app=handle_mcp),
        ],
        lifespan=lifespan,
    )


def _http_main(host: str, port: int) -> None:
    import uvicorn

    app = _build_http_app()
    uvicorn.run(app, host=host, port=port, log_level="info")


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

    if args.http:
        _http_main(args.host, args.port)
    else:
        asyncio.run(_stdio_main())


if __name__ == "__main__":
    main()
