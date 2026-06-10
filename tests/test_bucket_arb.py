"""Bucket-sum arbitrage detector tests. HTTP is never called."""
from __future__ import annotations

import os
import sys
import tempfile

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from foundation.ledger import Ledger
from strategies.base import ArbEvent, ArbLeg
from strategies.bucket_arb import (BucketSumArb, walk_book_for_shares,
                                     gamma_yes_sum, gamma_no_sum)


def _leg(title, ya, yas=None, na=None, nas=None, gamma_ask=None, gamma_bid=None):
    yes_asks = [{"price": p, "size": s} for p, s in (yas or [(ya, 1000)])]
    no_asks = [{"price": p, "size": s} for p, s in (nas or [(na or (1 - ya), 1000)])]
    return ArbLeg(
        market_id=f"mkt_{title}",
        leg_title=title,
        yes_token_id=f"yes_{title}",
        no_token_id=f"no_{title}",
        yes_asks=yes_asks,
        no_asks=no_asks,
        gamma_yes_ask=ya if gamma_ask is None else gamma_ask,
        gamma_yes_bid=(ya - 0.01) if gamma_bid is None else gamma_bid,
    )


def _event(neg_risk=True, legs=None, complete=True, note="MECE ok"):
    return ArbEvent(
        event_id="ev1", event_slug="test-event", event_title="Test event",
        end_date_iso="2030-01-01T00:00:00Z",
        neg_risk=neg_risk, legs=legs or [],
        completeness_verified=complete,
        completeness_note=note,
        books_fetched=True,
    )


def test_walk_book_for_shares_basic():
    asks = [{"price": 0.10, "size": 50}, {"price": 0.11, "size": 100}]
    vwap, filled, levels = walk_book_for_shares(asks, 100)
    assert filled == 100
    assert abs(vwap - 0.105) < 1e-9


def test_gamma_sums():
    legs = [_leg("A", 0.40), _leg("B", 0.55), _leg("C", 0.05)]
    ys, miss = gamma_yes_sum(legs)
    assert miss == 0
    assert abs(ys - 1.00) < 1e-9
    ns, miss = gamma_no_sum(legs)
    # implied NO ask = 1 - bid = 1 - (ask - 0.01) on each = 0.61 + 0.46 + 0.96 = 2.03
    assert miss == 0


def test_detect_yes_arb_locks_profit():
    """Three legs each at YES ask 0.20 => sum 0.60. With 3*1c slippage =
    0.03 and 0.5c buffer = locked 0.365 per share."""
    cfg = {"strategies": {"bucket_arb": {
        "safety_buffer": 0.005, "min_arb_profit": 1.0,
        "target_shares": 100.0, "slippage_cents": 0.01,
        "min_executable_shares": 5.0,
    }}}
    s = BucketSumArb(cfg)
    ev = _event(legs=[_leg("A", 0.20), _leg("B", 0.20), _leg("C", 0.20)])
    det = s.detect_side(ev, "YES")
    assert det is not None
    assert det.side == "YES"
    assert abs(det.sum_vwap_per_share - 0.60) < 1e-9
    # locked = 1.0 - 0.60 - 3*0.01 - 0.005 = 0.365
    assert abs(det.locked_profit_per_share - 0.365) < 1e-6
    assert det.locked_profit_usd > 30.0
    assert det.cleared_threshold


def test_skips_incomplete_event():
    cfg = {"strategies": {"bucket_arb": {}}}
    s = BucketSumArb(cfg)
    ev = _event(legs=[_leg("A", 0.20), _leg("B", 0.20)],
                complete=False, note="negRisk missing")
    # Run scan and confirm no detection
    result = s.scan_arb([ev], scanner=None, verbose=False)
    assert result["counters"]["incomplete"] == 1
    assert result["counters"]["complete"] == 0
    assert not result["detections"]


def test_no_arb_when_sum_exceeds_one():
    """Sum 0.50 + 0.50 = 1.00. After 2c slip + 0.5c buffer = locked -0.025."""
    cfg = {"strategies": {"bucket_arb": {
        "safety_buffer": 0.005, "target_shares": 100.0,
        "slippage_cents": 0.01, "min_executable_shares": 5.0,
    }}}
    s = BucketSumArb(cfg)
    ev = _event(legs=[_leg("A", 0.50), _leg("B", 0.50)])
    det = s.detect_side(ev, "YES")
    assert det is not None
    assert det.locked_profit_per_share < 0
    assert not det.cleared_threshold
