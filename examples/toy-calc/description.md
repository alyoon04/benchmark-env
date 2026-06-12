# Bug: `divide()` returns wrong results

Users report that `calc.divide(a, b)` returns incorrect values — for example
`divide(6, 3)` returns `18` instead of `2`.

Fix `divide()` in `calc.py` so it returns the quotient of `a` divided by `b`.
Existing behavior of `add()` and `subtract()` must not change.
