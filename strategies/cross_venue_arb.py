"""Strategy #3: cross-venue arbitrage (Polymarket <-> Kalshi).

Concept: the same real-world event listed on both platforms can be
priced differently. If you can buy YES-equivalent cheaply on one venue
AND NO-equivalent cheaply on the other such that combined cost +
fees + slippage < $1 - safety_buffer, one side MUST pay $1 and the
combined position locks a profit - **IF the two contracts are truly
equivalent and resolve identically.**

Non-negotiable: the rules-equivalence engine
(`foundation/equivalence.py`) classifies every candidate pair as
CERTIFIED-IDENTICAL / FUZZY / NON-MATCH. Only CERTIFIED pairs are ever
auto-traded. FUZZY pairs are logged for human review.

Even on CERTIFIED pairs, when the two venues use *different* resolution
sources (Polymarket weather = Wunderground/METAR; Kalshi weather = NWS
Climatological Report), we store a divergence_risk_note on every
detected gap. Both legs can lose simultaneously on a same-day source
disagreement, so the trader (and the human reviewer) must see the risk
explicitly.

The strategy is built in two layers per the spec:
  DETECTOR (always logs - every cross-venue gap, even untradeable).
  PAPER EXECUTOR (only fires on CERTIFIED-IDENTICAL pairs that clear
                  min_profit AFTER both platforms' fees + slippage +
                  safety_buffer).
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from foundation.equivalence import (CITY_ALIASES, EquivalenceResult,
                                     classify_pair, detect_city,
                                     detect_date, normalize_source,
                                     parse_bucket)
from foundation.venues.base import VenueMarket
from foundation.venues.kalshi import KalshiVenue
from foundation.venues.polymarket import PolymarketVenue
from strategies.base import Estimate, Market, Strategy


# --- per-leg book walk ---------------------------------------------------
def walk_book_for_shares(asks: list[dict], target: float
                          ) -> tuple[float, float, list[dict]]:
    """Walk ascending ask book consuming `target` shares. Returns
    (vwap, shares_filled, levels_consumed)."""
    if target <= 0 or not asks:
        return float("nan"), 0.0, []
    remaining = float(target)
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
        consumed.append({"price": price, "shares_taken": take, "usd_taken": usd})
        total_usd += usd
        total_shares += take
        remaining -= take
        if remaining <= 1e-9:
            break
    if total_shares <= 0:
        return float("nan"), 0.0, []
    return total_usd / total_shares, total_shares, consumed


def kalshi_quadratic_fee(price: float, multiplier: float = 1.0) -> float:
    """Kalshi standard quadratic fee = ceil(mult * 7c * p*(1-p))."""
    if not (0.0 < price < 1.0):
        return 0.0
    raw = multiplier * 0.07 * price * (1.0 - price)
    return math.ceil(raw * 100.0) / 100.0


# --- matcher -------------------------------------------------------------
def bucket_markets_by_key(markets: list[VenueMarket]) -> dict[tuple, list[VenueMarket]]:
    """Group markets by (city, date, kind) for cheap candidate-pair
    construction. Returns a dict keyed by (city, date, kind) tuples; only
    keys whose city + date both detected non-None are useful for
    cross-venue match."""
    out: dict[tuple, list[VenueMarket]] = {}
    for m in markets:
        text = " ".join([m.title, m.leg_title, m.rules_text,
                          m.extras.get("event_slug") or "",
                          m.extras.get("event_title") or "",
                          m.extras.get("series_title") or "",
                          m.venue_event_id or ""])
        city = detect_city(text)
        date = detect_date(text) or (m.close_time_iso or "")[:10] or None
        # kind = max/min for weather; for non-weather we use venue-specific hints
        kind = (m.extras.get("kind") or "").lower()
        if not kind:
            t = text.lower()
            if "high" in t and ("temperature" in t or "temp" in t):
                kind = "max"
            elif "low" in t and ("temperature" in t or "temp" in t):
                kind = "min"
        key = (city, date, kind)
        if city is None or date is None:
            continue
        out.setdefault(key, []).append(m)
    return out


# --- detection ----------------------------------------------------------
@dataclass
class CVDetection:
    pair_id: int                          # cv_pairs.id once persisted
    poly: VenueMarket
    kalshi: VenueMarket
    classification: str                   # CERTIFIED-IDENTICAL | FUZZY | NON-MATCH
    direction: str                        # POLY_YES_KAL_NO | POLY_NO_KAL_YES
    target_shares: float
    executable_shares: float
    poly_vwap: float
    poly_fee: float
    kalshi_vwap: float
    kalshi_fee: float
    safety_buffer: float
    total_cost_per_share: float
    locked_profit_per_share: float
    locked_profit_usd: float
    divergence_risk_note: str
    legs_detail: list[dict] = field(default_factory=list)
    cleared_threshold: bool = False


class CrossVenueArb(Strategy):
    """Per-market layer no-op; cross-venue work happens in scan_cv()."""

    name = "cross_venue_arb"

    def __init__(self, cfg: dict):
        s = (cfg.get("strategies") or {}).get(self.name, {})
        self.safety_buffer = float(s.get("safety_buffer", 0.005))
        self.min_arb_profit = float(s.get("min_arb_profit", 0.50))
        self.target_shares = float(s.get("target_shares", 50.0))
        self.poly_slippage_cents = float(s.get(
            "poly_slippage_cents",
            (cfg.get("paper") or {}).get("slippage_cents", 0.01)))
        self.kalshi_slippage_cents = float(s.get("kalshi_slippage_cents", 0.01))
        self.min_executable_shares = float(s.get("min_executable_shares", 5.0))
        self.min_hours_to_resolve = float(s.get("min_hours_to_resolve", 1.0))
        # The big switch. With execute_fuzzy=False (default) the executor
        # NEVER fires on FUZZY pairs. They still get logged to cv_gaps so
        # the human can review them.
        self.execute_fuzzy = bool(s.get("execute_fuzzy", False))
        # Kalshi category filter; default is weather (where the daily
        # overlap with Polymarket lives).
        self.kalshi_categories = s.get("kalshi_categories") or ["Climate and Weather"]

    # --- strategy ABC no-ops ----------------------------------------------
    def relevant_markets(self, markets: list[Market]) -> list[Market]:
        return []

    def estimate(self, market: Market) -> Estimate | None:
        return None

    # --- public scan ------------------------------------------------------
    def scan_cv(self, poly_venue: PolymarketVenue, kalshi_venue: KalshiVenue,
                  ledger, verbose: bool = True) -> dict[str, Any]:
        """Run the full cross-venue pass:
          1. Fetch markets from both venues.
          2. Bucket by (city, date, kind) and build candidate pairs.
          3. Classify each pair via the rules-equivalence engine; persist
             in cv_pairs.
          4. For each CERTIFIED / FUZZY pair, book-walk both directions
             (POLY_YES + KAL_NO; POLY_NO + KAL_YES) and log a cv_gaps row.
          5. For each CERTIFIED gap that clears min_arb_profit (and
             execute_fuzzy=True overrides), open a cv_positions row.

        Returns a dict with counters + the detections list for the caller.
        """
        if verbose:
            print(f"\n=== {self.name} ===")
            print("Fetching Polymarket markets ...")
        poly_markets = poly_venue.fetch_markets()
        if verbose:
            print(f"  polymarket: {len(poly_markets)} markets")
            print(f"Fetching Kalshi markets ({', '.join(self.kalshi_categories)}) ...")
        kal_markets: list[VenueMarket] = []
        for cat in self.kalshi_categories:
            kal_markets.extend(kalshi_venue.fetch_markets(category_hint=cat))
        if verbose:
            print(f"  kalshi:     {len(kal_markets)} markets")

        # Bucket both sides.
        poly_buckets = bucket_markets_by_key(poly_markets)
        kal_buckets = bucket_markets_by_key(kal_markets)
        # Candidate keys = intersection.
        shared = set(poly_buckets) & set(kal_buckets)
        if verbose:
            print(f"  shared (city, date, kind) buckets: {len(shared)}")

        # --- build + classify all candidate pairs -------------------------
        detections: list[CVDetection] = []
        certified = 0
        fuzzy = 0
        nonmatch = 0
        # First pass: classify every candidate pair WITHOUT fetching books
        # (so 99% of NON-MATCH pairs cost nothing). Persist CERT + FUZZY.
        cert_fuzz_pairs: list[tuple[int, VenueMarket, VenueMarket, EquivalenceResult]] = []
        for key in shared:
            poly_legs = poly_buckets[key]
            kal_legs = kal_buckets[key]
            for p in poly_legs:
                for k in kal_legs:
                    res = classify_pair(p, k)
                    if res.classification == "NON-MATCH":
                        nonmatch += 1
                        continue
                    if res.classification == "CERTIFIED-IDENTICAL":
                        certified += 1
                    else:
                        fuzzy += 1
                    pair_id = ledger.upsert_cv_pair({
                        "poly_market_id": p.venue_market_id,
                        "kalshi_ticker":  k.venue_market_id,
                        "poly_title": p.title,
                        "kalshi_title": k.title,
                        "poly_leg": p.leg_title,
                        "kalshi_leg": k.leg_title,
                        "poly_close": p.close_time_iso,
                        "kalshi_close": k.close_time_iso,
                        "poly_source": p.settlement_source,
                        "kalshi_source": k.settlement_source,
                        "city": key[0],
                        "date": key[1],
                        "classification": res.classification,
                        "reason": res.reason,
                        "criteria": res.criteria,
                        "divergence_risk_note": res.divergence_risk_note,
                    })
                    cert_fuzz_pairs.append((pair_id, p, k, res))

        # Second pass: lazy-fetch books ONLY for classified pairs we'll
        # walk. De-dupe via id() so each market's book is fetched once
        # even when it appears in multiple pairs.
        if verbose:
            print(f"  classified pairs (cert+fuzzy): {len(cert_fuzz_pairs)}; "
                  f"fetching books on demand ...")
        seen_poly: set = set()
        seen_kal: set = set()
        for pair_id, p, k, res in cert_fuzz_pairs:
            if id(p) not in seen_poly:
                try:
                    poly_venue.fetch_book_for(p)
                except Exception:
                    pass
                seen_poly.add(id(p))
            if id(k) not in seen_kal:
                try:
                    kalshi_venue.fetch_book_for(k)
                except Exception:
                    pass
                seen_kal.add(id(k))
            for direction in ("POLY_YES_KAL_NO", "POLY_NO_KAL_YES"):
                det = self._detect_direction(pair_id, p, k, direction, res)
                if det is None:
                    continue
                detections.append(det)

        # --- log every detection (above threshold or not) -----------------
        logged = 0
        for det in detections:
            ledger.record_cv_gap({
                "strategy": self.name,
                "pair_id": det.pair_id,
                "direction": det.direction,
                "classification": det.classification,
                "poly_vwap": det.poly_vwap,
                "poly_fee": det.poly_fee,
                "kalshi_vwap": det.kalshi_vwap,
                "kalshi_fee": det.kalshi_fee,
                "safety_buffer": det.safety_buffer,
                "target_shares": det.target_shares,
                "executable_shares": det.executable_shares,
                "total_cost_per_share": det.total_cost_per_share,
                "locked_profit_per_share": det.locked_profit_per_share,
                "locked_profit_usd": det.locked_profit_usd,
                "divergence_risk_note": det.divergence_risk_note,
                "cleared_threshold": det.cleared_threshold,
                "legs": det.legs_detail,
            })
            logged += 1

        # --- paper-execute only certified above threshold -----------------
        fired = 0
        for det in detections:
            if not det.cleared_threshold:
                continue
            if det.classification != "CERTIFIED-IDENTICAL" and not self.execute_fuzzy:
                continue
            today = datetime.now(timezone.utc).date().isoformat()
            if ledger.cv_pair_traded_today(det.pair_id, self.name,
                                             det.direction, today):
                continue
            pid = self._commit(det, ledger)
            if pid is not None:
                fired += 1
                if verbose:
                    print(f"  [CV FILL] pos #{pid} {det.direction} "
                          f"shares={det.executable_shares:.1f} "
                          f"locked=${det.locked_profit_usd:+.2f} "
                          f"{det.poly.title[:50]}")

        return {
            "detections": detections,
            "counters": {
                "polymarket_markets": len(poly_markets),
                "kalshi_markets":     len(kal_markets),
                "shared_keys":        len(shared),
                "certified":          certified,
                "fuzzy":              fuzzy,
                "nonmatch":           nonmatch,
                "logged_gaps":        logged,
                "fired":              fired,
            },
        }

    # --- internals -------------------------------------------------------
    def _detect_direction(self, pair_id: int, poly: VenueMarket,
                            kal: VenueMarket, direction: str,
                            res: EquivalenceResult) -> CVDetection | None:
        if direction == "POLY_YES_KAL_NO":
            poly_asks, poly_side = poly.yes_asks, "YES"
            kal_asks, kal_side = kal.no_asks, "NO"
        else:
            poly_asks, poly_side = poly.no_asks, "NO"
            kal_asks, kal_side = kal.yes_asks, "YES"

        p_vwap, p_filled, p_levels = walk_book_for_shares(poly_asks, self.target_shares)
        k_vwap, k_filled, k_levels = walk_book_for_shares(kal_asks, self.target_shares)
        if (math.isnan(p_vwap) or math.isnan(k_vwap)
                or p_filled <= 0 or k_filled <= 0):
            return None
        executable = min(p_filled, k_filled)
        if executable < self.min_executable_shares:
            return None
        # Re-walk at the bound share count.
        p_vwap2, _, p_levels2 = walk_book_for_shares(poly_asks, executable)
        k_vwap2, _, k_levels2 = walk_book_for_shares(kal_asks, executable)
        if math.isnan(p_vwap2) or math.isnan(k_vwap2):
            return None

        # Fees per share. Both venues use a quadratic schedule. For
        # Polymarket we look up the per-category rate (weather 1.25%,
        # sports 0.75%, ...) from foundation.fees - see BUILD_NOTES.md
        # for the verified 2026-03 table.
        from foundation.fees import polymarket_taker_fee_per_share
        kal_fee_mult = float((kal.fee_model or {}).get("multiplier", 1.0))
        kal_fee = kalshi_quadratic_fee(k_vwap2, kal_fee_mult)
        # Category hint: cross-venue pairs in scope are daily weather,
        # so the matcher's bucket key already groups by 'weather' kind.
        # Fall through to fees.py's default if the hint is missing.
        category_hint = " ".join([
            poly.title, poly.leg_title,
            (poly.extras or {}).get("event_slug") or "",
            (poly.extras or {}).get("event_title") or "",
        ])
        poly_fee = polymarket_taker_fee_per_share(p_vwap2, category=category_hint)

        # Slippage and total cost per share.
        p_cost = min(0.99, p_vwap2 + self.poly_slippage_cents) + poly_fee
        k_cost = min(0.99, k_vwap2 + self.kalshi_slippage_cents) + kal_fee
        total_cost = p_cost + k_cost
        # One leg pays $1, the other $0. Locked profit = $1 - total cost - buffer.
        profit_ps = 1.0 - total_cost - self.safety_buffer
        profit_usd = profit_ps * executable

        cleared = (profit_ps > 0 and profit_usd >= self.min_arb_profit)

        return CVDetection(
            pair_id=pair_id,
            poly=poly, kalshi=kal,
            classification=res.classification,
            direction=direction,
            target_shares=self.target_shares,
            executable_shares=executable,
            poly_vwap=p_vwap2, poly_fee=poly_fee,
            kalshi_vwap=k_vwap2, kalshi_fee=kal_fee,
            safety_buffer=self.safety_buffer,
            total_cost_per_share=total_cost,
            locked_profit_per_share=profit_ps,
            locked_profit_usd=profit_usd,
            divergence_risk_note=res.divergence_risk_note,
            legs_detail=[
                {"venue": "polymarket", "venue_market_id": poly.venue_market_id,
                 "side": poly_side, "vwap": p_vwap2, "fee_per_share": poly_fee,
                 "price_filled": p_cost, "shares": executable,
                 "cost": p_cost * executable, "levels_consumed": p_levels2,
                 "leg_title": poly.leg_title, "venue_title": poly.title},
                {"venue": "kalshi", "venue_market_id": kal.venue_market_id,
                 "side": kal_side, "vwap": k_vwap2, "fee_per_share": kal_fee,
                 "price_filled": k_cost, "shares": executable,
                 "cost": k_cost * executable, "levels_consumed": k_levels2,
                 "leg_title": kal.leg_title, "venue_title": kal.title},
            ],
            cleared_threshold=cleared,
        )

    def _commit(self, det: CVDetection, ledger) -> int | None:
        legs_for_db: list[dict[str, Any]] = []
        total_cost = 0.0
        for L in det.legs_detail:
            total_cost += float(L["cost"])
            legs_for_db.append({
                "venue": L["venue"],
                "venue_market_id": L["venue_market_id"],
                "side": L["side"],
                "vwap": L["vwap"],
                "price_filled": L["price_filled"],
                "fee_per_share": L["fee_per_share"],
                "shares": L["shares"],
                "cost": L["cost"],
                "levels_consumed": L["levels_consumed"],
            })
        return ledger.record_cv_position(
            {
                "strategy": self.name,
                "pair_id": det.pair_id,
                "direction": det.direction,
                "shares": det.executable_shares,
                "total_cost": total_cost,
                "expected_payout": det.executable_shares,   # exactly one leg pays $1
                "locked_profit": det.executable_shares - total_cost,
                "divergence_risk_note": det.divergence_risk_note,
            },
            legs_for_db,
        )


def build(cfg: dict) -> Strategy:
    return CrossVenueArb(cfg)
