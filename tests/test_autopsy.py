"""Synthetic histories per archetype; assert the classifier emits the
correct label + non-empty evidence."""
from __future__ import annotations

import math
import random
import time

import pytest

from foundation.autopsy import (
    DEFAULT_AUTOPSY_CFG, classify, fingerprint, ARCHETYPE_ANALOGUE,
)


NOW = 1750000000  # fixed clock for determinism


def _trade(ts: int, side: str, asset: str, price: float, size: float = 5.0,
           slug: str = "x", cond: str | None = None,
           tx: str | None = None) -> dict:
    return {
        "timestamp": ts, "side": side, "asset": asset,
        "price": price, "size": size,
        "slug": slug, "eventSlug": slug,
        "conditionId": cond or asset,
        "transactionHash": tx or f"0x{ts}{asset}{side}",
    }


# -------------------------------------------------------------- fixtures
def make_speed_reactor(n: int = 200) -> list[dict]:
    """Sub-minute median interval, diverse categories."""
    cats = ["nba", "trump", "btc", "openai", "russia"]
    rows = []
    ts = NOW - 86400 * 30
    for i in range(n):
        cat = cats[i % len(cats)]
        rows.append(_trade(ts, "BUY", f"asset_{i}", price=0.4 + 0.1 * (i % 3),
                            slug=f"{cat}-event-{i}", cond=f"cond_{i % 50}"))
        ts += random.choice([5, 12, 20, 40, 35, 50])
    return rows


def make_market_maker(n: int = 200) -> list[dict]:
    """Two-sided quoting: BUY+SELL on same asset within short windows.
    Spread across a handful of assets, regular cadence."""
    rows = []
    ts = NOW - 86400 * 60
    assets = [f"mm_asset_{k}" for k in range(4)]
    for i in range(n // 2):
        a = assets[i % len(assets)]
        rows.append(_trade(ts, "BUY", a, price=0.50, size=10,
                            slug="celeb-musk", cond=a))
        rows.append(_trade(ts + 30, "SELL", a, price=0.52, size=10,
                            slug="celeb-musk", cond=a))
        ts += 600
    return rows


def make_arbitrageur(n: int = 200) -> list[dict]:
    """Many BUY pairs on DIFFERENT assets within seconds."""
    rows = []
    ts = NOW - 86400 * 40
    for i in range(n // 2):
        rows.append(_trade(ts, "BUY", f"arb_a_{i}", price=0.40, size=20,
                            slug="election-2026", cond=f"cond_a_{i}"))
        rows.append(_trade(ts + 3, "BUY", f"arb_b_{i}", price=0.62, size=20,
                            slug="election-2026", cond=f"cond_b_{i}"))
        ts += 500
    return rows


def make_sharp_line_taker(n: int = 60) -> list[dict]:
    """Mid-price BUYs, holds to resolution (no SELLs), diverse markets so
    dominant_share stays well below the niche threshold."""
    rows = []
    ts = NOW - 86400 * 90
    cats = ["nba-game", "openai-model", "trump-poll",
            "russia-ceasefire", "musk-event"]
    for i in range(n):
        rows.append(_trade(ts, "BUY", f"sharp_{i}", price=0.45 + (i % 3) * 0.05,
                            slug=f"{cats[i % len(cats)]}-{i}",
                            cond=f"cond_sharp_{i}"))
        ts += 3600 * 8  # 1 trade every 8 hours
    return rows


def make_niche_judgment(n: int = 50) -> list[dict]:
    """One category dominates. Long holds (no SELLs). Infrequent."""
    rows = []
    ts = NOW - 86400 * 120
    for i in range(n):
        rows.append(_trade(ts, "BUY", f"niche_{i}", price=0.55,
                            slug=f"crypto-btc-{i}",
                            cond=f"cond_niche_{i}"))
        ts += 86400 * 3  # one per 3 days
    return rows


def make_endgame_grinder(n: int = 100) -> list[dict]:
    """Most BUYs at extreme prices (>0.85 or <0.15)."""
    rows = []
    ts = NOW - 86400 * 30
    for i in range(n):
        p = 0.90 if i % 2 == 0 else 0.08
        rows.append(_trade(ts, "BUY", f"endgame_{i}", price=p,
                            slug=f"nba-game-{i}",
                            cond=f"cond_eg_{i}"))
        ts += 1800
    return rows


def make_mixed(n: int = 80) -> list[dict]:
    """A bit of everything; no archetype dominates."""
    rows = []
    ts = NOW - 86400 * 60
    for i in range(n):
        side = "BUY" if i % 3 else "SELL"
        cat = ["nba", "openai", "russia", "musk", "btc"][i % 5]
        rows.append(_trade(ts, side, f"mix_{i % 12}", price=0.40 + (i % 5) * 0.08,
                            slug=f"{cat}-x-{i}",
                            cond=f"mix_cond_{i % 12}"))
        ts += 3600 * 4
    return rows


# -------------------------------------------------------------- tests
def test_empty_trades():
    fp = fingerprint([], now_ts=NOW)
    assert fp["n_trades"] == 0
    arch, conf, ev = classify(fp)
    assert arch == "mixed"
    assert conf == 0.0
    assert any("insufficient_data" in e for e in ev)


def test_below_min_trades_falls_back_to_mixed():
    trades = make_speed_reactor(n=10)
    fp = fingerprint(trades, now_ts=NOW)
    arch, conf, _ = classify(fp)
    assert arch == "mixed"
    assert conf == 0.0


def test_speed_reactor():
    random.seed(0)
    trades = make_speed_reactor(n=200)
    fp = fingerprint(trades, now_ts=NOW)
    arch, conf, ev = classify(fp)
    assert arch == "speed-reactor"
    assert conf > 0.0
    assert any("median_interval" in e for e in ev)
    assert any("n_categories_active" in e for e in ev)


def test_market_maker():
    trades = make_market_maker(n=200)
    fp = fingerprint(trades, now_ts=NOW)
    assert fp["two_sided_pairs"] > 0
    arch, conf, ev = classify(fp)
    assert arch == "market-maker"
    assert conf > 0.0
    assert any("two_sided_rate" in e for e in ev)


def test_arbitrageur():
    trades = make_arbitrageur(n=200)
    fp = fingerprint(trades, now_ts=NOW)
    assert fp["offsetting_pairs"] > 0
    arch, conf, ev = classify(fp)
    assert arch == "arbitrageur"
    assert conf > 0.0
    assert any("offsetting_rate" in e for e in ev)


def test_sharp_line_taker():
    trades = make_sharp_line_taker(n=60)
    fp = fingerprint(trades, now_ts=NOW)
    arch, conf, ev = classify(fp)
    assert arch == "sharp-line-taker"
    assert conf > 0.0
    assert any("pct_held_to_end" in e for e in ev)


def test_niche_judgment():
    trades = make_niche_judgment(n=50)
    fp = fingerprint(trades, now_ts=NOW)
    assert fp["dominant_share"] >= 0.7
    arch, conf, ev = classify(fp)
    assert arch == "niche-judgment"
    assert conf > 0.0
    assert any("dominant_category=crypto" in e for e in ev)


def test_endgame_grinder():
    trades = make_endgame_grinder(n=100)
    fp = fingerprint(trades, now_ts=NOW)
    assert fp["share_entry_extreme"] >= 0.5
    arch, conf, ev = classify(fp)
    assert arch == "endgame-grinder"
    assert conf > 0.0


def test_mixed_default():
    trades = make_mixed(n=80)
    fp = fingerprint(trades, now_ts=NOW)
    arch, _conf, ev = classify(fp)
    # The mixed bag may map to any low-confidence archetype, but it
    # shouldn't crash and the evidence list is always populated.
    assert arch in ARCHETYPE_ANALOGUE
    assert len(ev) >= 1


def test_archetype_analogue_coverage():
    """Every archetype the classifier can return must have an analogue
    mapping or _print_autopsy_single will KeyError."""
    valid = {"speed-reactor", "market-maker", "arbitrageur",
             "sharp-line-taker", "niche-judgment", "endgame-grinder", "mixed"}
    assert set(ARCHETYPE_ANALOGUE.keys()) == valid


def test_two_sided_rate_zero_for_buy_only():
    """A wallet that only ever BUYs (e.g. niche-judgment) must have
    two_sided_rate == 0, never accidentally triggering MM."""
    trades = make_niche_judgment(n=50)
    fp = fingerprint(trades, now_ts=NOW)
    assert fp["two_sided_pairs"] == 0
    assert fp["two_sided_rate"] == 0.0


def test_offsetting_rate_zero_when_buys_spaced():
    """Sequential BUYs >arb_window_sec apart must not register as arb."""
    rows = []
    ts = NOW - 86400 * 30
    for i in range(50):
        rows.append(_trade(ts, "BUY", f"slow_{i}", price=0.5,
                            slug="trump-2028", cond=f"slow_{i}"))
        ts += DEFAULT_AUTOPSY_CFG["arb_window_sec"] + 100
    fp = fingerprint(rows, now_ts=NOW)
    assert fp["offsetting_pairs"] == 0


def test_classify_returns_evidence_strings():
    """Evidence list must be a list of strings (not tuples or None)."""
    trades = make_sharp_line_taker(n=60)
    fp = fingerprint(trades, now_ts=NOW)
    _, _, ev = classify(fp)
    assert all(isinstance(e, str) and len(e) > 0 for e in ev)


def test_hold_time_for_buy_only_hits_held_to_end():
    """A BUY with no matching SELL within history should be counted
    as held-to-end (the held_to_end count drives sharp-line-taker)."""
    trades = make_sharp_line_taker(n=40)
    fp = fingerprint(trades, now_ts=NOW)
    assert fp["pct_held_to_end"] == 1.0
