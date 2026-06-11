"""PolyMarketBotV2 entrypoints: cycle, grade, report.

Run via:
  python main.py cycle      # scan + decide + paper-trade
  python main.py grade      # settle resolved markets, score Brier + P&L
  python main.py report     # print latest report
"""
from __future__ import annotations

import argparse
import importlib
import os
import sys
from typing import Iterable

import yaml

from foundation.bankroll import Bankroll
from foundation.executor import Executor, CycleDecision
from foundation.health import HealthSession, banner as health_banner
from foundation.ledger import Ledger, ledger_from_cfg
from foundation.scanner import Scanner, render_scanner_table
from strategies.base import ArbEvent, Market, Strategy


# Scanner pulls everything tagged under these slugs - strategies filter further.
DEFAULT_TAG_SLUGS = ["highest-temperature", "lowest-temperature"]


def load_config(path: str = "config.yaml") -> dict:
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def load_strategies(cfg: dict) -> list[Strategy]:
    strategies: list[Strategy] = []
    for entry in cfg.get("active_strategies", []):
        module_name = entry["module"] if isinstance(entry, dict) else str(entry)
        mod = importlib.import_module(f"strategies.{module_name}")
        if hasattr(mod, "build"):
            strategies.append(mod.build(cfg))
        else:
            raise RuntimeError(f"strategies.{module_name} must expose build(cfg)")
    return strategies


def scan_all(cfg: dict, scanner: Scanner, fetch_books: bool = True,
             tags: list[str] | None = None) -> list[Market]:
    seen: dict[str, Market] = {}
    cfg_tags = (cfg.get("scanner") or {}).get("tag_slugs")
    tag_list = tags or cfg_tags or DEFAULT_TAG_SLUGS
    for tag in tag_list:
        _, markets = scanner.scan_tag(tag, fetch_books=fetch_books)
        for m in markets:
            seen[m.market_id] = m
    return list(seen.values())


def _is_arb_strategy(s: Strategy) -> bool:
    """An arb strategy implements `scan_arb`; main.py routes it through
    the event path instead of the per-market estimate path."""
    return hasattr(s, "scan_arb") and callable(getattr(s, "scan_arb", None))


def _is_cv_strategy(s: Strategy) -> bool:
    """A cross-venue strategy implements `scan_cv` and operates on two
    venues (Polymarket + Kalshi) instead of a single per-market universe."""
    return hasattr(s, "scan_cv") and callable(getattr(s, "scan_cv", None))


def _is_copy_strategy(s: Strategy) -> bool:
    """A copy-trading strategy implements `follow`."""
    return hasattr(s, "follow") and callable(getattr(s, "follow", None))


def run_cv_cycle(strategy, scanner: Scanner, ledger: Ledger, cfg: dict,
                  verbose: bool = True) -> dict:
    """Cross-venue cycle: pair Polymarket markets with Kalshi markets via
    the rules-equivalence engine, log every cv_gap, paper-fire only
    CERTIFIED-IDENTICAL pairs above min_arb_profit."""
    from foundation.venues.kalshi import KalshiVenue
    from foundation.venues.polymarket import PolymarketVenue
    weather_cfg = (cfg.get("strategies") or {}).get("weather") or {}
    cities = weather_cfg.get("cities") or []
    poly_venue = PolymarketVenue(scanner=scanner, weather_cities_cfg=cities)
    kal_venue = KalshiVenue()
    result = strategy.scan_cv(poly_venue, kal_venue, ledger, verbose=verbose)
    counters = result["counters"]
    if verbose:
        print(
            f"  scan: poly={counters['polymarket_markets']} "
            f"kalshi={counters['kalshi_markets']} "
            f"shared_keys={counters['shared_keys']} "
            f"certified={counters['certified']} fuzzy={counters['fuzzy']} "
            f"nonmatch={counters['nonmatch']}"
        )
        print(f"  cv_gaps logged: {counters['logged_gaps']} | fired: {counters['fired']}")
    return result


def run_arb_cycle(strategy, scanner: Scanner, ledger: Ledger,
                   verbose: bool = True) -> dict:
    """Event-level cycle for bucket-sum arb (and any future arb strategies).

    Pulls every open+active event from Gamma, groups into ArbEvents (MECE-
    verified or flagged), runs the strategy's detector (which lazy-fetches
    CLOB books only on events that pass the cheap pre-filter), logs every
    gap to arb_gaps, and paper-commits any detection whose locked profit
    clears the strategy's min_arb_profit threshold.
    """
    if verbose:
        print(f"\n=== {strategy.name} (event-level) ===")
        print("Fetching every open+active event on Gamma ...")
    raw_events = scanner.fetch_all_events()
    if verbose:
        print(f"  pulled {len(raw_events)} events.")
    arb_events: list[ArbEvent] = [
        scanner.build_arb_event(e, fetch_books=False) for e in raw_events
    ]
    if verbose:
        ok = sum(1 for e in arb_events if e.completeness_verified)
        print(f"  MECE-verified: {ok} / {len(arb_events)}")

    result = strategy.scan_arb(arb_events, scanner=scanner, verbose=verbose)
    detections = result["detections"]
    gamma_only_gaps = result["gamma_only_gaps"]
    counters = result["counters"]

    # ---- log every detection as a gap row (above threshold or not).
    for det in detections:
        ledger.record_arb_gap({
            "strategy": strategy.name,
            "event_id": det.event.event_id,
            "event_slug": det.event.event_slug,
            "event_title": det.event.event_title,
            "n_legs": len(det.event.legs),
            "completeness_verified": det.event.completeness_verified,
            "completeness_note": det.event.completeness_note,
            "side": det.side,
            "walk_mode": det.walk_mode,
            "target_shares": det.target_shares,
            "executable_shares": det.executable_shares,
            "sum_vwap_per_share": det.sum_vwap_per_share,
            "slippage_per_share": det.slippage_per_share,
            "safety_buffer": det.safety_buffer,
            "payout_per_share": det.payout_per_share,
            "locked_profit_per_share": det.locked_profit_per_share,
            "locked_profit_usd": det.locked_profit_usd,
            "end_date_iso": det.event.end_date_iso,
            "legs": [
                {"market_id": L["market_id"], "leg_title": L["leg_title"],
                 "vwap": L["vwap"], "depth_usd": L["depth_usd"],
                 "shares_fillable": L["shares_fillable"]}
                for L in det.legs_detail
            ],
            "cleared_threshold": det.cleared_threshold,
        })

    # ---- log gamma-only (cheap-pass) gaps too, with walk_mode='gamma_only'
    # so the distribution analysis still sees the no-arb majority.
    for g in gamma_only_gaps:
        ev = g["event"]
        ledger.record_arb_gap({
            "strategy": strategy.name,
            "event_id": ev.event_id,
            "event_slug": ev.event_slug,
            "event_title": ev.event_title,
            "n_legs": len(ev.legs),
            "completeness_verified": ev.completeness_verified,
            "completeness_note": ev.completeness_note,
            "side": g["side"],
            "walk_mode": "gamma_only",
            "target_shares": None,
            "executable_shares": None,
            "sum_vwap_per_share": g["snap_sum"],
            "slippage_per_share": None,
            "safety_buffer": strategy.safety_buffer,
            "payout_per_share": g["payout_ps"],
            "locked_profit_per_share": g["profit_ps"],
            "locked_profit_usd": None,
            "end_date_iso": ev.end_date_iso,
            "legs": [],
            "cleared_threshold": False,
        })

    # ---- paper-execute every detection that cleared the threshold.
    fired = 0
    for det in detections:
        if not det.cleared_threshold:
            continue
        side_enabled = (det.side == "YES" and strategy.execute_yes) \
            or (det.side == "NO" and strategy.execute_no)
        if not side_enabled:
            continue
        pid = strategy.commit_detection(det, ledger)
        if pid is not None:
            fired += 1
            if verbose:
                print(f"  [ARB FILL] pos #{pid} {det.side} {det.event.event_slug} "
                      f"shares={det.executable_shares:.1f} "
                      f"profit_per_share={det.locked_profit_per_share:.4f} "
                      f"total=${det.locked_profit_usd:.2f}")

    # ---- Multi-outcome arb extension (Prompt A): write arb_multi rows
    # for every walked event using the $stake_notional variant. This
    # never re-fetches books - reuses the event objects from the main
    # detector pass. Trades fire only when net_gap_pct >= min_net_gap_pct
    # AND every leg can absorb the implied share count.
    multi_logged = 0
    multi_opened = 0
    multi_unfillable = 0
    multi_below = 0
    if getattr(strategy, "multi_mode_enabled", False):
        for ev in result.get("walked_events") or []:
            for side_name in ("YES", "NO"):
                if side_name == "YES" and not strategy.detect_yes:
                    continue
                if side_name == "NO" and not strategy.detect_no:
                    continue
                multi_det = strategy.detect_multi_side(ev, side_name)
                if multi_det is None:
                    continue
                rid = strategy.commit_multi(ev, multi_det, ledger)
                multi_logged += 1
                # Inspect what was written
                if multi_det["status_hint"] == "unfillable_leg":
                    multi_unfillable += 1
                elif multi_det["net_gap_pct"] < strategy.min_net_gap_pct:
                    multi_below += 1
                else:
                    multi_opened += 1
        if verbose:
            print(f"  multi-arb: logged={multi_logged} opened={multi_opened} "
                  f"unfillable={multi_unfillable} below_threshold={multi_below}")

    if verbose:
        print(
            f"  detector: scanned={counters['scanned']} "
            f"complete={counters['complete']} "
            f"incomplete={counters['incomplete']} "
            f"walked={counters['walked']} "
            f"gamma_only={counters['gamma_only_recorded']} "
            f"skipped_time={counters['skipped_time']} "
            f"skipped_cap={counters['skipped_cap']}"
        )
        print(f"  full-walk detections: {len(detections)} | fired: {fired}")
    return {
        "strategy": strategy.name,
        "detections": detections,
        "counters": counters,
        "gamma_only_gaps": gamma_only_gaps,
        "fired": fired,
    }


def cycle(cfg_path: str = "config.yaml", verbose: bool = True,
          tag: str | None = None) -> dict:
    cfg = load_config(cfg_path)
    ledger = ledger_from_cfg(cfg)
    scanner = Scanner(cfg)
    executor = Executor(cfg, ledger)
    bankroll = Bankroll(cfg, ledger)
    strategies = load_strategies(cfg)
    if not strategies:
        print("No active strategies in config.yaml. Nothing to do.")
        return {"decisions": [], "scanned": 0, "filled": 0}

    if verbose:
        print(f"Active strategies: {[s.name for s in strategies]}")

    # Route arb (event-level) and cross-venue strategies separately - they
    # don't go through the per-market scan_all / Estimate path. Each is
    # wrapped in a HealthSession so an exception in one strategy never
    # blocks the others.
    arb_results = []
    cv_results = []
    per_market_strategies: list[Strategy] = []
    for s in strategies:
        if _is_arb_strategy(s):
            with HealthSession(ledger, s.name) as h:
                r = run_arb_cycle(s, scanner, ledger, verbose=verbose)
                arb_results.append(r)
                h.markets_scanned = (r.get("counters") or {}).get("scanned", 0)
                h.fills = r.get("fired", 0)
        elif _is_cv_strategy(s):
            with HealthSession(ledger, s.name) as h:
                r = run_cv_cycle(s, scanner, ledger, cfg, verbose=verbose)
                cv_results.append(r)
                h.markets_scanned = ((r.get("counters") or {}).get("polymarket_markets", 0)
                                       + (r.get("counters") or {}).get("kalshi_markets", 0))
                h.fills = (r.get("counters") or {}).get("fired", 0)
        else:
            per_market_strategies.append(s)
    strategies = per_market_strategies

    if not strategies:
        # Pure arb / cross-venue cycle, no per-market work.
        return {
            "decisions": [], "scanned": 0, "filled": 0,
            "arb": arb_results, "cv": cv_results,
        }

    if verbose:
        print("\nScanning Polymarket (per-market) ...")
    universe = scan_all(cfg, scanner, fetch_books=True,
                        tags=[tag] if tag else None)
    if verbose:
        print(f"Scanned {len(universe)} markets (with order books).")

    decisions: list[CycleDecision] = []
    strategies_cfg = cfg.get("strategies", {})

    # --- Phase 1: evaluate every relevant market for every strategy.
    # Log snapshots + signals as we go. Do NOT commit fills yet.
    # Each strategy runs inside a HealthSession so that an exception in
    # one strategy does NOT abort the cycle for the others.
    pending: list[CycleDecision] = []
    for strat in strategies:
        with HealthSession(ledger, strat.name) as h:
            relevant = strat.relevant_markets(universe)
            h.markets_scanned = len(relevant)
            if verbose:
                print(f"\n[{strat.name}] {len(relevant)} relevant markets")
            for m in relevant:
                mid = ledger.upsert_market({
                    "condition_id": m.market_id,
                    "slug": m.slug,
                    "question": m.question,
                    "category": m.category,
                    "threshold": m.extras.get("parsed_threshold"),
                    "unit": m.extras.get("parsed_unit"),
                    "resolve_date": m.resolve_date,
                    "resolution_source": m.extras.get("station_url"),
                    "rules_text": m.rules_text,
                })
                ledger.record_snapshot(mid, m.yes_ask, m.yes_bid, m.no_ask, m.no_bid,
                                       m.book_depth_usd)
                est = strat.estimate(m)
                if est is None:
                    continue
                ledger.record_signal(mid, strat.name, est.p_final, est.confidence,
                                     est.metadata)
                d = executor.evaluate(m, est, strat, strategies_cfg)
                decisions.append(d)
                if d.decision == "PENDING_FILL":
                    pending.append(d)

    # --- Phase 2: rank PENDING_FILL by post-fill edge desc, commit until cap.
    pending.sort(key=lambda d: (d.edge or 0.0), reverse=True)
    open_at_start = len(ledger.open_positions())
    cap = executor.max_open_positions
    slots = max(0, cap - open_at_start)
    if verbose:
        print(f"\nPhase 2: {len(pending)} qualifying candidates, "
              f"{open_at_start} already open, {slots} slots free of cap {cap}.")
    committed: list[CycleDecision] = []
    for d in pending:
        if len(committed) >= slots:
            d.decision = "SKIP_MAX_OPEN"
            d.reason = (f"cap reached after {len(committed)} commits "
                        f"(cap {cap}, was open {open_at_start})")
            continue
        executor.commit(d, bankroll=bankroll)
        if d.decision == "FILLED":
            committed.append(d)

    if verbose:
        # Print per-decision lines now that final outcomes are known.
        for d in decisions:
            if d.decision == "NO_EDGE" or d.decision.startswith("SKIP") \
                    or d.decision == "FILLED" or d.decision == "NO_EDGE_POST_FILL":
                m = d.market
                marker = "[FILL]" if d.decision == "FILLED" else "  ..  "
                rng = m.extras.get("group_item_title") or ""
                edge_s = (f"edge={d.edge:.4f}"
                          if d.edge is not None else "edge=n/a")
                if d.decision in ("FILLED", "PENDING_FILL"):
                    print(f"  {marker} {m.extras.get('event_slug','')} "
                          f"| {rng:14s} | p={d.p_model:.3f} {edge_s} | "
                          f"{d.decision} | {d.reason}")

    fills = [d for d in decisions if d.decision == "FILLED"]
    if verbose:
        print(f"\nCycle summary: {len(decisions)} considered, "
              f"{len(pending)} qualified, {len(fills)} filled "
              f"(cap {cap}, was open {open_at_start}).")
    return {
        "decisions": decisions,
        "scanned": len(universe),
        "filled": len(fills),
        "qualified": len(pending),
        "arb": arb_results,
        "cv": cv_results,
    }


def show_scanner_only(cfg_path: str = "config.yaml", limit: int = 60) -> None:
    cfg = load_config(cfg_path)
    scanner = Scanner(cfg)
    universe = scan_all(cfg, scanner, fetch_books=True)
    # Print a focused table (5 cities only).
    cities = ["london", "paris", "new-york", "nyc", "miami", "chicago"]
    focused = [m for m in universe
               if any(c in (m.extras.get("event_slug") or "") for c in cities)]
    print(f"Scanner: {len(universe)} markets total, {len(focused)} across 5 cities.")
    print(render_scanner_table(focused, max_rows=limit))


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="polymarketbot")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("cycle", help="run one scan+decide+paper-trade cycle")
    sub.add_parser("scan", help="just show the scanner table")
    sub.add_parser("grade", help="settle resolved markets and update reports")
    sub.add_parser("report", help="print latest daily report")
    sub.add_parser("status", help="print open positions and bankrolls")
    sub.add_parser("arb", help="run the bucket-sum arb detector only (no weather)")
    sub.add_parser("arb-stats", help="print gap-distribution diagnostics for arb")
    sub.add_parser("cv", help="run the cross-venue arb (Polymarket x Kalshi) only")
    sub.add_parser("cv-stats", help="print cross-venue pair + gap diagnostics")
    sub.add_parser("scout", help="run the copy-trading scout: build/update roster")
    sub.add_parser("follow", help="run the copy-trading follower cycle once")
    sub.add_parser("copy-backtest", help="copy-trading backtest table (ESTIMATEs)")
    sub.add_parser("master-report", help="full V2 master report (banner, scoreboard, all strategy sections)")
    sub.add_parser("bankroll", help="print bankroll snapshot + recent txn audit")
    sub.add_parser("sharpline-post", help="post sharpline resting orders (fetches odds)")
    sub.add_parser("sharpline-fill-cycle", help="run sharpline fill-and-grade lifecycle")
    sub.add_parser("lp-sim", help="lp-sim quoting estimate pass")
    sub.add_parser("logic-scan", help="logic-scan pair detection + violation check")
    sub.add_parser("vacuum", help="prune cache.db retention + VACUUM both ledger and cache (daily housekeeping)")
    args = p.parse_args(argv)

    if args.cmd == "cycle":
        cycle()
        return 0
    if args.cmd == "scan":
        show_scanner_only()
        return 0
    if args.cmd == "grade":
        from foundation.grader import grade as grade_fn
        grade_fn()
        return 0
    if args.cmd == "report":
        from foundation.report import print_report
        print_report()
        return 0
    if args.cmd == "status":
        from foundation.report import print_status
        print_status()
        return 0
    if args.cmd == "arb":
        # Run just the bucket-sum arb strategy. The rest of cycle() routes
        # everything else through the per-market path; here we want just
        # the event-level pass.
        cfg = load_config()
        ledger = ledger_from_cfg(cfg)
        scanner = Scanner(cfg)
        strategies = load_strategies(cfg)
        ran = False
        for s in strategies:
            if _is_arb_strategy(s):
                run_arb_cycle(s, scanner, ledger, verbose=True)
                ran = True
        if not ran:
            print("No arb-style strategies active in config.yaml.")
        # Print summary stats after running.
        print_arb_stats(cfg, ledger)
        return 0
    if args.cmd == "arb-stats":
        cfg = load_config()
        ledger = ledger_from_cfg(cfg)
        print_arb_stats(cfg, ledger)
        return 0
    if args.cmd == "cv":
        cfg = load_config()
        ledger = ledger_from_cfg(cfg)
        scanner = Scanner(cfg)
        strategies = load_strategies(cfg)
        ran = False
        for s in strategies:
            if _is_cv_strategy(s):
                run_cv_cycle(s, scanner, ledger, cfg, verbose=True)
                ran = True
        if not ran:
            print("No cross-venue strategies active in config.yaml.")
        print_cv_stats(cfg, ledger)
        return 0
    if args.cmd == "cv-stats":
        cfg = load_config()
        ledger = ledger_from_cfg(cfg)
        print_cv_stats(cfg, ledger)
        return 0
    if args.cmd == "scout":
        return _run_scout()
    if args.cmd == "follow":
        return _run_follow()
    if args.cmd == "copy-backtest":
        return _run_copy_backtest()
    if args.cmd == "master-report":
        from foundation.report import print_master_report
        print_master_report()
        return 0
    if args.cmd == "sharpline-post":
        cfg = load_config()
        ledger = ledger_from_cfg(cfg)
        scanner = Scanner(cfg)
        universe = scan_all(cfg, scanner, fetch_books=True)
        from strategies.sharpline import Sharpline
        s = Sharpline(cfg)
        with HealthSession(ledger, s.name) as h:
            res = s.run(ledger, universe, verbose=True)
            h.markets_scanned = len(universe)
            h.fills = res.get("posted", 0)
        return 0
    if args.cmd == "sharpline-fill-cycle":
        cfg = load_config()
        ledger = ledger_from_cfg(cfg)
        scanner = Scanner(cfg)
        br = Bankroll(cfg, ledger)
        from strategies.sharpline import Sharpline
        s = Sharpline(cfg)
        gamma = (cfg.get("scanner") or {}).get(
            "gamma_url", "https://gamma-api.polymarket.com").rstrip("/")
        with HealthSession(ledger, s.name + "_fill") as h:
            res = s.simulate_fills_and_grade(ledger, scanner, gamma,
                                                bankroll=br, verbose=True)
            h.fills = res.get("filled", 0) + res.get("settled", 0)
        return 0
    if args.cmd == "lp-sim":
        cfg = load_config()
        ledger = ledger_from_cfg(cfg)
        scanner = Scanner(cfg)
        universe = scan_all(cfg, scanner, fetch_books=True)
        from strategies.lp_sim import LPSim
        s = LPSim(cfg)
        with HealthSession(ledger, s.name) as h:
            r = s.run(ledger, universe, verbose=True)
            h.markets_scanned = len(universe)
            h.fills = r.get("rows_logged", 0)
        return 0
    if args.cmd == "vacuum":
        cfg = load_config()
        ledger = ledger_from_cfg(cfg)
        keep = (cfg.get("retention") or {})
        pruned = ledger.prune_cache(
            snapshots_keep_days=int(keep.get("snapshots_keep_days", 7)),
            gaps_keep_days=int(keep.get("gaps_keep_days", 7)),
        )
        sizes = ledger.vacuum()
        print(f"  pruned: {pruned}")
        print(f"  ledger.db: {sizes.get('ledger_bytes', 0)/1024/1024:.2f} MB")
        print(f"  cache.db:  {sizes.get('cache_bytes', 0)/1024/1024:.2f} MB")
        return 0
    if args.cmd == "logic-scan":
        cfg = load_config()
        ledger = ledger_from_cfg(cfg)
        scanner = Scanner(cfg)
        universe = scan_all(cfg, scanner, fetch_books=True)
        from strategies.logic_scan import LogicScan
        s = LogicScan(cfg)
        with HealthSession(ledger, s.name) as h:
            r = s.scan(ledger, universe, verbose=True)
            h.markets_scanned = len(universe)
            h.fills = r.get("traded", 0)
        return 0
    if args.cmd == "bankroll":
        cfg = load_config()
        ledger = ledger_from_cfg(cfg)
        br = Bankroll(cfg, ledger)
        snap = br.snapshot()
        print("strategy           pct   start     cash   exposure  available")
        for r in snap:
            print(f"  {r['strategy']:18s} {float(r['pct'])*100:>4.1f}% "
                  f"${float(r['starting_alloc_usd']):>7.2f} ${float(r['current_cash_usd']):>7.2f} "
                  f"${float(r['open_exposure_usd']):>8.2f} ${float(r['available_usd']):>8.2f}")
        txns = ledger.list_bankroll_txns()[-20:]
        if txns:
            print("\nRecent txns (last 20):")
            for t in txns:
                print(f"  {t['ts']} {t['strategy']:14s} {t['kind']:18s} "
                      f"${float(t['amount_usd']):>+8.2f} "
                      f"cash=${float(t['cash_after_usd']):>8.2f} "
                      f"exposure=${float(t['exposure_after_usd']):>8.2f} "
                      f"{(t['note'] or '')[:40]}")
        return 0
    return 2


def _run_scout() -> int:
    from foundation.polymarket_data import PolymarketData
    cfg = load_config()
    ledger = ledger_from_cfg(cfg)
    data = PolymarketData()
    strategies = load_strategies(cfg)
    for s in strategies:
        if _is_copy_strategy(s):
            res = s.scout(data, ledger, verbose=True)
            print("scout:", res["candidates"], "candidates ->",
                  res["survivors"], "filtered ->",
                  res["roster_size"], "active roster")
            for r in res["roster"][:10]:
                print(f"  ACTIVE rank={r['rank']} score={r['score']} {r['wallet']}")
            return 0
    print("No copy_trading strategy active.")
    return 0


def _run_follow() -> int:
    from foundation.polymarket_data import PolymarketData
    cfg = load_config()
    ledger = ledger_from_cfg(cfg)
    scanner = Scanner(cfg)
    data = PolymarketData()
    strategies = load_strategies(cfg)
    for s in strategies:
        if _is_copy_strategy(s):
            res = s.follow(data, scanner, ledger, verbose=True)
            print(f"follow: leaders={res['leaders']} copied={res['copied']} "
                  f"unfillable={res['unfillable']} skipped_cap={res['skipped_cap']}")
            return 0
    print("No copy_trading strategy active.")
    return 0


def _run_copy_backtest() -> int:
    from foundation.polymarket_data import PolymarketData
    cfg = load_config()
    ledger = ledger_from_cfg(cfg)
    data = PolymarketData()
    strategies = load_strategies(cfg)
    for s in strategies:
        if _is_copy_strategy(s):
            rows = s.backtest(data, ledger, verbose=True)
            print(f"\nCopy backtest (last {s.backtest_lookback_days}d, ESTIMATES):")
            print(f"{'wallet':44s} {'trades':>7s} {'buys':>5s} {'est PnL':>10s}  flag")
            for r in rows[:20]:
                print(f"  {r['wallet']:44s} {r['trades_replayed']:>7d} "
                      f"{r['buys']:>5d} ${r['estimated_pnl_usd']:>+9.2f}  "
                      f"{r['estimate_marker']}")
            return 0
    print("No copy_trading strategy active.")
    return 0


def print_cv_stats(cfg: dict, ledger: Ledger) -> None:
    """Pretty-print cross-venue pair + gap diagnostics."""
    import sqlite3
    strategies = load_strategies(cfg)
    cv_names = [s.name for s in strategies if _is_cv_strategy(s)]
    if not cv_names:
        return
    pair_stats = ledger.cv_pair_stats()
    print()
    print("======================================================")
    print(" Cross-venue (Polymarket x Kalshi) diagnostics")
    print("======================================================")
    print(f"pair classifications: {pair_stats}")
    for sname in cv_names:
        gap_stats = ledger.cv_gap_stats(sname)
        c = ledger.raw_connect()
        try:
            top = list(c.execute(
                """SELECT g.classification, g.direction,
                          g.locked_profit_per_share, g.locked_profit_usd,
                          g.executable_shares, g.cleared_threshold,
                          g.divergence_risk_note, p.city, p.date,
                          p.poly_leg, p.kalshi_leg
                     FROM cache.cv_gaps g JOIN cache.cv_pairs p ON p.id = g.pair_id
                    WHERE g.strategy=? ORDER BY g.locked_profit_per_share DESC
                    LIMIT 10""", (sname,)).fetchall())
            certs = list(c.execute(
                """SELECT id, city, date, poly_leg, kalshi_leg, poly_source,
                          kalshi_source, divergence_risk_note
                     FROM cache.cv_pairs WHERE classification='CERTIFIED-IDENTICAL'
                    LIMIT 10""").fetchall())
            fuzzies = list(c.execute(
                """SELECT id, city, date, poly_leg, kalshi_leg, reason,
                          divergence_risk_note
                     FROM cache.cv_pairs WHERE classification='FUZZY' LIMIT 10""").fetchall())
            positions = list(c.execute(
                """SELECT id, direction, shares, total_cost, expected_payout,
                          locked_profit, divergence_risk_note, status, pnl
                     FROM cv_positions WHERE strategy=?
                    ORDER BY ts DESC""", (sname,)).fetchall())
        finally:
            c.close()
        print(f"\n[{sname}] cv_gaps logged: {gap_stats['total']}")
        print(f"  by classification: {gap_stats['by_classification']}")
        print(f"  by direction: {gap_stats['by_direction']}")
        print(f"  cleared threshold: {gap_stats['cleared']}")
        print(f"  with divergence risk note: {gap_stats['with_divergence']}")
        labels = [
            ("< -10c", -0.10), ("[-10c,-5c)", -0.05), ("[-5c,-2c)", -0.02),
            ("[-2c,-1c)", -0.01), ("[-1c,0)", 0.0), ("[0,0.5c)", 0.005),
            ("[0.5c,1c)", 0.01), ("[1c,2c)", 0.02), ("[2c,5c)", 0.05),
            ("[5c,10c)", 0.10), (">=10c", 1.0),
        ]
        print("  locked-profit-per-share distribution:")
        for label, hi in labels:
            print(f"    {label:14s} {gap_stats['profit_buckets'].get(hi,0)}")
        if certs:
            print("  sample CERTIFIED pairs:")
            for r in certs:
                div = "  [DIV-RISK]" if r["divergence_risk_note"] else ""
                print(f"    #{r['id']} {r['city']:14s} {r['date']} "
                      f"poly='{r['poly_leg'][:18]}' kal='{r['kalshi_leg'][:18]}'{div}")
        if fuzzies:
            print("  sample FUZZY pairs (NEVER auto-traded):")
            for r in fuzzies:
                div = "  [DIV-RISK]" if r["divergence_risk_note"] else ""
                print(f"    #{r['id']} {r['city']:14s} {r['date']} "
                      f"reason={r['reason'][:50]}{div}")
        print("  top 10 gaps by locked profit per share:")
        for r in top:
            mark = "*" if r["cleared_threshold"] else " "
            pps = r["locked_profit_per_share"]
            usd = r["locked_profit_usd"]
            div = "  [DIV-RISK]" if r["divergence_risk_note"] else ""
            print(f"    {mark} {r['classification']:18s} {r['direction']:18s} "
                  f"pps={pps:+.4f} usd=${usd:+.2f} sh={r['executable_shares']:.1f} "
                  f"{r['city'] or '-'}/{r['date'] or '-'}{div}")
        if positions:
            print(f"  paper cv positions: {len(positions)}")
            for p in positions[:10]:
                pnl_s = f"{p['pnl']:+.2f}" if p["pnl"] is not None else "open"
                div = "  [DIV-RISK]" if p["divergence_risk_note"] else ""
                print(f"    #{p['id']} {p['direction']:18s} shares={p['shares']:.1f} "
                      f"cost=${p['total_cost']:.2f} locked=${p['locked_profit']:.2f} "
                      f"status={p['status']} pnl={pnl_s}{div}")


def print_arb_stats(cfg: dict, ledger: Ledger) -> None:
    """Pretty-print the arb_gaps distribution + executed positions per strategy."""
    import sqlite3
    strategies = load_strategies(cfg)
    arb_names = [s.name for s in strategies if _is_arb_strategy(s)]
    if not arb_names:
        return
    print()
    print("======================================================")
    print(" Bucket-sum arb diagnostics")
    print("======================================================")
    for sname in arb_names:
        stats = ledger.arb_gap_stats(sname)
        c = ledger.raw_connect()
        try:
            # arb_gaps lives in cache.db; arb_positions in ledger.db.
            full = list(c.execute(
                """SELECT side, locked_profit_per_share, locked_profit_usd,
                          executable_shares, event_slug, cleared_threshold
                     FROM cache.arb_gaps
                    WHERE strategy=? AND walk_mode='full_book'
                    ORDER BY ts DESC LIMIT 50""", (sname,)).fetchall())
            positions = list(c.execute(
                """SELECT id, event_slug, side, shares, total_cost,
                          expected_payout, locked_profit, status, pnl
                     FROM arb_positions WHERE strategy=?
                    ORDER BY ts DESC""", (sname,)).fetchall())
        finally:
            c.close()
        print(f"\n[{sname}] total gap rows logged: {stats['total']} "
              f"(verified MECE: {stats['verified']})")
        print(f"  by walk_mode: {stats['by_mode']}")
        print(f"  by side: {stats['by_side']}")
        print(f"  cleared threshold: {stats['cleared']}")
        # Distribution of per-share locked profit, split full_book vs gamma_only.
        labels = [
            ("< -10c", -0.10), ("[-10c,-5c)", -0.05), ("[-5c,-2c)", -0.02),
            ("[-2c,-1c)", -0.01), ("[-1c,0)", 0.0), ("[0,0.5c)", 0.005),
            ("[0.5c,1c)", 0.01), ("[1c,2c)", 0.02), ("[2c,5c)", 0.05),
            ("[5c,10c)", 0.10), (">=10c", 1.0),
        ]
        fb = stats["profit_buckets"].get("full_book", {})
        go = stats["profit_buckets"].get("gamma_only", {})
        print("  profit-per-share distribution:")
        print(f"    {'bucket':14s} {'full_book':>10s} {'gamma_only':>11s}")
        for label, hi in labels:
            print(f"    {label:14s} {fb.get(hi,0):>10d} {go.get(hi,0):>11d}")
        print(f"  recent full-walk gaps (top 10):")
        for r in full[:10]:
            mark = "* TRADE" if r["cleared_threshold"] else "  "
            pps = r["locked_profit_per_share"]
            usd = r["locked_profit_usd"]
            pps_s = f"{pps:+.4f}" if pps is not None else "  n/a "
            usd_s = f"${usd:+.2f}" if usd is not None else "  n/a"
            sh = r["executable_shares"]
            sh_s = f"{sh:>6.1f}" if sh is not None else "   n/a"
            print(f"    {mark:7s} {r['side']:3s} pps={pps_s} usd={usd_s:>9s} "
                  f"shares={sh_s} {r['event_slug'][:60]}")
        if positions:
            print(f"  paper positions: {len(positions)}")
            for p in positions[:10]:
                pnl_s = f"{p['pnl']:+.2f}" if p["pnl"] is not None else "open"
                print(f"    #{p['id']} {p['side']:3s} {p['event_slug'][:55]:55s} "
                      f"shares={p['shares']:.1f} cost=${p['total_cost']:.2f} "
                      f"locked=${p['locked_profit']:.2f} status={p['status']} "
                      f"pnl={pnl_s}")


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
    except Exception:
        pass
    sys.exit(main())
