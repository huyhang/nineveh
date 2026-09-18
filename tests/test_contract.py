"""The committed API contract must describe the service that actually ships.

A specification nobody verifies drifts within a release or two, so this suite
compares `docs/openapi.json` against the live route table rather than trusting
that somebody remembered to regenerate it.
"""

from __future__ import annotations

import json
from collections import Counter
from pathlib import Path

import pytest

from nineveh import __version__
from nineveh.app import openapi_document
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
