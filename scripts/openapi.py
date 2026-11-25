"""Generate or check the committed dashboard OpenAPI document.

Usage:
    python scripts/openapi.py
    python scripts/openapi.py --check
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Optional

ROOT = Path(__file__).resolve().parents[1]
TARGET = ROOT / "docs" / "resources" / "reference" / "dashboard-openapi.json"


def rendered_schema() -> str:
    """Return the canonical formatted dashboard OpenAPI document."""
    from heavenbase.api import openapi_schema

    return json.dumps(openapi_schema(), indent=2, sort_keys=True, ensure_ascii=False) + "\n"


def main(argv: Optional[list[str]] = None) -> int:
    """Generate or check the committed OpenAPI document."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="Fail when the committed document is stale.")
    args = parser.parse_args(argv)
    rendered = rendered_schema()
    if args.check:
        current = TARGET.read_text(encoding="utf-8") if TARGET.exists() else ""
        if current != rendered:
            print(f"error: {TARGET.relative_to(ROOT)} is stale; run python scripts/openapi.py", file=sys.stderr)
            return 1
        print(f"[openapi] {TARGET.relative_to(ROOT)} is current")
        return 0
    TARGET.write_text(rendered, encoding="utf-8")
    print(f"[openapi] wrote {TARGET.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
