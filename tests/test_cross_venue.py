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
    # Check that no cv_position was opened on FUZZY pair
    import sqlite3
    c = sqlite3.connect(db_path)
    c.row_factory = sqlite3.Row
    positions = list(c.execute("SELECT * FROM cv_positions").fetchall())
    pairs = list(c.execute("SELECT id, classification FROM cv_pairs").fetchall())
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
