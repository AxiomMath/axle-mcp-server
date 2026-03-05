from __future__ import annotations

from axle_mcp_server.server import _default_environment


def test_selects_latest_stable_version():
    envs = [
        {"name": "lean-4.9.0"},
        {"name": "lean-4.29.0"},
        {"name": "lean-4.28.0"},
    ]
    assert _default_environment(envs) == "lean-4.29.0"


def test_micro_version_tiebreaker():
    envs = [
        {"name": "lean-4.29.0"},
        {"name": "lean-4.29.1"},
        {"name": "lean-4.28.0"},
    ]
    assert _default_environment(envs) == "lean-4.29.1"


def test_ignores_rc_versions():
    envs = [
        {"name": "lean-4.21.0-rc"},
        {"name": "lean-4.9.0"},
    ]
    assert _default_environment(envs) == "lean-4.9.0"


def test_ignores_non_lean_prefix():
    envs = [
        {"name": "pnt-4.25.0"},
        {"name": "lean-4.9.0"},
    ]
    assert _default_environment(envs) == "lean-4.9.0"


def test_falls_back_to_last_when_no_match():
    envs = [
        {"name": "pnt-4.25.0"},
        {"name": "custom-env"},
    ]
    assert _default_environment(envs) == "custom-env"
