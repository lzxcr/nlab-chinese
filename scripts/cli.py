#!/usr/bin/env python3
"""Thin entry point: keeps 'python3 scripts/cli.py <command>' working.

The implementation lives in src/nlab_pipeline (see pyproject.toml); this file
only puts src/ on sys.path and delegates to nlab_pipeline.cli.main.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from nlab_pipeline.cli import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
