"""Adaptive weather weighting: learn FAMILY weights, per-city BIAS, and
probability CALIBRATION from our own logged resolutions.

CORE PRINCIPLE (never optimize this away):
  We do NOT select or prune individual ensemble members. Members are
  interchangeable samples from a distribution. Per-member "skill" is
  noise; the spread across ALL members is what produces our probability
  estimate. The three things we learn and adapt are:
    (a) MODEL-FAMILY weights per city (GFS group vs ECMWF group)
    (b) per-city BIAS corrections (rolling mean signed error per family)
    (c) probability CALIBRATION toward observed frequencies

All three layers shrink toward neutral priors so thin data cannot
dominate:
    skill ----- (n / (n + shrink_n)) ----+
                                          +-- effective parameter
    equal ----- (shrink_n / (n + shrink_n))

Below `min_n` resolutions per city, weights stay exactly equal, biases
stay exactly zero. Below `calib_min_n` overall, calibration is identity.

This module is pure functions; no I/O. The caller is responsible for
fetching verification rows from the ledger and passing them in.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable

# Two families we adapt. AIFS pools into ecmwf.
FAMILIES = ("gfs", "ecmwf")


@dataclass
class AdaptiveConfig:
    enabled: bool = True
    window_days: int = 60
    shrink_n: int = 20       # k in the shrinkage formula
    min_n: int = 10          # below this per-city, force equal weights / zero bias
    calib_min_n: int = 30    # below this overall, calibration is identity
    max_bias_correction: float = 1.5   # absolute clip on per-family per-city bias (native units)
    epsilon_mae: float = 0.05         # avoids 1/0 in weight = 1/MAE


@dataclass
class FamilySkill:
    family: str
    n: int = 0
    mae: float = 0.0
    signed_bias: float = 0.0
    brier: float = 0.0


@dataclass
class CityAdaptive:
    """The applied adaptive state for one city. Inspectable; logged into
    every weather trade's signal metadata for audit."""
    city: str
    n_resolutions: int
    weights: dict[str, float] = field(default_factory=dict)
    biases: dict[str, float] = field(default_factory=dict)
    skill: dict[str, FamilySkill] = field(default_factory=dict)
    weights_status: str = "SHRUNK"     # SHRUNK | BLENDED | ACTIVE
    biases_status: str = "SHRUNK"

    def to_signal_metadata(self) -> dict[str, Any]:
        """Audit blob; embedded in each trade's signal metadata so any
        future P&L difference can be attributed to the weights that
        produced it."""
        return {
            "n_resolutions": self.n_resolutions,
            "weights": dict(self.weights),
            "biases": dict(self.biases),
            "weights_status": self.weights_status,
            "biases_status": self.biases_status,
        }


@dataclass
class CalibrationParam:
    """Logistic shrinkage: p_cal = sigmoid(alpha * logit(p_pred)).
    alpha = 1.0 is identity; 0 < alpha < 1 is shrink toward 0.5.
    alpha is selected by closed-form MLE on the verification set, then
    clipped to [0.25, 1.0] so a tiny adversarial dataset can't flip the
    model on its head.
    """
    alpha: float = 1.0
    n: int = 0
    status: str = "IDENTITY"     # IDENTITY | FIT


# ---------------------------------------------------------------- helpers
def _within_window(row, since_iso: str) -> bool:
    rd = row["resolve_date"] if hasattr(row, "keys") else row.get("resolve_date")
    if not rd:
        return False
    return str(rd)[:10] >= since_iso[:10]


def _since_window_iso(window_days: int, now: datetime | None = None) -> str:
    now = now or datetime.now(timezone.utc)
    return (now - timedelta(days=int(window_days))).date().isoformat()


def _shrink(skill: float, prior: float, n: int, shrink_n: int) -> float:
    if n <= 0:
        return prior
    w_data = n / (n + max(1, shrink_n))
    return w_data * skill + (1.0 - w_data) * prior


def _classify_status(n: int, min_n: int, shrink_n: int) -> str:
    if n < min_n:
        return "SHRUNK"
    if n < 3 * shrink_n:
        return "BLENDED"
    return "ACTIVE"


# ---------------------------------------------------------------- skill
def family_skill(rows: Iterable[dict], family: str) -> FamilySkill:
    """Compute MAE, signed bias, and Brier of the family's own
    P(threshold) over the rows that have non-null values for this
    family. Rows with missing per-family data are silently skipped -
    they don't downgrade skill; we just don't count them."""
    mae_sum = 0.0
    bias_sum = 0.0
    brier_sum = 0.0
    n = 0
    err_key = f"{family}_abs_error"
    signed_key = f"{family}_signed_error"
    pthresh_key = f"{family}_p_threshold"
    for r in rows:
        get = (r.get if hasattr(r, "get") else lambda k, d=None: r[k] if k in r.keys() else d)
        ae = get(err_key)
        se = get(signed_key)
        pth = get(pthresh_key)
        outcome = get("outcome")
        if ae is None or se is None:
            continue
        try:
            ae_f = float(ae)
            se_f = float(se)
        except (TypeError, ValueError):
            continue
        n += 1
        mae_sum += ae_f
        bias_sum += se_f
        if pth is not None and outcome in ("YES", "NO"):
            y = 1.0 if outcome == "YES" else 0.0
            try:
                brier_sum += (float(pth) - y) ** 2
            except (TypeError, ValueError):
                pass
    if n == 0:
        return FamilySkill(family=family)
    return FamilySkill(
        family=family, n=n,
        mae=mae_sum / n,
        signed_bias=bias_sum / n,
        brier=brier_sum / n if n > 0 else 0.0,
    )


def adaptive_for_city(rows_all: list, city: str | None,
                        cfg: AdaptiveConfig,
                        now: datetime | None = None) -> CityAdaptive:
    """Compute (weights, biases, statuses) for one city - or for OVERALL
    when city is None. Rows are filtered by the rolling window."""
    since = _since_window_iso(cfg.window_days, now)
    pool = [r for r in rows_all
            if (city is None or (
                (r.get("city") if hasattr(r, "get") else r["city"]) == city))
            and _within_window(r, since)]
    sk: dict[str, FamilySkill] = {f: family_skill(pool, f) for f in FAMILIES}
    n_city = max(s.n for s in sk.values()) if sk else 0

    # Weights from skill: w_f ∝ 1 / (MAE + epsilon). Below min_n use equal.
    if n_city < cfg.min_n:
        weights = {f: 0.5 for f in FAMILIES}
        biases = {f: 0.0 for f in FAMILIES}
        return CityAdaptive(
            city=city or "OVERALL", n_resolutions=n_city,
            weights=weights, biases=biases, skill=sk,
            weights_status="SHRUNK", biases_status="SHRUNK",
        )

    # Skill-driven weights (only families with data; if both have 0 n,
    # we'd have hit the min_n branch above, so at least one is non-zero).
    raw = {}
    for f in FAMILIES:
        s = sk[f]
        if s.n == 0:
            raw[f] = 0.0
        else:
            raw[f] = 1.0 / (s.mae + cfg.epsilon_mae)
    raw_sum = sum(raw.values()) or 1.0
    skill_weight = {f: raw[f] / raw_sum for f in FAMILIES}
    # Shrink each toward equal (0.5).
    weights = {}
    for f in FAMILIES:
        w = _shrink(skill_weight[f], 0.5, sk[f].n, cfg.shrink_n)
        weights[f] = w
    # Renormalize (numerical drift safety).
    s = sum(weights.values()) or 1.0
    weights = {f: w / s for f, w in weights.items()}

    # Biases per family, shrunk toward zero, clipped to ±max.
    biases = {}
    for f in FAMILIES:
        s_f = sk[f]
        bias = _shrink(s_f.signed_bias, 0.0, s_f.n, cfg.shrink_n)
        bias = max(-cfg.max_bias_correction,
                    min(cfg.max_bias_correction, bias))
        biases[f] = bias

    return CityAdaptive(
        city=city or "OVERALL", n_resolutions=n_city,
        weights=weights, biases=biases, skill=sk,
        weights_status=_classify_status(n_city, cfg.min_n, cfg.shrink_n),
        biases_status=_classify_status(n_city, cfg.min_n, cfg.shrink_n),
    )


# ---------------------------------------------------------------- calibration
def fit_calibration(rows_all: list, cfg: AdaptiveConfig,
                      now: datetime | None = None) -> CalibrationParam:
    """One-parameter logistic calibration on the BLENDED P_final column.

    p_cal = sigmoid(alpha * logit(p_pred))

    Below `calib_min_n` resolutions, returns alpha=1.0 (identity). Above
    we MLE-fit alpha and clip to [0.25, 1.0] so a tiny set with one bad
    week can't invert calibration.

    The fit uses a coarse grid search on alpha in [0.25, 1.5] then a
    refinement; the log-likelihood is concave-ish in alpha for this
    sigmoid family, and the closed-form Newton step is comparable in
    accuracy on a few-dozen points.
    """
    since = _since_window_iso(cfg.window_days, now)
    n = 0
    pts: list[tuple[float, float]] = []
    for r in rows_all:
        if not _within_window(r, since):
            continue
        outcome = r.get("outcome") if hasattr(r, "get") else r["outcome"]
        p_blend = r.get("p_blended") if hasattr(r, "get") else r["p_blended"]
        if outcome not in ("YES", "NO") or p_blend is None:
            continue
        try:
            p = float(p_blend)
        except (TypeError, ValueError):
            continue
        p = max(1e-4, min(1 - 1e-4, p))
        y = 1.0 if outcome == "YES" else 0.0
        pts.append((p, y))
        n += 1
    if n < cfg.calib_min_n:
        return CalibrationParam(alpha=1.0, n=n, status="IDENTITY")

    def loglik(alpha: float) -> float:
        ll = 0.0
        for p, y in pts:
            lp = math.log(p / (1.0 - p))
            cal = 1.0 / (1.0 + math.exp(-alpha * lp))
            cal = max(1e-9, min(1 - 1e-9, cal))
            ll += y * math.log(cal) + (1.0 - y) * math.log(1.0 - cal)
        return ll

    best_alpha = 1.0
    best_ll = loglik(1.0)
    for step in (0.05,):
        for k in range(int((1.5 - 0.25) / step) + 1):
            a = 0.25 + k * step
            ll = loglik(a)
            if ll > best_ll:
                best_ll = ll
                best_alpha = a
    best_alpha = max(0.25, min(1.0, best_alpha))
    return CalibrationParam(alpha=best_alpha, n=n, status="FIT")


def apply_calibration(p: float, cal: CalibrationParam) -> float:
    if cal.alpha == 1.0:
        return p
    p = max(1e-9, min(1 - 1e-9, p))
    lp = math.log(p / (1.0 - p))
    return 1.0 / (1.0 + math.exp(-cal.alpha * lp))


# ---------------------------------------------------------------- dispute forensics
@dataclass
class DisputeStation:
    station: str
    n: int
    mean_om_minus_wu: float
    std_om_minus_wu: float
    samples: list[float] = field(default_factory=list)


def dispute_forensics(rows_all: Iterable[dict]) -> dict[str, DisputeStation]:
    """Group rows by station and compute (OM − WU) distribution. A
    near-zero std with a non-zero mean indicates a SYSTEMATIC OFFSET per
    station (likely fixable by bias correction or settlement source
    config); a large std indicates real-time SOURCE DISAGREEMENT."""
    by_station: dict[str, list[float]] = {}
    for r in rows_all:
        get = (r.get if hasattr(r, "get") else lambda k, d=None: r[k] if k in r.keys() else d)
        station = get("station") or "?"
        om = get("om_value")
        wu = get("wu_value")
        if om is None or wu is None:
            continue
        try:
            d = float(om) - float(wu)
        except (TypeError, ValueError):
            continue
        by_station.setdefault(station, []).append(d)
    out: dict[str, DisputeStation] = {}
    for st, ds in by_station.items():
        n = len(ds)
        mean = sum(ds) / n if n else 0.0
        var = sum((d - mean) ** 2 for d in ds) / n if n else 0.0
        out[st] = DisputeStation(
            station=st, n=n,
            mean_om_minus_wu=mean,
            std_om_minus_wu=math.sqrt(var),
            samples=ds,
        )
    return out
