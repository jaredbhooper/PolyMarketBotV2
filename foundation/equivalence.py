"""Cross-venue rules-equivalence engine.

For each candidate cross-venue (Polymarket, Kalshi) market pair, this
module classifies the pair into one of three buckets:

  CERTIFIED-IDENTICAL  - same real-world event, same resolution source,
                         same cutoff, same condition. Tradeable.
  FUZZY                - same event but at least one of (source, cutoff,
                         condition) differs. Logged for human review.
                         NEVER auto-traded.
  NON-MATCH            - different events entirely. Not logged as an arb
                         opportunity.

A divergence_risk_note is attached to every CERTIFIED pair where the two
venues use different resolution sources for the same underlying
observation (e.g. Polymarket = Wunderground KORD vs Kalshi = NWS
Climatological Report Chicago Midway). Even when both pages "should"
report the same value, the underlying instruments differ by minutes /
station, so a same-date disagreement is possible and we record the risk
on every detected gap so the executor (and the human) can act on it.
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any


# ---------------------------------------------------------- normalization
_SOURCE_PATTERNS = {
    "wunderground": "wunderground",
    "weather underground": "wunderground",
    "metar": "metar",
    "nws": "nws",
    "national weather service": "nws",
    "noaa": "nws",
    "nws climatological report": "nws_climatological",
    "open-meteo": "open_meteo",
    "open meteo": "open_meteo",
    "accuweather": "accuweather",
}


def normalize_source(text: str) -> str:
    """Map a free-form source string to a canonical short code. Returns
    'unknown' if no canonical match is found."""
    t = (text or "").strip().lower()
    if not t:
        return "unknown"
    # Try longest-first match so 'national weather service' wins over 'nws'.
    keys = sorted(_SOURCE_PATTERNS.keys(), key=len, reverse=True)
    for k in keys:
        if k in t:
            return _SOURCE_PATTERNS[k]
    return "unknown"


def normalize_title(s: str) -> str:
    s = unicodedata.normalize("NFKD", s or "")
    s = s.encode("ascii", "ignore").decode("ascii")
    s = s.lower()
    s = re.sub(r"[^a-z0-9]+", " ", s)
    return " ".join(s.split())


# ---------------------------------------------------------- temperature parsing
_C_TO_F = lambda c: c * 9.0 / 5.0 + 32.0
_F_TO_C = lambda f: (f - 32.0) * 5.0 / 9.0


def parse_bucket(label: str) -> dict | None:
    """Parse a temperature bucket label into a canonical Fahrenheit range.

    Examples:
      '14C'             -> {lo_f: 56.3, hi_f: 58.1, kind: 'eq', label_native: '14C'}
      '13C or below'    -> {lo_f: -inf, hi_f: 56.3, kind: 'le'}
      '32C or higher'   -> {lo_f: 88.7, hi_f: inf, kind: 'ge'}
      '90-91F'          -> {lo_f: 89.5, hi_f: 91.5, kind: 'range'}
      '87 or below'     -> ambiguous unit; defaults to F if used by Kalshi
      '88' or below     -> Kalshi 'or below' style is integer cutoff

    Polymarket uses round-half-up on whole degrees; Kalshi uses
    half-degree-edges (e.g. T88 = "below 88F" = actual < 88.0 round to
    integer; B90.5 = "90 to 91" = 89.5 <= actual < 91.5). We canonicalize
    to F since most US Kalshi weather is F and Polymarket US weather is F.
    """
    t = (label or "").replace("°", "").strip()
    if not t:
        return None
    # Two-number range "X-Y unit?"
    m = re.search(r"(-?\d+(?:\.\d+)?)\s*[-–]\s*(-?\d+(?:\.\d+)?)\s*([CF])?",
                  t, re.IGNORECASE)
    if m:
        a = float(m.group(1)); b = float(m.group(2))
        if a > b:
            a, b = b, a
        unit = (m.group(3) or "").upper() or "F"
        lo_f, hi_f = (a, b + 1.0) if unit == "F" else (_C_TO_F(a), _C_TO_F(b + 1.0))
        # round-half-up integer convention -> the bucket [lo-0.5, hi+0.5)
        if unit == "F":
            lo_f, hi_f = a - 0.5, b + 0.5
        else:
            lo_f, hi_f = _C_TO_F(a - 0.5), _C_TO_F(b + 0.5)
        return {"lo_f": lo_f, "hi_f": hi_f, "kind": "range",
                "label_native": label, "unit": unit, "lo_native": a, "hi_native": b}
    # 'or below' / 'or higher' / 'or above'
    m = re.search(r"(-?\d+(?:\.\d+)?)\s*([CF])?\s*(or\s+(?:below|above|higher|lower|more|less))?",
                  t, re.IGNORECASE)
    if not m:
        return None
    n = float(m.group(1))
    unit = (m.group(2) or "").upper() or "F"
    qual = (m.group(3) or "").lower()
    if "below" in qual or "lower" in qual or "less" in qual:
        # Polymarket "13C or below" -> [-inf, n+0.5)C. Kalshi "87 or below" -> [-inf, 87.5)F.
        hi_f = (n + 0.5) if unit == "F" else _C_TO_F(n + 0.5)
        return {"lo_f": float("-inf"), "hi_f": hi_f, "kind": "le",
                "label_native": label, "unit": unit, "lo_native": None, "hi_native": n}
    if "above" in qual or "higher" in qual or "more" in qual:
        lo_f = (n - 0.5) if unit == "F" else _C_TO_F(n - 0.5)
        return {"lo_f": lo_f, "hi_f": float("inf"), "kind": "ge",
                "label_native": label, "unit": unit, "lo_native": n, "hi_native": None}
    # Single integer = single bucket
    if unit == "F":
        lo_f, hi_f = n - 0.5, n + 0.5
    else:
        lo_f, hi_f = _C_TO_F(n - 0.5), _C_TO_F(n + 0.5)
    return {"lo_f": lo_f, "hi_f": hi_f, "kind": "eq",
            "label_native": label, "unit": unit, "lo_native": n, "hi_native": n}


def buckets_overlap_equivalently(a: dict | None, b: dict | None,
                                   tol_f: float = 0.05) -> bool:
    """Two buckets are CERTIFIED-equivalent iff they cover the same
    half-open Fahrenheit interval to within `tol_f` degrees. Approximate
    equality on the F-edges because C-to-F conversion of a half-degree
    bucket leaves us at 0.55-0.56F instead of an exact width."""
    if a is None or b is None:
        return False
    return (abs(a["lo_f"] - b["lo_f"]) < tol_f
            and abs(a["hi_f"] - b["hi_f"]) < tol_f)


# ---------------------------------------------------------- city / date
# City keywords mapped both ways. The matcher uses these to bucket
# candidate markets before the per-pair classification.
CITY_ALIASES: dict[str, list[str]] = {
    "nyc":           ["nyc", "new york", "new-york", "newyork"],
    "miami":         ["miami"],
    "chicago":       ["chicago"],
    "los_angeles":   ["los angeles", "los-angeles", "la"],
    "houston":       ["houston"],
    "dallas":        ["dallas"],
    "denver":        ["denver"],
    "boston":        ["boston"],
    "philadelphia":  ["philadelphia", "philly"],
    "seattle":       ["seattle"],
    "san_francisco": ["san francisco", "san-francisco", "sf"],
    "las_vegas":     ["las vegas", "las-vegas"],
    "phoenix":       ["phoenix"],
    "atlanta":       ["atlanta"],
    "austin":        ["austin"],
    "minneapolis":   ["minneapolis", "minnesota"],
    "washington_dc": ["washington", " dc ", "washington dc"],
    "new_orleans":   ["new orleans", "nola"],
    "oklahoma_city": ["oklahoma city", "okc"],
    "san_antonio":   ["san antonio", "satx"],
    "death_valley":  ["death valley"],
}


def detect_city(text: str) -> str | None:
    """Return a canonical city key or None."""
    t = (text or "").lower()
    for key, aliases in CITY_ALIASES.items():
        for a in aliases:
            if a in t:
                return key
    return None


_MONTHS = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "sept": 9, "oct": 10, "nov": 11, "dec": 12,
}


def detect_date(text: str) -> str | None:
    """Best-effort YYYY-MM-DD detection from a title.

    Recognises 'Jun 11, 2026', 'Jun-11-2026', '2026-06-11', 'june 11 2026'.
    """
    t = (text or "")
    # ISO date first
    m = re.search(r"(\d{4})-(\d{2})-(\d{2})", t)
    if m:
        return f"{m.group(1)}-{m.group(2)}-{m.group(3)}"
    m = re.search(r"\b(jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec)[a-z]*[-\s]+(\d{1,2})[,\s-]+(\d{4})",
                  t, re.IGNORECASE)
    if m:
        mo = _MONTHS[m.group(1).lower()[:3] if len(m.group(1)) >= 3 else m.group(1).lower()]
        return f"{m.group(3)}-{mo:02d}-{int(m.group(2)):02d}"
    # Kalshi event tickers embed dates: KXHIGHNY-26JUN10 -> 2026-06-10
    m = re.search(r"-(\d{2})([A-Z]{3})(\d{2})\b", t)
    if m:
        yr, mo, day = int(m.group(1)) + 2000, _MONTHS.get(m.group(2).lower(), 0), int(m.group(3))
        if mo:
            return f"{yr:04d}-{mo:02d}-{day:02d}"
    return None


# ---------------------------------------------------------- classification
@dataclass
class EquivalenceResult:
    classification: str          # CERTIFIED-IDENTICAL | FUZZY | NON-MATCH
    reason: str                  # human-readable summary
    criteria: dict[str, Any] = field(default_factory=dict)
    divergence_risk_note: str = ""    # populated for CERTIFIED pairs whose
                                       # sources differ in meaningful ways


def classify_pair(poly: "VenueMarket", kal: "VenueMarket") -> EquivalenceResult:
    """Decide CERTIFIED / FUZZY / NON-MATCH for a single cross-venue
    market pair.

    Comparisons (each yields a boolean + a note):
      1. event - same city + same date + same kind (max/min)
      2. cutoff - close_time within 24h
      3. condition - bucket interval matches in canonical Fahrenheit
      4. source - canonical source code matches

    All four match -> CERTIFIED-IDENTICAL.
    (1) matches but any of (2)(3)(4) fails -> FUZZY.
    (1) fails -> NON-MATCH.

    Even on CERTIFIED, if the two sources are not literally the same
    instrument (e.g. Wunderground at one airport vs NWS Climatological
    Report at a near-but-different station), we attach a divergence note
    so the trader sees the source-divergence risk on every gap.
    """
    crit: dict[str, Any] = {}

    # 1) event identity = city + date + kind
    poly_text = " ".join([poly.title, poly.leg_title, poly.rules_text,
                          poly.extras.get("event_slug") or "",
                          poly.extras.get("event_title") or ""])
    kal_text = " ".join([kal.title, kal.leg_title, kal.rules_text,
                         kal.extras.get("event_title") or "",
                         kal.extras.get("series_title") or "",
                         kal.venue_event_id])
    poly_city = detect_city(poly_text)
    kal_city = detect_city(kal_text)
    crit["city"] = {"poly": poly_city, "kalshi": kal_city,
                    "match": poly_city is not None and poly_city == kal_city}

    poly_date = detect_date(poly_text) or (poly.close_time_iso or "")[:10] or None
    kal_date = detect_date(kal_text) or (kal.close_time_iso or "")[:10] or None
    crit["date"] = {"poly": poly_date, "kalshi": kal_date,
                    "match": poly_date is not None and poly_date == kal_date}

    poly_kind = (poly.extras.get("kind") or "").lower()
    kal_series = (kal.extras.get("series_ticker") or "").lower()
    if "high" in kal_series:
        kal_kind = "max"
    elif "low" in kal_series:
        kal_kind = "min"
    else:
        kal_kind = ""
    crit["kind"] = {"poly": poly_kind, "kalshi": kal_kind,
                    "match": (poly_kind and poly_kind == kal_kind) or (not poly_kind and not kal_kind)}

    event_match = crit["city"]["match"] and crit["date"]["match"] and crit["kind"]["match"]

    if not event_match:
        # Different real-world events - don't even try the other criteria.
        return EquivalenceResult(
            classification="NON-MATCH",
            reason=(f"event identity differs (city {poly_city}/{kal_city}, "
                    f"date {poly_date}/{kal_date}, kind {poly_kind}/{kal_kind})"),
            criteria=crit,
        )

    # Early bucket-overlap check. Two markets on the SAME (city, date,
    # kind) but with disjoint settlement buckets are not even FUZZY
    # equivalents - they're entirely different settlement conditions
    # (e.g. Polymarket "14C" vs Kalshi "70F"). Treat those as NON-MATCH
    # so we don't pile thousands of irrelevant pairs into cv_pairs.
    quick_poly_bucket = parse_bucket(poly.leg_title)
    quick_kal_bucket = parse_bucket(kal.leg_title)
    if quick_poly_bucket and quick_kal_bucket:
        # Buckets DON'T overlap at all in canonical Fahrenheit => different
        # settlement conditions. Different markets.
        lo_max = max(quick_poly_bucket["lo_f"], quick_kal_bucket["lo_f"])
        hi_min = min(quick_poly_bucket["hi_f"], quick_kal_bucket["hi_f"])
        if hi_min <= lo_max:
            return EquivalenceResult(
                classification="NON-MATCH",
                reason=(f"same event but disjoint settlement buckets: "
                        f"poly {poly.leg_title} vs kalshi {kal.leg_title}"),
                criteria={**crit, "condition": {
                    "poly_bucket": quick_poly_bucket,
                    "kalshi_bucket": quick_kal_bucket,
                    "match": False, "overlap": False}},
            )

    # 2) cutoff
    def _parse_iso(s: str | None) -> datetime | None:
        if not s:
            return None
        try:
            return datetime.fromisoformat(s.replace("Z", "+00:00"))
        except (ValueError, TypeError):
            return None
    poly_close = _parse_iso(poly.close_time_iso)
    kal_close = _parse_iso(kal.close_time_iso)
    if poly_close and kal_close:
        diff_h = abs((poly_close - kal_close).total_seconds()) / 3600.0
        cutoff_match = diff_h < 24.0
        crit["cutoff"] = {"poly": str(poly_close), "kalshi": str(kal_close),
                          "delta_hours": round(diff_h, 2), "match": cutoff_match}
    else:
        crit["cutoff"] = {"poly": str(poly_close), "kalshi": str(kal_close),
                          "match": False}

    # 3) condition (bucket)
    poly_bucket = parse_bucket(poly.leg_title)
    kal_bucket = parse_bucket(kal.leg_title)
    bucket_match = buckets_overlap_equivalently(poly_bucket, kal_bucket)
    crit["condition"] = {"poly_bucket": poly_bucket, "kalshi_bucket": kal_bucket,
                         "match": bucket_match}

    # 4) settlement source
    poly_src = normalize_source(poly.settlement_source)
    kal_src = normalize_source(kal.settlement_source)
    source_match = (poly_src == kal_src and poly_src != "unknown")
    crit["source"] = {"poly": poly.settlement_source, "kalshi": kal.settlement_source,
                      "poly_code": poly_src, "kalshi_code": kal_src,
                      "match": source_match}

    all_match = (crit["cutoff"]["match"] and bucket_match and source_match)
    if all_match:
        classification = "CERTIFIED-IDENTICAL"
        reason = "event + cutoff + condition + source all match"
        divergence_note = ""
        # Even when canonical codes match, the underlying station may differ:
        # e.g. Wunderground KORD vs Wunderground LaGuardia.
        if poly.settlement_source_url and kal.settlement_source_url \
                and poly.settlement_source_url != kal.settlement_source_url:
            divergence_note = (
                f"Same source TYPE ({poly_src}) but distinct station URLs: "
                f"poly={poly.settlement_source_url} vs kalshi={kal.settlement_source_url}. "
                "Same-day station-mismatch can flip ~1-in-3 boundary fills.")
    else:
        classification = "FUZZY"
        miss = []
        if not crit["cutoff"]["match"]: miss.append("cutoff")
        if not bucket_match: miss.append("condition")
        if not source_match: miss.append("source")
        reason = f"event matches but {','.join(miss)} differs"
        # Source divergence is the central risk - record it explicitly.
        if not source_match:
            divergence_note = (
                f"Resolution sources differ: poly={poly.settlement_source} "
                f"({poly_src}) vs kalshi={kal.settlement_source} ({kal_src}). "
                "Both legs can lose simultaneously if the two sources "
                "disagree on the same day (Wunderground/METAR airport "
                "observation vs NWS Climatological Report can diverge by "
                "1F on warm-front days).")
        else:
            divergence_note = ""
    return EquivalenceResult(
        classification=classification,
        reason=reason,
        criteria=crit,
        divergence_risk_note=divergence_note,
    )
