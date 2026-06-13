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


# ---------------------------------------------------------------- watcher
def detect_and_record(ledger, cities, client: ForecastClient,
                      forecast_days: int = 3,
                      deadline=None,
                      verbose: bool = False) -> list[dict]:
    """Fetch ensemble per city, hash the lightweight summary, compare
    against the stored hash. Records a CHANGE for every city whose hash
    flipped since the last watch. The first observation for a city is
    seeded silently (no change event) so we don't paper-trigger on
    cold-start.

    Returns the list of recorded changes:
      [{"city", "old_hash", "new_hash", "change_ts"}, ...]
    """
    from foundation.deadline import Deadline
    deadline = Deadline.coerce(deadline)
    changes: list[dict] = []
    for c in cities:
        if deadline.expired():
            if verbose:
                print(f"  wx-change-watch: deadline reached, "
                      f"skipping rest (next: {c.city})")
            break
        try:
            fc = client.ensemble(c.lat, c.lon,
                                 forecast_days=forecast_days)
        except RuntimeError as e:
            if verbose:
                print(f"  {c.city}: ensemble fetch failed: {e}")
            continue
        new_hash = compute_hash(summary_payload(fc))
        old_hash = get_hash(ledger, c.city)
        now_iso = datetime.now(timezone.utc).isoformat(timespec="seconds")
        if old_hash is None:
            store_hash_only(ledger, c.city, new_hash)
            if verbose:
                print(f"  {c.city}: seed {new_hash[:10]}")
            continue
        if old_hash != new_hash:
            store_change(ledger, c.city, new_hash, now_iso)
            changes.append({"city": c.city, "old_hash": old_hash,
                            "new_hash": new_hash, "change_ts": now_iso})
            if verbose:
                print(f"  {c.city}: HASH CHANGE "
                      f"{old_hash[:10]} -> {new_hash[:10]}")
        elif verbose:
            print(f"  {c.city}: stable {new_hash[:10]}")
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
