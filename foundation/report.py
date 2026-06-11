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
    print(f"=== PolyMarketBotV2 daily report  ({datetime.now(timezone.utc).isoformat(timespec='seconds')}) ===")
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


def print_master_report(cfg_path: str = "config.yaml") -> None:
    """V2 master report (Prompt B). Top section: health banner + per-strategy
    scoreboard. Below: each strategy's detailed section."""
    from foundation.bankroll import Bankroll
    from foundation.health import banner as health_banner
    cfg = _cfg(cfg_path)
    ledger = Ledger(cfg["database"]["path"])
    today = datetime.now(timezone.utc).date().isoformat()

    print(f"=== PolyMarketBotV2 master report ({datetime.now(timezone.utc).isoformat(timespec='seconds')}) ===")
    # ---- Health banner --------------------------------------------------
    stale_cfg = (cfg.get("health") or {}).get("stale_after_hours") or {}
    print(health_banner(ledger, stale_after_hours=stale_cfg))

    # ---- Per-strategy scoreboard ---------------------------------------
    snapshot = Bankroll(cfg, ledger).snapshot()
    total_starting = sum(float(r["starting_alloc_usd"]) for r in snapshot) or 0.0
    total_cash = sum(float(r["current_cash_usd"]) for r in snapshot) or 0.0
    total_exposure = sum(float(r["open_exposure_usd"]) for r in snapshot) or 0.0
    total_value = total_cash + 0.0   # marked at cost; positions still in exposure
    print()
    print(f"Total bankroll start=${total_starting:.2f} cash=${total_cash:.2f} "
          f"open_exposure=${total_exposure:.2f}")

    verdict_cfg = cfg.get("verdict") or {}
    min_settled = int(verdict_cfg.get("min_settled_for_pass", 20))
    fail_pct = float(verdict_cfg.get("fail_loss_pct", 0.10))

    print()
    print(f"{'strategy':18s} {'pct':>5s} {'alloc':>8s} {'cash':>8s} "
          f"{'open':>8s} {'settled':>7s} {'realized':>10s} {'verdict':>10s}")
    print("-" * 86)
    settled_map = _settled_per_strategy(ledger)
    for r in snapshot:
        s = r["strategy"]
        sd = settled_map.get(s, {"n": 0, "wins": 0, "pnl": 0.0, "deployed": 0.0})
        pct = float(r["pct"])
        cash = float(r["current_cash_usd"])
        exp = float(r["open_exposure_usd"])
        verdict = _verdict(sd, min_settled, fail_pct,
                            starting_alloc=float(r["starting_alloc_usd"]))
        print(f"  {s:16s} {pct*100:>4.1f}% ${float(r['starting_alloc_usd']):>7.2f} "
              f"${cash:>7.2f} ${exp:>7.2f} {sd['n']:>7d} ${sd['pnl']:>+9.2f} "
              f"{verdict:>10s}")

    # ---- Strategy detail sections --------------------------------------
    print()
    print_report(cfg_path)

    # ---- Today equity snapshot (if not already there) ------------------
    for r in snapshot:
        s = r["strategy"]
        sd = settled_map.get(s, {"pnl": 0.0})
        ledger.record_equity_point(
            today, s, float(r["current_cash_usd"]),
            float(r["open_exposure_usd"]),
            float(sd["pnl"]),
        )


def _settled_per_strategy(ledger: Ledger) -> dict[str, dict]:
    """Aggregate realized P&L across paper_trades + arb_positions + arb_multi
    + cv_positions + copied_trades for the scoreboard."""
    import sqlite3
    out: dict[str, dict] = {}

    def _bump(s: str, n: int = 0, wins: int = 0, pnl: float = 0.0, deployed: float = 0.0):
        d = out.setdefault(s, {"n": 0, "wins": 0, "pnl": 0.0, "deployed": 0.0})
        d["n"] += n; d["wins"] += wins
        d["pnl"] += pnl; d["deployed"] += deployed

    with sqlite3.connect(ledger.db_path) as c:
        c.row_factory = sqlite3.Row
        for row in c.execute(
            """SELECT strategy, status, COUNT(*) n, COALESCE(SUM(pnl),0) p,
                      COALESCE(SUM(stake),0) d FROM paper_trades
               WHERE status IN ('WIN','LOSS','VOID') GROUP BY strategy, status"""
        ).fetchall():
            _bump(row["strategy"], n=int(row["n"]),
                   wins=int(row["n"]) if row["status"] == "WIN" else 0,
                   pnl=float(row["p"]), deployed=float(row["d"]))
        for row in c.execute(
            """SELECT strategy, status, COUNT(*) n, COALESCE(SUM(pnl),0) p,
                      COALESCE(SUM(total_cost),0) d FROM arb_positions
               WHERE status IN ('CLOSED','VOID') GROUP BY strategy, status"""
        ).fetchall():
            _bump(row["strategy"], n=int(row["n"]),
                   wins=int(row["n"]) if row["status"] == "CLOSED" and float(row["p"]) > 0 else 0,
                   pnl=float(row["p"]), deployed=float(row["d"]))
        for row in c.execute(
            """SELECT 'bucket_arb' AS strategy, status, COUNT(*) n,
                      COALESCE(SUM(realized_pnl),0) p,
                      COALESCE(SUM(total_cost),0) d FROM arb_multi
               WHERE status IN ('CLOSED','VOID') GROUP BY status"""
        ).fetchall():
            _bump("bucket_arb", n=int(row["n"]),
                   wins=int(row["n"]) if row["status"] == "CLOSED" and float(row["p"]) > 0 else 0,
                   pnl=float(row["p"]), deployed=float(row["d"]))
        for row in c.execute(
            """SELECT strategy, status, COUNT(*) n, COALESCE(SUM(pnl),0) p,
                      COALESCE(SUM(total_cost),0) d FROM cv_positions
               WHERE status IN ('CLOSED','VOID','DIVERGED') GROUP BY strategy, status"""
        ).fetchall():
            _bump(row["strategy"], n=int(row["n"]),
                   wins=int(row["n"]) if row["status"] == "CLOSED" and float(row["p"]) > 0 else 0,
                   pnl=float(row["p"]), deployed=float(row["d"]))
        for row in c.execute(
            """SELECT 'copy_trading' AS strategy, COUNT(*) n,
                      COALESCE(SUM(our_pnl),0) p,
                      COALESCE(SUM(stake),0) d FROM copied_trades
               WHERE status='settled'"""
        ).fetchall():
            wins = c.execute(
                "SELECT COUNT(*) n FROM copied_trades WHERE status='settled' AND our_pnl > 0"
            ).fetchone()
            _bump("copy_trading", n=int(row["n"]), wins=int(wins["n"]),
                   pnl=float(row["p"]), deployed=float(row["d"]))
    return out


def _verdict(sd: dict, min_settled: int, fail_pct: float,
              starting_alloc: float) -> str:
    if sd["n"] < min_settled:
        return "TOO EARLY"
    if sd["pnl"] > 0:
        return "AHEAD"
    if abs(sd["pnl"]) > fail_pct * starting_alloc:
        return "BEHIND"
    return "FLAT"


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
