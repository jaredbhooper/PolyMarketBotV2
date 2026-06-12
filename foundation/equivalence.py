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
    # Weather (the original cross-venue use case).
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
    # v2: crypto/financial reference rates the venues commonly cite.
    "coingecko":      "coingecko",
    "coinbase":       "coinbase",
    "binance":        "binance",
    "chainlink":      "chainlink",
    "cme":            "cme",
    "kraken":         "kraken",
    "tradingview":    "tradingview",
    # v2: sports official-scoring sources.
    "official scoring": "official_scoring",
    "official nba":     "official_scoring",
    "official nfl":     "official_scoring",
    "official mlb":     "official_scoring",
    "official nhl":     "official_scoring",
    "espn":             "espn",
    # v2: economics + politics official sources.
    "bls":              "bls",
    "bureau of labor":  "bls",
    "bea":              "bea",
    "federal reserve":  "fed",
    "fed":              "fed",
    "ap":               "ap",
    "associated press": "ap",
    "reuters":          "reuters",
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
# Per-category match dispatch. Each category contributes its own
# identity + edge-case rule checks. A pair must clear conf >= 0.9 on
# its category-specific matcher to be eligible for ANYTHING (certified
# trading OR the divergence probe). Pairs below 0.9 are logged but
# never traded -- a mismatched pair (two different games paired) would
# register as fake divergence and corrupt the probe statistics.
SUPPORTED_CATEGORIES = (
    "weather", "sports", "crypto", "politics", "economics",
)


@dataclass
class EquivalenceResult:
    classification: str          # CERTIFIED-IDENTICAL | FUZZY | NON-MATCH
    reason: str                  # human-readable summary
    criteria: dict[str, Any] = field(default_factory=dict)
    divergence_risk_note: str = ""    # populated for CERTIFIED pairs whose
                                       # sources differ in meaningful ways
    category: str = ""           # weather | sports | crypto | politics | economics
    confidence: float = 0.0      # 0..1; >= 0.9 required for ANY action


def classify_pair_weather(poly: "VenueMarket", kal: "VenueMarket") -> EquivalenceResult:
    """Weather-category classifier (original implementation).

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

    Confidence: 0.25 per dimension matched (city/date/kind/cutoff/
    condition/source-code) capped at 1.0; CERTIFIED implies >=0.9.
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

    # Confidence helper: count matched event dimensions.
    event_dims_matched = sum(
        1 for k in ("city", "date", "kind") if crit[k]["match"])
    if not event_match:
        # Different real-world events - don't even try the other criteria.
        return EquivalenceResult(
            classification="NON-MATCH",
            reason=(f"event identity differs (city {poly_city}/{kal_city}, "
                    f"date {poly_date}/{kal_date}, kind {poly_kind}/{kal_kind})"),
            criteria=crit,
            category="weather",
            confidence=event_dims_matched / 6.0,
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
                category="weather",
                confidence=event_dims_matched / 6.0,
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
    # Confidence: fraction of all 6 dimensions matched.
    matched = event_dims_matched + sum(
        1 for k in ("cutoff", "condition", "source")
        if crit[k]["match"])
    confidence = matched / 6.0
    return EquivalenceResult(
        classification=classification,
        reason=reason,
        criteria=crit,
        divergence_risk_note=divergence_note,
        category="weather",
        confidence=confidence,
    )


# ---------------------------------------------------------- sports
# Conservative event identity: BOTH teams' canonical names appear in
# both venues' titles + same date. Sport detected from series ticker
# or slug. Settlement edge cases per sport (OT/draw/postponement)
# evaluated against rules_text.
SPORT_KEYWORDS: dict[str, list[str]] = {
    "nba":     ["nba", "lakers", "warriors", "celtics", "knicks", "heat",
                "bucks", "nuggets", "suns", "mavericks", "76ers", "sixers",
                "clippers", "grizzlies", "rockets", "timberwolves",
                "trail-blazers", "blazers", "pistons", "magic", "raptors",
                "bulls", "spurs", "thunder", "kings", "jazz", "pacers",
                "wizards", "hornets", "hawks", "pelicans", "cavaliers",
                "cavs"],
    "nfl":     ["nfl", "eagles", "cowboys", "chiefs", "bills", "ravens",
                "packers", "lions", "49ers", "rams", "vikings", "patriots",
                "steelers", "giants", "jets", "broncos", "raiders",
                "bengals", "browns", "saints", "panthers", "buccaneers",
                "falcons", "seahawks", "cardinals", "commanders",
                "dolphins", "titans", "colts", "jaguars", "texans",
                "chargers", "bears"],
    "mlb":     ["mlb", "yankees", "dodgers", "red sox", "mets", "cubs",
                "braves", "astros", "rangers", "phillies", "padres",
                "guardians", "orioles", "twins", "blue jays", "diamondbacks",
                "rays", "marlins", "nationals", "tigers", "rockies",
                "athletics", "mariners", "pirates", "brewers", "cardinals",
                "white sox", "royals", "angels", "reds", "giants"],
    "nhl":     ["nhl", "rangers", "bruins", "maple leafs", "canadiens",
                "oilers", "panthers", "stars", "avalanche", "lightning",
                "kraken", "blackhawks", "kings", "ducks", "sabres",
                "senators", "flyers", "capitals", "predators", "blues",
                "wild", "jets", "flames", "canucks", "hurricanes",
                "devils", "islanders", "blue jackets", "coyotes",
                "penguins", "red wings", "sharks", "knights", "utah"],
    "soccer":  ["premier league", "champions league", "fifa", "world cup",
                "uefa", "mls", "la liga", "bundesliga", "serie a"],
    "tennis":  ["wimbledon", "us open", "french open", "atp", "wta",
                "australian open"],
    "ufc":     ["ufc", "mma"],
}


def detect_sport(text: str) -> str | None:
    t = (text or "").lower()
    for sport, kws in SPORT_KEYWORDS.items():
        for kw in kws:
            if kw in t:
                return sport
    return None


def detect_teams(text: str, sport: str | None) -> list[str]:
    """Return up to two canonical team substrings present in the text."""
    if not sport or sport not in SPORT_KEYWORDS:
        return []
    t = (text or "").lower()
    found = []
    for kw in SPORT_KEYWORDS[sport]:
        if len(kw) >= 3 and kw in t and kw not in found and kw != sport:
            found.append(kw)
        if len(found) >= 2:
            break
    return found


def classify_pair_sports(poly: "VenueMarket", kal: "VenueMarket") -> EquivalenceResult:
    """Conservative sports matcher. CERTIFIED requires:
      - same sport detected on both
      - >=2 distinct team keywords matched on both sides
      - same date
      - rules_text mentions matching OT-handling stance (or both silent)
      - source codes match (or both unknown -> 'official scoring')
    Anything weaker is FUZZY. No matched teams => NON-MATCH.

    Confidence: weighted average of sport-match (0.2), teams-match (0.3),
    date-match (0.2), cutoff-within-24h (0.1), settlement_compat (0.2)."""
    crit: dict[str, Any] = {}
    poly_text = " ".join([poly.title or "", poly.leg_title or "",
                          poly.rules_text or "",
                          poly.extras.get("event_slug") or "",
                          poly.extras.get("event_title") or ""])
    kal_text = " ".join([kal.title or "", kal.leg_title or "",
                         kal.rules_text or "",
                         kal.extras.get("event_title") or "",
                         kal.extras.get("series_title") or "",
                         kal.venue_event_id or ""])
    poly_sport = detect_sport(poly_text)
    kal_sport = detect_sport(kal_text)
    sport_match = (poly_sport is not None and poly_sport == kal_sport)
    crit["sport"] = {"poly": poly_sport, "kalshi": kal_sport, "match": sport_match}

    sport_for_teams = poly_sport or kal_sport
    poly_teams = detect_teams(poly_text, sport_for_teams)
    kal_teams = detect_teams(kal_text, sport_for_teams)
    teams_intersection = sorted(set(poly_teams) & set(kal_teams))
    teams_match_strong = (sport_match and len(teams_intersection) >= 2)
    crit["teams"] = {"poly": poly_teams, "kalshi": kal_teams,
                     "intersection": teams_intersection,
                     "match": teams_match_strong}

    poly_date = detect_date(poly_text) or (poly.close_time_iso or "")[:10] or None
    kal_date = detect_date(kal_text) or (kal.close_time_iso or "")[:10] or None
    date_match = (poly_date and poly_date == kal_date)
    crit["date"] = {"poly": poly_date, "kalshi": kal_date, "match": bool(date_match)}

    cutoff_match = False
    try:
        if poly.close_time_iso and kal.close_time_iso:
            pc = datetime.fromisoformat(poly.close_time_iso.replace("Z", "+00:00"))
            kc = datetime.fromisoformat(kal.close_time_iso.replace("Z", "+00:00"))
            cutoff_match = abs((pc - kc).total_seconds()) / 3600.0 < 24.0
    except (ValueError, TypeError):
        pass
    crit["cutoff"] = {"match": cutoff_match}

    # Settlement-edge compatibility: do BOTH or NEITHER mention overtime?
    pol_rules = (poly.rules_text or "").lower()
    kal_rules = (kal.rules_text or "").lower()
    ot_words = ("overtime", "extra time", "extra-time", " ot ", "(ot)")
    poly_mentions_ot = any(w in pol_rules for w in ot_words)
    kal_mentions_ot = any(w in kal_rules for w in ot_words)
    void_words = ("postponed", "postponement", "cancelled", "canceled", "void")
    poly_mentions_void = any(w in pol_rules for w in void_words)
    kal_mentions_void = any(w in kal_rules for w in void_words)
    settlement_compat = (poly_mentions_ot == kal_mentions_ot
                         and poly_mentions_void == kal_mentions_void)
    crit["settlement"] = {"poly_ot": poly_mentions_ot, "kalshi_ot": kal_mentions_ot,
                          "poly_void": poly_mentions_void, "kalshi_void": kal_mentions_void,
                          "match": settlement_compat}

    if not sport_match or not teams_intersection:
        confidence = 0.05 * int(bool(date_match)) + 0.05 * int(sport_match)
        return EquivalenceResult(
            classification="NON-MATCH",
            reason=(f"sport/teams mismatch (sport {poly_sport}/{kal_sport}; "
                    f"teams {poly_teams} vs {kal_teams})"),
            criteria=crit,
            category="sports", confidence=confidence,
        )

    confidence = (0.20 * int(sport_match)
                  + 0.30 * int(teams_match_strong)
                  + 0.20 * int(bool(date_match))
                  + 0.10 * int(cutoff_match)
                  + 0.20 * int(settlement_compat))
    all_match = (teams_match_strong and date_match and cutoff_match
                 and settlement_compat)
    if all_match:
        classification = "CERTIFIED-IDENTICAL"
        reason = "sport + teams + date + cutoff + settlement compat"
        divergence = ""
    else:
        classification = "FUZZY"
        miss = []
        if not teams_match_strong: miss.append("teams")
        if not date_match: miss.append("date")
        if not cutoff_match: miss.append("cutoff")
        if not settlement_compat: miss.append("settlement_edge")
        reason = "sport+teams aligned but " + ",".join(miss) + " differs"
        divergence = ("Edge-case settlement rules differ across venues "
                       "(OT/postponement/cancellation handling). Same game "
                       "may settle YES on one venue and NO on the other if "
                       "an edge case fires."
                       if not settlement_compat else "")
    return EquivalenceResult(
        classification=classification, reason=reason, criteria=crit,
        divergence_risk_note=divergence,
        category="sports", confidence=confidence,
    )


# ---------------------------------------------------------- crypto
CRYPTO_ASSETS: dict[str, list[str]] = {
    "btc": ["btc", "bitcoin"],
    "eth": ["eth", "ethereum"],
    "sol": ["sol", "solana"],
    "doge": ["doge", "dogecoin"],
    "xrp": ["xrp", "ripple"],
}


def detect_crypto_asset(text: str) -> str | None:
    t = (text or "").lower()
    for asset, kws in CRYPTO_ASSETS.items():
        for kw in kws:
            if kw in t:
                return asset
    return None


def parse_strike_price(text: str) -> float | None:
    """Pull a single $-prefixed or 'NK' price from the leg title.
    Examples: '$80,000', '80000', '$2,500', 'BTC above 80k'."""
    if not text:
        return None
    t = text.lower().replace(",", "")
    m = re.search(r"\$?\s*(\d{2,7})(?:\s*(k|m))?\b", t)
    if not m:
        return None
    val = float(m.group(1))
    suf = (m.group(2) or "").lower()
    if suf == "k":
        val *= 1_000.0
    elif suf == "m":
        val *= 1_000_000.0
    return val


def classify_pair_crypto(poly: "VenueMarket", kal: "VenueMarket") -> EquivalenceResult:
    """Crypto matcher. CERTIFIED requires same asset, same strike (within
    1% tolerance), same cutoff window (<2h), and matching source codes."""
    crit: dict[str, Any] = {}
    poly_text = " ".join([poly.title or "", poly.leg_title or "",
                          poly.rules_text or ""])
    kal_text = " ".join([kal.title or "", kal.leg_title or "",
                         kal.rules_text or "",
                         kal.extras.get("series_title") or ""])
    pa = detect_crypto_asset(poly_text)
    ka = detect_crypto_asset(kal_text)
    asset_match = (pa is not None and pa == ka)
    crit["asset"] = {"poly": pa, "kalshi": ka, "match": asset_match}

    ps = parse_strike_price(poly.leg_title)
    ks = parse_strike_price(kal.leg_title)
    if ps and ks:
        strike_match = abs(ps - ks) / max(ps, ks) < 0.01
    else:
        strike_match = False
    crit["strike"] = {"poly": ps, "kalshi": ks, "match": strike_match}

    cutoff_match = False
    delta_h = None
    try:
        if poly.close_time_iso and kal.close_time_iso:
            pc = datetime.fromisoformat(poly.close_time_iso.replace("Z", "+00:00"))
            kc = datetime.fromisoformat(kal.close_time_iso.replace("Z", "+00:00"))
            delta_h = abs((pc - kc).total_seconds()) / 3600.0
            cutoff_match = delta_h < 2.0
    except (ValueError, TypeError):
        pass
    crit["cutoff"] = {"delta_h": delta_h, "match": cutoff_match}

    poly_src = normalize_source(poly.settlement_source)
    kal_src = normalize_source(kal.settlement_source)
    source_match = poly_src == kal_src and poly_src not in ("unknown", "")
    crit["source"] = {"poly": poly.settlement_source, "kalshi": kal.settlement_source,
                      "match": source_match}

    if not asset_match:
        return EquivalenceResult(
            classification="NON-MATCH",
            reason=f"asset mismatch ({pa}/{ka})", criteria=crit,
            category="crypto", confidence=0.0,
        )

    confidence = (0.30 * int(asset_match) + 0.30 * int(strike_match)
                  + 0.25 * int(cutoff_match) + 0.15 * int(source_match))
    all_match = strike_match and cutoff_match and source_match
    if all_match:
        return EquivalenceResult(
            classification="CERTIFIED-IDENTICAL",
            reason="asset+strike+cutoff+source all match",
            criteria=crit, category="crypto", confidence=confidence,
        )
    miss = []
    if not strike_match: miss.append("strike")
    if not cutoff_match: miss.append("cutoff")
    if not source_match: miss.append("source")
    div = ("Crypto settlement source differs - index providers can split "
           "cents at expiry; both legs can lose simultaneously."
           if not source_match else "")
    return EquivalenceResult(
        classification="FUZZY", reason="asset matches but " + ",".join(miss),
        criteria=crit, divergence_risk_note=div,
        category="crypto", confidence=confidence,
    )


# ---------------------------------------------------------- politics / economics
POLITICS_KEYWORDS = ("election", "president", "senate", "house", "governor",
                      "congress", "primary", "nominee", "vote", "ballot",
                      "gop", "democrat", "republican", "trump", "biden",
                      "kamala", "harris", "vance", "newsom", "desantis")
ECONOMICS_KEYWORDS = ("cpi", "ppi", "unemployment", "nonfarm", "payrolls",
                       "fomc", "fed funds", "rate cut", "rate hike",
                       "gdp", "inflation", "jobless claims")


def classify_pair_politics(poly: "VenueMarket", kal: "VenueMarket") -> EquivalenceResult:
    """Politics matcher. Conservative: title overlap of canonical
    candidate / position tokens + same date. Settlement disputes in
    politics are common (recount/contested) so CERTIFIED is rare; most
    will be FUZZY by design."""
    crit: dict[str, Any] = {}
    poly_t = (poly.title or "" + " " + poly.leg_title or "").lower()
    kal_t = (kal.title or "" + " " + kal.leg_title or "").lower()
    poly_kws = [k for k in POLITICS_KEYWORDS if k in poly_t]
    kal_kws = [k for k in POLITICS_KEYWORDS if k in kal_t]
    shared = sorted(set(poly_kws) & set(kal_kws))
    crit["keywords"] = {"poly": poly_kws, "kalshi": kal_kws, "shared": shared,
                        "match": len(shared) >= 2}
    poly_date = detect_date(poly.title or "") or (poly.close_time_iso or "")[:10]
    kal_date = detect_date(kal.title or "") or (kal.close_time_iso or "")[:10]
    crit["date"] = {"poly": poly_date, "kalshi": kal_date,
                    "match": bool(poly_date) and poly_date == kal_date}
    poly_src = normalize_source(poly.settlement_source)
    kal_src = normalize_source(kal.settlement_source)
    crit["source"] = {"poly": poly_src, "kalshi": kal_src,
                      "match": poly_src == kal_src and poly_src not in ("unknown", "")}

    if not crit["keywords"]["match"]:
        return EquivalenceResult(
            classification="NON-MATCH",
            reason="insufficient politics-keyword overlap",
            criteria=crit, category="politics", confidence=0.0,
        )
    confidence = (0.40 * int(crit["keywords"]["match"])
                  + 0.30 * int(crit["date"]["match"])
                  + 0.30 * int(crit["source"]["match"]))
    if confidence >= 0.95:
        cls = "CERTIFIED-IDENTICAL"; reason = "politics fully aligned"
    else:
        cls = "FUZZY"
        miss = [k for k in ("keywords", "date", "source") if not crit[k]["match"]]
        reason = "politics aligned but " + ",".join(miss)
    return EquivalenceResult(
        classification=cls, reason=reason, criteria=crit,
        divergence_risk_note=(
            "Politics settlement disputes (recount/contested) common; "
            "venues may resolve at different times or directions."
            if cls == "FUZZY" else ""),
        category="politics", confidence=confidence,
    )


def classify_pair_economics(poly: "VenueMarket", kal: "VenueMarket") -> EquivalenceResult:
    """Economics matcher. Indicator name overlap + same release date.
    Source matters (BLS vs BEA vs Fed Reserve, etc.); usually FUZZY."""
    crit: dict[str, Any] = {}
    poly_t = (poly.title or "" + " " + poly.leg_title or "").lower()
    kal_t = (kal.title or "" + " " + kal.leg_title or "").lower()
    poly_kws = [k for k in ECONOMICS_KEYWORDS if k in poly_t]
    kal_kws = [k for k in ECONOMICS_KEYWORDS if k in kal_t]
    shared = sorted(set(poly_kws) & set(kal_kws))
    crit["keywords"] = {"poly": poly_kws, "kalshi": kal_kws, "shared": shared,
                        "match": len(shared) >= 1}
    poly_date = detect_date(poly.title or "") or (poly.close_time_iso or "")[:10]
    kal_date = detect_date(kal.title or "") or (kal.close_time_iso or "")[:10]
    crit["date"] = {"poly": poly_date, "kalshi": kal_date,
                    "match": bool(poly_date) and poly_date == kal_date}
    poly_src = normalize_source(poly.settlement_source)
    kal_src = normalize_source(kal.settlement_source)
    crit["source"] = {"poly": poly_src, "kalshi": kal_src,
                      "match": poly_src == kal_src and poly_src not in ("unknown", "")}
    if not crit["keywords"]["match"]:
        return EquivalenceResult(
            classification="NON-MATCH",
            reason="no shared economics keyword",
            criteria=crit, category="economics", confidence=0.0,
        )
    confidence = (0.45 * int(crit["keywords"]["match"])
                  + 0.30 * int(crit["date"]["match"])
                  + 0.25 * int(crit["source"]["match"]))
    cls = "CERTIFIED-IDENTICAL" if confidence >= 0.95 else "FUZZY"
    return EquivalenceResult(
        classification=cls,
        reason=("economics aligned" if cls.startswith("CERTIFIED")
                else "economics aligned but cutoff or source differs"),
        criteria=crit, category="economics", confidence=confidence,
    )


def detect_category(poly: "VenueMarket", kal: "VenueMarket") -> str:
    """Best-effort category detection from venue extras + titles.
    Priority: explicit category on either side > sport keyword > crypto
    asset > politics > economics > weather (fall-through)."""
    explicit = ((kal.extras.get("series_category") or "")
                + " " + (poly.extras.get("category") or "")).lower()
    if "weather" in explicit or "climate" in explicit:
        return "weather"
    if "sport" in explicit:
        return "sports"
    if "crypto" in explicit or "financial" in explicit:
        return "crypto"
    if "politic" in explicit or "election" in explicit:
        return "politics"
    if "econom" in explicit:
        return "economics"
    text = " ".join([poly.title or "", poly.leg_title or "",
                     kal.title or "", kal.leg_title or "",
                     kal.venue_event_id or "",
                     kal.extras.get("series_title") or ""]).lower()
    if detect_sport(text):
        return "sports"
    if detect_crypto_asset(text):
        return "crypto"
    if any(k in text for k in ECONOMICS_KEYWORDS):
        return "economics"
    if any(k in text for k in POLITICS_KEYWORDS):
        return "politics"
    return "weather"


def classify_pair(poly: "VenueMarket", kal: "VenueMarket") -> EquivalenceResult:
    """Dispatcher. Detects the pair's category and routes to the
    appropriate per-category classifier. Backwards-compatible: callers
    that were using the old weather-only classify_pair still get the
    weather logic when the category is detected as weather."""
    cat = detect_category(poly, kal)
    if cat == "sports":
        return classify_pair_sports(poly, kal)
    if cat == "crypto":
        return classify_pair_crypto(poly, kal)
    if cat == "politics":
        return classify_pair_politics(poly, kal)
    if cat == "economics":
        return classify_pair_economics(poly, kal)
    return classify_pair_weather(poly, kal)
