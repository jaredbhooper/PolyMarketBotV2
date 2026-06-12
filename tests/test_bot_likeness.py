"""bot_likeness contract:
- A perfectly even 24/7 bot (regular intervals, uniform stake, crypto-only)
  scores >= 0.8.
- A human (sleeps 8h, irregular intervals, varied stake, varied categories)
  scores <= 0.4.
- Tiny histories return 0.0 (never auto-flag a brand new wallet).
- Weights are config-overridable.
"""
from __future__ import annotations

import math
import random

import pytest

from foundation.autopsy import (
    DEFAULT_BOT_LIKENESS_WEIGHTS, compute_bot_likeness,
)


NOW = 1750000000


def _t(ts: int, side: str, asset: str, price: float, size: float,
       slug: str) -> dict:
    return {
        "timestamp": ts, "side": side, "asset": asset,
        "price": price, "size": size,
        "slug": slug, "eventSlug": slug,
        "conditionId": asset,
    }


def make_fixed_bot(n: int = 480) -> list[dict]:
    """Perfectly even 24/7 - fires every hour for 20 days on BTC markets.
    Identical $50 notional each time."""
    rows = []
    ts = NOW - 86400 * 20
    for i in range(n):
        rows.append(_t(ts, "BUY", f"a_{i % 5}", price=0.5, size=100,
                        slug=f"btc-up-{i // 24}"))
        ts += 3600  # exactly one per hour
    return rows


def make_human(n: int = 200) -> list[dict]:
    """Active during 08:00-22:00 UTC (16h waking window), irregular
    intervals (30 min - 12 h), varied notional, multi-category."""
    random.seed(42)
    rows = []
    ts = NOW - 86400 * 60
    cats = ["nba-game", "trump-poll", "openai-model", "musk-event",
            "russia-ceasefire"]
    asset_i = 0
    while len(rows) < n:
        from datetime import datetime, timezone
        hr = datetime.fromtimestamp(ts, tz=timezone.utc).hour
        if 8 <= hr <= 22:
            cat = random.choice(cats)
            sz = random.choice([3.0, 12.0, 47.0, 8.0, 25.0])
            pr = random.uniform(0.2, 0.8)
            rows.append(_t(ts, "BUY", f"h_{asset_i}", price=pr, size=sz,
                            slug=f"{cat}-{asset_i}"))
            asset_i += 1
        ts += random.choice([1800, 3600, 7200, 18000, 43200])
    return rows


def test_fixed_bot_scores_high():
    rows = make_fixed_bot(n=480)
    bl = compute_bot_likeness(rows)
    assert bl["bot_likeness"] >= 0.8, (
        f"expected >=0.8 for perfectly even bot, got "
        f"{bl['bot_likeness']:.3f}; breakdown={bl}")


def test_human_scores_low():
    rows = make_human(n=200)
    bl = compute_bot_likeness(rows)
    assert bl["bot_likeness"] <= 0.4, (
        f"expected <=0.4 for irregular human, got "
        f"{bl['bot_likeness']:.3f}; breakdown={bl}")


def test_tiny_history_returns_zero():
    bl = compute_bot_likeness([])
    assert bl["bot_likeness"] == 0.0
    bl4 = compute_bot_likeness([{"timestamp": NOW, "side": "BUY",
                                   "asset": "a", "price": 0.5, "size": 5}] * 4)
    assert bl4["bot_likeness"] == 0.0


def test_weights_are_overridable():
    rows = make_fixed_bot(n=240)
    bl_default = compute_bot_likeness(rows)
    # Suppress the crypto-share component entirely; bot still bot-like
    # via the other four signals.
    bl_no_crypto = compute_bot_likeness(rows, weights={
        **DEFAULT_BOT_LIKENESS_WEIGHTS, "crypto_share": 0.0,
    })
    assert bl_no_crypto["bot_likeness"] > 0.5
    # And the crypto_share component should be the only diff direction.
    assert bl_default["crypto_share"] == bl_no_crypto["crypto_share"]


def test_breakdown_components_in_unit_range():
    rows = make_fixed_bot(n=240)
    bl = compute_bot_likeness(rows)
    for key in ("hour_entropy", "interval_regularity", "stake_uniformity",
                 "crypto_share", "trades_per_active_day"):
        v = bl[key]
        assert 0.0 <= v <= 1.0, f"{key}={v} outside [0,1]"
    assert 0.0 <= bl["bot_likeness"] <= 1.0


def test_score_monotone_with_more_signal():
    """A bot fixture with hour-of-day shifted to a single 6-hour window
    must score lower than the fully-even bot (less hour_entropy)."""
    rows_even = make_fixed_bot(n=240)
    # Bot that runs but only fires during a 6-hour window per day.
    rows_partial = []
    ts = NOW - 86400 * 20
    for i in range(240):
        from datetime import datetime, timezone
        hr = datetime.fromtimestamp(ts, tz=timezone.utc).hour
        if 0 <= hr < 6:
            rows_partial.append(_t(ts, "BUY", f"p_{i % 4}", price=0.5, size=100,
                                     slug=f"btc-up-{i}"))
        ts += 1800
    bl_even = compute_bot_likeness(rows_even)
    bl_partial = compute_bot_likeness(rows_partial)
    assert bl_even["hour_entropy"] > bl_partial["hour_entropy"]
