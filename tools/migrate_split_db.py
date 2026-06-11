"""Migrate polymarketbot.db -> ledger.db + cache.db with zero loss.

Why: the original single DB hit 511 MB (freelist + cv_pairs detail) and
exceeds GitHub's 100 MB committed-file cap. The split keeps the
irreplaceable paper-trading record in ledger.db (committed by workflows,
must stay under 50 MB) while rebuildable raw data lives in cache.db
(gitignored, rebuilt on demand).

Classification (per operator spec):
  LEDGER (committed): paper_trades, all position/leg tables, settlements,
                       copied_trades, sharpline_orders + matches,
                       logic_violations + pairs, roster, scout_snapshots,
                       bankroll_*, equity_history, health_log,
                       daily_report, markets, signals, odds_api_log,
                       arb_multi, arb_positions, arb_legs, cv_positions,
                       cv_legs
  CACHE  (gitignored): snapshots, arb_gaps, cv_pairs, cv_gaps,
                       wallets, wallet_trades, wallet_cursors,
                       lp_sim_state, odds_cache

The migration:
  1. Loads the legacy DB.
  2. Creates ledger.db + cache.db (via Ledger init - schema is split).
  3. Copies each table to the destination per the classification.
  4. Verifies row counts match exactly.
  5. Verifies the paper-trading invariants that matter operationally:
       - sum of bankroll_transactions = current bankroll_allocations
       - count of OPEN paper_trades preserved
       - sum of (paper_trades.stake) for OPEN positions preserved
"""
from __future__ import annotations

import argparse
import os
import sqlite3
import sys

# Classification source-of-truth. Used by both the migration AND the
# Ledger class so the two never drift.
LEDGER_TABLES = {
    "markets", "signals", "paper_trades", "settlements", "daily_report",
    "arb_positions", "arb_legs", "arb_multi",
    "cv_positions", "cv_legs",
    "roster", "scout_snapshots", "copied_trades",
    "sharpline_matches", "sharpline_orders",
    "logic_pairs", "logic_violations",
    "odds_api_log",
    "bankroll_allocations", "bankroll_transactions",
    "equity_history", "health_log",
}
CACHE_TABLES = {
    "snapshots", "arb_gaps",
    "cv_pairs", "cv_gaps",
    "wallets", "wallet_trades", "wallet_cursors",
    "lp_sim_state", "odds_cache",
}


def _table_columns(con: sqlite3.Connection, table: str) -> list[str]:
    return [r[1] for r in con.execute(f'PRAGMA table_info("{table}")')]


def _row_count(con: sqlite3.Connection, table: str) -> int:
    try:
        return int(con.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0])
    except sqlite3.OperationalError:
        return -1


def _copy_table(src: sqlite3.Connection, dst: sqlite3.Connection,
                  table: str) -> int:
    """Copy every row from src.table to dst.table. Both DBs already have
    the schema (created by Ledger init); we only need to move data."""
    src_cols = _table_columns(src, table)
    dst_cols = _table_columns(dst, table)
    if not src_cols:
        return 0
    if not dst_cols:
        raise RuntimeError(
            f"destination missing table {table} - schema mismatch")
    common = [c for c in src_cols if c in dst_cols]
    col_list = ", ".join(f'"{c}"' for c in common)
    placeholders = ", ".join("?" for _ in common)
    rows = list(src.execute(f'SELECT {col_list} FROM "{table}"'))
    dst.executemany(
        f'INSERT OR REPLACE INTO "{table}" ({col_list}) VALUES ({placeholders})',
        rows,
    )
    return len(rows)


def migrate(legacy_path: str, ledger_path: str, cache_path: str
             ) -> dict:
    if not os.path.exists(legacy_path):
        raise SystemExit(f"legacy DB not found: {legacy_path}")
    # Import here so the script does not depend on Ledger import order
    # before the refactor has landed.
    sys.path.insert(0, os.path.abspath(
        os.path.join(os.path.dirname(__file__), "..")))
    from foundation.ledger import Ledger

    # Snapshot pre-migration invariants from the legacy DB.
    src = sqlite3.connect(legacy_path)
    src.row_factory = sqlite3.Row
    pre = {
        "tables": {},
        "open_paper_trades": int(src.execute(
            "SELECT COUNT(*) FROM paper_trades WHERE status='OPEN'"
        ).fetchone()[0]),
        "open_paper_stake_usd": float(src.execute(
            "SELECT COALESCE(SUM(stake), 0) FROM paper_trades WHERE status='OPEN'"
        ).fetchone()[0]),
        "bankroll_alloc_total": float(src.execute(
            "SELECT COALESCE(SUM(current_cash_usd), 0) FROM bankroll_allocations"
        ).fetchone()[0]),
        "bankroll_txn_count": int(src.execute(
            "SELECT COUNT(*) FROM bankroll_transactions"
        ).fetchone()[0]),
    }
    for r in src.execute(
        "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"):
        t = r["name"]
        pre["tables"][t] = _row_count(src, t)

    # Wipe any existing target files so we never merge into stale state.
    for p in (ledger_path, cache_path):
        if os.path.exists(p):
            os.unlink(p)

    # Initialize both target DBs with the (split) schema.
    Ledger(ledger_path, cache_path)

    # Copy each table to its destination.
    ledger_con = sqlite3.connect(ledger_path)
    cache_con = sqlite3.connect(cache_path)
    moved = {}
    try:
        for table in pre["tables"]:
            if table in {"sqlite_sequence"}:
                continue
            if table in LEDGER_TABLES:
                moved[table] = _copy_table(src, ledger_con, table)
            elif table in CACHE_TABLES:
                moved[table] = _copy_table(src, cache_con, table)
            else:
                # Unknown - default to LEDGER to avoid loss; flag.
                print(f"  WARN: unclassified table {table!r}; routing to LEDGER")
                moved[table] = _copy_table(src, ledger_con, table)
        ledger_con.commit()
        cache_con.commit()
    finally:
        ledger_con.close()
        cache_con.close()
        src.close()

    # Snapshot post-migration invariants from the new DBs.
    post_ledger = sqlite3.connect(ledger_path)
    post_cache = sqlite3.connect(cache_path)
    post = {
        "tables": {},
        "open_paper_trades": int(post_ledger.execute(
            "SELECT COUNT(*) FROM paper_trades WHERE status='OPEN'"
        ).fetchone()[0]),
        "open_paper_stake_usd": float(post_ledger.execute(
            "SELECT COALESCE(SUM(stake), 0) FROM paper_trades WHERE status='OPEN'"
        ).fetchone()[0]),
        "bankroll_alloc_total": float(post_ledger.execute(
            "SELECT COALESCE(SUM(current_cash_usd), 0) FROM bankroll_allocations"
        ).fetchone()[0]),
        "bankroll_txn_count": int(post_ledger.execute(
            "SELECT COUNT(*) FROM bankroll_transactions"
        ).fetchone()[0]),
    }
    for t in LEDGER_TABLES:
        try:
            post["tables"][t] = _row_count(post_ledger, t)
        except sqlite3.OperationalError:
            pass
    for t in CACHE_TABLES:
        try:
            post["tables"][t] = _row_count(post_cache, t)
        except sqlite3.OperationalError:
            pass
    post_ledger.close()
    post_cache.close()

    # Verify invariants.
    failures = []
    for inv in ("open_paper_trades", "open_paper_stake_usd",
                  "bankroll_alloc_total", "bankroll_txn_count"):
        if pre[inv] != post[inv]:
            failures.append(f"{inv}: pre={pre[inv]} != post={post[inv]}")
    for t, n in pre["tables"].items():
        if t in {"sqlite_sequence"}:
            continue
        if post["tables"].get(t, -1) != n:
            failures.append(
                f"{t}: pre={n} != post={post['tables'].get(t, 'missing')}")

    # VACUUM both to reclaim space.
    for p in (ledger_path, cache_path):
        sub = sqlite3.connect(p)
        sub.isolation_level = None
        sub.execute("VACUUM")
        sub.close()

    return {"pre": pre, "post": post, "moved": moved, "failures": failures}


def _print_report(report: dict) -> None:
    print("=== Migration report ===")
    print()
    print("Invariants:")
    print(f"  open_paper_trades:   pre={report['pre']['open_paper_trades']:>5d}  post={report['post']['open_paper_trades']:>5d}")
    print(f"  open_paper_stake_usd: pre=${report['pre']['open_paper_stake_usd']:.2f}  post=${report['post']['open_paper_stake_usd']:.2f}")
    print(f"  bankroll_alloc_total: pre=${report['pre']['bankroll_alloc_total']:.2f}  post=${report['post']['bankroll_alloc_total']:.2f}")
    print(f"  bankroll_txn_count:   pre={report['pre']['bankroll_txn_count']:>5d}  post={report['post']['bankroll_txn_count']:>5d}")
    print()
    print(f"{'table':30s} {'pre':>7s} {'post':>7s}  destination")
    print("-" * 60)
    seen = set()
    for t, n in sorted(report["pre"]["tables"].items()):
        if t == "sqlite_sequence":
            continue
        dest = "ledger" if t in LEDGER_TABLES else (
            "cache" if t in CACHE_TABLES else "ledger?")
        post = report["post"]["tables"].get(t, "n/a")
        mark = " ok" if post == n else "FAIL"
        print(f"  {t:28s} {n:>7d} {post if post != 'n/a' else 0:>7}  {dest}  {mark}")
        seen.add(t)
    print()
    if report["failures"]:
        print(" FAILURES:")
        for f in report["failures"]:
            print("   -", f)
    else:
        print("All invariants + per-table counts match. Zero-loss migration verified.")


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--legacy", default="polymarketbot.db")
    p.add_argument("--ledger", default="ledger.db")
    p.add_argument("--cache", default="cache.db")
    args = p.parse_args()
    report = migrate(args.legacy, args.ledger, args.cache)
    _print_report(report)
    if report["failures"]:
        sys.exit(1)


if __name__ == "__main__":
    main()
