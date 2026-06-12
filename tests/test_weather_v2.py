"""WeatherModel v2 shadow challenger -- kernel smoothing, bias
correction, and weight updates.

These tests pin the v2 contract:
  - Gaussian-mixture bucket integral at bucket boundaries (the original
    raw-fraction estimator's worst calibration zone).
  - Bias application shifts members BEFORE smoothing.
  - Per-city family weights track inverse rolling Brier.
"""
from __future__ import annotations

import math
import os
import sys
import tempfile

import numpy as np
import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from foundation.ledger import Ledger
from strategies.weather_v2 import (gaussian_mixture_bucket_prob,
                                       family_bias_for_city,
                                       family_weights_for_city,
                                       MAE_TO_SIGMA)


# ---------------------------------------------------------------- kernel
def test_kernel_centered_at_lower_boundary_yields_half():
    """All members exactly at the bucket lower cut: P(<= cut) = 0.5
    because the Gaussian on each member places half its mass below the
    cut. This is the boundary the raw-fraction estimator overshoots."""
    members = np.array([14.0, 14.0, 14.0])
    p = gaussian_mixture_bucket_prob(members, sigma=1.0,
                                          bound="le", lo_cut=None, hi_cut=14.0)
    assert p == pytest.approx(0.5, abs=1e-6)


def test_kernel_well_below_le_bucket_is_close_to_one():
    members = np.array([8.0, 8.5, 9.0])
    p = gaussian_mixture_bucket_prob(members, sigma=1.0,
                                          bound="le", lo_cut=None, hi_cut=14.0)
    assert p > 0.99


def test_kernel_eq_bucket_integral_equals_strip_mass():
    """eq bucket [-0.5, +0.5] around the member -- a 1C strip centered
    on the kernel mean should hold the central ~38% of mass for sigma=1
    (Phi(0.5)-Phi(-0.5)=0.3829)."""
    members = np.array([18.0])
    p = gaussian_mixture_bucket_prob(members, sigma=1.0, bound="eq",
                                          lo_cut=17.5, hi_cut=18.5)
    assert p == pytest.approx(0.3829249, abs=1e-5)


def test_kernel_weights_skew_toward_weighted_member():
    """Two members spaced 4C apart; weight 0.9 on the cooler member
    drags the eq-bucket probability toward the cooler answer."""
    members = np.array([14.0, 18.0])
    # eq-bucket around 14C: with equal weights ~half mass; with weights
    # 0.9 on 14C, the 18C contribution is tiny.
    p_eq = gaussian_mixture_bucket_prob(members, sigma=1.0, bound="eq",
                                              lo_cut=13.5, hi_cut=14.5)
    p_weighted = gaussian_mixture_bucket_prob(
        members, sigma=1.0, bound="eq",
        lo_cut=13.5, hi_cut=14.5,
        weights=np.array([0.9, 0.1]))
    assert p_weighted > p_eq


def test_mae_to_sigma_factor_matches_gaussian_half_normal():
    """For X ~ N(0,sigma^2), E[|X|] = sigma * sqrt(2/pi). Inverting,
    sigma = MAE * sqrt(pi/2). The constant we ship must satisfy that."""
    assert MAE_TO_SIGMA == pytest.approx(math.sqrt(math.pi / 2.0), rel=1e-12)


# ---------------------------------------------------------------- bias
def _row(city: str, family_mean: float, actual: float, family: str = "gfs",
          resolve_date: str = "2026-06-12"):
    return {
        "city": city,
        f"{family}_mean": family_mean,
        "actual_value": actual,
        "resolve_date": resolve_date,
    }


def test_family_bias_zero_when_too_few_rows():
    """The bias path must NOT move the model on thin data -- under 3
    rows triggers the zero-bias short-circuit."""
    rows = [_row("nyc", 24.0, 25.0)]
    assert family_bias_for_city(rows, "nyc", "gfs", 30) == 0.0


def test_family_bias_signed_mean_when_enough_rows():
    """Mean of (actual - family_mean) is +1.0 across three samples."""
    rows = [_row("nyc", 24.0, 25.0, resolve_date="2026-06-10"),
             _row("nyc", 30.0, 31.0, resolve_date="2026-06-11"),
             _row("nyc", 26.0, 27.0, resolve_date="2026-06-12")]
    assert family_bias_for_city(rows, "nyc", "gfs", 30) == pytest.approx(1.0)


def test_family_bias_filters_by_city():
    rows = [_row("nyc", 24.0, 25.0, resolve_date="2026-06-10"),
             _row("nyc", 30.0, 31.0, resolve_date="2026-06-11"),
             _row("nyc", 26.0, 27.0, resolve_date="2026-06-12"),
             _row("miami", 28.0, 35.0, resolve_date="2026-06-10")]
    assert family_bias_for_city(rows, "nyc", "gfs", 30) == pytest.approx(1.0)
    assert family_bias_for_city(rows, "miami", "gfs", 30) == 0.0  # only 1 row


def test_family_bias_filters_by_window():
    """A row outside the window doesn't contribute."""
    rows = [_row("nyc", 24.0, 25.0, resolve_date="2026-06-12"),
             _row("nyc", 30.0, 31.0, resolve_date="2026-06-11"),
             # ancient row gets dropped
             _row("nyc", 26.0, 100.0, resolve_date="1990-01-01")]
    # Only 2 rows remain in window -> below threshold, returns 0.
    assert family_bias_for_city(rows, "nyc", "gfs", 30) == 0.0


# ---------------------------------------------------------------- weights
def _scoring_row(city: str, outcome: str, gfs_p: float, ecmwf_p: float,
                   resolve_date: str = "2026-06-12"):
    return {
        "city": city, "outcome": outcome,
        "gfs_p_threshold": gfs_p, "ecmwf_p_threshold": ecmwf_p,
        "resolve_date": resolve_date,
    }


def test_family_weights_prior_when_no_data():
    """No graded rows -> 50/50 prior."""
    out = family_weights_for_city([], "nyc", 30)
    assert out == {"gfs": 0.5, "ecmwf": 0.5}


def test_family_weights_favor_lower_brier():
    """gfs predicts perfectly (Brier ~0); ecmwf predicts wrong every
    time (Brier ~1). gfs should get the bulk of the weight."""
    rows = []
    for i in range(8):
        rows.append(_scoring_row(
            "nyc", outcome="YES", gfs_p=0.95, ecmwf_p=0.05,
            resolve_date=f"2026-06-{i+1:02d}"))
    w = family_weights_for_city(rows, "nyc", 365)
    assert w["gfs"] > 0.95
    assert w["ecmwf"] < 0.05


def test_family_weights_thin_data_stays_neutral():
    """With <5 rows per family the prior wins."""
    rows = [_scoring_row("nyc", "YES", 0.9, 0.1,
                           resolve_date=f"2026-06-{i+1:02d}")
            for i in range(3)]
    w = family_weights_for_city(rows, "nyc", 30)
    # Both familes equal -> 0.5
    assert w["gfs"] == pytest.approx(0.5)
    assert w["ecmwf"] == pytest.approx(0.5)


# ---------------------------------------------------------------- ledger schema
def _temp_ledger():
    f = tempfile.NamedTemporaryFile(suffix=".db", delete=False); f.close()
    return Ledger(f.name), f.name


def test_shadow_trades_table_in_ledger_db():
    """Pinned: shadow_trades must live in ledger.db (committed) so the
    head-to-head record survives ephemeral runners."""
    import sqlite3
    ledger, path = _temp_ledger()
    try:
        with sqlite3.connect(ledger.ledger_path) as c:
            row = c.execute(
                "SELECT 1 FROM sqlite_master "
                "WHERE type='table' AND name='shadow_trades'"
            ).fetchone()
        assert row is not None
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass


def test_upsert_close_and_stats_roundtrip():
    """End-to-end: log a shadow trade, close it with an outcome, read
    the by-city stats back. champ_p=0.7 with outcome=YES gives a Brier
    of (0.7-1)^2 = 0.09."""
    ledger, path = _temp_ledger()
    try:
        # Set up a minimal markets row referenced by FK contract.
        mid = ledger.upsert_market({
            "condition_id": "0xtest", "slug": "s",
            "question": "Will NYC be 30C on Jun 12?",
            "category": "weather", "threshold": 30.0, "unit": "C",
            "resolve_date": "2026-06-12",
            "resolution_source": None, "rules_text": "",
        })
        sid = ledger.upsert_shadow_trade({
            "market_id": int(mid), "city": "nyc",
            "resolve_date": "2026-06-12",
            "champ_p": 0.70, "champ_side": "YES", "champ_edge": 0.10,
            "champ_price_filled": 0.65, "champ_stake": 15.0,
            "champ_shares": 23.0,
            "chal_p": 0.50, "chal_side": "NONE", "chal_edge": None,
            "chal_price_filled": None, "chal_stake": None,
            "chal_shares": None,
        })
        assert sid > 0
        ledger.close_shadow_trade(sid, outcome="YES",
                                       champ_pnl=8.0, chal_pnl=0.0)
        rows = list(ledger.shadow_stats_by_city())
        assert len(rows) == 1
        r = rows[0]
        assert r["city"] == "nyc"
        assert int(r["n"]) == 1
        assert int(r["champ_n_trades"]) == 1
        assert int(r["chal_n_trades"]) == 0
        assert float(r["champ_brier"]) == pytest.approx(0.09, abs=1e-6)
        assert float(r["chal_brier"]) == pytest.approx(0.25, abs=1e-6)
        assert float(r["champ_total_pnl"]) == pytest.approx(8.0)
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass
