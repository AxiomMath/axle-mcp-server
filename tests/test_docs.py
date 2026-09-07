from __future__ import annotations

import urllib.error
from typing import Any

import pytest

import axle_mcp_server.server as srv

# Captured before the autouse fixture swaps it for a stub, so the loading tests
# can exercise the real implementation.
_REAL_LOAD_DOCS = srv._load_docs

# Shaped like /v1/docs/all.json: manifest + inlined source markdown, nav order.
MOCK_BUNDLE: dict[str, Any] = {
    "version": "8f3c1d2",
    "pages": [
        {
            "slug": "quickstart",
            "title": "Quick Start",
            "html_url": "https://axle.axiommath.ai/v1/docs/quickstart/",
            "markdown": "# Quick Start\n\nInstall the client, then call `check`.",
        },
        {
            "slug": "tools/verify_proof",
            "title": "verify_proof",
            "html_url": "https://docs.example.com/v1/docs/tools/verify_proof/",
            "markdown": (
                "# verify_proof\n\n"
                "See [lean4checker](https://github.com/leanprover/lean4checker).\n\n"
                "## Input Parameters\n\n### `formal_statement`\n\n"
                "```lean\ntheorem t : 1 = 1 := by sorry\n```"
            ),
        },
    ],
}

MOCK_PAGES: list[srv.DocPage] = srv._pages_from_bundle(MOCK_BUNDLE)


@pytest.fixture(autouse=True)
def _patch_docs(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(srv, "_docs_cache", None)

    async def _fake_load() -> list[srv.DocPage]:
        return MOCK_PAGES

    monkeypatch.setattr(srv, "_load_docs", _fake_load)


async def _read_docs(**arguments: Any) -> str:
    result = await srv.handle_call_tool("read_docs", arguments)
    assert len(result) == 1
    return str(result[0].text)


def test_read_docs_is_registered_as_a_builtin() -> None:
    tool = next(t for t in srv._build_tool_defs({}, "lean-4.28.0") if t.name == "read_docs")
    assert set(tool.inputSchema["properties"]) == {"page"}
    # Some model APIs reject oneOf/anyOf/allOf in a tool's input_schema.
    assert not {"oneOf", "anyOf", "allOf", "required"} & set(tool.inputSchema)
    assert "read_docs" in srv.BUILTIN_TOOLS
    assert "read_docs" not in srv.ENDPOINT_NAMES


def test_bundle_normalizes_pages_and_passes_markdown_through() -> None:
    assert [p["slug"] for p in MOCK_PAGES] == ["quickstart", "tools/verify_proof"]
    page = MOCK_PAGES[1]
    assert page["title"] == "verify_proof"
    assert page["html_url"] == "https://docs.example.com/v1/docs/tools/verify_proof/"
    assert page["markdown"] == str(MOCK_BUNDLE["pages"][1]["markdown"])
    assert "https://github.com/leanprover/lean4checker" in page["markdown"]
    assert "### `formal_statement`" in page["markdown"]


@pytest.mark.parametrize("payload", [{}, {"pages": []}, []])
def test_bundle_rejects_unusable_payloads(payload: Any) -> None:
    with pytest.raises(RuntimeError, match="returned no pages"):
        srv._pages_from_bundle(payload)


@pytest.mark.parametrize(
    ("given", "expected"),
    [
        ("verify_proof", "tools/verify_proof"),
        ("tools/verify_proof/", "tools/verify_proof"),
        ("tools/verify_proof/#input-parameters", "tools/verify_proof"),
        ("https://axle.axiommath.ai/v1/docs/tools/verify_proof/", "tools/verify_proof"),
        ("quickstart", "quickstart"),
    ],
)
def test_find_doc_page_accepts_slugs_bare_names_and_urls(given: str, expected: str) -> None:
    assert srv._find_doc_page(given, MOCK_PAGES)["slug"] == expected


def test_find_doc_page_unknown_lists_available_pages() -> None:
    with pytest.raises(ValueError, match="Unknown docs page 'nope'") as excinfo:
        srv._find_doc_page("nope", MOCK_PAGES)
    assert "tools/verify_proof" in str(excinfo.value)


async def test_no_arguments_returns_the_page_index() -> None:
    out = await _read_docs()
    assert "# AXLE documentation index" in out
    assert "- `quickstart` — Quick Start" in out
    assert "- `tools/verify_proof` — verify_proof" in out


async def test_page_returns_markdown_verbatim_after_a_source_line() -> None:
    out = await _read_docs(page="verify_proof")
    assert out == (
        "Source: https://docs.example.com/v1/docs/tools/verify_proof/\n\n"
        f"{MOCK_PAGES[1]['markdown']}"
    )


async def test_blank_page_argument_falls_through_to_the_index() -> None:
    assert "# AXLE documentation index" in await _read_docs(page="   ")


async def test_unknown_page_raises() -> None:
    with pytest.raises(ValueError, match="Unknown docs page"):
        await _read_docs(page="nope")


async def test_load_docs_fetches_once_and_caches(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(srv, "_docs_cache", None)
    calls: list[str] = []

    def _fake_fetch(path: str) -> Any:
        calls.append(path)
        return MOCK_BUNDLE

    monkeypatch.setattr(srv, "_fetch_json", _fake_fetch)
    assert await _REAL_LOAD_DOCS() == await _REAL_LOAD_DOCS() == MOCK_PAGES
    assert calls == ["/v1/docs/all.json"]


@pytest.mark.parametrize(("code", "match"), [(401, "require an API key"), (404, "404")])
async def test_load_docs_surfaces_http_errors(
    monkeypatch: pytest.MonkeyPatch, code: int, match: str
) -> None:
    monkeypatch.setattr(srv, "_docs_cache", None)

    def _fake_fetch(path: str) -> Any:
        raise urllib.error.HTTPError(path, code, "Nope", {}, None)  # type: ignore[arg-type]

    monkeypatch.setattr(srv, "_fetch_json", _fake_fetch)
    with pytest.raises(RuntimeError, match=match):
        await _REAL_LOAD_DOCS()


async def test_load_docs_does_not_cache_failures(monkeypatch: pytest.MonkeyPatch) -> None:
    """An unauthenticated first call must not poison a later authenticated one."""
    monkeypatch.setattr(srv, "_docs_cache", None)
    attempts: list[str] = []

    def _fake_fetch(path: str) -> Any:
        attempts.append(path)
        if len(attempts) == 1:
            raise urllib.error.HTTPError(path, 401, "Unauthorized", {}, None)  # type: ignore[arg-type]
        return MOCK_BUNDLE

    monkeypatch.setattr(srv, "_fetch_json", _fake_fetch)
    with pytest.raises(RuntimeError):
        await _REAL_LOAD_DOCS()
    assert await _REAL_LOAD_DOCS() == MOCK_PAGES
