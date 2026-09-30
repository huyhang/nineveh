"""Which front door a request came through, and what it may do there.

The public origin serves readers. Private origins -- a Tailscale Serve name, a
LAN address -- serve administrators and the librarian agent. A Host header
proves nothing on its own, so a request is private only when it names a private
origin *and* arrives from an allowed client network.

With no private origin configured every request counts as private, which is
how an install behaves until its operator opts in to split access.
"""

from __future__ import annotations

import ipaddress
import re
from collections.abc import Sequence
from urllib.parse import urlsplit

from starlette.requests import Request

Network = ipaddress.IPv4Network | ipaddress.IPv6Network

HEALTH_LIVE = "/api/v1/health/live"
_PRIVATE_PREFIXES = ("/admin/", "/api/v1/admin/", "/api/v1/librarian/")
_PRIVATE_PATHS = frozenset(
    {"/admin", "/docs", "/docs/oauth2-redirect", "/redoc", "/openapi.json"}
)
# Administrator actions that live beside the reader pages they act on.
_PRIVATE_PATTERNS = tuple(
    re.compile(pattern)
    for pattern in (
        r"/series/[^/]+/(?:privacy|spread-detection)",
        r"/series/[^/]+/metadata(?:/.*)?",
        r"/libraries/[^/]+/[^/]+/metadata(?:/.*)?",
        r"/publications/[^/]+/spread-start",
        r"/api/v1/series/[^/]+/privacy",
    )
)


def normalize_origin(value: str) -> str:
    """`scheme://host[:port]`, lower-cased, with the default port dropped."""
    parsed = urlsplit(value.strip())
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError(f"Not an HTTP origin: {value!r}")
    if parsed.path not in {"", "/"} or parsed.query or parsed.fragment:
        raise ValueError(f"An origin has no path, query or fragment: {value!r}")
    host = parsed.hostname.casefold()
    if ":" in host:
        host = f"[{host}]"
    default = 443 if parsed.scheme == "https" else 80
    port = "" if parsed.port in {None, default} else f":{parsed.port}"
    return f"{parsed.scheme}://{host}{port}"


def parse_networks(value: str) -> tuple[Network, ...]:
    networks = []
    for raw in value.split(","):
        entry = raw.strip()
        if not entry:
            continue
        try:
            networks.append(ipaddress.ip_network(entry, strict=False))
        except ValueError as error:
            raise ValueError(f"Not a network: {entry!r}") from error
    return tuple(networks)


def client_address(request: Request) -> str:
    """The peer, after uvicorn has applied its trusted-proxy list."""
    return request.client.host if request.client else "unknown"


def is_private_route(path: str) -> bool:
    if path in _PRIVATE_PATHS or path.startswith(_PRIVATE_PREFIXES):
        return True
    return any(pattern.fullmatch(path) for pattern in _PRIVATE_PATTERNS)


def _is_loopback(address: str) -> bool:
    try:
        return ipaddress.ip_address(address).is_loopback
    except ValueError:
        return False


class Ingress:
    def __init__(
        self,
        public_origin: str | None,
        private_origins: Sequence[str] = (),
        private_networks: str = "",
    ) -> None:
        # Kept verbatim for installs without split access, whose public URL
        # has always been used as written, path and all.
        self._configured = public_origin
        self._private = frozenset(normalize_origin(item) for item in private_origins)
        if self._private and not public_origin:
            raise ValueError("A private origin needs a public origin beside it")
        self._networks = parse_networks(private_networks) if self._private else ()
        self._public = normalize_origin(public_origin) if self._private else None
        if self._public in self._private:
            raise ValueError(f"{self._public} cannot be both public and private")
        if self._private and not self._networks:
            raise ValueError("Split access needs at least one private network")

    @property
    def split(self) -> bool:
        return bool(self._private)

    def origin(self, request: Request) -> str | None:
        try:
            return normalize_origin(str(request.base_url))
        except ValueError:
            return None

    def accepts(self, request: Request) -> bool:
        """Split access answers only the origins it was configured with."""
        if not self.split:
            return True
        if request.url.path == HEALTH_LIVE and _is_loopback(client_address(request)):
            return True  # the container's own health check
        return self.origin(request) in self._private | {self._public}

    def is_private(self, request: Request) -> bool:
        if not self.split:
            return True
        if self.origin(request) not in self._private:
            return False
        try:
            address = ipaddress.ip_address(client_address(request))
        except ValueError:
            return False
        return any(address in network for network in self._networks)

    def admits_route(self, request: Request) -> bool:
        return self.is_private(request) or not is_private_route(request.url.path)

    def admits_account(self, request: Request, *, is_admin: bool) -> bool:
        """Administrator accounts never sign in through the public origin."""
        return not is_admin or self.is_private(request)

    def valid_form_origin(self, request: Request) -> bool:
        """The browser sign-in form must come from the origin it posts to."""
        supplied = request.headers.get("origin")
        if not self.split:
            return not supplied or supplied.rstrip("/") == self.link_origin(request)
        try:
            return normalize_origin(supplied or "") == self.link_origin(request)
        except ValueError:
            return False

    def link_origin(self, request: Request) -> str:
        """Where generated links should point, without a trailing slash.

        Under split access that is the front door the request used -- already
        known to be a configured one -- so an administrator browsing over the
        tailnet is not handed links to the public origin that refuses them.
        """
        if self.split:
            return self.origin(request) or self._public or ""
        return (self._configured or str(request.base_url)).rstrip("/")
