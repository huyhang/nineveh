#!/usr/bin/env python3
"""Regenerate the committed API contracts.

    python scripts/export-openapi.py

Writes two files. `docs/openapi.json` is the whole published surface.
`docs/librarian-openapi.json` is the slice a librarian agent consumes, so a
client in another repository can vendor a contract that moves only when its
own endpoints move -- not every time an unrelated one does.

`tests/test_contract.py` fails if either file and the live route table
disagree, so run this after adding, removing, or re-shaping an endpoint.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from nineveh.app import openapi_document, tagged_contract

DOCS = Path(__file__).resolve().parent.parent / "docs"
AGENT_TAG = "librarian"


def write(destination: Path, document: dict) -> None:
    rendered = json.dumps(document, indent=2, sort_keys=True) + "\n"
    destination.write_text(rendered, encoding="utf-8")
    print(f"{destination} ({len(rendered.splitlines())} lines)")


def main() -> None:
    DOCS.mkdir(parents=True, exist_ok=True)
    document = openapi_document()
    write(DOCS / "openapi.json", document)
    write(DOCS / f"{AGENT_TAG}-openapi.json", tagged_contract(AGENT_TAG, document))


if __name__ == "__main__":
    main()
