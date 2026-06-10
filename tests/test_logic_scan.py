"""LOGIC-SCAN (Prompt C, Phase 3) tests."""
from __future__ import annotations

import os
import sys
import tempfile

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from foundation.ledger import Ledger
from strategies.base import Market
from strategies.logic_scan import LogicScan, detect_pair


def _temp_ledger():
    f = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    f.close()
    return Ledger(f.name), f.name


def _mk(id_, question, slug, event="2026-world-cup", yes_ask=0.5):
    return Market(
        market_id=id_, slug=slug, question=question, category="x",
        rules_text="", resolve_date=None, end_date_iso=None,
        yes_token_id="y", no_token_id="n",
        yes_ask=yes_ask, yes_bid=yes_ask - 0.01,
        no_ask=1 - (yes_ask - 0.01), no_bid=1 - yes_ask,
        extras={"event_slug": event})


# ---------------------------------------------------------- detect_pair
def test_detect_champion_vs_finalist():
    a = _mk("a", "Will France win the World Cup?",
             "france-wins-world-cup", event="2026-world-cup")
    b = _mk("b", "Will France reach the final?",
             "france-reaches-final", event="2026-world-cup")
    res = detect_pair(a, b)
    assert res is not None
    assert res["template"] == "champion_vs_finalist"
    assert res["confidence"] >= 0.95


def test_detect_presidency_vs_nomination():
    a = _mk("a", "Will Smith win the presidency in 2028?",
             "smith-wins-presidency-2028", event="presidency-2028")
    b = _mk("b", "Will Smith win the nomination in 2028?",
             "smith-wins-nomination-2028", event="presidency-2028")
    res = detect_pair(a, b)
    assert res is not None
    assert res["template"] == "presidency_vs_nomination"


def test_detect_rejects_cross_event():
    """Same template strings but different event_slug => no pair."""
    a = _mk("a", "Will France win the World Cup?",
             "france-wins-world-cup", event="2026-world-cup")
    b = _mk("b", "Will France reach the final?",
             "france-reaches-final-euros", event="2024-euros")
    res = detect_pair(a, b)
    assert res is None


def test_detect_rejects_non_implication():
    """Same event but unrelated questions => no pair."""
    a = _mk("a", "Will France score in the first half?",
             "france-scores-h1", event="2026-world-cup")
    b = _mk("b", "Will it rain in Doha during the final?",
             "doha-rain-final", event="2026-world-cup")
    res = detect_pair(a, b)
    assert res is None


# ---------------------------------------------------------- violation math
def test_violation_logged_when_margin_exceeded():
    """P(A=0.50) - P(B=0.40) = 0.10 > min_margin 0.03 => violation traded."""
    ledger, path = _temp_ledger()
    cfg = {"strategies": {"logic_scan": {
        "min_confidence_to_trade": 0.95, "min_margin": 0.03,
        "stake_usd": 10.0, "fee_pct": 0.0,
    }}}
    s = LogicScan(cfg)
    a = _mk("a", "Will France win the World Cup?",
             "france-wins-world-cup", event="2026-world-cup", yes_ask=0.50)
    b = _mk("b", "Will France reach the final?",
             "france-reaches-final", event="2026-world-cup", yes_ask=0.40)
    res = s.scan(ledger, [a, b], verbose=False)
    assert res["traded"] == 1
    stats = ledger.logic_violation_stats()
    assert stats.get("traded", 0) == 1
    try:
        os.unlink(path)
    except OSError:
        pass


def test_near_miss_logged_separately():
    """P(A) > P(B) but margin below threshold => logged as near_miss."""
    ledger, path = _temp_ledger()
    cfg = {"strategies": {"logic_scan": {
        "min_confidence_to_trade": 0.95, "min_margin": 0.03,
    }}}
    s = LogicScan(cfg)
    a = _mk("a", "Will France win the World Cup?",
             "france-wins-world-cup", event="2026-world-cup", yes_ask=0.41)
    b = _mk("b", "Will France reach the final?",
             "france-reaches-final", event="2026-world-cup", yes_ask=0.40)
    res = s.scan(ledger, [a, b], verbose=False)
    assert res["traded"] == 0
    stats = ledger.logic_violation_stats()
    assert stats.get("near_miss", 0) == 1
    try:
        os.unlink(path)
    except OSError:
        pass


def test_pair_persisted_to_review_table_at_low_confidence():
    """Even if a candidate pair's confidence is below the trade
    threshold, it should be persisted in logic_pairs for review."""
    ledger, path = _temp_ledger()
    cfg = {"strategies": {"logic_scan": {
        "min_confidence_to_trade": 0.99,    # impossible bar
        "min_margin": 0.03,
    }}}
    s = LogicScan(cfg)
    a = _mk("a", "Will France win the World Cup?",
             "france-wins-world-cup", event="2026-world-cup", yes_ask=0.50)
    b = _mk("b", "Will France reach the final?",
             "france-reaches-final", event="2026-world-cup", yes_ask=0.40)
    s.scan(ledger, [a, b], verbose=False)
    pairs = ledger.list_logic_pairs()
    assert len(pairs) >= 1
    try:
        os.unlink(path)
    except OSError:
        pass
