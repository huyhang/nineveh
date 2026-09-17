from __future__ import annotations

import argparse
import struct
import zipfile
import zlib
from pathlib import Path

ISSUES = (
    {
        "filename": "Nineveh Adventures 001.cbz",
        "title": "The Gates of Nineveh",
        "number": "1",
        "summary": "A courier discovers a hidden passage beneath the ancient city.",
        "colors": ((151, 58, 45), (223, 154, 76), (55, 83, 109)),
    },
    {
        "filename": "Nineveh Adventures 002.cbz",
        "title": "The Lion's Road",
        "number": "2",
        "summary": "The journey continues beyond the walls and into the eastern hills.",
        "colors": ((47, 95, 86), (187, 126, 63), (92, 62, 99)),
    },
)


def png(width: int, height: int, color: tuple[int, int, int], page: int) -> bytes:
    rows = []
    accent = tuple(min(channel + 55, 255) for channel in color)
    for y in range(height):
        stripe = (y // 90 + page) % 3 == 0
        row_color = accent if stripe else color
        rows.append(b"\x00" + bytes(row_color) * width)
    raw = b"".join(rows)
    return b"\x89PNG\r\n\x1a\n" + b"".join(
        (
            chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)),
            chunk(b"IDAT", zlib.compress(raw, level=9)),
            chunk(b"IEND", b""),
        )
    )


def chunk(kind: bytes, data: bytes) -> bytes:
    checksum = zlib.crc32(kind + data) & 0xFFFFFFFF
    return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", checksum)


def comic_info(issue: dict[str, object]) -> str:
    return f"""<?xml version="1.0" encoding="utf-8"?>
<ComicInfo>
  <Title>{issue["title"]}</Title>
  <Series>Nineveh Adventures</Series>
  <Number>{issue["number"]}</Number>
  <Summary>{issue["summary"]}</Summary>
  <Writer>Nineveh Sample Author</Writer>
  <Pages>
    <Page Image="0" Type="FrontCover" />
    <Page Image="1" />
    <Page Image="2" />
  </Pages>
</ComicInfo>
"""


def create_issue(destination: Path, issue: dict[str, object]) -> None:
    colors = issue["colors"]
    assert isinstance(colors, tuple)
    with zipfile.ZipFile(destination, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("ComicInfo.xml", comic_info(issue))
        for page_number, color in enumerate(colors, start=1):
            archive.writestr(
                f"pages/{page_number:03}.png",
                png(600, 900, color, page_number),
            )


def main() -> None:
    parser = argparse.ArgumentParser(description="Create two sample Nineveh CBZ issues")
    parser.add_argument("root", nargs="?", type=Path, default=Path("example-data"))
    args = parser.parse_args()
    series = args.root / "Sample Library" / "comics" / "Nineveh Adventures"
    series.mkdir(parents=True, exist_ok=True)
    for issue in ISSUES:
        destination = series / str(issue["filename"])
        create_issue(destination, issue)
        print(destination)


if __name__ == "__main__":
    main()
