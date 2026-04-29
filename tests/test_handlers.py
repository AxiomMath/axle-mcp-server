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


async def test_share_url_with_explicit_tool_name() -> None:
    post_mock = AsyncMock(return_value={"saved_at": "T"})
    get_mock = AsyncMock()
    with patch("axle_mcp_server.server._post_shared_link", post_mock), patch(
        "axle_mcp_server.server._get_shared_link", get_mock
    ):
        result = await handle_call_tool(
            "share_url", {"request_id": "rid-1", "tool_name": "verify_proof"}
        )

    post_mock.assert_called_once_with("rid-1")
    get_mock.assert_not_called()
    parsed = json.loads(result[0].text)
    assert parsed["share_url"].endswith("/verify_proof#r=rid-1")
    assert parsed["saved_at"] == "T"


async def test_share_url_looks_up_tool_name_when_omitted() -> None:
    post_mock = AsyncMock(return_value={"saved_at": "T"})
    get_mock = AsyncMock(return_value={"tool_name": "merge"})
    with patch("axle_mcp_server.server._post_shared_link", post_mock), patch(
        "axle_mcp_server.server._get_shared_link", get_mock
    ):
        result = await handle_call_tool("share_url", {"request_id": "rid-2"})

    get_mock.assert_called_once_with("rid-2")
    parsed = json.loads(result[0].text)
    assert parsed["share_url"].endswith("/merge#r=rid-2")


async def test_share_url_rejects_missing_request_id() -> None:
    with pytest.raises(ValueError, match="request_id"):
        await handle_call_tool("share_url", {})


async def test_read_share_url_extracts_uuid_from_full_url() -> None:
    uuid = "12345678-1234-1234-1234-123456789abc"
    get_mock = AsyncMock(
        return_value={
            "request_id": uuid,
            "tool_name": "verify_proof",
            "inputs": {"content": "x"},
            "result": {"okay": True},
            "state": "succeeded",
            "created_at": "T",
            "tier_saved": "alpha",
            "source": "hot_store",
        }
    )
    with patch("axle_mcp_server.server._get_shared_link", get_mock):
        result = await handle_call_tool(
            "read_share_url",
            {"share_url": f"https://axle.axiommath.ai/verify_proof#r={uuid}"},
        )

    get_mock.assert_called_once_with(uuid)
    parsed = json.loads(result[0].text)
    assert parsed["tool_name"] == "verify_proof"
    assert parsed["state"] == "succeeded"
    assert "tier_saved" not in parsed
    assert "source" not in parsed


async def test_read_share_url_accepts_bare_uuid() -> None:
    uuid = "12345678-1234-1234-1234-123456789abc"
    get_mock = AsyncMock(return_value={"request_id": uuid, "tool_name": "check"})
    with patch("axle_mcp_server.server._get_shared_link", get_mock):
        await handle_call_tool("read_share_url", {"share_url": uuid})

    get_mock.assert_called_once_with(uuid)


async def test_read_share_url_rejects_input_without_uuid() -> None:
    with pytest.raises(ValueError, match="UUID"):
        await handle_call_tool("read_share_url", {"share_url": "not-a-uuid"})


async def test_read_share_url_rejects_missing_input() -> None:
    with pytest.raises(ValueError, match="share_url"):
        await handle_call_tool("read_share_url", {})
