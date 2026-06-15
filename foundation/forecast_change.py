"""Forecast-change detection.

A lightweight per-city ensemble summary, hashed for content stability.
When the hash flips between fast-cycle runs the weather edge for that
city is potentially stale -- we want the cycle to re-scan immediately
instead of waiting for the next 30-minute cron.

The summary is intentionally coarse: per-family hourly mean snapped to
the nearest 0.5 C across the first 48 hours. Coarse enough to ignore
floating-point noise across identical Open-Meteo responses; fine enough
to trip on a real model update of a few tenths of a degree per family.

The watcher writes three keys per city into the cv_state kv table:
  forecast_hash:<city>        most recently observed hash
  forecast_change_at:<city>   UTC iso ts when the hash last flipped
  forecast_scan_at:<city>     UTC iso ts when we last *triggered* a scan
                              (used for the 1/hr per-city guard)
"""
from __future__ import annotations

import hashlib
import json
import time
from datetime import datetime, timezone

from strategies.weather import ForecastClient, family_of


# Per-family quantization step (Celsius). Stable under Open-Meteo
# floating-point jitter, sensitive to real model movement.
QUANTIZE_C = 0.5
# Hours of horizon included in the hash. Markets in scope resolve today
# or tomorrow; forecast_days=3 is overkill for change detection.
SUMMARY_HOURS = 48


def quantize(v: float, step: float = QUANTIZE_C) -> float:
    return round(v / step) * step


def summary_payload(fc, hours: int = SUMMARY_HOURS) -> dict:
    """Per-family hourly mean over `hours`, snapped to QUANTIZE_C."""
    by_family: dict[str, list[str]] = {}
    for k in fc.members:
        by_family.setdefault(family_of(k), []).append(k)
    fams: dict[str, list[float | None]] = {}
    n = min(len(fc.times), hours)
    for fam in sorted(by_family):
        series: list[float | None] = []
        for i in range(n):
            vals: list[float] = []
            for k in by_family[fam]:
                v = fc.members[k][i] if i < len(fc.members[k]) else None
                if v is not None:
                    vals.append(float(v))
            if vals:
                series.append(quantize(sum(vals) / len(vals)))
            else:
                series.append(None)
        fams[fam] = series
    return {"times": fc.times[:n], "families": fams}


def compute_hash(payload: dict) -> str:
    blob = json.dumps(payload, sort_keys=True,
                     separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(blob).hexdigest()


# ---------------------------------------------------------------- kv keys
def _hash_key(city: str) -> str:
    return f"forecast_hash:{city}"


def _change_at_key(city: str) -> str:
    return f"forecast_change_at:{city}"


def _scan_at_key(city: str) -> str:
    return f"forecast_scan_at:{city}"


# ---------------------------------------------------------------- io
def store_change(ledger, city: str, h: str, change_ts: str) -> None:
    ledger.cv_state_set(_hash_key(city), h)
    ledger.cv_state_set(_change_at_key(city), change_ts)


def store_hash_only(ledger, city: str, h: str) -> None:
    """First observation for this city: pin the hash WITHOUT recording a
    change event (we don't know when it actually changed -- it's just the
    starting baseline)."""
    ledger.cv_state_set(_hash_key(city), h)


def get_hash(ledger, city: str) -> str | None:
    return ledger.cv_state_get(_hash_key(city))


def get_change_at(ledger, city: str) -> str | None:
    return ledger.cv_state_get(_change_at_key(city))


def get_scan_at(ledger, city: str) -> str | None:
    return ledger.cv_state_get(_scan_at_key(city))


def mark_scan_at(ledger, city: str, ts: str) -> None:
    ledger.cv_state_set(_scan_at_key(city), ts)


# ---------------------------------------------------------------- helpers
def _parse_iso(s: str | None) -> datetime | None:
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None


def minutes_between(now_iso: str, then_iso: str | None) -> float | None:
    a = _parse_iso(now_iso)
    b = _parse_iso(then_iso)
    if a is None or b is None:
        return None
    return (a - b).total_seconds() / 60.0


def minutes_since_forecast_change(ledger, city: str,
                                  now_iso: str | None = None
                                  ) -> float | None:
    """Wall-clock minutes between `now` and the last recorded hash flip
    for this city. Returns None when no change has ever been recorded."""
    now_iso = now_iso or datetime.now(timezone.utc).isoformat(
        timespec="seconds")
    return minutes_between(now_iso, get_change_at(ledger, city))


# ---------------------------------------------------------------- proximity ranking
def representative_max_c(payload: dict) -> float | None:
    """Pick a single representative max-temp value from the lightweight
    summary -- the highest hourly per-family-mean across the next 24h,
    preferring GFS (largest population, smoothest signal). The payload
    is quantized to a 0.5-degree grid for hash stability, so values
    here are snapped (use representative_max_c_raw for the unquantized
    continuous version that distance-to-boundary needs).
    """
    families = (payload or {}).get("families") or {}
    for fam in ("gfs", "ifs", "aifs", "icon"):
        series = families.get(fam) or []
        next24 = [v for v in series[:24] if v is not None]
        if next24:
            return float(max(next24))
    return None


def representative_max_c_raw(fc) -> float | None:
    """Same idea as `representative_max_c` but reads the RAW ensemble
    values (before the 0.5-degree quantization used for hash stability).
    The dispatch-ranking distance-to-bucket-boundary calculation needs
    continuous values -- if we read off the quantized payload, every
    distance is either exactly 0 or exactly 0.5, and the proximity
    ranking degenerates into a stable-sort over source order.
    """
    by_fam: dict[str, list[list]] = {}
    for k, series in fc.members.items():
        by_fam.setdefault(family_of(k), []).append(series)
    for fam in ("gfs", "ifs", "aifs", "icon"):
        keys = by_fam.get(fam) or []
        if not keys:
            continue
        n_hours = min(min(len(s) for s in keys), 24)
        per_hour_means: list[float] = []
        for h in range(n_hours):
            vals = [s[h] for s in keys if s[h] is not None]
            if vals:
                per_hour_means.append(sum(vals) / len(vals))
        if per_hour_means:
            return float(max(per_hour_means))
    return None


def distance_to_bucket_boundary(temp_c: float | None) -> float:
    """Distance (in degrees C, 0..0.5) from `temp_c` to the nearest
    integer-rounded bucket boundary. Polymarket weather markets bucket
    by integer degree, so the boundary between bucket X and X+1 sits
    at X+0.5. A forecast at 14.4 (distance 0.1) is much more
    likely to flip YES->NO on a model update than one at 14.0
    (distance 0.5, dead-center).

    None inputs yield 0.5 (treated as deprioritized / max distance).
    """
    if temp_c is None:
        return 0.5
    return 0.5 - abs(temp_c - round(temp_c))


# ---------------------------------------------------------------- watcher
# An Open-Meteo model rotation (e.g. GFS 06z run finishing) flips EVERY
# city's hash at once -- it's not per-city forecast change, it's a
# global model-version swap. Dispatching a separate scan for every
# changed city in that situation wastes Actions minutes and starves
# scheduled cycles of their concurrency window. When more than this
# fraction of configured cities flip in a single watch, treat it as a
# rotation: re-seed every city's hash silently (no change_at writes,
# no dispatch) and let the next watch establish a true per-city signal.
ROTATION_THRESHOLD_FRACTION = 0.5
# Hard cap on per-watch dispatches. Even when rotation isn't detected
# (e.g. 40% flip), dispatching dozens of cycles in one watch starves
# the concurrency group of any other work. Three is enough to cover
# the most edge-sensitive cities; the rest can wait for the next watch.
MAX_DISPATCHES_PER_WATCH = 3


def detect_and_record(ledger, cities, client: ForecastClient,
                      forecast_days: int = 3,
                      deadline=None,
                      verbose: bool = False,
                      throttle_seconds: float = 0.5,
                      rotation_threshold_fraction: float = ROTATION_THRESHOLD_FRACTION,
                      max_dispatches: int = MAX_DISPATCHES_PER_WATCH,
                      ) -> list[dict]:
    """Two-pass watcher with global-rotation guard.

    Pass 1: fetch per-city ensemble, hash the lightweight summary, build
            a candidate change list. NOTHING is written to the ledger
            yet (no change_at timestamps, no hash updates).

    Decision: if `len(candidates) > rotation_threshold_fraction * len(cities)`,
              this looks like an Open-Meteo model rotation rather than
              real per-city forecast movement. Re-seed every candidate
              city's hash silently (write new_hash, NO change_at), log
              "ROTATION DETECTED", and return [] so the dispatch step
              fires zero workflows.

    Pass 2: otherwise, record change_at for each candidate, rank by
            distance-to-bucket-boundary (smaller is more market-relevant),
            cap at `max_dispatches`, and return the truncated list.
    """
    from foundation.deadline import Deadline
    deadline = Deadline.coerce(deadline)
    # Pass 1: collect candidates. Seeds (new cities) are still written
    # immediately because they don't dispatch and we don't want to lose
    # the baseline.
    candidates: list[dict] = []
    examined = 0
    for i, c in enumerate(cities):
        if deadline.expired():
            if verbose:
                print(f"  wx-change-watch: deadline reached, "
                      f"skipping rest (next: {c.city})")
            break
        if i > 0 and throttle_seconds > 0:
            time.sleep(throttle_seconds)
        try:
            fc = client.ensemble(c.lat, c.lon,
                                 forecast_days=forecast_days)
        except RuntimeError as e:
            if verbose:
                print(f"  {c.city}: ensemble fetch failed: {e}")
            continue
        examined += 1
        payload = summary_payload(fc)
        new_hash = compute_hash(payload)
        old_hash = get_hash(ledger, c.city)
        if old_hash is None:
            store_hash_only(ledger, c.city, new_hash)
            if verbose:
                print(f"  {c.city}: seed {new_hash[:10]}")
            continue
        if old_hash == new_hash:
            if verbose:
                print(f"  {c.city}: stable {new_hash[:10]}")
            continue
        rep_c = representative_max_c_raw(fc)
        dist = distance_to_bucket_boundary(rep_c)
        candidates.append({
            "city": c.city,
            "old_hash": old_hash,
            "new_hash": new_hash,
            "rep_max_c": rep_c,
            "distance_to_boundary": dist,
        })
        if verbose:
            print(f"  {c.city}: HASH CHANGE "
                  f"{old_hash[:10]} -> {new_hash[:10]} "
                  f"(max~{rep_c}C, d={dist:.2f})")

    # Rotation guard. examined excludes cities we couldn't fetch.
    if examined > 0 and len(candidates) > rotation_threshold_fraction * examined:
        if verbose:
            print(f"  ROTATION DETECTED: {len(candidates)}/{examined} "
                  f"cities flipped (> {rotation_threshold_fraction*100:.0f}%). "
                  f"Re-seeding silently, dispatching ZERO.")
        # Re-seed every candidate's hash WITHOUT recording change_at.
        # store_hash_only writes only forecast_hash:<city>, leaving
        # forecast_change_at untouched -- so minutes_since_forecast_change
        # for downstream consumers continues to reflect the LAST real
        # per-city change, not this rotation.
        for cand in candidates:
            store_hash_only(ledger, cand["city"], cand["new_hash"])
        return []

    # Pass 2: this is real per-city change. Record change_at, rank, cap.
    now_iso = datetime.now(timezone.utc).isoformat(timespec="seconds")
    changes: list[dict] = []
    for cand in candidates:
        store_change(ledger, cand["city"], cand["new_hash"], now_iso)
        cand["change_ts"] = now_iso
        changes.append(cand)
    # Rank by proximity to bucket boundary (smaller = more sensitive).
    changes.sort(key=lambda d: d["distance_to_boundary"])
    if len(changes) > max_dispatches:
        if verbose:
            print(f"  dispatch cap: {len(changes)} candidates -> "
                  f"keeping top {max_dispatches} by bucket proximity")
        changes = changes[:max_dispatches]
    return changes


def should_trigger_scan(ledger, city: str, now_iso: str | None = None,
                        guard_minutes: float = 60.0) -> bool:
    """Per-city dedupe guard: at most one triggered scan per
    `guard_minutes`. Returns True when no prior trigger exists or the
    last trigger is older than the guard."""
    last = get_scan_at(ledger, city)
    if last is None:
        return True
    mins = minutes_between(
        now_iso or datetime.now(timezone.utc).isoformat(timespec="seconds"),
        last,
    )
    if mins is None:
        return True
    return mins >= guard_minutes
