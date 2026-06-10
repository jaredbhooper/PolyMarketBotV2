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

-- Bucket-sum arbitrage detector tables (strategy #2).
-- arb_gaps logs EVERY detected gap (even sub-threshold) so we can measure
-- how often real, fillable complete sets actually exist in the wild.
CREATE TABLE IF NOT EXISTS arb_gaps (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts TEXT NOT NULL,
  strategy TEXT NOT NULL,
  event_id TEXT NOT NULL,
  event_slug TEXT,
  event_title TEXT,
  n_legs INTEGER NOT NULL,
  completeness_verified INTEGER NOT NULL,    -- 1 if MECE confirmed
  completeness_note TEXT,
  side TEXT NOT NULL,                        -- YES or NO
  walk_mode TEXT NOT NULL,                   -- 'gamma_only' or 'full_book'
  target_shares REAL,
  executable_shares REAL,                    -- min fillable across legs
  sum_vwap_per_share REAL,                   -- sum of leg VWAPs
  slippage_per_share REAL,                   -- N * slippage_cents
  safety_buffer REAL,
  payout_per_share REAL,                     -- 1.0 (YES) or N-1 (NO)
  locked_profit_per_share REAL,              -- payout - sum_vwap - slippage - buffer
  locked_profit_usd REAL,                    -- profit_per_share * executable_shares
  end_date_iso TEXT,
  legs_json TEXT,                            -- per-leg vwap, depth, levels
  cleared_threshold INTEGER NOT NULL DEFAULT 0
);

-- An arb_positions row is one paper-traded complete-set arb. Each row links
-- to N arb_legs (one per outcome). All N legs settle together when the
-- event resolves.
CREATE TABLE IF NOT EXISTS arb_positions (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  strategy TEXT NOT NULL,
  ts TEXT NOT NULL,
  event_id TEXT NOT NULL,
  event_slug TEXT,
  event_title TEXT,
  side TEXT NOT NULL,                        -- YES or NO (which side we bought on every leg)
  n_legs INTEGER NOT NULL,
  shares REAL NOT NULL,                      -- common share count across legs
  total_cost REAL NOT NULL,                  -- sum(leg cost)
  expected_payout REAL NOT NULL,             -- shares * payout_per_share
  locked_profit REAL NOT NULL,               -- expected_payout - total_cost
  status TEXT NOT NULL DEFAULT 'OPEN',       -- OPEN, CLOSED, VOID
  pnl REAL,
  end_date_iso TEXT
);

CREATE TABLE IF NOT EXISTS arb_legs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  position_id INTEGER NOT NULL,
  market_id TEXT NOT NULL,                   -- conditionId
  leg_title TEXT,
  token_id TEXT,
  side TEXT NOT NULL,                        -- matches parent for now
  vwap REAL NOT NULL,
  price_filled REAL NOT NULL,                -- vwap + slippage_cents
  shares REAL NOT NULL,
  cost REAL NOT NULL,                        -- shares * price_filled
  levels_consumed_json TEXT,
  outcome TEXT,                              -- YES/NO/VOID once graded
  payout REAL,                               -- realized payout
  FOREIGN KEY(position_id) REFERENCES arb_positions(id)
);

-- Cross-venue (Polymarket x Kalshi) arbitrage tables (strategy #3).
-- cv_pairs is one row per matched candidate pair, persistent across
-- cycles. The equivalence classification + per-criterion verdicts +
-- divergence_risk_note all live here. The detector re-uses the pair
-- row to log each cycle's gap into cv_gaps.
CREATE TABLE IF NOT EXISTS cv_pairs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts_first_seen TEXT NOT NULL,
  ts_last_classified TEXT NOT NULL,
  poly_market_id TEXT NOT NULL,
  kalshi_ticker TEXT NOT NULL,
  poly_title TEXT,
  kalshi_title TEXT,
  poly_leg TEXT,
  kalshi_leg TEXT,
  poly_close TEXT,
  kalshi_close TEXT,
  poly_source TEXT,
  kalshi_source TEXT,
  city TEXT,
  date TEXT,
  classification TEXT NOT NULL,        -- CERTIFIED-IDENTICAL | FUZZY | NON-MATCH
  reason TEXT,
  criteria_json TEXT,
  divergence_risk_note TEXT,
  UNIQUE(poly_market_id, kalshi_ticker)
);

CREATE TABLE IF NOT EXISTS cv_gaps (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts TEXT NOT NULL,
  strategy TEXT NOT NULL,
  pair_id INTEGER NOT NULL,
  direction TEXT NOT NULL,             -- 'POLY_YES_KAL_NO' or 'POLY_NO_KAL_YES'
  classification TEXT NOT NULL,
  poly_vwap REAL,
  poly_fee REAL,                       -- per-share USD
  kalshi_vwap REAL,
  kalshi_fee REAL,
  safety_buffer REAL,
  target_shares REAL,
  executable_shares REAL,
  total_cost_per_share REAL,
  locked_profit_per_share REAL,
  locked_profit_usd REAL,
  divergence_risk_note TEXT,
  cleared_threshold INTEGER NOT NULL DEFAULT 0,
  legs_json TEXT,                      -- per-leg detail incl. consumed levels
  FOREIGN KEY(pair_id) REFERENCES cv_pairs(id)
);

CREATE TABLE IF NOT EXISTS cv_positions (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  strategy TEXT NOT NULL,
  ts TEXT NOT NULL,
  pair_id INTEGER NOT NULL,
  direction TEXT NOT NULL,
  shares REAL NOT NULL,
  total_cost REAL NOT NULL,            -- both legs incl. fees
  expected_payout REAL NOT NULL,       -- one leg pays $1, the other pays $0
  locked_profit REAL NOT NULL,
  divergence_risk_note TEXT,
  status TEXT NOT NULL DEFAULT 'OPEN',  -- OPEN | CLOSED | VOID | DIVERGED
  pnl REAL,
  FOREIGN KEY(pair_id) REFERENCES cv_pairs(id)
);

CREATE TABLE IF NOT EXISTS cv_legs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  position_id INTEGER NOT NULL,
  venue TEXT NOT NULL,                  -- polymarket | kalshi
  venue_market_id TEXT NOT NULL,
  side TEXT NOT NULL,                   -- YES | NO
  vwap REAL NOT NULL,
  price_filled REAL NOT NULL,           -- vwap + slippage + fee_per_share
  fee_per_share REAL NOT NULL,
  shares REAL NOT NULL,
  cost REAL NOT NULL,
  levels_consumed_json TEXT,
  outcome TEXT,                         -- YES | NO | VOID once graded
  payout REAL,
  FOREIGN KEY(position_id) REFERENCES cv_positions(id)
);

CREATE INDEX IF NOT EXISTS idx_snapshots_market ON snapshots(market_id, ts);
CREATE INDEX IF NOT EXISTS idx_signals_market ON signals(market_id, strategy, ts);
CREATE INDEX IF NOT EXISTS idx_trades_status ON paper_trades(status, strategy);
CREATE INDEX IF NOT EXISTS idx_arb_gaps_event ON arb_gaps(event_id, ts);
CREATE INDEX IF NOT EXISTS idx_arb_positions_status ON arb_positions(status, strategy);
CREATE INDEX IF NOT EXISTS idx_arb_legs_position ON arb_legs(position_id);
CREATE INDEX IF NOT EXISTS idx_cv_gaps_pair ON cv_gaps(pair_id, ts);
CREATE INDEX IF NOT EXISTS idx_cv_positions_status ON cv_positions(status, strategy);
CREATE INDEX IF NOT EXISTS idx_cv_legs_position ON cv_legs(position_id);
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

    # --- arb gaps + arb positions (strategy #2) ----------------------------
    def record_arb_gap(self, gap: dict[str, Any]) -> int:
        """Insert one gap log row. Callers should always populate the
        per-share fields; legs_json carries the per-leg breakdown."""
        with self._conn() as c:
            cur = c.execute(
                """INSERT INTO arb_gaps (
                    ts, strategy, event_id, event_slug, event_title, n_legs,
                    completeness_verified, completeness_note,
                    side, walk_mode, target_shares, executable_shares,
                    sum_vwap_per_share, slippage_per_share, safety_buffer,
                    payout_per_share, locked_profit_per_share,
                    locked_profit_usd, end_date_iso, legs_json,
                    cleared_threshold)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    utcnow_iso(), gap["strategy"], gap["event_id"],
                    gap.get("event_slug"), gap.get("event_title"),
                    int(gap["n_legs"]),
                    1 if gap.get("completeness_verified") else 0,
                    gap.get("completeness_note"),
                    gap["side"], gap["walk_mode"],
                    gap.get("target_shares"), gap.get("executable_shares"),
                    gap.get("sum_vwap_per_share"),
                    gap.get("slippage_per_share"),
                    gap.get("safety_buffer"),
                    gap.get("payout_per_share"),
                    gap.get("locked_profit_per_share"),
                    gap.get("locked_profit_usd"),
                    gap.get("end_date_iso"),
                    json.dumps(gap.get("legs") or []),
                    1 if gap.get("cleared_threshold") else 0,
                ),
            )
            return int(cur.lastrowid)

    def record_arb_position(self, pos: dict[str, Any],
                             legs: list[dict[str, Any]]) -> int:
        with self._conn() as c:
            cur = c.execute(
                """INSERT INTO arb_positions (
                    strategy, ts, event_id, event_slug, event_title, side,
                    n_legs, shares, total_cost, expected_payout,
                    locked_profit, status, end_date_iso)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'OPEN', ?)""",
                (
                    pos["strategy"], utcnow_iso(), pos["event_id"],
                    pos.get("event_slug"), pos.get("event_title"),
                    pos["side"], int(pos["n_legs"]),
                    float(pos["shares"]), float(pos["total_cost"]),
                    float(pos["expected_payout"]),
                    float(pos["locked_profit"]),
                    pos.get("end_date_iso"),
                ),
            )
            pid = int(cur.lastrowid)
            for leg in legs:
                c.execute(
                    """INSERT INTO arb_legs (
                        position_id, market_id, leg_title, token_id, side,
                        vwap, price_filled, shares, cost, levels_consumed_json)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        pid, leg["market_id"], leg.get("leg_title"),
                        leg.get("token_id"), leg["side"],
                        float(leg["vwap"]), float(leg["price_filled"]),
                        float(leg["shares"]), float(leg["cost"]),
                        json.dumps(leg.get("levels_consumed") or []),
                    ),
                )
            return pid

    def open_arb_positions(self, strategy: str | None = None) -> list[sqlite3.Row]:
        with self._conn() as c:
            if strategy:
                return list(c.execute(
                    "SELECT * FROM arb_positions WHERE status='OPEN' AND strategy=?",
                    (strategy,)).fetchall())
            return list(c.execute(
                "SELECT * FROM arb_positions WHERE status='OPEN'").fetchall())

    def arb_legs_for(self, position_id: int) -> list[sqlite3.Row]:
        with self._conn() as c:
            return list(c.execute(
                "SELECT * FROM arb_legs WHERE position_id=? ORDER BY id",
                (position_id,)).fetchall())

    def close_arb_position(self, position_id: int, status: str, pnl: float,
                            leg_outcomes: list[dict[str, Any]]) -> None:
        with self._conn() as c:
            c.execute(
                "UPDATE arb_positions SET status=?, pnl=? WHERE id=?",
                (status, pnl, position_id),
            )
            for lo in leg_outcomes:
                c.execute(
                    "UPDATE arb_legs SET outcome=?, payout=? WHERE id=?",
                    (lo["outcome"], float(lo["payout"]), int(lo["leg_id"])),
                )

    def already_arb_today(self, event_id: str, strategy: str, side: str,
                           day_iso: str) -> bool:
        with self._conn() as c:
            row = c.execute(
                """SELECT 1 FROM arb_positions
                   WHERE event_id=? AND strategy=? AND side=?
                     AND substr(ts,1,10)=?""",
                (event_id, strategy, side, day_iso)).fetchone()
            return row is not None

    # --- cross-venue (strategy #3) ----------------------------------------
    def upsert_cv_pair(self, pair: dict[str, Any]) -> int:
        with self._conn() as c:
            row = c.execute(
                "SELECT id FROM cv_pairs WHERE poly_market_id=? AND kalshi_ticker=?",
                (pair["poly_market_id"], pair["kalshi_ticker"]),
            ).fetchone()
            if row:
                c.execute(
                    """UPDATE cv_pairs SET ts_last_classified=?, poly_title=?,
                        kalshi_title=?, poly_leg=?, kalshi_leg=?, poly_close=?,
                        kalshi_close=?, poly_source=?, kalshi_source=?, city=?,
                        date=?, classification=?, reason=?, criteria_json=?,
                        divergence_risk_note=? WHERE id=?""",
                    (
                        utcnow_iso(), pair.get("poly_title"), pair.get("kalshi_title"),
                        pair.get("poly_leg"), pair.get("kalshi_leg"),
                        pair.get("poly_close"), pair.get("kalshi_close"),
                        pair.get("poly_source"), pair.get("kalshi_source"),
                        pair.get("city"), pair.get("date"),
                        pair["classification"], pair.get("reason"),
                        json.dumps(pair.get("criteria") or {}),
                        pair.get("divergence_risk_note") or "",
                        int(row["id"]),
                    ),
                )
                return int(row["id"])
            cur = c.execute(
                """INSERT INTO cv_pairs (
                    ts_first_seen, ts_last_classified, poly_market_id, kalshi_ticker,
                    poly_title, kalshi_title, poly_leg, kalshi_leg,
                    poly_close, kalshi_close, poly_source, kalshi_source,
                    city, date, classification, reason, criteria_json,
                    divergence_risk_note)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    utcnow_iso(), utcnow_iso(),
                    pair["poly_market_id"], pair["kalshi_ticker"],
                    pair.get("poly_title"), pair.get("kalshi_title"),
                    pair.get("poly_leg"), pair.get("kalshi_leg"),
                    pair.get("poly_close"), pair.get("kalshi_close"),
                    pair.get("poly_source"), pair.get("kalshi_source"),
                    pair.get("city"), pair.get("date"),
                    pair["classification"], pair.get("reason"),
                    json.dumps(pair.get("criteria") or {}),
                    pair.get("divergence_risk_note") or "",
                ),
            )
            return int(cur.lastrowid)

    def record_cv_gap(self, gap: dict[str, Any]) -> int:
        with self._conn() as c:
            cur = c.execute(
                """INSERT INTO cv_gaps (
                    ts, strategy, pair_id, direction, classification,
                    poly_vwap, poly_fee, kalshi_vwap, kalshi_fee,
                    safety_buffer, target_shares, executable_shares,
                    total_cost_per_share, locked_profit_per_share,
                    locked_profit_usd, divergence_risk_note,
                    cleared_threshold, legs_json)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    utcnow_iso(), gap["strategy"], int(gap["pair_id"]),
                    gap["direction"], gap["classification"],
                    gap.get("poly_vwap"), gap.get("poly_fee"),
                    gap.get("kalshi_vwap"), gap.get("kalshi_fee"),
                    gap.get("safety_buffer"),
                    gap.get("target_shares"), gap.get("executable_shares"),
                    gap.get("total_cost_per_share"),
                    gap.get("locked_profit_per_share"),
                    gap.get("locked_profit_usd"),
                    gap.get("divergence_risk_note") or "",
                    1 if gap.get("cleared_threshold") else 0,
                    json.dumps(gap.get("legs") or []),
                ),
            )
            return int(cur.lastrowid)

    def record_cv_position(self, pos: dict[str, Any],
                            legs: list[dict[str, Any]]) -> int:
        with self._conn() as c:
            cur = c.execute(
                """INSERT INTO cv_positions (
                    strategy, ts, pair_id, direction, shares, total_cost,
                    expected_payout, locked_profit, divergence_risk_note,
                    status) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'OPEN')""",
                (
                    pos["strategy"], utcnow_iso(), int(pos["pair_id"]),
                    pos["direction"], float(pos["shares"]),
                    float(pos["total_cost"]), float(pos["expected_payout"]),
                    float(pos["locked_profit"]),
                    pos.get("divergence_risk_note") or "",
                ),
            )
            pid = int(cur.lastrowid)
            for leg in legs:
                c.execute(
                    """INSERT INTO cv_legs (
                        position_id, venue, venue_market_id, side, vwap,
                        price_filled, fee_per_share, shares, cost,
                        levels_consumed_json) VALUES (?,?,?,?,?,?,?,?,?,?)""",
                    (
                        pid, leg["venue"], leg["venue_market_id"], leg["side"],
                        float(leg["vwap"]), float(leg["price_filled"]),
                        float(leg.get("fee_per_share") or 0.0),
                        float(leg["shares"]), float(leg["cost"]),
                        json.dumps(leg.get("levels_consumed") or []),
                    ),
                )
            return pid

    def cv_pair_stats(self) -> dict[str, Any]:
        with self._conn() as c:
            rows = c.execute(
                """SELECT classification, COUNT(*) as n FROM cv_pairs
                   GROUP BY classification""").fetchall()
            d = {r["classification"]: int(r["n"]) for r in rows}
            divergence = c.execute(
                """SELECT COUNT(*) as n FROM cv_pairs
                   WHERE divergence_risk_note IS NOT NULL
                     AND divergence_risk_note <> ''""").fetchone()
            d["with_divergence_risk_note"] = int(divergence["n"] or 0)
            return d

    def cv_gap_stats(self, strategy: str) -> dict[str, Any]:
        with self._conn() as c:
            rows = c.execute(
                """SELECT classification, direction, cleared_threshold,
                          locked_profit_per_share, divergence_risk_note
                     FROM cv_gaps WHERE strategy=?""",
                (strategy,)).fetchall()
        stats = {
            "total": len(rows),
            "by_classification": {},
            "by_direction": {},
            "cleared": sum(1 for r in rows if r["cleared_threshold"]),
            "with_divergence": sum(1 for r in rows if r["divergence_risk_note"]),
            "profit_buckets": {},
        }
        for r in rows:
            stats["by_classification"][r["classification"]] = (
                stats["by_classification"].get(r["classification"], 0) + 1)
            stats["by_direction"][r["direction"]] = (
                stats["by_direction"].get(r["direction"], 0) + 1)
            lp = r["locked_profit_per_share"]
            if lp is None:
                continue
            for hi in (-0.10, -0.05, -0.02, -0.01, 0.0, 0.005, 0.01, 0.02, 0.05, 0.10, 1.0):
                if lp < hi:
                    stats["profit_buckets"][hi] = stats["profit_buckets"].get(hi, 0) + 1
                    break
        return stats

    def open_cv_positions(self, strategy: str | None = None) -> list[sqlite3.Row]:
        with self._conn() as c:
            if strategy:
                return list(c.execute(
                    "SELECT * FROM cv_positions WHERE status='OPEN' AND strategy=?",
                    (strategy,)).fetchall())
            return list(c.execute(
                "SELECT * FROM cv_positions WHERE status='OPEN'").fetchall())

    def cv_legs_for(self, position_id: int) -> list[sqlite3.Row]:
        with self._conn() as c:
            return list(c.execute(
                "SELECT * FROM cv_legs WHERE position_id=? ORDER BY id",
                (position_id,)).fetchall())

    def close_cv_position(self, position_id: int, status: str, pnl: float,
                           leg_outcomes: list[dict[str, Any]]) -> None:
        with self._conn() as c:
            c.execute(
                "UPDATE cv_positions SET status=?, pnl=? WHERE id=?",
                (status, pnl, position_id),
            )
            for lo in leg_outcomes:
                c.execute(
                    "UPDATE cv_legs SET outcome=?, payout=? WHERE id=?",
                    (lo["outcome"], float(lo["payout"]), int(lo["leg_id"])),
                )

    def cv_pair_traded_today(self, pair_id: int, strategy: str,
                              direction: str, day_iso: str) -> bool:
        with self._conn() as c:
            row = c.execute(
                """SELECT 1 FROM cv_positions
                   WHERE pair_id=? AND strategy=? AND direction=?
                     AND substr(ts,1,10)=?""",
                (pair_id, strategy, direction, day_iso)).fetchone()
            return row is not None

    def arb_gap_stats(self, strategy: str) -> dict[str, Any]:
        """Aggregate distribution of detected gaps for the diagnostics print.

        profit_buckets is split by walk_mode so full_book (true fillable
        profit) and gamma_only (snapshot estimate) read separately.
        """
        with self._conn() as c:
            rows = c.execute(
                """SELECT walk_mode, side, completeness_verified,
                          locked_profit_per_share, cleared_threshold
                     FROM arb_gaps WHERE strategy=?""",
                (strategy,)).fetchall()
        stats = {
            "total": len(rows),
            "verified": sum(1 for r in rows if r["completeness_verified"]),
            "by_mode": {},
            "by_side": {},
            "profit_buckets": {"full_book": {}, "gamma_only": {}},
            "cleared": sum(1 for r in rows if r["cleared_threshold"]),
        }
        for r in rows:
            stats["by_mode"][r["walk_mode"]] = stats["by_mode"].get(r["walk_mode"], 0) + 1
            stats["by_side"][r["side"]] = stats["by_side"].get(r["side"], 0) + 1
            lp = r["locked_profit_per_share"]
            if lp is None:
                continue
            buckets = stats["profit_buckets"].setdefault(r["walk_mode"], {})
            for hi in (-0.10, -0.05, -0.02, -0.01, 0.0, 0.005, 0.01, 0.02, 0.05, 0.10, 1.0):
                if lp < hi:
                    buckets[hi] = buckets.get(hi, 0) + 1
                    break
        return stats
