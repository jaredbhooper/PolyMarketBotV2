"""Grader: settle resolved markets, compute Brier + P&L per strategy.

The grader is strategy-pluggable (sec 3.3): each strategy may expose a
resolve(market, settled_at) returning {actual_value, source_value, outcome}.
If a strategy doesn't, the grader falls back to a generic "skip" path -
no fake outcomes ever get written.
"""
from __future__ import annotations

import importlib
import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Iterable

import yaml

from foundation.ledger import Ledger
from strategies.base import Market, Strategy


def _load_strategies(cfg: dict) -> dict[str, Strategy]:
    out: dict[str, Strategy] = {}
    for entry in cfg.get("active_strategies", []):
        mod_name = entry["module"] if isinstance(entry, dict) else str(entry)
        mod = importlib.import_module(f"strategies.{mod_name}")
        if hasattr(mod, "build"):
            s = mod.build(cfg)
            out[s.name] = s
    return out


def _row_to_market(row: sqlite3.Row) -> Market:
    """Reconstruct just enough Market shape to call strategy.resolve()."""
    slug = row["slug"] or ""
    # Infer market kind from the slug (the scanner doesn't write `kind` to
    # the markets table; we recover it here for the resolver).
    if "lowest-temperature" in slug:
        kind = "min"
    else:
        kind = "max"
    extras = {
        "parsed_threshold": row["threshold"],
        "parsed_unit": row["unit"],
        "parsed_bound": None,    # We re-parse from question / slug below.
        "lo": None, "hi": None,
        "event_slug": slug,
        "kind": kind,
        "station_url": row["resolution_source"],
    }
    # Re-parse so resolve() has the bound info.
    from foundation.scanner import parse_group_title
    # Polymarket weather slugs encode the range; we can also pull from question.
    # Try the question first.
    question = row["question"] or ""
    parsed = None
    for tail in [question, row["slug"] or ""]:
        # Try common suffixes "X°C or below" / "X°C or above" / "X°C" / "X-YF"
        # found in the question text. The slug itself often has the bucket too.
        for marker in ["highest temperature in", "highest-temperature-in"]:
            pass
        # Heuristic: take last 30 chars of question and feed to parse_group_title.
        snippet = tail.strip().rstrip("?").strip()
        # If the question/slug ends with bucket info, the parser will grab it.
        parsed = parse_group_title(snippet)
        if parsed and parsed.get("threshold") is not None:
            break
    if parsed:
        extras["parsed_bound"] = parsed.get("bound")
        extras["lo"] = parsed.get("lo")
        extras["hi"] = parsed.get("hi")
        if extras["parsed_threshold"] is None:
            extras["parsed_threshold"] = parsed.get("threshold")
        if not extras["parsed_unit"]:
            extras["parsed_unit"] = parsed.get("unit")
    return Market(
        market_id=row["condition_id"],
        slug=row["slug"] or "",
        question=question,
        category=row["category"] or "",
        rules_text=row["rules_text"] or "",
        resolve_date=row["resolve_date"],
        end_date_iso=row["resolve_date"],
        yes_token_id=None, no_token_id=None,
        yes_ask=None, yes_bid=None, no_ask=None, no_bid=None,
        extras=extras,
    )


@dataclass
class GradeOutcome:
    market_row_id: int
    outcome: str       # YES / NO / VOID
    actual_value: float | None
    source_value: str


def _settle_trade_pnl(side: str, price_filled: float, stake: float,
                      shares: float, outcome: str) -> float:
    """For YES position: win = shares*$1 - stake; loss = -stake.
    For NO position: same shape - we bought NO shares, each pays $1 if NO wins.
    VOID -> 0.
    """
    if outcome == "VOID":
        return 0.0
    won = (side == outcome)
    if won:
        return float(shares) - float(stake)
    return -float(stake)


def grade(cfg_path: str = "config.yaml", lookback_days: int = 14,
          verbose: bool = True) -> dict:
    """Settle markets whose resolve_date <= today (UTC) and update reports.

    For each open trade whose market is past its end date:
      1) ask the strategy that opened it for resolve(...). If it returns an
         outcome, settle.
      2) compute pnl, mark trade WIN / LOSS.
    Then for each strategy, write/update today's daily_report row.
    """
    cfg = yaml.safe_load(open(cfg_path, "r", encoding="utf-8"))
    ledger = Ledger(cfg["database"]["path"])
    strategies = _load_strategies(cfg)
    today = datetime.now(timezone.utc).date()

    settled = 0
    skipped = 0
    open_rows = ledger.open_positions()
    if verbose:
        print(f"Grader: {len(open_rows)} open positions to evaluate.")

    for trade in open_rows:
        market_row = ledger.get_market(int(trade["market_id"]))
        if not market_row:
            continue
        rd = market_row["resolve_date"]
        # Only settle markets whose resolve_date has passed.
        try:
            r_date = datetime.fromisoformat((rd or "").split("T")[0]).date()
        except (ValueError, AttributeError, TypeError):
            r_date = None
        if r_date is None or r_date >= today:
            continue

        strat = strategies.get(trade["strategy"])
        if strat is None:
            ledger.close_trade(int(trade["id"]), "VOID", 0.0)
            ledger.record_settlement(int(trade["market_id"]), None,
                                     "strategy retired", "VOID")
            skipped += 1
            continue

        market = _row_to_market(market_row)
        existing = ledger.get_settlement(int(trade["market_id"]))
        if existing:
            outcome = existing["outcome"]
            actual = existing["actual_value"]
            source = existing["source_value"]
            wu_val = existing["wu_value"] if "wu_value" in existing.keys() else None
        else:
            resolved = strat.resolve(market, datetime.now(timezone.utc).isoformat())
            if resolved is None:
                if verbose:
                    print(f"  ?? trade {trade['id']} ({trade['strategy']}): "
                          f"resolve() returned None for {market.slug}; "
                          f"leaving OPEN")
                continue
            outcome = resolved["outcome"]
            actual = resolved.get("actual_value")
            source = resolved.get("source_value", "")
            wu_val = resolved.get("wu_value")
            om_val = resolved.get("om_value")
            wu_source = resolved.get("wu_source", "")
            ledger.record_settlement(
                int(trade["market_id"]),
                float(actual) if actual is not None else None,
                source, outcome,
                om_value=float(om_val) if om_val is not None else None,
                wu_value=float(wu_val) if wu_val is not None else None,
                wu_source=wu_source,
            )
            if outcome == "DISPUTED" and verbose:
                print(f"  !! DISPUTED trade {trade['id']} ({trade['strategy']}) "
                      f"{market.slug}: {resolved.get('dispute_note', '')}; "
                      f"trade left OPEN")

        if outcome == "DISPUTED":
            # Don't close the trade. The settlement row is recorded; the
            # daily report surfaces the dispute. Human can flip the
            # settlement outcome to YES/NO/VOID by hand and re-run grade.
            skipped += 1
            continue

        pnl = _settle_trade_pnl(trade["side"], float(trade["price_filled"]),
                                float(trade["stake"]), float(trade["shares"]),
                                outcome)
        status = "VOID" if outcome == "VOID" else (
            "WIN" if trade["side"] == outcome else "LOSS"
        )
        ledger.close_trade(int(trade["id"]), status, pnl)
        settled += 1
        if verbose:
            extra = f" wu={wu_val}" if wu_val is not None else ""
            print(f"  Settled trade {trade['id']} ({trade['strategy']}) "
                  f"{trade['side']}@{trade['price_filled']:.3f} stake "
                  f"${trade['stake']:.2f} -> {outcome} ({status}) PnL "
                  f"${pnl:+.2f} actual={actual}{extra}")

    # Settle arb_positions whose end_date has passed.
    arb_settled, arb_skipped = grade_arb_positions(cfg, ledger, today, verbose=verbose)

    # Recompute per-strategy daily report rows.
    update_reports(ledger, cfg, today.isoformat(), verbose=verbose)

    return {"settled": settled, "skipped": skipped,
            "arb_settled": arb_settled, "arb_skipped": arb_skipped,
            "open_remaining": len(ledger.open_positions())}


def grade_arb_positions(cfg: dict, ledger: Ledger, today, verbose: bool = True
                          ) -> tuple[int, int]:
    """Settle every OPEN arb_position whose event has resolved.

    For each open position:
      - For every leg, query Gamma for the leg's conditionId. If the leg's
        `closed=True` and `outcomePrices[0]` (YES price) is 1.0, that leg
        is the winner. Else, the leg lost (NO would have paid $1).
      - In a MECE event, exactly one leg's YES resolves 1.0.
      - YES-side position: payout = shares (only winning leg's YES pays).
        NO-side position: payout = shares * (N - 1) (every losing leg's NO).
      - Net pnl = total_payout - total_cost.

    Requires the Gamma API to be reachable for each leg's conditionId. If
    any leg can't be confirmed resolved, we leave the position OPEN.
    """
    import requests
    settled = 0
    skipped = 0
    open_arbs = ledger.open_arb_positions()
    if verbose:
        print(f"Grader: {len(open_arbs)} open arb positions to evaluate.")
    gamma = (cfg.get("scanner") or {}).get(
        "gamma_url", "https://gamma-api.polymarket.com").rstrip("/")
    session = requests.Session()

    for pos in open_arbs:
        end = pos["end_date_iso"]
        if end:
            try:
                e_date = datetime.fromisoformat((end or "").replace("Z", "+00:00").split("T")[0]).date()
                if e_date >= today:
                    continue
            except (ValueError, TypeError):
                pass
        legs = ledger.arb_legs_for(int(pos["id"]))
        if not legs:
            continue
        # Look up every leg's resolution status.
        leg_outcomes = []
        all_resolved = True
        winner_idx = -1
        for i, leg in enumerate(legs):
            try:
                r = session.get(
                    f"{gamma}/markets",
                    params={"condition_ids": leg["market_id"]},
                    timeout=20,
                )
                data = r.json()
                m = data[0] if data else None
            except Exception:
                m = None
            if not m or not m.get("closed"):
                all_resolved = False
                leg_outcomes.append((leg, None, None))
                continue
            # Polymarket marks outcomePrices [YES, NO]; the winner is 1.
            try:
                op = m.get("outcomePrices")
                if isinstance(op, str):
                    op = json.loads(op)
                yes_price = float(op[0]) if op and len(op) > 0 else None
            except (TypeError, ValueError, json.JSONDecodeError):
                yes_price = None
            won = yes_price is not None and yes_price > 0.99
            leg_outcomes.append((leg, "YES" if won else "NO", yes_price))
            if won:
                if winner_idx >= 0:
                    # Two YES winners would mean Polymarket broke MECE -
                    # extremely unlikely but worth a loud bail.
                    if verbose:
                        print(f"  !! arb pos {pos['id']}: TWO winners found "
                              f"({legs[winner_idx]['leg_title']} and "
                              f"{leg['leg_title']}). Marking VOID.")
                    winner_idx = -2   # sentinel
                else:
                    winner_idx = i
        if not all_resolved:
            skipped += 1
            continue
        if winner_idx == -2:
            # MECE violation - VOID and refund cost.
            outcomes_for_close = []
            for leg, _, _ in leg_outcomes:
                outcomes_for_close.append({
                    "leg_id": int(leg["id"]), "outcome": "VOID", "payout": 0.0})
            ledger.close_arb_position(int(pos["id"]), "VOID",
                                       -float(pos["total_cost"]),
                                       outcomes_for_close)
            settled += 1
            continue
        if winner_idx == -1:
            # No winner found - probably means the event resolved with no
            # YES winner (rare but possible with negRisk). Refund.
            if verbose:
                print(f"  ?? arb pos {pos['id']} ({pos['event_slug']}): "
                      f"no winning leg detected; leaving OPEN")
            skipped += 1
            continue
        # Compute payouts per leg.
        outcomes_for_close = []
        total_payout = 0.0
        side = pos["side"]
        for i, (leg, outcome, _) in enumerate(leg_outcomes):
            if side == "YES":
                # We bought YES on every leg. Only winner pays $1/share.
                payout = float(leg["shares"]) if i == winner_idx else 0.0
            else:
                # We bought NO on every leg. Loser legs each pay $1/share.
                payout = float(leg["shares"]) if i != winner_idx else 0.0
            total_payout += payout
            outcomes_for_close.append({
                "leg_id": int(leg["id"]),
                "outcome": outcome,
                "payout": payout,
            })
        pnl = total_payout - float(pos["total_cost"])
        status = "CLOSED"
        ledger.close_arb_position(int(pos["id"]), status, pnl, outcomes_for_close)
        settled += 1
        if verbose:
            print(f"  Settled arb pos {pos['id']} ({pos['strategy']}) "
                  f"{side} {pos['event_slug']} shares={pos['shares']:.1f} "
                  f"cost=${pos['total_cost']:.2f} payout=${total_payout:.2f} "
                  f"PnL=${pnl:+.2f}")
    return settled, skipped


def update_reports(ledger: Ledger, cfg: dict, date_iso: str,
                   verbose: bool = True) -> None:
    paper = cfg.get("paper", {})
    starting = float(paper.get("starting_bankroll", 1000.0))
    strategies = _load_strategies(cfg)
    import sqlite3
    for sname in strategies:
        trades = ledger.all_trades(sname)
        closed = [t for t in trades if t["status"] in ("WIN", "LOSS", "VOID")]
        n_trades = len(closed)
        n_wins = sum(1 for t in closed if t["status"] == "WIN")
        pnl = sum(float(t["pnl"] or 0) for t in closed)
        # Roll in closed arb_positions for this strategy too.
        with sqlite3.connect(ledger.db_path) as c:
            c.row_factory = sqlite3.Row
            arbs = list(c.execute(
                """SELECT status, pnl FROM arb_positions
                   WHERE strategy=? AND status IN ('CLOSED','VOID')""",
                (sname,)).fetchall())
        arb_n = len(arbs)
        arb_wins = sum(1 for a in arbs if a["status"] == "CLOSED" and (a["pnl"] or 0) > 0)
        arb_pnl = sum(float(a["pnl"] or 0) for a in arbs)
        n_trades += arb_n
        n_wins += arb_wins
        pnl += arb_pnl
        brier = _brier(closed)
        bankroll = ledger.bankroll(sname, starting) + arb_pnl
        ledger.upsert_daily_report(date_iso, sname, n_trades, n_wins,
                                   pnl, brier, bankroll)
        # Surface disputed settlements that touch this strategy's open book.
        with sqlite3.connect(ledger.db_path) as c:
            c.row_factory = sqlite3.Row
            disputes = list(c.execute(
                """SELECT s.market_id, s.om_value, s.wu_value,
                          s.source_value, s.wu_source, m.slug, m.unit,
                          m.resolve_date
                   FROM settlements s
                   JOIN markets m ON m.id = s.market_id
                   WHERE s.outcome = 'DISPUTED'
                     AND s.market_id IN (
                       SELECT market_id FROM paper_trades
                       WHERE strategy = ? AND status = 'OPEN'
                     )""", (sname,)).fetchall())
        if verbose:
            br = f"{brier:.4f}" if brier is not None else "n/a"
            disp_n = len(disputes)
            disp_str = f"  DISPUTED={disp_n}" if disp_n else ""
            print(f"  report[{sname}] {date_iso}: n={n_trades} wins={n_wins} "
                  f"PnL=${pnl:+.2f} brier={br} bankroll=${bankroll:.2f}{disp_str}")
            for d in disputes:
                ov = d["om_value"]; wv = d["wu_value"]
                unit = d["unit"] or "?"
                ov_s = f"{ov:.2f}" if ov is not None else "n/a"
                wv_s = f"{wv:.2f}" if wv is not None else "n/a"
                print(f"    DISPUTED {d['slug']} ({d['resolve_date']}): "
                      f"OM={ov_s}{unit} vs WU={wv_s}{unit} "
                      f"-> human review at {d['wu_source']}")


def _brier(closed_trades: Iterable[sqlite3.Row]) -> float | None:
    """Brier score: average squared error between p_model and outcome.
    p_model is the p of the SIDE taken: if side==YES use p; if NO use 1-p.
    Coin-flipper baseline = 0.25. Target < 0.18.
    """
    deltas = []
    for t in closed_trades:
        if t["status"] == "VOID":
            continue
        p = float(t["p_model_at_entry"])
        if t["side"] == "NO":
            p = 1.0 - p
        # The "outcome" from the perspective of the side we took: 1 if WIN.
        y = 1.0 if t["status"] == "WIN" else 0.0
        deltas.append((p - y) ** 2)
    if not deltas:
        return None
    return sum(deltas) / len(deltas)
