from __future__ import annotations

from typing import Any

from axle_mcp_server.server import build_input_schema


def test_required_fields() -> None:
    inputs: list[dict[str, Any]] = [
        {"name": "content", "type": "textarea", "required": True},
        {"name": "names", "type": "list", "required": False},
    ]
    assert build_input_schema(inputs)["required"] == ["content"]


def test_environment_gets_default() -> None:
    inputs: list[dict[str, Any]] = [
        {"name": "environment", "type": "text", "required": True},
    ]
    schema = build_input_schema(inputs, default_environment="lean-4.28.0")
    assert schema["properties"]["environment"]["default"] == "lean-4.28.0"
    assert "environment" not in schema.get("required", [])


def test_no_required_key_when_empty() -> None:
    inputs: list[dict[str, Any]] = [{"name": "x", "type": "text", "required": False}]
    assert "required" not in build_input_schema(inputs)
