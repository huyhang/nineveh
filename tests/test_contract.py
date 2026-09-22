"""The committed API contract must describe the service that actually ships.

A specification nobody verifies drifts within a release or two, so this suite
compares `docs/openapi.json` against the live route table rather than trusting
that somebody remembered to regenerate it.
"""

from __future__ import annotations

import json
import re
from collections import Counter
from pathlib import Path

import pytest

from nineveh import __version__
from nineveh.app import openapi_document, tagged_contract
from nineveh.http_api import router as api_router
from nineveh.http_web import router as web_router

CONTRACT = Path(__file__).resolve().parent.parent / "docs" / "openapi.json"
METHODS = {"get", "post", "put", "patch", "delete", "head", "options"}


@pytest.fixture(scope="module")
def document() -> dict:
    return json.loads(CONTRACT.read_text(encoding="utf-8"))


def _operations(document: dict) -> list[tuple[str, str]]:
    return [
        (path, method)
        for path, item in document["paths"].items()
        for method in item
        if method in METHODS
    ]


def test_the_committed_contract_matches_the_running_application():
    expected = json.dumps(openapi_document(), indent=2, sort_keys=True) + "\n"
    assert CONTRACT.read_text(encoding="utf-8") == expected, (
        "docs/openapi.json is stale — run `python scripts/export-openapi.py`"
    )


def test_every_documented_operation_has_a_unique_identifier(document: dict):
    """Client generators reject a specification that reuses an operationId."""
    identifiers = [
        item[method]["operationId"]
        for path, method in _operations(document)
        for item in [document["paths"][path]]
        if "operationId" in item[method]
    ]
    repeated = sorted(name for name, count in Counter(identifiers).items() if count > 1)
    assert repeated == []
    assert len(identifiers) == len(_operations(document))


def test_the_contract_covers_the_whole_public_route_table(document: dict):
    published = {
        (route.path, method.lower())
        for route in api_router.routes
        for method in route.methods
        if route.include_in_schema
    }
    assert set(_operations(document)) == published


def test_browser_pages_stay_out_of_the_contract(document: dict):
    """The HTML surface is not an API; documenting it would invite clients."""
    assert all(not route.include_in_schema for route in web_router.routes)
    documented = {path for path, _ in _operations(document)}
    assert not {path for path in documented if path in {"/", "/login", "/admin"}}


def test_the_contract_is_versioned_with_the_package(document: dict):
    assert document["info"]["version"] == __version__
    assert document["openapi"].startswith("3.1")


def test_every_operation_is_tagged_for_navigation(document: dict):
    untagged = [
        (path, method)
        for path, method in _operations(document)
        if not document["paths"][path][method].get("tags")
    ]
    assert untagged == []


# --------------------------------------------------------------------------
# The per-tag slice a client in another repository vendors
# --------------------------------------------------------------------------

AGENT_CONTRACT = CONTRACT.parent / "librarian-openapi.json"
SCHEMA_REF = re.compile(r'"\$ref":\s*"#/components/schemas/([^"]+)"')


@pytest.fixture(scope="module")
def agent_document() -> dict:
    return json.loads(AGENT_CONTRACT.read_text(encoding="utf-8"))


def test_the_committed_agent_contract_matches_the_running_application():
    expected = json.dumps(tagged_contract("librarian"), indent=2, sort_keys=True) + "\n"
    assert AGENT_CONTRACT.read_text(encoding="utf-8") == expected, (
        "docs/librarian-openapi.json is stale — run `python scripts/export-openapi.py`"
    )


def test_the_agent_contract_carries_every_librarian_operation(
    document: dict, agent_document: dict
):
    tagged = {
        (path, method)
        for path, method in _operations(document)
        if "librarian" in (document["paths"][path][method].get("tags") or [])
    }
    assert set(_operations(agent_document)) == tagged
    assert tagged, "the tag filter matched nothing, so the subset proves nothing"


def test_the_agent_contract_leaves_out_administration(agent_document: dict):
    """A token-management endpoint is the operator's, not the agent's."""
    assert not [path for path, _ in _operations(agent_document) if "/admin/" in path]


def test_every_reference_in_the_agent_contract_resolves_inside_it(
    agent_document: dict,
):
    """A subset that references a schema it does not carry fails at code
    generation, which is far later and more confusing than failing here."""
    carried = set(agent_document.get("components", {}).get("schemas", {}))
    referenced = set(SCHEMA_REF.findall(json.dumps(agent_document)))
    assert referenced - carried == set()


def test_the_agent_contract_carries_nothing_it_does_not_reference(
    agent_document: dict,
):
    """Unused schemas would reintroduce the churn the subset exists to avoid."""
    carried = set(agent_document.get("components", {}).get("schemas", {}))
    referenced = set(SCHEMA_REF.findall(json.dumps(agent_document)))
    assert carried - referenced == set()


def test_the_agent_contract_is_versioned_with_the_package(agent_document: dict):
    assert agent_document["info"]["version"] == __version__
    assert agent_document["openapi"].startswith("3.1")
    assert agent_document["info"]["title"].endswith("(librarian)")


def test_an_unknown_tag_yields_an_empty_contract():
    """So a typo in the export script fails loudly rather than shipping a stub."""
    empty = tagged_contract("no-such-tag")
    assert empty["paths"] == {}
    assert "components" not in empty
