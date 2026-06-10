"""Strategy #2: bucket-sum arbitrage detector.

Polymarket negRisk events are mutually exclusive collectively exhaustive
(MECE) sets of binary outcomes - exactly one leg resolves YES and pays $1
per share. Two free-money arbs follow:

  YES-side: if SUM(YES ask) across every leg < $1 - safety_buffer, buying
            one share of YES on every leg locks ($1 - cost) per share.
  NO-side:  if SUM(NO ask) across every leg < $(N-1) - safety_buffer,
            buying one share of NO on every leg locks ($(N-1) - cost). The
            N-1 legs whose YES loses each pay $1 on NO.

The strategy operates in two layers:

  1. DETECTOR (always runs, even on sub-threshold gaps): walk every leg's
     book for a common share count, compute the true all-in cost, log the
     gap to arb_gaps with all metadata. Built and trusted first.

  2. PAPER EXECUTOR (fires only when locked profit clears
     `min_arb_profit`): paper-log the multi-leg trade as a single linked
     arb_position with all legs, settled together at resolution.

Pessimistic fill rules match the rest of the foundation: walk the book
level-by-level, 1c slippage per leg, never midpoint. Skips any event
where MECE completeness isn't confirmable (negRisk flag + all legs
deployed) - an incomplete set produces fake arb signals.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from strategies.base import ArbEvent, ArbLeg, Estimate, Market, Strategy


# --------------------------------------------------------- per-leg book walk
def walk_book_for_shares(asks: list[dict], target_shares: float
                          ) -> tuple[float, float, list[dict]]:
    """Walk an ascending ask book consuming `target_shares` shares.

    Returns (vwap, shares_filled, levels_consumed). If the book can't
    absorb the full target, shares_filled is what it could fill.

    levels_consumed = [{price, shares_taken, usd_taken}, ...].
    """
    if target_shares <= 0 or not asks:
        return float("nan"), 0.0, []
    remaining = float(target_shares)
    total_usd = 0.0
    total_shares = 0.0
    consumed: list[dict] = []
    for lvl in asks:
        price = float(lvl["price"])
        size = float(lvl["size"])
        take = min(remaining, size)
        if take <= 0:
            continue
        usd = take * price
        consumed.append({
            "price": price,
            "shares_taken": take,
            "usd_taken": usd,
        })
        total_usd += usd
        total_shares += take
        remaining -= take
        if remaining <= 1e-9:
            break
    if total_shares <= 0:
        return float("nan"), 0.0, []
    return total_usd / total_shares, total_shares, consumed


# --------------------------------------------------------- pre-filter
def gamma_yes_sum(legs: list[ArbLeg]) -> tuple[float | None, int]:
    """Sum of best YES asks from the Gamma snapshot. Returns (sum, n_missing)."""
    s = 0.0
    missing = 0
    for leg in legs:
        if leg.gamma_yes_ask is None:
            missing += 1
        else:
            s += leg.gamma_yes_ask
    return s if missing == 0 else None, missing


def gamma_no_sum(legs: list[ArbLeg]) -> tuple[float | None, int]:
    """Sum of implied NO asks = sum(1 - bestBid). None if any bid missing."""
    s = 0.0
    missing = 0
    for leg in legs:
        if leg.gamma_yes_bid is None:
            missing += 1
        else:
            s += (1.0 - leg.gamma_yes_bid)
    return s if missing == 0 else None, missing


# --------------------------------------------------------- detection
@dataclass
class ArbDetection:
    """One detected gap. Always logged; only paper-traded if cleared_threshold."""
    event: ArbEvent
    side: str                              # YES or NO
    walk_mode: str                         # gamma_only | full_book
    target_shares: float
    executable_shares: float               # min fillable across legs
    sum_vwap_per_share: float              # sum of leg VWAPs
    slippage_per_share: float              # n_legs * slippage_cents
    safety_buffer: float
    payout_per_share: float                # 1.0 (YES) or n_legs - 1 (NO)
    locked_profit_per_share: float
    locked_profit_usd: float
    cleared_threshold: bool
    legs_detail: list[dict] = field(default_factory=list)


class BucketSumArb(Strategy):
    """Strategy contract used at the per-market layer is a no-op for this
    arb strategy - the work happens at the event level via scan_arb().
    main.py routes any strategy that implements scan_arb() through the
    event-walking path instead of the per-market estimate path."""

    name = "bucket_arb"

    def __init__(self, cfg: dict):
        s = (cfg.get("strategies") or {}).get(self.name, {})
        # Knobs requested in the spec:
        self.safety_buffer = float(s.get("safety_buffer", 0.005))
        self.min_arb_profit = float(s.get("min_arb_profit", 1.0))     # USD
        # Per-event sizing target. Capped by per-leg book depth.
        self.target_shares = float(s.get("target_shares", 100.0))
        # Pre-filter band: walk full books only when gamma-snapshot sum is
        # within this much of the arb threshold on either side. Saves
        # ~30k CLOB calls on the universe.
        self.walk_band = float(s.get("walk_band", 0.10))
        # Per-leg slippage (cents). Independent from foundation slippage so
        # arb can use its own value if desired.
        self.slippage_cents = float(s.get(
            "slippage_cents", (cfg.get("paper") or {}).get("slippage_cents", 0.01)))
        # Sides to detect / execute.
        self.detect_yes = bool(s.get("detect_yes", True))
        self.detect_no = bool(s.get("detect_no", True))
        self.execute_yes = bool(s.get("execute_yes", True))
        self.execute_no = bool(s.get("execute_no", True))
        # Skip events resolving in less than this many hours.
        self.min_hours_to_resolve = float(s.get("min_hours_to_resolve", 1.0))
        # Minimum executable shares to bother logging a full-walk gap.
        self.min_executable_shares = float(s.get("min_executable_shares", 5.0))
        # Hard cap on how many full-book events we walk per cycle (safety).
        self.max_walks_per_cycle = int(s.get("max_walks_per_cycle", 250))

    # --- per-market path is a no-op for this strategy ---------------------
    def relevant_markets(self, markets: list[Market]) -> list[Market]:
        return []

    def estimate(self, market: Market) -> Estimate | None:
        return None

    # --- pre-filter on cheap Gamma snapshot --------------------------------
    def gamma_prefilter(self, event: ArbEvent) -> tuple[bool, bool, str]:
        """Returns (walk_yes, walk_no, reason). walk_X True iff that side's
        Gamma snapshot sum is within walk_band of the arb threshold."""
        if not event.completeness_verified:
            return False, False, f"completeness: {event.completeness_note}"
        n = len(event.legs)
        ys, miss_y = gamma_yes_sum(event.legs)
        ns, miss_n = gamma_no_sum(event.legs)
        walk_yes = False
        walk_no = False
        notes = []
        if self.detect_yes and ys is not None:
            # YES arb requires sum < 1.0. Add band for snapshot lag.
            if ys < 1.0 + self.walk_band:
                walk_yes = True
            notes.append(f"yes_sum_gamma={ys:.3f}")
        elif self.detect_yes:
            notes.append(f"yes_sum_gamma=na ({miss_y} missing asks)")
        if self.detect_no and ns is not None:
            if ns < (n - 1) + self.walk_band:
                walk_no = True
            notes.append(f"no_sum_gamma={ns:.3f}")
        elif self.detect_no:
            notes.append(f"no_sum_gamma=na ({miss_n} missing bids)")
        return walk_yes, walk_no, "; ".join(notes)

    # --- detector core ----------------------------------------------------
    def detect_side(self, event: ArbEvent, side: str) -> ArbDetection | None:
        """Book-walk every leg for `target_shares` on `side`. Returns an
        ArbDetection (always - even negative profit) so the caller can log
        the gap. Returns None only on pathological inputs (no books)."""
        n = len(event.legs)
        if n < 2:
            return None
        # Per-leg walk: get vwap and fillable shares.
        per_leg_vwap = []
        per_leg_filled = []
        per_leg_levels = []
        leg_details = []
        for leg in event.legs:
            asks = leg.yes_asks if side == "YES" else leg.no_asks
            vwap, filled, levels = walk_book_for_shares(asks, self.target_shares)
            per_leg_vwap.append(vwap)
            per_leg_filled.append(filled)
            per_leg_levels.append(levels)
            leg_details.append({
                "market_id": leg.market_id,
                "leg_title": leg.leg_title,
                "token_id": leg.yes_token_id if side == "YES" else leg.no_token_id,
                "vwap": None if math.isnan(vwap) else vwap,
                "shares_fillable": filled,
                "depth_usd": sum(l["usd_taken"] for l in levels),
                "levels_consumed": levels,
            })
        # Need every leg fillable, otherwise no arb possible.
        if any(math.isnan(v) for v in per_leg_vwap):
            return None
        if any(f <= 0 for f in per_leg_filled):
            return None
        executable = min(per_leg_filled)
        if executable < self.min_executable_shares:
            return None
        # Re-compute the per-leg cost at the executable share count (since a
        # short leg implies less depth on every other leg's walk too -
        # vwap may be flatter when only filling `executable` shares).
        sum_vwap = 0.0
        for i, leg in enumerate(event.legs):
            asks = leg.yes_asks if side == "YES" else leg.no_asks
            v2, f2, lv2 = walk_book_for_shares(asks, executable)
            if math.isnan(v2) or f2 < executable - 1e-9:
                return None
            sum_vwap += v2
            leg_details[i]["vwap"] = v2
            leg_details[i]["shares_fillable"] = f2
            leg_details[i]["levels_consumed"] = lv2
            leg_details[i]["depth_usd"] = sum(l["usd_taken"] for l in lv2)
        slippage_total = n * self.slippage_cents
        payout = 1.0 if side == "YES" else float(n - 1)
        # locked profit per share = payout - all-in cost - safety_buffer.
        profit_per_share = payout - sum_vwap - slippage_total - self.safety_buffer
        profit_usd = profit_per_share * executable
        cleared = profit_usd >= self.min_arb_profit and profit_per_share > 0
        return ArbDetection(
            event=event,
            side=side,
            walk_mode="full_book",
            target_shares=self.target_shares,
            executable_shares=executable,
            sum_vwap_per_share=sum_vwap,
            slippage_per_share=slippage_total,
            safety_buffer=self.safety_buffer,
            payout_per_share=payout,
            locked_profit_per_share=profit_per_share,
            locked_profit_usd=profit_usd,
            cleared_threshold=cleared,
            legs_detail=leg_details,
        )

    # --- public scan entry point -------------------------------------------
    def scan_arb(self, events: list[ArbEvent], scanner=None,
                  verbose: bool = False) -> dict[str, Any]:
        """Run the detector across the event universe.

        For each event:
          1. Verify MECE completeness (negRisk + all legs deployed).
          2. Cheap pre-filter on Gamma bestAsk sums.
          3. If pre-filter passes either side, fetch full books and walk.
             Caller passes `scanner` to enable the lazy book fetch.
          4. Emit ArbDetection per side detected.

        Returns a dict with `detections` (full-walk only), and counters
        for diagnostics (scanned, complete, gamma-only sub, walked, etc).
        """
        out_detections: list[ArbDetection] = []
        gamma_only_gaps: list[dict] = []   # for logging coarse distribution

        scanned = 0
        complete = 0
        incomplete = 0
        walked = 0
        gamma_only_recorded = 0
        skipped_time = 0
        skipped_cap = 0
        prefilter_pass_yes = 0
        prefilter_pass_no = 0

        now = datetime.now(timezone.utc)

        for ev in events:
            scanned += 1
            if not ev.completeness_verified:
                incomplete += 1
                continue
            complete += 1

            # Skip events resolving too soon to act on.
            end = ev.end_date_iso
            if end:
                try:
                    s = end.replace("Z", "+00:00")
                    dt = datetime.fromisoformat(s)
                    if dt.tzinfo is None:
                        dt = dt.replace(tzinfo=timezone.utc)
                    hours = (dt - now).total_seconds() / 3600.0
                    if hours < self.min_hours_to_resolve:
                        skipped_time += 1
                        continue
                except (ValueError, TypeError):
                    pass

            walk_yes, walk_no, note = self.gamma_prefilter(ev)
            if walk_yes:
                prefilter_pass_yes += 1
            if walk_no:
                prefilter_pass_no += 1

            # If neither side is close to arb, log a gamma-only summary so
            # we still have distribution data on the no-arb majority.
            if not walk_yes and not walk_no:
                ys, _ = gamma_yes_sum(ev.legs)
                ns, _ = gamma_no_sum(ev.legs)
                for side_name, snap_sum, payout in (
                    ("YES", ys, 1.0),
                    ("NO", ns, len(ev.legs) - 1.0),
                ):
                    if snap_sum is None:
                        continue
                    if side_name == "YES" and not self.detect_yes:
                        continue
                    if side_name == "NO" and not self.detect_no:
                        continue
                    # Per-share profit on the snapshot, no slippage applied
                    # because we didn't walk. Just records "this is how far
                    # the snapshot is from arb".
                    profit_ps = payout - snap_sum - self.safety_buffer
                    gamma_only_gaps.append({
                        "event": ev,
                        "side": side_name,
                        "snap_sum": snap_sum,
                        "profit_ps": profit_ps,
                        "payout_ps": payout,
                    })
                gamma_only_recorded += 1
                continue

            if walked >= self.max_walks_per_cycle:
                skipped_cap += 1
                continue

            # Full book walk needed. Caller must have provided a scanner so
            # we can lazy-fetch the books now.
            if scanner is not None and not ev.books_fetched:
                for leg in ev.legs:
                    if leg.yes_token_id and not leg.yes_asks:
                        yb = scanner.fetch_book(leg.yes_token_id) or {}
                        leg.yes_asks = scanner._normalize_levels(yb.get("asks"), ascending=True)
                        leg.yes_bids = scanner._normalize_levels(yb.get("bids"), ascending=False)
                    if leg.no_token_id and not leg.no_asks:
                        nb = scanner.fetch_book(leg.no_token_id) or {}
                        leg.no_asks = scanner._normalize_levels(nb.get("asks"), ascending=True)
                        leg.no_bids = scanner._normalize_levels(nb.get("bids"), ascending=False)
                ev.books_fetched = True
            walked += 1

            if walk_yes and self.detect_yes:
                det = self.detect_side(ev, "YES")
                if det is not None:
                    out_detections.append(det)
            if walk_no and self.detect_no:
                det = self.detect_side(ev, "NO")
                if det is not None:
                    out_detections.append(det)

        return {
            "detections": out_detections,
            "gamma_only_gaps": gamma_only_gaps,
            "counters": {
                "scanned": scanned,
                "complete": complete,
                "incomplete": incomplete,
                "walked": walked,
                "gamma_only_recorded": gamma_only_recorded,
                "skipped_time": skipped_time,
                "skipped_cap": skipped_cap,
                "prefilter_pass_yes": prefilter_pass_yes,
                "prefilter_pass_no": prefilter_pass_no,
            },
        }

    # --- paper executor ---------------------------------------------------
    def commit_detection(self, det: ArbDetection, ledger) -> int | None:
        """Open one paper arb_position covering all legs. Returns position_id
        or None if duplicate-today guard fires."""
        today = datetime.now(timezone.utc).date().isoformat()
        if ledger.already_arb_today(det.event.event_id, self.name, det.side, today):
            return None
        legs: list[dict[str, Any]] = []
        total_cost = 0.0
        for leg_info in det.legs_detail:
            price_filled = min(0.99, leg_info["vwap"] + self.slippage_cents)
            shares = det.executable_shares
            cost = shares * price_filled
            total_cost += cost
            legs.append({
                "market_id": leg_info["market_id"],
                "leg_title": leg_info["leg_title"],
                "token_id": leg_info["token_id"],
                "side": det.side,
                "vwap": leg_info["vwap"],
                "price_filled": price_filled,
                "shares": shares,
                "cost": cost,
                "levels_consumed": leg_info["levels_consumed"],
            })
        expected_payout = det.executable_shares * det.payout_per_share
        return ledger.record_arb_position(
            {
                "strategy": self.name,
                "event_id": det.event.event_id,
                "event_slug": det.event.event_slug,
                "event_title": det.event.event_title,
                "side": det.side,
                "n_legs": len(det.event.legs),
                "shares": det.executable_shares,
                "total_cost": total_cost,
                "expected_payout": expected_payout,
                "locked_profit": expected_payout - total_cost,
                "end_date_iso": det.event.end_date_iso,
            },
            legs,
        )


def build(cfg: dict) -> Strategy:
    return BucketSumArb(cfg)
