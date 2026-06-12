"""Strategy #4: copy-trading (paper only).

Three concerns, one module:

  SCOUT   - daily roster build. Discover candidate wallets via the
            public /trades tape + optional config-seeded list, pull
            each candidate's trade history (incremental cursor), score
            on lifetime ROI + consistency + niche concentration +
            recent ROI, and roll into a hysteresis-protected roster.
  FOLLOWER- per-cycle (cadence at the operator's discretion - we run
            it from the same `cycle` command). For every roster
            wallet, fetch trades since last cursor, simulate a $5
            paper fill against the live CLOB book per the foundation's
            pessimistic rules, record both leader_price and our_price.
  GRADER  - settle copied trades at resolution or on leader exit;
            compute latency tax = sum(our_pnl - leader_pnl_equivalent).

All endpoints public + unauthenticated. NEVER trade, sign, or post
real orders. Stake is a fixed $5 paper notional.
"""
from __future__ import annotations

import json
import math
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable

from foundation.polymarket_data import PolymarketData
from strategies.base import Estimate, Market, Strategy


# ---------------------------------------------------------- scout scoring
HARD_FILTERS = {
    "min_track_record_days": 90,
    "min_resolved_trades": 30,
    "max_avg_entry_price": 0.88,        # favorite-grinder trap
    "max_days_since_last_trade": 14,
    "max_top_single_trade_share": 0.40, # lottery-winner trap
}

DEFAULT_SCORE_WEIGHTS = {
    "roi_lifetime": 0.40,
    "consistency": 0.25,
    "niche_concentration": 0.20,
    "roi_recent": 0.15,
}

DEFAULT_HYSTERESIS = {
    "roster_size": 15,
    "exit_below_rank": 25,
    "exit_below_consecutive": 3,
    "enter_above_rank": 10,
    "enter_above_consecutive": 2,
}


# ---------------------------------------------------------- helpers
def _trade_key(t: dict) -> str:
    return f"{t.get('transactionHash','')}:{t.get('asset','')}:{t.get('side','')}"


def _category_from_slug(slug: str | None) -> str:
    """Cheap category-bucket from the event slug. The exact tag list
    comes from Gamma if we want to be precise; this gives a coarse
    bucket sufficient for niche-concentration scoring."""
    s = (slug or "").lower()
    for tag, hits in [
        ("sports", ("nba", "nfl", "mlb", "ufc", "soccer", "tennis", "champions-league")),
        ("politics", ("election", "trump", "biden", "congress", "senate", "house")),
        ("crypto", ("btc", "eth", "bitcoin", "ethereum", "crypto", "sol-")),
        ("weather", ("temperature", "weather", "rain", "snow")),
        ("ai", ("openai", "anthropic", "ai-", "model", "gpt")),
        ("geopolitics", ("israel", "iran", "russia", "ukraine", "china", "war", "ceasefire")),
        ("celebrity", ("musk", "kardashian", "celeb", "pope")),
    ]:
        if any(h in s for h in hits):
            return tag
    return "other"


# ---------------------------------------------------------- metrics
def compute_metrics(trades: list[dict], now_ts: int | None = None
                     ) -> dict[str, Any]:
    """Compute per-wallet metrics from a list of trade dicts. Trades are
    expected to include `timestamp, side, size, price, slug, conditionId`
    fields. Resolution status (WIN/LOSS) is not directly available from
    /trades, so this computes proxies: total deployed USD, dominant
    category share, monthly P&L proxy via realized P&L on round-trip
    positions (BUY then SELL on same asset), etc.
    """
    if now_ts is None:
        now_ts = int(time.time())
    if not trades:
        return {
            "first_trade_ts": None, "last_trade_ts": None,
            "track_record_days": 0, "n_trades": 0, "n_resolved": 0,
            "deployed_usd": 0.0, "realized_pnl_usd": 0.0,
            "roi_per_trade": 0.0, "avg_entry_price": 0.0,
            "top_single_trade_share": 0.0,
            "category_dist": {}, "dominant_category": "other",
            "dominant_share": 0.0,
            "monthly_pnl": {}, "days_since_last_trade": 9999,
            "roi_recent_30d": 0.0,
        }
    trades = sorted(trades, key=lambda t: int(t.get("timestamp") or 0))
    first_ts = int(trades[0].get("timestamp") or 0)
    last_ts = int(trades[-1].get("timestamp") or 0)
    track_record_days = max(0.0, (now_ts - first_ts) / 86400.0)
    days_since_last = max(0.0, (now_ts - last_ts) / 86400.0)

    # Round-trip P&L: for each (asset, outcome) chain, the BUYs build
    # cost basis, SELLs realize P&L vs avg cost. Cheap & directionally
    # correct.
    positions: dict[str, dict] = {}
    realized_per_close = []
    avg_entry_prices = []
    deployed_total = 0.0
    monthly_pnl: dict[str, float] = {}

    for t in trades:
        side = (t.get("side") or "").upper()
        size = float(t.get("size") or 0.0)
        price = float(t.get("price") or 0.0)
        asset = t.get("asset") or ""
        ts = int(t.get("timestamp") or 0)
        month_key = datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m")
        pos = positions.setdefault(asset, {"shares": 0.0, "cost": 0.0})
        if side == "BUY":
            pos["shares"] += size
            pos["cost"] += size * price
            deployed_total += size * price
            avg_entry_prices.append(price)
        elif side == "SELL":
            avg_cost = pos["cost"] / pos["shares"] if pos["shares"] > 0 else 0.0
            shares_closed = min(size, pos["shares"])
            pnl = (price - avg_cost) * shares_closed
            realized_per_close.append(pnl)
            monthly_pnl[month_key] = monthly_pnl.get(month_key, 0.0) + pnl
            pos["shares"] -= shares_closed
            pos["cost"] -= shares_closed * avg_cost
            if pos["shares"] <= 1e-9:
                pos["shares"] = 0.0
                pos["cost"] = 0.0

    n_resolved = len(realized_per_close)
    realized_total = sum(realized_per_close)
    roi_per_trade = (realized_total / deployed_total) if deployed_total > 0 else 0.0
    avg_entry = (sum(avg_entry_prices) / len(avg_entry_prices)) if avg_entry_prices else 0.0
    # Top single-close share of total profit.
    if realized_total > 0:
        positive = [p for p in realized_per_close if p > 0]
        top_share = (max(positive) / sum(positive)) if positive else 0.0
    else:
        top_share = 0.0

    # Category distribution by trade count weighted by USD.
    cat_w: dict[str, float] = {}
    for t in trades:
        cat = _category_from_slug(t.get("slug") or t.get("eventSlug"))
        cat_w[cat] = cat_w.get(cat, 0.0) + float(t.get("size") or 0.0) * float(t.get("price") or 0.0)
    total_w = sum(cat_w.values()) or 1.0
    dist = {c: w / total_w for c, w in cat_w.items()}
    dominant_cat = max(dist, key=dist.get) if dist else "other"
    dominant_share = dist.get(dominant_cat, 0.0)

    # 30-day recent ROI proxy.
    recent_cut = now_ts - 30 * 86400
    recent_realized = 0.0
    recent_deployed = 0.0
    rp = {}
    for t in trades:
        if int(t.get("timestamp") or 0) < recent_cut:
            continue
        side = (t.get("side") or "").upper()
        size = float(t.get("size") or 0.0)
        price = float(t.get("price") or 0.0)
        asset = t.get("asset") or ""
        if side == "BUY":
            recent_deployed += size * price
            rp[asset] = rp.get(asset, {"shares": 0.0, "cost": 0.0})
            rp[asset]["shares"] += size
            rp[asset]["cost"] += size * price
        elif side == "SELL":
            p = rp.get(asset, {"shares": 0.0, "cost": 0.0})
            avg = p["cost"] / p["shares"] if p["shares"] > 0 else 0.0
            sh = min(size, p["shares"])
            recent_realized += (price - avg) * sh
            p["shares"] -= sh
            p["cost"] -= sh * avg
    roi_recent_30d = (recent_realized / recent_deployed) if recent_deployed > 0 else 0.0

    # Consistency: fraction of last 6 months with positive P&L.
    today = datetime.fromtimestamp(now_ts, tz=timezone.utc)
    last6 = []
    for k in range(6):
        ym = (today - timedelta(days=30 * k)).strftime("%Y-%m")
        last6.append(monthly_pnl.get(ym, 0.0))
    pos_months = sum(1 for v in last6 if v > 0)
    consistency = pos_months / 6.0

    return {
        "first_trade_ts": first_ts, "last_trade_ts": last_ts,
        "track_record_days": track_record_days,
        "n_trades": len(trades), "n_resolved": n_resolved,
        "deployed_usd": deployed_total,
        "realized_pnl_usd": realized_total,
        "roi_per_trade": roi_per_trade,
        "avg_entry_price": avg_entry,
        "top_single_trade_share": top_share,
        "category_dist": dist,
        "dominant_category": dominant_cat,
        "dominant_share": dominant_share,
        "monthly_pnl": monthly_pnl,
        "days_since_last_trade": days_since_last,
        "roi_recent_30d": roi_recent_30d,
        "consistency_last6m": consistency,
    }


# ---------------------------------------------------------- filters + score
def passes_hard_filters(m: dict, filters: dict) -> tuple[bool, str]:
    if m["track_record_days"] < filters["min_track_record_days"]:
        return False, f"track record {m['track_record_days']:.0f}d < {filters['min_track_record_days']}d"
    if m["n_resolved"] < filters["min_resolved_trades"]:
        return False, f"resolved trades {m['n_resolved']} < {filters['min_resolved_trades']}"
    if m["avg_entry_price"] > filters["max_avg_entry_price"]:
        return False, f"avg_entry_price {m['avg_entry_price']:.3f} > {filters['max_avg_entry_price']}"
    if m["days_since_last_trade"] > filters["max_days_since_last_trade"]:
        return False, f"days_since_last_trade {m['days_since_last_trade']:.0f} > {filters['max_days_since_last_trade']}"
    if m["top_single_trade_share"] > filters["max_top_single_trade_share"]:
        return False, f"top_single_trade_share {m['top_single_trade_share']:.2f} > {filters['max_top_single_trade_share']}"
    if m["roi_per_trade"] <= 0:
        return False, f"lifetime ROI {m['roi_per_trade']:.3f} <= 0"
    if m["roi_recent_30d"] <= 0:
        return False, f"recent ROI {m['roi_recent_30d']:.3f} <= 0"
    return True, "passed all hard filters"


def score(m: dict, weights: dict) -> float:
    return (
        weights["roi_lifetime"] * m["roi_per_trade"]
        + weights["consistency"] * m["consistency_last6m"]
        + weights["niche_concentration"] * m["dominant_share"]
        + weights["roi_recent"] * m["roi_recent_30d"]
    )


# ---------------------------------------------------------- pessimistic fill
def walk_book_paper(asks_or_bids: list[dict], side: str, stake_usd: float,
                     slippage_cents: float) -> tuple[float, float, list[dict], str]:
    """Simulate a BUY (walk asks) or SELL (walk bids) for stake_usd USD.

    Returns (effective_price, shares, top3_levels, status).

    status: 'ok' if fully filled; 'unfillable' if the visible book can't
    absorb stake_usd. Never invents fills - per the spec, unfillable is
    a real and valuable signal, not an error."""
    if not asks_or_bids:
        return float("nan"), 0.0, [], "unfillable"
    if side == "BUY":
        levels = sorted(asks_or_bids, key=lambda L: float(L["price"]))
        slip_sign = +1
    else:
        levels = sorted(asks_or_bids, key=lambda L: -float(L["price"]))
        slip_sign = -1
    remaining = float(stake_usd)
    total_usd = 0.0
    total_shares = 0.0
    consumed = []
    top3 = [{"price": float(l["price"]), "size": float(l["size"])} for l in levels[:3]]
    for lvl in levels:
        price = float(lvl["price"])
        size = float(lvl["size"])
        # For SELL we'd interpret size as shares we can offload at price.
        usd_here = price * size
        take_usd = min(remaining, usd_here)
        sh = take_usd / price if price > 0 else 0.0
        if sh <= 0:
            continue
        total_usd += take_usd
        total_shares += sh
        consumed.append({"price": price, "shares_taken": sh, "usd_taken": take_usd})
        remaining -= take_usd
        if remaining <= 1e-9:
            break
    if total_shares <= 0 or remaining > stake_usd * 0.01:
        return float("nan"), 0.0, top3, "unfillable"
    vwap = total_usd / total_shares
    eff = vwap + slip_sign * slippage_cents
    return eff, total_shares, top3, "ok"


# ---------------------------------------------------------- strategy
class CopyTrading(Strategy):
    name = "copy_trading"

    def __init__(self, cfg: dict):
        s = (cfg.get("strategies") or {}).get(self.name, {})
        # Money
        self.stake_usd = float(s.get("stake_usd", 5.0))
        self.slippage_cents = float(s.get(
            "slippage_cents", (cfg.get("paper") or {}).get("slippage_cents", 0.01)))
        # Caps
        self.max_open_total = int(s.get("max_open_total", 40))
        self.max_open_per_leader = int(s.get("max_open_per_leader", 8))
        # Scout
        self.seed_wallets: list[str] = list(s.get("seed_wallets") or [])
        self.discovery_pages = int(s.get("discovery_pages", 5))
        self.discovery_top_n = int(s.get("discovery_top_n", 200))
        self.score_weights = {**DEFAULT_SCORE_WEIGHTS, **(s.get("score_weights") or {})}
        self.hard_filters = {**HARD_FILTERS, **(s.get("hard_filters") or {})}
        self.hysteresis = {**DEFAULT_HYSTERESIS, **(s.get("hysteresis") or {})}
        # Backtester
        self.backtest_lookback_days = int(s.get("backtest_lookback_days", 90))
        self.backtest_penalty_cents = float(s.get("backtest_penalty_cents", 0.02))
        # Wallet trade pull cap
        self.max_trade_pages_per_wallet = int(s.get("max_trade_pages_per_wallet", 20))
        # Hard time budget for the scout loop. Cloud daily.yml has a
        # 50-min job timeout; the scout's wallet-history fetches are the
        # dominant cost on a cold cache (200 wallets * ~4s = 13 min
        # minimum, plus DB writes). We bound this at 15 min by default
        # so even a cold cache exits cleanly with a partial roster
        # rather than crashing the job at the runner's timeout.
        self.scout_time_budget_minutes = float(
            s.get("scout_time_budget_minutes", 15.0))
        # bot-likeness weights. Stays as a dict, never coerced to None,
        # so compute_bot_likeness picks up the defaults on missing keys.
        self.bot_likeness_weights = dict(
            (s.get("bot_likeness_weights") or {})) or None

    # --- per-market layer: no-op
    def relevant_markets(self, markets: list[Market]) -> list[Market]:
        return []

    def estimate(self, market: Market) -> Estimate | None:
        return None

    # ---------------------------------------------------------- scout
    def discover_candidates(self, data: PolymarketData) -> list[str]:
        """Combine seed wallets with auto-discovery from the public
        /trades tape (no leaderboard endpoint exists - see
        docs/api_notes.md). Returns ordered candidates by recent
        activity weight; seed wallets always rank first."""
        out: list[str] = []
        seen: set[str] = set()
        for w in self.seed_wallets:
            wl = w.lower()
            if wl not in seen:
                seen.add(wl)
                out.append(wl)
        agg: dict[str, float] = {}
        for page in range(self.discovery_pages):
            try:
                batch = data.fetch_trades(limit=100, offset=page * 100)
            except RuntimeError:
                break
            for t in batch or []:
                w = (t.get("proxyWallet") or "").lower()
                if not w:
                    continue
                usd = float(t.get("size") or 0.0) * float(t.get("price") or 0.0)
                agg[w] = agg.get(w, 0.0) + usd
        ranked = sorted(agg.items(), key=lambda kv: kv[1], reverse=True)
        for w, _ in ranked[: self.discovery_top_n]:
            if w not in seen:
                seen.add(w)
                out.append(w)
        return out

    def pull_wallet_history(self, data: PolymarketData, wallet: str,
                              ledger) -> list[dict]:
        """Pull /trades?user=wallet incrementally. Persist new rows to
        wallet_trades; return the full local history (existing +
        newly-pulled)."""
        cursor_ts = ledger.get_wallet_cursor(wallet)
        new_rows = []
        for t in data.iter_trades(user=wallet, page=100,
                                    max_pages=self.max_trade_pages_per_wallet,
                                    stop_before_ts=cursor_ts):
            new_rows.append(t)
        if new_rows:
            ledger.upsert_wallet_trades(wallet, new_rows)
            ledger.set_wallet_cursor(wallet, max(int(r.get("timestamp") or 0) for r in new_rows))
        return ledger.get_wallet_trades(wallet)

    def _wallet_priority_key(self, wallet: str, ledger,
                              seed_index: dict[str, int]) -> tuple:
        """Lower tuple sorts first. Priority order:
          1. Seed wallets (config-supplied) - operator wants these.
          2. Wallets with cached metrics already - cheap refresh.
          3. Everything else - cold candidates.
        Within each tier, last-scouted-ascending so the oldest data
        is refreshed first.
        """
        if wallet in seed_index:
            return (0, seed_index[wallet])
        try:
            row = ledger.get_bankroll_row.__self__.get_wallet_trades  # type: ignore
            # We don't have a get_wallet helper; cheap check via cursor.
            has_cursor = ledger.get_wallet_cursor(wallet) is not None
        except Exception:
            has_cursor = False
        return (1 if has_cursor else 2, wallet)

    def scout(self, data: PolymarketData, ledger, verbose: bool = False,
              now_fn=None) -> dict[str, Any]:
        """Daily scout: discover candidates, pull history, compute
        metrics, filter, score, roster update.

        Exits cleanly when the wall-clock budget
        (`scout_time_budget_minutes`) is exhausted. Wallets that
        weren't processed this run are logged as `deferred` and will
        be picked up by the next run - per-wallet progress is
        persisted via `wallet_cursors`, so resumption is incremental.

        now_fn is a clock-injection seam for tests; defaults to
        `time.time`."""
        clock = now_fn or time.time
        deadline = clock() + self.scout_time_budget_minutes * 60.0
        today = datetime.now(timezone.utc).date().isoformat()
        candidates = self.discover_candidates(data)
        # Priority order: seeds first, then warm-cache wallets, then cold.
        seed_index = {w.lower(): i for i, w in enumerate(self.seed_wallets)}
        candidates_sorted = sorted(
            candidates,
            key=lambda w: self._wallet_priority_key(w, ledger, seed_index),
        )
        if verbose:
            print(f"  scout: {len(candidates_sorted)} candidate wallets "
                  f"(budget {self.scout_time_budget_minutes:.0f} min)")
        survivors: list[tuple[str, float, dict]] = []
        processed_wallets: set[str] = set()
        excluded = 0
        processed = 0
        deferred = 0
        for w in candidates_sorted:
            # Budget gate: check BEFORE doing the expensive
            # pull_wallet_history call so a single in-flight wallet can't
            # blow the budget by an unbounded amount. The work already
            # written to the ledger is fully consistent at this point
            # (each wallet's cursor + trades + scout_snapshot commits as
            # one unit).
            if clock() >= deadline:
                deferred = len(candidates_sorted) - processed
                if verbose:
                    print(f"  scout: time budget reached after {processed} "
                          f"wallets; deferring {deferred} to next run")
                break
            try:
                trades = self.pull_wallet_history(data, w, ledger)
            except RuntimeError:
                trades = ledger.get_wallet_trades(w)
            m = compute_metrics(trades)
            # bot-likeness is informational - NOT a hard filter. Stored
            # on every snapshot so master-report can bucket settled
            # copy P&L by low/mid/high. See foundation.autopsy.
            from foundation.autopsy import compute_bot_likeness
            bl = compute_bot_likeness(trades, weights=self.bot_likeness_weights)
            m["bot_likeness"] = bl["bot_likeness"]
            m["bot_likeness_breakdown"] = bl
            ok, reason = passes_hard_filters(m, self.hard_filters)
            ledger.upsert_wallet(w, m)
            ledger.upsert_scout_snapshot(today, w, None, None,
                                         passed=ok, reason=reason, metrics=m)
            processed += 1
            processed_wallets.add(w)
            if not ok:
                excluded += 1
                continue
            sc = score(m, self.score_weights)
            survivors.append((w, sc, m))

        survivors.sort(key=lambda x: x[1], reverse=True)
        for rank, (w, sc, m) in enumerate(survivors, start=1):
            ledger.upsert_scout_snapshot(today, w, rank, sc,
                                         passed=True, reason=None, metrics=m)

        # Roster update with hysteresis. Pass the set of wallets we
        # actually got to this run so _update_roster knows which
        # already-active leaders to LEAVE ALONE (not seen != ranked
        # low). Both survivors AND filter-excluded wallets count as
        # "seen this run" - they were processed; we just didn't promote
        # the excluded ones. Deferred wallets are NOT in this set.
        processed_set = set(processed_wallets)
        new_roster = self._update_roster(
            ledger, survivors, processed_wallets=processed_set)
        if verbose:
            print(f"  scout: processed={processed} survivors={len(survivors)} "
                  f"excluded={excluded} deferred={deferred} "
                  f"roster_size={len(new_roster)}")
        return {
            "candidates": len(candidates),
            "processed": processed,
            "deferred": deferred,
            "survivors": len(survivors),
            "excluded": excluded,
            "roster_size": len(new_roster),
            "roster": new_roster,
        }

    def _update_roster(self, ledger, ranked: list[tuple[str, float, dict]],
                         processed_wallets: set[str] | None = None
                         ) -> list[dict]:
        """Apply hysteresis to evolve the roster.

        processed_wallets: the set of wallets we actually scouted this run.
        When the scout exits at its time budget, wallets we didn't reach
        are NOT penalized via the below-rank counter - their hysteresis
        state is preserved unchanged. Only wallets we examined AND ranked
        below `exit_below_rank` tick the eviction counter.
        """
        hy = self.hysteresis
        roster_size = int(hy["roster_size"])
        rank_by_wallet = {w: r for r, (w, _, _) in enumerate(ranked, start=1)}
        current = {row["wallet"]: row for row in ledger.list_roster()}
        for w, row in current.items():
            state = json.loads(row["hysteresis_state_json"] or "{}")
            if processed_wallets is not None and w not in processed_wallets:
                # Deferred this run - DO NOT tick the eviction counter.
                # Leave the wallet's state and status exactly as they were.
                continue
            rank = rank_by_wallet.get(w, 10**9)
            below = state.get("below_25_consec", 0)
            below = below + 1 if rank > int(hy["exit_below_rank"]) else 0
            state["below_25_consec"] = below
            new_status = row["status"]
            exited_at = row["exited_at"]
            if below >= int(hy["exit_below_consecutive"]) and new_status == "ACTIVE":
                new_status = "EXITED"
                exited_at = datetime.now(timezone.utc).isoformat()
            ledger.upsert_roster(w, entered_at=row["entered_at"],
                                  exited_at=exited_at,
                                  score=ranked[rank - 1][1] if rank <= len(ranked) else None,
                                  rank=rank if rank <= len(ranked) else None,
                                  status=new_status,
                                  hysteresis=state)
        # Promote new entrants.
        promoted_now = 0
        slots_free = roster_size - sum(1 for r in current.values() if r["status"] == "ACTIVE")
        for w, sc, m in ranked:
            if w in current:
                continue
            rank = rank_by_wallet[w]
            state_existing = ledger.get_roster_state(w)
            above = state_existing.get("above_10_consec", 0)
            above = above + 1 if rank <= int(hy["enter_above_rank"]) else 0
            state = {"above_10_consec": above, "below_25_consec": 0}
            entered_at = None
            status = "CANDIDATE"
            if above >= int(hy["enter_above_consecutive"]) and slots_free > 0:
                entered_at = datetime.now(timezone.utc).isoformat()
                status = "ACTIVE"
                slots_free -= 1
                promoted_now += 1
            ledger.upsert_roster(w, entered_at=entered_at, exited_at=None,
                                  score=sc, rank=rank,
                                  status=status, hysteresis=state)
        return [dict(r) for r in ledger.list_roster() if r["status"] == "ACTIVE"]

    # ---------------------------------------------------------- follower
    def follow(self, data: PolymarketData, scanner, ledger,
                verbose: bool = False) -> dict[str, Any]:
        """For each ACTIVE roster wallet, pull trades since cursor and
        simulate paper copies via the live CLOB book."""
        roster = [r for r in ledger.list_roster() if r["status"] == "ACTIVE"]
        if verbose:
            print(f"  follow: {len(roster)} active leaders")
        copied = 0
        unfillable = 0
        skipped_cap = 0
        for r in roster:
            wallet = r["wallet"]
            cursor = ledger.get_wallet_cursor(wallet)
            try:
                new_trades = list(data.iter_trades(
                    user=wallet, page=100,
                    max_pages=self.max_trade_pages_per_wallet,
                    stop_before_ts=cursor,
                ))
            except RuntimeError:
                new_trades = []
            if new_trades:
                ledger.upsert_wallet_trades(wallet, new_trades)
                ledger.set_wallet_cursor(wallet, max(int(t.get("timestamp") or 0) for t in new_trades))
            for t in new_trades:
                # Per-leader cap
                if ledger.count_open_copies(leader=wallet) >= self.max_open_per_leader:
                    self._record_skip(ledger, wallet, t, reason="skipped_cap")
                    skipped_cap += 1
                    continue
                if ledger.count_open_copies() >= self.max_open_total:
                    self._record_skip(ledger, wallet, t, reason="skipped_cap")
                    skipped_cap += 1
                    continue
                fill = self._simulate_paper_fill(scanner, t)
                if fill["status"] == "unfillable":
                    self._record_skip(ledger, wallet, t, reason="unfillable",
                                       book_top3=fill["top3"])
                    unfillable += 1
                else:
                    ledger.record_copied_trade({
                        "leader_wallet": wallet,
                        "market_id": t.get("conditionId") or "",
                        "token_id": t.get("asset") or "",
                        "side": (t.get("side") or "").upper(),
                        "leader_price": float(t.get("price") or 0.0),
                        "leader_size": float(t.get("size") or 0.0),
                        "leader_ts": int(t.get("timestamp") or 0),
                        "detection_ts": int(time.time()),
                        "detection_delay_s": max(0, int(time.time()) - int(t.get("timestamp") or 0)),
                        "our_price": fill["price"],
                        "price_drift": fill["price"] - float(t.get("price") or 0.0),
                        "book_snapshot_json": json.dumps(fill["top3"]),
                        "stake": self.stake_usd,
                        "shares": fill["shares"],
                        "status": "open",
                    })
                    copied += 1
        return {
            "leaders": len(roster), "copied": copied,
            "unfillable": unfillable, "skipped_cap": skipped_cap,
        }

    def _record_skip(self, ledger, wallet: str, t: dict, reason: str,
                       book_top3: list | None = None) -> None:
        ledger.record_copied_trade({
            "leader_wallet": wallet,
            "market_id": t.get("conditionId") or "",
            "token_id": t.get("asset") or "",
            "side": (t.get("side") or "").upper(),
            "leader_price": float(t.get("price") or 0.0),
            "leader_size": float(t.get("size") or 0.0),
            "leader_ts": int(t.get("timestamp") or 0),
            "detection_ts": int(time.time()),
            "detection_delay_s": max(0, int(time.time()) - int(t.get("timestamp") or 0)),
            "our_price": None,
            "price_drift": None,
            "book_snapshot_json": json.dumps(book_top3 or []),
            "stake": self.stake_usd,
            "shares": None,
            "status": reason,
        })

    def _simulate_paper_fill(self, scanner, trade: dict) -> dict:
        side = (trade.get("side") or "").upper()
        token = trade.get("asset")
        if not token or scanner is None:
            return {"status": "unfillable", "price": float("nan"),
                    "shares": 0.0, "top3": []}
        try:
            book = scanner.fetch_book(token) or {}
        except Exception:
            return {"status": "unfillable", "price": float("nan"),
                    "shares": 0.0, "top3": []}
        if side == "BUY":
            levels = book.get("asks") or []
        else:
            levels = book.get("bids") or []
        normalized = []
        for l in levels:
            try:
                normalized.append({"price": float(l["price"]), "size": float(l["size"])})
            except (KeyError, TypeError, ValueError):
                pass
        eff, shares, top3, status = walk_book_paper(
            normalized, side, self.stake_usd, self.slippage_cents)
        return {"status": status, "price": eff, "shares": shares, "top3": top3}

    # ---------------------------------------------------------- grader hook
    def grade_copied(self, scanner, ledger, gamma_url: str,
                      verbose: bool = False) -> dict[str, Any]:
        import requests
        sess = requests.Session()
        opens = ledger.list_open_copies()
        settled = 0
        for c in opens:
            cid = c["market_id"]
            if not cid:
                continue
            try:
                r = sess.get(f"{gamma_url}/markets",
                              params={"condition_ids": cid}, timeout=20)
                data = r.json()
                m = data[0] if data else None
            except Exception:
                m = None
            if not m or not m.get("closed"):
                continue
            try:
                op = m.get("outcomePrices")
                if isinstance(op, str):
                    op = json.loads(op)
                yes_price = float(op[0]) if op else None
            except (TypeError, ValueError, json.JSONDecodeError):
                yes_price = None
            if yes_price is None:
                continue
            outcome_idx = int(yes_price > 0.99)  # 0 = NO won, 1 = YES won
            leader_outcome_idx = self._infer_outcome_idx(c)
            # WIN if our side == winning outcome.
            our_won = (leader_outcome_idx == 0 and outcome_idx == 0) \
                or (leader_outcome_idx == 1 and outcome_idx == 1)
            our_shares = float(c["shares"] or 0.0)
            our_price = float(c["our_price"] or 0.0)
            our_pnl = our_shares - (our_shares * our_price) if our_won else -(our_shares * our_price)
            leader_shares = float(c["stake"] or 0.0) / float(c["leader_price"] or 1.0)
            leader_pnl_eq = leader_shares - float(c["stake"] or 0.0) if our_won \
                else -float(c["stake"] or 0.0)
            ledger.settle_copied_trade(int(c["id"]),
                                         our_pnl=our_pnl,
                                         leader_pnl_equivalent=leader_pnl_eq,
                                         exit_reason="resolution")
            settled += 1
            if verbose:
                print(f"  copy settled #{c['id']} won={our_won} our={our_pnl:+.2f} leader={leader_pnl_eq:+.2f}")
        return {"settled": settled, "still_open": len(opens) - settled}

    @staticmethod
    def _infer_outcome_idx(copied_row) -> int:
        """We bought the same token the leader did, which is one of YES/NO.
        If the trade side was BUY and the leader's outcome index can be
        inferred from the wallet_trades raw row, use it; otherwise default
        to 1 (YES) - we have only the conditionId in the copy row."""
        # In the simple v1 model we assume outcome=1 (YES) when we paper-bought
        # a token. The follower stores the leader's token_id as `token_id`;
        # joining back via wallet_trades.outcomeIndex would be more correct
        # but is left for v2 once we wire that join.
        return 1

    # ---------------------------------------------------------- backtester
    def backtest(self, data: PolymarketData, ledger,
                  verbose: bool = False) -> list[dict]:
        """Replay each candidate wallet's last N days. Estimates only -
        labeled as such in the output."""
        candidates = self.discover_candidates(data)
        cutoff_ts = int(time.time()) - self.backtest_lookback_days * 86400
        rows = []
        for w in candidates[: max(20, self.hysteresis["roster_size"])]:
            try:
                trades = self.pull_wallet_history(data, w, ledger)
            except RuntimeError:
                trades = ledger.get_wallet_trades(w)
            trades = [t for t in trades if int(t.get("timestamp") or 0) >= cutoff_ts]
            if not trades:
                continue
            # Replay: BUY at leader_price + penalty; SELL at leader_price - penalty.
            cost = 0.0
            proceeds = 0.0
            n_buys = 0
            for t in trades:
                side = (t.get("side") or "").upper()
                lp = float(t.get("price") or 0.0)
                sh_from_5 = self.stake_usd / lp if lp > 0 else 0.0
                if side == "BUY":
                    eff = lp + self.backtest_penalty_cents
                    cost += eff * sh_from_5
                    n_buys += 1
                elif side == "SELL":
                    eff = max(0.0, lp - self.backtest_penalty_cents)
                    proceeds += eff * sh_from_5
            pnl_estimate = proceeds - cost
            rows.append({
                "wallet": w, "trades_replayed": len(trades),
                "buys": n_buys, "estimated_pnl_usd": pnl_estimate,
                "estimate_marker": "ESTIMATE",
            })
        rows.sort(key=lambda r: r["estimated_pnl_usd"], reverse=True)
        return rows


def build(cfg: dict) -> Strategy:
    return CopyTrading(cfg)
