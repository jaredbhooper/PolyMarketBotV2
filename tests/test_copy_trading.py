"""Copy-trading strategy tests. All HTTP is mocked."""
from __future__ import annotations

import os
import sys
import tempfile
import time

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from foundation.ledger import Ledger
from strategies.copy_trading import (CopyTrading, compute_metrics,
                                       passes_hard_filters, score,
                                       walk_book_paper, HARD_FILTERS,
                                       DEFAULT_SCORE_WEIGHTS)


# ---------------------------------------------------------- helpers
NOW = 1781200000   # 2026-06-08 around the test-day epoch


def _trade(ts: int, side: str, asset: str, price: float, size: float,
            slug: str = "nba-knicks-vs-celtics-2026-04-15",
            tx: str = None) -> dict:
    return {
        "transactionHash": tx or f"0x{ts:08x}",
        "asset": asset,
        "side": side,
        "price": price,
        "size": size,
        "timestamp": ts,
        "slug": slug,
        "eventSlug": slug,
        "conditionId": "0xfeed",
        "outcome": "Yes",
        "outcomeIndex": 1,
        "proxyWallet": "0xtest",
    }


# ---------------------------------------------------------- scout filters
def test_scout_excludes_99c_grinder():
    """Buys at 0.95 + 0.97 + 0.98 = avg ~0.967 > 0.88 cap -> excluded."""
    trades = []
    for i in range(40):
        ts = NOW - (100 - i) * 86400      # 100 days back ramping up to recent
        trades.append(_trade(ts, "BUY", f"asset_{i}", 0.95 + (i % 3) * 0.01, 50))
        trades.append(_trade(ts + 100, "SELL", f"asset_{i}", 1.00, 50))
    m = compute_metrics(trades, now_ts=NOW)
    ok, reason = passes_hard_filters(m, HARD_FILTERS)
    assert not ok
    assert "avg_entry_price" in reason


def test_scout_excludes_single_spike_lottery():
    """40 small trades + 1 huge win on the same asset -> top single-share > 40% => excluded."""
    trades = []
    for i in range(40):
        ts = NOW - (100 - i) * 86400
        trades.append(_trade(ts, "BUY", f"asset_{i}", 0.50, 10))
        trades.append(_trade(ts + 100, "SELL", f"asset_{i}", 0.51, 10))   # ~ +$0.10
    # One huge win
    ts = NOW - 10 * 86400
    trades.append(_trade(ts, "BUY", "asset_jackpot", 0.05, 500))
    trades.append(_trade(ts + 100, "SELL", "asset_jackpot", 0.99, 500))
    m = compute_metrics(trades, now_ts=NOW)
    ok, reason = passes_hard_filters(m, HARD_FILTERS)
    assert not ok
    assert "top_single_trade_share" in reason


def test_scout_passes_niche_specialist():
    """120 small BUYs at 0.40-0.50, 120 SELLs at 0.50-0.55, all in 'sports'
    over 100 days -> avg entry well below cap, top share modest, recent
    activity, positive ROI."""
    trades = []
    for i in range(120):
        ts = NOW - (100 - i * 100 // 120) * 86400
        trades.append(_trade(ts, "BUY", f"a_{i}", 0.45, 100, slug="nba-knicks-vs-celtics"))
        trades.append(_trade(ts + 100, "SELL", f"a_{i}", 0.50, 100, slug="nba-knicks-vs-celtics"))
    m = compute_metrics(trades, now_ts=NOW)
    ok, reason = passes_hard_filters(m, HARD_FILTERS)
    assert ok, f"specialist should pass, got: {reason}"
    sc = score(m, DEFAULT_SCORE_WEIGHTS)
    assert sc > 0
    # Niche concentration counts
    assert m["dominant_category"] == "sports"
    assert m["dominant_share"] > 0.5


def test_scout_excludes_stale_wallet():
    """Hasn't traded in 30 days -> excluded by max_days_since_last_trade."""
    trades = []
    for i in range(35):
        ts = NOW - 80 * 86400 - i * 86400
        trades.append(_trade(ts, "BUY", f"a_{i}", 0.5, 100))
        trades.append(_trade(ts + 100, "SELL", f"a_{i}", 0.6, 100))
    m = compute_metrics(trades, now_ts=NOW)
    ok, reason = passes_hard_filters(m, HARD_FILTERS)
    assert not ok
    assert "days_since_last_trade" in reason


# ---------------------------------------------------------- book walk
def test_walk_book_paper_fill():
    asks = [{"price": "0.40", "size": "20"}, {"price": "0.41", "size": "30"}]
    eff, sh, top3, status = walk_book_paper(asks, "BUY", stake_usd=5.0,
                                              slippage_cents=0.01)
    assert status == "ok"
    # 5 USD at 0.40 -> 12.5 shares at vwap 0.40, plus 1c slip = 0.41
    assert abs(eff - 0.41) < 1e-9
    assert abs(sh - 12.5) < 1e-9


def test_walk_book_paper_unfillable():
    """Tiny book that can't absorb $5."""
    asks = [{"price": "0.40", "size": "1"}]
    eff, sh, top3, status = walk_book_paper(asks, "BUY", stake_usd=5.0,
                                              slippage_cents=0.01)
    assert status == "unfillable"


# ---------------------------------------------------------- follower
class FakeData:
    def __init__(self, by_user: dict[str, list[dict]]):
        self.by_user = by_user

    def fetch_trades(self, user=None, limit=100, offset=0):
        if user:
            return self.by_user.get(user, [])[offset: offset + limit]
        # Discovery path - merge everything
        all_t = []
        for v in self.by_user.values():
            all_t.extend(v)
        return all_t[offset: offset + limit]

    def iter_trades(self, user=None, page=100, max_pages=20,
                     stop_before_ts=None):
        for t in self.fetch_trades(user=user, limit=page, offset=0):
            if stop_before_ts is not None and int(t.get("timestamp") or 0) <= stop_before_ts:
                return
            yield t


class FakeScanner:
    def __init__(self, books: dict[str, dict]):
        self.books = books

    def fetch_book(self, token_id):
        return self.books.get(token_id) or {"asks": [], "bids": []}


def _temp_ledger():
    f = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    f.close()
    return Ledger(f.name), f.name


def test_follower_dedupes_via_cursor_and_records_copy():
    """Follower pulls trades > cursor, skips already-seen trades, copies
    a deep-book trade and stores both leader_price + our_price."""
    ledger, path = _temp_ledger()
    cfg = {"strategies": {"copy_trading": {"max_open_total": 10,
                                             "max_open_per_leader": 10}}}
    strat = CopyTrading(cfg)
    w = "0xabc"
    # Seed a roster row so the follower picks it up
    ledger.upsert_roster(w, entered_at="2026-06-01T00:00:00Z",
                          exited_at=None, score=0.5, rank=1,
                          status="ACTIVE", hysteresis={})
    # Set cursor at ts=1000 - so any trade with ts<=1000 is ignored
    ledger.set_wallet_cursor(w, 1000)
    new_trade = _trade(2000, "BUY", "tok_1", 0.45, 100, tx="0xnew")
    new_trade["proxyWallet"] = w
    data = FakeData({w: [new_trade]})
    scanner = FakeScanner({"tok_1": {"asks": [{"price": "0.45", "size": "1000"}]}})
    res = strat.follow(data, scanner, ledger, verbose=False)
    assert res["copied"] == 1
    rows = ledger.list_open_copies()
    assert len(rows) == 1
    assert rows[0]["leader_price"] == pytest.approx(0.45)
    assert rows[0]["our_price"] == pytest.approx(0.46)   # vwap 0.45 + 1c slip
    # Second call should not re-copy because cursor advanced.
    res2 = strat.follow(data, scanner, ledger, verbose=False)
    assert res2["copied"] == 0
    try:
        os.unlink(path)
    except OSError:
        pass


def test_follower_records_unfillable_on_thin_book():
    ledger, path = _temp_ledger()
    cfg = {"strategies": {"copy_trading": {}}}
    strat = CopyTrading(cfg)
    w = "0xthin"
    ledger.upsert_roster(w, entered_at="2026-06-01T00:00:00Z",
                          exited_at=None, score=0.1, rank=1,
                          status="ACTIVE", hysteresis={})
    t = _trade(2000, "BUY", "tok_thin", 0.45, 100, tx="0xthin1")
    t["proxyWallet"] = w
    data = FakeData({w: [t]})
    scanner = FakeScanner({"tok_thin": {"asks": [{"price": "0.45", "size": "1"}]}})
    res = strat.follow(data, scanner, ledger, verbose=False)
    assert res["unfillable"] == 1
    rows = ledger.list_open_copies()
    assert len(rows) == 0    # unfillable rows are not 'open'
    try:
        os.unlink(path)
    except OSError:
        pass


def test_follower_respects_per_leader_cap():
    ledger, path = _temp_ledger()
    cfg = {"strategies": {"copy_trading": {"max_open_per_leader": 2,
                                             "max_open_total": 50}}}
    strat = CopyTrading(cfg)
    w = "0xcap"
    ledger.upsert_roster(w, entered_at="2026-06-01T00:00:00Z",
                          exited_at=None, score=0.5, rank=1,
                          status="ACTIVE", hysteresis={})
    trades = []
    for i in range(5):
        t = _trade(2000 + i, "BUY", f"tok_{i}", 0.45, 100, tx=f"0x{i}")
        t["proxyWallet"] = w
        trades.append(t)
    data = FakeData({w: trades})
    scanner = FakeScanner({f"tok_{i}":
                            {"asks": [{"price": "0.45", "size": "1000"}]}
                            for i in range(5)})
    res = strat.follow(data, scanner, ledger, verbose=False)
    # Should copy 2, then skip 3 with skipped_cap
    assert res["copied"] == 2
    assert res["skipped_cap"] == 3
    try:
        os.unlink(path)
    except OSError:
        pass


# ---------------------------------------------------------- grader math
def test_grader_settles_with_latency_tax():
    """Our fill at 0.46 (leader 0.45). Market resolves YES. shares=5/0.46.
       our_pnl = shares * (1 - 0.46); leader_pnl = (5/0.45) * (1 - 0.45)."""
    ledger, path = _temp_ledger()
    cfg = {"strategies": {"copy_trading": {}}}
    strat = CopyTrading(cfg)
    # Open a copied_trade row directly.
    cid = "0xfeed"
    ledger.record_copied_trade({
        "leader_wallet": "0xw", "market_id": cid, "token_id": "tok",
        "side": "BUY", "leader_price": 0.45, "leader_size": 100,
        "leader_ts": 1000, "detection_ts": 1300, "detection_delay_s": 300,
        "our_price": 0.46, "price_drift": 0.01,
        "book_snapshot_json": "[]", "stake": 5.0,
        "shares": 5.0 / 0.46, "status": "open",
    })

    class FakeSess:
        def get(self, url, params=None, timeout=20):
            class R:
                status_code = 200
                def json(self_inner):
                    return [{"closed": True, "outcomePrices": '["1.0","0.0"]'}]
            return R()
    import requests
    original_session = requests.Session
    requests.Session = lambda: FakeSess()
    try:
        res = strat.grade_copied(scanner=None, ledger=ledger,
                                  gamma_url="https://example.com", verbose=False)
    finally:
        requests.Session = original_session
    assert res["settled"] == 1
    rows = ledger.list_open_copies()
    assert len(rows) == 0
    # Verify pnl numbers
    import sqlite3
    c = sqlite3.connect(path); c.row_factory = sqlite3.Row
    row = c.execute("SELECT our_pnl, leader_pnl_equivalent FROM copied_trades").fetchone()
    c.close()
    # our: (5/0.46) * 1 - 5 = ~5.870
    # leader: (5/0.45) * 1 - 5 = ~6.111
    assert row["our_pnl"] == pytest.approx(5.0 / 0.46 - 5.0, abs=1e-4)
    assert row["leader_pnl_equivalent"] == pytest.approx(5.0 / 0.45 - 5.0, abs=1e-4)
    assert row["our_pnl"] < row["leader_pnl_equivalent"]   # latency tax > 0
    try:
        os.unlink(path)
    except OSError:
        pass
