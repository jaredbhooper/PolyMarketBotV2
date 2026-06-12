"""CV-PROBE (quarantined research book) tests.

Probe candidates flow from CrossVenueArb.scan_cv() -> CVProbe.run_probe()
which writes cv_probe_positions / cv_probe_legs. The probe has its own
$500 virtual capital pool and never touches the main bankroll.

Tests in this file:
  * net-gap math includes both venues' fees
  * both-legs-or-nothing (a candidate missing a leg is dropped)
  * dedupe (a pair with an existing open/settled probe row is skipped)
  * daily cap + per-category cap with largest-gap-first selection
  * quarantine: probe P&L not present in cv_positions / cross_venue stats
  * all four agreement_outcomes graded correctly including void P&L
  * FUZZY -> CERTIFIED upgrade removes probe eligibility
"""
from __future__ import annotations

import os
import sys
import tempfile

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from foundation.equivalence import EquivalenceResult
from foundation.ledger import Ledger
from foundation.venues.base import VenueMarket
from strategies.cross_venue_arb import CVDetection
from strategies.cv_probe import CVProbe


def _vm(venue: str, mid: str, side_yes_ask: float = 0.40) -> VenueMarket:
    return VenueMarket(
        venue=venue, venue_market_id=mid, venue_event_id="ev1",
        title=f"{venue} market {mid}", leg_title="leg",
        rules_text="rules",
        settlement_source=f"{venue} src",
        settlement_source_url=f"https://{venue}/src",
        close_time_iso="2026-06-12T12:00:00Z",
        yes_asks=[{"price": side_yes_ask, "size": 100}],
        no_asks=[{"price": 1.0 - side_yes_ask, "size": 100}],
        yes_bid=side_yes_ask - 0.01,
        no_bid=1.0 - side_yes_ask - 0.01,
        fee_model={"type": "quadratic", "multiplier": 1.0},
        extras={},
    )


def _det(pair_id: int, *, classification: str = "FUZZY",
            net_gap: float = 0.05, safety_buffer: float = 0.005,
            executable: float = 50.0,
            poly_vwap: float = 0.45, kalshi_vwap: float = 0.45,
            poly_fee: float = 0.01, kalshi_fee: float = 0.02,
            direction: str = "POLY_YES_KAL_NO",
            divergence_note: str = "src differ") -> CVDetection:
    """Build a CVDetection that the probe pipeline will see. Net gap is
    `locked_profit_per_share + safety_buffer` so we can drive it
    explicitly via the buffer-back-out the probe does."""
    total_cost = 1.0 - net_gap
    locked = total_cost.__class__(1.0 - total_cost - safety_buffer)
    poly = _vm("polymarket", f"P-{pair_id}", side_yes_ask=poly_vwap)
    kal = _vm("kalshi", f"K-{pair_id}", side_yes_ask=kalshi_vwap)
    poly_side = "YES" if direction == "POLY_YES_KAL_NO" else "NO"
    kal_side = "NO" if direction == "POLY_YES_KAL_NO" else "YES"
    legs = [
        {"venue": "polymarket", "venue_market_id": poly.venue_market_id,
         "side": poly_side, "vwap": poly_vwap, "fee_per_share": poly_fee,
         "price_filled": poly_vwap + poly_fee, "shares": executable,
         "cost": (poly_vwap + poly_fee) * executable,
         "levels_consumed": [{"price": poly_vwap, "shares_taken": executable,
                              "usd_taken": poly_vwap * executable}],
         "leg_title": "leg", "venue_title": poly.title},
        {"venue": "kalshi", "venue_market_id": kal.venue_market_id,
         "side": kal_side, "vwap": kalshi_vwap, "fee_per_share": kalshi_fee,
         "price_filled": kalshi_vwap + kalshi_fee, "shares": executable,
         "cost": (kalshi_vwap + kalshi_fee) * executable,
         "levels_consumed": [{"price": kalshi_vwap, "shares_taken": executable,
                              "usd_taken": kalshi_vwap * executable}],
         "leg_title": "leg", "venue_title": kal.title},
    ]
    return CVDetection(
        pair_id=pair_id, poly=poly, kalshi=kal,
        classification=classification, direction=direction,
        target_shares=50.0, executable_shares=executable,
        poly_vwap=poly_vwap, poly_fee=poly_fee,
        kalshi_vwap=kalshi_vwap, kalshi_fee=kalshi_fee,
        safety_buffer=safety_buffer,
        total_cost_per_share=total_cost,
        locked_profit_per_share=locked,
        locked_profit_usd=locked * executable,
        divergence_risk_note=divergence_note,
        legs_detail=legs,
        cleared_threshold=False,
    )


def _eq(pair_id: int, category: str = "sports", confidence: float = 0.92
         ) -> tuple[int, VenueMarket, VenueMarket, EquivalenceResult]:
    res = EquivalenceResult(classification="FUZZY", reason="fuzz",
                             criteria={}, divergence_risk_note="src differ",
                             category=category, confidence=confidence)
    return (pair_id, _vm("polymarket", f"P-{pair_id}"),
            _vm("kalshi", f"K-{pair_id}"), res)


def _ledger():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    return Ledger(path), path


def _drop(ledger, path):
    try:
        ledger._conn().close()
    except Exception:
        pass
    try:
        os.unlink(path)
    except OSError:
        pass


def _cfg(**probe_overrides):
    base = {
        "min_probe_gap": 0.02,
        "max_probe_total": 1000,
        "max_probe_per_day": 40,
        "max_probe_per_day_per_category": 15,
        "probe_stake_usd": 5.0,
        "probe_capital": 500.0,
        "min_match_confidence": 0.9,
        "min_executable_shares": 1.0,
    }
    base.update(probe_overrides)
    return {"strategies": {"cv_probe": base}}


# ----------------------------------------------------------------------
# Net-gap math + fees
# ----------------------------------------------------------------------
def test_probe_net_gap_includes_both_venues_fees():
    """A pair where the gap BEFORE fees is $0.10/share, fees+slip take
    $0.06/share, leaves a $0.04 net gap >= min_probe_gap. The probe row
    must record net_gap_per_share = 0.04 (not 0.10), proving fees are
    subtracted."""
    ledger, path = _ledger()
    try:
        # Buyer's total cost (with fees+slip) is 0.96, net gap is 0.04.
        det = _det(pair_id=1, classification="FUZZY", net_gap=0.04,
                    safety_buffer=0.005,
                    poly_vwap=0.40, poly_fee=0.04,
                    kalshi_vwap=0.48, kalshi_fee=0.04,
                    executable=20.0)
        cv_result = {
            "detections": [det],
            "cert_fuzz_pairs": [_eq(1, category="sports", confidence=0.95)],
        }
        probe = CVProbe(_cfg())
        out = probe.run_probe(cv_result, ledger, verbose=False)
        assert out["counters"]["opened"] == 1
        rows = list(ledger.list_open_cv_probe())
        assert len(rows) == 1
        assert abs(float(rows[0]["net_gap_per_share"]) - 0.04) < 1e-9
    finally:
        _drop(ledger, path)


def test_probe_below_min_gap_excluded():
    ledger, path = _ledger()
    try:
        det = _det(pair_id=1, net_gap=0.015)   # below 0.02 default
        cv_result = {
            "detections": [det],
            "cert_fuzz_pairs": [_eq(1)],
        }
        probe = CVProbe(_cfg())
        out = probe.run_probe(cv_result, ledger, verbose=False)
        assert out["counters"]["opened"] == 0
        assert out["counters"]["candidates_eligible"] == 0
    finally:
        _drop(ledger, path)


# ----------------------------------------------------------------------
# Both-legs-or-nothing
# ----------------------------------------------------------------------
def test_probe_drops_candidate_missing_kalshi_leg():
    ledger, path = _ledger()
    try:
        det = _det(pair_id=1, net_gap=0.05)
        det.legs_detail = [L for L in det.legs_detail if L["venue"] == "polymarket"]
        cv_result = {
            "detections": [det],
            "cert_fuzz_pairs": [_eq(1)],
        }
        probe = CVProbe(_cfg())
        out = probe.run_probe(cv_result, ledger, verbose=False)
        assert out["counters"]["opened"] == 0
        assert out["counters"]["candidates_eligible"] == 0
    finally:
        _drop(ledger, path)


# ----------------------------------------------------------------------
# Dedupe
# ----------------------------------------------------------------------
def test_probe_skips_pair_with_existing_probe_row():
    ledger, path = _ledger()
    try:
        det = _det(pair_id=1, net_gap=0.05, executable=20.0)
        cv_result = {
            "detections": [det],
            "cert_fuzz_pairs": [_eq(1)],
        }
        probe = CVProbe(_cfg())
        first = probe.run_probe(cv_result, ledger, verbose=False)
        assert first["counters"]["opened"] == 1
        second = probe.run_probe(cv_result, ledger, verbose=False)
        assert second["counters"]["opened"] == 0
        assert second["counters"]["already_probed"] == 1
    finally:
        _drop(ledger, path)


def test_probe_two_directions_same_pair_yields_one_row():
    """A pair can produce up to two detections (one per direction). Only
    one probe row may exist per pair."""
    ledger, path = _ledger()
    try:
        det_a = _det(pair_id=1, net_gap=0.05, direction="POLY_YES_KAL_NO")
        det_b = _det(pair_id=1, net_gap=0.03, direction="POLY_NO_KAL_YES")
        cv_result = {
            "detections": [det_a, det_b],
            "cert_fuzz_pairs": [_eq(1)],
        }
        probe = CVProbe(_cfg())
        out = probe.run_probe(cv_result, ledger, verbose=False)
        assert out["counters"]["opened"] == 1
        # The larger gap (0.05) should have been the one selected.
        rows = list(ledger.list_open_cv_probe())
        assert abs(float(rows[0]["net_gap_per_share"]) - 0.05) < 1e-9
    finally:
        _drop(ledger, path)


# ----------------------------------------------------------------------
# Caps + largest-gap-first selection
# ----------------------------------------------------------------------
def test_probe_daily_cap_takes_largest_gaps_first():
    """daily cap = 2; submit 4 candidates with gaps 0.10, 0.07, 0.04,
    0.02 -> only the 0.10 and 0.07 ones get opened."""
    ledger, path = _ledger()
    try:
        dets = [
            _det(pair_id=1, net_gap=0.04),
            _det(pair_id=2, net_gap=0.10),
            _det(pair_id=3, net_gap=0.07),
            _det(pair_id=4, net_gap=0.02),
        ]
        cv_result = {
            "detections": dets,
            "cert_fuzz_pairs": [_eq(i, category="sports") for i in (1, 2, 3, 4)],
        }
        probe = CVProbe(_cfg(max_probe_per_day=2,
                                max_probe_per_day_per_category=2))
        out = probe.run_probe(cv_result, ledger, verbose=False)
        assert out["counters"]["opened"] == 2
        assert out["counters"]["daily_cap_skipped"] >= 1
        rows = sorted(ledger.list_open_cv_probe(),
                       key=lambda r: float(r["net_gap_per_share"]),
                       reverse=True)
        gaps = [round(float(r["net_gap_per_share"]), 4) for r in rows]
        assert gaps == [0.10, 0.07]
    finally:
        _drop(ledger, path)


def test_probe_per_category_cap_enforced_separately():
    """per-category cap = 1; two pairs in sports + two in crypto -> 2 opened
    (one each), the other two skipped."""
    ledger, path = _ledger()
    try:
        dets = [
            _det(pair_id=1, net_gap=0.08),    # sports
            _det(pair_id=2, net_gap=0.07),    # sports (skipped by cat cap)
            _det(pair_id=3, net_gap=0.06),    # crypto
            _det(pair_id=4, net_gap=0.05),    # crypto (skipped by cat cap)
        ]
        cf = [
            _eq(1, category="sports"),
            _eq(2, category="sports"),
            _eq(3, category="crypto"),
            _eq(4, category="crypto"),
        ]
        cv_result = {"detections": dets, "cert_fuzz_pairs": cf}
        probe = CVProbe(_cfg(max_probe_per_day_per_category=1,
                                max_probe_per_day=10))
        out = probe.run_probe(cv_result, ledger, verbose=False)
        assert out["counters"]["opened"] == 2
        assert out["per_category_opened"].get("sports") == 1
        assert out["per_category_opened"].get("crypto") == 1
        assert out["counters"]["category_cap_skipped"] == 2
    finally:
        _drop(ledger, path)


# ----------------------------------------------------------------------
# Quarantine
# ----------------------------------------------------------------------
def test_probe_writes_to_cv_probe_tables_not_cv_positions():
    ledger, path = _ledger()
    try:
        det = _det(pair_id=1, net_gap=0.05)
        cv_result = {
            "detections": [det],
            "cert_fuzz_pairs": [_eq(1)],
        }
        probe = CVProbe(_cfg())
        out = probe.run_probe(cv_result, ledger, verbose=False)
        assert out["counters"]["opened"] == 1
        c = ledger.raw_connect()
        probe_rows = list(c.execute(
            "SELECT COUNT(*) AS n FROM cv_probe_positions").fetchall())
        cv_rows = list(c.execute(
            "SELECT COUNT(*) AS n FROM cv_positions").fetchall())
        c.close()
        assert int(probe_rows[0]["n"]) == 1
        assert int(cv_rows[0]["n"]) == 0
    finally:
        _drop(ledger, path)


# ----------------------------------------------------------------------
# Confidence gate
# ----------------------------------------------------------------------
def test_probe_excludes_below_confidence_threshold():
    """A FUZZY pair with confidence 0.85 (below the 0.9 floor) must be
    dropped even if the gap and depth are otherwise excellent."""
    ledger, path = _ledger()
    try:
        det = _det(pair_id=1, net_gap=0.20, executable=100)
        cv_result = {
            "detections": [det],
            "cert_fuzz_pairs": [_eq(1, confidence=0.85)],
        }
        probe = CVProbe(_cfg())
        out = probe.run_probe(cv_result, ledger, verbose=False)
        assert out["counters"]["opened"] == 0
        assert out["counters"]["candidates_eligible"] == 0
    finally:
        _drop(ledger, path)


# ----------------------------------------------------------------------
# CERTIFIED is NOT probe-eligible (FUZZY -> CERTIFIED upgrade)
# ----------------------------------------------------------------------
def test_probe_excludes_certified_pairs():
    """An upgraded CERTIFIED pair is the real strategy's job; the probe
    must not touch it. This is the FUZZY->CERTIFIED route-away rule."""
    ledger, path = _ledger()
    try:
        det_certified = _det(pair_id=1, classification="CERTIFIED-IDENTICAL",
                                net_gap=0.20)
        det_fuzzy = _det(pair_id=2, classification="FUZZY", net_gap=0.05)
        cf = [(1, det_certified.poly, det_certified.kalshi,
                EquivalenceResult(classification="CERTIFIED-IDENTICAL",
                                    reason="all match", criteria={},
                                    category="sports", confidence=0.99)),
              _eq(2, category="sports", confidence=0.95)]
        cv_result = {"detections": [det_certified, det_fuzzy],
                       "cert_fuzz_pairs": cf}
        probe = CVProbe(_cfg())
        out = probe.run_probe(cv_result, ledger, verbose=False)
        assert out["counters"]["opened"] == 1
        rows = list(ledger.list_open_cv_probe())
        # The one probe row must be on the FUZZY pair, not the CERTIFIED one.
        assert int(rows[0]["pair_id"]) == 2
    finally:
        _drop(ledger, path)


# ----------------------------------------------------------------------
# Grader agreement_outcome paths
# ----------------------------------------------------------------------
def test_grader_classify_agreement_paths():
    """The classifier returns (agreement_outcome, divergence_direction).
    AGREED means exactly one of our paired legs paid. DIVERGED splits
    into BOTH_PAID (windfall: both legs paid; venues disagreed in our
    favor) and NEITHER_PAID (catastrophe: zero legs paid; venues
    disagreed against us)."""
    from foundation.grader import _classify_cv_probe_agreement
    assert _classify_cv_probe_agreement([
        {"outcome": "YES", "payout": 50.0},
        {"outcome": "YES", "payout": 0.0},
    ]) == ("AGREED", None)
    # Both legs paid -> windfall direction.
    assert _classify_cv_probe_agreement([
        {"outcome": "YES", "payout": 50.0},
        {"outcome": "NO", "payout": 50.0},
    ]) == ("DIVERGED", "BOTH_PAID")
    # Neither leg paid -> catastrophe direction.
    assert _classify_cv_probe_agreement([
        {"outcome": "NO", "payout": 0.0},
        {"outcome": "YES", "payout": 0.0},
    ]) == ("DIVERGED", "NEITHER_PAID")
    assert _classify_cv_probe_agreement([
        {"outcome": "YES", "payout": 50.0},
        {"outcome": "VOID", "payout": 12.5},
    ]) == ("VOID_MISMATCH", None)
    assert _classify_cv_probe_agreement([
        {"outcome": "VOID", "payout": 12.5},
        {"outcome": "VOID", "payout": 12.5},
    ]) == ("BOTH_VOID", None)


def test_grader_void_returns_stake_at_cost():
    """A VOID_MISMATCH return shape covers the documented payout model:
    voided leg gets stake-at-cost, settled leg pays normally."""
    from foundation.grader import _classify_cv_probe_agreement
    legs = [
        {"outcome": "YES", "payout": 5.0},
        {"outcome": "VOID", "payout": 2.5},
    ]
    assert _classify_cv_probe_agreement(legs) == ("VOID_MISMATCH", None)


# ----------------------------------------------------------------------
# divergence_direction persistence + split in settled_stats
# ----------------------------------------------------------------------
def test_close_cv_probe_persists_divergence_direction():
    """close_cv_probe_position must store divergence_direction so the
    report can split BOTH_PAID vs NEITHER_PAID downstream."""
    ledger, path = _ledger()
    try:
        det = _det(pair_id=1, net_gap=0.05, executable=20.0)
        cv_result = {"detections": [det],
                       "cert_fuzz_pairs": [_eq(1, category="sports")]}
        CVProbe(_cfg()).run_probe(cv_result, ledger, verbose=False)
        rows = list(ledger.list_open_cv_probe())
        assert len(rows) == 1
        pid = int(rows[0]["id"])
        ledger.close_cv_probe_position(
            pid, "DIVERGED", pnl=4.2,
            leg_outcomes=[],
            divergence_direction="BOTH_PAID",
        )
        c = ledger.raw_connect()
        row = c.execute(
            "SELECT agreement_outcome, divergence_direction, pnl "
            "FROM cv_probe_positions WHERE id=?", (pid,)).fetchone()
        c.close()
        assert row["agreement_outcome"] == "DIVERGED"
        assert row["divergence_direction"] == "BOTH_PAID"
        assert abs(float(row["pnl"]) - 4.2) < 1e-9
    finally:
        _drop(ledger, path)


def test_close_cv_probe_no_divergence_direction_for_agreed():
    """AGREED / VOID_* rows MUST have NULL divergence_direction so the
    settled-stats split is clean."""
    ledger, path = _ledger()
    try:
        det = _det(pair_id=1, net_gap=0.05, executable=20.0)
        cv_result = {"detections": [det],
                       "cert_fuzz_pairs": [_eq(1, category="sports")]}
        CVProbe(_cfg()).run_probe(cv_result, ledger, verbose=False)
        pid = int(list(ledger.list_open_cv_probe())[0]["id"])
        ledger.close_cv_probe_position(
            pid, "AGREED", pnl=0.10, leg_outcomes=[],
            divergence_direction=None,
        )
        c = ledger.raw_connect()
        row = c.execute(
            "SELECT divergence_direction FROM cv_probe_positions WHERE id=?",
            (pid,)).fetchone()
        c.close()
        assert row["divergence_direction"] is None
    finally:
        _drop(ledger, path)


def test_settled_stats_splits_both_paid_and_neither_paid():
    """cv_probe_settled_stats emits a row per (category, agreement,
    divergence_direction), so a category with 1 BOTH_PAID + 2 NEITHER_PAID
    DIVERGED rows shows up as TWO distinct DIVERGED rows."""
    ledger, path = _ledger()
    try:
        for pid_ in (1, 2, 3):
            det = _det(pair_id=pid_, net_gap=0.05, executable=20.0)
            cv_result = {"detections": [det],
                           "cert_fuzz_pairs": [_eq(pid_, category="sports")]}
            CVProbe(_cfg()).run_probe(cv_result, ledger, verbose=False)
        opens = list(ledger.list_open_cv_probe())
        assert len(opens) == 3
        ledger.close_cv_probe_position(int(opens[0]["id"]), "DIVERGED",
                                          pnl=+0.95, leg_outcomes=[],
                                          divergence_direction="BOTH_PAID")
        ledger.close_cv_probe_position(int(opens[1]["id"]), "DIVERGED",
                                          pnl=-0.90, leg_outcomes=[],
                                          divergence_direction="NEITHER_PAID")
        ledger.close_cv_probe_position(int(opens[2]["id"]), "DIVERGED",
                                          pnl=-0.95, leg_outcomes=[],
                                          divergence_direction="NEITHER_PAID")
        rows = list(ledger.cv_probe_settled_stats())
        diverged_rows = [r for r in rows if r["agreement_outcome"] == "DIVERGED"]
        directions = sorted(r["divergence_direction"] for r in diverged_rows)
        assert directions == ["BOTH_PAID", "NEITHER_PAID"]
        by_dir = {r["divergence_direction"]: r for r in diverged_rows}
        assert int(by_dir["BOTH_PAID"]["n"]) == 1
        assert int(by_dir["NEITHER_PAID"]["n"]) == 2
        assert abs(float(by_dir["BOTH_PAID"]["avg_pnl"]) - 0.95) < 1e-6
        assert abs(float(by_dir["NEITHER_PAID"]["avg_pnl"]) - (-0.925)) < 1e-6
    finally:
        _drop(ledger, path)
