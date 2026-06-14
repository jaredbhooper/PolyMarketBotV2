"""SQLite ledger. Two databases, attached as one logical store.

  ledger.db (committed; must stay under 50 MB) - the irreplaceable
    paper-trading record: positions, settlements, bankroll, equity,
    health, daily/scout reports, sharpline orders, logic violations,
    arb_positions / arb_multi / cv_positions, plus the lookup tables
    they reference (markets, signals).

  cache.db (gitignored; rebuildable) - raw data the workflows can
    re-pull on demand: scan snapshots, arb_gaps detail, cv_pairs +
    cv_gaps detail, wallet trade history, odds api response cache,
    lp_sim estimates.

The Ledger class always operates on a single SQLite connection with
cache.db attached as schema name `cache`. All cache-table queries are
prefixed accordingly (e.g. `INSERT INTO cache.snapshots ...`). The
classification source-of-truth is `tools/migrate_split_db.py`.
"""
from __future__ import annotations

import json
import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

LEDGER_SCHEMA = """
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
-- cv_pairs / cv_gaps live in cache.db; cv_positions + cv_legs are the
-- paper-traded record and live in ledger.db.
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

CREATE INDEX IF NOT EXISTS idx_signals_market ON signals(market_id, strategy, ts);
CREATE INDEX IF NOT EXISTS idx_trades_status ON paper_trades(status, strategy);
CREATE INDEX IF NOT EXISTS idx_arb_positions_status ON arb_positions(status, strategy);
CREATE INDEX IF NOT EXISTS idx_arb_legs_position ON arb_legs(position_id);
-- Weather verification + adaptive-weighting telemetry (v2.1).
-- One row per resolved weather market. Logged by the grader on
-- settlement; backfilled from existing signals + settlements where data
-- permits. Source of truth for foundation.wx_skill which derives
-- per-city family weights, bias corrections, and calibration.
--
-- Core principle (this lives here too so it travels with the schema):
-- we NEVER select or prune individual ensemble members. We only ever
-- learn (a) MODEL FAMILY weights, (b) per-city BIAS corrections, (c)
-- probability CALIBRATION. Per-member skill is noise.
CREATE TABLE IF NOT EXISTS wx_verification (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts_logged TEXT NOT NULL,
  market_row_id INTEGER NOT NULL,
  city TEXT,
  station TEXT,
  threshold REAL,
  unit TEXT,
  bound TEXT,
  resolve_date TEXT,
  lead_time_hours REAL,
  -- Both observed values + the official market settlement value.
  official_value REAL,
  official_value_unit TEXT,
  om_value REAL,
  om_value_unit TEXT,
  wu_value REAL,
  wu_value_unit TEXT,
  -- Per family (gfs = GEFS; ecmwf = IFS + AIFS pooled). Skill is
  -- learned at FAMILY granularity, never per-member.
  gfs_mean REAL,
  gfs_spread REAL,
  gfs_p_threshold REAL,
  gfs_signed_error REAL,
  gfs_abs_error REAL,
  ecmwf_mean REAL,
  ecmwf_spread REAL,
  ecmwf_p_threshold REAL,
  ecmwf_signed_error REAL,
  ecmwf_abs_error REAL,
  -- Blended probability + market state
  p_blended REAL,
  market_price REAL,
  outcome TEXT,
  signal_id INTEGER,
  settlement_id INTEGER,
  UNIQUE(market_row_id)
);

-- SHARPLINE / LP-SIM / LOGIC-SCAN (Prompt C). All paper-only.
-- ESTIMATE tag on each row whose pnl came from a maker-side simulation.
CREATE TABLE IF NOT EXISTS odds_api_log (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts TEXT NOT NULL,
  month TEXT NOT NULL,
  sport TEXT NOT NULL,
  status_code INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS sharpline_matches (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts TEXT NOT NULL,
  sport_key TEXT NOT NULL,
  poly_market_id TEXT,
  poly_event_slug TEXT,
  bookmaker_event_id TEXT,
  home_team TEXT,
  away_team TEXT,
  confidence REAL NOT NULL,
  status TEXT NOT NULL                 -- MATCHED | UNMATCHED | AMBIGUOUS
);

CREATE TABLE IF NOT EXISTS sharpline_orders (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts TEXT NOT NULL,
  match_id INTEGER NOT NULL,
  poly_market_id TEXT NOT NULL,
  side TEXT NOT NULL,                   -- YES | NO
  outcome TEXT,                         -- 'home' | 'away' | etc
  our_price REAL NOT NULL,              -- limit price we 'posted'
  fair_prob_at_post REAL NOT NULL,
  edge_at_post REAL NOT NULL,
  stake_usd REAL NOT NULL,
  league TEXT,
  status TEXT NOT NULL,                 -- RESTING | FILLED | CANCELLED | UNFILLED_RESOLVED
  filled_at TEXT,
  line_at_fill REAL,                    -- fair_prob at the moment of fill
  adverse_selection REAL,               -- fair_prob_at_post - line_at_fill (signed)
  resolved_outcome TEXT,                -- WIN | LOSS | VOID once settled
  realized_pnl REAL,
  estimate_marker TEXT NOT NULL DEFAULT 'ESTIMATE'
);

CREATE TABLE IF NOT EXISTS logic_pairs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  event_id TEXT NOT NULL,
  market_a_id TEXT NOT NULL,            -- "more likely" implication child
  market_b_id TEXT NOT NULL,            -- "stronger" implication parent (A => B)
  template TEXT NOT NULL,
  confidence REAL NOT NULL,
  first_seen TEXT NOT NULL,
  notes TEXT
);

CREATE TABLE IF NOT EXISTS logic_violations (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts TEXT NOT NULL,
  pair_id INTEGER NOT NULL,
  pa REAL NOT NULL,
  pb REAL NOT NULL,
  margin REAL NOT NULL,                 -- pa - pb
  status TEXT NOT NULL,                 -- traded | near_miss
  stake_usd REAL,
  realized_pnl REAL,
  resolved_at TEXT,
  FOREIGN KEY(pair_id) REFERENCES logic_pairs(id)
);

-- Virtual bankroll + master report + health monitor (Prompt B).
-- bankroll_allocations holds the latest snapshot per strategy; the full
-- audit trail of every debit / credit lives in bankroll_transactions so
-- any bankroll number on the master report can be reconstructed from
-- the txn log.
CREATE TABLE IF NOT EXISTS bankroll_allocations (
  strategy TEXT PRIMARY KEY,
  pct REAL NOT NULL,
  starting_alloc_usd REAL NOT NULL,
  current_cash_usd REAL NOT NULL,
  open_exposure_usd REAL NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS bankroll_transactions (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts TEXT NOT NULL,
  strategy TEXT NOT NULL,
  kind TEXT NOT NULL,                  -- 'debit' (open) | 'credit' (close) | 'init' | 'skipped_no_capital'
  amount_usd REAL NOT NULL,
  related_table TEXT,                  -- paper_trades | arb_positions | arb_multi | cv_positions | copied_trades
  related_id INTEGER,
  cash_after_usd REAL NOT NULL,
  exposure_after_usd REAL NOT NULL,
  note TEXT
);

CREATE TABLE IF NOT EXISTS equity_history (
  date TEXT NOT NULL,
  strategy TEXT NOT NULL,
  cash_usd REAL NOT NULL,
  open_exposure_usd REAL NOT NULL,
  realized_pnl_today REAL NOT NULL,
  PRIMARY KEY(date, strategy)
);

CREATE TABLE IF NOT EXISTS health_log (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts TEXT NOT NULL,
  strategy TEXT NOT NULL,
  ok INTEGER NOT NULL,
  duration_s REAL,
  markets_scanned INTEGER,
  fills INTEGER,
  error_text TEXT,
  extras_json TEXT
);

-- Multi-outcome arb extension (Prompt A): arb_multi table.
-- Distinct from arb_positions: arb_multi uses a fixed $10 notional stake
-- per full set, tracks net_gap_pct AFTER fees, and uses 'unfillable_leg'
-- as a first-class status (research shows ~73% of historical arb profit
-- comes from multi-outcome rebalancing gaps, and they persist longer than
-- binary gaps, so the dedicated table makes the daily report clean).
CREATE TABLE IF NOT EXISTS arb_multi (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts TEXT NOT NULL,
  event_id TEXT NOT NULL,
  event_slug TEXT,
  event_title TEXT,
  outcome_count INTEGER NOT NULL,
  side TEXT NOT NULL,                   -- YES | NO
  stake_notional REAL NOT NULL,         -- $10 default per set
  shares REAL,                          -- shares bought per leg (or NULL if unfillable)
  leg_fills_json TEXT NOT NULL,         -- [{market_id, vwap, price_filled, fee_per_share, ...}]
  total_cost REAL NOT NULL,             -- USD all-in incl. fees + slippage
  guaranteed_payout REAL NOT NULL,      -- USD payout if any one leg wins
  fees REAL NOT NULL,                   -- USD - total fees component
  net_gap_pct REAL NOT NULL,            -- (payout - cost) / cost, signed
  status TEXT NOT NULL,                 -- OPEN | observed_below_threshold | unfillable_leg | CLOSED | VOID
  resolved_at TEXT,
  realized_pnl REAL,
  end_date_iso TEXT
);

-- Copy-trading (strategy #4) tables. wallets / wallet_trades /
-- wallet_cursors are CACHE (rebuildable from data-api). roster +
-- scout_snapshots + copied_trades are LEDGER (the experiment record).
CREATE TABLE IF NOT EXISTS roster (
  wallet TEXT PRIMARY KEY,
  entered_at TEXT,
  exited_at TEXT,
  score REAL,
  rank INTEGER,
  status TEXT NOT NULL,                -- ACTIVE | EXITED
  hysteresis_state_json TEXT           -- {below_25_consec, above_10_consec}
);

CREATE TABLE IF NOT EXISTS scout_snapshots (
  date TEXT NOT NULL,
  wallet TEXT NOT NULL,
  rank INTEGER,
  score REAL,
  passed_filters INTEGER NOT NULL,
  exclusion_reason TEXT,
  metrics_json TEXT,
  PRIMARY KEY(date, wallet)
);

CREATE TABLE IF NOT EXISTS copied_trades (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  leader_wallet TEXT NOT NULL,
  market_id TEXT NOT NULL,             -- conditionId
  token_id TEXT NOT NULL,              -- asset
  side TEXT NOT NULL,                  -- BUY | SELL
  leader_price REAL NOT NULL,
  leader_size REAL NOT NULL,
  leader_ts INTEGER NOT NULL,
  detection_ts INTEGER NOT NULL,
  detection_delay_s INTEGER NOT NULL,
  our_price REAL,                      -- NULL when unfillable / skipped
  price_drift REAL,                    -- our_price - leader_price (signed)
  book_snapshot_json TEXT,
  stake REAL NOT NULL,
  shares REAL,
  status TEXT NOT NULL,                -- open | settled | unfillable | skipped_cap
  exit_reason TEXT,                    -- leader_exit | resolution | NULL
  our_pnl REAL,
  leader_pnl_equivalent REAL,
  closed_at TEXT,
  ts_opened TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_signals_market ON signals(market_id, strategy, ts);
CREATE INDEX IF NOT EXISTS idx_trades_status ON paper_trades(status, strategy);
CREATE INDEX IF NOT EXISTS idx_arb_positions_status ON arb_positions(status, strategy);
CREATE INDEX IF NOT EXISTS idx_arb_legs_position ON arb_legs(position_id);
CREATE INDEX IF NOT EXISTS idx_cv_positions_status ON cv_positions(status, strategy);
CREATE INDEX IF NOT EXISTS idx_cv_legs_position ON cv_legs(position_id);
CREATE INDEX IF NOT EXISTS idx_copied_trades_leader ON copied_trades(leader_wallet, status);
CREATE INDEX IF NOT EXISTS idx_copied_trades_status ON copied_trades(status);
CREATE INDEX IF NOT EXISTS idx_arb_multi_status ON arb_multi(status, event_id);
CREATE INDEX IF NOT EXISTS idx_bankroll_txn_strategy ON bankroll_transactions(strategy, ts);
CREATE INDEX IF NOT EXISTS idx_health_strategy_ts ON health_log(strategy, ts);
CREATE INDEX IF NOT EXISTS idx_sharpline_status ON sharpline_orders(status);
CREATE INDEX IF NOT EXISTS idx_logic_violations_pair ON logic_violations(pair_id, ts);
CREATE INDEX IF NOT EXISTS idx_wx_verify_city_date ON wx_verification(city, resolve_date);

-- v2.3 shadow_trades: WeatherModel v2 head-to-head book. Every market
-- the live weather strategy scans logs BOTH the champion model's and
-- the challenger model's evaluation here. Shadow trades simulate the
-- same Kelly/fill rules and get graded by the same fixed grader, but
-- NEVER touch the main bankroll. The daily report renders a
-- CHAMPION vs CHALLENGER comparison + a promotion verdict based on
-- shadow Brier + expectancy.
CREATE TABLE IF NOT EXISTS shadow_trades (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts TEXT NOT NULL,
  market_id INTEGER NOT NULL,
  city TEXT,
  resolve_date TEXT,
  -- CHAMPION model (existing weather strategy)
  champ_p REAL,
  champ_side TEXT,             -- YES / NO / NONE
  champ_edge REAL,
  champ_price_filled REAL,
  champ_stake REAL,
  champ_shares REAL,
  champ_pnl REAL,
  -- CHALLENGER model (WeatherModelV2)
  chal_p REAL,
  chal_side TEXT,
  chal_edge REAL,
  chal_price_filled REAL,
  chal_stake REAL,
  chal_shares REAL,
  chal_pnl REAL,
  outcome TEXT,                -- YES / NO / VOID after grading
  status TEXT NOT NULL DEFAULT 'OPEN',
  closed_at TEXT,
  UNIQUE(market_id)
);
CREATE INDEX IF NOT EXISTS idx_shadow_trades_status
  ON shadow_trades(status, resolve_date);
CREATE INDEX IF NOT EXISTS idx_shadow_trades_city
  ON shadow_trades(city, status);

-- v2.2 cv_state: tiny kv table for cross-cycle state the cv strategy
-- needs (Kalshi category round-robin pointer, etc). MUST live in
-- ledger.db (committed) so GitHub Actions runners pick up where the
-- previous run left off; cache.db is gitignored and would lose the
-- pointer between runs.
CREATE TABLE IF NOT EXISTS cv_state (
  key TEXT PRIMARY KEY,
  value TEXT
);

-- v2 CV-PROBE (quarantined research book). Probe positions paper-trade
-- FUZZY pairs to empirically measure how often non-identical referees
-- actually disagree. ENTIRELY separated from the main bankroll and
-- main scoreboard. These tables live in ledger.db (committed) so the
-- experiment record survives cache rebuilds; older builds put them in
-- CACHE_SCHEMA, and the _migrate_cv_probe_to_ledger step on init
-- moves any cache-resident rows over the first time this code runs.
CREATE TABLE IF NOT EXISTS cv_probe_positions (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts TEXT NOT NULL,
  pair_id INTEGER NOT NULL,
  category TEXT NOT NULL,
  match_confidence REAL NOT NULL,
  direction TEXT NOT NULL,
  shares REAL NOT NULL,
  total_cost REAL NOT NULL,
  expected_payout REAL NOT NULL,
  net_gap_per_share REAL NOT NULL,
  divergence_risk_note TEXT,
  status TEXT NOT NULL DEFAULT 'OPEN',
  agreement_outcome TEXT,              -- AGREED/DIVERGED/VOID_MISMATCH/BOTH_VOID
  divergence_direction TEXT,           -- BOTH_PAID / NEITHER_PAID / NULL
  pnl REAL,
  closed_at TEXT
);

CREATE TABLE IF NOT EXISTS cv_probe_legs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  position_id INTEGER NOT NULL,
  venue TEXT NOT NULL,
  venue_market_id TEXT NOT NULL,
  side TEXT NOT NULL,
  vwap REAL NOT NULL,
  price_filled REAL NOT NULL,
  fee_per_share REAL NOT NULL,
  shares REAL NOT NULL,
  cost REAL NOT NULL,
  outcome TEXT,                        -- YES/NO/VOID at the leg's own venue
  payout REAL,
  levels_consumed_json TEXT,
  FOREIGN KEY(position_id) REFERENCES cv_probe_positions(id)
);

CREATE INDEX IF NOT EXISTS idx_cv_probe_positions_status ON cv_probe_positions(status, ts);
CREATE INDEX IF NOT EXISTS idx_cv_probe_positions_pair ON cv_probe_positions(pair_id);
CREATE INDEX IF NOT EXISTS idx_cv_probe_legs_pos ON cv_probe_legs(position_id);
"""

# ============================================================ CACHE_SCHEMA
# Rebuildable raw data. Lives in cache.db (gitignored).
CACHE_SCHEMA = """
CREATE TABLE IF NOT EXISTS snapshots (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  market_id INTEGER NOT NULL,
  ts TEXT NOT NULL,
  yes_ask REAL, yes_bid REAL,
  no_ask REAL, no_bid REAL,
  book_depth_usd REAL
);

CREATE TABLE IF NOT EXISTS arb_gaps (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts TEXT NOT NULL,
  strategy TEXT NOT NULL,
  event_id TEXT NOT NULL,
  event_slug TEXT,
  event_title TEXT,
  n_legs INTEGER NOT NULL,
  completeness_verified INTEGER NOT NULL,
  completeness_note TEXT,
  side TEXT NOT NULL,
  walk_mode TEXT NOT NULL,
  target_shares REAL,
  executable_shares REAL,
  sum_vwap_per_share REAL,
  slippage_per_share REAL,
  safety_buffer REAL,
  payout_per_share REAL,
  locked_profit_per_share REAL,
  locked_profit_usd REAL,
  end_date_iso TEXT,
  legs_json TEXT,
  cleared_threshold INTEGER NOT NULL DEFAULT 0
);

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
  classification TEXT NOT NULL,
  reason TEXT,
  criteria_json TEXT,
  divergence_risk_note TEXT,
  category TEXT,                       -- v2: weather/sports/crypto/politics/economics
  confidence REAL,                     -- v2: 0..1; >=0.9 required for any action
  UNIQUE(poly_market_id, kalshi_ticker)
);

-- cv_probe_positions / cv_probe_legs moved to LEDGER_SCHEMA so the
-- experiment record survives cache rebuilds. The init step in
-- Ledger.__init__ copies any leftover rows from cache.db -> ledger.db
-- on the first run after this change, then drops the cache copies.

CREATE TABLE IF NOT EXISTS cv_gaps (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts TEXT NOT NULL,
  strategy TEXT NOT NULL,
  pair_id INTEGER NOT NULL,
  direction TEXT NOT NULL,
  classification TEXT NOT NULL,
  poly_vwap REAL,
  poly_fee REAL,
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
  legs_json TEXT
);

CREATE TABLE IF NOT EXISTS wallets (
  wallet TEXT PRIMARY KEY,
  first_seen TEXT NOT NULL,
  last_scouted TEXT,
  metrics_json TEXT
);

CREATE TABLE IF NOT EXISTS wallet_trades (
  wallet TEXT NOT NULL,
  trade_key TEXT NOT NULL,
  ts INTEGER NOT NULL,
  side TEXT NOT NULL,
  asset TEXT NOT NULL,
  condition_id TEXT,
  size REAL NOT NULL,
  price REAL NOT NULL,
  title TEXT,
  event_slug TEXT,
  outcome TEXT,
  outcome_index INTEGER,
  raw_json TEXT,
  PRIMARY KEY(wallet, trade_key)
);

CREATE TABLE IF NOT EXISTS wallet_cursors (
  wallet TEXT PRIMARY KEY,
  last_ts INTEGER NOT NULL,
  last_pulled_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS odds_cache (
  sport TEXT PRIMARY KEY,
  fetched_at TEXT NOT NULL,
  payload_json TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS lp_sim_state (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts TEXT NOT NULL,
  poly_market_id TEXT NOT NULL,
  quote_spread REAL NOT NULL,
  quote_size REAL NOT NULL,
  score REAL NOT NULL,
  est_share_of_pool REAL NOT NULL,
  est_daily_reward_usd REAL NOT NULL,
  est_trading_pnl_usd REAL NOT NULL,
  adverse_selection_usd REAL NOT NULL,
  estimate_marker TEXT NOT NULL DEFAULT 'ESTIMATE'
);

CREATE INDEX IF NOT EXISTS idx_snapshots_market ON snapshots(market_id, ts);
CREATE INDEX IF NOT EXISTS idx_arb_gaps_event ON arb_gaps(event_id, ts);
CREATE INDEX IF NOT EXISTS idx_cv_gaps_pair ON cv_gaps(pair_id, ts);
CREATE INDEX IF NOT EXISTS idx_wallet_trades_wallet ON wallet_trades(wallet, ts);
"""


def utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def ledger_from_cfg(cfg: dict) -> "Ledger":
    """Build a Ledger from a `config.yaml`-shaped dict. Honours the new
    `ledger_path` / `cache_path` keys; falls back to the legacy `path`
    (which gets a co-located `*.cache.db` sibling)."""
    d = (cfg.get("database") or {})
    ledger_path = d.get("ledger_path") or d.get("path") or "ledger.db"
    cache_path = d.get("cache_path")
    return Ledger(ledger_path, cache_path)


class Ledger:
    """Two-DB SQLite ledger. Every connection opens ledger.db and ATTACHes
    cache.db as schema name `cache`. All cache-table SQL is qualified
    with the `cache.` prefix.

    Pass `cache_path` explicitly. For tests, pass distinct temp files
    so each test gets an isolated pair. The legacy single-path signature
    `Ledger(path)` still works for back-compat: if only one path is
    given, cache.db is co-located alongside it with suffix '.cache.db'.
    """

    def __init__(self, ledger_path: str | Path,
                 cache_path: str | Path | None = None):
        self.ledger_path = str(ledger_path)
        if cache_path is None:
            # Back-compat default: co-located cache file. Used when
            # legacy callers haven't been updated yet.
            base = self.ledger_path.removesuffix(".db")
            self.cache_path = f"{base}.cache.db"
        else:
            self.cache_path = str(cache_path)
        # `db_path` is kept as an alias for the LEDGER path so existing
        # raw `sqlite3.connect(ledger.db_path)` callers keep working;
        # they only see the ledger tables until they upgrade to
        # `raw_connect()` which attaches the cache.
        self.db_path = self.ledger_path
        Path(self.ledger_path).parent.mkdir(parents=True, exist_ok=True)
        Path(self.cache_path).parent.mkdir(parents=True, exist_ok=True)
        # Create both files + their schemas via standalone connections.
        lc = sqlite3.connect(self.ledger_path)
        try:
            lc.executescript(LEDGER_SCHEMA)
            self._migrate(lc)
            lc.commit()
        finally:
            lc.close()
        cc = sqlite3.connect(self.cache_path)
        try:
            cc.executescript(CACHE_SCHEMA)
            self._migrate_cache(cc)
            cc.commit()
        finally:
            cc.close()
        # Cross-DB step: cv_probe tables moved from cache.db -> ledger.db
        # in v2.1. Copy any leftover rows now (no-op on fresh DBs and on
        # DBs that have already been migrated). Done AFTER both schemas
        # are in place because the helper opens self._conn() which
        # ATTACHes cache.db.
        try:
            self._migrate_cv_probe_to_ledger()
        except sqlite3.OperationalError:
            # If anything in the move fails (e.g. corrupt cache copy),
            # don't block startup -- the experiment record is preserved
            # in cache.db until a future run resolves the issue.
            pass

    @staticmethod
    def _migrate(c: sqlite3.Connection) -> None:
        """Forward-only column additions for ledger.db. SQLite has no
        ALTER TABLE ... IF NOT EXISTS, so check PRAGMA first."""
        for table, col, type_ in [
            ("settlements", "wu_value", "REAL"),
            ("settlements", "wu_source", "TEXT"),
            ("settlements", "om_value", "REAL"),
            # v2 cv_probe split: when agreement_outcome=DIVERGED, distinguish
            # BOTH_PAID (windfall) vs NEITHER_PAID (catastrophe). NULL for
            # AGREED/VOID_*/OPEN rows.
            ("cv_probe_positions", "divergence_direction", "TEXT"),
            # Forecast-change watcher: wall-clock minutes between the
            # last recorded hash flip for this trade's city and the
            # moment the trade was logged. NULL for non-weather strategies
            # and for weather trades opened before the watcher existed.
            ("paper_trades", "minutes_since_forecast_change", "REAL"),
            ("shadow_trades", "minutes_since_forecast_change", "REAL"),
        ]:
            cols = [r[1] for r in c.execute(
                f"PRAGMA table_info({table})").fetchall()]
            if col not in cols:
                c.execute(f"ALTER TABLE {table} ADD COLUMN {col} {type_}")

    @staticmethod
    def _migrate_cache(c: sqlite3.Connection) -> None:
        """Forward-only column additions for cache.db tables."""
        for table, col, type_ in [
            ("cv_pairs", "category", "TEXT"),
            ("cv_pairs", "confidence", "REAL"),
        ]:
            cols = [r[1] for r in c.execute(
                f"PRAGMA table_info({table})").fetchall()]
            if col not in cols:
                c.execute(f"ALTER TABLE {table} ADD COLUMN {col} {type_}")

    def _migrate_cv_probe_to_ledger(self) -> int:
        """Idempotent: if cache.db contains cv_probe_positions /
        cv_probe_legs rows (from the older build where those tables
        lived in CACHE_SCHEMA), copy them into ledger.db preserving the
        primary keys, then drop the cache copies. Returns the number
        of probe positions copied (0 in steady state).

          - Steady state: cache has no cv_probe tables -> no-op return 0.
          - Older build with rows: copy positions + legs preserving id,
            then DROP from cache. INSERT OR IGNORE keeps re-runs safe.
          - Older-still build whose cv_probe_positions lacks
            divergence_direction (pre v2.0 migration): we read the
            cache schema first and substitute NULL for missing columns
            so the SELECT never references a column that doesn't exist.
        """
        copied = 0
        with self._conn() as c:
            has_cache_tables = bool(c.execute(
                "SELECT 1 FROM cache.sqlite_master "
                "WHERE type='table' AND name='cv_probe_positions' LIMIT 1"
            ).fetchone())
            if not has_cache_tables:
                return 0
            cache_cols = {r[1] for r in c.execute(
                "PRAGMA cache.table_info(cv_probe_positions)").fetchall()}

            def _pick(col: str) -> str:
                return col if col in cache_cols else "NULL"

            pos_sql = f"""
                INSERT OR IGNORE INTO cv_probe_positions
                  (id, ts, pair_id, category, match_confidence, direction,
                   shares, total_cost, expected_payout, net_gap_per_share,
                   divergence_risk_note, status, agreement_outcome,
                   divergence_direction, pnl, closed_at)
                SELECT id, ts, pair_id, category, match_confidence, direction,
                       shares, total_cost, expected_payout, net_gap_per_share,
                       divergence_risk_note, status, agreement_outcome,
                       {_pick('divergence_direction')}, pnl, closed_at
                  FROM cache.cv_probe_positions
            """
            cur = c.execute(pos_sql)
            copied = cur.rowcount or 0
            c.execute("""
                INSERT OR IGNORE INTO cv_probe_legs
                  (id, position_id, venue, venue_market_id, side, vwap,
                   price_filled, fee_per_share, shares, cost,
                   outcome, payout, levels_consumed_json)
                SELECT id, position_id, venue, venue_market_id, side, vwap,
                       price_filled, fee_per_share, shares, cost,
                       outcome, payout, levels_consumed_json
                  FROM cache.cv_probe_legs
            """)
            c.execute("DROP TABLE IF EXISTS cache.cv_probe_legs")
            c.execute("DROP TABLE IF EXISTS cache.cv_probe_positions")
        return copied

    @contextmanager
    def _conn(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.ledger_path)
        conn.row_factory = sqlite3.Row
        # Attach cache.db so every query in this connection can reach
        # cache tables via the `cache.<table>` prefix.
        conn.execute("ATTACH DATABASE ? AS cache",
                      (self.cache_path,))
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    def raw_connect(self) -> sqlite3.Connection:
        """Unmanaged connection with cache.db attached. Used by main.py
        and report.py for ad-hoc cross-table reporting queries. Caller
        owns commit + close."""
        conn = sqlite3.connect(self.ledger_path)
        conn.row_factory = sqlite3.Row
        conn.execute("ATTACH DATABASE ? AS cache", (self.cache_path,))
        return conn

    def vacuum(self, vacuum_ledger: bool = True,
                 vacuum_cache: bool = True) -> dict[str, int]:
        """Run VACUUM on each DB and return resulting byte sizes. Used by
        the daily workflow's housekeeping step. VACUUM cannot run inside
        an attached transaction so each DB is opened standalone."""
        out: dict[str, int] = {}
        if vacuum_ledger:
            lc = sqlite3.connect(self.ledger_path)
            lc.isolation_level = None
            lc.execute("VACUUM")
            lc.close()
            out["ledger_bytes"] = os.path.getsize(self.ledger_path)
        if vacuum_cache:
            cc = sqlite3.connect(self.cache_path)
            cc.isolation_level = None
            cc.execute("VACUUM")
            cc.close()
            out["cache_bytes"] = os.path.getsize(self.cache_path)
        return out

    def prune_ledger(self, signals_keep_days: int = 7,
                     arb_multi_keep_days: int = 7) -> dict[str, int]:
        """Drop unbounded log rows from the committed ledger.

        Two tables grow without bound across cycles:
          - `signals`: one row per strategy x relevant market per cycle.
            The grader's wx-verification backfill + the master report
            only ever read `latest_signal(market_id, strategy)` -- older
            rows are dead history. Pruning by ts keeps the latest per
            market because we drop strictly older days.
          - `arb_multi`: one row per walked bucket-arb event per cycle.
            ~99% of rows are `status='observed_below_threshold'` (no
            position was ever opened) -- the recent-distribution log
            for these is ALREADY in cache.arb_gaps. We only prune the
            non-position statuses (observed_below_threshold + unfillable_leg)
            so any real OPEN/CLOSED position record is preserved
            regardless of age.

        Both retentions default to 7 days -- enough for any verifier or
        recent-distribution view, while keeping the committed ledger.db
        well under GitHub's per-file limits.
        """
        from datetime import timedelta as _td
        cutoff_signals = (datetime.now(timezone.utc).date()
                          - _td(days=int(signals_keep_days))).isoformat()
        cutoff_arb = (datetime.now(timezone.utc).date()
                      - _td(days=int(arb_multi_keep_days))).isoformat()
        out: dict[str, int] = {}
        with self._conn() as c:
            cur = c.execute(
                "DELETE FROM signals WHERE substr(ts,1,10) < ?",
                (cutoff_signals,))
            out["signals_deleted"] = cur.rowcount
            # Only prune non-position rows. status 'open'/'closed'/'VOID'
            # / etc are real bucket-arb positions and stay forever.
            cur = c.execute(
                "DELETE FROM arb_multi "
                " WHERE substr(ts,1,10) < ? "
                "   AND status IN ('observed_below_threshold','unfillable_leg')",
                (cutoff_arb,))
            out["arb_multi_deleted"] = cur.rowcount
        return out

    def prune_cache(self, snapshots_keep_days: int = 7,
                     gaps_keep_days: int = 7) -> dict[str, int]:
        """Drop cache rows older than the retention window. Daily.yml
        runs this before VACUUM to keep cache.db from growing without
        bound across workflow runs."""
        out: dict[str, int] = {}
        cutoff_ts_iso = (datetime.now(timezone.utc)
                          .replace(microsecond=0)
                          .isoformat()[:10])
        # naive lexicographic cut on ISO date string
        cut_day = (datetime.fromisoformat(cutoff_ts_iso)
                    - __import__("datetime").timedelta(days=int(snapshots_keep_days))
                  ).date().isoformat()
        cut_day_gaps = (datetime.fromisoformat(cutoff_ts_iso)
                          - __import__("datetime").timedelta(days=int(gaps_keep_days))
                        ).date().isoformat()
        with self._conn() as c:
            cur = c.execute(
                "DELETE FROM cache.snapshots WHERE substr(ts,1,10) < ?",
                (cut_day,))
            out["snapshots_deleted"] = cur.rowcount
            cur = c.execute(
                "DELETE FROM cache.arb_gaps WHERE substr(ts,1,10) < ?",
                (cut_day_gaps,))
            out["arb_gaps_deleted"] = cur.rowcount
            cur = c.execute(
                "DELETE FROM cache.cv_gaps WHERE substr(ts,1,10) < ?",
                (cut_day_gaps,))
            out["cv_gaps_deleted"] = cur.rowcount
        return out

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
                """INSERT INTO cache.snapshots (market_id, ts, yes_ask, yes_bid,
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
                     levels_consumed: list[dict],
                     minutes_since_forecast_change: float | None = None,
                     ) -> int:
        with self._conn() as c:
            cur = c.execute(
                """INSERT INTO paper_trades (market_id, strategy, ts, side,
                    price_filled, stake, shares, p_model_at_entry,
                    edge_at_entry, levels_consumed_json, status,
                    minutes_since_forecast_change)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'OPEN', ?)""",
                (market_id, strategy, utcnow_iso(), side, price_filled,
                 stake, shares, p_model_at_entry, edge_at_entry,
                 json.dumps(levels_consumed),
                 minutes_since_forecast_change),
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
                """INSERT INTO cache.arb_gaps (
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
                "SELECT id FROM cache.cv_pairs WHERE poly_market_id=? AND kalshi_ticker=?",
                (pair["poly_market_id"], pair["kalshi_ticker"]),
            ).fetchone()
            if row:
                c.execute(
                    """UPDATE cache.cv_pairs SET ts_last_classified=?, poly_title=?,
                        kalshi_title=?, poly_leg=?, kalshi_leg=?, poly_close=?,
                        kalshi_close=?, poly_source=?, kalshi_source=?, city=?,
                        date=?, classification=?, reason=?, criteria_json=?,
                        divergence_risk_note=?, category=?, confidence=?
                        WHERE id=?""",
                    (
                        utcnow_iso(), pair.get("poly_title"), pair.get("kalshi_title"),
                        pair.get("poly_leg"), pair.get("kalshi_leg"),
                        pair.get("poly_close"), pair.get("kalshi_close"),
                        pair.get("poly_source"), pair.get("kalshi_source"),
                        pair.get("city"), pair.get("date"),
                        pair["classification"], pair.get("reason"),
                        json.dumps(pair.get("criteria") or {}),
                        pair.get("divergence_risk_note") or "",
                        pair.get("category"),
                        float(pair["confidence"]) if pair.get("confidence") is not None else None,
                        int(row["id"]),
                    ),
                )
                return int(row["id"])
            cur = c.execute(
                """INSERT INTO cache.cv_pairs (
                    ts_first_seen, ts_last_classified, poly_market_id, kalshi_ticker,
                    poly_title, kalshi_title, poly_leg, kalshi_leg,
                    poly_close, kalshi_close, poly_source, kalshi_source,
                    city, date, classification, reason, criteria_json,
                    divergence_risk_note, category, confidence)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
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
                    pair.get("category"),
                    float(pair["confidence"]) if pair.get("confidence") is not None else None,
                ),
            )
            return int(cur.lastrowid)

    def record_cv_gap(self, gap: dict[str, Any]) -> int:
        with self._conn() as c:
            cur = c.execute(
                """INSERT INTO cache.cv_gaps (
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
                """SELECT classification, COUNT(*) as n FROM cache.cv_pairs
                   GROUP BY classification""").fetchall()
            d = {r["classification"]: int(r["n"]) for r in rows}
            divergence = c.execute(
                """SELECT COUNT(*) as n FROM cache.cv_pairs
                   WHERE divergence_risk_note IS NOT NULL
                     AND divergence_risk_note <> ''""").fetchone()
            d["with_divergence_risk_note"] = int(divergence["n"] or 0)
            return d

    def cv_gap_stats(self, strategy: str) -> dict[str, Any]:
        with self._conn() as c:
            rows = c.execute(
                """SELECT classification, direction, cleared_threshold,
                          locked_profit_per_share, divergence_risk_note
                     FROM cache.cv_gaps WHERE strategy=?""",
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

    # --- CV-PROBE (quarantined research book) ---------------------------
    def record_cv_probe_position(self, pos: dict[str, Any],
                                   legs: list[dict[str, Any]]) -> int:
        """Insert a new cv_probe position + its legs. The position writes
        to ledger.db (the experiment record persists across cache rebuilds)."""
        with self._conn() as c:
            cur = c.execute(
                """INSERT INTO cv_probe_positions (
                    ts, pair_id, category, match_confidence, direction,
                    shares, total_cost, expected_payout, net_gap_per_share,
                    divergence_risk_note, status)
                    VALUES (?,?,?,?,?,?,?,?,?,?, 'OPEN')""",
                (
                    utcnow_iso(), int(pos["pair_id"]),
                    pos["category"], float(pos["match_confidence"]),
                    pos["direction"], float(pos["shares"]),
                    float(pos["total_cost"]), float(pos["expected_payout"]),
                    float(pos["net_gap_per_share"]),
                    pos.get("divergence_risk_note") or "",
                ),
            )
            pid = int(cur.lastrowid)
            for leg in legs:
                c.execute(
                    """INSERT INTO cv_probe_legs (
                        position_id, venue, venue_market_id, side, vwap,
                        price_filled, fee_per_share, shares, cost,
                        levels_consumed_json)
                        VALUES (?,?,?,?,?,?,?,?,?,?)""",
                    (
                        pid, leg["venue"], leg["venue_market_id"], leg["side"],
                        float(leg["vwap"]), float(leg["price_filled"]),
                        float(leg.get("fee_per_share") or 0.0),
                        float(leg["shares"]), float(leg["cost"]),
                        json.dumps(leg.get("levels_consumed") or []),
                    ),
                )
            return pid

    def cv_probe_pair_has_open_or_settled(self, pair_id: int) -> bool:
        """DEDUPE rule: max one open or settled probe position per pair
        per resolution event. A pair that already has any non-VOIDED row
        is skipped (logged as 'already_probed')."""
        with self._conn() as c:
            row = c.execute(
                "SELECT 1 FROM cv_probe_positions WHERE pair_id=? "
                "AND status IN ('OPEN','SETTLED') LIMIT 1",
                (int(pair_id),)).fetchone()
            return row is not None

    def cv_probe_count_open(self) -> int:
        with self._conn() as c:
            row = c.execute(
                "SELECT COUNT(*) AS n FROM cv_probe_positions "
                "WHERE status='OPEN'").fetchone()
            return int(row["n"])

    def cv_probe_count_today(self, day_iso: str,
                              category: str | None = None) -> int:
        with self._conn() as c:
            if category:
                row = c.execute(
                    "SELECT COUNT(*) AS n FROM cv_probe_positions "
                    "WHERE substr(ts,1,10)=? AND category=?",
                    (day_iso, category)).fetchone()
            else:
                row = c.execute(
                    "SELECT COUNT(*) AS n FROM cv_probe_positions "
                    "WHERE substr(ts,1,10)=?",
                    (day_iso,)).fetchone()
            return int(row["n"])

    def list_open_cv_probe(self) -> list[sqlite3.Row]:
        with self._conn() as c:
            return list(c.execute(
                "SELECT * FROM cv_probe_positions WHERE status='OPEN'"
            ).fetchall())

    def cv_probe_legs_for(self, position_id: int) -> list[sqlite3.Row]:
        with self._conn() as c:
            return list(c.execute(
                "SELECT * FROM cv_probe_legs WHERE position_id=?",
                (int(position_id),)).fetchall())

    def close_cv_probe_position(self, position_id: int,
                                  agreement_outcome: str, pnl: float,
                                  leg_outcomes: list[dict[str, Any]],
                                  divergence_direction: str | None = None
                                  ) -> None:
        """Close a probe position. `divergence_direction` is BOTH_PAID or
        NEITHER_PAID when agreement_outcome=DIVERGED; None otherwise."""
        with self._conn() as c:
            c.execute(
                "UPDATE cv_probe_positions SET status='SETTLED', "
                "agreement_outcome=?, divergence_direction=?, pnl=?, "
                "closed_at=? WHERE id=?",
                (agreement_outcome, divergence_direction, float(pnl),
                 utcnow_iso(), int(position_id)),
            )
            for lo in leg_outcomes:
                c.execute(
                    "UPDATE cv_probe_legs SET outcome=?, payout=? WHERE id=?",
                    (lo["outcome"], float(lo["payout"]), int(lo["leg_id"])),
                )

    def cv_probe_settled_stats(self) -> list[sqlite3.Row]:
        """Per-category settled probe stats for the report. Groups by
        (category, agreement_outcome, divergence_direction) so DIVERGED
        rows split into the BOTH_PAID (windfall) and NEITHER_PAID
        (catastrophe) buckets. Older rows that pre-date the
        divergence_direction column have NULL there and aggregate as a
        single legacy bucket."""
        with self._conn() as c:
            return list(c.execute(
                """SELECT category, agreement_outcome,
                          COALESCE(divergence_direction, '') AS divergence_direction,
                          COUNT(*) AS n,
                          COALESCE(AVG(net_gap_per_share), 0.0) AS avg_gap,
                          COALESCE(AVG(pnl), 0.0) AS avg_pnl,
                          COALESCE(SUM(pnl), 0.0) AS sum_pnl
                   FROM cv_probe_positions
                   WHERE status='SETTLED'
                   GROUP BY category, agreement_outcome, divergence_direction
                   ORDER BY category, agreement_outcome, divergence_direction"""
            ).fetchall())

    # --- shadow_trades (WeatherModel v2 challenger book) -----------------
    def upsert_shadow_trade(self, row: dict[str, Any]) -> int:
        """Idempotent on market_id. Used by main.py cycle() weather phase
        to log BOTH the champion's and challenger's evaluation of each
        scanned market. The challenger never touches the main bankroll;
        this row is only used to drive the CHAMPION vs CHALLENGER table
        in the daily report and the promotion gate."""
        with self._conn() as c:
            existing = c.execute(
                "SELECT id FROM shadow_trades WHERE market_id=?",
                (int(row["market_id"]),)).fetchone()
            if existing:
                c.execute(
                    """UPDATE shadow_trades
                          SET ts=?, city=?, resolve_date=?,
                              champ_p=?, champ_side=?, champ_edge=?,
                              champ_price_filled=?, champ_stake=?, champ_shares=?,
                              chal_p=?, chal_side=?, chal_edge=?,
                              chal_price_filled=?, chal_stake=?, chal_shares=?,
                              minutes_since_forecast_change=?
                        WHERE id=?""",
                    (
                        row.get("ts") or utcnow_iso(),
                        row.get("city"),
                        row.get("resolve_date"),
                        row.get("champ_p"),
                        row.get("champ_side"),
                        row.get("champ_edge"),
                        row.get("champ_price_filled"),
                        row.get("champ_stake"),
                        row.get("champ_shares"),
                        row.get("chal_p"),
                        row.get("chal_side"),
                        row.get("chal_edge"),
                        row.get("chal_price_filled"),
                        row.get("chal_stake"),
                        row.get("chal_shares"),
                        row.get("minutes_since_forecast_change"),
                        int(existing["id"]),
                    ),
                )
                return int(existing["id"])
            cur = c.execute(
                """INSERT INTO shadow_trades (
                       ts, market_id, city, resolve_date,
                       champ_p, champ_side, champ_edge,
                       champ_price_filled, champ_stake, champ_shares,
                       chal_p, chal_side, chal_edge,
                       chal_price_filled, chal_stake, chal_shares,
                       status, minutes_since_forecast_change)
                   VALUES (?,?,?,?, ?,?,?, ?,?,?, ?,?,?, ?,?,?, 'OPEN', ?)""",
                (
                    row.get("ts") or utcnow_iso(),
                    int(row["market_id"]),
                    row.get("city"),
                    row.get("resolve_date"),
                    row.get("champ_p"),
                    row.get("champ_side"),
                    row.get("champ_edge"),
                    row.get("champ_price_filled"),
                    row.get("champ_stake"),
                    row.get("champ_shares"),
                    row.get("chal_p"),
                    row.get("chal_side"),
                    row.get("chal_edge"),
                    row.get("chal_price_filled"),
                    row.get("chal_stake"),
                    row.get("chal_shares"),
                    row.get("minutes_since_forecast_change"),
                ),
            )
            return int(cur.lastrowid)

    def list_open_shadow_trades(self) -> list[sqlite3.Row]:
        with self._conn() as c:
            return list(c.execute(
                "SELECT * FROM shadow_trades WHERE status='OPEN'"
            ).fetchall())

    def close_shadow_trade(self, shadow_id: int, outcome: str,
                              champ_pnl: float | None,
                              chal_pnl: float | None) -> None:
        with self._conn() as c:
            c.execute(
                """UPDATE shadow_trades
                      SET status='SETTLED', outcome=?,
                          champ_pnl=?, chal_pnl=?, closed_at=?
                    WHERE id=?""",
                (outcome,
                 None if champ_pnl is None else float(champ_pnl),
                 None if chal_pnl is None else float(chal_pnl),
                 utcnow_iso(), int(shadow_id)),
            )

    def shadow_stats_by_city(self) -> list[sqlite3.Row]:
        """Per-city aggregate of settled shadow trades. Champion vs
        challenger Brier + expectancy (avg pnl per shadowed market).
        Brier is computed using champ_p / chal_p as the YES probability
        regardless of which side the model would have traded -- the
        probability IS the calibration question, independent of the
        trade decision. Markets where the model declined to trade still
        contribute to Brier."""
        with self._conn() as c:
            return list(c.execute(
                """SELECT city,
                          COUNT(*) AS n,
                          SUM(CASE WHEN champ_side IS NOT NULL
                                    AND champ_side != 'NONE' THEN 1 ELSE 0 END) AS champ_n_trades,
                          SUM(CASE WHEN chal_side IS NOT NULL
                                    AND chal_side != 'NONE' THEN 1 ELSE 0 END) AS chal_n_trades,
                          AVG((champ_p - CASE WHEN outcome='YES' THEN 1.0 ELSE 0.0 END)
                              *(champ_p - CASE WHEN outcome='YES' THEN 1.0 ELSE 0.0 END)) AS champ_brier,
                          AVG((chal_p - CASE WHEN outcome='YES' THEN 1.0 ELSE 0.0 END)
                              *(chal_p - CASE WHEN outcome='YES' THEN 1.0 ELSE 0.0 END)) AS chal_brier,
                          AVG(COALESCE(champ_pnl, 0.0)) AS champ_expectancy,
                          AVG(COALESCE(chal_pnl, 0.0)) AS chal_expectancy,
                          SUM(COALESCE(champ_pnl, 0.0)) AS champ_total_pnl,
                          SUM(COALESCE(chal_pnl, 0.0)) AS chal_total_pnl
                     FROM shadow_trades
                    WHERE status='SETTLED' AND outcome IN ('YES','NO')
                    GROUP BY city
                    ORDER BY city"""
            ).fetchall())

    def shadow_overall_stats(self) -> sqlite3.Row | None:
        with self._conn() as c:
            return c.execute(
                """SELECT COUNT(*) AS n,
                          AVG((champ_p - CASE WHEN outcome='YES' THEN 1.0 ELSE 0.0 END)
                              *(champ_p - CASE WHEN outcome='YES' THEN 1.0 ELSE 0.0 END)) AS champ_brier,
                          AVG((chal_p - CASE WHEN outcome='YES' THEN 1.0 ELSE 0.0 END)
                              *(chal_p - CASE WHEN outcome='YES' THEN 1.0 ELSE 0.0 END)) AS chal_brier,
                          AVG(COALESCE(champ_pnl, 0.0)) AS champ_expectancy,
                          AVG(COALESCE(chal_pnl, 0.0)) AS chal_expectancy
                     FROM shadow_trades
                    WHERE status='SETTLED' AND outcome IN ('YES','NO')"""
            ).fetchone()

    # --- cv_state kv (cross-cycle persistence) ---------------------------
    def cv_state_get(self, key: str, default: str | None = None
                       ) -> str | None:
        """Read a value from the cv_state kv table. Returns `default`
        when the key has never been set."""
        with self._conn() as c:
            row = c.execute(
                "SELECT value FROM cv_state WHERE key=?", (key,)).fetchone()
            return row["value"] if row else default

    def cv_state_set(self, key: str, value: str) -> None:
        """Upsert a cv_state value. Used by the Kalshi category
        round-robin to persist the pointer across cycle runs."""
        with self._conn() as c:
            c.execute(
                "INSERT INTO cv_state (key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, value),
            )

    # --- copy-trading (strategy #4) ---------------------------------------
    def upsert_wallet(self, wallet: str, metrics: dict[str, Any]) -> None:
        with self._conn() as c:
            row = c.execute("SELECT wallet FROM cache.wallets WHERE wallet=?",
                             (wallet,)).fetchone()
            if row:
                c.execute(
                    "UPDATE cache.wallets SET last_scouted=?, metrics_json=? WHERE wallet=?",
                    (utcnow_iso(), json.dumps(metrics), wallet))
            else:
                c.execute(
                    """INSERT INTO cache.wallets (wallet, first_seen, last_scouted, metrics_json)
                        VALUES (?, ?, ?, ?)""",
                    (wallet, utcnow_iso(), utcnow_iso(), json.dumps(metrics)))

    def upsert_wallet_trades(self, wallet: str, trades: list[dict]) -> int:
        if not trades:
            return 0
        n = 0
        with self._conn() as c:
            for t in trades:
                key = f"{t.get('transactionHash','')}:{t.get('asset','')}:{t.get('side','')}"
                try:
                    c.execute(
                        """INSERT OR IGNORE INTO cache.wallet_trades (
                            wallet, trade_key, ts, side, asset, condition_id,
                            size, price, title, event_slug, outcome,
                            outcome_index, raw_json)
                            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                        (
                            wallet, key, int(t.get("timestamp") or 0),
                            (t.get("side") or "").upper(),
                            t.get("asset") or "", t.get("conditionId"),
                            float(t.get("size") or 0.0),
                            float(t.get("price") or 0.0),
                            t.get("title"), t.get("eventSlug"),
                            t.get("outcome"),
                            int(t["outcomeIndex"]) if t.get("outcomeIndex") is not None else None,
                            json.dumps(t),
                        ),
                    )
                    n += c.total_changes
                except sqlite3.Error:
                    pass
        return n

    def get_wallet_trades(self, wallet: str) -> list[dict]:
        with self._conn() as c:
            rows = c.execute(
                "SELECT raw_json FROM cache.wallet_trades WHERE wallet=? ORDER BY ts",
                (wallet,)).fetchall()
        out: list[dict] = []
        for r in rows:
            try:
                out.append(json.loads(r["raw_json"]))
            except (TypeError, json.JSONDecodeError):
                pass
        return out

    def get_wallet_cursor(self, wallet: str) -> int | None:
        with self._conn() as c:
            row = c.execute(
                "SELECT last_ts FROM cache.wallet_cursors WHERE wallet=?",
                (wallet,)).fetchone()
            return int(row["last_ts"]) if row else None

    def set_wallet_cursor(self, wallet: str, last_ts: int) -> None:
        with self._conn() as c:
            c.execute(
                """INSERT INTO cache.wallet_cursors (wallet, last_ts, last_pulled_at)
                    VALUES (?, ?, ?)
                    ON CONFLICT(wallet) DO UPDATE SET last_ts=excluded.last_ts,
                        last_pulled_at=excluded.last_pulled_at""",
                (wallet, int(last_ts), utcnow_iso()))

    def list_wallets_top_by_realized(self, limit: int = 20) -> list[dict]:
        """Top wallets by realized P&L. Reads scout metrics_json when
        available (fast path); when the wallets table is empty (fresh
        cache, no scout run yet), falls back to enumerating distinct
        wallets in wallet_trades and computing realized P&L per wallet
        from the cached trade rows. The fallback is slower but means
        `autopsy-top` produces useful output from whatever cache state
        the operator has."""
        with self._conn() as c:
            rows = c.execute(
                "SELECT wallet, metrics_json FROM cache.wallets "
                "WHERE metrics_json IS NOT NULL").fetchall()
        out = []
        for r in rows:
            try:
                m = json.loads(r["metrics_json"] or "{}")
            except (TypeError, json.JSONDecodeError):
                continue
            out.append({
                "wallet": r["wallet"],
                "realized_pnl_usd": float(m.get("realized_pnl_usd") or 0.0),
                "n_trades": int(m.get("n_trades") or 0),
            })
        if out:
            out.sort(key=lambda x: x["realized_pnl_usd"], reverse=True)
            return out[:limit]
        # Fallback: derive from wallet_trades directly.
        from strategies.copy_trading import compute_metrics
        with self._conn() as c:
            wallets = [r["wallet"] for r in c.execute(
                "SELECT DISTINCT wallet FROM cache.wallet_trades").fetchall()]
        for w in wallets:
            trades = self.get_wallet_trades(w)
            if not trades:
                continue
            m = compute_metrics(trades)
            out.append({
                "wallet": w,
                "realized_pnl_usd": float(m.get("realized_pnl_usd") or 0.0),
                "n_trades": int(m.get("n_trades") or 0),
            })
        out.sort(key=lambda x: x["realized_pnl_usd"], reverse=True)
        return out[:limit]

    def list_roster(self) -> list[sqlite3.Row]:
        with self._conn() as c:
            return list(c.execute("SELECT * FROM roster").fetchall())

    def get_roster_state(self, wallet: str) -> dict:
        with self._conn() as c:
            row = c.execute(
                "SELECT hysteresis_state_json FROM roster WHERE wallet=?",
                (wallet,)).fetchone()
            if not row:
                return {}
            try:
                return json.loads(row["hysteresis_state_json"] or "{}")
            except (TypeError, json.JSONDecodeError):
                return {}

    def upsert_roster(self, wallet: str, entered_at: str | None,
                       exited_at: str | None, score: float | None,
                       rank: int | None, status: str,
                       hysteresis: dict) -> None:
        with self._conn() as c:
            row = c.execute("SELECT wallet FROM roster WHERE wallet=?",
                             (wallet,)).fetchone()
            if row:
                c.execute(
                    """UPDATE roster SET entered_at=?, exited_at=?, score=?,
                        rank=?, status=?, hysteresis_state_json=? WHERE wallet=?""",
                    (entered_at, exited_at, score, rank, status,
                     json.dumps(hysteresis), wallet))
            else:
                c.execute(
                    """INSERT INTO roster (wallet, entered_at, exited_at,
                        score, rank, status, hysteresis_state_json)
                        VALUES (?, ?, ?, ?, ?, ?, ?)""",
                    (wallet, entered_at, exited_at, score, rank, status,
                     json.dumps(hysteresis)))

    def upsert_scout_snapshot(self, date_iso: str, wallet: str,
                                rank: int | None, score: float | None,
                                passed: bool, reason: str | None,
                                metrics: dict) -> None:
        with self._conn() as c:
            c.execute(
                """INSERT INTO scout_snapshots (date, wallet, rank, score,
                    passed_filters, exclusion_reason, metrics_json)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(date, wallet) DO UPDATE SET
                    rank=excluded.rank, score=excluded.score,
                    passed_filters=excluded.passed_filters,
                    exclusion_reason=excluded.exclusion_reason,
                    metrics_json=excluded.metrics_json""",
                (date_iso, wallet, rank, score,
                 1 if passed else 0, reason, json.dumps(metrics)))

    def record_copied_trade(self, c: dict[str, Any]) -> int:
        with self._conn() as con:
            cur = con.execute(
                """INSERT INTO copied_trades (
                    leader_wallet, market_id, token_id, side, leader_price,
                    leader_size, leader_ts, detection_ts, detection_delay_s,
                    our_price, price_drift, book_snapshot_json, stake,
                    shares, status, ts_opened)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    c["leader_wallet"], c["market_id"], c["token_id"],
                    c["side"], float(c["leader_price"]),
                    float(c["leader_size"]), int(c["leader_ts"]),
                    int(c["detection_ts"]), int(c["detection_delay_s"]),
                    c.get("our_price"), c.get("price_drift"),
                    c.get("book_snapshot_json"),
                    float(c["stake"]),
                    c.get("shares"),
                    c["status"],
                    utcnow_iso(),
                ),
            )
            return int(cur.lastrowid)

    def count_open_copies(self, leader: str | None = None) -> int:
        with self._conn() as c:
            if leader:
                row = c.execute(
                    """SELECT COUNT(*) AS n FROM copied_trades
                       WHERE leader_wallet=? AND status='open'""",
                    (leader,)).fetchone()
            else:
                row = c.execute(
                    "SELECT COUNT(*) AS n FROM copied_trades WHERE status='open'"
                ).fetchone()
            return int(row["n"])

    def list_open_copies(self) -> list[sqlite3.Row]:
        with self._conn() as c:
            return list(c.execute(
                "SELECT * FROM copied_trades WHERE status='open'").fetchall())

    def settle_copied_trade(self, trade_id: int, our_pnl: float,
                              leader_pnl_equivalent: float,
                              exit_reason: str) -> None:
        with self._conn() as c:
            c.execute(
                """UPDATE copied_trades SET status='settled', our_pnl=?,
                    leader_pnl_equivalent=?, exit_reason=?, closed_at=?
                    WHERE id=?""",
                (our_pnl, leader_pnl_equivalent, exit_reason,
                 utcnow_iso(), int(trade_id)))

    def copy_stats(self) -> dict[str, Any]:
        with self._conn() as c:
            rows = list(c.execute(
                """SELECT status, COUNT(*) as n FROM copied_trades GROUP BY status"""
            ).fetchall())
            settled = list(c.execute(
                """SELECT leader_wallet, detection_delay_s, price_drift,
                          our_pnl, leader_pnl_equivalent
                     FROM copied_trades WHERE status='settled'""").fetchall())
        per_leader: dict[str, dict[str, float]] = {}
        for s in settled:
            w = s["leader_wallet"]
            d = per_leader.setdefault(w, {
                "n": 0, "our_pnl": 0.0, "leader_pnl": 0.0,
                "delay_sum": 0.0, "drift_sum": 0.0})
            d["n"] += 1
            d["our_pnl"] += float(s["our_pnl"] or 0)
            d["leader_pnl"] += float(s["leader_pnl_equivalent"] or 0)
            d["delay_sum"] += float(s["detection_delay_s"] or 0)
            d["drift_sum"] += float(s["price_drift"] or 0)
        return {
            "by_status": {r["status"]: int(r["n"]) for r in rows},
            "settled": len(settled),
            "per_leader": per_leader,
        }

    # --- signals lookup helpers used by the wx verifier -------------------
    def latest_signal(self, market_row_id: int, strategy: str
                        ) -> sqlite3.Row | None:
        with self._conn() as c:
            return c.execute(
                """SELECT * FROM signals WHERE market_id=? AND strategy=?
                   ORDER BY id DESC LIMIT 1""",
                (int(market_row_id), strategy)).fetchone()

    def latest_snapshot(self, market_row_id: int) -> sqlite3.Row | None:
        with self._conn() as c:
            return c.execute(
                """SELECT * FROM cache.snapshots WHERE market_id=?
                   ORDER BY id DESC LIMIT 1""",
                (int(market_row_id),)).fetchone()

    # --- wx_verification (weather skill telemetry) ------------------------
    def upsert_wx_verification(self, row: dict[str, Any]) -> int:
        """Idempotent on market_row_id. Re-running grade safely refreshes
        the row with newer values (e.g., when WU/OM updates after a partial
        early settlement)."""
        with self._conn() as c:
            existing = c.execute(
                "SELECT id FROM wx_verification WHERE market_row_id=?",
                (int(row["market_row_id"]),)).fetchone()
            cols = (
                "ts_logged, market_row_id, city, station, threshold, unit, "
                "bound, resolve_date, lead_time_hours, official_value, "
                "official_value_unit, om_value, om_value_unit, wu_value, "
                "wu_value_unit, gfs_mean, gfs_spread, gfs_p_threshold, "
                "gfs_signed_error, gfs_abs_error, ecmwf_mean, ecmwf_spread, "
                "ecmwf_p_threshold, ecmwf_signed_error, ecmwf_abs_error, "
                "p_blended, market_price, outcome, signal_id, settlement_id"
            )
            vals = (
                utcnow_iso(), int(row["market_row_id"]), row.get("city"),
                row.get("station"), row.get("threshold"), row.get("unit"),
                row.get("bound"), row.get("resolve_date"),
                row.get("lead_time_hours"),
                row.get("official_value"), row.get("official_value_unit"),
                row.get("om_value"), row.get("om_value_unit"),
                row.get("wu_value"), row.get("wu_value_unit"),
                row.get("gfs_mean"), row.get("gfs_spread"),
                row.get("gfs_p_threshold"), row.get("gfs_signed_error"),
                row.get("gfs_abs_error"),
                row.get("ecmwf_mean"), row.get("ecmwf_spread"),
                row.get("ecmwf_p_threshold"), row.get("ecmwf_signed_error"),
                row.get("ecmwf_abs_error"),
                row.get("p_blended"), row.get("market_price"),
                row.get("outcome"), row.get("signal_id"),
                row.get("settlement_id"),
            )
            if existing:
                # UPDATE in column order.
                col_list = cols.split(", ")
                set_clause = ", ".join(f"{cl}=?" for cl in col_list[1:])
                c.execute(f"UPDATE wx_verification SET {set_clause} WHERE id=?",
                           (*vals[1:], int(existing["id"])))
                return int(existing["id"])
            placeholders = ", ".join("?" for _ in cols.split(", "))
            cur = c.execute(
                f"INSERT INTO wx_verification ({cols}) VALUES ({placeholders})",
                vals,
            )
            return int(cur.lastrowid)

    def list_wx_verifications(self, city: str | None = None,
                                since_date_iso: str | None = None,
                                ) -> list[sqlite3.Row]:
        sql = "SELECT * FROM wx_verification WHERE 1=1"
        args: list[Any] = []
        if city:
            sql += " AND city=?"
            args.append(city)
        if since_date_iso:
            sql += " AND resolve_date >= ?"
            args.append(since_date_iso)
        sql += " ORDER BY resolve_date"
        with self._conn() as c:
            return list(c.execute(sql, args).fetchall())

    # --- odds-api (Prompt C, Phase 1: SHARPLINE) --------------------------
    def odds_api_requests_this_month(self, month: str) -> int:
        with self._conn() as c:
            row = c.execute(
                "SELECT COUNT(*) AS n FROM odds_api_log WHERE month=?",
                (month,)).fetchone()
            return int(row["n"])

    def record_odds_api_request(self, month: str, sport: str,
                                  status_code: int) -> None:
        with self._conn() as c:
            c.execute(
                """INSERT INTO odds_api_log (ts, month, sport, status_code)
                    VALUES (?, ?, ?, ?)""",
                (utcnow_iso(), month, sport, int(status_code)))

    def get_odds_cache(self, sport: str, ttl_seconds: int = 1800
                         ) -> list[dict] | None:
        with self._conn() as c:
            row = c.execute(
                "SELECT fetched_at, payload_json FROM cache.odds_cache WHERE sport=?",
                (sport,)).fetchone()
        if not row:
            return None
        try:
            dt = datetime.fromisoformat(row["fetched_at"].replace("Z", "+00:00"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            age = (datetime.now(timezone.utc) - dt).total_seconds()
            if age > ttl_seconds:
                return None
            return json.loads(row["payload_json"])
        except (ValueError, TypeError, json.JSONDecodeError):
            return None

    def put_odds_cache(self, sport: str, payload: list[dict]) -> None:
        with self._conn() as c:
            c.execute(
                """INSERT INTO cache.odds_cache (sport, fetched_at, payload_json)
                    VALUES (?, ?, ?)
                    ON CONFLICT(sport) DO UPDATE SET
                    fetched_at=excluded.fetched_at,
                    payload_json=excluded.payload_json""",
                (sport, utcnow_iso(), json.dumps(payload)))

    def record_sharpline_match(self, match: dict[str, Any]) -> int:
        with self._conn() as c:
            cur = c.execute(
                """INSERT INTO sharpline_matches (
                    ts, sport_key, poly_market_id, poly_event_slug,
                    bookmaker_event_id, home_team, away_team, confidence,
                    status) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (utcnow_iso(), match["sport_key"], match.get("poly_market_id"),
                 match.get("poly_event_slug"), match.get("bookmaker_event_id"),
                 match.get("home_team"), match.get("away_team"),
                 float(match["confidence"]), match["status"]))
            return int(cur.lastrowid)

    def record_sharpline_order(self, order: dict[str, Any]) -> int:
        with self._conn() as c:
            cur = c.execute(
                """INSERT INTO sharpline_orders (
                    ts, match_id, poly_market_id, side, outcome, our_price,
                    fair_prob_at_post, edge_at_post, stake_usd, league, status)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (utcnow_iso(), int(order["match_id"]),
                 order["poly_market_id"], order["side"], order.get("outcome"),
                 float(order["our_price"]),
                 float(order["fair_prob_at_post"]),
                 float(order["edge_at_post"]),
                 float(order["stake_usd"]),
                 order.get("league"), order["status"]))
            return int(cur.lastrowid)

    def list_sharpline_orders(self, status: str | None = None) -> list[sqlite3.Row]:
        with self._conn() as c:
            if status:
                return list(c.execute(
                    "SELECT * FROM sharpline_orders WHERE status=?",
                    (status,)).fetchall())
            return list(c.execute(
                "SELECT * FROM sharpline_orders").fetchall())

    def update_sharpline_order(self, order_id: int, **fields) -> None:
        if not fields:
            return
        cols = ", ".join(f"{k}=?" for k in fields.keys())
        with self._conn() as c:
            c.execute(f"UPDATE sharpline_orders SET {cols} WHERE id=?",
                       (*fields.values(), int(order_id)))

    # --- LP-SIM (Prompt C, Phase 2) ---------------------------------------
    def record_lp_sim(self, row: dict[str, Any]) -> int:
        with self._conn() as c:
            cur = c.execute(
                """INSERT INTO cache.lp_sim_state (
                    ts, poly_market_id, quote_spread, quote_size, score,
                    est_share_of_pool, est_daily_reward_usd,
                    est_trading_pnl_usd, adverse_selection_usd)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (utcnow_iso(), row["poly_market_id"],
                 float(row["quote_spread"]), float(row["quote_size"]),
                 float(row["score"]),
                 float(row["est_share_of_pool"]),
                 float(row["est_daily_reward_usd"]),
                 float(row["est_trading_pnl_usd"]),
                 float(row["adverse_selection_usd"])))
            return int(cur.lastrowid)

    def lp_sim_latest(self, limit: int = 10) -> list[sqlite3.Row]:
        with self._conn() as c:
            return list(c.execute(
                "SELECT * FROM cache.lp_sim_state ORDER BY id DESC LIMIT ?",
                (limit,)).fetchall())

    # --- LOGIC-SCAN (Prompt C, Phase 3) -----------------------------------
    def upsert_logic_pair(self, pair: dict[str, Any]) -> int:
        with self._conn() as c:
            row = c.execute(
                """SELECT id FROM logic_pairs
                   WHERE market_a_id=? AND market_b_id=?""",
                (pair["market_a_id"], pair["market_b_id"])).fetchone()
            if row:
                return int(row["id"])
            cur = c.execute(
                """INSERT INTO logic_pairs (
                    event_id, market_a_id, market_b_id, template,
                    confidence, first_seen, notes)
                    VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (pair["event_id"], pair["market_a_id"], pair["market_b_id"],
                 pair["template"], float(pair["confidence"]),
                 utcnow_iso(), pair.get("notes")))
            return int(cur.lastrowid)

    def list_logic_pairs(self, min_confidence: float | None = None
                           ) -> list[sqlite3.Row]:
        with self._conn() as c:
            if min_confidence is not None:
                return list(c.execute(
                    "SELECT * FROM logic_pairs WHERE confidence >= ?",
                    (float(min_confidence),)).fetchall())
            return list(c.execute("SELECT * FROM logic_pairs").fetchall())

    def record_logic_violation(self, row: dict[str, Any]) -> int:
        with self._conn() as c:
            cur = c.execute(
                """INSERT INTO logic_violations (
                    ts, pair_id, pa, pb, margin, status, stake_usd)
                    VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (utcnow_iso(), int(row["pair_id"]),
                 float(row["pa"]), float(row["pb"]),
                 float(row["margin"]), row["status"],
                 row.get("stake_usd")))
            return int(cur.lastrowid)

    def logic_violation_stats(self) -> dict[str, int]:
        with self._conn() as c:
            rows = c.execute(
                """SELECT status, COUNT(*) AS n FROM logic_violations GROUP BY status"""
            ).fetchall()
            return {r["status"]: int(r["n"]) for r in rows}

    # --- bankroll (Prompt B) -----------------------------------------------
    def get_bankroll_row(self, strategy: str) -> sqlite3.Row | None:
        with self._conn() as c:
            return c.execute(
                "SELECT * FROM bankroll_allocations WHERE strategy=?",
                (strategy,)).fetchone()

    def upsert_bankroll_row(self, strategy: str, pct: float, starting: float,
                              cash: float, exposure: float) -> None:
        with self._conn() as c:
            c.execute(
                """INSERT INTO bankroll_allocations (
                    strategy, pct, starting_alloc_usd,
                    current_cash_usd, open_exposure_usd, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?)
                    ON CONFLICT(strategy) DO UPDATE SET
                    pct=excluded.pct,
                    starting_alloc_usd=excluded.starting_alloc_usd,
                    current_cash_usd=excluded.current_cash_usd,
                    open_exposure_usd=excluded.open_exposure_usd,
                    updated_at=excluded.updated_at""",
                (strategy, float(pct), float(starting), float(cash),
                 float(exposure), utcnow_iso()))

    def list_bankroll_rows(self) -> list[sqlite3.Row]:
        with self._conn() as c:
            return list(c.execute(
                "SELECT * FROM bankroll_allocations ORDER BY strategy"
            ).fetchall())

    def record_bankroll_txn(self, strategy: str, kind: str, amount: float,
                              related_table: str | None,
                              related_id: int | None,
                              cash_after: float, exposure_after: float,
                              note: str | None = None) -> int:
        with self._conn() as c:
            cur = c.execute(
                """INSERT INTO bankroll_transactions (
                    ts, strategy, kind, amount_usd, related_table,
                    related_id, cash_after_usd, exposure_after_usd, note)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (utcnow_iso(), strategy, kind, float(amount),
                 related_table, related_id, float(cash_after),
                 float(exposure_after), note))
            return int(cur.lastrowid)

    def list_bankroll_txns(self, strategy: str | None = None
                             ) -> list[sqlite3.Row]:
        with self._conn() as c:
            if strategy:
                return list(c.execute(
                    "SELECT * FROM bankroll_transactions WHERE strategy=? ORDER BY id",
                    (strategy,)).fetchall())
            return list(c.execute(
                "SELECT * FROM bankroll_transactions ORDER BY id"
            ).fetchall())

    def record_equity_point(self, date_iso: str, strategy: str,
                              cash: float, exposure: float,
                              realized_pnl_today: float) -> None:
        with self._conn() as c:
            c.execute(
                """INSERT INTO equity_history (
                    date, strategy, cash_usd, open_exposure_usd, realized_pnl_today)
                    VALUES (?, ?, ?, ?, ?)
                    ON CONFLICT(date, strategy) DO UPDATE SET
                    cash_usd=excluded.cash_usd,
                    open_exposure_usd=excluded.open_exposure_usd,
                    realized_pnl_today=excluded.realized_pnl_today""",
                (date_iso, strategy, float(cash), float(exposure),
                 float(realized_pnl_today)))

    def equity_history(self, strategy: str | None = None) -> list[sqlite3.Row]:
        with self._conn() as c:
            if strategy:
                return list(c.execute(
                    "SELECT * FROM equity_history WHERE strategy=? ORDER BY date",
                    (strategy,)).fetchall())
            return list(c.execute(
                "SELECT * FROM equity_history ORDER BY date, strategy"
            ).fetchall())

    # --- health -----------------------------------------------------------
    def record_health(self, strategy: str, ok: bool, duration_s: float,
                        markets_scanned: int, fills: int,
                        error_text: str | None, extras: dict) -> int:
        with self._conn() as c:
            cur = c.execute(
                """INSERT INTO health_log (
                    ts, strategy, ok, duration_s, markets_scanned, fills,
                    error_text, extras_json)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (utcnow_iso(), strategy, 1 if ok else 0,
                 float(duration_s), int(markets_scanned), int(fills),
                 error_text, json.dumps(extras or {})))
            return int(cur.lastrowid)

    def latest_health_per_strategy(self) -> list[sqlite3.Row]:
        with self._conn() as c:
            return list(c.execute(
                """SELECT strategy, ts, ok, duration_s, markets_scanned,
                          fills, error_text
                   FROM health_log h1
                   WHERE id = (SELECT MAX(id) FROM health_log h2
                                WHERE h2.strategy = h1.strategy)"""
            ).fetchall())

    def cv_pair_traded_today(self, pair_id: int, strategy: str,
                              direction: str, day_iso: str) -> bool:
        with self._conn() as c:
            row = c.execute(
                """SELECT 1 FROM cv_positions
                   WHERE pair_id=? AND strategy=? AND direction=?
                     AND substr(ts,1,10)=?""",
                (pair_id, strategy, direction, day_iso)).fetchone()
            return row is not None

    # --- multi-outcome arb (Prompt A) -------------------------------------
    def record_arb_multi(self, row: dict[str, Any]) -> int:
        with self._conn() as c:
            cur = c.execute(
                """INSERT INTO arb_multi (
                    ts, event_id, event_slug, event_title, outcome_count,
                    side, stake_notional, shares, leg_fills_json,
                    total_cost, guaranteed_payout, fees, net_gap_pct,
                    status, end_date_iso)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    utcnow_iso(), row["event_id"], row.get("event_slug"),
                    row.get("event_title"), int(row["outcome_count"]),
                    row["side"], float(row["stake_notional"]),
                    row.get("shares"),
                    json.dumps(row.get("leg_fills") or []),
                    float(row["total_cost"]),
                    float(row["guaranteed_payout"]),
                    float(row["fees"]),
                    float(row["net_gap_pct"]),
                    row["status"],
                    row.get("end_date_iso"),
                ))
            return int(cur.lastrowid)

    def open_arb_multis(self) -> list[sqlite3.Row]:
        with self._conn() as c:
            return list(c.execute(
                "SELECT * FROM arb_multi WHERE status='OPEN'").fetchall())

    def count_open_arb_multis(self) -> int:
        with self._conn() as c:
            row = c.execute(
                "SELECT COUNT(*) AS n FROM arb_multi WHERE status='OPEN'"
            ).fetchone()
            return int(row["n"])

    def already_arb_multi_today(self, event_id: str, side: str,
                                 day_iso: str) -> bool:
        with self._conn() as c:
            row = c.execute(
                """SELECT 1 FROM arb_multi
                   WHERE event_id=? AND side=? AND status='OPEN'
                     AND substr(ts,1,10)=?""",
                (event_id, side, day_iso)).fetchone()
            return row is not None

    def settle_arb_multi(self, row_id: int, status: str, realized_pnl: float
                           ) -> None:
        with self._conn() as c:
            c.execute(
                """UPDATE arb_multi SET status=?, realized_pnl=?, resolved_at=?
                    WHERE id=?""",
                (status, realized_pnl, utcnow_iso(), int(row_id)))

    def arb_multi_stats(self) -> dict[str, Any]:
        with self._conn() as c:
            by_status = list(c.execute(
                "SELECT status, COUNT(*) AS n, COALESCE(SUM(realized_pnl),0) AS pnl FROM arb_multi GROUP BY status"
            ).fetchall())
            dist = list(c.execute(
                "SELECT net_gap_pct FROM arb_multi"
            ).fetchall())
        out = {
            "by_status": {r["status"]: int(r["n"]) for r in by_status},
            "realized_pnl_by_status": {r["status"]: float(r["pnl"]) for r in by_status},
            "gap_pct_buckets": {},
        }
        for r in dist:
            v = r["net_gap_pct"]
            for hi in (-0.10, -0.05, -0.02, -0.01, 0.0, 0.005, 0.01, 0.02, 0.05, 0.10, 1.0):
                if v < hi:
                    out["gap_pct_buckets"][hi] = out["gap_pct_buckets"].get(hi, 0) + 1
                    break
        return out

    def arb_gap_stats(self, strategy: str) -> dict[str, Any]:
        """Aggregate distribution of detected gaps for the diagnostics print.

        profit_buckets is split by walk_mode so full_book (true fillable
        profit) and gamma_only (snapshot estimate) read separately.
        """
        with self._conn() as c:
            rows = c.execute(
                """SELECT walk_mode, side, completeness_verified,
                          locked_profit_per_share, cleared_threshold
                     FROM cache.arb_gaps WHERE strategy=?""",
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
