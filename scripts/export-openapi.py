#!/usr/bin/env python3
"""Regenerate the committed API contract.

    python scripts/export-openapi.py

`tests/test_contract.py` fails if the committed file and the live route table
disagree, so run this after adding, removing, or re-shaping an endpoint.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from nineveh.app import openapi_document

DESTINATION = Path(__file__).resolve().parent.parent / "docs" / "openapi.json"


def main() -> None:
    DESTINATION.parent.mkdir(parents=True, exist_ok=True)
    document = json.dumps(openapi_document(), indent=2, sort_keys=True) + "\n"
    DESTINATION.write_text(document, encoding="utf-8")
    print(f"{DESTINATION} ({len(document.splitlines())} lines)")


if __name__ == "__main__":
    main()
