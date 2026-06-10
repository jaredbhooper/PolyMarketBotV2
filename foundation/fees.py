"""Polymarket fee schedule (verified 2026-03 update, sources documented
in BUILD_NOTES.md).

Polymarket CLOB charges a quadratic taker fee, identical structurally
to Kalshi's:

    fee_per_share_usd = rate × p × (1 - p)

where `rate` depends on the market category (peaks at p=0.50, drops
toward zero near 0 and 1, so longshot legs barely pay).

Maker fills earn a rebate of ~20-25% of the counterparty's taker fee
(no out-of-pocket cost). We model the maker side as zero fee + zero
rebate by default to stay conservative; flip `maker_rebate_pct` in
config if you want to take credit for the rebate.

For markets whose category we can't classify (e.g. local sports keys,
mentions sub-tags), we fall back to `default_taker_rate` (0.01 = 1%).
"""
from __future__ import annotations


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
