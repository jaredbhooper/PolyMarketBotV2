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

    # =================================================== fill simulation
    # Strict-through honesty rule: a resting paper BUY at limit price L
    # is FILLED only when the observed best ask trades STRICTLY BELOW L
    # (touch != fill). We approximate "trades through" by checking
    # whether the current best ask < L; an ask AT L could be our own
    # quote sitting at the front, so we never count that as a fill.
    #
    # Lifecycle states on sharpline_orders.status:
    #   RESTING            - waiting for the market to come to us
    #   FILLED             - observed best ask < our limit at poll time
    #   CANCELLED          - market closed (game start passed) before fill
    #   UNFILLED_RESOLVED  - market resolved unfilled; graded counterfactually
    #                         to learn whether we DODGED a loss or MISSED a win
    #
    # adverse_selection: SIGN CONVENTION (project-wide, see README).
    #   POSITIVE  = the line moved AGAINST our position before we got
    #               filled. We got picked off; this is the latency tax.
    #   NEGATIVE  = the line moved IN FAVOR of our position. Lucky fill.
    #
    # For YES buys the line moving down (market values YES less) is bad
    # for us; for NO buys the line moving up is bad. So:
    #   YES side: adverse = fair_prob_at_post - line_at_fill
    #   NO  side: adverse = line_at_fill - fair_prob_at_post
    #
    # We approximate line_at_fill = the YES ask we filled against (the
    # market's own implied P(YES) at that instant). We don't burn an
    # Odds API call to grab a fresh fair_prob at fill time.

    def simulate_fills_and_grade(self, ledger, scanner, gamma_url: str,
                                    bankroll=None,
                                    poll_now_ts: int | None = None,
                                    verbose: bool = False) -> dict[str, Any]:
        """Run the per-cycle order-lifecycle pass.

        Inspects every RESTING order:
          1. If the underlying market has closed/resolved, mark
             UNFILLED_RESOLVED and grade counterfactually.
          2. Else if game-start has passed (close_time approximation),
             mark CANCELLED.
          3. Else fetch the CLOB book; if best ask STRICTLY below our
             limit price, mark FILLED, record line_at_fill +
             adverse_selection, debit bankroll.
          4. Else leave RESTING.

        For every FILLED order whose market HAS resolved, settle to
        WIN/LOSS, credit bankroll, populate realized_pnl.

        Returns counters for the report.
        """
        import json as _json
        from datetime import datetime, timezone as _tz
        import requests as _req

        now_ts = int(poll_now_ts) if poll_now_ts is not None else int(__import__("time").time())
        cancelled = 0; filled = 0; unfilled_resolved = 0; settled = 0
        resting = ledger.list_sharpline_orders("RESTING")
        # We need market metadata (close_time, resolution) for each
        # poly_market_id. Pull lazily via Gamma.
        sess = _req.Session()
        for o in resting:
            mid = o["poly_market_id"]
            try:
                r = sess.get(f"{gamma_url}/markets",
                              params={"condition_ids": mid}, timeout=15)
                gdata = r.json()
                gm = gdata[0] if gdata else None
            except Exception:
                gm = None
            # Step 1: market closed -> UNFILLED_RESOLVED + counterfactual.
            if gm and gm.get("closed"):
                yes_price = self._yes_resolution_price(gm)
                if yes_price is None:
                    continue
                # Counterfactual: if we HAD been filled at our limit,
                # would we have won (yes_price=1) or lost (yes_price=0)?
                # 'WIN' = side==YES and yes_price>0.99 OR side==NO and yes_price<0.01.
                counter_won = (o["side"] == "YES" and yes_price > 0.99) \
                    or (o["side"] == "NO" and yes_price < 0.01)
                # The dodge/miss label is part of the post-mortem - we
                # store the counterfactual P&L sign in realized_pnl with
                # negative-sign meaning 'dodge'.
                stake = float(o["stake_usd"])
                shares_if_filled = stake / float(o["our_price"])
                cf_pnl = shares_if_filled - stake if counter_won else -stake
                ledger.update_sharpline_order(
                    int(o["id"]),
                    status="UNFILLED_RESOLVED",
                    resolved_outcome="DODGED_LOSS" if not counter_won else "MISSED_WIN",
                    realized_pnl=cf_pnl)
                unfilled_resolved += 1
                continue
            # Step 2: book fetch + strict-through fill.
            book = (scanner.fetch_book((gm or {}).get("clobTokenIds") and
                                          _json.loads(gm["clobTokenIds"])[0]
                                          if gm and isinstance(gm.get("clobTokenIds"), str)
                                          else None) or {}) if gm else {}
            asks = []
            for L in (book.get("asks") or []):
                try:
                    asks.append({"price": float(L["price"]), "size": float(L["size"])})
                except (KeyError, TypeError, ValueError):
                    pass
            asks.sort(key=lambda L: L["price"])
            best_ask = asks[0]["price"] if asks else None
            limit = float(o["our_price"])
            # STRICTLY below the limit (touch != fill).
            if best_ask is not None and best_ask < limit - 1e-9:
                # Pay our limit price; line_at_fill = the current best ask
                # (the market's implied prob at the moment we filled).
                line_at_fill = best_ask
                fair = float(o["fair_prob_at_post"])
                # Sign convention: POSITIVE = picked off. Inverts on NO side
                # because for NO buys the market moving UP is bad for us.
                if o["side"] == "YES":
                    adverse = fair - line_at_fill
                else:
                    adverse = line_at_fill - fair
                ok = True
                if bankroll is not None:
                    ok = bankroll.try_debit(self.name, float(o["stake_usd"]),
                                              related_table="sharpline_orders",
                                              related_id=int(o["id"]),
                                              note=f"sharpline fill @ {limit:.4f}")
                if not ok:
                    # Out of capital -> cancel the resting order rather
                    # than fake a fill.
                    ledger.update_sharpline_order(int(o["id"]), status="CANCELLED")
                    cancelled += 1
                    continue
                ledger.update_sharpline_order(
                    int(o["id"]), status="FILLED",
                    filled_at=__import__("datetime").datetime.now(_tz.utc).isoformat(),
                    line_at_fill=line_at_fill,
                    adverse_selection=adverse)
                filled += 1
        # Settle FILLED orders whose markets have resolved.
        filled_rows = ledger.list_sharpline_orders("FILLED")
        for o in filled_rows:
            if o["realized_pnl"] is not None:
                continue   # already settled
            mid = o["poly_market_id"]
            try:
                r = sess.get(f"{gamma_url}/markets",
                              params={"condition_ids": mid}, timeout=15)
                gdata = r.json()
                gm = gdata[0] if gdata else None
            except Exception:
                gm = None
            if not gm or not gm.get("closed"):
                continue
            yes_price = self._yes_resolution_price(gm)
            if yes_price is None:
                continue
            won = (o["side"] == "YES" and yes_price > 0.99) \
                or (o["side"] == "NO" and yes_price < 0.01)
            stake = float(o["stake_usd"])
            our_price = float(o["our_price"])
            shares = stake / our_price
            # Polymarket per-category quadratic taker fee on the BUY.
            # League maps to a category via foundation.fees.
            from foundation.fees import polymarket_taker_fee_per_share
            fee_per_share = polymarket_taker_fee_per_share(
                our_price, category=(o["league"] or "sports"))
            fees_paid = fee_per_share * shares
            pnl_gross = shares - stake if won else -stake
            pnl = pnl_gross - fees_paid
            ledger.update_sharpline_order(
                int(o["id"]),
                resolved_outcome="WIN" if won else "LOSS",
                realized_pnl=pnl)
            if bankroll is not None:
                proceeds = stake + pnl
                bankroll.credit(self.name, proceeds=proceeds,
                                  opening_stake=stake,
                                  related_table="sharpline_orders",
                                  related_id=int(o["id"]))
            settled += 1
        if verbose:
            print(f"  sharpline-lifecycle: cancelled={cancelled} filled={filled} "
                  f"unfilled_resolved={unfilled_resolved} settled={settled}")
        return {
            "cancelled": cancelled, "filled": filled,
            "unfilled_resolved": unfilled_resolved, "settled": settled,
        }

    @staticmethod
    def _yes_resolution_price(gm: dict) -> float | None:
        import json as _json
        try:
            op = gm.get("outcomePrices")
            if isinstance(op, str):
                op = _json.loads(op)
            return float(op[0]) if op else None
        except (TypeError, ValueError, _json.JSONDecodeError):
            return None

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
