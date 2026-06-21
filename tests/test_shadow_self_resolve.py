"""Shadow self-resolve path: settle shadow rows whose market never
entered the live paper book.

Pre-fix, `grade_shadow_trades` required `get_settlement(market_id)` to
return a row, which only ever happens for markets the live paper book
traded. With the paper book capped at ~15 open positions, the vast
majority of shadow-scored markets never had a settlement row and the
shadow rows stayed OPEN forever (264 side-taken zombies in production
as of 2026-06-21).

These tests pin the new fallback: when no live settlement exists for
a past-date shadow row, the grader calls weather.resolve() directly,
settles the shadow row, and books champ_pnl / chal_pnl using the
fill data already on the row. Critically:

  - the live settlements table is NEVER written by this path
  - the paper_trades table is NEVER touched
  - re-running grade does not double-settle (idempotent via status)
  - rows with side but NULL stake/shares settle outcome but book $0
"""
from __future__ import annotations

import os
import sqlite3
import sys
import tempfile
from datetime import datetime, timedelta, timezone

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from foundation.grader import grade_shadow_trades
from foundation.ledger import Ledger


# ---------------------------------------------------------------- helpers
def _temp_ledger():
    f = tempfile.NamedTemporaryFile(suffix=".db", delete=False); f.close()
    return Ledger(f.name), f.name


def _cleanup(path: str) -> None:
    for p in (path, path.removesuffix(".db") + ".cache.db"):
        try:
            os.unlink(p)
        except OSError:
            pass


class _StubWeatherStrategy:
    """Minimal stub that returns the configured outcome per market_id."""
    name = "weather"

    def __init__(self, outcomes: dict[int, str | None]):
        self._by_mid: dict[int, str | None] = outcomes
        self.resolve_calls: list[int] = []

    def resolve(self, market, settled_at):
        # `market.market_id` is the polymarket condition_id (str). Our
        # stub keys by the internal ledger market_id which lives in the
        # row, but we don't have it here -- the test constructs the
        # stub keyed by the slug instead.
        self.resolve_calls.append(market.slug)
        outcome = self._by_mid.get(market.slug)
        if outcome is None:
            return None
        return {
            "outcome": outcome,
            "actual_value": 20.0,
            "om_value": 20.0,
            "wu_value": 20.0,
            "source_value": "stub",
            "wu_source": "stub",
            "unit": "C",
            "kind": "max",
            "rounded_val": 20.0,
            "wu_rounded_val": 20.0,
            "disagreement": 0,
            "dispute_note": "",
        }


def _make_market(ledger: Ledger, slug: str, resolve_date: str) -> int:
    return ledger.upsert_market({
        "condition_id": f"cond-{slug}",
        "slug": slug,
        "question": "q",
        "category": "weather",
        "threshold": 20,
        "unit": "C",
        "resolve_date": resolve_date,
        "resolution_source": None,
        "rules_text": "",
    })


def _open_shadow(ledger: Ledger, mid: int, resolve_date: str,
                  champ_side: str | None = "YES",
                  champ_pf: float | None = 0.30,
                  champ_stake: float | None = 15.0,
                  champ_shares: float | None = 50.0,
                  chal_side: str | None = "NO",
                  chal_pf: float | None = 0.70,
                  chal_stake: float | None = 15.0,
                  chal_shares: float | None = 21.4286) -> int:
    return ledger.upsert_shadow_trade({
        "market_id": mid,
        "city": "nyc",
        "resolve_date": resolve_date,
        "champ_p": 0.7, "champ_side": champ_side, "champ_edge": 0.10,
        "champ_price_filled": champ_pf, "champ_stake": champ_stake,
        "champ_shares": champ_shares,
        "chal_p": 0.2, "chal_side": chal_side, "chal_edge": 0.10,
        "chal_price_filled": chal_pf, "chal_stake": chal_stake,
        "chal_shares": chal_shares,
    })


def _grade(ledger: Ledger, weather_stub, today=None) -> tuple[int, int]:
    """Inject the stub via monkeypatch of _load_strategies."""
    import foundation.grader as g
    real_load = g._load_strategies
    g._load_strategies = lambda cfg: {"weather": weather_stub}
    try:
        return grade_shadow_trades(
            cfg={}, ledger=ledger,
            today=today or datetime.now(timezone.utc).date(),
            verbose=False)
    finally:
        g._load_strategies = real_load


# ---------------------------------------------------------------- self-resolve
def test_self_resolve_settles_past_date_shadow_with_no_paper_trade():
    """The canonical zombie: shadow row for a market that the live
    paper book never traded, resolve_date in the past. Self-resolve
    must settle it via weather.resolve()."""
    ledger, path = _temp_ledger()
    try:
        yesterday = (datetime.now(timezone.utc).date()
                      - timedelta(days=1)).isoformat()
        mid = _make_market(ledger, "h-nyc-yesterday-20c", yesterday)
        _open_shadow(ledger, mid, yesterday)
        stub = _StubWeatherStrategy({"h-nyc-yesterday-20c": "YES"})
        settled, skipped = _grade(ledger, stub)
        assert settled == 1
        # The shadow row is now SETTLED with correct pnls.
        with sqlite3.connect(ledger.ledger_path) as c:
            c.row_factory = sqlite3.Row
            r = c.execute("SELECT status, outcome, champ_pnl, chal_pnl "
                          "FROM shadow_trades WHERE market_id=?",
                          (mid,)).fetchone()
        assert r["status"] == "SETTLED"
        assert r["outcome"] == "YES"
        # champion: YES @ 0.30, stake $15, shares 50 -> WIN -> +35
        assert r["champ_pnl"] == pytest.approx(35.0)
        # challenger: NO @ 0.70, stake $15, shares 21.43 -> LOSS -> -15
        assert r["chal_pnl"] == pytest.approx(-15.0)
    finally:
        _cleanup(path)


def test_self_resolve_does_not_write_to_settlements_or_paper_trades():
    """Acceptance guard: self-resolve must NOT touch the live tables.
    Capture row counts before and after."""
    ledger, path = _temp_ledger()
    try:
        yesterday = (datetime.now(timezone.utc).date()
                      - timedelta(days=1)).isoformat()
        mid = _make_market(ledger, "h-paris-yesterday", yesterday)
        _open_shadow(ledger, mid, yesterday)
        with sqlite3.connect(ledger.ledger_path) as c:
            n_settl_before = c.execute(
                "SELECT COUNT(*) FROM settlements").fetchone()[0]
            n_paper_before = c.execute(
                "SELECT COUNT(*) FROM paper_trades").fetchone()[0]
        stub = _StubWeatherStrategy({"h-paris-yesterday": "NO"})
        settled, _ = _grade(ledger, stub)
        assert settled == 1
        with sqlite3.connect(ledger.ledger_path) as c:
            n_settl_after = c.execute(
                "SELECT COUNT(*) FROM settlements").fetchone()[0]
            n_paper_after = c.execute(
                "SELECT COUNT(*) FROM paper_trades").fetchone()[0]
        assert n_settl_after == n_settl_before, \
            "settlements table must not be touched by shadow self-resolve"
        assert n_paper_after == n_paper_before, \
            "paper_trades must not be touched by shadow self-resolve"
    finally:
        _cleanup(path)


def test_self_resolve_idempotent_on_rerun():
    """Re-running grade after a settle must not change anything --
    close_shadow_trade flipped status to SETTLED so list_open... skips it."""
    ledger, path = _temp_ledger()
    try:
        yesterday = (datetime.now(timezone.utc).date()
                      - timedelta(days=1)).isoformat()
        mid = _make_market(ledger, "h-tokyo-yesterday", yesterday)
        _open_shadow(ledger, mid, yesterday)
        stub = _StubWeatherStrategy({"h-tokyo-yesterday": "YES"})
        s1, _ = _grade(ledger, stub)
        s2, _ = _grade(ledger, stub)
        assert s1 == 1
        assert s2 == 0  # nothing left OPEN -> nothing to settle
        # And the resolve stub was only called once
        assert stub.resolve_calls.count("h-tokyo-yesterday") == 1
    finally:
        _cleanup(path)


def test_self_resolve_skips_future_dated():
    """A shadow row whose resolve_date is in the future must NOT be
    self-resolved -- it isn't eligible yet."""
    ledger, path = _temp_ledger()
    try:
        tomorrow = (datetime.now(timezone.utc).date()
                     + timedelta(days=2)).isoformat()
        mid = _make_market(ledger, "h-future", tomorrow)
        _open_shadow(ledger, mid, tomorrow)
        stub = _StubWeatherStrategy({"h-future": "YES"})
        settled, _ = _grade(ledger, stub)
        assert settled == 0
        # Stub MUST NOT have been called -- we short-circuit before
        # invoking the resolver on future-dated rows.
        assert stub.resolve_calls == []
    finally:
        _cleanup(path)


def test_self_resolve_skips_when_resolver_returns_none():
    """resolve() returns None when local-day hasn't ended or both
    WU+OM are missing. Grader must skip silently, not crash."""
    ledger, path = _temp_ledger()
    try:
        yesterday = (datetime.now(timezone.utc).date()
                      - timedelta(days=1)).isoformat()
        mid = _make_market(ledger, "h-deferred", yesterday)
        _open_shadow(ledger, mid, yesterday)
        stub = _StubWeatherStrategy({"h-deferred": None})  # resolver returns None
        settled, _ = _grade(ledger, stub)
        assert settled == 0
        # Row is still OPEN for the next pass to retry.
        with sqlite3.connect(ledger.ledger_path) as c:
            r = c.execute("SELECT status FROM shadow_trades "
                          "WHERE market_id=?", (mid,)).fetchone()
        assert r[0] == "OPEN"
    finally:
        _cleanup(path)


def test_self_resolve_handles_side_taken_with_null_stake():
    """A row with side but NULL stake/shares (executor returned a side
    but no FillResult) must settle outcome cleanly and book $0 P&L --
    not crash on a None stake."""
    ledger, path = _temp_ledger()
    try:
        yesterday = (datetime.now(timezone.utc).date()
                      - timedelta(days=1)).isoformat()
        mid = _make_market(ledger, "h-nullstake", yesterday)
        _open_shadow(ledger, mid, yesterday,
                      champ_side="YES", champ_pf=None, champ_stake=None,
                      champ_shares=None,
                      chal_side="NONE", chal_pf=None, chal_stake=None,
                      chal_shares=None)
        stub = _StubWeatherStrategy({"h-nullstake": "YES"})
        settled, _ = _grade(ledger, stub)
        assert settled == 1
        with sqlite3.connect(ledger.ledger_path) as c:
            c.row_factory = sqlite3.Row
            r = c.execute("SELECT champ_pnl, chal_pnl, status "
                          "FROM shadow_trades WHERE market_id=?",
                          (mid,)).fetchone()
        assert r["status"] == "SETTLED"
        # Side taken but no stake -> $0 P&L (graceful, not crash).
        assert r["champ_pnl"] == 0.0
        assert r["chal_pnl"] == 0.0
    finally:
        _cleanup(path)


# ---------------------------------------------------------------- fast path
def test_fast_path_still_uses_live_settlement_when_present():
    """Backward compat: shadow rows whose market DOES have a live
    settlement still use the fast path. weather.resolve() is NOT
    called for those (faster + uses the canonical truth)."""
    ledger, path = _temp_ledger()
    try:
        yesterday = (datetime.now(timezone.utc).date()
                      - timedelta(days=1)).isoformat()
        mid = _make_market(ledger, "h-fastpath", yesterday)
        _open_shadow(ledger, mid, yesterday)
        # Pre-populate a live settlement (simulating that the paper
        # book traded this market and the live grader settled it).
        ledger.record_settlement(mid, actual_value=20.0,
                                  source_value="test", outcome="NO")
        # Stub returns YES (would conflict if called) but should NOT
        # be called because fast path uses the live settlement (NO).
        stub = _StubWeatherStrategy({"h-fastpath": "YES"})
        settled, _ = _grade(ledger, stub)
        assert settled == 1
        with sqlite3.connect(ledger.ledger_path) as c:
            r = c.execute("SELECT outcome FROM shadow_trades "
                          "WHERE market_id=?", (mid,)).fetchone()
        assert r[0] == "NO"  # from the live settlement, not the stub
        assert stub.resolve_calls == []
    finally:
        _cleanup(path)
