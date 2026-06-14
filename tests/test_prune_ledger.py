"""Prune_ledger contract: bound the two unbounded log tables (signals,
arb_multi) without touching positions or recent rows.

The 50 MB ledger.db Guard outage in production was caused by these
two tables growing ~10 MB/day. Pin the prune so a regression that
re-floods either table fails loudly in CI."""
from __future__ import annotations

import os
import sqlite3
import sys
import tempfile
from datetime import datetime, timedelta, timezone

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from foundation.ledger import Ledger


def _temp_ledger():
    f = tempfile.NamedTemporaryFile(suffix=".db", delete=False); f.close()
    return Ledger(f.name), f.name


def _cleanup(path: str) -> None:
    for p in (path, path.removesuffix(".db") + ".cache.db"):
        try:
            os.unlink(p)
        except OSError:
            pass


def _insert_signal(ledger: Ledger, market_id: int, ts_iso: str,
                    strategy: str = "weather") -> None:
    with sqlite3.connect(ledger.ledger_path) as c:
        c.execute(
            "INSERT INTO signals (market_id, strategy, ts, p_final, "
            "confidence, metadata_json) VALUES (?,?,?,?,?,?)",
            (market_id, strategy, ts_iso, 0.5, 1.0, "{}"),
        )


def _insert_arb_multi(ledger: Ledger, ts_iso: str, status: str) -> None:
    with sqlite3.connect(ledger.ledger_path) as c:
        c.execute(
            "INSERT INTO arb_multi (ts, event_id, outcome_count, side, "
            "stake_notional, shares, leg_fills_json, total_cost, "
            "guaranteed_payout, fees, net_gap_pct, status) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (ts_iso, "evt", 3, "YES", 10.0, 5.0, "[]", 9.0, 10.0, 0.0,
             0.01, status),
        )


# ---------------------------------------------------------------- signals
def test_prune_drops_old_signals_keeps_recent():
    ledger, path = _temp_ledger()
    try:
        today = datetime.now(timezone.utc).date()
        # 3 old (10/15/30d ago), 3 recent (0/1/3d ago)
        old_dates = [today - timedelta(days=d) for d in (10, 15, 30)]
        new_dates = [today - timedelta(days=d) for d in (0, 1, 3)]
        for d in old_dates:
            _insert_signal(ledger, 1, f"{d.isoformat()}T12:00:00+00:00")
        for d in new_dates:
            _insert_signal(ledger, 1, f"{d.isoformat()}T12:00:00+00:00")
        out = ledger.prune_ledger(signals_keep_days=7, arb_multi_keep_days=7)
        assert out["signals_deleted"] == 3
        with sqlite3.connect(ledger.ledger_path) as c:
            remaining = c.execute("SELECT COUNT(*) FROM signals").fetchone()[0]
        assert remaining == 3
    finally:
        _cleanup(path)


def test_prune_is_a_noop_on_empty_signals():
    ledger, path = _temp_ledger()
    try:
        out = ledger.prune_ledger(signals_keep_days=7)
        assert out["signals_deleted"] == 0
    finally:
        _cleanup(path)


# ---------------------------------------------------------------- arb_multi
def test_prune_drops_old_below_threshold_rows():
    ledger, path = _temp_ledger()
    try:
        old = (datetime.now(timezone.utc).date()
               - timedelta(days=14)).isoformat() + "T12:00:00+00:00"
        new = (datetime.now(timezone.utc).date()
               - timedelta(days=2)).isoformat() + "T12:00:00+00:00"
        _insert_arb_multi(ledger, old, "observed_below_threshold")
        _insert_arb_multi(ledger, old, "unfillable_leg")
        _insert_arb_multi(ledger, new, "observed_below_threshold")
        out = ledger.prune_ledger(arb_multi_keep_days=7)
        assert out["arb_multi_deleted"] == 2
        with sqlite3.connect(ledger.ledger_path) as c:
            n = c.execute("SELECT COUNT(*) FROM arb_multi").fetchone()[0]
        assert n == 1
    finally:
        _cleanup(path)


def test_prune_preserves_real_arb_positions_even_if_old():
    """A real OPEN/CLOSED bucket-arb position must survive pruning
    regardless of age -- the experiment record is permanent. Only the
    non-position status rows get pruned."""
    ledger, path = _temp_ledger()
    try:
        ancient = "2020-01-01T00:00:00+00:00"
        for status in ("OPEN", "CLOSED", "VOID"):
            _insert_arb_multi(ledger, ancient, status)
        # And one stale observation that SHOULD prune
        _insert_arb_multi(ledger, ancient, "observed_below_threshold")
        out = ledger.prune_ledger(arb_multi_keep_days=7)
        assert out["arb_multi_deleted"] == 1
        with sqlite3.connect(ledger.ledger_path) as c:
            statuses = sorted(
                r[0] for r in c.execute("SELECT status FROM arb_multi"))
        assert statuses == ["CLOSED", "OPEN", "VOID"]
    finally:
        _cleanup(path)


def test_prune_returns_zero_counts_when_nothing_old():
    ledger, path = _temp_ledger()
    try:
        new = datetime.now(timezone.utc).date().isoformat() + "T12:00:00+00:00"
        _insert_signal(ledger, 1, new)
        _insert_arb_multi(ledger, new, "observed_below_threshold")
        out = ledger.prune_ledger(signals_keep_days=7, arb_multi_keep_days=7)
        assert out == {"signals_deleted": 0, "arb_multi_deleted": 0}
    finally:
        _cleanup(path)
