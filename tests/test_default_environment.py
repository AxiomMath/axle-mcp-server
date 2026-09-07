from __future__ import annotations

import pytest

from axle_mcp_server.server import _default_environment, _resolve_default_environment


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


# `_resolve_default_environment` layers an `AXLE_DEFAULT_ENVIRONMENT`
# override on top of `_default_environment`'s auto-pick.

def test_resolve_falls_back_to_auto_pick_when_unset(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("AXLE_DEFAULT_ENVIRONMENT", raising=False)
    envs = [{"name": "lean-4.27.0"}, {"name": "lean-4.29.0"}]
    assert _resolve_default_environment(envs) == "lean-4.29.0"


def test_resolve_uses_override_when_set_to_known_env(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("AXLE_DEFAULT_ENVIRONMENT", "pnt-4.28.0")
    envs = [{"name": "lean-4.28.0"}, {"name": "pnt-4.28.0"}]
    assert _resolve_default_environment(envs) == "pnt-4.28.0"


def test_resolve_treats_empty_override_as_unset(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("AXLE_DEFAULT_ENVIRONMENT", "")
    envs = [{"name": "lean-4.28.0"}]
    assert _resolve_default_environment(envs) == "lean-4.28.0"


def test_resolve_strips_whitespace_override(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("AXLE_DEFAULT_ENVIRONMENT", "  pnt-4.28.0  ")
    envs = [{"name": "lean-4.28.0"}, {"name": "pnt-4.28.0"}]
    assert _resolve_default_environment(envs) == "pnt-4.28.0"


def test_resolve_rejects_unknown_override(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("AXLE_DEFAULT_ENVIRONMENT", "lean-99.99.99")
    envs = [{"name": "lean-4.28.0"}]
    with pytest.raises(RuntimeError, match=r"not a registered environment"):
        _resolve_default_environment(envs)
