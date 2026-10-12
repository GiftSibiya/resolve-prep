#!/usr/bin/env python3
"""Thin launcher for VidPrep — keeps install paths stable."""

from __future__ import annotations

import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from vidprep.app import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
