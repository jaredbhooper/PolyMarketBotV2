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
             tags: list[str] | None = None, deadline=None) -> list[Market]:
    """Scan all configured tags. With the master deadline (v2.3) passed
    in, this exits cleanly between tags when time is up -- a half-fetched
    universe is far better than no universe at all because the workflow
    cancelled the run before reaching Commit. Returns whatever markets
    were collected up to that point."""
    from foundation.deadline import Deadline
    deadline = Deadline.coerce(deadline)
    seen: dict[str, Market] = {}
    cfg_tags = (cfg.get("scanner") or {}).get("tag_slugs")
    tag_list = tags or cfg_tags or DEFAULT_TAG_SLUGS
    for tag in tag_list:
        if deadline.expired():
            print(f"  scan_all: deadline reached, skipping remaining tags "
                  f"(was about to scan {tag!r}; collected so far: "
                  f"{len(seen)} markets)")
            break
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
                  verbose: bool = True, deadline=None) -> dict:
    """Cross-venue cycle: pair Polymarket markets with Kalshi markets via
    the rules-equivalence engine, log every cv_gap, paper-fire only
    CERTIFIED-IDENTICAL pairs above min_arb_profit.

    Tail step (v2): if strategies.cv_probe is configured, run the
    quarantined FUZZY divergence probe on the cv result. Probe never
    touches the main bankroll; it has its own $500 virtual side-book.
    """
    from foundation.deadline import Deadline
    from foundation.venues.kalshi import KalshiVenue
    from foundation.venues.polymarket import PolymarketVenue
    deadline = Deadline.coerce(deadline)
    weather_cfg = (cfg.get("strategies") or {}).get("weather") or {}
    cities = weather_cfg.get("cities") or []
    poly_venue = PolymarketVenue(scanner=scanner, weather_cities_cfg=cities)
    kal_venue = KalshiVenue()
    result = strategy.scan_cv(poly_venue, kal_venue, ledger,
                                 verbose=verbose, deadline=deadline)
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
        per_cat = result.get("per_category") or {}
        if per_cat:
            cat_strs = [f"{cat}: c{v['cert']}/f{v['fuzzy']}/n{v['non']}"
                        for cat, v in sorted(per_cat.items())]
            print(f"  per-category (cert/fuzzy/non): {' | '.join(cat_strs)}")

    # Tail step: cv_probe. Skip entirely if not configured or if the
    # master cycle deadline is past (probe's work is cheap but it still
    # has a small DB write per opened position).
    probe_cfg = (cfg.get("strategies") or {}).get("cv_probe")
    if probe_cfg is not None and not deadline.expired():
        from strategies.cv_probe import CVProbe
        probe = CVProbe(cfg)
        probe_out = probe.run_probe(result, ledger, verbose=verbose)
        result["probe"] = probe_out
    elif probe_cfg is not None and verbose:
        print("  cv_probe: skipped (master deadline reached)")
    return result


def run_arb_cycle(strategy, scanner: Scanner, ledger: Ledger,
                   verbose: bool = True, deadline=None) -> dict:
    """Event-level cycle for bucket-sum arb (and any future arb strategies).

    Pulls every open+active event from Gamma, groups into ArbEvents (MECE-
    verified or flagged), runs the strategy's detector (which lazy-fetches
    CLOB books only on events that pass the cheap pre-filter), logs every
    gap to arb_gaps, and paper-commits any detection whose locked profit
    clears the strategy's min_arb_profit threshold.
    """
    from foundation.deadline import Deadline
    deadline = Deadline.coerce(deadline)
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

    result = strategy.scan_arb(arb_events, scanner=scanner, verbose=verbose,
                                  deadline=deadline)
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
    import time as _time
    from foundation.deadline import Deadline
    cycle_start = _time.time()
    phase_timings: list[tuple[str, float]] = []

    def _phase(name: str, start: float) -> None:
        phase_timings.append((name, _time.time() - start))

    cfg = load_config(cfg_path)
    # Master cycle deadline (v2.3). Plumbed through every phase so the
    # workflow ALWAYS reaches the phase table, Guard, and Commit. A
    # cycle must never die by timeout -- strategies truncate inner work
    # when the deadline fires and return whatever they completed.
    deadline_minutes = float(cfg.get("cycle_deadline_minutes", 20))
    deadline = Deadline.in_minutes(deadline_minutes)
    if verbose:
        print(f"Master cycle deadline: {deadline_minutes:.1f} minutes "
              f"({deadline.left():.0f}s left)")

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
            t0 = _time.time()
            with HealthSession(ledger, s.name) as h:
                if deadline.expired():
                    if verbose:
                        print(f"  {s.name}: skipped (master deadline reached)")
                else:
                    r = run_arb_cycle(s, scanner, ledger, verbose=verbose,
                                        deadline=deadline)
                    arb_results.append(r)
                    h.markets_scanned = (r.get("counters") or {}).get("scanned", 0)
                    h.fills = r.get("fired", 0)
            _phase(s.name, t0)
        elif _is_cv_strategy(s):
            t0 = _time.time()
            with HealthSession(ledger, s.name) as h:
                if deadline.expired():
                    if verbose:
                        print(f"  {s.name}: skipped (master deadline reached)")
                else:
                    r = run_cv_cycle(s, scanner, ledger, cfg, verbose=verbose,
                                       deadline=deadline)
                    cv_results.append(r)
                    h.markets_scanned = ((r.get("counters") or {}).get("polymarket_markets", 0)
                                           + (r.get("counters") or {}).get("kalshi_markets", 0))
                    h.fills = (r.get("counters") or {}).get("fired", 0)
            _phase(s.name, t0)
        else:
            per_market_strategies.append(s)
    strategies = per_market_strategies

    if not strategies:
        # Pure arb / cross-venue cycle, no per-market work.
        _run_logic_lp_status_inline(cfg, scanner, ledger, [], deadline,
                                       _phase, verbose=verbose)
        _print_phase_summary(phase_timings, cycle_start, verbose=verbose)
        return {
            "decisions": [], "scanned": 0, "filled": 0,
            "arb": arb_results, "cv": cv_results,
            "phase_timings": phase_timings,
        }

    if verbose:
        print("\nScanning Polymarket (per-market) ...")
    t_scan = _time.time()
    if deadline.expired():
        if verbose:
            print("  scan_per_market: skipped (master deadline reached)")
        universe = []
    else:
        universe = scan_all(cfg, scanner, fetch_books=True,
                            tags=[tag] if tag else None,
                            deadline=deadline)
    _phase("scan_per_market", t_scan)
    if verbose:
        print(f"Scanned {len(universe)} markets (with order books).")

    decisions: list[CycleDecision] = []
    strategies_cfg = cfg.get("strategies", {})

    # --- Phase 1: evaluate every relevant market for every strategy.
    # Log snapshots + signals as we go. Do NOT commit fills yet.
    # Each strategy runs inside a HealthSession so that an exception in
    # one strategy does NOT abort the cycle for the others.
    pending: list[CycleDecision] = []
    # Build the v2 challenger ONCE (lazily; only if weather is among
    # the active strategies and weather_v2 is configured). Shadow
    # writes never affect the main bankroll.
    shadow_challenger = None
    if (cfg.get("strategies") or {}).get("weather_v2") is not None \
            and any(getattr(s, "name", "") == "weather" for s in strategies):
        try:
            from strategies.weather_v2 import WeatherModelV2
            shadow_challenger = WeatherModelV2(cfg)
            shadow_challenger.attach_ledger(ledger)
        except Exception as exc:
            if verbose:
                print(f"  weather_v2 init failed: {exc}")
            shadow_challenger = None

    for strat in strategies:
        t_strat = _time.time()
        skipped_deadline = 0
        is_weather = getattr(strat, "name", "") == "weather"
        with HealthSession(ledger, strat.name) as h:
            # Some strategies (e.g. weather's adaptive layer) need a
            # cycle-scoped read handle to the ledger. Optional hook.
            if hasattr(strat, "attach_ledger"):
                strat.attach_ledger(ledger)
            if deadline.expired():
                if verbose:
                    print(f"\n[{strat.name}] skipped (master deadline reached)")
                _phase(strat.name, t_strat)
                continue
            relevant = strat.relevant_markets(universe)
            h.markets_scanned = len(relevant)
            if verbose:
                print(f"\n[{strat.name}] {len(relevant)} relevant markets")
            for m in relevant:
                if deadline.expired():
                    skipped_deadline += 1
                    continue
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
                    # v2 challenger may still want to score even when
                    # champion bails out (no symmetric reason here but
                    # left as a hook). Skip head-to-head for clarity.
                    continue
                ledger.record_signal(mid, strat.name, est.p_final, est.confidence,
                                     est.metadata)
                d = executor.evaluate(m, est, strat, strategies_cfg)
                decisions.append(d)
                if d.decision == "PENDING_FILL":
                    pending.append(d)

                # Shadow challenger: score the same market with the v2
                # model + log both decisions to shadow_trades. This is
                # paper-only and never touches the main bankroll.
                if is_weather and shadow_challenger is not None:
                    try:
                        est_v2 = shadow_challenger.estimate(m)
                        d_v2 = (executor.evaluate(m, est_v2,
                                                      shadow_challenger,
                                                      strategies_cfg)
                                  if est_v2 is not None else None)
                        ledger.upsert_shadow_trade({
                            "market_id": mid,
                            "city": (est.metadata or {}).get("city"),
                            "resolve_date": m.resolve_date,
                            "champ_p": float(est.p_final),
                            "champ_side": getattr(d, "side", None) or "NONE",
                            "champ_edge": getattr(d, "edge", None),
                            "champ_price_filled": (
                                d.fill.price_filled if d.fill else None),
                            "champ_stake": (
                                d.fill.stake if d.fill else None),
                            "champ_shares": (
                                d.fill.shares if d.fill else None),
                            "chal_p": (float(est_v2.p_final)
                                          if est_v2 is not None else None),
                            "chal_side": (getattr(d_v2, "side", None) or "NONE"
                                            if d_v2 is not None else "NONE"),
                            "chal_edge": (getattr(d_v2, "edge", None)
                                              if d_v2 is not None else None),
                            "chal_price_filled": (
                                d_v2.fill.price_filled
                                if d_v2 and d_v2.fill else None),
                            "chal_stake": (
                                d_v2.fill.stake
                                if d_v2 and d_v2.fill else None),
                            "chal_shares": (
                                d_v2.fill.shares
                                if d_v2 and d_v2.fill else None),
                        })
                    except Exception as exc:
                        if verbose:
                            print(f"  shadow log failed for market "
                                    f"{mid}: {exc}")
            if skipped_deadline and verbose:
                print(f"  [{strat.name}] deadline truncated: "
                      f"{skipped_deadline} markets deferred")
        _phase(strat.name, t_strat)

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

    # Tail strategies that used to run as separate `python main.py ...`
    # commands now run in-process so they share the master deadline and
    # the already-fetched universe + scanner connections. Workflow
    # previously cancelled mid-tail because each `python` re-fetched the
    # universe -- a 4-minute scan each.
    _run_logic_lp_status_inline(cfg, scanner, ledger, universe, deadline,
                                  _phase, verbose=verbose)

    _print_phase_summary(phase_timings, cycle_start, verbose=verbose)
    return {
        "decisions": decisions,
        "scanned": len(universe),
        "filled": len(fills),
        "qualified": len(pending),
        "arb": arb_results,
        "cv": cv_results,
        "phase_timings": phase_timings,
    }


def _run_logic_lp_status_inline(cfg: dict, scanner: Scanner, ledger: Ledger,
                                   universe: list, deadline, _phase,
                                   verbose: bool = True) -> None:
    """Run logic-scan + lp-sim + status as in-process tail steps inside
    the same master deadline. v2.3 fix: the cycle.yml workflow used to
    invoke each of these as a separate `python` subprocess that
    re-fetched the entire universe with books (~4 min each), so they
    routinely ate the rest of the job timeout and forced GitHub Actions
    to cancel before Guard / Commit could run. By running them here we
    share the deadline + the already-fetched universe and the cycle
    ALWAYS reaches the phase-timing table."""
    import time as _time
    # logic-scan
    t = _time.time()
    if deadline.expired():
        if verbose:
            print("\nlogic-scan: skipped (master deadline reached)")
    else:
        try:
            from strategies.logic_scan import LogicScan
            from foundation.health import HealthSession
            ls = LogicScan(cfg)
            with HealthSession(ledger, ls.name) as h:
                r = ls.scan(ledger, universe, verbose=verbose)
                h.markets_scanned = len(universe)
                h.fills = r.get("traded", 0) if isinstance(r, dict) else 0
        except Exception as exc:
            if verbose:
                print(f"  logic-scan failed: {exc}")
    _phase("logic_scan_tail", t)
    # lp-sim
    t = _time.time()
    if deadline.expired():
        if verbose:
            print("\nlp-sim: skipped (master deadline reached)")
    else:
        try:
            from strategies.lp_sim import LPSim
            from foundation.health import HealthSession
            lp = LPSim(cfg)
            with HealthSession(ledger, lp.name) as h:
                r = lp.run(ledger, universe, verbose=verbose)
                h.markets_scanned = len(universe)
                h.fills = r.get("rows_logged", 0) if isinstance(r, dict) else 0
        except Exception as exc:
            if verbose:
                print(f"  lp-sim failed: {exc}")
    _phase("lp_sim_tail", t)
    # status -- cheap, always run for visibility
    t = _time.time()
    try:
        from foundation.report import print_status
        print_status()
    except Exception as exc:
        if verbose:
            print(f"  status failed: {exc}")
    _phase("status_tail", t)


def _print_phase_summary(phase_timings: list[tuple[str, float]],
                          cycle_start: float, verbose: bool = True) -> None:
    """Print a phase->elapsed table at the end of a cycle so duration
    regressions are visible at a glance from the Actions tab. Total
    matches the cycle wall-clock; any difference is the cycle-bootstrap
    overhead (config + ledger + scanner init + strategy load)."""
    if not verbose:
        return
    import time as _time
    total = _time.time() - cycle_start
    tracked = sum(elapsed for _, elapsed in phase_timings)
    print()
    print(f"=== cycle phase timings ===")
    print(f"  {'phase':<22s}  {'elapsed':>9s}  {'% of cycle':>10s}")
    print(f"  {'-'*22}  {'-'*9}  {'-'*10}")
    for name, elapsed in phase_timings:
        pct = (elapsed / total * 100.0) if total > 0 else 0.0
        print(f"  {name:<22s}  {elapsed:>7.1f}s  {pct:>9.1f}%")
    overhead = max(0.0, total - tracked)
    print(f"  {'(bootstrap/misc)':<22s}  {overhead:>7.1f}s  "
          f"{(overhead/total*100.0 if total>0 else 0.0):>9.1f}%")
    print(f"  {'-'*22}  {'-'*9}  {'-'*10}")
    print(f"  {'TOTAL':<22s}  {total:>7.1f}s")


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
    sub.add_parser("cv-probe",
                    help="print CV-PROBE research-book stats (FUZZY divergence experiment)")
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
    sub.add_parser("wx-weights", help="print the current per-city adaptive weather weights, biases, and calibration")
    sub.add_parser("wx-verify", help="print the WX-VERIFY skill + dispute-forensics report")
    sub.add_parser("wx-regrade-disputed",
                    help="re-settle DISPUTED weather positions on WU (Wunderground is authoritative)")
    aut = sub.add_parser("autopsy", help="behavioral fingerprint + archetype for a single wallet")
    aut.add_argument("wallet")
    aut.add_argument("--refresh", action="store_true",
                     help="pull fresh trades via Polymarket data API before classifying")
    autt = sub.add_parser("autopsy-top",
                          help="run autopsy over top wallets by realized P&L from the candidate pool")
    autt.add_argument("-n", "--limit", type=int, default=20)
    autt.add_argument("--refresh", action="store_true")
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
    if args.cmd == "cv-probe":
        cfg = load_config()
        ledger = ledger_from_cfg(cfg)
        from foundation.report import print_cv_probe_report
        print_cv_probe_report(cfg, ledger)
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
    if args.cmd == "wx-weights":
        cfg = load_config()
        ledger = ledger_from_cfg(cfg)
        _print_wx_weights(cfg, ledger)
        return 0
    if args.cmd == "wx-verify":
        cfg = load_config()
        ledger = ledger_from_cfg(cfg)
        _print_wx_verify(cfg, ledger)
        return 0
    if args.cmd == "wx-regrade-disputed":
        from foundation.grader import regrade_disputed_on_wu
        from datetime import datetime, timezone
        cfg = load_config()
        ledger = ledger_from_cfg(cfg)
        today = datetime.now(timezone.utc).date()
        regrade_disputed_on_wu(cfg, ledger, today, verbose=True)
        return 0
    if args.cmd == "autopsy":
        cfg = load_config()
        ledger = ledger_from_cfg(cfg)
        pmd = None
        if args.refresh:
            from foundation.polymarket_data import PolymarketData
            pmd = PolymarketData()
        _print_autopsy_single(cfg, ledger, args.wallet, polymarket_data=pmd,
                              refresh=args.refresh)
        return 0
    if args.cmd == "autopsy-top":
        cfg = load_config()
        ledger = ledger_from_cfg(cfg)
        pmd = None
        if args.refresh:
            from foundation.polymarket_data import PolymarketData
            pmd = PolymarketData()
        _print_autopsy_top(cfg, ledger, n=args.limit,
                           polymarket_data=pmd, refresh=args.refresh)
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
            print(f"scout: {res['candidates']} candidates -> "
                  f"processed {res.get('processed', res.get('survivors',0)+res.get('excluded',0))}, "
                  f"survivors {res['survivors']}, "
                  f"deferred {res.get('deferred', 0)}, "
                  f"active roster {res['roster_size']}")
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


def _print_wx_weights(cfg: dict, ledger: Ledger) -> None:
    """Inspect the adaptive weather layer: per-city family weights,
    per-city per-family biases, and the global calibration alpha."""
    from foundation.wx_skill import (AdaptiveConfig, adaptive_for_city,
                                       fit_calibration)
    s = (cfg.get("strategies") or {}).get("weather") or {}
    adapt_cfg_raw = s.get("adaptive_weights")
    enabled = True
    if adapt_cfg_raw is False:
        enabled = False
        adapt_cfg_raw = {}
    elif not isinstance(adapt_cfg_raw, dict):
        adapt_cfg_raw = {}
    adapt_cfg = AdaptiveConfig(
        enabled=enabled,
        window_days=int(adapt_cfg_raw.get("window_days", 60)),
        shrink_n=int(adapt_cfg_raw.get("shrink_n", 20)),
        min_n=int(adapt_cfg_raw.get("min_n", 10)),
        calib_min_n=int(adapt_cfg_raw.get("calib_min_n", 30)),
        max_bias_correction=float(adapt_cfg_raw.get(
            "max_bias_correction", 1.5)),
        epsilon_mae=float(adapt_cfg_raw.get("epsilon_mae", 0.05)),
    )
    rows = [dict(r) for r in ledger.list_wx_verifications()]
    print()
    print(f"=== Adaptive weather weights ===")
    kill = " (KILL SWITCH off - layer disabled)" if not enabled else ""
    print(f"  config: window={adapt_cfg.window_days}d  shrink_n={adapt_cfg.shrink_n}"
          f"  min_n={adapt_cfg.min_n}  calib_min_n={adapt_cfg.calib_min_n}"
          f"  max_bias_clip=±{adapt_cfg.max_bias_correction:.1f}{kill}")
    print(f"  verifications loaded: {len(rows)}")
    print()
    print(f"  {'city':18s} {'n':>4s} {'w_gfs':>7s} {'w_ecmwf':>9s} "
          f"{'b_gfs':>7s} {'b_ecmwf':>9s} {'status':>8s}")
    print(f"  {'-'*18} {'-'*4} {'-'*7} {'-'*9} {'-'*7} {'-'*9} {'-'*8}")
    cities_in_cfg = [c.get("city") for c in (s.get("cities") or [])
                       if c.get("city")]
    for cname in sorted(set(cities_in_cfg)):
        st = adaptive_for_city(rows, cname, adapt_cfg)
        print(f"  {cname:18s} {st.n_resolutions:>4d} "
              f"{st.weights.get('gfs', 0.5):>7.3f} "
              f"{st.weights.get('ecmwf', 0.5):>9.3f} "
              f"{st.biases.get('gfs', 0.0):>+7.2f} "
              f"{st.biases.get('ecmwf', 0.0):>+9.2f} "
              f"{st.weights_status:>8s}")
    # Overall row
    overall = adaptive_for_city(rows, None, adapt_cfg)
    print(f"  {'-'*18} {'-'*4} {'-'*7} {'-'*9} {'-'*7} {'-'*9} {'-'*8}")
    print(f"  {'OVERALL':18s} {overall.n_resolutions:>4d} "
          f"{overall.weights.get('gfs', 0.5):>7.3f} "
          f"{overall.weights.get('ecmwf', 0.5):>9.3f} "
          f"{overall.biases.get('gfs', 0.0):>+7.2f} "
          f"{overall.biases.get('ecmwf', 0.0):>+9.2f} "
          f"{overall.weights_status:>8s}")
    cal = fit_calibration(rows, adapt_cfg)
    print()
    print(f"  calibration alpha = {cal.alpha:.3f}  n={cal.n}  status={cal.status}")
    print(f"  (alpha=1.0 = identity; alpha<1 shrinks predictions toward 0.5)")
    print()


def _print_wx_verify(cfg: dict, ledger: Ledger) -> None:
    """Per-city + overall skill tables, reliability bins, and dispute
    forensics (OM − WU per station-day).

    v2.3 rendering fix: family rows print whenever the family has ANY
    signal we can score (per-family MAE+bias OR per-family p_threshold +
    YES/NO outcome). Older backfilled rows lack the per-family mean
    forecast so MAE/bias are absent -- the table still renders Brier
    from p_threshold so we can see calibration even when the absolute
    error is unavailable. n/a marks the missing columns honestly
    instead of silently dropping the row.
    """
    from foundation.wx_skill import (AdaptiveConfig, FAMILIES, family_skill,
                                       dispute_forensics)
    rows = [dict(r) for r in ledger.list_wx_verifications()]
    print()
    print(f"=== WX-VERIFY  (n={len(rows)} verifications) ===")
    if not rows:
        print("  no verifications yet")
        return

    def _brier_only(pool: list[dict], family: str) -> tuple[int, float]:
        """Compute (n, mean_brier) using only p_threshold + outcome.
        Used as a fallback when family means/spreads are absent."""
        pkey = f"{family}_p_threshold"
        n = 0; bsum = 0.0
        for r in pool:
            p = r.get(pkey)
            o = r.get("outcome")
            if p is None or o not in ("YES", "NO"):
                continue
            try:
                pf = float(p)
            except (TypeError, ValueError):
                continue
            n += 1
            y = 1.0 if o == "YES" else 0.0
            bsum += (pf - y) ** 2
        return n, (bsum / n if n else 0.0)

    by_city: dict[str, list[dict]] = {}
    for r in rows:
        by_city.setdefault(r.get("city") or "?", []).append(r)
    print()
    print(f"  {'city':18s} {'n':>3s} {'fam':>5s} {'n_f':>4s} {'MAE':>6s} "
          f"{'bias':>7s} {'brier':>7s}")
    print(f"  {'-'*18} {'-'*3} {'-'*5} {'-'*4} {'-'*6} {'-'*7} {'-'*7}")

    def _emit(label: str, pool: list[dict]) -> None:
        for f in FAMILIES:
            sk = family_skill(pool, f)
            if sk.n > 0:
                print(f"  {label:18s} {len(pool):>3d} {f:>5s} {sk.n:>4d} "
                      f"{sk.mae:>6.2f} {sk.signed_bias:>+7.2f} "
                      f"{sk.brier:>7.4f}")
                continue
            # Calibration-only fallback: render Brier from p_threshold +
            # outcome when per-family error metadata is absent.
            n_b, brier = _brier_only(pool, f)
            if n_b == 0:
                continue
            print(f"  {label:18s} {len(pool):>3d} {f:>5s} {n_b:>4d} "
                  f"{'   n/a':>6s} {'    n/a':>7s} {brier:>7.4f}")

    for cname in sorted(by_city):
        _emit(cname, by_city[cname])
    _emit("OVERALL", rows)

    # Reliability: how often did our P_blended buckets actually hit?
    print()
    print("  Reliability of P_blended (predicted vs observed YES-rate):")
    buckets = [(0.5, 0.6), (0.6, 0.7), (0.7, 0.8), (0.8, 0.9), (0.9, 1.001)]
    print(f"    {'bucket':14s} {'n':>4s} {'predicted':>10s} {'observed':>9s}")
    for lo, hi in buckets:
        n_bk = 0; pred_sum = 0.0; obs_sum = 0.0
        for r in rows:
            p = r.get("p_blended")
            o = r.get("outcome")
            if p is None or o not in ("YES", "NO"):
                continue
            try:
                p = float(p)
            except (TypeError, ValueError):
                continue
            if not (lo <= p < hi):
                continue
            n_bk += 1
            pred_sum += p
            obs_sum += 1.0 if o == "YES" else 0.0
        if n_bk == 0:
            continue
        print(f"    [{lo:.2f}, {hi:.2f}) {n_bk:>4d} "
              f"{pred_sum/n_bk:>10.3f} {obs_sum/n_bk:>9.3f}")

    # Dispute forensics.
    print()
    print("  Dispute forensics  (OM − WU per station, native units):")
    fc = dispute_forensics(rows)
    if not fc:
        print("    no rows with both OM + WU values")
    else:
        print(f"    {'station':14s} {'n':>3s} {'mean':>7s} {'std':>6s}  verdict")
        print(f"    {'-'*14} {'-'*3} {'-'*7} {'-'*6}  {'-'*30}")
        for st, ds in sorted(fc.items()):
            verdict = ("constant offset (likely bug)"
                       if ds.n >= 2 and ds.std_om_minus_wu < 0.3
                       else "variable (real source diff)" if ds.n >= 2
                       else "single sample")
            print(f"    {st:14s} {ds.n:>3d} {ds.mean_om_minus_wu:>+7.2f} "
                  f"{ds.std_om_minus_wu:>6.2f}  {verdict}")
    print()


def _print_autopsy_single(cfg: dict, ledger: Ledger, wallet: str,
                            polymarket_data=None, refresh: bool = False) -> None:
    from foundation.autopsy import autopsy
    aut_cfg = (cfg.get("autopsy") or {})
    res = autopsy(wallet, ledger, polymarket_data=polymarket_data,
                  cfg=aut_cfg, refresh=refresh)
    fp = res["fingerprint"]
    print()
    print("=== Wallet autopsy ===")
    print(f"  wallet:           {res['wallet']}")
    print(f"  n_trades:         {fp['n_trades']}  "
          f"n_resolved:{fp.get('n_resolved', 0)}  "
          f"track_record:{fp.get('track_record_days', 0):.0f}d")
    print(f"  realized_pnl_usd: {fp.get('realized_pnl_usd', 0.0):+.2f}  "
          f"roi/trade:{fp.get('roi_per_trade', 0.0):+.3f}")
    print()
    print("  --- HABITAT ---")
    print(f"  dominant_category={fp.get('dominant_category', '?')}  "
          f"dominant_share={fp.get('dominant_share', 0.0):.2f}  "
          f"n_categories_active={fp.get('n_categories_active', 0)}")
    print()
    print("  --- TIMING ---")
    med = fp.get("median_interval_sec", float("inf"))
    med_s = f"{med:.0f}s" if med != float("inf") else "n/a"
    print(f"  median_interval={med_s}  "
          f"interval_cv={fp.get('interval_cv', 0.0):.2f}  "
          f"hour_entropy_norm={fp.get('hour_entropy_norm', 0.0):.2f}  "
          f"share_overnight_utc={fp.get('share_overnight', 0.0):.2f}")
    print(f"  trades_per_active_day={fp.get('trades_per_active_day', 0.0):.2f}  "
          f"stake_uniformity={fp.get('stake_uniformity', 0.0):.2f}")
    print()
    print("  --- ENTRY PRICE ---")
    print(f"  mean={fp.get('entry_price_mean', 0.0):.2f}  "
          f"std={fp.get('entry_price_std', 0.0):.2f}  "
          f"p10/p50/p90={fp.get('entry_price_p10', 0.0):.2f}/"
          f"{fp.get('entry_price_p50', 0.0):.2f}/"
          f"{fp.get('entry_price_p90', 0.0):.2f}")
    print(f"  share_entry_extreme={fp.get('share_entry_extreme', 0.0):.2f}")
    print()
    print("  --- HOLD + EXIT ---")
    print(f"  median_hold_days={fp.get('median_hold_days', 0.0):.2f}  "
          f"pct_held_to_end={fp.get('pct_held_to_end', 0.0):.2f}")
    print()
    print("  --- BEHAVIOR ---")
    print(f"  two_sided_pairs={fp.get('two_sided_pairs', 0)} (rate "
          f"{fp.get('two_sided_rate', 0.0):.2f}) -- MM signature")
    print(f"  offsetting_pairs={fp.get('offsetting_pairs', 0)} (rate "
          f"{fp.get('offsetting_rate', 0.0):.2f}) -- arb signature")
    print()
    print("  --- VERDICT ---")
    print(f"  archetype:        {res['archetype']}  "
          f"(confidence {res['confidence']:.2f})")
    print(f"  closest strategy: {res['closest_strategy']}")
    print(f"  copyability:      {res['copyability']}")
    print(f"  evidence:")
    for e in res["evidence"]:
        print(f"    - {e}")
    print()


def _print_autopsy_top(cfg: dict, ledger: Ledger, n: int = 20,
                        polymarket_data=None, refresh: bool = False) -> None:
    from foundation.autopsy import autopsy_top, ARCHETYPE_ANALOGUE
    aut_cfg = (cfg.get("autopsy") or {})
    out = autopsy_top(ledger, polymarket_data=polymarket_data,
                      cfg=aut_cfg, n=n, refresh=refresh)
    print()
    print("=== Wallet autopsy (top-N by realized P&L) ===")
    if not out["results"]:
        print("  no candidate wallets cached. Run `scout` first.")
        return
    print(f"  processed: {out['n_processed']}")
    print()
    print("  --- ARCHETYPE CENSUS ---")
    print(f"  {'archetype':18s} {'count':>5s}  {'closest strategy / copyability'}")
    print(f"  {'-'*18} {'-'*5}  {'-'*60}")
    for arch in ("speed-reactor", "market-maker", "arbitrageur",
                  "sharp-line-taker", "niche-judgment",
                  "endgame-grinder", "mixed"):
        cnt = out["census"].get(arch, 0)
        if cnt == 0:
            continue
        a = ARCHETYPE_ANALOGUE[arch]
        print(f"  {arch:18s} {cnt:>5d}  {a['analogue']} | {a['copyable']}")
    print()
    print("  --- PER-WALLET VERDICTS ---")
    print(f"  {'wallet':44s} {'pnl_usd':>10s} {'n':>5s} {'archetype':18s} {'conf':>5s}")
    print(f"  {'-'*44} {'-'*10} {'-'*5} {'-'*18} {'-'*5}")
    for r in sorted(out["results"], key=lambda x: x.get("realized_pnl_usd", 0.0),
                     reverse=True):
        print(f"  {r['wallet']:44s} {r.get('realized_pnl_usd', 0.0):>+10.2f} "
              f"{r['n_trades']:>5d} {r['archetype']:18s} "
              f"{r.get('confidence', 0.0):>5.2f}")
    print()


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
