"""Decode one untrusted image in a process of its own, under resource limits.

Invoked by `SubprocessThumbnailRenderer`. The first line on stdout says whether
the limits took hold, so the parent can warn instead of silently running an
unconstrained decoder; exit status 3 means the image is too large.
"""

from __future__ import annotations

import sys
from pathlib import Path

EXIT_FAILED = 1
EXIT_USAGE = 2
EXIT_TOO_LARGE = 3


def apply_limits(memory_bytes: int, cpu_seconds: int) -> list[str]:
    """Lower address space, CPU time and open files; return what failed."""
    try:
        import resource
    except ImportError:  # pragma: no cover - Windows has no rlimits
        return ["resource limits are unavailable on this platform"]
    failed = []
    for name, wanted in (
        ("RLIMIT_AS", memory_bytes),
        ("RLIMIT_CPU", cpu_seconds),
        ("RLIMIT_NOFILE", 64),
    ):
        kind = getattr(resource, name)
        _, hard = resource.getrlimit(kind)
        ceiling = wanted if hard == resource.RLIM_INFINITY else min(wanted, hard)
        try:
            resource.setrlimit(kind, (ceiling, hard))
        except (OSError, ValueError) as error:  # macOS refuses a lower RLIMIT_AS
            failed.append(f"{name}: {error}")
    return failed


def main(arguments: list[str] | None = None) -> int:
    values = sys.argv[1:] if arguments is None else arguments
    if len(values) != 8:
        return EXIT_USAGE
    source, destination = Path(values[0]), Path(values[1])
    width, height, quality, max_pixels, memory, cpu = map(int, values[2:])
    failed = apply_limits(memory, cpu)
    print("limits: " + ("; ".join(failed) if failed else "applied"), flush=True)
    from .imaging import ImageTooLarge, render_webp

    try:
        render_webp(
            source,
            destination,
            box=(width, height),
            quality=quality,
            max_pixels=max_pixels,
        )
    except ImageTooLarge as error:
        print(error, file=sys.stderr)
        return EXIT_TOO_LARGE
    except (OSError, ValueError) as error:
        print(f"{type(error).__name__}: {error}", file=sys.stderr)
        return EXIT_FAILED
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
