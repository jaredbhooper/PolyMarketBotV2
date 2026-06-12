"""WeatherModel v2 — shadow challenger.

Runs alongside the production WeatherStrategy on every cycle. Both
models score the same markets; the live executor uses the champion's
estimate, while this challenger's estimate is logged to shadow_trades
and graded by the same fixed grader. Promotion is gated by config
(default OFF) on >= 75 shadow-graded markets, lower Brier, and
non-negative expectancy delta.

Differences from the production champion:

  1. Multi-model ensemble: same Open-Meteo endpoint, same DEFAULT_MODELS
     bundle (gfs_seamless + ecmwf_ifs025[+aifs] + icon) -- the existing
     ForecastClient already pulls all of them. We just bucket members
     by family and weight families separately.

  2. Per-city, per-model additive bias: rolling mean of
     (actual_value - family_median) over the last bias_window_days
     graded rows for this city. The correction shifts every member of
     that family BEFORE smoothing.

  3. Per-city, per-lead-time learned kernel sigma: from graded history,
     family_abs_error MAE -> Gaussian sigma via MAE * sqrt(pi/2)
     (~1.2533). Floor at min_sigma_c (default 0.3 C). Fallback when
     no data: empirical ensemble member std for this scan.

  4. Per-city family weights: start 50/50 GFS vs ECMWF, update by
     inverse rolling Brier. Other families (ICON, AIFS pooled with IFS)
     get equal-weight residual.

  5. Bucket probability via Gaussian-mixture integration:
       P(bucket) = mean_i [ Phi((hi_cut - m_i)/sigma)
                            - Phi((lo_cut - m_i)/sigma) ]
     where i runs over bias-corrected members, weighted by family.
     This is a non-parametric kernel-smoothed estimate -- smoother than
     the champion's raw member-fraction at thin bucket boundaries.
"""
from __future__ import annotations

import math
from datetime import datetime, timezone
from typing import Any

import numpy as np
from scipy.stats import norm

from strategies.base import Estimate, Market, Strategy
from strategies.weather import (CityCfg, ForecastClient, _load_cities,
                                  _match_city, _local_window_for_date,
                                  c_to_f, f_to_c, family_of,
                                  member_extrema)


# MAE -> Gaussian sigma factor. For X ~ N(0, sigma^2),
# E[|X|] = sigma * sqrt(2/pi)  =>  sigma = MAE * sqrt(pi/2).
MAE_TO_SIGMA = math.sqrt(math.pi / 2.0)


def gaussian_mixture_bucket_prob(members: np.ndarray, sigma: float,
                                    bound: str, lo_cut: float | None,
                                    hi_cut: float | None,
                                    weights: np.ndarray | None = None,
                                    ) -> float:
    """Integral of a Gaussian-kernel mixture over the bucket bounds.

    members:  ndarray of per-member point predictions (already
              bias-corrected, in Celsius).
    sigma:    kernel width (Celsius). Same for every member; per-city
              per-lead-time tuning happens at the call site.
    bound:    'le', 'ge', 'eq', or 'range'.
    lo_cut / hi_cut:
              Bucket edges in Celsius, ALREADY continuity-corrected by
              the caller (i.e. for an "X or below" bucket pass
              hi_cut = X + 0.5). None means open on that side.
    weights:  Optional ndarray of per-member weights summing to 1
              (used for per-family weighting). Defaults to uniform.

    Returns the bucket probability in [0, 1].
    """
    if len(members) == 0:
        return 0.5
    if weights is None:
        weights = np.ones(len(members)) / len(members)
    else:
        weights = np.asarray(weights, dtype=float)
        w_sum = weights.sum()
        if w_sum <= 0:
            return 0.5
        weights = weights / w_sum
    sigma = max(float(sigma), 1e-6)

    def _cdf(cut: float) -> np.ndarray:
        return norm.cdf((cut - members) / sigma)

    if bound == "le":
        # P(max <= hi_cut). lo_cut is -infty.
        if hi_cut is None:
            return 1.0
        return float((weights * _cdf(hi_cut)).sum())
    if bound == "ge":
        if lo_cut is None:
            return 1.0
        return float((weights * (1.0 - _cdf(lo_cut))).sum())
    # 'eq' or 'range'.
    lo = -math.inf if lo_cut is None else lo_cut
    hi = math.inf if hi_cut is None else hi_cut
    lo_part = np.zeros(len(members)) if lo == -math.inf else _cdf(lo)
    hi_part = np.ones(len(members)) if hi == math.inf else _cdf(hi)
    return float((weights * (hi_part - lo_part)).sum())


def _family_of_market(family_keys: dict[str, list[str]]) -> dict[str, np.ndarray]:
    """Group member values arrays by family, given the per-family key list."""
    return family_keys


# ---------------------------------------------------------------- bias
def family_bias_for_city(rows: list[dict], city: str, family: str,
                          window_days: int) -> float:
    """Rolling additive bias = mean(actual_value - family_mean) over
    rows for this city within the window. Returns 0.0 when fewer than 3
    rows are available -- thin data must not move the model."""
    if not rows or not city:
        return 0.0
    today = datetime.now(timezone.utc).date()
    horizon = today.toordinal() - int(window_days)
    mean_key = f"{family}_mean"
    diffs: list[float] = []
    for r in rows:
        if (r.get("city") or "") != city:
            continue
        rd = r.get("resolve_date")
        try:
            d = datetime.fromisoformat((rd or "").split("T")[0]).date()
        except (TypeError, ValueError):
            continue
        if d.toordinal() < horizon:
            continue
        actual = r.get("actual_value")
        mean = r.get(mean_key)
        if actual is None or mean is None:
            continue
        try:
            diffs.append(float(actual) - float(mean))
        except (TypeError, ValueError):
            continue
    if len(diffs) < 3:
        return 0.0
    return float(np.mean(diffs))


# ---------------------------------------------------------------- sigma
def learned_sigma_for(rows: list[dict], city: str, family: str,
                        lead_bucket: int, window_days: int,
                        fallback_spread: float, min_sigma: float) -> float:
    """Per-city, per-family, per-lead-time learned sigma.

    Computes MAE of family_mean vs actual_value over graded rows in
    this city + lead-time-bucket + window, then converts MAE to a
    Gaussian sigma via MAE * sqrt(pi/2). When fewer than 5 rows match,
    fall back to the ensemble's own member spread for this scan.
    """
    if not rows:
        return max(fallback_spread, min_sigma)
    today = datetime.now(timezone.utc).date()
    horizon = today.toordinal() - int(window_days)
    err_key = f"{family}_abs_error"
    errs: list[float] = []
    for r in rows:
        if (r.get("city") or "") != city:
            continue
        rd = r.get("resolve_date")
        try:
            d = datetime.fromisoformat((rd or "").split("T")[0]).date()
        except (TypeError, ValueError):
            continue
        if d.toordinal() < horizon:
            continue
        lt = r.get("lead_time_hours")
        if lt is None:
            continue
        try:
            lt_b = int(float(lt) // 24)
        except (TypeError, ValueError):
            continue
        if lt_b != lead_bucket:
            continue
        ae = r.get(err_key)
        if ae is None:
            continue
        try:
            errs.append(float(ae))
        except (TypeError, ValueError):
            continue
    if len(errs) < 5:
        return max(fallback_spread, min_sigma)
    mae = float(np.mean(errs))
    return max(MAE_TO_SIGMA * mae, min_sigma)


# ---------------------------------------------------------------- weights
def family_weights_for_city(rows: list[dict], city: str,
                              window_days: int) -> dict[str, float]:
    """Per-city family weights, starting 50/50 GFS/ECMWF and updating by
    inverse rolling Brier. Only families with >=5 scored rows update;
    everyone else keeps the equal-share prior.

    Returns a dict {family_key: weight} normalized to sum 1.0 over the
    learned families. Caller decides how to spread weight across the
    raw ensemble keys (gfs vs ifs vs aifs etc.)."""
    if not city or not rows:
        return {"gfs": 0.5, "ecmwf": 0.5}
    today = datetime.now(timezone.utc).date()
    horizon = today.toordinal() - int(window_days)
    by_fam: dict[str, list[float]] = {"gfs": [], "ecmwf": []}
    for r in rows:
        if (r.get("city") or "") != city:
            continue
        rd = r.get("resolve_date")
        try:
            d = datetime.fromisoformat((rd or "").split("T")[0]).date()
        except (TypeError, ValueError):
            continue
        if d.toordinal() < horizon:
            continue
        outcome = r.get("outcome")
        if outcome not in ("YES", "NO"):
            continue
        y = 1.0 if outcome == "YES" else 0.0
        for fam, pkey in (("gfs", "gfs_p_threshold"),
                            ("ecmwf", "ecmwf_p_threshold")):
            p = r.get(pkey)
            if p is None:
                continue
            try:
                by_fam[fam].append((float(p) - y) ** 2)
            except (TypeError, ValueError):
                continue
    inv = {}
    for fam, brs in by_fam.items():
        if len(brs) < 5:
            inv[fam] = 1.0  # equal-share prior
            continue
        b = max(float(np.mean(brs)), 1e-6)
        inv[fam] = 1.0 / b
    total = sum(inv.values()) or 1.0
    return {f: inv[f] / total for f in inv}


# ---------------------------------------------------------------- strategy
class WeatherModelV2(Strategy):
    """Shadow challenger. Same per-market signature as WeatherStrategy."""

    name = "weather_v2"

    def __init__(self, cfg: dict):
        s = (cfg.get("strategies") or {}).get("weather_v2", {})
        # Foundation gating knobs -- same defaults as champion so the
        # head-to-head doesn't bake in a different threshold.
        self.edge_threshold = float(s.get("edge_threshold", 0.08))
        self.kelly_fraction = float(s.get("kelly_fraction", 0.15))
        # v2-specific knobs.
        self.bias_window_days = int(s.get("bias_window_days", 30))
        self.sigma_window_days = int(s.get("sigma_window_days", 60))
        self.weight_window_days = int(s.get("weight_window_days", 30))
        self.min_sigma_c = float(s.get("min_sigma_c", 0.3))
        self.clamp_min = float(s.get("clamp_min", 0.02))
        self.clamp_max = float(s.get("clamp_max", 0.98))
        self.forecast_days = int(s.get("forecast_days", 3))
        # Promotion gate. Default OFF -- v2 stays in shadow.
        self.promotion_enabled = bool(s.get("promotion_enabled", False))
        self.promotion_min_n = int(s.get("promotion_min_n", 75))
        # Reuse the champion's helpers so cities config and ensemble
        # API endpoint stay in one place.
        self.cities = _load_cities(cfg)
        self.client = ForecastClient()
        # Cached per-cycle adaptive state.
        self._ledger = None
        self._wx_rows: list[dict] | None = None

    def attach_ledger(self, ledger) -> None:
        self._ledger = ledger
        self._wx_rows = None

    def _rows(self) -> list[dict]:
        if self._wx_rows is None and self._ledger is not None:
            try:
                self._wx_rows = [dict(r) for r in self._ledger.list_wx_verifications()]
            except Exception:
                self._wx_rows = []
        return self._wx_rows or []

    # ----- Strategy ABC -------------------------------------------------
    def relevant_markets(self, markets: list[Market]) -> list[Market]:
        # Mirror the champion's filter so head-to-head pairs line up.
        from strategies.weather import WeatherStrategy
        # Cheap re-use: walk the same predicate inline rather than
        # constructing a champion just to call relevant_markets.
        out = []
        today_utc = datetime.now(timezone.utc).date()
        for m in markets:
            slug = (m.extras.get("event_slug") or "")
            if not ("highest-temperature" in slug
                    or "lowest-temperature" in slug):
                continue
            if _match_city(m, self.cities) is None:
                continue
            if not m.extras.get("parsed_unit"):
                continue
            if m.resolve_date is None:
                continue
            try:
                d = datetime.fromisoformat(m.resolve_date).date()
            except (TypeError, ValueError):
                continue
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
                              metadata={"error": str(e),
                                          "stage": "v2_ensemble_fetch"})
        try:
            start, end = _local_window_for_date(market.resolve_date,
                                                    city.timezone)
        except (TypeError, ValueError):
            return None
        kind = market.extras.get("kind") or "max"
        extrema = member_extrema(fc, start, end, kind=kind)
        if not extrema:
            return Estimate(p_final=0.5, confidence=0.0,
                              metadata={"error": "no member extrema",
                                          "stage": "v2_extrema"})

        # Group per-family keys + values.
        family_keys: dict[str, list[str]] = {}
        for k in extrema:
            family_keys.setdefault(family_of(k), []).append(k)

        # Map raw OM families -> the two learned-skill super-families:
        #   gfs            -> "gfs"
        #   ifs + aifs     -> "ecmwf"
        #   icon           -> "icon" (no learned bias yet; uncorrected)
        def _super(f: str) -> str:
            if f == "gfs": return "gfs"
            if f in ("ifs", "aifs"): return "ecmwf"
            return f

        rows = self._rows()
        biases: dict[str, float] = {}
        for fam in family_keys:
            sf = _super(fam)
            if sf in biases:
                continue
            biases[sf] = family_bias_for_city(
                rows, city.city, sf, self.bias_window_days)

        # Determine lead-time bucket (full days) for sigma learning.
        try:
            sig_ts = datetime.now(timezone.utc)
            r_dt = datetime.fromisoformat(market.resolve_date).replace(
                tzinfo=timezone.utc)
            lead_h = (r_dt - sig_ts).total_seconds() / 3600.0
            lead_bucket = max(0, int(lead_h // 24))
        except (TypeError, ValueError):
            lead_bucket = 0

        # Apply bias and assemble per-family arrays of bias-corrected
        # member values (in Celsius).
        family_arrays: dict[str, np.ndarray] = {}
        for fam, keys in family_keys.items():
            sf = _super(fam)
            adj = np.array([float(extrema[k]) + biases.get(sf, 0.0)
                              for k in keys])
            family_arrays.setdefault(sf, []).extend(adj.tolist())
        # Convert to arrays once everyone is appended.
        family_arrays = {f: np.array(v) for f, v in family_arrays.items()}

        # Learn per-family sigma at this city + lead bucket. Fallback to
        # this scan's family member std.
        family_sigmas: dict[str, float] = {}
        for sf, arr in family_arrays.items():
            fallback = float(np.std(arr, ddof=1)) if len(arr) > 1 else 0.5
            family_sigmas[sf] = learned_sigma_for(
                rows, city.city, sf, lead_bucket,
                self.sigma_window_days, fallback,
                min_sigma=self.min_sigma_c)

        # Family weights for THIS city.
        weights = family_weights_for_city(rows, city.city,
                                              self.weight_window_days)

        # Bucket bounds with continuity correction (caller side).
        if bound == "le":
            lo_cut, hi_cut = None, hi_c + 0.5
        elif bound == "ge":
            lo_cut, hi_cut = lo_c - 0.5, None
        else:  # eq / range
            lo_cut = (lo_c if lo_c is not None else None)
            hi_cut = (hi_c if hi_c is not None else None)
            if lo_cut is not None:
                lo_cut = lo_cut - 0.5
            if hi_cut is not None:
                hi_cut = hi_cut + 0.5

        # Per-family bucket probabilities, blended by city weights.
        family_p: dict[str, float] = {}
        for sf, arr in family_arrays.items():
            family_p[sf] = gaussian_mixture_bucket_prob(
                arr, family_sigmas[sf], bound, lo_cut, hi_cut)
        # Normalize weights across only the families we actually have
        # (handles cycles where ECMWF or GFS members didn't return).
        present_weights = {f: weights.get(f, 1.0 / max(len(family_p), 1))
                              for f in family_p}
        wsum = sum(present_weights.values()) or 1.0
        p_final = sum(present_weights[f] / wsum * family_p[f]
                        for f in family_p)
        p_final = float(np.clip(p_final, self.clamp_min, self.clamp_max))

        # Family means for the wx_verification cross-check + future
        # bias learning. Per-family medians (better than mean for
        # skewed ensembles).
        family_means = {f: float(np.median(arr))
                          for f, arr in family_arrays.items()}
        family_spreads = {f: float(np.std(arr, ddof=1)) if len(arr) > 1 else 0.0
                            for f, arr in family_arrays.items()}

        return Estimate(
            p_final=p_final,
            confidence=1.0,
            metadata={
                "model": "weather_v2",
                "city": city.city,
                "kind": kind,
                "bound": bound,
                "lo_c": lo_c, "hi_c": hi_c,
                "family_p": family_p,
                "family_means": family_means,
                "family_spreads": family_spreads,
                "family_sigmas": family_sigmas,
                "family_weights": present_weights,
                "biases_c": biases,
                "lead_bucket_days": lead_bucket,
            },
        )


def build(cfg: dict) -> Strategy:
    return WeatherModelV2(cfg)
