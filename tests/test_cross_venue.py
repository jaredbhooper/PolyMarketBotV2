"""Cross-venue strategy tests. HTTP is never called - we synthesize
VenueMarket objects directly."""
from __future__ import annotations

import os
import sys
import tempfile

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from foundation.equivalence import (classify_pair, parse_bucket,
                                     buckets_overlap_equivalently,
                                     normalize_source, detect_city,
                                     detect_date)
from foundation.ledger import Ledger
from foundation.venues.base import VenueMarket
from foundation.venues.kalshi import KalshiVenue
from strategies.cross_venue_arb import (CrossVenueArb, walk_book_for_shares,
                                          kalshi_quadratic_fee,
                                          bucket_markets_by_key)


def _mk(venue, mid, title, leg, src, src_url, close, yes_asks, no_asks,
        extras=None, fees=None):
    return VenueMarket(
        venue=venue, venue_market_id=mid, venue_event_id="ev",
        title=title, leg_title=leg, rules_text="rules",
        settlement_source=src, settlement_source_url=src_url,
        close_time_iso=close,
        yes_asks=yes_asks, no_asks=no_asks,
        yes_bid=yes_asks[0]["price"] - 0.01 if yes_asks else None,
        no_bid=no_asks[0]["price"] - 0.01 if no_asks else None,
        fee_model=fees or {"type": "flat", "per_contract": 0.0},
        extras=extras or {},
    )


# -------------------------------------------------------------- helpers
def test_walk_book_for_shares():
    asks = [{"price": 0.10, "size": 50}, {"price": 0.11, "size": 100}]
    vwap, filled, levels = walk_book_for_shares(asks, 100)
    assert filled == 100
    assert abs(vwap - 0.105) < 1e-9
    # depth overflow
    vwap2, filled2, _ = walk_book_for_shares(asks, 1000)
    assert filled2 == 150


def test_kalshi_quadratic_fee():
    # ceil(0.07 * 0.5 * 0.5 * 100) = ceil(1.75) = 2c
    assert kalshi_quadratic_fee(0.5) == 0.02
    # edge: 0.99 -> ceil(0.07 * 0.99 * 0.01 * 100) = ceil(0.0693)=1c
    assert kalshi_quadratic_fee(0.99) == 0.01
    # 0 and 1 -> 0
    assert kalshi_quadratic_fee(0.0) == 0.0
    assert kalshi_quadratic_fee(1.0) == 0.0


# -------------------------------------------------------------- equivalence
def test_parse_bucket_basic():
    b = parse_bucket("87F")
    assert b["unit"] == "F" and abs(b["lo_f"] - 86.5) < 1e-6 and abs(b["hi_f"] - 87.5) < 1e-6


def test_parse_bucket_range():
    b = parse_bucket("90-91F")
    assert abs(b["lo_f"] - 89.5) < 1e-6 and abs(b["hi_f"] - 91.5) < 1e-6


def test_parse_bucket_open_ended():
    b1 = parse_bucket("13C or below")
    assert b1["lo_f"] == float("-inf")
    b2 = parse_bucket("32C or higher")
    assert b2["hi_f"] == float("inf")


def test_normalize_source_known():
    assert normalize_source("Wunderground KORD (https://...)") == "wunderground"
    assert normalize_source("NWS Climatological Report") == "nws_climatological"
    assert normalize_source("National Weather Service") == "nws"
    assert normalize_source("Bogus Inc") == "unknown"


def test_detect_city_and_date():
    assert detect_city("Highest temperature in Miami on Jun 11") == "miami"
    assert detect_city("LaGuardia (NYC) reading") == "nyc"
    assert detect_date("Highest temp in NYC on Jun 11, 2026") == "2026-06-11"
    assert detect_date("KXHIGHNY-26JUN10") == "2026-06-10"
    assert detect_date("2026-06-11T00:00:00Z") == "2026-06-11"


# -------------------------------------------------------------- classification
SAME_SOURCE_KAL = "NWS Climatological Report Miami (Kalshi)"
SAME_SOURCE_POLY = "NWS Climatological Report Miami"


def test_classify_certified_identical_when_all_match():
    poly = _mk("polymarket", "0xpoly", "Highest temp Miami Jun 11, 2026?",
               "87F", SAME_SOURCE_POLY, "https://nws/x", "2026-06-12T05:00:00Z",
               [{"price": 0.10, "size": 100}], [{"price": 0.85, "size": 100}],
               extras={"kind": "max"})
    kal = _mk("kalshi", "KXHIGHMIA-26JUN11-T87", "Highest temp Miami Jun 11, 2026?",
              "87F", SAME_SOURCE_KAL, "https://nws/x", "2026-06-12T04:59:00Z",
              [{"price": 0.08, "size": 100}], [{"price": 0.93, "size": 100}],
              extras={"series_ticker": "KXHIGHMIA"})
    r = classify_pair(poly, kal)
    assert r.classification == "CERTIFIED-IDENTICAL"
    assert r.divergence_risk_note == ""


def test_classify_fuzzy_when_source_differs():
    poly = _mk("polymarket", "0xpoly", "Highest temp Miami Jun 11, 2026?",
               "87F", "Wunderground KMIA", "https://wu/x", "2026-06-12T05:00:00Z",
               [{"price": 0.10, "size": 100}], [{"price": 0.85, "size": 100}],
               extras={"kind": "max"})
    kal = _mk("kalshi", "KXHIGHMIA-26JUN11-T87", "Highest temp Miami Jun 11, 2026?",
              "87F", "NWS Climatological Report Miami (Kalshi)",
              "https://nws/x", "2026-06-12T04:59:00Z",
              [{"price": 0.08, "size": 100}], [{"price": 0.93, "size": 100}],
              extras={"series_ticker": "KXHIGHMIA"})
    r = classify_pair(poly, kal)
    assert r.classification == "FUZZY"
    assert "source" in r.reason
    # Divergence note must be populated when sources differ
    assert "Resolution sources differ" in r.divergence_risk_note


def test_classify_nonmatch_when_buckets_disjoint():
    poly = _mk("polymarket", "0xpoly", "Highest temp Miami Jun 11, 2026?",
               "70F", SAME_SOURCE_POLY, "https://nws/x", "2026-06-12T05:00:00Z",
               [{"price": 0.10, "size": 100}], [{"price": 0.85, "size": 100}],
               extras={"kind": "max"})
    kal = _mk("kalshi", "KXHIGHMIA-26JUN11-T87", "Highest temp Miami Jun 11, 2026?",
              "87F", SAME_SOURCE_KAL, "https://nws/x", "2026-06-12T04:59:00Z",
              [{"price": 0.08, "size": 100}], [{"price": 0.93, "size": 100}],
              extras={"series_ticker": "KXHIGHMIA"})
    r = classify_pair(poly, kal)
    assert r.classification == "NON-MATCH"


def test_classify_nonmatch_when_event_differs():
    poly = _mk("polymarket", "0xpoly", "Highest temp Miami Jun 11, 2026?",
               "87F", SAME_SOURCE_POLY, "https://nws/x", "2026-06-12T05:00:00Z",
               [{"price": 0.10, "size": 100}], [{"price": 0.85, "size": 100}],
               extras={"kind": "max"})
    kal = _mk("kalshi", "KXHIGHNYC-26JUN11-T87", "Highest temp NYC Jun 11, 2026?",
              "87F", SAME_SOURCE_POLY, "https://nws/x", "2026-06-12T04:59:00Z",
              [{"price": 0.08, "size": 100}], [{"price": 0.93, "size": 100}],
              extras={"series_ticker": "KXHIGHNY"})
    r = classify_pair(poly, kal)
    assert r.classification == "NON-MATCH"


# -------------------------------------------------------------- bucket grouping
def test_bucket_markets_by_key_filters_unknown_city_date():
    m1 = _mk("polymarket", "id1", "Highest temp Miami on Jun 11, 2026?",
             "87F", "Wunderground", None, "2026-06-12T00:00:00Z",
             [{"price": 0.1, "size": 100}], [{"price": 0.9, "size": 100}],
             extras={"kind": "max"})
    m2 = _mk("polymarket", "id2", "Unknown event", "x",
             "Wunderground", None, "2026-06-12T00:00:00Z",
             [{"price": 0.1, "size": 100}], [{"price": 0.9, "size": 100}])
    buckets = bucket_markets_by_key([m1, m2])
    assert ("miami", "2026-06-11", "max") in buckets
    # m2 has no city detected -> not bucketed
    assert all(len(v) == 1 for v in buckets.values())


# -------------------------------------------------------------- end-to-end
def test_scan_cv_logs_certified_and_fires_above_threshold(monkeypatch):
    """Synthetic venues. The CERTIFIED-IDENTICAL pair has a cheap fillable
    arb (sum_yes 0.06 + sum_no 0.05 + fees + slip well under $1). The
    detector should log the gap AND fire it. FUZZY pair gets logged but
    NEVER fires."""
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = f.name
    ledger = Ledger(db_path)
    cfg = {"strategies": {"cross_venue_arb": {
        "safety_buffer": 0.005,
        "min_arb_profit": 1.0,
        "target_shares": 100.0,
        "poly_slippage_cents": 0.01,
        "kalshi_slippage_cents": 0.01,
        "min_executable_shares": 5.0,
        "execute_fuzzy": False,
        "kalshi_categories": ["Climate and Weather"],
    }}}
    strat = CrossVenueArb(cfg)

    # CERTIFIED-IDENTICAL pair: both side asks ~0.05 -> total cost ~0.10
    # plus 2c slip + ~1c fees = locked profit ~0.86 per share, $86 USD on
    # 100 shares.  Cleared, will fire.
    poly_cert = _mk("polymarket", "0xC1", "Highest temp Miami Jun 11, 2026?",
                    "87F", SAME_SOURCE_POLY, "https://nws/x",
                    "2026-06-12T05:00:00Z",
                    [{"price": 0.05, "size": 200}], [{"price": 0.05, "size": 200}],
                    extras={"kind": "max"})
    kal_cert = _mk("kalshi", "KXHIGHMIA-26JUN11-T87",
                    "Highest temp Miami Jun 11, 2026?", "87F",
                    SAME_SOURCE_KAL, "https://nws/x",
                    "2026-06-12T04:59:00Z",
                    [{"price": 0.06, "size": 200}], [{"price": 0.04, "size": 200}],
                    extras={"series_ticker": "KXHIGHMIA"},
                    fees={"type": "quadratic", "multiplier": 1.0})
    # FUZZY pair: different source. Should NEVER fire even if profitable.
    poly_fuz = _mk("polymarket", "0xF1", "Highest temp NYC Jun 11, 2026?",
                    "87F", "Wunderground KLGA", "https://wu/x",
                    "2026-06-12T05:00:00Z",
                    [{"price": 0.05, "size": 200}], [{"price": 0.05, "size": 200}],
                    extras={"kind": "max"})
    kal_fuz = _mk("kalshi", "KXHIGHNY-26JUN11-T87",
                    "Highest temp NYC Jun 11, 2026?", "87F",
                    "NWS Climatological Report NYC (Kalshi)", "https://nws/y",
                    "2026-06-12T04:59:00Z",
                    [{"price": 0.06, "size": 200}], [{"price": 0.04, "size": 200}],
                    extras={"series_ticker": "KXHIGHNY"},
                    fees={"type": "quadratic", "multiplier": 1.0})

    class FakePoly:
        def fetch_markets(self, category_hint=None): return [poly_cert, poly_fuz]
    class FakeKal:
        def fetch_markets(self, category_hint=None): return [kal_cert, kal_fuz]

    result = strat.scan_cv(FakePoly(), FakeKal(), ledger, verbose=False)
    counters = result["counters"]
    assert counters["certified"] >= 1
    assert counters["fuzzy"] >= 1
    # At least one fire on the CERTIFIED pair
    assert counters["fired"] >= 1
    # Check that no cv_position was opened on FUZZY pair. cv_pairs lives
    # in cache.db; raw_connect attaches it as schema `cache`.
    c = ledger.raw_connect()
    positions = list(c.execute("SELECT * FROM cv_positions").fetchall())
    pairs = list(c.execute("SELECT id, classification FROM cache.cv_pairs").fetchall())
    pair_class = {int(r["id"]): r["classification"] for r in pairs}
    for p in positions:
        cl = pair_class.get(int(p["pair_id"]))
        assert cl == "CERTIFIED-IDENTICAL", \
            f"Fired pos on non-CERTIFIED pair (classification={cl})"
    c.close()
    try:
        os.unlink(db_path)
    except OSError:
        pass     # Windows file-lock during teardown is harmless


# -------------------------------------------------------------- v2: multi-category matcher + confidence
def test_classify_certified_sports_when_teams_date_settlement_align():
    """Synthetic identical sports market: same teams, same date, same OT
    rules, same source code, cutoffs within 24h -> CERTIFIED-IDENTICAL
    with confidence >= 0.9."""
    poly = _mk("polymarket", "0xS1",
                "NBA: Lakers vs Celtics on Jun 11, 2026?",
                "Lakers", "Official scoring NBA",
                "https://nba/x", "2026-06-12T05:00:00Z",
                [{"price": 0.50, "size": 100}],
                [{"price": 0.50, "size": 100}],
                extras={"event_slug": "lakers-celtics-2026-06-11"})
    poly.rules_text = "Includes overtime. Settles via official NBA scoring."
    kal = _mk("kalshi", "KXNBA-LAKCEL-26JUN11",
               "NBA: Lakers vs Celtics on Jun 11, 2026?",
               "Lakers", "Official scoring NBA (Kalshi)",
               "https://nba/x", "2026-06-12T04:59:00Z",
               [{"price": 0.51, "size": 100}],
               [{"price": 0.49, "size": 100}],
               extras={"series_ticker": "KXNBA",
                       "series_title": "NBA Game Winner Lakers Celtics"})
    kal.rules_text = "Includes overtime. Settles via official NBA scoring."
    r = classify_pair(poly, kal)
    assert r.category == "sports"
    assert r.confidence >= 0.9
    assert r.classification == "CERTIFIED-IDENTICAL"


def test_classify_sports_below_confidence_threshold_is_not_certified():
    """Mismatched OT-rule mention drops confidence under 0.9 and
    classification falls to FUZZY -- the conservatism gate."""
    poly = _mk("polymarket", "0xS2",
                "NBA: Lakers vs Celtics on Jun 11, 2026?",
                "Lakers", "Official scoring NBA",
                "https://nba/x", "2026-06-12T05:00:00Z",
                [{"price": 0.50, "size": 100}],
                [{"price": 0.50, "size": 100}],
                extras={"event_slug": "lakers-celtics-2026-06-11"})
    poly.rules_text = "Settles in regulation only; ties refunded."
    kal = _mk("kalshi", "KXNBA-LAKCEL-26JUN11",
               "NBA: Lakers vs Celtics on Jun 11, 2026?",
               "Lakers", "Official scoring NBA (Kalshi)",
               "https://nba/x", "2026-06-12T04:59:00Z",
               [{"price": 0.51, "size": 100}],
               [{"price": 0.49, "size": 100}],
               extras={"series_ticker": "KXNBA",
                       "series_title": "NBA Game Winner Lakers Celtics"})
    # Different overtime handling -- divergent settlement edge.
    kal.rules_text = "Includes overtime. Settles via official NBA scoring."
    r = classify_pair(poly, kal)
    # Not necessarily NON-MATCH (teams align), but must NOT be CERTIFIED
    # since the OT mention diverges.
    assert r.classification != "CERTIFIED-IDENTICAL"
    assert r.confidence < 0.95


def test_classify_crypto_pair_matches_on_asset_strike_source():
    """Synthetic BTC > $80k on Jun 11; same asset, same strike, same
    source, same cutoff -> CERTIFIED, confidence >= 0.9."""
    poly = _mk("polymarket", "0xCR1",
                "Bitcoin price above $80,000 on Jun 11, 2026?",
                "$80,000", "CoinGecko BTC",
                "https://coingecko/btc", "2026-06-12T05:00:00Z",
                [{"price": 0.30, "size": 100}],
                [{"price": 0.70, "size": 100}],
                extras={})
    kal = _mk("kalshi", "KXBTC-26JUN11-T80000",
               "BTC > $80,000 on Jun 11, 2026?",
               "$80,000", "CoinGecko BTC (Kalshi)",
               "https://coingecko/btc", "2026-06-12T05:30:00Z",
               [{"price": 0.31, "size": 100}],
               [{"price": 0.69, "size": 100}],
               extras={"series_ticker": "KXBTC",
                       "series_title": "BTC Daily Strike"})
    r = classify_pair(poly, kal)
    assert r.category == "crypto"
    assert r.classification == "CERTIFIED-IDENTICAL"
    assert r.confidence >= 0.9


def test_classify_returns_confidence_field_on_every_result():
    """Schema sanity: every EquivalenceResult carries a confidence in
    [0, 1] regardless of category or classification."""
    poly = _mk("polymarket", "0xX", "Random title", "X",
                "Some source", None, "2026-06-12T05:00:00Z",
                [{"price": 0.5, "size": 10}], [{"price": 0.5, "size": 10}])
    kal = _mk("kalshi", "K-X", "Other title", "Y",
               "Some source", None, "2026-06-12T05:00:00Z",
               [{"price": 0.5, "size": 10}], [{"price": 0.5, "size": 10}])
    r = classify_pair(poly, kal)
    assert hasattr(r, "confidence")
    assert 0.0 <= r.confidence <= 1.0
    assert hasattr(r, "category") and r.category != ""


# -------------------------------------------------------------- v2: time budget
def test_scan_cv_respects_time_budget(monkeypatch):
    """Set cv_scan_budget_minutes to ~0; the deadline must be reached
    before the classifier even starts pairing, so certified + fuzzy +
    nonmatch all stay at zero. Two synthetic weather buckets are present
    -- without a budget they'd both classify."""
    poly_a = _mk("polymarket", "0xpA",
                  "Highest temp Miami on Jun 11, 2026?", "87F",
                  SAME_SOURCE_POLY, "https://nws/x",
                  "2026-06-12T05:00:00Z",
                  [{"price": 0.05, "size": 200}],
                  [{"price": 0.05, "size": 200}],
                  extras={"kind": "max"})
    kal_a = _mk("kalshi", "KXHIGHMIA-26JUN11-T87",
                 "Highest temp Miami on Jun 11, 2026?", "87F",
                 SAME_SOURCE_KAL, "https://nws/x",
                 "2026-06-12T04:59:00Z",
                 [{"price": 0.06, "size": 200}],
                 [{"price": 0.04, "size": 200}],
                 extras={"series_ticker": "KXHIGHMIA"},
                 fees={"type": "quadratic", "multiplier": 1.0})
    poly_b = _mk("polymarket", "0xpB",
                  "Highest temp NYC on Jun 11, 2026?", "87F",
                  SAME_SOURCE_POLY, "https://nws/y",
                  "2026-06-12T05:00:00Z",
                  [{"price": 0.05, "size": 200}],
                  [{"price": 0.05, "size": 200}],
                  extras={"kind": "max"})
    kal_b = _mk("kalshi", "KXHIGHNY-26JUN11-T87",
                 "Highest temp NYC on Jun 11, 2026?", "87F",
                 SAME_SOURCE_KAL, "https://nws/y",
                 "2026-06-12T04:59:00Z",
                 [{"price": 0.06, "size": 200}],
                 [{"price": 0.04, "size": 200}],
                 extras={"series_ticker": "KXHIGHNY"},
                 fees={"type": "quadratic", "multiplier": 1.0})

    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = f.name
    ledger = Ledger(db_path)
    cfg = {"strategies": {"cross_venue_arb": {
        "safety_buffer": 0.005,
        "min_arb_profit": 1.0,
        "target_shares": 50.0,
        "poly_slippage_cents": 0.01,
        "kalshi_slippage_cents": 0.01,
        "min_executable_shares": 5.0,
        "execute_fuzzy": False,
        "kalshi_categories": ["Climate and Weather"],
        # Tiny budget -> deadline fires after the first bucket.
        "cv_scan_budget_minutes": 0.0,
    }}}
    strat = CrossVenueArb(cfg)

    class FakePoly:
        def fetch_markets(self, category_hint=None): return [poly_a, poly_b]
        def fetch_book_for(self, vm): return None
    class FakeKal:
        def fetch_markets(self, category_hint=None): return [kal_a, kal_b]
        def fetch_book_for(self, vm): return None

    # `import time as _time` inside scan_cv creates a local alias to the
    # global time module. Patching time.time on the global module is seen
    # via that alias, so the deadline check fires on the first iteration.
    import time as time_mod
    base = time_mod.time()
    def _t():
        # Always far past `base + 0` (== deadline with 0-min budget).
        return base + 60.0
    monkeypatch.setattr(time_mod, "time", _t)

    result = strat.scan_cv(FakePoly(), FakeKal(), ledger, verbose=False)
    # Deadline exceeded before any pair processed.
    assert result["counters"]["certified"] == 0
    assert result["counters"]["fuzzy"] == 0
    assert result["counters"]["nonmatch"] == 0
    try:
        os.unlink(db_path)
    except OSError:
        pass


# -------------------------------------------------------------- v2.2 cv round-robin
def test_kalshi_round_robin_advances_pointer_each_cycle():
    """With kalshi_round_robin=True, _pick_kalshi_categories must
    advance the pointer in cv_state every call so each cycle scans the
    NEXT configured category. After len(categories) calls the rotation
    wraps. Persistence is the whole point -- cycle workflow runs are
    ephemeral, so the pointer has to survive in ledger.db."""
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = f.name
    ledger = Ledger(db_path)
    rotation = ["Climate and Weather", "Sports", "Financials",
                "Politics", "Economics"]
    cfg = {"strategies": {"cross_venue_arb": {
        "kalshi_categories": rotation,
        "category_rotation": rotation,
        "kalshi_round_robin": True,
    }}}
    strat = CrossVenueArb(cfg)
    # Drive 7 calls (one cycle past one full wrap) and verify the order.
    picks = [strat._pick_kalshi_categories(ledger)[0] for _ in range(7)]
    assert picks == rotation + rotation[:2]
    # Pointer must be persisted in cv_state.
    assert ledger.cv_state_get("kalshi_rr_last") == rotation[1]
    try:
        os.unlink(db_path)
    except OSError:
        pass


def test_kalshi_round_robin_off_returns_all_categories():
    """When kalshi_round_robin=False (e.g. manual `cv` CLI), we want a
    one-shot sweep of every category, still bounded by the budget."""
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = f.name
    ledger = Ledger(db_path)
    cfg = {"strategies": {"cross_venue_arb": {
        "kalshi_categories": ["Climate and Weather", "Sports", "Crypto"],
        "kalshi_round_robin": False,
    }}}
    strat = CrossVenueArb(cfg)
    out = strat._pick_kalshi_categories(ledger)
    assert out == ["Climate and Weather", "Sports", "Crypto"]
    try:
        os.unlink(db_path)
    except OSError:
        pass


def test_scan_cv_budget_bounds_kalshi_fetch_phase(monkeypatch):
    """v2.2 fix: a zero-minute budget must abort cleanly inside the
    Kalshi fetch loop without ever invoking the per-category fetch (the
    previous build started the timer AFTER Kalshi discovery, so a
    multi-hour fetch could run unbounded). Synthesizes a Kalshi venue
    that records every category-hint it was asked to fetch."""
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = f.name
    ledger = Ledger(db_path)
    cfg = {"strategies": {"cross_venue_arb": {
        "kalshi_categories": ["Climate and Weather", "Sports"],
        "kalshi_round_robin": False,
        "cv_scan_budget_minutes": 0.0,
    }}}
    strat = CrossVenueArb(cfg)
    fetched_categories: list[str | None] = []
    class FakePoly:
        def fetch_markets(self, category_hint=None): return []
        def fetch_book_for(self, vm): return None
    class FakeKal:
        def fetch_markets(self, category_hint=None):
            fetched_categories.append(category_hint)
            return []
        def fetch_book_for(self, vm): return None
    import time as time_mod
    base = time_mod.time()
    monkeypatch.setattr(time_mod, "time", lambda: base + 60.0)
    result = strat.scan_cv(FakePoly(), FakeKal(), ledger, verbose=False)
    assert fetched_categories == [], fetched_categories
    assert result["counters"]["kalshi_markets"] == 0
    try:
        os.unlink(db_path)
    except OSError:
        pass


def test_kalshi_orderbook_normalization():
    """The Kalshi orderbook returns no_dollars = NO bid book ascending.
    The venue adapter should normalize to yes_asks = ascending price book
    where yes_ask_price = 1 - no_bid_price."""
    venue = KalshiVenue()
    # no_dollars: [(0.10, 50), (0.20, 100), (0.74, 30)]
    # YES asks = 1 - bid for each; sorted ascending:
    #   1 - 0.74 = 0.26 (top of YES asks), size 30
    #   1 - 0.20 = 0.80, size 100
    #   1 - 0.10 = 0.90, size 50
    yes_asks = venue._to_asks_from_opposite_bids(
        [["0.10", "50"], ["0.20", "100"], ["0.74", "30"]])
    prices = [round(a["price"], 4) for a in yes_asks]
    sizes = [a["size"] for a in yes_asks]
    assert prices == [0.26, 0.80, 0.90]
    assert sizes == [30.0, 100.0, 50.0]
