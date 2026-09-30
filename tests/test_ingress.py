"""Which front door a request used, decided without a server."""

from __future__ import annotations

import inspect
import re

import pytest
from starlette.requests import Request
from starlette.routing import Route

from nineveh.config import TAILSCALE_NETWORKS
from nineveh.http_api import router as api_router
from nineveh.http_web import router as web_router
from nineveh.ingress import Ingress, is_private_route, normalize_origin

PUBLIC = "https://read.example"
TAILNET = "https://nas.tail.ts.net"
LAN = "https://192.168.7.168:5443"
NETWORKS = f"{TAILSCALE_NETWORKS},192.168.7.0/24"


def request(
    host: str,
    client: str,
    path: str = "/opds/v2/catalog.json",
    *,
    scheme: str = "https",
    origin: str | None = None,
) -> Request:
    headers = [(b"host", host.encode())]
    if origin is not None:
        headers.append((b"origin", origin.encode()))
    return Request(
        {
            "type": "http",
            "method": "GET",
            "scheme": scheme,
            "path": path,
            "root_path": "",
            "query_string": b"",
            "headers": headers,
            "client": (client, 50000),
            "server": (host, 443),
        }
    )


@pytest.fixture
def split() -> Ingress:
    return Ingress(PUBLIC, (TAILNET, LAN), NETWORKS)


def test_origins_are_normalized():
    assert normalize_origin("HTTPS://NAS.Example:443/") == "https://nas.example"
    assert normalize_origin("http://nas:8080") == "http://nas:8080"
    assert normalize_origin("https://[::1]:8443") == "https://[::1]:8443"
    for bad in ("nas.example", "ftp://nas", "https://nas/path", "https://nas/?q=1"):
        with pytest.raises(ValueError):
            normalize_origin(bad)


def test_tailscale_and_lan_are_both_private(split: Ingress):
    assert split.is_private(request("nas.tail.ts.net", "100.101.102.103"))
    assert split.is_private(request("192.168.7.168:5443", "192.168.7.50"))
    assert split.is_private(request("nas.tail.ts.net", "fd7a:115c:a1e0::5"))


def test_a_private_hostname_from_an_outside_address_is_not_private(split: Ingress):
    forged = request("nas.tail.ts.net", "203.0.113.9")
    assert split.accepts(forged)
    assert not split.is_private(forged)


def test_an_allowed_address_on_the_public_origin_is_not_private(split: Ingress):
    assert not split.is_private(request("read.example", "100.101.102.103"))


def test_split_access_answers_only_configured_origins(split: Ingress):
    assert split.accepts(request("read.example", "203.0.113.9"))
    assert not split.accepts(request("evil.example", "203.0.113.9"))
    # The same LAN host on another port is another origin.
    assert not split.accepts(request("192.168.7.168:5001", "192.168.7.50"))


def test_the_containers_own_health_check_is_answered_on_loopback(split: Ingress):
    assert split.accepts(request("127.0.0.1:8080", "127.0.0.1", "/api/v1/health/live"))
    assert not split.accepts(
        request("127.0.0.1:8080", "127.0.0.1", "/api/v1/health/ready")
    )
    assert not split.accepts(
        request("127.0.0.1:8080", "203.0.113.9", "/api/v1/health/live")
    )


def test_administrators_are_admitted_only_on_a_private_origin(split: Ingress):
    public = request("read.example", "203.0.113.9")
    private = request("nas.tail.ts.net", "100.101.102.103")
    assert split.admits_account(public, is_admin=False)
    assert not split.admits_account(public, is_admin=True)
    assert split.admits_account(private, is_admin=True)


@pytest.mark.parametrize(
    "path",
    [
        "/admin",
        "/admin/users",
        "/api/v1/admin/users",
        "/api/v1/librarian/ingest",
        "/docs",
        "/openapi.json",
        "/series/abc/privacy",
        "/series/abc/metadata/cover",
        "/libraries/lib/manga/metadata/lookup",
        "/publications/abc/spread-start",
        "/api/v1/series/abc/privacy",
    ],
)
def test_private_routes_are_refused_on_the_public_origin(split: Ingress, path: str):
    assert is_private_route(path)
    assert not split.admits_route(request("read.example", "203.0.113.9", path))
    assert split.admits_route(request("nas.tail.ts.net", "100.101.102.103", path))


@pytest.mark.parametrize(
    "path", ["/", "/series/abc", "/administrator", "/api/v1/series/abc", "/login"]
)
def test_reader_routes_stay_public(path: str):
    assert not is_private_route(path)


def test_links_name_the_origin_a_request_arrived_on(split: Ingress):
    assert split.link_origin(request("read.example", "203.0.113.9")) == PUBLIC
    assert split.link_origin(request("nas.tail.ts.net", "100.101.102.103")) == TAILNET


def test_split_sign_in_requires_the_matching_origin_header(split: Ingress):
    host, client = "read.example", "203.0.113.9"
    assert split.valid_form_origin(request(host, client, origin=PUBLIC))
    assert not split.valid_form_origin(request(host, client))
    assert not split.valid_form_origin(request(host, client, origin=TAILNET))
    assert not split.valid_form_origin(request(host, client, origin="null"))


def test_without_split_access_everything_is_private_and_links_are_as_configured():
    legacy = Ingress("https://example.com/nineveh", (), "")
    outside = request("anything.example", "203.0.113.9", scheme="http")
    assert not legacy.split
    assert legacy.accepts(outside)
    assert legacy.is_private(outside)
    assert legacy.admits_account(outside, is_admin=True)
    assert legacy.link_origin(outside) == "https://example.com/nineveh"
    assert legacy.valid_form_origin(outside)
    assert not legacy.valid_form_origin(
        request("anything.example", "203.0.113.9", origin="https://elsewhere")
    )


def test_an_unconfigured_install_links_to_the_request_url():
    legacy = Ingress(None)
    assert legacy.link_origin(request("nas:8080", "10.0.0.2", scheme="http")) == (
        "http://nas:8080"
    )


@pytest.mark.parametrize(
    ("public", "private", "networks"),
    [
        (None, (TAILNET,), NETWORKS),
        (PUBLIC, (PUBLIC,), NETWORKS),
        (PUBLIC, (TAILNET,), ""),
        (PUBLIC, (TAILNET,), "not-a-network"),
    ],
)
def test_misconfigured_split_access_is_refused(public, private, networks):
    with pytest.raises(ValueError):
        Ingress(public, private, networks)


def _admin_only(endpoint) -> bool:
    source = inspect.getsource(endpoint)
    return "Depends(administrator)" in source or "_require_admin(request)" in source


def _concrete(path: str) -> str:
    return re.sub(r"\{[^}]+\}", "x", path)


def _routes() -> list[Route]:
    return [
        route
        for route in (*api_router.routes, *web_router.routes)
        if isinstance(route, Route)
    ]


def test_every_administrator_route_is_classified_private():
    """Adding an admin route the ingress does not know about fails here."""
    admin_routes = [route for route in _routes() if _admin_only(route.endpoint)]
    assert len(admin_routes) > 40
    unguarded = [
        route.path
        for route in admin_routes
        if not is_private_route(_concrete(route.path))
    ]
    assert unguarded == []


def test_every_librarian_route_is_classified_private():
    librarian = [
        route
        for route in _routes()
        if "librarian_identity" in inspect.getsource(route.endpoint)
    ]
    assert librarian
    assert [r.path for r in librarian if not is_private_route(_concrete(r.path))] == []
