from __future__ import annotations

import json
from unittest.mock import AsyncMock, patch

import pytest

from axle_mcp_server.server import handle_call_tool


async def test_unknown_tool_raises() -> None:
    with pytest.raises(ValueError, match="Unknown tool"):
        await handle_call_tool("nonexistent_tool", {})


async def test_check_fills_default_environment() -> None:
    mock = AsyncMock(return_value={"okay": True, "content": "..."})
    with patch("axle_mcp_server.server._call_endpoint", mock):
        result = await handle_call_tool("check", {"content": "theorem x : 1 = 1 := rfl"})

    mock.assert_called_once_with(
        "check",
        {"content": "theorem x : 1 = 1 := rfl", "environment": "lean-4.28.0"},
    )
    assert json.loads(result[0].text)["okay"] is True


async def test_check_respects_explicit_environment() -> None:
    mock = AsyncMock(return_value={"okay": True})
    with patch("axle_mcp_server.server._call_endpoint", mock):
        await handle_call_tool("check", {"content": "x", "environment": "lean-4.21.0"})

    mock.assert_called_once_with(
        "check", {"content": "x", "environment": "lean-4.21.0"}
    )


async def test_strips_none_values() -> None:
    mock = AsyncMock(return_value={"okay": True})
    with patch("axle_mcp_server.server._call_endpoint", mock):
        await handle_call_tool("check", {"content": "x", "names": None})

    assert "names" not in mock.call_args[0][1]


async def test_list_environments_tool() -> None:
    result = await handle_call_tool("list_environments", {})
    parsed = json.loads(result[0].text)
    assert len(parsed) == 2
    assert parsed[0]["name"] == "lean-4.27.0"


async def test_none_arguments_defaults_to_empty() -> None:
    result = await handle_call_tool("list_environments", None)
    assert len(result) == 1
