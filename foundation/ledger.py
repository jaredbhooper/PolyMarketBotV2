"""SQLite ledger - the only thing that talks to the DB."""
from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

SCHEMA = """
CREATE TABLE IF NOT EXISTS markets (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  condition_id TEXT UNIQUE NOT NULL,
  slug TEXT,
  question TEXT,
  category TEXT,
  threshold REAL,
  unit TEXT,
  resolve_date TEXT,
  resolution_source TEXT,
  rules_text TEXT,
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS snapshots (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  market_id INTEGER NOT NULL,
  ts TEXT NOT NULL,
  yes_ask REAL, yes_bid REAL,
  no_ask REAL, no_bid REAL,
  book_depth_usd REAL,
  FOREIGN KEY(market_id) REFERENCES markets(id)
);

CREATE TABLE IF NOT EXISTS signals (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  market_id INTEGER NOT NULL,
  strategy TEXT NOT NULL,
  ts TEXT NOT NULL,
  p_final REAL NOT NULL,
  confidence REAL NOT NULL,
  metadata_json TEXT,
  FOREIGN KEY(market_id) REFERENCES markets(id)
);

CREATE TABLE IF NOT EXISTS paper_trades (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  market_id INTEGER NOT NULL,
  strategy TEXT NOT NULL,
  ts TEXT NOT NULL,
  side TEXT NOT NULL,                 -- YES or NO
  price_filled REAL NOT NULL,         -- VWAP after walking the book + slippage
  stake REAL NOT NULL,                -- USD risked
  shares REAL NOT NULL,               -- stake / price_filled
  p_model_at_entry REAL NOT NULL,
  edge_at_entry REAL NOT NULL,
  levels_consumed_json TEXT,          -- [{price, size_taken}, ...]
  status TEXT NOT NULL DEFAULT 'OPEN',
  pnl REAL,
  FOREIGN KEY(market_id) REFERENCES markets(id)
);

CREATE TABLE IF NOT EXISTS settlements (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  market_id INTEGER NOT NULL,
  actual_value REAL,                  -- TRUTH (WU when available, else OM). Use this for bias correction.
  om_value REAL,                      -- Open-Meteo archive value (secondary)
  wu_value REAL,                      -- Wunderground value (primary truth)
  source_value TEXT,                  -- e.g. "wunderground <station>" or "open-meteo archive <station>"
  wu_source TEXT,                     -- WU history page URL or error note
  outcome TEXT NOT NULL,              -- YES / NO / DISPUTED / VOID
  settled_at TEXT NOT NULL,
  FOREIGN KEY(market_id) REFERENCES markets(id)
);

CREATE TABLE IF NOT EXISTS daily_report (
  date TEXT NOT NULL,
  strategy TEXT NOT NULL,
  n_trades INTEGER NOT NULL,
  n_wins INTEGER NOT NULL,
  pnl REAL NOT NULL,
  brier REAL,
  bankroll REAL NOT NULL,
  PRIMARY KEY (date, strategy)
);

CREATE INDEX IF NOT EXISTS idx_snapshots_market ON snapshots(market_id, ts);
CREATE INDEX IF NOT EXISTS idx_signals_market ON signals(market_id, strategy, ts);
CREATE INDEX IF NOT EXISTS idx_trades_status ON paper_trades(status, strategy);
"""


def utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Ledger:
    def __init__(self, db_path: str | Path):
        self.db_path = str(db_path)
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        with self._conn() as c:
            c.executescript(SCHEMA)
            self._migrate(c)

    @staticmethod
    def _migrate(c: sqlite3.Connection) -> None:
        """Forward-only column additions for older DBs. SQLite has no
        ALTER TABLE ... IF NOT EXISTS, so check PRAGMA first."""
        for table, col, type_ in [
            ("settlements", "wu_value", "REAL"),
            ("settlements", "wu_source", "TEXT"),
            ("settlements", "om_value", "REAL"),
        ]:
            cols = [r[1] for r in c.execute(
                f"PRAGMA table_info({table})").fetchall()]
            if col not in cols:
                c.execute(f"ALTER TABLE {table} ADD COLUMN {col} {type_}")

    @contextmanager
    def _conn(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    # --- markets ----------------------------------------------------------
    def upsert_market(self, m: dict[str, Any]) -> int:
        with self._conn() as c:
            row = c.execute(
                "SELECT id FROM markets WHERE condition_id = ?",
                (m["condition_id"],),
            ).fetchone()
            if row:
                c.execute(
                    """UPDATE markets SET slug=?, question=?, category=?,
                        threshold=?, unit=?, resolve_date=?, resolution_source=?,
                        rules_text=? WHERE id=?""",
                    (
                        m.get("slug"), m.get("question"), m.get("category"),
                        m.get("threshold"), m.get("unit"), m.get("resolve_date"),
                        m.get("resolution_source"), m.get("rules_text"), row["id"],
                    ),
                )
                return int(row["id"])
            cur = c.execute(
                """INSERT INTO markets (condition_id, slug, question, category,
                    threshold, unit, resolve_date, resolution_source, rules_text, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    m["condition_id"], m.get("slug"), m.get("question"),
                    m.get("category"), m.get("threshold"), m.get("unit"),
                    m.get("resolve_date"), m.get("resolution_source"),
                    m.get("rules_text"), utcnow_iso(),
                ),
            )
            return int(cur.lastrowid)

    # --- snapshots --------------------------------------------------------
    def record_snapshot(self, market_id: int, yes_ask, yes_bid, no_ask, no_bid,
                        book_depth_usd) -> None:
        with self._conn() as c:
            c.execute(
                """INSERT INTO snapshots (market_id, ts, yes_ask, yes_bid,
                    no_ask, no_bid, book_depth_usd) VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (market_id, utcnow_iso(), yes_ask, yes_bid, no_ask, no_bid,
                 book_depth_usd),
            )

    # --- signals ----------------------------------------------------------
    def record_signal(self, market_id: int, strategy: str, p_final: float,
                      confidence: float, metadata: dict | None) -> int:
        with self._conn() as c:
            cur = c.execute(
                """INSERT INTO signals (market_id, strategy, ts, p_final,
                    confidence, metadata_json) VALUES (?, ?, ?, ?, ?, ?)""",
                (market_id, strategy, utcnow_iso(), p_final, confidence,
                 json.dumps(metadata or {})),
            )
            return int(cur.lastrowid)

    # --- trades -----------------------------------------------------------
    def record_trade(self, market_id: int, strategy: str, side: str,
                     price_filled: float, stake: float, shares: float,
                     p_model_at_entry: float, edge_at_entry: float,
                     levels_consumed: list[dict]) -> int:
        with self._conn() as c:
            cur = c.execute(
                """INSERT INTO paper_trades (market_id, strategy, ts, side,
                    price_filled, stake, shares, p_model_at_entry,
                    edge_at_entry, levels_consumed_json, status)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'OPEN')""",
                (market_id, strategy, utcnow_iso(), side, price_filled,
                 stake, shares, p_model_at_entry, edge_at_entry,
                 json.dumps(levels_consumed)),
            )
            return int(cur.lastrowid)

    def already_traded_today(self, market_id: int, strategy: str,
                             day_iso: str) -> bool:
        with self._conn() as c:
            row = c.execute(
                """SELECT 1 FROM paper_trades
                   WHERE market_id=? AND strategy=? AND substr(ts,1,10)=?""",
                (market_id, strategy, day_iso),
            ).fetchone()
            return row is not None

    def open_positions(self, strategy: str | None = None) -> list[sqlite3.Row]:
        with self._conn() as c:
            if strategy:
                return list(c.execute(
                    "SELECT * FROM paper_trades WHERE status='OPEN' AND strategy=?",
                    (strategy,),
                ).fetchall())
            return list(c.execute(
                "SELECT * FROM paper_trades WHERE status='OPEN'"
            ).fetchall())

    def all_trades(self, strategy: str | None = None) -> list[sqlite3.Row]:
        with self._conn() as c:
            if strategy:
                return list(c.execute(
                    "SELECT * FROM paper_trades WHERE strategy=? ORDER BY id",
                    (strategy,),
                ).fetchall())
            return list(c.execute(
                "SELECT * FROM paper_trades ORDER BY id"
            ).fetchall())

    # --- settlements ------------------------------------------------------
    def record_settlement(self, market_id: int, actual_value: float | None,
                          source_value: str, outcome: str,
                          om_value: float | None = None,
                          wu_value: float | None = None,
                          wu_source: str | None = None) -> int:
        with self._conn() as c:
            cur = c.execute(
                """INSERT INTO settlements (market_id, actual_value, om_value,
                    wu_value, source_value, wu_source, outcome, settled_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (market_id, actual_value, om_value, wu_value, source_value,
                 wu_source, outcome, utcnow_iso()),
            )
            return int(cur.lastrowid)

    def get_settlement(self, market_id: int) -> sqlite3.Row | None:
        with self._conn() as c:
            return c.execute(
                "SELECT * FROM settlements WHERE market_id=?",
                (market_id,),
            ).fetchone()

    def close_trade(self, trade_id: int, status: str, pnl: float) -> None:
        with self._conn() as c:
            c.execute(
                "UPDATE paper_trades SET status=?, pnl=? WHERE id=?",
                (status, pnl, trade_id),
            )

    def get_market(self, market_id: int) -> sqlite3.Row | None:
        with self._conn() as c:
            return c.execute("SELECT * FROM markets WHERE id=?",
                             (market_id,)).fetchone()

    # --- reporting --------------------------------------------------------
    def upsert_daily_report(self, date_iso: str, strategy: str, n_trades: int,
                            n_wins: int, pnl: float, brier: float | None,
                            bankroll: float) -> None:
        with self._conn() as c:
            c.execute(
                """INSERT INTO daily_report (date, strategy, n_trades, n_wins,
                    pnl, brier, bankroll) VALUES (?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(date, strategy) DO UPDATE SET
                    n_trades=excluded.n_trades, n_wins=excluded.n_wins,
                    pnl=excluded.pnl, brier=excluded.brier,
                    bankroll=excluded.bankroll""",
                (date_iso, strategy, n_trades, n_wins, pnl, brier, bankroll),
            )

    def latest_daily_report(self, strategy: str) -> sqlite3.Row | None:
        with self._conn() as c:
            return c.execute(
                """SELECT * FROM daily_report WHERE strategy=?
                   ORDER BY date DESC LIMIT 1""",
                (strategy,),
            ).fetchone()

    def bankroll(self, strategy: str, starting: float) -> float:
        """Realised bankroll = starting + sum(pnl of closed trades)."""
        with self._conn() as c:
            row = c.execute(
                """SELECT COALESCE(SUM(pnl), 0) AS p FROM paper_trades
                   WHERE strategy=? AND status IN ('WIN','LOSS','VOID')""",
                (strategy,),
            ).fetchone()
            return float(starting) + float(row["p"])

    def open_stake(self, strategy: str) -> float:
        with self._conn() as c:
            row = c.execute(
                """SELECT COALESCE(SUM(stake),0) AS s FROM paper_trades
                   WHERE strategy=? AND status='OPEN'""",
                (strategy,),
            ).fetchone()
            return float(row["s"])
