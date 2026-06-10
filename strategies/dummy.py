"""Dummy strategy used to test the foundation pipeline before weather exists.

Returns a fixed probability for any market matching a slug substring -
useful for verifying the edge engine and fill logic against real books.
"""
from __future__ import annotations

from strategies.base import Estimate, Market, Strategy


class DummyStrategy(Strategy):
    name = "dummy"

    def __init__(self, cfg: dict):
        s = (cfg.get("strategies") or {}).get("dummy", {})
        self.fixed_p = float(s.get("fixed_p", 0.65))
        self.fixed_confidence = float(s.get("fixed_confidence", 1.0))
        self.match_slug = s.get("match_slug", "highest-temperature")
        self.edge_threshold = float(s.get("edge_threshold", 0.08))
        self.kelly_fraction = float(s.get("kelly_fraction", 0.15))

    def relevant_markets(self, markets):
        return [m for m in markets if self.match_slug in (m.slug or "")
                                  or self.match_slug in (m.extras.get("event_slug") or "")]

    def estimate(self, market: Market) -> Estimate | None:
        return Estimate(
            p_final=self.fixed_p,
            confidence=self.fixed_confidence,
            metadata={"source": "dummy_fixed_p"},
        )


def build(cfg: dict) -> Strategy:
    return DummyStrategy(cfg)
