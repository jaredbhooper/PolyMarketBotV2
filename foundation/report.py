"""Reporter: console summary. Optional Telegram hook left as a stub."""
from __future__ import annotations

import json
import os
import sqlite3
from datetime import datetime, timezone

import yaml

from foundation.ledger import Ledger


def _cfg(path: str = "config.yaml") -> dict:
    return yaml.safe_load(open(path, "r", encoding="utf-8"))


def print_report(cfg_path: str = "config.yaml") -> None:
    cfg = _cfg(cfg_path)
    ledger = Ledger(cfg["database"]["path"])
    starting = float(cfg.get("paper", {}).get("starting_bankroll", 1000.0))
    print(f"=== PolyMarketBotV1 daily report  ({datetime.now(timezone.utc).isoformat(timespec='seconds')}) ===")
    print(f"Starting bankroll per strategy: ${starting:.2f}")
    print()

    with sqlite3.connect(ledger.db_path) as c:
        c.row_factory = sqlite3.Row
        strats = [r["strategy"] for r in c.execute(
            "SELECT DISTINCT strategy FROM paper_trades"
        ).fetchall()]
        if not strats:
            strats = [(e["module"] if isinstance(e, dict) else e)
                      for e in cfg.get("active_strategies", [])]
        for s in strats:
            print(f"--- {s} ---")
            closed = list(c.execute(
                """SELECT side, status, pnl, p_model_at_entry FROM paper_trades
                   WHERE strategy=? AND status IN ('WIN','LOSS','VOID')""",
                (s,)).fetchall())
            opn = list(c.execute(
                """SELECT id, market_id, side, price_filled, stake, shares,
                          p_model_at_entry, edge_at_entry, ts
                   FROM paper_trades WHERE strategy=? AND status='OPEN'""",
                (s,)).fetchall())
            n = len(closed)
            wins = sum(1 for t in closed if t["status"] == "WIN")
            pnl = sum(float(t["pnl"] or 0) for t in closed)
            bankroll = ledger.bankroll(s, starting)
            br = c.execute(
                "SELECT brier FROM daily_report WHERE strategy=? ORDER BY date DESC LIMIT 1",
                (s,)).fetchone()
            brier = br["brier"] if br else None
            print(f"  closed: {n}  wins: {wins}  P&L: ${pnl:+.2f}  "
                  f"bankroll: ${bankroll:.2f}  brier: "
                  f"{brier if brier is None else f'{brier:.4f}'}")
            if opn:
                print(f"  open positions: {len(opn)}")
                for t in opn:
                    mk = c.execute("SELECT slug, question FROM markets WHERE id=?",
                                    (t["market_id"],)).fetchone()
                    slug = (mk["slug"] if mk else "")[:50]
                    print(f"    #{t['id']} {t['side']} @ {t['price_filled']:.4f} "
                          f"stake ${t['stake']:.2f} ({t['shares']:.1f} shares) "
                          f"p_model={t['p_model_at_entry']:.3f} "
                          f"edge={t['edge_at_entry']:.3f}  {slug}")
            disputes = list(c.execute(
                """SELECT s.om_value, s.wu_value, s.wu_source,
                          m.slug, m.unit, m.resolve_date
                   FROM settlements s JOIN markets m ON m.id = s.market_id
                   WHERE s.outcome = 'DISPUTED'
                     AND s.market_id IN (
                       SELECT market_id FROM paper_trades
                       WHERE strategy = ? AND status = 'OPEN'
                     )""", (s,)).fetchall())
            if disputes:
                print(f"  DISPUTED settlements: {len(disputes)} (trades stay OPEN)")
                for d in disputes:
                    u = d["unit"] or "?"
                    ov = f"{d['om_value']:.2f}" if d["om_value"] is not None else "n/a"
                    wv = f"{d['wu_value']:.2f}" if d["wu_value"] is not None else "n/a"
                    print(f"    {d['slug']} ({d['resolve_date']}): "
                          f"OM={ov}{u} vs WU={wv}{u}  -> {d['wu_source']}")
            print()


def print_status(cfg_path: str = "config.yaml") -> None:
    cfg = _cfg(cfg_path)
    ledger = Ledger(cfg["database"]["path"])
    starting = float(cfg.get("paper", {}).get("starting_bankroll", 1000.0))
    with sqlite3.connect(ledger.db_path) as c:
        c.row_factory = sqlite3.Row
        for s in [r["strategy"] for r in c.execute(
            "SELECT DISTINCT strategy FROM paper_trades").fetchall()]:
            print(f"[{s}] bankroll=${ledger.bankroll(s, starting):.2f}  "
                  f"open=${ledger.open_stake(s):.2f}")


def telegram_send(text: str) -> bool:
    """Optional alerting. Set TELEGRAM_BOT_TOKEN + TELEGRAM_CHAT_ID env vars
    (in Actions secrets) to enable. Silent no-op otherwise."""
    tok = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat = os.environ.get("TELEGRAM_CHAT_ID")
    if not tok or not chat:
        return False
    try:
        import requests
        r = requests.post(
            f"https://api.telegram.org/bot{tok}/sendMessage",
            json={"chat_id": chat, "text": text}, timeout=15,
        )
        return r.status_code == 200
    except Exception:
        return False
