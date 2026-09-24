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
from conftest import authorization
from fastapi.testclient import TestClient

from nineveh import __version__
from nineveh.app import CONTRACT_SLICES, openapi_document, tagged_contract
from nineveh.archives import PageRenditionService, ThumbnailService
from nineveh.http_api import router as api_router
from nineveh.http_web import router as web_router
from nineveh.opds import CBZ_MEDIA_TYPE

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


def _success_media(document: dict) -> list[tuple[str, str, str, dict]]:
    """(path, method, media type, schema) for every documented 2xx body."""
    return [
        (path, method, media_type, media.get("schema") or {})
        for path, method in _operations(document)
        for code, response in document["paths"][path][method]["responses"].items()
        if code.startswith("2")
        for media_type, media in (response.get("content") or {}).items()
    ]


def _nodes(node: object):
    if isinstance(node, dict):
        yield node
        for value in node.values():
            yield from _nodes(value)
    elif isinstance(node, list):
        for value in node:
            yield from _nodes(value)


def test_no_response_refuses_fields_a_later_release_adds(document: dict):
    """The server validates responses with `extra="forbid"` to keep its
    builders honest. Published as `additionalProperties: false`, that tells a
    client to reject any field added later -- swift-openapi-generator does --
    so every compatible addition would break apps already installed."""
    schemas = document["components"]["schemas"]
    pending = [schema for *_, schema in _success_media(document)]
    seen: set[str] = set()
    closed: list[str] = []
    while pending:
        for node in _nodes(pending.pop()):
            if node.get("additionalProperties") is False:
                closed.append(node.get("title", "?"))
            name = node.get("$ref", "").removeprefix("#/components/schemas/")
            if name and name not in seen:
                seen.add(name)
                pending.append(schemas[name])
    assert seen, "no response references a schema; the walk proved nothing"
    assert closed == []


# --------------------------------------------------------------------------
# The per-client slices another repository vendors
# --------------------------------------------------------------------------

SLICES = sorted(CONTRACT_SLICES)
SCHEMA_REF = re.compile(r'"\$ref":\s*"#/components/schemas/([^"]+)"')


def _slice_path(name: str) -> Path:
    return CONTRACT.parent / f"{name}-openapi.json"


@pytest.fixture(scope="module", params=SLICES)
def sliced(request) -> tuple[str, dict]:
    name = request.param
    return name, json.loads(_slice_path(name).read_text(encoding="utf-8"))


@pytest.mark.parametrize("name", SLICES)
def test_the_committed_slice_matches_the_running_application(name: str):
    contract = tagged_contract(name, CONTRACT_SLICES[name])
    expected = json.dumps(contract, indent=2, sort_keys=True) + "\n"
    assert _slice_path(name).read_text(encoding="utf-8") == expected, (
        f"docs/{name}-openapi.json is stale — run `python scripts/export-openapi.py`"
    )


def test_every_committed_slice_is_still_generated():
    """A slice dropped from the export would otherwise linger in `docs/`, still
    vendored by some client long after nothing checks it."""
    committed = {path.name for path in CONTRACT.parent.glob("*-openapi.json")}
    assert committed == {_slice_path(name).name for name in SLICES}


@pytest.mark.parametrize("name", SLICES)
def test_every_tag_a_slice_names_is_published(document: dict, name: str):
    """A misspelt or retired tag silently drops its operations, and the slice
    still looks plausible because the other tags filled it."""
    published = {
        tag
        for path, method in _operations(document)
        for tag in document["paths"][path][method].get("tags") or []
    }
    assert CONTRACT_SLICES[name] - published == set()


def test_each_slice_carries_every_operation_tagged_for_it(
    document: dict, sliced: tuple[str, dict]
):
    name, contract = sliced
    tags = CONTRACT_SLICES[name]
    tagged = {
        (path, method)
        for path, method in _operations(document)
        if not tags.isdisjoint(document["paths"][path][method].get("tags") or [])
    }
    assert set(_operations(contract)) == tagged


def test_no_slice_carries_administration(sliced: tuple[str, dict]):
    """Token and user management are the operator's, not any client's."""
    _, contract = sliced
    assert not [path for path, _ in _operations(contract) if "/admin/" in path]


def test_every_reference_in_a_slice_resolves_inside_it(sliced: tuple[str, dict]):
    """A subset that references a schema it does not carry fails at code
    generation, which is far later and more confusing than failing here."""
    _, contract = sliced
    carried = set(contract.get("components", {}).get("schemas", {}))
    referenced = set(SCHEMA_REF.findall(json.dumps(contract)))
    assert referenced - carried == set()


def test_a_slice_carries_nothing_it_does_not_reference(sliced: tuple[str, dict]):
    """Unused schemas would reintroduce the churn the subset exists to avoid."""
    _, contract = sliced
    carried = set(contract.get("components", {}).get("schemas", {}))
    referenced = set(SCHEMA_REF.findall(json.dumps(contract)))
    assert carried - referenced == set()


def test_a_slice_defines_exactly_the_security_schemes_it_requires(
    sliced: tuple[str, dict],
):
    """An operation that requires a scheme the document never defines is
    rejected by generators just as a dangling `$ref` is."""
    _, contract = sliced
    required = {
        scheme
        for path, method in _operations(contract)
        for requirement in contract["paths"][path][method].get("security") or []
        for scheme in requirement
    }
    defined = set(contract.get("components", {}).get("securitySchemes", {}))
    assert required == defined


SCHEMES = {
    "app": {"HTTPBasic": ("http", "basic")},
    "librarian": {"LibrarianToken": ("http", "bearer")},
}
# What a client may call before it has credentials.
PUBLIC = {"app": {("/opds/v2/authentication.json", "get")}, "librarian": set()}


def test_each_slice_tells_a_generator_how_to_authenticate(sliced: tuple[str, dict]):
    """Otherwise the scheme check above passes vacuously, and a client
    generated from the slice has no idea its requests need credentials."""
    name, contract = sliced
    schemes = contract.get("components", {}).get("securitySchemes", {})
    assert {
        scheme: (details["type"], details["scheme"])
        for scheme, details in schemes.items()
    } == SCHEMES[name]
    public = {
        (path, method)
        for path, method in _operations(contract)
        if not contract["paths"][path][method].get("security")
    }
    assert public == PUBLIC[name]


def test_each_slice_describes_every_json_body_it_returns(sliced: tuple[str, dict]):
    """A free-form object leaves a generated client decoding the body by hand,
    and no drift test notices a field that is renamed. An empty schema is no
    better, and it is what FastAPI documents for a route that sends a file.
    The OPDS feeds count: they follow a published spec, but which of its
    optional fields Nineveh fills is Nineveh's to promise."""
    _, contract = sliced
    media = _success_media(contract)
    undescribed = [
        (path, method)
        for path, method, media_type, schema in media
        if (media_type == "application/json" or media_type.endswith("+json"))
        and (
            not schema
            or (schema.get("type") == "object" and "properties" not in schema)
        )
    ]
    assert undescribed == []
    assert any("$ref" in schema for *_, schema in media)


def test_what_the_app_downloads_arrives_as_the_slice_says(
    client: TestClient, publication_id: str
):
    """Every contract check above compares a document with itself. This one
    asks the routes -- which is how a cover documented as JSON gets caught."""
    app = json.loads(_slice_path("app").read_text(encoding="utf-8"))
    headers = authorization()
    progress = f"/api/v1/publications/{publication_id}/progress"
    client.put(progress, headers=headers, json={"page": 1, "mode": "single"})
    [series] = client.app.state.container.repository.catalog_series()
    arguments = {
        "publication_id": publication_id,
        "series_id": series.id,
        "number": 1,
        "library": series.library,
    }
    received: set[str] = set()
    for path, method in _operations(app):
        if method not in {"get", "head"}:
            continue
        operation = app["paths"][path][method]
        query = {
            parameter["name"]: arguments[parameter["name"]]
            for parameter in operation.get("parameters") or []
            if parameter["in"] == "query" and parameter.get("required")
        }
        response = client.request(
            method, path.format(**arguments), params=query, headers=headers
        )
        documented = operation["responses"]["200"]
        assert response.status_code == 200, (method, path, response.text)
        declared = set(documented.get("content") or {})
        media_type = response.headers["content-type"].split(";")[0]
        if method == "head":
            # A HEAD answer has no body, so there is no content to declare.
            assert declared == set(), path
        else:
            assert media_type in declared, (path, media_type)
            received.add(media_type)
        documented_headers = documented.get("headers") or {}
        missing = [name for name in documented_headers if name not in response.headers]
        assert missing == [], (method, path)
    assert {"application/json", "image/png", "image/webp", CBZ_MEDIA_TYPE} <= received


def _listed(schema: dict) -> list:
    """The enum of a parameter, looking through the `anyOf` an optional one has."""
    for option in [schema, *schema.get("anyOf", [])]:
        if "enum" in option:
            return option["enum"]
    return []


def test_every_width_the_app_slice_lists_renders(
    client: TestClient, publication_id: str
):
    """Pages and covers render at a few widths and refuse the rest, so the
    slice lists them. Ask the routes that each listed width renders and that
    the lists are the services' whole choice, not a stale copy of part of it."""
    app = json.loads(_slice_path("app").read_text(encoding="utf-8"))
    headers = authorization()
    listed = {
        path: _listed(parameter["schema"])
        for path, method in _operations(app)
        if method == "get"
        for parameter in app["paths"][path][method].get("parameters") or []
        if parameter["name"] == "width"
    }
    page = "/api/v1/publications/{publication_id}/pages/{number}"
    cover = "/api/v1/publications/{publication_id}/cover"
    assert set(listed) == {page, cover}
    assert set(listed[page]) == PageRenditionService.ALLOWED_WIDTHS
    assert set(listed[cover]) == ThumbnailService.ALLOWED_WIDTHS
    for path, widths in listed.items():
        url = path.format(publication_id=publication_id, number=1)
        for width in widths:
            response = client.get(url, params={"width": width}, headers=headers)
            assert response.status_code == 200, (path, width, response.text)
        refused = client.get(url, params={"width": max(widths) + 1}, headers=headers)
        assert refused.status_code == 422, path


def test_each_slice_is_versioned_with_the_package(sliced: tuple[str, dict]):
    name, contract = sliced
    assert contract["info"]["version"] == __version__
    assert contract["openapi"].startswith("3.1")
    assert contract["info"]["title"].endswith(f"({name})")


def test_an_unknown_tag_yields_an_empty_contract():
    """So a slice whose tags all went stale ships nothing, not a stub."""
    empty = tagged_contract("typo", {"no-such-tag"})
    assert empty["paths"] == {}
    assert "components" not in empty
