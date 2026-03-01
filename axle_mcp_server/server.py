"""MCP Server for AXLE (https://axle.axiommath.ai/).

Copyright (c) 2026 Axiom Math. MIT License.
"""
from __future__ import annotations

import asyncio
import importlib.metadata
import json
import logging
import os
import urllib.request
from typing import Any, Final, TypedDict

import mcp.server.stdio
import mcp.types as types
from mcp.server import Server

logger = logging.getLogger(__name__)

VERSION: Final[str] = importlib.metadata.version("axiom-axle-mcp")
AXLE_API_URL: Final[str] = os.environ.get("AXLE_API_URL", "https://axle.axiommath.ai")
AXLE_API_KEY: Final[str | None] = os.environ.get("AXLE_API_KEY")

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
    if AXLE_API_KEY:
        h["Authorization"] = f"Bearer {AXLE_API_KEY}"
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


ENDPOINTS: Final[dict[str, Any]] = _fetch_json("/v1/endpoints")
ENVIRONMENTS: Final[list[dict[str, Any]]] = _fetch_json("/v1/environments")
DEFAULT_ENVIRONMENT: Final[str] = ENVIRONMENTS[-1]["name"]
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


async def _amain() -> None:
    async with mcp.server.stdio.stdio_server() as (read_stream, write_stream):
        await server.run(
            read_stream,
            write_stream,
            server.create_initialization_options(),
        )


def main() -> None:
    asyncio.run(_amain())


if __name__ == "__main__":
    main()
