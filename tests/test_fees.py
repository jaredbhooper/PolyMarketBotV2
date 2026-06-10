"""Polymarket fee-schedule verification tests."""
from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from foundation.fees import (DEFAULT_TAKER_RATES, polymarket_taker_fee_per_share,
                                normalize_category, taker_rate)


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
