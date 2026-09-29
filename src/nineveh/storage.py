"""Storage roots: where a catalog identity actually lives on disk.

Nineveh indexes several *data mounts*. Each is a directory that is already
visible inside the container — Nineveh can register one, but it cannot create
a bind mount, so the deployment still has to expose the host directory first.
Libraries are direct children of a mount root, and every component that
touches the filesystem resolves through `StoragePathResolver` rather than
assuming a single data directory.
"""

from __future__ import annotations

import os
from pathlib import Path, PurePosixPath
from typing import Protocol

from .domain import (
    DEFAULT_MOUNT_ID,
    DataMount,
    ManagedLibrary,
    MountHealth,
    MountStatus,
    Publication,
)
from .ports import MountRepository

__all__ = [
    "DEFAULT_MOUNT_ID",
    "MountInUse",
    "MountNotFound",
    "MountService",
    "StorageError",
    "StoragePathResolver",
]

# Registering one of these as a library root is always a misconfiguration, and
# some of them hold secrets the container mounts for itself. Everything below
# a listed directory is refused too: `/run/secrets` is where the administrator
# password arrives.
PROTECTED_TREES = (
    "/bin",
    "/boot",
    "/dev",
    "/etc",
    "/lib",
    "/lib32",
    "/lib64",
    "/libx32",
    "/proc",
    "/root",
    "/run",
    "/sbin",
    "/sys",
)
# A whole system tree as a single library root is a mistake, but directories
# underneath one are ordinary places to keep media.
PROTECTED_EXACT = ("/home", "/opt", "/srv", "/tmp", "/usr", "/var")

MOUNT_NAME_LIMIT = 80

_DISCONNECTED_DETAIL = (
    "Disconnected: readers cannot see these libraries and scans skip them. "
    "The index, grants, and media stay untouched."
)


def _missing_detail(path: str) -> str:
    return (
        f"Nothing is mounted at {path}. Libraries stay indexed but cannot be "
        "scanned until the storage returns."
    )


class StorageError(ValueError):
    """A mount or a stored path cannot be used as asked."""


class MountNotFound(StorageError):
    """No mount has that identifier."""


class MountInUse(StorageError):
    """The mount cannot be changed while something still depends on it."""


class StorageRepository(MountRepository, Protocol):
    def managed_library(self, library_id: str) -> ManagedLibrary | None: ...


class StoragePathResolver:
    """Turns stable catalog identities into paths under their own mount."""

    def __init__(self, repository: StorageRepository, fallback_root: Path) -> None:
        self._repository = repository
        self._fallback_root = fallback_root

    def mount_for_library(self, library: ManagedLibrary) -> DataMount:
        mount = self._repository.data_mount(library.mount_id)
        if mount is None:
            raise StorageError("Data mount is no longer configured")
        return mount

    def library_root(self, library: ManagedLibrary) -> Path:
        mount = self.mount_for_library(library)
        return _safe_child(Path(mount.path), library.relative_path)

    def publication_root(self, publication: Publication) -> Path:
        library = self._library_for(publication)
        if library is None:
            return self._fallback_root
        return Path(self.mount_for_library(library).path)

    def publication_path(self, publication: Publication) -> Path:
        library = self._library_for(publication)
        if library is None:
            return _safe_relative(self._fallback_root, publication.relative_path)
        mount = self.mount_for_library(library)
        if not mount.enabled or not library.enabled:
            raise StorageError("Publication data mount is disconnected")
        return _safe_relative(Path(mount.path), publication.relative_path)

    def ingest_path(
        self, library: ManagedLibrary, category: str, series: str, filename: str
    ) -> Path:
        mount = self.mount_for_library(library)
        if not mount.enabled:
            raise StorageError("Data mount is disconnected")
        if not mount.allow_ingest:
            raise StorageError("Data mount is read-only")
        root = Path(mount.path)
        relative = PurePosixPath(library.relative_path, category, series, filename)
        target = _safe_relative(root, relative.as_posix())
        _reject_symlinks(root, target.parent)
        return target

    def _library_for(self, publication: Publication) -> ManagedLibrary | None:
        if publication.library_id is None:
            return None
        library = self._repository.managed_library(publication.library_id)
        if library is None:
            raise StorageError("Publication library is no longer configured")
        return library


class MountService:
    """Validates and manages the storage roots an administrator registers."""

    def __init__(
        self, repository: StorageRepository, state_dir: Path, default_root: Path
    ) -> None:
        self._repository = repository
        self._state_dir = state_dir
        self._default_root = default_root

    def initialize(self) -> DataMount:
        """Register the configured data directory as the default mount."""
        return self._repository.ensure_default_mount(
            str(self._default_root.resolve(strict=False))
        )

    def statuses(self) -> list[MountStatus]:
        usage = self._repository.mount_usage()
        return [
            self._status(mount, usage.get(mount.id, (0, 0, 0)))
            for mount in self._repository.data_mounts()
        ]

    def status(self, mount: DataMount) -> MountStatus:
        return self._status(mount, self._repository.mount_usage().get(mount.id))

    def add(
        self,
        name: str,
        path: str,
        *,
        allow_ingest: bool = False,
        scan_enabled: bool = True,
    ) -> DataMount:
        resolved = self._validate_path(path)
        self._require_writable_for_ingest(allow_ingest, resolved)
        return self._repository.add_data_mount(
            _mount_name(name),
            str(resolved),
            allow_ingest=allow_ingest,
            scan_enabled=scan_enabled,
        )

    def update(
        self,
        mount_id: str,
        *,
        name: str,
        path: str,
        allow_ingest: bool,
        scan_enabled: bool,
    ) -> DataMount:
        self._require_mount(mount_id)
        resolved = self._validate_path(path, exclude_id=mount_id)
        self._require_writable_for_ingest(allow_ingest, resolved)
        saved = self._repository.update_data_mount(
            mount_id,
            name=_mount_name(name),
            path=str(resolved),
            allow_ingest=allow_ingest,
            scan_enabled=scan_enabled,
        )
        if saved is None:  # pragma: no cover - guarded by _require_mount
            raise MountNotFound("Data mount not found")
        return saved

    def disconnect(self, mount_id: str) -> DataMount:
        self._require_mount(mount_id)
        saved = self._repository.set_data_mount_enabled(mount_id, False)
        if saved is None:  # pragma: no cover - guarded by _require_mount
            raise MountNotFound("Data mount not found")
        return saved

    def reconnect(self, mount_id: str) -> DataMount:
        mount = self._require_mount(mount_id)
        resolved = self._validate_path(mount.path, exclude_id=mount_id)
        self._require_writable_for_ingest(mount.allow_ingest, resolved)
        saved = self._repository.set_data_mount_enabled(mount_id, True)
        if saved is None:  # pragma: no cover - guarded by _require_mount
            raise MountNotFound("Data mount not found")
        return saved

    def forget(self, mount_id: str) -> DataMount:
        mount = self._require_mount(mount_id)
        if mount.is_default:
            raise StorageError(
                "The original data mount cannot be forgotten. It is the "
                "directory Nineveh was configured with."
            )
        if mount.enabled:
            raise MountInUse("Disconnect the data mount before forgetting it")
        saved = self._repository.forget_data_mount(mount_id)
        if saved is None:  # pragma: no cover - guarded by _require_mount
            raise MountNotFound("Data mount not found")
        return saved

    def _require_mount(self, mount_id: str) -> DataMount:
        mount = self._repository.data_mount(mount_id)
        if mount is None:
            raise MountNotFound("Data mount not found")
        return mount

    @staticmethod
    def _require_writable_for_ingest(allow_ingest: bool, resolved: Path) -> None:
        if allow_ingest and not _writable(resolved):
            raise StorageError("Ingest requires a writable data mount")

    def _status(
        self, mount: DataMount, usage: tuple[int, int, int] | None
    ) -> MountStatus:
        libraries, publications, size = usage or (0, 0, 0)
        health, detail = self._health(mount)
        writable = health is MountHealth.HEALTHY and _writable(Path(mount.path))
        if health is MountHealth.HEALTHY and mount.allow_ingest and not writable:
            detail = "Ingest is enabled, but this path is not writable"
        return MountStatus(
            mount=mount,
            health=health,
            writable=writable,
            detail=detail,
            library_count=libraries,
            publication_count=publications,
            size=size,
        )

    @staticmethod
    def _health(mount: DataMount) -> tuple[MountHealth, str | None]:
        if not mount.enabled:
            return MountHealth.DISCONNECTED, _DISCONNECTED_DETAIL
        path = Path(mount.path)
        if not path.is_dir():
            return MountHealth.MISSING, _missing_detail(mount.path)
        return MountHealth.HEALTHY, None

    def _validate_path(self, raw: str, *, exclude_id: str | None = None) -> Path:
        candidate = Path(raw.strip())
        if not candidate.is_absolute():
            raise StorageError("Enter an absolute path inside the container")
        # Before anything else, so "/etc is protected" is what an administrator
        # is told, rather than the incidental "/etc is a symbolic link".
        _reject_protected(candidate)
        if candidate.is_symlink():
            raise StorageError("A data mount cannot be a symbolic link")
        try:
            resolved = candidate.resolve(strict=True)
        except OSError as error:
            raise StorageError("Data mount directory does not exist") from error
        if not resolved.is_dir():
            raise StorageError("Data mount path must be a directory")
        # Again on the resolved path, so a link into a protected tree is
        # refused too.
        _reject_protected(resolved)
        self._reject_state_dir(resolved)
        self._reject_overlap(resolved, exclude_id)
        if not _readable(resolved):
            raise StorageError("Data mount directory is not readable")
        return resolved

    def _reject_state_dir(self, resolved: Path) -> None:
        if _overlaps(resolved, self._state_dir.resolve(strict=False)):
            raise StorageError("The state directory cannot be used as a data mount")

    def _reject_overlap(self, resolved: Path, exclude_id: str | None) -> None:
        for mount in self._repository.data_mounts():
            if mount.id == exclude_id:
                continue
            if _overlaps(resolved, Path(mount.path).resolve(strict=False)):
                raise StorageError(f"Data mount overlaps with {mount.name}")


def _mount_name(value: str) -> str:
    cleaned = " ".join(value.split())
    if not 1 <= len(cleaned) <= MOUNT_NAME_LIMIT:
        raise StorageError(f"Mount label must be 1-{MOUNT_NAME_LIMIT} characters")
    return cleaned


def _system_path(path: Path) -> PurePosixPath:
    """The path as the operating system names it.

    macOS resolves `/etc` to `/private/etc`, so the protected list would miss
    it without this. `/private/tmp` collapses to `/tmp` the same way.
    """
    posix = PurePosixPath(path.as_posix())
    if posix.parts[:2] == ("/", "private"):
        return PurePosixPath("/", *posix.parts[2:])
    return posix


def _reject_protected(resolved: Path) -> None:
    if resolved == Path(resolved.anchor):
        raise StorageError("The filesystem root cannot be used as a data mount")
    system = _system_path(resolved)
    if str(system) in PROTECTED_EXACT:
        raise StorageError(
            f"{system} is a system directory. Register the media directory "
            "inside it instead."
        )
    for tree in PROTECTED_TREES:
        if system == PurePosixPath(tree) or system.is_relative_to(tree):
            raise StorageError(f"{tree} is a protected system directory")


def _safe_child(root: Path, child: str) -> Path:
    if Path(child).name != child or child in {"", ".", ".."}:
        raise StorageError("Library path is not a direct child of its mount")
    return _safe_relative(root, child)


def _safe_relative(root: Path, value: str) -> Path:
    relative = PurePosixPath(value)
    if relative.is_absolute() or ".." in relative.parts or not relative.parts:
        raise StorageError("Stored media path is unsafe")
    candidate = root.joinpath(*relative.parts)
    try:
        candidate.resolve(strict=False).relative_to(root.resolve(strict=False))
    except ValueError as error:
        raise StorageError("Stored media path escapes its mount") from error
    return candidate


def _reject_symlinks(root: Path, parent: Path) -> None:
    current = root
    for part in parent.relative_to(root).parts:
        current = current / part
        if current.is_symlink():
            raise StorageError("Symbolic links cannot be used for ingest")


def _overlaps(left: Path, right: Path) -> bool:
    return left.is_relative_to(right) or right.is_relative_to(left)


def _readable(path: Path) -> bool:
    try:
        next(path.iterdir(), None)
    except OSError:
        return False
    return True


def _writable(path: Path) -> bool:
    # Advisory only. Atomic placement during ingest stays authoritative.
    return os.access(path, os.W_OK)
