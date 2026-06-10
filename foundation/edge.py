"""Edge utilities. Pure functions; the executor decides what to do with them."""
from __future__ import annotations


def yes_edge(p_model: float, yes_ask: float | None) -> float | None:
    if yes_ask is None:
        return None
    return float(p_model) - float(yes_ask)


def no_edge(p_model: float, no_ask: float | None) -> float | None:
    if no_ask is None:
        return None
    return (1.0 - float(p_model)) - float(no_ask)
