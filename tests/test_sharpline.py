"""Sharpline (Prompt C, Phase 1) tests. HTTP mocked."""
from __future__ import annotations

import os
import sys
import tempfile

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from foundation.ledger import Ledger
from strategies.base import Market
from strategies.sharpline import (Sharpline, remove_vig, fuzzy_team_score,
                                     match_polymarket_to_bookmaker)


def _temp_ledger():
    f = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    f.close()
    return Ledger(f.name), f.name


def _mk(market_id="0xabc", slug="nba-knicks-vs-celtics-2026-04-15",
         question="Will the Knicks beat the Celtics on Apr 15?",
         yes_ask=0.35, yes_book=None) -> Market:
    return Market(
        market_id=market_id, slug=slug, question=question, category="sports",
        rules_text="", resolve_date=None, end_date_iso="2026-04-15T22:00:00Z",
        yes_token_id="y", no_token_id="n",
        yes_ask=yes_ask, yes_bid=yes_ask - 0.01,
        no_ask=1 - (yes_ask - 0.01), no_bid=1 - yes_ask,
        yes_book=yes_book or [{"price": yes_ask, "size": 100}],
        extras={"event_slug": "nba-knicks-vs-celtics-2026-04-15"})


# ---------------------------------------------------------- math
def test_remove_vig_balanced_book():
    # Two 1.90 decimal odds -> implied 52.6% + 52.6% = 105% -> fair 50/50
    fair = remove_vig(1.90, 1.90)
    assert fair == pytest.approx(0.5, abs=1e-6)


def test_remove_vig_skewed_book():
    # 1.50 / 2.50 -> imp 66.7% / 40% = 107% -> fair ~62.5% / 37.5%
    fair = remove_vig(1.50, 2.50)
    assert fair == pytest.approx(0.6249, abs=1e-3)


def test_remove_vig_invalid():
    assert remove_vig(0.5, 1.5) is None
    assert remove_vig(1.0, 1.0) is None


# ---------------------------------------------------------- matching
def test_matching_accepts_exact():
    polys = [_mk(market_id="0xexact",
                 question="Will the Knicks beat the Celtics?",
                 slug="knicks-vs-celtics")]
    bm = [{"id": "bm1", "home_team": "New York Knicks",
            "away_team": "Boston Celtics", "sport_key": "basketball_nba"}]
    matches = match_polymarket_to_bookmaker(polys, bm, min_confidence=0.5)
    assert matches[0]["status"] == "MATCHED"


def test_matching_rejects_ambiguous_only_one_team_agreed():
    """Single team match should NOT match (we require min(home_score, away_score) >= 0.9)."""
    polys = [_mk(market_id="0xpartial",
                 question="Knicks vs Lakers — who wins?",
                 slug="knicks-vs-lakers")]
    bm = [{"id": "bm1", "home_team": "New York Knicks",
            "away_team": "Boston Celtics", "sport_key": "basketball_nba"}]
    matches = match_polymarket_to_bookmaker(polys, bm, min_confidence=0.9)
    # Lakers != Celtics so confidence < 0.9
    assert matches[0]["status"] == "UNMATCHED"


# ---------------------------------------------------------- observe mode
def test_observe_mode_runs_without_key(monkeypatch):
    monkeypatch.setenv("ODDS_API_KEY", "")
    # Stub load_dotenv so a real .env on disk doesn't re-populate the key.
    import foundation.odds_api as oa
    monkeypatch.setattr(oa, "load_dotenv", lambda path=".env": None)
    ledger, path = _temp_ledger()
    cfg = {"strategies": {"sharpline": {}}}
    s = Sharpline(cfg)
    polys = [
        _mk(market_id="x1", question="Will Trump win?", slug="trump-2028"),
        _mk(market_id="x2", question="Will Knicks beat Celtics?",
            slug="nba-knicks-vs-celtics"),
        _mk(market_id="x3", question="CS2 Major final winner",
            slug="esports-csgo-final"),
    ]
    res = s.run(ledger, polys, verbose=False)
    assert res["observe_mode"] is True
    # Two sports / esports keywords match (knicks/celtics & csgo)
    assert res["in_scope"] >= 2
    try:
        os.unlink(path)
    except OSError:
        pass


# ---------------------------------------------------------- edge math
def test_edge_above_threshold_posts_order(monkeypatch):
    """Set the api key, mock the budget+cache, and feed one matching event."""
    monkeypatch.setenv("ODDS_API_KEY", "fake")
    ledger, path = _temp_ledger()
    cfg = {"strategies": {"sharpline": {
        "edge_min": 0.10, "match_min_confidence": 0.5,
    }}}
    s = Sharpline(cfg)
    poly = _mk(market_id="0xknicks", yes_ask=0.30,
               question="Will the Knicks beat the Celtics?",
               slug="knicks-vs-celtics")
    # Pre-seed the cache with a bookmaker event giving 0.50 implied fair
    ledger.put_odds_cache("esports_csgo", [{
        "id": "bm1", "sport_key": "esports_csgo",
        "commence_time": "2026-04-15T22:00:00Z",
        "home_team": "New York Knicks", "away_team": "Boston Celtics",
        "bookmakers": [{
            "key": "pinnacle",
            "markets": [{"key": "h2h",
                          "outcomes": [
                              {"name": "New York Knicks", "price": 1.90},
                              {"name": "Boston Celtics", "price": 1.90},
                          ]}],
        }],
    }])
    s.sports = ["esports_csgo"]   # avoid hitting other sports caches
    res = s.run(ledger, [poly], verbose=False)
    # Fair = 0.5, ask = 0.30, edge = (0.5 - 0.3)/0.3 = 0.667 - posts
    assert res["posted"] >= 1
    orders = ledger.list_sharpline_orders("RESTING")
    assert len(orders) >= 1
    assert orders[0]["estimate_marker"] == "ESTIMATE"
    try:
        os.unlink(path)
    except OSError:
        pass


# ---------------------------------------------------------- fill lifecycle
class FakeGammaSession:
    """Stand-in for requests.Session() used by simulate_fills_and_grade."""
    def __init__(self, by_market_id: dict[str, dict]):
        self.by_market_id = by_market_id

    def get(self, url, params=None, timeout=15):
        cid = (params or {}).get("condition_ids")
        m = self.by_market_id.get(cid)
        class R:
            status_code = 200
            def __init__(self_inner, m):
                self_inner._m = m
            def json(self_inner):
                return [self_inner._m] if self_inner._m else []
        return R(m)


class FakeBookScanner:
    def __init__(self, books_by_token: dict[str, dict]):
        self.books_by_token = books_by_token

    def fetch_book(self, token_id):
        return self.books_by_token.get(token_id) or {"asks": [], "bids": []}


def _seed_order(ledger, market_id, side="YES", our_price=0.50,
                 fair_prob_at_post=0.55, stake=10.0):
    match_id = ledger.record_sharpline_match({
        "sport_key": "soccer_epl", "poly_market_id": market_id,
        "poly_event_slug": "x", "bookmaker_event_id": "bm",
        "home_team": "A", "away_team": "B",
        "confidence": 1.0, "status": "MATCHED",
    })
    oid = ledger.record_sharpline_order({
        "match_id": match_id, "poly_market_id": market_id,
        "side": side, "outcome": "home", "our_price": our_price,
        "fair_prob_at_post": fair_prob_at_post,
        "edge_at_post": (fair_prob_at_post - our_price) / max(our_price, 1e-9),
        "stake_usd": stake, "league": "soccer_epl", "status": "RESTING",
    })
    return oid


def test_touch_does_not_fill(monkeypatch):
    """Best ask AT our limit (touch) must NOT count as a fill."""
    monkeypatch.setenv("ODDS_API_KEY", "")
    import foundation.odds_api as oa
    monkeypatch.setattr(oa, "load_dotenv", lambda path=".env": None)
    ledger, path = _temp_ledger()
    cfg = {"strategies": {"sharpline": {}}}
    s = Sharpline(cfg)
    oid = _seed_order(ledger, market_id="0xtouch", our_price=0.50)
    # Open market with best ask EXACTLY at 0.50 (touch only).
    gm = {"closed": False, "clobTokenIds": '["tok_touch","tok_no"]'}
    scanner = FakeBookScanner({"tok_touch":
                                  {"asks": [{"price": "0.50", "size": "100"}]}})
    import requests
    monkeypatch.setattr(requests, "Session",
                          lambda: FakeGammaSession({"0xtouch": gm}))
    res = s.simulate_fills_and_grade(ledger, scanner, "http://gamma",
                                       bankroll=None, verbose=False)
    assert res["filled"] == 0
    rows = ledger.list_sharpline_orders("RESTING")
    assert len(rows) == 1   # still RESTING
    try:
        os.unlink(path)
    except OSError:
        pass


def test_strict_through_fills_and_records_adverse_selection(monkeypatch):
    """Best ask STRICTLY BELOW our limit -> FILLED + adverse_selection.

    YES buy. fair=0.60 at post; line=0.45 at fill (market thinks YES is
    LESS likely than the sharp said). For our long-YES position the line
    moved AGAINST us -> adverse_selection should be POSITIVE per the
    project-wide convention (positive = picked off)."""
    monkeypatch.setenv("ODDS_API_KEY", "")
    import foundation.odds_api as oa
    monkeypatch.setattr(oa, "load_dotenv", lambda path=".env": None)
    ledger, path = _temp_ledger()
    cfg = {"strategies": {"sharpline": {}}}
    s = Sharpline(cfg)
    oid = _seed_order(ledger, market_id="0xfill", our_price=0.50,
                       fair_prob_at_post=0.60)
    gm = {"closed": False, "clobTokenIds": '["tok_fill","tok_no"]'}
    scanner = FakeBookScanner({"tok_fill":
                                  {"asks": [{"price": "0.45", "size": "100"}]}})
    import requests
    monkeypatch.setattr(requests, "Session",
                          lambda: FakeGammaSession({"0xfill": gm}))
    res = s.simulate_fills_and_grade(ledger, scanner, "http://gamma",
                                       bankroll=None, verbose=False)
    assert res["filled"] == 1
    filled = ledger.list_sharpline_orders("FILLED")
    assert len(filled) == 1
    r = filled[0]
    assert r["line_at_fill"] == pytest.approx(0.45)
    # 0.60 - 0.45 = +0.15. Positive => picked off per convention.
    assert r["adverse_selection"] == pytest.approx(0.15, abs=1e-6)
    assert r["adverse_selection"] > 0
    try:
        os.unlink(path)
    except OSError:
        pass


def test_adverse_selection_negative_when_line_moves_in_our_favor(monkeypatch):
    """YES buy at limit 0.70 (we thought fair=0.60). Best ask drops to
    0.65, we fill. Market now values YES at 0.65 > 0.60 fair - so YES
    became MORE likely vs the sharp; that's good for our long-YES
    position. adverse_selection must be NEGATIVE (line moved IN FAVOR
    of us per the project-wide convention)."""
    monkeypatch.setenv("ODDS_API_KEY", "")
    import foundation.odds_api as oa
    monkeypatch.setattr(oa, "load_dotenv", lambda path=".env": None)
    ledger, path = _temp_ledger()
    cfg = {"strategies": {"sharpline": {}}}
    s = Sharpline(cfg)
    _seed_order(ledger, market_id="0xadv", our_price=0.70,
                 fair_prob_at_post=0.60)
    gm = {"closed": False, "clobTokenIds": '["tok_adv","tok_no"]'}
    scanner = FakeBookScanner({"tok_adv":
                                  {"asks": [{"price": "0.65", "size": "100"}]}})
    import requests
    monkeypatch.setattr(requests, "Session",
                          lambda: FakeGammaSession({"0xadv": gm}))
    s.simulate_fills_and_grade(ledger, scanner, "http://gamma", verbose=False)
    r = ledger.list_sharpline_orders("FILLED")[0]
    # 0.60 - 0.65 = -0.05; negative => line moved in favor of us.
    assert r["adverse_selection"] < 0
    assert r["adverse_selection"] == pytest.approx(-0.05, abs=1e-6)
    try:
        os.unlink(path)
    except OSError:
        pass


def test_adverse_selection_sign_for_no_side_inverts(monkeypatch):
    """NO buy. fair P(YES)=0.60 at post. line_at_fill = 0.55 (market
    thinks YES less likely now -> NO became MORE valuable -> favorable
    for our long-NO). adverse should be NEGATIVE.

    Mirror: line_at_fill = 0.65 (market thinks YES MORE likely now ->
    NO became LESS valuable -> picked off). adverse should be POSITIVE.
    """
    monkeypatch.setenv("ODDS_API_KEY", "")
    import foundation.odds_api as oa
    monkeypatch.setattr(oa, "load_dotenv", lambda path=".env": None)
    ledger, path = _temp_ledger()
    cfg = {"strategies": {"sharpline": {}}}
    s = Sharpline(cfg)
    # Favorable NO case: line moves DOWN from sharp's 0.60 to 0.55.
    # For a NO buy on YES-token-NO we don't actually walk the NO ask
    # book; we use the SAME _simulate path. Seed an order side='NO' and
    # let it fill against an ask < limit.
    _seed_order(ledger, market_id="0xnofav", side="NO",
                 our_price=0.50, fair_prob_at_post=0.60)
    gm = {"closed": False, "clobTokenIds": '["tok_nofav","tok_no"]'}
    scanner = FakeBookScanner({"tok_nofav":
                                  {"asks": [{"price": "0.45", "size": "100"}]}})
    import requests
    monkeypatch.setattr(requests, "Session",
                          lambda: FakeGammaSession({"0xnofav": gm}))
    s.simulate_fills_and_grade(ledger, scanner, "http://gamma", verbose=False)
    r = ledger.list_sharpline_orders("FILLED")[0]
    # NO side: adverse = line - fair = 0.45 - 0.60 = -0.15 (line down =>
    # YES less likely => good for our NO).
    assert r["adverse_selection"] == pytest.approx(-0.15, abs=1e-6)
    try:
        os.unlink(path)
    except OSError:
        pass


def test_unfilled_at_resolution_counterfactual_grades(monkeypatch):
    """Order never filled; market resolves YES. Side was YES -> counterfactual
    MISSED_WIN. realized_pnl should be POSITIVE."""
    monkeypatch.setenv("ODDS_API_KEY", "")
    import foundation.odds_api as oa
    monkeypatch.setattr(oa, "load_dotenv", lambda path=".env": None)
    ledger, path = _temp_ledger()
    cfg = {"strategies": {"sharpline": {}}}
    s = Sharpline(cfg)
    _seed_order(ledger, market_id="0xresolved", side="YES",
                 our_price=0.50, fair_prob_at_post=0.60)
    gm = {"closed": True, "outcomePrices": '["1.0","0.0"]',
          "clobTokenIds": '["tok_r","tok_n"]'}
    scanner = FakeBookScanner({})
    import requests
    monkeypatch.setattr(requests, "Session",
                          lambda: FakeGammaSession({"0xresolved": gm}))
    res = s.simulate_fills_and_grade(ledger, scanner, "http://gamma", verbose=False)
    assert res["unfilled_resolved"] == 1
    rows = ledger.list_sharpline_orders("UNFILLED_RESOLVED")
    assert len(rows) == 1
    r = rows[0]
    assert r["resolved_outcome"] == "MISSED_WIN"
    # 10 USD stake, would have bought 20 shares at 0.50 -> 20 - 10 = 10
    assert r["realized_pnl"] == pytest.approx(10.0, abs=1e-6)
    try:
        os.unlink(path)
    except OSError:
        pass


def test_request_budget_blocks_when_exhausted(monkeypatch):
    """Mock the API to never be called when the budget says 0 remaining."""
    monkeypatch.setenv("ODDS_API_KEY", "fake")
    ledger, path = _temp_ledger()
    # Burn the budget.
    from foundation.odds_api import OddsAPI
    api = OddsAPI(ledger, monthly_cap=1)
    # Insert 1 fake request directly
    from datetime import datetime, timezone
    month = datetime.now(timezone.utc).strftime("%Y-%m")
    ledger.record_odds_api_request(month, "esports_csgo", status_code=200)
    # Now budget remaining is 0
    assert api.budget_status()["remaining"] == 0
    assert api.fetch_odds("esports_csgo") == []
    try:
        os.unlink(path)
    except OSError:
        pass
