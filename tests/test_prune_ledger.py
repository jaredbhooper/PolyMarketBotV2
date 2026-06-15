"""Prune contracts after the v2.5 signals-to-cache move.

  - prune_ledger handles only arb_multi now (signals moved to cache.db).
  - prune_cache handles snapshots + arb_gaps + cv_gaps + signals.

The 50 MB ledger.db Guard outage was caused by these two log tables
growing ~10 MB/day. Pin both prunes so a regression that re-floods
either table fails loudly in CI.
"""
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
    """Signals now live in cache.db; the public Ledger.record_signal()
    writes there. For tests that need to backdate `ts`, we insert
    directly into cache.signals."""
    with sqlite3.connect(ledger.cache_path) as c:
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


# ---------------------------------------------------------------- signals (cache.db)
def test_signals_table_lives_in_cache_not_ledger():
    """Pinned: post v2.5, signals MUST live in cache.db only. A
    regression that recreates it in ledger.db (e.g. someone restores
    the old CREATE TABLE) fails this test."""
    ledger, path = _temp_ledger()
    try:
        with sqlite3.connect(ledger.ledger_path) as c:
            in_ledger = c.execute(
                "SELECT 1 FROM sqlite_master "
                "WHERE type='table' AND name='signals'").fetchone()
        with sqlite3.connect(ledger.cache_path) as c:
            in_cache = c.execute(
                "SELECT 1 FROM sqlite_master "
                "WHERE type='table' AND name='signals'").fetchone()
        assert in_ledger is None, "signals table reintroduced in ledger.db"
        assert in_cache is not None, "signals table missing from cache.db"
    finally:
        _cleanup(path)


def test_record_signal_writes_to_cache():
    """record_signal() writes via the cache.signals attached path; the
    row must be visible in cache.db, not in ledger.db (which doesn't
    have the table anymore)."""
    ledger, path = _temp_ledger()
    try:
        ledger.record_signal(1, "weather", 0.7, 1.0, {"x": 1})
        with sqlite3.connect(ledger.cache_path) as c:
            n = c.execute("SELECT COUNT(*) FROM signals").fetchone()[0]
        assert n == 1
    finally:
        _cleanup(path)


def test_prune_cache_drops_old_signals_keeps_recent():
    ledger, path = _temp_ledger()
    try:
        today = datetime.now(timezone.utc).date()
        # 3 old (5/10/30d ago), 3 recent (0/1d ago) -- default cutoff is 2d
        for d in (5, 10, 30):
            _insert_signal(ledger, 1,
                           f"{(today - timedelta(days=d)).isoformat()}T12:00:00+00:00")
        for d in (0, 1):
            _insert_signal(ledger, 1,
                           f"{(today - timedelta(days=d)).isoformat()}T12:00:00+00:00")
        out = ledger.prune_cache(signals_keep_days=2)
        assert out["signals_deleted"] == 3
        with sqlite3.connect(ledger.cache_path) as c:
            remaining = c.execute("SELECT COUNT(*) FROM signals").fetchone()[0]
        assert remaining == 2
    finally:
        _cleanup(path)


# ---------------------------------------------------------------- arb_multi (ledger.db)
def test_prune_drops_old_below_threshold_rows():
    ledger, path = _temp_ledger()
    try:
        old = (datetime.now(timezone.utc).date()
               - timedelta(days=14)).isoformat() + "T12:00:00+00:00"
        new = (datetime.now(timezone.utc).date()
               - timedelta(days=0)).isoformat() + "T12:00:00+00:00"
        _insert_arb_multi(ledger, old, "observed_below_threshold")
        _insert_arb_multi(ledger, old, "unfillable_leg")
        _insert_arb_multi(ledger, new, "observed_below_threshold")
        out = ledger.prune_ledger(arb_multi_keep_days=2)
        assert out["arb_multi_deleted"] == 2
        with sqlite3.connect(ledger.ledger_path) as c:
            n = c.execute("SELECT COUNT(*) FROM arb_multi").fetchone()[0]
        assert n == 1
    finally:
        _cleanup(path)


def test_prune_preserves_real_arb_positions_even_if_old():
    """A real OPEN/CLOSED bucket-arb position must survive pruning
    regardless of age. Only non-position observation rows get pruned."""
    ledger, path = _temp_ledger()
    try:
        ancient = "2020-01-01T00:00:00+00:00"
        for status in ("OPEN", "CLOSED", "VOID"):
            _insert_arb_multi(ledger, ancient, status)
        _insert_arb_multi(ledger, ancient, "observed_below_threshold")
        out = ledger.prune_ledger(arb_multi_keep_days=2)
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
        out_l = ledger.prune_ledger(arb_multi_keep_days=2)
        out_c = ledger.prune_cache(signals_keep_days=2)
        assert out_l == {"arb_multi_deleted": 0}
        assert out_c["signals_deleted"] == 0
    finally:
        _cleanup(path)


# ---------------------------------------------------------------- migration
def test_signals_migration_moves_pre_existing_rows_to_cache():
    """If the ledger.db is an older build that still has the signals
    table, init must copy any rows to cache.signals and drop the
    table from ledger.db. Idempotent: second init is a no-op."""
    f = tempfile.NamedTemporaryFile(suffix=".db", delete=False); f.close()
    path = f.name
    try:
        # Simulate an old-build ledger.db: create the table + insert.
        with sqlite3.connect(path) as c:
            c.execute(
                "CREATE TABLE signals ("
                "  id INTEGER PRIMARY KEY AUTOINCREMENT, "
                "  market_id INTEGER NOT NULL, "
                "  strategy TEXT NOT NULL, "
                "  ts TEXT NOT NULL, "
                "  p_final REAL NOT NULL, "
                "  confidence REAL NOT NULL, "
                "  metadata_json TEXT)")
            c.execute(
                "INSERT INTO signals (market_id, strategy, ts, p_final, "
                "confidence, metadata_json) VALUES (?,?,?,?,?,?)",
                (1, "weather", "2026-06-14T12:00:00+00:00", 0.5, 1.0, "{}"))
        # Open via Ledger -> migration fires.
        Ledger(path)
        with sqlite3.connect(path) as c:
            in_ledger = c.execute(
                "SELECT 1 FROM sqlite_master "
                "WHERE type='table' AND name='signals'").fetchone()
        cache_path = path.removesuffix(".db") + ".cache.db"
        with sqlite3.connect(cache_path) as c:
            n = c.execute("SELECT COUNT(*) FROM signals").fetchone()[0]
        assert in_ledger is None, "signals table should be dropped from ledger.db"
        assert n == 1, "row should be copied into cache.signals"
        # Idempotent: re-open with no work to do.
        Ledger(path)
    finally:
        _cleanup(path)
