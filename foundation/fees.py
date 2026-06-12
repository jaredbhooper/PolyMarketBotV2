"""Fee schedules for Polymarket AND Kalshi.

POLYMARKET (verified 2026-03 update, sources documented in BUILD_NOTES.md).

  fee_per_share_usd = rate × p × (1 - p)

`rate` depends on the market category (peaks at p=0.50, drops toward
zero near 0 and 1). Maker fills earn a rebate of ~20-25% of the
counterparty's taker fee (we model the maker side as zero fee + zero
rebate by default to stay conservative).

For unclassified markets we fall back to `default_taker_rate` = 0.01.

KALSHI (verified 2026-04 fee schedule via kalshi.com/docs/kalshi-fee-schedule.pdf
and corroborated by marketmath.io 2026 guide; see BUILD_NOTES.md):

  taker_fee_per_contract_usd = ceil(fee_multiplier × 0.07 × p × (1-p) × 100) / 100
  maker_fee_per_contract_usd = ceil(fee_multiplier × 0.0175 × p × (1-p) × 100) / 100

Where `fee_multiplier` is per-series (default 1.0). Some premium series
(e.g. crypto, sports) carry multipliers > 1.0 published in the series
endpoint as `fee_multiplier`. Rounded UP to the next whole cent.

The Kalshi venue adapter reads `series.fee_multiplier` and passes it in.
"""
from __future__ import annotations

import math


DEFAULT_TAKER_RATES = {
    "crypto":     0.0180,
    "economics":  0.0150,
    "mentions":   0.0156,
    "culture":    0.0125,
    "weather":    0.0125,
    "finance":    0.0100,
    "politics":   0.0100,
    "tech":       0.0100,
    "sports":     0.0075,
    "geopolitics": 0.0000,
}
DEFAULT_TAKER_FALLBACK = 0.0100   # 1% conservative when category unknown.

# Categories Polymarket Gamma tags / our internal slug heuristics might use.
CATEGORY_ALIASES = {
    "highest-temperature": "weather",
    "lowest-temperature":  "weather",
    "climate and weather": "weather",
    "nba": "sports", "nfl": "sports", "mlb": "sports", "ufc": "sports",
    "soccer": "sports", "csgo": "sports", "dota": "sports",
    "lol": "sports", "esports": "sports",
    "elections": "politics", "election": "politics",
    "ai": "tech",
    "btc": "crypto", "eth": "crypto",
}


def normalize_category(text: str | None) -> str:
    t = (text or "").lower().strip()
    if not t:
        return ""
    for alias, canon in CATEGORY_ALIASES.items():
        if alias in t:
            return canon
    if t in DEFAULT_TAKER_RATES:
        return t
    return ""


def taker_rate(category: str | None,
                 rates: dict[str, float] | None = None,
                 fallback: float = DEFAULT_TAKER_FALLBACK) -> float:
    """Look up the per-share fee rate for a category."""
    table = rates or DEFAULT_TAKER_RATES
    canon = normalize_category(category)
    if not canon:
        return fallback
    return float(table.get(canon, fallback))


def polymarket_taker_fee_per_share(price: float,
                                      category: str | None = None,
                                      rates: dict[str, float] | None = None,
                                      fallback: float = DEFAULT_TAKER_FALLBACK
                                      ) -> float:
    """fee_per_share = rate × p × (1 - p). Returns USD per contract."""
    if not (0.0 < price < 1.0):
        return 0.0
    return taker_rate(category, rates, fallback) * price * (1.0 - price)


# ----------------------------------------------------------------- Kalshi
# Pinned to the Kalshi fee schedule published 2026-04
# (kalshi.com/docs/kalshi-fee-schedule.pdf). If the upstream rate changes,
# update KALSHI_TAKER_BASE / KALSHI_MAKER_BASE here AND update the pinned
# test in tests/test_fees.py — the test is the regression guard.
KALSHI_TAKER_BASE = 0.07
KALSHI_MAKER_BASE = 0.0175


def kalshi_fee_per_contract(price: float, multiplier: float = 1.0,
                              maker: bool = False) -> float:
    """Kalshi fee per contract in USD.

      taker = ceil(multiplier * 0.07 * p * (1-p) * 100) / 100
      maker = ceil(multiplier * 0.0175 * p * (1-p) * 100) / 100

    multiplier comes from series.fee_multiplier (default 1.0; premium
    series like crypto carry higher multipliers). Settlement does not
    incur a fee (price == 0.0 or 1.0 returns 0)."""
    if not (0.0 < price < 1.0):
        return 0.0
    base = KALSHI_MAKER_BASE if maker else KALSHI_TAKER_BASE
    raw = multiplier * base * price * (1.0 - price)
    return math.ceil(raw * 100.0) / 100.0
