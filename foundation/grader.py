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

from foundation.ledger import Ledger, ledger_from_cfg
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


def _backfill_wx_verifications(ledger: Ledger, verbose: bool = False) -> int:
    """For every market that already has a settlement row but no
    matching wx_verification row, run the logger once. Returns the
    count of rows backfilled."""
    n = 0
    with sqlite3.connect(ledger.ledger_path) as c:
        c.row_factory = sqlite3.Row
        rows = list(c.execute(
            """SELECT s.id AS settlement_id, s.market_id,
                      s.actual_value, s.om_value, s.wu_value,
                      s.source_value, s.outcome,
                      m.slug, m.question, m.category, m.resolve_date,
                      m.resolution_source, m.rules_text, m.threshold, m.unit
                 FROM settlements s
                 JOIN markets m ON m.id = s.market_id
                 LEFT JOIN wx_verification v ON v.market_row_id = s.market_id
                WHERE v.id IS NULL"""
        ).fetchall())
    for s in rows:
        # We need a trade-shaped object to reuse _log_wx_verification.
        # Synthesize the minimal columns it reads.
        trade_stub = {"strategy": "weather", "market_id": int(s["market_id"])}
        market_stub_cols = {"id": int(s["market_id"]),
                              "resolve_date": s["resolve_date"],
                              "slug": s["slug"]}
        try:
            _log_wx_verification(
                ledger,
                trade_stub,
                market_stub_cols,
                outcome=s["outcome"],
                om_val=s["om_value"], wu_val=s["wu_value"],
                source_value=s["source_value"] or "",
                settlement_id=int(s["settlement_id"]),
            )
            n += 1
        except Exception as exc:
            if verbose:
                print(f"  wx-verify backfill failed for market {s['market_id']}: {exc}")
    if verbose and n:
        print(f"  wx-verify backfilled {n} rows from existing settlements")
    return n


def _log_wx_verification(ledger: Ledger, trade_row, market_row,
                           outcome: str, om_val, wu_val, source_value: str,
                           settlement_id: int | None) -> None:
    """Write one wx_verification row for a resolved weather market.

    Idempotent on market_row_id (the ledger uses upsert). Tolerant of
    missing per-family fields in older signal metadata - those columns
    just stay NULL and the skill aggregation silently skips them.
    Truth value preference: WU > OM (WU is the actual market source).
    """
    if trade_row["strategy"] != "weather":
        return
    sig = ledger.latest_signal(int(trade_row["market_id"]), "weather")
    if sig is None:
        return
    try:
        meta = json.loads(sig["metadata_json"] or "{}")
    except (TypeError, json.JSONDecodeError):
        meta = {}
    city = meta.get("city")
    station = meta.get("station")
    unit = meta.get("unit")
    bound = meta.get("bound")
    lo = meta.get("lo")
    hi = meta.get("hi")
    threshold = lo if lo is not None else hi

    # Truth: prefer WU since that's the market source; else OM. Both
    # values + units are stored side-by-side for forensic dispute analysis.
    om_val_f = float(om_val) if om_val is not None else None
    wu_val_f = float(wu_val) if wu_val is not None else None
    if wu_val_f is not None:
        official_value = wu_val_f
    elif om_val_f is not None:
        official_value = om_val_f
    else:
        official_value = None
    # Units: the resolution sources return the SAME unit the market uses.
    om_unit = unit
    wu_unit = unit
    official_unit = unit

    # Per-family (gfs, ecmwf=ifs+aifs pooled). family_means / family_spreads
    # land in metadata only on estimates emitted by the post-v2.1 weather
    # code path; backfilled estimates leave them None and we accept that.
    family_p = meta.get("family_p") or {}
    family_means = meta.get("family_means") or {}
    family_spreads = meta.get("family_spreads") or {}
    family_n = meta.get("family_n") or {}

    def _ecmwf_pool(d):
        ifs = d.get("ifs"); aifs = d.get("aifs")
        if ifs is None and aifs is None:
            return None
        if ifs is None:
            return aifs
        if aifs is None:
            return ifs
        # ECMWF mean = member-count-weighted average of IFS + AIFS means.
        n_ifs = family_n.get("ifs", 0)
        n_aifs = family_n.get("aifs", 0)
        n_tot = (n_ifs + n_aifs) or 1
        return (n_ifs * ifs + n_aifs * aifs) / n_tot

    gfs_mean = family_means.get("gfs")
    gfs_spread = family_spreads.get("gfs")
    gfs_p = family_p.get("gfs")
    ecmwf_mean = _ecmwf_pool(family_means)
    ecmwf_spread = _ecmwf_pool(family_spreads)
    ecmwf_p = meta.get("p_ecmwf")

    def _err(mean, truth):
        if mean is None or truth is None:
            return None, None
        try:
            se = float(mean) - float(truth)
            return se, abs(se)
        except (TypeError, ValueError):
            return None, None

    gfs_se, gfs_ae = _err(gfs_mean, official_value)
    ecmwf_se, ecmwf_ae = _err(ecmwf_mean, official_value)

    # Lead time: from the signal's timestamp to the resolve_date midnight.
    lead = None
    try:
        sig_ts = datetime.fromisoformat(sig["ts"].replace("Z", "+00:00"))
        if sig_ts.tzinfo is None:
            sig_ts = sig_ts.replace(tzinfo=timezone.utc)
        rd = (market_row["resolve_date"] or "").split("T")[0]
        if rd:
            r_dt = datetime.fromisoformat(rd).replace(tzinfo=timezone.utc)
            lead = (r_dt - sig_ts).total_seconds() / 3600.0
    except (ValueError, TypeError, AttributeError):
        pass

    # Last-known market price (from snapshot near settlement)
    snap = ledger.latest_snapshot(int(trade_row["market_id"]))
    market_price = float(snap["yes_ask"]) if snap and snap["yes_ask"] is not None else None

    ledger.upsert_wx_verification({
        "market_row_id": int(trade_row["market_id"]),
        "city": city, "station": station,
        "threshold": threshold, "unit": unit, "bound": bound,
        "resolve_date": market_row["resolve_date"],
        "lead_time_hours": lead,
        "official_value": official_value, "official_value_unit": official_unit,
        "om_value": om_val_f, "om_value_unit": om_unit,
        "wu_value": wu_val_f, "wu_value_unit": wu_unit,
        "gfs_mean": gfs_mean, "gfs_spread": gfs_spread,
        "gfs_p_threshold": gfs_p,
        "gfs_signed_error": gfs_se, "gfs_abs_error": gfs_ae,
        "ecmwf_mean": ecmwf_mean, "ecmwf_spread": ecmwf_spread,
        "ecmwf_p_threshold": ecmwf_p,
        "ecmwf_signed_error": ecmwf_se, "ecmwf_abs_error": ecmwf_ae,
        "p_blended": float(sig["p_final"]) if sig["p_final"] is not None else None,
        "market_price": market_price,
        "outcome": outcome,
        "signal_id": int(sig["id"]),
        "settlement_id": settlement_id,
    })


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
    ledger = ledger_from_cfg(cfg)
    strategies = _load_strategies(cfg)
    today = datetime.now(timezone.utc).date()

    # Hard cap on how many resolutions any single grade run processes.
    # Each settlement does ~1 HTTP call (Wunderground / Open-Meteo /
    # Gamma) so the per-call latency drives runtime. With a per-run
    # cap, the grader is bounded; the next daily run picks up where
    # this one stopped.
    grade_cap = int((cfg.get("grader") or {}).get("max_settlements_per_run", 500))

    settled = 0
    skipped = 0
    open_rows = ledger.open_positions()
    if verbose:
        print(f"Grader: {len(open_rows)} open positions to evaluate "
              f"(cap {grade_cap} per run).")

    # Backfill wx_verifications for any weather paper_trade whose market
    # has already resolved. Idempotent (upsert by market_row_id) - cheap
    # to call every run and self-healing if earlier runs missed any.
    backfilled = _backfill_wx_verifications(ledger, verbose=verbose)

    for trade in open_rows:
        if settled >= grade_cap:
            if verbose:
                print(f"  grade cap {grade_cap} reached; deferring remaining "
                      f"{len(open_rows) - settled - skipped} positions")
            break
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
            settlement_id = ledger.record_settlement(
                int(trade["market_id"]),
                float(actual) if actual is not None else None,
                source, outcome,
                om_value=float(om_val) if om_val is not None else None,
                wu_value=float(wu_val) if wu_val is not None else None,
                wu_source=wu_source,
            )
            # Log the verification row right here - we have the freshest
            # OM + WU values in scope, and we want the row written even
            # for DISPUTED outcomes (the dispute forensics depend on it).
            try:
                _log_wx_verification(
                    ledger, trade, market_row, outcome,
                    om_val, wu_val, source, settlement_id)
            except Exception as exc:
                if verbose:
                    print(f"  wx-verify log failed for trade {trade['id']}: {exc}")
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

    # Settle arb_multi rows.
    multi_settled, multi_skipped = grade_arb_multi(cfg, ledger, today, verbose=verbose)

    # Settle cross-venue positions.
    cv_settled, cv_skipped = grade_cv_positions(cfg, ledger, today, verbose=verbose)

    # Settle cv_probe (quarantined research book). Same dual-venue
    # resolution as cv_positions but tracks agreement_outcome and writes
    # to cv_probe_positions instead.
    probe_settled, probe_skipped = grade_cv_probe_positions(
        cfg, ledger, today, verbose=verbose)

    # Settle shadow_trades (WeatherModel v2 challenger head-to-head book).
    # Both legs are paper-only -- no bankroll txns -- but the same fill
    # math drives champ_pnl + chal_pnl so the daily report can compare.
    shadow_settled, shadow_skipped = grade_shadow_trades(
        cfg, ledger, today, verbose=verbose)

    # Recompute per-strategy daily report rows.
    update_reports(ledger, cfg, today.isoformat(), verbose=verbose)

    return {"settled": settled, "skipped": skipped,
            "arb_settled": arb_settled, "arb_skipped": arb_skipped,
            "cv_settled": cv_settled, "cv_skipped": cv_skipped,
            "cv_probe_settled": probe_settled, "cv_probe_skipped": probe_skipped,
            "shadow_settled": shadow_settled, "shadow_skipped": shadow_skipped,
            "open_remaining": len(ledger.open_positions())}


def _verdict_from_wu(market_row, wu_val: float, kind: str
                       ) -> tuple[str, float, int]:
    """Recompute outcome for a single market using WU's value as truth.

    Returns (outcome 'YES'/'NO', truth_value, rounded_int). The market
    row carries parsed bound/lo/hi/unit in the markets table (or in the
    re-parsed `extras` we synthesize); we read what's available there.
    Round-half-up matches the resolver -- WU 73.5F rounds to 74.
    """
    import math
    unit = (market_row["unit"] if "unit" in market_row.keys() else "F") or "F"
    # WU value is stored in the market's display unit (the resolver kept
    # it that way), so no conversion needed.
    wu_round = math.floor(float(wu_val) + 0.5)
    # Pull bound/lo/hi out of the synthesized Market.extras the way the
    # original resolver does. We rebuild the Market here so we can lean
    # on the parser the resolver uses.
    m = _row_to_market(market_row)
    bound = m.extras.get("parsed_bound")
    lo = m.extras.get("lo")
    hi = m.extras.get("hi")
    if bound == "le":
        won = wu_round <= (hi if hi is not None else wu_round)
    elif bound == "ge":
        won = wu_round >= (lo if lo is not None else wu_round)
    elif bound == "eq":
        won = wu_round == (lo if lo is not None else wu_round)
    else:
        won = (
            (lo is None or wu_round >= lo)
            and (hi is None or wu_round <= hi)
        )
    return ("YES" if won else "NO", float(wu_val), int(wu_round))


def regrade_disputed_on_wu(cfg: dict, ledger: Ledger, today,
                              verbose: bool = True) -> dict:
    """Settle every DISPUTED weather settlement whose wu_value is
    populated. Polymarket settles on Wunderground; OM stays as a logged
    cross-check (om_value in settlements + wx_verification).

    For each DISPUTED row:
      1. Recompute YES/NO using WU rounded vs the market's bucket.
      2. UPDATE settlements: outcome, actual_value=wu_value,
         source_value='wunderground <station>'.
      3. Close every OPEN paper_trade attached to the market with the
         realized PnL.
      4. UPDATE wx_verification.outcome so the WX-VERIFY skill table
         picks up the now-finalized YES/NO instead of the stale DISPUTED.

    Idempotent: a settlement that's already YES/NO isn't touched.
    Returns a dict with per-position settlement detail for the report.
    """
    settled_rows: list[dict] = []
    # Read every DISPUTED + WU-present row through the ledger's own
    # connection so we don't leak a stale read lock from a parallel
    # sqlite3.connect handle that could defer the UPDATE commits below.
    with ledger._conn() as c:
        disputed = list(c.execute(
            """SELECT s.id AS settlement_id, s.market_id, s.actual_value,
                      s.om_value, s.wu_value, s.wu_source, s.source_value,
                      s.outcome,
                      m.slug, m.unit, m.resolve_date, m.question, m.category,
                      m.threshold, m.resolution_source, m.rules_text,
                      m.condition_id
                 FROM settlements s
                 JOIN markets m ON m.id = s.market_id
                WHERE s.outcome = 'DISPUTED'
                  AND s.wu_value IS NOT NULL"""
        ).fetchall())

    if verbose:
        print(f"Re-grading {len(disputed)} DISPUTED settlements on WU ...")
    for s in disputed:
        # Determine kind (max/min) from the market slug -- the resolver
        # writes "highest-temperature" / "lowest-temperature" tags into
        # the slug.
        kind = "min" if "lowest-temperature" in (s["slug"] or "") else "max"
        try:
            outcome, truth_val, wu_round = _verdict_from_wu(s, float(s["wu_value"]), kind)
        except Exception as exc:
            if verbose:
                print(f"  ! regrade failed for market {s['market_id']}: {exc}")
            continue
        # Identify the station note for source_value (best-effort).
        station = ""
        if s["wu_source"]:
            url = s["wu_source"]
            station = url.split("/daily/")[-1].split("/date/")[0] \
                if "/daily/" in url else url
        new_source = f"wunderground {station}".strip()

        # Apply the settlement update and close the linked trades. Use
        # an explicit commit at the end of the with-block to make sure
        # the UPDATEs land even if any later step in this row raises.
        with ledger._conn() as c:
            c.execute(
                """UPDATE settlements
                       SET outcome=?, actual_value=?, source_value=?
                     WHERE id=?""",
                (outcome, truth_val, new_source, int(s["settlement_id"])),
            )
            c.execute(
                """UPDATE wx_verification SET outcome=? WHERE settlement_id=?""",
                (outcome, int(s["settlement_id"])),
            )
            open_trades = list(c.execute(
                """SELECT id, side, price_filled, stake, shares, strategy
                     FROM paper_trades
                    WHERE market_id=? AND status='OPEN'""",
                (int(s["market_id"]),)).fetchall())
            c.commit()

        for tr in open_trades:
            pnl = _settle_trade_pnl(tr["side"], float(tr["price_filled"]),
                                    float(tr["stake"]), float(tr["shares"]),
                                    outcome)
            status = "WIN" if tr["side"] == outcome else "LOSS"
            ledger.close_trade(int(tr["id"]), status, pnl)
            settled_rows.append({
                "trade_id": int(tr["id"]),
                "strategy": tr["strategy"],
                "market_id": int(s["market_id"]),
                "slug": s["slug"],
                "resolve_date": s["resolve_date"],
                "side": tr["side"],
                "stake": float(tr["stake"]),
                "shares": float(tr["shares"]),
                "price_filled": float(tr["price_filled"]),
                "wu_value": float(s["wu_value"]),
                "om_value": float(s["om_value"]) if s["om_value"] is not None else None,
                "wu_round": wu_round,
                "outcome": outcome,
                "status": status,
                "pnl": pnl,
            })
            if verbose:
                print(f"  settled trade {tr['id']} ({tr['strategy']}) {tr['side']}"
                      f" stake=${tr['stake']:.2f} -> {outcome} ({status}) "
                      f"PnL=${pnl:+.2f}  (WU={s['wu_value']}, OM={s['om_value']})")

    if settled_rows:
        update_reports(ledger, cfg, today.isoformat(), verbose=False)
    return {"settled": settled_rows, "n_settlements": len(disputed)}


def grade_arb_multi(cfg: dict, ledger: Ledger, today, verbose: bool = True
                       ) -> tuple[int, int]:
    """Settle every OPEN arb_multi row whose event has resolved.

    arb_multi rows store leg_fills_json with each market_id; we look up
    each leg via Gamma /markets?condition_ids=... For YES side: one leg
    pays $1 per share; rest pay $0. For NO side: N-1 pay $1 per share.
    """
    import requests
    settled = 0
    skipped = 0
    open_rows = ledger.open_arb_multis()
    if verbose:
        print(f"Grader: {len(open_rows)} open arb_multi rows to evaluate.")
    gamma = (cfg.get("scanner") or {}).get(
        "gamma_url", "https://gamma-api.polymarket.com").rstrip("/")
    sess = requests.Session()
    for row in open_rows:
        end = row["end_date_iso"]
        if end:
            try:
                e_date = datetime.fromisoformat((end or "").replace("Z", "+00:00").split("T")[0]).date()
                if e_date >= today:
                    continue
            except (ValueError, TypeError):
                pass
        try:
            legs = json.loads(row["leg_fills_json"] or "[]")
        except (TypeError, json.JSONDecodeError):
            legs = []
        if not legs:
            continue
        winners = 0
        all_resolved = True
        for leg in legs:
            mid = leg.get("market_id")
            if not mid:
                all_resolved = False
                continue
            try:
                r = sess.get(f"{gamma}/markets",
                              params={"condition_ids": mid}, timeout=20)
                data = r.json()
                m = data[0] if data else None
            except Exception:
                m = None
            if not m or not m.get("closed"):
                all_resolved = False
                continue
            try:
                op = m.get("outcomePrices")
                if isinstance(op, str):
                    op = json.loads(op)
                yes_price = float(op[0]) if op else None
            except (TypeError, ValueError, json.JSONDecodeError):
                yes_price = None
            if yes_price is None:
                all_resolved = False
                continue
            if yes_price > 0.99:
                winners += 1
        if not all_resolved:
            skipped += 1
            continue
        side = row["side"]
        shares = float(row["shares"] or 0.0)
        total_cost = float(row["total_cost"])
        if side == "YES":
            payout = shares * 1.0 if winners >= 1 else 0.0
        else:
            payout = shares * (int(row["outcome_count"]) - winners)
        pnl = payout - total_cost
        status = "CLOSED"
        ledger.settle_arb_multi(int(row["id"]), status, pnl)
        settled += 1
        if verbose:
            print(f"  Settled arb_multi {row['id']} {side} {row['event_slug']} "
                  f"cost=${total_cost:.2f} payout=${payout:.2f} pnl=${pnl:+.2f}")
    return settled, skipped


def grade_cv_positions(cfg: dict, ledger: Ledger, today, verbose: bool = True
                         ) -> tuple[int, int]:
    """Settle every OPEN cv_position whose legs have all resolved.

    For each leg, query the leg's own venue for resolution:
      - Polymarket leg: Gamma /markets?condition_ids=... -> outcomePrices.
      - Kalshi leg: /markets/{ticker} -> result + status='settled'.

    A cross-venue arb pays $1 if our chosen side won on its venue, $0 if
    it lost. Net P&L = sum(payouts) - sum(costs). If the two venues
    SETTLE the same event differently (source divergence) - e.g.
    Polymarket says YES, Kalshi says NO for the same date - we may be
    on the unlucky side of both legs. The position is marked DIVERGED
    if both legs lose (the divergence risk materialized) and pnl =
    -total_cost.
    """
    import requests
    settled = 0
    skipped = 0
    open_cvs = ledger.open_cv_positions()
    if verbose:
        print(f"Grader: {len(open_cvs)} open cross-venue positions to evaluate.")
    gamma = (cfg.get("scanner") or {}).get(
        "gamma_url", "https://gamma-api.polymarket.com").rstrip("/")
    kalshi_base = "https://api.elections.kalshi.com/trade-api/v2"
    sess = requests.Session()

    for pos in open_cvs:
        legs = ledger.cv_legs_for(int(pos["id"]))
        if not legs:
            continue
        leg_outcomes = []
        all_resolved = True
        for leg in legs:
            payout = None
            outcome = None
            if leg["venue"] == "polymarket":
                try:
                    r = sess.get(f"{gamma}/markets",
                                  params={"condition_ids": leg["venue_market_id"]},
                                  timeout=20)
                    data = r.json()
                    m = data[0] if data else None
                except Exception:
                    m = None
                if not m or not m.get("closed"):
                    all_resolved = False
                else:
                    try:
                        op = m.get("outcomePrices")
                        if isinstance(op, str):
                            op = json.loads(op)
                        yes_price = float(op[0]) if op else None
                    except (TypeError, ValueError, json.JSONDecodeError):
                        yes_price = None
                    if yes_price is None:
                        all_resolved = False
                    else:
                        won = yes_price > 0.99
                        outcome = "YES" if won else "NO"
                        # Did the side we bought win?
                        side_won = (leg["side"] == outcome)
                        payout = float(leg["shares"]) if side_won else 0.0
            elif leg["venue"] == "kalshi":
                try:
                    r = sess.get(f"{kalshi_base}/markets/{leg['venue_market_id']}",
                                  timeout=20)
                    data = r.json()
                    m = data.get("market") if isinstance(data, dict) else None
                except Exception:
                    m = None
                if not m or m.get("status") != "settled":
                    all_resolved = False
                else:
                    # Kalshi result: 'yes' / 'no'
                    res_str = (m.get("result") or "").lower()
                    if res_str not in ("yes", "no"):
                        all_resolved = False
                    else:
                        outcome = res_str.upper()
                        side_won = (leg["side"] == outcome)
                        payout = float(leg["shares"]) if side_won else 0.0
            if payout is not None and outcome is not None:
                leg_outcomes.append({
                    "leg_id": int(leg["id"]),
                    "outcome": outcome,
                    "payout": payout,
                })
        if not all_resolved:
            skipped += 1
            continue
        total_payout = sum(L["payout"] for L in leg_outcomes)
        total_cost = float(pos["total_cost"])
        pnl = total_payout - total_cost
        # If both legs lost, the source-divergence risk materialized.
        all_lost = all(L["payout"] == 0.0 for L in leg_outcomes)
        status = "DIVERGED" if all_lost else "CLOSED"
        ledger.close_cv_position(int(pos["id"]), status, pnl, leg_outcomes)
        settled += 1
        if verbose:
            div_note = " (DIVERGED)" if status == "DIVERGED" else ""
            print(f"  Settled cv pos {pos['id']} ({pos['strategy']}) "
                  f"{pos['direction']} shares={pos['shares']:.1f} "
                  f"cost=${total_cost:.2f} payout=${total_payout:.2f} "
                  f"PnL=${pnl:+.2f}{div_note}")
    return settled, skipped


def _resolve_cv_leg(leg, sess, gamma: str, kalshi_base: str
                       ) -> tuple[str | None, float | None]:
    """Look up resolution for a single CV leg. Returns (outcome, payout).

    outcome is one of: 'YES', 'NO', 'VOID', or None if not yet resolved.
    payout is in dollars: shares if our side won, 0 if lost, or
    leg['cost'] if VOID (stake returned at cost).

    'VOID' is detected as:
      - Polymarket: `closed=True` but `outcomePrices` missing or not [1,0]/[0,1]
        (e.g. event invalidated). Conservative — most Polymarket markets
        resolve cleanly to YES/NO, so VOID is rare here.
      - Kalshi: status='settled' but result is empty or one of the
        documented void/invalidation strings.
    """
    if leg["venue"] == "polymarket":
        try:
            r = sess.get(f"{gamma}/markets",
                          params={"condition_ids": leg["venue_market_id"]},
                          timeout=20)
            data = r.json()
            m = data[0] if data else None
        except Exception:
            m = None
        if not m or not m.get("closed"):
            return None, None
        try:
            op = m.get("outcomePrices")
            if isinstance(op, str):
                op = json.loads(op)
        except (TypeError, ValueError, json.JSONDecodeError):
            op = None
        if not op or len(op) < 2:
            # Closed but no decisive prices -> treat as VOID.
            return "VOID", float(leg["cost"])
        try:
            yes_price = float(op[0])
            no_price = float(op[1])
        except (TypeError, ValueError):
            return "VOID", float(leg["cost"])
        if yes_price > 0.99:
            outcome = "YES"
        elif no_price > 0.99:
            outcome = "NO"
        else:
            return "VOID", float(leg["cost"])
        side_won = (leg["side"] == outcome)
        return outcome, float(leg["shares"]) if side_won else 0.0
    if leg["venue"] == "kalshi":
        try:
            r = sess.get(f"{kalshi_base}/markets/{leg['venue_market_id']}",
                          timeout=20)
            data = r.json()
            m = data.get("market") if isinstance(data, dict) else None
        except Exception:
            m = None
        if not m:
            return None, None
        status = (m.get("status") or "").lower()
        result = (m.get("result") or "").lower()
        if status != "settled":
            return None, None
        if result in ("yes", "no"):
            outcome = result.upper()
            side_won = (leg["side"] == outcome)
            return outcome, float(leg["shares"]) if side_won else 0.0
        # Kalshi voids settle with empty/'void'/'invalidated' results.
        if result in ("", "void", "voided", "invalidated", "cancelled", "canceled"):
            return "VOID", float(leg["cost"])
        # Unknown result string -> conservative VOID so the position
        # closes cleanly rather than hanging open forever.
        return "VOID", float(leg["cost"])
    return None, None


def _classify_cv_probe_agreement(leg_outcomes: list[dict]
                                    ) -> tuple[str, str | None]:
    """Compute (agreement_outcome, divergence_direction) for a probe
    position.

    agreement_outcome:
      AGREED         - both venues settled (YES/NO), exactly one leg paid.
      DIVERGED       - both settled but venues disagreed on the event.
      VOID_MISMATCH  - one venue VOID, the other settled YES/NO.
      BOTH_VOID      - both VOID.
    divergence_direction (only set when agreement_outcome=DIVERGED):
      BOTH_PAID      - venues disagreed AND we were on the right side of
                       each (windfall: ~$1+gap per share).
      NEITHER_PAID   - venues disagreed AND we were wrong on both
                       (catastrophe: ~$1-gap loss per share).
    Returns ("PENDING", None) if any leg has no resolution yet.

    In any POLY_YES_KAL_NO probe, AGREED means EXACTLY one of our two
    legs paid out (one venue settled YES and the other NO of the same
    event). 2 payers = both venues disagreed in our favor (windfall);
    0 payers = both disagreed against us (catastrophe)."""
    outs = [(L.get("outcome") or "").upper() for L in leg_outcomes]
    if not outs or any(not o for o in outs):
        return "PENDING", None
    if all(o == "VOID" for o in outs):
        return "BOTH_VOID", None
    if any(o == "VOID" for o in outs):
        return "VOID_MISMATCH", None
    payers = sum(1 for L in leg_outcomes if float(L.get("payout") or 0.0) > 0)
    if payers == 1:
        return "AGREED", None
    if payers == len(leg_outcomes):
        return "DIVERGED", "BOTH_PAID"
    return "DIVERGED", "NEITHER_PAID"


def grade_cv_probe_positions(cfg: dict, ledger: Ledger, today,
                                verbose: bool = True) -> tuple[int, int]:
    """Settle every OPEN cv_probe position whose legs have all resolved.

    Same dual-venue resolution as cv_positions, but records
    agreement_outcome and writes to cv_probe_positions. P&L is calculated
    identically (sum of leg payouts - sum of leg costs); for VOID legs we
    treat the venue as returning the stake at cost (payout=leg_cost),
    which makes a BOTH_VOID position settle at 0 P&L."""
    import requests
    settled = 0
    skipped = 0
    open_probes = ledger.list_open_cv_probe()
    if verbose:
        print(f"Grader: {len(open_probes)} open cv_probe positions to evaluate.")
    gamma = (cfg.get("scanner") or {}).get(
        "gamma_url", "https://gamma-api.polymarket.com").rstrip("/")
    kalshi_base = "https://api.elections.kalshi.com/trade-api/v2"
    sess = requests.Session()

    for pos in open_probes:
        legs = ledger.cv_probe_legs_for(int(pos["id"]))
        if not legs:
            continue
        leg_outcomes = []
        all_resolved = True
        for leg in legs:
            outcome, payout = _resolve_cv_leg(leg, sess, gamma, kalshi_base)
            if outcome is None or payout is None:
                all_resolved = False
                break
            leg_outcomes.append({
                "leg_id": int(leg["id"]),
                "outcome": outcome,
                "payout": payout,
            })
        if not all_resolved:
            skipped += 1
            continue
        total_payout = sum(L["payout"] for L in leg_outcomes)
        total_cost = float(pos["total_cost"])
        pnl = total_payout - total_cost
        agreement, divergence_direction = _classify_cv_probe_agreement(
            leg_outcomes)
        ledger.close_cv_probe_position(int(pos["id"]), agreement, pnl,
                                          leg_outcomes,
                                          divergence_direction=divergence_direction)
        settled += 1
        if verbose:
            dd = f"/{divergence_direction}" if divergence_direction else ""
            print(f"  Settled cv_probe pos {pos['id']} {pos['category']} "
                  f"{pos['direction']} -> {agreement}{dd} PnL=${pnl:+.2f}")
    return settled, skipped


def grade_shadow_trades(cfg: dict, ledger: Ledger, today,
                            verbose: bool = True) -> tuple[int, int]:
    """Settle every OPEN shadow_trades row whose market has a final
    settlement outcome. v2.3 WeatherModel head-to-head book.

    For each side (champion / challenger):
      - If the row has no side at all (NONE / null), pnl stays at 0 --
        the model declined to trade. Brier is still computed at report
        time from champ_p / chal_p.
      - Otherwise apply the same _settle_trade_pnl shape: a winning
        side returns shares - stake; a losing side returns -stake.
    """
    settled = 0
    skipped = 0
    open_rows = ledger.list_open_shadow_trades()
    if verbose:
        print(f"Grader: {len(open_rows)} open shadow_trades to evaluate.")

    def _side_pnl(side: str | None, price_filled, stake, shares,
                    outcome: str) -> float | None:
        if not side or side == "NONE" or stake is None or shares is None:
            return 0.0
        if outcome not in ("YES", "NO"):
            return None
        if side == outcome:
            return float(shares) - float(stake)
        return -float(stake)

    for row in open_rows:
        market_id = int(row["market_id"])
        settlement = ledger.get_settlement(market_id)
        if not settlement:
            skipped += 1
            continue
        outcome = settlement["outcome"]
        if outcome not in ("YES", "NO"):
            skipped += 1
            continue
        champ_pnl = _side_pnl(row["champ_side"], row["champ_price_filled"],
                                row["champ_stake"], row["champ_shares"],
                                outcome)
        chal_pnl = _side_pnl(row["chal_side"], row["chal_price_filled"],
                               row["chal_stake"], row["chal_shares"],
                               outcome)
        ledger.close_shadow_trade(int(row["id"]), outcome, champ_pnl, chal_pnl)
        settled += 1
    return settled, skipped


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
            cvs = list(c.execute(
                """SELECT status, pnl FROM cv_positions
                   WHERE strategy=? AND status IN ('CLOSED','VOID','DIVERGED')""",
                (sname,)).fetchall())
        arb_n = len(arbs)
        arb_wins = sum(1 for a in arbs if a["status"] == "CLOSED" and (a["pnl"] or 0) > 0)
        arb_pnl = sum(float(a["pnl"] or 0) for a in arbs)
        cv_n = len(cvs)
        cv_wins = sum(1 for a in cvs if a["status"] == "CLOSED" and (a["pnl"] or 0) > 0)
        cv_pnl = sum(float(a["pnl"] or 0) for a in cvs)
        n_trades += arb_n + cv_n
        n_wins += arb_wins + cv_wins
        pnl += arb_pnl + cv_pnl
        brier = _brier(closed)
        bankroll = ledger.bankroll(sname, starting) + arb_pnl + cv_pnl
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
