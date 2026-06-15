"""Storm-rotation guard + dispatch cap.

An Open-Meteo ensemble model rotation (~5x/day) flips EVERY city's
hash in a single watch. The watcher must NOT treat that as 41
independent forecast changes and dispatch 41 cycles -- that wastes
Actions minutes and starves the polymarketbot-state concurrency
group of any real scheduled work. Two layers of protection:

  1) If >50% of examined cities flip in one watch, treat as model
     rotation: re-seed every flipped hash silently (no change_at
     write, no dispatch), return [].
  2) Even when rotation isn't triggered (e.g. 40% flip), hard-cap
     dispatches at 3 per watch, ranked by proximity-to-bucket-boundary
     (smaller distance = more market-relevant).
"""
from __future__ import annotations

import os
import sys
import tempfile

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from foundation.forecast_change import (MAX_DISPATCHES_PER_WATCH,
                                          ROTATION_THRESHOLD_FRACTION,
                                          SUMMARY_HOURS,
                                          detect_and_record,
                                          distance_to_bucket_boundary,
                                          get_change_at, get_hash,
                                          representative_max_c,
                                          summary_payload)
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


class _C:
    def __init__(self, city, lat, lon):
        self.city = city; self.lat = lat; self.lon = lon


def _forecast(value: float) -> EnsembleForecast:
    """Stub ensemble whose families are all flat at `value`."""
    times = [f"2026-06-15T{h:02d}:00" for h in range(SUMMARY_HOURS)]
    members = {f"temperature_2m_member{i:02d}_ncep_gefs_seamless":
                [value for _ in range(SUMMARY_HOURS)]
                for i in range(3)}
    return EnsembleForecast(
        station_lat=0.0, station_lon=0.0, timezone="UTC",
        times=times, members=members, elevation_m=0.0,
    )


class _ClientByCity:
    """Stub ForecastClient: returns a configured forecast per (lat,lon)."""

    def __init__(self, by_latlon: dict[tuple[float, float], EnsembleForecast]):
        self._m = by_latlon

    def ensemble(self, lat, lon, forecast_days=3):
        return self._m[(lat, lon)]


# ---------------------------------------------------------------- proximity helpers
def test_distance_to_bucket_boundary_midbucket_is_half():
    """A forecast dead-center in an integer bucket (X.0) is maximally
    far from the .5 boundary -- distance 0.5."""
    assert distance_to_bucket_boundary(14.0) == pytest.approx(0.5)
    assert distance_to_bucket_boundary(20.0) == pytest.approx(0.5)


def test_distance_to_bucket_boundary_near_upper_edge_is_small():
    assert distance_to_bucket_boundary(14.4) == pytest.approx(0.1, abs=1e-9)


def test_distance_to_bucket_boundary_at_boundary_is_zero():
    """14.5 sits exactly between bucket 14 and bucket 15."""
    assert distance_to_bucket_boundary(14.5) == pytest.approx(0.0, abs=1e-9)


def test_distance_to_bucket_boundary_symmetric():
    """Equidistant inputs (14.4 vs 14.6) yield equal distances."""
    assert distance_to_bucket_boundary(14.4) == pytest.approx(
        distance_to_bucket_boundary(14.6), abs=1e-9)


def test_distance_to_bucket_boundary_none_input_is_half():
    """A missing forecast deprioritizes the city (max distance)."""
    assert distance_to_bucket_boundary(None) == 0.5


def test_representative_max_c_picks_largest_24h_value():
    payload = {"families": {"gfs": [10.0, 12.0, 14.0, 11.0]}}
    assert representative_max_c(payload) == 14.0


def test_representative_max_c_prefers_gfs_over_other_families():
    payload = {"families": {"gfs": [10.0], "icon": [99.0]}}
    assert representative_max_c(payload) == 10.0


def test_representative_max_c_falls_back_when_gfs_empty():
    payload = {"families": {"gfs": [], "icon": [99.0]}}
    assert representative_max_c(payload) == 99.0


def test_representative_max_c_returns_none_when_empty():
    assert representative_max_c({}) is None
    assert representative_max_c({"families": {}}) is None


# ---------------------------------------------------------------- rotation guard
def test_rotation_guard_reseeds_silently_when_majority_flip():
    """11/20 cities flip (55%) -> model-rotation pattern.
    Watcher must re-seed every changed hash WITHOUT recording
    change_at and return [] so the dispatch loop fires zero."""
    ledger, path = _temp_ledger()
    try:
        cities = [_C(f"c{i:02d}", float(i), float(i)) for i in range(20)]
        # Seed every city with hash for value=10.0.
        seed_client = _ClientByCity(
            {(c.lat, c.lon): _forecast(10.0) for c in cities})
        seeded = detect_and_record(ledger, cities, seed_client,
                                    throttle_seconds=0)
        assert seeded == []  # all seeds, no changes
        # Now move 11/20 cities to a new value (15.0); the rest stay at 10.0.
        rotated = dict(seed_client._m)
        for c in cities[:11]:
            rotated[(c.lat, c.lon)] = _forecast(15.0)
        rotated_client = _ClientByCity(rotated)
        out = detect_and_record(ledger, cities, rotated_client,
                                  throttle_seconds=0)
        assert out == []  # rotation detected -> zero dispatches
        # Hashes must have been re-seeded to the new value.
        for c in cities[:11]:
            h_new = get_hash(ledger, c.city)
            assert h_new is not None
        # CRITICAL: change_at must NOT be set for any of them. The
        # downstream `minutes_since_forecast_change` reader must keep
        # reflecting the LAST real per-city change, not the rotation.
        for c in cities[:11]:
            assert get_change_at(ledger, c.city) is None
    finally:
        _cleanup(path)


def test_rotation_guard_does_not_fire_below_threshold():
    """8/20 cities flip (40%) -- below the 50% rotation threshold.
    These should be treated as real per-city changes and surfaced
    (subject to the dispatch cap below)."""
    ledger, path = _temp_ledger()
    try:
        cities = [_C(f"c{i:02d}", float(i), float(i)) for i in range(20)]
        seed_client = _ClientByCity(
            {(c.lat, c.lon): _forecast(10.0) for c in cities})
        detect_and_record(ledger, cities, seed_client, throttle_seconds=0)
        partial = dict(seed_client._m)
        for c in cities[:8]:
            partial[(c.lat, c.lon)] = _forecast(15.0)
        partial_client = _ClientByCity(partial)
        out = detect_and_record(ledger, cities, partial_client,
                                  throttle_seconds=0)
        # Real changes -> non-empty (but capped at MAX_DISPATCHES_PER_WATCH).
        assert len(out) > 0
        # And change_at IS recorded for the dispatched ones.
        for ch in out:
            assert get_change_at(ledger, ch["city"]) is not None
    finally:
        _cleanup(path)


def test_rotation_threshold_constant_is_half():
    """Sanity-check the constant matches the spec."""
    assert ROTATION_THRESHOLD_FRACTION == 0.5


# ---------------------------------------------------------------- dispatch cap
def test_dispatch_cap_keeps_only_top_three_by_proximity():
    """5 cities flip (below rotation threshold of 50% of 20). The cap
    of MAX_DISPATCHES_PER_WATCH=3 keeps the 3 closest to a bucket
    boundary, dropping the 2 that sit dead-center in their buckets."""
    ledger, path = _temp_ledger()
    try:
        cities = [_C(f"c{i:02d}", float(i), float(i)) for i in range(20)]
        # Seed all at 10.0.
        seed_client = _ClientByCity(
            {(c.lat, c.lon): _forecast(10.0) for c in cities})
        detect_and_record(ledger, cities, seed_client, throttle_seconds=0)
        # Flip 5 cities, each to a different forecast value with
        # KNOWN distance to nearest .5 boundary:
        #   c00: 14.0 -> distance 0.5  (worst -- mid-bucket)
        #   c01: 14.45 -> distance 0.05 (best -- near boundary)
        #   c02: 20.0 -> distance 0.5  (worst)
        #   c03: 22.1 -> distance 0.4
        #   c04: 18.5 -> distance 0.0  (perfect boundary, best)
        flip_values = {
            "c00": 14.0,
            "c01": 14.45,
            "c02": 20.0,
            "c03": 22.1,
            "c04": 18.5,
        }
        flipped = dict(seed_client._m)
        for c in cities[:5]:
            flipped[(c.lat, c.lon)] = _forecast(flip_values[c.city])
        flipped_client = _ClientByCity(flipped)
        out = detect_and_record(ledger, cities, flipped_client,
                                  throttle_seconds=0)
        assert len(out) == MAX_DISPATCHES_PER_WATCH == 3
        kept = {d["city"] for d in out}
        # The three lowest-distance cities should be kept; the two
        # mid-bucket (14.0/20.0) should be dropped.
        assert kept == {"c04", "c01", "c03"}
        # And the list is sorted ascending by distance (most sensitive first).
        distances = [d["distance_to_boundary"] for d in out]
        assert distances == sorted(distances)
    finally:
        _cleanup(path)


def test_dispatch_cap_is_a_noop_when_fewer_than_cap():
    """Two cities flip; cap of 3 is not invoked, both returned."""
    ledger, path = _temp_ledger()
    try:
        cities = [_C(f"c{i:02d}", float(i), float(i)) for i in range(20)]
        seed = _ClientByCity({(c.lat, c.lon): _forecast(10.0) for c in cities})
        detect_and_record(ledger, cities, seed, throttle_seconds=0)
        flipped = dict(seed._m)
        flipped[(0.0, 0.0)] = _forecast(15.0)
        flipped[(1.0, 1.0)] = _forecast(16.5)
        out = detect_and_record(ledger, cities, _ClientByCity(flipped),
                                  throttle_seconds=0)
        assert len(out) == 2
        assert {d["city"] for d in out} == {"c00", "c01"}
    finally:
        _cleanup(path)


def test_max_dispatches_constant_is_three():
    assert MAX_DISPATCHES_PER_WATCH == 3
