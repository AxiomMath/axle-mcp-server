from __future__ import annotations

import pytest

MOCK_ENDPOINTS = {
    "check": {
        "description": "Evaluate Lean code and collect all messages.",
        "inputs": [
            {"name": "content", "type": "textarea", "description": "Lean source code", "required": True},
            {"name": "environment", "type": "text", "description": "Lean environment", "required": True},
        ],
    },
    "verify_proof": {
        "description": "Validate a candidate Lean theorem.",
        "inputs": [
            {"name": "formal_statement", "type": "textarea", "description": "Sorried theorem", "required": True},
            {"name": "content", "type": "textarea", "description": "Candidate proof", "required": True},
            {"name": "environment", "type": "text", "description": "Lean environment", "required": True},
        ],
    },
    "merge": {
        "description": "Merge Lean documents.",
        "inputs": [
            {"name": "documents", "type": "textarea_list", "description": "Lean documents", "required": True},
            {"name": "environment", "type": "text", "description": "Lean environment", "required": True},
        ],
    },
}

MOCK_ENVIRONMENTS = [
    {"name": "lean-4.27.0"},
    {"name": "lean-4.28.0"},
]


@pytest.fixture(autouse=True)
def _patch_startup_data(monkeypatch: pytest.MonkeyPatch) -> None:
    """Patch module-level state so tests don't hit the network."""
    import axle_mcp_server.server as srv

    default_env = MOCK_ENVIRONMENTS[-1]["name"]
    monkeypatch.setattr(srv, "ENDPOINTS", MOCK_ENDPOINTS)
    monkeypatch.setattr(srv, "ENVIRONMENTS", MOCK_ENVIRONMENTS)
    monkeypatch.setattr(srv, "DEFAULT_ENVIRONMENT", default_env)
    tool_defs = srv._build_tool_defs(MOCK_ENDPOINTS, default_env)
    monkeypatch.setattr(srv, "TOOL_DEFS", tool_defs)
    monkeypatch.setattr(srv, "ENDPOINT_NAMES", {t.name for t in tool_defs} - {"list_environments"})
