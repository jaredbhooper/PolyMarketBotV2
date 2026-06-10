"""Strategy #6 - LP-SIM: liquidity-rewards scoring simulator (paper only).

Polymarket pays daily liquidity rewards for resting limit orders near
the midpoint (paid even if never filled), plus maker rebates on fills,
and makers pay zero fees. We can't earn real rewards on paper, but we
CAN simulate the quoting strategy and compute our would-be reward
score, to learn which markets and quote-widths would pay before any
real capital exists.

Honesty rule (same as Sharpline): a simulated maker fill counts ONLY
when observed price trades STRICTLY THROUGH our quote. Every reward
and P&L figure is labeled ESTIMATE in the database and the report.

Reward formula (per Polymarket docs - implementation marker v1):
  - One-sided depth in the reward band scores half-credit.
  - Two-sided depth scores full credit.
  - Tighter spread quadratic-boosts the score.
  - Per-minute sampling -> we approximate via the snapshot at cycle
    time and assume that's the day's average exposure (acknowledged
    estimate).

Competition caveat: we cannot see other makers' scores; we approximate
the competitive denominator using visible book depth within the reward
band as a proxy for total scored exposure. Documented in the row.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

from strategies.base import Estimate, Market, Strategy


@dataclass
class LPSimConfig:
    max_markets: int = 10
    quote_size_usd: float = 25.0
    quote_spread_cents: float = 0.02     # quote at mid +/- this
    reward_band_cents: float = 0.05      # within this of mid scores
    daily_pool_usd_default: float = 50.0


def score_quote(quote_spread: float, quote_size: float,
                  two_sided: bool, reward_band: float = 0.05) -> float:
    """Polymarket reward score (v1 implementation per published docs).

    Tighter spread => quadratically higher score. Two-sided depth =
    full credit; one-sided = half credit. Size enters linearly subject
    to the reward-band cap."""
    if quote_spread <= 0 or quote_size <= 0 or reward_band <= 0:
        return 0.0
    # spread factor: quadratic, capped at reward_band.
    spread_clipped = min(quote_spread, reward_band)
    spread_factor = (reward_band - spread_clipped) ** 2 / reward_band ** 2
    size_factor = quote_size
    sided_factor = 1.0 if two_sided else 0.5
    return spread_factor * size_factor * sided_factor


class LPSim(Strategy):
    name = "lp_sim"

    def __init__(self, cfg: dict):
        s = (cfg.get("strategies") or {}).get(self.name, {})
        self.params = LPSimConfig(
            max_markets=int(s.get("max_markets", 10)),
            quote_size_usd=float(s.get("quote_size_usd", 25.0)),
            quote_spread_cents=float(s.get("quote_spread_cents", 0.02)),
            reward_band_cents=float(s.get("reward_band_cents", 0.05)),
            daily_pool_usd_default=float(s.get("daily_pool_usd_default", 50.0)),
        )

    def relevant_markets(self, markets: list[Market]) -> list[Market]:
        return []

    def estimate(self, market: Market) -> Estimate | None:
        return None

    def run(self, ledger, markets: list[Market], verbose: bool = False
              ) -> dict[str, Any]:
        # Rank markets by depth-near-midpoint (a proxy for reward
        # eligibility / liquidity).
        scored: list[tuple[Market, float, float]] = []
        for m in markets:
            if m.yes_ask is None or m.yes_bid is None:
                continue
            mid = (float(m.yes_ask) + float(m.yes_bid)) / 2.0
            two_sided = bool(m.yes_book and m.no_book)
            # Visible competitive denominator: depth within reward band.
            depth_in_band = sum(
                float(L["price"]) * float(L["size"]) for L in (m.yes_book or [])
                if abs(float(L["price"]) - mid) <= self.params.reward_band_cents)
            our_score = score_quote(self.params.quote_spread_cents,
                                      self.params.quote_size_usd,
                                      two_sided=two_sided,
                                      reward_band=self.params.reward_band_cents)
            scored.append((m, our_score, depth_in_band + our_score))
        scored.sort(key=lambda x: x[1], reverse=True)
        rows_logged = 0
        for m, sc, comp in scored[: self.params.max_markets]:
            share = sc / comp if comp > 0 else 0.0
            est_reward = share * self.params.daily_pool_usd_default
            # We don't simulate fills in v1 - just record the reward
            # estimate + spread P&L estimate (= 0 in static mode).
            ledger.record_lp_sim({
                "poly_market_id": m.market_id,
                "quote_spread": self.params.quote_spread_cents,
                "quote_size": self.params.quote_size_usd,
                "score": sc,
                "est_share_of_pool": share,
                "est_daily_reward_usd": est_reward,
                "est_trading_pnl_usd": 0.0,
                "adverse_selection_usd": 0.0,
            })
            rows_logged += 1
        if verbose:
            print(f"  lp_sim: {rows_logged} markets quoted (ESTIMATES)")
        return {"rows_logged": rows_logged}


def build(cfg: dict) -> Strategy:
    return LPSim(cfg)
