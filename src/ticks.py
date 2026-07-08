"""IDX tick size (fraksi harga) helpers.

The Indonesia Stock Exchange uses a tiered tick size that depends on the price
band, so moving "N ticks" must be done one tick at a time, re-evaluating the
tick size as the price crosses a band boundary.

Bands (current regime):
    price < 200          -> 1
    200  <= price < 500  -> 2
    500  <= price < 2000 -> 5
    2000 <= price < 5000 -> 10
    price >= 5000        -> 25
"""
from __future__ import annotations

# (upper_bound_exclusive, tick) ordered ascending; None = no upper bound.
_TICK_BANDS = [
    (200, 1),
    (500, 2),
    (2000, 5),
    (5000, 10),
    (None, 25),
]


def tick_size(price: int) -> int:
    """Return the IDX tick size for a given price."""
    for upper, tick in _TICK_BANDS:
        if upper is None or price < upper:
            return tick
    return _TICK_BANDS[-1][1]


def add_ticks(price: int, ticks: int) -> int:
    """Move ``price`` by ``ticks`` IDX ticks (positive = up, negative = down).

    Stepping one tick at a time so band boundaries are respected.
    """
    result = int(price)
    step = 1 if ticks >= 0 else -1
    for _ in range(abs(ticks)):
        if step > 0:
            result += tick_size(result)
        else:
            # When moving down, the tick that applies is the one just below.
            result -= tick_size(result - 1)
    return result
