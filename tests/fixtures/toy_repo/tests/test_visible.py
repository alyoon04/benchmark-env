"""Visible test: NOT in a hidden bucket, so the solver is allowed to see and run it."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from calc import add


def test_add_visible() -> None:
    assert add(1, 2) == 3
