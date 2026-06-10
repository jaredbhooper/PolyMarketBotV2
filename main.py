"""PolyMarketBotV1 entrypoints: cycle, grade, report.

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

from foundation.executor import Executor, CycleDecision
from foundation.ledger import Ledger
from foundation.scanner import Scanner, render_scanner_table
from strategies.base import Market, Strategy


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


def cycle(cfg_path: str = "config.yaml", verbose: bool = True,
          tag: str | None = None) -> dict:
    cfg = load_config(cfg_path)
    ledger = Ledger(cfg["database"]["path"])
    scanner = Scanner(cfg)
    executor = Executor(cfg, ledger)
    strategies = load_strategies(cfg)
    if not strategies:
        print("No active strategies in config.yaml. Nothing to do.")
        return {"decisions": [], "scanned": 0, "filled": 0}

    if verbose:
        print(f"Active strategies: {[s.name for s in strategies]}")
        print("Scanning Polymarket ...")
    universe = scan_all(cfg, scanner, fetch_books=True,
                        tags=[tag] if tag else None)
    if verbose:
        print(f"Scanned {len(universe)} markets (with order books).")

    decisions: list[CycleDecision] = []
    strategies_cfg = cfg.get("strategies", {})

    # --- Phase 1: evaluate every relevant market for every strategy.
    # Log snapshots + signals as we go. Do NOT commit fills yet.
    pending: list[CycleDecision] = []
    for strat in strategies:
        relevant = strat.relevant_markets(universe)
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
        executor.commit(d)
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
    return 2


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
    except Exception:
        pass
    sys.exit(main())
