"""Reporter: console summary. Optional Telegram hook left as a stub."""
from __future__ import annotations

import json
import os
import sqlite3
from datetime import datetime, timezone

import yaml

from foundation.ledger import Ledger, ledger_from_cfg


def _cfg(path: str = "config.yaml") -> dict:
    return yaml.safe_load(open(path, "r", encoding="utf-8"))


def print_report(cfg_path: str = "config.yaml") -> None:
    cfg = _cfg(cfg_path)
    ledger = ledger_from_cfg(cfg)
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
    ledger = ledger_from_cfg(cfg)
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

    # ---- WX-VERIFY section (v2.1) ---------------------------------------
    # Always rendered; degrades gracefully when there's no data yet.
    try:
        from main import _print_wx_verify
        print()
        _print_wx_verify(cfg, ledger)
    except Exception as exc:
        print(f"  WX-VERIFY render failed: {exc}")

    # ---- Copy-trading bot-likeness section ------------------------------
    try:
        print()
        _print_copy_bot_likeness(cfg, ledger)
    except Exception as exc:
        print(f"  COPY bot_likeness render failed: {exc}")

    # ---- CV-PROBE (research book) section --------------------------------
    # Quarantined experiment - NOT part of the main bankroll scoreboard.
    try:
        print()
        print_cv_probe_report(cfg, ledger)
    except Exception as exc:
        print(f"  CV-PROBE render failed: {exc}")

    # ---- WeatherModel CHAMPION vs CHALLENGER section --------------------
    # Shadow book; never touches the main bankroll. Promotion gate
    # is config-driven (default OFF).
    try:
        print()
        print_weather_v2_shadow(cfg, ledger)
    except Exception as exc:
        print(f"  WEATHER v2 shadow render failed: {exc}")

    # ---- EDGE FRESHNESS section (v2.4) ---------------------------------
    # Expectancy + hit rate bucketed by minutes since the last forecast
    # change. Probes whether the fast-cycle change watcher actually
    # locates higher-edge entry windows.
    try:
        print()
        print_edge_freshness(cfg, ledger)
    except Exception as exc:
        print(f"  EDGE FRESHNESS render failed: {exc}")

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
    ledger = ledger_from_cfg(cfg)
    starting = float(cfg.get("paper", {}).get("starting_bankroll", 1000.0))
    with sqlite3.connect(ledger.db_path) as c:
        c.row_factory = sqlite3.Row
        for s in [r["strategy"] for r in c.execute(
            "SELECT DISTINCT strategy FROM paper_trades").fetchall()]:
            print(f"[{s}] bankroll=${ledger.bankroll(s, starting):.2f}  "
                  f"open=${ledger.open_stake(s):.2f}")


def _print_copy_bot_likeness(cfg: dict, ledger: Ledger) -> None:
    """Active roster + settled copy P&L bucketed by leader bot_likeness.
    Score is informational only (NOT a hard filter)."""
    import sqlite3
    print("=== COPY bot-likeness ===")
    with sqlite3.connect(ledger.db_path) as c:
        c.row_factory = sqlite3.Row
        c.execute("ATTACH DATABASE ? AS cache", (ledger.cache_path,))
        # active roster + per-wallet bot_likeness from cached metrics.
        rows = list(c.execute(
            "SELECT wallet, score, rank, status FROM roster "
            "WHERE status='ACTIVE' ORDER BY rank").fetchall())
        if not rows:
            print("  no ACTIVE roster wallets")
        else:
            wallets = [r["wallet"] for r in rows]
            placeholders = ",".join("?" * len(wallets))
            metric_rows = c.execute(
                f"SELECT wallet, metrics_json FROM cache.wallets "
                f"WHERE wallet IN ({placeholders})", wallets).fetchall()
            bl_map: dict[str, float] = {}
            for mr in metric_rows:
                try:
                    md = json.loads(mr["metrics_json"] or "{}")
                except (TypeError, json.JSONDecodeError):
                    md = {}
                bl_map[mr["wallet"]] = float(md.get("bot_likeness") or 0.0)
            print(f"  {'wallet':44s} {'rank':>4s} {'score':>7s} {'bot_lik':>8s}")
            print(f"  {'-'*44} {'-'*4} {'-'*7} {'-'*8}")
            for r in rows:
                bl = bl_map.get(r["wallet"], 0.0)
                rank = r["rank"] if r["rank"] is not None else 0
                sc = r["score"] if r["score"] is not None else 0.0
                print(f"  {r['wallet']:44s} {rank:>4d} {sc:>7.3f} {bl:>8.3f}")

        # settled copy P&L + latency tax bucketed by leader bot_likeness.
        print()
        print("  --- settled P&L by leader bot_likeness ---")
        settled = list(c.execute(
            """SELECT leader_wallet, our_pnl,
                      COALESCE(leader_pnl_equivalent, 0.0) AS lpe
               FROM copied_trades WHERE status='settled'""").fetchall())
        if not settled:
            print("    (no settled copy trades yet)")
            print()
            return
        # leader -> bot_likeness from cache.wallets.metrics_json
        leaders = list({r["leader_wallet"] for r in settled})
        placeholders = ",".join("?" * len(leaders))
        leader_bl: dict[str, float] = {}
        for mr in c.execute(
            f"SELECT wallet, metrics_json FROM cache.wallets "
            f"WHERE wallet IN ({placeholders})", leaders).fetchall():
            try:
                md = json.loads(mr["metrics_json"] or "{}")
            except (TypeError, json.JSONDecodeError):
                md = {}
            leader_bl[mr["wallet"]] = float(md.get("bot_likeness") or 0.0)
        buckets = {"low (<0.33)": {"n": 0, "pnl": 0.0, "tax": 0.0},
                   "mid (0.33-0.66)": {"n": 0, "pnl": 0.0, "tax": 0.0},
                   "high (>=0.66)": {"n": 0, "pnl": 0.0, "tax": 0.0}}
        for r in settled:
            bl = leader_bl.get(r["leader_wallet"], 0.0)
            key = ("low (<0.33)" if bl < 0.33
                    else "mid (0.33-0.66)" if bl < 0.66
                    else "high (>=0.66)")
            buckets[key]["n"] += 1
            buckets[key]["pnl"] += float(r["our_pnl"] or 0.0)
            buckets[key]["tax"] += float(r["our_pnl"] or 0.0) - float(r["lpe"] or 0.0)
        print(f"    {'bucket':20s} {'n':>5s} {'copy_pnl':>10s} {'latency_tax':>12s}")
        print(f"    {'-'*20} {'-'*5} {'-'*10} {'-'*12}")
        for k, v in buckets.items():
            print(f"    {k:20s} {v['n']:>5d} ${v['pnl']:>+9.2f} ${v['tax']:>+11.2f}")
    print()


def print_cv_probe_report(cfg: dict, ledger: Ledger) -> None:
    """CV-PROBE (research) section. Quarantined from main bankroll.

    Per category + overall:
      - pairs OPEN / SETTLED (incl. by agreement_outcome)
      - average net gap captured on AGREED pairs (probe revenue)
      - average loss on DIVERGED pairs (probe risk)
      - average P&L on VOID_MISMATCH pairs
      - total P&L
      - breakeven divergence rate implied by the average gap:
          breakeven = gap / (gap + loss_per_divergence)
        (When you lose `gap+1` on divergence and earn `gap` on agreement,
        breakeven divergence rate is gap / (gap + (1 + gap)) ~= gap / (1+2*gap).
        We use the empirical avg loss per diverged pair from the data
        instead of the theoretical $1+gap so the verdict reflects reality.)
      - VERDICT line: divergence% vs breakeven% -> POSITIVE/NEGATIVE EV.

    Cap snapshot at the top: open positions, lifetime opened, daily caps.
    """
    pcfg = (cfg.get("strategies") or {}).get("cv_probe") or {}
    print("=== CV-PROBE (quarantined research book) ===")
    print(f"  capital pool: ${float(pcfg.get('probe_capital', 500.0)):.0f} virtual "
          f"(NOT part of main bankroll)")
    print(f"  stake per pair: ${float(pcfg.get('probe_stake_usd', 5.0)):.2f}  |  "
          f"min gap to open: ${float(pcfg.get('min_probe_gap', 0.02)):.2f}  |  "
          f"min match conf: {float(pcfg.get('min_match_confidence', 0.9)):.2f}")
    print(f"  daily caps: {int(pcfg.get('max_probe_per_day', 40))} total/day, "
          f"{int(pcfg.get('max_probe_per_day_per_category', 15))} per-category/day, "
          f"{int(pcfg.get('max_probe_total', 1000))} lifetime")
    today = datetime.now(timezone.utc).date().isoformat()
    open_n = ledger.cv_probe_count_open()
    today_n = ledger.cv_probe_count_today(today)
    print(f"  positions: {open_n} OPEN  |  {today_n} opened today")

    rows = ledger.cv_probe_settled_stats()
    if not rows:
        print()
        print("  (no settled probe positions yet)")
        return

    # Pivot rows by category. Each settled-stats row is
    # (category, agreement_outcome, divergence_direction, n, avg_gap,
    # avg_pnl, sum_pnl). The DIVERGED rows split by divergence_direction
    # (BOTH_PAID vs NEITHER_PAID); other agreement_outcomes have
    # divergence_direction = '' (the COALESCE in settled_stats).
    BUCKETS = ("AGREED", "DIVERGED_BOTH", "DIVERGED_NEITHER",
                "VOID_MISMATCH", "BOTH_VOID")

    def _empty() -> dict:
        return {"n": 0, "sum_gap": 0.0, "sum_pnl": 0.0, "sum_avg_pnl_w": 0.0}

    by_cat: dict[str, dict[str, dict]] = {}
    for r in rows:
        cat = r["category"] or "unknown"
        ao = r["agreement_outcome"] or "?"
        dd = r["divergence_direction"] or ""
        bucket_key = ao
        if ao == "DIVERGED":
            if dd == "BOTH_PAID":
                bucket_key = "DIVERGED_BOTH"
            elif dd == "NEITHER_PAID":
                bucket_key = "DIVERGED_NEITHER"
            else:
                # Legacy rows from before the column existed - we cannot
                # tell which direction they were, so bucket them under
                # DIVERGED_NEITHER (the conservative loss direction)
                # rather than silently dropping them. New rows always set
                # divergence_direction so this only affects old data.
                bucket_key = "DIVERGED_NEITHER"
        bucket = by_cat.setdefault(cat, {})
        cur = bucket.setdefault(bucket_key, _empty())
        n_i = int(r["n"])
        cur["n"] += n_i
        cur["sum_gap"] += float(r["avg_gap"] or 0.0) * n_i
        cur["sum_pnl"] += float(r["sum_pnl"] or 0.0)
        cur["sum_avg_pnl_w"] += float(r["avg_pnl"] or 0.0) * n_i

    # Header.
    print()
    print(f"  {'category':10s} {'pairs':>5s} {'AGR':>4s} {'DIV':>4s} "
          f"{'D-Both':>6s} {'avgP+':>6s} {'D-Neith':>7s} {'avgP-':>6s} "
          f"{'VOID':>4s} {'BOTH':>4s} {'div_rate':>9s} {'avg_gap':>9s} "
          f"{'break_div':>10s} {'total_pnl':>10s}  verdict")
    print("  " + "-" * 130)

    def _vals(bucket: dict, key: str) -> dict:
        return bucket.get(key, _empty())

    cats_sorted = sorted(by_cat) + ["__OVERALL__"]
    for cat in cats_sorted:
        if cat == "__OVERALL__":
            bucket: dict[str, dict] = {}
            for _, c_bucket in by_cat.items():
                for key, vals in c_bucket.items():
                    cur = bucket.setdefault(key, _empty())
                    cur["n"] += vals["n"]
                    cur["sum_pnl"] += vals["sum_pnl"]
                    cur["sum_gap"] += vals["sum_gap"]
                    cur["sum_avg_pnl_w"] += vals["sum_avg_pnl_w"]
            label = "OVERALL"
        else:
            bucket = by_cat[cat]
            label = cat

        agr = _vals(bucket, "AGREED")
        dboth = _vals(bucket, "DIVERGED_BOTH")
        dneith = _vals(bucket, "DIVERGED_NEITHER")
        vm = _vals(bucket, "VOID_MISMATCH")
        bv = _vals(bucket, "BOTH_VOID")

        n_agr = agr["n"]; n_dboth = dboth["n"]; n_dneith = dneith["n"]
        n_div = n_dboth + n_dneith
        n_vm = vm["n"]; n_bv = bv["n"]

        # BOTH_VOID excluded from divergence-rate denominator (no
        # information about whether the venues would have agreed).
        n_eff = n_agr + n_div + n_vm
        n_total = n_eff + n_bv

        agr_avg_gap = (agr["sum_gap"] / n_agr) if n_agr else 0.0
        avg_pnl_dboth = (dboth["sum_avg_pnl_w"] / n_dboth) if n_dboth else 0.0
        avg_pnl_dneith = (dneith["sum_avg_pnl_w"] / n_dneith) if n_dneith else 0.0
        sum_pnl = (agr["sum_pnl"] + dboth["sum_pnl"] + dneith["sum_pnl"]
                    + vm["sum_pnl"] + bv["sum_pnl"])

        div_rate = (n_div / n_eff) if n_eff else 0.0
        # Catastrophe-direction loss (positive number = $ lost per pair).
        # Used in the empirical breakeven: how big does the gap need to
        # be to cover the realized catastrophe rate?
        avg_loss_neith = -avg_pnl_dneith if n_dneith else 0.0
        # Empirical breakeven against the NEITHER_PAID direction (the
        # actual loss direction; BOTH_PAID already pays for itself).
        if n_dneith >= 1 and avg_loss_neith > 0:
            breakeven = agr_avg_gap / (agr_avg_gap + avg_loss_neith)
        elif agr_avg_gap > 0:
            # Theoretical fallback: catastrophe loses ~$1 per share.
            breakeven = agr_avg_gap / (agr_avg_gap + 1.0)
        else:
            breakeven = 0.0

        if n_eff < 10:
            verdict = f"INSUFFICIENT (n={n_eff})"
        elif div_rate < breakeven:
            verdict = "POSITIVE EV"
        else:
            verdict = "NEGATIVE EV"

        print(f"  {label[:10]:10s} {n_total:>5d} {n_agr:>4d} {n_div:>4d} "
              f"{n_dboth:>6d} ${avg_pnl_dboth:>+5.2f} "
              f"{n_dneith:>7d} ${avg_pnl_dneith:>+5.2f} "
              f"{n_vm:>4d} {n_bv:>4d} {div_rate*100:>7.1f}% "
              f"${agr_avg_gap:>7.3f} {breakeven*100:>8.1f}% "
              f"${sum_pnl:>+9.2f}  {verdict}")
    print()


def print_edge_freshness(cfg: dict, ledger: Ledger) -> None:
    """EDGE FRESHNESS table.

    Bucket weather trades (live paper + shadow champion) by the
    `minutes_since_forecast_change` recorded at entry and report
    expectancy + hit rate per bucket. If forecast-change watching is
    paying off, the 0-15 minute bucket -- trades opened immediately
    after a model update -- should outperform the 60+ minute bucket
    where the same forecast has been priced in for an hour.

    Three rows: 0-15, 15-60, 60+. Plus a NULL row for trades opened
    before the watcher saw a change for that city (cold-start / pre-
    watcher data) so we can see the migration progress at a glance.
    """
    import sqlite3 as _sql
    BUCKETS = [
        ("0-15m",  0.0,  15.0),
        ("15-60m", 15.0, 60.0),
        ("60+m",   60.0, None),
    ]
    print("=== EDGE FRESHNESS (weather trades bucketed by minutes since "
          "last forecast change) ===")

    def _agg(c, sql: str, params: tuple) -> dict:
        row = c.execute(sql, params).fetchone()
        n = int(row["n"] or 0)
        wins = int(row["wins"] or 0)
        pnl = float(row["pnl"] or 0.0)
        stake = float(row["stake"] or 0.0)
        expectancy = (pnl / stake) if stake > 0 else 0.0
        hit_rate = (wins / n) if n > 0 else 0.0
        return {"n": n, "wins": wins, "pnl": pnl, "stake": stake,
                "expectancy": expectancy, "hit_rate": hit_rate}

    with _sql.connect(ledger.ledger_path) as c:
        c.row_factory = _sql.Row
        # ---- live paper -------------------------------------------------
        live_rows = []
        for label, lo, hi in BUCKETS:
            if hi is None:
                sql = (
                    "SELECT COUNT(*) AS n, "
                    "  COALESCE(SUM(CASE WHEN status='WIN' THEN 1 ELSE 0 END),0) wins, "
                    "  COALESCE(SUM(pnl),0) pnl, COALESCE(SUM(stake),0) stake "
                    "  FROM paper_trades "
                    " WHERE strategy='weather' "
                    "   AND status IN ('WIN','LOSS','VOID') "
                    "   AND minutes_since_forecast_change IS NOT NULL "
                    "   AND minutes_since_forecast_change >= ?")
                live_rows.append((label, _agg(c, sql, (lo,))))
            else:
                sql = (
                    "SELECT COUNT(*) AS n, "
                    "  COALESCE(SUM(CASE WHEN status='WIN' THEN 1 ELSE 0 END),0) wins, "
                    "  COALESCE(SUM(pnl),0) pnl, COALESCE(SUM(stake),0) stake "
                    "  FROM paper_trades "
                    " WHERE strategy='weather' "
                    "   AND status IN ('WIN','LOSS','VOID') "
                    "   AND minutes_since_forecast_change >= ? "
                    "   AND minutes_since_forecast_change <  ?")
                live_rows.append((label, _agg(c, sql, (lo, hi))))
        null_sql_live = (
            "SELECT COUNT(*) AS n, "
            "  COALESCE(SUM(CASE WHEN status='WIN' THEN 1 ELSE 0 END),0) wins, "
            "  COALESCE(SUM(pnl),0) pnl, COALESCE(SUM(stake),0) stake "
            "  FROM paper_trades "
            " WHERE strategy='weather' "
            "   AND status IN ('WIN','LOSS','VOID') "
            "   AND minutes_since_forecast_change IS NULL")
        live_null = _agg(c, null_sql_live, ())

        # ---- shadow champion (the production-equivalent side of the
        # shadow book; never touches the bankroll) -----------------------
        shadow_rows = []
        for label, lo, hi in BUCKETS:
            if hi is None:
                sql = (
                    "SELECT COUNT(*) AS n, "
                    "  COALESCE(SUM(CASE WHEN champ_pnl > 0 THEN 1 "
                    "                    WHEN champ_pnl <= 0 AND champ_pnl IS NOT NULL THEN 0 "
                    "                    ELSE 0 END),0) wins, "
                    "  COALESCE(SUM(champ_pnl),0) pnl, "
                    "  COALESCE(SUM(champ_stake),0) stake "
                    "  FROM shadow_trades "
                    " WHERE status IN ('WIN','LOSS','VOID') "
                    "   AND champ_side IS NOT NULL AND champ_side != 'NONE' "
                    "   AND minutes_since_forecast_change IS NOT NULL "
                    "   AND minutes_since_forecast_change >= ?")
                shadow_rows.append((label, _agg(c, sql, (lo,))))
            else:
                sql = (
                    "SELECT COUNT(*) AS n, "
                    "  COALESCE(SUM(CASE WHEN champ_pnl > 0 THEN 1 ELSE 0 END),0) wins, "
                    "  COALESCE(SUM(champ_pnl),0) pnl, "
                    "  COALESCE(SUM(champ_stake),0) stake "
                    "  FROM shadow_trades "
                    " WHERE status IN ('WIN','LOSS','VOID') "
                    "   AND champ_side IS NOT NULL AND champ_side != 'NONE' "
                    "   AND minutes_since_forecast_change >= ? "
                    "   AND minutes_since_forecast_change <  ?")
                shadow_rows.append((label, _agg(c, sql, (lo, hi))))
        null_sql_shadow = (
            "SELECT COUNT(*) AS n, "
            "  COALESCE(SUM(CASE WHEN champ_pnl > 0 THEN 1 ELSE 0 END),0) wins, "
            "  COALESCE(SUM(champ_pnl),0) pnl, "
            "  COALESCE(SUM(champ_stake),0) stake "
            "  FROM shadow_trades "
            " WHERE status IN ('WIN','LOSS','VOID') "
            "   AND champ_side IS NOT NULL AND champ_side != 'NONE' "
            "   AND minutes_since_forecast_change IS NULL")
        shadow_null = _agg(c, null_sql_shadow, ())

    print()
    print(f"  {'bucket':<8s} {'book':<8s} {'n':>5s} {'wins':>5s} "
          f"{'hit_rate':>9s} {'pnl':>9s} {'stake':>9s} {'expectancy':>11s}")
    print(f"  {'-'*8} {'-'*8} {'-'*5} {'-'*5} {'-'*9} {'-'*9} {'-'*9} {'-'*11}")
    for label, agg in live_rows:
        print(f"  {label:<8s} {'live':<8s} {agg['n']:>5d} {agg['wins']:>5d} "
              f"{agg['hit_rate']*100:>8.1f}% ${agg['pnl']:>+7.2f} "
              f"${agg['stake']:>7.2f} {agg['expectancy']*100:>+9.2f}%")
    print(f"  {'NULL':<8s} {'live':<8s} {live_null['n']:>5d} "
          f"{live_null['wins']:>5d} "
          f"{live_null['hit_rate']*100:>8.1f}% ${live_null['pnl']:>+7.2f} "
          f"${live_null['stake']:>7.2f} "
          f"{live_null['expectancy']*100:>+9.2f}%")
    print(f"  {'-'*8} {'-'*8} {'-'*5} {'-'*5} {'-'*9} {'-'*9} {'-'*9} {'-'*11}")
    for label, agg in shadow_rows:
        print(f"  {label:<8s} {'shadow':<8s} {agg['n']:>5d} {agg['wins']:>5d} "
              f"{agg['hit_rate']*100:>8.1f}% ${agg['pnl']:>+7.2f} "
              f"${agg['stake']:>7.2f} {agg['expectancy']*100:>+9.2f}%")
    print(f"  {'NULL':<8s} {'shadow':<8s} {shadow_null['n']:>5d} "
          f"{shadow_null['wins']:>5d} "
          f"{shadow_null['hit_rate']*100:>8.1f}% ${shadow_null['pnl']:>+7.2f} "
          f"${shadow_null['stake']:>7.2f} "
          f"{shadow_null['expectancy']*100:>+9.2f}%")

    total_live = sum(a["n"] for _, a in live_rows) + live_null["n"]
    total_shadow = sum(a["n"] for _, a in shadow_rows) + shadow_null["n"]
    if total_live == 0 and total_shadow == 0:
        print()
        print("  (no settled weather trades yet -- table populates once "
              "today's markets resolve and grade fires)")
    print()


def print_weather_v2_shadow(cfg: dict, ledger: Ledger) -> None:
    """WeatherModel CHAMPION vs CHALLENGER section. Brier + expectancy
    per city + overall, on the shadow book. Promotion verdict follows
    the rule in cfg.strategies.weather_v2: challenger replaces champion
    only after >= promotion_min_n shadow-graded markets with lower
    Brier AND non-negative expectancy delta. Default OFF (promotion is
    not auto-applied; the section reports whether the conditions
    would have triggered)."""
    v2cfg = (cfg.get("strategies") or {}).get("weather_v2") or {}
    print("=== WEATHER v2 (shadow CHAMPION vs CHALLENGER) ===")
    print(f"  shadow book: paper only, never touches main bankroll")
    print(f"  promotion gate: enabled={bool(v2cfg.get('promotion_enabled', False))}  "
          f"min_n={int(v2cfg.get('promotion_min_n', 75))}")

    # Open + total counters so the operator can see the shadow IS
    # scanning even before any market resolves.
    import sqlite3 as _sql
    with _sql.connect(ledger.ledger_path) as c:
        c.row_factory = _sql.Row
        agg = c.execute(
            """SELECT COUNT(*) AS n_total,
                      SUM(CASE WHEN status='OPEN' THEN 1 ELSE 0 END) AS n_open,
                      SUM(CASE WHEN champ_side IS NOT NULL
                                AND champ_side != 'NONE' THEN 1 ELSE 0 END) AS champ_trades,
                      SUM(CASE WHEN chal_side IS NOT NULL
                                AND chal_side != 'NONE' THEN 1 ELSE 0 END) AS chal_trades
                 FROM shadow_trades""").fetchone()
    print(f"  shadow rows: {int(agg['n_total'] or 0)} total  "
          f"({int(agg['n_open'] or 0)} OPEN, awaiting resolution); "
          f"champion would-trade: {int(agg['champ_trades'] or 0)}, "
          f"challenger would-trade: {int(agg['chal_trades'] or 0)}")

    overall = ledger.shadow_overall_stats()
    n_overall = int(overall["n"]) if overall and overall["n"] is not None else 0
    if n_overall == 0:
        print()
        print("  (no settled shadow trades yet -- table populates once today's "
              "markets resolve and grade fires)")
        return

    rows = ledger.shadow_stats_by_city()
    print()
    print(f"  {'city':<14s} {'n':>4s} {'cN':>4s} {'champ_brier':>11s} "
          f"{'chal_brier':>10s} {'champ_E':>9s} {'chal_E':>9s} "
          f"{'champ_$':>9s} {'chal_$':>9s}")
    print(f"  {'-'*14} {'-'*4} {'-'*4} {'-'*11} {'-'*10} {'-'*9} {'-'*9} "
          f"{'-'*9} {'-'*9}")
    for r in rows:
        city = r["city"] or "?"
        n = int(r["n"]); cN = int(r["champ_n_trades"] or 0)
        cb = float(r["champ_brier"] or 0.0); xb = float(r["chal_brier"] or 0.0)
        ce = float(r["champ_expectancy"] or 0.0)
        xe = float(r["chal_expectancy"] or 0.0)
        ct = float(r["champ_total_pnl"] or 0.0)
        xt = float(r["chal_total_pnl"] or 0.0)
        print(f"  {city[:14]:<14s} {n:>4d} {cN:>4d} {cb:>11.4f} "
              f"{xb:>10.4f} ${ce:>+7.2f} ${xe:>+7.2f} ${ct:>+7.2f} ${xt:>+7.2f}")
    # Overall row
    cb = float(overall["champ_brier"] or 0.0)
    xb = float(overall["chal_brier"] or 0.0)
    ce = float(overall["champ_expectancy"] or 0.0)
    xe = float(overall["chal_expectancy"] or 0.0)
    print(f"  {'-'*14} {'-'*4} {'-'*4} {'-'*11} {'-'*10} {'-'*9} {'-'*9} "
          f"{'-'*9} {'-'*9}")
    print(f"  {'OVERALL':<14s} {n_overall:>4d} {' ':>4s} {cb:>11.4f} "
          f"{xb:>10.4f} ${ce:>+7.2f} ${xe:>+7.2f}")

    # Promotion verdict.
    min_n = int(v2cfg.get("promotion_min_n", 75))
    enabled = bool(v2cfg.get("promotion_enabled", False))
    print()
    if n_overall < min_n:
        print(f"  promotion verdict: INSUFFICIENT (n={n_overall} < min_n={min_n})")
    else:
        brier_better = xb < cb
        exp_non_neg = xe >= ce
        if brier_better and exp_non_neg:
            verdict = "WOULD PROMOTE" if not enabled else "PROMOTING"
        else:
            reasons = []
            if not brier_better: reasons.append(f"Brier {xb:.4f} >= {cb:.4f}")
            if not exp_non_neg: reasons.append(
                f"Expectancy {xe:+.2f} < {ce:+.2f}")
            verdict = "HOLD (" + "; ".join(reasons) + ")"
        print(f"  promotion verdict: {verdict}")
    print()


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
