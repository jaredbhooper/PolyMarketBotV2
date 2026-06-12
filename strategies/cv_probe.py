"""Cross-venue FUZZY divergence probe (research book, quarantined).

Purpose: empirically measure (a) how often non-identical referees actually
disagree on the same event, and (b) whether betting FUZZY price gaps
would have been profitable. This is NOT a strategy that should ever earn
the main bankroll's money — it's a side experiment.

Quarantine rules (enforced in code, verified by tests):
  * Probe positions live in `cv_probe_positions` / `cv_probe_legs`
    (separate from cv_positions/cv_legs).
  * Probe capital is a virtual $500 side-book; the main bankroll is
    untouched.
  * Probe rows never appear in cv-stats or master-report scoreboard P&L;
    they get their own clearly-labelled CV-PROBE section.

Eligibility (ALL must hold):
  * pair classification == FUZZY  (CERTIFIED routes to the real strategy;
    NON-MATCH is never traded);
  * match confidence >= min_match_confidence (default 0.9);
  * net gap per share (= 1 - total_cost_per_share, NET of both venues'
    fees and our standard slippage) >= min_probe_gap (default $0.02);
  * both legs fillable within visible book depth in the SAME scan cycle
    (no partial sets — unfillable legs logged but never faked);
  * the pair has no existing OPEN or SETTLED probe row (dedupe).

Daily selection (per cycle):
  * stake = probe_stake_usd ($5) notional per pair;
  * caps: max_probe_total (1000 lifetime), max_probe_per_day (40),
    max_probe_per_day_per_category (15) — the diversity cap is what
    prevents weather from hogging the experiment.
  * Within a category's daily quota, the LARGEST net gaps are taken first.

The probe consumes the result dict returned by
`CrossVenueArb.scan_cv` (so it does NOT re-fetch markets, re-walk books,
or re-classify pairs).
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from foundation.venues.base import VenueMarket
from strategies.base import Estimate, Market, Strategy


# Direction labels mirror cross_venue_arb so the probe + cert paths are
# legible side-by-side in the database.
DIRECTIONS = ("POLY_YES_KAL_NO", "POLY_NO_KAL_YES")


@dataclass
class ProbeCandidate:
    """One row of the cv_probe selection pipeline. Built from a CV
    detection + the equivalence result; carries everything we need to
    persist a probe position when (and only when) it survives caps."""
    pair_id: int
    category: str
    confidence: float
    direction: str
    poly: VenueMarket
    kalshi: VenueMarket
    poly_vwap: float
    poly_fee: float
    kalshi_vwap: float
    kalshi_fee: float
    poly_levels: list[dict]
    kalshi_levels: list[dict]
    cost_per_share: float          # WITHOUT safety_buffer (true probe cost)
    net_gap_per_share: float       # 1 - cost_per_share (what the probe earns
                                   # if both legs settle our way)
    executable_shares: float       # min(p_filled, k_filled) at target
    divergence_risk_note: str
    poly_side: str                 # YES or NO
    kalshi_side: str


class CVProbe(Strategy):
    """Quarantined FUZZY divergence probe. Drives nothing on its own —
    runs ON the result of CrossVenueArb.scan_cv() to open paper probe
    positions on FUZZY pairs that pass all filters."""

    name = "cv_probe"

    def __init__(self, cfg: dict):
        s = (cfg.get("strategies") or {}).get(self.name, {})
        self.min_probe_gap = float(s.get("min_probe_gap", 0.02))
        self.max_probe_total = int(s.get("max_probe_total", 1000))
        self.max_probe_per_day = int(s.get("max_probe_per_day", 40))
        self.max_probe_per_day_per_category = int(
            s.get("max_probe_per_day_per_category", 15))
        self.probe_stake_usd = float(s.get("probe_stake_usd", 5.0))
        # Virtual side-book. NOT routed through the main bankroll.
        self.probe_capital = float(s.get("probe_capital", 500.0))
        self.min_match_confidence = float(s.get("min_match_confidence", 0.9))
        # Lowest fill we'll accept on EITHER leg. Probe shares can be
        # fractional, but rounding to <1 share gives garbage stats.
        self.min_executable_shares = float(s.get("min_executable_shares", 1.0))
        # Safety-buffer is intentionally NOT applied to probe candidacy —
        # the experiment measures the headline gap, not a tradeable margin.
        # The probe IS still pessimistic on prices (uses VWAP + slippage
        # + fees), so a positive probe gap is a real positive paper-gap.

    # --- strategy ABC no-ops ------------------------------------------------
    def relevant_markets(self, markets: list[Market]) -> list[Market]:
        return []

    def estimate(self, market: Market) -> Estimate | None:
        return None

    # --- core selection -----------------------------------------------------
    def _candidates_from_cv_result(self, cv_result: dict
                                      ) -> list[ProbeCandidate]:
        """Project the cross_venue_arb scan result into eligible probe
        candidates. Filters by FUZZY classification, confidence, net gap,
        and minimum executable depth. ONE candidate per (pair, direction)."""
        detections = cv_result.get("detections") or []
        cert_fuzz_pairs = cv_result.get("cert_fuzz_pairs") or []
        # pair_id -> (category, confidence) from the equivalence result.
        meta_by_pair: dict[int, tuple[str, float]] = {
            pid: ((res.category or "unknown"), float(res.confidence))
            for pid, _p, _k, res in cert_fuzz_pairs
        }
        out: list[ProbeCandidate] = []
        for det in detections:
            if det.classification != "FUZZY":
                # CERTIFIED is the real strategy's job; NON-MATCH never
                # gets a detection here (cross_venue_arb skips it before
                # the book walk).
                continue
            category, confidence = meta_by_pair.get(
                det.pair_id, ("unknown", 0.0))
            if confidence < self.min_match_confidence:
                continue
            # True net gap = 1 - cost_per_share. (locked_profit_per_share
            # is 1 - cost_per_share - safety_buffer; back out the buffer
            # so the probe sees the headline gap.)
            net_gap = (det.locked_profit_per_share
                        + float(getattr(det, "safety_buffer", 0.0)))
            if net_gap < self.min_probe_gap:
                continue
            if det.executable_shares < self.min_executable_shares:
                continue
            # Both legs must have at least min_executable_shares of fill.
            # _detect_direction already enforced both > 0 and >= min_executable
            # on the executable bound, so this is a safety net only.
            poly_leg = next((L for L in det.legs_detail
                              if L["venue"] == "polymarket"), None)
            kal_leg = next((L for L in det.legs_detail
                             if L["venue"] == "kalshi"), None)
            if poly_leg is None or kal_leg is None:
                continue
            out.append(ProbeCandidate(
                pair_id=int(det.pair_id),
                category=category,
                confidence=confidence,
                direction=det.direction,
                poly=det.poly, kalshi=det.kalshi,
                poly_vwap=float(det.poly_vwap), poly_fee=float(det.poly_fee),
                kalshi_vwap=float(det.kalshi_vwap), kalshi_fee=float(det.kalshi_fee),
                poly_levels=list(poly_leg.get("levels_consumed") or []),
                kalshi_levels=list(kal_leg.get("levels_consumed") or []),
                cost_per_share=float(det.total_cost_per_share),
                net_gap_per_share=net_gap,
                executable_shares=float(det.executable_shares),
                divergence_risk_note=det.divergence_risk_note,
                poly_side=str(poly_leg["side"]),
                kalshi_side=str(kal_leg["side"]),
            ))
        return out

    def _apply_caps(self, candidates: list[ProbeCandidate],
                     ledger) -> tuple[list[ProbeCandidate], dict]:
        """Apply lifetime/total + daily + per-category caps, with
        largest-gap-first selection. Returns (winners, skip_counters).

        DEDUPE: candidates whose pair already has an OPEN or SETTLED probe
        row are dropped (logged in skip_counters['already_probed'])."""
        skip = {"already_probed": 0, "total_cap": 0,
                  "daily_cap": 0, "category_cap": 0,
                  "below_threshold": 0}
        # Dedupe first (cheap; ledger lookup per pair).
        deduped: list[ProbeCandidate] = []
        seen_pairs: set[int] = set()
        for c in candidates:
            if c.pair_id in seen_pairs:
                # Same pair, two directions — only keep the first one (the
                # one we'd open on dedup) so we count one "probe per pair".
                continue
            if ledger.cv_probe_pair_has_open_or_settled(c.pair_id):
                skip["already_probed"] += 1
                seen_pairs.add(c.pair_id)
                continue
            seen_pairs.add(c.pair_id)
            deduped.append(c)
        # If two directions on the same pair both survived (rare), keep
        # the one with the larger net gap.
        by_pair: dict[int, ProbeCandidate] = {}
        for c in deduped:
            cur = by_pair.get(c.pair_id)
            if cur is None or c.net_gap_per_share > cur.net_gap_per_share:
                by_pair[c.pair_id] = c
        candidates = list(by_pair.values())

        # Sort by net gap descending — biggest gaps win the cap budget.
        candidates.sort(key=lambda c: c.net_gap_per_share, reverse=True)

        # Apply caps.
        today = datetime.now(timezone.utc).date().isoformat()
        open_now = ledger.cv_probe_count_open()
        already_today = ledger.cv_probe_count_today(today)
        # Per-category counts seen TODAY.
        per_cat_today: dict[str, int] = {}
        winners: list[ProbeCandidate] = []
        # Daily cap: room left after what's already opened today (incl.
        # settled-same-day, which already count against the diversity cap).
        daily_room = max(0, self.max_probe_per_day - already_today)
        total_room = max(0, self.max_probe_total - open_now - already_today)
        for c in candidates:
            if len(winners) >= daily_room:
                skip["daily_cap"] += 1
                continue
            if len(winners) >= total_room:
                skip["total_cap"] += 1
                continue
            cat_today = per_cat_today.get(c.category, 0) \
                + ledger.cv_probe_count_today(today, c.category)
            if cat_today >= self.max_probe_per_day_per_category:
                skip["category_cap"] += 1
                continue
            winners.append(c)
            per_cat_today[c.category] = per_cat_today.get(c.category, 0) + 1
        return winners, skip

    # --- public entry -------------------------------------------------------
    def run_probe(self, cv_result: dict, ledger, *,
                    verbose: bool = True) -> dict:
        """Open paper probe positions on FUZZY pairs that pass all
        filters. Returns counters for the report.

        Idempotent: re-running within the same cycle is a no-op (dedupe
        guarantees one probe per pair, ever).
        """
        candidates = self._candidates_from_cv_result(cv_result)
        if verbose:
            print(f"\n=== {self.name} ===")
            print(f"  fuzzy candidates eligible: {len(candidates)} "
                  f"(min_gap=${self.min_probe_gap:.2f}, "
                  f"min_conf={self.min_match_confidence:.2f})")
        winners, skip = self._apply_caps(candidates, ledger)
        if verbose:
            print(f"  after caps: {len(winners)} winners "
                  f"(skipped: already_probed={skip['already_probed']}, "
                  f"daily_cap={skip['daily_cap']}, "
                  f"category_cap={skip['category_cap']}, "
                  f"total_cap={skip['total_cap']})")
        opened_ids: list[int] = []
        per_category_opened: dict[str, int] = {}
        for c in winners:
            pid = self._open_position(c, ledger)
            if pid is None:
                continue
            opened_ids.append(pid)
            per_category_opened[c.category] = (
                per_category_opened.get(c.category, 0) + 1)
            if verbose:
                print(f"  [PROBE OPEN] pos #{pid} {c.category} "
                      f"{c.direction} gap=${c.net_gap_per_share:+.3f} "
                      f"stake=${self.probe_stake_usd:.0f} "
                      f"conf={c.confidence:.2f} "
                      f"{(c.poly.title or '')[:48]}")
        return {
            "counters": {
                "candidates_eligible":  len(candidates),
                "winners":              len(winners),
                "opened":               len(opened_ids),
                "already_probed":       skip["already_probed"],
                "daily_cap_skipped":    skip["daily_cap"],
                "category_cap_skipped": skip["category_cap"],
                "total_cap_skipped":    skip["total_cap"],
            },
            "opened_position_ids": opened_ids,
            "per_category_opened": per_category_opened,
        }

    # --- internals ----------------------------------------------------------
    def _open_position(self, c: ProbeCandidate, ledger) -> int | None:
        """Compute the actual probe stake (capped by both venues' visible
        depth) and persist via ledger.record_cv_probe_position."""
        # Buy `probe_stake_usd` worth at the cost_per_share. The detection
        # already book-walked at executable_shares (>= probe stake at $5
        # in almost all cases). We trust the VWAP and just resize to the
        # probe stake; if the executable bound is smaller than what the
        # stake would buy, fall back to the bound (smaller-than-target
        # but never partial: both legs equal shares).
        if c.cost_per_share <= 0:
            return None
        nominal_shares = self.probe_stake_usd / c.cost_per_share
        shares = min(nominal_shares, c.executable_shares)
        if shares < self.min_executable_shares:
            return None
        total_cost = shares * c.cost_per_share
        expected_payout = shares  # one leg pays $1 if our chosen side wins
        legs = [
            {
                "venue": "polymarket",
                "venue_market_id": c.poly.venue_market_id,
                "side": c.poly_side,
                "vwap": c.poly_vwap,
                "price_filled": min(0.99, c.poly_vwap) + c.poly_fee,
                "fee_per_share": c.poly_fee,
                "shares": shares,
                "cost": shares * (c.poly_vwap + c.poly_fee),
                "levels_consumed": c.poly_levels,
            },
            {
                "venue": "kalshi",
                "venue_market_id": c.kalshi.venue_market_id,
                "side": c.kalshi_side,
                "vwap": c.kalshi_vwap,
                "price_filled": min(0.99, c.kalshi_vwap) + c.kalshi_fee,
                "fee_per_share": c.kalshi_fee,
                "shares": shares,
                "cost": shares * (c.kalshi_vwap + c.kalshi_fee),
                "levels_consumed": c.kalshi_levels,
            },
        ]
        return ledger.record_cv_probe_position(
            {
                "pair_id": c.pair_id,
                "category": c.category,
                "match_confidence": c.confidence,
                "direction": c.direction,
                "shares": shares,
                "total_cost": total_cost,
                "expected_payout": expected_payout,
                "net_gap_per_share": c.net_gap_per_share,
                "divergence_risk_note": c.divergence_risk_note,
            },
            legs,
        )


def build(cfg: dict) -> Strategy:
    return CVProbe(cfg)
