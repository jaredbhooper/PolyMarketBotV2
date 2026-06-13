"""Weather strategy v1 (sec 5 of the build plan).

Ensemble temperature model: Open-Meteo Ensemble API -> per-member daily-max
in the market's resolution window -> raw fraction + gaussian blend ->
Estimate. Implements the Strategy ABC.
"""
from __future__ import annotations

import math
import re
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

import numpy as np
import requests
from scipy.stats import norm

from foundation.wunderground import (
    WundergroundClient,
    parse_station_from_url,
)
from strategies.base import Estimate, Market, Strategy


ENSEMBLE_URL = "https://ensemble-api.open-meteo.com/v1/ensemble"
ARCHIVE_URL = "https://archive-api.open-meteo.com/v1/archive"

# Default model bundle for the ensemble call. Open-Meteo serves:
#   gfs_seamless      -> 31 GEFS members      (NCEP)
#   ecmwf_ifs025      -> 51 IFS members       (ECMWF dynamical ensemble)
#   ecmwf_aifs025     -> 51 AIFS members      (ECMWF AI ensemble)
#   icon_seamless     -> 40 ICON EPS members  (DWD)
# Total: 173 (up from 82 with just GFS+IFS). Note: `ecmwf_aifs025_single`
# returns the 1-member deterministic AIFS forecast, which is currently
# unpopulated through the ensemble endpoint; `ecmwf_aifs025` (no _single
# suffix) is the full perturbed AIFS ensemble and is what we want.
DEFAULT_MODELS = "gfs_seamless,ecmwf_ifs025,ecmwf_aifs025,icon_seamless"


# Family classification - drives the multi-family disagreement gate.
def family_of(member_key: str) -> str:
    """Identify which model family a `temperature_2m...` series belongs to.

    Keys look like:
      temperature_2m_ncep_gefs_seamless              (control)
      temperature_2m_member07_ncep_gefs_seamless
      temperature_2m_ecmwf_ifs025_ensemble           (control)
      temperature_2m_member03_ecmwf_ifs025_ensemble
      temperature_2m_ecmwf_aifs025_single
      temperature_2m_member01_icon_seamless_eps
    """
    suffix = member_key.replace("temperature_2m_", "")
    if suffix.startswith("member"):
        # member01_ncep_gefs_seamless -> drop "memberNN_"
        try:
            suffix = suffix.split("_", 1)[1]
        except IndexError:
            return "other"
    s = suffix.lower()
    if "aifs" in s:
        return "aifs"
    if "gefs" in s or s.endswith("gfs_seamless"):
        return "gfs"
    if "ifs" in s:
        return "ifs"
    if "icon" in s:
        return "icon"
    return "other"


# ---------------------------------------------------------------- units
def c_to_f(c: float) -> float:
    return c * 9.0 / 5.0 + 32.0


def f_to_c(f: float) -> float:
    return (f - 32.0) * 5.0 / 9.0


# ---------------------------------------------------------------- city config
@dataclass
class CityCfg:
    city: str
    station_name: str
    lat: float
    lon: float
    timezone: str
    resolution_source: str
    slug_hints: list[str]


def _load_cities(cfg: dict) -> list[CityCfg]:
    raw = (cfg.get("strategies") or {}).get("weather", {}).get("cities", [])
    out = []
    for c in raw:
        out.append(CityCfg(
            city=c["city"],
            station_name=c.get("station_name", ""),
            lat=float(c["lat"]),
            lon=float(c["lon"]),
            timezone=c.get("timezone", "UTC"),
            resolution_source=c.get("resolution_source", ""),
            slug_hints=[h.lower() for h in c.get("slug_hints", [])],
        ))
    return out


def _match_city(market: Market, cities: list[CityCfg]) -> CityCfg | None:
    blob = " ".join([
        (market.slug or "").lower(),
        (market.extras.get("event_slug") or "").lower(),
        (market.question or "").lower(),
        (market.extras.get("event_title") or "").lower(),
    ])
    for c in cities:
        # All slug_hints come pre-lowered; match on word boundary so "ord"
        # doesn't accidentally hit "word" / "chord".
        for h in c.slug_hints:
            if not h:
                continue
            if re.search(rf"\b{re.escape(h)}\b", blob):
                return c
    return None


# ---------------------------------------------------------------- forecast
@dataclass
class EnsembleForecast:
    station_lat: float
    station_lon: float
    timezone: str
    times: list[str]           # local ISO strings (per Open-Meteo)
    members: dict[str, list[float]]   # member_key -> hourly series in C
    elevation_m: float

    def member_keys_by_model(self) -> tuple[list[str], list[str]]:
        # Legacy two-family helper (kept for callers that just want GFS+ECMWF).
        gfs = [k for k in self.members if family_of(k) == "gfs"]
        ecmwf = [k for k in self.members if family_of(k) in ("ifs", "aifs")]
        return gfs, ecmwf

    def member_keys_by_family(self) -> dict[str, list[str]]:
        out: dict[str, list[str]] = {}
        for k in self.members:
            out.setdefault(family_of(k), []).append(k)
        return out


class ForecastClient:
    def __init__(self, timeout: int = 30):
        self.timeout = timeout
        self._sess = requests.Session()
        self._sess.headers.update({"User-Agent": "PolyMarketBotV2/0.1 (weather)"})
        self._cache: dict[tuple[float, float, int], EnsembleForecast] = {}

    def _get(self, url: str, params: dict, retries: int = 3) -> dict:
        last = None
        for i in range(retries):
            try:
                r = self._sess.get(url, params=params, timeout=self.timeout)
                if r.status_code == 200:
                    return r.json()
                last = f"HTTP {r.status_code}: {r.text[:200]}"
            except requests.RequestException as e:
                last = str(e)
            time.sleep(0.5 * (2 ** i))
        raise RuntimeError(f"GET {url} failed: {last}")

    def ensemble(self, lat: float, lon: float, forecast_days: int = 3) -> EnsembleForecast:
        key = (round(lat, 4), round(lon, 4), int(forecast_days))
        if key in self._cache:
            return self._cache[key]
        data = self._get(ENSEMBLE_URL, {
            "latitude": lat, "longitude": lon,
            "hourly": "temperature_2m",
            "models": DEFAULT_MODELS,
            "forecast_days": forecast_days,
            "timezone": "auto",
        })
        hourly = data.get("hourly", {})
        times = hourly.get("time", [])
        members = {k: v for k, v in hourly.items()
                   if k.startswith("temperature_2m") and k != "time"}
        fc = EnsembleForecast(
            station_lat=lat, station_lon=lon,
            timezone=data.get("timezone", "UTC"),
            times=times, members=members,
            elevation_m=float(data.get("elevation", 0.0)),
        )
        self._cache[key] = fc
        return fc

    def archive_max_temp_c(self, lat: float, lon: float, day_iso: str,
                           tz: str) -> float | None:
        return self._archive_daily(lat, lon, day_iso, tz, "temperature_2m_max")

    def archive_min_temp_c(self, lat: float, lon: float, day_iso: str,
                           tz: str) -> float | None:
        return self._archive_daily(lat, lon, day_iso, tz, "temperature_2m_min")

    def _archive_daily(self, lat: float, lon: float, day_iso: str,
                       tz: str, field: str) -> float | None:
        try:
            data = self._get(ARCHIVE_URL, {
                "latitude": lat, "longitude": lon,
                "start_date": day_iso, "end_date": day_iso,
                "daily": field,
                "timezone": tz,
            })
            arr = (data.get("daily") or {}).get(field, [])
            if arr and arr[0] is not None:
                return float(arr[0])
        except RuntimeError:
            return None
        return None


# ---------------------------------------------------------------- window logic
def _local_window_for_date(date_iso: str, tz: str) -> tuple[datetime, datetime]:
    """The market resolution window: full local calendar day.
    Polymarket weather mkts resolve on the named station's reported high
    for the named calendar day (per the rules text).
    """
    z = ZoneInfo(tz)
    d = datetime.fromisoformat(date_iso).date()
    start = datetime(d.year, d.month, d.day, 0, 0, tzinfo=z)
    end = start + timedelta(days=1) - timedelta(hours=1)
    return start, end


def _parse_local_time(t: str, tz: str) -> datetime:
    # Open-Meteo returns "YYYY-MM-DDTHH:MM" already in the requested tz.
    return datetime.fromisoformat(t).replace(tzinfo=ZoneInfo(tz))


def local_day_has_ended(date_iso: str, tz: str,
                        settled_at: str | None = None) -> bool:
    """True when the named local calendar day is fully over.

    Polymarket weather markets resolve on the station's LOCAL calendar
    day, NOT on UTC. A market with resolve_date == today UTC may already
    be settle-able (Asia/Pacific stations) or still mid-afternoon
    (Americas). The grader gates on UTC date for cheap pre-filtering;
    the strategy is the authority that knows the timezone, so this is
    where the local check lives.

    `settled_at` is an ISO timestamp ("now" from the grader's POV). When
    omitted, datetime.now(utc) is used. Tests pass an explicit value so
    the contract is deterministic.
    """
    try:
        z = ZoneInfo(tz)
        d = datetime.fromisoformat(date_iso).date()
    except (ValueError, TypeError):
        return False
    end_local = datetime(d.year, d.month, d.day, 23, 59, 59,
                         tzinfo=z) + timedelta(seconds=1)  # exclusive end
    if settled_at:
        try:
            now = datetime.fromisoformat(settled_at.replace("Z", "+00:00"))
        except (ValueError, TypeError):
            now = datetime.now(timezone.utc)
    else:
        now = datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    return now >= end_local


def member_extreme_in_window(fc: EnsembleForecast, member_key: str,
                             start: datetime, end: datetime,
                             kind: str = "max") -> float | None:
    series = fc.members.get(member_key) or []
    if not series:
        return None
    out = None
    for t, v in zip(fc.times, series):
        if v is None:
            continue
        ts = _parse_local_time(t, fc.timezone)
        if start <= ts <= end:
            if out is None:
                out = float(v)
            elif kind == "min" and v < out:
                out = float(v)
            elif kind == "max" and v > out:
                out = float(v)
    return out


def member_extrema(fc: EnsembleForecast, start: datetime, end: datetime,
                   kind: str = "max") -> dict[str, float]:
    out = {}
    for k in fc.members:
        v = member_extreme_in_window(fc, k, start, end, kind=kind)
        if v is not None:
            out[k] = v
    return out


# Back-compat aliases (used by older unit tests).
def member_max_in_window(fc, key, start, end):
    return member_extreme_in_window(fc, key, start, end, "max")
def member_maxima(fc, start, end):
    return member_extrema(fc, start, end, "max")


# ---------------------------------------------------------------- probability
def _prob_for_band(maxima_c: np.ndarray, bound: str, lo_c: float | None,
                   hi_c: float | None, blend_raw: float = 0.5) -> tuple[float, float, float, float, float]:
    """Return (p_blend, p_raw, p_gauss, mu, sigma).

    bound:  'le' -> max <= hi
            'ge' -> max >= lo
            'eq' -> floor(max) == threshold (1 deg bucket)
            'range' -> lo <= max <= hi  (Miami's 2F buckets etc.)

    All temps in Celsius (we convert market threshold to C upstream).
    """
    if len(maxima_c) == 0:
        return 0.5, 0.5, 0.5, float("nan"), float("nan")
    mu = float(np.mean(maxima_c))
    sigma = float(np.std(maxima_c, ddof=1)) if len(maxima_c) > 1 else 0.5
    sigma = max(sigma, 0.2)

    if bound == "le":
        # P(max <= hi). Polymarket "13C or below" is integer rounded, so
        # boundary is hi + 0.5 (continuity correction).
        cut = hi_c + 0.5
        p_raw = float(np.mean(maxima_c <= cut))
        p_gauss = float(norm.cdf(cut, mu, sigma))
    elif bound == "ge":
        # P(max >= lo). "32C or above" -> >= 32 -> cut at 31.5
        cut = lo_c - 0.5
        p_raw = float(np.mean(maxima_c >= cut))
        p_gauss = float(1.0 - norm.cdf(cut, mu, sigma))
    elif bound == "eq":
        # P(floor(max) == t) -> P(t - 0.5 <= max < t + 0.5)
        lo_cut = (lo_c or 0) - 0.5
        hi_cut = (hi_c or 0) + 0.5
        p_raw = float(np.mean((maxima_c >= lo_cut) & (maxima_c < hi_cut)))
        p_gauss = float(norm.cdf(hi_cut, mu, sigma) - norm.cdf(lo_cut, mu, sigma))
    else:  # 'range'  lo..hi inclusive of floor(maxes)
        lo_cut = (lo_c if lo_c is not None else -math.inf) - 0.5
        hi_cut = (hi_c if hi_c is not None else math.inf) + 0.5
        p_raw = float(np.mean((maxima_c >= lo_cut) & (maxima_c < hi_cut)))
        p_gauss = float(norm.cdf(hi_cut, mu, sigma) - norm.cdf(lo_cut, mu, sigma))

    p_blend = blend_raw * p_raw + (1.0 - blend_raw) * p_gauss
    return p_blend, p_raw, p_gauss, mu, sigma


# ---------------------------------------------------------------- strategy class
class WeatherStrategy(Strategy):
    name = "weather"

    def __init__(self, cfg: dict):
        s = (cfg.get("strategies") or {}).get("weather", {})
        self.edge_threshold = float(s.get("edge_threshold", 0.08))
        self.kelly_fraction = float(s.get("kelly_fraction", 0.15))
        self.blend_raw_weight = float(s.get("blend_raw_weight", 0.5))
        self.disagreement_threshold = float(s.get("disagreement_threshold", 0.25))
        self.low_confidence = float(s.get("low_confidence", 0.5))
        self.clamp_min = float(s.get("clamp_min", 0.02))
        self.clamp_max = float(s.get("clamp_max", 0.98))
        self.forecast_days = int(s.get("forecast_days", 3))
        self.cities = _load_cities(cfg)
        self.client = ForecastClient()
        # Multi-family disagreement gate: a family must have >= this many
        # members to contribute its P to the disagreement computation. AIFS
        # at 1 member always gets pooled into the main estimate but doesn't
        # itself trigger the gate (binary single-member P is uninformative).
        self.min_family_members = int(s.get("min_family_members_for_gate", 10))
        # Cross-check against Wunderground (the actual resolution source).
        self.dispute_threshold = float(s.get("dispute_threshold_degrees", 1.0))
        self.wu = WundergroundClient()
        # Adaptive layer (v2.1). NEVER prunes individual ensemble members
        # - members are interchangeable samples, per-member skill is
        # noise. We only learn (a) MODEL-FAMILY weights per city,
        # (b) per-city per-family BIAS corrections, (c) probability
        # CALIBRATION. All three layers shrink toward neutral priors so
        # thin data cannot dominate. Kill-switch: adaptive_weights=false
        # reverts to the exact pre-v2.1 behaviour.
        from foundation.wx_skill import AdaptiveConfig
        # Backwards compat: allow `adaptive_weights: false` as a top-level
        # boolean OR a dict of params. Check the raw value BEFORE the
        # `or {}` coercion (which turns False into the default dict).
        raw_adapt = s.get("adaptive_weights")
        if raw_adapt is False:
            self.adaptive_enabled = False
            self.adaptive_cfg = AdaptiveConfig(enabled=False)
            adapt_cfg = {}
        else:
            adapt_cfg = raw_adapt or {}
            if not isinstance(adapt_cfg, dict):
                adapt_cfg = {}
            self.adaptive_enabled = bool(adapt_cfg.get("enabled", True))
            self.adaptive_cfg = AdaptiveConfig(
                enabled=self.adaptive_enabled,
                window_days=int(adapt_cfg.get("window_days", 60)),
                shrink_n=int(adapt_cfg.get("shrink_n", 20)),
                min_n=int(adapt_cfg.get("min_n", 10)),
                calib_min_n=int(adapt_cfg.get("calib_min_n", 30)),
                max_bias_correction=float(adapt_cfg.get(
                    "max_bias_correction", 1.5)),
                epsilon_mae=float(adapt_cfg.get("epsilon_mae", 0.05)),
            )
        # Per-cycle cache of adaptive state. Cleared on attach_ledger; the
        # cycle loop wires the ledger in BEFORE calling estimate so the
        # cache is fresh each cycle.
        self._adaptive_cache: dict[str, Any] = {}
        self._adaptive_ledger = None
        self._adaptive_verifications_cached = None
        self._adaptive_calibration = None

    def attach_ledger(self, ledger) -> None:
        """Called by main.py once per cycle before estimate() so the
        adaptive layer can pull verification rows. Resets the per-cycle
        cache - the cached values are valid only for this cycle."""
        self._adaptive_ledger = ledger
        self._adaptive_cache = {}
        self._adaptive_verifications_cached = None
        self._adaptive_calibration = None

    def _adaptive_state(self, city: str | None):
        """Lazy compute per-city CityAdaptive + CalibrationParam. Cached
        for the cycle. When no ledger is attached or adaptive is OFF,
        returns (None, None) and the caller falls back to the existing
        pool-all-members logic."""
        if not self.adaptive_enabled or self._adaptive_ledger is None:
            return None, None
        if self._adaptive_verifications_cached is None:
            try:
                rows = self._adaptive_ledger.list_wx_verifications()
                self._adaptive_verifications_cached = [dict(r) for r in rows]
            except Exception:
                self._adaptive_verifications_cached = []
        rows = self._adaptive_verifications_cached
        # Calibration is computed across all cities (one global parameter).
        if self._adaptive_calibration is None:
            from foundation.wx_skill import fit_calibration
            self._adaptive_calibration = fit_calibration(rows, self.adaptive_cfg)
        if city in self._adaptive_cache:
            return self._adaptive_cache[city], self._adaptive_calibration
        from foundation.wx_skill import adaptive_for_city
        state = adaptive_for_city(rows, city, self.adaptive_cfg)
        self._adaptive_cache[city] = state
        return state, self._adaptive_calibration

    # ------------- foundation contract --------------------------------
    def relevant_markets(self, markets: list[Market]) -> list[Market]:
        out = []
        for m in markets:
            event_slug = (m.extras.get("event_slug") or "")
            if not ("highest-temperature" in event_slug
                    or "lowest-temperature" in event_slug):
                continue
            if _match_city(m, self.cities) is None:
                continue
            if not m.extras.get("parsed_unit"):
                continue
            if m.resolve_date is None:
                continue
            # Only today + tomorrow (sec 4).
            try:
                d = datetime.fromisoformat(m.resolve_date).date()
            except (TypeError, ValueError):
                continue
            today_utc = datetime.now(timezone.utc).date()
            if d < today_utc or (d - today_utc).days > 2:
                continue
            out.append(m)
        return out

    def estimate(self, market: Market) -> Estimate | None:
        city = _match_city(market, self.cities)
        if city is None:
            return None
        unit = market.extras.get("parsed_unit") or "C"
        bound = market.extras.get("parsed_bound") or "eq"
        lo = market.extras.get("lo")
        hi = market.extras.get("hi")
        if bound in ("eq", "range") and (lo is None or hi is None):
            return None
        # Convert market thresholds to Celsius (model is Celsius).
        if unit == "F":
            lo_c = f_to_c(lo) if lo is not None else None
            hi_c = f_to_c(hi) if hi is not None else None
        else:
            lo_c = float(lo) if lo is not None else None
            hi_c = float(hi) if hi is not None else None

        try:
            fc = self.client.ensemble(city.lat, city.lon,
                                      forecast_days=self.forecast_days)
        except RuntimeError as e:
            return Estimate(p_final=0.5, confidence=0.0,
                            metadata={"error": str(e), "stage": "ensemble_fetch"})

        try:
            start, end = _local_window_for_date(market.resolve_date, city.timezone)
        except (TypeError, ValueError):
            return None

        kind = market.extras.get("kind") or "max"
        extrema = member_extrema(fc, start, end, kind=kind)
        if not extrema:
            return Estimate(p_final=0.5, confidence=0.0,
                            metadata={"error": "no member extrema",
                                      "kind": kind,
                                      "window": [start.isoformat(), end.isoformat()]})
        # Bucket members by family for the multi-family disagreement gate.
        families_keys: dict[str, list[str]] = {}
        for k in extrema:
            families_keys.setdefault(family_of(k), []).append(k)
        families_arr = {f: np.array([extrema[k] for k in ks])
                        for f, ks in families_keys.items()}

        # ---------- Adaptive layer: per-family BIAS correction ----------
        # Apply ONCE before any aggregation. NEVER touches individual
        # members in a selective way - we just shift the whole family
        # toward zero signed error using the rolling-window estimate
        # (which itself is shrunk toward zero - see foundation/wx_skill).
        # The bias correction is ALWAYS applied even with kill switch off,
        # because adaptive_for_city returns zero biases when n < min_n.
        adapt_state, adapt_calibration = self._adaptive_state(city.city)
        if adapt_state is not None:
            # Map per-family bias (gfs / ecmwf) onto raw families
            # (gfs / ifs / aifs / icon). ECMWF bias applies to both ifs
            # and aifs. ICON has no learned bias yet (only adapt gfs +
            # ecmwf for now); it stays uncorrected.
            family_bias = {"gfs": adapt_state.biases.get("gfs", 0.0),
                            "ifs": adapt_state.biases.get("ecmwf", 0.0),
                            "aifs": adapt_state.biases.get("ecmwf", 0.0),
                            "icon": 0.0}
            for f, arr in families_arr.items():
                b = float(family_bias.get(f, 0.0))
                if b != 0.0:
                    families_arr[f] = arr - b

        all_max = np.concatenate([a for a in families_arr.values() if len(a)]) \
            if families_arr else np.array([])

        p_all, p_raw, p_gauss, mu, sigma = _prob_for_band(
            all_max, bound, lo_c, hi_c, self.blend_raw_weight,
        )
        # Per-family P, mean, spread, member counts (always computed so
        # we can log them into signal metadata for future verification).
        family_p: dict[str, float] = {}
        family_n: dict[str, int] = {}
        family_means: dict[str, float] = {}
        family_spreads: dict[str, float] = {}
        for f, arr in families_arr.items():
            family_n[f] = int(len(arr))
            if len(arr) >= 1:
                family_p[f] = float(_prob_for_band(
                    arr, bound, lo_c, hi_c, self.blend_raw_weight)[0])
                family_means[f] = float(np.mean(arr))
                family_spreads[f] = float(np.std(arr)) if len(arr) > 1 else 0.0

        # ---------- Adaptive layer: re-blend with family weights ----------
        # If adaptive is OFF or all families are below min_n, the
        # adaptive_for_city returns equal weights AND p_all already
        # reflects the pool-all-members baseline. We keep p_all in that
        # case (cheaper, byte-identical to pre-v2.1).
        if (adapt_state is not None
                and self.adaptive_enabled
                and adapt_state.weights_status != "SHRUNK"):
            ecmwf_count = family_n.get("ifs", 0) + family_n.get("aifs", 0)
            if ecmwf_count > 0:
                ecmwf_all = np.concatenate([
                    families_arr.get(f, np.array([]))
                    for f in ("ifs", "aifs") if f in families_arr
                ])
                p_ecmwf_adj = float(_prob_for_band(
                    ecmwf_all, bound, lo_c, hi_c, self.blend_raw_weight)[0])
            else:
                p_ecmwf_adj = None
            p_gfs_adj = family_p.get("gfs")
            w_gfs = adapt_state.weights.get("gfs", 0.5)
            w_ecmwf = adapt_state.weights.get("ecmwf", 0.5)
            if p_gfs_adj is not None and p_ecmwf_adj is not None:
                p_all = w_gfs * p_gfs_adj + w_ecmwf * p_ecmwf_adj

        # Disagreement gate: only families with enough members count.
        gated_ps = [p for f, p in family_p.items()
                    if family_n[f] >= self.min_family_members]
        confidence = 1.0
        disag = None
        worst_pair = None
        if len(gated_ps) >= 2:
            disag = max(gated_ps) - min(gated_ps)
            # Identify the pair driving disagreement (purely diagnostic).
            sorted_by_p = sorted(
                [(f, p) for f, p in family_p.items()
                 if family_n[f] >= self.min_family_members],
                key=lambda x: x[1])
            worst_pair = f"{sorted_by_p[0][0]}={sorted_by_p[0][1]:.3f} vs " \
                         f"{sorted_by_p[-1][0]}={sorted_by_p[-1][1]:.3f}"
            if disag > self.disagreement_threshold:
                confidence = self.low_confidence

        # Back-compat metadata keys: GFS and ECMWF (= IFS + AIFS pooled).
        # Keeps any older queries / unit tests still working.
        p_gfs = family_p.get("gfs")
        p_ecmwf = None
        ecmwf_count = family_n.get("ifs", 0) + family_n.get("aifs", 0)
        if ecmwf_count > 0:
            ecmwf_all = np.concatenate([
                families_arr.get(f, np.array([]))
                for f in ("ifs", "aifs") if f in families_arr
            ])
            p_ecmwf = float(_prob_for_band(
                ecmwf_all, bound, lo_c, hi_c, self.blend_raw_weight)[0])

        # ---------- Adaptive layer: probability CALIBRATION ----------
        # Logistic shrink toward 0.5 to match observed reliability. Below
        # calib_min_n verifications, alpha=1.0 (identity).
        if adapt_calibration is not None and adapt_calibration.alpha != 1.0:
            from foundation.wx_skill import apply_calibration
            p_all = apply_calibration(p_all, adapt_calibration)

        p_final = float(max(self.clamp_min, min(self.clamp_max, p_all)))

        # Audit blob: the EXACT adaptive parameters that produced p_final.
        # Embedded in signal metadata so any future P&L difference between
        # a trade and a re-evaluation can be attributed to the weights
        # that made it.
        adaptive_audit = None
        if adapt_state is not None:
            adaptive_audit = adapt_state.to_signal_metadata()
            if adapt_calibration is not None:
                adaptive_audit["calibration_alpha"] = adapt_calibration.alpha
                adaptive_audit["calibration_n"] = adapt_calibration.n
                adaptive_audit["calibration_status"] = adapt_calibration.status

        # Forecast-change freshness: wall-clock minutes since the
        # lightweight forecast hash for this city last flipped. Pulled
        # from the cv_state kv populated by `wx-change-watch` in fast.yml.
        # NULL when the watcher hasn't recorded a change for this city
        # yet (e.g. cold start or pre-watcher data).
        mins_since_change = None
        if self._adaptive_ledger is not None:
            try:
                from foundation.forecast_change import (
                    minutes_since_forecast_change)
                mins_since_change = minutes_since_forecast_change(
                    self._adaptive_ledger, city.city)
            except Exception:
                mins_since_change = None

        return Estimate(
            p_final=p_final,
            confidence=confidence,
            metadata={
                "city": city.city,
                "station": city.station_name,
                "kind": kind,
                "lat": city.lat, "lon": city.lon, "timezone": city.timezone,
                "window_local": [start.isoformat(), end.isoformat()],
                "bound": bound, "lo": lo, "hi": hi, "unit": unit,
                "lo_c": lo_c, "hi_c": hi_c,
                # Per-family member counts + Ps (new with multi-family bundle).
                "family_n": family_n,
                "family_p": family_p,
                # New in v2.1: per-family mean + spread so the verifier
                # can compute signed_error / abs_error at settlement.
                "family_means": family_means,
                "family_spreads": family_spreads,
                "n_members": int(len(all_max)),
                # Per-family member counts. n_ecmwf is IFS+AIFS pooled for
                # back-compat with the pre-bundle code; n_ifs and n_aifs
                # break it out separately.
                "n_gfs":   family_n.get("gfs", 0),
                "n_ifs":   family_n.get("ifs", 0),
                "n_aifs":  family_n.get("aifs", 0),
                "n_icon":  family_n.get("icon", 0),
                "n_ecmwf": ecmwf_count,
                "mu_c": mu, "sigma_c": sigma,
                "p_raw": p_raw, "p_gauss": p_gauss, "p_blend": p_all,
                "p_gfs": p_gfs, "p_ecmwf": p_ecmwf,
                "disagreement": disag,
                "disagreement_pair": worst_pair,
                "min_family_members_for_gate": self.min_family_members,
                "elevation_m": fc.elevation_m,
                # Adaptive weights audit (v2.1). None when the kill switch
                # is off or ledger isn't attached.
                "adaptive": adaptive_audit,
                # Forecast-change freshness (v2.4). NULL when no flip has
                # been recorded for this city.
                "minutes_since_forecast_change": mins_since_change,
            },
        )

    # ------------- grading helper -------------------------------------
    def resolve(self, market: Market, settled_at: str) -> dict | None:
        """Strategy-specific truth fetch (sec 3.3). Pulls BOTH the
        Open-Meteo archive and the Wunderground history (the actual
        resolution source for these markets) and cross-checks.

        Returns:
          {
            "outcome": "YES" | "NO" | "DISPUTED",
            "actual_value": truth value in market unit (WU when present),
            "wu_value": Wunderground reading in market unit (or None),
            "om_value": Open-Meteo archive reading in market unit (or None),
            "source_value": "wunderground <station>" or fallback note,
            "wu_source": WU history page URL (or error note),
            "unit", "rounded_val", "wu_rounded_val", "disagreement",
          }

        Returns `None` (the grader treats this as "leave OPEN, retry next
        run") when:
          - the station's LOCAL calendar day for resolve_date hasn't
            ended yet (grader is called with UTC date, but settlement
            authority is local-day -- Wellington at 03:00 NZ is graded
            for "yesterday" UTC), OR
          - the local day HAS ended but neither WU nor OM has a reading
            yet (transient: WU usually publishes within 3-4h; OM archive
            typically lags ~2 days). The next grade will retry.

        Outcome rules:
          - WU has data -> grade on WU's rounded integer. OM disagreement
            (>= dispute_threshold) is logged as `dispute_note` but does
            NOT block settlement (WU is authoritative).
          - WU unavailable but OM is -> grade on OM, note WU error in
            source_value.
        """
        city = _match_city(market, self.cities)
        if city is None or market.resolve_date is None:
            return None
        # Defer to the airport's LOCAL calendar day. A market does not
        # resolve mid-afternoon-local just because UTC has rolled over.
        if not local_day_has_ended(market.resolve_date, city.timezone,
                                    settled_at):
            return None
        kind = market.extras.get("kind") or "max"
        unit = market.extras.get("parsed_unit") or "C"
        bound = market.extras.get("parsed_bound") or "eq"
        lo = market.extras.get("lo")
        hi = market.extras.get("hi")

        # --- Wunderground (authoritative) ---
        wu_val: float | None = None
        wu_rounded: int | None = None
        wu_source = ""
        wu_error: str | None = None
        wu_url = market.extras.get("station_url") or city.resolution_source
        parsed = parse_station_from_url(wu_url) if wu_url else None
        if parsed:
            icao, country = parsed
            wu = self.wu.daily_extreme(icao=icao, country=country,
                                       date_iso=market.resolve_date,
                                       tz=city.timezone, unit=unit, kind=kind)
            wu_source = wu.source_url
            if wu.max_temp is not None:
                wu_val = float(wu.max_temp)
                wu_rounded = math.floor(wu_val + 0.5)
            else:
                wu_error = wu.error
        else:
            wu_error = f"unparseable station URL: {wu_url!r}"

        # --- Open-Meteo archive (cross-check; may not be backfilled
        # yet on fresh days). NOT a hard requirement -- we no longer
        # bail when OM is missing if WU has the data.
        if kind == "min":
            om_c = self.client.archive_min_temp_c(
                city.lat, city.lon, market.resolve_date, city.timezone,
            )
        else:
            om_c = self.client.archive_max_temp_c(
                city.lat, city.lon, market.resolve_date, city.timezone,
            )
        om_val: float | None = None
        om_rounded: int | None = None
        if om_c is not None:
            om_val = c_to_f(om_c) if unit == "F" else float(om_c)
            om_rounded = math.floor(om_val + 0.5)

        # Neither source has data yet -- defer. The local day is over
        # but obs/archive haven't propagated. Next grade run retries.
        if wu_val is None and om_val is None:
            return None

        disagreement = None
        if wu_rounded is not None and om_rounded is not None:
            disagreement = abs(om_rounded - wu_rounded)

        # --- grade. Prefer WU's rounded integer when available, since WU
        # IS the resolution source; fall back to OM when WU was unavailable.
        verdict_val = wu_rounded if wu_rounded is not None else om_rounded
        if bound == "le":
            won = verdict_val <= (hi if hi is not None else verdict_val)
        elif bound == "ge":
            won = verdict_val >= (lo if lo is not None else verdict_val)
        elif bound == "eq":
            won = verdict_val == (lo if lo is not None else verdict_val)
        else:
            won = (
                (lo is None or verdict_val >= lo)
                and (hi is None or verdict_val <= hi)
            )

        if wu_val is not None:
            truth_val = wu_val
            source_value = f"wunderground {city.station_name}"
        else:
            truth_val = float(om_val)
            source_value = (f"open-meteo archive {city.station_name} "
                            f"(wu unavailable: {wu_error})")
        dispute_note = ""
        if disagreement is not None and disagreement >= self.dispute_threshold:
            dispute_note = (
                f"{kind}: Open-Meteo {om_val:.2f} -> {om_rounded}; "
                f"Wunderground {wu_val:.2f} -> {wu_rounded}; "
                f"WU authoritative -> graded {('YES' if won else 'NO')}"
            )
        return {
            "outcome": "YES" if won else "NO",
            "actual_value": float(truth_val),         # truth target (WU when present)
            "om_value": float(om_val) if om_val is not None else None,
            "actual_value_c": float(om_c) if om_c is not None else None,
            "wu_value": wu_val,
            "source_value": source_value,
            "wu_source": wu_source,
            "unit": unit,
            "kind": kind,
            "rounded_val": float(om_rounded) if om_rounded is not None else None,
            "wu_rounded_val": float(wu_rounded) if wu_rounded is not None else None,
            "disagreement": disagreement,
            "dispute_note": dispute_note,
        }


def build(cfg: dict) -> Strategy:
    return WeatherStrategy(cfg)
