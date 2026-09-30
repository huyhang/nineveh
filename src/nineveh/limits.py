"""Request-body ceilings, enforced before a route parses anything.

A declared `Content-Length` over the limit is refused outright; a chunked body
is counted as it streams and cut off the moment it crosses the limit. Routes
whose bodies Starlette spools to disk also stop when the state volume reaches
its free-space reserve, so an upload can never fill the disk the database
lives on.
"""

from __future__ import annotations

import re
import shutil
from collections.abc import Callable
from pathlib import Path

from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

INGEST_PATH = "/api/v1/librarian/ingest"
_COVER_UPLOAD = re.compile(r"/series/[^/]+/metadata/cover")
MULTIPART_OVERHEAD = 1024 * 1024


class BodyTooLarge(Exception):
    pass


class BodyLimits:
    """Which ceiling applies to a request, and whether it spools to disk."""

    def __init__(self, default: int, ingest: int, cover: int) -> None:
        self._default = default
        self._ingest = ingest + MULTIPART_OVERHEAD
        self._cover = cover + MULTIPART_OVERHEAD

    def limit(self, method: str, path: str) -> int:
        if method == "POST" and path == INGEST_PATH:
            return self._ingest
        if method == "POST" and _COVER_UPLOAD.fullmatch(path):
            return self._cover
        return self._default

    def spools(self, method: str, path: str) -> bool:
        return self.limit(method, path) != self._default


def _free_bytes(directory: Path) -> int:
    return shutil.disk_usage(directory).free


class BodyLimitMiddleware:
    def __init__(
        self,
        app: ASGIApp,
        limits: BodyLimits,
        spool_dir: Path,
        free_reserve: int,
        free_bytes: Callable[[Path], int] = _free_bytes,
    ) -> None:
        self.app = app
        self._limits = limits
        self._spool_dir = spool_dir
        self._free_reserve = free_reserve
        self._free_bytes = free_bytes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        method, path = scope["method"], scope["path"]
        limit = self._limits.limit(method, path)
        if _declared_length(scope) > limit:
            await _refuse(scope, receive, send)
            return
        counter = _Counter(receive, limit, self._space_check(method, path))
        guarded = _ResponseGuard(send, lambda: counter.exceeded)
        try:
            await self.app(scope, counter.receive, guarded.send)
        except BodyTooLarge:
            pass
        if counter.exceeded:
            if guarded.started:
                raise BodyTooLarge  # too late for a clean 413; drop the connection
            await _refuse(scope, receive, send)

    def _space_check(self, method: str, path: str) -> Callable[[int], bool]:
        if not self._limits.spools(method, path):
            return lambda _chunk: True
        return lambda chunk: (
            self._free_bytes(self._spool_dir) - chunk >= self._free_reserve
        )


class _Counter:
    """Counts body bytes as the app reads them, raising past the limit."""

    def __init__(self, receive: Receive, limit: int, room: Callable[[int], bool]):
        self._receive = receive
        self._limit = limit
        self._room = room
        self._consumed = 0
        self.exceeded = False

    async def receive(self) -> Message:
        message = await self._receive()
        if message["type"] == "http.request":
            chunk = len(message.get("body", b""))
            self._consumed += chunk
            if self._consumed > self._limit or (chunk and not self._room(chunk)):
                self.exceeded = True
                raise BodyTooLarge
        return message


class _ResponseGuard:
    """Swallows whatever the app answers once the body has been refused.

    A form parser may catch the refusal and answer 400 itself; the client
    should see the 413 that explains it instead.
    """

    def __init__(self, send: Send, refused: Callable[[], bool]) -> None:
        self._send = send
        self._refused = refused
        self.started = False

    async def send(self, message: Message) -> None:
        if self._refused():
            return
        if message["type"] == "http.response.start":
            self.started = True
        await self._send(message)


def _declared_length(scope: Scope) -> int:
    for name, value in scope.get("headers", ()):
        if name.lower() == b"content-length":
            try:
                return int(value)
            except ValueError:
                return 1 << 62  # unparseable: treat as over any limit
    return 0


async def _refuse(scope: Scope, receive: Receive, send: Send) -> None:
    response = JSONResponse({"detail": "Request body is too large"}, status_code=413)
    await response(scope, receive, send)
