"""Generic edge engine + paper executor (foundation). Section 6 rules.

This module is strategy-agnostic: it takes (Market, Estimate, Strategy)
triples and decides whether to log a paper trade. Strategies never reach
into the order book themselves.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from strategies.base import Estimate, Market, Strategy


@dataclass
class FillResult:
    side: str                 # YES / NO
    vwap: float               # avg price across consumed levels
    price_filled: float       # vwap + slippage (capped at <0.99 / >0.01)
    stake: float              # USD risked
    shares: float             # stake / price_filled
    levels_consumed: list[dict]


@dataclass
class CycleDecision:
    market: Market
    strategy: str
    estimate: Estimate
    side: str | None          # YES, NO, or None
    edge: float | None        # POST-fill edge (== effective edge if we fill)
    p_model: float
    ask_used: float | None
    decision: str             # PENDING_FILL, FILLED, SKIP_*, NO_EDGE
    reason: str
    fill: FillResult | None = None
    market_row_id: int | None = None
    pre_fill_edge: float | None = None   # edge against top-of-book best ask


# ---------------------------------------------------------------- book walking
def walk_book(ask_levels: list[dict], stake_usd: float) -> tuple[float, list[dict]]:
    """Walk an ascending-price ask book consuming `stake_usd` of notional.

    Returns (vwap_price, levels_consumed). vwap = sum(price*shares)/sum(shares).
    levels_consumed = [{price, shares_taken, usd_taken}, ...].

    Each level has size in *shares* (each share pays out $1 if YES wins),
    so the USD needed to buy `size` shares at `price` = price * size.
    """
    if stake_usd <= 0 or not ask_levels:
        return float("nan"), []
    remaining_usd = float(stake_usd)
    consumed: list[dict] = []
    total_shares = 0.0
    total_usd = 0.0
    for lvl in ask_levels:
        price = float(lvl["price"])
        size_shares = float(lvl["size"])
        level_usd_cap = price * size_shares
        usd_here = min(remaining_usd, level_usd_cap)
        shares_here = usd_here / price if price > 0 else 0.0
        if shares_here <= 0:
            continue
        consumed.append({
            "price": price,
            "shares_taken": shares_here,
            "usd_taken": usd_here,
        })
        total_shares += shares_here
        total_usd += usd_here
        remaining_usd -= usd_here
        if remaining_usd <= 1e-9:
            break
    if total_shares <= 0:
        return float("nan"), []
    vwap = total_usd / total_shares
    # If the book couldn't absorb the full stake, the caller sees it
    # via sum(usd_taken) < stake. We deliberately do NOT pad with a
    # fake worse level - if depth is short we'd rather skip.
    return vwap, consumed


def kelly_fraction(p: float, price: float) -> float:
    """Standard binary Kelly: f* = (b*p - q) / b where b = (1-price)/price.

    Returns 0 if there's no edge. Cap at [0, 1] just in case.
    """
    if not (0 < price < 1) or not (0 < p < 1):
        return 0.0
    b = (1.0 - price) / price
    q = 1.0 - p
    f = (b * p - q) / b
    if not math.isfinite(f):
        return 0.0
    return max(0.0, min(1.0, f))


# ---------------------------------------------------------------- skip rules
def _hours_until(end_iso: str | None) -> float | None:
    if not end_iso:
        return None
    try:
        s = end_iso.replace("Z", "+00:00")
        end = datetime.fromisoformat(s)
        if end.tzinfo is None:
            end = end.replace(tzinfo=timezone.utc)
        delta = end - datetime.now(timezone.utc)
        return delta.total_seconds() / 3600.0
    except (ValueError, TypeError):
        return None


def _depth_within(levels: list[dict], best_ask: float,
                  slippage_band: float = 0.05) -> float:
    """USD of cumulative liquidity at ask prices within `slippage_band`
    of the best ask. Honest interpretation of 'book depth at ask': the
    book can absorb $X without us paying more than (best_ask + band).
    """
    if not levels:
        return 0.0
    cap = float(best_ask) + float(slippage_band)
    return sum(float(l["price"]) * float(l["size"])
               for l in levels if float(l["price"]) <= cap)


def _spread(ask: float | None, bid: float | None) -> float | None:
    if ask is None or bid is None:
        return None
    return float(ask) - float(bid)


# ---------------------------------------------------------------- executor
class Executor:
    def __init__(self, cfg: dict, ledger):
        p = cfg.get("paper", {})
        self.starting_bankroll = float(p.get("starting_bankroll", 1000.0))
        self.max_position_usd = float(p.get("max_position_usd", 50.0))
        self.max_open_positions = int(p.get("max_open_positions", 6))
        self.default_edge_threshold = float(p.get("default_edge_threshold", 0.08))
        self.default_kelly_fraction = float(p.get("default_kelly_fraction", 0.15))
        self.slippage = float(p.get("slippage_cents", 0.01))
        self.min_depth = float(p.get("min_book_depth_usd", 50.0))
        self.max_spread = float(p.get("max_spread_cents", 0.06))
        self.min_hours = float(p.get("min_hours_to_resolve", 2.0))
        # Longshot YES-side protection: when YES sells under `longshot_yes_ask_cap`,
        # any model miscalibration of even a few percent is huge in relative
        # terms. Require a tighter edge for those bets. NO-side and YES-side
        # at higher asks use `default_edge_threshold`.
        self.longshot_yes_ask_cap = float(p.get("longshot_yes_ask_cap", 0.15))
        self.longshot_yes_edge_threshold = float(
            p.get("longshot_yes_edge_threshold", 0.15))
        self.ledger = ledger

    def required_edge(self, side: str, ask: float | None,
                      base_threshold: float) -> float:
        """Return the edge a (side, ask) pair must clear. Asymmetric per
        section "Asymmetric edge threshold" in the README:
          * YES buys at ask < longshot_yes_ask_cap require longshot threshold
          * Everything else uses the strategy/base threshold
        """
        if side == "YES" and ask is not None and ask < self.longshot_yes_ask_cap:
            return max(base_threshold, self.longshot_yes_edge_threshold)
        return base_threshold

    def _strategy_param(self, strategy: Strategy, name: str, default: Any,
                        strategies_cfg: dict | None) -> Any:
        # Per-strategy override in config beats the foundation default.
        if strategies_cfg and strategy.name in strategies_cfg:
            v = strategies_cfg[strategy.name].get(name)
            if v is not None:
                return v
        if hasattr(strategy, name):
            v = getattr(strategy, name)
            if v is not None:
                return v
        return default

    def evaluate(self, market: Market, estimate: Estimate, strategy: Strategy,
                 strategies_cfg: dict | None = None) -> CycleDecision:
        """Run all foundation gating against (market, estimate). Does NOT
        record_trade and does NOT enforce max_open_positions - that's the
        cycle loop's job in Phase 2 after sorting by post-fill edge.

        Returns a CycleDecision with:
          decision = "PENDING_FILL" -> ready to commit; .fill is populated
                                       and .edge is the POST-fill edge
          decision = "NO_EDGE" / "SKIP_*" -> gated out; do not commit

        The edge field on a passing decision is the *post-fill* edge - the
        edge actually captured after walking the book + 1c slippage. The
        previous version gated on pre-fill (top-of-book) edge, which let
        thin-book fills slip below the threshold.
        """

        edge_threshold = float(
            self._strategy_param(strategy, "edge_threshold",
                                 self.default_edge_threshold, strategies_cfg)
        )
        kelly_frac = float(
            self._strategy_param(strategy, "kelly_fraction",
                                 self.default_kelly_fraction, strategies_cfg)
        )

        # --- pick the side. We trade whichever side the model favours most.
        p = float(estimate.p_final)
        yes_ask = market.yes_ask
        no_ask = market.no_ask
        # If we don't have a NO book, derive an implied NO ask = 1 - YES bid.
        derived_no_ask = None
        if no_ask is None and market.yes_bid is not None:
            derived_no_ask = round(1.0 - float(market.yes_bid), 4)

        yes_edge = (p - yes_ask) if yes_ask is not None else None
        no_p = 1.0 - p
        no_eff_ask = no_ask if no_ask is not None else derived_no_ask
        no_edge = (no_p - no_eff_ask) if no_eff_ask is not None else None

        candidates: list[tuple[str, float, float, list[dict], list[dict]]] = []
        if yes_edge is not None:
            candidates.append(("YES", yes_edge, yes_ask, market.yes_book, market.yes_book_bids))
        if no_edge is not None and market.no_book:
            candidates.append(("NO", no_edge, no_eff_ask, market.no_book, market.no_book_bids))
        candidates.sort(key=lambda c: c[1], reverse=True)

        if not candidates:
            return CycleDecision(market, strategy.name, estimate, None, None,
                                 p, None, "SKIP_NO_PRICES",
                                 "no ask price available on either side")

        # --- asymmetric pre-fill edge gate. Each side has its own threshold;
        # the YES-longshot rule raises it from 0.08 to 0.15 when YES ask < 0.15.
        # Pick the best side whose pre-edge passes ITS threshold, not just
        # the side with the highest raw pre-edge (which might be a longshot
        # YES that fails 0.15 while the NO side would have passed 0.08).
        scored = []
        for s, e, a, asks_, bids_ in candidates:
            req = self.required_edge(s, a, edge_threshold)
            scored.append((s, e, a, asks_, bids_, req, e - req))   # slack
        passing = [t for t in scored if t[1] >= t[5]]
        if not passing:
            # Diagnostic: report the closest miss.
            s, e, a, _, _, req, _ = max(scored, key=lambda t: t[6])
            return CycleDecision(market, strategy.name, estimate, s, e, p,
                                 a, "NO_EDGE",
                                 f"{s} edge {e:.3f} < threshold {req:.3f} "
                                 f"(asym: YES<{self.longshot_yes_ask_cap:g} "
                                 f"needs {self.longshot_yes_edge_threshold:g})",
                                 pre_fill_edge=e)
        # Of the passing sides, take the highest pre-fill edge.
        passing.sort(key=lambda t: t[1], reverse=True)
        side, pre_edge, ask, asks, bids, side_threshold, _ = passing[0]

        hours = _hours_until(market.end_date_iso or market.resolve_date)
        if hours is not None and hours < self.min_hours:
            return CycleDecision(market, strategy.name, estimate, side, pre_edge, p,
                                 ask, "SKIP_TIME",
                                 f"resolves in {hours:.2f}h < {self.min_hours}h",
                                 pre_fill_edge=pre_edge)

        depth_within = _depth_within(asks, ask)
        if depth_within < self.min_depth:
            return CycleDecision(market, strategy.name, estimate, side, pre_edge, p,
                                 ask, "SKIP_DEPTH",
                                 f"depth within 5c of ask ${depth_within:.2f} < ${self.min_depth}",
                                 pre_fill_edge=pre_edge)

        # Spread is measured on the side we're buying.
        if side == "YES":
            sp = _spread(market.yes_ask, market.yes_bid)
        else:
            sp = _spread(market.no_ask, market.no_bid)
        if sp is not None and sp > self.max_spread:
            return CycleDecision(market, strategy.name, estimate, side, pre_edge, p,
                                 ask, "SKIP_SPREAD",
                                 f"spread {sp:.3f} > {self.max_spread:.3f}",
                                 pre_fill_edge=pre_edge)

        # One position per market per day per strategy (sec 6).
        # upsert_market is idempotent (UNIQUE on condition_id) so the
        # double-call from main.py's snapshot step is harmless.
        market_row_id = self.ledger.upsert_market({
            "condition_id": market.market_id,
            "slug": market.slug,
            "question": market.question,
            "category": market.category,
            "threshold": market.extras.get("parsed_threshold")
                or market.extras.get("threshold"),
            "unit": market.extras.get("parsed_unit") or market.extras.get("unit"),
            "resolve_date": market.resolve_date,
            "resolution_source": market.extras.get("station_url"),
            "rules_text": market.rules_text,
        })
        today = datetime.now(timezone.utc).date().isoformat()
        if self.ledger.already_traded_today(market_row_id, strategy.name, today):
            return CycleDecision(market, strategy.name, estimate, side, pre_edge, p,
                                 ask, "SKIP_DUPLICATE",
                                 "already have a trade in this market today",
                                 pre_fill_edge=pre_edge)

        # --- sizing
        bankroll = self.ledger.bankroll(strategy.name, self.starting_bankroll)
        side_p = p if side == "YES" else (1 - p)
        f_kelly = kelly_fraction(side_p, ask)
        confidence = max(0.0, min(1.0, float(estimate.confidence)))
        raw_stake = bankroll * kelly_frac * f_kelly * confidence
        stake = min(raw_stake, self.max_position_usd)
        if stake < 1.0:
            return CycleDecision(market, strategy.name, estimate, side, pre_edge, p,
                                 ask, "SKIP_TINY_STAKE",
                                 f"sized stake ${stake:.2f} < $1.00 (kelly={f_kelly:.3f})",
                                 pre_fill_edge=pre_edge)

        # --- walk the book honestly
        vwap, levels = walk_book(asks, stake)
        consumed_usd = sum(l["usd_taken"] for l in levels)
        if consumed_usd < stake * 0.99 or math.isnan(vwap):
            return CycleDecision(market, strategy.name, estimate, side, pre_edge, p,
                                 ask, "SKIP_THIN_BOOK",
                                 f"book absorbed only ${consumed_usd:.2f} of ${stake:.2f}",
                                 pre_fill_edge=pre_edge)

        # 1c adverse slippage on top of VWAP; clamp inside [0.01, 0.99].
        price_filled = min(0.99, vwap + self.slippage)
        shares = stake / price_filled

        # --- POST-fill edge check using the SAME (possibly asymmetric)
        # threshold we used pre-fill. For longshot YES this is 0.15, for
        # everything else 0.08. Walking the book + 1c slip can drop a 0.08
        # pre-edge below threshold (trade #13 root cause), and on YES
        # longshots a few extra cents of slippage is the difference between
        # +EV and -EV.
        post_edge = side_p - price_filled
        if post_edge < side_threshold:
            return CycleDecision(market, strategy.name, estimate, side, post_edge, p,
                                 ask, "NO_EDGE_POST_FILL",
                                 f"{side} post-fill edge {post_edge:.4f} "
                                 f"(pre {pre_edge:.4f}) < threshold "
                                 f"{side_threshold:.3f} after VWAP {vwap:.4f} "
                                 f"+ {self.slippage:.2f}c slip",
                                 pre_fill_edge=pre_edge)

        fill = FillResult(
            side=side, vwap=vwap, price_filled=price_filled,
            stake=stake, shares=shares, levels_consumed=levels,
        )
        return CycleDecision(
            market, strategy.name, estimate, side, post_edge, p, ask,
            "PENDING_FILL",
            f"would fill {side} @ {price_filled:.4f} (vwap {vwap:.4f}, "
            f"stake ${stake:.2f}, shares {shares:.2f}, edge {post_edge:.4f})",
            fill=fill,
            market_row_id=market_row_id,
            pre_fill_edge=pre_edge,
        )

    def commit(self, decision: CycleDecision) -> CycleDecision:
        """Write a PENDING_FILL decision's trade to the ledger. Idempotent
        in spirit: caller should only invoke for decisions whose status is
        PENDING_FILL."""
        if decision.decision != "PENDING_FILL" or decision.fill is None \
                or decision.market_row_id is None:
            return decision
        fill = decision.fill
        trade_id = self.ledger.record_trade(
            market_id=decision.market_row_id,
            strategy=decision.strategy,
            side=fill.side,
            price_filled=fill.price_filled,
            stake=fill.stake,
            shares=fill.shares,
            p_model_at_entry=decision.p_model,
            edge_at_entry=float(decision.edge or 0.0),   # POST-fill edge
            levels_consumed=fill.levels_consumed,
        )
        decision.decision = "FILLED"
        decision.reason = (
            f"trade #{trade_id} {fill.side} @ {fill.price_filled:.4f} "
            f"(vwap {fill.vwap:.4f}, stake ${fill.stake:.2f}, "
            f"shares {fill.shares:.2f}, edge {decision.edge:.4f})"
        )
        return decision

    # --- back-compat one-shot kept for tests; main.py uses evaluate+commit.
    def consider(self, market: Market, estimate: Estimate, strategy: Strategy,
                 strategies_cfg: dict | None = None) -> CycleDecision:
        d = self.evaluate(market, estimate, strategy, strategies_cfg)
        # Old behaviour enforced the cap mid-loop. Preserve it here.
        if d.decision == "PENDING_FILL":
            open_n = len(self.ledger.open_positions())
            if open_n >= self.max_open_positions:
                d.decision = "SKIP_MAX_OPEN"
                d.reason = (f"{open_n} open positions >= "
                            f"cap {self.max_open_positions}")
                return d
            self.commit(d)
        return d
