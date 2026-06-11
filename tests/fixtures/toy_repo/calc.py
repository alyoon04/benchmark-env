"""A tiny calculator with a deliberate bug: divide() multiplies instead of dividing.

The toy task's golden patch fixes divide(); fail2pass tests cover it, pass2pass
tests cover add()/subtract().
"""


def add(a: float, b: float) -> float:
    return a + b


def subtract(a: float, b: float) -> float:
    return a - b


def divide(a: float, b: float) -> float:
    return a * b
