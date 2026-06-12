from __future__ import annotations

import json
from unittest.mock import AsyncMock, patch

from axle_mcp_server.server import _briefen, handle_call_tool

_FULL_RESPONSE = {
    "okay": False,
    "content": "theorem foo : 1 = 1 := rfl\n",
    "lean_messages": {"errors": [], "warnings": [], "infos": ["Try this: exact foo"]},
    "tool_messages": {"errors": ["mismatch"], "warnings": [], "infos": []},
    "timings": {"total_ms": 160, "candidate_ms": 28},
    "failed_declarations": ["foo"],
}


def test_briefen_drops_timings_and_empty_buckets_keeps_the_rest() -> None:
    out = _briefen(_FULL_RESPONSE, submitted_content=None)
    assert "timings" not in out
    assert out["tool_messages"] == {"errors": ["mismatch"]}
    assert out["lean_messages"] == {"infos": ["Try this: exact foo"]}
    assert out["okay"] is False
    assert out["failed_declarations"] == ["foo"]


def test_briefen_flags_echoed_content() -> None:
    content = "theorem foo : 1 = 1 := rfl\n"
    out = _briefen({"okay": True, "content": content}, submitted_content=content)
    assert "content" not in out
    assert out["content_unchanged"] is True


def test_briefen_keeps_transformed_content() -> None:
    out = _briefen({"content": "renamed\n"}, submitted_content="original\n")
    assert out["content"] == "renamed\n"
    assert "content_unchanged" not in out


def test_briefen_passthrough_non_dict() -> None:
    assert _briefen([1, 2], submitted_content=None) == [1, 2]


def test_verbosity_param_in_schema() -> None:
    import axle_mcp_server.server as srv

    schema = next(t.inputSchema for t in srv.TOOL_DEFS if t.name == "check")
    assert schema["properties"]["verbosity"]["enum"] == ["brief", "verbose"]
    assert schema["properties"]["verbosity"]["default"] == "verbose"
    assert "verbosity" not in schema.get("required", [])


async def test_verbosity_not_forwarded_to_api() -> None:
    mock = AsyncMock(return_value={"okay": True})
    with patch("axle_mcp_server.server._call_endpoint", mock):
        await handle_call_tool("check", {"content": "x", "verbosity": "brief"})

    assert "verbosity" not in mock.call_args[0][1]


async def test_brief_output_is_compact() -> None:
    mock = AsyncMock(return_value=_FULL_RESPONSE)
    with patch("axle_mcp_server.server._call_endpoint", mock):
        result = await handle_call_tool("check", {"content": "x", "verbosity": "brief"})

    text = result[0].text
    assert "\n" not in text
    assert "timings" not in json.loads(text)


async def test_full_is_default_and_pretty() -> None:
    mock = AsyncMock(return_value=_FULL_RESPONSE)
    with patch("axle_mcp_server.server._call_endpoint", mock):
        result = await handle_call_tool("check", {"content": "x"})

    text = result[0].text
    assert "\n" in text
    assert json.loads(text)["timings"]["total_ms"] == 160
