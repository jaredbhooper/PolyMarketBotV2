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
