"""Grader / weather-resolve local-day contract.

Pins the fix for the UTC-vs-local-day bug: Polymarket weather markets
resolve on the station's LOCAL calendar day, not on UTC. A market with
resolve_date == today UTC may be settle-able for an Asia/Pacific station
(local day already over) or still mid-afternoon-local for a US station.
The grader's UTC-date gate must NOT block the first case, and resolve()
must NOT settle the second case.
"""
from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from strategies.base import Market
from strategies.weather import (CityCfg, WeatherStrategy,
                                  local_day_has_ended)
from foundation.wunderground import WuResult


# ---------------------------------------------------------------- helpers
def _wellington_cfg() -> dict:
    """Minimal config exercising one Wellington max-temp market."""
    return {
        "strategies": {
            "weather": {
                "edge_threshold": 0.08,
                "kelly_fraction": 0.15,
                "blend_raw_weight": 0.5,
                "disagreement_threshold": 0.25,
                "low_confidence": 0.5,
                "clamp_min": 0.02,
                "clamp_max": 0.98,
                "forecast_days": 3,
                "dispute_threshold_degrees": 1.0,
                "adaptive_weights": False,
                "cities": [{
                    "slug_hints": ["nzwn", "wellington"],
                    "city": "wellington",
                    "station_name": "Wellington Intl (NZWN)",
                    "lat": -41.3272, "lon": 174.8053,
                    "timezone": "Pacific/Auckland",
                    "resolution_source":
                        "https://www.wunderground.com/history/daily/nz/wellington/NZWN",
                }],
            },
        },
    }


def _wellington_market(threshold: int = 15) -> Market:
    """A 'highest temp in Wellington on 2026-06-13 will be 15C' market.

    Wellington is UTC+12 in June (NZST). 2026-06-13 local ends at
    2026-06-13T23:59:59+12:00 == 2026-06-13T11:59:59Z. Useful: pick a
    settled_at on either side of that boundary.
    """
    return Market(
        market_id="cond-wellington-15",
        slug=f"highest-temperature-in-wellington-on-june-13-2026-{threshold}c",
        question=f"Will the highest temperature in Wellington be {threshold}C on June 13?",
        category="weather",
        rules_text="",
        resolve_date="2026-06-13",
        end_date_iso="2026-06-13",
        yes_token_id=None, no_token_id=None,
        yes_ask=None, yes_bid=None, no_ask=None, no_bid=None,
        extras={
            "event_slug": "highest-temperature-in-wellington",
            "kind": "max",
            "parsed_unit": "C",
            "parsed_bound": "eq",
            "lo": threshold, "hi": threshold,
            "station_url": "https://www.wunderground.com/history/daily/nz/wellington/NZWN",
        },
    )


class _StubWU:
    """WundergroundClient stub: returns a fixed daily_extreme each call."""

    def __init__(self, max_temp: float | None, error: str | None = None):
        self.max_temp = max_temp
        self.error = error
        self.calls = 0

    def daily_extreme(self, icao, country, date_iso, tz, unit, kind):
        self.calls += 1
        return WuResult(
            icao=icao, country=country, date_iso=date_iso, unit=unit,
            max_temp=self.max_temp, n_obs=48 if self.max_temp is not None else 0,
            source_url=f"https://www.wunderground.com/history/daily/{country.lower()}/-/{icao}/date/{date_iso}",
            error=self.error,
        )


class _StubForecastClient:
    """ForecastClient stub: archive returns the configured value."""

    def __init__(self, archive_value_c: float | None):
        self._archive = archive_value_c
        self.archive_calls = 0

    def archive_max_temp_c(self, lat, lon, day_iso, tz):
        self.archive_calls += 1
        return self._archive

    def archive_min_temp_c(self, lat, lon, day_iso, tz):
        self.archive_calls += 1
        return self._archive


def _strategy_with_stubs(wu_temp: float | None = 15.0,
                          archive_c: float | None = None) -> WeatherStrategy:
    s = WeatherStrategy(_wellington_cfg())
    s.wu = _StubWU(max_temp=wu_temp)
    s.client = _StubForecastClient(archive_value_c=archive_c)
    return s


# ---------------------------------------------------------------- local-day helper
def test_local_day_helper_recognizes_end_of_wellington_day():
    """Wellington (UTC+12) day 2026-06-13 ends at 11:59:59 UTC."""
    # 1 minute before the local end: not yet ended.
    assert local_day_has_ended(
        "2026-06-13", "Pacific/Auckland",
        settled_at="2026-06-13T11:58:00+00:00") is False
    # 1 second after the local end: ended.
    assert local_day_has_ended(
        "2026-06-13", "Pacific/Auckland",
        settled_at="2026-06-14T00:00:01+12:00") is True


def test_local_day_helper_handles_americas():
    """Los Angeles (UTC-7 in summer) day 2026-06-13 ends at 06:59:59 UTC
    on 2026-06-14. Same UTC-date stamp differs in status by city."""
    same_utc = "2026-06-13T15:00:00+00:00"   # noon-ish UTC
    # Wellington 2026-06-13 ended already at this moment.
    assert local_day_has_ended(
        "2026-06-13", "Pacific/Auckland", settled_at=same_utc) is True
    # LA 2026-06-13 is still mid-morning local at the same UTC moment.
    assert local_day_has_ended(
        "2026-06-13", "America/Los_Angeles", settled_at=same_utc) is False


# ---------------------------------------------------------------- resolve() contract
def test_resolve_returns_none_when_local_day_not_ended():
    """CRITICAL: same market, same WU stub. settled_at BEFORE Wellington
    2026-06-13 local-day end must yield None (no partial-day reading).

    This is the contract that prevents 03:00-local 'mid-afternoon UTC'
    settlements when Polymarket itself wouldn't have resolved yet.
    """
    strat = _strategy_with_stubs(wu_temp=15.0, archive_c=15.0)
    market = _wellington_market(threshold=15)
    out = strat.resolve(market,
                        settled_at="2026-06-13T11:00:00+00:00")  # 23:00 NZ, day still in progress
    assert out is None
    # Critically: WU was NOT called -- short-circuit before any network.
    assert strat.wu.calls == 0


def test_resolve_settles_on_wu_when_local_day_ended():
    """CRITICAL: same market, same WU stub. settled_at AFTER Wellington
    2026-06-13 local-day end must settle on the WU value alone, even
    when Open-Meteo archive hasn't backfilled yet."""
    strat = _strategy_with_stubs(wu_temp=15.0, archive_c=None)
    market = _wellington_market(threshold=15)
    out = strat.resolve(market,
                        settled_at="2026-06-13T15:00:00+00:00")  # 03:00 NZ next day, ended
    assert out is not None
    assert out["wu_value"] == 15.0
    assert out["actual_value"] == 15.0    # truth from WU
    assert out["om_value"] is None        # OM archive missing -> None
    assert out["outcome"] == "YES"        # eq-bucket 15C matches 15.0
    assert out["wu_rounded_val"] == 15
    assert "wunderground" in out["source_value"].lower()


def test_resolve_settles_with_wu_only_when_om_missing():
    """A market with no OM archive coverage (e.g. fresh day, archive
    not yet propagated) must NOT block on OM if WU has the data.
    Previously resolve() bailed early when om_c was None; this is the
    regression that caused the 'settled count never advances' symptom.
    """
    strat = _strategy_with_stubs(wu_temp=18.0, archive_c=None)
    market = _wellington_market(threshold=15)  # eq=15; truth 18 -> NO
    out = strat.resolve(market,
                        settled_at="2026-06-13T15:00:00+00:00")
    assert out is not None
    assert out["outcome"] == "NO"
    assert out["wu_value"] == 18.0
    assert out["om_value"] is None
    assert out["disagreement"] is None    # no OM -> no diff to compute


def test_resolve_returns_none_when_neither_source_has_data():
    """Both WU and OM unavailable for an already-ended day -- defer,
    do not invent a settlement. The next grade run retries."""
    strat = _strategy_with_stubs(wu_temp=None, archive_c=None)
    market = _wellington_market(threshold=15)
    out = strat.resolve(market,
                        settled_at="2026-06-13T15:00:00+00:00")
    assert out is None


def test_resolve_records_wu_om_disagreement_but_still_grades_on_wu():
    """WU has 15C, OM has 18C (3C disagreement, above threshold). WU is
    authoritative; we still grade on WU and emit a dispute_note."""
    strat = _strategy_with_stubs(wu_temp=15.0, archive_c=18.0)
    market = _wellington_market(threshold=15)
    out = strat.resolve(market,
                        settled_at="2026-06-13T15:00:00+00:00")
    assert out is not None
    assert out["outcome"] == "YES"        # WU=15 matches eq=15
    assert out["wu_value"] == 15.0
    assert out["om_value"] == 18.0
    assert out["disagreement"] == 3
    assert out["dispute_note"]            # non-empty
    assert "wunderground" in out["source_value"].lower()
