"""Master cycle deadline plumbing.

A cycle must NEVER die by the GitHub Actions job timeout. The master
deadline is created at cycle start and passed to every phase; each
strategy checks it between units of work and exits cleanly. These tests
pin the contract:

  - Deadline helper: in_minutes, none, left, expired, coerce(None).
  - scan_arb (bucket_arb) takes deadline=, walks zero candidates when
    expired, counts them in skipped_budget so the diagnostic is honest.
  - scan_cv (cross_venue_arb) takes deadline=, skips the Kalshi
    discovery loop entirely when expired before any per-category fetch
    is invoked.
  - scan_all takes deadline=, stops between tags when expired.
"""
from __future__ import annotations

import os
import sys
import tempfile

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from foundation.deadline import Deadline


# ---------------------------------------------------------------- helper
def test_deadline_left_and_expired():
    d = Deadline.in_minutes(0.0)
    # Within the first millisecond of construction, the deadline is
    # already expired by definition (target = now).
    assert d.expired() is True
    assert d.left() <= 0.0


def test_deadline_in_minutes_positive():
    d = Deadline.in_minutes(5.0)
    assert not d.expired()
    assert 290.0 < d.left() <= 300.0


def test_deadline_none_never_expires():
    d = Deadline.none()
    assert not d.expired()
    assert d.left() > 24 * 3600  # at least a full day in the future


def test_deadline_coerce_none_becomes_open():
    """API boundaries pass None as 'no master deadline'. coerce must
    upgrade it to a sentinel so the caller never has to check for None."""
    d = Deadline.coerce(None)
    assert isinstance(d, Deadline)
    assert not d.expired()


def test_deadline_coerce_passthrough_existing():
    src = Deadline.in_minutes(3)
    assert Deadline.coerce(src) is src


# ---------------------------------------------------------------- scan_arb
def test_scan_arb_respects_master_deadline():
    """A bucket_arb scan_arb call with deadline already expired walks
    zero candidates and counts every walk-eligible event in
    skipped_budget. (The per-strategy scan_budget_minutes is left at its
    default 7.0 so this proves the MASTER deadline -- not the local
    budget -- did the bounding.)"""
    from strategies.base import ArbEvent
    from strategies.bucket_arb import BucketSumArb
    from tests.test_bucket_arb import _scoring_event
    cfg = {"strategies": {"bucket_arb": {
        "safety_buffer": 0.005, "target_shares": 100.0,
        "slippage_cents": 0.01, "min_executable_shares": 5.0,
        "walk_band": 0.50,
        "max_walks_per_cycle": 1000,
        "scan_budget_minutes": 60.0,   # local budget is wide-open
    }}}
    s = BucketSumArb(cfg)
    events = [_scoring_event(f"e{i}", 0.10 + 0.05 * i) for i in range(3)]
    result = s.scan_arb(events, scanner=None, verbose=False,
                         deadline=Deadline.in_minutes(0.0))
    assert result["counters"]["walked"] == 0
    assert result["counters"]["skipped_budget"] == 3
    assert not result["detections"]


# ---------------------------------------------------------------- scan_cv
def test_scan_cv_respects_master_deadline_before_kalshi(monkeypatch):
    """A cross_venue_arb scan_cv with master deadline already expired
    must abort BEFORE calling Kalshi fetch_markets even once. The local
    cv_scan_budget_minutes is left at 60.0 so this proves the master
    bounded the fetch."""
    from foundation.ledger import Ledger
    from strategies.cross_venue_arb import CrossVenueArb
    f = tempfile.NamedTemporaryFile(suffix=".db", delete=False); f.close()
    try:
        ledger = Ledger(f.name)
        cfg = {"strategies": {"cross_venue_arb": {
            "kalshi_categories": ["Climate and Weather", "Sports"],
            "kalshi_round_robin": False,
            "cv_scan_budget_minutes": 60.0,  # local budget wide-open
        }}}
        strat = CrossVenueArb(cfg)
        fetched: list[str | None] = []
        class FakePoly:
            def fetch_markets(self, category_hint=None): return []
            def fetch_book_for(self, vm): return None
        class FakeKal:
            def fetch_markets(self, category_hint=None):
                fetched.append(category_hint)
                return []
            def fetch_book_for(self, vm): return None
        # Master deadline expired -> _budget_left() returns False right
        # at the start, no Kalshi category is fetched.
        import time as time_mod
        base = time_mod.time()
        monkeypatch.setattr(time_mod, "time", lambda: base + 60.0)
        result = strat.scan_cv(FakePoly(), FakeKal(), ledger,
                                  verbose=False,
                                  deadline=Deadline.in_minutes(0.0))
        assert fetched == []
        assert result["counters"]["kalshi_markets"] == 0
    finally:
        try:
            os.unlink(f.name)
        except OSError:
            pass


# ---------------------------------------------------------------- scan_all
def test_scan_all_stops_between_tags_when_deadline_expired(capsys):
    """scan_all loops over configured tags. When master deadline has
    expired before any tag is fetched, it returns an empty list and
    prints a 'skipping remaining tags' line so the operator can see
    what was truncated."""
    from main import scan_all
    class _StubScanner:
        def __init__(self):
            self.calls: list[str] = []
        def scan_tag(self, tag_slug, fetch_books=True):
            self.calls.append(tag_slug)
            return ([], [])
    sc = _StubScanner()
    cfg = {"scanner": {"tag_slugs": ["a", "b", "c"]}}
    out = scan_all(cfg, sc, fetch_books=False,
                     deadline=Deadline.in_minutes(0.0))
    assert out == []
    assert sc.calls == []   # never fetched any tag
    captured = capsys.readouterr().out
    assert "skipping remaining tags" in captured


def test_kalshi_fetch_markets_respects_deadline(monkeypatch):
    """v2.3 fix: KalshiVenue.fetch_markets must check the deadline
    between series AND between events. A single category with N series
    was previously able to hold the lock for 20+ minutes, killing
    cycle 27443485735 on Sports discovery alone."""
    from foundation.venues.kalshi import KalshiVenue
    v = KalshiVenue()
    # Stub the three HTTP helpers so no real network call fires.
    fake_series = [{"ticker": f"S{i}", "frequency": "daily",
                     "settlement_sources": [], "fee_multiplier": 1.0,
                     "fee_type": "quadratic"} for i in range(5)]
    fake_events = [{"event_ticker": f"E{i}", "title": "ev"}
                   for i in range(3)]
    fake_markets = [{"ticker": f"M{i}", "title": "m",
                      "status": "active",
                      "yes_sub_title": "X", "no_sub_title": "Y",
                      "rules_primary": "", "rules_secondary": "",
                      "close_time": "2026-06-13T00:00:00Z"} for i in range(2)]
    monkeypatch.setattr(v, "load_series_index",
                          lambda category: fake_series)
    monkeypatch.setattr(v, "fetch_events_for_series",
                          lambda ticker: fake_events)
    monkeypatch.setattr(v, "fetch_markets_for_event",
                          lambda et: fake_markets)
    # Already-expired deadline: should return zero markets without
    # iterating any series.
    out_zero = v.fetch_markets(category_hint="Sports",
                                  deadline=Deadline.in_minutes(0.0))
    assert out_zero == []
    # Open deadline: should iterate everything (5 series * 3 events *
    # 2 markets = 30 markets).
    out_full = v.fetch_markets(category_hint="Sports",
                                  deadline=Deadline.none())
    assert len(out_full) == 30


def test_scan_all_runs_when_no_deadline():
    """deadline=None must NOT bound scan_all (manual CLI usage)."""
    from main import scan_all
    from strategies.base import Market
    class _StubScanner:
        def __init__(self):
            self.calls: list[str] = []
        def scan_tag(self, tag_slug, fetch_books=True):
            self.calls.append(tag_slug)
            mk = Market(
                market_id=f"m-{tag_slug}", slug=tag_slug, question="q",
                category="c", rules_text="",
                resolve_date=None, end_date_iso=None,
                yes_token_id=None, no_token_id=None,
                yes_ask=None, yes_bid=None, no_ask=None, no_bid=None,
            )
            return ([], [mk])
    sc = _StubScanner()
    cfg = {"scanner": {"tag_slugs": ["a", "b"]}}
    out = scan_all(cfg, sc, fetch_books=False, deadline=None)
    assert sc.calls == ["a", "b"]
    assert len(out) == 2
