"""One WebP resize, shared by the in-process renderer and the isolated worker.

Kept free of the rest of Nineveh so the worker process can import it after it
has lowered its own resource limits.
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path
from typing import BinaryIO

from PIL import Image, ImageOps


class ImageTooLarge(ValueError):
    pass


def render_webp(
    source: BinaryIO | Path,
    destination: Path,
    *,
    box: tuple[int, int],
    quality: int,
    max_pixels: int,
) -> None:
    """Fit `source` inside `box` and publish it at `destination` atomically."""
    destination.parent.mkdir(parents=True, exist_ok=True)
    with Image.open(source) as opened:
        if opened.width * opened.height > max_pixels:
            raise ImageTooLarge("image dimensions exceed the configured limit")
        image = ImageOps.exif_transpose(opened)
        image.thumbnail(box, Image.Resampling.LANCZOS)
        if image.mode not in ("RGB", "RGBA"):
            image = image.convert("RGBA" if "transparency" in image.info else "RGB")
        with tempfile.NamedTemporaryFile(
            prefix="render-", suffix=".webp", dir=destination.parent, delete=False
        ) as temporary:
            temporary_path = Path(temporary.name)
        try:
            image.save(temporary_path, format="WEBP", quality=quality, method=4)
            os.replace(temporary_path, destination)
        finally:
            temporary_path.unlink(missing_ok=True)
