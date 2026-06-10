"""Multi-outcome arb extension tests (Prompt A)."""
from __future__ import annotations

import os
import sys
import tempfile

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from foundation.ledger import Ledger
from strategies.base import ArbEvent, ArbLeg
from strategies.bucket_arb import BucketSumArb


def _leg(title: str, yes_ask_levels: list[tuple[float, float]],
          no_ask_levels: list[tuple[float, float]] | None = None,
          gamma_ask: float | None = None) -> ArbLeg:
    yes_asks = [{"price": p, "size": s} for p, s in yes_ask_levels]
    no_asks = [{"price": p, "size": s} for p, s in (no_ask_levels or [(1 - yes_ask_levels[0][0], 1000)])]
    return ArbLeg(
        market_id=f"mkt_{title}", leg_title=title,
        yes_token_id=f"yes_{title}", no_token_id=f"no_{title}",
        yes_asks=yes_asks, no_asks=no_asks,
        gamma_yes_ask=gamma_ask if gamma_ask is not None else yes_ask_levels[0][0],
        gamma_yes_bid=(gamma_ask if gamma_ask is not None else yes_ask_levels[0][0]) - 0.01,
    )


def _ev(legs):
    return ArbEvent(
        event_id="ev_multi", event_slug="test-event", event_title="Test event",
        end_date_iso="2030-01-01T00:00:00Z",
        neg_risk=True, legs=legs,
        completeness_verified=True, completeness_note="ok",
        books_fetched=True,
    )


def _temp_ledger():
    f = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    f.close()
    return Ledger(f.name), f.name


# ---------------------------------------------------------- detect arb
def test_three_outcome_sums_to_097_detected_as_arb():
    """Three legs at 0.32, 0.32, 0.33 = sum 0.97. With slippage 1c per leg
    (3c total) and zero fees, net cost 1.00. Just barely break-even -
    use slip=0 to test the detection."""
    cfg = {"strategies": {"bucket_arb": {
        "stake_notional_usd": 10.0,
        "slippage_cents": 0.0,
        "fee_taker_pct": 0.0,
        "min_net_gap_pct": 0.01,
        "max_open_multi_sets": 20,
    }}}
    s = BucketSumArb(cfg)
    ev = _ev([
        _leg("A", [(0.32, 10000)]),
        _leg("B", [(0.32, 10000)]),
        _leg("C", [(0.33, 10000)]),
    ])
    det = s.detect_multi_side(ev, "YES")
    assert det is not None
    assert det["status_hint"] == "fillable"
    # cost ≈ 0.97 per share, payout = 1.0 -> gap ≈ 0.031 = 3.1%
    assert det["net_gap_pct"] > 0.025
    assert det["net_gap_pct"] < 0.04


def test_thin_book_records_unfillable_leg():
    """One of the legs has only 1 share available, so $10 stake (≈ 30 shares
    per leg at 0.33) can't fill -> unfillable_leg, no position opened."""
    cfg = {"strategies": {"bucket_arb": {
        "stake_notional_usd": 10.0,
        "slippage_cents": 0.0,
        "fee_taker_pct": 0.0,
        "min_net_gap_pct": 0.01,
    }}}
    s = BucketSumArb(cfg)
    ev = _ev([
        _leg("A", [(0.32, 10000)]),
        _leg("B", [(0.33, 1)]),     # only 1 share visible
        _leg("C", [(0.32, 10000)]),
    ])
    det = s.detect_multi_side(ev, "YES")
    assert det is not None
    assert det["status_hint"] == "unfillable_leg"


def test_sum_0995_below_min_gap_not_traded():
    """Sum YES = 0.5 + 0.495 = 0.995. After 0 fees but threshold 1.0%,
    net gap = (1.0 - 0.995)/0.995 ≈ 0.503% < 1.0% threshold => logged as
    observed_below_threshold."""
    ledger, path = _temp_ledger()
    cfg = {"strategies": {"bucket_arb": {
        "stake_notional_usd": 10.0,
        "slippage_cents": 0.0,
        "fee_taker_pct": 0.0,
        "min_net_gap_pct": 0.01,
        "max_open_multi_sets": 20,
    }}}
    s = BucketSumArb(cfg)
    ev = _ev([
        _leg("A", [(0.50, 10000)]),
        _leg("B", [(0.495, 10000)]),
    ])
    det = s.detect_multi_side(ev, "YES")
    assert det["status_hint"] == "fillable"
    assert det["net_gap_pct"] < 0.01
    rid = s.commit_multi(ev, det, ledger)
    # Inspect
    import sqlite3
    c = sqlite3.connect(path); c.row_factory = sqlite3.Row
    row = c.execute("SELECT status FROM arb_multi WHERE id=?", (rid,)).fetchone()
    c.close()
    assert row["status"] == "observed_below_threshold"
    try:
        os.unlink(path)
    except OSError:
        pass


def test_fee_math_subtracts_from_locked_profit():
    """Same legs as the 'just-arb' case but with taker fee = 2% applied to
    each leg's vwap. 3-outcome event each at 0.33: probe sum = 0.99.
    With 2% fee per leg on a 0.33 leg = 0.0066 fee/share/leg = 0.0198
    total. So total cost ≈ 0.99 + 0.0198 ≈ 1.01 - clearly NOT arb."""
    cfg = {"strategies": {"bucket_arb": {
        "stake_notional_usd": 10.0,
        "slippage_cents": 0.0,
        "fee_taker_pct": 0.02,
        "min_net_gap_pct": 0.01,
    }}}
    s = BucketSumArb(cfg)
    ev = _ev([
        _leg("A", [(0.33, 10000)]),
        _leg("B", [(0.33, 10000)]),
        _leg("C", [(0.33, 10000)]),
    ])
    det = s.detect_multi_side(ev, "YES")
    assert det is not None
    # With fees the gap should be NEGATIVE.
    assert det["net_gap_pct"] < 0
    # And fees recorded should equal: ~0.02 * 0.33 * (10/cost_per_share) per leg, 3 legs
    assert det["fees"] > 0


def test_open_cap_blocks_arb():
    """Cap = 1, open one arb, the second logs as observed_below_threshold."""
    ledger, path = _temp_ledger()
    cfg = {"strategies": {"bucket_arb": {
        "stake_notional_usd": 10.0,
        "slippage_cents": 0.0,
        "fee_taker_pct": 0.0,
        "min_net_gap_pct": 0.01,
        "max_open_multi_sets": 1,
    }}}
    s = BucketSumArb(cfg)
    ev = _ev([
        _leg("A", [(0.30, 10000)]),
        _leg("B", [(0.30, 10000)]),
        _leg("C", [(0.30, 10000)]),
    ])
    det = s.detect_multi_side(ev, "YES")
    rid1 = s.commit_multi(ev, det, ledger)
    rid2 = s.commit_multi(ev, det, ledger, day_iso="2026-06-12")
    import sqlite3
    c = sqlite3.connect(path); c.row_factory = sqlite3.Row
    rows = list(c.execute("SELECT id, status FROM arb_multi ORDER BY id").fetchall())
    c.close()
    assert rows[0]["status"] == "OPEN"
    # Second should be blocked by cap since one is already OPEN
    assert rows[1]["status"] == "observed_below_threshold"
    try:
        os.unlink(path)
    except OSError:
        pass
