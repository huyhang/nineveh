#!/usr/bin/env python3
"""Regenerate the committed API contracts.

    python scripts/export-openapi.py

`docs/openapi.json` is the whole published surface. Beside it, one
`docs/<name>-openapi.json` per entry in `CONTRACT_SLICES`: the librarian
agent's slice and the native reading app's. A client in another repository
vendors its own slice, so its copy moves only when its own endpoints move --
not every time an unrelated one does.

`tests/test_contract.py` fails if any file and the live route table
disagree, so run this after adding, removing, or re-shaping an endpoint.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from nineveh.app import CONTRACT_SLICES, openapi_document, tagged_contract

DOCS = Path(__file__).resolve().parent.parent / "docs"


def write(destination: Path, document: dict) -> None:
    rendered = json.dumps(document, indent=2, sort_keys=True) + "\n"
    destination.write_text(rendered, encoding="utf-8")
    print(f"{destination} ({len(rendered.splitlines())} lines)")


def main() -> None:
    DOCS.mkdir(parents=True, exist_ok=True)
    document = openapi_document()
    write(DOCS / "openapi.json", document)
    for name, tags in CONTRACT_SLICES.items():
        write(DOCS / f"{name}-openapi.json", tagged_contract(name, tags, document))


if __name__ == "__main__":
    main()
