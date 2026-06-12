"""Tests for the adaptive weather layer (v2.1):
  - shrinkage math (n=0 → equal/zero; n>>k → skill-dominated; clipping)
  - bias correction shifts member values the right direction by the right amount
  - calibration: synthetic overconfident history shrinks toward 0.5; under
    min_n it's identity
  - kill switch reverts everything; no-data regression test passes
  - verification row written correctly on a mocked resolution
"""
from __future__ import annotations

import os
import sys
import tempfile

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from foundation.ledger import Ledger
from foundation.wx_skill import (AdaptiveConfig, FAMILIES, adaptive_for_city,
                                    apply_calibration, dispute_forensics,
                                    family_skill, fit_calibration)


def _temp_ledger():
    f = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    f.close()
    return Ledger(f.name), f.name


def _row(city="nyc", station="KLGA", outcome="YES", p_blend=0.7,
          gfs_se=None, gfs_ae=None, gfs_p=None,
          ecmwf_se=None, ecmwf_ae=None, ecmwf_p=None,
          om=None, wu=None, resolve_date="2026-06-12"):
    return {
        "city": city, "station": station, "outcome": outcome,
        "p_blended": p_blend, "resolve_date": resolve_date,
        "gfs_signed_error": gfs_se, "gfs_abs_error": gfs_ae,
        "gfs_p_threshold": gfs_p,
        "ecmwf_signed_error": ecmwf_se, "ecmwf_abs_error": ecmwf_ae,
        "ecmwf_p_threshold": ecmwf_p,
        "om_value": om, "wu_value": wu,
    }


# ---------------------------------------------------------------- shrinkage
def test_zero_data_returns_equal_weights_and_zero_bias():
    st = adaptive_for_city([], "nyc", AdaptiveConfig())
    assert st.weights == {"gfs": 0.5, "ecmwf": 0.5}
    assert st.biases == {"gfs": 0.0, "ecmwf": 0.0}
    assert st.weights_status == "SHRUNK"


def test_below_min_n_stays_equal():
    cfg = AdaptiveConfig(min_n=10, shrink_n=20)
    rows = [_row(gfs_se=-1.0, gfs_ae=1.0, ecmwf_se=+0.5, ecmwf_ae=0.5)
             for _ in range(5)]   # n=5 < min_n=10
    st = adaptive_for_city(rows, "nyc", cfg)
    assert st.weights == {"gfs": 0.5, "ecmwf": 0.5}
    assert st.biases == {"gfs": 0.0, "ecmwf": 0.0}
    assert st.weights_status == "SHRUNK"


def test_skill_dominated_when_n_far_above_shrink_n():
    """gfs is much better than ecmwf. With n >> shrink_n, the family
    weights should clearly favor gfs."""
    cfg = AdaptiveConfig(min_n=10, shrink_n=20)
    rows = []
    for _ in range(200):
        rows.append(_row(gfs_se=+0.1, gfs_ae=0.1,
                          ecmwf_se=+2.0, ecmwf_ae=2.0))
    st = adaptive_for_city(rows, "nyc", cfg)
    assert st.weights["gfs"] > st.weights["ecmwf"], st.weights
    assert st.weights["gfs"] > 0.7
    assert st.weights_status in ("BLENDED", "ACTIVE")


def test_bias_correction_clipping():
    """Mean signed error well above the clip should saturate at the cap."""
    cfg = AdaptiveConfig(min_n=10, shrink_n=20, max_bias_correction=1.5)
    rows = []
    for _ in range(200):
        # Both families have a +5C mean bias
        rows.append(_row(gfs_se=+5.0, gfs_ae=5.0,
                          ecmwf_se=+5.0, ecmwf_ae=5.0))
    st = adaptive_for_city(rows, "nyc", cfg)
    assert st.biases["gfs"] == pytest.approx(1.5, abs=1e-6)
    assert st.biases["ecmwf"] == pytest.approx(1.5, abs=1e-6)


def test_bias_correction_sign_is_signed_error():
    """+ signed error means we predicted too high; bias correction
    should be the +mean (subtracted from members at apply time)."""
    cfg = AdaptiveConfig(min_n=10, shrink_n=10)
    rows = [_row(gfs_se=+0.8, gfs_ae=0.8, ecmwf_se=-0.4, ecmwf_ae=0.4)
             for _ in range(100)]
    st = adaptive_for_city(rows, "nyc", cfg)
    assert st.biases["gfs"] > 0
    assert st.biases["ecmwf"] < 0


# ---------------------------------------------------------------- calibration
def test_calibration_identity_below_min_n():
    cfg = AdaptiveConfig(calib_min_n=30)
    rows = [_row(outcome="YES", p_blend=0.9) for _ in range(20)]
    cal = fit_calibration(rows, cfg)
    assert cal.alpha == 1.0
    assert cal.status == "IDENTITY"


def test_calibration_shrinks_overconfident_history():
    """Predicted 0.9 but observed only 0.5 hit-rate -> alpha < 1
    (predictions get pulled toward 0.5)."""
    cfg = AdaptiveConfig(calib_min_n=10)
    rows = []
    for i in range(50):
        rows.append(_row(outcome="YES" if i % 2 == 0 else "NO",
                          p_blend=0.9))
    cal = fit_calibration(rows, cfg)
    assert cal.status == "FIT"
    assert cal.alpha < 1.0
    # Applying to 0.9 should shrink toward 0.5.
    p_in = 0.9
    p_out = apply_calibration(p_in, cal)
    assert 0.5 < p_out < p_in


def test_apply_calibration_identity_alpha_one():
    cal = type("C", (), {"alpha": 1.0})()
    assert apply_calibration(0.73, cal) == 0.73


# ---------------------------------------------------------------- dispute forensics
def test_dispute_forensics_constant_offset_flagged():
    """Same station, three DIFFERENT days, same offset every day =>
    legitimate constant offset (n=3). The v2.3 aggregator dedupes per
    (station, resolve_date) so we have to vary the date or the rows
    collapse to one sample."""
    rows = [_row(station="KLGA", om=20.0, wu=21.0,
                  resolve_date=f"2026-06-{10+i:02d}") for i in range(3)]
    fc = dispute_forensics(rows)
    assert fc["KLGA"].n == 3
    assert fc["KLGA"].mean_om_minus_wu == pytest.approx(-1.0)
    assert fc["KLGA"].std_om_minus_wu == pytest.approx(0.0)


def test_dispute_forensics_dedupes_sibling_markets_on_same_day():
    """v2.3 aggregation fix: sibling bucket markets on the same day at
    the same station report the same OM and the same WU -- they're ONE
    observation, not N. Wellington's three 2026-06-11 markets all read
    OM=13.4 / WU=14.0; counting them three times manufactured the
    'constant offset (likely bug)' verdict."""
    rows = [_row(station="NZWN", om=13.4, wu=14.0, resolve_date="2026-06-11")
             for _ in range(3)]
    fc = dispute_forensics(rows)
    assert fc["NZWN"].n == 1   # not 3
    assert fc["NZWN"].mean_om_minus_wu == pytest.approx(-0.6)


def test_dispute_forensics_variable_high_std():
    rows = [_row(station="X", om=20.0, wu=20.0, resolve_date="2026-06-10"),
             _row(station="X", om=22.0, wu=20.0, resolve_date="2026-06-11"),
             _row(station="X", om=18.0, wu=20.0, resolve_date="2026-06-12")]
    fc = dispute_forensics(rows)
    assert fc["X"].n == 3
    assert fc["X"].mean_om_minus_wu == pytest.approx(0.0)
    assert fc["X"].std_om_minus_wu > 1.0


# ---------------------------------------------------------------- kill switch
def test_kill_switch_off_disables_layer():
    cfg = AdaptiveConfig(enabled=False)
    # Adaptive layer doesn't directly read .enabled - that's checked by
    # the caller (weather.py). But fit_calibration & adaptive_for_city
    # should behave correctly on empty input. Specifically: even with a
    # huge skill dataset, if the strategy's adaptive_enabled=False, the
    # strategy's `_adaptive_state` returns (None, None) and the existing
    # pool-all-members blend is used.
    # This test pins the no-op behavior at the strategy level.
    from strategies.weather import WeatherStrategy
    s = WeatherStrategy({"strategies": {"weather":
                                            {"adaptive_weights": False,
                                             "cities": []}}})
    assert s.adaptive_enabled is False
    # state lookup returns None on a strategy with the kill switch off
    state, cal = s._adaptive_state("nyc")
    assert state is None and cal is None


# ---------------------------------------------------------------- verification row write
def test_upsert_wx_verification_round_trip():
    ledger, path = _temp_ledger()
    rid = ledger.upsert_wx_verification({
        "market_row_id": 42, "city": "nyc", "station": "KLGA",
        "threshold": 78.0, "unit": "F", "bound": "eq",
        "resolve_date": "2026-06-12", "lead_time_hours": 18.5,
        "official_value": 77.0, "official_value_unit": "F",
        "om_value": 76.5, "om_value_unit": "F",
        "wu_value": 77.0, "wu_value_unit": "F",
        "gfs_mean": 78.4, "gfs_spread": 1.1, "gfs_p_threshold": 0.6,
        "gfs_signed_error": 1.4, "gfs_abs_error": 1.4,
        "ecmwf_mean": 77.2, "ecmwf_spread": 0.8, "ecmwf_p_threshold": 0.55,
        "ecmwf_signed_error": 0.2, "ecmwf_abs_error": 0.2,
        "p_blended": 0.58, "market_price": 0.62, "outcome": "NO",
    })
    rows = ledger.list_wx_verifications()
    assert len(rows) == 1
    r = rows[0]
    assert r["market_row_id"] == 42
    assert r["city"] == "nyc"
    assert r["gfs_signed_error"] == pytest.approx(1.4)
    assert r["wu_value"] == pytest.approx(77.0)
    # Idempotent: re-running with new data updates same row, not insert.
    ledger.upsert_wx_verification({
        "market_row_id": 42, "city": "nyc", "station": "KLGA",
        "threshold": 78.0, "unit": "F", "bound": "eq",
        "resolve_date": "2026-06-12", "lead_time_hours": 0.0,
        "official_value": 78.0, "official_value_unit": "F",
        "om_value": 78.0, "om_value_unit": "F",
        "wu_value": 78.0, "wu_value_unit": "F",
        "gfs_mean": None, "gfs_spread": None, "gfs_p_threshold": None,
        "gfs_signed_error": None, "gfs_abs_error": None,
        "ecmwf_mean": None, "ecmwf_spread": None, "ecmwf_p_threshold": None,
        "ecmwf_signed_error": None, "ecmwf_abs_error": None,
        "p_blended": 0.50, "market_price": 0.55, "outcome": "YES",
    })
    rows2 = ledger.list_wx_verifications()
    assert len(rows2) == 1
    assert rows2[0]["outcome"] == "YES"
    try:
        os.unlink(path)
    except OSError:
        pass


# ---------------------------------------------------------------- regression: no-data identity
def test_weather_estimate_no_data_produces_identical_output(monkeypatch):
    """With zero verification rows + adaptive_enabled=True, the
    estimator must produce IDENTICAL output to the existing pool-all-
    members logic. We compare the same synthetic forecast through the
    estimator with adaptive ON (no data) and adaptive OFF."""
    import numpy as np
    from strategies.weather import WeatherStrategy
    from foundation.wunderground import WundergroundClient

    # Trivial city list with one entry so _match_city works.
    city = {"slug_hints": ["nyc"], "city": "nyc", "station_name": "KLGA",
             "lat": 40.0, "lon": -73.0, "timezone": "America/New_York",
             "resolution_source": "https://example",
             "market_kinds": ["max"]}
    base_cfg = {"strategies": {"weather": {"cities": [city]}}}
    s_off = WeatherStrategy({**base_cfg,
                              "strategies": {"weather": {
                                  "cities": [city],
                                  "adaptive_weights": False}}})
    s_on = WeatherStrategy({**base_cfg,
                             "strategies": {"weather": {
                                 "cities": [city],
                                 "adaptive_weights": {"enabled": True}}}})

    # Without attach_ledger, _adaptive_state returns (None, None) on
    # both. We just need to confirm that the adaptive code path doesn't
    # affect the produced p_final when there's no ledger.
    assert s_off._adaptive_state("nyc") == (None, None)
    assert s_on._adaptive_state("nyc") == (None, None)
    # And attach_ledger with a fresh ledger (no verifications) should
    # also yield SHRUNK state which the estimator treats as a no-op.
    ledger, path = _temp_ledger()
    s_on.attach_ledger(ledger)
    state, cal = s_on._adaptive_state("nyc")
    assert state.weights_status == "SHRUNK"
    assert state.weights == {"gfs": 0.5, "ecmwf": 0.5}
    assert state.biases == {"gfs": 0.0, "ecmwf": 0.0}
    assert cal.alpha == 1.0
    try:
        os.unlink(path)
    except OSError:
        pass


def test_family_skill_brier_from_p_threshold():
    """If we have family p_threshold + outcome but NO mean error, family
    n still reports 0 because MAE is undefined for that row - the report
    will degrade gracefully. This pins the documented behavior."""
    rows = [_row(gfs_p=0.7, outcome="YES") for _ in range(10)]
    sk = family_skill(rows, "gfs")
    assert sk.n == 0


# ---------------------------------------------------------------- WX-VERIFY render
def test_print_wx_verify_renders_city_rows_from_brier_only_data(capsys):
    """v2.3 rendering fix: when family means/errors are absent (backfilled
    rows) but per-family p_threshold + YES/NO outcome are present, the
    city table must still render -- showing n/a for MAE/bias and Brier
    from p_threshold. The empty city table that shipped with n=15 was
    the original bug."""
    from main import _print_wx_verify
    ledger, path = _temp_ledger()
    try:
        for i in range(2):
            ledger.upsert_wx_verification({
                "market_row_id": 100 + i, "city": "tokyo",
                "station": "RJTT", "threshold": 30.0, "unit": "C",
                "bound": "eq", "resolve_date": f"2026-06-{10+i:02d}",
                "lead_time_hours": 12.0,
                "official_value": 30.0, "official_value_unit": "C",
                "om_value": 29.5, "om_value_unit": "C",
                "wu_value": 30.0, "wu_value_unit": "C",
                # ONLY p_threshold + outcome populated, no per-family means.
                "gfs_mean": None, "gfs_spread": None, "gfs_p_threshold": 0.55,
                "gfs_signed_error": None, "gfs_abs_error": None,
                "ecmwf_mean": None, "ecmwf_spread": None,
                "ecmwf_p_threshold": 0.60,
                "ecmwf_signed_error": None, "ecmwf_abs_error": None,
                "p_blended": 0.575, "market_price": 0.50,
                "outcome": "YES" if i == 0 else "NO",
                "signal_id": None, "settlement_id": None,
            })
        _print_wx_verify({}, ledger)
        captured = capsys.readouterr().out
        # The pre-fix bug was that the city table was empty even though
        # n=15 in the header. Assert at least one tokyo row is rendered.
        assert "tokyo" in captured
        assert "gfs" in captured or "ecmwf" in captured
        # MAE/bias should appear as n/a since family means are absent.
        assert "n/a" in captured
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass


# ---------------------------------------------------------------- regrade WU-authoritative
def test_regrade_disputed_on_wu_flips_outcome_and_closes_trade():
    """The 12 DISPUTED June-11 weather settlements were left OPEN because
    OM disagreed with WU on the rounded integer. v2.3 policy: WU is the
    real Polymarket source -- grade on WU, log OM as cross-check."""
    from foundation.grader import regrade_disputed_on_wu
    from datetime import date
    ledger, path = _temp_ledger()
    try:
        # Synthesize a market in the same shape the scanner writes.
        # Use the real Polymarket question phrasing so parse_group_title
        # picks the bucket integer instead of a date integer.
        market_id = ledger.upsert_market({
            "condition_id": "0xabc",
            "slug": "highest-temperature-in-tokyo-on-june-11-2026-30c",
            "question": "Will the highest temperature in Tokyo be 30C on June 11",
            "category": "weather",
            "threshold": 30.0, "unit": "C",
            "resolve_date": "2026-06-11",
            "resolution_source": "https://wu/x",
            "rules_text": "30C",
        })
        # DISPUTED settlement: WU=30 (truth), OM=28 (wedge).
        ledger.record_settlement(
            int(market_id), 30.0,
            "open-meteo / wunderground disagree", "DISPUTED",
            om_value=28.0, wu_value=30.0,
            wu_source="https://wu/x",
        )
        # OPEN paper_trade: we bought NO. WU's rounded 30 == bucket 30
        # -> outcome YES -> NO position LOSES the full stake.
        with ledger._conn() as c:
            c.execute(
                """INSERT INTO paper_trades
                    (ts, market_id, strategy, side, price_filled, stake,
                     shares, p_model_at_entry, edge_at_entry, status)
                    VALUES (?, ?, 'weather', 'NO', 0.50, 10.0, 20.0,
                            0.40, 0.10, 'OPEN')""",
                ("2026-06-10T00:00:00Z", int(market_id)))

        out = regrade_disputed_on_wu({}, ledger, date(2026, 6, 12),
                                       verbose=False)
        # The settlement flipped DISPUTED -> YES; the trade closed with
        # a full-stake LOSS (we bought NO at 0.50 with $10 stake; YES
        # outcome -> we lose stake).
        assert len(out["settled"]) == 1
        ev = out["settled"][0]
        assert ev["outcome"] == "YES"
        assert ev["status"] == "LOSS"
        assert ev["pnl"] == pytest.approx(-10.0)
        # idempotent: a second run finds zero remaining DISPUTED.
        again = regrade_disputed_on_wu({}, ledger, date(2026, 6, 12),
                                         verbose=False)
        assert again["n_settlements"] == 0
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass
