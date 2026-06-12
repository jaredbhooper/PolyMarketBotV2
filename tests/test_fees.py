"""Polymarket fee-schedule verification tests."""
from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from foundation.fees import (DEFAULT_TAKER_RATES, KALSHI_MAKER_BASE,
                                KALSHI_TAKER_BASE, kalshi_fee_per_contract,
                                normalize_category,
                                polymarket_taker_fee_per_share, taker_rate)


def test_weather_quadratic_peaks_at_50():
    """Weather rate 0.0125; fee at p=0.50 = 0.0125 * 0.5 * 0.5 = 0.003125."""
    f = polymarket_taker_fee_per_share(0.50, "weather")
    assert f == pytest.approx(0.003125, abs=1e-9)


def test_sports_lower_than_weather():
    fs = polymarket_taker_fee_per_share(0.50, "sports")
    fw = polymarket_taker_fee_per_share(0.50, "weather")
    # sports 0.0075 vs weather 0.0125
    assert fs < fw


def test_geopolitics_zero_fee():
    assert polymarket_taker_fee_per_share(0.50, "geopolitics") == 0.0


def test_quadratic_drops_near_zero_and_one():
    near_zero = polymarket_taker_fee_per_share(0.02, "weather")
    near_half = polymarket_taker_fee_per_share(0.50, "weather")
    assert near_zero < near_half * 0.1


def test_unknown_category_falls_back_to_default():
    r = taker_rate("nonexistent")
    assert r == 0.01


def test_normalize_category_alias_maps_to_canon():
    assert normalize_category("highest-temperature") == "weather"
    assert normalize_category("NBA finals winner") == "sports"
    assert normalize_category("2028 elections") == "politics"


def test_rate_table_matches_verified_2026_03_schedule():
    """The published rates (sources in BUILD_NOTES.md). If Polymarket
    changes the schedule, this test fails loudly."""
    expected = {
        "crypto": 0.0180, "economics": 0.0150, "mentions": 0.0156,
        "culture": 0.0125, "weather": 0.0125,
        "finance": 0.0100, "politics": 0.0100, "tech": 0.0100,
        "sports": 0.0075, "geopolitics": 0.0000,
    }
    for k, v in expected.items():
        assert DEFAULT_TAKER_RATES[k] == pytest.approx(v)


# ----------------------------------------------------------------- Kalshi
def test_kalshi_base_rates_pinned_to_2026_04_schedule():
    """Source: kalshi.com/docs/kalshi-fee-schedule.pdf (April 2026).
    Taker base = 0.07; maker base = 0.0175. If Kalshi updates the
    schedule, this test fails loudly to force a re-verification."""
    assert KALSHI_TAKER_BASE == pytest.approx(0.07)
    assert KALSHI_MAKER_BASE == pytest.approx(0.0175)


def test_kalshi_fee_at_50_cent_peak():
    """At p=0.50, p*(1-p)=0.25. raw = 0.07*0.25 = 0.0175; ceil to next
    cent = $0.02 per contract."""
    assert kalshi_fee_per_contract(0.50) == pytest.approx(0.02)


def test_kalshi_fee_at_extremes_rounds_to_one_cent():
    """At p=0.99, raw = 0.07*0.99*0.01 = 0.000693; ceil to $0.01."""
    assert kalshi_fee_per_contract(0.99) == pytest.approx(0.01)
    assert kalshi_fee_per_contract(0.01) == pytest.approx(0.01)


def test_kalshi_fee_zero_at_settlement_prices():
    """No fee on resolved contracts (price exactly 0 or 1)."""
    assert kalshi_fee_per_contract(0.0) == 0.0
    assert kalshi_fee_per_contract(1.0) == 0.0


def test_kalshi_maker_fee_quarter_of_taker():
    """Maker base is 0.0175 = 0.07/4. At p=0.50, raw=0.004375; ceil=$0.01."""
    assert kalshi_fee_per_contract(0.50, maker=True) == pytest.approx(0.01)


def test_kalshi_series_multiplier_scales_fee():
    """A 3x premium-series multiplier triples the raw fee before ceiling.
    At p=0.50, mult=3: raw = 3*0.07*0.25 = 0.0525; ceil = $0.06."""
    assert kalshi_fee_per_contract(0.50, multiplier=3.0) == pytest.approx(0.06)
    # mult=2 at p=0.50: raw = 2*0.07*0.25 = 0.035; ceil = $0.04.
    assert kalshi_fee_per_contract(0.50, multiplier=2.0) == pytest.approx(0.04)
