"""Forecast-change watcher: hash stability + trigger dedupe."""
from __future__ import annotations

import os
import sys
import tempfile
from datetime import datetime, timedelta, timezone

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from foundation.forecast_change import (compute_hash,
                                          detect_and_record,
                                          get_change_at,
                                          get_hash,
                                          mark_scan_at,
                                          minutes_since_forecast_change,
                                          should_trigger_scan,
                                          summary_payload,
                                          quantize,
                                          QUANTIZE_C,
                                          SUMMARY_HOURS)
from foundation.ledger import Ledger
from strategies.weather import EnsembleForecast


# ---------------------------------------------------------------- helpers
def _temp_ledger():
    f = tempfile.NamedTemporaryFile(suffix=".db", delete=False); f.close()
    return Ledger(f.name), f.name


def _cleanup(path: str) -> None:
    for p in (path, path.removesuffix(".db") + ".cache.db"):
        try:
            os.unlink(p)
        except OSError:
            pass


def _fc(times: list[str], members: dict[str, list[float]],
         tz: str = "UTC") -> EnsembleForecast:
    return EnsembleForecast(
        station_lat=40.0, station_lon=-74.0, timezone=tz,
        times=times, members=members, elevation_m=10.0,
    )


# ---------------------------------------------------------------- hash stability
def test_hash_stable_under_floating_point_jitter():
    """Two ensembles whose members differ by < quantization step (0.5C)
    must hash to the same value -- the watcher should not trip on
    Open-Meteo serialization noise."""
    times = [f"2026-06-13T{h:02d}:00" for h in range(SUMMARY_HOURS)]
    base = {f"temperature_2m_member{i:02d}_ncep_gefs_seamless":
             [20.0 + (h % 5) * 0.1 for h in range(SUMMARY_HOURS)]
            for i in range(3)}
    jittered = {k: [v + 1e-9 for v in series] for k, series in base.items()}
    h1 = compute_hash(summary_payload(_fc(times, base)))
    h2 = compute_hash(summary_payload(_fc(times, jittered)))
    assert h1 == h2


def test_hash_changes_when_family_mean_moves_one_step():
    """A 0.5C shift to every member of one family must change the hash.
    This is the minimum signal we want detected."""
    times = [f"2026-06-13T{h:02d}:00" for h in range(SUMMARY_HOURS)]
    base = {f"temperature_2m_member{i:02d}_ncep_gefs_seamless":
             [20.0 for _ in range(SUMMARY_HOURS)]
            for i in range(3)}
    shifted = {k: [v + QUANTIZE_C for v in series]
               for k, series in base.items()}
    h1 = compute_hash(summary_payload(_fc(times, base)))
    h2 = compute_hash(summary_payload(_fc(times, shifted)))
    assert h1 != h2


def test_hash_invariant_under_member_reorder():
    """The hash must NOT depend on the dict iteration order of members,
    so two equivalent forecasts with members inserted in different
    orders hash to the same value."""
    times = [f"2026-06-13T{h:02d}:00" for h in range(SUMMARY_HOURS)]
    keys = [f"temperature_2m_member{i:02d}_ncep_gefs_seamless"
             for i in range(4)]
    forward = {k: [20.0 + i for _ in range(SUMMARY_HOURS)]
                for i, k in enumerate(keys)}
    reverse = {k: forward[k] for k in reversed(keys)}
    assert compute_hash(summary_payload(_fc(times, forward))) \
           == compute_hash(summary_payload(_fc(times, reverse)))


def test_quantize_snaps_to_half_degree_grid():
    assert quantize(20.24) == 20.0
    assert quantize(20.26) == 20.5
    assert quantize(20.5) == 20.5
    assert quantize(20.74) == 20.5
    assert quantize(20.76) == 21.0


def test_summary_payload_truncates_to_horizon():
    """Open-Meteo returns more than SUMMARY_HOURS hours when
    forecast_days >= 3; the summary must cap at SUMMARY_HOURS so the
    hash is stable across long-tail trim differences."""
    times = [f"2026-06-13T{h:02d}:00" for h in range(SUMMARY_HOURS + 24)]
    members = {f"temperature_2m_member{i:02d}_ncep_gefs_seamless":
                [20.0 + h * 0.01 for h in range(SUMMARY_HOURS + 24)]
                for i in range(2)}
    payload = summary_payload(_fc(times, members))
    assert len(payload["times"]) == SUMMARY_HOURS
    for series in payload["families"].values():
        assert len(series) == SUMMARY_HOURS


# ---------------------------------------------------------------- ledger seed + change
class _FakeClient:
    """Stub ForecastClient. Returns whatever forecast is configured
    per (lat, lon)."""

    def __init__(self, by_city: dict[tuple[float, float], EnsembleForecast]):
        self._by_city = by_city

    def ensemble(self, lat, lon, forecast_days=3):
        return self._by_city[(lat, lon)]


class _C:
    def __init__(self, city, lat, lon):
        self.city = city; self.lat = lat; self.lon = lon


def _trivial_forecast(value: float) -> EnsembleForecast:
    times = [f"2026-06-13T{h:02d}:00" for h in range(SUMMARY_HOURS)]
    members = {f"temperature_2m_member{i:02d}_ncep_gefs_seamless":
                [value for _ in range(SUMMARY_HOURS)]
                for i in range(3)}
    return _fc(times, members)


def test_first_observation_seeds_no_change():
    """The first time we ever see a city, we don't emit a change event
    (we don't know when the model 'actually' moved -- it's just the
    initial baseline)."""
    ledger, path = _temp_ledger()
    try:
        client = _FakeClient({(1.0, 2.0): _trivial_forecast(20.0)})
        cities = [_C("nyc", 1.0, 2.0)]
        changes = detect_and_record(ledger, cities, client)
        assert changes == []
        assert get_hash(ledger, "nyc") is not None
        assert get_change_at(ledger, "nyc") is None
    finally:
        _cleanup(path)


def test_subsequent_stable_observation_is_a_noop():
    ledger, path = _temp_ledger()
    try:
        client = _FakeClient({(1.0, 2.0): _trivial_forecast(20.0)})
        cities = [_C("nyc", 1.0, 2.0)]
        detect_and_record(ledger, cities, client)
        h0 = get_hash(ledger, "nyc")
        changes = detect_and_record(ledger, cities, client)
        assert changes == []
        assert get_hash(ledger, "nyc") == h0
        assert get_change_at(ledger, "nyc") is None
    finally:
        _cleanup(path)


def test_real_movement_emits_change():
    ledger, path = _temp_ledger()
    try:
        # First call seeds 20.0.
        client_a = _FakeClient({(1.0, 2.0): _trivial_forecast(20.0)})
        cities = [_C("nyc", 1.0, 2.0)]
        # rotation_threshold_fraction=1.01 disables the storm guard for
        # this single-city test (a 1/1 flip would otherwise read as a
        # global rotation event and yield zero changes -- which is the
        # right call in production but not what this test is pinning).
        detect_and_record(ledger, cities, client_a,
                          rotation_threshold_fraction=1.01)
        h_seed = get_hash(ledger, "nyc")
        # Second call sees 23.0 (a real model update).
        client_b = _FakeClient({(1.0, 2.0): _trivial_forecast(23.0)})
        changes = detect_and_record(ledger, cities, client_b,
                                     rotation_threshold_fraction=1.01)
        assert len(changes) == 1
        assert changes[0]["city"] == "nyc"
        assert changes[0]["old_hash"] == h_seed
        assert changes[0]["new_hash"] != h_seed
        # change_ts populated.
        assert get_change_at(ledger, "nyc") is not None
    finally:
        _cleanup(path)


# ---------------------------------------------------------------- trigger dedupe
def test_trigger_guard_skips_within_one_hour():
    """A city that triggered a scan 30 minutes ago must NOT trigger
    again until the guard window passes."""
    ledger, path = _temp_ledger()
    try:
        now = datetime(2026, 6, 13, 12, 0, tzinfo=timezone.utc)
        thirty_min_ago = (now - timedelta(minutes=30)).isoformat(timespec="seconds")
        mark_scan_at(ledger, "nyc", thirty_min_ago)
        assert should_trigger_scan(
            ledger, "nyc",
            now_iso=now.isoformat(timespec="seconds"),
            guard_minutes=60.0) is False
    finally:
        _cleanup(path)


def test_trigger_guard_allows_after_one_hour():
    ledger, path = _temp_ledger()
    try:
        now = datetime(2026, 6, 13, 12, 0, tzinfo=timezone.utc)
        ninety_min_ago = (now - timedelta(minutes=90)).isoformat(timespec="seconds")
        mark_scan_at(ledger, "nyc", ninety_min_ago)
        assert should_trigger_scan(
            ledger, "nyc",
            now_iso=now.isoformat(timespec="seconds"),
            guard_minutes=60.0) is True
    finally:
        _cleanup(path)


def test_trigger_guard_allows_first_ever_trigger():
    ledger, path = _temp_ledger()
    try:
        now = datetime(2026, 6, 13, 12, 0, tzinfo=timezone.utc)
        assert should_trigger_scan(
            ledger, "nyc",
            now_iso=now.isoformat(timespec="seconds"),
            guard_minutes=60.0) is True
    finally:
        _cleanup(path)


def test_trigger_dedupe_per_city_independent():
    """Marking nyc must not block miami from triggering."""
    ledger, path = _temp_ledger()
    try:
        now = datetime(2026, 6, 13, 12, 0, tzinfo=timezone.utc)
        recent = (now - timedelta(minutes=5)).isoformat(timespec="seconds")
        mark_scan_at(ledger, "nyc", recent)
        assert should_trigger_scan(
            ledger, "nyc",
            now_iso=now.isoformat(timespec="seconds"),
            guard_minutes=60.0) is False
        assert should_trigger_scan(
            ledger, "miami",
            now_iso=now.isoformat(timespec="seconds"),
            guard_minutes=60.0) is True
    finally:
        _cleanup(path)


# ---------------------------------------------------------------- helper math
def test_minutes_since_forecast_change_reads_kv():
    ledger, path = _temp_ledger()
    try:
        now = datetime(2026, 6, 13, 12, 0, tzinfo=timezone.utc)
        from foundation.forecast_change import store_change
        flip = (now - timedelta(minutes=42)).isoformat(timespec="seconds")
        store_change(ledger, "nyc", "abc123", flip)
        mins = minutes_since_forecast_change(
            ledger, "nyc",
            now_iso=now.isoformat(timespec="seconds"))
        assert mins == pytest.approx(42.0, abs=0.05)
    finally:
        _cleanup(path)


def test_minutes_since_forecast_change_is_none_for_unknown_city():
    ledger, path = _temp_ledger()
    try:
        assert minutes_since_forecast_change(ledger, "nyc") is None
    finally:
        _cleanup(path)
