"""Hidden fail2pass tests: fail on the buggy baseline, pass once divide() is fixed."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from calc import divide


def test_divide_integers() -> None:
    assert divide(6, 3) == 2


def test_divide_fractional() -> None:
    assert divide(7, 2) == 3.5
