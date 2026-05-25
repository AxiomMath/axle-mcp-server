from __future__ import annotations

import pathlib
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

import axle_mcp_server.server as srv
from axle_mcp_server.server import _build_tool_defs, handle_call_tool


def test_schema_injects_file_uri_for_content_endpoints() -> None:
    defs = _build_tool_defs(
        {
            "check": {
                "inputs": [
                    {"name": "content", "type": "textarea", "required": True},
                    {"name": "environment", "type": "text", "required": True},
                ]
            }
        },
        "lean-4.28.0",
    )
    schema = next(t.inputSchema for t in defs if t.name == "check")
    assert "file_uri" in schema["properties"]
    assert schema["properties"]["file_uri"]["type"] == "string"
    assert schema["properties"]["file_uri"]["format"] == "uri"
    assert schema["oneOf"] == [
        {"required": ["content"]},
        {"required": ["file_uri"]},
    ]
    assert "content" not in schema.get("required", [])


def test_schema_omits_file_uri_for_non_content_endpoints() -> None:
    defs = _build_tool_defs(
        {
            "merge": {
                "inputs": [
                    {"name": "documents", "type": "textarea_list", "required": True},
                ]
            }
        },
        "lean-4.28.0",
    )
    schema = next(t.inputSchema for t in defs if t.name == "merge")
    assert "file_uri" not in schema["properties"]
    assert "oneOf" not in schema


async def test_file_uri_substitutes_into_content(tmp_path: pathlib.Path) -> None:
    f = tmp_path / "p.lean"
    f.write_text("theorem x : 1 = 1 := rfl")
    mock = AsyncMock(return_value={"okay": True})
    with patch("axle_mcp_server.server._call_endpoint", mock):
        await handle_call_tool("check", {"file_uri": f.as_uri()})

    sent: dict[str, Any] = mock.call_args[0][1]
    assert sent["content"] == "theorem x : 1 = 1 := rfl"
    assert "file_uri" not in sent
    assert sent["environment"] == "lean-4.28.0"


async def test_file_uri_accepts_bare_absolute_path(tmp_path: pathlib.Path) -> None:
    f = tmp_path / "p.lean"
    f.write_text("ok")
    mock = AsyncMock(return_value={"okay": True})
    with patch("axle_mcp_server.server._call_endpoint", mock):
        await handle_call_tool("check", {"file_uri": str(f)})

    assert mock.call_args[0][1]["content"] == "ok"


async def test_rejects_both_content_and_file_uri(tmp_path: pathlib.Path) -> None:
    f = tmp_path / "p.lean"
    f.write_text("y")
    with pytest.raises(ValueError, match="exactly one"):
        await handle_call_tool(
            "check", {"content": "x", "file_uri": f.as_uri()}
        )


async def test_rejects_missing_file() -> None:
    with pytest.raises(ValueError, match="regular file"):
        await handle_call_tool(
            "check", {"file_uri": "file:///definitely/does/not/exist.lean"}
        )


async def test_rejects_directory(tmp_path: pathlib.Path) -> None:
    with pytest.raises(ValueError, match="regular file"):
        await handle_call_tool("check", {"file_uri": tmp_path.as_uri()})


async def test_rejects_empty_string() -> None:
    with pytest.raises(ValueError, match="non-empty"):
        await handle_call_tool("check", {"file_uri": ""})


async def test_rejects_file_outside_declared_roots(
    tmp_path: pathlib.Path,
) -> None:
    inside = tmp_path / "inside"
    inside.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    f = outside / "p.lean"
    f.write_text("x")
    with patch(
        "axle_mcp_server.server._client_roots",
        AsyncMock(return_value=[inside.resolve()]),
    ):
        with pytest.raises(ValueError, match="outside.*roots"):
            await handle_call_tool("check", {"file_uri": f.as_uri()})


async def test_accepts_file_inside_declared_roots(
    tmp_path: pathlib.Path,
) -> None:
    f = tmp_path / "p.lean"
    f.write_text("ok")
    mock = AsyncMock(return_value={"okay": True})
    with patch(
        "axle_mcp_server.server._client_roots",
        AsyncMock(return_value=[tmp_path.resolve()]),
    ), patch("axle_mcp_server.server._call_endpoint", mock):
        await handle_call_tool("check", {"file_uri": f.as_uri()})

    assert mock.call_args[0][1]["content"] == "ok"


async def test_empty_roots_list_denies_all(tmp_path: pathlib.Path) -> None:
    f = tmp_path / "p.lean"
    f.write_text("x")
    with patch(
        "axle_mcp_server.server._client_roots", AsyncMock(return_value=[])
    ):
        with pytest.raises(ValueError, match="outside.*roots"):
            await handle_call_tool("check", {"file_uri": f.as_uri()})


async def test_http_mode_rejects_file_uri(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(srv._config, "http_mode", True)
    f = tmp_path / "p.lean"
    f.write_text("x")
    with pytest.raises(ValueError, match="stdio mode"):
        await handle_call_tool("check", {"file_uri": f.as_uri()})
