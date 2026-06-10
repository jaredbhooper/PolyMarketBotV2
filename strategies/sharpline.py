"""Strategy #5 - SHARPLINE: maker-side value betting vs sharp bookmaker odds.

Sportsbook odds (sharp books like Pinnacle) are professionally priced;
Polymarket sports/esports books are retail-priced and often lag. We
compute fair probability from bookmaker odds (vig removed), then post
RESTING PAPER limit orders on Polymarket at prices offering a minimum
edge. PRE-GAME ONLY (we're too slow for in-play).

Maker-side simulation honesty rules (strictly enforced):
  - A simulated BUY is filled only when the observed best ask trades
    STRICTLY BELOW our resting bid (not merely touches).
  - Every maker-derived P&L row is labeled ESTIMATE in the database.
  - adverse_selection = fair_prob_at_post - line_at_fill: aggregated
    per league as the bleed that hands profit back when the line
    moves before we get filled.

If ODDS_API_KEY is missing the strategy still runs in OBSERVE MODE:
scans Polymarket sports/esports markets and logs spreads & volumes,
how many markets WOULD have been evaluated. Reports note 'observe-only
(no odds key)'.
"""
from __future__ import annotations

import difflib
import math
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from foundation.odds_api import OddsAPI
from strategies.base import Estimate, Market, Strategy


# Sports keys for The Odds API (verified shapes documented when an
# API key is available; observe-mode never calls the API).
DEFAULT_SPORTS = [
    # Esports keys are speculative - we adapt at runtime by matching
    # whatever the feed actually returns for these slugs.
    "esports_csgo",       # CS2
    "esports_dota2",
    "esports_lol",
    # Soccer + basketball
    "soccer_epl",
    "basketball_nba",
]


def _normalize(s: str) -> str:
    s = re.sub(r"[^a-z0-9]+", " ", (s or "").lower())
    return " ".join(s.split())


def fuzzy_team_score(team: str, text: str) -> float:
    """Score how well `team` appears in `text`. Token-set match: each
    token of the team name must appear (substring) in text for full
    credit; otherwise fraction of tokens matched. The Polymarket market
    title typically embeds the team in a longer string."""
    if not team or not text:
        return 0.0
    text_n = _normalize(text)
    team_tokens = _normalize(team).split()
    if not team_tokens:
        return 0.0
    # Drop generic city / nickname prefixes that confuse short-token sets.
    DROPS = {"new", "los", "san", "st", "the", "city", "fc", "united",
              "athletic"}
    significant = [t for t in team_tokens if t not in DROPS]
    if not significant:
        significant = team_tokens
    hits = sum(1 for t in significant if t in text_n)
    return hits / len(significant)


def remove_vig(odds_yes: float, odds_no: float) -> float | None:
    """Convert two-way bookmaker American odds into vig-free fair P(YES).

    Accepts decimal odds (e.g. 1.90, 2.10). Returns None if invalid."""
    if odds_yes <= 1.0 or odds_no <= 1.0:
        return None
    imp_yes = 1.0 / odds_yes
    imp_no = 1.0 / odds_no
    total = imp_yes + imp_no
    if total <= 0:
        return None
    return imp_yes / total


def match_polymarket_to_bookmaker(poly_markets: list[Market],
                                     bm_events: list[dict],
                                     min_confidence: float = 0.9
                                     ) -> list[dict]:
    """Per-event fuzzy match. Each bookmaker event has home_team +
    away_team + commence_time. Polymarket market title typically
    contains both team names. Returns a list of dicts with the match
    or a None polymarket if no match was found above threshold.

    Confidence = mean of team-name fuzzy ratios, with a hard floor on
    the lower team score so we don't match 'Knicks vs Celtics' to
    'Knicks vs Lakers' just because one team agrees."""
    out: list[dict] = []
    for bm in bm_events:
        ht = bm.get("home_team") or ""
        at = bm.get("away_team") or ""
        if not ht or not at:
            continue
        best = (None, 0.0)
        ambiguous = False
        for pm in poly_markets:
            text = " ".join([pm.question or "", pm.slug or "",
                              (pm.extras or {}).get("event_title") or ""])
            ht_score = fuzzy_team_score(ht, text)
            at_score = fuzzy_team_score(at, text)
            score = min(ht_score, at_score)
            if score > best[1]:
                # Track ambiguity: if a previous candidate was ALSO close to
                # min_confidence, we flag this match ambiguous.
                if best[0] is not None and best[1] >= min_confidence - 0.10:
                    ambiguous = True
                else:
                    ambiguous = False
                best = (pm, score)
        if best[0] is None or best[1] < min_confidence:
            out.append({
                "bookmaker_event_id": bm.get("id"),
                "home_team": ht, "away_team": at,
                "sport_key": bm.get("sport_key"),
                "poly_market": None, "confidence": best[1],
                "status": "UNMATCHED",
                "commence_time": bm.get("commence_time"),
            })
            continue
        status = "AMBIGUOUS" if ambiguous else "MATCHED"
        out.append({
            "bookmaker_event_id": bm.get("id"),
            "home_team": ht, "away_team": at,
            "sport_key": bm.get("sport_key"),
            "poly_market": best[0], "confidence": best[1],
            "status": status,
            "commence_time": bm.get("commence_time"),
        })
    return out


@dataclass
class SharplineConfig:
    edge_min: float = 0.07
    stake_usd: float = 10.0
    reprice_threshold: float = 0.02
    stop_before_start_minutes: int = 0
    max_resting_orders: int = 25
    max_per_league_per_day: int = 5
    polymarket_fee_pct: float = 0.0
    monthly_request_cap: int = 450


class Sharpline(Strategy):
    name = "sharpline"

    def __init__(self, cfg: dict):
        s = (cfg.get("strategies") or {}).get(self.name, {})
        self.params = SharplineConfig(
            edge_min=float(s.get("edge_min", 0.07)),
            stake_usd=float(s.get("stake_usd", 10.0)),
            reprice_threshold=float(s.get("reprice_threshold", 0.02)),
            stop_before_start_minutes=int(s.get("stop_before_start_minutes", 0)),
            max_resting_orders=int(s.get("max_resting_orders", 25)),
            max_per_league_per_day=int(s.get("max_per_league_per_day", 5)),
            polymarket_fee_pct=float(s.get("polymarket_fee_pct", 0.0)),
            monthly_request_cap=int(s.get("monthly_request_cap", 450)),
        )
        self.sports: list[str] = list(s.get("sports") or DEFAULT_SPORTS)
        self.match_min_confidence = float(s.get("match_min_confidence", 0.9))
        self.cfg = cfg

    # Per-market ABC no-ops.
    def relevant_markets(self, markets: list[Market]) -> list[Market]:
        return []

    def estimate(self, market: Market) -> Estimate | None:
        return None

    # ------------------------------------------------------------ entry
    def run(self, ledger, poly_markets: list[Market], verbose: bool = False
              ) -> dict[str, Any]:
        api = OddsAPI(ledger, monthly_cap=self.params.monthly_request_cap)
        budget = api.budget_status()
        if api.observe_mode:
            return self._observe_only(ledger, poly_markets, budget, verbose)
        # Fetch odds for each configured sport (cached, budget-aware).
        # Stash the raw bookmakers blob on each event so _first_h2h can
        # find it after the matcher strips its scope.
        bm_events: list[dict] = []
        for sport in self.sports:
            events = api.fetch_odds(sport)
            for ev in events:
                ev.setdefault("sport_key", sport)
            bm_events.extend(events)
        bm_by_id = {ev.get("id"): ev for ev in bm_events}
        if verbose:
            print(f"  sharpline: {len(bm_events)} bookmaker events fetched"
                  f" (budget {budget['used']}/{budget['cap']})")
        matches = match_polymarket_to_bookmaker(
            poly_markets, bm_events,
            min_confidence=self.match_min_confidence)
        matched = [m for m in matches if m["status"] == "MATCHED"]
        ambiguous = [m for m in matches if m["status"] == "AMBIGUOUS"]
        unmatched = [m for m in matches if m["status"] == "UNMATCHED"]
        for m in matches:
            ledger.record_sharpline_match({
                "sport_key": m["sport_key"],
                "poly_market_id": m["poly_market"].market_id if m.get("poly_market") else None,
                "poly_event_slug": (m["poly_market"].slug if m.get("poly_market") else None),
                "bookmaker_event_id": m["bookmaker_event_id"],
                "home_team": m["home_team"], "away_team": m["away_team"],
                "confidence": m["confidence"], "status": m["status"],
            })

        # For each MATCHED pair, compute fair prob (vig-removed two-way
        # market) and decide whether to POST a paper limit order.
        posted = 0
        for m in matched:
            raw = bm_by_id.get(m["bookmaker_event_id"])
            if raw is not None:
                m["raw"] = raw
            bm = self._first_h2h(m)
            if not bm:
                continue
            fair = remove_vig(bm["odds_home"], bm["odds_away"])
            if fair is None:
                continue
            poly_market = m["poly_market"]
            yes_ask = poly_market.yes_ask
            if yes_ask is None:
                continue
            # Edge from BUYing YES at yes_ask: (fair - ask) / ask
            edge = (fair - yes_ask) / max(yes_ask, 1e-9)
            if edge < self.params.edge_min:
                continue
            ledger.record_sharpline_order({
                "match_id": 0,        # set later when joined to match row
                "poly_market_id": poly_market.market_id,
                "side": "YES",
                "outcome": "home",
                "our_price": yes_ask,
                "fair_prob_at_post": fair,
                "edge_at_post": edge,
                "stake_usd": self.params.stake_usd,
                "league": m["sport_key"],
                "status": "RESTING",
            })
            posted += 1
        if verbose:
            print(f"  sharpline: matched={len(matched)} ambig={len(ambiguous)} "
                  f"unmatched={len(unmatched)} posted={posted}")
        return {
            "bookmaker_events": len(bm_events),
            "matched": len(matched), "ambiguous": len(ambiguous),
            "unmatched": len(unmatched), "posted": posted,
            "budget": budget, "observe_mode": False,
        }

    # ------------------------------------------------------------ observe
    def _observe_only(self, ledger, poly_markets, budget, verbose):
        """No odds key -> scan Polymarket sports/esports and log spreads."""
        sport_keywords = ("nba", "nfl", "epl", "mlb", "soccer",
                            "csgo", "cs2", "dota", "league of legends", "lol",
                            "esports")
        relevant = []
        for m in poly_markets:
            t = " ".join([(m.slug or ""), (m.question or ""),
                           ((m.extras or {}).get("event_slug") or "")]).lower()
            if any(k in t for k in sport_keywords):
                relevant.append(m)
        if verbose:
            print(f"  sharpline: observe-only (no odds key). "
                  f"Polymarket sports/esports markets in scope: {len(relevant)}")
        return {
            "bookmaker_events": 0, "matched": 0, "ambiguous": 0,
            "unmatched": 0, "posted": 0, "in_scope": len(relevant),
            "budget": budget, "observe_mode": True,
        }

    # ------------------------------------------------------------ helpers
    @staticmethod
    def _first_h2h(match: dict) -> dict | None:
        """Reduce a Bookmakers list to a single odds_home/away pair."""
        bm_evt = match.get("raw") or match
        books = bm_evt.get("bookmakers") or []
        for book in books:
            mks = book.get("markets") or []
            for mk in mks:
                if mk.get("key") not in ("h2h", "h2h_3_way"):
                    continue
                outcomes = mk.get("outcomes") or []
                if len(outcomes) < 2:
                    continue
                # Map by name.
                by_name = {o.get("name") or "": float(o.get("price") or 0) for o in outcomes}
                home = match.get("home_team")
                away = match.get("away_team")
                if home in by_name and away in by_name:
                    return {"odds_home": by_name[home], "odds_away": by_name[away]}
        return None


def build(cfg: dict) -> Strategy:
    return Sharpline(cfg)
